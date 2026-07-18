# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2014-present chimera-robobs authors

"""Template-compliance smoke tests: hardware/bus-free import and
instantiation, and the console entry point."""

from importlib.metadata import entry_points


def test_import_and_instantiate_controller():
    from chimera_robobs.controllers.robobs import RobObs

    controller = RobObs()  # no bus, no database, no hardware
    assert controller["site"] == "/Site/0"
    assert controller["schedulers"] == "/Scheduler/0"
    assert controller["database"] is None


def test_console_entry_point_resolves():
    (script,) = entry_points(group="console_scripts", name="chimera-robobs")
    assert script.load().__name__ == "main"
