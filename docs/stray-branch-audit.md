# Stray-branch audit: features lost outside master

Date: 2026-07-17.  Surveyed repo: `chimera-supervisor` (the pre-split home
of robobs; `origin` ≡ `upstream`).  Master line tip: `5e7a23f` — the base
of both the chimera-supervisor 2.0 refactor and the chimera-robobs 2.0
port.  Anything that only lived on the branches below never reached either
2.0 codebase.

## Branch map

The branches nest — each is a superset of the previous:

```
compress (3) ⊂ bugfix/block_length (5) ⊂ feature/remove_future_check (6)
  ⊂ bugfix/observation_after_night_end (7) ⊂ feature/telegram_set (17)

lna (3) ⊂ mysql (34);  compress ⊂ mysql
wschoenell-patch-1 (1)          # the ra=0 fix, 2018-09-11 — the newest
                                # robobs commit anywhere, never merged
dev (0)                         # fully merged, nothing unique
```

The LNA Robo40 2018B deployment ran the **mysql** branch (its config files
use `past_meridian_only`, which only ever existed there).

## Recovered into chimera-robobs 2.0 (applied 2026-07-17, with tests)

| fix | branch/commit | what it does |
|---|---|---|
| RA=0 target selection | `wschoenell-patch-1` `f390349` | `select_blocks` wrap-around LST window used `target_ra > 0`, excluding targets at exactly RA 0h; now `>=`. |
| Inclusive moon-brightness bounds | `mysql` `3138e3b` | `min <= brightness <= max` instead of strict `<`; with the ubiquitous `min: 0 / max: 100` configs, an exactly-new-moon or full-moon brightness failed the check.  Applied in `engine.check_conditions` and both spots in `Higher.process`. |
| Recurrent: NULL `last_observation` | `mysql` `b2925f7` | Blocks marked `observed` with no `last_observation` date (left by simulations) were excluded from Recurrent scheduling forever; a third OR clause keeps them eligible. |
| Block length from stored `ObsBlock.length` | `bugfix/block_length` `d317c21`/`d567cd4` | The engine's program length (feeds the end-of-night and end-of-block checks) and the `process-queue` simulation now use the ingest-time stored length (which includes 12 s readout and 600 s focus overheads) instead of recomputing a bare exposure sum; falls back to the exposure sum when no length is stored. |

## Lost robobs features — candidates, not applied

* **`past_meridian_only`** (`mysql` `3138e3b`) — pid-config flag restricting
  Higher-family selection to targets already past the meridian (pier-flip
  avoidance on the Robo40 GEM; used by the LNA focus configs).  Trivial to
  re-implement in the refactored `Higher.process` (one mask condition); the
  legacy `lst > ra` comparison also needs a 24 h-wrap fix (hour angle test)
  — it misclassified targets near RA 0.  The 2.0 CLI currently warns and
  ignores the key.
* **Park-then-STOP on empty queue** (`bugfix/observation_after_night_end`
  `ab7ecd3`) — after queuing the SAFETY park program, the branch *stopped*
  robobs entirely ("prevents observations after the night ends"); 2.0
  instead parks once and retries every 5 minutes (interruptible).  The
  retry behavior recovers automatically when programs appear; the stop
  behavior is safer at dawn.  Open design decision — could become a config
  option (`stop_when_empty`).
* **`reset_scheduler()` on `start()`** (`mysql` `e91e84f`) — on the branch,
  switching robobs on first *deleted every program in the chimera scheduler
  queue* (stale-queue cleanup after a restart).  2.0 deleted
  `reset_scheduler` as dead code (master never called it).  Worth
  considering as an explicit `clean_scheduler_on_start` config, since a
  stale chimera queue after a crash means re-observing old programs.
* **`--yesterday` option** (`compress` `5bab1ac`) — shifted the scheduling
  window one day back for re-planning/re-plotting the previous night.
  Superseded in practice by 2.0's `--jd-start/--jd-end/--date-start/
  --date-end`, but the one-flag convenience is gone.
* **Observing-plan plotting improvements** (`mysql` `93beae0`, `7ad3ebe`) —
  aborted programs drawn dashed, per-track moon-distance annotations,
  `-f` output-filename option, robust start/end mismatch handling.  Lost
  together with the whole `makeObservingLog` plotting feature, which the
  2.0 port dropped (documented in `robobs-port-notes.md`).  Only relevant
  if plotting is ever resurrected.

## Superseded or deliberately not carried

* **MySQL backend** (`mysql`: `aad1f41`, `03f51e3`, `257bbe6`, `88e72f1`,
  `5bccf7b`, `7af3066`, `c2da1d9`, `0fa8e5c`) — engine DSN from a
  `robo_scheduler.yaml`, `NullPool`, `DOUBLE` floats, `String(65)` lengths,
  integer FKs, `ObsBlock.blockid`/`BlockPar.bid`/`Program.pi` dropped,
  unique `BlockPar.name`.  2.0 is sqlite-only by design (supervisor
  DESIGN.md); notably, the branch's *good schema ideas* (FKs pointing at
  real primary keys) were independently re-done in the 2.0 schema.  If a
  server database is ever wanted again, this chain is the reference.
* **Session close/leak fixes** (`mysql` `88e72f1`) — 2.0's engine/algorithm
  session hygiene covers this.
* **`wait_dome`/`window`/`binning`/`compress_format` propagation**
  (`compress` `9a0680a`, `lna` `28b5422`) — recovered earlier during the
  2.0 deep refactor (`Expose` columns + `chimera_action()`).
* **`valid_for = -1`** (`lna` `454a950`) — chimera 0.2's default; covered.
* **`--lst-start/--lst-end`** (`mysql` `7ad3ebe`) — implemented in 2.0
  (the port made the previously parsed-but-unused options functional).
* **Future-observability re-check removal** (`feature/remove_future_check`
  `7515b32`) — the branch commented out a guard that skipped
  higher-priority programs when they couldn't be observed after the
  current block; on master that guard had already decayed into a
  debug-only log, which the 2.0 refactor deleted.  Current engine behavior
  matches the branch's intent.
* **`pool_size`/`max_sched_blocks` removal** (`mysql` `6f798ef`) — a
  temporary hack, reverted on the branch itself by `3138e3b`.  Non-issue.

## Lost on the chimera-supervisor side (not robobs — tracked here for the
record, belongs in that repo's backlog)

* **Telegram `/setflag` command** (`feature/telegram_set`, 10 commits) —
  chat command to set an instrument's operation flag (with dynamic
  valid-flag help, LOCK refusal, and locked-instrument protection), plus a
  `controllers` config key registering extra flag-managed names.
  Supervisor 2.0's `OperatorCommands` has `/lock`/`/unlock`/`/run` but no
  flag-setting command (the controller API `set_flag` exists; the chat
  surface doesn't).
* **Sunrise-after-midnight fix** (`mysql` `78f925d`) — the legacy
  `TimeHandler` computed the *previous* sunrise when the clock was past
  midnight; fixed by using the next day's sunrise after local noon.
  Supervisor 2.0 rewrote the condition system — verify its time/altitude
  conditions handle the past-midnight case.
* **Boolean-typed handler returns** (`mysql` `407de11`) — numpy-bool
  coercion in dew handlers; almost certainly moot in the 2.0 rewrite.
