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

Commands that talk to a running chimera server:

    chimera-robobs make-queue --pid PID [--pid-config file.yaml] [time options]
    chimera-robobs process-queue [time options]        (offline simulation)
    chimera-robobs start | stop | wake | monitor

The YAML/CSV input formats are compatible with the legacy chimera-robobs
tool; legacy key names (``maxairmass``, ``imageType``, ...) are accepted and
mapped to the new database column names.
"""

import argparse
import datetime as dt
import logging
import math
import os
import random
import shutil
import sys
import time
from types import SimpleNamespace

import numpy as np
import yaml
from chimera.util.coord import Coord
from chimera.util.position import Position
from sqlalchemy import and_, desc, or_

from chimera_robobs.scheduling.algorithms import build_algorithms
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

#: legacy YAML block-parameter keys -> new column names (new names also accepted)
LEGACY_BLOCKPAR_KEYS = {
    "maxairmass": "max_airmass",
    "minairmass": "min_airmass",
    "maxmoonBright": "max_moon_bright",
    "minmoonBright": "min_moon_bright",
    "minmoonDist": "min_moon_distance",
    "maxseeing": "max_seeing",
    "cloudcover": "cloud_cover",
    "schedalgorith": "sched_algorithm",
    "applyextcorr": "apply_ext_corr",
}

BLOCKPAR_FIELDS = set(LEGACY_BLOCKPAR_KEYS.values())

#: legacy YAML action keys -> new column names
LEGACY_ACTION_KEYS = {
    "imageType": "image_type",
    "objectName": "object_name",
    "targetName": "target_name",
}

#: legacy CSV column names -> Target columns
TARGET_CSV_COLUMNS = {
    "name": "name",
    "type": "type",
    "mag": "target_mag",
    "epoch": "target_epoch",
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

    _out("-Reading observing block information...")

    if "observing_blocks" in config:
        _out(f"--Found {len(config['observing_blocks'])} blocks.")
        for observing_block in config["observing_blocks"]:
            block_config = config["observing_blocks"][observing_block]
            b_id = block_config["id"]
            b_pid = block_config["pid"]
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
                if key in ("id", "pid"):
                    continue
                column = LEGACY_BLOCKPAR_KEYS.get(key, key)
                if column in BLOCKPAR_FIELDS:
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


def add_targets_from_table(session, targets_table) -> int:
    """Add targets from an astropy table (CSV) to the database.

    Returns the number of targets added.  Does not check for duplicates.
    """
    columns = {name.lower(): name for name in targets_table.dtype.names}

    for required in ("ra", "dec"):
        if required not in columns:
            raise ValueError(
                f"Required parameter, {required}, missing from input file..."
            )

    nadded = 0
    for i in range(len(targets_table)):
        ra = targets_table[columns["ra"]][i]
        dec = targets_table[columns["dec"]][i]
        try:
            position = Position.from_ra_dec(str(ra), str(dec))
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
                    tpar[column] = float(value)
                else:
                    tpar[column] = str(value)

        target = Target(**tpar)
        _out(f"--Adding {target.name}...")
        session.add(target)
        session.commit()
        nadded += 1

    return nadded


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
        add_targets_from_table(session, targets_table)
    except ValueError as e:
        _err(f"*{e}")
        return 1

    _out("-Done")
    return 0


def cmd_clean_targets(args) -> int:
    """Delete all targets from the database."""
    backup_database(args)

    session = _session_factory(args)()

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
    """Template context for string action parameters ({name}, {pid}, ...).

    Includes both the new snake_case column names and the legacy camelCase
    aliases so old block configuration files keep working.
    """
    ctx = {}
    for obj in (target, block):
        for column in obj.__table__.columns:
            try:
                ctx[column.key] = getattr(obj, column.key)
            except Exception:
                continue
    aliases = {
        "targetRa": "target_ra",
        "targetDec": "target_dec",
        "targetEpoch": "target_epoch",
        "targetMag": "target_mag",
        "magFilter": "mag_filter",
        "lastObservation": "last_observation",
        "objid": "target_id",
        "bparid": "block_par_id",
    }
    for legacy, new in aliases.items():
        if new in ctx:
            ctx[legacy] = ctx[new]
    return ctx


def _apply_action_config(act, actconfig, ctx) -> None:
    """Set plain action attributes from an action configuration mapping."""
    for key, value in actconfig.items():
        if key == "action":
            continue
        attr = LEGACY_ACTION_KEYS.get(key, key)
        if not hasattr(act, attr):
            continue
        if isinstance(value, str):
            value = value.format(**ctx)
        try:
            setattr(act, attr, value)
        except Exception:
            _err(
                "Could not set attribute {} = {} on action {}".format(
                    attr, actconfig[key], actconfig.get("action")
                )
            )


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
#: length, hard-coded as in the legacy tool
INGEST_READOUT_OVERHEAD = 12.0
INGEST_AUTOFOCUS_OVERHEAD = 600.0


def add_observing_block(session, row, config) -> ObsBlock | None:
    """Add (or replace) one observing block from a block-list row.

    ``row`` is ``(pid, blockid, target_id, config_filename, blockpar_bid)``
    and ``config`` the parsed block YAML (with ``pre-actions``/``pos-actions``).
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

    # process pre-slew actions
    for actconfig in config.get("pre-actions", []):
        act = _make_action(actconfig, target, addblock, with_offsets=False)
        addblock.actions.append(act)

    # slew to target
    position = Position.from_ra_dec(target.target_ra, target.target_dec, "J2000")
    slewto = Point()
    slewto.target_ra_dec = position
    addblock.actions.append(slewto)
    _out(f"Slew to: {target.name} ({slewto})")

    # process post-slew actions
    post_actions = []
    for actconfig in config.get("pos-actions", []):
        act = _make_action(actconfig, target, addblock, with_offsets=True)
        post_actions.append(act)
        addblock.actions.append(act)

    # only post-slew actions count towards the stored block length
    addblock.length = block_duration(
        post_actions,
        readout=INGEST_READOUT_OVERHEAD,
        autofocus_sweep=INGEST_AUTOFOCUS_OVERHEAD,
    )
    session.add(addblock)
    session.commit()
    return addblock


