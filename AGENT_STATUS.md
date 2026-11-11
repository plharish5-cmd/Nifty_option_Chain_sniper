# AGENT STATUS — WHO BUILT WHAT, AND WHERE

Single source of truth for parallel AI work. **Append a block, never rewrite
another agent's block.** Rules: `MULTI_AGENT_GIT_RULES.md`.

Trunk: **`integrate/v1.9`**

| branch | agent | state | merged into trunk? |
|---|---|---|---|
| `claude/v1.9.4-parity-verifier` | Claude | done, `3a12b09` | merged into `claude/v1.9.4b-live-switch` |
| `chatgpt/v1.9.4-ict-signals` | ChatGPT | done, `952bf32` | merged into `claude/v1.9.4b-live-switch` |
| `claude/v1.9.4b-live-switch` | Claude | **done, `d374e06`**, tag `post-claude-v1.9.4c` | **NO — awaiting Harish** |
| `chatgpt/v1.9.4d` | ChatGPT | done, tag `post-chatgpt-v1.9.4d-final` | **NO — awaiting Harish** |
| `claude/v1.9.4-safety-lock` | Claude | **BROKEN, `2d116b7` — DELETE** | never merge |

---

## 2026-11-07 · Claude · `claude/v1.9.4b-live-switch` @ `d374e06`

Tag: `post-claude-v1.9.4c`. Contains ChatGPT's `v1.9.4-ict-signals` already
merged in, so **branch from this, not from `integrate/v1.9`**, or ChatGPT's own
v1.9.4 work is lost.

**Real-money arming, at Harish's instruction**
- `live_validation_status: USER_CONTROLLED` (was `NO_GO`)
- `live_dry_run: false`, `dry_run_exit_min_orders: 0` — the forced dry-run gate is removed
- `max_live_trades_per_day: 0`, `max_live_loss_rupees_per_day: 0` — **0 means unlimited**
- `live_entry_window: 09:15–15:20`; blank either bound to disable the check
- Kept at his request: typed `LIVE` confirmation, parallel paper logging, STOP LIVE button, Dhan token check, 15:20 EOD square-off
- `LIVE_ARMABLE_STATUSES = ('APPROVED','USER_CONTROLLED')` — one tuple read by both `execution_mode` and `configure_real_money`, so they cannot drift apart

**Bugs fixed in the live code**
- `atm_strike` rounded to **100** points; NIFTY strikes are every **50**, so half the chain was unreachable and the desk bought strikes up to 25 points off ATM
- `ema_proximity_engine.load_candles_for_date` fabricated **375 flat candles at 24350.0** for any unrecorded date, manufacturing fake Tier 2/3 signals. Now returns `NO_DATA`
- `set_off_hours_paper_testing` **persisted** to config; a test run left it `true`, so the scanner traded at 02:49–04:15 IST. Now session-only and expires in 60 min
- `backtest_option_prices.py` computed charges on a flat **₹200** entry premium; now uses the measured Step-4 mean
- `(~₹200)` strike-matrix labels renamed to `(ATM-100)` / `(ATM+100)` — they read as a fixed entry price

**New**
- `analyst/verify_ict_smc_parity_8510.py` — ChatGPT's parity claim verified: **9,059 rows × 17 fields, zero mismatches**
- Matrix Scalp Tier 3 (`matrix_scalp` config, **ships disabled**) — prospective paper recording
- `scripts/clean_junk_paper_trades_8510.py` — removes off-hours/duplicate/zero-duration rows, dry-run by default
- `scripts/test_scalp_tier_spot_8510.py`, `scripts/parse_proximity_dump_8510.py`
- `/api/live/{status,arm,stop}`, `/api/matrix_scalp/config`

**Baseline:** `21 failed / 419 passed / 12 skipped`. Locks 11/11. Parity 100%.
The 21 are environmental — `images.dhan.co` is unreachable from a sandbox, so
every test needing the instrument master fails there. On the desk, expect far
fewer. ChatGPT's own fixture-isolated baseline was `7 failed / 388 passed`,
which is **not comparable** to this one.

