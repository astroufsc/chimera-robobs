# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2014-present chimera-robobs authors

"""Regression tests ingesting verbatim excerpts of the production input
files (LNA Robo40 and T80-South), so the legacy dialects keep working."""

import os

import pytest

from chimera_robobs.cli import robobs as cli
from chimera_robobs.scheduling import model

DATA = os.path.join(os.path.dirname(__file__), "data")


def _data(name: str) -> str:
    return os.path.join(DATA, name)


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "robobs.db")


def _run(db, *argv):
    return cli.main(["--database", db, *argv])


def _session(db):
    return model.open_database(db)()


def test_etacar_project_camelcase_keys(db):
    assert _run(db, "add-project", "-f", _data("etacar_proj.yaml")) == 0

    session = _session(db)
    project = session.query(model.Project).one()
    assert project.pid == "ETACAR"
    assert project.priority == 20

    blockpar = session.query(model.BlockPar).one()
    assert blockpar.bid == 0
    assert blockpar.pid == "ETACAR"
    assert blockpar.max_airmass == 3.0
    assert blockpar.min_airmass == -1
    assert blockpar.max_moon_bright == 100.0
    assert blockpar.min_moon_bright == 0.0
    assert blockpar.min_moon_distance == 40.0
    assert blockpar.max_seeing == 1.5
    # the misspelled legacy key name must keep working
    assert blockpar.sched_algorithm == 3
    assert bool(blockpar.apply_ext_corr) is True


def test_kelt_project_block_without_id_and_pid(db):
    # kelt-style project files omit id/pid in the block section
    assert _run(db, "add-project", "-f", _data("kelt_proj.yaml")) == 0

    session = _session(db)
    blockpar = session.query(model.BlockPar).one()
    assert blockpar.pid == "KELT"  # defaulted from the project pid
    assert blockpar.bid == 0  # defaulted from the enumeration order
    assert blockpar.max_airmass == 2.0
    assert blockpar.sched_algorithm == 0


def test_epoc_header_and_space_padded_csv(db):
    assert _run(db, "add-targets", "-f", _data("etacar_pointings.csv")) == 0

    session = _session(db)
    target = session.query(model.Target).one()
    # values are space-padded in the production CSVs
    assert target.name == "ETACAR"
    assert target.target_ra == pytest.approx(10.7509975, abs=1e-4)
    assert target.target_dec == pytest.approx(-59.6845, abs=1e-3)
    # EPOC (production spelling, no H) maps to target_epoch
    assert target.target_epoch == pytest.approx(2000.0)


def _add_etacar_target_and_block(db, block_yaml):
    _run(db, "add-project", "-f", _data("etacar_proj.yaml"))
    _run(db, "add-targets", "-f", _data("etacar_pointings.csv"))
    session = _session(db)
    target = session.query(model.Target).one()
    # 5-column whitespace-mixed block list (tabs + spaces, as in production)
    blocks_list = _data(block_yaml).replace(".yaml", "") + ".list"
    return target, blocks_list


def test_t80s_block_compress_format_and_dither(db, tmp_path):
    _run(db, "add-project", "-f", _data("etacar_proj.yaml"))
    _run(db, "add-targets", "-f", _data("etacar_pointings.csv"))
    session = _session(db)
    target = session.query(model.Target).one()

    blocks = tmp_path / "blocks.list"
    blocks.write_text(f"ETACAR 27\t{target.id}  {_data('splus_block.yaml')} 0\n")
    assert _run(db, "add-observing-block", "-f", str(blocks)) == 0

    session = _session(db)
    block = session.query(model.ObsBlock).one()
    assert block.blockid == 27
    # slew point + expose + dither point + expose
    exposes = [a for a in block.actions if isinstance(a, model.Expose)]
    points = [a for a in block.actions if isinstance(a, model.Point)]
    assert len(exposes) == 2 and len(points) == 2

    for expose in exposes:
        # compress_format was silently dropped before the refactor
        assert expose.compress_format == "fits_rice"
        assert expose.image_type == "OBJECT"
        assert expose.object_name == "ETACAR-ETACAR"
        chimera_expose = expose.chimera_action()
        assert chimera_expose.compress_format == "fits_rice"

    dither = points[1]
    assert float(dither.offset_ew.arcsec) == pytest.approx(-10.0)  # east < 0


