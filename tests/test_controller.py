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
    assert rob._handed


def test_handle_scheduler_idle_when_off_does_nothing(rob, chimera_session):
    _populate_program(rob)
    rob.rob_state = RobState.OFF
    assert rob._handle_scheduler_idle() is None
    assert chimera_session().query(chimera_model.Program).count() == 0


def test_handle_scheduler_idle_empty_queue_backs_off_without_moving(
    rob, chimera_session
):
    """An empty queue must not move the telescope.

    This used to enqueue a SAFETY program pointing at a fixed alt/az park
    position, so a routine gap between programs dragged the mount off the
    sky - and on a night where nothing was currently observable it did so
    on every retry. Parking is the supervisor's end-of-night job.
    """
    rob.rob_state = RobState.ON

    assert rob._handle_scheduler_idle() == 0.0
    assert chimera_session().query(chimera_model.Program).count() == 0
    assert rob._no_program_on_queue is True

    # afterwards: simply back off
    assert rob._handle_scheduler_idle() == EMPTY_QUEUE_RETRY
    assert chimera_session().query(chimera_model.Program).count() == 0


def test_state_changed_event_runs_reschedule_on_machine_thread(rob):
    _populate_program(rob)
    rob.rob_state = RobState.ON
    rob.machine.start()
    try:
        # only IDLE -> OFF triggers; anything else is ignored
        rob._watch_state_changed(SchedState.BUSY, SchedState.IDLE)
        time.sleep(0.05)
        assert not rob._handed

        started = time.monotonic()
        rob._watch_state_changed(SchedState.OFF, SchedState.IDLE)
        elapsed = time.monotonic() - started
        # the event handler must return immediately (work happens on the
        # machine thread, not on the caller/bus thread)
        assert elapsed < 0.5
        assert _wait_for(lambda: bool(rob._handed))
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
        assert not rob._handed
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
    assert rob._handed

    csession = chimera_session()
    cprogram = csession.query(chimera_model.Program).one()

    rob._watch_program_complete(cprogram.id, "OK")

    assert not rob._handed
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
    # the stop's queue clean un-finishes the errored program (its chimera
    # row never completed), so the next start re-offers it - retries now go
    # through the recovery path, not a stale in-memory pointer
    session = rob._session()
    assert session.query(model.Program).one().finished is False


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


def test_program_complete_leaves_tracking_to_the_scheduler(rob, chimera_session):
    """robobs must not touch tracking: the stop belongs to the scheduler, which
    issues it inline at program end.  Stopping it from here raced the next
    program's slew and untracked the target it had just acquired."""
    _populate_program(rob)
    rob.rob_state = RobState.ON
    rob._handle_scheduler_idle()
    cprogram = chimera_session().query(chimera_model.Program).one()

    rob._watch_program_complete(cprogram.id, "OK")
    time.sleep(0.1)
    assert rob._fake_telescope.calls == []


def _populate_timed_program(controller):
    """A timed (algorithm 2) program with one due occurrence."""
    session = controller._session()
    target = model.Target(name="tgt", target_ra=10.0, target_dec=0.0)
    session.add(target)
    session.commit()
    blockpar = model.BlockPar(bid=1, pid="P01")
    blockpar.sched_algorithm = 2
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
        slew_at=controller._site.mjd(),
        pid="P01",
        obsblock_id=block.id,
        blockpar_id=blockpar.id,
    )
    session.add(program)
    session.add(
        model.TimedDB(pid="P01", execute_at=controller._site.mjd(), min_gap=0.0)
    )
    session.commit()
    return program


def test_stop_recovers_handed_but_unrun_programs(rob, chimera_session):
    """A stop wipes the chimera queue, but handed-over programs that never
    RAN must come back: the robobs side had already marked them finished,
    so nothing re-offered them and only a full replan rebuilt the night
    (2026-07-22, twice, 55 programs lost)."""
    _populate_timed_program(rob)
    rob.rob_state = RobState.ON
    assert rob._handle_scheduler_idle() == 0.0

    session = rob._session()
    handed = session.query(model.Program).one()
    assert handed.finished is True
    assert handed.chimera_id is not None
    assert (
        session.query(model.TimedDB)
        .filter(model.TimedDB.scheduled == True)  # noqa: E712
        .count()
        == 1
    )

    rob.stop()

    session = rob._session()
    recovered = session.query(model.Program).one()
    assert recovered.finished is False
    assert recovered.chimera_id is None
    # the timed occurrence the commit consumed is released too
    assert (
        session.query(model.TimedDB)
        .filter(model.TimedDB.scheduled == True)  # noqa: E712
        .count()
        == 0
    )
    assert chimera_session().query(chimera_model.Program).count() == 0

    # and the next start re-offers the same program
    rob.rob_state = RobState.ON
    assert rob._handle_scheduler_idle() == 0.0
    assert chimera_session().query(chimera_model.Program).count() == 1


def test_stop_keeps_completed_programs_finished(rob, chimera_session):
    """Only handed-but-UNRUN programs are recovered: one whose chimera row
    is finished already ran and must stay finished or it would repeat."""
    _populate_timed_program(rob)
    rob.rob_state = RobState.ON
    rob._handle_scheduler_idle()
    csession = chimera_session()
    cprogram = csession.query(chimera_model.Program).one()
    cprogram.finished = True
    csession.commit()

    rob.stop()

    session = rob._session()
    assert session.query(model.Program).one().finished is True


