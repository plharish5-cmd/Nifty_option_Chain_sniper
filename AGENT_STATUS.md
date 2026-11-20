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
| `agy/ema8-proto-corrections` | Antigravity | **done, `b1d23ca`**, tag `post-agy-ema8-proto-corrections` | **NO — awaiting Harish** |
| `gemini/v2.0.24-proto-oi-filters` | Gemini/Cursor | **done, `41fd6be` + momentum v2** (see block) tags `pre/post-gemini-v2.0.24` | **NO — awaiting Harish** |
| `gemini/v2.0.25-proto-candle-trail` | Gemini/Cursor | **done, `18eaee4`** tags `pre/post-gemini-v2.0.25` | **NO — awaiting Harish** |
| `gemini/v2.0.26-oi3m-separate-feature` | Gemini/Cursor | **done, `4ec5afa`** tags `pre/post-gemini-v2.0.26` | **NO — awaiting Harish** |
| `gemini/v2.0.27-pece-volume` | Gemini/Cursor | **done, `e2f5199`** tags `pre/post-gemini-v2.0.27` | **NO — awaiting Harish** |
| `gemini/v2.0.28-dhan-expiry-parse` | Gemini/Cursor | **done, `d35ef9d`** tags `pre/post-gemini-v2.0.28` | **NO — awaiting Harish** |
| `gemini/v2.0.29-backtest-sweep` | Gemini/Cursor | **done, `1bedd34`** tags `pre/post-gemini-v2.0.29` | **NO — awaiting Harish** |
| `gemini/v2.0.30-ema-display-audit` | Gemini/Cursor | **done, `2631903`** tags `pre/post-gemini-v2.0.30` | **NO — awaiting Harish** |
| `gemini/v2.0.31-no-offhours-cleanup` | Gemini/Cursor | **done, `5c58145`** tags `pre/post-gemini-v2.0.31` | **NO — awaiting Harish** |
| `agy/v2.0.32-reversal-revamp` | AGY/Cursor | **WIP foundation** tags `pre-agy-v2.0.32` | **NO — awaiting Harish** |
| `gemini/v2.0.34-finish-and-enable` | Gemini/Cursor | **done** tags `pre/post-gemini-v2.0.34` | **NO — awaiting Harish** |
| `gemini/v2.0.35-reversal-oi-panel` | Gemini/Cursor | **done** tags `pre/post-gemini-v2.0.35` | **NO — awaiting Harish** |
| `gemini/v2.0.36-clean-and-scope` | Gemini/Cursor | **done** tags `pre/post-gemini-v2.0.36` | **NO — awaiting Harish** |
| `gemini/v2.0.37-oi-filter-backtest` | Gemini/Cursor | **done** (run job + backtest path fix) tags `pre/post-gemini-v2.0.37` | **NO — awaiting Harish** |

---

## 2026-11-18 · Gemini/Cursor · `gemini/v2.0.37-oi-filter-backtest`

**Goal:** RUN — Proto OI filters `total_oi` / `fresh_oi` / `writer_flow` on max **chain** history (not full spot history).

**Coverage:** 26 spot days, **25 chain_snap days** (2026-10-12..2026-11-17), 3573 snapshots. Held-out baseline **−66.6 pts** (34 trades).

**Result:** All three block configs **0 trades touched on held-out** → **NOT TESTABLE on test**; **keep OFF**. Train-only blocks for fresh/writer (−17 pts train) — not a PASS. Fix: `backtest_board_reads_at` spot from `candles_1m` + recording chain fallback.

**Report (gitignored):** `data/terminal8510/sweeps/20261118-165037/OI_FILTER_BACKTEST_REPORT.md`

**Verify:** `test_proto_oi_filters_8510.py` 12/12; `verify_locks` 11/11.

---

## 2026-11-18 · Gemini/Cursor · `gemini/v2.0.36-clean-and-scope`

**Goal:** Paper ledger hygiene + desk scoped to Prototype, Reversal, 1H Flagship only (config reversible).

**Done:** `desk_scope_8510` + patch `patch_desk_scope_three_lanes_8510.py`; `clean_paper_ledger_8510.py` (--preview / --drop); `ledger_guard_8510` + conftest sqlite guard; orders book API/UI hide fixtures & disabled lanes; expired contract cleanup already in off-hours helper.

**Verify:** `verify_locks.py` 11/11; `pytest analyst/test_clean_scope_8510.py analyst/test_offhours_cleanup_8510.py` 14/14.

**Owner run:**
```bash
python3 scripts/clean_paper_ledger_8510.py --preview
python3 scripts/clean_paper_ledger_8510.py --hide
python3 scripts/patch_desk_scope_three_lanes_8510.py
# restart engine_8510_live.py
```

**Follow-up (ledger hide, same branch):** Append-only ledger rejects DELETE; cleanup uses `execution_provenance.hidden=1` (`ledger_execution_hide_8510.py`). Script `--hide` (not delete); `order_book_row_visible` + API/UI also drop expired contracts and provenance-hidden rows. Tags `pre/post-gemini-v2.0.36-ledger-hide`.

---

## 2026-11-18 · Gemini/Cursor · `gemini/v2.0.35-reversal-oi-panel`

**Goal:** Independent **8600 OI Filters** panel on the 8 EMA Reversal card (`scanner.reversal.oi_filters` only).

**Done:** Cockpit `rev*` controls + present-key guard; live telemetry on `crossover_reversal` via `_sync_reversal_crossover_desk_status` (display-only); `test_reversal_oi_panel_8510.py` (6) independence + HTML wiring.

**Verify:** `verify_locks.py` 11/11; `pytest analyst/test_reversal_oi_panel_8510.py` 6/6.

**Run:**
```bash
git checkout gemini/v2.0.35-reversal-oi-panel
python3 scripts/verify_locks.py
PYTHONPATH=. .venv/bin/python -m pytest analyst/test_reversal_oi_panel_8510.py -q
```

---

## 2026-11-18 · Gemini/Cursor · `gemini/v2.0.34-finish-and-enable`

**Goal:** Finish Reversal for real money behind cross-lane double-fill gate; enable filters **log_only**; Prototype path unchanged; Stage 2/3 paper-locked.

**Done:** Prototype-grade contract pick + live candle trail + chaser path for `CROSSOVER`; pre-reserve `ema_lane_entry_gate_8510`; `test_reversal_crosslane_8510.py` **green (cross-lane + Stage golden + integration)** → Reversal may use `execution_mode: LIVE_ALLOWED` after owner runs `scripts/patch_finish_and_enable_8510.py`; v2.0.33 price-confirm + matrix recorder wired; `reversal_stage_variants()` in proto_sweep payload + `backtest_lab.merge_proto_overrides` reversal merge; cockpit reversal toggles + API.

**Verify:** `verify_locks.py` 11/11; `pytest analyst/test_reversal_crosslane_8510.py` 9/9; `test_reversal_revamp_8510.py` 6/6.

**Not verified:** live Dhan order fill; full PE-CE cockpit volume polish; dedicated reversal trade runner in Backtest Lab (factors exported for merge).

**Run (owner machine):**
```bash
git checkout gemini/v2.0.34-finish-and-enable
python3 scripts/verify_locks.py
PYTHONPATH=. .venv/bin/python -m pytest analyst/test_reversal_crosslane_8510.py analyst/test_reversal_revamp_8510.py -q
python3 scripts/patch_finish_and_enable_8510.py   # backs up terminal8510_config.json
```

**Double-fill gate:** **PASSED** — Reversal **real-money enabled** once patch sets `scanner.reversal.execution_mode` to `LIVE_ALLOWED` (default scaffold stays `PAPER_ONLY` until patch).

---

## 2026-11-18 · AGY/Cursor · `agy/v2.0.32-reversal-revamp` (foundation)

**Goal:** Prototype-grade Reversal revamp scaffold — **all new options default OFF**; Stage 2/3 legacy path preserved inside `mj_sir_stage_reversal_8510.py`.

**Done:** `resample_nse_5m`; `reversal_revamp_8510.py` (5m/15m/both, pre-arm, separation, execution labels); `ema_lane_lock_8510.py` + backend gates (OI import, lock telemetry); `scripts/patch_reversal_revamp_8510.py`; `test_reversal_revamp_8510.py` (6).

**Not in this commit (follow-up):** full cockpit filter toggles; Reversal live candle-trail exit loop; Prototype-grade contract picker on Reversal; `proto_sweep` factor rows; wider test matrix from prompt; live Dhan.

**Verify:** `verify_locks.py` 11/11; `pytest analyst/test_reversal_revamp_8510.py` 6/6.

---

## 2026-11-18 · Gemini/Cursor · `gemini/v2.0.31-no-offhours-cleanup`

**Goal:** Block **all** new entries (paper, dry-run, live) when NSE closed or spot stale; catch-all cleanup closes EOD / off-hours / expired strays; every auto-close logged + cockpit **Auto-closed today** card. Real `live_entry_window` unchanged. Exits off-hours still allowed via `enforce_market_hours=False`.

**Files:** `analyst/offhours_cleanup_8510.py`, `terminal8510_backend.py`, `Algorithmic_Trading_Workspace_8510.html`, `test_offhours_cleanup_8510.py` (8); `test_phase5_eod_8510.py` (fixture symbol).

**Verify:** `verify_locks.py` 11/11; `pytest analyst/test_offhours_cleanup_8510.py` 8/8; `test_phase5_eod_8510.py` 5/5.

**Commit:** `5c58145` (+ v15/conftest follow-up); tags `pre-gemini-v2.0.31` / `post-gemini-v2.0.31`.

**Follow-up:** `test_v15_fixes_8510` off-hours override → asserts entry **blocked**; `conftest` market-only entry guard for general unit tests; full `pytest analyst`: **866 passed, 35 failed, 42 errors** (failure count matches handover baseline).

**Not verified:** live scanner loop with stuck paper positions; browser.

---

## 2026-11-18 · Gemini/Cursor · `gemini/v2.0.30-ema-display-audit`

**Goal:** Display + audit only — **8EMA (live)** vs **8EMA (15m close)** in Prototype cockpit; touch audit lines in `ema8_prototype_log`; **>5s stale spot → EMA STALE**, worker skips touch (fail-open). **No EMA maths change**, no rounding, no new engine.

**Files:** `analyst/ema8_display_audit_8510.py`, `cockpit_views_8510.py`, `terminal8510_backend.py`, `Algorithmic_Trading_Workspace_8510.html`, `test_ema8_display_audit_8510.py` (5).

**Verify:** `verify_locks.py` 11/11; `pytest analyst/test_ema8_display_audit_8510.py` 5/5.

**Commit:** `2631903`; tags `pre-gemini-v2.0.30` / `post-gemini-v2.0.30`.

**Not verified:** live cockpit screenshot at 10:15; browser.

---

## 2026-11-18 · Gemini/Cursor · `gemini/v2.0.29-backtest-sweep`

**Goal:** Staged Prototype filter sweep harness on `backtest_lab_8510` — held-out 70/30, Benjamini–Hochberg FDR (q=0.10), block-bootstrap by day, permutation null, random-sign floor. Stages 0–3 (not 2^N). Honest “keep filters OFF” verdict allowed.

**Files:** `analyst/proto_sweep_8510.py`, `analyst/test_proto_sweep_8510.py` (6); `backtest_lab_8510.py` adds `merge_proto_overrides`, `run_ema8_prototype_trades`.

**Run (owner machine, recorded spot + chain DB):**
```bash
python3 -m analyst.proto_sweep_8510 --from YYYY-MM-DD --to YYYY-MM-DD --stage all \
  --out data/terminal8510/sweeps/$(date +%Y%m%d-%H%M%S)/
```
Outputs: `sweep_ranked.csv`, `sweep_report.md`, `sweep_result.json` in that folder (under `data/`, gitignored).

**Verify:** `verify_locks.py` 11/11; `pytest analyst/test_proto_sweep_8510.py` 6/6 (+ backtest_lab tests).

**Commit:** `1bedd34`; tags `pre-gemini-v2.0.29` / `post-gemini-v2.0.29`.

**Not verified:** full sweep execution in sandbox (no recorded market DB / long engine run).

---

## 2026-11-17 (night) · Gemini/Cursor · `gemini/v2.0.28-dhan-expiry-parse`

**Goal:** Fix `_dhan_credentials_live_ok` parsing `token_valid_till` human string (`%d %b %Y %H:%M IST`) with `fromisoformat` → false "unreadable" disable.

**Fix:** Honor `is_expired` / `token_expired` / `connected`; parse `exp_ts` → `token_expiry_utc` (ISO) → human `strptime`. Fail-safe unreadable only when no parseable expiry fields. `get_status()` now also returns `exp_ts`, `token_expiry_utc`, `token_expired` from `decode_token_expiry`.

**Files:** `analyst/terminal8510_backend.py` (`parse_dhan_token_expiry_from_status`), `analyst/test_live_exit_fixes_8510.py` (+5 guard tests).

**Verify:** `verify_locks.py` 11/11; `pytest analyst/test_live_exit_fixes_8510.py` 20/20; full `pytest analyst -q --continue-on-collection-errors` on owner machine vs baseline (~829/34/42 +5 passes expected).

**Commit:** `d35ef9d`; tags `pre-gemini-v2.0.28` / `post-gemini-v2.0.28`.

