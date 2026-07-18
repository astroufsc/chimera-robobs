# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2014-present chimera-robobs authors

"""Slot-allocation (process()) tests for the scheduling algorithms."""

import datetime as dt
import math

import pytest

from chimera_robobs.scheduling import model
from chimera_robobs.scheduling.algorithms import build_algorithms
from chimera_robobs.scheduling.dates import MJD_JD_OFFSET, jd_from_datetime

from .fakes import UT, RotatingSite

#: night start such that a target at RA 10 h is on the meridian at sunset
NIGHT_START_LST_HOURS = 10.0


@pytest.fixture
def session_factory(tmp_path):
    return model.open_database(str(tmp_path / "robobs.db"))


@pytest.fixture
def site():
    return RotatingSite(
        latitude=0.0, lst_rads=NIGHT_START_LST_HOURS * math.pi / 12.0, ut_now=UT
    )


@pytest.fixture
def algorithms(session_factory, site):
    return build_algorithms(session_factory, site)


def _add_block(
    session,
    ra_hours,
    blockid,
    pid="P01",
    sched_algorithm=0,
    max_airmass=5.0,
    exptime=30.0,
    observed_days_ago=None,
):
    target = model.Target(name=f"tgt{blockid}", target_ra=ra_hours, target_dec=0.0)
    session.add(target)
    session.commit()
    blockpar = model.BlockPar(bid=blockid, pid=pid)
    blockpar.max_airmass = max_airmass
    blockpar.sched_algorithm = sched_algorithm
    session.add(blockpar)
    session.commit()
    block = model.ObsBlock(
        target_id=target.id, blockid=blockid, pid=pid, block_par_id=blockpar.id
    )
    block.actions.append(model.Expose(frames=1, exptime=exptime))
    if observed_days_ago is not None:
        block.observed = True
        block.last_observation = UT.replace(tzinfo=None) - dt.timedelta(
            days=observed_days_ago
        )
    session.add(block)
    session.commit()
    return block


def _query(session, pid="P01"):
    from sqlalchemy import desc

    return (
        session.query(model.ObsBlock, model.BlockPar, model.Target)
        .join(model.BlockPar, model.ObsBlock.block_par_id == model.BlockPar.id)
        .join(model.Target, model.ObsBlock.target_id == model.Target.id)
        .filter(model.ObsBlock.pid == pid)
        .order_by(desc(model.Target.target_ah))
    )


NIGHT_HOURS = 2.0


def _window():
    jd_start = jd_from_datetime(UT)
    return jd_start, jd_start + NIGHT_HOURS / 24.0


def test_higher_allocates_highest_and_removes_selected(
    session_factory, algorithms, site
):
    session = session_factory()
    # RA 10 h culminates at night start; RA 11 h one hour later
    _add_block(session, ra_hours=10.0, blockid=1)
    _add_block(session, ra_hours=11.0, blockid=2)

    obs_start, obs_end = _window()
    slots = algorithms[0].process(
        obs_start=obs_start,
        obs_end=obs_end,
        query=_query(session),
        config={"slotLen": 3600.0},
    )

    # first slot: RA 10 h is at the zenith; second slot: it was removed from
    # the candidate pool, so RA 11 h (now culminating) is chosen
    scheduled = [b for b in slots["blockid"] if b > 0]
    assert scheduled == [1, 2]


def test_timesequence_keeps_selected_target(session_factory, algorithms, site):
    session = session_factory()
    _add_block(session, ra_hours=10.5, blockid=1, sched_algorithm=4)
    _add_block(session, ra_hours=18.0, blockid=2, sched_algorithm=4)

    obs_start, obs_end = _window()
    slots = algorithms[4].process(
        obs_start=obs_start,
        obs_end=obs_end,
        query=_query(session),
        config={"slotLen": 3600.0},
    )

    # the same (higher) target is selected in every slot: a time sequence
    scheduled = [b for b in slots["blockid"] if b > 0]
    assert len(scheduled) >= 2 and set(scheduled) == {1}


def test_higher_max_sched_blocks(session_factory, algorithms, site):
    session = session_factory()
    _add_block(session, ra_hours=10.0, blockid=1)
    _add_block(session, ra_hours=11.0, blockid=2)

    obs_start, obs_end = _window()
    slots = algorithms[0].process(
        obs_start=obs_start,
        obs_end=obs_end,
        query=_query(session),
        config={"slotLen": 3600.0, "max_sched_blocks": 1},
    )
    assert list(slots["blockid"]).count(1) + list(slots["blockid"]).count(2) == 1


def test_higher_moon_distance_veto(session_factory, site):
    factory = session_factory
    session = factory()
    _add_block(session, ra_hours=10.0, blockid=1)
    blockpar = session.query(model.BlockPar).one()
    blockpar.min_moon_distance = 60.0  # moon fake sits at RA 12 h (~30 deg)
    session.commit()

    algorithms = build_algorithms(factory, site)
    obs_start, obs_end = _window()
    slots = algorithms[0].process(
        obs_start=obs_start,
        obs_end=obs_end,
        query=_query(session),
        config={"slotLen": 3600.0},
    )
    assert set(slots["blockid"]) == {-1}


def test_higher_airmass_veto(session_factory, site):
    factory = session_factory
    session = factory()
    # RA 22 h is ~30 deg below the horizon at LST 10 h
    _add_block(session, ra_hours=22.0, blockid=1, max_airmass=1.5)

    algorithms = build_algorithms(factory, site)
    obs_start, obs_end = _window()
    slots = algorithms[0].process(
        obs_start=obs_start,
        obs_end=obs_end,
        query=_query(session),
        config={"slotLen": 3600.0},
    )
    assert set(slots["blockid"]) == {-1}


