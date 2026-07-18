# chimera-robobs

Robotic observation scheduler (**robobs**) for the
[chimera](https://github.com/astroufsc/chimera) observatory control system.

robobs sits on top of chimera's own scheduler: it keeps its own database of
*projects*, *targets*, *observing blocks* and per-block observing constraints
(airmass, moon distance/brightness, seeing) plus a queue of *programs* built
by pluggable scheduling algorithms (higher-in-the-sky, extinction monitor,
timed, recurrent, time-sequence).  The `RobObs` chimera controller watches
the chimera scheduler; whenever the scheduler goes idle it re-schedules,
picks the best program for the current conditions, converts it into a chimera
scheduler program (with its point/expose/autofocus actions) and wakes the
scheduler up again.

This package was extracted from
[chimera-supervisor](https://github.com/astroufsc/chimera-supervisor) 2.0 and
ported from Python 2 / old chimera to Python 3.13 / chimera 0.2 (see
`docs/robobs-port-notes.md` for every deliberate change and fixed bug, and
`docs/plans/deep-refactor.md` for the subsequent cleanup).  It **needs
on-sky validation before production use**.

## Installation

```
pip install -U git+https://github.com/astroufsc/chimera-robobs.git
```

## Configuration Example

Add to your chimera configuration:

```yaml
controllers:
  - type: RobObs
    name: robobs
    site: /Site/0
    schedulers: /Scheduler/0
    # database: ~/.chimera/robobs.db     (default)
```

## Command line

Offline (operate directly on the robobs database, default
`~/.chimera/robobs.db`, override with `--database`):

```
chimera-robobs add-project -f project.yaml
chimera-robobs add-targets -f targets.csv
chimera-robobs add-observing-block -f blocks.txt
chimera-robobs make-queue --pid PID [--pid-config config.yaml]
chimera-robobs process-queue            # offline simulation of a night
chimera-robobs observing-log [--start 2026/07/06-18:00:00]
chimera-robobs clean-queue --pid PID
chimera-robobs delete-project --pid PID
```

Controller control (needs a running chimera server):

```
chimera-robobs [--host H --port P --robobs /RobObs/0] start | stop | wake | monitor
```

The project YAML (`project:` + `observing_blocks:`), block YAML
(`pre-actions`/`pos-actions`) and targets CSV formats are unchanged from the
legacy tool; legacy key names (`maxairmass`, `imageType`, `EPOC`, ...) are
still accepted.  The legacy pid-config key `past_meridian_only` is parsed
but **not implemented** (as in the port; a warning is printed).

## Development

```
uv sync
uv run pre-commit install --install-hooks
uv run pytest -q
uv run ruff check src tests
```

## License

GPL-2.0-or-later

## Contact

- chimera discussion list: https://groups.google.com/g/chimera-discuss
- https://github.com/astroufsc/chimera-robobs
