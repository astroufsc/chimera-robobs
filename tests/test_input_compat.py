# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2014-present chimera-robobs authors

"""Input-dialect tests.

The CLI only accepts the canonical snake_case dialect; the verbatim legacy
production excerpts under ``tests/data`` are converted with the standalone
``scripts/migrate_legacy_config.py`` first (and direct legacy ingestion must
fail loudly, pointing at the script).
"""

import os
import subprocess
import sys

import pytest

from chimera_robobs.cli import robobs as cli
from chimera_robobs.scheduling import model

DATA = os.path.join(os.path.dirname(__file__), "data")
MIGRATE_SCRIPT = os.path.join(
    os.path.dirname(__file__), "..", "scripts", "migrate_legacy_config.py"
)


def _data(name: str) -> str:
    return os.path.join(DATA, name)


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "robobs.db")


def _run(db, *argv):
    return cli.main(["--database", db, *argv])


def _session(db):
    return model.open_database(db)()


def _migrate(tmp_path, *files) -> list[str]:
    """Run the standalone migration script; return the converted paths."""
    outdir = tmp_path / "migrated"
    result = subprocess.run(
        [sys.executable, MIGRATE_SCRIPT, *files, "-o", str(outdir)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return [str(outdir / os.path.basename(f)) for f in files]


# ----------------------------------------------------------------------
# legacy production files: rejected directly, accepted after migration
# ----------------------------------------------------------------------


def test_legacy_project_rejected_with_migration_hint(db, capsys):
    assert _run(db, "add-project", "-f", _data("etacar_proj.yaml")) == 1
    captured = capsys.readouterr()
    assert "maxairmass" in captured.err
    assert "migrate_legacy_config" in captured.err


def test_legacy_block_rejected_with_migration_hint(db, tmp_path, capsys):
    _migrated = _migrate(tmp_path, _data("etacar_proj.yaml"))
    _run(db, "add-project", "-f", _migrated[0])
    _run(db, "add-targets", "-f", _data("etacar_pointings.csv"))
    session = _session(db)
    target = session.query(model.Target).one()

    blocks = tmp_path / "blocks.list"
    blocks.write_text(f"ETACAR 1 {target.id} {_data('lna_block.yaml')} 0\n")
    assert _run(db, "add-observing-block", "-f", str(blocks)) == 1
    captured = capsys.readouterr()
    assert "migrate_legacy_config" in captured.err


def test_migrated_etacar_project(db, tmp_path):
    (migrated,) = _migrate(tmp_path, _data("etacar_proj.yaml"))
    text = open(migrated).read()
    assert "scheduling_algorithm: recurrent" in text
    assert "schedalgorith" not in text
    assert "maxmoonBright" not in text

    assert _run(db, "add-project", "-f", migrated) == 0

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
    assert blockpar.sched_algorithm == 3
    assert bool(blockpar.apply_ext_corr) is True


def test_migrated_kelt_project_block_without_id_and_pid(db, tmp_path):
    # kelt-style project files omit id/pid in the block section
    (migrated,) = _migrate(tmp_path, _data("kelt_proj.yaml"))
    assert _run(db, "add-project", "-f", migrated) == 0

    session = _session(db)
    blockpar = session.query(model.BlockPar).one()
    assert blockpar.pid == "KELT"  # defaulted from the project pid
    assert blockpar.bid == 0  # defaulted from the enumeration order
    assert blockpar.max_airmass == 2.0
    assert blockpar.sched_algorithm == 0


def test_epoc_header_and_space_padded_csv(db):
    # CSV headers are unchanged by the dialect switch: EPOC (production
    # spelling) and space-padded values keep working
    assert _run(db, "add-targets", "-f", _data("etacar_pointings.csv")) == 0

    session = _session(db)
    target = session.query(model.Target).one()
    assert target.name == "ETACAR"
    assert target.target_ra == pytest.approx(10.7509975, abs=1e-4)
    assert target.target_dec == pytest.approx(-59.6845, abs=1e-3)
    assert target.target_epoch == pytest.approx(2000.0)


def _project_and_targets(db, tmp_path):
    (migrated,) = _migrate(tmp_path, _data("etacar_proj.yaml"))
    _run(db, "add-project", "-f", migrated)
    _run(db, "add-targets", "-f", _data("etacar_pointings.csv"))
    session = _session(db)
    return session.query(model.Target).one()


def test_migrated_t80s_block_compress_format_and_dither(db, tmp_path):
    target = _project_and_targets(db, tmp_path)
    (migrated_block,) = _migrate(tmp_path, _data("splus_block.yaml"))

    blocks = tmp_path / "blocks.list"
    # 5-column whitespace-mixed block list (tabs + spaces, as in production)
    blocks.write_text(f"ETACAR 27\t{target.id}  {migrated_block} 0\n")
    assert _run(db, "add-observing-block", "-f", str(blocks)) == 0

    session = _session(db)
    block = session.query(model.ObsBlock).one()
    assert block.blockid == 27
    exposes = [a for a in block.actions if isinstance(a, model.Expose)]
    points = [a for a in block.actions if isinstance(a, model.Point)]
    assert len(exposes) == 2 and len(points) == 2

    for expose in exposes:
        assert expose.compress_format == "fits_rice"
        assert expose.image_type == "OBJECT"
        assert expose.object_name == "ETACAR-ETACAR"
        assert expose.chimera_action().compress_format == "fits_rice"

    dither = points[1]
    assert float(dither.offset_ew.arcsec) == pytest.approx(-10.0)  # east < 0


def test_migrated_lna_block_wait_dome_and_binning(db, tmp_path):
    target = _project_and_targets(db, tmp_path)
    (migrated_block,) = _migrate(tmp_path, _data("lna_block.yaml"))
    text = open(migrated_block).read()
    assert "post_actions:" in text and "pre_actions:" in text
    assert "image_type:" in text and "imageType" not in text

    blocks = tmp_path / "blocks.list"
    blocks.write_text(f"ETACAR 1 {target.id} {migrated_block} 0\n")
    assert _run(db, "add-observing-block", "-f", str(blocks)) == 0

    session = _session(db)
    block = session.query(model.ObsBlock).one()
    bias, slew, science, focus = block.actions
    assert isinstance(bias, model.Expose) and bias.image_type == "bias"
    assert isinstance(slew, model.Point)
    assert isinstance(science, model.Expose)
    assert science.binning == "2x2"
    assert science.wait_dome is True
    assert science.chimera_action().wait_dome is True
    assert isinstance(focus, model.AutoFocus) and focus.step == 250

    # block length: 2 x (20.5 + 12 s readout) science + 600 s focus sweep
    assert block.length == pytest.approx((20.5 + 12.0) * 2 + 600.0)


def test_migrate_pid_config(tmp_path):
    pid_config = tmp_path / "extimoni_pid.conf"
    pid_config.write_text("pool_size: 16\nslotLen: 250.\nnstars: 2\nnairmass: 3\n")
    (migrated,) = _migrate(tmp_path, str(pid_config))
    text = open(migrated).read()
    assert "slot_len: 250.0" in text
    assert "n_stars: 2" in text
    assert "n_airmass: 3" in text
    assert "slotLen" not in text


def test_migrate_rewrites_template_placeholders(tmp_path):
    legacy = tmp_path / "block.yaml"
    legacy.write_text(
        "pos-actions:\n"
        "  - action: expose\n"
        "    exptime: 1\n"
        '    filename: "{objid}-{targetRa}-$DATE"\n'
    )
    (migrated,) = _migrate(tmp_path, str(legacy))
    text = open(migrated).read()
    assert "{target_id}-{target_ra}-$DATE" in text


# ----------------------------------------------------------------------
# canonical dialect and strictness
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


def test_slot_len_resolution():
    from chimera_robobs.scheduling.algorithms.higher import Higher

    higher = Higher(None)
    assert higher._slot_len({"slot_len": 300.0}, None) == 300.0
    assert higher._slot_len({}, 120.0) == 120.0
    assert higher._slot_len({}, None) == Higher.default_slot_len


def test_unknown_pid_config_key_rejected(db, tmp_path, capsys):
    config = tmp_path / "pid.yaml"
    config.write_text("pool_size: 16\nslotLen: 600.0\n")  # legacy spelling

    _run(db, "add-project", "-f", _data("canonical_proj.yaml"))
    capsys.readouterr()

    assert _run(db, "make-queue", "--pid", "CANON", "--pid-config", str(config)) == 1
    captured = capsys.readouterr()
    assert "slotLen" in captured.err
    assert "migrate_legacy_config" in captured.err


def test_past_meridian_only_is_a_supported_key(db, tmp_path, capsys):
    # implemented since the stray-branch recovery: accepted without warnings
    config = tmp_path / "pid.yaml"
    config.write_text("pool_size: 16\npast_meridian_only: True\n")

    _run(db, "add-project", "-f", _data("canonical_proj.yaml"))
    capsys.readouterr()

    import chimera_robobs.cli.robobs as cli_module

    def fail_connect(args, location):
        raise RuntimeError("reached the connection step")

    original = cli_module._connect
    cli_module._connect = fail_connect
    try:
        with pytest.raises(RuntimeError):
            _run(db, "make-queue", "--pid", "CANON", "--pid-config", str(config))
    finally:
        cli_module._connect = original

    captured = capsys.readouterr()
    assert "past_meridian_only" not in captured.err
