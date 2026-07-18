# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2014-present chimera-robobs authors

"""Deterministic fakes for the robobs tests (no bus, no hardware)."""

import datetime as dt
import math

from chimera.util.position import Position

from chimera_robobs.scheduling.dates import ensure_datetime, jd_from_datetime

UT = dt.datetime(2026, 7, 6, 5, 0, 0, tzinfo=dt.UTC)

#: sidereal days per solar day
SIDEREAL_RATE = 1.0027379


class FakeSite:
    """Implements the site interface consumed by the robobs engine and
    algorithms (the ``SiteAdapter`` surface), with a fixed LST."""

    def __init__(
        self,
        latitude: float = 0.0,
        lst_rads: float = 0.0,
        ut_now: dt.datetime = UT,
        moon_ra_dec: tuple[float, float] = (12.0, 0.0),
        moon_phase: float = 0.2,
        night_length_hours: float = 12.0,
    ):
        self.latitude = latitude
        self._lst = lst_rads
        self._ut = ut_now
        self._moon = moon_ra_dec
        self._moon_phase = moon_phase
        self._night_length = night_length_hours

    def ut(self) -> dt.datetime:
        return self._ut

    def mjd(self) -> float:
        return jd_from_datetime(self._ut) - 2400000.5

    def jd(self) -> float:
        return jd_from_datetime(self._ut)

    def lst_in_rads(self, date=None) -> float:
        return self._lst

    def sunset_twilight_end(self, date=None) -> dt.datetime:
        return self._ut

    def sunrise_twilight_begin(self, date=None) -> dt.datetime:
        return self._ut + dt.timedelta(hours=self._night_length)

    def ra_dec_to_alt_az(self, ra_hours, dec_deg, lst_in_rads) -> tuple[float, float]:
        return Position.ra_dec_to_alt_az(
            float(ra_hours), float(dec_deg), self.latitude, float(lst_in_rads)
        )

    def moon_ra_dec(self, date=None) -> tuple[float, float]:
        return self._moon

    def moon_phase(self, date=None) -> float:
        return self._moon_phase


class RotatingSite(FakeSite):
    """FakeSite whose LST advances with the date passed in.

    The LST equals ``lst_rads`` at ``ut_now`` and advances at the sidereal
    rate; dates may be datetimes or pyephem-style strings (as sent by
    ``SiteAdapter`` over the fake proxy boundary).
    """

    def _parse(self, date) -> dt.datetime:
        if date is None:
            return self._ut
        if isinstance(date, str):
            return dt.datetime.strptime(date, "%Y/%m/%d %H:%M:%S").replace(
                tzinfo=dt.UTC
            )
        return ensure_datetime(date)

    def lst_in_rads(self, date=None) -> float:
        date = self._parse(date)
        elapsed_days = (date - self._ut).total_seconds() / 86400.0
        return (self._lst + elapsed_days * SIDEREAL_RATE * 2.0 * math.pi) % (
            2.0 * math.pi
        )


class FakeSchedulerProxy:
    """Records the calls the RobObs machine makes on the chimera scheduler."""

    def __init__(self):
        self.calls = []

    def start(self):
        self.calls.append("start")
        return True

    def stop(self):
        self.calls.append("stop")
        return True


class FakeBus:
    def __init__(self):
        self.shutdown_called = False

    def shutdown(self):
        self.shutdown_called = True