def test_lna_block_wait_dome_and_binning(db, tmp_path):
    _run(db, "add-project", "-f", _data("etacar_proj.yaml"))
    _run(db, "add-targets", "-f", _data("etacar_pointings.csv"))
    session = _session(db)
    target = session.query(model.Target).one()

    blocks = tmp_path / "blocks.list"
    blocks.write_text(f"ETACAR 1 {target.id} {_data('lna_block.yaml')} 0\n")
    assert _run(db, "add-observing-block", "-f", str(blocks)) == 0

    session = _session(db)
    block = session.query(model.ObsBlock).one()
    # pre-action bias + slew + science expose + autofocus
    bias, slew, science, focus = block.actions
    assert isinstance(bias, model.Expose) and bias.image_type == "bias"
    assert isinstance(slew, model.Point)
    assert isinstance(science, model.Expose)
    assert science.binning == "2x2"
    assert science.wait_dome is True
    assert science.image_type == "object"  # lowercase value stored verbatim
    assert science.chimera_action().wait_dome is True
    assert isinstance(focus, model.AutoFocus) and focus.step == 250

    # block length: 2 x (20.5 + 12 s readout) science + 600 s focus sweep
    assert block.length == pytest.approx((20.5 + 12.0) * 2 + 600.0)


def test_unsupported_pid_config_key_warns(db, tmp_path, capsys):
    # past_meridian_only is used at both sites but not implemented
    config = tmp_path / "pid.yaml"
    config.write_text("pool_size: 16\nslotLen: 600.0\npast_meridian_only: True\n")

    _run(db, "add-project", "-f", _data("etacar_proj.yaml"))
    capsys.readouterr()

    # make-queue exits early (no site connection attempted after the check),
    # but the pid-config validation happens first
    import chimera_robobs.cli.robobs as cli_module

    def fail_connect(args, location):
        raise RuntimeError("stop before connecting")

    original = cli_module._connect
    cli_module._connect = fail_connect
    try:
        with pytest.raises(RuntimeError):
            _run(db, "make-queue", "--pid", "ETACAR", "--pid-config", str(config))
    finally:
        cli_module._connect = original

    captured = capsys.readouterr()
    assert "past_meridian_only" in captured.err


# ----------------------------------------------------------------------
# canonical (supervisor-style) dialect
# ----------------------------------------------------------------------


def test_parse_algorithm_id_accepts_names_and_ids():
    from chimera_robobs.scheduling.algorithms import parse_algorithm_id

    assert parse_algorithm_id("higher") == 0
    assert parse_algorithm_id("extinction_monitor") == 1
    assert parse_algorithm_id("STD") == 1
    assert parse_algorithm_id("timed") == 2
    assert parse_algorithm_id("recurrent") == 3
    assert parse_algorithm_id("RECURRENT") == 3
    assert parse_algorithm_id("time_sequence") == 4
    assert parse_algorithm_id("time-sequence") == 4
    assert parse_algorithm_id("TIMESEQUENCE") == 4
    assert parse_algorithm_id(3) == 3
    assert parse_algorithm_id("3") == 3
    with pytest.raises(ValueError):
        parse_algorithm_id("no_such_algorithm")
    with pytest.raises(ValueError):
        parse_algorithm_id(99)
    with pytest.raises(ValueError):
        parse_algorithm_id(True)


def test_canonical_project_dialect(db):
    assert _run(db, "add-project", "-f", _data("canonical_proj.yaml")) == 0

    session = _session(db)
    blockpar = session.query(model.BlockPar).one()
    # the readable name is stored as the persisted numeric id
    assert blockpar.sched_algorithm == 3
    assert blockpar.max_airmass == 1.9
    assert bool(blockpar.apply_ext_corr) is False


