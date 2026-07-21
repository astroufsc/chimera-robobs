# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2014-present chimera-robobs authors

"""RobObs controller tests: machine states, event reactions and the
scheduler-idle handling — all without a bus (fake proxies, temp databases)."""

import math
import time

import pytest
from chimera.controllers.scheduler import model as chimera_model
from chimera.controllers.scheduler.states import State as SchedState
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from chimera_robobs.controllers.robobs import (
    EMPTY_QUEUE_RETRY,
    Machine,
    MachineState,
    RobObs,
    RobState,
)
from chimera_robobs.scheduling import model
from chimera_robobs.scheduling.algorithms import build_algorithms
from chimera_robobs.scheduling.engine import RobObsEngine

from .fakes import FakeSchedulerProxy, FakeSite, FakeTelescopeProxy


@pytest.fixture
def chimera_session(tmp_path, monkeypatch):
    """Point the chimera scheduler model at a temp database (its module-level
    Session would otherwise write to ~/.chimera/scheduler.db)."""
    engine = create_engine(f"sqlite:///{tmp_path / 'scheduler.db'}")
    chimera_model.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(chimera_model, "Session", factory)
    return factory


@pytest.fixture
def rob(tmp_path, chimera_session):
    """A RobObs wired up with fakes, machine not started."""
    controller = RobObs()
    controller._session = model.open_database(str(tmp_path / "robobs.db"))
    controller._site = FakeSite(latitude=0.0, lst_rads=10.0 * math.pi / 12.0)
    controller._algorithms = build_algorithms(controller._session, controller._site)
    controller.engine = RobObsEngine(
        controller._session,
        controller._site,
        log=controller.log,
        algorithms=controller._algorithms,
    )
    controller._fake_scheduler = FakeSchedulerProxy()
    controller.get_scheduler = lambda: controller._fake_scheduler
    controller._fake_telescope = FakeTelescopeProxy()
    controller.get_proxy = lambda location=None: controller._fake_telescope
    controller.machine = Machine(controller)
    return controller


def _populate_program(controller, slew_at=0.0):
    session = controller._session()
    target = model.Target(name="tgt", target_ra=10.0, target_dec=0.0)
    session.add(target)
    session.commit()
    blockpar = model.BlockPar(bid=1, pid="P01")
    blockpar.sched_algorithm = 0
    session.add(blockpar)
    session.commit()
    block = model.ObsBlock(
        target_id=target.id, blockid=1, pid="P01", block_par_id=blockpar.id
    )
    block.actions.append(model.Expose(frames=1, exptime=1.0))
    session.add(block)
    session.commit()
    program = model.Program(
        target_id=target.id,
        name=target.name,
        priority=1,
        slew_at=slew_at,
        pid="P01",
        obsblock_id=block.id,
        blockpar_id=blockpar.id,
    )
    session.add(program)
    session.commit()
    return program


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_state_string(rob):
    assert rob.state() == "robstate=OFF machine=OFF"
    rob.rob_state = RobState.ON
    assert rob.state().startswith("robstate=ON")


def test_machine_start_walk(rob):
    rob.machine.start()
    try:
        assert rob.machine.state() == MachineState.OFF

        # a wake with robobs OFF must NOT start the chimera scheduler:
        # it would replay stale queued programs (the 2026-07-20 daytime
        # exposures with the dome closed)
        rob.wake()
        assert _wait_for(lambda: rob.machine.state() == MachineState.OFF)
        assert "start" not in rob._fake_scheduler.calls

        rob.rob_state = RobState.ON
        rob.wake()
        assert _wait_for(lambda: "start" in rob._fake_scheduler.calls)
        assert _wait_for(lambda: rob.machine.state() == MachineState.BUSY)
    finally:
        rob.machine.state(MachineState.SHUTDOWN)
        rob.machine.join(timeout=5.0)
        assert not rob.machine.is_alive()


def test_handle_scheduler_idle_submits_program(rob, chimera_session):
    _populate_program(rob)
    rob.rob_state = RobState.ON

    assert rob._handle_scheduler_idle() == 0.0

    # the program (with actions) landed on the chimera scheduler queue
    csession = chimera_session()
    cprogram = csession.query(chimera_model.Program).one()
    assert cprogram.name == "tgt"
    assert len(cprogram.actions) == 1

    # bookkeeping on the robobs side
    session = rob._session()
    assert session.query(model.Program).one().finished is True
    assert rob._current_program is not None


def test_handle_scheduler_idle_when_off_does_nothing(rob, chimera_session):
    _populate_program(rob)
    rob.rob_state = RobState.OFF
    assert rob._handle_scheduler_idle() is None
    assert chimera_session().query(chimera_model.Program).count() == 0


def test_handle_scheduler_idle_empty_queue_parks_then_backs_off(rob, chimera_session):
    rob.rob_state = RobState.ON

    # first time: a SAFETY park program is queued
    assert rob._handle_scheduler_idle() == 0.0
    csession = chimera_session()
    safety = csession.query(chimera_model.Program).one()
    assert safety.name == "SAFETY"
    assert rob._no_program_on_queue is True

    # afterwards: back off instead of stacking park programs
    assert rob._handle_scheduler_idle() == EMPTY_QUEUE_RETRY
    assert chimera_session().query(chimera_model.Program).count() == 1


