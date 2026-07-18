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
| `past_meridian_only` | `mysql` `3138e3b` | pid-config flag restricting Higher-family selection to targets that already crossed the meridian (pier-flip avoidance on the Robo40 GEM; the LNA focus configs use it).  Re-implemented with a proper hour-angle test — the legacy `lst > ra` comparison misclassified targets near RA 0 h. |

## Lost robobs features — candidates, not applied
* **Park-then-STOP on empty queue** (`bugfix/observation_after_night_end`
  `ab7ecd3`) — superseded: the night-window guard removes the daylight
  hazard the stop protected against, and tracking is stopped after every
  program (see the follow-up below), while the 5-minute retry keeps
  mid-night Timed recovery.
* **`reset_scheduler()` on `start()`** (`mysql` `e91e84f`) — recovered as
  the `clean_scheduler_on_start` config option (see follow-up below).
* **`--yesterday` option** (`compress` `5bab1ac`) — shifted the scheduling
  window one day back for re-planning/re-plotting the previous night.
  Superseded in practice by 2.0's `--jd-start/--jd-end/--date-start/
  --date-end`, but the one-flag convenience is gone.

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

## Investigation: what happens when the queue empties during the night

(2.0 behavior, traced 2026-07-17; relates to the `ab7ecd3` candidate above.)

Chain: scheduler goes IDLE→OFF → `_watch_state_changed` records the event
and wakes the machine thread → `_handle_scheduler_idle()` →
`engine.reschedule()` returns `None` → **first** empty pass queues one
chimera program `SAFETY` (a scheduler `Point` to alt 88°/az 89° — not a
real `telescope.park()`; dome/cover untouched) and starts the scheduler;
**subsequent** passes return a 300 s backoff — the machine waits
(interruptibly) and re-runs the engine directly, without bouncing the
chimera scheduler.  The loop exits when a program becomes eligible (e.g. a
Timed `execute_at` arrives — which the `ab7ecd3` stop-instead approach
would sleep through) or when robobs is stopped.

Improvements over both legacy lines: the wait no longer blocks the bus
dispatch pool, only one SAFETY program is queued per empty episode, and
retries don't start/stop the chimera scheduler.

**Confirmed gap — nothing stops the loop at dawn.**  `check_conditions`
has no night-window/sun-altitude test; its only time gate is "observation
ends before `site.sunrise_twilight_begin(now)`", and pyephem's
`next_rising` yields *tomorrow's* dawn once the sun is up, so the check
passes trivially in daylight.  Any program left `finished == False` after
dawn (aborted, or weather-rejected all night) keeps being re-evaluated
every 5 minutes through the day, and is submitted the moment its
airmass/moon checks pass — a daylight exposure, the exact failure
`ab7ecd3` fixed in 2018 by stopping robobs after the park.  In production
the guard is external: the supervisor's `StopRobObsEnd` checklist
(`robobs stop` in a ±30 min window around sunrise twilight) and
`LockDomeOnSunrise`; if the supervisor is down or its window conditions
don't match, nothing protects the telescope from pointing near the Sun.

Follow-up (implemented 2026-07-17):

1. **Night-window guard** in `check_conditions`: daytime (next
   `sunset_twilight_end` before next `sunrise_twilight_begin`) rejects
   outright — closes the gap for all paths, including a manual `wake` at
   3 pm.
2. Instead of `stop_when_empty`: **tracking is stopped after every
   finished program** (`telescope` config key, `program_complete`
   watcher, off the bus dispatch pool) so the mount never tracks into a
   limit regardless of what happens next.
3. **`clean_scheduler_on_start`** (default on): switching robobs on wipes
   stale programs from the chimera scheduler queue (mysql `e91e84f`).
4. The **plotting feature** was resurrected as `chimera-robobs plot-log`
   with the mysql-branch improvements (aborted programs dashed, hourly
   moon-distance annotations, `-f` output file, robust start/end pairing,
   Simulation/Observed title); matplotlib is a core dependency.
