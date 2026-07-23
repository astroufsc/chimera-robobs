# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2014-present chimera-robobs authors

"""Sky-flat calibration algorithm (id 5, name SKYFLAT).

Port of the T80S crontab logic (``query_skyflats.py 5 9 evening`` /
``3 9 morning``) into a robobs calibration project.  Each observing block
holds the ``autoflat`` actions for one filter; this algorithm decides

* **when**: one program per selected block in the evening twilight window
  (sunset -> -18 deg dusk) and/or the morning one (-18 deg dawn ->
  sunrise).  The exact sun-altitude gate is the sky-flat controller's job
  (``sun_alt_hi``/``sun_alt_low``); robobs only starts the programs at the
  window edge, in order.
* **which filters**: the blocks whose filters have the fewest flats taken
  in the last ``lookback`` days, from the robobs database's own
  ``skyflatdb`` ledger (T80S queried the reduction pipeline; here
  ``observed()`` records every executed autoflat).  Filters with no ledger
  entry at all sort first.

Execution order follows the sky brightness: blocks are listed in the
project from the most to the least sensitive filter, executed in that
order in the MORNING (sky brightens: sensitive filters while it is still
dark) and reversed in the EVENING (sky dims: least sensitive first) — the
same convention as the T80S ``filter_order`` reversal.

Scheduling config (project ``scheduling:`` section or ``--pid-config``):

``flat_window``
    ``evening`` | ``morning`` | ``both`` (default ``both``).
``n_filters``
    blocks per window: an integer, or a mapping
    ``{evening: 5, morning: 3}`` (default: all blocks).  The morning
    selection excludes the blocks already picked for the evening.
``lookback``
    ledger look-back window in days (default 15, as on T80S).
"""

import datetime as dt
import logging

import numpy as np

from chimera_robobs.scheduling.algorithms.base import BaseScheduleAlgorithm
from chimera_robobs.scheduling.algorithms.higher import SLOT_DTYPE
from chimera_robobs.scheduling.dates import (
    datetime_from_jd,
    datetime_from_mjd,
    jd_from_datetime,
)
from chimera_robobs.scheduling.model import AutoFlat, SkyFlatDB

log = logging.getLogger(__name__)

#: Coarse sun-altitude gate for offering flats at all, in degrees. The
#: precise window is the sky-flat controller's (sun_alt_hi/sun_alt_low);
#: these bounds are deliberately wider so the controller stays
#: authoritative, and only exist to keep flats out of the deep night.
SUN_ALT_HI = 5.0
SUN_ALT_LOW = -25.0

#: spacing between consecutive flat programs of the same window (seconds);
#: only fixes the execution order — the actual pace is set by the sky-flat
#: controller waiting for the right sky level
STAGGER = 60.0


def _block_filters(block) -> list[str]:
    return [act.filter for act in block.actions if isinstance(act, AutoFlat)]


