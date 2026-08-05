# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2014-present chimera-robobs authors

"""chimera-robobs command line tool.

Offline commands (operate directly on the robobs database):

    chimera-robobs add-project -f project.yaml
    chimera-robobs delete-project --pid PID
    chimera-robobs clean-project
    chimera-robobs add-targets -f targets.csv
    chimera-robobs clean-targets
    chimera-robobs add-observing-block -f blocks.txt
    chimera-robobs clean-observing-blocks
    chimera-robobs delete-observing-block --pid PID
    chimera-robobs clean-queue --pid PID
    chimera-robobs observing-log [--start ...] [--end ...]
    chimera-robobs plot-log [-f obsplan.png] [--simulation] [time options]

Commands that talk to a running chimera server:

    chimera-robobs make-queue --pid PID [--pid-config file.yaml] [time options]
    chimera-robobs process-queue [time options]        (offline simulation)
    chimera-robobs status [--json] [--watch [s]]       (night-state panel)
    chimera-robobs start | stop | wake | monitor

The YAML inputs use the canonical snake_case dialect with readable
algorithm names (``scheduling_algorithm: recurrent``,
``pre_actions``/``post_actions``, ``slot_len``); files with unknown keys
are rejected whole.  Legacy-dialect files (``schedalgorith: 3``,
``maxairmass``, ``imageType``, ``pos-actions``, ``slotLen``, ...) must be
converted once with ``scripts/migrate_legacy_config.py``.
"""

import argparse
import contextlib
import datetime as dt
import json
import logging
import math
import os
import random
import shutil
import sys
import tempfile
import time
from types import SimpleNamespace

import numpy as np
import yaml
from chimera.util.coord import Coord
from chimera.util.position import Position
from sqlalchemy import and_, desc, or_
from sqlalchemy import func as sqla_func

from chimera_robobs.cli.status import cmd_status
from chimera_robobs.scheduling.algorithms import (
    build_algorithms,
    parse_algorithm_id,
)
from chimera_robobs.scheduling.algorithms.skyflat import SkyFlat
from chimera_robobs.scheduling.dates import (
    MJD_JD_OFFSET,
    SECONDS_PER_DAY,
    datetime_from_jd,
    datetime_from_mjd,
    jd_from_datetime,
)
from chimera_robobs.scheduling.engine import RobObsEngine
from chimera_robobs.scheduling.model import (
    DEFAULT_ROBOBS_DATABASE,
    AutoFlat,
    AutoFocus,
    BlockPar,
    Expose,
    ObsBlock,
    ObservingLog,
    Point,
    PointVerify,
    Program,
    Project,
    Target,
    block_duration,
    open_database,
)
from chimera_robobs.scheduling.siteadapter import SiteAdapter

log = logging.getLogger(__name__)

ACTION_TYPES = {
    "autofocus": AutoFocus,
    "autoflat": AutoFlat,
    "pointverify": PointVerify,
    "point": Point,
    "expose": Expose,
}

#: How far outside the observing window the process-queue simulation may
#: reach, in DAYS. Twilight calibrations are anchored at sunset, ~1.5 h
#: before the -18 deg dusk at OPD, so a few hours is enough; the bound
#: exists to stop a queue left over from an EARLIER NIGHT from dragging the
#: simulated clock back into it (opd-40 2026-07-30).
SIM_NIGHT_MARGIN = 6.0 / 24.0

#: hint appended to errors caused by unconverted legacy-dialect files
MIGRATE_HINT = "legacy files must be converted with scripts/migrate_legacy_config.py"

#: project block-parameter keys (``scheduling_algorithm`` is the readable
#: spelling of the ``sched_algorithm`` column)
BLOCKPAR_KEY_ALIASES = {"scheduling_algorithm": "sched_algorithm"}
BLOCKPAR_FIELDS = {
    "max_airmass",
    "min_airmass",
    "max_moon_bright",
    "min_moon_bright",
    "min_moon_distance",
    "max_seeing",
    "cloud_cover",
    "sched_algorithm",
    "apply_ext_corr",
}

#: pid-config (make-queue --pid-config) keys understood by the scheduling
#: algorithms; unknown keys reject the file
PID_CONFIG_KEYS = {
    "pid",
    "slot_len",
    "pool_size",
    "max_sched_blocks",
    "n_stars",
    "n_airmass",
    "recurrence",
    "times",
    "past_meridian_only",
    "expire_overdue",
    "flat_window",
    "n_filters",
    "lookback",
    "flat_sun_alt",
    "night_boundary",
}

#: legacy CSV column names -> Target columns.  The production pointing CSVs
#: spell the epoch column ``EPOC`` (no H); both spellings are accepted.
TARGET_CSV_COLUMNS = {
    "name": "name",
    "type": "type",
    "mag": "target_mag",
    "epoch": "target_epoch",
    "epoc": "target_epoch",
    "magfilter": "mag_filter",
    "link": "link",
}


def _out(message: str = "") -> None:
    print(message)


def _err(message: str) -> None:
    print(message, file=sys.stderr)


def _database_path(args) -> str:
    return args.database or DEFAULT_ROBOBS_DATABASE


def _session_factory(args):
    return open_database(args.database)


def backup_database(args) -> None:
    """Save a timestamped copy of the robobs database.

    The legacy tool copied the checklist database by mistake in several
    commands; this always backs up the actual robobs database in use.
    """
    path = _database_path(args)
    if os.path.exists(path):
        backup = "{}.{}.bak".format(path, time.strftime("%Y%m%d%H%M%S"))
        shutil.copy(path, backup)
        _out(f"-Database backed up to {backup}")


def _load_yaml(path: str):
    with open(path) as fp:
        return yaml.safe_load(fp)


# ----------------------------------------------------------------------
# projects
# ----------------------------------------------------------------------


def upsert_project(session, config) -> Project:
    """Create or update a project (and its block parameters) from a parsed
    project YAML document."""
    if "project" not in config:
        raise ValueError("No project section defined in configuration file.")

    pid = config["project"]["pid"]
    pi = config["project"]["pi"]
    abstract = config["project"]["abstract"]
    url = config["project"]["url"]
    priority = config["project"]["priority"]

    project = session.query(Project).filter(Project.pid == pid).first()
    if project is not None:
        _out(f"-Project {pid} already in database. Updating...")
        project.pi = pi
        project.abstract = abstract
        project.url = url
        project.priority = priority
    else:
        _out(f"-Adding {pid} to the database ...")
        project = Project(pid=pid, pi=pi, abstract=abstract, url=url, priority=priority)
        session.add(project)

    # scheduling-algorithm parameters: stored with the project and used as
    # the make-queue defaults (the separate --pid-config file becomes a
    # per-night override).  An absent section leaves any stored one as is.
    if "scheduling" in config:
        scheduling = config["scheduling"] or {}
        unknown = set(scheduling) - (PID_CONFIG_KEYS - {"pid"})
        if unknown:
            raise ValueError(f"unknown scheduling keys {sorted(unknown)}")
        _out(f"-Scheduling parameters: {scheduling}")
        # PyYAML parses unquoted timestamps (``at: 2026-07-25 03:12:00``)
        # into datetimes; store them as the ISO strings the Timed time
        # parser accepts back
        project.scheduling = json.dumps(
            scheduling,
            default=lambda v: v.isoformat() if isinstance(v, dt.datetime) else str(v),
        )

    _out("-Reading observing block information...")

    if "observing_blocks" in config:
        _out(f"--Found {len(config['observing_blocks'])} blocks.")
        for index, observing_block in enumerate(config["observing_blocks"]):
            block_config = config["observing_blocks"][observing_block]
            # some production project files omit id/pid in the block section
            b_id = block_config.get("id", index)
            b_pid = block_config.get("pid", pid)
            block = (
                session.query(BlockPar)
                .filter(BlockPar.bid == b_id)
                .filter(BlockPar.pid == b_pid)
                .first()
            )
            add = False
            if block is None:
                _out(f"---Adding block {b_pid}.{b_id} to the database...")
                block = BlockPar(bid=b_id, pid=b_pid)
                add = True
            else:
                _out(f"---Block {b_pid}.{b_id} already in database. Updating...")

            for key, value in block_config.items():
                if key in ("id", "pid", "name"):
                    continue
                column = BLOCKPAR_KEY_ALIASES.get(key, key)
                if column not in BLOCKPAR_FIELDS:
                    raise ValueError(
                        f"unknown observing-block key {key!r} ({MIGRATE_HINT})"
                    )
                if column == "sched_algorithm":
                    value = parse_algorithm_id(value)
                _out(f" {column}: {value}")
                setattr(block, column, value)
            if add:
                session.add(block)
        session.commit()
    else:
        _out("--No block definition found.")

    session.commit()
    return project


