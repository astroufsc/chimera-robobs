# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2014-present chimera-robobs authors

"""Sky-flat calibration algorithm (id 5) tests."""

import datetime as dt
import logging

import pytest

from chimera_robobs.scheduling import model
from chimera_robobs.scheduling.algorithms import build_algorithms
from chimera_robobs.scheduling.dates import jd_from_datetime
from chimera_robobs.scheduling.engine import RobObsEngine

from .fakes import UT, FakeSite

LOG = logging.getLogger("test-skyflat")

PID = "CAL"


@pytest.fixture
def session_factory(tmp_path):
    return model.open_database(str(tmp_path / "robobs.db"))


@pytest.fixture
def site():
    return FakeSite(ut_now=UT)


def _add_flat_block(session, blockid, filter_name, frames=9):
    """One skyflat block per filter, like the lna40/T80S calibration
    project: sensitivity order = ingestion (blockid) order."""
    target = session.query(model.Target).filter(model.Target.name == "SKYFLAT").first()
    if target is None:
        target = model.Target(name="SKYFLAT", target_ra=0.0, target_dec=-22.0)
        session.add(target)
        session.commit()
    blockpar = model.BlockPar(bid=blockid, pid=PID)
    blockpar.sched_algorithm = 5
    session.add(blockpar)
    session.commit()
    block = model.ObsBlock(
        target_id=target.id, blockid=blockid, pid=PID, block_par_id=blockpar.id
    )
    block.actions.append(model.AutoFlat(filter=filter_name, frames=frames))
    session.add(block)
    session.commit()
    return block


def _query(session):
    return (
        session.query(model.ObsBlock, model.BlockPar, model.Target)
        .join(model.BlockPar, model.ObsBlock.block_par_id == model.BlockPar.id)
        .join(model.Target, model.ObsBlock.target_id == model.Target.id)
        .filter(model.ObsBlock.pid == PID)
    )


def _window():
    jd_start = jd_from_datetime(UT)
    return jd_start, jd_start + 10.0 / 24.0


def test_skyflat_windows_and_execution_order(session_factory, site):
    session = session_factory()
    # sensitivity order: CLEAR (most) -> R -> B (least)
    _add_flat_block(session, 1, "CLEAR")
    _add_flat_block(session, 2, "R")
    _add_flat_block(session, 3, "B")

    algorithms = build_algorithms(session_factory, site)
    obs_start, obs_end = _window()
    slots = algorithms[5].process(
        obs_start=obs_start,
        obs_end=obs_end,
        query=_query(session),
        config={"pid": PID, "n_filters": {"evening": 2, "morning": 1}},
    )

    # evening picks the 2 needlest (ledger empty: block order -> CLEAR, R)
    # and runs them REVERSED (sky dims: least sensitive first); the morning
    # takes the next needy block at dawn
    assert [int(s["blockid"]) for s in slots] == [2, 1, 3]
    sunset_jd = obs_start - 1.3 / 24.0  # FakeSite: sunset 1.3 h before dusk
    assert slots["start"][0] == pytest.approx(sunset_jd, abs=1e-6)
    assert slots["start"][1] == pytest.approx(sunset_jd + 60.0 / 86400.0, abs=1e-6)
    assert slots["start"][2] == pytest.approx(obs_end, abs=1e-6)


def test_skyflat_ledger_orders_by_need(session_factory, site):
    session = session_factory()
    _add_flat_block(session, 1, "CLEAR")
    _add_flat_block(session, 2, "R")
    _add_flat_block(session, 3, "B")

    now = UT.replace(tzinfo=None)
    # CLEAR has fresh flats; R has only STALE ones (beyond the look-back)
    session.add(model.SkyFlatDB(pid=PID, filter="CLEAR", frames=9, observed_at=now))
    session.add(
        model.SkyFlatDB(
            pid=PID,
            filter="R",
            frames=9,
            observed_at=now - dt.timedelta(days=20),
        )
    )
    session.commit()

    algorithms = build_algorithms(session_factory, site)
    obs_start, obs_end = _window()
    slots = algorithms[5].process(
        obs_start=obs_start,
        obs_end=obs_end,
        query=_query(session),
        config={"pid": PID, "flat_window": "morning", "n_filters": 2},
    )

    # need order: R (stale only) and B (never) before CLEAR (fresh);
    # morning executes in sensitivity order restricted to the selection
    assert [int(s["blockid"]) for s in slots] == [2, 3]


def test_skyflat_observed_writes_ledger_only_when_not_soft(session_factory, site):
    session = session_factory()
    block = _add_flat_block(session, 1, "V", frames=7)
    target = session.query(model.Target).one()
    blockpar = session.query(model.BlockPar).one()
    program = model.Program(
        target_id=target.id,
        name=target.name,
        priority=0,
        slew_at=61000.0,
        pid=PID,
        obsblock_id=block.id,
        blockpar_id=blockpar.id,
    )
    session.add(program)
    session.commit()

    algorithms = build_algorithms(session_factory, site)
    row = (program, blockpar, block, target)

    algorithms[5].observed(61000.0, row, soft=True)
    assert session_factory().query(model.SkyFlatDB).count() == 0  # simulation

    algorithms[5].observed(61000.0, row, soft=False)
    ledger = session_factory().query(model.SkyFlatDB).one()
    assert (ledger.filter, ledger.frames) == ("V", 7)
    assert ledger.observed_at is not None


def test_engine_waives_conditions_for_twilight_calibration(session_factory):
    """Sky flats run in twilight ('daytime' by the -18 deg guard) on a
    placeholder target: the night/airmass/moon checks must not apply."""
    session = session_factory()
    block = _add_flat_block(session, 1, "R")
    target = session.query(model.Target).one()
    blockpar = session.query(model.BlockPar).one()
    program = model.Program(
        target_id=target.id,
        name=target.name,
        priority=0,
        slew_at=61000.0,
        pid=PID,
        obsblock_id=block.id,
        blockpar_id=blockpar.id,
    )
    session.add(program)
    session.commit()

    day_site = FakeSite(daytime=True)
    engine = RobObsEngine(session_factory, day_site, log=LOG)
    assert engine.check_conditions((program, blockpar, block, target), 61000.0)

    # a regular program under the same sky is rejected by the night guard
    blockpar.sched_algorithm = 0
    session.commit()
    assert not engine.check_conditions((program, blockpar, block, target), 61000.0)


def test_block_duration_counts_autoflat_frames():
    actions = [model.AutoFlat(filter="R", frames=9)]
    assert model.block_duration(actions, autoflat_frame=60.0) == pytest.approx(540.0)
    assert model.block_duration(actions) == pytest.approx(0.0)
