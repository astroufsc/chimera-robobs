# chimera-robobs deep refactor plan

Status: **draft — awaiting execution**
Scope: whole repository (`src/`, `tests/`, packaging, docs).
Baseline: `cd34b23` — 34 tests passing, `ruff check` clean.

## Context and goals

chimera-robobs was extracted from chimera-supervisor and ported to
Python 3.13 / chimera 0.2 (see `docs/robobs-port-notes.md`).  The port was
mechanical-but-careful; the *structure* is still largely the legacy one that
"grew in an uncontrolled manner" out of the original chimera-sched.  This
refactor:

1. matches the repo to the **chimera-template** conventions and the house
   style established by the **chimera-supervisor 2.0 refactor** (its
   `docs/DESIGN.md` is the decision record and explicitly prescribes the
   robobs database choices);
2. removes dead code and legacy duplication (Higher/TimeSequence are ~200
   nearly identical lines; airmass/duration/MJD math is copy-pasted across
   five files);
3. fixes real inconsistencies found by auditing the current chimera core
   API and the **production input files** at
   `~/workspace/chimera/lna40/old/robobs` and `~/workspace/t80s_scripts/robobs`;
4. modernizes the database layer within the prescribed constraint
   (**keep SQLAlchemy 1.4**, the same pin as chimera core — supervisor
   DESIGN.md §"SQLAlchemy trade-off");
5. adds the missing test coverage (the `process()` schedulers and the
   controller are currently untested).

Non-goals: no new features (no Slack/Telegram surface, no weather checks),
no data migration (pre-production: the DB is rebuilt from input files), no
change to the persisted algorithm ids/names (0 HIG, 1 STD, 2 TIMED,
3 RECURRENT, 4 TIMESEQUENCE).

## Audit findings

### A. Template / repo compliance gaps

| item | current | target (template / supervisor) |
|---|---|---|
| `.pre-commit-config.yaml` | missing | ruff (`--fix`) + ruff-format |
| `CLAUDE.md` | missing | template 3-point form (uv only; ruff; keep template compat) |
| `[tool.ruff]` | lint select only | + `line-length = 88`, `target-version = "py313"`, flake8-copyright `notice-rgx` |
| SPDX headers | 5 files still say `chimera-supervisor authors` | `2014-present chimera-robobs authors` everywhere |
| `ruff format` | never applied | format whole tree, enforced by pre-commit |
| `.gitignore` | minimal | template list (add `.history/`, `.coverage`, `htmlcov/`, `*.swp`, `.DS_Store`, `.vscode/`, `.idea/`) |
| `uv.lock` | tracked | keep tracked (deployable app with a CLI; deviation from template noted here) |
| README.md | good content | reorder to template sections (Install / Config example / Development / License / Contact), keep content |

### B. Dead code (verified unreferenced)

* `RobObs._current_program_condition` — created, never used.
* `RobObs.get_site()` — never called (engine gets the adapter injected).
* `RobObs.reset_scheduler()` — no caller anywhere (CLI never exposed it;
  supervisor drives robobs only via `start/stop/wake`).  Delete; recoverable
  from git if an operator API is ever wanted.
* `Machine.current_program` — never used.
* `BaseScheduleAlgorith.merit_figure()` / `.model()` — never called.
* `engine.check_conditions(external_checker=...)` — dead parameter
  (`if external_checker is not None: pass`).
* `engine.reschedule()` lines 229–242 — two leftover *debug-only* blocks
  that re-run the expensive `check_conditions` purely to log; removing them
  changes no behavior.
* `TimeSequence.process` `mask` bookkeeping — written, never updated, the
  `len(mask) == 0` break can never trigger (goes away with the dedup).
* CLI `best_slot_len()` — hard-coded 15.0 that is *dead in practice*: it is
  passed positionally to `process(*args)`, which only reads `args[0]` when
  `len(args) > 1` (never).  Real slot lengths always come from the pid-config
  `slotLen` key (250–1500 s in every production file).  Delete the function;
  make the per-algorithm defaults explicit.

### C. Duplication

