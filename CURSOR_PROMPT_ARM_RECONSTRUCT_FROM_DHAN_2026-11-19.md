# Cursor Work Order — REBUILD the arm from Dhan candles on every open (not just resume a saved arm)

> THE complete fix for "open/close mid-session = missed arm = missed trade, for ALL setups." Persistence
> (v2.0.39) only RESUMES a saved arm — it does nothing when the app opens fresh mid-session or dies before
> saving. This job makes the app RECONSTRUCT the arm from real candles via Dhan on every startup, at any
> time in the session, so a genuine setup is never missed because of when the owner opened or closed the app.
> Branch gemini/v2.0.41-arm-reconstruct off gemini/v2.0.39-arm-persistence (rebase onto v2.0.40-finish-line
> if that has landed). Tags pre-/post-. Push. Do NOT merge. No protected modules. verify_locks 11/11.
> NO entry-rule / EMA-maths / exit changes. PAPER_ONLY. Dhan historical-quote only — no order placement.

```
OWNER REQUIREMENT (verbatim intent)
"If I open the app mid-session, or it closes abruptly for any reason, we miss the arming part and it never
triggers — for ALL setups. Opening or closing on my will should NOT make genuine setups get missed. The app
should RECALCULATE: query Dhan API, populate candles, draw 8 EMA and 4 EMA for ALL timeframes using our
app's 3-second-rule brain; if candles have touched the 8 EMA (above/below) it should ARM — whenever I open
or close. The app should behave as if it was open 09:15–15:45."

ROOT CAUSE THAT REMAINS
Persistence resumes a saved arm, but:
  - Fresh open mid-session (no prior save today) → no arm to resume → setup missed.
  - Abrupt crash before the last state write → arm lost → setup missed.
The arm must therefore be DERIVED FROM THE CANDLES, not only from saved memory. The candle history is the
source of truth for "what is armed right now".

FIX — RECONSTRUCT-ON-STARTUP (runs on every engine start / scanner re-init, any time 09:15–15:45)
1. BACKFILL CANDLES FROM DHAN
   - On startup, query Dhan historical/intraday candles for TODAY'S session (09:15 → now) for the spot
     feed, for every timeframe the setups use: the base series plus 5m, 15m, 1H (Prototype 15m, Reversal
     5m/15m, 1H Flagship). Include enough PRIOR lookback to seed the EMA identically to the live brain.
   - Rate-limit aware: ONE batched historical call per timeframe (not a per-candle flood). Respect the
     Dhan limits from Incident_2026-10-30_Dhan_rate_limit. On a rate-limit/err, back off and retry a
     bounded number of times; if still failing, fall back to persisted arm + log "RECONSTRUCT DEGRADED —
     Dhan history unavailable, using persisted arm only". Never crash the desk on backfill failure.
2. REDRAW 4 EMA + 8 EMA — EXACTLY like the live brain
   - Compute dynamic_8ema / ema4_ema8 over the backfilled candles with the SAME maths used live
     (TradingView EMA, adjust=False, alpha=2/(n+1); forming-bar close = latest spot), seeded from the same
     warmup so the reconstructed EMA converges to the SAME value the 3s brain would hold now. NO strike
     rounding — continuous EMA. This MUST reuse nse_spot_candles_8510's EMA function, not a reimplementation.
   - Verify: reconstructed EMA at the latest closed bar == live-brain EMA for the same bar (assert within a
     tiny epsilon in the test). If they diverge, the seeding is wrong — fix the warmup, do not fudge.
3. REPLAY THE SESSION THROUGH THE SAME ARM STATE MACHINE
   - Feed the backfilled bars through the IDENTICAL arm logic (cross 8 EMA → ARM, no entry on cross bar →
     away ≥2pts seen → waiting for touch; SL-blocked-side; opposite-close flips) for EACH setup, in order,
     to derive the CURRENT state as of now: armed_side, cross_bar_ts, away_seen, sl_blocked_side, and
     whether a position would already be open. This is deriving STATE from history — it is NOT placing
     historical trades.
   - Result: the app comes up already armed exactly as if it had been running since 09:15.
4. CONTINUE LIVE FROM THE CURRENT TICK
   - Hand the reconstructed state to the live 3s brain and continue. The NEXT genuine touch (away-then-±3
     on live spot) is TAKEN normally. If price is ALREADY in the touch band at resume and the setup is
     validly armed, that live touch counts and is taken — it is a live touch now, not a historical one.
5. RECONCILE WITH PERSISTENCE (reconstruction wins)
   - If a persisted arm exists AND agrees with the reconstruction → log "ARM RESUMED (persisted, confirmed
     by candles)".
   - If they disagree, or no persisted arm exists → TRUST THE RECONSTRUCTION, log "ARM REBUILT FROM DHAN
     HISTORY (<setup>, armed <side>, waiting for touch)". Reconstruction is the authority because it comes
     from real candles.
6. HONESTY — the one unrecoverable case (must be logged, not hidden)
   - A touch that BOTH opened AND closed while the app was down cannot be filled retroactively (no live
     price to execute a trade that already came and went). Rebuild the arm, take the next live touch, and
     for any fully-past completed touch in the dead window, log "MISSED WHILE OFFLINE (open HH:MM–HH:MM):
     <setup> <side> touch at HH:MM — not fillable retroactively". Do NOT fabricate a fill. This is the
     true ceiling of the fix and the owner wants to see it, not have it papered over.

APPLIES TO ALL SETUPS
Prototype (15m), 8 EMA Reversal (5m and 15m, incl. 5m-pre-arms-15m), and 1H Flagship — each reconstructed
on its own timeframe. The one-trade-at-a-time lane lock is reconstructed too (if a position would be open,
the lane is held).

GUARDRAILS
- Continuity/reconstruction only: NO change to entry rules (arm→away→touch±3), EMA maths, exits, OI filters,
  or any strategy logic. Setups behave identically; they just rebuild their place from candles on open.
- Dhan: HISTORICAL CANDLE/QUOTE reads ONLY. No order placement, no real-money toggling. PAPER_ONLY stays.
- No protected modules. verify_locks 11/11. Config via patch only (preserve M terminal8510_config.json).
- Reuse nse_spot_candles_8510 EMA; do not reimplement the EMA.

TESTS (analyst/test_arm_reconstruct_8510.py)
- Fresh open mid-session with NO persisted state: feed a synthetic Dhan history where 15m crossed + went
  away before "now" → engine comes up ARMED waiting for touch → a live touch after resume is TAKEN.
- Reconstructed EMA == live-brain EMA at the last closed bar (epsilon assert), all timeframes.
- Crash-with-stale-save then reopen: reconstruction corrects the arm and logs "REBUILT FROM DHAN HISTORY".
- Fully-past completed touch in the dead window → logged "MISSED WHILE OFFLINE", NOT filled.
- Dhan backfill failure → degraded fallback to persisted arm, desk does not crash.
- All four setups reconstruct on their own timeframe; lane lock reconstructs an open position.
- verify_locks 11/11; full pytest diffed against baseline — no NEW failures.

DELIVERABLE
Commit on gemini/v2.0.41-arm-reconstruct, tags, push, AGENT_STATUS block. File list + run commands. State
plainly: after this, opening the app at ANY time 09:15–15:45 rebuilds the arm from Dhan candles and the next
genuine touch is taken — a mid-session open or an abrupt close no longer misses a live setup; only a touch
that fully completed while offline is unrecoverable, and that is logged. Do not merge.

CONFIRM before coding: reconstruct arm from Dhan candles on every startup for ALL setups (not just resume a
saved arm); redraw 4/8 EMA with the SAME maths, reusing nse_spot_candles_8510, EMA matches live within
epsilon; replay derives STATE not historical fills; next live touch after open is TAKEN; reconstruction
beats persistence on disagreement; fully-past touches logged "MISSED WHILE OFFLINE" not fabricated; Dhan
historical reads only, PAPER_ONLY, no rule/EMA/exit changes.
```

---

## For you, Harish — plainly
- **This is the version that actually ends the missed-arm problem.** Persistence was half of it (resume a
  saved arm). This is the other half: **when there's nothing saved — a fresh open, or a crash — the app
  rebuilds the arm from the real candles by asking Dhan, every time it opens.** Open at 11:00, 13:30,
  whenever: it recalculates where the 4/8 EMA are on every timeframe, sees the cross and the touch history,
  and comes up armed exactly as if it had run since 09:15.
- **The only trade it still can't take** is one that opened *and* finished while the app was dead — there's
  no price left to fill it. It won't fake that; it'll show it in the log as "missed while offline" so you
  know it was a real one and not a bug.
- **Dhan is read-only here** (just pulling candles) — no orders, no real money touched by this change.
- Pair this with the Dhan IP fix (DH-905) and a stable run, and the genuine setups get armed and taken no
  matter how you open or close the app.