def _populate_skyflat_program(controller):
    """A sky-flat (algorithm 5) program whose block configures R x 9."""
    session = controller._session()
    target = model.Target(name="flat", target_ra=0.0, target_dec=0.0)
    session.add(target)
    session.commit()
    blockpar = model.BlockPar(bid=1, pid="CAL")
    blockpar.sched_algorithm = 5
    session.add(blockpar)
    session.commit()
    block = model.ObsBlock(
        target_id=target.id, blockid=1, pid="CAL", block_par_id=blockpar.id
    )
    block.actions.append(model.AutoFlat(filter="R", frames=9))
    session.add(block)
    session.commit()
    program = model.Program(
        target_id=target.id,
        name="skyflat",
        priority=0,
        slew_at=controller._site.mjd(),
        pid="CAL",
        obsblock_id=block.id,
        blockpar_id=blockpar.id,
    )
    session.add(program)
    session.commit()
    return program


def test_program_complete_ledgers_actual_flat_frames(rob, chimera_session):
    """The skyflat ledger must record what the controller actually TOOK
    (per its expose_complete events): the fallback filter walk switches
    filters mid-set, so the block's configured count is neither the
    filters nor the frames that ran."""
    _populate_skyflat_program(rob)
    rob._site._sun_alt = -10.0  # inside the coarse twilight gate
    rob.rob_state = RobState.ON
    assert rob._handle_scheduler_idle() == 0.0
    cprogram = chimera_session().query(chimera_model.Program).one()

    rob._watch_program_begin(cprogram.id)
    for i in range(3):
        rob._watch_flat_expose_complete("R", i + 1, 10.0, 25000.0)
    for i in range(2):
        rob._watch_flat_expose_complete("CLEAR", i + 4, 5.0, 24000.0)
    rob._watch_program_complete(cprogram.id, "OK")

    session = rob._session()
    ledger = {e.filter: e.frames for e in session.query(model.SkyFlatDB).all()}
    assert ledger == {"R": 3, "CLEAR": 2}

    # counts must not leak into the next program
    assert rob._flat_frames == {}


def test_program_complete_attributes_by_chimera_id(rob, chimera_session):
    """With several programs handed over at once, a completion must credit
    the robobs program it belongs to, not the last one handed. Live on
    2026-07-23 02:21: the first focus completion of the night marked a
    just-handed OPOP block observed instead of the focus."""
    session = rob._session()
    blockpar = model.BlockPar(bid=1, pid="P01")
    blockpar.sched_algorithm = 2
    session.add(blockpar)
    session.commit()
    now = rob._site.mjd()
    for i, ra in enumerate((10.0, 10.5)):
        target = model.Target(name=f"tgt{i}", target_ra=ra, target_dec=0.0)
        session.add(target)
        session.commit()
        block = model.ObsBlock(
            target_id=target.id, blockid=i + 1, pid="P01", block_par_id=blockpar.id
        )
        block.actions.append(model.Expose(frames=1, exptime=1.0))
        session.add(block)
        session.commit()
        session.add(
            model.Program(
                target_id=target.id,
                name=target.name,
                priority=1,
                slew_at=now,
                pid="P01",
                obsblock_id=block.id,
                blockpar_id=blockpar.id,
            )
        )
        session.add(model.TimedDB(pid="P01", execute_at=now + i * 0.0007, min_gap=0.0))
    session.commit()

    rob.rob_state = RobState.ON
    assert rob._handle_scheduler_idle() == 0.0
    assert rob._handle_scheduler_idle() == 0.0
    assert len(rob._handed) == 2

    csession = chimera_session()
    first = (
        csession.query(chimera_model.Program).order_by(chimera_model.Program.id).first()
    )
    rob._watch_program_complete(first.id, "OK")

    session = rob._session()
    occurrences = session.query(model.TimedDB).order_by(model.TimedDB.execute_at).all()
    # the FIRST program's occurrence is done; the still-queued one is not
    assert occurrences[0].finished is True
    assert occurrences[0].observed_at > 0
    assert occurrences[1].finished is False
    assert len(rob._handed) == 1


def test_queue_clean_clears_every_stale_link(rob, chimera_session):
    """sqlite reuses program ids once the queue table empties, so a link
    surviving a wipe can match a NEW queue's ids: the recovery then
    un-finishes a program that RAN (8 recovered vs 7 removed, 2026-07-23
    02:39 - a completed focus went back into the pool). After a clean, no
    robobs program may keep a chimera_id."""
    _populate_timed_program(rob)
    rob.rob_state = RobState.ON
    assert rob._handle_scheduler_idle() == 0.0
    csession = chimera_session()
    cprogram = csession.query(chimera_model.Program).one()
    rob._watch_program_complete(cprogram.id, "OK")
    # the chimera scheduler's own bookkeeping on success
    cprogram.finished = True
    csession.commit()

    session = rob._session()
    ran = session.query(model.Program).one()
    assert ran.finished is True
    assert ran.chimera_id is not None  # the link survives completion

    rob.stop()

    session = rob._session()
    ran = session.query(model.Program).one()
    assert ran.finished is True, "a program that ran was spuriously recovered"
    assert ran.chimera_id is None, "stale link survived the queue wipe"
