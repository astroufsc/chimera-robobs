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
    telescope: /Telescope/0        # tracking stopped after each program
    # database: ~/.chimera/robobs.db       (default)
    # clean_scheduler_on_start: true       (wipe stale scheduler queue)
```

Safety behavior: programs are only considered while it is astronomically
night (daytime evaluation is rejected outright), the telescope tracking is
stopped after every finished program so the mount never tracks into a
limit, and switching robobs on wipes stale programs left in the chimera
scheduler queue by a previous run.

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
chimera-robobs plot-log [-f obsplan.png] [--simulation]
```

Controller control (needs a running chimera server):

```
chimera-robobs [--host H --port P --robobs /RobObs/0] start | stop | wake | monitor
```

## Input files

The canonical YAML dialect is snake_case with readable algorithm names
(mirroring the chimera-supervisor 2.0 configuration style).  Project files:

```yaml
project:
  pid: ETACAR
  pi: A. Investigator
  abstract: Eta-carinae long term observations
  url: www.lna.br
  priority: 20
observing_blocks:
  block 1:
    id: 0
    max_airmass: 3.0
    min_moon_distance: 40.0
    max_seeing: 1.5
    scheduling_algorithm: recurrent   # higher | extinction_monitor | timed
                                      # | recurrent | time_sequence
```

Block files use `pre_actions:` (before the slew to the target) and
`post_actions:` (after it) with snake_case action keys (`image_type`,
`object_name`, ...); pid-config files use `slot_len`, `n_stars`,
`n_airmass`, `pool_size`, `recurrence`, `times`, `past_meridian_only`
(restrict the Higher-family selection to targets that already crossed the
meridian — pier-flip avoidance on German equatorial mounts).  Files with
unknown keys are rejected whole (never half-loaded).

Legacy-dialect files (`schedalgorith: 3`, `maxairmass`, `imageType`,
`pos-actions`, `slotLen`, ...) are **not** accepted by the CLI; convert
them once with the standalone script:

```
python scripts/migrate_legacy_config.py legacy.yaml [...] [-o outdir]
```

The legacy `EPOC` CSV header is still accepted (targets CSVs are numerous
and harmless).

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