def test_state_changed_event_runs_reschedule_on_machine_thread(rob):
    _populate_program(rob)
    rob.rob_state = RobState.ON
    rob.machine.start()
    try:
        # only IDLE -> OFF triggers; anything else is ignored
        rob._watch_state_changed(SchedState.BUSY, SchedState.IDLE)
        time.sleep(0.05)
        assert rob._current_program is None

        started = time.monotonic()
        rob._watch_state_changed(SchedState.OFF, SchedState.IDLE)
        elapsed = time.monotonic() - started
        # the event handler must return immediately (work happens on the
        # machine thread, not on the caller/bus thread)
        assert elapsed < 0.5
        assert _wait_for(lambda: rob._current_program is not None)
        assert _wait_for(lambda: "start" in rob._fake_scheduler.calls)
    finally:
        rob.machine.state(MachineState.SHUTDOWN)
        rob.machine.join(timeout=5.0)


def test_state_changed_ignored_when_off(rob):
    _populate_program(rob)
    rob.rob_state = RobState.OFF
    rob.machine.start()
    try:
        rob._watch_state_changed(SchedState.OFF, SchedState.IDLE)
        time.sleep(0.1)
        assert rob._current_program is None
        assert rob._fake_scheduler.calls == []
    finally:
        rob.machine.state(MachineState.SHUTDOWN)
        rob.machine.join(timeout=5.0)


def test_empty_queue_wait_is_interruptible(rob, monkeypatch):
    """The 5-minute empty-queue backoff must not block shutdown (the legacy
    handler slept 300 s on the bus dispatch thread)."""
    rob.rob_state = RobState.ON
    rob.machine.start()
    try:
        # empty robobs queue: park once, then a timed wait
        rob._watch_state_changed(SchedState.OFF, SchedState.IDLE)
        assert _wait_for(lambda: rob._no_program_on_queue)
        rob._watch_state_changed(SchedState.OFF, SchedState.IDLE)
        time.sleep(0.1)

        started = time.monotonic()
        rob.machine.state(MachineState.SHUTDOWN)
        rob.machine.join(timeout=5.0)
        assert not rob.machine.is_alive()
        assert time.monotonic() - started < 5.0
    finally:
        if rob.machine.is_alive():
            rob.machine.state(MachineState.SHUTDOWN)
            rob.machine.join(timeout=5.0)


def test_program_complete_ok_marks_observed(rob, chimera_session):
    program = _populate_program(rob)
    rob.rob_state = RobState.ON
    assert rob._handle_scheduler_idle() == 0.0
    assert rob._current_program is not None

    csession = chimera_session()
    cprogram = csession.query(chimera_model.Program).one()

    rob._watch_program_complete(cprogram.id, "OK")

    assert rob._current_program is None
    session = rob._session()
    block = session.query(model.ObsBlock).one()
    assert block.observed is True
    log_entries = session.query(model.ObservingLog).all()
    assert any("Program End" in entry.action for entry in log_entries)
    assert session.query(model.Program).get(program.id).finished is True


def test_program_complete_error_stops_robobs(rob, chimera_session):
    _populate_program(rob)
    rob.rob_state = RobState.ON
    rob._handle_scheduler_idle()
    csession = chimera_session()
    cprogram = csession.query(chimera_model.Program).one()

    rob._watch_program_complete(cprogram.id, "ERROR", "boom")

    assert rob.rob_state == RobState.OFF
    assert rob._current_program is not None  # kept for a retry after restart


def test_program_begin_writes_observing_log(rob, chimera_session):
    _populate_program(rob)
    rob.rob_state = RobState.ON
    rob._handle_scheduler_idle()
    cprogram = chimera_session().query(chimera_model.Program).one()

    rob._watch_program_begin(cprogram.id)

    session = rob._session()
    entries = session.query(model.ObservingLog).all()
    assert any("Program Started" in entry.action for entry in entries)

    # unknown ids must not blow up
    rob._watch_program_begin(99999)


def test_start_cleans_stale_scheduler_queue(rob, chimera_session):
    """Recovered mysql-branch behavior (e91e84f): switching robobs on wipes
    programs left over in the chimera scheduler queue."""
    csession = chimera_session()
    stale = chimera_model.Program(name="STALE", pi="OLD", priority=1)
    stale.actions.append(chimera_model.Expose(frames=1, exptime=1))
    csession.add(stale)
    csession.commit()

    assert rob.start() is True
    csession = chimera_session()
    assert csession.query(chimera_model.Program).count() == 0
    assert csession.query(chimera_model.Action).count() == 0

    # configurable: with the option off the queue is preserved
    rob["clean_scheduler_on_start"] = False
    csession.add(chimera_model.Program(name="KEEP", pi="OLD", priority=1))
    csession.commit()
    rob.start()
    assert chimera_session().query(chimera_model.Program).count() == 1


def test_program_complete_stops_telescope_tracking(rob, chimera_session):
    """Any finished program stops the telescope tracking so the mount never
    tracks into a limit."""
    _populate_program(rob)
    rob.rob_state = RobState.ON
    rob._handle_scheduler_idle()
    cprogram = chimera_session().query(chimera_model.Program).one()

    rob._watch_program_complete(cprogram.id, "OK")
    assert _wait_for(lambda: "stop_tracking" in rob._fake_telescope.calls)

    # not tracking: checked but not commanded
    rob._fake_telescope.calls.clear()
    rob._fake_telescope.tracking = False
    rob._watch_program_complete(cprogram.id, "ERROR", "boom")
    assert _wait_for(lambda: "is_tracking" in rob._fake_telescope.calls)
    time.sleep(0.05)
    assert "stop_tracking" not in rob._fake_telescope.calls


def test_tracking_stop_disabled_without_telescope(rob, chimera_session):
    rob["telescope"] = None
    _populate_program(rob)
    rob.rob_state = RobState.ON
    rob._handle_scheduler_idle()
    cprogram = chimera_session().query(chimera_model.Program).one()

    rob._watch_program_complete(cprogram.id, "OK")
    time.sleep(0.1)
    assert rob._fake_telescope.calls == []
