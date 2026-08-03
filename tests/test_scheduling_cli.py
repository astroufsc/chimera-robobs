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
    max_airmass: 1.9
    min_airmass: 1.0
    max_moon_bright: 99.0
    min_moon_bright: 0.0
    min_moon_distance: 25.0
    max_seeing: 2.5
    cloud_cover: 1
    scheduling_algorithm: higher
    apply_ext_corr: false
"""

TARGETS_CSV = """\
RA,DEC,NAME,TYPE,MAG,EPOCH,MAGFILTER
10:00:00,-20:00:00,NGC0001,OBJECT,12.5,2000,V
11:30:00,+05:00:00,NGC0002,OBJECT,13.0,2000,V
not-a-coord,also-bad,BROKEN,OBJECT,0,2000,V
"""

BLOCK_YAML = """\
pre_actions:
  - action: expose
    frames: 1
    exptime: 0
    image_type: BIAS
    shutter: CLOSE
    filename: "bias-{name}"

post_actions:
  - action: expose
    filter: R
    frames: 2
    exptime: 20.5
    image_type: OBJECT
    shutter: OPEN
    object_name: "{name}"
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
    project = session.query(model.Project).one()
    assert project.pid == "P01"
    assert project.priority == 1

    blockpar = session.query(model.BlockPar).one()
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
    assert session.query(model.Project).count() == 1
    assert session.query(model.Project).one().priority == 7
    assert session.query(model.BlockPar).count() == 1


def test_add_targets_from_csv(tmp_path, db):
    filename = _write(tmp_path, "targets.csv", TARGETS_CSV)
    assert _run(db, "add-targets", "-f", filename) == 0

    session = _session(db)
    targets = session.query(model.Target).order_by(model.Target.id).all()
    # the invalid row is skipped with a warning
    assert [t.name for t in targets] == ["NGC0001", "NGC0002"]
    assert targets[0].target_ra == pytest.approx(10.0)
    assert targets[0].target_dec == pytest.approx(-20.0)
    assert targets[0].target_mag == pytest.approx(12.5)
    assert targets[0].mag_filter == "V"
    assert targets[1].target_ra == pytest.approx(11.5)
    assert targets[1].target_dec == pytest.approx(5.0)


def test_add_targets_is_append_only_by_default(tmp_path, db):
    """The default must not change: names are not unique in general, and the
    observing log stores target ids."""
    filename = _write(tmp_path, "targets.csv", TARGETS_CSV)
    assert _run(db, "add-targets", "-f", filename) == 0
    assert _run(db, "add-targets", "-f", filename) == 0

    session = _session(db)
    assert [t.name for t in session.query(model.Target).order_by(model.Target.id)] == [
        "NGC0001",
        "NGC0002",
        "NGC0001",
        "NGC0002",
    ]


def test_add_targets_match_by_name_reuses_the_row(tmp_path, db):
    """--match-by-name makes re-ingesting a regenerated file idempotent AND
    keeps the row id, which is what the observing log references.  Without
    it, keeping generated inputs current costs a delete-and-re-add on every
    run and orphans the log entry of everything already observed."""
    filename = _write(tmp_path, "targets.csv", TARGETS_CSV)
    assert _run(db, "add-targets", "-f", filename) == 0
    ids = {t.name: t.id for t in _session(db).query(model.Target)}

    # same names, moved coordinates - a refreshed prediction
    moved = _write(
        tmp_path,
        "targets2.csv",
        "RA,DEC,NAME,MAG,FILTER\n"
        "12:00:00,-30:00:00,NGC0001,12.5,V\n"
        "11:30:00,+05:00:00,NGC0002,13.0,V\n",
    )
    assert _run(db, "add-targets", "-f", moved, "--match-by-name") == 0

    session = _session(db)
    targets = session.query(model.Target).order_by(model.Target.id).all()
    assert [t.name for t in targets] == ["NGC0001", "NGC0002"]  # no duplicates
    assert {t.name: t.id for t in targets} == ids  # ids preserved
    assert targets[0].target_ra == pytest.approx(12.0)  # coordinates refreshed
    assert targets[0].target_dec == pytest.approx(-30.0)


