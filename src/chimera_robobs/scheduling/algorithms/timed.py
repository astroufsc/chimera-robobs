# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2014-present chimera-robobs authors

"""Timed scheduling algorithm (id 2, name TIMED).

Schedules observations at specific times.  A ``times`` entry is one of

* a number — hours after the evening twilight (recurrent tasks, e.g.
  focus runs); the target is selected with the :class:`Higher` algorithm.
* an UT date-time string (``"2026-07-25 03:12:00"``) — absolute time;
  entries that do not fall in tonight's observing window are skipped, so
  they are safe to keep permanently in the project's ``scheduling:``
  section.
* a mapping ``{target: NAME, at: <number or UT string>}`` — the occurrence
  is bound to that target (occultations, transits): no Higher selection,
  the named target is observed at the requested time or the occurrence is
  given up.

The requested times are kept in the ``timeddb`` table and override the
selected program's ``slew_at``.
"""

import datetime as dt
import json
import logging
import math

import numpy as np
from chimera.util.position import Position
from sqlalchemy import or_

from chimera_robobs.scheduling.algorithms.base import TimedError
from chimera_robobs.scheduling.algorithms.higher import SLOT_DTYPE, Higher
from chimera_robobs.scheduling.dates import (
    MJD_JD_OFFSET,
    SECONDS_PER_DAY,
    datetime_from_jd,
    jd_from_datetime,
)
from chimera_robobs.scheduling.model import Project, TimedDB

log = logging.getLogger(__name__)