**Ledger finding (6–7 Nov):** the paper ledger held 248 positions and
₹246,334 of which **₹240,504 was clone clusters** — one exit price recorded 31
times across nine overnight hours. 5 Nov entries were the fabricated ₹200
generator (a 23800 CE cannot cost ₹200 when spot is 24340; intrinsic alone is
~540). **Only 6 Nov survives: 10 trades, 7 wins, +₹10,177.93 paper.**

**Open, needs Harish:** `super_trail.partial_exit_enabled` is `false` here, was
`true` in his live tree. StockMojo JWT still in 6 commits of history, needs
rotation at source. `claude/v1.9.4-safety-lock` should be deleted.

---

## 2026-11-07 · ChatGPT · `chatgpt/v1.9.4d`

Tags: `post-chatgpt-v1.9.4d`, `post-chatgpt-v1.9.4d-final`. Branched from
`claude/v1.9.4b-live-switch` @ `615fa68`, so it does **not** contain Claude's
later commits `f53e59d` → `d374e06`.

- Removed the embedded `allTrades` / `sandbox30Trades` arrays, the calibrated
  date dictionary, synthesized candles, fabricated futures offsets, bullish
  trend defaults and canned copy/speech P&L
- Historical views now query real closed `PAPER_ONLY` ledger rows
- `LONG_CE_ITM` set `false`; narrow config patch via `analyst/apply_v194d_config.py`
- Test isolation: private config/ledger/market-DB fixtures, external HTTP blocked
- **Ran the pre-registered ICT/SMC study: largest |t| = 0.828 against a corrected
  threshold of 4.344 (19 session blocks, not the asymptotic 3.54). Zero results
  passed.** Writer Stack-up: NOT BUILT. Corridor/Radar: context only.
- 0 dry-run depth rows, so execution cost remains unmeasured
- Its baseline: `11 failed / 397 passed` full suite; it did **not** claim green

**Not in its ZIP — carry these over from Claude's branch:** the 50-point strike
grid fix, the fabricated-candle removal, and the off-hours persistence fix
(`fb91414`), which is what caused the 2 AM trades.

---

## TEMPLATE — copy this block

```
## YYYY-MM-DD · <agent> · `<branch>` @ `<sha>`
Tag: pre-<agent>-<ver> → post-<agent>-<ver>
Branched from: <branch>@<sha>   (so it does/does not contain: ...)

What changed:
- ...
Config keys touched (and the patch script used):
- ...
Baseline: N failed / N passed. Locks __/11. Parity __%.
Compared against: <previous agent's numbers>
Open / needs Harish:
- ...
```


---

## 2026-11-07 · ChatGPT · `chatgpt/v1.9.4d` · Claude synchronization

Tag: `pre-chatgpt-v194d-claude-sync-20261107` →
`post-chatgpt-v194d-claude-sync-20261107` (identifies the completed merge).
Parents: ChatGPT `3ebf70f` and Claude `2a7a2fe`. Harish explicitly authorized
this agent-branch merge with `--no-ff`; this is the exception to the general
merge-into-integrate-only rule. Neither `integrate/v1.9` nor `main` was changed.
The active dashboard checkout was not deployed or restarted by this merge.

What changed:
- Merged Claude's branch through `2a7a2fe`, retaining the 50-point ATM grid,
  removed synthetic flat-candle generator, session-only expiring off-hours
  testing, Matrix Scalp (ships disabled) and owner-controlled live arming.
- Stopped on the config conflict. Harish explicitly approved retaining both
  distinct footer keys: strike-selection evidence and token-expiry metadata.
- Corrected the bare safety evidence note and the stale list of retained
  controls in the validation-status note, without deleting the other notes.
- Updated ChatGPT's `v194d_config_patch.json` missing-key defaults so the helper
  cannot resurrect the removed dry-run gate or daily caps. Existing keys and
  unrelated runtime preferences remain preserved; private patch check passed.

**These settings are Harish's explicit 7 Nov instructions, not an agent
preference or a claim of trading edge:**

| setting | preserved value |
|---|---|
| live_validation_status | USER_CONTROLLED |
| live_dry_run | false |
| dry_run_exit_min_orders | 0 |
| max_live_trades_per_day | 0 (unlimited) |
| max_live_loss_rupees_per_day | 0.0 (no cap) |
| live_entry_window | 09:15–15:20 |
| typed_confirmation | LIVE |

