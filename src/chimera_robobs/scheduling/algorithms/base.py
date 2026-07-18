# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2014-present chimera-robobs authors

"""Base class and shared helpers for the robobs scheduling algorithms.

The legacy modules relied on ``from ... import *`` re-exports that were
removed long ago, so every algorithm raised ``NameError`` at runtime.  In
this port each module imports exactly what it needs and database access goes
through a session factory installed with :func:`configure` (falling back to
the default robobs database when unconfigured).
"""

import logging
import math

from chimera.core.exceptions import ChimeraException
from sqlalchemy.orm import Session, sessionmaker

log = logging.getLogger(__name__)

_session_factory: sessionmaker | None = None


def configure(session_factory: sessionmaker) -> None:
    """Install the session factory used by all scheduling algorithms."""
    global _session_factory
    _session_factory = session_factory


def get_session() -> Session:
    """Return a new session, opening the default database if unconfigured."""
    global _session_factory
    if _session_factory is None:
        from chimera_robobs.scheduling.model import open_database

        log.warning("algorithms used before configure(); opening default database")
        _session_factory = open_database()
    return _session_factory()


class ExtintionMonitorException(ChimeraException):
    pass


class TimedException(ChimeraException):
    pass


class RecurrentAlgorithException(ChimeraException):
    pass


def airmass(alt: float) -> float:
    """Plane-parallel airmass for an altitude in degrees (999 below horizon)."""
    am = 1.0 / math.cos(math.pi / 2.0 - math.radians(alt))
    if am < 0.0:
        am = 999.0
    return am


class BaseScheduleAlgorith:
    """Static-method contract shared by all scheduling algorithms.

    The ids returned by :meth:`id` are stored in the database
    (``blockpar.sched_algorithm``) and must not change:
    0 Higher/HIG, 1 ExtintionMonitor/STD, 2 Timed/TIMED, 3 Recurrent/RECURRENT,
    4 TimeSequence/TIMESEQUENCE.
    """

    #: chimera Site proxy/adapter injected by the RobObs controller (or CLI)
    site = None

    @staticmethod
    def name() -> str:
        return "BASE"

    @staticmethod
    def id() -> int:
        return -1

    @staticmethod
    def process(*args, **kwargs):
        """Build the observing queue (slots) for this algorithm."""

    @staticmethod
    def merit_figure(target):
        pass

    @staticmethod
    def next(time, programs):
        """Select the next program to observe with this algorithm."""

    @staticmethod
    def observed(time, program, site=None, soft=False):
        """Process a program as observed."""

    @staticmethod
    def add(block):
        """Process a block being added to the queue."""

    @staticmethod
    def clean(pid):
        """Hard clean: wipe all algorithm bookkeeping for a project."""

    @staticmethod
    def soft_clean(pid, block=None):
        """Soft clean: erase only information about past observations."""

    @staticmethod
    def model():
        pass

    @staticmethod
    def timed_constraint() -> bool:
        return True