def cmd_add_observing_block(args) -> int:
    """Add observing block definitions to the database."""
    from astropy.table import Table

    if not args.filename:
        _err("*Input not given. Use '-f'...")
        return 1

    _out(f"-Reading observing blocks from {args.filename}")

    block_list = Table.read(args.filename, format="ascii.no_header")

    backup_database(args)
    session = _session_factory(args)()

    for entry in block_list:
        raw = list(entry)
        row = (str(raw[0]), int(raw[1]), int(raw[2]), str(raw[3]), int(raw[4]))
        try:
            config = _load_yaml(row[3])
        except yaml.YAMLError as exc:
            _err(str(exc))
            return 1
        try:
            add_observing_block(session, row, config)
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


def make_times(args, site: SiteAdapter) -> SimpleNamespace:
    """Determine the start/end times of the night (legacy ``mktimes``)."""
    obs_start = site.sunset_twilight_end()
    obs_end = site.sunrise_twilight_begin(obs_start)

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
    if lst_start < lst_end:
        query = query.filter(Target.target_ra > lst_start, Target.target_ra < lst_end)
    else:
        query = query.filter(
            or_(
                and_(Target.target_ra > lst_start, Target.target_ra < 24.0),
                and_(Target.target_ra > 0.0, Target.target_ra < lst_end),
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
            pi="",
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

    pgrconfig = {}
    if args.pid_config is not None:
        try:
            pgrconfig = _load_yaml(args.pid_config)
        except yaml.YAMLError as exc:
            _err(str(exc))
            return 1
    pgrconfig.setdefault("pid", args.pid)

    _out(f"-Selecting targets from project {args.pid}")

    bus, site_proxy = _connect(args, args.site)
    try:
        site = SiteAdapter(site_proxy)
        times = make_times(args, site)
        lst_start = times.lst_start - 2.0
        lst_end = times.lst_end + 2.0

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
    )


def cmd_process_queue(args) -> int:
    """Process the queue like chimera would during an observation
    (offline simulation; writes 'Simulation:' entries to the observing log)."""
    factory = _session_factory(args)
    session = factory()

    bus, site_proxy = _connect(args, args.site)
    try:
        site = SiteAdapter(site_proxy)
        times = make_times(args, site)
        obs_start = times.jd_start - MJD_JD_OFFSET
        obs_end = times.jd_end - MJD_JD_OFFSET

        algorithms = build_algorithms(factory, site)
        engine = RobObsEngine(factory, site, log=log, algorithms=algorithms)

        otime = obs_start
        app_open = 0.0
        idle = 0.0

        tel_pos = None  # current telescope position

        while otime < obs_end:
            _out(f"Requesting target @ {otime:f}")
            program_list = engine.reschedule(otime)
            if not program_list:
                break
            program = session.merge(program_list[0])
            _out(f"slew@: {program.slew_at}")

            aplen = calc_obs_time(session, program, 20.0)

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

        # reset the simulation bookkeeping
        for program in session.query(Program).filter(Program.finished == True):  # noqa: E712
            program.finished = False

        allpid = [p.pid for p in session.query(Project)]

        session.commit()
        for sched in algorithms.values():
            for pid in allpid:
                sched.soft_clean(pid)

        session.commit()

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


def _connect(args, location: str):
    import threading
    import time

    from chimera.core.bus import Bus
    from chimera.core.proxy import Proxy

    bus = Bus(f"tcp://{args.host}:{random.randint(10000, 60000)}")
    # the client bus must run its receive loop, or replies never arrive
    threading.Thread(target=bus.run_forever, daemon=True).start()
    started = getattr(bus, "_bus_started", None)
    if started is not None:
        started.wait(5)
    else:
        time.sleep(0.5)
    url = f"tcp://{args.host}:{args.port}{location}"
    proxy = Proxy(url, bus)
    proxy.resolve()
    return bus, proxy


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

    p = sub.add_parser("delete-project", help="delete a project from the database")
    p.add_argument("--pid", required=True)
    p.set_defaults(func=cmd_delete_project)

    p = sub.add_parser("clean-project", help="delete all projects/blocks")
    p.set_defaults(func=cmd_clean_project)

    p = sub.add_parser("add-targets", help="add targets from a CSV file")
    p.add_argument("-f", "--file", dest="filename", required=True)
    p.set_defaults(func=cmd_add_targets)

    p = sub.add_parser("clean-targets", help="delete all targets")
    p.set_defaults(func=cmd_clean_targets)

    p = sub.add_parser(
        "add-observing-block", help="add observing block definitions from a file"
    )
    p.add_argument("-f", "--file", dest="filename", required=True)
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

    p = sub.add_parser("observing-log", help="show the observing log")
    p.add_argument("--start", default=None, help="only entries after this time")
    p.add_argument("--end", default=None, help="only entries up to this time")
    p.set_defaults(func=cmd_observing_log)

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