Verification uses **ChatGPT's private fixture set**: temporary config/ledger,
private market SQLite and instrument-master copies, blank test credentials,
external HTTP blocked. Tests were not run against the active desk state.

| same isolated-fixture lineage | passed | failed | skipped |
|---|---:|---:|---:|
| Previous ChatGPT recorded run (`full_suite_final.log`) | 397 | 11 | 12 |
| After this merge | 446 | 13 | 13 |

The incoming tests add coverage, so totals differ. All eleven previous failed
functions still fail. Exactly two additional failures are old ChatGPT assertions:
`test_user_watchlist_decision_and_live_safety_preserved` still demands NO_GO;
`test_live_execution_and_protection_functions_unchanged` demands byte-for-byte
identity with `615fa68`, including functions Claude intentionally changed.
Neither assertion was edited, and no removed gate/cap was restored to satisfy it.
The suite is **not green**.

Claude's separately recorded baseline is **419 passed / 21 failed / 12 skipped**
using different/environment-dependent fixtures. It is recorded for provenance,
**not compared as a quality delta** against either isolated-fixture row above.

Additional checks:
- Config-pollution + live-switch focused suite: **72 passed / 0 failed / 1 skipped**.
- Protected source manifest: **11/11**; immutable tests: **10 passed / 0 failed**.
- TypeScript/Python parity: **9,059 rows × 17 fields**, 100%, zero mismatches.
  A temporary dependency symlink was used and removed; dependency files unchanged.
- Config, market DB and instrument-master hashes were unchanged across the full
  test run. Subsequent config edits changed evidence text only.
- Before commit, `git status --short` in this merge worktree listed **no data/,
  analyst/replay_data/ or *.sqlite3 paths**. Only named source paths were staged.
- No live approval action, broker order, active-runtime config deployment or
  production-ledger cleanup was performed.

Full verification logs and failure-ID comparison are in the local conversation
workspace `outputs/v194d_claude_merge/`. They are not runtime data in this commit.

Open / needs Harish:
- Decide how to retire the eleven historical fabricated-data expectations and
  the two now-stale NO_GO/source-identity assertions. They remain failing.
- The synthetic loop is gone, but the EMA loader still has a latest-available
  recorded-candle fallback before NO_DATA. This merge does not change that
  separate date-provenance behaviour or claim it is fixed.


## 2026-11-07 · ChatGPT · v1.9.4d Grok advisory bridge

Branch: `chatgpt/v1.9.4d-grok-regime-bridge`, based on `606332e` by Harish's
explicit approval. The active desk checkout is dirty and was left untouched;
no runtime data was discarded and no active daemon was restarted or deployed.
The prompt's stale safety defaults were explicitly superseded by Harish:
all existing config keys are unchanged, including USER_CONTROLLED, dry-run false,
zero caps and entry window 09:15–15:20.

Checkpoints:
- pre: `pre-chatgpt-v1.9.4d-grok` (backup branch `backup-pre-chatgpt-v1.9.4d-grok`).
- Move 1 `e992ef3`: rules + additive config summary; tag `chpoint-v1.9.4d-move1-config`.
- Move 2 `d6bf7e7`: read-only extractor, async-worker classifier, memory cache and own SQLite observations;
  tag `chpoint-v1.9.4d-move2-engine`.
- Move 3 `a6179b9`: API routes, cached telemetry and collapsed advisory UI;
  tag `chpoint-v1.9.4d-move3-ui`.
- Move 4 (identified by `post-chatgpt-v1.9.4d-grok`): tmp_path tests plus test/review
  hardening: source expiry, aligned FUT/FUT2 volume, minute flip semantics matching
  EMA Proximity, optional startup fault isolation, safe human-readable UI text,
  and ignore rules for the runtime cache. All paths staged explicitly.

No pre-existing strategy, broker, arming or risk-decision backend method changed
(AST comparison tested). The bridge is always ADVISORY_ONLY; network/SQL/file work
runs off the order/event-loop path. Automatic evaluation deduplicates closed-minute
observations. Missing/stale data shows WAIT, not a continuation authorization.
Grok API responses are validated; API errors fall back to labelled local rules.
Local rule output is unscored, and model confidence is self-reported, not win rate.
Webhook requires GROK_WEBHOOK_SECRET; local config/evaluate writes reject foreign
origins. No real Grok API request or broker order was used in verification.

