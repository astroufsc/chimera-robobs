# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2014-present chimera-robobs authors

"""SQLAlchemy model for the robobs scheduling database.

Ported from ``legacy_py2/controllers/scheduler/model.py`` with a reorganized
schema.  The database layout changed (snake_case columns, foreign keys that
point at real primary keys instead of non-unique columns, ``Expose.exptime``
as Float); **no data migration from the legacy database is provided** — the
robobs database is rebuilt from the project/target/block input files.

Schema changes relative to the legacy model:

* all columns snake_case (``lastObservation`` -> ``last_observation``,
  ``targetRa`` -> ``target_ra``, ``maxairmass`` -> ``max_airmass``,
  ``schedalgorith`` -> ``sched_algorithm``, ...);
* ``obsblock.block_par_id`` references ``blockpar.id`` (legacy pointed at the
  non-unique ``blockpar.bid``), ``obsblock.target_id``/``program.target_id``
  reference ``targets.id``, ``program.project_id`` references ``projects.id``;
* legacy foreign keys to non-unique columns were dropped
  (``program.name -> targets.name``, ``observinglog.name -> targets.name``,
  ``observinglog.priority -> program.priority``, ``*.pid -> projects.pid`` —
  ``pid`` columns are now plain strings holding the project code);
* ``program.expose_at`` was dropped (never written by the tooling and there
  is no chimera 0.2 equivalent; ``slew_at`` maps to chimera's ``start_at``);
* ``Expose.exptime`` is a Float (legacy Integer truncated exposure times) and
  ``Expose.binning``/``AutoFocus.binning`` are Strings to match chimera 0.2;
* no import-time side effects: use :func:`open_database` to create/attach a
  database and obtain a session factory.
"""

import datetime as dt
import os

from chimera.core.constants import SYSTEM_CONFIG_DIRECTORY
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    PickleType,
    String,
    Text,
    create_engine,
)
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import backref, declarative_base, relationship, sessionmaker

#: default location of the robobs scheduling database
DEFAULT_ROBOBS_DATABASE = os.path.join(SYSTEM_CONFIG_DIRECTORY, "robobs.db")

Base = declarative_base()


def _utcnow() -> dt.datetime:
    """Naive UTC timestamp for column defaults (the schema stores naive UTC)."""
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


def open_database(path: str | None = None, echo: bool = False) -> sessionmaker:
    """Create/attach the robobs database and return a session factory.

    :param path: sqlite database path (default ``~/.chimera/robobs.db``).
    :param echo: enable SQLAlchemy statement logging.
    """
    if path is None:
        path = DEFAULT_ROBOBS_DATABASE
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{path}",
        connect_args={"check_same_thread": False},
        echo=echo,
    )
    Base.metadata.create_all(engine)
    # expire_on_commit=False: program tuples are passed between sessions
    # (controller event handlers, scheduling algorithms) and must stay
    # readable after commits, as the legacy code assumed.
    return sessionmaker(bind=engine, expire_on_commit=False)


class ExtMoniDB(Base):
    """Bookkeeping for the extinction-monitor scheduling algorithm."""

    __tablename__ = "extmonidb"

    id = Column(Integer, primary_key=True)

    nairmass = Column(Integer, default=1)

    pid = Column(String)  # project code
    target_id = Column(Integer, ForeignKey("targets.id"))

    observed_am = relationship(
        "ObservedAM",
        backref=backref("extmonidb", order_by="ObservedAM.id"),
        cascade="all, delete, delete-orphan",
    )

    def __str__(self):
        return f"extmonidb[{self.pid}:{self.target_id}]: {len(self.observed_am)}/{self.nairmass}"


class ObservedAM(Base):
    __tablename__ = "observedam"

    id = Column(Integer, primary_key=True)
    extmoni_id = Column(Integer, ForeignKey("extmonidb.id"))

    airmass = Column(Float, default=1.0)
    altitude = Column(Float, default=90.0)


class TimedDB(Base):
    """Bookkeeping for the timed scheduling algorithm."""

    __tablename__ = "timeddb"

    id = Column(Integer, primary_key=True)

    pid = Column(String)  # project code
    block_id = Column(Integer, ForeignKey("obsblock.id"))
    target_id = Column(Integer, ForeignKey("targets.id"))

    execute_at = Column(Float, default=0.0)
    observed_at = Column(Float, default=0.0)
    finished = Column(Boolean, default=False)
    scheduled = Column(Boolean, default=False)

    def __str__(self):
        status = (
            f"block:{self.block_id} @{self.observed_at:.3f}"
            if self.finished
            else "pending"
        )
        return f"[timed:{self.pid}] execute@: {self.execute_at:.3f} [{status}]"


