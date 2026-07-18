# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2014-present chimera-robobs authors

"""Extinction-monitor scheduling algorithm (id 1, name STD).

Ported from the legacy ``extintionmonitor.py`` (typo fixed in the module
name; the algorithm name string and id are unchanged because they are stored
in the database).
"""

import logging

import numpy as np
from chimera.util.position import Position

from chimera_robobs.scheduling.algorithms.base import (
    BaseScheduleAlgorithm,
    ExtinctionMonitorError,
    get_session,
)
from chimera_robobs.scheduling.algorithms.base import (
    airmass as _airmass,
)
from chimera_robobs.scheduling.dates import datetime_from_jd
from chimera_robobs.scheduling.model import ExtMoniDB, ObservedAM

log = logging.getLogger(__name__)


class ExtinctionMonitor(BaseScheduleAlgorithm):
    """Schedule standard stars over a range of airmasses."""

    @staticmethod
    def name() -> str:
        return "STD"

    @staticmethod
    def id() -> int:
        return 1

    @staticmethod
    def clean(pid):
        session = get_session()
        try:
            ext_moni_blocks = session.query(ExtMoniDB).filter(ExtMoniDB.pid == pid)
            for block in ext_moni_blocks:
                for observed_am in block.observed_am:
                    session.delete(observed_am)
                session.delete(block)
        finally:
            session.commit()

    @staticmethod
    def soft_clean(pid, block=None):
        session = get_session()
        try:
            ext_moni_blocks = session.query(ExtMoniDB).filter(ExtMoniDB.pid == pid)
            for ext_block in ext_moni_blocks:
                for observed_am in ext_block.observed_am:
                    session.delete(observed_am)
        finally:
            session.commit()

    @staticmethod
    def add(block):
        session = get_session()
        try:
            obsblock = session.merge(block[0])
            # Check if this is already in the database
            ext_moni_block = (
                session.query(ExtMoniDB)
                .filter(
                    ExtMoniDB.pid == obsblock.pid,
                    ExtMoniDB.target_id == obsblock.target_id,
                )
                .first()
            )

            if ext_moni_block is not None:
                # already in the database, just update
                ext_moni_block.nairmass += 1
            else:
                ext_moni_block = ExtMoniDB(
                    pid=obsblock.pid, target_id=obsblock.target_id
                )
                session.add(ext_moni_block)
        finally:
            session.commit()

    @staticmethod
    def process(*args, **kwargs):
        slot_len = 60.0
        if "slotLen" in kwargs:
            slot_len = kwargs["slotLen"]
        elif len(args) > 1:
            try:
                slot_len = float(args[0])
            except (TypeError, ValueError):
                slot_len = 60.0

        # Selecting standard stars is not only searching for the highest in
        # that time, but selecting stars that can be observed at 3 or more
        # (nairmass) different airmasses.

        nightstart = kwargs["obsStart"]
        nightend = kwargs["obsEnd"]
        time_grid = np.arange(nightstart, nightend, slot_len / 60.0 / 60.0 / 24.0)
        site = kwargs["site"]
        targets = kwargs["query"]
        rows = targets[:]

        nstars = 3
        nairmass = 3

        overheads = {
            "autofocus": {"align": 0.0, "set": 0.0},
            "point": 0.0,
            "readout": 0.0,
        }

        if "overheads" in kwargs:
            overheads.update(kwargs["overheads"])

        if "config" in kwargs:
            config = kwargs["config"]
            if "nstars" in config:
                nstars = config["nstars"]
            if "nairmass" in config:
                nairmass = config["nairmass"]

        min_altitude = 10.0
        max_airmass_default = 1.0 / np.cos(np.pi / 2.0 - np.pi / 18.0)

        radec_array = np.array(
            [Position.from_ra_dec(rows[0][2].target_ra, rows[0][2].target_dec)]
        )
        target_name_array = np.array([rows[0][2].name])

        # Create observation slots.
        slot_dtype = [
            ("start", float),
            ("end", float),
            ("slotid", int),
            ("blockid", int),
            ("filled", int),
        ]
        obs_slots = np.array([], dtype=slot_dtype)

        blockid = rows[0][0].blockid
        radec_pos = np.array([0])
        blockid_list = np.array([blockid])
        block_duration = np.array([0.0])  # duration of each block
        max_airmass = np.array([rows[0][1].max_airmass])
        min_airmass = np.array([rows[0][1].min_airmass])
        if max_airmass[0] < 0:
            max_airmass[0] = max_airmass_default
        if min_airmass[0] < 0:
            min_airmass[0] = max_airmass_default  # ignore min airmass if not set

        # Get single block ids and determine block durations
        for itr, row in enumerate(rows):
            if blockid != row[0].blockid:
                radec_array = np.append(
                    radec_array,
                    Position.from_ra_dec(row[2].target_ra, row[2].target_dec),
                )
                target_name_array = np.append(target_name_array, row[2].name)
                blockid = row[0].blockid
                radec_pos = np.append(radec_pos, itr)
                blockid_list = np.append(blockid_list, blockid)
                if row[1].max_airmass > 0:
                    max_airmass = np.append(max_airmass, row[1].max_airmass)
                else:
                    max_airmass = np.append(max_airmass, max_airmass_default)

                if row[1].min_airmass > 0:
                    min_airmass = np.append(min_airmass, row[1].min_airmass)
                else:
                    min_airmass = np.append(min_airmass, max_airmass_default)

                block_duration = np.append(block_duration, 0.0)

            for blk_action in row[0].actions:
                if blk_action.__tablename__ == "action_expose":
                    block_duration[-1] += (
                        blk_action.exptime + overheads["readout"]
                    ) * blk_action.frames
                elif blk_action.__tablename__ == "action_focus":
                    if blk_action.step > 0:
                        block_duration[-1] += overheads["autofocus"]["align"]
                    elif blk_action.step == 0:
                        block_duration[-1] += overheads["autofocus"]["set"]

        # Start allocating
        # get lst at the middle of the observing window
        midnight = (nightstart + nightend) / 2.0
        lstmid = site.lst_in_rads(datetime_from_jd(midnight))  # in radians

        nalloc = 0  # number of stars allocated
        nblock = 0  # block iterator
        nballoc = 0  # total number of blocks allocated

        while nalloc < nstars and nblock < len(radec_array):
            # get airmasses
            olst = radec_array[nblock].ra.radian * 0.999
            ra_h = radec_array[nblock].ra.hour
            dec_d = radec_array[nblock].dec.deg
            max_altitude, _ = site.ra_dec_to_alt_az(ra_h, dec_d, olst)
            min_am = 1.0 / np.cos(np.pi / 2.0 - max_altitude * np.pi / 180.0)

            log.debug("Altitude max/min: %.2f/%.2f", max_altitude, min_altitude)
            log.debug("Airmass max/min: %.2f/%.2f", max_airmass[nblock], min_am)
            log.debug("Working on: %s", target_name_array[nblock])

            if max_altitude < min_altitude:
                log.debug(
                    "Max altitude %6.2f lower than minimum: %s",
                    max_altitude,
                    radec_array[nblock],
                )
                nblock += 1
                continue
            elif min_am > min_airmass[nblock]:
                log.warning(
                    "Min airmass %7.3f higher than minimum: %7.3f",
                    min_am,
                    min_airmass[nblock],
                )

            # set desired altitudes
            minalt = 90.0 - np.arccos(1.0 / max_airmass[nblock]) * 180.0 / np.pi
            minalt *= 1.1
            desire_alt = np.linspace(minalt, max_altitude, nairmass)
            # set desired airmasses
            dair_mass = 1.0 / np.cos(np.pi / 2.0 - desire_alt * np.pi / 180.0)
            dair_mass.sort()
            # Decide the start and end times for allocation
            start = nightstart
            end = nightend

            # find times where the object is at the desired airmasses
            allocate_slot = np.array([], dtype=slot_dtype)

            start = max(start, nightstart)
            end = min(end, nightend)
            log.debug("Trying to allocate %s", radec_array[nblock])
            nballoc_tmp = nballoc

            lst_grid = [site.lst_in_rads(datetime_from_jd(tt)) for tt in time_grid]
            airmass_grid = np.array(
                [
                    _airmass(site.ra_dec_to_alt_az(ra_h, dec_d, lst)[0])
                    for lst in lst_grid
                ]
            )
            min_amidx = np.argmin(airmass_grid)
            for dam in dair_mass:
                converged = False
                time = None

                # Before/after culmination
                if airmass_grid[1] < airmass_grid[0]:
                    dam_grid = np.abs(airmass_grid[:min_amidx] - dam)
                    mm = dam_grid < max_airmass[nblock]
                    log.debug("%s", dam_grid)
                    dam_grid[mm] = np.max(dam_grid)
                    dam_pos = np.argmin(np.abs(airmass_grid[:min_amidx] - dam))
                    if np.abs(airmass_grid[dam_pos] - dam) < 1e-1:
                        time = time_grid[dam_pos]
                        converged = True
                    else:
                        dam_pos = np.argmin(np.abs(airmass_grid - dam))
                        mm = dam_grid < max_airmass[nblock]
                        dam_grid[mm] = np.max(dam_grid)
                        if np.abs(airmass_grid[dam_pos] - dam) < 1e-1:
                            time = time_grid[dam_pos]
                            converged = True
                else:
                    dam_grid = np.abs(airmass_grid[min_amidx:] - dam)
                    mm = dam_grid < max_airmass[nblock]
                    log.debug("%s", dam_grid)
                    dam_grid[mm] = np.max(dam_grid)
                    dam_pos = np.argmin(np.abs(airmass_grid[min_amidx:] - dam))
                    if np.abs(airmass_grid[dam_pos] - dam) < 1e-1:
                        time = time_grid[dam_pos]
                        converged = True
                    else:
                        dam_pos = np.argmin(np.abs(airmass_grid - dam))
                        mm = dam_grid < max_airmass[nblock]
                        dam_grid[mm] = np.max(dam_grid)
                        if np.abs(airmass_grid[dam_pos] - dam) < 1e-1:
                            time = time_grid[dam_pos]
                            converged = True

                if not converged:
                    break

                filled = False
                # Found time, try to allocate
                for islot in range(len(obs_slots)):
                    if obs_slots["start"][islot] < time < obs_slots["end"][islot]:
                        filled = True
                        log.debug(
                            "Slot[%i] filled %.3f/%.3f @ %.3f",
                            islot,
                            obs_slots["start"][islot],
                            obs_slots["end"][islot],
                            time,
                        )
                        break

                if filled:
                    break

                # Check that it complies with the block constraints.
                # Airmass should be ok since allocation is airmass based,
                # so we only need to check moon distance and brightness.
                _date_time = datetime_from_jd(time)
                lst = site.lst_in_rads(_date_time)
                moon_ra, moon_dec = site.moon_ra_dec(_date_time)
                moon_alt, _ = site.ra_dec_to_alt_az(moon_ra, moon_dec, lst)
                # check that the moon is above the horizon!
                if moon_alt > 0.0:
                    moon_ra_dec = Position.from_ra_dec(moon_ra, moon_dec)
                    moon_brightness = site.moon_phase(_date_time) * 100.0
                    s_target = rows[nblock]

                    moon_dist = float(radec_array[nblock].angsep(moon_ra_dec))
                    if (moon_dist < s_target[1].min_moon_distance) or not (
                        s_target[1].min_moon_bright
                        < moon_brightness
                        < s_target[1].max_moon_bright
                    ):
                        log.warning(
                            "Cannot allocate target due to moon restrictions..."
                        )
                        log.debug(
                            "Moon Conditions @ %s: Target@ %s | Moon@: %s | "
                            "AngSep: %.2f (min.: %.2f) | Moon Brightness: %.2f "
                            "(%.2f:%.2f)",
                            time,
                            radec_array[nblock],
                            moon_ra_dec,
                            moon_dist,
                            s_target[1].min_moon_distance,
                            moon_brightness,
                            s_target[1].min_moon_bright,
                            s_target[1].max_moon_bright,
                        )
                        break

                if nightstart <= time < nightend:
                    allocate_slot = np.append(
                        allocate_slot,
                        np.array(
                            [
                                (
                                    time,
                                    time + block_duration[nblock] / 60.0 / 60.0 / 24.0,
                                    nballoc_tmp,
                                    blockid_list[nblock],
                                    True,
                                )
                            ],
                            dtype=slot_dtype,
                        ),
                    )
                else:
                    log.warning(
                        "Wrong time stamp. time: %.4f (%.4f/%.4f)",
                        time,
                        nightstart,
                        nightend,
                    )
                    break
                nballoc_tmp += 1

                start = nightstart
                end = nightend

                if olst > lstmid:
                    end = midnight + (olst - lstmid) * 12.0 / np.pi / 24.0
                else:
                    start = midnight - (lstmid - olst) * 12.0 / np.pi / 24.0

            if len(allocate_slot) == nairmass:
                log.info("Allocating...")
                obs_slots = np.append(obs_slots, allocate_slot)
                keep_mask = np.zeros_like(time_grid) == 0
                for islot in range(len(obs_slots)):
                    keep_mask = np.bitwise_and(
                        keep_mask,
                        np.bitwise_not(
                            np.bitwise_and(
                                time_grid > obs_slots["start"][islot],
                                time_grid < obs_slots["end"][islot],
                            )
                        ),
                    )
                time_grid = time_grid[keep_mask]
                nalloc += 1
                nballoc += nballoc_tmp
            else:
                nballoc_tmp = 0
                log.debug("Failed...")
            nblock += 1

        if nalloc < nstars:
            log.warning(
                "Could not find enough stars.. Found %i of %i...", nalloc, nstars
            )

        return obs_slots

    @staticmethod
    def next(time, programs):
        log.debug("Selecting target with ExtinctionMonitor algorithm.")

        site = ExtinctionMonitor.site
        mjd = time
        lst = site.lst_in_rads(datetime_from_jd(time + 2400000.5))

        observe_program = None
        waittime = 1.0
        slew_at = mjd

        session = get_session()

        try:
            for program in programs:
                extmoni_info = (
                    session.query(ExtMoniDB)
                    .filter(
                        ExtMoniDB.pid == program[0].pid,
                        ExtMoniDB.target_id == program[0].target_id,
                    )
                    .first()
                )
                if extmoni_info is None:
                    log.warning(
                        "Program %s not in extinction monitor database. Skipping.",
                        program[0],
                    )
                    continue

                ra_h = program[3].target_ra
                dec_d = program[3].target_dec
                # set desired altitudes
                max_airmass = program[1].max_airmass
                minalt = 90.0 - np.arccos(1.0 / max_airmass) * 180.0 / np.pi
                minalt *= 1.1

                olst = np.radians(ra_h * 15.0) * 0.999
                maxalt, _ = site.ra_dec_to_alt_az(ra_h, dec_d, olst)

                # add 1 to nairmass so the values can be treated as boundaries
                desire_alt = np.linspace(minalt, maxalt, extmoni_info.nairmass + 1)

                covered = False
                if program[0].slew_at < mjd:
                    log.debug(
                        "Slew time has passed. Calculating target's current altitude."
                    )
                    alt, _ = site.ra_dec_to_alt_az(ra_h, dec_d, lst)

                    if not (minalt < alt < maxalt):
                        log.debug(
                            "Target altitude (%.2f) outside limit (%.2f/%.2f)",
                            alt,
                            minalt,
                            maxalt,
                        )
                        continue

                    level = np.where(desire_alt <= alt)[0][-1]
                    log.debug("Checking if this altitude position was already covered.")

                    for observed_am in extmoni_info.observed_am:
                        level_covered = np.where(desire_alt <= observed_am.altitude)[0][
                            -1
                        ]
                        if level == level_covered:
                            log.debug("Position already covered, continue.")
                            covered = True
                            break
                else:
                    log.debug(
                        "Slew still in the future, try to find a good time to slew "
                        "between now and then"
                    )
                    log.debug(
                        "Now: %.4f | Slew@: %.2f | Altitude: min/max: %.2f/%.2f",
                        mjd,
                        program[0].slew_at,
                        minalt,
                        maxalt,
                    )
                    slew_at = program[0].slew_at
                    for tt in np.linspace(mjd, program[0].slew_at, 10):
                        observe_lst = site.lst_in_rads(datetime_from_jd(tt + 2400000.5))
                        alt, _ = site.ra_dec_to_alt_az(ra_h, dec_d, observe_lst)
                        log.debug(
                            "Slew@: %.2f (alt/airmass: %.2f/%.3f )",
                            tt,
                            alt,
                            1.0 / np.cos(np.pi / 2.0 - alt * np.pi / 180.0),
                        )

                        if minalt < alt < maxalt:
                            level = np.where(desire_alt <= alt)[0][-1]
                            log.debug(
                                "Check if this altitude position is already covered"
                            )
                            covered = False
                            for observed_am in extmoni_info.observed_am:
                                level_covered = np.where(
                                    desire_alt <= observed_am.altitude
                                )[0][-1]
                                if level == level_covered:
                                    log.debug("Position already covered")
                                    covered = True
                                    break
                                else:
                                    log.debug("Position uncovered")
                                    covered = False

                            if not covered:
                                log.debug("Position uncovered")
                                slew_at = tt
                                break
                            else:
                                log.debug("Position covered. continuing")
                        else:
                            log.debug(
                                "Current altitude (%.2f) out of range (%.2f/%.2f)",
                                alt,
                                minalt,
                                maxalt,
                            )

                if not covered:
                    awaittime = slew_at - mjd
                    if awaittime < 0.0:
                        awaittime = 0.0
                    if awaittime < waittime:
                        observe_program = program
                    break

            if observe_program is not None:
                log.debug("Target ok")
                observe_program[0].slew_at = slew_at
            else:
                log.debug("Could not find suitable target")
        finally:
            session.commit()
        return observe_program

    @staticmethod
    def observed(time, program, site=None, soft=False):
        if site is None:
            site = ExtinctionMonitor.site
        lst = site.lst_in_rads(datetime_from_jd(time + 2400000.5))

        session = get_session()
        try:
            prog = session.merge(program[0])
            prog.finished = True
            obsblock = session.merge(program[2])
            target = session.merge(program[3])
            extmoni_info = (
                session.query(ExtMoniDB)
                .filter(
                    ExtMoniDB.pid == prog.pid, ExtMoniDB.target_id == prog.target_id
                )
                .first()
            )
            if extmoni_info is None:
                raise ExtinctionMonitorError(
                    f"Could not find program {prog.pid} in the database."
                )

            alt, _ = site.ra_dec_to_alt_az(target.target_ra, target.target_dec, lst)
            observed_am = ObservedAM(airmass=_airmass(alt), altitude=alt)

            extmoni_info.observed_am.append(observed_am)

            obsblock.observed = True
            # These targets are never completed
            if not soft:
                obsblock.last_observation = site.ut().replace(tzinfo=None)
        finally:
            session.commit()
