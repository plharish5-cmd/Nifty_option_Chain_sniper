# Cursor Work Order — v2.0.42: stop the SILENT desk-stalls + make the app catch its own misses

> Owner is tired of being the reconciliation layer ("every day I miss trades, every day I update").
> Two classes of silent all-day stall remain in code (orphaned lane lock = S4/L1; stuck-open position =
> X1/L2), and the live loop has NO self-check that a qualified setup actually produced an order (P3/P4).
> This job fixes both stalls AND adds a live Missed-Trade Watchdog so the APP reports a genuine miss, not
> the owner. Optional Part D closes the S2′ Flagship gap in reconstruct. Branch gemini/v2.0.42-selfaudit
> off gemini/v2.0.41-arm-reconstruct. Tags pre-/post-. Push. Do NOT merge. No protected modules.
> verify_locks 11/11. NO entry-rule / EMA / exit-THRESHOLD changes. PAPER_ONLY. The watchdog NEVER
> places or cancels an order — observe + alert only. Lane/exit recovery only closes/releases what reality
> already satisfies.

```
CODE ANCHORS (owner-validated — read these before changing anything)
- Reserve + immediate persist: terminal8510_backend.py ~7665–7675 (try_reserve_lane_for_signal → ...
  _save_ema_lane_lock_state). Unfilled release path ~7968–7975 (finally). GAP: process death AFTER the
  persist but BEFORE/at fill leaves a holder with NO open position. (test_lane_lock_survives_restart is
  exactly this failure frame.)
- Open-position cleanup today is EOD / off-hours / expiry ONLY: check_open_position_cleanup ~4508–4550.
  It does NOT handle a missed intraday spot exit during a feed gap.
- Entry gate honors open positions: lane_lock_blocks_entry(..., open_position_count>0) → a stuck-OPEN
  row blocks the desk for the rest of the day.

PART A — LANE + EXIT RECONCILIATION (fix the two silent "zero trades all day" stalls)   [S4/L1 + X1/L2]
A1. STARTUP RECONCILE — run AFTER reconstruct_arms_on_startup, BEFORE the scanner starts:
    if the lane holder is set AND get_active_positions() count == 0 →
      release the lock, clear the SQLite lane-lock row, log
      "LANE LOCK AUTO-RELEASED (no open position)".
    Optional hardening: also release if a reservation has no order id and its since_ist age > 15 min.
A2. OPEN-POSITION EXIT SWEEP — periodic on the eval loop AND once at startup:
    re-walk the spot exit rules (±40 spot / ±40 option / opposite 15m close / 15:20) for EACH OPEN
    Prototype / Reversal / Flagship ledger row against current (or last-known) price + time. If an exit
    condition is ALREADY satisfied but no exit was recorded →
      modify_open_position(CLOSE) at the current/last-known price,
      record reason "EXIT RECOVERED (missed at HH:MM, feed gap)",
      release the lane, and sync proto_state["open"] (and the Reversal/Flagship equivalents).
    This COMPLEMENTS the existing EOD cleanup (X3); it adds the intraday-gap case (X1). Never leave a
    position open past 15:20 — square-off recovery runs here and at startup.
A3. These are RECONCILIATION actions only: do NOT change the exit THRESHOLDS or WHAT triggers an exit —
    only ensure a satisfied exit cannot be silently skipped and a lock cannot outlive its position.

PART B — MISSED-TRADE WATCHDOG (the app reports misses, so the owner stops)   [P3 / P4 / T1 / T2]
B1. On each 3s cycle (and at each 1m close), for every setup (Prototype, Reversal 5m/15m, 1H Flagship):
    read the SAME qualification the entry path computes — "is this setup ARMED and is the retouch math
    (away-then-touch ±3) TRUE on the current spot/tick range right now?" Do NOT re-implement the rule;
    reuse the engine's own decision inputs so the watchdog can never disagree with the gate.
B2. If a setup QUALIFIED but produced NO signal/order within N seconds, classify from the engine's own
    state:
      LEGITIMATE (info only, NO alarm): lane held by a REAL open position; SL-blocked side; outside
        09:30–15:20; a filter in BLOCK mode actually blocked it; active cooldown.
      PROBLEM (ALARM): qualified, lane free, in-window, not legitimately blocked, yet no order → name the
        mechanical cause: "no option quote (retrying)", "strike selection failed", "broker rejected:
        <DH-xxx>", "arm present but entry path not reached", "stale spot at touch".
B3. On a PROBLEM emit "WOULD_TAKE_UNFILLED": cockpit banner line (e.g. "⚠ MISS: Prototype PE qualified
    13:04, no order — no option quote"), append to data/terminal8510/missed_trades.log, and push to the
    Telegram/alert hook if configured. Dedupe by setup+side+bar — one alert per distinct miss, no spam.
B4. ARMED-IDLE HEARTBEAT: if a setup has been ARMED + WAITING past armed_idle_heartbeat_min (default 10)
    with no evaluator progress, emit low-priority "ARMED_IDLE (still waiting for touch)" so an idle-but-
    alive desk is distinguishable from a dead one.
B5. T2 RETRY (confirm first, change only if needed): when a touch qualifies but there is NO option quote,
    the entry must RETRY on subsequent ticks WHILE the touch condition still holds (band still held /
    same bar) and WITHOUT consuming the arm/touch — not abandon after one failed quote. skip_reason +
    skipped_signal_retry_seconds (5s) + _release_unfilled_setup already exist; PROVE armed+band-held
    retries across ticks without eating the arm. If it already retries, just surface it — don't change
    the rule.

PART C — VERIFY-AND-REPORT (fix only if clearly broken; otherwise document the finding)   [C1 / C2]
C1. LIVE TICK-GAP (T1): check_retouch (~ema8_prototype_8510.py 377–380) forbids away+touch on the same
    tick and uses per-tick tick_high/tick_low; the worker widens the range from 1m slices between 3s
    polls (~terminal8510_backend.py 5217–5218). GAP TO TEST: a band cross entirely BETWEEN polls with no
    captured extreme can still be missed. Fixture: spot path jumps across the e8 band in <3s without
    updating tick_hi/lo → does a touch register? If it can be skipped, a minimal fix is to treat "spot
    crossed the e8 line since the last sample" as a touch. Report the finding either way.
C2. IST BAR BOUNDARY (D3): the 09:15 session anchor + resample (resample_nse_15m / _1h) are explicit in
    nse_spot_candles_8510.py. PROVE that bar-OPEN ts and the "15m close" handlers agree on IST
    (Asia/Kolkata) EVERYWHERE they're consumed — the prototype 3s worker, the MJ dynamic path, AND the
    OI baseline — not only inside resample. Report pass/fail per consumer.

PART D (OPTIONAL, owner's call) — S2′: Flagship into the reconstruct story
Today arm_reconstruct_8510.py rebuilds Prototype + Reversal only; 1H Flagship arm/state is still live-
path only, so a mid-session open leaves Flagship un-reconstructed while the other two are correct. If
included: extend arm_reconstruct_8510.py to replay the 1H session candles through the Flagship arm state
machine on startup, same pattern (reuse nse_spot_candles_8510 EMA; derive state, don't place historical
fills; reconstruction beats persistence on disagreement). Same guardrails. If NOT included now, the FMEA
keeps S2′ as a known ⚠️ gap — do not silently skip it, call it out in AGENT_STATUS.

GUARDRAILS
- Reconciliation + observability only. NO change to entry rules (arm→away→touch±3), EMA maths, exit
  THRESHOLDS, or OI filters. Watchdog never places/cancels orders. No protected modules. verify_locks
  11/11. Config via patch (new keys: miss_watchdog_enabled=True, armed_idle_heartbeat_min=10,
  quote_retry_max). Preserve M terminal8510_config.json. PAPER_ONLY; no real-money toggling.
- Owner-only, do NOT work around in code: DH-905 Dhan IP allowlist; 8600 systemd restart; mid-session
  redeploy discipline. Keep surfacing them, don't fake around them.

TESTS (analyst/test_self_audit_8510.py)
- CRASH-AFTER-RESERVE: lane holder persisted, get_active_positions()==0 at startup → auto-released +
  SQLite row cleared.
- STUCK-OPEN AT TARGET: OPEN row whose spot exit is already satisfied with a stale LTP → sweep closes it
  (EXIT RECOVERED) + lane freed + proto_state.open synced.
- OPEN past 15:20 → squared off on next tick / at startup.
- QUALIFIED-NO-ORDER, lane free + in-window → watchdog raises WOULD_TAKE_UNFILLED with the right cause.
- QUALIFIED-NO-ORDER, legitimately blocked (real open pos / SL-blocked / out-of-window / BLOCK filter)
  → NO alarm.
- DEDUPE: one alert per setup+side+bar.
- NO-QUOTE: retries while touch holds without consuming arm/touch.
- (If Part D) Flagship reconstructs on its 1H timeframe on a fresh mid-session open.
- verify_locks 11/11; full pytest diffed vs baseline — no NEW failures.

DELIVERABLE
Commit on gemini/v2.0.42-selfaudit, tags, push, AGENT_STATUS block (state whether Part D was included;
if not, flag S2′ as a remaining gap). File list + run commands. Paste back the C1/C2 verify findings.
State plainly: after this, (1) the desk can no longer silently stop trading for the day on an orphaned
lock or a stuck-open position, and (2) when a genuine setup qualifies and no order fires, the APP reports
it live with the cause — the owner no longer hunts misses by hand. Do not merge.

CONFIRM before coding: startup lane reconcile (holder set + 0 active positions → release + clear row,
after reconstruct / before scanner); intraday open-position exit sweep with EXIT RECOVERED + lane
release + proto_state sync (plus 15:20 square-off recovery); live Missed-Trade Watchdog raising
WOULD_TAKE_UNFILLED on a qualified-but-no-order setup with its mechanical cause, silent on legitimate
blocks, deduped; ARMED_IDLE heartbeat; no-quote retry while touch holds without eating the arm; C1 tick-
gap + C2 IST-boundary verified and reported; Part D Flagship-reconstruct in or flagged; watchdog never
places/cancels orders; no rule/EMA/exit-threshold changes; PAPER_ONLY.
```

---

## For you, Harish — plainly
- **Part A kills the two silent "no trades all day" bugs you confirmed in the code.** Orphaned lock with
  no position → auto-released at startup. Missed intraday exit leaving a position stuck OPEN → swept and
  recovered so the lane frees. The desk can't look "alive" while quietly trading nothing anymore.
- **Part B is the end of "every day I update."** The app reconciles itself every tick; the moment a real
  setup qualifies and no order comes out with no honest reason, it tells YOU — cockpit, log, Telegram —
  with the exact cause. You stop being the detector.
- **Part D** (optional) brings **1H Flagship** into the same rebuild-from-candles story as Prototype and
  Reversal, so a mid-session open arms all three consistently. Your call whether to do it now or leave it
  flagged as a known gap.
- Changes **nothing** about your rules — same arm, same touch, same exits. It only stops them being
  silently skipped and makes the app honest when they are.
