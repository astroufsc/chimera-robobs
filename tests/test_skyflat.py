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
    # what the controller reports actually taking - the only source the
    # ledger trusts
    program._skyflat_frames_taken = {"V": 7}

    algorithms[5].observed(61000.0, row, soft=True)
    assert session_factory().query(model.SkyFlatDB).count() == 0  # simulation

    algorithms[5].observed(61000.0, row, soft=False)
    ledger = session_factory().query(model.SkyFlatDB).one()
    assert (ledger.filter, ledger.frames) == ("V", 7)
    assert ledger.observed_at is not None


def test_a_set_that_took_nothing_writes_no_ledger_entry(session_factory, site):
    """An empty twilight must not look like coverage.

    The configured frame counts used to stand in when the controller sent
    no report - but no report is exactly what a set that exposed NOTHING
    produces. On opd-40 2026-07-30 four real CLEAR frames were followed by
    five 9-frame entries for filters that never opened the shutter: 4
    frames on disk, 49 in the ledger. The fewest-flats selection then reads
    those filters as freshly covered and skips them, so one empty twilight
    costs the next several nights of rotation as well.
    """
    session = session_factory()
    block = _add_flat_block(session, 1, "V", frames=9)
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

    # the controller never reported a frame: the set took nothing
    algorithms[5].observed(61000.0, row, soft=False)
    assert session_factory().query(model.SkyFlatDB).count() == 0

    # and a filter that reports zero frames is not coverage either
    program._skyflat_frames_taken = {"V": 0, "R": 3}
    algorithms[5].observed(61000.0, row, soft=False)
    ledger = session_factory().query(model.SkyFlatDB).all()
    assert [(e.filter, e.frames) for e in ledger] == [("R", 3)]


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

    # in twilight the night/airmass/moon checks are waived...
    day_site = FakeSite(daytime=True, sun_alt=-8.0)
    engine = RobObsEngine(session_factory, day_site, log=LOG)
    assert engine.check_conditions((program, blockpar, block, target), 61000.0)

    # ...but not in the deep night: the waiver used to be unconditional, so
    # flats were offered at sun -46 deg, served ahead of everything and only
    # declined by the controller after it had slewed to the flat position
    night_site = FakeSite(daytime=True, sun_alt=-46.0)
    night_engine = RobObsEngine(session_factory, night_site, log=LOG)
    assert not night_engine.check_conditions(
        (program, blockpar, block, target), 61000.0
    )

    # a regular program under the same sky is rejected by the night guard
    blockpar.sched_algorithm = 0
    session.commit()
    assert not engine.check_conditions((program, blockpar, block, target), 61000.0)


def test_block_duration_counts_autoflat_frames():
    actions = [model.AutoFlat(filter="R", frames=9)]
    assert model.block_duration(actions, autoflat_frame=60.0) == pytest.approx(540.0)
    assert model.block_duration(actions) == pytest.approx(0.0)


def test_ledger_survives_block_id_reassignment(session_factory):
    """The ledger must record the filter the flats were actually taken in.

    observed() used to resolve the filter through the program's obsblock id
    at COMPLETION time - but a clean/reload between selection and completion
    reassigns those ids, so the lookup landed on a different filter's block.
    Live on 2026-07-22: a 9-frame CLEAR set entered the ledger as "I", and
    the need-order selection started chasing phantom coverage.
    """
    factory = session_factory
    session = factory()
    block = _add_flat_block(session, 1, "CLEAR")
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

    algorithm = build_algorithms(factory, FakeSite())[5]
    row = (program, blockpar, block, target)

    chosen = algorithm.next(61000.0, [row])
    assert chosen is not None
    # the controller reports the filter it actually used
    chosen[0]._skyflat_frames_taken = {"CLEAR": 9}

    # a clean/reload between selection and completion: the same obsblock id
    # now carries a different filter
    for act in session.query(model.AutoFlat):
        act.filter = "I"
    session.commit()

    algorithm.observed(61000.01, chosen)

    ledger = session.query(model.SkyFlatDB).all()
    assert len(ledger) == 1
    assert ledger[0].filter == "CLEAR", (
        f"ledger recorded {ledger[0].filter!r} for a CLEAR run"
    )


