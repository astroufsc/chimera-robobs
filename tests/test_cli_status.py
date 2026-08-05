# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2014-present chimera-robobs authors

"""Rendering tests for ``chimera-robobs status`` (no bus: synthetic
snapshots straight into the renderer)."""

from rich.console import Console

from chimera_robobs.cli.status import _fetch, render_status

#: MJD 61230.0 = 2026-07-09 00:00 UT
SNAPSHOT = {
    "schema": 1,
    "time_utc": "2026-07-09T01:30:00+00:00",
    "robobs": {
        "state": "ON",
        "machine": "BUSY",
        "events_connected": True,
        "consecutive_errors": 1,
        "max_consecutive_errors": 3,
        "no_program_on_queue": False,
        "database": "/home/observer/.chimera/robobs.db",
    },
    "night": {
        "now": 61230.0625,  # 01:30 UT
        "dusk": 61229.916,  # 21:59 UT the evening before
        "dawn": 61230.375,  # 09:00 UT
        "is_night": True,
        "lst_hours": 14.2,
        "sun_altitude": -35.0,
        "moon_phase": 0.42,
    },
    "scheduler": {
        "location": "/Scheduler/0",
        "state": "BUSY",
        "queue_len": 2,
        "current_program": {"id": 7, "name": "ETACAR", "pi": "OPD", "priority": -2},
        "current_action": {
            "id": 12,
            "program_id": 7,
            "type": "Expose",
            "description": "expose: exptime=30.00 frames=10 type=OBJECT",
        },
    },
    "programs": [
        {
            "id": 1,
            "pid": "CAL",
            "name": "SKYFLAT",
            "priority": 1,
            "slew_at": 61229.90,
            "algorithm": "skyflat",
            "length": 420.0,
            "chimera_id": None,
            "state": "done",
        },
        {
            "id": 2,
            "pid": "OPOP",
            "name": "ETACAR",
            "priority": 2,
            "slew_at": 61230.06,
            "algorithm": "timed",
            "length": 1800.0,
            "chimera_id": 7,
            "state": "running",
        },
        {
            "id": 3,
            "pid": "FOCUS",
            "name": "SAO 180244",
            "priority": 3,
            "slew_at": 61230.01,  # before "now": overdue
            "algorithm": "recurrent",
            "length": 300.0,
            "chimera_id": None,
            "state": "queued",
        },
    ],
    "occurrences": {
        "FOCUS": {"pending": 2, "committed": 1, "next_execute_at": 61230.125}
    },
    "log": [
        {
            "time_utc": "2026-07-09T01:00:00+00:00",
            "name": "SKYFLAT",
            "priority": 1,
            "action": "ROBOBS: Program End with status ERROR(no flats)",
        }
    ],
    "handed": [],
    "errors": {},
}


def _render(snapshot) -> str:
    console = Console(record=True, width=120, force_terminal=False)
    console.print(render_status(snapshot, "opd-40:6379/RobObs/0"))
    return console.export_text()


def test_render_shows_every_section():
    text = _render(SNAPSHOT)

    assert "opd-40:6379/RobObs/0" in text
    assert "ON" in text and "BUSY" in text
    assert "1/3 consecutive" in text
    # night times rendered as UT, with the MJD kept visible
    assert "2026-07-09 01:30:00 UT" in text
    assert "MJD 61230.06250" in text
    # the queue, its states and the overdue marker
    assert "ETACAR" in text and "running" in text
    assert "SAO 180244" in text and "(overdue)" in text
    assert "SKYFLAT" in text and "done" in text
    assert "skyflat" in text and "recurrent" in text
    assert "1 running, 1 queued, 1 done" in text
    # occurrences and the log tail (with its ERROR entry)
    assert "FOCUS 2 pending +1 committed" in text
    assert "ERROR(no flats)" in text
    # the current program/action from the scheduler
    assert "expose: exptime=30.00" in text


def test_render_survives_a_minimal_snapshot():
    text = _render(
        {
            "schema": 0,
            "robobs": {"state": "OFF", "machine": None},
            "scheduler": {},
            "errors": {"status": "controller has no status()"},
        }
    )
    assert "OFF" in text
    assert "controller has no status()" in text
    assert "no programs in tonight's queue" in text


def test_fetch_falls_back_to_the_state_string():
    class OldProxy:
        def status(self):
            raise AttributeError("unknown method status")

        def state(self):
            return "robstate=ON machine=BUSY"

    snapshot = _fetch(OldProxy())
    assert snapshot["robobs"] == {"state": "ON", "machine": "BUSY"}
    assert "status" in snapshot["errors"]
