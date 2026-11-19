# Independent Test Charter for ChatGPT — does the 8510 desk actually EXECUTE 8 EMA trades?

> You (ChatGPT) are the INDEPENDENT reviewer. Claude wrote the fixes and Cursor implemented them. Your
> job is NOT to confirm they work — it is to TRY TO BREAK THEM and find why the app has, day after day,
> failed to execute genuine **8 EMA** trades (NOT ATM — the signal is the 8 EMA line). Assume nothing in
> the claims below is true until you have reproduced it yourself from the code and tests. Report what you
> can prove, what you can disprove, and what you cannot verify. Push back on Claude's conclusions freely.

## What you are reviewing (claims to falsify, not accept)
Repo: Nifty_option_Chain_sniper. Branch that allegedly contains everything implemented today:
`gemini/v2.0.41-arm-reconstruct` (tip `0fcda9a`). Lineage is claimed linear:
`v2.0.41 → v2.0.40 (9983e53) → v2.0.39 (4a14e2a) → v242 (176c836)`.

Claims made (19 Nov 2026) — verify or refute each independently:
1. v2.0.39 — arm state (Prototype + Reversal) PERSISTS across engine restarts; a restart no longer
   loses the arm. Files: analyst/ema_arm_persistence_8510.py, analyst/ema_lane_lock_8510.py.
2. v2.0.40 — a 19-Nov marked-opportunity replay was produced
   (analyst/reports/MARKED_OPP_REPLAY_2026-11-19.md, score 4/9 "would take"); log-only no longer prints
   a bare "BLOCKED"; engine exit cause is logged (analyst/engine_exit_log_8510.py).
3. v2.0.41 — on every startup the app REBUILDS the arm from Dhan session candles, so opening the app
   mid-session doesn't miss a genuine setup (Prototype + Reversal only; 1H Flagship NOT yet).
   File: analyst/arm_reconstruct_8510.py.
4. NOT yet built: lane auto-release for orphaned locks, intraday stuck-open exit recovery, and a live
   missed-trade watchdog (v2.0.42). Claude claims these are the two remaining silent "zero trades all
   day" bugs (orphaned lane lock; stuck-open position blocking the lane) plus the observability hole.

## Your independent tests (design your own; these are the MINIMUM questions to answer)
A. END-TO-END EXECUTION (the core doubt): starting from a clean checkout of the branch, can you make an
   8 EMA Prototype trade go all the way from 15m-close-cross → arm → away → touch ±3 → ORDER, in paper,
   on recorded or synthetic data? Walk the actual code path and show where the order is (or is not)
   produced. If it stops short, pinpoint the exact line/condition.
B. RESTART CONTINUITY: kill the engine while armed, restart, and confirm (or refute) that the SAME arm
   resumes and the NEXT live touch is taken — not skipped as "historical". Do the same for a FRESH open
   mid-session with no saved state (the v2.0.41 reconstruct claim). Try to produce a case where the arm
   is still lost.
C. THE SILENT STALLS (try hard to trigger them): (1) reserve the lane, kill the process after the lock
   persists but before a fill, restart — is the lane stuck "held" with no open position, blocking all
   further trades? (2) Put a position OPEN whose spot exit is already satisfied but unrecorded — does the
   lane stay locked for the rest of the day? These are the bugs Claude says are UNFIXED (v2.0.42). Prove
   whether they bite in the current branch.
D. TOUCH FIDELITY: can a fast spot move cross the ±3 band BETWEEN two 3s polls and be missed? (C1 in the
   FMEA.) Do the 15m/1H bar boundaries and the 09:15 anchor agree on IST everywhere, or can a boundary
   land on the wrong minute? (C2.)
E. REAL-MONEY PATH: the 09:41 real order was rejected DH-905 (Invalid IP) while paper filled. Confirm
   this is purely a broker-allowlist issue external to the code — or find a code reason real orders don't
   place even with a valid IP.
F. THE REPLAY: independently re-run analyst/replay_marked_opportunities_8510 and judge whether 4/9 is
   honest — are the 5 "no" verdicts legitimate (mid-hold / no-touch / out-of-window) or is the engine
   genuinely missing entries? Re-derive, don't trust the report.

## Rules for your review (so it stays independent)
- Reproduce from code and tests; do not take Claude's table, the FMEA, or this charter as evidence.
- Separate three verdicts cleanly: CONFIRMED (you reproduced it), REFUTED (you showed it false), and
  UNVERIFIABLE (needs live market / live Dhan / owner's host).
- Read-only and PAPER_ONLY. Do not place real orders. Do not flip real-money switches. Do not change
  strategy rules. If you must run the app, keep it on recorded/paper data.
- Rank what you find by impact on "does it execute a genuine 8 EMA trade", most severe first.
- Where Claude's fix is right, say so plainly; where it is wrong, incomplete, or papering over something,
  say that just as plainly. The goal is an app that executes — not agreement.

## Deliverable back to Harish
A findings report: per claim (1–4) CONFIRMED / REFUTED / UNVERIFIABLE with the evidence (file:line, test
name, or repro steps); the ranked list of anything that can still stop a genuine 8 EMA trade; and a clear
verdict on whether v2.0.41 is safe to run live once the Dhan IP is fixed, or what must be fixed first.