def test_add_targets_match_by_name_refuses_an_ambiguous_name(tmp_path, db):
    """Two rows of the same name: refuse rather than guess, the same rule
    plot-log uses for an orphaned log entry."""
    filename = _write(tmp_path, "targets.csv", TARGETS_CSV)
    assert _run(db, "add-targets", "-f", filename) == 0
    assert _run(db, "add-targets", "-f", filename) == 0  # now two of each

    assert _run(db, "add-targets", "-f", filename, "--match-by-name") == 1
    # and nothing was written by the failed run
    session = _session(db)
    assert session.query(model.Target).count() == 4


def test_add_observing_block(tmp_path, db):
    _run(
        db,
        "add-project",
        "-f",
        _write(tmp_path, "p.yaml", PROJECT_YAML.format(priority=1)),
    )
    _run(db, "add-targets", "-f", _write(tmp_path, "t.csv", TARGETS_CSV))

    block_yaml = _write(tmp_path, "block.yaml", BLOCK_YAML)
    blocks_txt = _write(tmp_path, "blocks.txt", f"P01 1 1 {block_yaml} 1\n")

    assert _run(db, "add-observing-block", "-f", blocks_txt) == 0

    session = _session(db)
    block = session.query(model.ObsBlock).one()
    blockpar = session.query(model.BlockPar).one()
    target = session.query(model.Target).filter(model.Target.name == "NGC0001").one()

    assert block.pid == "P01"
    assert block.blockid == 1
    assert block.target_id == target.id
    # the FK now points at the blockpar PRIMARY KEY, resolved from (pid, bid)
    assert block.block_par_id == blockpar.id

    # pre-action expose + slew point + pos-action expose + pos-action point
    assert len(block.actions) == 4
    bias, slew, science, offset_point = block.actions

    assert isinstance(bias, model.Expose)
    assert bias.image_type == "BIAS"
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


def test_block_length_overheads_are_configurable(tmp_path, db):
    """The readout and focus-sweep budgets are properties of the CAMERA, not
    of the scheduler: opd-40's QHY600 measured ~2.8 s/frame against the
    12 s default, which made every block ~4x too long - and that number
    lands in the night-end fit check, the derived slot length and the
    process-queue simulation alike."""
    _run(
        db,
        "add-project",
        "-f",
        _write(tmp_path, "p.yaml", PROJECT_YAML.format(priority=1)),
    )
    _run(db, "add-targets", "-f", _write(tmp_path, "t.csv", TARGETS_CSV))
    block_yaml = _write(tmp_path, "block.yaml", BLOCK_YAML)
    blocks_txt = _write(tmp_path, "blocks.txt", f"P01 1 1 {block_yaml} 1\n")

    assert (
        _run(db, "add-observing-block", "--readout-overhead", "2.8", "-f", blocks_txt)
        == 0
    )

    block = _session(db).query(model.ObsBlock).one()
    assert block.length == pytest.approx((20.5 + 2.8) * 2)


def test_clean_commands_backup_the_robobs_database(tmp_path, db):
    _run(
        db,
        "add-project",
        "-f",
        _write(tmp_path, "p.yaml", PROJECT_YAML.format(priority=1)),
    )
    _run(db, "add-targets", "-f", _write(tmp_path, "t.csv", TARGETS_CSV))

    assert _run(db, "clean-targets") == 0
    session = _session(db)
    assert session.query(model.Target).count() == 0
    # the backup must be a copy of the robobs database itself
    # (the legacy tool copied an unrelated database)
    assert glob.glob(db + ".*.bak")

    assert _run(db, "delete-project", "--pid", "P01") == 0
    session = _session(db)
    assert session.query(model.Project).count() == 0
    assert session.query(model.BlockPar).count() == 0