1. **`Higher.process` vs `TimeSequence.process`** — ~200 duplicated lines.
   Differences: TimeSequence keeps the selected target as a candidate for
   subsequent slots (time-monitoring) and skips the end-of-block airmass
   check.  Extract one slot-allocation routine in `higher.py` parameterized
   by `keep_selected_target` / `check_end_airmass`.
2. **Airmass formula** — `1/cos(pi/2 - alt)` hand-rolled 8× across engine,
   higher, timesequence, extinctionmonitor while `base.airmass()` exists.
   Use the helper everywhere (identical math; the helper only adds the
   below-horizon→999 clamp already wanted at each call site — call sites
   that must not clamp keep the raw expression via a `clamp=False` arg if
   needed).
3. **Block duration** — four near-copies (engine `get_program`, CLI
   `_action_length`, CLI `calc_obs_time`, extinctionmonitor block_duration),
   each with deliberately different overhead constants.  One helper in
   `model.py`: `block_duration(actions, readout=0.0, autofocus_sweep=600.0,
   autofocus_set=0.0)`; call sites keep their current constants (engine:
   readout 0; CLI ingest: readout 12; CLI simulation: readout 20;
   extinctionmonitor: config-driven overheads).  Also replaces every
   `act.__tablename__ == "action_expose"` string check with `isinstance`.
4. **MJD/JD constants** — `2400000.5` ×15 and `86.4e3`/`86400` ×12.  Add to
   `dates.py`: `MJD_JD_OFFSET = 2400000.5`, `SECONDS_PER_DAY = 86400.0`,
   plus `datetime_from_mjd()` / `mjd_from_datetime()`.  Kills the noisy
   `datetime_from_jd(t + 2400000.5)` idiom.
5. **Program row tuples** — `(Program, BlockPar, ObsBlock, Targets)` rows
   are indexed `program[0]`…`program[3]` throughout.  Converting to a
   NamedTuple breaks the `Query.filter()` chaining make-queue relies on, so
   the row stays a SQLAlchemy row; instead every consumer unpacks once at
   the top (`program, blockpar, block, target = row`) — readability without
   protocol change.

### D. Naming consistency (nothing here is persisted — table/column names in
the DB are only changed where marked, and the DB is rebuilt from inputs)

* `BaseScheduleAlgorith` → `BaseScheduleAlgorithm`; `ExtintionMonitor` →
  `ExtinctionMonitor`; `ExtintionMonitorException` →
  `ExtinctionMonitorError`; `TimedException` → `TimedError`;
  `RecurrentAlgorithException` → `RecurrentError`.  (Algorithm *name
  strings* `"STD"` etc. and ids stay — those are persisted.)
* Model classes `Targets` → `Target`, `Projects` → `Project`
  (`__tablename__` unchanged: `targets`, `projects`).
* Columns `tid` → `target_id` in `ExtMoniDB`, `TimedDB`, `RecurrentDB`,
  `ObservingLog` (schema change; pre-production, no migration needed) —
  matches `ObsBlock.target_id`/`Program.target_id`.
* `RecurrentDB.__tablename__` `"recurrent"` → `"recurrentdb"` (consistency
  with `extmonidb`/`timeddb`; same pre-production argument).
* `ObsBlock.blockid` / `BlockPar.bid` stay as-is (user-level id spaces,
  distinct from the `*_id` FK convention) but gain doc comments.
* Algorithm `process()` kwargs `obsStart/obsEnd/slotLen` → snake_case
  explicit keywords (`obs_start`, `obs_end`, `slot_len`).  The **YAML**
  config key `slotLen` in pid-config files is still accepted (input compat).
* `RobObs._inject_instrument` → dissolved by the algorithm-injection change
  (E2).

### E. Design changes

1. **Fold `scheduling/machine.py` into the controller and move the
   scheduler-idle reaction off the bus thread.**  Today
   `_watch_state_changed` runs the full reschedule *and a
   `time.sleep(300)`* inline on the bus dispatch pool — the exact deadlock
   supervisor's `_run_hook` docstring warns about, and the sleep makes
   `__stop__` unresponsive for up to 5 minutes.  New shape: the event
   handler only records "scheduler went idle" and wakes the machine thread;
   the machine performs reschedule → submit program → `sched.start()`, and
   the no-program backoff becomes `Condition.wait(timeout=300)` so
   `stop()`/`__stop__` interrupt it.  The Machine class moves into
   `controllers/robobs.py` (68 lines, used nowhere else) with its own tiny
   `enum.Enum` states instead of borrowing the core scheduler's `State`.
