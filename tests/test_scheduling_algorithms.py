# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2014-present chimera-robobs authors

import datetime as dt
import importlib
from types import SimpleNamespace

import pytest

from chimera_robobs.scheduling import algorithms, model
from chimera_robobs.scheduling.algorithms import (
    ALGORITHMS,
    ExtinctionMonitor,
    Higher,
    Recurrent,
    Timed,
    TimeSequence,
)

from .fakes import FakeSite


@pytest.mark.parametrize(
    "module",
    [
        "chimera_robobs.scheduling.algorithms.base",
        "chimera_robobs.scheduling.algorithms.higher",
        "chimera_robobs.scheduling.algorithms.extinctionmonitor",
        "chimera_robobs.scheduling.algorithms.timed",
        "chimera_robobs.scheduling.algorithms.recurrent",
        "chimera_robobs.scheduling.algorithms.timesequence",
    ],
)
def test_modules_import(module):
    importlib.import_module(module)


def test_ids_and_names_are_stable():
    # the ids/names are stored in the database and must not change
    assert ALGORITHMS[0] is Higher and Higher.name() == "HIG"
    assert ALGORITHMS[1] is ExtinctionMonitor and ExtinctionMonitor.name() == "STD"
    assert ALGORITHMS[2] is Timed and Timed.name() == "TIMED"
    assert ALGORITHMS[3] is Recurrent and Recurrent.name() == "RECURRENT"
    assert ALGORITHMS[4] is TimeSequence and TimeSequence.name() == "TIMESEQUENCE"


def test_higher_next_picks_slew_at_closest_to_now():
    now = 61000.0
    programs = [
        (SimpleNamespace(slew_at=60999.0), None, None, None),
        (SimpleNamespace(slew_at=61000.1), None, None, None),
        (SimpleNamespace(slew_at=61002.0), None, None, None),
    ]
    chosen = Higher.next(now, programs)
    assert chosen is programs[1]
    assert Higher.next(now, []) is None
    assert not Higher.timed_constraint()


@pytest.fixture
def session_factory(tmp_path):
    factory = model.open_database(str(tmp_path / "robobs.db"))
    algorithms.configure(factory)
    return factory


def _make_block(session, pid="P01", sched_algorithm=3):
    target = model.Target(name="obj", target_ra=10.0, target_dec=0.0)
    session.add(target)
    session.commit()
    blockpar = model.BlockPar(bid=1, pid=pid)
    blockpar.sched_algorithm = sched_algorithm
    session.add(blockpar)
    session.commit()
    block = model.ObsBlock(
        target_id=target.id, blockid=1, pid=pid, block_par_id=blockpar.id
    )
    session.add(block)
    session.commit()
    program = model.Program(
        target_id=target.id,
        name=target.name,
        priority=1,
        slew_at=61000.0,
        pid=pid,
        obsblock_id=block.id,
        blockpar_id=blockpar.id,
    )
    session.add(program)
    session.commit()
    return program, blockpar, block, target


def test_recurrent_add_and_observed_round_trip(session_factory):
    session = session_factory()
    program, blockpar, block, target = _make_block(session)

    Recurrent.add((block, blockpar, target))

    session = session_factory()
    recurrent = session.query(model.RecurrentDB).one()
    assert recurrent.pid == "P01"
    assert recurrent.block_id == block.id  # the legacy code stored a tuple here
    assert isinstance(recurrent.block_id, int)
    assert recurrent.target_id == target.id
    assert recurrent.visits == 0

    # mark it observed (hard mode) at a known MJD
    mjd = 61000.0
    Recurrent.observed(mjd, (program, blockpar, block, target), soft=False)

    session = session_factory()
    recurrent = session.query(model.RecurrentDB).one()
    assert recurrent.visits == 1
    assert isinstance(recurrent.block_id, int)
    expected = dt.datetime(1858, 11, 17) + dt.timedelta(days=mjd)
    assert abs((recurrent.last_visit - expected).total_seconds()) < 1.0

    block = session.query(model.ObsBlock).one()
    assert block.observed is True
    assert block.last_observation == recurrent.last_visit

    prog = session.query(model.Program).one()
    assert prog.finished is True

    # a second observation increments the visit count
    Recurrent.observed(mjd + 1, (program, blockpar, block, target), soft=False)
    session = session_factory()
    assert session.query(model.RecurrentDB).one().visits == 2


def test_timed_clean_and_soft_clean(session_factory):
    session = session_factory()
    for execute_at, finished in ((61000.1, True), (61000.2, False)):
        timed = model.TimedDB(pid="P01", execute_at=execute_at)
        timed.finished = finished
        session.add(timed)
    session.add(model.TimedDB(pid="OTHER", execute_at=61000.3))
    session.commit()

    Timed.soft_clean("P01")
    session = session_factory()
    assert (
        session.query(model.TimedDB)
        .filter(model.TimedDB.finished == True)  # noqa: E712
        .count()
        == 0
    )
    assert session.query(model.TimedDB).count() == 3

    Timed.clean("P01")
    session = session_factory()
    remaining = session.query(model.TimedDB).all()
    assert len(remaining) == 1
    assert remaining[0].pid == "OTHER"


def test_extinction_monitor_add_clean(session_factory):
    session = session_factory()
    program, blockpar, block, target = _make_block(session, sched_algorithm=1)

    ExtinctionMonitor.add((block, blockpar, target))
    ExtinctionMonitor.add((block, blockpar, target))

    session = session_factory()
    info = session.query(model.ExtMoniDB).one()
    assert info.pid == "P01"
    assert info.target_id == target.id
    assert info.nairmass == 2

    # observed() records the altitude/airmass of the observation
    ExtinctionMonitor.site = FakeSite(lst_rads=10.0 * 3.14159265 / 12.0)
    ExtinctionMonitor.observed(61000.0, (program, blockpar, block, target))
    session = session_factory()
    info = session.query(model.ExtMoniDB).one()
    assert len(info.observed_am) == 1
    assert info.observed_am[0].altitude == pytest.approx(90.0, abs=1.0)

    ExtinctionMonitor.soft_clean("P01")
    session = session_factory()
    assert session.query(model.ObservedAM).count() == 0

    ExtinctionMonitor.clean("P01")
    session = session_factory()
    assert session.query(model.ExtMoniDB).count() == 0
