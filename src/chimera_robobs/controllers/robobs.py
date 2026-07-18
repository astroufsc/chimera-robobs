# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2014-present chimera-robobs authors

"""The RobObs chimera controller.

Watches the chimera scheduler; whenever the scheduler goes idle (state
IDLE -> OFF) it picks the next program from the robobs database (see
:mod:`chimera_robobs.scheduling`), converts it to a chimera scheduler program
and wakes the scheduler up again.

The scheduler-idle reaction runs on the controller's machine thread, never
on the bus dispatch pool: event watchers that issue further bus requests
inline can exhaust the pool and deadlock the bus (and the legacy handler
even slept for five minutes there when the queue was empty).
"""

import enum
import logging.handlers
import os
import threading

from chimera.controllers.scheduler import model as chimera_model
from chimera.controllers.scheduler.states import State as SchedState
from chimera.controllers.scheduler.status import SchedulerStatus
from chimera.core.chimeraobject import ChimeraObject
from chimera.core.constants import SYSTEM_CONFIG_DIRECTORY
from chimera.util.coord import Coord
from chimera.util.position import Position

from chimera_robobs.scheduling.algorithms import build_algorithms
from chimera_robobs.scheduling.dates import datetime_from_mjd
from chimera_robobs.scheduling.engine import RobObsEngine
from chimera_robobs.scheduling.model import ObservingLog, open_database
from chimera_robobs.scheduling.siteadapter import SiteAdapter

#: how long to wait before retrying when the robobs queue is empty (seconds)
EMPTY_QUEUE_RETRY = 300.0

#: alt/az the telescope is sent to when there is nothing to observe
PARK_POSITION_ALT_AZ = (88.0, 89.0)


class RobState(enum.Enum):
    OFF = "OFF"
    ON = "ON"


class MachineState(enum.Enum):
    OFF = "OFF"
    START = "START"
    BUSY = "BUSY"
    SHUTDOWN = "SHUTDOWN"


class Machine(threading.Thread):
    """Thread driving the RobObs reactions.

    Scheduler events only record work and wake this thread; the actual
    rescheduling (which talks to the database and the chimera scheduler)
    happens here, off the bus dispatch pool.
    """

    def __init__(self, controller):
        threading.Thread.__init__(self, name="robobs-machine")
        self.controller = controller
        self.daemon = False

        self.__state = MachineState.OFF
        self.__reschedule_pending = False
        self.__wake_up_call = threading.Condition()

    def state(self, state: MachineState | None = None):
        log = self.controller.log
        with self.__wake_up_call:
            if state is None:
                return self.__state
            if state == self.__state:
                return None
            log.debug("Changing state, from %s to %s.", self.__state, state)
            self.__state = state
            self.__wake_up_call.notify_all()
            return None

    def request_reschedule(self):
        """Record that the chimera scheduler went idle and wake the thread."""
        with self.__wake_up_call:
            self.__reschedule_pending = True
            self.__wake_up_call.notify_all()

    def run(self):
        log = self.controller.log
        log.info("Starting robobs machine")
        sched = self.controller.get_scheduler()

        while self.state() != MachineState.SHUTDOWN:
            if self.state() == MachineState.START:
                log.debug("[start] waking scheduler...")
                sched.start()
                self.state(MachineState.BUSY)

            elif self._take_reschedule_request():
                # OFF or BUSY with pending work (the legacy event handler
                # reacted regardless of the machine state)
                delay = self.controller._handle_scheduler_idle()
                if delay is None:
                    # robobs is off (or shutting down): stay put
                    continue
                if delay > 0:
                    log.debug("retrying in %.0f s...", delay)
                    self._sleep(timeout=delay)
                    if self.state() in (MachineState.OFF, MachineState.BUSY):
                        self.request_reschedule()
                    continue
                self.state(MachineState.START)

            else:
                log.debug("[%s] waiting for something to happen..", self.state().value)
                self._sleep()

        log.debug("[shutdown] thread ending...")

    def _take_reschedule_request(self) -> bool:
        with self.__wake_up_call:
            pending = self.__reschedule_pending
            self.__reschedule_pending = False
            return pending

    def _sleep(self, timeout: float | None = None):
        with self.__wake_up_call:
            if self.__reschedule_pending and timeout is None:
                return  # work arrived before we went to sleep
            self.controller.log.debug("Sleeping")
            self.__wake_up_call.wait(timeout)


