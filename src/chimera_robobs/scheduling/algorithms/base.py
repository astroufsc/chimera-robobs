# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2014-present chimera-robobs authors

"""Base class and shared helpers for the robobs scheduling algorithms.

Algorithms are instances constructed with the robobs session factory (and,
when available, a site adapter) via
:func:`chimera_robobs.scheduling.algorithms.build_algorithms` — there are no
module-level globals to configure.
"""

import logging

from chimera.core.exceptions import ChimeraException
from sqlalchemy.orm import sessionmaker

log = logging.getLogger(__name__)

#: seconds added to a derived slot length to cover the slew and dome sync
#: between two blocks (they are not predictable; this is a floor, and an
#: unpinned algorithm does not depend on it being right - see
#: Algorithm.pin_start_time)
SLOT_SLEW_ALLOWANCE = 60.0


class ExtinctionMonitorError(ChimeraException):
    pass


class TimedError(ChimeraException):
    pass


class RecurrentError(ChimeraException):
    pass


class BaseScheduleAlgorithm:
    """Contract shared by all scheduling algorithms.

    The :attr:`id`/:attr:`name` values are stored in the database
    (``blockpar.sched_algorithm``) and must not change:
    0 Higher/HIG, 1 ExtinctionMonitor/STD, 2 Timed/TIMED,
    3 Recurrent/RECURRENT, 4 TimeSequence/TIMESEQUENCE, 5 SkyFlat/SKYFLAT.
    """

    id: int = -1
    name: str = "BASE"

    #: default slot length in seconds when neither the caller nor the
    #: pid-config ``slotLen`` key provides one
    default_slot_len: float = 60.0

    #: whether the program's ``slew_at`` is a hard constraint (the engine
    #: only searches for an earlier feasible start when this is False)
    timed_constraint: bool = True

    #: whether ``slew_at`` must be copied to the chimera program's
    #: ``start_at``, i.e. enforced by the chimera scheduler as a "do not
    #: start before" barrier.
    #:
    #: True for anything whose time carries meaning (a focus cadence, an
    #: occultation, a twilight flat).  False for algorithms whose slot
    #: times are only an artefact of laying blocks out across the night:
    #: there the barrier turns every difference between the ESTIMATED and
    #: the real block duration into dead time, because slew and dome-sync
    #: costs are not predictable.  A program from an unpinned algorithm is
    #: handed to the scheduler only once it is actually due, and then runs
    #: as soon as the telescope is free (see RobObs._handle_scheduler_idle).
    pin_start_time: bool = True

    #: twilight-calibration programs (sky flats) run outside the -18 deg
    #: night window on a placeholder target: the engine skips its night /
    #: airmass / moon checks for them (the sky-flat controller enforces its
    #: own sun-altitude window)
    twilight_calibration: bool = False

    def __init__(self, session_factory: sessionmaker, site=None):
        self.session = session_factory
        self.site = site

    def process(self, *, obs_start, obs_end, query, config=None, slot_len=None):
        """Build the observing queue (slots) for this algorithm.

        :param obs_start: start of the observing window (JD).
        :param obs_end: end of the observing window (JD).
        :param query: ``(ObsBlock, BlockPar, Target)`` row query.
        :param config: parsed pid-config mapping (a ``slot_len`` key
            overrides the ``slot_len`` argument).
        :param slot_len: slot length in seconds.
        """
        raise NotImplementedError()

    def next(self, now_mjd, programs, check=None):
        """Select the next program to observe with this algorithm.

        :param check: optional condition checker
            ``check(program_row, mjd, program_length) -> bool`` supplied by
            the engine; algorithms that reschedule a program to a different
            time can use it to skip candidates that would not be observable
            there.
        """
        raise NotImplementedError()

    def committed(self, program):
        """The engine COMMITTED to this program (it will be handed over).

        next() must stay side-effect free: the engine polls every priority
        queue while choosing, and an algorithm that consumes state on a
        mere offer loses one entry per poll it does not win (2026-07-22:
        six focus occurrences eaten in six reschedules, none executed).
        """

    def uncommitted(self, program):
        """A committed program was removed from the scheduler before running.

        Inverse of :meth:`committed`: whatever state the commit consumed
        must be released so the entry can be offered again (a stop used to
        eat the night's timed occurrences permanently). ``program`` is the
        robobs Program ROW, not the 4-tuple - the caller recovers programs
        straight from the database.
        """

    def observed(self, time, program, soft=False):
        """Process a program as observed."""

    def in_twilight_window(self, time: float) -> bool:
        """Whether a twilight-calibration program is worth starting at ``time``.

        Only consulted for :attr:`twilight_calibration` algorithms. It is a
        COARSE gate: the precise sun-altitude window belongs to the sky-flat
        controller. Without it the engine waived every condition at any hour,
        so flats were scheduled in the middle of the night, the telescope
        slewed to the flat position, and the controller only then declined.
        """
        return True

    def is_hard_timed(self, program) -> bool:
        """Whether ``program``'s scheduled time is immovable.

        The engine may delay an ordinary timed program (e.g. a focus run
        slips past a long block; ``expire_overdue`` absorbs the backlog),
        but a hard-timed one (an occultation bound to its instant) must
        never be scheduled behind a block that ends after its ``slew_at``.
        """
        return False

    def add(self, block):
        """Process a block being added to the queue."""

    def clean(self, pid):
        """Hard clean: wipe all algorithm bookkeeping for a project."""

    def soft_clean(self, pid, block=None):
        """Soft clean: erase only information about past observations."""

    def _slot_len(self, config, slot_len, blocks=None):
        """Resolve the slot length, in seconds.

        Order: the pid-config ``slot_len`` wins, then the caller's
        ``slot_len``, then a value DERIVED from the blocks being scheduled,
        then the per-algorithm default.

        The derived value is the longest block in the set plus
        :data:`SLOT_SLEW_ALLOWANCE`.  It exists so a project need not
        restate a number the database already knows: ``obsblock.length`` is
        computed from the block's own actions at ingest.  Getting it wrong
        by hand is expensive - a slot longer than its block is dead sky on
        every visit (opd-40 2026-07-27: 1500 s configured against a 985 s
        block, 8.6 min idle per visit).
        """
        if config and "slot_len" in config:
            return float(config["slot_len"])
        if slot_len is not None:
            return float(slot_len)
        derived = self._derived_slot_len(blocks)
        if derived is not None:
            log.info(
                "%s: no slot_len configured; using %.0f s derived from the "
                "longest block (+%.0f s for the slew)",
                self.name,
                derived,
                SLOT_SLEW_ALLOWANCE,
            )
            return derived
        return self.default_slot_len

    @staticmethod
    def _derived_slot_len(blocks) -> float | None:
        """Longest stored block length + slew allowance, or None."""
        if not blocks:
            return None
        lengths = [
            float(block.length) for block in blocks if getattr(block, "length", None)
        ]
        if not lengths:
            return None
        return max(lengths) + SLOT_SLEW_ALLOWANCE
