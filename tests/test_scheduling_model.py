# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2014-present chimera-robobs authors

import datetime as dt
import os

import pytest

from chimera_robobs.scheduling import model
from chimera_robobs.scheduling.dates import (
    datetime_from_jd,
    jd_from_datetime,
    to_ephem_date,
)


@pytest.fixture
def session_factory(tmp_path):
    return model.open_database(str(tmp_path / "robobs.db"))


def _populate(session):
    project = model.Project(pid="P01", pi="PI", abstract="", url="", priority=1)
    target = model.Target(name="NGC0001", target_ra=10.0, target_dec=-20.0)
    session.add_all([project, target])
    session.commit()

    blockpar = model.BlockPar(bid=1, pid="P01")
    blockpar.max_airmass = 1.8
    blockpar.min_moon_distance = 30.0
    blockpar.sched_algorithm = 3
    session.add(blockpar)
    session.commit()

    block = model.ObsBlock(
        target_id=target.id, blockid=1, pid="P01", block_par_id=blockpar.id
    )
    block.actions.append(model.Point(target_name="NGC0001"))
    block.actions.append(
        model.Expose(
            filter="R", frames=2, exptime=1.5, image_type="OBJECT", object_name="x"
        )
    )
    block.actions.append(model.AutoFocus(start=100, end=200, step=10))
    session.add(block)
    session.commit()

    program = model.Program(
        target_id=target.id,
        name=target.name,
        pi="PI",
        priority=1,
        slew_at=61000.5,
        pid="P01",
        project_id=project.id,
        obsblock_id=block.id,
        blockpar_id=blockpar.id,
    )
    session.add(program)
    session.commit()
    return project, target, blockpar, block, program


def test_open_database_creates_file_and_schema(tmp_path):
    path = tmp_path / "sub" / "robobs.db"
    factory = model.open_database(str(path))
    assert os.path.exists(path)
    session = factory()
    assert session.query(model.Target).count() == 0


def test_default_database_constant():
    assert model.DEFAULT_ROBOBS_DATABASE.endswith("robobs.db")
    assert ".chimera" in model.DEFAULT_ROBOBS_DATABASE


def test_relationships_round_trip(session_factory):
    session = session_factory()
    project, target, blockpar, block, program = _populate(session)

    # fresh session: query everything back through the foreign keys
    session = session_factory()
    prog, bp, ob, tg = (
        session.query(model.Program, model.BlockPar, model.ObsBlock, model.Target)
        .join(model.BlockPar, model.Program.blockpar_id == model.BlockPar.id)
        .join(model.ObsBlock, model.Program.obsblock_id == model.ObsBlock.id)
        .join(model.Target, model.Program.target_id == model.Target.id)
        .one()
    )
    assert tg.name == "NGC0001"
    assert bp.max_airmass == 1.8
    assert bp.sched_algorithm == 3
    assert ob.block_par_id == bp.id
    assert prog.project_id is not None
    assert len(ob.actions) == 3
    # exptime must survive as a float (legacy schema truncated it to int)
    assert ob.actions[1].exptime == 1.5
    # polymorphic round trip
    assert ob.actions[0].action_type == "Point"
    assert ob.actions[2].action_type == "AutoFocus"
    # __str__ of everything is exercised (legacy Project.__str__ crashed)
    for obj in (prog, bp, ob, tg, project) + tuple(ob.actions):
        assert str(obj)


def test_targets_lst_hybrid_property(session_factory):
    session = session_factory()
    target = model.Target(name="t", target_ra=10.0, target_dec=0.0)
    session.add(target)
    target.lst = 12.0
    assert target.target_ah == pytest.approx(2.0)
    target.lst = 23.0  # ah = 13 -> wraps to -11
    assert target.target_ah == pytest.approx(-11.0)