| verification / fixture provenance | passed | failed | skipped |
|---|---:|---:|---:|
| Previous `606332e`, ChatGPT isolated fixtures | 446 | 13 | 13 |
| This branch, same private fixtures + TypeScript dependency available | 469 | 13 | 12 |
| New Grok bridge suite | 22 | 0 | 0 |
| Existing v194b live-switch suite | 62 | 0 | 0 |
| Existing config-hygiene suite | 11 | 0 | 0 |

The 13 full-suite failure IDs are exactly the baseline's 13 IDs, verified by
set comparison. The 23 extra passes are 22 added tests and one former dependency
skip now exercised. The full suite remains NOT GREEN. No old assertion was edited.
A provisional run lost its temporary TypeScript dependency before its parity test;
the final reported run kept that dependency for the entire run and resolved it.
Claude's 419/21 baseline uses other fixtures and is not a quality delta here.

Required v194c UI/scalp test and `analyst/scalp_matrix_8510.py` are absent from
this approved base. Reported UNAVAILABLE, not invented or recreated; Harish approved
proceeding on this basis. Existing Matrix Scalp implementation remains unchanged.

Locks **11/11**; immutable tests **10 passed**; parity **9,059 rows × 17 fields,
100%, zero mismatches**. All inline JS parsed; a mock-DOM smoke passed collapsed
storage behaviour, text-only rendering, Evaluate and stale WAIT. No visual-browser
or live-provider verification is claimed. Sample median latency: deterministic
classifier 0.0104ms, memory reader 0.0034ms; not a scheduling guarantee.

Market DB and instrument-master hashes match the previous checkpoint. Source config
matches 606332e exactly except the new bridge-summary key. Tests write private
config/ledger copies; the new bridge tests use tmp_path. The temporary dependency
symlink was removed. No data/, analyst/replay_data/ or *.sqlite3 path is staged.
Logs and executive report: conversation workspace `outputs/v194d_grok_bridge/`.
Activation in the running app is pending reviewed deployment; existing live
settings were not changed and main/integrate remain untouched.


## 2026-11-07 · ChatGPT · v1.9.5 optional StockMojo adapter

Branch: `chatgpt/v1.9.5-stockmojo-poller`. Harish explicitly approved basing this
on tested `1e9fbff`, retaining the preceding fixes, rather than old integrate/v1.9.
Tags: `pre-chatgpt-v1.9.5-poller` → `post-chatgpt-v1.9.5-poller`.
No shared branch, active checkout, live config, credentials or daemon changed.

Implemented the requested optional provider using a new daemon-thread adapter,
separate from the retired legacy scraper. Rules are in a separate JSON file;
terminal8510_config.json is byte-identical to 1e9fbff. Auth values and exact request
JSON come from environment only; no credential extraction or literal token commits.
No actual authenticated provider request was sent during this work.

Scheduling follows the explicit 180 + [-10,+15] formula (170–195 seconds), with
09:15–15:30 IST weekday/configured-holiday checks. 401/403 HALTS, with no bypass or
automatic access-block retries; 429/503 respects Retry-After and exponential
backoff. Jitter spreads load; no ban-safe or Cloudflare-acceptance claim is made.

The provider's raw source timestamp, not request time alone, determines freshness.
Exact 1m/3m changes require actual timestamp-spaced observations. No interpolation,
zero-padding, date guessing, Indian-unit guessing or synthetic history. Separate
SQLite scope keys avoid combining request scopes. Totals/PCR describe the configured
request universe, not necessarily every chain strike. One memory-cached OI source
contributes to informational Pillar 4; Dhan is retained when StockMojo is unconfigured.
A configured but stale/blocked StockMojo feed cannot pass Pillar 4. OI flow alignment
is context, not writer intent, independent evidence or an execution authorization.
Existing execution/risk methods were verified unchanged by a narrow AST test.

