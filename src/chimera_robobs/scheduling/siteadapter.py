# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2014-present chimera-robobs authors

"""Adapter around a chimera ``Site`` proxy for the robobs scheduling code.

The chimera 0.2 bus serializes values as JSON, so a remote ``Site`` can only
exchange scalars, strings and ISO datetimes with us:

* datetimes returned by the proxy arrive as ISO strings — parsed back here;
* datetime arguments are sent as pyephem-style strings ("YYYY/MM/DD HH:MM:SS")
  which ``Site`` accepts for its ``date`` parameters;
* ``Site.moonpos()`` returns a ``Position`` (not JSON-serializable), so the
  moon position and phase are computed locally with pyephem.  The local moon
  ra/dec is geocentric (up to ~1 degree of parallax from the topocentric
  value), which is accurate enough for the moon-distance constraints used
  here.
"""

import datetime as dt

import ephem

from chimera_robobs.scheduling.dates import ensure_datetime, to_ephem_date


class SiteAdapter:
    """Exposes the site interface consumed by the robobs engine/algorithms.

    ``site`` may be a chimera proxy or any object with the same (subset of
    the) ``Site`` API; tests use simple fakes.
    """

    def __init__(self, site):
        self._site = site

    @staticmethod
    def _date_arg(date: dt.datetime | str | None):
        if isinstance(date, dt.datetime):
            return to_ephem_date(date)
        return date

    # -- time ----------------------------------------------------------
    def ut(self) -> dt.datetime:
        return ensure_datetime(self._site.ut())

    def mjd(self) -> float:
        return float(self._site.mjd())

    def jd(self) -> float:
        return float(self._site.jd())

    def lst_in_rads(self, date: dt.datetime | None = None) -> float:
        if date is None:
            return float(self._site.lst_in_rads())
        return float(self._site.lst_in_rads(self._date_arg(date)))

    # -- twilight ------------------------------------------------------
    def sunset_twilight_end(self, date: dt.datetime | None = None) -> dt.datetime:
        if date is None:
            return ensure_datetime(self._site.sunset_twilight_end())
        return ensure_datetime(self._site.sunset_twilight_end(self._date_arg(date)))

    def sunrise_twilight_begin(self, date: dt.datetime | None = None) -> dt.datetime:
        if date is None:
            return ensure_datetime(self._site.sunrise_twilight_begin())
        return ensure_datetime(self._site.sunrise_twilight_begin(self._date_arg(date)))

    # -- coordinates ----------------------------------------------------
    def ra_dec_to_alt_az(
        self, ra_hours: float, dec_deg: float, lst_in_rads: float
    ) -> tuple[float, float]:
        alt, az = self._site.ra_dec_to_alt_az(
            float(ra_hours), float(dec_deg), float(lst_in_rads)
        )
        return float(alt), float(az)

    # -- moon (computed locally, see module docstring) -------------------
    def _moon(self, date: dt.datetime | None = None) -> ephem.Moon:
        date = date or self.ut()
        moon = ephem.Moon()
        moon.compute(to_ephem_date(date))
        return moon

    def moon_ra_dec(self, date: dt.datetime | None = None) -> tuple[float, float]:
        """Geocentric moon (ra [hours], dec [degrees])."""
        moon = self._moon(date)
        import math

        return math.degrees(float(moon.ra)) / 15.0, math.degrees(float(moon.dec))

    def moon_phase(self, date: dt.datetime | None = None) -> float:
        """Moon illuminated fraction (0-1)."""
        return float(self._moon(date).phase) / 100.0