2. **Instance-based algorithms; kill the two global injection channels.**
   `algorithms.configure(session_factory)` (module global) and
   `ExtintionMonitor.site = ...` (class-attribute injection via
   `_inject_instrument`) both disappear.  Algorithms become instances:
   `build_algorithms(session_factory, site=None) -> dict[int, BaseScheduleAlgorithm]`;
   the engine, controller and CLI build one registry and pass it around
   (the engine already accepts an `algorithms` dict).  Static-method
   `id()/name()` become class attributes `id`/`name`.
3. **One logger.**  Replace the hand-built `_robobs_debug_` per-day
   `FileHandler` (+ the duplicated `self.log` *and* `self._debuglog` calls,
   and `Machine.get_logger()` indirection) with supervisor's pattern: a
   `RotatingFileHandler` (`robobs.log`, 50 MB × 10) attached to `self.log`
   in `_setup_logger()`.  The engine keeps its `log=` parameter (CLI passes
   a module logger, controller passes `self.log`).
4. **Engine session hygiene.**  Standardize: reads open a session and
   `close()` (no commit-for-read), writes commit; drop the vestigial outer
   session in `reschedule()` that is opened and only ever committed.
   `expire_on_commit=False` and the merge-on-entry pattern stay (documented
   in `model.open_database`).
5. **Engine "observe earlier" fix.**  `get_program()` scans
   `np.linspace(now, slew_at)` for an earlier feasible start but never
   breaks, so the *last* (latest) candidate wins and the feature is a
   near-no-op.  Fix: break at the first (earliest) feasible candidate.
   Behavior change — documented here and covered by a test.
6. **SQLAlchemy 1.4, modern spelling.**  Keep the 1.4 pin (prescribed;
   chimera core pins it).  Within 1.4: `sqlalchemy.orm.declarative_base` /
   `relationship()` instead of the pre-1.0 `ext.declarative` import and
   `relation()`; drop the redundant hand-written `__init__` on `ExtMoniDB`,
   `ObservedAM`, `TimedDB` (declarative kwargs constructor already does
   this); replace the deprecated `default=dt.datetime.utcnow` with a
   naive-UTC callable (`lambda: dt.datetime.now(dt.UTC).replace(tzinfo=None)`)
   on `Program.created_at` / `ObservingLog.time` (kills the py3.13
   DeprecationWarnings in the test run).
7. **Controller simplifications.**  `get_scheduler(index)` → parameterless
   `get_scheduler()` (comma-list config still parsed, first entry used —
   as today); event-subscription one-shot `control()` stays (supervisor
   pattern, already correct); `StrEnum` check confirmed — `status`/`state`
   comparisons against `SchedulerStatus`/`State` work with the JSON-string
   values arriving over the bus, no change needed.

### F. Input-format compatibility fixes (from the production-file audit)

These are cases where the current port would **break or silently degrade**
on real files:

1. **`Expose.compress_format` and `Expose.wait_dome` are silently dropped.**
   `compress_format:` appears in essentially every T80S expose action and
   `wait_dome:` in every LNA one; `_apply_action_config` skips unknown
   attributes, so both vanish.  chimera core `Expose` has both columns.
   Add them to the robobs model + `chimera_action()` copy.
2. **Targets CSV `EPOC` header** (spelled without H in every LNA/T80S
   pointing CSV) is not mapped; also `EPOCH` should map.  Add both to
   `TARGET_CSV_COLUMNS` → `target_epoch`.  Strip whitespace from string
   values (the production CSVs are space-padded).  Unknown columns (`PID`,
   `INFO`, `NEXP`, `BINNING`, `ROIx`…) are ignored — now with a one-line
   notice listing them instead of silence.
