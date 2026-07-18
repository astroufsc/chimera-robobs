# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2014-present chimera-robobs authors

"""Program-selection engine for robobs.

Holds the pure scheduling logic (reschedule / get_program / check_conditions)
extracted from the legacy ``RobObs`` controller so it can be used both by the
chimera controller and by the offline ``chimera-robobs process-queue``
simulation.  A *program* here is the 4-tuple
``(Program, BlockPar, ObsBlock, Target)`` returned by the database query.
"""

import datetime as dt
import logging
from collections.abc import Callable

import numpy as np
from chimera.util.position import Position
from sqlalchemy.orm import sessionmaker

from chimera_robobs.scheduling.algorithms import build_algorithms
from chimera_robobs.scheduling.algorithms.base import airmass
from chimera_robobs.scheduling.dates import (
    SECONDS_PER_DAY,
    datetime_from_mjd,
)
from chimera_robobs.scheduling.model import (
    BlockPar,
    ObsBlock,
    Program,
    Target,
    block_duration,
)

module_log = logging.getLogger(__name__)


class RobObsEngine:
    """Selects the next program to observe from the robobs database.

    :param session_factory: robobs database session factory
        (see :func:`chimera_robobs.scheduling.model.open_database`).
    :param site: a :class:`~chimera_robobs.scheduling.siteadapter.SiteAdapter`
        (or object with the same interface).
    :param log: logger for the (chatty) scheduling decisions.
    :param seeing: optional callable returning the current seeing in arcsec
        (negative meaning "no measurement available").
    :param algorithms: id-keyed algorithm registry (built over
        ``session_factory``/``site`` when omitted).
    """

    def __init__(
        self,
        session_factory: sessionmaker,
        site,
        log: logging.Logger | None = None,
        seeing: Callable[[], float] | None = None,
        algorithms: dict | None = None,
    ):
        self.session = session_factory
        self.site = site
        self.log = log or module_log
        self.seeing = seeing
        self.algorithms = (
            algorithms
            if algorithms is not None
            else build_algorithms(session_factory, site)
        )

    # ------------------------------------------------------------------

    def get_priority_list(self) -> list[int]:
        """Return the distinct program priorities, ordered."""
        session = self.session()
        try:
            return [
                p[0]
                for p in session.query(Program.priority)
                .distinct()
                .order_by(Program.priority)
            ]
        finally:
            session.close()

    def get_program(self, nowmjd: float, priority: int):
        """Return ``(program_tuple, length_seconds)`` for a priority queue.

        ``program_tuple`` is ``(Program, BlockPar, ObsBlock, Target)`` or
        ``None`` when nothing suitable is found.
        """
        session = self.session()

        self.log.debug(
            "Looking for program with priority %i to observe @ %.3f", priority, nowmjd
        )

        programs = (
            session.query(Program, BlockPar, ObsBlock, Target)
            .join(BlockPar, Program.blockpar_id == BlockPar.id)
            .join(ObsBlock, Program.obsblock_id == ObsBlock.id)
            .join(Target, Program.target_id == Target.id)
            .filter(Program.priority == priority, Program.finished == False)  # noqa: E712
            .order_by(Program.slew_at)
        )

        unique_sched_algorithms = {row[1].sched_algorithm for row in programs}

        for sal in sorted(unique_sched_algorithms):
            sched = self.algorithms[sal]

            program = sched.next(nowmjd, programs, check=self.check_conditions)

            if program is not None:
                self.log.debug("Found program %s", program[0])
                # prefer the length stored at ingest time (includes readout
                # and focus overheads — recovered 2018 fix from the
                # never-merged bugfix/block_length branch); fall back to the
                # bare exposure sum for blocks without a stored length
                length = program[2].length or block_duration(program[2].actions)

                if not sched.timed_constraint and program[0].slew_at > nowmjd:
                    self.log.debug("Checking if program can be observed earlier...")
                    # Search for the earliest feasible start before slew_at
                    # (the legacy loop never broke, so the *last* feasible
                    # candidate won and the search was a near-no-op).
                    for candidate in np.linspace(nowmjd, program[0].slew_at, num=50):
                        if self.check_conditions(program, candidate, length):
                            self.log.debug(
                                "Replacing program slew_at %.2f -> %.2f",
                                program[0].slew_at,
                                candidate,
                            )
                            program[0].slew_at = candidate
                            break

                session.commit()
                return program, length

        self.log.warning("No program found...")
        session.close()
        return None, 0.0

    def reschedule(self, now: float | None = None):
        """Choose the next program to execute (or ``None``).

        :param now: MJD to schedule for (defaults to the current site MJD).
        """
        nowmjd = self.site.mjd() if now is None else now

        # Get a list of priorities
        plist = self.get_priority_list()

        if len(plist) == 0:
            return None

        # Get the project with the highest priority as reference
        program, plen = self.get_program(nowmjd, plist[0])

        waittime = 0.0

        if program is not None:
            if (not program[0].slew_at) and self.check_conditions(
                program, nowmjd, plen
            ):
                # Program should be done right away!
                return program

            self.log.info(
                "Current program length: %.2f m. Slew@: %.3f",
                plen / 60.0,
                program[0].slew_at,
            )

            waittime = (program[0].slew_at - nowmjd) * SECONDS_PER_DAY
        else:
            self.log.warning("No program on %i priority queue.", plist[0])

        if waittime < 0:
            waittime = 0

        self.log.info("Wait time is: %.2f m", waittime / 60.0)

        for p in plist[1:]:
            # Get program and program duration (length)
            aprogram, aplen = self.get_program(nowmjd, p)

            if aprogram is None:
                continue

            checktime = max(nowmjd, aprogram[0].slew_at)

            can_observe = self.check_conditions(aprogram, checktime, aplen)
            if program is None and can_observe:
                self.log.info(
                    "No higher priority program. Choosing this instead and continue"
                )
                program, plen = aprogram, aplen
                waittime = max((program[0].slew_at - nowmjd) * SECONDS_PER_DAY, 0.0)
                self.log.info("Wait time is: %.2f m", waittime / 60.0)
                continue
            elif not can_observe:
                # if the condition is False, the project cannot be executed.
                # Go to the next in the list.
                self.log.info("Selected program cannot be observed. Skipping...")
                continue

            self.log.info(
                "Current program length: %.2f m. Slew@: %.3f",
                aplen / 60.0,
                aprogram[0].slew_at,
            )

            # If the alternate program fits, send it instead
            awaittime = max((aprogram[0].slew_at - nowmjd) * SECONDS_PER_DAY, 0.0)

            self.log.info("Wait time is: %.2f m", awaittime / 60.0)

            if awaittime + aplen < waittime:
                self.log.info(
                    "Program with priority %i fits in this slot. Selecting it instead.",
                    p,
                )
                program, plen, waittime = aprogram, aplen, awaittime
            elif (
                awaittime < waittime
                and not self.algorithms[program[1].sched_algorithm].is_hard_timed(
                    program
                )
                and self.check_conditions(
                    program, nowmjd + (awaittime + aplen) / SECONDS_PER_DAY, plen
                )
            ):
                # Checks if the program with higher priority can be observed
                # later on. If so, use the current program instead if the
                # waittime is lower.  Not for hard-timed programs (bound
                # occultations): they are still condition-observable after
                # the alternate ends, but their instant has passed — only an
                # alternate that FITS before slew_at (branch above) may cut
                # in front.
                self.log.info(
                    "Program with higher priority can be executed after current "
                    "program. Selecting program with priority %i.",
                    p,
                )
                program, plen, waittime = aprogram, aplen, awaittime

        if program is None:
            # if no project can be executed, return nothing.
            return None

        checktime = max(nowmjd, program[0].slew_at)
        if not self.check_conditions(program, checktime, plen):
            return None

        self.log.info("Choose program with priority %i", program[0].priority)
        return program

    # ------------------------------------------------------------------

    def check_conditions(
        self,
        program,
        time: float,
        program_length: float = 0.0,
    ) -> bool:
        """Check if a program can be executed given the restrictions imposed
        by airmass, moon distance/brightness, seeing, ...

        :param program: ``(Program, BlockPar, ObsBlock, Target)`` tuple.
        :param time: MJD of the intended start of the observation.
        :param program_length: program duration in seconds.
        :return: True (program can be executed) | False (it cannot).
        """
        session = self.session()
        try:
            target = session.merge(program[3])
            blockpar = session.merge(program[1])

            date_time = datetime_from_mjd(time)
            lst = self.site.lst_in_rads(date_time)  # in radians

            # 0) it must be night.  The sun is up whenever the *next*
            # sunset twilight comes before the *next* sunrise twilight.
            # Without this guard a program left unfinished at dawn kept
            # being re-evaluated (and could be executed) in full daylight —
            # the pre-2.0 "observations after the night ends" failure.
            # Evaluated one minute ahead: schedules start exactly at the
            # dusk instant and the MJD round trip loses microseconds, which
            # must not flip the decision to "daytime".
            guard_time = date_time + dt.timedelta(minutes=1)
            next_dusk = self.site.sunset_twilight_end(guard_time).replace(tzinfo=None)
            next_dawn = self.site.sunrise_twilight_begin(guard_time).replace(
                tzinfo=None
            )
            if next_dusk < next_dawn:
                self.log.warning(
                    "Daytime @ %s (next dusk %s < next dawn %s): not observable.",
                    date_time,
                    next_dusk,
                    next_dawn,
                )
                return False

            # 1) check airmass
            alt, _ = self.site.ra_dec_to_alt_az(
                target.target_ra, target.target_dec, lst
            )
            target_airmass = airmass(alt)

            if blockpar.min_airmass < target_airmass < blockpar.max_airmass:
                self.log.debug("\tairmass:%.3f", target_airmass)
            else:
                self.log.warning(
                    "Target %s out of airmass range @ %.3f... (%f < %f < %f)",
                    target,
                    time,
                    blockpar.min_airmass,
                    target_airmass,
                    blockpar.max_airmass,
                )
                return False

            if program_length > 0.0:
                end_mjd = time + program_length / SECONDS_PER_DAY
                observation_end = datetime_from_mjd(end_mjd).replace(tzinfo=None)
                night_end = next_dawn
                if observation_end > night_end:
                    self.log.warning(
                        "Block finish @ %s. Night end is @ %s!",
                        observation_end,
                        night_end,
                    )
                    return False
                self.log.debug(
                    "Block finish @ %s. Night end is @ %s!", observation_end, night_end
                )

                # airmass at the end of the observation (legacy left a FIXME
                # fall-through here instead of rejecting; also evaluated the
                # altitude at the start LST instead of the end-of-block LST)
                end_lst = self.site.lst_in_rads(datetime_from_mjd(end_mjd))
                end_alt, _ = self.site.ra_dec_to_alt_az(
                    target.target_ra, target.target_dec, end_lst
                )
                end_airmass = airmass(end_alt)

                if blockpar.min_airmass < end_airmass < blockpar.max_airmass:
                    self.log.debug("\tairmass @ block end:%.3f", end_airmass)
                else:
                    self.log.warning(
                        "Target %s out of airmass range at end of block @ %.3f... "
                        "(%f < %f < %f)",
                        target,
                        end_mjd,
                        blockpar.min_airmass,
                        end_airmass,
                        blockpar.max_airmass,
                    )
                    return False

            # 2) check moon brightness
            moon_ra, moon_dec = self.site.moon_ra_dec(date_time)
            moon_alt, _ = self.site.ra_dec_to_alt_az(moon_ra, moon_dec, lst)
            moon_brightness = self.site.moon_phase(date_time) * 100.0
            # inclusive bounds: min/max are commonly set to exactly 0/100
            # (recovered 2018 fix from the never-merged mysql branch)
            if blockpar.min_moon_bright <= moon_brightness <= blockpar.max_moon_bright:
                self.log.debug("\tMoon brightness:%.2f", moon_brightness)
            elif moon_alt < 0.0:
                self.log.warning(
                    "\tMoon below horizon. Moon brightness:%.2f", moon_brightness
                )
            else:
                self.log.warning(
                    "Wrong Moon Brightness... (%f < %f < %f)",
                    blockpar.min_moon_bright,
                    moon_brightness,
                    blockpar.max_moon_bright,
                )
                return False

            # 3) check moon distance
            ra_dec = Position.from_ra_dec(target.target_ra, target.target_dec)
            moon_ra_dec_pos = Position.from_ra_dec(moon_ra, moon_dec)

            moon_dist = float(ra_dec.angsep(moon_ra_dec_pos))

            if moon_dist < blockpar.min_moon_distance:
                self.log.warning(
                    "Object too close to the moon... "
                    "Target@ %s / Moon@ %s (moonDist = %f | minmoonDist = %f)",
                    ra_dec,
                    moon_ra_dec_pos,
                    moon_dist,
                    blockpar.min_moon_distance,
                )
                return False
            self.log.debug("\tMoon distance:%.3f", moon_dist)

            # 4) check seeing
            if self.seeing is not None:
                seeing = self.seeing()

                if seeing > blockpar.max_seeing:
                    self.log.warning(
                        "Seeing higher than specified... sm = %f | max = %f",
                        seeing,
                        blockpar.max_seeing,
                    )
                    return False
                elif seeing < 0.0:
                    self.log.warning("No seeing measurement...")
                else:
                    self.log.debug("Seeing %.3f", seeing)

            # 5) check cloud cover / weather: not implemented (as in the
            # legacy code, which had placeholder pass-throughs for these
            # sensors).

            self.log.debug("Target OK!")

            return True
        finally:
            # read-only check: never persist the merged copies
            session.close()