**Not verified:** live Dhan session in sandbox.

---

## 2026-11-17 (late night) · Gemini/Cursor · `gemini/v2.0.27-pece-volume`

**Owner UNLOCK (module #4 only):** PE-CE Matrix — Put/Call/PE−CE **per-interval volume** columns (additive). **Re-lock:** `LOCKED_MANIFEST.json` hashes for `pe_ce_matrix.py` + `PeceMatrix.tsx` only; `relock_history` entry 2026-11-17. **Parity honesty:** per-interval Δ cumulative Dhan `ce_vol`/`pe_vol` summed over the same strike scope as the table; owner's reference sheet is a **different vendor** — method match, not exact figures.

**Part A files:** `ict_smc_app/backend/app/engine/pe_ce_matrix.py` (`interval_volumes_from_cumulative`), `PeceMatrix.tsx`, `test_pe_ce_matrix.py`, `test_immutable_feature_lock.py`, `LOCKED_MANIFEST.json`.

**Part B (8510, not locked):** `require_volume_confirm` default **false** on `oi_filters.momentum`; churn downgrades S-based CE/PE → NEUTRAL (fail-open on unknown). Cockpit `· vol: backed|churn` on momentum rows. `scripts/patch_add_oi_filters_8510.py` + `proto_oi_filters_8510.py` / `ema8_prototype_8510.py` / `chain_reads_8510.py` / `Algorithmic_Trading_Workspace_8510.html` / `test_oi_momentum_8510.py`.

**Verify:** `scripts/verify_locks.py` **11/11**; `pytest ict_smc_app/backend/tests/test_pe_ce_matrix.py test_immutable_feature_lock.py analyst/test_oi_momentum_8510.py` (37+); `test_proto_oi_filters_8510.py` 11/11.

**Commit:** `e2f5199`; tags `pre-gemini-v2.0.27` / `post-gemini-v2.0.27`.

**Not verified:** live Dhan feed vs owner sheet magnitudes; browser PE-CE table; 8510 cockpit vol labels after engine restart.

---

## 2026-11-17 (night) · Gemini/Cursor · `gemini/v2.0.26-oi3m-separate-feature`

**Goal:** Isolated **oi_3m** gate (3-min `S=dD/T` via `compute_momentum_oi_reads` import) + **8600 context panel** (display-only). Config `scanner.oi_3m_feature` — separate from `oi_filters`. Default all OFF / log_only.

**Files:** `analyst/oi_3m_gate_8510.py`, `analyst/oi_context_panel_8510.py`, `analyst/test_oi_3m_separate_8510.py` (10), `scripts/patch_add_oi_3m_feature_8510.py`; wiring `ema8_prototype_8510.py`, `terminal8510_backend.py`, `engine_8510_live.py`, `Algorithmic_Trading_Workspace_8510.html`.

**Verify:** `verify_locks.py` 11/11; `pytest analyst/test_oi_3m_separate_8510.py` 10/10; `test_proto_oi_filters_8510.py` 11/11.

**Commit:** `4ec5afa`; tags `pre-gemini-v2.0.26` / `post-gemini-v2.0.26`.

**Not verified:** full analyst pytest diff; live 8600 bridge for Smart OI / PE-CE / CVD rows; browser.

---

## 2026-11-17 (late) · Gemini/Cursor · `gemini/v2.0.24-proto-oi-filters` — OI momentum v2 (S = dD/T)

**Goal:** Replace raw Cr/min direction with scale-free **Significance** `S = dD/T` (Option A, no per-minute division). Gate on **S only**; **B** (level bias) and cockpit display only; optional `require_cumd_confirm` (default off). Window snapshot must fall in `window_min±2` min band or direction `None` (fail OPEN). Units: `_to_lakhs()` at read boundary.

**Files:** `analyst/chain_reads_8510.py` (core math), `proto_oi_filters_8510.py`, `ema8_prototype_8510.py`, `scripts/patch_add_oi_filters_8510.py`, `Algorithmic_Trading_Workspace_8510.html`, `analyst/test_oi_momentum_8510.py` (10 tests), `test_proto_oi_filters_8510.py` defaults.

**Config `oi_filters.momentum`:** `{ "window_min": 10, "sig_min": 0.15, "sig_extreme": 0.30, "require_cumd_confirm": false }` — `deadzone_cr_per_min` retired/ignored.

**Verify:** `verify_locks.py` 11/11; `pytest analyst/test_oi_momentum_8510.py` 10/10; `test_proto_oi_filters_8510.py` 11/11.

**Commit:** `51d6268`; tags `pre-gemini-v2.0.24b-momentum-s` / `post-gemini-v2.0.24b-momentum-s`.

**Not verified:** full analyst suite diff this session; live cockpit vs 8600; browser persist.

---

---

## 2026-11-17 (evening) · Gemini/Cursor · `gemini/v2.0.24-proto-oi-filters` — OI momentum Cr/min

**Goal:** Replace **level-based** direction on `writer_flow`, `total_oi`, `fresh_oi` with rolling Cr/min **divergence/convergence** (window + dead-zone in config). `price_oi` / `volume_oi` unchanged. Insufficient history → `direction: None` (fail OPEN). All switches still default OFF, `mode: log_only`.

**Files**
- `analyst/chain_reads_8510.py` — `momentum_direction_from_crpm`, `compute_momentum_oi_reads` (full-chain OI sums from `chain_snap` rows)
- `analyst/desk8600_board_8510.py` — board override for the three momentum reads
- `analyst/proto_oi_filters_8510.py`, `analyst/ema8_prototype_8510.py` — `oi_filters.momentum` defaults
- `analyst/terminal8510_backend.py`, backtest path — pass `momentum_cfg`
- `Algorithmic_Trading_Workspace_8510.html` — cockpit Cr/min lines on filter rows
- `scripts/patch_add_oi_filters_8510.py` — merges `momentum` block
- `analyst/test_oi_momentum_8510.py` — divergence / convergence / net / dead-zone / no-history
- `analyst/test_proto_oi_filters_8510.py` — +1 momentum config load

**Config:** `scanner.ema8_prototype.oi_filters.momentum`: `{ "window_min": 10, "deadzone_cr_per_min": 0.05 }` via patch script only (not committing `terminal8510_config.json`).

**Verify:** `scripts/verify_locks.py` 11/11; full `.venv` `PYTHONPATH=. pytest analyst -q --continue-on-collection-errors` (17 Nov ~21:09 IST): **826 passed / 34 failed / 42 errors** — same failure count as prior OI-filters run (818/34/42); **no new failed test IDs** attributable to momentum (`test_oi_momentum_8510` 6 pass, `test_proto_oi_filters_8510` 11 pass). Expected after 15:20 IST: `test_phase5_eod_*` ×3, extra `test_phase7_*` wall-clock sensitivity.

**Commit:** `41fd6be` on `gemini/v2.0.24-proto-oi-filters`; `post-gemini-v2.0.24` moved to this SHA; pushed.

**Recorder note:** `market_recorder_8510.record_chain` stores every strike in the Dhan OC payload (not the 4-strike matrix). Momentum sums **all** strikes per snapshot. Harish should still spot-check cockpit Cr/min vs 8600 OI-Change chart in live hours.

**Ops:** log-only week → points saved vs cost on held-out days → only then `block` on `fresh_oi`/`total_oi` (per guide). **Block live:** not recommended until log shows net benefit.

**Browser (17 Nov ~21:12 IST, hard-refresh):** 8600 OI Filters block present; toggling **Fresh Change in OI** persisted across reload; filter rows show **—** after hours (no chain snapshots / stale reads — expected). No console errors captured via CDP during the check. **Restart 8510** after `git pull` so the running engine loads `41fd6be` Python (momentum + Cr/min telemetry); HTML may refresh without restart.

**Not verified here:** live-session Cr/min vs 8600 OI-Change chart parity (needs market hours + engine restart on this branch).

---

## 2026-11-17 · Gemini/Cursor · `gemini/v2.0.24-proto-oi-filters`

**Goal:** Five independent ON/OFF switches (`writer_flow`, `price_oi`, `total_oi`, `fresh_oi`, `volume_oi`) gate 8 EMA Prototype entries via 8600 board directions. Defaults: all OFF, `mode: log_only`, `rule: all_agree`, stale/missing fail OPEN.

**Files**
- `analyst/proto_oi_filters_8510.py` — `oi_switch_block`, backtest read helper
- `analyst/ema8_prototype_8510.py` — wired at touch; `DEFAULT_CFG.oi_filters`
- `analyst/terminal8510_backend.py` — board reads into intrabar/closed eval; telemetry; filter API
- `analyst/backtest_lab_8510.py` — optional `spec.oi_filters` on private config
- `Algorithmic_Trading_Workspace_8510.html` — Prototype “8600 OI Filters” cockpit
- `scripts/patch_add_oi_filters_8510.py` — narrow config patch (backs up first)
- `analyst/test_proto_oi_filters_8510.py` — unit tests (10 passed)

**Defaults:** `mode=log_only`, `rule=all_agree`, all switches OFF, `neutral_blocks=false`, stale/None fail OPEN.

**Verify:** `scripts/verify_locks.py` (11/11); `pytest analyst/test_proto_oi_filters_8510.py` (10); full `pytest analyst` **818 passed / 34 failed / 42 errors** (17 Nov ~20:42 IST) — compare to handover §6 baseline; no new failures attributed to this feature.

**Ops:** `python3 scripts/patch_add_oi_filters_8510.py` then restart 8510 + hard-refresh.

**Not verified here:** live 8600 bridge, browser click-through (toggles persist / 0 console errors).

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
   - Multi-day backtest verified across Nov 3, Nov 4 (+40 pts net), Nov 6 (+22.18 pts net), and Nov 9 (+233.82 pts net).
   - Port 8510 live server restarted and verified via `/api/telemetry` and `/api/replay/run`.

5. **Timestamp Display Format Fix (Per Harish's Approval):**
   - Fixed malformed timestamp strings (e.g. `2026-11-03T14:30:30:00+05:30`) resulting from double `:00` appends on precise intrabar seconds.
   - `analyst/desk_timestamp_8510.py`: Truncates redundant time fields to `HH:MM:SS` before ISO parsing, cleanly rendering timestamps as `DD Mon YYYY, HH:MM:SS IST`.
   - `analyst/terminal8510_backend.py`: Added `_to_iso()` safe formatting helper; same-bar / opposite-close exits show actual bar close time (`10:30` instead of `10:15`), eliminating reverse duration artifacts.
   - Added unit test `test_malformed_double_seconds` in `analyst/test_desk_timestamp_8510.py` (5/5 passed).

---

## 2026-11-11 — agy: 8 EMA Prototype Corrections (Phases 1 to 7) — Port 8510

Branch: `agy/ema8-proto-corrections`
Base: `origin/fix/8510-algo-v206` @ `ef3ded4`
Tags: `pre-agy-ema8-proto-corrections` @ `ef3ded4`, `post-agy-ema8-proto-corrections` @ `b1d23ca`

### Remediation Deliverables (Phases 1 through 7):
1. **Phase 1 — Real Exit Orders (`analyst/terminal8510_backend.py`):**
   - Built unified `_close_position_with_broker(pos, reason, spot_exit)` close path used by both live and paper executions.
   - Live exits route all triggers (target, stop, opposite close, EOD, manual, kill switch) through companion SL-L cancel confirmation, fresh marketfeed quote check, aggressive limit order with chasing loop (`place_order`/`modify_order`), and real fill ledger finalization.
   - Double-sell prevented via per-position `asyncio.Lock` and `EXIT_PENDING` ledger state.
   - If sell order is rejected: position marks `status='EXIT_FAILED'`, raises cockpit banner, re-places protective SL-L at Dhan.
   - Unit tests: `analyst/test_live_exit_orders_8510.py` (7/7 passed).

2. **Phase 2 — Option Exit is 40 Premium Points (`analyst/terminal8510_backend.py`):**
   - For `is_proto_order`, initial option target and stop levels in `execute_order_with_chaser` set to entry ± 40.0 points flat (instead of delta-scaled).
   - Non-prototype lanes (Flagship, Stage 2/3, Crossover, Matrix Scalp) remain delta-scaled.
   - Unit tests: `analyst/test_phase2_option_points_8510.py` (2/2 passed).

3. **Phase 3 — State: Timestamps, Persistence, One Arm (`analyst/ema8_prototype_8510.py`, `analyst/terminal8510_backend.py`):**
   - `cross_bar_open_ts`: Identifies crossing candle by open unix timestamp. No entry permitted on crossing candle.
   - `consumed_cross_ts`: Tracks consumed cross; exactly one trade per cross.
   - SQLite table `ema8_prototype_state` persists state across restarts.
   - Open position state derived directly from SQLite `positions` ledger (`status='OPEN'`), eliminating in-memory state drift.
   - Unit tests: `analyst/test_phase3_state_8510.py` (6/6 passed).

4. **Phase 4 — Retouch Distance & Movement from Correct Side (`analyst/ema8_prototype_8510.py`, `analyst/terminal8510_backend.py`):**
   - Retouch tolerance `touch_tol_pts = 3.0` points (Harish: ±2 to 3 points).
   - Moving away requirement: Price must first move > 3.0 points away from dynamic 8 EMA before a return to within 3.0 points triggers entry.
   - State `arm["away_seen"]` tracks this per arm across ticks and restarts.
   - Slow market exact line control: `touch_tol_slow_pts = 0.0` with manual toggle `slow_market_exact_line`.
   - Unit tests: `analyst/test_phase4_retouch_8510.py` (8/8 passed).

5. **Phase 5 — EOD Square-Off & Alternating CE/PE Trades (`analyst/terminal8510_backend.py`, `analyst/ema8_prototype_8510.py`):**
   - EOD square-off at desk's `eod_square_off_time` (15:20) squares off open prototype positions through Phase 1 close path (`EOD_SQUARE_OFF_15:20`) and clears pending arm.
   - Strict CE/PE alternation: Exactly 1 entry per cross; after a trade closes (target, stop, opposite close, EOD), no same-direction re-entry until an opposite 15m close arms the other side.
   - Unit tests: `analyst/test_phase5_eod_8510.py` (5/5 passed).

6. **Phase 6 — Backtest Matches Live Specification (`analyst/terminal8510_backend.py`):**
   - `simulate_prototype_spot_exit`: Forward walk starts strictly from the first completed 1m candle after entry (`ts > entry_ts`).
   - Opposite 8 EMA close checked ONLY at 15m candle closes (including entry candle close).
   - 30-second grid reads completed 1m candles (`day_1m.ts <= tick_ts`).
   - Ambiguous candles touching both target (+40) and stop (-40) marked `AMBIGUOUS` with dual scenario reporting (stops-first vs targets-first).
   - Synthetic prototype trades reported with zero fake delta and zero fake INR; real spot entry and separate dynamic 8 EMA column preserved.
   - Replay respects CE/PE alternation.
   - Unit tests: `analyst/test_phase6_backtest_8510.py` (8/8 passed).

7. **Phase 7 — Cockpit and Log (`analyst/terminal8510_backend.py`, `engine_8510_live.py`, `Algorithmic_Trading_Workspace_8510.html`):**
   - Persistent SQLite table `ema8_prototype_log` (`time`, `event`, `direction`, `dynamic_8ema`, `spot`, `reason`, `position_id`). Logs all transitions (ARMED, RETOUCH SEEN, ENTRY PLACED, TARGET HIT, STOP HIT, OPPOSITE CLOSE, EOD SQUARE OFF, CANCELLED, EXIT FAILED).
   - Strategy card displays plain English lifecycle phases:
     `WAITING FOR CLOSE`, `ARMED CE` / `ARMED PE`, `WAITING FOR RE-TOUCH (away_seen: YES/NO)`, `ENTRY`, `EXITED — <reason>`, `FLIPPED TO EMA SHORT / LONG`.
   - Filter block reasons displayed ONLY when filter is ON: `4-8 Attached (Ribbon ON)`, `EMA Separation`, `SuperTrend`, `Opposite Close — Exited`, `EXIT FAILED — <Dhan error>`.
   - Cockpit controls: Radio buttons for `retouch_requires_away` (ON/OFF) and `slow_market_exact_line` (Normal ±3 pts / Slow market exact line 0.0 pts) wired to `/api/scanner/ema8_prototype/filters`.
   - Active Positions table renders `EXIT_PENDING` (badge-gold) and `EXIT_FAILED` (badge-bear with tooltip error) under `Status / Source` column.
   - Unit tests: `analyst/test_phase7_cockpit_log_8510.py` (6/6 passed).

### Verification Summary:
- **Full Prototype Suite:** 51/51 tests passing (`pytest analyst/test_live_exit_orders_8510.py analyst/test_phase2_option_points_8510.py analyst/test_phase3_state_8510.py analyst/test_phase4_retouch_8510.py analyst/test_phase5_eod_8510.py analyst/test_phase6_backtest_8510.py analyst/test_phase7_cockpit_log_8510.py analyst/test_ema8_prototype_8510.py`).
- **Core Feature Locks:** 11/11 locked modules verified intact against `LOCKED_MANIFEST.json` (`python scripts/verify_locks.py`).
- **Immutable Lock Suite:** 10/10 passed (`ict_smc_app/backend/tests/test_immutable_feature_lock.py`).
- **Safety Integrity:** No dry-run gates or daily caps restored; other strategy modules unchanged.

---

## 2026-11-11 — AGY — In-App Master Controls & Safety Rollback Instrumentation

### Scope & Delivered Capabilities:
1. **Interactive In-App Master & Route Controls (`Algorithmic_Trading_Workspace_8510.html`):**
   - Added **Master Control** (`ACTIVE` vs `PAUSED / OFF`) directly to the 8 EMA Prototype card.
   - Added **Route Control** (`LIVE_ALLOWED` vs `PAPER_ONLY`) directly to the card.
   - Switching to `PAUSED / OFF` immediately posts `{enabled: false}` to `/api/scanner/ema8_prototype/filters` without requiring terminal access or app restart.
   - UI status badge dynamically displays `PAUSED` (red badge), phase `DISABLED (PAUSED IN COCKPIT)`, block reason `Prototype manually PAUSED in cockpit`, and badge `Prototype · DISABLED`.
   - Switching to `PAPER_ONLY` safely routes signals to paper ledger only, preventing live Dhan order placement.

2. **Backend Pause Bypass (`analyst/terminal8510_backend.py`):**
   - Both `start_ema8_prototype_intrabar_worker` and `evaluate_candle_dataframe` immediately halt scanning and trade entry when `enabled=False`.
   - `_sync_ema8_prototype_desk_status` marks `state="PAUSED"`, `phase="DISABLED"`, and `paper_only_lane=True` when `enabled=False`.

3. **Dedicated Safety Rollback Utility (`scripts/take_back_ema8_prototype.py`):**
   - `--status`: Inspects git branch, commit hash, prototype enabled state, execution mode, and tolerance.
   - `--disable`: Instantly pauses prototype in config (`enabled=false`) without code changes.
   - `--enable`: Re-enables prototype in config.
   - `--paper-only`: Forces prototype to paper execution mode.
   - `--live-allowed`: Restores live execution mode.
   - `--git-rollback`: Checks out tag `pre-agy-ema8-proto-corrections` (commit `ef3ded4`) cleanly.

4. **Permanent Remote Rollback Anchor:**
   - Safety branch `backup-pre-ema8-proto-corrections` created at `ef3ded4` and pushed to `origin`.

### Verification:
- **Prototype + Workspace Suite:** 55/55 passed (`pytest analyst/test_live_exit_orders_8510.py analyst/test_phase2_option_points_8510.py analyst/test_phase3_state_8510.py analyst/test_phase4_retouch_8510.py analyst/test_phase5_eod_8510.py analyst/test_phase6_backtest_8510.py analyst/test_phase7_cockpit_log_8510.py analyst/test_ema8_prototype_8510.py analyst/test_workspace_8510_html.py`).
- **Locks:** 11/11 intact (`python scripts/verify_locks.py`).
- **Immutable Lock Suite:** 10/10 passed (`pytest ict_smc_app/backend/tests/test_immutable_feature_lock.py -q`).




---

## 2026-11-11 · Claude · `claude/ema8-proto-live-exit-fixes`
Tag: pre-claude-ema8-proto-live-exit-fixes → post-claude-ema8-proto-live-exit-fixes
Branched from: `agy/ema8-proto-corrections` @ `f89f950` (contains all of AGY's Phases 1–7 and its in-app toggles)

Harish asked for the live-exit faults found in the read-only review of `f89f950` to be fixed.
Only `analyst/terminal8510_backend.py` changed, plus one new test file. No strategy rule,
signal, target or stop was changed for any lane. No config file was edited. No limit added.

What changed:
- **Sell only what was bought.** New `positions` columns `live_filled`, `live_order_id`,
  `live_fill_qty`, written at entry from what Dhan actually filled. The close path sends a
  real SELL only when `live_filled = 1`. Before, it trusted `mode`, which is `BOTH` whenever
  the desk is armed, so a paper leg whose real BUY was rejected would have been sold at
  Dhan (opening a short option). Rows from before the columns existed (NULL) keep the old
  mode-based behaviour.
- **Prototype PAPER_ONLY route is real.** With the lane set to PAPER_ONLY, the order now
  runs as PAPER_ONLY even when the desk is armed. Before, only the label changed.
- **A BUY that part-fills before its cancel** is recorded and gets a stop for the filled
  quantity. Before, it was booked as CANCELLED and the real holding was untracked.
- **The companion stop must be out of the market before a SELL is sent** (filled, dead, or
  absent from the book with the cancel accepted). If that cannot be confirmed, no SELL is
  sent and the position is EXIT_FAILED with the stop still tracked.
- **Exits survive a crash or restart.** The exit order id and the exit reason are stored.
  The monitor restarts any EXIT_PENDING position that has no running close, and a restart
  resumes the stored order instead of sending a second one. Close tasks are tracked, one
  per position, and their exceptions are logged.
- **Sell chaser.** Modifies only when the price moved; swaps to a fresh order after
  `chasing.exit_max_modifications_per_order` modifications (default 20, read with a
  default, not written to config); handles EXPIRED and partial fills; closes the ledger
  only when the whole quantity is filled, at the quantity-weighted average; reads the
  desk's quote feed before calling Dhan; backs off when the order book cannot be read.

Config keys touched: none. Optional new key read with a default:
`chasing.exit_max_modifications_per_order`.

Baseline (same sandbox, same command, `pytest analyst --continue-on-collection-errors`):
- `f89f950`: 40 failed / 519 passed / 12 skipped / 37 errors.
- This branch, excluding the new file: same failing set. Two differences are sandbox-only:
  `test_nse_spot_candles` (2) fails in the checkout with or without this change, and
  `test_classifier_port_evidence` needs `node_modules`, present in one checkout only.
- AGY's prototype suites + HTML: 55/55. New `test_live_exit_fixes_8510.py`: 15/15; 14 of
  the 15 fail or hang on `f89f950`, the 15th is a control that must pass on both.
- Locks 11/11. Immutable-lock tests 10/10.
Compared against: AGY's reported 55/55 at `f89f950`, reproduced here before changing anything.

Not verified — needs the live desk or Dhan's documentation:
- Dhan's real field names and status values (`orderStatus`, `filledQty`,
  `averageTradedPrice`, `PART_TRADED`, `EXPIRED`) are taken from the existing code.
- Dhan's per-order modification allowance (20 is a guess kept under the 25 I believe it is).
- Whether `quantity` on a modify means total or remaining quantity for a part-filled order.
- Nothing here was run against Dhan. Not deployed, not restarted, not merged.

Open / needs Harish:
- Every lane labelled "paper" (FLAGSHIP, Stage 2/3, Crossover, Matrix Scalp, PA @ S/R)
  still sends a real BUY when the desk is armed and the allowlist permits it: the label
  does not change the route. Only the prototype was changed, as asked. Say if the others
  should follow their label.
- The kill switch and the manual close still start their close directly rather than
  through the tracked-task helper. The per-position lock keeps them safe; they are just
  not in the task registry.
- AGY moved `fix/8510-algo-v206` itself to `f89f950`.
- Same-tick "away then touch" in `check_retouch` is unchanged.

---

## 2026-11-11 · Claude · `claude/four-algo-corrections`
Tag: pre-claude-four-algo-corrections → post-claude-four-algo-corrections
Branched from: `claude/ema8-proto-live-exit-fixes` @ `6686cac` (contains AGY Phases 1–7 and the live-exit fixes)

Harish answered the questions in `claude/Four_Algo_And_Backtest_Review_2026-11-11.md` (project doc)
and asked for the corrections. These are **his rules of 11 Nov 2026**, not agent preferences.

What changed:
- **Stage 2 / Stage 3: first touch of the dynamic 8 EMA.** The closed-candle scanner now only
  reports whether the setup is armed. A new 30-second worker
  (`start_mj_dynamic_intrabar_worker`, started in `engine_8510_live.py`) reads the running candle
  against the 8 EMA recomputed with live spot and fires on the first touch within 3 points.
  One entry per 4/8 stack (keyed to the cross candle). No entry before 09:30.
- **8 EMA Reversal:** touches the dynamic 8 EMA (not the fixed value at the cross), same 3 points,
  same wait-2 / window-2. The ribbon rule no longer blocks it.
- **Exits for Stage 2, Stage 3 and Reversal:** spot ±N or option ±N, whichever first (40 for
  Stage, 20 for Reversal), plus 15:20. No Super Trail. Same block the Prototype uses
  (`uses_fixed_spot_or_option_exit`). The Dhan companion stop is now fill − N flat.
- **Flagship:** direction comes from the trade, not from "BUY" (a PE was treated as a long and
  closed at once). S/R exit: long near R1–R4, short near S1–S4 (was the reverse). No entry before
  09:30. New cockpit option `flagship_or_8ema_require_trend` (default ON): the OR 8 EMA entry
  must agree with the daily and 1H trend.
- **Backtest / replay is read-only.** The per-candle pass over old data now runs on throwaway
  copies of the party side, Stage/Reversal memory, crossover record and cockpit cards, and skips
  the Prototype entirely. Before, it could square off a real open Prototype position.
- **Backtest summary:** backtested rows are scored on points. Before, every one counted as a loss.
- Cockpit card text updated for all four.

Config keys touched: none written by this branch. New keys read with defaults:
`scanner.mj_sir_lecture.{dynamic_8ema_entry=true, dynamic_touch_tol_pts=3.0,
no_entry_before_hm="09:30", reversal_ribbon_block=false, dynamic_refresh_seconds=30}`,
`scanner.flagship_no_entry_before="09:30"`, `scanner.flagship_or_8ema_require_trend=true`
(the last one is saved by the existing `/api/scanner/gates` when the cockpit box is changed).

Tests changed because the owner's rule changed (reported, not loosened):
- `test_phase2_option_points_8510.py::test_non_prototype_buy_delta_scaled` used Stage 2 as the
  "still delta-scaled" control; it now uses the 1H 8-EMA homework lane.
- `test_super_trail_8510.py::test_flagship_trail_and_sr_exit_helpers` pinned the reversed S/R exit.

Baseline, same sandbox, `pytest analyst --continue-on-collection-errors`:
- `6686cac`: 44 failed / 530 passed / 12 skipped / 37 errors.
- This branch (new file excluded): same failing set. Differences are sandbox-only
  (`test_nse_spot_candles` ×2 and `test_classifier_port_evidence` depend on which checkout has
  local data / node_modules).
- New `test_four_algo_corrections_8510.py`: 22/22. Locks 11/11. Immutable-lock + HTML: 14/14.
- Note: AGY's `test_phase5_eod_8510` (3) and one `test_phase7` test fail when run after 15:20 IST
  because they read the wall clock. They pass before 15:20. Same on `6686cac`.
Compared against: my own run of `6686cac` minutes earlier.

NOT done — still open:
- **Backtest of Stage 2/3/Reversal still uses the old `_backtest_legacy_cand_signals` rules**
  and the old candle-trail exit. It does not describe the new live rules. Do not use its numbers.
- 1H 8 EMA is not yet dynamic (Flagship OR branch and the H1 homework lane still read the last
  closed 1H candle).
- Flagship: doji stop has no minimum; the OR branch books entry at the candle high/low; 1H trend
  reads the forming hour. The +40 spot target is still not checked live.
- Stage/Reversal memory is in RAM; a restart forgets "first touch taken".
- Overlapping backtest trades; band maths (sample vs population) differ between live and backtest.
- "Paper only" lanes still send a real BUY when the desk is armed. Not changed: Harish's answer
  was that paper must always run alongside live, which it already does.
- `_compute_side_summary` is defined twice in the backend; both copies were fixed.
- Nothing was run against Dhan or the desk database. Not deployed, not restarted, not merged.

Correction to my earlier review: I wrote that the scanner re-reads the same closed 15m candle on
every 3-second pass. It does not — `_pending_closed_15m_open_ts` evaluates each closed candle
once. The "re-arming on every pass" and "card flicker" points were wrong.

---

## 2026-11-11 · Claude · `claude/four-algo-corrections` (second commit)
Same branch and pre-tag as the block above.

What changed:
- **Stage 2 / Stage 3 / Reversal are now backtested with the live rules.** New
  `backtest_mj_dynamic_at_bar` (in `mj_sir_stage_reversal_8510.py`) walks each candle on a
  30-second grid and hands every tick to `evaluate_mj_sir_four_pack` — the same function the live
  worker calls. It does not restate any rule. It keeps its own memory; live memory is not passed in.
- Their backtest exit is the 1-minute walk after the entry tick (spot target / stop / 15:20),
  via `simulate_prototype_spot_exit(..., exit_on_opposite_close=False)`. Rows are in spot points;
  no modelled premium, rupees or charges. The option-points exit cannot be modelled from spot.
- `_backtest_legacy_cand_signals` is used only when `dynamic_8ema_entry` is switched off.
- **No look-ahead on the 30-second grid for these lanes:** a 1-minute candle is stamped with the
  minute it opens, so a tick reads it only when `ts + 60 <= tick`.
- **Epoch conversion made pandas-version-safe** (`epoch_seconds` in `nse_spot_candles_8510.py`,
  used in 3 places). `astype("int64") // 10**9` returns 1 for every candle on pandas 3, where
  datetimes are not held in nanoseconds; that silently emptied every 1-minute-walk backtest,
  the Prototype's included. `requirements.txt` allows `pandas>=2.0`. On pandas 2 the result is
  unchanged.

Tests: new `test_mj_dynamic_backtest_8510.py` 6/6, including an end-to-end date-range backtest
on a synthetic market database. Full suite: same failing set as `52cd66a` (sandbox-only
differences as before). Locks 11/11.

Still open:
- **The Prototype backtest still reads the 1-minute candle that is open at the tick**
  (`forming_ohlc_from_1m` uses `ts <= tick`), a look-ahead of up to 60 seconds. AGY's
  `test_3_30s_grid_reads_completed_1m_candles` pins that reading, so I left it and am reporting it.
- Backtest trades of one lane can still overlap in time; Flagship backtest unchanged.
- Everything listed as open in the block above, except the Stage/Reversal backtest.

---

## 2026-11-11 · Claude · `claude/ema8-proto-reentry`
Tag: pre-claude-ema8-proto-reentry → post-claude-ema8-proto-reentry
Branched from: `claude/four-algo-corrections` @ `48f7b87`

Harish's rules for the 8 EMA Prototype, given the evening of 11 Nov. **These replace his earlier
answer** ("do not take the same side again after a trade closes"):

- **Re-entry on the same side.** After a trade closes — spot ±40, option ±40, or 15:20 — the arm
  stays and the next away-and-touch takes the same side again. Only an opposite 15m close removes
  the arm (and arms the other side). One Prototype trade open at a time.
- **Away distance is its own setting: `away_min_pts` = 10.** ("Go ahead" on the suggested 10–15;
  10 chosen. Touch tolerance stays 3.)

Also built (bug-level, reported to Harish):
- The tick that first sees "away" is never also the touch; the return must be on a later tick.
- No new entry from `no_entry_from_hm` = "15:20": a trade opened then was closed by the 15:20
  rule on the next pass. The arm is kept.
- The live workers (Prototype and Stage/Reversal) now record the high and low of the 1-second
  spot between 30-second ticks. Before, most ticks saw one price and a quick touch was missed.
- Prototype worker errors are logged as warnings, not debug.
- Old one-trade-per-cross memory saved earlier in the day is ignored when re-entry is on.

Config keys: none written. New keys read with defaults in `scanner.ema8_prototype`:
`away_min_pts=10.0`, `reentry_same_side=true`, `no_entry_from_hm="15:20"`.
`reentry_same_side=false` and `away_min_pts=<touch tolerance>` give the previous behaviour.

Tests changed because the owner's rule changed (reported, not loosened): AGY's
`test_phase3_state`, `test_phase4_retouch` and `test_phase7_cockpit_log` pinned one-trade-per-
cross, alternation and away = 3. Their configs now set the two legacy switches explicitly, with a
note at the top of each file; every assertion is unchanged. New rules are in
`test_ema8_proto_reentry_8510.py` (11 tests, including Harish's 11 Nov PE example).

Baseline: Prototype + exit + Stage + backtest suites 104 passed / 4 failed. The 4 are AGY's
wall-clock tests (they fail after 15:20 IST, same on `48f7b87`). Locks 11/11.

Still open / needs Harish:
- Should the Prototype also wait until 09:30? Not answered; unchanged (can enter from 09:15).
- With a 10-point away rule, a candle that closes just across the line and comes straight back
  without first moving 10 points does not trade. His 12:15 example may be such a case.
- The backtest allows a re-entry only from the candle after the previous trade's exit; live can
  re-enter inside the same candle.
- Prototype backtest still reads the 1-minute candle open at the tick (look-ahead up to 60s).
- No cockpit control yet for `away_min_pts` or the re-entry switch (config only).
- Nothing run against Dhan or desk data. Not deployed, not restarted, not merged.

---

## 2026-11-11 · Claude · `claude/ema8-proto-reentry` (second commit)
Same branch and pre-tag as the block above. Harish's final answers for the 8 EMA Prototype:

- **Away distance = 2 points** ("GO AHEAD 2"). `away_min_pts` default 10 → 2. Touch stays ±3.
- **No entry before 09:30** (`no_entry_before_hm`, "we shall see later if we need 09:15").
- **Re-entry depends on how the trade closed:**
  - Profit (+40 spot or +40 option): the arm stays; same side again on the next touch.
  - Stop-loss (−40 spot, −40 option, or the broker stop filled): that side is **cancelled**. Its
    arm is removed and it cannot arm again until a 15m close has armed the opposite side
    (`sl_blocked_side`, saved in `ema8_prototype_state`). `reentry_after_stop_loss=false`.
  - Opposite 15m close: as before — close, cancel, arm the other side.
  - 15:20 exit: neither; the arm is left as it is.
- **Placeholder only:** `oi_direction_filter_enabled` (default false). When ON *and* something has
  written `proto_state["oi_bias"]` = "CE"/"PE", only that side trades. Nothing writes it yet;
  the CE/PE change-in-OI logic is not built.

One rule function, `note_prototype_exit`, is called by both the live close path and the backtest.
Tests: `test_ema8_proto_reentry_8510.py` now 28. Suites: 122 passed / 4 failed (AGY's wall-clock
tests, same as before). Locks 11/11. Config file not edited. Not deployed, not merged.

Note for whoever continues: with away = 2 and touch = 3, "away" and "touch" overlap by a point.
The touch still has to come on a later 30-second tick than the away.

---

## 2026-11-11 · Claude · `claude/v2.0.7-holdout-study` @ see log
Tag: pre-claude-v2.0.7-holdout-study → post-claude-v2.0.7-holdout-study
Branched from: `fix/8510-algo-v206` @ `aed34fa`. It does **not** contain AGY's local, unpushed
`agy/v2.0.6-10pt-retest` (`995fa68`: touch 10, 09:15 arming-only guard). That commit touches the
same area of `ema8_prototype_8510.py` (`no_entry_before`); expect a small merge there.

Harish asked for (a) the Prototype backtest's look-ahead fix and (b) a train/test study built
into the app's own backtest. No strategy rule and no config file changed.

What changed:
- **Look-ahead removed from the Prototype backtest.** A 1-minute candle is stamped with the
  minute it opens; on the 30-second grid the backtest read it while that minute was still
  running, up to 60 seconds ahead. `forming_ohlc_from_1m(..., completed_only=True)` and the
  tick slice now read a candle only when `ts + 60 <= tick`. The live default is unchanged
  (live, the newest row is real-time data). Entries move up to a minute later; results from
  before this commit are flattered and should not be compared with results after it.
- **Backtest speed.** `run_date_range_backtest` now builds only the lanes the `strategy_filter`
  asks for (the Flagship pass ran for every candle whatever the filter), and the Prototype's
  per-tick EMA uses `prev + alpha * (spot - prev)` instead of rebuilding the whole series
  (identical to 1e-9; SuperTrend ON still uses the full path).
- **`analyst/holdout_study_8510.py`** — `python3 -m analyst.holdout_study_8510
  [--train-days 170 --test-days 84 --cost-pts 2.0]`. Splits the sessions in the market database
  by date, runs a fixed list of variants through `run_date_range_backtest` on both sets with a
  per-trade cost, picks on the train days only, and reports that pick's test result as the
  answer (pass rule fixed in the file: test net > 0 and t >= 2.0). Writes a markdown report to
  `data/terminal8510/holdout/` (ignored by git). Overrides live on a copy of the scanner config
  for the length of one call; the config file and the live object are not touched.
  Lists what cannot be tested yet (OI filter, trend agreement, option exit) and why.

Baseline: Prototype/exit/Stage/backtest suites + 7 new = 129 passed / 4 failed (AGY's
wall-clock tests, unchanged). Locks 11/11.

Not verified: never run on the real 254-day database — I do not have it. Run time on the desk is
unknown. AGY's 254-day figures came from its own `fast_backtest_254_days.py`, which is not in
the repo; they are not comparable with this study until it has been run.

## Claude — `claude/v2.0.8-oi-context` (11 Nov 2026) — owner request: "integrate full fledged into app"
Base `claude/v2.0.7-holdout-study` @ `06d042c`. Tags (local): `pre-/post-claude-v2.0.8-oi-context`.
Not merged anywhere.

- **`analyst/oi_context_8510.py`** (new, pure functions). From the recorded `chain_snap`
  snapshots (8510 recorder DB first, then the market DB; read-only): OI added since 09:15 on
  each side within spot ±1000 → day bias (more puts added = CE, more calls = PE, within 20% of
  each other = NEUTRAL); 30-minute change of the same number (against the bias = fading);
  nearest of the 3 heaviest call strikes above / put strikes below, room in points, boxed when
  both are within 100 pts in total. Three gates: direction, agreement, room. No locked module
  is imported or changed.
- **Backend.** `oi_context()` cached 60 s; `payload["oi_context"]` in telemetry; the bias is
  written to the Prototype state as `oi_bias` (the placeholder still needs its own switch);
  every signal gets a row in the new ledger table `signal_oi_context` (context + each gate's
  verdict + would_block/blocked), keyed by the same `signal_uid` as `signal_observations`.
- **Blocking is OFF by default.** A trade is stopped only when `oi_filter_block` is true (cockpit
  checkbox on the Prototype card, saved through the existing gate endpoint), only for lanes in
  `oi_context.apply_to` (default `EMA8_PROTOTYPE`), and never with NO_DATA (no snapshots, one
  snapshot, or newest older than 10 minutes). A blocked Prototype touch is logged with its
  reason; the arm stays and the next away-and-touch is judged again.
- **Config file not edited.** All values are code defaults under `scanner.oi_context`.
- `python3 -m analyst.oi_context_8510` prints the days that have recorded snapshots.

Tests: `test_oi_context_8510.py` 15 passed; related Prototype/exit/Stage/backtest/HTML suites 91
passed; locks 11/11. Whole `analyst` suite: the same 45 failures / 37 errors with and without
this branch (sandbox: missing StockMojo module, data files, wall-clock tests).

Not verified: never run against real snapshots — both databases are empty here. Thresholds
(20% dead zone, 10% slope, 40 pts room, 100 pts box, top-3 walls) are first guesses, not fitted
and not tested. Not in the backtest yet: needs enough recorded snapshot days.

## Claude — `claude/v2.0.9-desk8600-board` (11 Nov 2026) — owner request: 8600 reads every 3 min, cockpit buttons to filter trades
Base `claude/v2.0.8-oi-context` @ `310e3e7`. Tags (local): `pre-/post-claude-v2.0.9-desk8600-board`.
Not merged anywhere. Replaces v2.0.8's single "block on OI gates" checkbox (`oi_filter_block` removed).

- **`analyst/desk8600_board_8510.py`** (new). Eleven sources, each CE / PE / NEUTRAL / no data:
  seven from the 8600 app through the existing read-only `SignalBridge` (closed 3-minute bars,
  polled every 20 s): 3-min tape verdict, ticket, high-conviction ticket, Classic Writer Flow,
  Fresh Change in OI, Cumulative Total OI, PCR OI vs Futures; four from the recorded chain
  snapshots: day OI since 09:15, 15-min impulse, 3-min bar, walls/room.
  `SignalBridge` and its `use_for_algo_trades=False` flag are untouched; the board reads `bridge.last`.
- **Owner switches (cockpit panel on the Prototype card, `GET/POST /api/desk8600/board`):** one
  ON/OFF per source; rule "None against" or "Majority agrees"; "Neutral = skip"; lane buttons
  (Prototype, Stage 2, Stage 3, Reversal, Flagship, 1H 8 EMA; default Prototype). Saved to
  `scanner.desk8600_board` by the app the same way the other cockpit gates are saved.
  **Everything is OFF until he presses a button.** A source with no data never skips a trade.
- **Record:** every signal → ledger table `signal_desk8600` (all eleven readings as JSON, counts,
  which sources were against whether switched on or not, blocked + reasons).
- `oi_context_8510`: adds `flow_3m`, `impulse_15m` and 15-min option volume; cache 60 s → 20 s.
- `engine_8510_live.py`: one line handing the bridge to the backend, two endpoints.

Tests: `test_desk8600_board_8510.py` 13 + `test_oi_context_8510.py` 15 passed; related suites 119
passed; locks 11/11; whole `analyst` suite 45 failed / 37 errors, the same counts as the base
(620 passed, +13). Panel rendered in Chromium with sample readings.

Not verified: no live 8600 app or real snapshots here — never seen with real data. Not built:
8600 tickets do not open trades by themselves (no new lane); Option Volume, Smart OI / Matrix 2.0
bars and Writer Stack-up have no direction rule in the bridge, so they have no button.

## Claude — `claude/v2.0.10-ticket-lane-chain-reads` (12 Nov 2026) — owner request: ticket lane (paper and live), more option-chain reads, cockpit rework
Base `claude/v2.0.9-desk8600-board` @ `02f00c7`. Tags (local): `pre-/post-claude-v2.0.10-ticket-lane-chain-reads`.
Not merged anywhere. No existing strategy's entry or exit rule is changed.

- **8600 Ticket lane** (`analyst/desk8600_ticket_lane_8510.py`, new lane, ids `DESK8600_TICKET_LONG/SHORT`).
  A 🎯 Dip Buy / Sniper Breakout badge on the newest closed 3-minute 8600 bar buys CE; 🎯 Pullback
  Sell / Sniper Breakdown buys PE. One signal per ticket bar, no second ticket trade while one is
  open, ticket bar must be under 200 s old. The signal goes down the normal path (board filters,
  contract pick, chaser, paper ledger, live route). Exit = the fixed spot-or-option exit
  (`DESK8600_TICKET` added to `FIXED_SPOT_OR_OPTION_EXIT_PREFIXES`) + 15:20.
  Owner switches (cockpit card, `GET/POST /api/desk8600/ticket-lane`, saved to
  `scanner.desk8600_ticket_lane`): ON/OFF (**off until pressed**), route "Real when desk is armed"
  (default, as he asked: paper and live) or "Paper only" (downgrade in `execute_order_with_chaser`),
  high-conviction only, target/stop points (40/40). Switching it on adds its ids to the execution
  allowlist if that list is enabled. `SignalBridge` and its `use_for_algo_trades=False` are untouched;
  the lane reads `bridge.last`.
- **Four more board sources** (`analyst/chain_reads_8510.py`, from the recorded snapshots; the loader
  now also selects `ce_ltp, pe_ltp`): PCR zone, price-vs-OI buildup (15 min, ATM ±3 strikes),
  volume-backed OI (churn = neutral, self-calibrated against the day's earlier windows), gamma
  regime (Black-Scholes, IV solved from LTP; positive = "no room" both sides). Facts line: PCR,
  max pain, gamma flip. The board now has 15 sources and a seventh lane button (8600 Ticket lane).
- **Cockpit** (`Algorithmic_Trading_Workspace_8510.html`, `analyst/cockpit_views_8510.py`):
  Prototype card header replaced by a sentence + five-step tracker + spot / 8 EMA / gap and a route
  label in words (old elements kept hidden; `sc.ema8_prototype_view`, display only).
  Active Positions: summary cards (open MTM, booked today, day total, real-money MTM), per row
  real/paper badge, minutes held, spot move, tick flash, points and %, stop→target bar.
  Orders Book: summary cards, Today / All days, "Completed trades" view (one row per exit).
  Replay and Backtester: result in spot points — net, win rate, max drawdown, losing streak, profit
  factor, running-total curve, tables by exit reason / entry hour / side / day. All computed in the
  page from rows already returned; no backend change to replay or backtest.
  The vendor name of the external PE-CE feed no longer appears in visible cockpit text (element
  ids and endpoints unchanged).

Tests: `test_ticket_lane_chain_reads_8510.py` 21 passed; board + context 28 passed; locks 11/11;
whole `analyst` suite 645 passed / 41 failed / 37 errors — the same failing files as the base minus
AGY's four wall-clock tests (run before 15:20 IST this time). Cockpit rendered in Chromium with
sample data, no page errors.

Not verified: no live 8600 app, no real snapshots, no broker here. The ticket lane has never
taken a ticket from the real 8600 app. Thresholds in the chain reads are first guesses. The gamma
read assumes hedgers are long calls and short puts. Not built: vega / skew / premium decay /
straddle / VPIN / participant data from the 8501 app; ticket lane in replay or backtest; the
8600 ticket's own SL/target text (the bridge does not carry it).

## Claude — `claude/v2.0.11-link-status` (12 Nov 2026) — owner: "ticket lane shows waiting for status; how do I know 8600 is connected and live; where are the changes"
Base `claude/v2.0.10-ticket-lane-chain-reads` @ `4adfaac`. Tags (local): `pre-/post-claude-v2.0.11-link-status`. Not merged.
Display only; no trade logic changed.

- `analyst/desk8600_link_8510.py` + `GET /api/desk8600/link` + `payload["desk8600_link"]`: one plain
  answer for the 8600 app (LIVE / OLD data / NOT answering / no bars for today yet / starting / off)
  with the reason and what to do, the same for the stored option chain, and the engine build
  (label + git commit).
- Cockpit: a strip at the top of the Algo Terminal tab shows the three, plus a fold-out
  "What is new in this build, and where to find it". The strip, the Ticket Lane card and the Board
  are now also asked for directly every 15 s, so they fill without the telemetry stream; a 404
  from the engine is shown as "8510 engine is running OLD code — restart it".
Tests: 23 in `test_ticket_lane_chain_reads_8510.py`; board + HTML suites pass; locks 11/11.
Not verified against a running 8600 app.

## Claude — `claude/v2.0.13-proto-line-in-banner` (12 Nov 2026) — owner: show the Prototype's own 8 EMA in the live banner, prove it ticks
Base `composer/v2.0.12-tv-ema-live-panel` @ `4040077` (Composer's 3-s live banner; includes all Claude branches up to v2.0.11).
Tags (local): `pre-/post-claude-v2.0.13-proto-line-in-banner`. Not merged. Display only.

- The Prototype's 30-s worker now records what it used (`_ema8_proto_last_check`: line, spot, tick
  range, touch band, verdict, time). `ema8_prototype_check_status()` serves it; the live-cross
  endpoint adds it as `prototype_check` with `server_time_ist` and `market_open`.
- Banner, new third row: "8 EMA Prototype line" — live value (same math as the algo), touch band
  (±tol), spot distance with "ON THE LINE" inside the band, the algo's last 30-s check (time, line,
  spot, range, verdict, next in N s), how far the live line moved since; and a tick proof: counter,
  server time, last change; green dot when moving, amber "NOT MOVING" after 20 s unchanged with the
  market open, grey "market closed — frozen at the last bar".
Tests: +1 in `test_ticket_lane_chain_reads_8510.py` (24 pass); Prototype re-entry + HTML suites pass;
locks 11/11. `test_nse_spot_candles_8510.py`: two tests fail on Composer's base too (resample
returns empty frame under pandas 3 — not from this branch). Not verified with a live spot feed.

## Claude — `claude/v2.0.14-banner-redesign` (12 Nov 2026) — owner: "banner is a long column, make it horizontal; how do I see 8600/8510 live or dead"
Base `claude/v2.0.13-proto-line-in-banner` @ `30a6bd8`. Tags (local): `pre-/post-claude-v2.0.14-banner-redesign`. Not merged. HTML only.
- Sticky banner rebuilt as five tiles in one row (spot · 15m · 1H · 8 EMA Prototype line · Feeds), two rows under 1400 px.
  Same element ids, so Composer's 3-s poll and renderers are untouched; the spec text moved to a tooltip.
- Feeds tile: 8510 engine (LIVE + build commit / DEAD / OLD CODE), 8600 app (LIVE · bar time / OLD DATA / DEAD / no bars yet), Option chain
  (LIVE / OLD / no snapshots today), from the existing 15-s link poll.
Tests: `test_workspace_8510_html` + my suites pass (28); locks 11/11. 8 `test_super_trail_8510` failures are pre-existing on the base.

## Claude — `claude/v2.0.15-horizontal-cockpit` (12 Nov 2026) — owner: "make the strategy column a horizontal stack"
Base `claude/v2.0.14-banner-redesign` @ `3a71841`. Tags (local): `pre-/post-claude-v2.0.15-horizontal-cockpit`. Not merged. HTML only.
- Strategy grid (`#strategyCockpitGrid`): auto-fill 250 px cards, dense packing, top-aligned — Stage 2, Stage 3, Reversal,
  Ticket Lane, Flagship in one row; Ribbon, PA@S/R, Daily S/R, Daily Trend in another.
- 8 EMA Prototype card spans the full row: status + Master Control + route + filters + rules + log on the left,
  the 8600 Board on the right with its 15 sources in a 3-column grid (was one 15-row list inside the controls box).
  No ids changed; every renderer and switch works as before.
Tests: HTML + my suites 28 pass; locks 11/11. Rendered in Chromium at 1600 px.

## Claude — `claude/v2.0.16-cooldown-after-fill` (12 Nov 2026) — owner: "only one Prototype trade today, why"
Base `claude/v2.0.15-horizontal-cockpit` @ `fffed8b`. Tags (local): `pre-/post-claude-v2.0.16-cooldown-after-fill`. Not merged.
- Defect found in the 12 Nov log: the Prototype's first touch 11:18:48 was skipped after the 5-minute
  signal wait had already been stamped, so the next nine 30-second touches were discarded and the entry
  came at 11:24:20; the 12:16:29 touch after the profit exit was lost the same way. Fix: the wait
  (`signal_cooldown_5m_min`) is stamped only after `execute_order_with_chaser` returns ok. No rule change:
  the wait between placed orders is unchanged; a skipped touch simply lets the next touch try again.
  Applies to every lane that goes through `_process_strategy_signals`.
Tests: +1 guard; Prototype re-entry, live-exit, phase-2 suites pass (70); locks 11/11.

## Claude — `claude/v2.0.17-reentry-buttons` (12 Nov 2026) — owner: "don't switch on, just show both options as cockpit buttons"
Base `claude/v2.0.16-cooldown-after-fill` @ `3e102c3`. Tags (local): `pre-/post-claude-v2.0.17-reentry-buttons`. Not merged.
- Prototype card, new "Re-entry after an exit" block: "After profit: same side again ON/OFF" (default ON) and
  "After stop-loss: side cancelled, flip only / same side again ON" (default cancelled). Saved through the existing
  `/api/scanner/ema8_prototype/filters` endpoint as `reentry_same_side` / `reentry_after_stop_loss` — the switches that
  already existed in `ema8_prototype_8510.py`. **Defaults unchanged**; nothing switched on.
Tests: +1; Prototype re-entry + HTML suites pass (58); locks 11/11.

## Claude — `claude/v2.0.18-proto-contract-ge200` (12 Nov 2026) — owner's ledger: 18 Prototype touches skipped NO_ENABLED_CONTRACT
Base `claude/v2.0.17-reentry-buttons` @ `d5bf62b`. Tags (local): `pre-/post-claude-v2.0.18-proto-contract-ge200`. Not merged.
- Cause: the Prototype could only buy one of the four matrix contracts (ATM, ATM±100). On 12 Nov afternoon all
  four were under ₹200 (or their quote was stale), so every touch from 13:32 to 14:57 was rejected, and the
  11:18:48 touch too. The owner's rule is "CE/PE ≥ ₹200, current week".
- Fix: `_select_ema8_prototype_contract_any_strike` — the four first; if none qualifies, resolve strikes deeper in
  the money in 50-pt steps up to 500 pts (CE below spot, PE above), fetch fresh LTPs in one call, take the nearest
  strike with LTP ≥ the minimum. Directional-bias block still applies. The skip reason now says why
  ("NO_ENABLED_CONTRACT: no PE with premium ≥ ₹200 up to 500 pts ITM (best 25450 @ 188.00)").
- Prototype only; Stage/Flagship contract choice unchanged. Not a new limit.
Tests: +1 (72 pass across Prototype/exit/phase-2 suites); locks 11/11. Not run against the broker.

## Claude — `claude/v2.0.19-touch-every-3s` (12 Nov 2026) — owner: "catch the touch as soon as it happens, why 30 s"
Base `claude/v2.0.18-proto-contract-ge200` @ `3c976ae`. Tags (local): `pre-/post-claude-v2.0.19-touch-every-3s`. Not merged.
- Live touch check for the 8 EMA Prototype (`e8_refresh_seconds`) and Stage 2/3/Reversal (`dynamic_refresh_seconds`)
  defaults 30 → 3 s; workers clamp at 2–60 s and re-read the cadence every loop (no restart for a change).
  The tick high/low between checks is still used, so nothing between ticks is lost; what changes is the reaction time.
- Backtest/replay keep a 30-second grid under a new key `backtest_tick_seconds` (1-minute data cannot be read finer),
  so backtest numbers stay comparable with before.
- "RETOUCH SEEN" log line at most once per 30 s per side (the check now fires ten times more often).
- `scripts/patch_touch_cadence_8510.py` — narrow patch: sets only the two cadence keys in the config file
  (the repo config and the owner's local config both carry `e8_refresh_seconds: 30`, which overrides the default).
Tests: +1 (56 across Prototype/Stage/backtest suites, 93+ earlier suites); locks 11/11.

## Claude — `claude/v2.0.20-lightweight` (12 Nov 2026) — owner's removal list from the lightweight review
Base `claude/v2.0.19-touch-every-3s` @ `620145f`. Tags (local): `pre-/post-claude-v2.0.20-lightweight`. Not merged.
Owner's list, verbatim: "B3, B8 · Writer-flow gate, Trend Capture v1.9.2 service, External PE-CE feed (the vendor JSON
poll), trend_filters block, Futures Volume Surge (VSR), Goldilocks ATR separation". Nothing else was removed.

- **B3 Dynamic ATM Strike widget, B8 Multi-Strike Live Status card** — page only (commit `8fedc98`).
- **Goldilocks ATR separation** — `compute_goldilocks_separation`, `get_latest_goldilocks_status`, the scanner gate,
  telemetry fields, cockpit checkbox + pills (Tab 1 and Tab 5). `goldilocks_min/max_atr` stay: the EMA proximity engine
  and the five-factor score use them as band widths.
- **Futures Volume Surge** — engine no longer constructed (`futures_volume_engine = None`), signal tagging, telemetry,
  cockpit checkbox + pills. `analyst/futures_volume_engine.py` (module + its own tests) left on disk: research scripts import it.
  Consequence: the five-factor score no longer receives a volume reading, so it tops out at 4 of 5. Its gate is off.
- **Writer-flow gate** — config, `_writer_flow_context`, `filter_signals_by_writer_gate`, 3 endpoints, cockpit controls;
  `analyst/writer_flow_gate_8510.py` deleted. `apply_writer_flow_gates` → `apply_signal_gates` (only the optional
  five-factor gate is left in it).
- **External PE-CE vendor poll** — poller init, background task, `get_stockmojo_live*`, 3 endpoints, cockpit banner and
  master switch; `stockmojo_safe_poller_8510.py`, `stockmojo_status_8510.py` deleted. NOT touched: the research workspace
  router (`STOCKMOJO_RESEARCH_V199` block in the engine) and the old unwired `stockmojo_scraper.py`.
- **Trend Capture v1.9.2 service** — backend accessor, telemetry field, 4 endpoints, orphaned page functions;
  `trend_capture_service_8510.py` deleted. Research/evaluation/study modules stay (scripts import them).
- **`trend_filters` config block** — no code reads it. Config is not edited by hand:
  `scripts/patch_remove_dead_config_8510.py` deletes only `trend_filters`, `writer_flow_gate`, `stockmojo_live` and the three
  dead `proven_edges` switches, after writing a copy of the file.

Tests of removed features were removed with them (owner removed the features): 4 in `test_v17_proven_edges`, 4 in
`test_v192_trend_capture`, files `test_writer_flow_gate_8510.py` and `test_v195_stockmojo_poller_8510.py`; two assertions in
`test_v19_slot_volume_confluence` updated (no volume field; score 4). New guard test keeps the removed pieces removed.
Whole `analyst` suite: 642 passed / 43 failed / 37 errors; **no test fails that did not fail on the base** (47 before).
Locks 11/11. Page loaded in Chromium against real telemetry from the trimmed backend, all six tabs clicked: 0 script errors.
Size: page 7,705 → 7,273 lines; backend 10,780 → 10,402; engine 1,414 → 1,328; four modules deleted.

## Claude — `claude/v2.0.21-equity-guys-view-and-retry` (13 Nov 2026) — owner: "Equity Guys" view; look for the Prototype's bugs in the other lanes
Base `claude/v2.0.20-lightweight` @ `7c5b730`. Tags (local): `pre-/post-claude-v2.0.21-equity-guys-view-and-retry`. Not merged.

**View (page only).** Positions & Orders tab, filter card, new first row: VIEW [Everything] [Equity Guys] [Others].
Equity Guys = Stage 2, Stage 3, 8 EMA Reversal, 1 HR Flagship, 8 EMA Prototype; Others = 8600 Ticket lane, Ribbon Scalp,
PA @ S/R (incl. its "Homework" names), manual, anything else. One classifier (`strategyLaneOf`) from strategy id / setup
text drives Active Positions, the Orders Book, Completed trades, both summary strips and "Booked today". Choice is
remembered in the browser. Strategy buttons updated to the lanes that exist (Reversal, Flagship, Prototype, 8600 Ticket;
the dead "P minus C Filter" button removed). Selected filter buttons are now visibly highlighted.

**Same bug class in the other lanes — found and fixed.**
- Stage 2 / Stage 3 / 8 EMA Reversal marked "first touch taken" the moment the signal was emitted. If the order step
  then skipped (no contract, stale quote, duplicate, filter, rejection) the setup was used up for that whole 4/8 stack.
  Worse than the Prototype's 5-minute loss. Now `_process_strategy_signals` wraps each signal in try/finally and calls
  `_release_unfilled_setup` whenever no order was placed: Stage/Reversal fired keys are cleared, the Prototype's
  "moved away" is restored (it is still on the line), the Ticket lane's bar mark is cleared.
- 1 HR Flagship (and its 8 EMA entry) is evaluated once per 15-minute close; a skipped order lost the candle's signal.
  It is now offered again for `closed_candle_retry_seconds` (default 60) from first seen.
- Retries are spaced `skipped_signal_retry_seconds` (default 5) per strategy so the ledger is not flooded.
- Matrix lanes' NO_ENABLED_CONTRACT now says why ("both CE ATM and CE ITM are unticked in the Strike Matrix" /
  "matrix contract not built yet").
- Already common to all lanes from earlier branches: 5-minute wait starts only on a placed order; 3-second touch check.
- Card wording "30s" → "checked every 3s".
Not changed: Stage/Flagship contract choice stays ATM/ITM from the Strike Matrix (no premium rule exists for them).

Tests: 34 in `test_ticket_lane_chain_reads_8510.py` (classifier run in Node; release behaviour run through the real
signal processor); whole `analyst` suite 650 passed / 39 failed / 37 errors, no new failure vs the base; locks 11/11.
Not run against the broker or a live session.

## Claude — `claude/v2.0.22-backtest-revamp` (13 Nov 2026) — owner: remove Tab 5; revamp the backtester and replay so backtesting is robust and gives confidence
Base `claude/v2.0.21-equity-guys-view-and-retry` @ `7bbb16d`. Tags (local): `pre-/post-claude-v2.0.22-backtest-revamp`. Not merged.

**Removed (safely): Tab 5 "Multi-Factor Trend Lab & 128 Combos".** Nav button, the whole `#tabTrendLab` panel,
the JS (runTrendLabAPI, runCombinatorialMonteCarlo, autoSelectOptimalCombo, applyFilterPreset, runDateExplorer,
evaluateTradeAgainstTrendFilters, exportFilteredLedgerToExcel, exportMonteCarloToExcel), the backend
`run_trend_lab()` + its `trend_lab_variables` telemetry, and the `/api/trend_lab/run` endpoint. Tab 6 renumbered to 5.

**New: Backtest Lab** (`analyst/backtest_lab_8510.py`, +6 endpoints already present `/api/backtest_lab/{meta,start,status,result,day,cancel}`),
replacing the old date-range backtester card on the Historical Date Replay tab.
- A run is a BACKGROUND JOB in its own process (`subprocess`, niced): private config copy, throw-away ledger,
  market db read-only, cannot place an order or touch the live desk. Progress/result/cancel via files. One run at a time.
- Walks the range month-by-month through the app's own `run_date_range_backtest` (no second strategy implementation).
- Everything in NIFTY spot points, per trade, after a per-trade cost. No invented rupees.
- Reports: net/avg/win/profit-factor, t-stat + bootstrap 95% CI, max drawdown, longest losing run, by lane/month/weekday/
  hour/exit/side, equity curve, per-day heatmap, per-day 15m chart + trades. Ambiguous (target&stop in one minute) booked
  as stops with the "if targets" swing shown. Coverage of missing weekdays.
- **Hold-out verdict** (the confidence piece): split by date, last 30% held out, verdict PASS/WEAK/FAIL/NOT ENOUGH DATA
  read from held-out days only (PASS = held-out net>0 & t≥2 & earlier>0). Rule fixed before any run.
- CLI: `python3 -m analyst.backtest_lab_8510 --from .. --to ..`.
Replay card left intact (its points-analysis block from v2.0.10 stays); `run_date_range_backtest` kept (the lab uses it).

Tests: `test_backtest_lab_8510.py` 11 pass (stats, hold-out split, verdict, job lifecycle with a fake engine, cancel,
no ledger-path leak in result). Updated 3 tests for the removed features (required-fns list, my cockpit test, the
embedded-history test). Full `analyst` suite 661 passed / 39 failed / 37 errors — **no failure new to this branch**
(39 < 42 at base; the 4 EOD/phase7 wall-clock tests pass before 15:20 IST). Locks 11/11. Page: 7,300 → 6,630 lines;
loaded in Chromium against real backend telemetry and stubbed lab endpoints, all 5 tabs, verdict/summary/lanes/equity/
breakdowns/day-detail/by-day/past-runs all render, 0 script errors. End-to-end: real engine on a synthetic market db
produced 420 rows across lanes → lab normalized, split, graded.
Verified by running the real engine; not run against the live broker. Build label → v2.0.22.

## Claude — `claude/v2.0.23-proto-glitches` (13 Nov 2026) — owner: 3 live glitches + "look for Prototype bugs"
Base `claude/v2.0.22-backtest-revamp` @ `f6ae7c5`. Tags (local): `pre-/post-claude-v2.0.23-proto-glitches`. Not merged.
Three confirmed bugs, each found by following the code:

1. **15:20 square-off never ran (ALL lanes, real + paper).** `check_eod_square_off()` existed but was called by
   nothing — grep showed zero callers. Open positions were never auto-closed; the owner had 2 left open after close.
   Fix: the live scanner loop now calls it every tick, **before** the `scanner_enabled` gate (a paused desk must still
   close positions), guarded to 15:20–15:30:30 and logged once/day. Idempotent — `modify_open_position` only acts on
   OPEN, so repeated ticks and EXIT_PENDING/EXIT_FAILED rows are safe.
2. **Orders Book "Today" / "Booked today" showed nothing.** `get_order_book()` reformats `created_at` to a display
   string ("13 Nov 2026, 17:21 IST") that the cockpit's `orderDayKey` could not parse, so the day filter excluded
   every row. Orders DID record (proved by a round-trip test). Fix: backend adds a machine-readable `created_day`
   (ISO) before reformatting; frontend `orderDay(o)` prefers it, and `orderDayKey` also parses the "13 Nov 2026" form.
3. **"After stop-loss: same side again ON" radio snapped back to OFF.** The per-second telemetry calls
   `applyEma8ProtoFilterUI(sc.strategies.ema8_prototype)` with a status object that lacked the re-entry keys, so
   `!!undefined` forced the radio OFF every second. Fix: the status row now carries the full filter set
   (reentry_same_side/after_stop_loss + ribbon/sep/supertrend/away/slow), and `applyEma8ProtoFilterUI` only sets a
   radio when that key is present in the object (`setRadio` helper) — partial telemetry can never clobber a toggle.

Design answer recorded for the owner: the Prototype has no cap on trades/day — one at a time, re-enter same side on
the next touch after a +40, flip to the opposite side after a −40 stop or an opposite 15m close, 09:30–15:20, square
off 15:20. The 13 Nov chart's 5 trades (+40/−40/+40/+40/+35 ≈ +115) are all reachable under the current defaults
(the −40 stop naturally flips CE→PE); the stop-loss-same-side toggle is not needed for that sequence.

Tests: +4 in `test_ticket_lane_chain_reads_8510.py` (41 total): EOD call wired before the gate; status carries the
re-entry flags; applyFilterUI only sets present keys; order book sends created_day and the UI parses it. Radio
persistence also verified in a real browser (stays ON through a partial telemetry poll, still responds to an explicit
change). Whole suite 661 passed / 43 failed / 37 errors — the 4 "new" vs the pre-15:20 baseline are the known
wall-clock EOD/phase7 tests (test_4 fails identically on the committed base). Locks 11/11. Build label → v2.0.23.


## ChatGPT — v2.0.38 OI Decision Boards — 2026-11-18

- Requested branch: `gemini/v2.0.38-oi-decision-board` (authored by ChatGPT), isolated clone off `gemini/v2.0.36-clean-and-scope` at `c2c39d8`. Pre/post tags: `pre-gemini-v2.0.38-oi-decision-board` / `post-gemini-v2.0.38-oi-decision-board`. Do not merge automatically.
- Display only: independent Prototype/Reversal cockpit hosts; reported armed side/waiting reason, effective mode/rule, enabled filter readings and two-sided votes, blockers, source age and momentum/price context. Context is not double-counted. Existing telemetry cadence; no new timer or trade action. Partial payloads preserve boards and controls. Optional malformed S cannot break rendering.
- Telemetry is copied before enrichment. Calls existing `oi_switch_block` for both sides with each lane's CURRENT execution call shape. No changes to that function, entry methods, protected modules, runtime settings, credentials, live mode, cap settings or config files.
- IMPORTANT existing defect preserved: Reversal execution passes a flat OI config to a function expecting nested `oi_filters`, so configured switches are currently not consumed. Board explicitly warns CONFIG/GATE MISMATCH, shows not-applied votes and actual allowed verdicts. No gate repair authorized by this display-only request. Reversal omitted switches also inherit Prototype defaults through the existing adapter; explicit switch sets remain independent. Neutral-blocks=true remains honored if explicitly configured; stale data is ignored by the existing gate.
- Focused final: **55 passed / 0 failed** (38 new board cases plus existing Prototype/Reversal tests; parameterized parity exhausts 64 source combinations per lane/mode/rule/neutral case). Original focused before new board: existing tests unchanged.
- Full analyst regression, reproducible local baseline rather than quoted ~889 pass /35 fail /42 error: clean `c2c39d8` **746 passed /57 failed /7 errors /10 skipped**; feature **783 passed /57 failed /7 errors /9 skipped**. Exact failing/error test IDs identical, **zero new failures/errors**. Full feature run preceded final optional-S/age formatting hardening and two added focused tests; focused final rerun passed. One inherited skip became pass. Existing suite issues include unavailable fixtures and event-loop assumptions; suite is not clean.
- Both full runs: host OC `.venv/bin/python`, isolated tracked fixtures, `PYTHONPATH=.:../../v197_collector`, test-runner-only `v197_environment_plugin` (10ms asyncio wakeup and injected Matrix clock 2026-11-06 11:30), `--continue-on-collection-errors --ignore=analyst/test_time_series_bridge.py`. No production changes to accommodate tests. Reports: conversation outputs/v238_oi_decision_board/{baseline,feature}.{txt,xml}.
- Locks **11/11 PASS**; immutable feature tests **10/10 PASS**; parity **9,059 rows ×17 compared fields, 100%, zero mismatches**, using read-only host TypeScript dependency via temporary symlink removed afterward; diff whitespace check PASS.
- Browser: actual shipped renderer and scoped CSS in isolated synthetic two-card preview; CE/PE input change updates both boards, guard/control preservation true, captured JS error counter zero. This verifies component rendering, not production backend/browser end-to-end. Running 8510 app was not deployed/restarted or altered. Host-integrated cockpit validation remains required after review/merge.
- Commit staging explicitly names only HTML, backend, new helper, new tests, and this status file. `git status --short` before commit shows no data/, replay_data/, *.sqlite3 or config changes. Production OC dirty runtime config remains untouched.


## ChatGPT — v2.0.39 presentation-only desk layout — 18 Nov 2026

- Isolated branch `chatgpt/v239-desk-layout`, based on `0cada8e`, preserving the concurrent cockpit restoration commit. Pre/post tags checkpoint this layout. Only dashboard HTML and this audit note are committed. Concurrent cockpit_views/backend/config edits are left untouched and unstaged.
- Responsive two-column lane workspace: controls left, OI decision matrix plus existing audit log right; single column on narrow screens. Auto-fit overview cards, grouped filter controls, readable table wrapping, consistent spacing/contrast, reduced stacked sticky-header obstruction. Existing 8600 help text restored.
- Preserved all 339 existing IDs, all 85 inputs, 11 selectors, 60 buttons, both script blocks byte-for-byte as parsed; no handlers, trading calculations, defaults, routes, strategy enablement or execution settings changed. Audit table moved within the same Prototype card; all its IDs retained. Structural preservation report outside repository in outputs/v239_ui_revamp/preservation.json.
- Focused tests: **55 passed, 1 skipped, 0 failed**, versus installed v238 **56 passed, 0 failed**; current baseline cockpit restoration has its conditional skip. Locked modules **11/11 PASS**. No test assertions altered. Earlier v238 immutable **10/10** and parity **9,059 x17 zero mismatches** remain the last broader checks; full analyst suite was not repeated for this HTML/CSS-only change.
- Browser preview uses real GET telemetry through a localhost read-only proxy (POST rejected). Desktop two-column layout verified; narrow viewport stacks and measured page width equals viewport 375px, no page overflow. Existing backend status/credential-check warnings in preview are not layout failures and are not altered. Viewport override reset. Production HTML installation is fingerprint-checked against the reviewed baseline, with a source backup retained in the conversation outputs; no backend restart needed.
- Previous board installation is now completed in host OC at `7099a14`, followed by the other editor's `0cada8e`. The scoped 8510 restart was verified ONLINE/PAPER_ONLY, and both Prototype/Reversal telemetry boards rendered in the real browser with no console errors at that check. Source-only staging claim is superseded by this host installation record.


### v2.0.39 delivery status clarification
Final source keeps both original script blocks byte-for-byte, after restoring whitespace in 4d27874. Final focused rerun: 55 passed /1 skipped /0 failed; locks 11/11. Host installation was NOT completed in this tool turn: file-specific permission roots caused synthetic mount failures, and even the fallback runtime sees OC/.git read-only. No active HTML, backend or config was overwritten by this layout work. Fingerprint-checked installer is outputs/v239_ui_revamp/install_layout.py in the conversation workspace; it creates a host checkpoint/new branch, cherry-picks only the reviewed commits and verifies locks. The earlier v238 board installation remains installed independently. Desktop preview screenshot desktop.png; mobile width 375 matches page width 375. This is a layout delivery, not a trading-performance claim.


## ChatGPT — v2.0.41 EMA audit completion — 19 Nov 2026

- Active-source checkpoint of Cursor's previously uncommitted repairs: `0e62d13`, owned branch `chatgpt/v241-finish-ema-audit`; tags `pre-chatgpt-v241-cursor-snapshot` and `pre-chatgpt-v241-ema-finish`. Explicit source paths only; runtime config, data, databases and credentials excluded. Cursor's fixes/settings retained, not overwritten by older v240 files. Isolated verification branch `chatgpt/v241-verified-ema-finish` off that checkpoint.
- Remaining repairs: Reversal OFF guard precedes legacy return; most recent opposite crossover supersedes the old setup and pending status identifies direction; last entry attempt stage/reason/time exposed beside OI votes; pre-order exceptions release setup/reservation and permit the next candidate, while post-submission exceptions preserve batch abort because broker outcome may be unknown; pre-entry closed candles excluded from Reversal trailing; Prototype status cadence text reads actual config. No new caps, validation gates, strategy thresholds, live arming, routes, premium rules or filter changes.
- Initial reproduced probes: 4 failures/1 pass; opposite-cross probes separately 4 failures before repair. Final focused: **126 passed /0 failed /1 skipped** (private fixtures, mocked broker fills/stops for CE and PE in both lanes). Locked modules **11/11**, immutable tests **10/10**, recorded parity **9,059 rows x17 fields, zero mismatches**. No real broker orders submitted.
- Reproducible broad baseline on the Cursor checkpoint: **800 passed /57 failed /7 errors /10 skipped**. Same fixture method as v238 (private tracked fixtures, `v197_environment_plugin`, Matrix clock injection, no production test edits); earlier v238 counts 783/57/7/9 are a different source snapshot. Final broad repaired run remains in progress; its counts/differential will be appended when complete. No clean-full-suite claim.
- Controls and setup lifecycle are documented in `analyst/EMA_EXECUTION_AUDIT_V241.md`; new regressions in `analyst/test_ema_finish_v241_8510.py`. Existing test assertions were not changed in this follow-up. Existing runtime config already has owner-approved 3s cadence and Cursor's live-eligible lane settings; desk-wide live remains off.

### v241 host and telemetry follow-up

- Repair source deployed on the owned active branch as `594a8de`; Cursor checkpoint is its parent `0e62d13`. Port 8510 had stopped without this agent sending a stop/restart; verified no port owner or open/pending positions, then launched exactly one daemon. Actual GET acceptance: ONLINE, scanner running, both boards `OI_DECISION_BOARD_V241`, real money false, PAPER_ONLY. Runtime config bytes unchanged. No manual arming, order, cancel or close API called.
- Startup validation found Prototype evaluator's paper-only metadata disagreed with its actual route hint while desk was disarmed. Added copied-row route normalization on each scanner status response; execution logic untouched. Final focused **127 passed /0 failed /1 skipped** (previous phase 126/0/1). Final algorithm broad run still in progress; route metadata follow-up has dedicated focused coverage.

## ChatGPT — v242 missed Prototype trades and missing OI baseline — 19 Nov 2026
- Base a8d014f retains Cursor's intervening consolidation. Isolated branch chatgpt/v242-missed-trades; pre tag pre-chatgpt-v242-missed-trades. No protected 8600 engine, runtime config or broker setting changed.
- Reproduced SQUARE_OFF handler deleting the evaluator's new opposite arm. Preserved that arm; one-time empty repeat-entry state recovery uses completed session candles and respects consumed/stop/open/re-entry state, never past orders.
- Corrected second-resolution session baseline lookup; actual first recorded timestamp/delay is exposed, not claimed as 09:15. Distinct TIMEOUT link status instead of claiming process stopped.
- New regressions initially 3/3 failing, final 5/5 passing. Broader focused final 146 passed /1 skipped /0 failed; previous v241 focused127/1/0 has narrower fixture selection. Ticket/UI suite: baseline34 passed/3 failed versus repair34 passed/3 failed (same three IDs), no assertion changes. Locks11/11. Full analyst suite not yet repeated in this follow-up; no full-green claim. Historical v241 broad baseline800/57/7/10 is a different snapshot.
- Actual host broker entry rejected DH-905 Invalid IP; paper shadow exit does not establish real fill. Global PAPER_ONLY/live off, Prototype lane opt-in on, Reversal opt-in off at inspection. Do not imply trades were prevented by log-only OI. Evidence/limits in analyst/MISSED_TRADES_V242.md. No real orders submitted in verification.

### v242 autonomous quote refresh follow-up
- Host acceptance after first install: ONLINE, scanner running, Prototype recovered LONG; missing-baseline readings now present with actual09:16:15 basis. Runtime config SHA unchanged. No open positions before scoped restart; real money remains OFF.
- Found telemetry refreshed only on startup/UI GET, so closing browser could age scanner's quote cache. Added background caller of existing stale-ok/cache/inflight-guarded refresh path and shutdown cancellation. No new quote source, policy/age relaxation, arming or rate-limit bypass. Focused final148 pass/1skip/0fail; full same-source baseline and candidate runs now launched in separate private fixtures.

### v242 full regression and final transport/history follow-up
- Reproducible full baseline a8d014f:817 passed/56failed/7errors/10skipped; candidate824 passed/56failed/7errors/10skipped. Exact failing/error IDs identical (zero new), private fixtures and v197_environment_plugin as prior audit. Full run includes arm/baseline/background refresh changes, precedes final source-preference/flow-wrapper follow-up. Focused source/context suite66/0; new regressions final9/0; locks11/11, immutable10/10.
- Browser actual8510: recovered CE arm visible, global liveOFF, unchanged filters. Later history gap remained in shadow recorder; canonical market db has those missing minutes, now preferred without mixing datasets. 8600 flow route now offloads synchronous calculation to a thread (only API scheduling changed, protected engine untouched). Full regression not repeated for these last two narrow changes; dedicated tests cover them.
- 8510 became unavailable again during verification without a stop sent by this agent; no position open/pending. Relaunched one process with persistent data/terminal8510/v242_engine.log so future exits can be diagnosed; cause is not established. Do not claim unattended uptime is proven. Browser then loaded ONLINE/build bdf3d1a; source timeout remained separately visible.

### v242 installed handoff
- Active owned branch chatgpt/v242-host-missed-trades:404dd47,bdf3d1a,ed87b1a. 8510 scoped restarts checked empty position list before restart; final health ONLINE/PAPER_ONLY/livefalse/scannertrue; runtime config SHA identical. Browser actual8510 rendered recovered arm, decision matrices and TIMEOUT badge, console errors0. Final after-close reads are allowed to become stale; no source timestamp is renewed to hide missing data.
- Canonical market source now reports recorded09:15:00 baseline rather than restart-gapped shadow09:16:15. Final source/wrapper tests9pass, preceding focused148/1skip. Full paired regression824/56/7/10 vs817/56/7/10, identical failures/errors; last source preference and flow scheduling changes covered by dedicated66-pass source/context and9-pass final regression suites, not another full run. Previous parity v2419059x17 zero mismatches is last parity run; no locked calculation changed.
- 8600 is system service paperdesk-ict.service, MainPID81145 at inspection, Restart=always. Source wrapper change needs service reload; sudo -n requires a password and session has no sudo credential. Do not kill a system-owned service process as a privilege workaround. Owner final activation: sudo systemctl restart paperdesk-ict.service. Broker IP allowlist remains external/unmodified; rejected real orders cannot be counted as fills.


## Gemini (Cursor) — v2.0.39 arm persistence — 19 Nov 2026

- Branch `gemini/v2.0.39-arm-persistence` off v242 head; tags `pre-gemini-v2.0.39-arm-persistence` / `post-gemini-v2.0.39-arm-persistence`. **Do not merge** until Harish reviews.
- **Continuity only:** Prototype arm (`armed_side`, `cross_bar_open_ts`, `away_seen`, `sl_blocked_side`, `day`, `last_touch_ts`, consumed/flip metadata) + Reversal runtime (`crossover_mj_sir_state` JSON slice) + **ema lane lock** persist to ledger SQLite on every material change. Startup loads persisted state; worker logs **ARM RESUMED (persisted)** vs **ARM RECOVERED (rebuilt from cross — gap)**. Cross recovery runs only when no arm was loaded for the session. Stale-spot path pauses evaluation but **persists** the arm (no reset). Entry rules, EMA maths, OI filters, exits untouched; no protected modules; `terminal8510_config.json` not staged.
- **Broker rejection surfacing:** `format_broker_rejection` → human text in orders row, `rejection_reason_display` on order book API, `signal_observations.skip_reason`, and cockpit `execution_decision` (e.g. **DH-905: Invalid IP** from 19 Nov ledger). No retry/auto-place.
- Tests: `analyst/test_arm_persistence_8510.py` **7/7**; v241+v242 bundle **25/25**; `verify_locks` **11/11**. Full analyst suite not re-run this session.
- Full paths: `/home/harish/Documents/Swathi K N/1 Trading/option chain/OC/analyst/ema_arm_persistence_8510.py`, `analyst/test_arm_persistence_8510.py`, `analyst/terminal8510_backend.py` (ledger tables + load/save), `analyst/ema_lane_lock_8510.py` (`since_ist` on acquire).


## Gemini (Cursor) — v2.0.40 finish-line — 19 Nov 2026

- Branch `gemini/v2.0.40-finish-line` off `v2.0.39-arm-persistence`; tags `pre-gemini-v2.0.40-finish-line` / `post-gemini-v2.0.40-finish-line`. **Do not merge.**
- **Part 1:** `analyst/replay_marked_opportunities_8510.py` + report `analyst/reports/MARKED_OPP_REPLAY_2026-11-19.md` — spot replay from recorded 1m + live-shaped config; **4/9** owner marks = **WOULD TAKE** (Prototype 09:41 PE, 13:00 CE, 15:00 CE; Reversal 10:30 CE). Honest NOs: mid-hold at 10:15/10:30, 14:00 no touch, Reversal 09:45/13:15 window miss. No option quotes fabricated.
- **Part 2–3:** OI board `log_only` → **WOULD BLOCK (trading still allowed)**; armed wait strings (`waiting for away from 8 EMA`, `stale spot — touch paused (arm kept)`).
- **Part 4:** `analyst/engine_exit_log_8510.py` → `data/terminal8510/engine_exits.log` (startup/heartbeat/exception/signal/clean shutdown); port-conflict note on bind fail. Prior unexpected stop on v242: **no exit line in v242_engine.log** (likely external kill or second-instance port race — not proven in-repo).
- Tests: `analyst/test_finish_line_v240_8510.py`; oi board armed-string tweak; locks **11/11**. Full suite not re-run.
- Paths: `replay_marked_opportunities_8510.py`, `engine_exit_log_8510.py`, `engine_8510_live.py`, `oi_decision_board_8510.py`, `cockpit_views_8510.py`.


## Gemini (Cursor) — v2.0.41 arm reconstruct — 19 Nov 2026

- Branch `gemini/v2.0.41-arm-reconstruct` off `v2.0.40-finish-line`; tags `pre-gemini-v2.0.41-arm-reconstruct` / `post-gemini-v2.0.41-arm-reconstruct`. **Do not merge.**
- **Startup (09:15–15:45):** Dhan read-only intraday backfill (`analyst/dhan_session_backfill_8510.py`) → market `candles_1m`, then session replay through existing `backtest_ema8_prototype_at_bar` + `backtest_mj_dynamic_at_bar` (same EMA via `nse_spot_candles_8510`). Reconstruction **wins** over SQLite persistence on disagreement; logs **ARM RESUMED (persisted, confirmed by candles)** vs **ARM REBUILT FROM DHAN HISTORY**; **MISSED WHILE OFFLINE** for completed touches in the dead window (no retro fill). Dhan failure → **RECONSTRUCT DEGRADED** and legacy bootstrap/persisted arm only; desk does not crash.
- Hook: `terminal8510_backend.py` prototype worker bootstrap calls `reconstruct_arms_on_startup` before persistence-only bootstrap when replay did not apply.
- Tests: `analyst/test_arm_reconstruct_8510.py` **6/6**; `verify_locks` **11/11**. No entry/EMA/exit/OI logic changes; `terminal8510_config.json` not staged.
- Run: `python3 -m pytest analyst/test_arm_reconstruct_8510.py -q`; `python scripts/verify_locks.py`.
- Full paths: `analyst/arm_reconstruct_8510.py`, `analyst/dhan_session_backfill_8510.py`, `analyst/test_arm_reconstruct_8510.py`, `analyst/terminal8510_backend.py` (bootstrap hook only).


## Gemini (Cursor) — v2.0.42 self-audit — 19 Nov 2026

- Branch `gemini/v2.0.42-selfaudit` off `v2.0.41-arm-reconstruct`; tags `pre-gemini-v2.0.42-selfaudit` / `post-gemini-v2.0.42-selfaudit`. **Do not merge.**
- **Part A:** After reconstruct, `run_desk_self_audit_startup` → `reconcile_lane_lock_on_startup` (holder + zero OPEN → **LANE LOCK AUTO-RELEASED**, SQLite cleared) + `sweep_open_positions` (**EXIT RECOVERED**, lane + `proto_state.open` sync). Scanner loop runs periodic exit sweep alongside EOD cleanup.
- **Part B:** `missed_trade_watchdog_8510` on prototype 3s path — **WOULD_TAKE_UNFILLED** → `data/terminal8510/missed_trades.log` + optional Telegram; **ARMED_IDLE** heartbeat; legitimate blocks silent (open pos, SL-block, out-of-window, cooldown). **Observe only** — no orders.
- **Part C:** `analyst/reports/SELF_AUDIT_C1_C2_8510.md` — C1 PASS (1s range tracking, residual feed-stall risk); C2 PASS on IST 09:15 anchor consumers.
- **Part D:** **NOT included** — **S2′ Flagship reconstruct remains ⚠️.**
- Config patch (not committed): `scripts/patch_selfaudit_8510.py` (`miss_watchdog_enabled`, `armed_idle_heartbeat_min`, `quote_retry_max`).
- Tests: `analyst/test_self_audit_8510.py` **9/9**; `verify_locks` **11/11**. No entry/EMA/exit-threshold changes.
- Run: `python3 -m pytest analyst/test_self_audit_8510.py -q`; `python scripts/verify_locks.py`.

## ChatGPT — v244 order-book rejection display — 20 Nov 2026
- Branch chatgpt/v244-order-rejection-display from f33276e; active branch was gemini/v2.0.43-reject-reason but no committed repair or dirty HTML was present. Owner explicitly requested implementation. Only HTML presentation changed; runtime config and execution/backend untouched.
- Rejected rows display exact stored broker reason plus message-derived category, nested JSON response handling, HTML escaping and explicit missing-reason fallback. DH-905 alone does not imply Invalid IP. No change to paper/live mode labels in this narrow patch.
- Node renderer checks 8 passed/0 failed, including 3 actual rejected20-Nov ledger rows; no prior equivalent renderer baseline. Locks11/11 vs prior11/11. Actual running GET / serves new helper. Full suite/parity not repeated for this HTML-only change; no full-suite claim or browser visual acceptance claim. No broker order, cancel, arming, restart, DB write or config edit performed.

## ChatGPT — v245 public IPv4 comparison — 20 Nov 2026
- Own branch chatgpt/v245-public-ip-check from f9347ad; pre/post tags. New read-only /api/dhan/ip-check uses bounded asynchronous curl -4 to api.ipify.org, 30s cache, public IPv4 validation. Missing configured whitelist is NOT_CONFIGURED; lookup failure never substitutes localhost or old success.
- Header refresh every30s, browser online-event refresh, manual button; compares backend saved broker whitelist (not unsaved input). Explicitly does not verify Dhan registration, change network/IP settings or gate execution.
- Focused regression1pass/0fail (new coverage, no prior equivalent); live resolver check MATCH49.205.36.145; locks11/11 vs11/11. Compile/diff checks pass. Full suite/parity not rerun. Running trading daemon not restarted; endpoint activates on next scoped restart, no live-browser acceptance claimed. No config/database/order operations.

## ChatGPT — v246 stale IP placeholder clarification — 20 Nov 2026
- Verified env and runtime config whitelist both49.205.36.145. Old49.205.39.195 only matched HTML example placeholder among active files searched; removed example. Running daemon GET /api/dhan/ip-check returns404: v245 endpoint not loaded yet. Existing /api/dhan/status omits IP fields. No config mutation or broker operation.
- HTML-only explanatory update; diff check passes. No restart sent during market activity. v245 required restart remains outstanding; not a new IP rejection fix or verified broker order acceptance.

## ChatGPT — v247 OI freshness/direction display — 20 Nov 2026
- HTML-only change on owned branch: blue usable-freshness dot/yellow stale-or-missing; separate green bullish/red bearish direction and muted unavailable direction. Clarifies existing stale fail-open and log-only semantics. No trading gate/policy changes.
- Dhan OI panel now populates existing four numeric pills, dominance and receipt-age from the actual /api/dhan_oi/bias schema; stale values remain visibly stale. Receipt is not called exchange time. No fabricated values or directional verdict.
- Read-only host investigation: three chain momentum sources have no recorded snapshot10±2min before latest; these reads are computed locally from load_day_snapshots, despite board origin8600 label. Dhan API returns STALE/availablefalse with retained numeric values and age~592s at observation. Missing history/collection cause not repaired by UI; no freshness threshold widened.
- Filter suite12passed/0failed, JS full-inline syntax passes, locks11/11 vs prior11/11. No existing assertions modified; full suite/parity not rerun. No restart, arming, config/database mutation or broker orders.

## ChatGPT — v248 remove redundant OI panels from visible UI — 20 Nov 2026
- Owner asked to remove Dhan OI Master Filter & Trigger and standalone15-source8600board. Narrow CSS hides only tab1DhanOICard and proto-8600-strip lane panel; DOM preserved for existing JS bindings. Strategy-specific OI matrices untouched; no execution/config/filter changes.
- Diff check and unique DOM-target assertions pass; no backend restart required. Existing configured filters remain effective even though these redundant configuration panels are hidden. Full suite not rerun for4-line CSS-only change.

## ChatGPT — v249 hide unused desk heading/EOD summary — 20 Nov 2026
- Owner requested removal of Desk management heading and Post-Market Daily Summary card. Narrow2-selector CSS addition hides just heading/card. Parent wrapper is retained because it also contains Dhan API controls; those remain visible. No execution, settings or JS bindings changed. Diff check passes; no restart required.