class RecurrentDB(Base):
    """Bookkeeping for the recurrent scheduling algorithm."""

    __tablename__ = "recurrentdb"

    id = Column(Integer, primary_key=True)

    pid = Column(String)  # project code
    block_id = Column(Integer, ForeignKey("obsblock.id"))
    target_id = Column(Integer, ForeignKey("targets.id"))

    visits = Column(Integer, default=0)
    max_visits = Column(Integer, default=0)  # 0 means unrestricted
    last_visit = Column(DateTime, default=None)

    def __str__(self):
        return f"[Recurrent:{self.pid}] visits: {self.visits} lastVisit: {self.last_visit}]"


class Target(Base):
    __tablename__ = "targets"

    id = Column(Integer, primary_key=True)
    name = Column(String, default="Program")
    type = Column(String, default="OBJECT")
    last_observation = Column(DateTime, default=None)
    observed = Column(Boolean, default=False)
    scheduled = Column(Boolean, default=False)
    target_ra = Column(Float, default=0.0)  # hours
    target_dec = Column(Float, default=0.0)  # degrees
    target_epoch = Column(Float, default=2000.0)
    target_ah = Column(Float, default=0.0)  # hour angle, hours
    target_mag = Column(Float, default=0.0)
    mag_filter = Column(String, default=None)
    link = Column(String, default=None)

    def __str__(self):
        from chimera.util.position import Position

        ra_dec = Position.from_ra_dec(self.target_ra, self.target_dec, "J2000")

        prefix = (
            f"#[id: {self.id!s:>5}] [name: {self.name!s:>15} {ra_dec} "
            f"(ah: {self.target_ah:.2f})] [type: {self.type}]"
        )
        if self.observed:
            return f"{prefix} #LastObserved@: {self.last_observation}"
        return f"{prefix} #NeverObserved"

    @hybrid_property
    def lst(self):
        return self.target_ra + self.target_ah

    @lst.setter
    def lst(self, lmst):
        ah = lmst - self.target_ra
        if ah > 12.0:
            ah -= 24.0
        self.target_ah = ah


class BlockPar(Base):
    """Observing constraints shared by the blocks of a project."""

    __tablename__ = "blockpar"

    id = Column(Integer, primary_key=True)
    bid = Column(Integer)  # user-level block-parameter id (unique per project)
    pid = Column(String, default="")  # project code

    max_airmass = Column(Float, default=2.5)
    min_airmass = Column(Float, default=-1.0)
    max_moon_bright = Column(Float, default=100.0)  # percent
    min_moon_bright = Column(Float, default=0.0)  # percent
    min_moon_distance = Column(Float, default=-1.0)  # degrees
    max_seeing = Column(Float, default=2.0)  # arcsec
    cloud_cover = Column(Integer, default=0)  # user defined scale
    sched_algorithm = Column(Integer, default=0)  # scheduling algorithm id
    apply_ext_corr = Column(Boolean, default=False)

    def __str__(self):
        return (
            f"#[id: {self.id!s:>4}][bid: {self.bid!s:>4}][PID: {self.pid!s:>10}]"
            f"[airmass: {self.max_airmass:5.2f}][seeing: {self.max_seeing:5.2f}]"
            f"[cloud: {self.cloud_cover:2d}][schedAlgorithm: {self.sched_algorithm:2d}]"
        )


class ObsBlock(Base):
    __tablename__ = "obsblock"

    id = Column(Integer, primary_key=True)
    target_id = Column(Integer, ForeignKey("targets.id"))
    blockid = Column(Integer)  # user-level block id
    block_par_id = Column(Integer, ForeignKey("blockpar.id"))
    pid = Column(String)  # project code
    observed = Column(Boolean, default=False)
    completed = Column(Boolean, default=False)
    last_observation = Column(DateTime, default=None)
    scheduled = Column(Boolean, default=False)
    length = Column(Float, default=0.0)  # block length in seconds
    actions = relationship(
        "Action",
        backref=backref("obsblock", order_by="Action.id"),
        cascade="all, delete, delete-orphan",
    )

    def __str__(self):
        flags = ("| status: scheduled" if self.scheduled else "") + (
            "| status: completed" if self.completed else ""
        )
        if self.observed:
            return (
                f"#{self.blockid} {self.pid}[{self.target_id}] "
                f"[lastObserved: {self.last_observation}{flags}]: "
                f"with {len(self.actions)} actions."
            )
        return (
            f"#{self.blockid} {self.pid}[{self.target_id}] "
            f"[#NeverObserved{flags}]: with {len(self.actions)} actions."
        )


