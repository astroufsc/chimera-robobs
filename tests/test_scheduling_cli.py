# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2014-present chimera-robobs authors

import datetime as dt
import glob
import math
from types import SimpleNamespace

import pytest

from chimera_robobs.cli import robobs as cli
from chimera_robobs.scheduling import model

from .fakes import FakeSite

PROJECT_YAML = """\
project:
  pid: P01
  pi: "A. Investigator"
  abstract: "Legacy-format project file"
  url: "http://example.org/p01"
  priority: {priority}

observing_blocks:
  block1:
    id: 1
    pid: P01
    maxairmass: 1.9
    minairmass: 1.0
    maxmoonBright: 99.0
    minmoonBright: 0.0
    minmoonDist: 25.0
    maxseeing: 2.5
    cloudcover: 1
    schedalgorith: 0
    applyextcorr: false
"""

TARGETS_CSV = """\
RA,DEC,NAME,TYPE,MAG,EPOCH,MAGFILTER
10:00:00,-20:00:00,NGC0001,OBJECT,12.5,2000,V
11:30:00,+05:00:00,NGC0002,OBJECT,13.0,2000,V
not-a-coord,also-bad,BROKEN,OBJECT,0,2000,V
"""

BLOCK_YAML = """\
pre-actions:
  - action: expose
    frames: 1
    exptime: 0
    imageType: BIAS
    shutter: CLOSE
    filename: "bias-{name}"

pos-actions:
  - action: expose
    filter: R
    frames: 2
    exptime: 20.5
    imageType: OBJECT
    shutter: OPEN
    objectName: "{name}"
    filename: "{pid}-{name}"
  - action: point
    offset:
      north: 30
"""


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "robobs.db")


def _run(db, *argv):
    return cli.main(["--database", db, *argv])


def _write(tmp_path, name, content):
    path = tmp_path / name
    path.write_text(content)
    return str(path)


def _session(db):
    return model.open_database(db)()


def test_add_project_creates_and_updates(tmp_path, db):
    filename = _write(tmp_path, "project.yaml", PROJECT_YAML.format(priority=1))
    assert _run(db, "add-project", "-f", filename) == 0

    session = _session(db)
    project = session.query(model.Projects).one()
    assert project.pid == "P01"
    assert project.priority == 1

    blockpar = session.query(model.BlockPar).one()
    # legacy YAML keys must map onto the new column names
    assert blockpar.bid == 1
    assert blockpar.pid == "P01"
    assert blockpar.max_airmass == 1.9
    assert blockpar.min_airmass == 1.0
    assert blockpar.max_moon_bright == 99.0
    assert blockpar.min_moon_distance == 25.0
    assert blockpar.max_seeing == 2.5
    assert blockpar.cloud_cover == 1
    assert blockpar.sched_algorithm == 0
    assert blockpar.apply_ext_corr is False

    # running again updates instead of duplicating
    filename = _write(tmp_path, "project.yaml", PROJECT_YAML.format(priority=7))
    assert _run(db, "add-project", "-f", filename) == 0
    session = _session(db)
    assert session.query(model.Projects).count() == 1
    assert session.query(model.Projects).one().priority == 7
    assert session.query(model.BlockPar).count() == 1


def test_add_targets_from_csv(tmp_path, db):
    filename = _write(tmp_path, "targets.csv", TARGETS_CSV)
    assert _run(db, "add-targets", "-f", filename) == 0

    session = _session(db)
    targets = session.query(model.Targets).order_by(model.Targets.id).all()
    # the invalid row is skipped with a warning
    assert [t.name for t in targets] == ["NGC0001", "NGC0002"]
    assert targets[0].target_ra == pytest.approx(10.0)
    assert targets[0].target_dec == pytest.approx(-20.0)
    assert targets[0].target_mag == pytest.approx(12.5)
    assert targets[0].mag_filter == "V"
    assert targets[1].target_ra == pytest.approx(11.5)
    assert targets[1].target_dec == pytest.approx(5.0)


