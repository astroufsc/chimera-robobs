# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2014-present chimera-robobs authors

"""'Higher in the sky' scheduling algorithm (id 0, name HIG)."""

import logging
from multiprocessing.pool import ThreadPool

import numpy as np
from chimera.util.position import Position

from chimera_robobs.scheduling.algorithms.base import (
    BaseScheduleAlgorithm,
    get_session,
)
from chimera_robobs.scheduling.dates import datetime_from_jd
from chimera_robobs.scheduling.model import ObsBlock

log = logging.getLogger(__name__)

#: numpy dtype of the observing slots returned by process()
SLOT_DTYPE = [
    ("start", float),
    ("end", float),
    ("slotid", int),
    ("blockid", int),
]


class Higher(BaseScheduleAlgorithm):
    @staticmethod
    def name() -> str:
        return "HIG"

    @staticmethod
    def id() -> int:
        return 0

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

        pool_size = 1
        max_sched_blocks = -1
        if "config" in kwargs:
            config = kwargs["config"]
            if "pool_size" in config:
                pool_size = config["pool_size"]
            if "slotLen" in config:
                slot_len = config["slotLen"]
            if "max_sched_blocks" in config:
                max_sched_blocks = config["max_sched_blocks"]

        nightstart = kwargs["obsStart"]
        nightend = kwargs["obsEnd"]
        site = kwargs["site"]

        # Create observation slots.
        obs_slots = np.array(
            np.arange(nightstart, nightend, slot_len / 60.0 / 60.0 / 24.0),
            dtype=SLOT_DTYPE,
        )

        log.debug("Creating %i observing slots", len(obs_slots))

        obs_slots["end"] += slot_len / 60.0 / 60.0 / 24.0
        obs_slots["slotid"] = np.arange(len(obs_slots))
        obs_slots["blockid"] = np.zeros(len(obs_slots)) - 1

        # For each slot select the highest in the sky...
        targets = kwargs["query"]
        rows = targets[:]

        radec_array = np.array(
            [Position.from_ra_dec(rows[0][2].target_ra, rows[0][2].target_dec)]
        )

        moon_par = np.array(
            [
                (
                    row[1].min_moon_distance,
                    row[1].min_moon_bright,
                    row[1].max_moon_bright,
                    row[0].length,
                )
                for row in rows
            ],
            dtype=[
                ("minmoonDist", float),
                ("minmoonBright", float),
                ("maxmoonBright", float),
                ("length", float),
            ],
        )

        radec_pos = np.array([0])

        blockid = rows[0][0].blockid

        for itr, row in enumerate(rows):
            if blockid != row[0].blockid:
                radec_array = np.append(
                    radec_array,
                    Position.from_ra_dec(row[2].target_ra, row[2].target_dec),
                )
                blockid = row[0].blockid
                radec_pos = np.append(radec_pos, itr)

        mask = np.zeros(len(radec_array)) == 0
        nblocks_scheduled = 0

        for itr in range(len(obs_slots)):
            # this "if" is the key to multitarget blocks...
            if obs_slots["blockid"][itr] != -1:
                log.warning(
                    "Observing slot[%i]@%.4f is already filled with block id %i...",
                    itr,
                    obs_slots["start"][itr],
                    obs_slots["blockid"][itr],
                )
                continue

            date_time = datetime_from_jd(obs_slots["start"][itr])

            lst = site.lst_in_rads(date_time)  # in radians

            # Apply moon exclusion radius...
            moon_ra, moon_dec = site.moon_ra_dec(date_time)
            moon_ra_dec = Position.from_ra_dec(moon_ra, moon_dec)
            moon_alt, _ = site.ra_dec_to_alt_az(moon_ra, moon_dec, lst)

            moon_brightness = site.moon_phase(date_time) * 100.0

            if (
                not (
                    moon_par["minmoonBright"].max()
                    < moon_brightness
                    < moon_par["maxmoonBright"].min()
                )
            ) and (moon_alt > 0.0):
                log.warning(
                    "Slot[%03i]: Moon brightness (%5.1f%%) out of range "
                    "(%5.1f%% -> %5.1f%%). Moon alt. = %6.2f. Skipping this slot...",
                    itr + 1,
                    moon_brightness,
                    moon_par["minmoonBright"].max(),
                    moon_par["maxmoonBright"].min(),
                    moon_alt,
                )
                continue

            # Calculate target parameters
            log.debug("Starting slow loop")

            target_par = np.zeros(
                len(radec_array),
                dtype=[
                    ("altitude", float),
                    ("start_altitude", float),
                    ("end_altitude", float),
                    ("moonD", float),
                    ("minmoonD", float),
                    ("mask_moonBright", bool),
                ],
            )

            def worker(index):
                try:
                    # legacy behavior: block length in seconds treated as an
                    # arcsecond offset converted to radians
                    time_offset = np.radians(moon_par["length"][index] / 3600.0)
                    ra_h = radec_array[index].ra.hour
                    dec_d = radec_array[index].dec.deg
                    target_par[index] = (
                        site.ra_dec_to_alt_az(ra_h, dec_d, lst + time_offset / 2.0)[0],
                        site.ra_dec_to_alt_az(ra_h, dec_d, lst)[0],
                        site.ra_dec_to_alt_az(ra_h, dec_d, lst + time_offset)[0],
                        float(radec_array[index].angsep(moon_ra_dec)),
                        moon_par["minmoonDist"][index],
                        (
                            moon_par["minmoonBright"][index]
                            < moon_brightness
                            < moon_par["maxmoonBright"][index]
                        )
                        or (moon_alt < 0.0),
                    )
                except Exception:
                    log.exception("error computing target parameters")

            pool = ThreadPool(pool_size)
            for i in range(len(radec_array)):
                pool.apply_async(worker, (i,))

            log.debug("Starting pool")
            pool.close()
            pool.join()
            log.debug("Pool done")

            # Create moon mask
            moon_mask = np.bitwise_and(
                target_par["moonD"] > target_par["minmoonD"],
                target_par["mask_moonBright"],
            )

            mapping = np.arange(len(mask))[moon_mask]
            tmp_radec_array = np.array(radec_array[moon_mask], copy=True)
            tmp_radec_pos = np.array(radec_pos[moon_mask], copy=True)

            if len(tmp_radec_array) == 0:
                log.warning("Slot[%03i]: Could not find suitable target", itr + 1)
                continue

            alt = target_par["altitude"][moon_mask]

            stg = alt.argmax()
            start_alt = target_par["start_altitude"][moon_mask][stg]
            end_alt = target_par["end_altitude"][moon_mask][stg]

            # Check airmass
            slot_airmass = 1.0 / np.cos(np.pi / 2.0 - alt[stg] * np.pi / 180.0)
            start_airmass = 1.0 / np.cos(np.pi / 2.0 - start_alt * np.pi / 180.0)
            end_airmass = 1.0 / np.cos(np.pi / 2.0 - end_alt * np.pi / 180.0)
            max_airmass = rows[radec_pos[stg]][1].max_airmass
            # Since this is the highest at this time, it doesn't make sense
            # to iterate over it.
            if (
                start_airmass > max_airmass
                or end_airmass > max_airmass
                or slot_airmass < 0.0
                or start_alt < 0.0
            ):
                log.info(
                    "Object too low in the sky, (Alt.=%6.2f) airmass = "
                    "%5.2f/%5.2f/%5.2f (max = %5.2f)... Skipping this slot..",
                    alt[stg],
                    start_airmass,
                    slot_airmass,
                    end_airmass,
                    max_airmass,
                )
                continue

            s_target = rows[tmp_radec_pos[stg]]

            log.info(
                "Slot[%03i] @%.3f: %s %s (Alt.=%6.2f, airmass=%5.2f (max=%5.2f))",
                itr + 1,
                obs_slots["start"][itr],
                s_target[0],
                s_target[2],
                start_alt,
                slot_airmass,
                s_target[1].max_airmass,
            )

            mask[mapping[stg]] = False
            radec_array = radec_array[mask]
            radec_pos = radec_pos[mask]
            moon_par = moon_par[mask]
            mask = mask[mask]
            obs_slots["blockid"][itr] = s_target[0].blockid
            nblocks_scheduled += 1
            if 0 < max_sched_blocks <= nblocks_scheduled:
                log.info(
                    "Maximum number of scheduled blocks (%i) reached. Stopping.",
                    max_sched_blocks,
                )
                break

            # Check if this block has more targets...
            sec_targets = targets.filter(
                ObsBlock.blockid == s_target[0].blockid,
                ObsBlock.target_id != s_target[0].target_id,
            )

            if sec_targets.count() > 0:
                log.debug("Secondary targets not implemented yet...")

            if len(mask) == 0:
                break

        return obs_slots

    @staticmethod
    def next(time, programs):
        programs = programs[:]
        if len(programs) == 0:
            return None
        dt = np.array([np.abs(time - program[0].slew_at) for program in programs])
        iprog = np.argmin(dt)
        return programs[iprog]

    @staticmethod
    def observed(time, program, site=None, soft=False):
        """Process program as observed."""
        session = get_session()
        try:
            prog = session.merge(program[0])
            prog.finished = True
            obsblock = session.merge(program[2])
            obsblock.observed = True
            if not soft:
                obsblock.completed = True
                obsblock.last_observation = site.ut().replace(tzinfo=None)
        finally:
            session.commit()

    @staticmethod
    def timed_constraint() -> bool:
        return False
