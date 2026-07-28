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
from sqlalchemy import or_

from chimera_robobs.scheduling.algorithms import build_algorithms
from chimera_robobs.scheduling.dates import datetime_from_mjd
from chimera_robobs.scheduling.engine import RobObsEngine
from chimera_robobs.scheduling.model import (
    BlockPar,
    ObservingLog,
    Program,
    open_database,
)
from chimera_robobs.scheduling.siteadapter import SiteAdapter

#: how long to wait before retrying when the robobs queue is empty (seconds)
EMPTY_QUEUE_RETRY = 300.0

#: seconds of slack allowed when deciding an unpinned program is "due":
#: shorter than a slew, so waiting it out costs nothing
UNPINNED_DUE_TOLERANCE = 30.0

SECONDS_PER_DAY = 86400.0


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
                if self.controller.rob_state != RobState.ON:
                    # never start the scheduler with robobs off: it would
                    # replay stale queued programs
                    log.debug("[start] robobs is off, not waking the scheduler")
                    self.state(MachineState.OFF)
                    continue
                log.debug("[start] waking scheduler...")
                sched.start()
                self.state(MachineState.BUSY)
                # an empty freshly-started scheduler never emits the idle
                # event this machine waits for: force one planning pass
                self.request_reschedule()

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
        # unused: stopping tracking at program end moved to the Scheduler
        # (stop_tracking_on_program_end). Kept so deployed configs that still
        # set it keep loading - chimera raises on an unknown option.
        "telescope": "/Telescope/0",
        "weatherstations": None,
        "seeingmonitors": None,
        "cloudsensors": None,
        # robobs scheduling database (default: ~/.chimera/robobs.db)
        "database": None,
        # sky-flat controller location; when set, robobs counts its
        # expose_complete events so the ledger records the frames actually
        # taken (the fallback filter walk means the configured block is not
        # what ran)
        "autoflat": None,
        # wipe the chimera scheduler queue when robobs is switched on, so a
        # stale queue from a previous run is not re-executed
        "clean_scheduler_on_start": True,
        # how many CONSECUTIVE program errors robobs tolerates before it
        # stops itself. A single failure (a focus that could not fit, a bad
        # frame) skips one program and continues; a run of them means the
        # hardware or sky is unusable and the night should hold.
        "max_consecutive_errors": 3,
    }

    def __init__(self):
        ChimeraObject.__init__(self)
        self.rob_state = RobState.OFF
        # chimera program id -> handed-over program_info 4-tuple; completion
        # events resolve their robobs program here (in-memory, so the offer
        # stamps like _timed_request_id survive to observed())
        self._handed = {}
        # chimera program ids whose program_begin was seen: a completion for
        # a program that never began belongs to something else (a wiped
        # queue reusing rowids) or to a program the scheduler dropped
        # without running it - either way it must not credit an observation
        self._begun = set()
        self._no_program_on_queue = False
        self.machine = None
        self.engine = None
        self._session = None
        self._site = None
        self._algorithms = {}
        self._scheduler_list = []
        self._events_connected = False
        # per-filter frames taken by the running program (autoflat events)
        self._flat_frames = {}
        # consecutive program-error count; a run of them stops robobs
        self._consecutive_errors = 0

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

        # Our modules log under "chimera_robobs.*", a different tree from
        # chimera's "chimera.*" (whose handlers live on the "chimera"
        # logger). Nothing bridged them, so every decision the scheduling
        # algorithms made was invisible in BOTH chimera.log and robobs.log -
        # notably why a timed occurrence was expired instead of observed,
        # which had to be reconstructed from the database. Re-parent the
        # package logger so those records reach the same handlers.
        package_log = logging.getLogger("chimera_robobs")
        package_log.parent = self.log
        package_log.propagate = True
        package_log.setLevel(logging.DEBUG)

    # ------------------------------------------------------------------
    # public control API
    # ------------------------------------------------------------------

    def start(self) -> bool:
        self.log.debug("Switching robstate on...")
        if self.rob_state is RobState.ON:
            # Idempotent: starting an already-running robobs must NOT wipe
            # the scheduler queue. The queue holds the program the executor
            # is running right now, and deleting its rows underneath the
            # scheduler-program thread kills it mid-action - SQLAlchemy
            # raises ObjectDeletedError as soon as the executor touches the
            # action it is about to log (opd-40 2026-07-26 03:09 and 03:12:
            # two starts landed on a focus program that had already begun,
            # and both times the thread died). A deliberate reset is still
            # stop() then start().
            self.log.info(
                "robobs is already on; leaving the scheduler queue untouched."
            )
            return True
        if self["clean_scheduler_on_start"]:
            self._clean_scheduler_queue()
        self.rob_state = RobState.ON
        return True

    def _running_program_id(self):
        """Id of the chimera program the scheduler is executing right now.

        ``None`` when nothing is running, or when the scheduler cannot say
        (an older core without ``current_program()``, an unreachable bus) -
        the caller then behaves as it always did.
        """
        try:
            current = self.get_scheduler().current_program()
        except Exception as e:
            self.log.warning(
                "Could not ask the scheduler which program is running (%s); "
                "cleaning the whole queue.",
                e,
            )
            return None
        if not current:
            return None
        # JSON-safe snapshot ({id, name, pi, priority}) on current cores
        if isinstance(current, dict):
            return current.get("id")
        return getattr(current, "id", None)

    def _clean_scheduler_queue(self):
        """Delete every queued chimera scheduler program (recovered
        mysql-branch behavior, e91e84f): a stale queue left by a previous
        run would otherwise be re-executed.

        The program the scheduler is EXECUTING is left alone. Deleting it
        pulls the row out from under the scheduler-program thread, which
        then dies on sqlalchemy's ObjectDeletedError as soon as it logs the
        program it just aborted - every operator lock during a program did
        this (opd-40 2026-07-28; the abort landed 6 min after the delete).
        And once the queue empties, sqlite hands the freed rowid to the
        next program handed over, so the running program's completion is
        applied to whichever program inherited its id (2026-07-26: eta Car
        recorded as observed out of a bias run's completion).

        Removal must also be RECOVERABLE: a stop used to permanently lose
        every handed-over program that had not run yet - the robobs side
        had already marked them finished, so nothing re-offered them and
        only a full replan rebuilt the night (2026-07-22, twice, 55
        programs). Handed-but-unrun programs are un-finished here, and
        their timed occurrences un-committed, so the next start re-offers
        them.
        """
        csession = chimera_model.Session()
        rsession = self._session()
        running_id = self._running_program_id()
        try:
            programs = csession.query(chimera_model.Program).all()
            kept_id = None
            pending_ids = set()
            ran_ids = set()
            removed = 0
            for program in programs:
                if program.id == running_id and not program.finished:
                    kept_id = program.id
                    self.log.info(
                        "Keeping program #%s: the scheduler is executing it.",
                        program.id,
                    )
                    continue
                if program.finished:
                    ran_ids.add(program.id)
                else:
                    pending_ids.add(program.id)
                csession.delete(program)
                removed += 1
            if removed:
                self.log.info(
                    "Removed %i stale program(s) from the scheduler queue.",
                    removed,
                )

            # the wiped queue invalidates every in-memory hand-over record,
            # except the one still executing
            for program_id in list(self._handed):
                if program_id != kept_id:
                    self._forget_handover(program_id)

            # Handed-but-unrun programs, recognised by either marker: a
            # `chimera_id` that was still queued, or `handed_at` set with no
            # completion ever applied. The second is what brings back a
            # program whose chimera row had already vanished - a
            # scheduler.db wipe or a queue rebuild used to leave it
            # finished=1, chimera_id=NULL and unobservable forever
            # (2026-07-26: BRUCH's 3.4 h block and four focus runs, an
            # empty night that only a full replan fixed).
            recovered = 0
            rows = (
                rsession.query(Program, BlockPar)
                .join(BlockPar, Program.blockpar_id == BlockPar.id)
                .filter(
                    Program.finished == True,  # noqa: E712
                    or_(
                        Program.chimera_id.in_(pending_ids),
                        Program.handed_at != None,  # noqa: E711
                    ),
                )
                .all()
            )
            for rprogram, blockpar in rows:
                if kept_id is not None and rprogram.chimera_id == kept_id:
                    continue  # still executing: its own completion decides
                if rprogram.chimera_id in ran_ids:
                    # it RAN (the scheduler marks its row finished on
                    # success): re-offering it would observe it twice
                    rprogram.handed_at = None
                    continue
                self.log.info("Recovering handed-but-unrun program %s.", rprogram)
                self._recover_program(rprogram, blockpar.sched_algorithm)
                recovered += 1
            if recovered:
                self.log.info(
                    "Recovered %i handed-but-unrun program(s) for re-offer.",
                    recovered,
                )

            # Every other link is dead once the queue is wiped - and it MUST
            # be cleared, not just ignored: sqlite reuses program ids once
            # the table empties, so a stale link from a previous queue
            # generation matches a new queue's ids and the recovery
            # un-finishes a program that RAN (8 recovered vs 7 removed,
            # 2026-07-23 02:39).
            for lingering in (
                rsession.query(Program)
                .filter(Program.chimera_id != None)  # noqa: E711
                .all()
            ):
                if lingering.chimera_id != kept_id:
                    lingering.chimera_id = None
        finally:
            rsession.commit()
            csession.commit()

    def _recover_program(self, rprogram, sched_algorithm):
        """Put a handed-but-unrun program back on offer."""
        rprogram.finished = False
        rprogram.chimera_id = None
        rprogram.handed_at = None
        algorithm = self._algorithms.get(sched_algorithm)
        if algorithm is not None:
            algorithm.uncommitted(rprogram)

    def _forget_handover(self, program_id):
        """Drop the in-memory record of a handed-over chimera program."""
        self._handed.pop(program_id, None)
        self._begun.discard(program_id)

    def stop(self) -> bool:
        self.log.debug("Switching robstate off...")
        self.rob_state = RobState.OFF
        # pending programs must not survive the stop, or a later scheduler
        # start resurrects them
        if self["clean_scheduler_on_start"]:
            self._clean_scheduler_queue()
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

    def _get_autoflat(self):
        """Proxy to the sky-flat controller, or None when not configured."""
        if self["autoflat"] is None:
            return None
        try:
            return self.get_proxy(self["autoflat"])
        except Exception:
            self.log.exception("Could not resolve autoflat %s.", self["autoflat"])
            return None

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

        autoflat = self._get_autoflat()
        if autoflat:
            autoflat.expose_complete += me._watch_flat_expose_complete

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

        autoflat = self._get_autoflat()
        if autoflat:
            autoflat.expose_complete -= me._watch_flat_expose_complete

    def _get_chimera_program(self, csession, program_id):
        return (
            csession.query(chimera_model.Program)
            .filter(chimera_model.Program.id == program_id)
            .first()
        )

    def _add_observing_log(self, rsession, program, action: str):
        """Log an entry for a CHIMERA scheduler program (``tid`` naming)."""
        self._log_observation(
            rsession, program.tid, program.name, program.priority, action
        )

    def _log_observation(self, rsession, target_id, name, priority, action: str):
        rsession.add(
            ObservingLog(
                time=datetime_from_mjd(self._site.mjd()).replace(tzinfo=None),
                target_id=target_id,
                name=name,
                priority=priority,
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
            # only a program that actually STARTED may later be credited
            # with an observation (see _watch_program_complete)
            if program_id in self._handed:
                self._begun.add(program_id)
            # frames counted from here on belong to this program
            self._flat_frames = {}
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

            # The program_info this chimera program was handed over as.
            # Attribution must be BY ID: the scheduler queue holds many
            # handed programs at once, and crediting "the last one handed"
            # marked the wrong robobs program observed (2026-07-23: an OPOP
            # block eaten by the first focus completion of the night).
            info = self._handed.get(program_id)

            if (
                status == SchedulerStatus.OK
                and info is not None
                and program_id not in self._begun
            ):
                # A SUCCESS for a program that never emitted program_begin is
                # not this program's success. It happens when the queue is
                # rewritten under a running program and sqlite hands its
                # rowid to the next handover (2026-07-26: a hand-loaded bias
                # run's completion marked eta Car observed without the
                # telescope ever pointing at it), and on the scheduler's own
                # no-run path ("Program not valid anymore"), where crediting
                # an observation would be just as wrong. Failures need no
                # such guard - they credit nothing either way.
                self._handle_completion_without_begin(
                    rsession, program_id, program, info, status, message
                )
                return

            if program is not None:
                self._add_observing_log(
                    rsession,
                    program,
                    f"ROBOBS: Program End with status {status}({message})",
                )
                rsession.commit()
            elif info is not None:
                # on success the scheduler deletes its row before this event
                # arrives: log the End from the robobs program instead
                robobs_program = rsession.merge(info[0])
                self._log_observation(
                    rsession,
                    robobs_program.target_id,
                    robobs_program.name,
                    robobs_program.priority,
                    f"ROBOBS: Program End with status {status}({message})",
                )
                rsession.commit()

            if status == SchedulerStatus.OK and info is not None:
                self._consecutive_errors = 0
                cp = rsession.merge(info[0])
                cp.finished = True
                # the handover is settled: this program ran
                cp.handed_at = None
                info[0].handed_at = None
                rsession.commit()

                if self._flat_frames:
                    # what the sky-flat controller ACTUALLY took (its
                    # fallback filter walk means the configured block is not
                    # what ran); SkyFlat.observed prefers this over the
                    # block's configured actions
                    info[0]._skyflat_frames_taken = dict(self._flat_frames)
                    self._flat_frames = {}

                block_config = rsession.merge(info[1])
                sched = self._algorithms[block_config.sched_algorithm]
                sched.observed(self._site.mjd(), info)
                rsession.commit()

                self._forget_handover(program_id)
            elif status == SchedulerStatus.ABORTED:
                # An abort is an operator decision (a stop, a lock, the
                # weather), not a program failure: it must not count towards
                # max_consecutive_errors. The program did not run, so its
                # handover stays open and the next queue clean re-offers it -
                # aborted programs used to stay finished=True with a dead
                # chimera link and were simply lost.
                self.log.info(
                    "Program %s was aborted (%s); leaving it for re-offer.",
                    program_id,
                    message,
                )
                self._forget_handover(program_id)
            elif status != SchedulerStatus.OK:
                # A single program failure must cost ONE program, not the
                # night. The old self.stop() here turned every failed focus
                # into a stop -> clean -> recover -> re-hand churn and, with
                # the whole timed backlog re-offered, several catch-up runs
                # once it recovered (2026-07-26). The failed program is
                # already finished on the robobs side and its occurrence
                # stays committed, so it is not retried; just drop the
                # in-memory link and let the machine pick the next program.
                # Only a RUN of failures (camera/dome down) stops robobs.
                if info is not None:
                    failed = rsession.merge(info[0])
                    # the attempt is spent: keep it finished so the next
                    # queue clean does not re-offer a program that failed
                    failed.handed_at = None
                    info[0].handed_at = None
                    rsession.commit()
                self._forget_handover(program_id)
                self._consecutive_errors += 1
                limit = int(self["max_consecutive_errors"])
                if self._consecutive_errors >= limit:
                    self.log.error(
                        "%i consecutive program errors; stopping robobs "
                        "(something is systemically wrong).",
                        self._consecutive_errors,
                    )
                    self.stop()
                else:
                    self.log.warning(
                        "Program %s failed (%i/%i consecutive); skipping it "
                        "and continuing with the next program.",
                        program_id,
                        self._consecutive_errors,
                        limit,
                    )
        finally:
            csession.commit()
            rsession.commit()

    def _handle_completion_without_begin(
        self, rsession, program_id, program, info, status, message
    ):
        """Deal with a completion event for a handed program that never ran.

        Two shapes, told apart by the chimera row: still queued and unrun
        means the event belonged to something else (a rowid reused after the
        queue was rewritten) and this program is still waiting its turn;
        anything else means the scheduler dropped it without executing it,
        so the handover is released and the program goes back on offer.
        """
        if program is not None and not program.finished:
            self.log.warning(
                "Ignoring a completion for program #%s (%s): it never "
                "started and is still queued - the event belongs to a "
                "program that held this id before.",
                program_id,
                status,
            )
            return

        self.log.warning(
            "Program #%s completed (%s: %s) without ever starting; "
            "recovering %s for re-offer.",
            program_id,
            status,
            message,
            info[0],
        )
        rprogram = rsession.merge(info[0])
        self._log_observation(
            rsession,
            rprogram.target_id,
            rprogram.name,
            rprogram.priority,
            f"ROBOBS: Program never started ({status}); recovered for re-offer",
        )
        self._recover_program(rprogram, info[1].sched_algorithm)
        info[0].finished = False
        info[0].chimera_id = None
        info[0].handed_at = None
        rsession.commit()
        self._forget_handover(program_id)

    def _watch_flat_expose_complete(self, filter_id, i_flat, exp_time, sky_level):
        """Count each sky-flat frame per filter as the controller takes it."""
        self._flat_frames[filter_id] = self._flat_frames.get(filter_id, 0) + 1
        self.log.debug(
            "Sky flat %s on %s (%.1f s, %.0f counts): %i so far",
            i_flat,
            filter_id,
            exp_time,
            sky_level,
            self._flat_frames[filter_id],
        )

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
            algorithm = self._algorithms[program_info[1].sched_algorithm]

            # An UNPINNED program carries no start_at, so the chimera
            # scheduler would run it the moment it is queued. Handing one
            # over early would therefore start a block whose conditions the
            # engine only validated for its (later) slot time - the target
            # can still be below the airmass limit now. Wait for it instead:
            # the machine wakes up when it is due and hands it over then.
            if not algorithm.pin_start_time:
                due_in = (program.slew_at - self._site.mjd()) * SECONDS_PER_DAY
                if due_in > UNPINNED_DUE_TOLERANCE:
                    self.log.debug(
                        "%s is not due for %.1f s and its start time is not "
                        "pinned; waiting instead of queueing it early.",
                        program,
                        due_in,
                    )
                    csession.commit()
                    session.commit()
                    return min(due_in, EMPTY_QUEUE_RETRY)

            self.log.debug("Adding program %s to scheduler and starting.", program)
            cprogram = program.chimera_program(pin_start_time=algorithm.pin_start_time)
            for act in obs_block.actions:
                cprogram.actions.append(act.chimera_action())
            csession.add(cprogram)
            csession.commit()
            handed_at = self._site.mjd()
            program.finished = True
            # remember which chimera program this became: it is what lets a
            # later stop un-finish exactly the programs that never ran
            program.chimera_id = cprogram.id
            # ... and that it was only HANDED, not observed: the link alone
            # dies with the chimera row, this outlives it
            program.handed_at = handed_at
            session.commit()
            # keep the in-memory row in sync: the completion handler merges
            # it back, and merging the stale state CLOBBERED the link (an
            # OPOP program lost its chimera_id that way, 2026-07-23)
            program_info[0].finished = True
            program_info[0].chimera_id = cprogram.id
            program_info[0].handed_at = handed_at
            # tell the algorithm its offer was taken: consumption must not
            # happen in next() (the engine polls every queue while choosing)
            self._algorithms[program_info[1].sched_algorithm].committed(program_info)
            self._handed[cprogram.id] = program_info
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
            # An empty queue is not a reason to move the telescope. This used
            # to enqueue a SAFETY program that pointed at a fixed alt/az park
            # position, which meant a routine gap between programs dragged the
            # mount away from the sky and, on any night where nothing was
            # currently observable, did it once per retry. Parking belongs to
            # the supervisor's end-of-night items (park_telescope_after_flats,
            # lock_dome_on_sunrise), which know whether the night is over.
            self.log.warning("No program on robobs queue.")
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
