# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2014-present chimera-robobs authors

"""Compressed-night integration run for robobs.

Boots a REAL chimera stack (bus + manager + fake instruments + scheduler +
robobs) in a single process, with a Site whose clock can be moved, and runs
a whole "night" in about a minute.  This exercises the integration surface
the unit-test fakes cannot reach - exactly where the 2026-07 production
bugs lived:

1. cold start: ``robobs start`` with a CLEAN scheduler queue must plan and
   execute the first program (the machine used to sleep forever waiting
   for a scheduler-idle event that an empty scheduler never emits);
2. every successful program must leave BOTH a "Program Started" and a
   "Program End with status OK" observing-log entry (the OK End entries
   were silently dropped, blanking the observed plots);
3. after ``robobs stop``, nothing new may execute (daytime-zombie guard).

Meant to be launched by ``test_fast_night.py`` in a subprocess (isolated
``HOME``); can also be run by hand:  ``python tests/integration/fast_night.py``.
"""

import datetime as dt
import os
import random
import tempfile
import threading
import time

# ~/.chimera must land in a scratch HOME *before* any chimera import:
# the scheduler database path binds at import time.
_HOME = tempfile.mkdtemp(prefix="fastnight-home-")
os.environ["HOME"] = _HOME
os.makedirs(os.path.join(_HOME, ".chimera"), exist_ok=True)

from chimera.controllers.imageserver.imageserver import ImageServer  # noqa: E402
from chimera.controllers.scheduler.controller import Scheduler  # noqa: E402
from chimera.core.bus import Bus  # noqa: E402
from chimera.core.manager import Manager  # noqa: E402
from chimera.core.proxy import Proxy  # noqa: E402
from chimera.core.site import Site  # noqa: E402
from chimera.instruments.fakecamera import FakeCamera  # noqa: E402
from chimera.instruments.fakedome import FakeDome  # noqa: E402
from chimera.instruments.fakefilterwheel import FakeFilterWheel  # noqa: E402
from chimera.instruments.faketelescope import FakeTelescope  # noqa: E402

from chimera_robobs.controllers.robobs import RobObs  # noqa: E402
from chimera_robobs.scheduling import model  # noqa: E402
from chimera_robobs.scheduling.dates import ensure_datetime  # noqa: E402

# --- clock warp ---------------------------------------------------------
# Site derives all time/ephemeris from ut(): patching it at class level
# moves twilights, LST and MJD together for every Site in the process.
_CLOCK_OFFSET = dt.timedelta(0)
_real_ut = Site.ut
_real_localtime = Site.localtime


def _warped_ut(self):
    return _real_ut(self) + _CLOCK_OFFSET


def _warped_localtime(self):
    return _real_localtime(self) + _CLOCK_OFFSET


Site.ut = _warped_ut
Site.localtime = _warped_localtime


def time_travel(offset: dt.timedelta) -> None:
    global _CLOCK_OFFSET
    _CLOCK_OFFSET = offset


def fail(msg: str) -> None:
    print(f"FAIL: {msg}", flush=True)
    os._exit(1)


def step(msg: str) -> None:
    print(f"--- {msg}", flush=True)


