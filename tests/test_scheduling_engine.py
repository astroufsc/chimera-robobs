# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2014-present chimera-robobs authors

import logging
import math

import pytest

from chimera_robobs.scheduling import model
from chimera_robobs.scheduling.engine import RobObsEngine

from .fakes import FakeSite

LOG = logging.getLogger("test-engine")


@pytest.fixture
def session_factory(tmp_path):
    return model.open_database(str(tmp_path / "robobs.db"))


def _add_program(session, pid, priority, slew_at, ra=10.0, dec=0.0, max_airmass=2.5):
    target = model.Target(name=f"tgt-{pid}-{priority}", target_ra=ra, target_dec=dec)
    session.add(target)
    session.commit()
    blockpar = model.BlockPar(bid=priority, pid=pid)
    blockpar.max_airmass = max_airmass
    blockpar.sched_algorithm = 0  # Higher
    session.add(blockpar)
    session.commit()
    block = model.ObsBlock(
        target_id=target.id, blockid=priority, pid=pid, block_par_id=blockpar.id
    )
    block.actions.append(model.Expose(frames=2, exptime=30.0, image_type="OBJECT"))
    session.add(block)
    session.commit()
    program = model.Program(
        target_id=target.id,
        name=target.name,
        priority=priority,
        slew_at=slew_at,
        pid=pid,
        obsblock_id=block.id,
        blockpar_id=blockpar.id,
    )
    session.add(program)
    session.commit()
    return program


def _engine(session_factory, ra_hours=10.0):
    # site with the target of ra_hours crossing the meridian "now"
    site = FakeSite(latitude=0.0, lst_rads=ra_hours * math.pi / 12.0)
    return RobObsEngine(session_factory, site, log=LOG), site


def test_priority_list(session_factory):
    session = session_factory()
    _add_program(session, "P01", 1, 61000.0)
    _add_program(session, "P02", 3, 61000.0)
    engine, _ = _engine(session_factory)
    assert engine.get_priority_list() == [1, 3]


def test_get_program_and_reschedule(session_factory):
    session = session_factory()
    program = _add_program(session, "P01", 1, slew_at=61000.0)

    engine, site = _engine(session_factory)
    now = site.mjd()

    chosen, length = engine.get_program(now, 1)
    assert chosen is not None
    assert chosen[0].id == program.id
    assert length == pytest.approx(60.0)  # 2 x 30 s exposures

    selected = engine.reschedule(now)
    assert selected is not None
    assert selected[0].id == program.id


def test_reschedule_empty_queue(session_factory):
    engine, site = _engine(session_factory)
    assert engine.reschedule(61000.0) is None


def test_check_conditions_airmass(session_factory):
    session = session_factory()
    _add_program(session, "P01", 1, slew_at=61000.0)
    engine, site = _engine(session_factory)  # target at zenith, airmass 1.0

    rows = (
        session.query(model.Program, model.BlockPar, model.ObsBlock, model.Target)
        .join(model.BlockPar, model.Program.blockpar_id == model.BlockPar.id)
        .join(model.ObsBlock, model.Program.obsblock_id == model.ObsBlock.id)
        .join(model.Target, model.Program.target_id == model.Target.id)
        .one()
    )

    assert engine.check_conditions(rows, 61000.0)
    # with a program length the end-of-block airmass is also checked
    assert engine.check_conditions(rows, 61000.0, program_length=60.0)

    # a max_airmass below 1 rejects even a target at the zenith (the legacy
    # code had a FIXME fall-through in the end-of-block branch)
    rows[1].max_airmass = 0.5
    session.commit()
    assert not engine.check_conditions(rows, 61000.0)


def test_check_conditions_moon_distance(session_factory):
    session = session_factory()
    _add_program(session, "P01", 1, slew_at=61000.0)
    engine, site = _engine(session_factory)
    site._moon = (10.5, 5.0)  # ~9 degrees from the target (above horizon)

    rows = (
        session.query(model.Program, model.BlockPar, model.ObsBlock, model.Target)
        .join(model.BlockPar, model.Program.blockpar_id == model.BlockPar.id)
        .join(model.ObsBlock, model.Program.obsblock_id == model.ObsBlock.id)
        .join(model.Target, model.Program.target_id == model.Target.id)
        .one()
    )

    rows[1].min_moon_distance = 30.0
    session.commit()
    assert not engine.check_conditions(rows, 61000.0)

    rows[1].min_moon_distance = 2.0
    session.commit()
    assert engine.check_conditions(rows, 61000.0)


def test_check_conditions_night_end(session_factory):
    session = session_factory()
    _add_program(session, "P01", 1, slew_at=61000.0)
    engine, site = _engine(session_factory)
    site._night_length = 0.001  # night ends (almost) right away

    rows = (
        session.query(model.Program, model.BlockPar, model.ObsBlock, model.Target)
        .join(model.BlockPar, model.Program.blockpar_id == model.BlockPar.id)
        .join(model.ObsBlock, model.Program.obsblock_id == model.ObsBlock.id)
        .join(model.Target, model.Program.target_id == model.Target.id)
        .one()
    )

    now = site.mjd()
    # a one-hour block does not fit before the end of the night
    assert not engine.check_conditions(rows, now, program_length=3600.0)


def test_check_conditions_seeing(session_factory):
    session = session_factory()
    _add_program(session, "P01", 1, slew_at=61000.0)
    site = FakeSite(latitude=0.0, lst_rads=10.0 * math.pi / 12.0)
    engine = RobObsEngine(session_factory, site, log=LOG, seeing=lambda: 5.0)

    rows = (
        session.query(model.Program, model.BlockPar, model.ObsBlock, model.Target)
        .join(model.BlockPar, model.Program.blockpar_id == model.BlockPar.id)
        .join(model.ObsBlock, model.Program.obsblock_id == model.ObsBlock.id)
        .join(model.Target, model.Program.target_id == model.Target.id)
        .one()
    )
    assert not engine.check_conditions(rows, 61000.0)  # max_seeing default is 2.0

    engine.seeing = lambda: 1.0
    assert engine.check_conditions(rows, 61000.0)
