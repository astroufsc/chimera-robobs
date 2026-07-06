# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2014-present chimera-robobs authors

"""The small state machine thread driving the RobObs controller."""

import threading

from chimera.controllers.scheduler.states import State


class Machine(threading.Thread):
    def __init__(self, controller):
        threading.Thread.__init__(self)

        self.controller = controller
        self.current_program = None

        self.__state = None
        self.__state_lock = threading.Lock()
        self.__wake_up_call = threading.Condition()

        self.daemon = False

    def state(self, state=None):
        log = self.controller.get_logger()
        with self.__state_lock:
            if not state:
                return self.__state
            if state == self.__state:
                return
            log.debug("Changing state, from %s to %s.", self.__state, state)
            self.__state = state
            self.wakeup()

    def run(self):
        log = self.controller.get_logger()
        log.info("Starting robobs machine")
        sched = self.controller.get_scheduler()

        self.state(State.OFF)

        while self.state() != State.SHUTDOWN:
            if self.state() == State.OFF:
                log.debug("[off] will just sleep..")
                self.sleep()

            elif self.state() == State.START:
                log.debug("[start] waking scheduler...")
                sched.start()
                self.state(State.BUSY)

            elif self.state() == State.BUSY:
                log.debug("[busy] waiting for something to happen..")
                self.sleep()

        log.debug("[shutdown] thread ending...")

    def sleep(self):
        log = self.controller.get_logger()
        with self.__wake_up_call:
            log.debug("Sleeping")
            self.__wake_up_call.wait()

    def wakeup(self):
        log = self.controller.get_logger()
        with self.__wake_up_call:
            log.debug("Waking up")
            self.__wake_up_call.notify_all()
