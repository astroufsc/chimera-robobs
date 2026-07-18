# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2014-present chimera-robobs authors

"""'Higher in the sky' scheduling algorithm (id 0, name HIG).

Also hosts the slot-allocation loop shared with :class:`TimeSequence`
(which subclasses :class:`Higher` and only flips two behavior flags).
"""

import logging
from multiprocessing.pool import ThreadPool

import numpy as np
from chimera.util.position import Position

from chimera_robobs.scheduling.algorithms.base import (
    BaseScheduleAlgorithm,
    airmass,
)
from chimera_robobs.scheduling.dates import SECONDS_PER_DAY, datetime_from_jd
from chimera_robobs.scheduling.model import ObsBlock

log = logging.getLogger(__name__)

#: numpy dtype of the observing slots returned by process()
SLOT_DTYPE = [
    ("start", float),
    ("end", float),
    ("slotid", int),
    ("blockid", int),
]

#: dtype of the per-block moon/length parameters used by the allocation loop
MOON_PAR_DTYPE = [
    ("min_moon_distance", float),
    ("min_moon_bright", float),
    ("max_moon_bright", float),
    ("length", float),
]


class Higher(BaseScheduleAlgorithm):
    id = 0
    name = "HIG"
    default_slot_len = 60.0
    timed_constraint = False

    #: keep the selected target as a candidate for later slots
    #: (TimeSequence flips this to build monitoring sequences)
    keep_selected_target = False
    #: also require the end-of-slot airmass/altitude to be in range
    check_end_airmass = True

    def process(self, *, obs_start, obs_end, query, config=None, slot_len=None):
        config = config or {}
        slot_len = self._slot_len(config, slot_len)
        pool_size = int(config.get("pool_size", 1))
        max_sched_blocks = int(config.get("max_sched_blocks", -1))
        site = self.site

        # Create observation slots.
        slot_len_days = slot_len / SECONDS_PER_DAY
        starts = np.arange(obs_start, obs_end, slot_len_days)
        obs_slots = np.zeros(len(starts), dtype=SLOT_DTYPE)
        obs_slots["start"] = starts
        obs_slots["end"] = starts + slot_len_days
        obs_slots["slotid"] = np.arange(len(starts))
        obs_slots["blockid"] = -1

        log.debug("Creating %i observing slots", len(obs_slots))

        # For each slot select the highest in the sky...
        rows = query[:]

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
            dtype=MOON_PAR_DTYPE,
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
                    moon_par["min_moon_bright"].max()
                    < moon_brightness
                    < moon_par["max_moon_bright"].min()
                )
            ) and (moon_alt > 0.0):
                log.warning(
                    "Slot[%03i]: Moon brightness (%5.1f%%) out of range "
                    "(%5.1f%% -> %5.1f%%). Moon alt. = %6.2f. Skipping this slot...",
                    itr + 1,
                    moon_brightness,
                    moon_par["min_moon_bright"].max(),
                    moon_par["max_moon_bright"].min(),
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
                    ("moon_distance", float),
                    ("min_moon_distance", float),
                    ("moon_bright_ok", bool),
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
                        moon_par["min_moon_distance"][index],
                        (
                            moon_par["min_moon_bright"][index]
                            < moon_brightness
                            < moon_par["max_moon_bright"][index]
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
                target_par["moon_distance"] > target_par["min_moon_distance"],
                target_par["moon_bright_ok"],
            )

            mapping = np.arange(len(radec_array))[moon_mask]
            masked_radec_pos = radec_pos[moon_mask]

            if len(masked_radec_pos) == 0:
                log.warning("Slot[%03i]: Could not find suitable target", itr + 1)
                continue

            alt = target_par["altitude"][moon_mask]

            stg = alt.argmax()
            start_alt = target_par["start_altitude"][moon_mask][stg]
            end_alt = target_par["end_altitude"][moon_mask][stg]

            # Check airmass.  Legacy quirk (preserved): max_airmass is looked
            # up with the *unmasked* index.
            slot_airmass = airmass(alt[stg])
            start_airmass = airmass(start_alt)
            end_airmass = airmass(end_alt)
            max_airmass = rows[radec_pos[stg]][1].max_airmass
            # Since this is the highest at this time, it doesn't make sense
            # to iterate over it.
            too_low = start_airmass > max_airmass or slot_airmass >= 999.0
            if self.check_end_airmass:
                too_low = too_low or end_airmass > max_airmass or start_alt < 0.0
            if too_low:
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

            s_target = rows[masked_radec_pos[stg]]

            log.info(
                "Slot[%03i] @%.3f: %s %s (Alt.=%6.2f, airmass=%5.2f (max=%5.2f))",
                itr + 1,
                obs_slots["start"][itr],
                s_target[0],
                s_target[2],
                alt[stg],
                slot_airmass,
                s_target[1].max_airmass,
            )

            if not self.keep_selected_target:
                keep = np.ones(len(radec_array), dtype=bool)
                keep[mapping[stg]] = False
                radec_array = radec_array[keep]
                radec_pos = radec_pos[keep]
                moon_par = moon_par[keep]

            obs_slots["blockid"][itr] = s_target[0].blockid
            nblocks_scheduled += 1
            if 0 < max_sched_blocks <= nblocks_scheduled:
                log.info(
                    "Maximum number of scheduled blocks (%i) reached. Stopping.",
                    max_sched_blocks,
                )
                break

            # Check if this block has more targets...
            sec_targets = query.filter(
                ObsBlock.blockid == s_target[0].blockid,
                ObsBlock.target_id != s_target[0].target_id,
            )

            if sec_targets.count() > 0:
                log.debug("Secondary targets not implemented yet...")

            if len(radec_array) == 0:
                break

        return obs_slots

    def next(self, now_mjd, programs):
        programs = programs[:]
        if len(programs) == 0:
            return None
        dt = np.array([np.abs(now_mjd - program[0].slew_at) for program in programs])
        iprog = np.argmin(dt)
        return programs[iprog]

    def observed(self, time, program, soft=False):
        """Process program as observed."""
        session = self.session()
        try:
            prog = session.merge(program[0])
            prog.finished = True
            obsblock = session.merge(program[2])
            obsblock.observed = True
            if not soft:
                obsblock.completed = True
                obsblock.last_observation = self.site.ut().replace(tzinfo=None)
        finally:
            session.commit()