def test_recurrent_process_filters_by_recurrence(session_factory, site):
    factory = session_factory
    session = factory()
    _add_block(session, ra_hours=10.0, blockid=1, sched_algorithm=3)  # never
    _add_block(
        session, ra_hours=10.5, blockid=2, sched_algorithm=3, observed_days_ago=2
    )  # too recent
    _add_block(
        session, ra_hours=11.0, blockid=3, sched_algorithm=3, observed_days_ago=30
    )  # old enough

    algorithms = build_algorithms(factory, site)
    obs_start, obs_end = _window()
    slots = algorithms[3].process(
        obs_start=obs_start,
        obs_end=obs_end,
        query=_query(session),
        config={"recurrence": 7, "pid": "P01", "slotLen": 3600.0},
        today=UT,
    )

    scheduled = {b for b in slots["blockid"] if b > 0}
    assert 2 not in scheduled  # observed 2 days ago, recurrence is 7
    assert scheduled == {1, 3}


def test_recurrent_process_requires_recurrence(session_factory, algorithms):
    from chimera_robobs.scheduling.algorithms.base import RecurrentError

    with pytest.raises(RecurrentError):
        algorithms[3].process(obs_start=0.0, obs_end=1.0, query=None, config={})


def test_timed_process_stores_execute_times(session_factory, site):
    factory = session_factory
    session = factory()
    _add_block(session, ra_hours=10.0, blockid=1, sched_algorithm=2)

    algorithms = build_algorithms(factory, site)
    obs_start, obs_end = _window()
    algorithms[2].process(
        obs_start=obs_start,
        obs_end=obs_end,
        query=_query(session),
        config={"times": [1.0], "pid": "P01", "slotLen": 3600.0},
    )

    session = factory()
    timed = session.query(model.TimedDB).one()
    assert timed.pid == "P01"
    assert timed.execute_at == pytest.approx(obs_start - MJD_JD_OFFSET + 1.0 / 24.0)


def test_timed_next_overrides_slew_at(session_factory, site):
    factory = session_factory
    session = factory()
    block = _add_block(session, ra_hours=10.0, blockid=1, sched_algorithm=2)
    target = session.query(model.Target).one()
    blockpar = session.query(model.BlockPar).one()
    program = model.Program(
        target_id=target.id,
        name=target.name,
        priority=1,
        slew_at=61000.0,
        pid="P01",
        obsblock_id=block.id,
        blockpar_id=blockpar.id,
    )
    session.add(program)
    session.add(model.TimedDB(pid="P01", execute_at=61000.25))
    session.commit()

    algorithms = build_algorithms(factory, site)
    chosen = algorithms[2].next(61000.0, [(program, blockpar, block, target)])
    assert chosen is not None
    session = factory()
    assert session.query(model.Program).one().slew_at == pytest.approx(61000.25)

    # observed() marks the timed request as done
    algorithms[2].observed(61000.25, (program, blockpar, block, target), soft=True)
    session = factory()
    assert session.query(model.TimedDB).one().finished is True

    # with all requests finished there is nothing left to do
    assert algorithms[2].next(61000.3, [(program, blockpar, block, target)]) is None


def test_extinction_monitor_process_allocates_airmass_ladder(session_factory, site):
    factory = session_factory
    session = factory()
    # target past culmination at night start: airmass increases monotonically
    _add_block(session, ra_hours=9.0, blockid=1, sched_algorithm=1, max_airmass=2.5)

    algorithms = build_algorithms(factory, site)
    jd_start = jd_from_datetime(UT)
    slots = algorithms[1].process(
        obs_start=jd_start,
        obs_end=jd_start + 6.0 / 24.0,
        query=_query(session),
        config={"nstars": 1, "nairmass": 2, "slotLen": 300.0},
    )

    assert len(slots) == 2  # one star at two different airmasses
    assert set(slots["blockid"]) == {1}
    assert slots["start"][0] != slots["start"][1]


def test_extinction_monitor_next_skips_covered_levels(session_factory, site):
    factory = session_factory
    session = factory()
    # slightly past the meridian: exactly at the zenith the target sits
    # above the algorithm's max-altitude cut (the legacy 0.999*ra quirk)
    block = _add_block(session, ra_hours=9.8, blockid=1, sched_algorithm=1)
    target = session.query(model.Target).one()
    blockpar = session.query(model.BlockPar).one()
    blockpar.max_airmass = 2.5
    program = model.Program(
        target_id=target.id,
        name=target.name,
        priority=1,
        slew_at=0.0,  # in the past
        pid="P01",
        obsblock_id=block.id,
        blockpar_id=blockpar.id,
    )
    session.add(program)
    session.commit()

    algorithms = build_algorithms(factory, site)
    extmoni = algorithms[1]
    now_mjd = site.mjd()

    # not in the bookkeeping table yet: skipped
    assert extmoni.next(now_mjd, [(program, blockpar, block, target)]) is None

    extmoni.add((block, blockpar, target))
    chosen = extmoni.next(now_mjd, [(program, blockpar, block, target)])
    assert chosen is not None

    # observing it covers the current altitude level...
    extmoni.observed(now_mjd, (program, blockpar, block, target), soft=True)
    session = factory()
    info = session.query(model.ExtMoniDB).one()
    assert len(info.observed_am) == 1

    # ...so the same altitude is not selected again
    assert extmoni.next(now_mjd, [(program, blockpar, block, target)]) is None