class RobObs(ChimeraObject):
    __config__ = {
        "site": "/Site/0",
        "schedulers": "/Scheduler/0",
        "weatherstations": None,
        "seeingmonitors": None,
        "cloudsensors": None,
        # robobs scheduling database (default: ~/.chimera/robobs.db)
        "database": None,
    }

    def __init__(self):
        ChimeraObject.__init__(self)
        self.rob_state = RobState.OFF
        self._current_program = None
        self._no_program_on_queue = False
        self.machine = None
        self.engine = None
        self._session = None
        self._site = None
        self._algorithms = {}
        self._scheduler_list = []
        self._events_connected = False

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def __start__(self):
        self._scheduler_list = [
            s.strip() for s in str(self["schedulers"]).split(",") if s.strip()
        ]

        self._setup_logger()

        self._session = open_database(self["database"])

        self._site = SiteAdapter(self.get_proxy(self["site"]))
        self._algorithms = build_algorithms(self._session, self._site)
        self.engine = RobObsEngine(
            self._session,
            self._site,
            log=self.log,
            seeing=self._get_seeing if self["seeingmonitors"] is not None else None,
            algorithms=self._algorithms,
        )

        # event subscription happens on the first control() tick: during
        # __start__ the bus is not serving yet, so proxies can't resolve
        self._events_connected = False

        self.machine = Machine(self)
        self.machine.start()

    def control(self):
        """One-shot: subscribe to scheduler events once the bus is up, then
        stop the control loop (everything else is event-driven)."""
        if self._events_connected:
            return False
        try:
            self._connect_scheduler_events()
            self._events_connected = True
            return False
        except Exception as e:
            self.log.warning("could not subscribe to scheduler events yet: %s", e)
            return True  # retry on the next tick

    def __stop__(self):
        self._disconnect_scheduler_events()
        self.log.debug("Shutting down machine...")
        self.machine.state(MachineState.SHUTDOWN)
        self.machine.join(timeout=5.0)

    def _setup_logger(self):
        handler = logging.handlers.RotatingFileHandler(
            os.path.join(SYSTEM_CONFIG_DIRECTORY, "robobs.log"),
            maxBytes=50 * 1024 * 1024,
            backupCount=10,
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s %(threadName)s] %(message)s")
        )
        handler.setLevel(logging.DEBUG)
        self.log.setLevel(logging.DEBUG)
        self.log.addHandler(handler)

    # ------------------------------------------------------------------
    # public control API
    # ------------------------------------------------------------------

    def start(self) -> bool:
        self.log.debug("Switching robstate on...")
        self.rob_state = RobState.ON
        return True

    def stop(self) -> bool:
        self.log.debug("Switching robstate off...")
        self.rob_state = RobState.OFF
        return True

    def wake(self) -> bool:
        self.log.debug("Waking machine up...")
        self.machine.state(MachineState.START)
        return True

    def state(self) -> str:
        """Return a short human-readable status (proxy/JSON friendly)."""
        machine_state = self.machine.state().value if self.machine else None
        return f"robstate={self.rob_state.value} machine={machine_state}"

    # ------------------------------------------------------------------
    # proxies
    # ------------------------------------------------------------------

    def get_scheduler(self):
        """Proxy to the (first) configured chimera scheduler."""
        return self.get_proxy(self._scheduler_list[0])

    def _get_seeing(self) -> float:
        """Current seeing from the first configured seeing monitor."""
        try:
            locations = [
                s.strip() for s in str(self["seeingmonitors"]).split(",") if s.strip()
            ]
            return float(self.get_proxy(locations[0]).seeing())
        except Exception:
            self.log.exception("Could not get seeing measurement.")
            return -1.0

    # ------------------------------------------------------------------
    # scheduler events
    # ------------------------------------------------------------------

    def _connect_scheduler_events(self):
        sched = self.get_scheduler()
        if not sched:
            self.log.warning("Couldn't find scheduler.")
            return False

        me = self.get_proxy()
        sched.program_begin += me._watch_program_begin
        sched.program_complete += me._watch_program_complete
        sched.action_begin += me._watch_action_begin
        sched.action_complete += me._watch_action_complete
        sched.state_changed += me._watch_state_changed

    def _disconnect_scheduler_events(self):
        sched = self.get_scheduler()
        if not sched:
            self.log.warning("Couldn't find scheduler.")
            return False

        me = self.get_proxy()
        sched.program_begin -= me._watch_program_begin
        sched.program_complete -= me._watch_program_complete
        sched.action_begin -= me._watch_action_begin
        sched.action_complete -= me._watch_action_complete
        sched.state_changed -= me._watch_state_changed

    def _get_chimera_program(self, csession, program_id):
        return (
            csession.query(chimera_model.Program)
            .filter(chimera_model.Program.id == program_id)
            .first()
        )

    def _add_observing_log(self, rsession, program, action: str):
        rsession.add(
            ObservingLog(
                time=datetime_from_mjd(self._site.mjd()).replace(tzinfo=None),
                target_id=program.tid,
                name=program.name,
                priority=program.priority,
                action=action,
            )
        )

    def _watch_program_begin(self, program_id):
        """chimera 0.2 scheduler events carry the program id."""
        csession = chimera_model.Session()
        rsession = self._session()
        try:
            program = self._get_chimera_program(csession, program_id)
            if program is None:
                self.log.warning("Unknown program id %s started", program_id)
                return
            self.log.debug("Program %s started", program)
            self._add_observing_log(rsession, program, "ROBOBS: Program Started")
        finally:
            csession.commit()
            rsession.commit()

    def _watch_program_complete(self, program_id, status, message=None):
        csession = chimera_model.Session()
        rsession = self._session()
        try:
            program = self._get_chimera_program(csession, program_id)
            self.log.debug(
                "Program %s completed with status %s(%s)", program, status, message
            )

            if program is not None:
                self._add_observing_log(
                    rsession,
                    program,
                    f"ROBOBS: Program End with status {status}({message})",
                )
                rsession.commit()

            if status == SchedulerStatus.OK and self._current_program is not None:
                cp = rsession.merge(self._current_program[0])
                cp.finished = True
                rsession.commit()

                block_config = rsession.merge(self._current_program[1])
                sched = self._algorithms[block_config.sched_algorithm]
                sched.observed(self._site.mjd(), self._current_program)
                rsession.commit()

                self._current_program = None
            elif status != SchedulerStatus.OK:
                self.stop()
        finally:
            csession.commit()
            rsession.commit()

    def _watch_action_begin(self, action_id, message):
        self.log.debug("Action %s %s ...", action_id, message)

    def _watch_action_complete(self, action_id, status, message=None):
        if status == SchedulerStatus.OK:
            self.log.debug("Action %s: %s", action_id, status)
        else:
            self.log.debug("Action %s: %s (%s)", action_id, status, message)

    def _watch_state_changed(self, new_state, old_state):
        """Runs on the bus dispatch pool: only record the work and wake the
        machine thread (see the module docstring)."""
        self.log.debug("State changed %s -> %s...", old_state, new_state)
        if old_state != SchedState.IDLE or new_state != SchedState.OFF:
            return

        if self.rob_state != RobState.ON:
            self.log.debug("Current state is off. Won't respond.")
            return

        self.log.debug("Scheduler went idle. Requesting rescheduling...")
        self.machine.request_reschedule()

    # ------------------------------------------------------------------
    # scheduler-idle reaction (runs on the machine thread)
    # ------------------------------------------------------------------

    def _handle_scheduler_idle(self) -> float | None:
        """Pick the next program and queue it on the chimera scheduler.

        Returns the number of seconds the machine should wait before
        retrying (0 to start the scheduler right away), or ``None`` when
        robobs is off and nothing should happen.
        """
        if self.rob_state != RobState.ON:
            self.log.debug("Current state is off. Won't respond.")
            return None

        session = self._session()
        csession = chimera_model.Session()

        program_info = self.engine.reschedule()

        if program_info is not None:
            program = session.merge(program_info[0])
            obs_block = session.merge(program_info[2])
            self.log.debug("Adding program %s to scheduler and starting.", program)
            cprogram = program.chimera_program()
            for act in obs_block.actions:
                cprogram.actions.append(act.chimera_action())
            csession.add(cprogram)
            csession.commit()
            program.finished = True
            session.commit()
            self._current_program = program_info
            self._no_program_on_queue = False
            self.log.debug("Done")
        elif self._no_program_on_queue:
            self.log.warning(
                "No program on robobs queue, retrying in %.0f s.", EMPTY_QUEUE_RETRY
            )
            csession.commit()
            session.commit()
            return EMPTY_QUEUE_RETRY
        else:
            self.log.warning(
                "No program on robobs queue. Sending telescope to park position."
            )
            cprog = chimera_model.Program(name="SAFETY", pi="ROBOBS", priority=1)
            to_park_position = chimera_model.Point()
            to_park_position.target_alt_az = Position.from_alt_az(
                Coord.from_d(PARK_POSITION_ALT_AZ[0]),
                Coord.from_d(PARK_POSITION_ALT_AZ[1]),
            )
            cprog.actions.append(to_park_position)

            csession.add(cprog)
            self._no_program_on_queue = True

        csession.commit()
        session.commit()
        self.log.debug("Done")
        return 0.0

    # ------------------------------------------------------------------
    # scheduling (delegated to the engine)
    # ------------------------------------------------------------------

    def reschedule(self, now: float | None = None):
        return self.engine.reschedule(now)

    def get_program(self, nowmjd: float, priority: int):
        return self.engine.get_program(nowmjd, priority)

    def get_priority_list(self) -> list[int]:
        return self.engine.get_priority_list()

    def check_conditions(self, program, time, program_length=0.0) -> bool:
        return self.engine.check_conditions(program, time, program_length)