3. **Project YAML blocks without `id:`/`pid:`** (`kelt_project.yaml` style)
   currently raise `KeyError`.  Default: `pid` ← the project's pid, `id` ←
   enumeration order.  Warn on unknown block keys (would have caught the
   `name:` key drift).
4. **Unknown pid-config keys warn** (e.g. `past_meridian_only`, used at
   both sites but not implemented in the port — currently silently
   ignored; it stays unimplemented but is now *visibly* ignored, recorded
   in README as a known gap).
5. Non-changes, recorded as guarantees: `pos-actions` spelling,
   `schedalgorith` spelling, camelCase blockpar/action keys, `imageType`
   value case (`object`/`OBJECT` stored verbatim), 5-column whitespace-mixed
   block `.list` files, `.conf`≡`.yaml` pid-config — all already handled;
   each gets a regression test with a **verbatim production excerpt** as
   fixture.

### G. Resulting tree

```
src/chimera_robobs/
├── __init__.py
├── controllers/
│   └── robobs.py          # RobObs + Machine (folded in), one logger
├── scheduling/
│   ├── __init__.py
│   ├── algorithms/
│   │   ├── __init__.py    # build_algorithms(session_factory, site)
│   │   ├── base.py        # BaseScheduleAlgorithm (instance-based), airmass
│   │   ├── extinctionmonitor.py
│   │   ├── higher.py      # + shared slot-allocation used by timesequence
│   │   ├── recurrent.py
│   │   ├── timed.py
│   │   └── timesequence.py  # shrinks to ~60 lines
│   ├── dates.py           # + MJD helpers/constants
│   ├── engine.py
│   ├── model.py           # + block_duration(), Target/Project renames
│   └── siteadapter.py
├── cli/
│   └── robobs.py
```

Net: one module deleted (`machine.py`), no other file moves — so no
`git mv` is required beyond that deletion; the tree already matches the
supervisor/template shape (`controllers/` + domain package + `cli/`).
`RobObs` keeps its class name and `controllers/robobs.py` its path: both are
referenced by deployed chimera configs (`type: RobObs`).

## Execution order

Each phase leaves the suite green; one commit per phase, short imperative
messages, **no AI attribution**.

1. **Repo compliance** — pre-commit, CLAUDE.md, ruff config + format,
   SPDX normalization, .gitignore, README reorder.  (A)
2. **Dead code + naming** — deletions from (B); renames from (D); modern
   SQLAlchemy spelling (E6).  Mechanical, test-covered.
3. **Deduplication** — airmass, block_duration, MJD constants,
   Higher/TimeSequence extraction, row unpacking.  (C)
4. **Design changes** — algorithms as instances (E2), machine fold +
   off-bus rescheduling (E1), single logger (E3), engine session hygiene +
   earlier-observation fix (E4, E5), controller simplifications (E7).
5. **Input compatibility** — model columns, CSV/YAML mappings, warnings
   (F), with production-excerpt fixtures.
6. **Tests** — everything under "Test plan" not already added alongside
   phases 2–5; final `uv run pytest`, `uv run ruff check`, `ruff format
   --check`, pre-commit run.

## Test plan

Existing 34 tests are kept (updated for renames).  Comprehensive target
list — items marked ★ are new coverage of previously untested code:

**dates** — jd/datetime round trip (exists); ★ mjd round trip; ★
`MJD_JD_OFFSET`/`SECONDS_PER_DAY` sanity (`datetime_from_mjd(0)` =
1858-11-17).

**model** — schema/relationship round trip (exists); chimera conversion
(exists); ★ `Expose.compress_format`/`wait_dome` persisted and copied to
the chimera action; ★ `block_duration()` for each overhead profile
(engine/ingest/simulation/extmoni) incl. autofocus `step>0 / step==0 /
step<0` sentinel semantics; ★ `created_at`/`time` defaults are
call-time (two rows created at different times differ).

**algorithms/base** — ★ `airmass()` values (zenith 1.0, 60° zenith-dist
2.0, below-horizon clamp); ids/names stability (exists, updated).