def main() -> None:
    host, port = "127.0.0.1", random.randint(20000, 60000)

    def loc(path: str) -> str:
        return f"tcp://{host}:{port}{path}"

    bus = Bus(f"tcp://{host}:{port}")
    manager = Manager(bus=bus)
    threading.Thread(target=bus.run_forever, daemon=True).start()
    if not bus._bus_started.wait(10):
        fail("bus did not start")

    step("registering observatory")
    manager.add_class(
        Site,
        "opd",
        dict(
            name="OPD",
            latitude="-22:32:04",
            longitude="-45:34:57",
            altitude=1864,
        ),
    )
    manager.add_class(FakeTelescope, "fake", {})
    manager.add_class(FakeFilterWheel, "fake", {"filters": "CLEAR B V R I"})
    manager.add_class(FakeCamera, "fake", {})
    manager.add_class(
        FakeDome, "fake", {"telescope": loc("/FakeTelescope/fake"), "mode": "Stand"}
    )
    manager.add_class(ImageServer, "srv", {"httpd": False, "autoload": False})
    manager.add_class(
        Scheduler,
        "sched",
        {
            "site": loc("/Site/opd"),
            "telescope": loc("/FakeTelescope/fake"),
            "camera": loc("/FakeCamera/fake"),
            "filterwheel": loc("/FakeFilterWheel/fake"),
            "dome": loc("/FakeDome/fake"),
        },
    )

    robobs_db = os.path.join(_HOME, "robobs.db")
    manager.add_class(
        RobObs,
        "robobs",
        {
            "site": loc("/Site/opd"),
            "schedulers": loc("/Scheduler/sched"),
            "telescope": loc("/FakeTelescope/fake"),
            "database": robobs_db,
        },
    )

    site = Proxy(loc("/Site/opd"), bus, timeout=30)
    site.resolve()
    robobs = Proxy(loc("/RobObs/robobs"), bus, timeout=30)
    robobs.resolve()
    dome = Proxy(loc("/FakeDome/fake"), bus, timeout=60)
    dome.resolve()

    step("time travel: jump to 10 min after evening twilight")
    twilight_end = ensure_datetime(site.sunset_twilight_end()).replace(tzinfo=None)
    now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
    time_travel(twilight_end - now + dt.timedelta(minutes=10))
    fake_now = ensure_datetime(site.ut()).replace(tzinfo=None)
    fake_mjd = float(site.mjd())
    print(f"    fake now: {fake_now} (mjd {fake_mjd:.4f})")

    step("seeding one observable program (zenith target, 2s exposure)")
    factory = model.open_database(robobs_db)
    session = factory()
    lst_hours = float(site.lst_in_rads()) * 12.0 / 3.141592653589793
    project = model.Project(pid="TEST", pi="fast-night", priority=1)
    session.add(project)
    target = model.Target(name="tgt1", target_ra=lst_hours, target_dec=-22.53)
    session.add(target)
    session.commit()
    blockpar = model.BlockPar(bid=0, pid="TEST")
    blockpar.max_airmass = 5.0
    blockpar.sched_algorithm = 0  # HIG
    session.add(blockpar)
    session.commit()
    block = model.ObsBlock(
        target_id=target.id, blockid=0, pid="TEST", block_par_id=blockpar.id
    )
    block.actions.append(model.Expose(frames=1, exptime=2.0))
    session.add(block)
    session.commit()
    program = model.Program(
        target_id=target.id,
        name="tgt1",
        pi="fast-night",
        priority=1,
        slew_at=fake_mjd,
        pid="TEST",
        project_id=project.id,
        obsblock_id=block.id,
        blockpar_id=blockpar.id,
    )
    session.add(program)
    session.commit()

    step("opening the dome, cold-starting robobs")
    dome.open_slit()
    robobs.start()
    robobs.wake()

    # --- assertion 1+2: the program executes and logs Started AND End(OK)
    deadline = time.monotonic() + 120
    started = ended_ok = False
    while time.monotonic() < deadline and not (started and ended_ok):
        time.sleep(2)
        entries = [
            log_entry.action for log_entry in session.query(model.ObservingLog).all()
        ]
        session.expire_all()
        started = any("Program Started" in entry for entry in entries)
        ended_ok = any("Program End" in entry and "OK" in entry for entry in entries)
    if not started:
        fail("cold start: program never started (machine waiting forever?)")
    print("    PASS: cold start executes the first program")
    if not ended_ok:
        fail("observing log has no 'Program End ... OK' entry for a successful run")
    print("    PASS: successful program logged Started AND End(OK)")

    session.expire_all()
    finished = session.query(model.Program).filter(model.Program.finished).count()
    if finished != 1:
        fail(f"expected 1 finished program, found {finished}")
    print("    PASS: program marked finished")

    # --- assertion 3: after stop, a pending program must NOT execute
    step("daytime-zombie guard: stop robobs, seed another program, wake")
    robobs.stop()
    n_started_before = (
        session.query(model.ObservingLog)
        .filter(model.ObservingLog.action.like("%Program Started%"))
        .count()
    )
    zombie = model.Program(
        target_id=target.id,
        name="tgt1",
        pi="fast-night",
        priority=1,
        slew_at=float(site.mjd()),
        pid="TEST",
        project_id=project.id,
        obsblock_id=block.id,
        blockpar_id=blockpar.id,
    )
    session.add(zombie)
    session.commit()
    robobs.wake()
    time.sleep(12)
    session.expire_all()
    n_started_after = (
        session.query(model.ObservingLog)
        .filter(model.ObservingLog.action.like("%Program Started%"))
        .count()
    )
    if n_started_after != n_started_before:
        fail(
            "a program executed while robobs was STOPPED "
            f"({n_started_before} -> {n_started_after} started entries)"
        )
    print("    PASS: stopped robobs executes nothing")

    print("ALL PASS", flush=True)
    # skip normal interpreter teardown: the bus's non-daemon machinery is
    # known to hang on exit, and everything lives in a scratch HOME anyway
    os._exit(0)


if __name__ == "__main__":
    main()
