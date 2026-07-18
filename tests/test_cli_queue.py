# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2014-present chimera-robobs authors

"""End-to-end make-queue / process-queue tests (fake site, no bus)."""

import math

import pytest

from chimera_robobs.cli import robobs as cli
from chimera_robobs.scheduling import model
from chimera_robobs.scheduling.dates import jd_from_datetime

from .fakes import UT, FakeBus, RotatingSite

PROJECT_YAML = """\
project:
  pid: P01
  pi: "A. Investigator"
  abstract: "queue test project"
  url: "http://example.org/p01"
  priority: 1

observing_blocks:
  block1:
    id: 1
    pid: P01
    max_airmass: 5.0
    scheduling_algorithm: higher
"""

TARGETS_CSV = """\
RA,DEC,NAME
10:00:00,+00:00:00,T10
11:00:00,+00:00:00,T11
"""

BLOCK_YAML = """\
post_actions:
  - action: expose
    filter: R
    frames: 1
    exptime: 30
    image_type: OBJECT
    object_name: "{name}"
    filename: "{pid}-{name}"
"""


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "robobs.db")


def _run(db, *argv):
    return cli.main(["--database", db, *argv])


def _session(db):
    return model.open_database(db)()


@pytest.fixture
def populated(db, tmp_path):
    project = tmp_path / "p.yaml"
    project.write_text(PROJECT_YAML)
    targets = tmp_path / "t.csv"
    targets.write_text(TARGETS_CSV)
    block = tmp_path / "block.yaml"
    block.write_text(BLOCK_YAML)

    assert _run(db, "add-project", "-f", str(project)) == 0
    assert _run(db, "add-targets", "-f", str(targets)) == 0

    session = _session(db)
    lines = []
    for i, target in enumerate(session.query(model.Target), start=1):
        lines.append(f"P01 {i} {target.id} {block} 1\n")
    blocks = tmp_path / "blocks.list"
    blocks.write_text("".join(lines))
    assert _run(db, "add-observing-block", "-f", str(blocks)) == 0
    return db


@pytest.fixture
def fake_connect(monkeypatch):
    site = RotatingSite(latitude=0.0, lst_rads=10.0 * math.pi / 12.0, ut_now=UT)
    bus = FakeBus()
    monkeypatch.setattr(cli, "_connect", lambda args, location: (bus, site))
    return bus, site


def _window_args():
    jd_start = jd_from_datetime(UT)
    return [
        "--jd-start",
        str(jd_start),
        "--jd-end",
        str(jd_start + 2.0 / 24.0),
        "--lst-start",
        "9.0",
        "--lst-end",
        "13.0",
    ]


def test_make_queue_end_to_end(populated, fake_connect):
    db = populated
    bus, _ = fake_connect

    assert _run(db, "make-queue", "--pid", "P01", *_window_args()) == 0
    assert bus.shutdown_called

    session = _session(db)
    programs = session.query(model.Program).order_by(model.Program.slew_at).all()
    assert len(programs) == 2
    assert {p.pid for p in programs} == {"P01"}
    assert programs[0].name == "T10"  # culminates first
    assert programs[1].name == "T11"

    # blocks are marked as scheduled
    for block in session.query(model.ObsBlock):
        assert block.scheduled is True

    # re-running refuses to double-schedule
    assert _run(db, "make-queue", "--pid", "P01", *_window_args()) == 0
    session = _session(db)
    assert session.query(model.Program).count() == 2


def test_make_queue_unknown_project(db, fake_connect):
    assert _run(db, "make-queue", "--pid", "NOPE") == 1


def test_process_queue_simulation(populated, fake_connect):
    db = populated
    assert _run(db, "make-queue", "--pid", "P01", *_window_args()) == 0
    assert _run(db, "process-queue", *_window_args()) == 0

    session = _session(db)
    entries = session.query(model.ObservingLog).all()
    starts = [e for e in entries if e.action == "Simulation: Acquisition Start"]
    ends = [e for e in entries if e.action == "Simulation: Acquisition End"]
    assert len(starts) == 2
    assert len(ends) == 2

    # the simulation bookkeeping is reset afterwards so the queue can be
    # executed for real
    for program in session.query(model.Program):
        assert program.finished is False


def test_every_subcommand_has_help():
    for command in (
        "add-project",
        "delete-project",
        "clean-project",
        "add-targets",
        "clean-targets",
        "add-observing-block",
        "clean-observing-blocks",
        "delete-observing-block",
        "make-queue",
        "clean-queue",
        "process-queue",
        "observing-log",
        "plot-log",
        "start",
        "stop",
        "wake",
        "monitor",
    ):
        with pytest.raises(SystemExit) as excinfo:
            cli.main([command, "--help"])
        assert excinfo.value.code == 0


def test_plot_log_simulation(populated, fake_connect, tmp_path):
    """plot-log resurrects the legacy altitude chart with the mysql-branch
    improvements (aborted dashed, moon annotations, -f output)."""
    db = populated
    assert _run(db, "make-queue", "--pid", "P01", *_window_args()) == 0
    assert _run(db, "process-queue", *_window_args()) == 0

    # add an aborted program: a start with no matching end
    session = _session(db)
    target = session.query(model.Target).first()
    session.add(
        model.ObservingLog(
            time=session.query(model.ObservingLog).first().time,
            target_id=target.id,
            name=target.name,
            priority=1,
            action="Simulation: Acquisition Start",
        )
    )
    session.commit()

    out = tmp_path / "plot.png"
    assert _run(db, "plot-log", "--simulation", "-f", str(out), *_window_args()) == 0
    assert out.exists() and out.stat().st_size > 0

    # without entries in the window the command fails cleanly
    empty = tmp_path / "empty.png"
    assert _run(db, "plot-log", "-f", str(empty), *_window_args()) == 1
    assert not empty.exists()


def test_pair_observing_log_marks_aborted(tmp_path):
    import datetime as dt

    from chimera_robobs.cli.robobs import _pair_observing_log

    factory = model.open_database(str(tmp_path / "robobs.db"))
    session = factory()
    target = model.Target(name="tgt", target_ra=10.0, target_dec=0.0)
    session.add(target)
    session.commit()

    t0 = dt.datetime(2026, 7, 6, 1, 0, 0)

    def entry(minutes, action):
        return model.ObservingLog(
            time=t0 + dt.timedelta(minutes=minutes),
            target_id=target.id,
            name=target.name,
            action=action,
        )

    entries = [
        entry(0, "ROBOBS: Program Started"),  # aborted (no end before next)
        entry(10, "ROBOBS: Program Started"),
        entry(20, "ROBOBS: Program End with status OK(None)"),
        entry(30, "ROBOBS: Program Started"),  # trailing aborted
    ]
    programs = _pair_observing_log(session, entries, "Program Started", "Program End")
    assert [p["aborted"] for p in programs] == [True, False, True]
    assert programs[0]["end"] == entries[1].time  # closed at the next start
    assert programs[2]["end"] == entries[3].time + dt.timedelta(minutes=1)