class SkyFlat(BaseScheduleAlgorithm):
    id = 5
    name = "SKYFLAT"
    default_slot_len = 600.0
    timed_constraint = True
    twilight_calibration = True

    def in_twilight_window(self, time: float) -> bool:
        """Coarse sun-altitude gate (see the base class).

        Deliberately WIDER than the sky-flat controller's own
        sun_alt_hi/sun_alt_low: the controller stays authoritative about
        when a flat is actually worth taking, this only keeps the engine
        from offering flats in the middle of the night.
        """
        if self.site is None:
            return True
        try:
            sun_alt = self.site.sun_altitude(datetime_from_mjd(time))
        except Exception:
            log.exception("could not compute the sun altitude; allowing the flat")
            return True
        return SUN_ALT_HI >= sun_alt >= SUN_ALT_LOW

    def _need_order(self, rows, lookback_days):
        """Rows sorted by how much their filters need flats: fewest ledger
        frames in the look-back window first, ties broken by block order."""
        session = self.session()
        try:
            since = self.site.ut().replace(tzinfo=None) - dt.timedelta(
                days=lookback_days
            )
            counts = {}
            for entry in (
                session.query(SkyFlatDB).filter(SkyFlatDB.observed_at > since).all()
            ):
                counts[entry.filter] = counts.get(entry.filter, 0) + (entry.frames or 0)
        finally:
            session.commit()

        def need(indexed_row):
            index, row = indexed_row
            filters = _block_filters(row[0])
            frames = sum(counts.get(f, 0) for f in filters)
            return (frames, index)

        return [row for _, row in sorted(enumerate(rows), key=need)]

    def process(self, *, obs_start, obs_end, query, config=None, slot_len=None):
        config = config or {}
        window = str(config.get("flat_window", "both")).lower()
        if window not in ("evening", "morning", "both"):
            raise ValueError(f"invalid flat_window: {window!r}")
        lookback = float(config.get("lookback", 15.0))

        n_filters = config.get("n_filters")
        if isinstance(n_filters, dict):
            n_evening = n_filters.get("evening")
            n_morning = n_filters.get("morning")
        else:
            n_evening = n_morning = n_filters

        # one row per block, in ingestion (sensitivity) order
        rows = sorted(query[:], key=lambda row: row[0].blockid)
        if not rows:
            return np.zeros(0, dtype=SLOT_DTYPE)

        by_need = self._need_order(rows, lookback)

        evening_rows, morning_rows = [], []
        if window in ("evening", "both"):
            evening_rows = by_need[
                : len(by_need) if n_evening is None else int(n_evening)
            ]
        if window in ("morning", "both"):
            remaining = [row for row in by_need if row not in evening_rows]
            pool = remaining or by_need  # evening took everything: reuse
            morning_rows = pool[: len(pool) if n_morning is None else int(n_morning)]

        # Need decides WHICH filters get a window; sensitivity must decide
        # the ORDER within it, or the brightness matching inverts. The
        # need-order only coincides with sensitivity while the ledger is
        # empty - with real history it put R at the darkest morning sky and
        # CLEAR in the brightest (2026-07-22), the exact opposite of what
        # each filter needs. blockid is the sensitivity ranking (most
        # sensitive first, by convention of the block list).
        evening_rows = sorted(evening_rows, key=lambda row: row[0].blockid)
        morning_rows = sorted(morning_rows, key=lambda row: row[0].blockid)

        dusk = obs_start  # the observing window is dusk(-18) -> dawn(-18)
        dawn = obs_end
        slots = []

        if evening_rows:
            # the sunset immediately before tonight's dusk (the gap is
            # ~1.2-1.5 h; searching from 6 h earlier is safely inside the
            # afternoon)
            sunset = self.site.sunset(datetime_from_jd(dusk) - dt.timedelta(hours=6))
            sunset_jd = jd_from_datetime(sunset)
            # evening: sky dims — least sensitive filters first
            for i, row in enumerate(reversed(evening_rows)):
                start = sunset_jd + i * STAGGER / 86400.0
                slots.append((start, start, len(slots), row[0].blockid))
                log.info(
                    "Evening sky flat %i: block %i (%s) @ %.5f",
                    i,
                    row[0].blockid,
                    ",".join(_block_filters(row[0])),
                    start,
                )

        for i, row in enumerate(morning_rows):
            # morning: sky brightens — most sensitive filters first,
            # starting right at the -18 deg dawn
            start = dawn + i * STAGGER / 86400.0
            slots.append((start, start, len(slots), row[0].blockid))
            log.info(
                "Morning sky flat %i: block %i (%s) @ %.5f",
                i,
                row[0].blockid,
                ",".join(_block_filters(row[0])),
                start,
            )

        return np.array(slots, dtype=SLOT_DTYPE)

    def next(self, now_mjd, programs, check=None):
        """Earliest pending flat program (they execute in slew_at order)."""
        if not programs[:]:
            return None
        chosen = min(programs, key=lambda row: row[0].slew_at)
        # Capture the filters NOW, while the block row is the generation the
        # program was built from. observed() runs after the flats finish,
        # and a clean/reload in between reassigns the obsblock ids - the
        # completion-time lookup then lands on a different filter's block
        # (2026-07-22: a 9-frame CLEAR set entered the ledger as "I", and
        # the need-order selection started chasing phantom coverage).
        chosen[0]._skyflat_ledger = [
            (act.filter, act.frames or 0)
            for act in chosen[2].actions
            if isinstance(act, AutoFlat)
        ]
        return chosen

    def observed(self, time, program, soft=False):
        session = self.session()
        try:
            prog = session.merge(program[0])
            prog.finished = True
            block = session.merge(program[2])
            block.observed = True
            if not soft:
                now = self.site.ut().replace(tzinfo=None)
                block.last_observation = now
                # feed the ledger the selection reads back (production
                # only: the offline simulation passes soft=True).
                # Best source: the frames the controller ACTUALLY took, per
                # its expose_complete events - the fallback filter walk and
                # the sun window mean the configured set is neither the
                # filters nor the counts that ran. Next best: the filters
                # captured by next() at selection time (resolving
                # block.actions HERE trusts an obsblock id that a
                # clean/reload may have reassigned to another filter).
                taken = getattr(program[0], "_skyflat_frames_taken", None)
                if taken:
                    captured = list(taken.items())
                else:
                    captured = getattr(program[0], "_skyflat_ledger", None)
                if captured is None:
                    captured = [
                        (act.filter, act.frames or 0)
                        for act in block.actions
                        if isinstance(act, AutoFlat)
                    ]
                for filter_name, frames in captured:
                    session.add(
                        SkyFlatDB(
                            pid=prog.pid,
                            filter=filter_name,
                            frames=frames,
                            observed_at=now,
                        )
                    )
        finally:
            session.commit()

    def clean(self, pid):
        """The skyflat ledger is history, not queue state: keep it."""

    def soft_clean(self, pid, block=None):
        pass
