# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2014-present chimera-robobs authors

"""The RobObs chimera controller.

Watches the chimera scheduler; whenever the scheduler goes idle (state
IDLE -> OFF) it picks the next program from the robobs database (see
:mod:`chimera_robobs.scheduling`), converts it to a chimera scheduler program
and wakes the scheduler up again.
"""

import enum
import logging
import os
import threading
import time

from chimera.controllers.scheduler import model as chimera_model
from chimera.controllers.scheduler.states import State as SchedState
from chimera.controllers.scheduler.status import SchedulerStatus
from chimera.core.chimeraobject import ChimeraObject
from chimera.core.constants import SYSTEM_CONFIG_DIRECTORY
from chimera.util.coord import Coord
from chimera.util.position import Position

from chimera_robobs.scheduling import algorithms
from chimera_robobs.scheduling.algorithms import ALGORITHMS
from chimera_robobs.scheduling.dates import datetime_from_jd
from chimera_robobs.scheduling.engine import RobObsEngine
from chimera_robobs.scheduling.machine import Machine
from chimera_robobs.scheduling.model import ObservingLog, open_database
from chimera_robobs.scheduling.siteadapter import SiteAdapter


class RobState(enum.Enum):
    OFF = "OFF"
    ON = "ON"


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
        self._current_program_condition = threading.Condition()
        self._no_program_on_queue = False
        self._debuglog = None
        self.machine = None
        self.engine = None
        self._session = None
        self._site = None
        self._scheduler_list = []

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def __start__(self):
        self._scheduler_list = [
            s.strip() for s in str(self["schedulers"]).split(",") if s.strip()
        ]

        self._setup_debug_log()

        self._session = open_database(self["database"])
        algorithms.configure(self._session)

        self._site = SiteAdapter(self.get_proxy(self["site"]))
        self.engine = RobObsEngine(
            self._session,
            self._site,
            log=self._debuglog,
            seeing=self._get_seeing if self["seeingmonitors"] is not None else None,
        )

        # event subscription happens on the first control() tick: during
        # __start__ the bus is not serving yet, so proxies can't resolve
        self._events_connected = False

        self.machine = Machine(self)
        self.machine.start()

        self._inject_instrument()

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
        self._debuglog.debug("Shutting down machine...")
        self.machine.state(SchedState.SHUTDOWN)

    def _setup_debug_log(self):
        self._debuglog = logging.getLogger("_robobs_debug_")
        logfile = os.path.join(
            SYSTEM_CONFIG_DIRECTORY, f"robobs_{time.strftime('%Y%m%d')}.log"
        )
        log_handler = logging.FileHandler(logfile)
        log_handler.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s[%(levelname)s:%(threadName)s]-%(name)s-"
                "(%(filename)s:%(lineno)d):: %(message)s"
            )
        )
        self._debuglog.setLevel(logging.DEBUG)
        self._debuglog.addHandler(log_handler)
        self.log.setLevel(logging.INFO)

    # ------------------------------------------------------------------
    # public control API
    # ------------------------------------------------------------------

    def start(self) -> bool:
        self._debuglog.debug("Switching robstate on...")
        self.rob_state = RobState.ON
        return True

    def stop(self) -> bool:
        self._debuglog.debug("Switching robstate off...")
        self.rob_state = RobState.OFF
        return True

    def wake(self) -> bool:
        self._debuglog.debug("Waking machine up...")
        self.machine.state(SchedState.START)
        return True

    def state(self) -> str:
        """Return a short human-readable status (proxy/JSON friendly)."""
        machine_state = self.machine.state() if self.machine else None
        return f"robstate={self.rob_state.value} machine={machine_state}"

    def reset_scheduler(self):
        """Queue a bias frame on the chimera scheduler to reset it."""
        csession = chimera_model.Session()

        cprog = chimera_model.Program(name="RESET", pi="ROBOBS", priority=1)
        clean_program = chimera_model.Expose()
        clean_program.frames = 1
        clean_program.exptime = 0
        clean_program.image_type = "BIAS"
        clean_program.shutter = "CLOSE"
        clean_program.filename = "RESET-$DATE-$TIME"
        cprog.actions.append(clean_program)

        csession.add(cprog)
        # legacy bug: the session was never committed
        csession.commit()

    # ------------------------------------------------------------------
    # proxies
    # ------------------------------------------------------------------

    def get_site(self) -> SiteAdapter:
        return self._site

    def get_scheduler(self, index: int = 0):
        self.log.debug("%s", self._scheduler_list[index])
        if self._debuglog is not None:
            self._debuglog.debug("%s", self._scheduler_list[index])
        return self.get_proxy(self._scheduler_list[index])

    def get_logger(self):
        return self._debuglog

    def _get_seeing(self) -> float:
        """Current seeing from the first configured seeing monitor."""
        try:
            locations = [
                s.strip() for s in str(self["seeingmonitors"]).split(",") if s.strip()
            ]
            return float(self.get_proxy(locations[0]).seeing())
        except Exception:
            self._debuglog.exception("Could not get seeing measurement.")
            return -1.0

    def _inject_instrument(self):
        for algorithm in ALGORITHMS.values():
            try:
                algorithm.site = self._site
            except Exception as e:
                self.log.error("Could not inject site on %s handler", algorithm)
                self.log.exception(e)

    # ------------------------------------------------------------------
    # scheduler events
    # ------------------------------------------------------------------

    def _connect_scheduler_events(self):
        sched = self.get_scheduler()
        if not sched:
            self.log.warning("Couldn't find scheduler.")
            self._debuglog.warning("Couldn't find scheduler.")
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
            self._debuglog.warning("Couldn't find scheduler.")
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

    def _watch_program_begin(self, program_id):
        """chimera 0.2 scheduler events carry the program id."""
        csession = chimera_model.Session()
        rsession = self._session()
        try:
            program = self._get_chimera_program(csession, program_id)
            if program is None:
                self._debuglog.warning("Unknown program id %s started", program_id)
                return
            self._debuglog.debug("Program %s started", program)

            log_entry = ObservingLog(
                time=datetime_from_jd(self._site.mjd() + 2400000.5).replace(
                    tzinfo=None
                ),
                tid=program.tid,
                name=program.name,
                priority=program.priority,
                action="ROBOBS: Program Started",
            )
            rsession.add(log_entry)
        finally:
            csession.commit()
            rsession.commit()

    def _watch_program_complete(self, program_id, status, message=None):
        csession = chimera_model.Session()
        rsession = self._session()
        try:
            program = self._get_chimera_program(csession, program_id)
            self._debuglog.debug(
                "Program %s completed with status %s(%s)", program, status, message
            )

            if program is not None:
                log_entry = ObservingLog(
                    time=datetime_from_jd(self._site.mjd() + 2400000.5).replace(
                        tzinfo=None
                    ),
                    tid=program.tid,
                    name=program.name,
                    priority=program.priority,
                    action=f"ROBOBS: Program End with status {status}({message})",
                )
                rsession.add(log_entry)
                rsession.commit()

            if status == SchedulerStatus.OK and self._current_program is not None:
                cp = rsession.merge(self._current_program[0])
                cp.finished = True
                rsession.commit()

                block_config = rsession.merge(self._current_program[1])
                sched = ALGORITHMS[block_config.sched_algorithm]
                sched.observed(self._site.mjd(), self._current_program, self._site)
                rsession.commit()

                self._current_program = None
            elif status != SchedulerStatus.OK:
                self.stop()
        finally:
            csession.commit()
            rsession.commit()

    def _watch_action_begin(self, action_id, message):
        self._debuglog.debug("Action %s %s ...", action_id, message)

    def _watch_action_complete(self, action_id, status, message=None):
        if status == SchedulerStatus.OK:
            self._debuglog.debug("Action %s: %s", action_id, status)
        else:
            self._debuglog.debug("Action %s: %s (%s)", action_id, status, message)

    def _watch_state_changed(self, new_state, old_state):
        self._debuglog.debug("State changed %s -> %s...", old_state, new_state)
        if old_state != SchedState.IDLE or new_state != SchedState.OFF:
            return

        if self.rob_state != RobState.ON:
            self._debuglog.debug("Current state is off. Won't respond.")
            return

        self._debuglog.debug("Scheduler went from BUSY to OFF. Needs rescheduling...")

        session = self._session()
        csession = chimera_model.Session()

        program_info = self.reschedule()

        if program_info is not None:
            program = session.merge(program_info[0])
            obs_block = session.merge(program_info[2])
            self._debuglog.debug(
                "Adding program %s to scheduler and starting.", program
            )
            cprogram = program.chimera_program()
            for act in obs_block.actions:
                cprogram.actions.append(act.chimera_action())
            csession.add(cprogram)
            csession.commit()
            program.finished = True
            session.commit()
            self._current_program = program_info
            self._no_program_on_queue = False
            self._debuglog.debug("Done")
        elif self._no_program_on_queue:
            self._debuglog.warning("No program on robobs queue, waiting for 5 min.")
            time.sleep(300)
        else:
            self._debuglog.warning(
                "No program on robobs queue. Sending telescope to park position."
            )
            cprog = chimera_model.Program(name="SAFETY", pi="ROBOBS", priority=1)
            to_park_position = chimera_model.Point()
            to_park_position.target_alt_az = Position.from_alt_az(
                Coord.from_d(88.0), Coord.from_d(89.0)
            )
            cprog.actions.append(to_park_position)

            csession.add(cprog)
            self._no_program_on_queue = True

        csession.commit()
        session.commit()
        self.wake()
        self._debuglog.debug("Done")

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