def cmd_add_project(args) -> int:
    """Add a project (and related information) to the database."""
    _out(f"-Reading project information from {args.filename} ...")

    try:
        config = _load_yaml(args.filename)
    except yaml.YAMLError as exc:
        _err(str(exc))
        return 1

    session = _session_factory(args)()
    try:
        upsert_project(session, config)
    except ValueError as e:
        _err(f"[ERROR] - {e}")
        return 1

    _out("-Done")
    return 0


def cmd_add_project_inputs(args) -> int:
    """Ingest one project end to end: project YAML + targets CSV + a single
    block template, creating one observing block per target.

    Replaces the shell + sqlite3 dance that generated a block list from
    target row ids: the targets added here are known in-session, so the
    blocks are attached to them directly - no id bookkeeping, no raw SQL.
    Re-running updates the project/block parameters (priority, exposure
    times, ...) in place; run the clean-* commands first for a full reload.
    """
    try:
        project_config = _load_yaml(args.project)
        block_config = _load_yaml(args.block)
    except yaml.YAMLError as exc:
        _err(str(exc))
        return 1

    from astropy.table import Table

    targets_table = Table.read(args.targets, format="ascii.csv")

    backup_database(args)
    overheads = ingest_overheads(args)
    session = _session_factory(args)()
    try:
        project = upsert_project(session, project_config)
        pid = project.pid

        # the block parameters this project defined (add-project made them);
        # --blockpar-bid picks one when a project has several, else the sole
        # one is used (the production projects each have exactly one)
        blockpars = session.query(BlockPar).filter(BlockPar.pid == pid).all()
        if not blockpars:
            _err(f"*Project {pid} defines no observing_blocks; nothing to attach.")
            return 1
        if args.blockpar_bid is not None:
            bid = args.blockpar_bid
        elif len(blockpars) == 1:
            bid = blockpars[0].bid
        else:
            _err(
                f"*Project {pid} has {len(blockpars)} block parameters "
                f"{sorted(bp.bid for bp in blockpars)}; pick one with --blockpar-bid."
            )
            return 1

        targets = add_targets_from_table(session, targets_table)
        if not targets:
            _err(f"*No targets loaded from {args.targets}.")
            return 1

        for blockid, target in enumerate(targets, start=1):
            row = (pid, blockid, target.id, args.block, bid)
            add_observing_block(session, row, block_config, overheads)
    except ValueError as e:
        _err(f"*{e}")
        return 1

    _out(f"-Done: {pid} ({len(targets)} block(s) from {len(targets)} target(s)).")
    return 0


def cmd_delete_project(args) -> int:
    """Delete a project (and related information) from the database."""
    if not args.pid:
        _err("*Specify project to delete with '--pid' ...")
        return 1

    backup_database(args)

    session = _session_factory(args)()

    _out(f"-Deleting all references of project {args.pid} from database.")

    obsblock = session.query(ObsBlock).filter(ObsBlock.pid == args.pid)
    for block in obsblock:
        _out(
            f"--Deleting observing block {block.pid}.{block.blockid}.{block.target_id} ..."
        )
        for blk_action in block.actions:
            session.delete(blk_action)
        session.delete(block)

    blockpars = session.query(BlockPar).filter(BlockPar.pid == args.pid)
    for block in blockpars:
        _out(f"--Deleting block {block.pid}.{block.bid} parameters...")
        session.delete(block)

    projects = session.query(Project).filter(Project.pid == args.pid)
    for project in projects:
        _out(f"--Deleting project {project.pid}")
        session.delete(project)

    session.commit()
    _out("-Done")
    return 0


def cmd_clean_project(args) -> int:
    """Clean the whole project/blockpar/obsblock tables."""
    backup_database(args)

    session = _session_factory(args)()

    _out("-Cleaning project table from database.")

    for block in session.query(ObsBlock).all():
        _out(
            f"--Deleting observing block {block.pid}.{block.blockid}.{block.target_id} ..."
        )
        for blk_action in block.actions:
            session.delete(blk_action)
        session.delete(block)

    for block in session.query(BlockPar).all():
        _out(f"--Deleting block {block.pid}.{block.bid} parameters...")
        session.delete(block)

    for project in session.query(Project).all():
        _out(f"--Deleting project {project.pid}")
        session.delete(project)

    session.commit()
    _out("-Done")
    return 0


# ----------------------------------------------------------------------
# targets
# ----------------------------------------------------------------------


def add_targets_from_table(session, targets_table, match_by_name=False) -> list:
    """Add targets from an astropy table (CSV) to the database.

    Returns the list of :class:`Target` rows the file describes (populated
    ids after the commit), in file order.

    By default the table is APPEND-ONLY: rows are never updated and never
    matched against what is already there.  Names are not unique - two
    projects may each have a ``std`` or a ``test`` - so in general there is
    nothing to match on, and a reload must leave the old rows alone anyway:
    the observing log stores target ids, and rewriting or deleting them
    orphans every observation already recorded against them.  A reload
    appends a fresh set and the project's new blocks point at those; the
    previous rows stay behind as the history the log refers to.

    ``match_by_name`` opts out of that, for the generated inputs whose names
    ARE unique by construction (the OPOP occultations are
    ``<object>_<event_id>``).  An existing row of the same name is reused and
    its coordinates refreshed in place, so ingesting a regenerated file is
    idempotent and target ids stay stable - which is what lets the ingest be
    automated at all: without it, keeping the inputs current means
    ``delete-project`` + ``clean-targets`` on every run, orphaning the log
    entry of every occultation already observed.

    A name matching more than one existing row is refused rather than
    guessed at, the same rule ``plot-log`` uses when resolving an orphaned
    log entry by name.
    """
    columns = {name.lower().strip(): name for name in targets_table.dtype.names}

    for required in ("ra", "dec"):
        if required not in columns:
            raise ValueError(
                f"Required parameter, {required}, missing from input file..."
            )

    ignored = sorted(set(columns) - set(TARGET_CSV_COLUMNS) - {"ra", "dec"})
    if ignored:
        _out(f"-Ignoring unknown columns: {', '.join(ignored)}")

    added = []
    for i in range(len(targets_table)):
        ra = str(targets_table[columns["ra"]][i]).strip()
        dec = str(targets_table[columns["dec"]][i]).strip()
        try:
            position = Position.from_ra_dec(ra, dec)
        except ValueError:
            _err(
                f"*Object in line {i} has invalid coordinates ({ra},{dec}). Skipping..."
            )
            continue

        tpar = {"target_ra": position.ra.hour, "target_dec": position.dec.deg}
        for csv_name, column in TARGET_CSV_COLUMNS.items():
            if csv_name in columns:
                value = targets_table[columns[csv_name]][i]
                if column in ("target_mag", "target_epoch"):
                    # the production CSVs write epochs like "2000."
                    tpar[column] = float(value)
                else:
                    tpar[column] = str(value).strip()

        name = tpar.get("name")
        if match_by_name and name:
            matches = session.query(Target).filter(Target.name == name).all()
            if len(matches) > 1:
                raise ValueError(
                    f"{len(matches)} targets are named {name!r}; --match-by-name "
                    "cannot tell which one the file means"
                )
            if matches:
                target = matches[0]
                _out(f"--Updating {name} (id {target.id})...")
                for column, value in tpar.items():
                    setattr(target, column, value)
                added.append(target)
                continue

        target = Target(**tpar)
        _out(f"--Adding {target.name}...")
        session.add(target)
        added.append(target)

    session.commit()
    return added


def cmd_add_targets(args) -> int:
    """Add targets to the database from a CSV file."""
    from astropy.table import Table

    if not args.filename:
        _err("*Input not given. Use '-f'...")
        return 1

    _out(f"-Reading target list from {args.filename} ...")

    targets_table = Table.read(args.filename, format="ascii.csv")

    session = _session_factory(args)()
    try:
        add_targets_from_table(
            session, targets_table, match_by_name=getattr(args, "match_by_name", False)
        )
    except ValueError as e:
        _err(f"*{e}")
        return 1

    _out("-Done")
    return 0


def cmd_clean_targets(args) -> int:
    """Delete targets: all of them, or only those named in a CSV.

    ``--names-from`` deletes just the targets whose NAME appears in the
    given CSV, so a single project's targets can be dropped for a re-load
    without wiping the whole table (targets carry no project id). It never
    touches the observing log or the sky-flat ledger - those are history.
    """
    backup_database(args)

    session = _session_factory(args)()

    names_from = getattr(args, "names_from", None)
    if names_from:
        from astropy.table import Table

        table = Table.read(names_from, format="ascii.csv")
        columns = {name.lower().strip(): name for name in table.dtype.names}
        if "name" not in columns:
            _err(f"*{names_from} has no NAME column.")
            return 1
        names = {str(v).strip() for v in table[columns["name"]]}
        targets = session.query(Target).filter(Target.name.in_(names)).all()
        _out(f"-Deleting {len(targets)} of {len(names)} named target(s) from database")
        for target in targets:
            session.delete(target)
        session.commit()
        _out("-Done")
        return 0

    ntargets = int(session.query(Target).count())

    if ntargets == 0:
        _out("-Target list is already empty")
        session.commit()
        _out("-Done")
        return 0

    _out(f"-Deleting all {ntargets} targets from database")

    for target in session.query(Target).all():
        session.delete(target)

    session.commit()
    _out("-Done")
    return 0


