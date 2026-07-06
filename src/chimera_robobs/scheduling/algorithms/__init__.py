# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2014-present chimera-robobs authors

"""robobs scheduling algorithms.

The algorithm ids are stored in the database (``blockpar.sched_algorithm``)
and must not change.
"""

from chimera_robobs.scheduling.algorithms.base import (
    BaseScheduleAlgorith,
    configure,
)
from chimera_robobs.scheduling.algorithms.extinctionmonitor import ExtintionMonitor
from chimera_robobs.scheduling.algorithms.higher import Higher
from chimera_robobs.scheduling.algorithms.recurrent import Recurrent
from chimera_robobs.scheduling.algorithms.timed import Timed
from chimera_robobs.scheduling.algorithms.timesequence import TimeSequence

#: scheduling algorithms keyed by their database id
ALGORITHMS: dict[int, type[BaseScheduleAlgorith]] = {
    cls.id(): cls
    for cls in (Higher, ExtintionMonitor, Timed, Recurrent, TimeSequence)
}

__all__ = [
    "ALGORITHMS",
    "BaseScheduleAlgorith",
    "ExtintionMonitor",
    "Higher",
    "Recurrent",
    "Timed",
    "TimeSequence",
    "configure",
]
