# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2014-present chimera-robobs authors

"""Date/time helpers for the robobs scheduling code.

chimera 0.2 removed ``chimera.core.site.datetimeFromJD``; these are small
local replacements.  The chimera bus serializes values as JSON, so datetimes
returned by a remote ``Site`` proxy arrive as ISO strings and datetimes sent
as arguments must be formatted in a form pyephem understands
("YYYY/MM/DD HH:MM:SS", UTC).
"""

import datetime as dt

#: Julian date of the unix epoch (1970-01-01T00:00:00 UTC)
JD_UNIX_EPOCH = 2440587.5

#: JD = MJD + MJD_JD_OFFSET
MJD_JD_OFFSET = 2400000.5

SECONDS_PER_DAY = 86400.0


def datetime_from_jd(jd: float) -> dt.datetime:
    """Convert a julian date to a timezone-aware UTC datetime."""
    return dt.datetime(1970, 1, 1, tzinfo=dt.UTC) + dt.timedelta(
        days=jd - JD_UNIX_EPOCH
    )


def jd_from_datetime(date: dt.datetime) -> float:
    """Convert a datetime (naive datetimes are assumed UTC) to a julian date."""
    if date.tzinfo is None:
        date = date.replace(tzinfo=dt.UTC)
    delta = date - dt.datetime(1970, 1, 1, tzinfo=dt.UTC)
    return JD_UNIX_EPOCH + delta.total_seconds() / SECONDS_PER_DAY


def datetime_from_mjd(mjd: float) -> dt.datetime:
    """Convert a modified julian date to a timezone-aware UTC datetime."""
    return datetime_from_jd(mjd + MJD_JD_OFFSET)


def mjd_from_datetime(date: dt.datetime) -> float:
    """Convert a datetime (naive datetimes are assumed UTC) to a MJD."""
    return jd_from_datetime(date) - MJD_JD_OFFSET


def to_ephem_date(date: dt.datetime) -> str:
    """Format a datetime as a pyephem-compatible UTC date string."""
    if date.tzinfo is not None:
        date = date.astimezone(dt.UTC).replace(tzinfo=None)
    return date.strftime("%Y/%m/%d %H:%M:%S")


def ensure_datetime(value: dt.datetime | str) -> dt.datetime:
    """Return ``value`` as a datetime.

    Values crossing the chimera bus are JSON-encoded, so a datetime returned
    by a proxy arrives as an ISO-8601 string.
    """
    if isinstance(value, dt.datetime):
        return value
    return dt.datetime.fromisoformat(value)
