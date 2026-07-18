# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2014-present chimera-robobs authors

"""Base class and shared helpers for the robobs scheduling algorithms.

Algorithms are instances constructed with the robobs session factory (and,
when available, a site adapter) via
:func:`chimera_robobs.scheduling.algorithms.build_algorithms` — there are no
module-level globals to configure.
"""

import math

from chimera.core.exceptions import ChimeraException
from sqlalchemy.orm import sessionmaker


class ExtinctionMonitorError(ChimeraException):
    pass


class TimedError(ChimeraException):
    pass


class RecurrentError(ChimeraException):
    pass


def airmass(alt: float) -> float:
    """Plane-parallel airmass for an altitude in degrees (999 below horizon)."""
    am = 1.0 / math.cos(math.pi / 2.0 - math.radians(alt))
    if am < 0.0:
        am = 999.0
    return am


class BaseScheduleAlgorithm:
    """Contract shared by all scheduling algorithms.

    The :attr:`id`/:attr:`name` values are stored in the database
    (``blockpar.sched_algorithm``) and must not change:
    0 Higher/HIG, 1 ExtinctionMonitor/STD, 2 Timed/TIMED,
    3 Recurrent/RECURRENT, 4 TimeSequence/TIMESEQUENCE.
    """

    id: int = -1
    name: str = "BASE"

    #: default slot length in seconds when neither the caller nor the
    #: pid-config ``slotLen`` key provides one
    default_slot_len: float = 60.0

    #: whether the program's ``slew_at`` is a hard constraint (the engine
    #: only searches for an earlier feasible start when this is False)
    timed_constraint: bool = True

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

    def next(self, now_mjd, programs):
        """Select the next program to observe with this algorithm."""
        raise NotImplementedError()

    def observed(self, time, program, soft=False):
        """Process a program as observed."""

    def add(self, block):
        """Process a block being added to the queue."""

    def clean(self, pid):
        """Hard clean: wipe all algorithm bookkeeping for a project."""

    def soft_clean(self, pid, block=None):
        """Soft clean: erase only information about past observations."""

    def _slot_len(self, config, slot_len):
        """Resolve the slot length: the pid-config ``slot_len`` wins, then
        the caller's ``slot_len``, then the per-algorithm default."""
        if config and "slot_len" in config:
            return float(config["slot_len"])
        if slot_len is not None:
            return float(slot_len)
        return self.default_slot_len