def test_delete_and_clean_observing_blocks(tmp_path, db):
    _run(
        db,
        "add-project",
        "-f",
        _write(tmp_path, "p.yaml", PROJECT_YAML.format(priority=1)),
    )
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
    _run(
        db,
        "add-project",
        "-f",
        _write(tmp_path, "p.yaml", PROJECT_YAML.format(priority=1)),
    )
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
                target_id=1,
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
        jd_start=None,
        jd_end=None,
        date_start=None,
        date_end=None,
        lst_start=None,
        lst_end=None,
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

    project = model.Project(pid="P01", priority=1)
    session.add(project)
    blockpar = model.BlockPar(bid=1, pid="P01")
    session.add(blockpar)
    session.commit()

    for i, ra in enumerate((1.0, 10.0, 23.0)):
        target = model.Target(name=f"t{i}", target_ra=ra, target_dec=0.0)
        session.add(target)
        session.commit()
        session.add(
            model.ObsBlock(
                target_id=target.id,
                blockid=i + 1,
                pid="P01",
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

    # a target at exactly RA 0 must be selectable in a wrap-around window
    # (lost 2018 fix from the wschoenell-patch-1 branch: > 0 excluded it)
    target = model.Target(name="t-ra0", target_ra=0.0, target_dec=0.0)
    session.add(target)
    session.commit()
    session.add(
        model.ObsBlock(
            target_id=target.id, blockid=9, pid="P01", block_par_id=blockpar.id
        )
    )
    session.commit()
    rows = cli.select_blocks(session, "P01", 22.0, 2.0)[:]
    assert sorted(r[2].target_ra for r in rows) == [0.0, 1.0, 23.0]


def test_pool_lst_start_past_meridian_only():
    """A past_meridian_only project needs already-culminated targets, but
    the default cut (night-start LST - 2 h) keeps only targets that
    culminate DURING the night — the exact complement — so no candidate
    was ever eligible at the early occurrences (0 of 20 on 2026-07-21)."""
    assert cli.pool_lst_start(10.0, {}) == 10.0
    assert cli.pool_lst_start(10.0, {"past_meridian_only": False}) == 10.0
    widened = cli.pool_lst_start(10.0, {"past_meridian_only": True})
    assert widened == 10.0 - cli.PAST_MERIDIAN_POOL_HOURS
    assert cli.PAST_MERIDIAN_POOL_HOURS >= 4.0


def test_add_project_inputs_end_to_end(tmp_path, db):
    """One command ingests project + targets + block template and attaches
    one observing block per target - no block-list file, no sqlite3."""
    proj = _write(tmp_path, "p.yaml", PROJECT_YAML.format(priority=3))
    targets = _write(tmp_path, "t.csv", TARGETS_CSV)
    block = _write(tmp_path, "b.yaml", BLOCK_YAML)

    assert _run(db, "add-project-inputs", "-p", proj, "-t", targets, "-b", block) == 0

    session = _session(db)
    assert session.query(model.Project).one().pid == "P01"
    # the broken-coord row is skipped: 2 targets -> 2 blocks
    assert session.query(model.Target).count() == 2
    blocks = session.query(model.ObsBlock).order_by(model.ObsBlock.blockid).all()
    assert [b.blockid for b in blocks] == [1, 2]
    assert {b.pid for b in blocks} == {"P01"}
    # each block is attached to a real target and carries the template's
    # actions (pre expose + slew + post expose + point)
    for b in blocks:
        assert b.target_id in {t.id for t in session.query(model.Target)}
        assert len(b.actions) >= 3


def test_add_project_inputs_updates_in_place(tmp_path, db):
    """Re-running with edited YAML updates project/block parameters (the
    priority/exposure-time edit path), replacing blocks after a clean."""
    proj = _write(tmp_path, "p.yaml", PROJECT_YAML.format(priority=3))
    targets = _write(tmp_path, "t.csv", TARGETS_CSV)
    block = _write(tmp_path, "b.yaml", BLOCK_YAML)
    assert _run(db, "add-project-inputs", "-p", proj, "-t", targets, "-b", block) == 0

    # edit the priority and reload the project (targets/blocks cleaned first,
    # as the load script does for a full refresh)
    _write(tmp_path, "p.yaml", PROJECT_YAML.format(priority=9))
    assert _run(db, "clean-observing-blocks") == 0
    assert _run(db, "clean-targets") == 0
    assert _run(db, "add-project-inputs", "-p", proj, "-t", targets, "-b", block) == 0

    session = _session(db)
    assert session.query(model.Project).one().priority == 9
    assert session.query(model.ObsBlock).count() == 2


def test_add_observing_block_by_name_and_block_dir(tmp_path, db):
    """Per-object list: target column is @NAME@, resolved in-tool, and
    block-YAML paths are localized to --block-dir. This is the OPOP shape -
    each object its own block/exposure time, no sqlite id lookup."""
    _run(
        db,
        "add-project",
        "-f",
        _write(tmp_path, "p.yaml", PROJECT_YAML.format(priority=1)),
    )
    _run(db, "add-targets", "-f", _write(tmp_path, "t.csv", TARGETS_CSV))

    # two distinct per-object block YAMLs, in their own dir
    bdir = tmp_path / "blocks"
    bdir.mkdir()
    (bdir / "b1.yaml").write_text(BLOCK_YAML.replace("exptime: 20.5", "exptime: 111"))
    (bdir / "b2.yaml").write_text(BLOCK_YAML.replace("exptime: 20.5", "exptime: 222"))
    # the list embeds a DIFFERENT (generating-machine) path; --block-dir wins
    listfile = _write(
        tmp_path,
        "opop.list.in",
        "P01 1 @NGC0001@ /somewhere/else/b1.yaml 1\n"
        "P01 2 @NGC0002@ /somewhere/else/b2.yaml 1\n",
    )

    rc = _run(
        db, "add-observing-block", "--by-name", "--block-dir", str(bdir), "-f", listfile
    )
    assert rc == 0

    session = _session(db)
    by_target = {
        session.query(model.Target).get(b.target_id).name: b
        for b in session.query(model.ObsBlock)
    }
    assert set(by_target) == {"NGC0001", "NGC0002"}
    # each block took its own YAML's exposure time
    exptimes = {
        name: [
            a.exptime for a in b.actions if getattr(a, "exptime", None) in (111, 222)
        ]
        for name, b in by_target.items()
    }
    assert exptimes["NGC0001"] == [111]
    assert exptimes["NGC0002"] == [222]


def test_add_observing_block_by_name_unknown_target(tmp_path, db):
    _run(
        db,
        "add-project",
        "-f",
        _write(tmp_path, "p.yaml", PROJECT_YAML.format(priority=1)),
    )
    block = _write(tmp_path, "b.yaml", BLOCK_YAML)
    listfile = _write(tmp_path, "l.in", f"P01 1 @NOSUCH@ {block} 1\n")
    assert _run(db, "add-observing-block", "--by-name", "-f", listfile) == 1


def test_clean_targets_names_from(tmp_path, db):
    """Selective delete: only the named targets go, the rest stay."""
    _run(
        db, "add-targets", "-f", _write(tmp_path, "t.csv", TARGETS_CSV)
    )  # NGC0001, NGC0002
    session = _session(db)
    assert session.query(model.Target).count() == 2

    only = _write(tmp_path, "drop.csv", "RA,DEC,NAME\n10:00:00,-20:00:00,NGC0001\n")
    assert _run(db, "clean-targets", "--names-from", only) == 0

    session = _session(db)
    remaining = [t.name for t in session.query(model.Target)]
    assert remaining == ["NGC0002"]