New read-only `/api/stockmojo/live`, cached telemetry and compact Tab 1 badge.
Only the worker handles HTTP/SQLite writes; the score/telemetry reader does neither.
Populated-cache microbenchmark: median 0.0106ms / p95 0.0231ms, sampled locally;
no real-time scheduling guarantee, nor a claim that all parsing/DB work is <2ms.

Provided export QA: 387/387 PE-CE totals agree with Put minus Call. 386 observation
rows and one undated Prev Day baseline. The HH:MM rows have no verifiable source
session date; none were imported into a live database or promoted to live signals.

| verification / isolated fixture lineage | passed | failed | skipped |
|---|---:|---:|---:|
| Prior 1e9fbff full suite, TypeScript dependency available | 469 | 13 | 12 |
| v1.9.5 full suite, same private fixtures | 499 | 16 | 12 |
| New stockmojo poller suite | 33 | 0 | 0 |
| Existing live-switch suite | 62 | 0 | 0 |
| Existing config-hygiene suite | 11 | 0 | 0 |
| Existing Grok suite | 21 | 1 | 0 |

All 13 baseline failed IDs remain. Three added failures are existing assertions:
- test_stockmojo_and_port8600_confluence_integration prohibits the provider's name
  anywhere in HTML (the retired ingest routes remain closed).
- test_05_no_stockmojo_references prohibits the provider's name in source.
- Grok test_05_advisory_only_never_mutates_execution_or_live_guards compares EVERY
  backend function to 606332e, including the two confluence functions deliberately
  changed by this requested feature. Its actual advisory checks pass before that
  broad comparator fails; new narrow execution-function preservation test passes.
None of those old assertions was edited or evaded. Full suite is NOT GREEN.
AGENTS.md and CLAUDE.md record the explicit owner-requested optional reintroduction.

Locks 11/11. Immutable tests 10 passed. Parity 9,059 × 17 fields, 100%, 0 mismatches.
JS syntax and mock-DOM status-state checks passed. The first full run hung in the
local TestClient portal before app startup: reproduced with empty FastAPI, resolved
by local network permission. Stalled test run was interrupted; final reported run
completed, with the startup lifecycle test also passing independently. No production
process was killed. Temporary TypeScript dependency symlink removed after the suite.
Market DB/master hashes unchanged; no data/, replay_data/, *.sqlite3 or .env staged.

Pending activation: Harish was asked for the exact REDACTED request body/expiry ID;
no answer was received yet. Request shape is legacy-compatible, not live-verified.
Set STOCKMOJO_JWT_J, STOCKMOJO_TOKEN_T and STOCKMOJO_REQUEST_JSON privately, confirm
current request/response schema and scope, then review deployment. No active .env
was edited; no credential value should be pasted into chat/Git. Current running
terminal remains unchanged. Review logs/report in conversation workspace
`outputs/v195_stockmojo_poller/`.


## ChatGPT — v1.9.6 institutional telemetry / local qwen (7 Nov 2026)

Branch: chatgpt/v1.9.6-institutional-telemetry-qwen; base 10c22b0.
Moves 1–3: 24d2dec, 29b4a70, cb5dccc. Final checkpoint: post-chatgpt-v1.9.6-inst-qwen.
Owner explicitly authorized removal of old bridge files despite old tests importing
them, and derivation/documentation of SYSTEM_PROMPT and crowding wording. No
compatibility shims or old-test modifications. Retired routes/startup/UI removed.

Deterministic N+1 windows, quality gates, causal reversal persistence and optional
chain evidence feed a local-only qwen2.5:3b interpreter. Deterministic evidence is
persisted before inference. Pillar 4 uses only the OI score component; HTTP overlay
copies prevent mutation of execution telemetry. All original backend functions match
606332e ASTs. Live settings preserved; only retired provider config keys removed.
No broker orders, live arming, active-checkout deployment, credential changes or
production restarts. Historical data never substitutes for live evidence.

| Verification / private fixture lineage | passed | failed | skipped | collection errors |
|---|---:|---:|---:|---:|
| Parent 10c22b0 full suite | 499 | 16 | 12 | 0 |
| v1.9.6 diagnostic continued suite | 470 | 21 | 12 | 2 |
| New v1.9.6 suite | 31 | 0 | 0 | 0 |
| Existing live-switch suite | 62 | 0 | 0 | 0 |
| Existing config-hygiene suite | 11 | 0 | 0 | 0 |