def test_canonical_block_dialect(db, tmp_path):
    _run(db, "add-project", "-f", _data("canonical_proj.yaml"))
    _run(db, "add-targets", "-f", _data("etacar_pointings.csv"))
    session = _session(db)
    target = session.query(model.Target).one()

    blocks = tmp_path / "blocks.list"
    blocks.write_text(f"CANON 1 {target.id} {_data('canonical_block.yaml')} 1\n")
    assert _run(db, "add-observing-block", "-f", str(blocks)) == 0

    session = _session(db)
    block = session.query(model.ObsBlock).one()
    bias, slew, science = block.actions
    assert isinstance(bias, model.Expose) and bias.image_type == "BIAS"
    assert isinstance(slew, model.Point)
    assert isinstance(science, model.Expose)
    assert science.object_name == "ETACAR"
    assert science.filename == "CANON-ETACAR"


def test_normalize_config_and_canonical_slot_len():
    from chimera_robobs.scheduling.algorithms.base import normalize_config
    from chimera_robobs.scheduling.algorithms.higher import Higher

    normalized = normalize_config(
        {"slotLen": 600.0, "nstars": 2, "nairmass": 3, "pool_size": 16}
    )
    assert normalized == {
        "slot_len": 600.0,
        "n_stars": 2,
        "n_airmass": 3,
        "pool_size": 16,
    }
    higher = Higher(None)
    # canonical and legacy spellings resolve identically
    assert higher._slot_len(normalize_config({"slot_len": 300.0}), None) == 300.0
    assert higher._slot_len(normalize_config({"slotLen": 300.0}), None) == 300.0
    assert higher._slot_len({}, None) == Higher.default_slot_len


def test_migrate_config_project_round_trip(db, tmp_path, capsys):
    # convert the legacy production file to the canonical dialect...
    out = tmp_path / "migrated"
    assert cli.main(["migrate-config", _data("etacar_proj.yaml"), "-o", str(out)]) == 0
    migrated = out / "etacar_proj.yaml"
    text = migrated.read_text()
    assert "scheduling_algorithm: recurrent" in text
    assert "schedalgorith" not in text
    assert "maxmoonBright" not in text
    capsys.readouterr()

    # ...and both dialects must ingest to the identical BlockPar
    legacy_db = str(tmp_path / "legacy.db")
    canon_db = str(tmp_path / "canon.db")
    assert _run(legacy_db, "add-project", "-f", _data("etacar_proj.yaml")) == 0
    assert _run(canon_db, "add-project", "-f", str(migrated)) == 0

    legacy = _session(legacy_db).query(model.BlockPar).one()
    canon = _session(canon_db).query(model.BlockPar).one()
    for column in (
        "bid",
        "pid",
        "max_airmass",
        "min_airmass",
        "max_moon_bright",
        "min_moon_bright",
        "min_moon_distance",
        "max_seeing",
        "cloud_cover",
        "sched_algorithm",
    ):
        assert getattr(legacy, column) == getattr(canon, column), column


def test_migrate_config_block_and_pid_files(tmp_path, capsys):
    # block file: sections and action keys become snake_case
    assert cli.main(["migrate-config", _data("lna_block.yaml")]) == 0
    text = capsys.readouterr().out
    assert "post_actions:" in text
    assert "pre_actions:" in text
    assert "image_type:" in text
    assert "imageType" not in text
    assert "pos-actions" not in text

    # pid-config: slotLen/nstars/nairmass become snake_case
    pid_config = tmp_path / "extimoni_pid.conf"
    pid_config.write_text("pool_size: 16\nslotLen: 250.\nnstars: 2\nnairmass: 3\n")
    assert cli.main(["migrate-config", str(pid_config)]) == 0
    text = capsys.readouterr().out
    assert "slot_len: 250.0" in text
    assert "n_stars: 2" in text
    assert "n_airmass: 3" in text
    assert "slotLen" not in text