# ----------------------------------------------------------------------
# observing blocks
# ----------------------------------------------------------------------


def _validate_offset(value) -> Coord:
    try:
        offset = Coord.from_as(int(value))
    except ValueError:
        offset = Coord.from_dms(value)
    return offset


def _format_context(target, block) -> dict:
    """Template context for string action parameters ({name}, {pid}, ...):
    the target and block column values, by column name."""
    ctx = {}
    for obj in (target, block):
        for column in obj.__table__.columns:
            try:
                ctx[column.key] = getattr(obj, column.key)
            except Exception:
                continue
    return ctx


def _apply_action_config(act, actconfig, ctx) -> None:
    """Set plain action attributes from an action configuration mapping."""
    for key, value in actconfig.items():
        if key == "action":
            continue
        if not hasattr(act, key):
            raise ValueError(
                f"unknown key {key!r} for action "
                f"{actconfig.get('action')!r} ({MIGRATE_HINT})"
            )
        if isinstance(value, str):
            try:
                value = value.format(**ctx)
            except KeyError as e:
                raise ValueError(
                    f"unknown template placeholder {{{e.args[0]}}} in action "
                    f"{actconfig.get('action')!r} ({MIGRATE_HINT})"
                ) from None
        setattr(act, key, value)


def _build_point_action(act, actconfig, target, with_offsets) -> None:
    if "ra" in actconfig and "dec" in actconfig:
        epoch = actconfig.get("epoch", "J2000")
        position = Position.from_ra_dec(actconfig["ra"], actconfig["dec"], epoch)
        act.target_ra_dec = position
    elif "alt" in actconfig and "az" in actconfig:
        position = Position.from_alt_az(actconfig["alt"], actconfig["az"])
        act.target_alt_az = position
    elif "name" in actconfig:
        act.target_name = actconfig["name"]
    elif not (with_offsets and "offset" in actconfig):
        act.target_ra_dec = Position.from_ra_dec(
            target.target_ra, target.target_dec, "J2000"
        )

    if with_offsets and "offset" in actconfig:
        offset_config = actconfig["offset"]
        if "north" in offset_config:
            offset = _validate_offset(offset_config["north"])
            _out(f"Offset north: {offset}")
            act.offset_ns = offset
        elif "south" in offset_config:
            offset = _validate_offset(offset_config["south"])
            _out(f"Offset south: {offset}")
            act.offset_ns = Coord.from_as(-offset.arcsec)

        if "west" in offset_config:
            offset = _validate_offset(offset_config["west"])
            _out(f"Offset west: {offset}")
            act.offset_ew = offset
        elif "east" in offset_config:
            offset = _validate_offset(offset_config["east"])
            _out(f"Offset east: {offset}")
            act.offset_ew = Coord.from_as(-offset.arcsec)


def _make_action(actconfig, target, block, with_offsets) -> object:
    act = ACTION_TYPES[actconfig["action"]]()
    if actconfig["action"] == "point":
        _build_point_action(act, actconfig, target, with_offsets=with_offsets)
    else:
        _apply_action_config(act, actconfig, _format_context(target, block))
    return act


#: read-out and focus-sweep overheads (seconds) used for the stored block
#: length. Defaults inherited from the legacy tool - they are properties of
#: the CAMERA (and of its binning and compression), not of the scheduler, so
#: no single number is right for every deployment: opd-40's QHY600 at 1x1
#: with fits_rice measured ~2.8 s per frame against these 12, and a focus
#: block ~270 s against these 600, i.e. block lengths ~4x too long. Override
#: per ingest with --readout-overhead / --autofocus-overhead; the stored
#: `obsblock.length` feeds the night-end fit check, the derived slot length
#: and the process-queue simulation alike.
INGEST_READOUT_OVERHEAD = 12.0
INGEST_AUTOFOCUS_OVERHEAD = 600.0
#: per-frame budget of an autoflat action: the sky-flat controller decides
#: the exposure itself and waits for the right sky level between frames
INGEST_AUTOFLAT_FRAME_OVERHEAD = 60.0


def ingest_overheads(args=None) -> dict:
    """Block-length overheads (seconds) for one ingest run."""
    return {
        "readout": getattr(args, "readout_overhead", None) or INGEST_READOUT_OVERHEAD,
        "autofocus_sweep": getattr(args, "autofocus_overhead", None)
        or INGEST_AUTOFOCUS_OVERHEAD,
        "autoflat_frame": getattr(args, "autoflat_frame_overhead", None)
        or INGEST_AUTOFLAT_FRAME_OVERHEAD,
    }


def _add_overhead_options(parser):
    parser.add_argument(
        "--readout-overhead",
        type=float,
        default=INGEST_READOUT_OVERHEAD,
        help="seconds added per exposure for readout when estimating a "
        f"block's length (default: {INGEST_READOUT_OVERHEAD:.0f}; measure "
        "it for your camera, binning and compression)",
    )
    parser.add_argument(
        "--autofocus-overhead",
        type=float,
        default=INGEST_AUTOFOCUS_OVERHEAD,
        help="seconds budgeted for one autofocus sweep when estimating a "
        f"block's length (default: {INGEST_AUTOFOCUS_OVERHEAD:.0f})",
    )
    parser.add_argument(
        "--autoflat-frame-overhead",
        type=float,
        default=INGEST_AUTOFLAT_FRAME_OVERHEAD,
        help="seconds budgeted per sky-flat frame when estimating a block's "
        f"length (default: {INGEST_AUTOFLAT_FRAME_OVERHEAD:.0f})",
    )


def add_observing_block(session, row, config, overheads=None) -> ObsBlock | None:
    """Add (or replace) one observing block from a block-list row.

    ``row`` is ``(pid, blockid, target_id, config_filename, blockpar_bid)``
    and ``config`` the parsed block YAML (with ``pre-actions``/``pos-actions``).
    ``overheads`` are the block-length estimates (see :func:`ingest_overheads`).
    """
    pid, blockid, target_id, _, bparid = row

    target = session.query(Target).filter(Target.id == target_id).first()
    if target is None:
        raise ValueError(f"No target defined for specified block {blockid}.")

    blockpar = (
        session.query(BlockPar)
        .filter(BlockPar.pid == pid, BlockPar.bid == bparid)
        .first()
    )
    if blockpar is None:
        raise ValueError(
            f"No block parameters {pid}.{bparid} in the database. Run add-project first."
        )

    existing = (
        session.query(ObsBlock)
        .filter(ObsBlock.target_id == target_id)
        .filter(ObsBlock.blockid == blockid)
        .filter(ObsBlock.pid == pid)
    )

    if existing.count() > 0:
        _out(f"<<Deleting {existing.count()} blocks.")
        for block in existing:
            if block.observed:
                _out(f"!!Block {block.id} already observed. Leaving as is.")
                return None
            for blk_action in block.actions:
                session.delete(blk_action)
            session.delete(block)
            session.commit()

    _out(f">>Adding block: {list(row)}")
    addblock = ObsBlock(
        target_id=target_id,
        blockid=blockid,
        pid=pid,
        block_par_id=blockpar.id,
    )

    unknown_sections = set(config) - {"pre_actions", "post_actions"}
    if unknown_sections:
        raise ValueError(
            f"unknown block-file sections {sorted(unknown_sections)} ({MIGRATE_HINT})"
        )

    # process pre-slew actions
    for actconfig in config.get("pre_actions") or []:
        act = _make_action(actconfig, target, addblock, with_offsets=False)
        addblock.actions.append(act)

    # slew to target — except for sky-flat blocks: the sky-flat controller
    # does its own (anti-solar) pointing, and slewing to the placeholder
    # target around sunset could aim near the sun
    if blockpar.sched_algorithm == SkyFlat.id:
        _out(f"No slew action: {target.name} is a sky-flat placeholder.")
    else:
        position = Position.from_ra_dec(target.target_ra, target.target_dec, "J2000")
        slewto = Point()
        slewto.target_ra_dec = position
        addblock.actions.append(slewto)
        _out(f"Slew to: {target.name} ({slewto})")

    # process post-slew actions
    post_actions = []
    for actconfig in config.get("post_actions") or []:
        act = _make_action(actconfig, target, addblock, with_offsets=True)
        post_actions.append(act)
        addblock.actions.append(act)

    # only post-slew actions count towards the stored block length
    addblock.length = block_duration(post_actions, **(overheads or ingest_overheads()))
    _out(f"Estimated block length: {addblock.length:.0f} s")
    session.add(addblock)
    session.commit()
    return addblock


