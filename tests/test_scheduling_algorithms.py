# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2014-present chimera-robobs authors

import datetime as dt
import importlib
import math
from types import SimpleNamespace

import pytest

from chimera_robobs.scheduling import model
from chimera_robobs.scheduling.algorithms import (
    ExtinctionMonitor,
    Higher,
    Recurrent,
    SkyFlat,
    Timed,
    TimeSequence,
    build_algorithms,
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
    assert (Higher.id, Higher.name) == (0, "HIG")
    assert (ExtinctionMonitor.id, ExtinctionMonitor.name) == (1, "STD")
    assert (Timed.id, Timed.name) == (2, "TIMED")
    assert (Recurrent.id, Recurrent.name) == (3, "RECURRENT")
    assert (TimeSequence.id, TimeSequence.name) == (4, "TIMESEQUENCE")
    assert (SkyFlat.id, SkyFlat.name) == (5, "SKYFLAT")


def test_build_algorithms_registry(tmp_path):
    factory = model.open_database(str(tmp_path / "robobs.db"))
    registry = build_algorithms(factory, site=FakeSite())
    assert sorted(registry) == [0, 1, 2, 3, 4, 5]
    for algorithm_id, algorithm in registry.items():
        assert algorithm.id == algorithm_id
        assert algorithm.session is factory
        assert isinstance(algorithm.site, FakeSite)


def test_higher_next_picks_slew_at_closest_to_now():
    now = 61000.0
    programs = [
        (SimpleNamespace(slew_at=60999.0), None, None, None),
        (SimpleNamespace(slew_at=61000.1), None, None, None),
        (SimpleNamespace(slew_at=61002.0), None, None, None),
    ]
    higher = Higher(None)
    chosen = higher.next(now, programs)
    assert chosen is programs[1]
    assert higher.next(now, []) is None
    assert not Higher.timed_constraint


@pytest.fixture
def session_factory(tmp_path):
    return model.open_database(str(tmp_path / "robobs.db"))


@pytest.fixture
def algorithms(session_factory):
    return build_algorithms(
        session_factory, site=FakeSite(lst_rads=10.0 * math.pi / 12.0)
    )


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


def test_recurrent_add_and_observed_round_trip(session_factory, algorithms):
    session = session_factory()
    program, blockpar, block, target = _make_block(session)
    recurrent_algorithm = algorithms[3]

    recurrent_algorithm.add((block, blockpar, target))

    session = session_factory()
    recurrent = session.query(model.RecurrentDB).one()
    assert recurrent.pid == "P01"
    assert recurrent.block_id == block.id  # the legacy code stored a tuple here
    assert isinstance(recurrent.block_id, int)
    assert recurrent.target_id == target.id
    assert recurrent.visits == 0

    # mark it observed (hard mode) at a known MJD
    mjd = 61000.0
    recurrent_algorithm.observed(mjd, (program, blockpar, block, target), soft=False)

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
    recurrent_algorithm.observed(
        mjd + 1, (program, blockpar, block, target), soft=False
    )
    session = session_factory()
    assert session.query(model.RecurrentDB).one().visits == 2


def test_timed_clean_and_soft_clean(session_factory, algorithms):
    session = session_factory()
    for execute_at, finished in ((61000.1, True), (61000.2, False)):
        timed = model.TimedDB(pid="P01", execute_at=execute_at)
        timed.finished = finished
        session.add(timed)
    session.add(model.TimedDB(pid="OTHER", execute_at=61000.3))
    session.commit()

    timed_algorithm = algorithms[2]

    timed_algorithm.soft_clean("P01")
    session = session_factory()
    assert (
        session.query(model.TimedDB)
        .filter(model.TimedDB.finished == True)  # noqa: E712
        .count()
        == 0
    )
    assert session.query(model.TimedDB).count() == 3

    timed_algorithm.clean("P01")
    session = session_factory()
    remaining = session.query(model.TimedDB).all()
    assert len(remaining) == 1
    assert remaining[0].pid == "OTHER"


def test_extinction_monitor_add_clean(session_factory, algorithms):
    session = session_factory()
    program, blockpar, block, target = _make_block(session, sched_algorithm=1)
    extmoni = algorithms[1]

    extmoni.add((block, blockpar, target))
    extmoni.add((block, blockpar, target))

    session = session_factory()
    info = session.query(model.ExtMoniDB).one()
    assert info.pid == "P01"
    assert info.target_id == target.id
    assert info.nairmass == 2

    # observed() records the altitude/airmass of the observation
    extmoni.observed(61000.0, (program, blockpar, block, target))
    session = session_factory()
    info = session.query(model.ExtMoniDB).one()
    assert len(info.observed_am) == 1
    assert info.observed_am[0].altitude == pytest.approx(90.0, abs=1.0)

    extmoni.soft_clean("P01")
    session = session_factory()
    assert session.query(model.ObservedAM).count() == 0

    extmoni.clean("P01")
    session = session_factory()
    assert session.query(model.ExtMoniDB).count() == 0


# --------------------------------------------------------------------------
# start-time pinning and derived slot lengths
# --------------------------------------------------------------------------


def test_only_packing_algorithms_leave_the_start_time_unpinned():
    """`start_at` is a hard 'do not start before' barrier in the chimera
    scheduler. It belongs to algorithms whose time carries meaning; for a
    monitoring sequence the slot times are a packing artefact, and pinning
    them turned every over-estimate of the block length into dead sky."""
    assert Timed.pin_start_time is True  # focus cadence, occultations
    assert Recurrent.pin_start_time is True
    assert SkyFlat.pin_start_time is True
    assert TimeSequence.pin_start_time is False


def test_unpinned_program_carries_no_start_at():
    program = model.Program(
        target_id=1, name="WASP-145A", pi="", priority=25, slew_at=61249.022
    )

    pinned = program.chimera_program()
    assert pinned.start_at == 61249.022

    unpinned = program.chimera_program(pin_start_time=False)
    # 0.0 is chimera's "no constraint" sentinel (machine: `if start_at:`)
    assert not unpinned.start_at
    # everything else must still cross over
    assert unpinned.name == "WASP-145A"
    assert unpinned.priority == -25


def test_slot_len_is_derived_from_the_block_when_not_configured():
    higher = Higher(None)
    blocks = [SimpleNamespace(length=985.0), SimpleNamespace(length=600.0)]

    # explicit config always wins
    assert higher._slot_len({"slot_len": 1500.0}, None, blocks=blocks) == 1500.0
    # then the caller's value
    assert higher._slot_len({}, 1200.0, blocks=blocks) == 1200.0
    # then the longest block plus the slew allowance
    assert higher._slot_len({}, None, blocks=blocks) == 985.0 + 60.0
    # and the per-algorithm default when there is nothing to derive from
    assert higher._slot_len({}, None, blocks=[]) == Higher.default_slot_len
    assert (
        higher._slot_len({}, None, blocks=[SimpleNamespace(length=None)])
        == Higher.default_slot_len
    )
