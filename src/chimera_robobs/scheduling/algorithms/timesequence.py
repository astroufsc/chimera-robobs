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
    timed_constraint = True

    keep_selected_target = True
    check_end_airmass = False

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