def _read_block_list(
    path: str, *, by_name: bool = False, block_dir: str | None = None
) -> list[tuple[str, int, str | int, str, int]]:
    """Parse a block-list file: 5 whitespace-delimited columns
    ``pid blockid target config_yaml_path blockpar_bid`` (the production
    files mix spaces and tabs; blank lines and #-comments are skipped).

    ``target`` is a row id, or a target NAME when ``by_name`` (``@NAME@``
    delimiters are stripped) - the caller resolves names against the
    database, so a per-object list (each line its own block YAML, e.g. the
    OPOP occultations with per-event exposure times) needs no sqlite id
    lookup. ``block_dir`` rewrites each YAML path to that directory's copy
    (the generated lists embed the generating machine's absolute paths)."""
    rows = []
    with open(path) as fp:
        for lineno, line in enumerate(fp, start=1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) < 5:
                raise ValueError(f"{path}:{lineno}: expected 5 columns, got {line!r}")
            if by_name:
                target: str | int = parts[2].strip("@")
            else:
                target = int(parts[2])
            yaml_path = parts[3]
            if block_dir:
                yaml_path = os.path.join(block_dir, os.path.basename(yaml_path))
            rows.append((parts[0], int(parts[1]), target, yaml_path, int(parts[4])))
    return rows


def cmd_add_observing_block(args) -> int:
    """Add observing block definitions to the database."""
    if not args.filename:
        _err("*Input not given. Use '-f'...")
        return 1

    _out(f"-Reading observing blocks from {args.filename}")

    by_name = getattr(args, "by_name", False)
    try:
        block_list = _read_block_list(
            args.filename,
            by_name=by_name,
            block_dir=getattr(args, "block_dir", None),
        )
    except ValueError as e:
        _err(str(e))
        return 1

    backup_database(args)
    session = _session_factory(args)()

    overheads = ingest_overheads(args)

    # resolve target names once (by-name lists): a name that add-targets
    # loaded is known here, so no external id lookup is needed
    name_to_id = {}
    if by_name:
        name_to_id = {t.name: t.id for t in session.query(Target)}

    for row in block_list:
        if by_name:
            target_id = name_to_id.get(row[2])
            if target_id is None:
                _err(f"*target {row[2]!r} not in the database (run add-targets first).")
                return 1
            row = (row[0], row[1], target_id, row[3], row[4])
        try:
            config = _load_yaml(row[3])
        except yaml.YAMLError as exc:
            _err(str(exc))
            return 1
        try:
            add_observing_block(session, row, config, overheads)
        except ValueError as e:
            _err(str(e))
            return 1

    _out("-Done")
    return 0


def cmd_clean_observing_blocks(args) -> int:
    """Delete all observing blocks from the database."""
    backup_database(args)

    session = _session_factory(args)()

    nblocks = int(session.query(ObsBlock).count())

    if nblocks == 0:
        _out("-Observing block list is already empty")
        session.commit()
        _out("-Done")
        return 0

    _out(f"-Deleting all {nblocks} observing blocks from database")

    for block in session.query(ObsBlock).all():
        for blk_action in block.actions:
            session.delete(blk_action)
        session.delete(block)

    session.commit()
    _out("-Done")
    return 0


def cmd_delete_observing_block(args) -> int:
    """Delete the observing blocks of a specific project from the database."""
    if not args.pid:
        _err("*Specify project to delete with '--pid' ...")
        return 1

    backup_database(args)

    session = _session_factory(args)()

    query = session.query(ObsBlock).filter(ObsBlock.pid == args.pid)
    nblocks = int(query.count())

    if nblocks == 0:
        _out(f"-No observing block with PID={args.pid} to delete")
        session.commit()
        _out("-Done")
        return 0

    _out(f"-Deleting all {nblocks} observing blocks with PID={args.pid} from database")

    for block in query.all():
        for blk_action in block.actions:
            session.delete(blk_action)
        session.delete(block)

    session.commit()
    _out("-Done")
    return 0


# ----------------------------------------------------------------------
# observing log
# ----------------------------------------------------------------------


def _parse_when(value: str) -> dt.datetime:
    """Parse ISO-8601 or the legacy 'yyyy/mm/dd-hh:mm:ss' format."""
    try:
        return dt.datetime.fromisoformat(value)
    except ValueError:
        day, hour = value.split("-")
        yy, mm, dd = (int(v) for v in day.split("/"))
        hh, mi, ss = (int(v) for v in hour.split(":"))
        return dt.datetime(yy, mm, dd, hh, mi, ss)


def cmd_observing_log(args) -> int:
    """Show entries of the observing log."""
    session = _session_factory(args)()

    query = session.query(ObservingLog)
    if args.start:
        query = query.filter(ObservingLog.time > _parse_when(args.start))
    if args.end:
        query = query.filter(ObservingLog.time <= _parse_when(args.end))
    query = query.order_by(ObservingLog.time)

    for entry in query:
        _out(f"{entry}")
    return 0


# ----------------------------------------------------------------------
# observing-log plotting
# ----------------------------------------------------------------------


def _pair_observing_log(session, entries, start_marker, end_marker) -> list[dict]:
    """Pair start/end observing-log entries into program intervals.

    A start with no matching end (an aborted program) is closed at the next
    program's start — or one minute after its own start when it is the last
    entry — and flagged ``aborted`` so it can be drawn differently.
    """
    programs = []
    current = None
    flat_program_iters: dict[int, object] = {}
    ambiguous_names: set[str] = set()
    for entry in entries:
        if start_marker in entry.action:
            target = session.query(Target).filter(Target.id == entry.target_id).first()
            if target is None:
                # TEMPORARY, for databases that predate the append-only target
                # table. `clean-targets` used to run on every reload, deleting
                # rows the observing log still pointed at, so the whole night
                # before a reload vanished from the plot (opd-40 2026-07-28).
                # Names are NOT unique across projects, so resolve by name only
                # when exactly one target carries it: drawing an entry at
                # another target's altitude is worse than leaving a pre-reload
                # night off the plot. Nothing recorded after the append-only
                # change needs this - delete it once no live database carries
                # orphaned ids.
                matches = (
                    session.query(Target)
                    .filter(Target.name == entry.name)
                    .limit(2)
                    .all()
                )
                if len(matches) == 1:
                    target = matches[0]
                elif matches:
                    ambiguous_names.add(entry.name)
            if target is None:
                continue
            if current is not None:  # previous program never ended: aborted
                current["end"] = entry.time
                current["aborted"] = True
                programs.append(current)
            # project code for the color grouping (the log stores targets,
            # not projects: resolve through the queue programs)
            queue_program = (
                session.query(Program).filter(Program.target_id == target.id).first()
            )
            # sky-flat blocks point at a placeholder target (often below the
            # horizon at flat time): the plot draws them at the flat
            # position altitude instead of the placeholder's track
            is_flat = False
            if queue_program is not None:
                blockpar = (
                    session.query(BlockPar)
                    .filter(BlockPar.id == queue_program.blockpar_id)
                    .first()
                )
                is_flat = (
                    blockpar is not None and blockpar.sched_algorithm == SkyFlat.id
                )
            name = target.name
            if is_flat:
                # every flat block shares the placeholder target, so the
                # log name repeats; label the interval with the block's
                # FILTER instead.  The log entries and the queue programs
                # are both chronological, so pair them sequentially (a
                # nearest-slew_at match misassigns delayed blocks: they
                # are planned 60 s apart but run for minutes).
                if target.id not in flat_program_iters:
                    flat_program_iters[target.id] = iter(
                        session.query(Program)
                        .filter(Program.target_id == target.id)
                        .order_by(Program.slew_at)
                        .all()
                    )
                queue_program = (
                    next(flat_program_iters[target.id], None) or queue_program
                )
                block = (
                    session.query(ObsBlock)
                    .filter(ObsBlock.id == queue_program.obsblock_id)
                    .first()
                )
                filters = (
                    [a.filter for a in block.actions if isinstance(a, AutoFlat)]
                    if block is not None
                    else []
                )
                if filters:
                    name = ",".join(filters)
            current = {
                "name": name,
                "pid": queue_program.pid if queue_program is not None else None,
                "ra": target.target_ra,
                "dec": target.target_dec,
                "start": entry.time,
                "end": None,
                "aborted": False,
                "flat": is_flat,
            }
        elif end_marker in entry.action and current is not None:
            current["end"] = entry.time
            programs.append(current)
            current = None
    if current is not None:
        current["end"] = current["start"] + dt.timedelta(minutes=1)
        current["aborted"] = True
        programs.append(current)
    if ambiguous_names:
        _err(
            "*Left off the plot: orphaned log entries whose name matches more "
            f"than one target ({', '.join(sorted(ambiguous_names))})."
        )
    return programs


