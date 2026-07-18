# robobs port notes (Python 2 / old chimera → Python 3.13 / chimera 0.2)

This package was extracted from chimera-supervisor 2.0 and ported from the
legacy Python 2 sources (`chimera-supervisor/legacy_py2/controllers/robobs.py`,
`.../controllers/scheduler/*` and `scripts/chimera-robobs`).  This file lists
every deliberate deviation from the legacy behavior and every bug fixed on
the way.  Everything else is a mechanical port.

## Repository / layout

* New standalone repository `chimera-robobs`, package `chimera_robobs`
  (`controllers/` for chimera plugin discovery, `scheduling/` for the model +
  algorithms + engine, `cli/` for the `chimera-robobs` entry point).
* The scheduling decision logic (reschedule / get_program / check_conditions)
  was factored out of the controller into
  `scheduling/engine.py::RobObsEngine`.  Reason: the legacy CLI called
  `robobs.reshedule()` **through the proxy** and received SQLAlchemy rows;
  the chimera 0.2 bus is JSON (msgspec) and cannot serialize ORM objects, so
  `process-queue` now runs the same engine *offline* against the database.
  The controller delegates to the engine, behavior preserved.

## Database schema (reorganized — no data migration)

The robobs database is rebuilt from the project/target/block input files;
**no migration from the legacy `robo_scheduler.db` is provided.**  Default
path is now `~/.chimera/robobs.db` (`DEFAULT_ROBOBS_DATABASE`), configurable
via the controller's `database` config key and the CLI's `--database` option.

* snake_case columns everywhere (`lastObservation` → `last_observation`,
  `targetRa` → `target_ra`, `maxairmass` → `max_airmass`,
  `minmoonDist` → `min_moon_distance`, `schedalgorith` → `sched_algorithm`,
  `objid` → `target_id`, `bparid` → `block_par_id`, `slewAt` → `slew_at`, ...).
* Foreign keys now point at primary keys:
  * `obsblock.block_par_id` → `blockpar.id` (legacy pointed at the non-unique
    `blockpar.bid`; the CLI resolves the user-level `(pid, bid)` pair to the
    primary key when blocks are added — legacy stored the raw `bid` there
    while the queries joined on `BlockPar.id`, which was inconsistent),
  * `program.target_id` → `targets.id`, `program.project_id` → `projects.id`,
  * `observinglog.tid` → `targets.id`.
* Legacy FKs to non-unique columns removed (`program.name → targets.name`,
  `observinglog.name → targets.name`, `observinglog.priority →
  program.priority`, `*.pid → projects.pid`).  `pid` columns are plain
  strings holding the project code; `Program` keeps both `pid` (for the
  queries used throughout the algorithms) and the `project_id` FK.
* `program.expose_at` dropped: never written by any tooling and chimera 0.2
  has no equivalent; `slew_at` maps to chimera's `Program.start_at` in
  `chimera_program()`.  chimera's `valid_for` is left at its default (-1).
* `Expose.exptime` is Float (the legacy Integer truncated exposure times);
  `Expose.binning`, `Expose.window`, `AutoFocus.binning` are Strings to
  match chimera 0.2; `AutoFlat` gained `binning` (chimera 0.2 has it).
* `ObservedAM` got its own autoincrement PK + `extmoni_id` FK (legacy used a
  bizarre composite PK of id/airmass/altitude).
* `Program.created_at`/`ObservingLog.time` defaults are now callables
  (legacy `default=datetime.today()` was evaluated once at import time, so
  every row got the daemon start date).
