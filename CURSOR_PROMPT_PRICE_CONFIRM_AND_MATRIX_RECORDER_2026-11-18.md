# Cursor Prompt — writers+price confirm on the S gate (Path B) + 8600 Matrix recorder (Path A)

> Paste into Cursor (Gemini). Two parts on branch gemini/v2.0.33-price-confirm-matrix-recorder off
> the current tip. Path B is usable soon (backtestable). Path A only records, so the 8600 Matrix
> can be backtested LATER — it does NOT gate anything yet. Read
> `OI_Momentum_v2_And_OI3m_Handover_Pack_2026-11-17.md` first. Tags pre-/post-. Do not merge.
> No protected modules. verify_locks 11/11.

Context: the 8600 Unified Tape Read called all 6 major turns on 17 Nov (rally, top, breakdown,
bounce, EOD "not direction"), and its best component is "Confirmed — writers + price agree". But
the Matrix is "display port only, provenance not certified" (NO_RECORDED_DATA -> ADVISORY_ONLY),
so it cannot be backtested today and must NOT gate real money. Path B captures its best idea in a
backtestable form; Path A records the Matrix so it becomes backtestable in a few weeks.

```
=== PART A (Path B in the plan) — "writers + price agree" confirm on the momentum S gate ===
GOAL: add an optional confirmation so an S signal only counts when PRICE agrees with the OI
momentum — the backtestable version of the 8600 "Confirmed — writers + price agree".
- New config scanner.ema8_prototype.oi_filters.momentum.require_price_confirm (default false).
  Mirror it for the Reversal (scanner.reversal...) if that branch is present; otherwise Prototype only.
- When true, a momentum direction is kept only if price confirms it over the SAME window used for S:
    CE (bullish S) kept only if price is making higher highs / higher closes over the window
       (e.g. close_now > close_window_ago AND recent swing high rising);
    PE (bearish S) kept only if price is making lower lows / lower closes over the window.
  If price does NOT agree, downgrade the read to NEUTRAL (fail-OPEN -> never blocks). When false,
  no change at all.
- Compute price agreement from the SAME recorded spot series the Prototype already uses (no new
  feed). Pure, testable, no I/O. Reuse the existing window_min. Show "price: agrees / diverges"
  next to the S line on the cockpit (display).
- This is the divergence-vs-confirmation distinction from the tape: S alone = writers only;
  S + price-confirm = "confirmed". Default OFF; judge on held-out points like every other option.

=== PART B (Path A in the plan) — record the 8600 Matrix / tape verdict (NO gating) ===
GOAL: start persisting the 8600 Matrix 2.0 / Unified Tape verdict per interval so it gains
certified history and can be BACKTESTED later. This part adds NO gate and changes NO trade.
- On each scanner tick (when the 8600 bridge is live), capture whatever the bridge already exposes
  for the tape read — at minimum: timestamp, spot, the direction/bias, the conviction grade
  (A+/A/B/Noise + %), the "confirmed/divergence" flag, PCR, and the Put/Call angle (Δ) values if
  present — and append a row to a NEW recorded table (e.g. desk8600_matrix_log) in the recorded DB
  (the same gitignored data store; never commit it).
- Record ONLY what the bridge actually provides; do NOT synthesize or invent fields. If a field is
  absent, store null. Mark provenance = "recorded_live" so a future backtest knows it is certified
  (vs the current "display port only").
- Idempotent: one row per interval; skip if already recorded. Never raises; a recording failure
  must never affect trading.
- This does NOT feed oi_switch_block or any gate. It is purely accumulating history. Add a cockpit
  line "8600 Matrix recording: N rows today" so the owner can see it building.
- Explicit NOTE in code + AGENT_STATUS: until enough recorded history exists and it passes a
  held-out backtest, the Matrix stays ADVISORY_ONLY and must not gate real money.

GUARDRAILS (both)
- Part A default OFF, backtestable, held-out validated before use; Part B records only, no gating.
- No protected modules. verify_locks 11/11. Config via patch script (backup first); preserve
  M terminal8510_config.json. No remote model calls.
- TESTS: Part A — price-confirm off = identical to now; on + price agrees = S kept; on + price
  diverges = NEUTRAL (no block); computed from recorded spot only. Part B — a tick records one row
  with the bridge fields; absent fields -> null; idempotent (no dup); recording failure is swallowed
  and trading continues; recorder never touches oi_switch_block. verify_locks 11/11; full pytest vs baseline.

DELIVERABLE
Commit on gemini/v2.0.33-price-confirm-matrix-recorder, tags, push, AGENT_STATUS block. File list +
run commands. State what couldn't be verified (live 8600 bridge, live market). Do not merge.

CONFIRM before coding: Part A is default-OFF, computed from recorded spot, only downgrades to
NEUTRAL (never a new block), backtestable; Part B only RECORDS the 8600 Matrix verdict (no gate, no
trade change), idempotent, failure-swallowing, and the Matrix stays ADVISORY_ONLY until backtested.
```

---

## Why this is the right response to "the signals look great"
- The tape calling 6/6 turns on an up-day is **evidence to record and test it**, not to gate on it.
  Path B gives you the tape's best idea ("confirmed — writers + price agree") in a form you **can**
  backtest and trust with real money; Path A starts building the history so the full Matrix can be
  judged later, honestly, on down/chop days too — not just the one pretty up-day.
- Nothing here risks real money: Part A only ever *withholds* a trade (downgrades to NEUTRAL), and
  Part B touches no trade at all. Both default to no-change.
- In a few weeks, when the Matrix log has history, run it through the backtest sweep with FDR
  correction. If it survives on held-out days, *then* consider gating on it. If it doesn't, you'll
  have learned the 17 Nov fit was the up-day seduction — cheaply, without losing real money.
```
