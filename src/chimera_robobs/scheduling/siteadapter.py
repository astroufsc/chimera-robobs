# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2014-present chimera-robobs authors

"""Adapter around a chimera ``Site`` proxy for the robobs scheduling code.

The chimera 0.2 bus serializes values as JSON, so a remote ``Site`` can only
exchange scalars, strings and ISO datetimes with us:

* datetimes returned by the proxy arrive as ISO strings — parsed back here;
* datetime arguments are sent as pyephem-style strings ("YYYY/MM/DD HH:MM:SS")
  which ``Site`` accepts for its ``date`` parameters.

Everything else is delegated.  Values that ``Site`` cannot put on the bus
belong in ``Site`` as plain-float accessors, not in a workaround here: the
sun altitude (astroufsc/chimera#275) and the moon position
(``Site.moon_ra_dec()``) were both fixed that way, after local substitutes
here had gone wrong in their own ways.  Parsing datetimes back from ISO
strings is the one job left, and it is really the proxy's
(astroufsc/chimera#288).
"""

import datetime as dt

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

    def sunset(self, date: dt.datetime | None = None) -> dt.datetime:
        """Next sunset (horizon 0) after ``date`` — for sky-flat windows."""
        if date is None:
            return ensure_datetime(self._site.sunset())
        return ensure_datetime(self._site.sunset(self._date_arg(date)))

    def sunrise(self, date: dt.datetime | None = None) -> dt.datetime:
        """Next sunrise (horizon 0) after ``date`` — for sky-flat windows."""
        if date is None:
            return ensure_datetime(self._site.sunrise())
        return ensure_datetime(self._site.sunrise(self._date_arg(date)))

    def sun_altitude(self, date: dt.datetime | None = None) -> float:
        """Sun altitude in DEGREES.

        Uses ``Site.sun_altitude()`` (astroufsc/chimera#275), never
        ``sunpos()``: a ``Position`` is not msgspec-encodable, so reading it
        through a proxy logs

            bus: serialization issue, won't work on remote buses:
            TypeError: Encoding objects of type Position is unsupported

        on every call, and works at all only because robobs happens to
        share a bus with the site. The engine asks for this on every
        twilight-window check of every calibration program it considers, so
        it was one of the noisiest lines in the log (opd-40 2026-07-30).
        """
        if date is None:
            return float(self._site.sun_altitude())
        return float(self._site.sun_altitude(self._date_arg(date)))

    # -- coordinates ----------------------------------------------------
    def ra_dec_to_alt_az(
        self, ra_hours: float, dec_deg: float, lst_in_rads: float
    ) -> tuple[float, float]:
        alt, az = self._site.ra_dec_to_alt_az(
            float(ra_hours), float(dec_deg), float(lst_in_rads)
        )
        return float(alt), float(az)

    # -- moon ------------------------------------------------------------
    def moon_ra_dec(self, date: dt.datetime | None = None) -> tuple[float, float]:
        """Moon apparent TOPOCENTRIC (ra [hours], dec [degrees]).

        Uses ``Site.moon_ra_dec()``. This used to
        recompute the moon here with a bare pyephem ``Moon``, because
        ``Site.moonpos()`` returns a non-encodable ``Position`` - but a bare
        Moon is GEOCENTRIC, up to ~1 degree from the observer's view (the
        moon's horizontal parallax). Harmless against a 10-30 degree
        moon-distance limit, wrong for anything tighter, and there is no
        reason to carry an ephemeris of our own to get a worse answer.
        """
        if date is None:
            ra, dec = self._site.moon_ra_dec()
        else:
            ra, dec = self._site.moon_ra_dec(self._date_arg(date))
        return float(ra), float(dec)

    def moon_phase(self, date: dt.datetime | None = None) -> float:
        """Moon illuminated fraction (0-1)."""
        if date is None:
            return float(self._site.moonphase())
        return float(self._site.moonphase(self._date_arg(date)))