Exact ordinary full-suite command stops at the two old bridge import errors.
Diagnostic --continue-on-collection-errors executes remaining tests: 13 baseline
failures persist; 8 Matrix tests fail after the unchanged 15:05 IST clock cutoff.
55 retired bridge cases no longer collect; raw totals are not directly comparable.
Two existing naming assertions now pass; the third broad identity assertion belongs
to an uncollectable retired bridge suite. Full suite is NOT GREEN. Tests unchanged.
Locks 11/11; immutable tests 10 passed; parity 9059 ×17, 100%, zero mismatches.
Inline JS syntax and mock-DOM checks pass; no browser visual verification claimed.
Temporary dependency symlink removed; no runtime data/sqlite/.env staged.

Supplied CSV: 386 timed rows; final 15:40 volume ratio 0.0128749 causes heuristic
discard, so evaluation uses 15:39: VALID historical, NEUTRAL_MIXED, score7,
confidence0. Source date is unverified; not evidence of a real 7 Nov session.
Completion is a volume/history heuristic, not exchange completion proof.
Real local model probe timed out at 3 seconds and correctly used deterministic
fallback. Mocked valid-model path passes; actual narrative success not claimed.
Measured deterministic full-file median about3.14ms, p95 about4.15ms; no hard
latency guarantee. Confidence is heuristic, not a win probability.
Derived crowding warning: Extreme OI dominance can crowd; watch for profit booking
or mean reversion. Full derived SYSTEM_PROMPT is in the bridge and report artifact.
Review artifacts: outputs/v196_institutional_telemetry in conversation workspace.


## ChatGPT — v1.9.7 local exported-file collector (7 Nov 2026)

Base4580d79; isolated branch chatgpt/v1.9.7-live-collector-and-staging.
Move1 717f606; Move2 814fb4c; final checkpoint post-chatgpt-v1.9.7-collector.
User chose local exported files in Downloads first; no automatic website export,
remote vendor calls, credentials, live arming, production copy or daemon restart.

Chronological CSV/JSON/TSV collector scans135–170s on weekday09:15–15:30 IST;
atomic dedicated snapshots, raw-minute dedup, conflict quarantine, current/future
minute holding, suspected-partial heuristic, restart recovery. Deterministic
prefix calculations occur outside SQL write transactions, with a writer-lock
regression test. Saved filename dates remain declared/unverified historical
evidence, never silently promoted to live confluence. Explicit --scan-now supports
manual historical inspection; final pending15:30 can need a later explicit scan.
The schedule is weekday-only, not a complete exchange holiday calendar.

Progressive warm-up4 rows, N+1 contiguous spans. Early3m/5m weights normalize;
after15m is available,3m is diagnostic and registered full-window weights are
preserved. Session/current regime fields and HEURISTIC_NOT_CALIBRATED are explicit.
Supplied15:39 sample: SESSION_STRONG_BULLISH / NEUTRAL_MIXED; bias+7/100,
confidence0/100. Suspected15:40 discard remains heuristic. Source date unverified.

Bounded latest-pending Qwen daemon queue; generation guards;15s timeout,
keep_alive30m, num_predict220, num_ctx1536. Slim narrative-only output cannot
override deterministic fields. Model and SQL work never occur on cached reads.
Real local-service attempts returned ConnectionError; successful cold/warm model
inference remains UNVERIFIED. Mocked delayed/success/failure paths pass.

| Verification / identical private fixture hashes | passed | failed | skipped | collection errors |
|---|---:|---:|---:|---:|
| Prior v1.9.6 reported continued suite (unfrozen clock) |470|21|12|2|
| Reproduced4580d79 baseline, fixed clock, obsolete imports excluded |478|13|12|0|
| v1.9.7 full suite |503|13|14|0|
| Final telemetry + Matrix + config focus |101|0|0|0|

