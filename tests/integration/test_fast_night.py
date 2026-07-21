# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2014-present chimera-robobs authors

"""Run the compressed-night integration script in a clean subprocess.

The script boots a full chimera stack (bus/manager/scheduler/robobs) and
must therefore live in its own process: it needs an isolated ``HOME``
*before* any chimera import, and the bus is known to hang the interpreter
at exit (the script os._exits).  See fast_night.py for what is asserted.
"""

import os
import subprocess
import sys

SCRIPT = os.path.join(os.path.dirname(__file__), "fast_night.py")


def test_compressed_night():
    result = subprocess.run(
        [sys.executable, SCRIPT],
        capture_output=True,
        text=True,
        timeout=300,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, f"fast night failed:\n{output}"
    assert "ALL PASS" in result.stdout
