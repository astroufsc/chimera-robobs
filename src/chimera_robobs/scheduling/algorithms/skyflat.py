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
from chimera_robobs.scheduling.dates import datetime_from_jd, jd_from_datetime
from chimera_robobs.scheduling.model import AutoFlat, SkyFlatDB

log = logging.getLogger(__name__)

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
        return min(programs, key=lambda row: row[0].slew_at)

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
                # only: the offline simulation passes soft=True)
                for act in block.actions:
                    if isinstance(act, AutoFlat):
                        session.add(
                            SkyFlatDB(
                                pid=prog.pid,
                                filter=act.filter,
                                frames=act.frames or 0,
                                observed_at=now,
                            )
                        )
        finally:
            session.commit()

    def clean(self, pid):
        """The skyflat ledger is history, not queue state: keep it."""

    def soft_clean(self, pid, block=None):
        pass
