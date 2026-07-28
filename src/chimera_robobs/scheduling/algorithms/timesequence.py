# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2014-present chimera-robobs authors

"""Time-sequence scheduling algorithm (id 4, name TIMESEQUENCE).

Provides time monitoring on targets: unlike :class:`Higher`, a selected
target stays in the candidate pool, so it is scheduled again in the next
slot while it remains the highest in the sky — building a monitoring
sequence.  Blocks are never marked observed/completed, so they can go back
to the queue as long as they are the most suitable ones.
"""

import logging

from chimera_robobs.scheduling.algorithms.higher import Higher

log = logging.getLogger(__name__)


class TimeSequence(Higher):
    id = 4
    name = "TIMESEQUENCE"
    # Pair to pin_start_time below. Unpinning alone only MOVES the wait:
    # the chimera scheduler stops holding the program until slew_at, and
    # robobs holds it instead (it may not hand an unpinned program over
    # early). The slot times are still spaced by the estimated block
    # length, so the idle survives - 337 s per visit on opd-40 2026-07-28,
    # exactly slot_len minus the real block duration.
    #
    # False lets the engine re-time the visit to the earliest instant that
    # passes check_conditions (engine.reschedule), which is the safe way to
    # start early: the conditions are re-evaluated AT the earlier time
    # rather than assumed from the slot's.
    timed_constraint = False

    keep_selected_target = True
    check_end_airmass = False

    # A monitoring sequence has no meaningful per-visit start time: the
    # slots exist only so the allocator can hand out N visits across the
    # night. Pinning them made every visit wait for its nominal slot, so
    # the difference between the estimated block length and the real one
    # became dead sky - 8.6 min per 25 min slot on opd-40 2026-07-27,
    # where 30 x 30 s took 985 s against a 1260 s estimate. Unpinned, each
    # visit starts when the previous ends, whatever the slew and dome
    # actually cost.
    pin_start_time = False

    def observed(self, time, program, soft=False):
        """Never marks a block as observed, so it can go back to the queue
        as long as it is the most suitable one."""
        session = self.session()
        try:
            prog = session.merge(program[0])
            prog.finished = True
            block = session.merge(program[2])
            if not soft:
                block.last_observation = self.site.ut().replace(tzinfo=None)
        finally:
            session.commit()
