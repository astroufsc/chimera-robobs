# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2014-present chimera-robobs authors

"""robobs scheduling algorithms.

The algorithm ids are stored in the database (``blockpar.sched_algorithm``)
and must not change.  Use :func:`build_algorithms` to obtain the id-keyed
registry of algorithm instances.
"""

from sqlalchemy.orm import sessionmaker

from chimera_robobs.scheduling.algorithms.base import BaseScheduleAlgorithm
from chimera_robobs.scheduling.algorithms.extinctionmonitor import ExtinctionMonitor
from chimera_robobs.scheduling.algorithms.higher import Higher
from chimera_robobs.scheduling.algorithms.recurrent import Recurrent
from chimera_robobs.scheduling.algorithms.timed import Timed
from chimera_robobs.scheduling.algorithms.timesequence import TimeSequence

#: the scheduling algorithm classes, in database-id order
ALGORITHM_CLASSES = (Higher, ExtinctionMonitor, Timed, Recurrent, TimeSequence)


def build_algorithms(
    session_factory: sessionmaker, site=None
) -> dict[int, BaseScheduleAlgorithm]:
    """Build the scheduling-algorithm registry keyed by database id.

    :param session_factory: robobs database session factory
        (see :func:`chimera_robobs.scheduling.model.open_database`).
    :param site: site adapter; required by the algorithms that compute
        target altitudes (``process``/``next``/``observed``), optional for
        purely database-side operations (``add``/``clean``/``soft_clean``).
    """
    return {cls.id: cls(session_factory, site) for cls in ALGORITHM_CLASSES}


__all__ = [
    "ALGORITHM_CLASSES",
    "BaseScheduleAlgorithm",
    "ExtinctionMonitor",
    "Higher",
    "Recurrent",
    "Timed",
    "TimeSequence",
    "build_algorithms",
]