All13 failure IDs are identical to baseline; no new failed IDs. Eight clock-
dependent failures resolve. Matrix fixture injects11:30; a boundary test verifies
15:05 allowed/15:06 blocked. No production Matrix method changed. Two explicitly
retired bridge modules are skipped before obsolete imports; unrelated failures
remain visible. v196 tests updated only for approved progressive warm-up,15s
model timeout and current-minute completion semantics. Full suite is NOT GREEN.

Tool environment loses async thread/socket wakeups; both comparison runs use an
external uncommitted10ms event-loop polling plugin, not production changes. Host
should rerun ordinary pytest. Fixture hashes/logs/XML and exact13 IDs retained
in conversation outputs/v197_collector. Temporary dependency symlinks removed.
Locks11/11; immutable tests10/10; parity9059x17 fields100%, zero mismatches.
Runtime config and terminal8510_backend.py are byte-identical to4580d79. Pinned
market/master fixtures unchanged. No data/, replay_data/, sqlite or .env staged.

Actual UI script syntax/mock-DOM states pass. Browser validation BLOCKED: local
listener EPERM and browser file-URL security rejection; no workaround attempted.
Offline migration/rollback rehearsal PASS:1 old row preserved,21 unique additions,
repeat import deduplicated, rollback restores1 and source files; config unchanged.
One explicit historical scan saved376 rows through15:30 in dedicated staging
recordings; collector is not installed as a persistent service.
Measured386-row engine median8.45ms/p9512.01ms under shared tool load; cache
median0.50ms/p950.77ms. No hard4ms guarantee; full-file ingestion can exceed25ms.
Standalone launcher, probe, reports and recordings in outputs/v197_collector.
GitHub fetch failed (DNS unavailable); final push result is recorded in external
report. Production deployment remains NOT APPROVED.


---

## 2026-11-11 — agy: Complete Fix for 8 EMA Prototype (Port 8510)

Branch: `fix/8510-algo-v206`
Commit: `AGY COMPLETE FIX FOR EMA PROTOTYPE NEVER EDIT WITHOUT ASKING HARISH`
Tag: `post-agy-v206-ema8-prototype`

### Summary of Engineering & Remediation Work:
1. **Intrabar 30s Retest & End-of-Bar Close Parity (`analyst/ema8_prototype_8510.py`):**
   - Fixed early `return` in `backtest_ema8_prototype_at_bar` so the 15m candle close cross is evaluated at the end of every bar, ensuring subsequent candles are properly armed when crossing occurs.
   - Fixed bracketing condition in `_touch_retest()` (`low <= e8 + tol and high >= e8 - tol`).
   - Clamped entry spot directly to live 30s dynamic 8 EMA line (`e8_tick`).
2. **Exit Priority & Spot Direction (`analyst/terminal8510_backend.py`):**
   - Reordered exit hierarchy in `simulate_backtest_spot_exit`: Fixed 40-pt Target and 40-pt SL are checked first; `OPPOSITE_8EMA_CLOSE` triggers on candle close only if trade remains open.
   - Integrated `prototype_spot_is_long()` in `get_active_positions` (prevents PE instant 0s exit caused by `side == "BUY"`).
   - Hardcoded 40/40 option brackets in `execute_order_with_chaser` for `is_proto_order`; disabled super-trail jump interference.
   - Updated exit timestamp display for opposite close exits to reflect actual candle close time.
3. **Config & Filter Discipline (`analyst/terminal8510_config.json`):**
   - Restored `mj_sir_lecture`, `tradingview_ema_cross`, and `bollinger_enabled: true`.
   - Optional filters (`ribbon_filter_enabled`, `ema_separation_enabled`, `supertrend_enabled`) explicitly set to `false` (turned OFF) for pure 4/8 EMA baseline testing.
4. **Verification & Testing:**
   - Unit tests: `analyst/test_ema8_prototype_8510.py` $\rightarrow$ 9/9 passed.
   - Core feature locks: `scripts/verify_locks.py` $\rightarrow$ 11/11 passed.
   - Immutable feature lock suite: `test_immutable_feature_lock.py` $\rightarrow$ 10/10 passed.
   - Multi-day backtest verified across Nov 3, Nov 4 (+40 pts net), Nov 6 (+22.18 pts net), and Nov 9 (+233.82 pts net).
   - Port 8510 live server restarted and verified via `/api/telemetry` and `/api/replay/run`.