class Project(Base):
    __tablename__ = "projects"

    id = Column(Integer, primary_key=True)
    pid = Column(String, default="PID")  # project code
    pi = Column(String, default="Anonymous Investigator")
    abstract = Column(Text, default="")
    url = Column(String, default="")
    priority = Column(Integer, default=0)

    def __str__(self):
        # legacy __str__ referenced the non-existent self.flag
        return (
            f"#{self.id!s:>3} {self.pid} pi:{self.pi} "
            f"#abstract: {self.abstract} #url: {self.url}"
        )


class Program(Base):
    __tablename__ = "program"

    id = Column(Integer, primary_key=True)
    target_id = Column(Integer, ForeignKey("targets.id"))
    name = Column(String)  # target name (plain copy, no FK)
    pi = Column(String, default="Anonymous Investigator")

    priority = Column(Integer, default=0)

    created_at = Column(DateTime, default=_utcnow)
    finished = Column(Boolean, default=False)
    slew_at = Column(Float, default=0.0)  # MJD

    # Extra information, not present in the chimera scheduler schema,
    # required to link an observing program with its observing block.
    pid = Column(String)  # project code (plain copy, convenient for queries)
    project_id = Column(Integer, ForeignKey("projects.id"))
    obsblock_id = Column(Integer, ForeignKey("obsblock.id"))
    blockpar_id = Column(Integer, ForeignKey("blockpar.id"))

    def __str__(self):
        return (
            f"#{self.id} {self.pid}:{self.name} pi:{self.pi} "
            f"[obsblock: {self.obsblock_id}|blockpar: {self.blockpar_id} "
            f"| target: {self.target_id}]"
        )

    def chimera_program(self):
        """Convert to a chimera scheduler ``Program`` (without actions)."""
        from chimera.controllers.scheduler.model import Program as CProgram

        cp = CProgram()
        cp.tid = self.target_id
        cp.name = self.name
        cp.pi = self.pi
        cp.priority = self.priority
        cp.created_at = self.created_at
        cp.finished = self.finished
        # legacy slewAt/exposeAt were merged into chimera 0.2's start_at
        cp.start_at = self.slew_at
        return cp


class ObservingLog(Base):
    __tablename__ = "observinglog"

    id = Column(Integer, primary_key=True)
    time = Column(DateTime, default=_utcnow)
    target_id = Column(Integer, ForeignKey("targets.id"))
    name = Column(String)  # target name (plain copy, no FK)
    priority = Column(Integer, default=-1)
    action = Column(String)

    def __str__(self):
        return f"{self.time} [{self.name}] P{self.priority} Action: {self.action}"


class Action(Base):
    __tablename__ = "action"

    id = Column(Integer, primary_key=True)
    block_id = Column(Integer, ForeignKey("obsblock.id"))
    action_type = Column("type", String(100))

    __mapper_args__ = {"polymorphic_on": action_type}

    def chimera_action(self):
        """Convert to the equivalent chimera scheduler action."""
        raise NotImplementedError()


class AutoFocus(Action):
    __tablename__ = "action_focus"
    __mapper_args__ = {"polymorphic_identity": "AutoFocus"}

    id = Column(Integer, ForeignKey("action.id"), primary_key=True)
    start = Column(Integer, default=0)
    end = Column(Integer, default=1)
    step = Column(Integer, default=1)
    filter = Column(String, default=None)
    exptime = Column(Float, default=1.0)
    binning = Column(String, default=None)
    window = Column(String, default=None)

    def __str__(self):
        return (
            f"autofocus: start={self.start} end={self.end} "
            f"step={self.step} exptime={self.exptime:.2f}"
        )

    def chimera_action(self):
        from chimera.controllers.scheduler.model import AutoFocus as CAutoFocus

        chim_act = CAutoFocus()
        chim_act.start = self.start
        chim_act.end = self.end
        chim_act.step = self.step
        chim_act.filter = self.filter
        chim_act.exptime = self.exptime
        chim_act.binning = self.binning
        chim_act.window = self.window
        return chim_act


class AutoFlat(Action):
    __tablename__ = "action_flat"
    __mapper_args__ = {"polymorphic_identity": "AutoFlats"}

    id = Column(Integer, ForeignKey("action.id"), primary_key=True)
    filter = Column(String, default=None)
    frames = Column(Integer, default=1)
    binning = Column(String, default=None)

    def __str__(self):
        return f"autoflat: filter={self.filter} frames={self.frames}"

    def chimera_action(self):
        from chimera.controllers.scheduler.model import AutoFlat as CAutoFlat

        ca = CAutoFlat()
        ca.filter = self.filter
        ca.frames = self.frames
        ca.binning = self.binning
        return ca


