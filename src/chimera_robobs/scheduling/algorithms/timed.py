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
        if not config:
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

        # Store the desired times in the database.  With ``expire_overdue``
        # each occurrence carries its cadence to the previous one, so a run
        # delayed into a later occurrence's window absorbs it (see next()).
        expire_overdue = bool(config.get("expire_overdue", False))
        session = self.session()
        try:
            previous = None
            for execute_at in sorted(execute_at_mjds):
                if execute_at > obs_end:
                    log.warning("Request for observation after the end of the night.")

                min_gap = 0.0
                if expire_overdue and previous is not None:
                    min_gap = execute_at - previous
                previous = execute_at

                log.info("Requesting observation @ %.3f", execute_at)
                session.add(
                    TimedDB(pid=config["pid"], execute_at=execute_at, min_gap=min_gap)
                )
            return programs
        finally:
            session.commit()

    def next(self, now_mjd, programs, check=None):
        session = self.session()

        try:
            pid = programs[0][0].pid
            pending = (
                session.query(TimedDB)
                .filter(TimedDB.finished == False, TimedDB.pid == pid)  # noqa: E712
                .order_by(TimedDB.execute_at)
                .all()
            )

            # the last occurrence that actually ran (observed_at is only
            # written on execution, never on expiry)
            last_run = (
                session.query(TimedDB)
                .filter(
                    TimedDB.finished == True,  # noqa: E712
                    TimedDB.pid == pid,
                    TimedDB.observed_at > 0,
                )
                .order_by(TimedDB.observed_at.desc())
                .first()
            )
            last_run_at = last_run.observed_at if last_run is not None else None

            timed_observation = None
            for request in pending:
                if (
                    request.min_gap
                    and last_run_at is not None
                    and request.execute_at < last_run_at + request.min_gap
                ):
                    # expire_overdue: the previous occurrence ran into this
                    # one's window (it executed late) — skip it instead of
                    # producing back-to-back runs
                    log.info(
                        "Timed request @ %.3f expired: previous run @ %.3f "
                        "consumed its window (gap %.3f d).",
                        request.execute_at,
                        last_run_at,
                        request.min_gap,
                    )
                    request.finished = True
                    continue
                timed_observation = request
                break

            if timed_observation is None:
                return None

            execute_at = timed_observation.execute_at

            # Walk the candidates in Higher order (slot time closest to
            # now) and take the first whose target is actually observable
            # at the requested execution time.  The legacy code committed
            # to the single closest candidate and gave the night's request
            # up when it failed a condition (seen live: the focus standard
            # closest in time sat 0.7 deg inside its own moon limit while
            # 60 others were fine).
            candidates = sorted(
                programs[:], key=lambda row: abs(now_mjd - row[0].slew_at)
            )
            program_list = None
            for row in candidates:
                if check is None or check(row, execute_at, row[2].length or 0.0):
                    program_list = row
                    break
                log.info(
                    "Timed candidate %s not observable @ %.3f; trying the next one.",
                    row[0],
                    execute_at,
                )
            if program_list is None:
                log.warning(
                    "No timed candidate observable @ %.3f (%i tried).",
                    execute_at,
                    len(candidates),
                )
                return None

            # Replace the slot slew time with the requested execution time —
            # on the caller's row: setting it on a merged copy in this
            # session left the caller holding the stale slot time (also
            # seen live).
            program_list[0].slew_at = execute_at

            timed_observation.target_id = program_list[0].target_id
            timed_observation.block_id = program_list[2].id

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
                # actual run time: the expire_overdue window in next() is
                # anchored on it
                timed_observation.observed_at = time
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
                # forget actual run times too, or a re-simulation would
                # expire occurrences against the previous night's runs
                timed.observed_at = 0.0
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
