# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2014-present chimera-robobs authors

"""Timed scheduling algorithm (id 2, name TIMED).

Schedules observations at specific times (in hours) with respect to the
night start twilight.  Targets are selected with the :class:`Higher`
algorithm; the requested times are kept in the ``timeddb`` table and
override the selected program's ``slew_at``.
"""

import logging

from chimera_robobs.scheduling.algorithms.base import TimedError
from chimera_robobs.scheduling.algorithms.higher import Higher
from chimera_robobs.scheduling.dates import MJD_JD_OFFSET
from chimera_robobs.scheduling.model import TimedDB

log = logging.getLogger(__name__)


class Timed(Higher):
    id = 2
    name = "TIMED"
    default_slot_len = 1800.0
    timed_constraint = True

    def process(self, *, obs_start, obs_end, query, config=None, slot_len=None):
        # Try to read times from the configuration. If none is provided,
        # raise an exception.
        if config is None:
            raise TimedError("No configuration file provided.")

        execute_at_mjds = [
            obs_start - MJD_JD_OFFSET + (hours / 24.0) for hours in config["times"]
        ]
        for i, execute_at in enumerate(execute_at_mjds):
            log.debug("Executing time %i @ %.5f", i, execute_at)

        # Select targets with the Higher algorithm
        programs = super().process(
            obs_start=obs_start,
            obs_end=obs_end,
            query=query,
            config=config,
            slot_len=slot_len,
        )

        # Store the desired times in the database
        session = self.session()
        try:
            for execute_at in execute_at_mjds:
                if execute_at > obs_end:
                    log.warning("Request for observation after the end of the night.")

                log.info("Requesting observation @ %.3f", execute_at)
                session.add(TimedDB(pid=config["pid"], execute_at=execute_at))
            return programs
        finally:
            session.commit()

    def next(self, now_mjd, programs):
        session = self.session()

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

            program_list = super().next(now_mjd, programs)

            program = session.merge(program_list[0])

            # Again, use higher to select a target but replace slew_at with
            # execute_at.
            program.slew_at = timed_observation.execute_at

            obsblock = session.merge(program_list[2])
            timed_observation.target_id = program.target_id
            timed_observation.block_id = obsblock.id

            return program_list
        finally:
            session.commit()

    def observed(self, time, program, soft=False):
        session = self.session()

        try:
            prog = session.merge(program[0])
            prog.finished = True
            block = session.merge(program[2])
            block.observed = True
            if not soft:
                block.last_observation = self.site.ut().replace(tzinfo=None)

            timed_observation = (
                session.query(TimedDB)
                .filter(
                    TimedDB.pid == prog.pid,
                    TimedDB.block_id == block.id,
                    TimedDB.target_id == prog.target_id,
                    TimedDB.finished == False,  # noqa: E712
                )
                .order_by(TimedDB.execute_at)
                .first()
            )

            if timed_observation is not None:
                timed_observation.finished = True
        finally:
            session.commit()

    def soft_clean(self, pid, block=None):
        """Soft clean: only erase information about past observations."""
        session = self.session()

        try:
            timed_observations = session.query(TimedDB).filter(
                TimedDB.pid == pid,
                TimedDB.finished == True,  # noqa: E712
            )

            for timed in timed_observations:
                timed.finished = False
        finally:
            session.commit()

    def clean(self, pid):
        """Hard clean: wipe all information from the database."""
        session = self.session()

        try:
            timed_observations = session.query(TimedDB).filter(TimedDB.pid == pid)

            for timed in timed_observations:
                session.delete(timed)
        finally:
            session.commit()
