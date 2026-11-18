# Cursor Prompt — finish & enable the full stack (Reversal real-money + filters live)

> Owner wants everything enabled and is ready to trade real money. Enable the full stack — but
> COMPLETE the missing real-money-safety pieces in the SAME pass rather than flipping unproven
> switches. The one hard gate: the Reversal goes real-money ONLY after its cross-lane double-fill
> test is green. Branch gemini/v2.0.34-finish-and-enable off agy/v2.0.32-reversal-revamp (so it
> carries the Reversal scaffold). Tags pre-/post-. Push. Do NOT merge. No protected modules.
> verify_locks 11/11.

```
GOAL: make the Reversal real-money-capable and enable the discussed features, SAFELY and today.

=== PART 1 — finish Reversal execution (required before real money) ===
- Contract pick: current-week CE/PE, premium >= ₹200, deeper-ITM walk in 50pt steps — REUSE the
  Prototype's selector (_select_ema8_prototype_contract_any_strike or equivalent), not only the ATM
  matrix. Same NO_ENABLED_CONTRACT reason string.
- Live candle trail: after +20 on the ACTIVE timeframe (5m candles in 5m mode, 15m in 15m/pre-arm),
  trail via flagship_candle_trail_update (long green-low / short red-high, monotonic). ±20 base,
  opposite-cross and 15:20 square-off still apply. Reuse the helper; no new trail maths.
- Route through the SAME real execution path as the Prototype: chaser, companion SL, sell only when
  live_filled=1, companion stop confirmed out before SELL, resumable EXIT_PENDING. 15:20 square-off
  and the v2.0.31 off-hours entry guard apply (paper + real).

=== PART 2 — the safety gate (MANDATORY before lifting the paper lock) ===
- Write analyst/test_reversal_crosslane_8510.py proving the ema_lane_lock prevents ANY second open
  position across Reversal(5m)+Reversal(15m)+Prototype: when one lane holds a position (paper OR
  real), no other lane can open one; lock releases on close; first-to-arm-and-touch wins. Also a
  Stage 2/3 golden-parity test proving their behaviour is byte-for-byte unchanged.
- ONLY if BOTH tests pass: lift the mj_sir_paper_only lock for the REVERSAL lane so it can route
  real money (execution_mode LIVE_ALLOWED/PAPER_ONLY like the Prototype). KEEP Stage 2 and Stage 3
  HARD paper-locked (mj_sir_paper_only stays True for them — owner did NOT ask to real-money those).
- If the double-fill test cannot be made to pass in this pass, DO NOT lift the lock; leave Reversal
  paper-only and say so plainly in the report. Never ship real-money double-fill risk.

=== PART 3 — enable the features (as options, safe defaults) ===
Patch scanner config (via patch script, backup first) to ENABLE and surface everything, with
real-money-safe defaults:
- Reversal: enabled=true, timeframe_mode="15m" (toggle to 5m/both available), ema_separation ON,
  candle_trail ON, pre_arm_15m_from_5m available (default off). Full cockpit toggles (TF, pre-arm,
  separation, trail, OI) with the present-key guard.
- OI filters + momentum + oi_3m + price_confirm (Prototype AND Reversal): switches ON but
  mode="log_only" — they run live and show on the cockpit (S, bias, CumD, price agrees/diverges,
  would-block) but DO NOT block real trades yet. Expose the "block" toggle so the owner can flip to
  gating after a backtest. Default rule all_agree, sig 0.15/0.30, fail-OPEN.
- PE-CE volume columns + volume-confirm: ensure visible/available.
- Everything stays per-strategy and per-lane toggleable from the cockpit.

=== PART 4 — backtest hooks (so he can validate what's now live) ===
- Add the Reversal factors (TF, separation, trail, OI filters, price_confirm) into proto_sweep_8510
  / Backtest Lab so the owner can run held-out on exactly what's enabled. Report ±20 vs ±20+trail,
  5m vs 15m, filters log vs block.

GUARDRAILS
- Prototype real-money path unchanged (it was already ready). Real entry window 09:15-15:20 intact.
- Stage 2/3 stay paper-locked and byte-for-byte unchanged. Protected modules untouched; verify_locks
  11/11. Config via patch script; preserve M terminal8510_config.json. No remote model calls.
- The Reversal lifts to real money ONLY behind a green double-fill test. Filters enabled in log_only,
  not blocking real trades, until the owner flips them post-backtest.
- TESTS: cross-lane double-fill (Part 2), Stage 2/3 golden parity, Reversal contract pick, live trail
  after +20, off-hours entry blocked, filters log_only = no real block. verify_locks 11/11; full
  pytest diffed against the ~879/35/42 baseline — no NEW failures.

DELIVERABLE
Commit on gemini/v2.0.34-finish-and-enable, tags, push, AGENT_STATUS block. File list + run commands.
State clearly: did the double-fill test pass (so Reversal is real-money enabled) or not (so it stays
paper)? State what couldn't be verified (live Dhan/market). Do not merge.

CONFIRM before coding: finish Reversal execution (contract pick + live trail + real exec path);
the double-fill + Stage golden-parity tests MUST pass before lifting the Reversal paper lock; Stage
2/3 stay paper-locked; filters enabled in LOG-ONLY (not blocking real money) until backtested;
Prototype + protected modules unchanged; report truthfully whether Reversal reached real-money state.
```

---

## For you, plainly
- This gets the Reversal to **real money the right way, likely today** — the moment the double-fill
  test is green, the lock lifts and you can arm it live (paper-first for a session is still smart).
- The filters turn **on today** in log-only — live, visible, gathering evidence — so no day is
  "lost"; you're building the backtest data from today. Flip them to block the day the numbers say so.
- **The Prototype is ready now** — renew the Dhan token and you trade real money today regardless of
  any of the above.
- If Cursor reports the double-fill test did NOT pass, the Reversal stays paper — and that's the
  system protecting you from two conflicting real orders, not another lost day. Better one more
  paper session than a real double-fill loss.
```