class PointVerify(Action):
    __tablename__ = "action_pv"
    __mapper_args__ = {"polymorphic_identity": "PointVerify"}

    id = Column(Integer, ForeignKey("action.id"), primary_key=True)
    here = Column(Boolean, default=None)
    choose = Column(Boolean, default=None)

    def __str__(self):
        if self.choose is True:
            return "pointing verification: system defined field"
        elif self.here is True:
            return "pointing verification: current field"
        return "pointing verification"

    def chimera_action(self):
        from chimera.controllers.scheduler.model import PointVerify as CPointVerify

        ca = CPointVerify()
        ca.here = self.here
        ca.choose = self.choose
        return ca


class Point(Action):
    __tablename__ = "action_point"
    __mapper_args__ = {"polymorphic_identity": "Point"}

    id = Column(Integer, ForeignKey("action.id"), primary_key=True)
    target_ra_dec = Column(PickleType, default=None)
    target_alt_az = Column(PickleType, default=None)
    offset_ns = Column(PickleType, default=None)  # offset North (>0)/South (<0)
    offset_ew = Column(PickleType, default=None)  # offset West (>0)/East (<0)
    target_name = Column(String, default=None)

    def chimera_action(self):
        from chimera.controllers.scheduler.model import Point as CPoint

        ca = CPoint()
        if self.target_ra_dec is not None:
            ca.target_ra_dec = self.target_ra_dec
        elif self.target_alt_az is not None:
            ca.target_alt_az = self.target_alt_az
        elif self.target_name is not None:
            ca.target_name = self.target_name

        if self.offset_ns is not None:
            ca.offset_ns = self.offset_ns
        if self.offset_ew is not None:
            ca.offset_ew = self.offset_ew
        return ca

    def __str__(self):
        offset_ns_str = (
            ""
            if self.offset_ns is None
            else (
                f" north {self.offset_ns}"
                if self.offset_ns > 0
                else f" south {self.offset_ns}"
            )
        )
        offset_ew_str = (
            ""
            if self.offset_ew is None
            else (
                f" west {self.offset_ew}"
                if self.offset_ew > 0
                else f" east {self.offset_ew}"
            )
        )
        offset = (
            ""
            if self.offset_ns is None and self.offset_ew is None
            else f"offset: {offset_ns_str}{offset_ew_str}"
        )

        if self.target_ra_dec is not None:
            return f"point: (ra,dec) {self.target_ra_dec}{offset}"
        elif self.target_alt_az is not None:
            return f"point: (alt,az) {self.target_alt_az}{offset}"
        elif self.target_name is not None:
            return f"point: (object) {self.target_name}{offset}"
        elif self.offset_ns is not None or self.offset_ew is not None:
            return offset
        return "No target to point to."


class Expose(Action):
    __tablename__ = "action_expose"
    __mapper_args__ = {"polymorphic_identity": "Expose"}

    id = Column(Integer, ForeignKey("action.id"), primary_key=True)
    filter = Column(String, default=None)
    frames = Column(Integer, default=1)

    exptime = Column(Float, default=5.0)  # legacy Integer truncated exposures

    binning = Column(String, default=None)
    window = Column(String, default=None)

    shutter = Column(String, default="OPEN")
    wait_dome = Column(Boolean, default=True)

    image_type = Column(String, default="")
    filename = Column(String, default="$DATE-$TIME")
    object_name = Column(String, default="")
    compress_format = Column(String, default="NO")

    def __str__(self):
        return (
            f"expose: exptime={self.exptime:.2f} frames={self.frames} "
            f"type={self.image_type}"
        )

    def chimera_action(self):
        from chimera.controllers.scheduler.model import Expose as CExpose

        ca = CExpose()
        ca.filter = self.filter
        ca.frames = self.frames
        ca.exptime = self.exptime
        ca.binning = self.binning
        ca.window = self.window
        ca.shutter = self.shutter
        ca.wait_dome = self.wait_dome
        ca.image_type = self.image_type
        ca.filename = self.filename
        ca.object_name = self.object_name
        ca.compress_format = self.compress_format
        return ca


def block_duration(
    actions,
    readout: float = 0.0,
    autofocus_sweep: float = 0.0,
    autofocus_set: float = 0.0,
) -> float:
    """Estimated duration in seconds of a sequence of block actions.

    Only exposures and autofocus runs contribute; the overhead constants
    differ per caller (engine program length: no overheads; CLI block
    ingest: 12 s readout + 600 s focus sweep; offline simulation: 20 s
    readout; extinction monitor: config-driven), so they are parameters.
    ``AutoFocus.step > 0`` is a focus sweep; ``step == 0`` is the "set
    focuser position" sentinel used at T80S; ``step < 0`` takes no time.
    """
    total = 0.0
    for act in actions:
        if isinstance(act, Expose):
            total += (float(act.exptime or 0.0) + readout) * int(act.frames or 1)
        elif isinstance(act, AutoFocus):
            step = act.step or 0
            if step > 0:
                total += autofocus_sweep
            elif step == 0:
                total += autofocus_set
    return total
