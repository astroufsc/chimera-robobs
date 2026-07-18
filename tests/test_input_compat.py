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
