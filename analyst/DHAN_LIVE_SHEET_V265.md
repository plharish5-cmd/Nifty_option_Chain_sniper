# Dhan live sheet — gate-first staging implementation

Base: a54c3c2 on origin/reconcile/v2.6.0-consolidated. Own branch chatgpt/v265-dhan-live-sheet. Do not merge without owner instruction. Production checkout/configs remain unchanged.

## Data path

Existing 8600 ChainPoller → existing chain_snap / chain cache → existing 8510 board → desk8600_lane_gating_8510 → existing strategy execution / decision_audit. The new read-only 8510 GET /api/dhan-live-sheet/gates calls that same evaluator for both CE/PE on Prototype, Reversal, Flagship. 8600 GET /api/dhan-live-sheet mirrors this response and displays its cache. It does not create setups, grant live arming, or submit/cancel orders.

All four defaults remain LOG_ONLY. Existing owner-selected BLOCK settings are preserved; no runtime config patch is performed. A BLOCK source vetoes only a usable opposing observation. Missing, invalid, future-aged, nonfinite, expired or non-LIVE observations never veto. Advance/decline is absent from the gate inputs.

Fixes: snapshot_ts preserved across calculation caching; board recomputes age when read; Classic Writer Flow uses its original bridge output, not the duplicate fresh-OI momentum alias. Audit includes source timestamp and original detail; it does not infer who initiated an option trade.

## View

After an approved merge and build/restart, open http://127.0.0.1:8600 → bottom tabs → Dhan live sheet → Gate decisions. The expiry selector changes the chain table only; gate decisions retain the 8510 engine’s configured source scope. This shows OI permission only; NO OI VETO does not mean a setup triggered or an order filled. Engine decisions remain in the existing 8510 decision audit.

Nifty table reads the existing normalized cache; Most active oi ranks standing OI. All Data and Charts persist observed minute totals, top CE/PE by standing OI, and OI × current LTP values (not actual inflows) in the separate dhan_live_sheet.sqlite3 under the existing 8600 DATA_DIR. Missing minutes stay missing. Recorder runs from chain updates independently of browser visibility. Receipt time is explicitly NOT an exchange timestamp. After hours the chain is STALE.

Original workbook preserved. This is the gate-first slice, not a completed replacement for every workbook tab: Banknifty, Midselect and futures chains are not acquired by this module; rho is computed locally using a labelled European Black–Scholes/no-dividend model, ACT/365, configured 6.5% rate; missing IV or expired contracts yield unavailable, not zero. No verified constituent list was found; both breadth views are unavailable/context-only. Cached chain spot is not a 1–2-second WebSocket execution quote. The existing chain poll cadence is unchanged (currently 6 seconds, plus expiry rotation); no 3–5-second live SLA is claimed. Existing locked normalizer defaults some missing provider fields to zero, so the view warns it is not executable quote evidence.

## Start / 09:05 checks after approved integration

1. Refresh the existing Dhan token using the app's existing secure flow or .env; never paste credentials into this feature. No second token store.
2. Run `python3 scripts/verify_locks.py` and focused tests below. Build frontend with `npm ci` then `npm run build`. Never commit dist or runtime DBs.
3. Start the existing 8600 and 8510 services using their established launch commands; do not launch competing daemons or another chain poller.
4. Read GET http://127.0.0.1:8510/api/dhan-live-sheet/gates and GET http://127.0.0.1:8600/api/dhan-live-sheet. Check actual source times/age, expiry and lane settings.
5. At 09:15 observe the real source baseline. Missing history is unavailable; do not fabricate 09:15 values. Verify fresh opposing BLOCK vs LOG_ONLY behaviour in paper testing before changing owner settings.
6. If 8510 mirror is unavailable the view shows UNAVAILABLE, never inferred permissions. A browser refresh does not change lane configuration.

Focused: `.venv/bin/python -m pytest analyst/test_dhan_live_sheet_8510.py analyst/test_8600_gating_truthtable_8510.py analyst/test_desk8600_board_8510.py -q`.

No authenticated Dhan requests, live orders, arming changes or production restarts were performed during this build. Browser verification uses clearly labelled synthetic fixtures, not financial-performance evidence.

## Cursor finish (branch `cursor/v265-oi-gating-into-8510`)

ChatGPT tip lived in the Codex worktree (`v265_dhan_live_sheet`); `origin/chatgpt/v265-dhan-live-sheet` in OC was still at `a54c3c2`. Cursor ported gate mirror + primitives and finished live wiring:

- `writer_bias_primitives_8510.py` + `writer_bias_live_reads_8510.py` → poll vs 09:15 baselines, first-poll unclassified, ATM±6 writing aggregates, 180s slope history, divergence on signal 3; `day_oi` / `fresh_oi` / `total_oi` enriched on the existing board (Classic Writer Flow stays on the 8600 bridge only).
- `desk8600_lane_gating_8510.py` freshness gate (finite age ≤ `stale_after_seconds`, `feed_state==LIVE`); `terminal8510_backend.py` runs `apply_execution_lane_gating` on **Prototype**, Reversal, and Flagship touches; `GET /api/dhan-live-sheet/gates` on 8510.
- Config defaults: `analyst/live_sheet_rules.json` (`oi_window_strikes:6`, etc.), tunable via `scanner_cfg.writer_bias`.
- Verification: `verify_locks.py` **11/11 PASS**; focused pytest **136 passed**; tags `pre-cursor-v265-oi-gating` @ `a54c3c2`, `post-cursor-v265-oi-gating` @ `581a73c`. Session PE-CE writing for signal 1; `nifty_four_oi_8510.classify_fresh_oi_regime` on fresh; prior-day OI deltas in facts; block audits show score/slope/age. 8600 `live_sheet.py` UI not ported.
