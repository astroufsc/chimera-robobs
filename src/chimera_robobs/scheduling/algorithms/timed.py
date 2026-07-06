# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2014-present chimera-robobs authors

"""Timed scheduling algorithm (id 2, name TIMED).

Schedules observations at specific times (in hours) with respect to the
night start twilight.
"""

import logging

from chimera_robobs.scheduling.algorithms.base import (
    BaseScheduleAlgorith,
    TimedException,
    get_session,
)
from chimera_robobs.scheduling.algorithms.higher import Higher
from chimera_robobs.scheduling.model import TimedDB

log = logging.getLogger(__name__)


class Timed(BaseScheduleAlgorith):
    @staticmethod
    def name() -> str:
        return "TIMED"

    @staticmethod
    def id() -> int:
        return 2

    @staticmethod
    def process(*args, **kwargs):
        # Try to read times from the configuration. If none is provided,
        # raise an exception.
        if "config" not in kwargs:
            raise TimedException("No configuration file provided.")

        config = kwargs["config"]

        nightstart = kwargs["obsStart"]
        nightend = kwargs["obsEnd"]

        for i in range(len(config["times"])):
            execute_at = nightstart - 2400000.5 + (config["times"][i] / 24.0)
            log.debug("Executing time %i @ %.5f", i, execute_at)
            config["times"][i] = execute_at

        slot_len = 1800.0
        if "slotLen" in kwargs:
            slot_len = kwargs["slotLen"]
        elif len(args) > 1:
            try:
                slot_len = float(args[0])
            except (TypeError, ValueError):
                slot_len = 1800.0

        # Select targets with the Higher algorithm
        kwargs.pop("slotLen", None)
        programs = Higher.process(slotLen=slot_len, *args, **kwargs)

        session = get_session()
        # Store the desired times in the database
        try:
            for obs_time in config["times"]:
                if obs_time > nightend:
                    log.warning("Request for observation after the end of the night.")

                log.info("Requesting observation @ %.3f", obs_time)
                timed = TimedDB(pid=config["pid"], execute_at=obs_time)
                session.add(timed)
            return programs
        finally:
            session.commit()

    @staticmethod
    def next(time, programs):
        session = get_session()

        try:
            program = session.merge(programs[0][0])
            timed_observation = (
                session.query(TimedDB)
                .filter(TimedDB.finished == False, TimedDB.pid == program.pid)  # noqa: E712
                .order_by(TimedDB.execute_at)
                .first()
            )

            if timed_observation is None:
                return None

            program_list = Higher.next(time, programs)

            program = session.merge(program_list[0])

            # Again, use higher to select a target but replace slew_at with
            # execute_at.
            program.slew_at = timed_observation.execute_at

            obsblock = session.merge(program_list[2])
            timed_observation.tid = program.target_id
            timed_observation.block_id = obsblock.id

            return program_list
        finally:
            session.commit()

    @staticmethod
    def observed(time, program, site=None, soft=False):
        session = get_session()

        try:
            prog = session.merge(program[0])
            prog.finished = True
            block = session.merge(program[2])
            block.observed = True
            if not soft:
                block.last_observation = site.ut().replace(tzinfo=None)

            timed_observation = (
                session.query(TimedDB)
                .filter(
                    TimedDB.pid == prog.pid,
                    TimedDB.block_id == block.id,
                    TimedDB.tid == prog.target_id,
                    TimedDB.finished == False,  # noqa: E712
                )
                .order_by(TimedDB.execute_at)
                .first()
            )

            if timed_observation is not None:
                timed_observation.finished = True
        finally:
            session.commit()

    @staticmethod
    def soft_clean(pid, block=None):
        """Soft clean: only erase information about past observations."""
        session = get_session()

        try:
            timed_observations = session.query(TimedDB).filter(
                TimedDB.pid == pid,
                TimedDB.finished == True,  # noqa: E712
            )

            for timed in timed_observations:
                timed.finished = False
        finally:
            session.commit()

    @staticmethod
    def clean(pid):
        """Hard clean: wipe all information from the database."""
        session = get_session()

        try:
            timed_observations = session.query(TimedDB).filter(TimedDB.pid == pid)

            for timed in timed_observations:
                session.delete(timed)
        finally:
            session.commit()