def parse_time_entry(entry, obs_start, obs_end):
    """Resolve one ``times`` entry to ``(execute_at_mjd, target_name)``.

    ``target_name`` is ``None`` for unbound entries.  Returns ``None`` when
    an absolute time does not fall in tonight's ``[obs_start, obs_end]``
    JD window.  Raises :class:`TimedError` on a malformed entry.
    """
    target_name = None
    if isinstance(entry, dict):
        unknown = set(entry) - {"target", "at"}
        if unknown:
            raise TimedError(f"unknown keys in times entry: {sorted(unknown)}")
        if "target" not in entry or "at" not in entry:
            raise TimedError(f"times entry needs 'target' and 'at': {entry!r}")
        target_name = str(entry["target"])
        entry = entry["at"]

    if isinstance(entry, bool):
        raise TimedError(f"invalid times entry: {entry!r}")
    if isinstance(entry, (int, float)):
        return (obs_start - MJD_JD_OFFSET + float(entry) / 24.0, target_name)

    # absolute UT: an ISO string, or a datetime (PyYAML parses unquoted
    # timestamps); naive values are UTC
    if isinstance(entry, str):
        try:
            entry = dt.datetime.fromisoformat(entry)
        except ValueError as exc:
            raise TimedError(f"invalid times entry: {exc}") from exc
    if not isinstance(entry, dt.datetime):
        raise TimedError(f"invalid times entry: {entry!r}")
    jd = jd_from_datetime(entry)
    if not obs_start <= jd <= obs_end:
        log.info("Timed request @ %s UT is not tonight. Skipping.", entry)
        return None
    return (jd - MJD_JD_OFFSET, target_name)


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

        # Resolve the times entries; absolute times not falling in tonight's
        # window drop out here (parse_time_entry returns None for them).
        occurrences = []
        for entry in config["times"]:
            parsed = parse_time_entry(entry, obs_start, obs_end)
            if parsed is None:
                continue
            execute_at, target_name = parsed
            if execute_at > obs_end - MJD_JD_OFFSET:
                log.warning("Request for observation after the end of the night.")
            log.debug(
                "Executing time %i @ %.5f%s",
                len(occurrences),
                execute_at,
                f" (target {target_name})" if target_name else "",
            )
            occurrences.append((execute_at, target_name))

        if not occurrences:
            log.info("No timed request falls in tonight's window.")
            return np.zeros(0, dtype=SLOT_DTYPE)

        # Unbound occurrences: select targets with the Higher algorithm, as
        # always.  Bound occurrences bypass the Higher selection entirely —
        # each gets a synthetic slot for its own target at its own time.
        rows = query[:]
        unbound = [occ for occ in occurrences if occ[1] is None]
        bound = [occ for occ in occurrences if occ[1] is not None]
        if unbound and bound:
            log.warning(
                "Mixing target-bound and unbound times entries in one "
                "project: the Higher selection may schedule a bound target "
                "for an unbound occurrence."
            )

        slots = (
            super().process(
                obs_start=obs_start,
                obs_end=obs_end,
                query=query,
                config=config,
                slot_len=slot_len,
            )
            if unbound
            else np.zeros(0, dtype=SLOT_DTYPE)
        )

        rows_by_name = {row[2].name: row for row in rows}
        # (execute_at, target_name, query row or None) triples
        accepted = [occ + (None,) for occ in unbound]
        extra_slots = []
        slot_len_days = self._slot_len(config, slot_len) / SECONDS_PER_DAY
        for execute_at, target_name in bound:
            row = rows_by_name.get(target_name)
            if row is None:
                log.warning(
                    "times entry target %r is not selectable tonight "
                    "(unknown name, or outside the LST window). Skipping "
                    "the occurrence.",
                    target_name,
                )
                continue
            start_jd = execute_at + MJD_JD_OFFSET
            extra_slots.append(
                (
                    start_jd,
                    start_jd + slot_len_days,
                    len(slots) + len(extra_slots),
                    row[0].blockid,
                )
            )
            accepted.append((execute_at, target_name, row))
        if extra_slots:
            slots = np.concatenate([slots, np.array(extra_slots, dtype=SLOT_DTYPE)])

        # Store the requests in the database.  With ``expire_overdue`` each
        # occurrence carries its cadence to the previous one, so a run
        # delayed into a later occurrence's window absorbs it (see next()).
        expire_overdue = bool(config.get("expire_overdue", False))
        session = self.session()
        try:
            previous = None
            for execute_at, target_name, row in sorted(accepted, key=lambda o: o[0]):
                min_gap = 0.0
                if expire_overdue and previous is not None:
                    min_gap = execute_at - previous
                previous = execute_at

                log.info(
                    "Requesting observation @ %.3f%s",
                    execute_at,
                    f" of {target_name}" if target_name else "",
                )
                request = TimedDB(
                    pid=config["pid"], execute_at=execute_at, min_gap=min_gap
                )
                if row is not None:
                    request.bound = True
                    request.target_id = row[0].target_id
                    request.block_id = row[0].id
                session.add(request)
            return slots
        finally:
            session.commit()

    def _past_meridian_only(self, session, pid):
        """``past_meridian_only`` from the project's ``scheduling`` JSON.

        next() has no config argument, and the option is enforced by the
        Higher selection at each SLOT's LST — which is not the LST the
        occurrence actually runs at.  Read it back here so the re-timed
        candidate can be re-checked.
        """
        project = session.query(Project).filter(Project.pid == pid).first()
        if project is None or not project.scheduling:
            return False
        try:
            return bool(json.loads(project.scheduling).get("past_meridian_only", False))
        except (ValueError, AttributeError):
            log.warning("could not parse scheduling JSON of project %s", pid)
            return False

    def _is_past_meridian(self, row, at_mjd):
        """True if the row's target has crossed the meridian at ``at_mjd``."""
        position = Position.from_ra_dec(row[3].target_ra, row[3].target_dec)
        lst = self.site.lst_in_rads(datetime_from_jd(at_mjd + MJD_JD_OFFSET))
        # folded to [0, 2pi): (0, pi) means past the meridian and setting
        hour_angle = (lst - position.ra.radian) % (2.0 * math.pi)
        return 0.0 < hour_angle < math.pi

    def next(self, now_mjd, programs, check=None):
        session = self.session()

        try:
            pid = programs[0][0].pid
            # scheduled == False matters as much as finished == False: an
            # occurrence stays unfinished until the program actually runs,
            # and robobs asks for the next program as soon as it has handed
            # the previous one to the chimera scheduler. Without this the
            # SAME occurrence was served over and over - 13 focus programs
            # queued in one second on 2026-07-22, all executed back to back
            # instead of one every two hours.
            pending = (
                session.query(TimedDB)
                .filter(
                    TimedDB.finished == False,  # noqa: E712
                    TimedDB.scheduled == False,  # noqa: E712
                    TimedDB.pid == pid,
                )
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

            # An occurrence must not be expired just because the previous run
            # took time to execute: min_gap is the FULL nominal spacing, so
            # with observed_at recorded at completion, any duration at all
            # pushed the boundary past the next occurrence and silently
            # halved the cadence (seen live: 6 focus requests 2 h apart, 3
            # served). Discount the block's own length from the boundary.
            block_length_days = (programs[0][2].length or 0.0) / SECONDS_PER_DAY

            timed_observation = None
            for request in pending:
                if (
                    request.min_gap
                    and last_run_at is not None
                    and request.execute_at
                    < last_run_at + request.min_gap - block_length_days
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
                if request.bound:
                    # target-bound occurrence (occultation/transit): observe
                    # exactly this target at this time or give the occurrence
                    # up — the conditions at a fixed time are deterministic,
                    # so a failed check now would fail all night and wedge
                    # every later request of the project behind it.
                    row = next(
                        (
                            r
                            for r in programs
                            if r[0].target_id == request.target_id
                            and r[2].id == request.block_id
                        ),
                        None,
                    )
                    if row is None:
                        log.warning(
                            "Bound timed request @ %.3f: target id %s is not "
                            "in the queue. Expiring the occurrence.",
                            request.execute_at,
                            request.target_id,
                        )
                        request.finished = True
                        continue
                    if check is not None and not check(
                        row, request.execute_at, row[2].length or 0.0
                    ):
                        log.warning(
                            "Bound timed request @ %.3f: %s not observable at "
                            "its fixed time. Expiring the occurrence.",
                            request.execute_at,
                            row[0],
                        )
                        request.finished = True
                        continue
                    row[0].slew_at = request.execute_at
                    return row
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
            past_meridian_only = self._past_meridian_only(session, pid)
            program_list = None
            for row in candidates:
                # Higher applied past_meridian_only at the SLOT's LST, but
                # the candidate is about to be re-timed to execute_at below.
                # Without this it schedules targets still east of the
                # meridian - a pier flip mid-run on a GEM (seen live: the
                # first focus of the night 1.4 h before the meridian).
                if past_meridian_only and not self._is_past_meridian(row, execute_at):
                    log.info(
                        "Timed candidate %s is still east of the meridian @ "
                        "%.3f; trying the next one.",
                        row[0],
                        execute_at,
                    )
                    continue
                if check is None or check(row, execute_at, row[2].length or 0.0):
                    program_list = row
                    break
                log.info(
                    "Timed candidate %s not observable @ %.3f; trying the next one.",
                    row[0],
                    execute_at,
                )
            if program_list is None:
                if execute_at > now_mjd:
                    # NOT DUE YET. next() walks pending occurrences in time
                    # order, so this is routinely a request hours away being
                    # tested against a queue that will have changed by then -
                    # expiring it here burned 5 of 6 focus runs in one night.
                    log.debug(
                        "No candidate for the %.3f occurrence yet (due in "
                        "%.1f min); leaving it pending.",
                        execute_at,
                        (execute_at - now_mjd) * 24.0 * 60.0,
                    )
                    return None
                # Due or overdue: every condition is a function of the FIXED
                # execute_at, so it will fail identically at every later
                # poll. Expire it, or the whole project wedges behind it and
                # loses the night's remaining runs.
                log.warning(
                    "No timed candidate observable @ %.3f (%i tried). "
                    "Expiring the occurrence.",
                    execute_at,
                    len(candidates),
                )
                timed_observation.finished = True
                return None

            # Replace the slot slew time with the requested execution time —
            # on the caller's row: setting it on a merged copy in this
            # session left the caller holding the stale slot time (also
            # seen live).
            program_list[0].slew_at = execute_at

            timed_observation.target_id = program_list[0].target_id
            timed_observation.block_id = program_list[2].id
            # OFFER only: stamp which occurrence this program serves and
            # leave it pending. The engine polls every priority queue while
            # choosing, so consuming here lost one occurrence per poll that
            # FOCUS did not win. committed() consumes it if the engine
            # actually takes the program.
            program_list[0]._timed_request_id = timed_observation.id

            return program_list
        finally:
            session.commit()

    def is_hard_timed(self, program) -> bool:
        """A program serving a pending bound occurrence is immovable."""
        session = self.session()
        try:
            return (
                session.query(TimedDB)
                .filter(
                    TimedDB.bound == True,  # noqa: E712
                    TimedDB.finished == False,  # noqa: E712
                    TimedDB.pid == program[0].pid,
                    TimedDB.target_id == program[0].target_id,
                    TimedDB.block_id == program[2].id,
                )
                .first()
                is not None
            )
        finally:
            session.commit()

    def committed(self, program):
        """Consume the offered occurrence: the program is being handed over.

        If the program never actually runs the occurrence is simply lost,
        which is the intended "skip it" behaviour - it is not re-offered.
        """
        request_id = getattr(program[0], "_timed_request_id", None)
        if request_id is None:
            return
        session = self.session()
        try:
            request = session.query(TimedDB).get(request_id)
            if request is not None:
                request.scheduled = True
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

            # prefer the occurrence stamped at offer time: the fallback
            # lookup below trusts block/target ids that reloads may remap
            request_id = getattr(prog, "_timed_request_id", None) or getattr(
                program[0], "_timed_request_id", None
            )
            timed_observation = (
                session.query(TimedDB).get(request_id) if request_id else None
            )
            if timed_observation is not None and timed_observation.finished:
                timed_observation = None
            if timed_observation is None:
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
                or_(
                    TimedDB.finished == True,  # noqa: E712
                    TimedDB.scheduled == True,  # noqa: E712
                ),
            )

            for timed in timed_observations:
                timed.finished = False
                timed.scheduled = False
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