def test_execution_order_is_sensitivity_not_need(session_factory):
    """Need picks WHICH filters run; sensitivity must set the ORDER.

    With real ledger history the two disagree, and the need-order put R at
    the darkest morning sky and CLEAR in the brightest (2026-07-22) - the
    opposite of what each filter wants: the most sensitive filter belongs
    in the darkest sky.
    """
    import datetime as dt

    factory = session_factory
    session = factory()
    _add_flat_block(session, 1, "CLEAR")  # most sensitive
    _add_flat_block(session, 2, "R")
    _add_flat_block(session, 3, "HBETA")  # least sensitive
    # CLEAR is well covered, R less, HBETA untouched: need order is
    # HBETA, R, CLEAR - the reverse of sensitivity
    now = dt.datetime(2026, 7, 22, 12, 0)
    session.add(model.SkyFlatDB(pid=PID, filter="CLEAR", frames=18, observed_at=now))
    session.add(model.SkyFlatDB(pid=PID, filter="R", frames=9, observed_at=now))
    session.commit()

    algorithm = build_algorithms(factory, FakeSite(ut_now=now))[5]
    rows = [
        (
            block,
            session.query(model.BlockPar).filter_by(id=block.block_par_id).one(),
            session.query(model.Target).one(),
        )
        for block in session.query(model.ObsBlock).order_by(model.ObsBlock.blockid)
    ]

    class Query(list):
        def __getitem__(self, item):
            return list.__getitem__(self, item) if isinstance(item, int) else list(self)

    slots = algorithm.process(
        obs_start=2461244.5,
        obs_end=2461245.0,
        query=Query(rows),
        config={
            "pid": PID,
            "flat_window": "both",
            "n_filters": {"evening": 1, "morning": 2},
        },
    )

    by_start = sorted(slots, key=lambda s: s["start"])
    # evening slot(s) first, then morning. Morning got R and CLEAR (the two
    # neediest after HBETA went to the evening)... whichever filters need
    # chose, the MORNING pair must run most-sensitive first:
    morning = by_start[-2:]
    assert morning[0]["blockid"] < morning[1]["blockid"], (
        f"morning runs blockid {morning[0]['blockid']} before "
        f"{morning[1]['blockid']}: least sensitive placed in the darkest sky"
    )


class RisingSunSite(FakeSite):
    """A site whose sun actually climbs, so the morning anchor can be
    resolved: altitude = alt0 + rate * (t - dawn), degrees per hour."""

    def __init__(self, dawn, alt0=-18.0, rate=13.0, **kwargs):
        super().__init__(**kwargs)
        self._dawn = dawn
        self._alt0 = alt0
        self._rate = rate

    def sun_altitude(self, date=None) -> float:
        when = self._parse(date)
        hours = (when - self._dawn).total_seconds() / 3600.0
        return float(self._alt0 + self._rate * hours)


def test_the_morning_set_can_be_anchored_where_the_controller_can_expose(
    session_factory,
):
    """Anchoring the morning flats at the night's end hands the telescope
    over long before the controller can take a frame: on opd-40 the -18 deg
    anchor was 46 min ahead of the first exposable frame at -8, and the
    scheduler simply waited through usable sky (lna40 PENDING_ISSUES 50).
    """
    session = session_factory()
    _add_flat_block(session, 1, "CLEAR")
    _add_flat_block(session, 2, "R")

    dawn_dt = UT + dt.timedelta(hours=10)
    site = RisingSunSite(dawn=dawn_dt, ut_now=UT)
    algorithm = build_algorithms(session_factory, site)[5]
    jd_start, jd_end = jd_from_datetime(UT), jd_from_datetime(dawn_dt)

    def morning_starts(config):
        slots = algorithm.process(
            obs_start=jd_start,
            obs_end=jd_end,
            query=_query(session),
            config=dict({"pid": PID, "flat_window": "morning"}, **config),
        )
        return sorted(float(s[0]) for s in slots)

    at_dawn = morning_starts({})
    at_minus_8 = morning_starts({"flat_sun_alt": -8.0})

    assert len(at_dawn) == len(at_minus_8) == 2
    # -18 -> -8 at 13 deg/h is ~46 min, and the stagger is preserved
    delay_min = (at_minus_8[0] - at_dawn[0]) * 24 * 60
    assert delay_min == pytest.approx(10.0 / 13.0 * 60, abs=1.0)
    assert (at_minus_8[1] - at_minus_8[0]) == pytest.approx(60.0 / 86400.0, rel=1e-3)


def test_the_morning_anchor_is_left_alone_when_the_sun_never_gets_there(
    session_factory,
):
    """A target the sun does not reach inside the search span must not
    silently move the flats: fall back to the night's end."""
    session = session_factory()
    _add_flat_block(session, 1, "CLEAR")

    dawn_dt = UT + dt.timedelta(hours=10)
    site = RisingSunSite(dawn=dawn_dt, ut_now=UT, rate=0.1)  # barely climbs
    algorithm = build_algorithms(session_factory, site)[5]

    slots = algorithm.process(
        obs_start=jd_from_datetime(UT),
        obs_end=jd_from_datetime(dawn_dt),
        query=_query(session),
        config={"pid": PID, "flat_window": "morning", "flat_sun_alt": -8.0},
    )

    assert float(slots[0][0]) == pytest.approx(jd_from_datetime(dawn_dt), abs=1e-9)