def cmd_plot_log(args) -> int:
    """Plot the observing-log altitude chart of a night.

    Resurrects the legacy ``makeObservingLog`` plot with the improvements
    from the never-merged mysql branch: aborted programs drawn dashed,
    hourly moon-distance annotations along each track, configurable output
    file and a Simulation/Observed title.
    """
    # imported lazily: matplotlib is slow to load and only this command
    # needs it
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.dates import DateFormatter

    session = _session_factory(args)()
    bus, site_proxy = _connect(args, args.site)
    try:
        site = SiteAdapter(site_proxy)
        times = make_times(args, site)
        obs_start = times.obs_start.replace(tzinfo=None)
        obs_end = times.obs_end.replace(tzinfo=None)
        pad = dt.timedelta(hours=2)

        # The astronomical night containing the window. obs_start is NOT the
        # night start on a mid-night --tonight replan (it is "now"), and with
        # --date-start/--date-end it is an arbitrary query bound - anchor on
        # the dusk preceding obs_end instead, and name the night after it.
        dusk_aware = site.sunset_twilight_end(times.obs_end - dt.timedelta(hours=24))
        dawn = site.sunrise_twilight_begin(dusk_aware).replace(tzinfo=None)
        dusk = dusk_aware.replace(tzinfo=None)

        # Frame the axis on the SUN, not on the query window: --date-start/
        # --date-end are deliberately generous (plot_night_progress.sh asks
        # for 20:00-13:00 UT so one window covers both sides of UT midnight)
        # and letting them drive the axis left hours of empty daylight on the
        # right of every plot. site.sunset() returns the NEXT sunset after the
        # date it is given, so anchor on UT noon of the dusk date.
        noon = dusk.replace(hour=12, minute=0, second=0, microsecond=0)
        sunset = site.sunset(noon).replace(tzinfo=None)
        sunrise = site.sunrise(sunset).replace(tzinfo=None)
        margin = dt.timedelta(minutes=30)
        x_lo = sunset - margin
        x_hi = sunrise + margin

        if args.simulation:
            start_marker = "Simulation: Acquisition Start"
            end_marker = "Simulation: Acquisition End"
        else:
            start_marker = "Program Started"
            end_marker = "Program End"

        entries = (
            session.query(ObservingLog)
            .filter(
                ObservingLog.time > obs_start - pad,
                ObservingLog.time <= obs_end + pad,
            )
            .order_by(ObservingLog.time)
            .all()
        )
        programs = _pair_observing_log(session, entries, start_marker, end_marker)
        if not programs:
            _err("*No matching observing-log entries in the selected window.")
            return 1

        # the sun window is the frame, not a filter: widen it rather than clip
        # anything that really ran outside it (a daytime test, a long abort)
        x_lo = min([x_lo] + [p["start"] for p in programs])
        x_hi = max([x_hi] + [p["end"] for p in programs])

        def altitude(ra, dec, when):
            return site.ra_dec_to_alt_az(ra, dec, site.lst_in_rads(when))[0]

        alt_min, alt_max = 15.0, 90.0
        fig, ax = plt.subplots(figsize=(14, 8))

        # astronomical-night boundaries, the altitude floor, and when this
        # plot was made (off-scale and invisible when plotting a past night)
        for boundary in (dusk, dawn):
            ax.plot([boundary, boundary], [alt_min, alt_max], "r--")
        ax.plot([x_lo, x_hi], [alt_min + 10] * 2, "r--")
        now = site.ut().replace(tzinfo=None)
        ax.plot([now, now], [alt_min, alt_max], "b--", label=f"plotted {now:%H:%M}")

        # moon altitude track
        moon_grid = [x_lo + i * (x_hi - x_lo) / 100 for i in range(101)]
        moon_alts = []
        for when in moon_grid:
            moon_ra, moon_dec = site.moon_ra_dec(when)
            moon_alts.append(altitude(moon_ra, moon_dec, when))
        ax.plot(moon_grid, moon_alts, "--", color="black", label="Moon")

        # one color per PROJECT; tracks are labeled with their target name
        colors: dict[str, str] = {}
        flat_count = 0  # staggers consecutive flat labels vertically
        for program in programs:
            minutes = int((program["end"] - program["start"]).total_seconds() // 60)
            grid = [
                program["start"] + dt.timedelta(minutes=i)
                for i in range(max(minutes, 1) + 1)
            ]
            if program.get("flat"):
                # sky flats: fixed band at the flat position altitude (the
                # placeholder target's own track is meaningless)
                alts = [80.0] * len(grid)
            else:
                alts = [altitude(program["ra"], program["dec"], t) for t in grid]

            group = program["pid"] or program["name"]
            label = None if group in colors else group
            style = "--" if program["aborted"] else "-"
            (line,) = ax.plot(grid, alts, style, color=colors.get(group), label=label)
            colors.setdefault(group, line.get_color())
            if not program["aborted"]:
                ax.fill_between(grid, alts, alt_min, facecolor=colors[group], alpha=0.5)
            if program.get("flat"):
                # consecutive flat blocks are narrow and share the same
                # altitude: cycle the label height so they don't overprint
                label_x = grid[len(grid) // 2]
                label_y = alts[0] + 1.2 + (flat_count % 3) * 2.6
                flat_count += 1
            else:
                label_x, label_y = grid[0], alts[0] + 1.2
            ax.text(
                label_x,
                label_y,
                program["name"],
                fontsize=7,
                color=colors[group],
                clip_on=True,
                ha="center" if program.get("flat") else "left",
            )

            if program.get("flat"):
                continue  # moon separations are meaningless for flats

            # hourly moon-distance annotations along the track
            target_pos = Position.from_ra_dec(program["ra"], program["dec"])
            for t, alt in zip(grid[::60], alts[::60]):
                moon_ra, moon_dec = site.moon_ra_dec(t)
                separation = float(
                    target_pos.angsep(Position.from_ra_dec(moon_ra, moon_dec))
                )
                ax.text(t, alt, f"{separation:.0f}", fontsize=7, clip_on=True)

        kind = "Simulation" if args.simulation else "Observed"
        ax.set_title(f"robobs {kind} — night of {dusk.date()}")
        ax.set_xlim(x_lo, x_hi)
        ax.set_ylim(alt_min, alt_max)
        ax.set_ylabel("Altitude (deg)")
        ax.xaxis.set_major_formatter(DateFormatter("%H:%M"))
        fig.autofmt_xdate(rotation=70)
        ax.legend(loc="upper right", fontsize="small")
        fig.savefig(args.filename, bbox_inches="tight")
        plt.close(fig)
        _out(f"-Plot saved to {args.filename}")
        return 0
    finally:
        bus.shutdown()


# ----------------------------------------------------------------------
# queue handling
# ----------------------------------------------------------------------


def _require_project(session, pid: str | None) -> bool:
    if not pid:
        _err("*Specify project id (--pid). Available options are:")
        for project in session.query(Project):
            _err(f"**{project.pid}")
        return False
    if session.query(Project).filter(Project.pid == pid).count() == 0:
        _err(f"*No project named {pid} on the database. Available options are:")
        for project in session.query(Project):
            _err(f"**{project.pid}")
        return False
    return True


def cmd_clean_queue(args) -> int:
    """Delete all scheduled programs of the specified project."""
    factory = _session_factory(args)
    session = factory()

    if not _require_project(session, args.pid):
        session.commit()
        return 1

    _out(
        f"-Deleting all scheduled observing blocks from project {args.pid} from the queue."
    )

    for program in session.query(Program).filter(Program.pid == args.pid):
        _out("--Deleting")
        _out(f"---{program}")
        session.delete(program)
        _out("--Done")

    session.commit()

    sched_blocks = session.query(ObsBlock).filter(
        ObsBlock.pid == args.pid,
        ObsBlock.scheduled == True,  # noqa: E712
    )
    for block in sched_blocks:
        block.scheduled = False

    for sched in build_algorithms(factory).values():
        sched.clean(args.pid)

    session.commit()
    return 0


def make_times(args, site: SiteAdapter, boundary: str = "night") -> SimpleNamespace:
    """Determine the start/end times of the night (legacy ``mktimes``).

    The default window is the night after *today's* evening twilight — which,
    past UTC midnight, silently jumps to the NEXT night and orphans the one
    in progress.  ``--tonight`` resolves the CURRENT night instead: from now
    (if already dark) or the coming evening twilight, to the morning twilight
    that ends it.

    ``boundary`` picks the project's night bracket.  It must match the
    engine's: ``parse_time_entry`` drops entries outside
    [obs_start, obs_end], so a wider-bracket entry is discarded here before
    the guard is ever consulted.
    """

    def dusk_of(when):
        if boundary == "twilight":
            return (
                site.sunset_twilight_begin()
                if when is None
                else site.sunset_twilight_begin(when)
            )
        return (
            site.sunset_twilight_end()
            if when is None
            else site.sunset_twilight_end(when)
        )

    def dawn_of(when):
        if boundary == "twilight":
            return (
                site.sunrise_twilight_end()
                if when is None
                else site.sunrise_twilight_end(when)
            )
        return (
            site.sunrise_twilight_begin()
            if when is None
            else site.sunrise_twilight_begin(when)
        )

    if getattr(args, "tonight", False):
        now = site.ut()
        obs_start = dusk_of(now)  # next evening twilight
        obs_end = dawn_of(now)  # next morning twilight
        if obs_end < obs_start:
            # the morning twilight comes first: we are inside a night —
            # schedule the remainder of it, starting now
            obs_start = now
    else:
        obs_start = dusk_of(None)
        obs_end = dawn_of(obs_start)

    if getattr(args, "jd_start", None):
        obs_start = datetime_from_jd(args.jd_start)
    elif getattr(args, "date_start", None):
        obs_start = _parse_when(args.date_start)

    if getattr(args, "jd_end", None):
        obs_end = datetime_from_jd(args.jd_end)
    elif getattr(args, "date_end", None):
        obs_end = _parse_when(args.date_end)

    lst_start = site.lst_in_rads(obs_start) * 12.0 / math.pi  # hours
    lst_end = site.lst_in_rads(obs_end) * 12.0 / math.pi  # hours

    if getattr(args, "lst_start", None) is not None:
        lst_start = args.lst_start
    if getattr(args, "lst_end", None) is not None:
        lst_end = args.lst_end

    return SimpleNamespace(
        obs_start=obs_start,
        obs_end=obs_end,
        lst_start=lst_start,
        lst_end=lst_end,
        jd_start=jd_from_datetime(obs_start),
        jd_end=jd_from_datetime(obs_end),
    )


#: extra hours of already-culminated sky admitted to the pool when a
#: project runs past_meridian_only. The default cut (night-start LST - 2 h)
#: keeps only targets that culminate DURING the night - the exact
#: complement of what the constraint needs, so no candidate was ever
#: eligible at the early occurrences (0 of 20 on 2026-07-21). 5 h of
#: western sky covers a target down to ~airmass 2.8 past the meridian;
#: the airmass check still rejects anything that has set.
PAST_MERIDIAN_POOL_HOURS = 5.0


def pool_lst_start(lst_start: float, config: dict) -> float:
    """Left edge of the target-selection LST window for a project."""
    if config.get("past_meridian_only"):
        return lst_start - PAST_MERIDIAN_POOL_HOURS
    return lst_start


def select_blocks(session, pid: str, lst_start: float, lst_end: float):
    """Query the not-yet-scheduled blocks of a project inside an LST window.

    Returns a query of ``(ObsBlock, BlockPar, Target)`` tuples ordered by
    descending hour angle.
    """
    query = (
        session.query(ObsBlock, BlockPar, Target)
        .join(BlockPar, ObsBlock.block_par_id == BlockPar.id)
        .join(Target, ObsBlock.target_id == Target.id)
        .filter(
            ObsBlock.pid == pid,
            BlockPar.pid == pid,
            ObsBlock.scheduled == False,  # noqa: E712
            ObsBlock.completed == False,  # noqa: E712
        )
    )
    # sky-flat blocks point at a placeholder target: exempt from the LST cut
    all_sky = BlockPar.sched_algorithm == SkyFlat.id
    if lst_start < lst_end:
        query = query.filter(
            or_(
                all_sky,
                and_(Target.target_ra > lst_start, Target.target_ra < lst_end),
            )
        )
    else:
        query = query.filter(
            or_(
                all_sky,
                and_(Target.target_ra > lst_start, Target.target_ra < 24.0),
                # >= so targets at exactly RA 0 are selectable (2018 fix from
                # the never-merged wschoenell-patch-1 branch)
                and_(Target.target_ra >= 0.0, Target.target_ra < lst_end),
            )
        )
    return query.order_by(desc(Target.target_ah))


def add_observation(session, algorithms, block_rows, obstime_jd: float) -> None:
    """Create queue programs for every ``(ObsBlock, BlockPar, Target)`` row."""
    programs = []

    for subblock in block_rows:
        obs_block, blockpar, target = subblock
        project = session.query(Project).filter(Project.pid == obs_block.pid).first()
        slew_at = obstime_jd - MJD_JD_OFFSET
        _out(f"\t @{slew_at:.3f} - {target}")
        program = Program(
            target_id=obs_block.target_id,
            name=target.name,
            # carried all the way into the PROG_PI header of every frame
            pi=project.pi,
            priority=project.priority,
            slew_at=slew_at,
            pid=obs_block.pid,
            project_id=project.id,
            obsblock_id=obs_block.id,
            blockpar_id=blockpar.id,
        )
        programs.append(program)

        algorithms[blockpar.sched_algorithm].add(subblock)

    session.add_all(programs)
    session.commit()


def cmd_make_queue(args) -> int:
    """Select targets of a project to be observed."""
    factory = _session_factory(args)
    session = factory()

    if not _require_project(session, args.pid):
        session.commit()
        return 1

    if session.query(Program).filter(Program.pid == args.pid).first() is not None:
        _out(
            "+Project already processed... Reprocessing a queue is a nasty job... "
            "Clean it and try again..."
        )
        session.commit()
        return 0

    # scheduling parameters: the project's stored ``scheduling:`` section is
    # the default; a --pid-config file overrides per key (per-night knobs)
    project_row = session.query(Project).filter(Project.pid == args.pid).one()
    pgrconfig = json.loads(project_row.scheduling) if project_row.scheduling else {}
    if args.pid_config is not None:
        try:
            pgrconfig.update(_load_yaml(args.pid_config) or {})
        except yaml.YAMLError as exc:
            _err(str(exc))
            return 1
    pgrconfig.setdefault("pid", args.pid)
    unknown = set(pgrconfig) - PID_CONFIG_KEYS
    if unknown:
        _err(f"*Unknown pid-config keys: {sorted(unknown)} ({MIGRATE_HINT})")
        return 1

    _out(f"-Selecting targets from project {args.pid}")

    bus, site_proxy = _connect(args, args.site)
    try:
        site = SiteAdapter(site_proxy)
        times = make_times(args, site, pgrconfig.get("night_boundary", "night"))
        lst_start = pool_lst_start(times.lst_start - 2.0, pgrconfig)
        lst_end = times.lst_end + 2.0
        if lst_start != times.lst_start - 2.0:
            _out(
                f"-past_meridian_only: widening the pool to LST "
                f"{lst_start:4.1f} h (already-culminated targets admitted)"
            )

        _out(
            f"-Observation start @ {str(times.obs_start)[:19]} | LST = {lst_start:4.1f} h"
        )
        _out(f"-Observation end   @ {str(times.obs_end)[:19]} | LST = {lst_end:4.1f} h")

        obs_start = times.jd_start
        obs_end = times.jd_end

        ohh = int(np.floor((obs_end - obs_start) * 24.0))
        omm = int(np.floor(((obs_end - obs_start) * 24.0 - ohh) * 60.0))
        _out(f"-Observing time: {ohh:02d}:{omm:02d} h")

        # Update the targets' hour angle for the selection ordering.
        for target in session.query(Target):
            target.lst = times.lst_start
        session.commit()

        tlist = select_blocks(session, args.pid, lst_start, lst_end)

        if len(tlist[:]) == 0:
            _out("+No targets available from this project this night...")
            session.commit()
            return 1

        _out(f"-Found {len(tlist[:])} suitable targets...")
        for row in tlist:
            _out(f" - {row[2]}")

        unique_algorithm_ids = sorted({t[1].sched_algorithm for t in tlist})

        _out(f"-Found {len(unique_algorithm_ids)} types of scheduling algorithms...")
        for i, sa_type in enumerate(unique_algorithm_ids):
            _out(f"--SA Type[{i + 1}] = {sa_type}")

        algorithms = build_algorithms(factory, site)
        for sal in unique_algorithm_ids:
            nquery = tlist.filter(BlockPar.sched_algorithm == sal)

            sched = algorithms[sal]

            obs_targets = sched.process(
                obs_start=obs_start,
                obs_end=obs_end,
                query=nquery,
                config=pgrconfig,
            )

            # First schedule all...
            for slot in obs_targets:
                if slot["blockid"] > 0:
                    oblock = nquery.filter(ObsBlock.blockid == int(slot["blockid"]))
                    add_observation(session, algorithms, oblock, float(slot["start"]))

            # ...then mark as scheduled.
            for slot in obs_targets:
                if slot["blockid"] > 0:
                    oblock = nquery.filter(ObsBlock.blockid == int(slot["blockid"]))
                    for row in oblock:
                        row[0].scheduled = True
                    session.commit()

        session.commit()
        return 0
    finally:
        bus.shutdown()


def calc_obs_time(session, program, readout_time: float = 0.0) -> float:
    """Estimated duration (seconds) of a program's observing block."""
    obs_block = (
        session.query(ObsBlock).filter(ObsBlock.id == program.obsblock_id).first()
    )
    if obs_block is None:
        return 0.0
    return block_duration(
        obs_block.actions,
        readout=readout_time,
        autofocus_sweep=INGEST_AUTOFOCUS_OVERHEAD,
        autoflat_frame=INGEST_AUTOFLAT_FRAME_OVERHEAD,
    )


def cmd_process_queue(args) -> int:
    """Process the queue like chimera would during an observation
    (offline simulation; writes 'Simulation:' entries to the observing log).

    The simulation runs against a SNAPSHOT of the database and never
    mutates the live one - see _simulation_snapshot.
    """
    live_path = _database_path(args)
    with _simulation_snapshot(live_path) as snapshot:
        factory = open_database(snapshot)
        rc = _process_queue(args, factory)
        if rc == 0:
            _copy_simulation_log(snapshot, live_path)
    return rc


@contextlib.contextmanager
def _simulation_snapshot(live_path: str):
    """A throwaway copy of the robobs database for the simulation to chew on.

    process-queue walks the night by MARKING PROGRAMS OBSERVED, then used to
    undo it with

        for program in session.query(Program).filter(Program.finished == True):
            program.finished = False

    which is not an undo: ``finished`` is also robobs' HANDOVER marker, so
    that loop un-handed every program already given to the chimera scheduler
    and made them re-offerable - the double-observation hazard. Anything
    legitimately finished earlier in the night was silently resurrected too.

    Running on a copy makes the whole question disappear: the simulation can
    write what it likes, and the live queue is untouched because it was never
    opened for writing. The database is small (~200 kB at LNA40) so the copy
    costs nothing. Only the 'Simulation:' log rows are carried back, because
    that is what plot-log --simulation reads.
    """
    fd, path = tempfile.mkstemp(prefix="robobs-sim-", suffix=".db")
    os.close(fd)
    try:
        shutil.copy(live_path, path)
        yield path
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(path + suffix)
            except OSError:
                pass


def _copy_simulation_log(snapshot: str, live_path: str) -> None:
    """Bring the simulated night back to the live database, and nothing else.

    plot-log --simulation reads these rows; every other table stays as the
    observatory left it.
    """
    live = open_database(live_path)()
    try:
        live.query(ObservingLog).filter(
            ObservingLog.action.like("Simulation:%")
        ).delete(synchronize_session=False)
        for entry in (
            open_database(snapshot)()
            .query(ObservingLog)
            .filter(ObservingLog.action.like("Simulation:%"))
        ):
            live.add(
                ObservingLog(
                    time=entry.time,
                    target_id=entry.target_id,
                    name=entry.name,
                    priority=entry.priority,
                    action=entry.action,
                )
            )
    finally:
        live.commit()


def _process_queue(args, factory) -> int:
    session = factory()

    bus, site_proxy = _connect(args, args.site)
    try:
        site = SiteAdapter(site_proxy)
        times = make_times(args, site)
        obs_start = times.jd_start - MJD_JD_OFFSET
        obs_end = times.jd_end - MJD_JD_OFFSET

        algorithms = build_algorithms(factory, site)
        engine = RobObsEngine(factory, site, log=log, algorithms=algorithms)

        # Drop the previous run's simulation entries. They are re-derived on
        # every run, and leaving them made plot-log --simulation draw the old
        # plan on top of the new one: a rebuilt night showed 9 focus runs for
        # 6 actually planned. Only 'Simulation:' rows go - real observations
        # are logged under different actions and are history.
        purged = (
            session.query(ObservingLog)
            .filter(ObservingLog.action.like("Simulation:%"))
            .delete(synchronize_session=False)
        )
        session.commit()
        if purged:
            log.debug("dropped %i stale simulation log entries", purged)

        # Twilight calibration programs (sky flats) live outside the -18 deg
        # night, so the simulation clock is widened to cover them - but only
        # by SIM_NIGHT_MARGIN, never to an arbitrary queue entry.
        #
        # Unbounded, `min(slew_at)` reaches into PREVIOUS nights: any queue
        # left unfinished from an earlier night drags the simulation back to
        # it, and the whole phantom night is walked before tonight is ever
        # reached. On opd-40 2026-07-30 a stale RUP147 program from 07-29
        # 22:15 started the simulation ~24 h early; EXOPL was consumed
        # against that dead night and vanished from the plan, while the real
        # scheduler offered it happily all night. The plan the operator sees
        # then disagrees with what the telescope actually does, which is
        # worse than a plan that is merely incomplete.
        slew_range = (
            session.query(
                sqla_func.min(Program.slew_at), sqla_func.max(Program.slew_at)
            )
            .filter(Program.finished == False)  # noqa: E712
            .filter(Program.slew_at >= obs_start - SIM_NIGHT_MARGIN)
            .filter(Program.slew_at <= obs_end + SIM_NIGHT_MARGIN)
            .one()
        )
        otime = obs_start
        sim_end = obs_end
        if slew_range[0] is not None:
            otime = max(
                min(obs_start, float(slew_range[0])), obs_start - SIM_NIGHT_MARGIN
            )
            sim_end = min(
                max(obs_end, float(slew_range[1]) + 1800.0 / SECONDS_PER_DAY),
                obs_end + SIM_NIGHT_MARGIN,
            )
        app_open = 0.0
        idle = 0.0

        tel_pos = None  # current telescope position

        while otime < sim_end:
            _out(f"Requesting target @ {otime:f}")
            program_list = engine.reschedule(otime)
            if not program_list:
                break
            program = session.merge(program_list[0])
            _out(f"slew@: {program.slew_at}")

            # prefer the block length stored at ingest (recovered 2018 fix
            # from the never-merged bugfix/block_length branch)
            obs_block = session.merge(program_list[2])
            aplen = obs_block.length or calc_obs_time(session, program, 20.0)

            msg = ""
            slew_at = float(program.slew_at)
            _idle = slew_at - otime
            stime = otime
            if _idle > 1e-5:
                msg += "[info: Program slew %.3fm in the future. waiting...]" % (
                    _idle * 24.0 * 60.0
                )
                idle += _idle
                stime += _idle
            elif _idle < -1e-5:
                msg += "[info: Program slew %.3fm in the past. Slewing now...]" % (
                    _idle * 24.0 * 60.0
                )

            _idle = _idle if _idle > 0 else 0.0
            slewtime = 0.0
            target = (
                session.query(Target).filter(Target.id == program.target_id).first()
            )

            target_pos = Position.from_ra_dec(target.target_ra, target.target_dec)
            if tel_pos:
                adist = tel_pos.angsep(target_pos)
                # consider 1 arcmin / second
                slewtime = float(adist.to_as()) / 60.0 / 60.0 / SECONDS_PER_DAY
            # if slewtime larger than idle time, slewtime will be zero
            slewtime = slewtime if slewtime > _idle else 0
            msg += " | slewtime = %.5fm" % (slewtime * 60.0 * 60.0)
            _out(
                f"@ {otime:.5f} ({slew_at:.5f}): "
                f"Acquiring {program!s:>45} {msg} (len: {aplen:.2f})"
            )
            session.add(
                ObservingLog(
                    time=datetime_from_mjd(stime).replace(tzinfo=None),
                    target_id=program.target_id,
                    name=program.name,
                    priority=program.priority,
                    action="Simulation: Acquisition Start",
                )
            )
            session.commit()

            session.add(
                ObservingLog(
                    time=datetime_from_mjd(stime + aplen / SECONDS_PER_DAY).replace(
                        tzinfo=None
                    ),
                    target_id=program.target_id,
                    name=program.name,
                    priority=program.priority,
                    action="Simulation: Acquisition End",
                )
            )

            otime += (aplen / SECONDS_PER_DAY) + slewtime + _idle

            app_open += aplen
            program.finished = True
            session.commit()

            blockpar = session.merge(program_list[1])
            _out(
                f"{blockpar}: {blockpar.sched_algorithm} "
                f"{algorithms[blockpar.sched_algorithm].name}"
            )

            algorithms[blockpar.sched_algorithm].observed(
                otime, program_list, soft=True
            )
            tel_pos = target_pos
            session.commit()

        # No bookkeeping to undo: this whole run happened on a throwaway
        # snapshot (see _simulation_snapshot), which is deleted on the way
        # out. The old reset walked the LIVE database setting finished =
        # False on every finished program - and finished is also the
        # handover marker, so it un-handed everything already given to the
        # chimera scheduler and made it re-offerable.
        session.commit()

        if otime < obs_end:
            _out(f"@ {otime:.4f}: Idle for {(obs_end - otime) * 24.0:.2f}h")
            idle += obs_end - otime
        _out(f"@ {obs_end:.4f}: Night end")
        _out("-Total idle time: %.2fh" % (idle * 24.0))
        _out("-Total open shutter time: %.2fh" % (app_open / 60.0 / 60.0))
        return 0
    finally:
        bus.shutdown()


# ----------------------------------------------------------------------
# online commands (talk to the RobObs controller)
# ----------------------------------------------------------------------


def _client_bus(args):
    import threading
    import time

    from chimera.core.bus import Bus

    bus = Bus(f"tcp://{args.host}:{random.randint(10000, 60000)}")
    # the client bus must run its receive loop, or replies never arrive
    threading.Thread(target=bus.run_forever, daemon=True).start()
    started = getattr(bus, "_bus_started", None)
    if started is not None:
        started.wait(5)
    else:
        time.sleep(0.5)
    return bus


def _resolve_proxy(bus, args, location: str):
    from chimera.core.proxy import Proxy

    url = f"tcp://{args.host}:{args.port}{location}"
    # every CLI call here is short (ephemeris lookups, start/stop): bound
    # them so a lost bus response fails loudly instead of hanging the CLI
    # forever (which blocks the supervisor's run_script when scripted).
    # older chimera cores don't take the timeout kwarg - fall back cleanly.
    try:
        proxy = Proxy(url, bus, timeout=60.0)
    except TypeError:
        proxy = Proxy(url, bus)
    proxy.resolve()
    return proxy


def _connect(args, location: str):
    bus = _client_bus(args)
    return bus, _resolve_proxy(bus, args, location)


def _online(args, call) -> int:
    bus = None
    try:
        bus, proxy = _connect(args, args.robobs)
        return call(proxy) or 0
    except Exception as e:
        _err(
            f"error: could not talk to robobs at {args.host}:{args.port}{args.robobs}: {e}"
        )
        return 1
    finally:
        if bus is not None:
            bus.shutdown()


def cmd_start(args) -> int:
    def call(proxy):
        _out("Starting robobs...")
        _out("OK" if proxy.start() else "FAILED")

    return _online(args, call)


def cmd_stop(args) -> int:
    def call(proxy):
        _out("Stopping robobs...")
        _out("OK" if proxy.stop() else "FAILED")

    return _online(args, call)


def cmd_wake(args) -> int:
    return _online(args, lambda proxy: proxy.wake() and None)


def cmd_monitor(args) -> int:
    return _online(args, lambda proxy: _out(str(proxy.state())))


# ----------------------------------------------------------------------
# entry point
# ----------------------------------------------------------------------


def _add_time_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--tonight",
        action="store_true",
        help="use the CURRENT night (from now, if already dark) instead of "
        "the night after today's evening twilight - after UTC midnight the "
        "default jumps to the next night",
    )
    parser.add_argument(
        "--jd-start",
        type=float,
        help="Julian date of the start of the observations. Overrides the date.",
    )
    parser.add_argument(
        "--jd-end",
        type=float,
        help="Julian date of the end of the observations. Overrides the date.",
    )
    parser.add_argument(
        "--date-start",
        help="Date (yyyy/mm/dd-hh:mm:ss or ISO) of the start of the observations.",
    )
    parser.add_argument(
        "--date-end",
        help="Date (yyyy/mm/dd-hh:mm:ss or ISO) of the end of the observations.",
    )
    parser.add_argument(
        "--lst-start",
        type=float,
        help="Overwrite the LST target selection cut (hours).",
    )
    parser.add_argument(
        "--lst-end",
        type=float,
        help="Overwrite the LST target selection cut (hours).",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="chimera-robobs",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--database",
        default=None,
        help=f"robobs database path (default: {DEFAULT_ROBOBS_DATABASE})",
    )
    parser.add_argument("--host", default="127.0.0.1", help="chimera server host")
    parser.add_argument("--port", type=int, default=6379, help="chimera server port")
    parser.add_argument(
        "--robobs", default="/RobObs/0", help="robobs controller location"
    )
    parser.add_argument("--site", default="/Site/0", help="site location")

    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("add-project", help="add a project (YAML) to the database")
    p.add_argument("-f", "--file", dest="filename", required=True)
    p.set_defaults(func=cmd_add_project)

    p = sub.add_parser(
        "add-project-inputs",
        help="ingest a project end to end: project YAML + targets CSV + one "
        "block template (one block per target); no block-list file needed",
    )
    p.add_argument("-p", "--project", required=True, help="project YAML")
    p.add_argument("-t", "--targets", required=True, help="targets CSV")
    p.add_argument("-b", "--block", required=True, help="block-template YAML")
    p.add_argument(
        "--blockpar-bid",
        type=int,
        default=None,
        help="block-parameter id to attach (default: the project's only one)",
    )
    _add_overhead_options(p)
    p.set_defaults(func=cmd_add_project_inputs)

    p = sub.add_parser("delete-project", help="delete a project from the database")
    p.add_argument("--pid", required=True)
    p.set_defaults(func=cmd_delete_project)

    p = sub.add_parser("clean-project", help="delete all projects/blocks")
    p.set_defaults(func=cmd_clean_project)

    p = sub.add_parser("add-targets", help="add targets from a CSV file")
    p.add_argument("-f", "--file", dest="filename", required=True)
    p.add_argument(
        "--match-by-name",
        action="store_true",
        help="reuse an existing target of the same name instead of appending "
        "a duplicate, keeping its row id (and so the observing log's "
        "references) stable. Only for generated inputs whose names are "
        "unique by construction, like the OPOP occultations; the default "
        "stays append-only because names are not unique in general.",
    )
    p.set_defaults(func=cmd_add_targets)

    p = sub.add_parser(
        "clean-targets", help="delete all targets, or only those named in a CSV"
    )
    p.add_argument(
        "--names-from",
        default=None,
        help="delete only the targets whose NAME appears in this CSV "
        "(default: delete every target)",
    )
    p.set_defaults(func=cmd_clean_targets)

    p = sub.add_parser(
        "add-observing-block", help="add observing block definitions from a file"
    )
    p.add_argument("-f", "--file", dest="filename", required=True)
    p.add_argument(
        "--by-name",
        action="store_true",
        help="the target column is a NAME (resolved against the database), "
        "not a row id - use for per-object lists like the OPOP occultations",
    )
    p.add_argument(
        "--block-dir",
        default=None,
        help="rewrite each block-YAML path to this directory's copy "
        "(the generated lists embed the generating machine's paths)",
    )
    _add_overhead_options(p)
    p.set_defaults(func=cmd_add_observing_block)

    p = sub.add_parser("clean-observing-blocks", help="delete all observing blocks")
    p.set_defaults(func=cmd_clean_observing_blocks)

    p = sub.add_parser(
        "delete-observing-block", help="delete the observing blocks of a project"
    )
    p.add_argument("--pid", required=True)
    p.set_defaults(func=cmd_delete_observing_block)

    p = sub.add_parser("make-queue", help="build the observing queue for a project")
    p.add_argument("--pid", required=True)
    p.add_argument(
        "--pid-config",
        default=None,
        help="project (YAML) configuration passed to the scheduling algorithm",
    )
    _add_time_options(p)
    p.set_defaults(func=cmd_make_queue)

    p = sub.add_parser("clean-queue", help="delete the scheduled queue of a project")
    p.add_argument("--pid", required=True)
    p.set_defaults(func=cmd_clean_queue)

    p = sub.add_parser(
        "process-queue", help="simulate the queue execution (offline simulation)"
    )
    _add_time_options(p)
    p.set_defaults(func=cmd_process_queue)

    p = sub.add_parser(
        "status",
        help="show the current state of the night (robobs, scheduler, "
        "queue, observing log)",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="dump the raw RobObs.status() snapshot as JSON",
    )
    p.add_argument(
        "--watch",
        nargs="?",
        type=float,
        const=5.0,
        default=None,
        metavar="SECONDS",
        help="redraw the panel every SECONDS (default 5) until interrupted",
    )
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("observing-log", help="show the observing log")
    p.add_argument("--start", default=None, help="only entries after this time")
    p.add_argument("--end", default=None, help="only entries up to this time")
    p.set_defaults(func=cmd_observing_log)

    p = sub.add_parser(
        "plot-log",
        help="plot the observing-log altitude chart (needs matplotlib)",
    )
    p.add_argument(
        "-f",
        "--file",
        dest="filename",
        default="obsplan.png",
        help="output image file (default: obsplan.png)",
    )
    p.add_argument(
        "--simulation",
        "--sim",
        action="store_true",
        help="plot 'Simulation:' entries (from process-queue) instead of "
        "observed programs",
    )
    _add_time_options(p)
    p.set_defaults(func=cmd_plot_log)

    for name, func, doc in (
        ("start", cmd_start, "switch the robobs controller on"),
        ("stop", cmd_stop, "switch the robobs controller off"),
        ("wake", cmd_wake, "wake the robobs machine up"),
        ("monitor", cmd_monitor, "show the robobs controller state"),
    ):
        p = sub.add_parser(name, help=doc)
        p.set_defaults(func=func)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
