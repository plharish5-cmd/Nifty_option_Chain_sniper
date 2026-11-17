# Cursor Prompt — no off-hours/stale entries (paper included) + catch-all position cleanup

> Paste into Cursor (Gemini). Owner-requested: stop the Homework/Flagship PAPER lane from opening
> trades outside market hours, and make sure every stray position (paper or real) always closes —
> so the cockpit never shows phantom open positions that erode trust before going live.
> Branch gemini/v2.0.31-no-offhours-cleanup off the current tip. Do NOT merge. No protected
> modules. verify_locks 11/11.

---

```
ROLE / CONTEXT
Root cause (confirmed): live_entry_window (terminal8510_backend.py ~2700) gates REAL orders to
09:15-15:20 but explicitly EXEMPTS paper/dry-run ("Paper and dry-run are unaffected; this gates
real orders only"). So the "1H 8-EMA Support + 15m PA (Homework)" / Flagship PAPER lane opened
trades at 19:31 and 00:22 on stale after-hours quotes, and they never hit the 15:20 square-off
(which only runs 15:20-15:30), so they sit open — one even on the expired 17-Nov contract.
Owner's call: turn OFF off-hours paper entries, and guarantee stray positions always close.

GOAL — two parts, both default behaviour (owner asked for this; not a toggle)

PART 1 — No entry when the market is closed or the spot feed is STALE (PAPER INCLUDED)
- Before opening ANY new position (paper, dry-run OR real), require BOTH:
    self.is_nse_market_open(now) is True  AND  spot is_stale is False.
  Currently real orders are gated but paper is not — extend the SAME guard to paper/dry-run
  entries. The Prototype intrabar worker already skips eval on stale spot (v2.0.30); apply the
  equivalent guard to the Flagship/Homework lane and any other lane that can open a paper trade.
- Keep the strategy entry windows as they are for in-session timing (e.g. Prototype 09:30-15:20);
  this is an ADDITIONAL outer guard: outside 09:15-15:30 or on a stale feed, NO lane opens a
  position, paper or real.
- Exits/closes must STILL be allowed outside market hours (modify_open_position already uses
  enforce_market_hours=False) — we must be able to clean up after the close. Only ENTRIES are
  blocked off-hours, never exits.

PART 2 — Catch-all position cleanup (paper AND real)
- Extend the existing EOD sweep (check_eod_square_off) into a general cleanup that runs on the
  scanner loop and closes ANY open position when:
    (a) it is past 15:20 and still open (existing EOD case), OR
    (b) the market is closed and the position is still open (off-hours stray), OR
    (c) the position's contract EXPIRY date < today (expired-contract stray).
  Close reason tags: EOD_SQUARE_OFF_15:20 (a), OFF_HOURS_CLEANUP (b), EXPIRED_CONTRACT_CLEANUP (c).
  Use the least-destructive existing close path (modify_open_position CLOSE, enforce_market_hours
  =False). Cover paper positions the same as real.
- This must be idempotent and safe to run every loop: only acts on positions that are genuinely
  open and match a rule; logs once per close.

VISIBILITY (this is the trust fix — make closes provable)
- Every auto-close writes a clear, timestamped log line AND a telemetry/status field, e.g.:
    "AUTO-CLOSE 17 Nov NIFTY 25400 CE (PAPER) reason=OFF_HOURS_CLEANUP at 00:31:02 IST, LTP 0.05"
- The cockpit should surface a short "last cleanup" note so the owner can SEE positions were
  closed and why (not silently vanish). Also expose a count: "Open positions auto-closed today: N".

EXISTING STUCK POSITIONS
- The two current paper positions (17 Nov 25400 CE, 23 Nov 25400 CE, Homework lane) should be
  closed by the Part-2 cleanup on the next scanner loop (off-hours / expired). Confirm they clear;
  if a paper position can't be matched to a close path, log why rather than leaving it silently open.

GUARDRAILS
- Real-money behaviour unchanged except it also benefits from the catch-all cleanup. The real
  entry window (09:15-15:20) stays. Do not widen any real-order permission.
- No protected modules. verify_locks 11/11. Config via patch script if any new keys; preserve
  M terminal8510_config.json.
- TESTS (analyst/test_offhours_cleanup_8510.py): paper entry attempted off-hours -> blocked;
  paper entry attempted on stale spot -> blocked; in-session paper entry -> allowed; open position
  past 15:20 -> closed EOD; open position off-hours -> closed OFF_HOURS_CLEANUP; position on
  expired contract -> closed EXPIRED_CONTRACT_CLEANUP; exit allowed off-hours (not blocked);
  cleanup is idempotent (second run closes nothing new). verify_locks 11/11; full pytest vs baseline.

DELIVERABLE
Commit on gemini/v2.0.31-no-offhours-cleanup, tags pre-/post-, push, AGENT_STATUS block. File list
+ run commands. State what couldn't be verified (live session). Do not merge.

CONFIRM before coding: entries (paper too) blocked when market closed or spot stale; exits always
allowed; catch-all cleanup closes EOD + off-hours + expired-contract strays for paper and real,
least-destructive path, every close logged and shown; real entry window unchanged.
```

---

## Why this fixes the trust problem (for you)
- **The scary thing can't happen with real money anyway** — real entries are already locked to
  09:15–15:20. This change makes **paper behave the same**, so the cockpit stops showing phantom
  off-hours positions that made it look broken.
- **Every close is now logged and shown**, so instead of wondering "did it close?", you'll see
  "AUTO-CLOSE … reason=OFF_HOURS_CLEANUP" and a daily count. That's the evidence that builds the
  confidence to go live.
- **Exits are never blocked off-hours** — only entries. So cleanup can always run, even at midnight.
- Your two stuck paper positions get swept on the next loop once this is in (or square them off
  manually now — they're paper, zero cost).
```
