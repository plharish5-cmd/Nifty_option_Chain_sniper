# Cursor Work Order — stop the SILENT desk-stalls + make the app catch its own misses

> Owner is tired of being the reconciliation layer ("every day I miss trades, every day I update").
> Two classes of silent all-day stall remain (orphaned lane lock; stuck-open position), and the app
> has NO self-check that a qualified setup actually produced an order. This job fixes both stalls AND
> adds a live Missed-Trade Watchdog so the APP reports a genuine miss, not the owner. Branch
> gemini/v2.0.42-selfaudit off gemini/v2.0.41-arm-reconstruct (or latest consolidated tip). Tags
> pre-/post-. Push. Do NOT merge. No protected modules. verify_locks 11/11. NO entry-rule / EMA /
> exit-rule changes. PAPER_ONLY. No order placement by the watchdog — it observes and alerts only.

```
PART A — LANE AUTO-RELEASE (fix the silent "zero trades all day" stall)   [FMEA S4 + X1]
Two states currently lock the one-trade-at-a-time lane forever with no live position behind it, so the
desk silently takes no further trades the rest of the day:
  S4. Orphaned reservation: engine crashed AFTER a lane reservation but BEFORE/at order submit; on
      restart the lane shows "held" but no real position exists.
  X1. Stuck-open position: an exit (spot ±40 / option ±40 / opposite 15m close / 15:20) was missed
      because of a feed gap at exit time, so the position stays "open" and the lane stays locked.
FIX:
  A1. On startup AND every eval tick, RECONCILE the lane lock against reality: a lane may be "held"
      ONLY if there is a corresponding OPEN position row in the ledger with a live/known status. If the
      lock is held but no matching open position exists → RELEASE the lane, log
      "LANE AUTO-RELEASED (orphaned reservation, no open position)".
  A2. Stuck-open detection: for every position the engine believes is OPEN, re-check its exit conditions
      every tick against current spot/option price AND the time rules. If an exit condition is already
      satisfied (e.g. price is well past the ±40 stop, or it is past 15:20) but no exit was recorded,
      FORCE the exit now at the current/last-known price, record it with reason
      "EXIT RECOVERED (missed at HH:MM, feed gap)", and release the lane. Never leave a position open
      past 15:20; square-off recovery runs on startup too.
  A3. These are RECONCILIATION actions, not new strategy: do not change WHAT the exit thresholds are,
      only ensure a satisfied exit cannot be silently skipped and a lock cannot outlive its position.

PART B — MISSED-TRADE WATCHDOG (the app reports misses, so the owner stops having to)   [FMEA P3/P4/T1/T2]
Add a live reconciliation pass (every eval tick, all setups: Prototype, Reversal 5m/15m, 1H Flagship):
  B1. For each setup, compute "did this setup QUALIFY to enter on this tick?" using the SAME arm→away→
      touch±3 logic the engine already uses (derive from the engine's own decision inputs — do NOT
      re-implement the rule; read the same qualification the entry path computes).
  B2. If a setup QUALIFIED but NO order was produced, classify the reason from the engine's own state:
        - LEGITIMATE (no alarm, info only): lane held by a real open position; SL-blocked side; outside
          09:30–15:20 window; filter in BLOCK mode actually blocked it; cooldown.
        - PROBLEM (ALARM): qualified, lane free, in-window, not legitimately blocked, yet no order →
          classify the mechanical cause: "no option quote (retrying)", "strike selection failed",
          "broker rejected: <DH-xxx>", "arm present but entry path not reached", "stale spot at touch".
  B3. On a PROBLEM: raise it immediately — cockpit banner line ("⚠ MISS: Prototype PE qualified 13:04,
      no order — no option quote"), write to a dedicated log data/terminal8510/missed_trades.log, and
      (if the Telegram/alert hook is configured) push it. One alert per distinct miss (dedupe by
      setup+side+bar), not a spam loop.
  B4. END-OF-TICK HEARTBEAT: if any setup has been ARMED and WAITING past a configurable staleness
      (default 10 min) with no state progress, emit a low-priority "still armed, waiting for touch"
      heartbeat so an idle-but-alive desk is distinguishable from a dead one.
  B5. T2 retry: when a touch qualifies but there is NO option quote, the entry must RETRY on subsequent
      ticks while the touch condition still holds (within the touch band / same bar), not abandon the
      setup after one failed quote. Cap retries sensibly; log each retry reason. (Confirm current
      behaviour first; if it already retries, just surface it — do not change the rule.)

PART C — quick verifies (report findings; fix only if clearly broken, else just document)
  C1. T1 live tick-gap: confirm the LIVE touch check (not just the closed-bar low/high test) catches a
      spot that leaps across the ±3 band between two 3s samples — i.e. it tests "crossed through the
      band since last tick", not only "within ±3 at this instant". If it can skip a fast cross, log it
      as a finding; a minimal fix is to treat "spot crossed the e8 line since last sample" as a touch.
  C2. D3 bar boundary: confirm 15m/1H bar close timestamps and the 09:15 session anchor are computed in
      IST (Asia/Kolkata), not UTC, so bars close on the right minute. Report the result.

GUARDRAILS
- Reconciliation + observability only. NO change to entry rules (arm→away→touch±3), EMA maths, exit
  THRESHOLDS, or OI filters. The watchdog NEVER places or cancels an order — it observes and alerts.
  Lane auto-release/exit-recovery only close/release what reality already satisfies. No protected
  modules. verify_locks 11/11. Config via patch (new keys: miss_watchdog_enabled=True,
  armed_idle_heartbeat_min=10, quote_retry_max). PAPER_ONLY; no real-money toggling.
- TESTS (analyst/test_self_audit_8510.py): orphaned lock with no open position → auto-released;
  satisfied-but-unrecorded exit → recovered + lane freed; position open past 15:20 → squared off on
  next tick/startup; qualified-no-order with free lane + in-window → watchdog raises a PROBLEM with the
  right cause; qualified-no-order legitimately blocked → NO alarm; dedupe (one alert per miss);
  no-quote → retries while touch holds. verify_locks 11/11; full pytest diffed vs baseline — no NEW
  failures.

DELIVERABLE
Commit on gemini/v2.0.42-selfaudit, tags, push, AGENT_STATUS. File list + run commands. State plainly:
after this, (1) the desk can no longer silently stop trading for the day on an orphaned lock or a
stuck-open position, and (2) when a genuine setup qualifies and no order fires, the APP reports it
live — the owner no longer has to discover misses by hand. Paste back the C1/C2 verify findings. Do
not merge.

CONFIRM before coding: lane auto-release when held with no open position; exit recovery when an exit
is satisfied but unrecorded (and 15:20 square-off recovery); live Missed-Trade Watchdog that flags a
qualified-but-no-order setup with its mechanical cause and stays silent on legitimate blocks; armed-
idle heartbeat; no-quote retry while touch holds; C1 tick-gap + C2 IST-boundary verifies reported;
watchdog never places/cancels orders; no rule/EMA/exit-threshold changes; PAPER_ONLY.
```

---

## For you, Harish — plainly
- **Part A kills the two silent "no trades all day" bugs.** If the app crashed mid-entry, or missed an
  exit because the feed blinked, the lane was staying locked forever and the desk quietly traded
  nothing — looking "alive" the whole time. Now it checks every tick that a lock has a real position
  behind it, and recovers a missed exit instead of freezing.
- **Part B is the end of "every day I have to update."** The app now watches itself: the moment a real
  setup qualifies and no order comes out — with no honest reason — it tells YOU (cockpit + log +
  Telegram), with the exact cause. You stop being the one who finds the misses.
- **Part C** answers two I couldn't fully confirm from the code yet (fast cross between ticks; IST bar
  boundaries) — Cursor verifies and reports, fixes only if actually broken.
- This changes **nothing** about your rules — same arm, same touch, same exits. It only stops them from
  being silently skipped, and makes the app honest about it when they are.