* No import-time engine/`create_all`: `open_database(path, echo)` returns a
  `sessionmaker` (with `expire_on_commit=False` — program tuples are passed
  across sessions/commits by the controller and algorithms, as the legacy
  code assumed).  Chimera-model imports inside `chimera_program()`/
  `chimera_action()` are deferred so importing `chimera_robobs.scheduling
  .model` has no side effects (importing *chimera's* scheduler model still
  creates chimera's own `scheduler.db`; that is chimera 0.2 behavior).

## chimera 0.2 API adaptations

* `SiteAdapter` (`scheduling/siteadapter.py`) wraps the `Site` proxy: the
  bus only carries JSON, so datetimes are sent as pyephem-format strings and
  ISO strings returned by the proxy are parsed back to datetimes.
* `Site.moonpos()` returns a `Position` (not JSON-serializable), so moon
  ra/dec and phase are computed **locally** with pyephem.  The local moon
  position is geocentric — up to ~1° of parallax versus the topocentric
  value, negligible for the moon-distance constraints used here.
  Target/moon altitudes use the proxy-safe `ra_dec_to_alt_az(ra, dec, lst)`.
* `datetimeFromJD` no longer exists in chimera; `scheduling/dates.py`
  provides `datetime_from_jd`/`jd_from_datetime` (unix-epoch based, UTC).
* chimera 0.2 scheduler events carry **ids** (`program_begin(program_id)`,
  `action_begin(action_id, ...)`), not ORM objects.  The controller queries
  the chimera scheduler database by id to write the observing log, and logs
  action ids in the action callbacks.
* Event names: `programBegin/programComplete/actionBegin/actionComplete/
  stateChanged` → `program_begin/program_complete/action_begin/
  action_complete/state_changed`.
* `chimeraProgram()/chimeraAction()` → `chimera_program()` and a polymorphic
  `chimera_action()` instance method (the legacy static-method-with-self and
  the `getattr(sys.modules[__name__], act.action_type)` dispatch are gone).
* `RobState` is a stdlib `enum.Enum`; the legacy `ScheduleOptions`
  Enum("HIG","STD") in algorithms/base.py was unused and dropped.
* Renames: `reshedule` → `reschedule`, `getPList` → `get_priority_list`,
  `getProgram` → `get_program`, `checkConditions` → `check_conditions`,
  `getSched` → `get_scheduler`.

## Algorithms

* Every module imports exactly what it uses; the legacy `from ... import *`
  chain was broken at runtime (`NameError` for `Session`, `Position`,
  `datetime`, `and_`/`or_`, `Pool`, output colors, ...).
* Database access goes through a session factory installed with
  `algorithms.configure(session_factory)` (lazy default database when
  unconfigured).  Ids/names unchanged: 0 Higher/HIG, 1 ExtintionMonitor/STD,
  2 Timed/TIMED, 3 Recurrent/RECURRENT, 4 TimeSequence/TIMESEQUENCE
  (module file renamed `extinctionmonitor.py`; the class name and the "STD"
  string are kept because the id/name are what is persisted).
* The legacy `Pool` (never imported!) is `multiprocessing.pool.ThreadPool` —
  the workers write into a shared numpy array, so threads are the only
  choice that could ever have worked.
* The import-time `scheduler_algorithms.log` rotating file handler was
  dropped; algorithms use module-level loggers that propagate to the robobs
  debug log / chimera logging.
* `ExtintionMonitor.next()` now skips programs missing from its bookkeeping
  table instead of crashing; `observed()` records the actual airmass along
  with the altitude (legacy always stored the column default 1.0).
* Preserved legacy quirks (documented, not "fixed", to keep behavior):
  the engine passes the full priority queue to each algorithm's `next()`
  (legacy computed a per-algorithm filter and discarded it); Higher/
  TimeSequence `process()` look up `max_airmass` with the unmasked index;
  the block length is converted to a radian offset via *arcseconds* in the
  slot altitude computation.

## Fixed bugs (from the legacy code review)

1. `recurrent.py`: `reccurent_block.blockid = obsblock.id,` — the trailing
   comma stored a one-element **tuple** in the database.  Fixed (and covered
   by a test).
2. `robobs.py::reset_scheduler()` built a RESET program but never committed
   the session.  Fixed.
3. CLI `cleanProject`/`cleanTargetsList`/`cleanObservingBlock` backed up the
   **checklist** database (`DEFAULT_PROGRAM_DATABASE`) instead of the robobs
   database.  All destructive commands (and `add-observing-block`, which
   deletes existing blocks) now back up the actual database in use.
4. `checkConditions()` had a `# FIXME` fall-through: when the airmass at the
   end of the block was out of range it did `pass` instead of rejecting.
   The port rejects — and evaluates the altitude at the end-of-block LST
   (legacy reused the start LST, so the check could never have triggered).
5. `Projects.__str__` referenced the non-existent `self.flag`.
6. Legacy CLI `addObservingBlock` accumulated the exposure time **once per
   configuration key** (indentation bug), inflating `ObsBlock.length` by the
   number of keys; block length is now computed once per action.
7. Legacy `addTargets` built a case-insensitive column map but then indexed
   `targets['RA']` directly, forcing uppercase headers; now genuinely
   case-insensitive.
8. `robobs.py` referenced `self.getSM()` which did not exist (NameError as
   soon as a seeing monitor was configured); a real seeing getter using the
   first `seeingmonitors` proxy is provided.
9. Bare `except:` clauses replaced with `except Exception` + logging;
   `yaml.load` → `yaml.safe_load`.
10. The legacy robobs machine busy-spun at 100% CPU while in the BUSY state
    (the state had no branch in the loop); it now sleeps until woken.
11. `Program.created_at` import-time default (see schema section).

## Not ported

* `legacy_py2/controllers/instrumentcontainer.py` (broken, unused) and the
  dead code paths of the legacy scheduler machine.
* CLI actions `makeObservingLog` (matplotlib altitude chart) and
  `cleanObservingLog`.  `observing-log` shows entries, optionally filtered
  with `--start/--end` (defaults to all entries instead of requiring a site
  connection for twilight times).
* The legacy `--lststart/--lstend` options were parsed but never used; the
  new `--lst-start/--lst-end` actually override the LST selection cut in
  `make-queue`.
* `monitor` was a no-op in the legacy CLI; it now prints the controller
  state (`RobObs.state()` was added for this).
* Weather-station/cloud-sensor checks remain unimplemented placeholders, as
  in the legacy code.

## Compatibility of input files

Project YAML (`project:` + `observing_blocks:`), block YAML
(`pre-actions`/`pos-actions`) and the targets CSV keep the legacy formats.
Legacy key names (`maxairmass`, `minmoonBright`, `schedalgorith`,
`imageType`, `objectName`, ...) are accepted and mapped to the new column
names; string templates in block files may use both the legacy
(`{targetRa}`) and the new (`{target_ra}`) field names.

## 2.0.0.dev deep refactor (2026-07)

The pre-release schema changed once more (still no production 2.0 database
exists): `tid` columns renamed `target_id` (extmonidb/timeddb/recurrentdb/
observinglog), the recurrent table renamed `recurrentdb`, model classes
`Targets`/`Projects` renamed `Target`/`Project` (table names unchanged),
`Expose` gained `compress_format`/`wait_dome` (used by every production
block file).  See `docs/plans/deep-refactor.md`.

## Canonical input dialect (2026-07, supervisor-style)

The YAML inputs gained a canonical snake_case dialect mirroring the
chimera-supervisor 2.0 configuration style: `scheduling_algorithm` takes a
readable name (`higher`/`extinction_monitor`/`timed`/`recurrent`/
`time_sequence`) instead of a numeric id, block files use
`pre_actions`/`post_actions`, pid-config uses `slot_len`/`n_stars`/
`n_airmass`.  The legacy dialect is **not** accepted by the CLI (files
with unknown keys are rejected whole); the standalone
`scripts/migrate_legacy_config.py` converts legacy files once.  The
`EPOC` CSV header remains accepted.

## Scheduling section merged into the project file (2026-07)

The standalone `_pid.yaml` files were historical (a make-queue runtime
argument): the project file now carries an optional `scheduling:` section
(same keys) stored with the project (`projects.scheduling`, JSON) and used
as the make-queue default; `--pid-config` remains as a per-key, per-night
override.

## Timed `expire_overdue` option (2026-07)

`timeddb` gained `min_gap` (cadence to the previous occurrence, days) and
`observed_at` is now written on execution.  With `expire_overdue: true` in
the scheduling section, an occurrence whose `execute_at` falls within
`min_gap` of the previous occurrence's actual run time is expired
(`finished` with `observed_at` 0) — a run delayed by a long block no
longer produces back-to-back timed runs, and longer backlogs
self-collapse to a single run.
