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
        config={"slot_len": 3600.0},
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
        config={"slot_len": 3600.0},
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
        config={"slot_len": 3600.0, "max_sched_blocks": 1},
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
        config={"slot_len": 3600.0},
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
        config={"slot_len": 3600.0},
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
    # observed but with no last_observation date (simulation leftover):
    # must stay eligible (recovered 2018 fix, mysql branch)
    simulated = _add_block(session, ra_hours=11.5, blockid=4, sched_algorithm=3)
    simulated.observed = True
    session.commit()

    algorithms = build_algorithms(factory, site)
    obs_start, obs_end = _window()
    slots = algorithms[3].process(
        obs_start=obs_start,
        obs_end=obs_end,
        query=_query(session),
        config={"recurrence": 7, "pid": "P01", "slot_len": 3600.0},
        today=UT,
    )

    scheduled = {b for b in slots["blockid"] if b > 0}
    assert 2 not in scheduled  # observed 2 days ago, recurrence is 7
    assert scheduled == {1, 3, 4}


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
        config={"times": [1.0], "pid": "P01", "slot_len": 3600.0},
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
    # the override must land on the CALLER's row, not only in the database
    # (a merged copy left the caller holding the stale slot time)
    assert chosen[0].slew_at == pytest.approx(61000.25)

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
        config={"n_stars": 1, "n_airmass": 2, "slot_len": 300.0},
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


def test_past_meridian_only_selects_setting_targets(session_factory, site):
    """Recovered mysql-branch feature: with past_meridian_only the west
    (setting) target is chosen even though the east one is higher."""
    factory = session_factory
    session = factory()
    # at night start LST is 10 h: RA 9 h is 1 h past the meridian (setting),
    # RA 10.2 h is still east of it (and higher in the sky)
    _add_block(session, ra_hours=9.0, blockid=1)
    _add_block(session, ra_hours=10.2, blockid=2)

    algorithms = build_algorithms(factory, site)
    obs_start = jd_from_datetime(UT)
    obs_end = obs_start + 0.5 / 24.0  # single slot

    slots = algorithms[0].process(
        obs_start=obs_start,
        obs_end=obs_end,
        query=_query(session),
        config={"slot_len": 1800.0, "past_meridian_only": True},
    )
    scheduled = [b for b in slots["blockid"] if b > 0]
    assert scheduled and scheduled[0] == 1  # the setting target wins

    # without the flag, the higher (east) target wins the first slot
    slots = algorithms[0].process(
        obs_start=obs_start,
        obs_end=obs_end,
        query=_query(session),
        config={"slot_len": 1800.0},
    )
    scheduled = [b for b in slots["blockid"] if b > 0]
    assert scheduled and scheduled[0] == 2


def test_past_meridian_only_handles_ra_wrap(session_factory):
    """The legacy ``lst > ra`` comparison misclassified targets near RA 0;
    the hour-angle test must not."""
    factory = session_factory
    session = factory()
    # LST 23.9 h: RA 23.5 h crossed the meridian 0.4 h ago (eligible);
    # RA 0.1 h is 0.2 h EAST of the meridian (legacy lst>ra wrongly
    # classified it as past meridian)
    _add_block(session, ra_hours=23.5, blockid=1)
    _add_block(session, ra_hours=0.1, blockid=2)

    site = RotatingSite(latitude=0.0, lst_rads=23.9 * math.pi / 12.0, ut_now=UT)
    algorithms = build_algorithms(factory, site)
    obs_start = jd_from_datetime(UT)

    slots = algorithms[0].process(
        obs_start=obs_start,
        obs_end=obs_start + 0.25 / 24.0,  # single slot
        query=_query(session),
        config={"slot_len": 1800.0, "past_meridian_only": True},
    )
    scheduled = [b for b in slots["blockid"] if b > 0]
    # RA 0.1 h is higher in the sky but still east: RA 23.5 h must win
    assert scheduled and scheduled[0] == 1


def test_timed_next_falls_back_when_candidate_fails_conditions(session_factory):
    """Recovered-from-live improvement: if the closest candidate is not
    observable at execute_at (e.g. inside its moon limit), Timed tries the
    next one instead of giving the request up for the night."""
    from chimera_robobs.scheduling.engine import RobObsEngine

    factory = session_factory
    session = factory()
    # fake moon sits at RA 12 h, dec 0: the RA 12.2 h target is ~3 deg away
    # (fails min_moon_distance 30), the RA 9 h target is ~45 deg away
    near_moon = _add_block(session, ra_hours=12.2, blockid=1, sched_algorithm=2)
    far_moon = _add_block(session, ra_hours=9.0, blockid=2, sched_algorithm=2)
    for blockpar in session.query(model.BlockPar):
        blockpar.min_moon_distance = 30.0
    session.commit()

    site = RotatingSite(latitude=0.0, lst_rads=10.0 * math.pi / 12.0, ut_now=UT)
    now = site.mjd()
    execute_at = now + 0.02

    programs = []
    for i, block in enumerate((near_moon, far_moon)):
        target = session.query(model.Target).get(block.target_id)
        blockpar = session.query(model.BlockPar).get(block.block_par_id)
        program = model.Program(
            target_id=target.id,
            name=target.name,
            priority=1,
            # the near-moon target's slot is closest to "now": the legacy
            # behavior would commit to it and fail
            slew_at=now + i * 0.01,
            pid="P01",
            obsblock_id=block.id,
            blockpar_id=blockpar.id,
        )
        session.add(program)
        programs.append(program)
    session.add(model.TimedDB(pid="P01", execute_at=execute_at))
    session.commit()

    engine = RobObsEngine(factory, site, algorithms=build_algorithms(factory, site))

    chosen, _ = engine.get_program(now, 1)
    assert chosen is not None
    # the far-from-moon target won although the near one was closer in time
    assert chosen[0].id == programs[1].id
    assert chosen[0].slew_at == pytest.approx(execute_at)

    # bookkeeping follows the actually-chosen candidate
    session = factory()
    timed = session.query(model.TimedDB).one()
    assert timed.target_id == far_moon.target_id
    assert timed.block_id == far_moon.id

    # and when NO candidate is observable, the request yields nothing
    for blockpar in session.query(model.BlockPar):
        blockpar.min_moon_distance = 179.0
    session.commit()
    chosen, _ = engine.get_program(now, 1)
    assert chosen is None
