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

#: canonical YAML names for the algorithms (supervisor-style readable
#: strings), keyed by database id
ALGORITHM_YAML_NAMES = {
    0: "higher",
    1: "extinction_monitor",
    2: "timed",
    3: "recurrent",
    4: "time_sequence",
}

#: every accepted spelling (lowercase, ``-``/`` `` folded to ``_``) -> id:
#: the canonical YAML names plus the persisted short names (HIG, STD, ...)
_ALGORITHM_ALIASES = {name: aid for aid, name in ALGORITHM_YAML_NAMES.items()} | {
    cls.name.lower(): cls.id for cls in ALGORITHM_CLASSES
}


def parse_algorithm_id(value) -> int:
    """Parse a ``scheduling_algorithm`` YAML value into the database id.

    Accepts the canonical names (``higher``, ``extinction_monitor``,
    ``timed``, ``recurrent``, ``time_sequence``), the persisted short names
    (``HIG``, ``STD``, ...) case-insensitively, and the legacy numeric ids.
    """
    if isinstance(value, bool):
        raise ValueError(f"invalid scheduling algorithm: {value!r}")
    if isinstance(value, int | float):
        aid = int(value)
    else:
        text = str(value).strip().lower().replace("-", "_").replace(" ", "_")
        if text.lstrip("-").isdigit():
            aid = int(text)
        else:
            try:
                return _ALGORITHM_ALIASES[text]
            except KeyError:
                raise ValueError(
                    f"unknown scheduling algorithm {value!r} "
                    f"(use one of: {', '.join(sorted(_ALGORITHM_ALIASES))})"
                ) from None
    if aid not in ALGORITHM_YAML_NAMES:
        raise ValueError(
            f"unknown scheduling algorithm id {aid} "
            f"(known ids: {sorted(ALGORITHM_YAML_NAMES)})"
        )
    return aid


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
    "ALGORITHM_YAML_NAMES",
    "BaseScheduleAlgorithm",
    "ExtinctionMonitor",
    "Higher",
    "Recurrent",
    "Timed",
    "TimeSequence",
    "build_algorithms",
    "parse_algorithm_id",
]