def test_chimera_program_conversion(session_factory):
    from chimera.controllers.scheduler.model import (
        Expose as CExpose,
    )
    from chimera.controllers.scheduler.model import (
        Point as CPoint,
    )
    from chimera.controllers.scheduler.model import (
        Program as CProgram,
    )

    session = session_factory()
    _, target, blockpar, block, program = _populate(session)

    cprogram = program.chimera_program()
    for act in block.actions:
        cprogram.actions.append(act.chimera_action())

    assert isinstance(cprogram, CProgram)
    assert cprogram.tid == target.id
    assert cprogram.name == "NGC0001"
    # negated: robobs runs lowest-number first, chimera's sequential
    # scheduler runs highest first - a verbatim copy inverted the night order
    assert cprogram.priority == -1
    assert cprogram.start_at == 61000.5
    assert len(cprogram.actions) == 3
    assert isinstance(cprogram.actions[0], CPoint)
    assert cprogram.actions[0].target_name == "NGC0001"
    assert isinstance(cprogram.actions[1], CExpose)
    assert cprogram.actions[1].exptime == 1.5
    assert cprogram.actions[1].image_type == "OBJECT"
    assert cprogram.actions[1].object_name == "x"
    assert cprogram.actions[2].start == 100


def test_dates_helpers():
    date = dt.datetime(2026, 7, 6, 12, 0, 0, tzinfo=dt.UTC)
    jd = jd_from_datetime(date)
    assert datetime_from_jd(jd) == date
    # JD 2440587.5 is the unix epoch
    assert jd_from_datetime(dt.datetime(1970, 1, 1)) == 2440587.5
    assert to_ephem_date(date) == "2026/07/06 12:00:00"


def test_block_duration_overhead_profiles():
    actions = [
        model.Point(target_name="x"),
        model.Expose(frames=2, exptime=30.0),
        model.AutoFocus(start=100, end=200, step=10),  # sweep
        model.AutoFocus(start=0, end=0, step=0),  # T80S "set position" sentinel
        model.AutoFocus(start=0, end=0, step=-1),  # T80S "align" no-op
    ]
    # engine profile: exposures only, no overheads
    assert model.block_duration(actions) == pytest.approx(60.0)
    # CLI ingest profile: 12 s readout + 600 s focus sweep
    assert model.block_duration(
        actions, readout=12.0, autofocus_sweep=600.0
    ) == pytest.approx(2 * (30.0 + 12.0) + 600.0)
    # extinction-monitor profile: config-driven align/set overheads
    assert model.block_duration(
        actions, readout=1.0, autofocus_sweep=100.0, autofocus_set=7.0
    ) == pytest.approx(2 * 31.0 + 100.0 + 7.0)
    assert model.block_duration([]) == 0.0


def test_expose_compress_format_and_wait_dome_round_trip(session_factory):
    session = session_factory()
    expose = model.Expose(
        frames=1, exptime=1.0, compress_format="fits_rice", wait_dome=False
    )
    session.add(expose)
    session.commit()

    session = session_factory()
    stored = session.query(model.Expose).one()
    assert stored.compress_format == "fits_rice"
    assert stored.wait_dome is False
    chimera_expose = stored.chimera_action()
    assert chimera_expose.compress_format == "fits_rice"
    assert chimera_expose.wait_dome is False


def test_mjd_helpers():
    from chimera_robobs.scheduling.dates import (
        MJD_JD_OFFSET,
        datetime_from_mjd,
        mjd_from_datetime,
    )

    # MJD 0 is 1858-11-17T00:00:00 UTC
    assert datetime_from_mjd(0.0) == dt.datetime(1858, 11, 17, tzinfo=dt.UTC)
    date = dt.datetime(2026, 7, 6, 12, 0, 0, tzinfo=dt.UTC)
    assert datetime_from_mjd(mjd_from_datetime(date)) == date
    assert jd_from_datetime(date) - mjd_from_datetime(date) == MJD_JD_OFFSET


def test_created_at_default_is_call_time(session_factory):
    import time as time_module

    session = session_factory()
    first = model.Program(name="a")
    session.add(first)
    session.commit()
    time_module.sleep(0.02)
    second = model.Program(name="b")
    session.add(second)
    session.commit()
    # legacy default=datetime.today() was evaluated once at import time
    assert second.created_at > first.created_at


def test_blockpar_str_with_fractional_cloud_cover():
    # the production project files carry cloudcover: 0.8 (a fraction); the
    # legacy :2d format crashed on it (seen live on the zwo-nuc deployment)
    blockpar = model.BlockPar(bid=1, pid="P01", max_airmass=2.5, max_seeing=2.0)
    blockpar.cloud_cover = 0.8
    blockpar.sched_algorithm = 3
    assert "0.8" in str(blockpar)