**higher** — `next` picks closest slew_at (exists); ★ `process()` on a
populated DB with FakeSite: slots filled highest-first, selected target
removed, moon-brightness slot veto, moon-distance target veto, max-airmass
veto, `max_sched_blocks` stop; ★ multi-target block "already filled slot"
branch.

**timesequence** — ★ `process()` keeps the selected target available to
later slots (the defining difference from Higher); ★ shares the slot
helper (no behavioral drift: same output as Higher when
`keep_selected_target=False`).

**extinctionmonitor** — add/clean/soft_clean/observed (exists); ★
`process()` allocates `nairmass` slots across airmasses for `nstars`
stars on a FakeSite with a rotating LST; ★ `next()` skips covered
altitude levels and un-bookkept programs.

**timed** — clean/soft_clean (exists); ★ `process()` stores `times:` as
MJDs relative to night start; ★ `next()` overrides slew_at with
`execute_at`; ★ `observed()` marks the TimedDB row finished.

**recurrent** — add/observed round trip incl. tuple-bug regression
(exists); ★ `process()` filters by recurrence window (never-observed kept,
recently-observed excluded, old-observation kept); ★ max_visits completes
the block.

**engine** — priority list, basic reschedule, airmass/moon/night-end/seeing
conditions (exist); ★ earlier-observation fix: future `slew_at` with
feasible earlier time → earliest feasible chosen (regression for E5); ★
lower-priority program selected when it fits inside the higher-priority
wait slot; ★ higher-priority program deferred when observable later; ★
algorithms receive the injected registry (no globals touched).

**controller** (★ all new; fake Bus-less proxies) — machine state walk
(OFF→START→BUSY→SHUTDOWN); `state_changed(IDLE→OFF)` while ON triggers
reschedule *on the machine thread* and submits the chimera program +
actions; not-ON ignores; `program_complete(OK)` marks finished + calls
`algorithm.observed`; non-OK status stops robobs; empty queue → SAFETY park
program queued once, then 5-min wait is interruptible by `stop()` (test
with a short timeout); event handlers never block (submit thread-identity
assertion); `__stop__` joins the machine promptly.

**cli** (existing add/clean/delete/log/select/times tests kept) — ★
ingest of *verbatim production excerpts* under `tests/data/`:
`etacar_proj.yaml` (camelCase keys, floats-with-trailing-dot,
`schedalgorith: 3`), a kelt-style project block without `id`/`pid`, an
`EPOC`-header space-padded CSV, a T80S splus block YAML
(`compress_format`, dither `offset: {east: 10}`, `imageType: OBJECT`), an
LNA block YAML (`wait_dome`, `binning`, lowercase `object`), a 5-column
`.list` with irregular whitespace; ★ make-queue offline end-to-end
(monkeypatched `_connect` → FakeSite): programs created, blocks marked
scheduled, algorithm `add()` bookkeeping written, LST window ±2 h; ★
process-queue simulation end-to-end: log entries written, bookkeeping
soft-cleaned, `finished` reset; ★ `--help` smoke for every subcommand; ★
unknown-key warnings (project block, pid-config, CSV columns).

**packaging** — ★ `import chimera_robobs` + instantiate `RobObs` with no
bus/hardware (template compliance check); entry point resolves.

## Risks

* **E1 (off-bus rescheduling)** is the only structural behavior change in
  the controller path; mitigated by the new controller tests and by keeping
  the reaction logic itself byte-for-byte (only the executing thread and
  the sleep change).  Needs on-sky validation regardless (already the
  repo's stated status).
* **E5** intentionally changes scheduling behavior (documented above).
* Schema renames (D) are safe only because no 2.0 production database
  exists; `robobs-port-notes.md` gains a line stating the 2.0.0.dev schema
  changed again pre-release.
* Everything else is mechanical and test-guarded.

## Verification

`uv run pytest -q` (all green, no DeprecationWarnings from our code),
`uv run ruff check src tests`, `uv run ruff format --check`,
`uv run pre-commit run --all-files`, plus a manual smoke: ingest the real
`etacar_proj.yaml` + pointings CSV + block list from
`~/workspace/chimera/lna40/old/robobs/chimera-config/` into a scratch DB
and run `make-queue`/`process-queue` offline against a fake site.
