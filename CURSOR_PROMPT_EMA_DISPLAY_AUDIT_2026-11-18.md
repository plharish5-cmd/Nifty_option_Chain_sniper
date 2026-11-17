# Cursor Prompt — 8 EMA display + audit (NO maths change, NO new engine)

> Paste the box into Cursor (Gemini). This is the SAFE version. It does NOT build a new EMA
> engine and does NOT round the EMA to a strike (that would break the validated ±3pt touch).
> Context: the 8510 already has ONE shared EMA engine (`ema_tv`/`ema4_ema8` in
> `nse_spot_candles_8510.py`) imported by Prototype, Stage 2/3, Reversal and Flagship — no
> duplicate calculators, no rounding, spot-only. The only real "mismatch" is live-vs-candle,
> which is BY DESIGN. This feature just makes that visible and auditable.

---

```
ROLE
Edit 8510 Algo Terminal on new branch gemini/v2.0.30-ema-display-audit, cut from the current tip.
Do NOT merge. Do NOT touch PROTECTED MODULES. verify_locks 11/11.

HARD RULES — DO NOT BREAK THE PROTOTYPE
- 8 EMA is CONTINUOUS, never rounded to a strike. Formula stays exactly:
  ema = prev + (2/9)*(price - prev) on 15m closes, via existing ema4_ema8 in
  analyst/nse_spot_candles_8510.py. Do NOT add round(Price/50)*50, /50, /100 anywhere near EMA.
  Do NOT create a new EMA engine.
- Strike rounding is ONLY for contract selection (ATM±100 matrix). It stays completely separate
  from EMA calculation and touch logic.
- Touch rule unchanged: price within ±3 of the continuous 8 EMA.
- Source stays NIFTY 50 spot only. No futures.

GOAL — display + audit only
1. Cockpit: show BOTH values side-by-side, labelled:
   "8EMA (live): 25411.3"        — running EMA, recomputed every 3s with live spot
   "8EMA (15m close): 25408.0"   — EMA from the last closed 15m bar
   One-line note: "Live vs close differ mid-candle by design; they converge at candle close."
2. Trade log: timestamp + source-stamp every EMA used for a touch:
   "8EMA 25411.3 @ 10:15:03 from NIFTY spot 25411.0, 15m close EMA 25408.0"
   so any trade can be replayed to prove which value fired the entry.
3. Stale guard: if spot hasn't updated in >5s, show EMA STALE in cockpit and do NOT act on it
   (fail OPEN). Reuse the existing desk-stale banner if present; do not duplicate it.

TESTS
- EMA value unchanged vs baseline on the same data (byte-for-byte; no rounding introduced).
- Live and close labels both present; values differ mid-candle and converge at the close in backtest.
- Log contains timestamp + spot source for every touch.
- Stale >5s -> STALE shown, no trade taken.
- verify_locks 11/11; full pytest diffed against baseline.

DELIVERABLE
Commit on gemini/v2.0.30-ema-display-audit, tags pre-/post-, push, AGENT_STATUS.md block. Do NOT merge.
CONFIRM before coding: no new EMA engine, no rounding, no maths change, spot-only, display + audit only.
```

---

## Why this is safe (and the ATM-Engine prompt was not)
- The earlier "single 8 EMA Engine with `round(Price/50)*50`" idea was a mistake: it treated the
  EMA as a strike. The EMA is continuous (25411.3), and the touch is ±3 of that value — rounding
  it to 50 would destroy the logic validated on the 13:08 data. **That prompt is dead; do not use it.**
- The codebase already has one shared EMA engine, no rounding, spot-only — so there is nothing to
  consolidate. This feature only adds **display + audit**, with the Prototype maths untouched.

## The 1-minute test tomorrow (10:15:00 sharp)
Screenshot the Prototype `8EMA (live)`, the chart's 8 EMA, and spot LTP. Expect: live EMA near
spot, chart EMA at the last 15m close, a few points apart, converging by the 10:15 candle close.
If they're 20+ apart and NOT converging, that's a feed-lag issue to chase — not EMA maths.