def test_add_observing_block(tmp_path, db):
    _run(db, "add-project", "-f", _write(tmp_path, "p.yaml", PROJECT_YAML.format(priority=1)))
    _run(db, "add-targets", "-f", _write(tmp_path, "t.csv", TARGETS_CSV))

    block_yaml = _write(tmp_path, "block.yaml", BLOCK_YAML)
    blocks_txt = _write(tmp_path, "blocks.txt", f"P01 1 1 {block_yaml} 1\n")

    assert _run(db, "add-observing-block", "-f", blocks_txt) == 0

    session = _session(db)
    block = session.query(model.ObsBlock).one()
    blockpar = session.query(model.BlockPar).one()
    target = session.query(model.Targets).filter(model.Targets.name == "NGC0001").one()

    assert block.pid == "P01"
    assert block.blockid == 1
    assert block.target_id == target.id
    # the FK now points at the blockpar PRIMARY KEY, resolved from (pid, bid)
    assert block.block_par_id == blockpar.id

    # pre-action expose + slew point + pos-action expose + pos-action point
    assert len(block.actions) == 4
    bias, slew, science, offset_point = block.actions

    assert isinstance(bias, model.Expose)
    assert bias.image_type == "BIAS"  # legacy imageType key
    assert bias.filename == "bias-NGC0001"  # {name} template

    assert isinstance(slew, model.Point)
    assert slew.target_ra_dec is not None

    assert isinstance(science, model.Expose)
    assert science.exptime == pytest.approx(20.5)  # Float, not truncated
    assert science.frames == 2
    assert science.object_name == "NGC0001"
    assert science.filename == "P01-NGC0001"

    assert isinstance(offset_point, model.Point)
    assert float(offset_point.offset_ns.arcsec) == pytest.approx(30.0)

    # block length: only pos-action exposures count, once per action
    assert block.length == pytest.approx((20.5 + 12.0) * 2)

    # re-adding replaces the block instead of duplicating it
    assert _run(db, "add-observing-block", "-f", blocks_txt) == 0
    session = _session(db)
    assert session.query(model.ObsBlock).count() == 1
    assert session.query(model.Action).count() == 4


def test_clean_commands_backup_the_robobs_database(tmp_path, db):
    _run(db, "add-project", "-f", _write(tmp_path, "p.yaml", PROJECT_YAML.format(priority=1)))
    _run(db, "add-targets", "-f", _write(tmp_path, "t.csv", TARGETS_CSV))

    assert _run(db, "clean-targets") == 0
    session = _session(db)
    assert session.query(model.Targets).count() == 0
    # the backup must be a copy of the robobs database itself
    # (the legacy tool copied an unrelated database)
    assert glob.glob(db + ".*.bak")

    assert _run(db, "delete-project", "--pid", "P01") == 0
    session = _session(db)
    assert session.query(model.Projects).count() == 0
    assert session.query(model.BlockPar).count() == 0


def test_delete_and_clean_observing_blocks(tmp_path, db):
    _run(db, "add-project", "-f", _write(tmp_path, "p.yaml", PROJECT_YAML.format(priority=1)))
    _run(db, "add-targets", "-f", _write(tmp_path, "t.csv", TARGETS_CSV))
    block_yaml = _write(tmp_path, "block.yaml", BLOCK_YAML)
    blocks_txt = _write(tmp_path, "blocks.txt", f"P01 1 1 {block_yaml} 1\n")
    _run(db, "add-observing-block", "-f", blocks_txt)

    assert _run(db, "delete-observing-block", "--pid", "OTHER") == 0
    assert _session(db).query(model.ObsBlock).count() == 1

    assert _run(db, "delete-observing-block", "--pid", "P01") == 0
    session = _session(db)
    assert session.query(model.ObsBlock).count() == 0
    assert session.query(model.Action).count() == 0

    _run(db, "add-observing-block", "-f", blocks_txt)
    assert _run(db, "clean-observing-blocks") == 0
    assert _session(db).query(model.ObsBlock).count() == 0


