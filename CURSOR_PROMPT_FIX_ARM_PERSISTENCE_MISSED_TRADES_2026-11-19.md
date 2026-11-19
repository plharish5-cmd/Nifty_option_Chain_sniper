# Cursor Prompt — FIX THE ROOT CAUSE of chronic missed trades: arm-state persistence

> THE bug behind "app never executes / misses every trade." Audit log proof (19 Nov):
> 09:46 LONG ARMED → five-hour hole → 14:40/15:02/15:23 "ARM RECOVERED — no historical entry
> placed." The armed state is LOST on every engine restart / scanner re-sync and rebuilt too late,
> so touches in the gap are never taken. This is the single highest-priority fix. ONE job — do not
> add features. Branch gemini/v2.0.39-arm-persistence off gemini/v2.0.38-oi-decision-board.
> Tags pre-/post-. Push. Do NOT merge. No protected modules. verify_locks 11/11.
> NOTE: ChatGPT has a tag pre-chatgpt-v242-missed-trades — coordinate; do not clobber its branch.

```
ROOT CAUSE (confirmed from the live audit log)
The Prototype/Reversal armed state (armed side, cross-bar ts, away_seen, sl_blocked_side, in-trade,
last touch) lives in memory. On engine restart or scanner re-init it is lost, then "recovered" from
the last completed 15m cross — but the recovery explicitly places "no historical entry," so every
touch that occurred while the arm was missing is permanently skipped. Owner restarts the engine
often (dev churn), so the arm is dropped repeatedly during market hours → chronic missed trades.

FIX — PERSIST AND RESUME THE ARM STATE CONTINUOUSLY
1. Persist the full lane/arm state for Prototype AND Reversal to the state/ledger DB (not just
   in-memory): armed_side, cross_bar_open_ts, away_seen, sl_blocked_side, in_trade/position ref,
   last_touch_ts, timeframe, and the lane-lock holder. Write on every state change.
2. On engine startup / scanner re-init, LOAD the persisted arm state and RESUME it live — keep
   evaluating touches continuously from the current tick. Do NOT discard the arm and rebuild from
   the last cross; the persisted arm IS the truth. If armed LONG and a valid touch (away-then-±3)
   occurs AFTER resume, TAKE the entry normally — it is a live touch, not a historical one.
3. Keep "no historical entry" ONLY for genuinely past touches (you cannot trade a touch that already
   happened before the process was alive). The point is the arm must no longer be LOST, so after a
   restart the SAME arm continues and the NEXT live touch is taken. A restart must not create a gap.
4. Stale-spot handling: a transient stale-spot blip may PAUSE touch evaluation (fail-open) but must
   NOT drop or reset the arm. When spot is fresh again, resume the same arm. (Board showed "spot
   feed stale — touch logic paused" — ensure pause != lose arm.)
5. Make the recovery log honest: distinguish "ARM RESUMED (persisted)" from "ARM RECOVERED (rebuilt
   from cross — gap, entries in gap missed)". If you ever see RECOVERED-rebuilt during a normal
   session, that is a persistence miss to fix, not normal behaviour.

ALSO — surface WHY real orders are REJECTED
- The 19 Nov 09:41 real BUY 25300 PE shows REJECTED while paper filled. Capture and SHOW the broker
  rejection reason (token invalid / margin / order param / market-closed) in the audit log and the
  orders book row, so the owner can see WHY a real order failed instead of a bare "REJECTED". Do not
  retry or auto-place anything — just surface the reason.

GUARDRAILS
- This is a CONTINUITY/persistence fix: do NOT change the entry rules (arm→away→touch±3), the EMA
  maths, exits, or the OI filters. Prototype and Reversal logic stay identical; they just stop
  losing their place across restarts. No protected modules. verify_locks 11/11. No config schema
  change beyond a state table/columns; preserve M terminal8510_config.json.
- Do NOT enable/flip anything to real money in this change, and do NOT touch the OI filters — the
  decision board already proved they are not the cause (all stale/no-data/fail-open, never skip).
- TESTS (analyst/test_arm_persistence_8510.py): arm persists and reloads across a simulated restart
  for Prototype and Reversal; after reload, a live touch is TAKEN (not skipped as "historical");
  a stale-spot blip pauses but does not drop the arm; the lane lock survives restart; the five-hour-
  hole scenario (arm at T, restart at T+n, live touch at T+2n) now TAKES the T+2n entry. verify_locks
  11/11; full pytest diffed against the ~889/35/42 baseline — no new failures.

DELIVERABLE
Commit on gemini/v2.0.39-arm-persistence, tags, push, AGENT_STATUS block. File list + run commands.
State plainly: after this, an engine restart RESUMES the armed state and the next live touch is
taken — no more five-hour arm holes. State the real-order rejection reason you surfaced (if
determinable). Do not merge.

CONFIRM before coding: persist + resume arm state across restarts for Prototype AND Reversal (next
live touch after restart is TAKEN, not skipped); stale-spot pauses but never drops the arm; surface
the real-order rejection reason; NO entry-rule / EMA / filter / exit changes; NO real-money toggling.
```

---

## For you, Harish — plainly
- **This is the fix for "it never executes."** After it, restarting the engine (which you do every
  deploy) no longer throws away the arm — the same armed setup resumes and the next real touch is
  taken. The five-hour holes stop.
- **The filters and 8600 are cleared** — your own decision board proves they never blocked a trade.
  So stop suspecting them; the cure is arm persistence.
- **Real money:** once the arm persists AND we know why the real leg rejected (token/margin), the
  enabled lanes will actually fill. I'm not flipping more real-money switches onto a path that's
  dropping arms and rejecting orders — that just loses real money. Fix this bug first; then real
  money works instead of missing.
- **8600:** start the 8600 app so its data flows — but that's for *context*, not for fixing the
  misses.
- **One ask that will genuinely help:** once this lands, **stop redeploying mid-session.** Let one
  fix run for a full day. Half of today's churn was restarts eating the arm — persistence fixes the
  mechanism, but a stable day with no mid-session restarts is how you'll finally see it trade clean.
```