def test_clean_queue(tmp_path, db):
    _run(db, "add-project", "-f", _write(tmp_path, "p.yaml", PROJECT_YAML.format(priority=1)))
    session = _session(db)
    session.add(model.Program(pid="P01", name="x", priority=1))
    session.add(model.ObsBlock(pid="P01", blockid=1, scheduled=True))
    session.commit()

    assert _run(db, "clean-queue", "--pid", "P01") == 0
    session = _session(db)
    assert session.query(model.Program).count() == 0
    assert session.query(model.ObsBlock).one().scheduled is False

    # unknown project id fails and lists the available projects
    assert _run(db, "clean-queue", "--pid", "NOPE") == 1


def test_observing_log_show(tmp_path, db, capsys):
    session = model.open_database(db)()
    for hour, action in ((1, "one"), (2, "two"), (3, "three")):
        session.add(
            model.ObservingLog(
                time=dt.datetime(2026, 7, 6, hour, 0, 0),
                tid=1,
                name="tgt",
                priority=1,
                action=action,
            )
        )
    session.commit()

    assert _run(db, "observing-log") == 0
    out = capsys.readouterr().out
    assert "one" in out and "two" in out and "three" in out

    assert (
        _run(
            db,
            "observing-log",
            "--start",
            "2026/07/06-01:30:00",
            "--end",
            "2026-07-06T02:30:00",
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "two" in out
    assert "one" not in out and "three" not in out


def test_parse_when_formats():
    assert cli._parse_when("2026/07/06-18:30:00") == dt.datetime(2026, 7, 6, 18, 30, 0)
    assert cli._parse_when("2026-07-06T18:30:00") == dt.datetime(2026, 7, 6, 18, 30, 0)


def test_make_times_with_overrides():
    site = FakeSite(lst_rads=math.pi)  # LST = 12 h
    args = SimpleNamespace(
        jd_start=None, jd_end=None, date_start=None, date_end=None,
        lst_start=None, lst_end=None,
    )
    times = cli.make_times(args, site)
    assert times.obs_start == site.sunset_twilight_end()
    assert times.obs_end - times.obs_start == dt.timedelta(hours=12)
    assert times.lst_start == pytest.approx(12.0)
    assert times.jd_end - times.jd_start == pytest.approx(0.5)

    args.jd_start = 2461000.5
    args.date_end = "2026/07/07-10:00:00"
    args.lst_end = 20.0
    times = cli.make_times(args, site)
    assert times.jd_start == pytest.approx(2461000.5)
    assert times.obs_end == dt.datetime(2026, 7, 7, 10, 0, 0)
    assert times.lst_end == 20.0


def test_select_blocks_lst_window(tmp_path, db):
    factory = model.open_database(db)
    session = factory()

    project = model.Projects(pid="P01", priority=1)
    session.add(project)
    blockpar = model.BlockPar(bid=1, pid="P01")
    session.add(blockpar)
    session.commit()

    for i, ra in enumerate((1.0, 10.0, 23.0)):
        target = model.Targets(name=f"t{i}", target_ra=ra, target_dec=0.0)
        session.add(target)
        session.commit()
        session.add(
            model.ObsBlock(
                target_id=target.id, blockid=i + 1, pid="P01",
                block_par_id=blockpar.id,
            )
        )
        session.commit()

    # plain window
    rows = cli.select_blocks(session, "P01", 8.0, 12.0)[:]
    assert [r[2].target_ra for r in rows] == [10.0]

    # wrap-around window (22h -> 2h)
    rows = cli.select_blocks(session, "P01", 22.0, 2.0)[:]
    assert sorted(r[2].target_ra for r in rows) == [1.0, 23.0]
