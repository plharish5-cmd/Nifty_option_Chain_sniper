# AGY Prompt — 8 EMA Reversal revamp to Prototype-grade (5m/15m, trail, OI filters, real money)

> For AGY. Comprehensive revamp of the existing 8 EMA Reversal into a real-money-capable strategy
> built to the same standard as the 8 EMA Prototype. Read first:
> `EMA8_Prototype_Design_RealMoney_Paper_Backtest_2026-11-13.md`,
> `OI_Momentum_v2_And_OI3m_Handover_Pack_2026-11-17.md`, `HANDOVER_8510_2026-11-13.md`.
> Branch `agy/v2.0.32-reversal-revamp` off the current tip. Tags pre-/post-. Do NOT merge.
> Do NOT touch PROTECTED MODULES (8600). verify_locks 11/11.

---

```
ROLE / CONTEXT
The 8 EMA Reversal lives in analyst/mj_sir_stage_reversal_8510.py (shared with Stage 2 / Stage 3).
Today it is 15m-only: 4/8 EMA cross -> wait 2 bars -> touch within 2 bars of the DYNAMIC 8 EMA
(dynamic_8ema(b15, live_spot), dynamic_touch_tol_pts 3.0) -> enter; exit ±20. It ALREADY uses the
same dynamic 8 EMA engine as the Prototype (ema4_ema8, forming-bar close = live spot). Owner will
run this on REAL money, so it must reach Prototype-grade: real execution plumbing, square-off,
off-hours guard, OI filters, candle trail, backtest, cockpit — all default-OFF for the new parts.

ABSOLUTE PRESERVATION RULES
- Do NOT change Stage 2 / Stage 3 behaviour (same file). Their logic, config and tests stay
  byte-for-byte. Scope every change to the REVERSAL lane only.
- The existing 15m Reversal default behaviour must be unchanged when the new options are OFF.
- Reuse, do not duplicate: the dynamic EMA (ema4_ema8 / dynamic_8ema), the OI momentum filter
  (import from the oi_filters / momentum modules), and the candle trail (flagship_candle_trail_update
  in terminal8510_backend.py). Import them; never re-derive the maths.

=== PART 1 — timeframe toggle (5m / 15m) + "5m pre-arms 15m" mode ===
- New config scanner.reversal.timeframe_mode: "15m" (default) | "5m" | "both".
  - "15m": current behaviour on 15m bars.
  - "5m": the same cross->wait->touch logic on 5m bars (see Part 2 for 5m EMA).
  - "both": run 5m and 15m lanes together, but ONE TRADE AT A TIME (Part 6 traffic control).
- Owner's note: 5m = earlier entry but noisier; 15m = later but cleaner. The toggle lets him pick
  per day.
- NEW optional mode scanner.reversal.pre_arm_15m_from_5m (default false): when timeframe_mode is
  "15m" and this is ON, a 5m reversal signal PRE-ARMS the 15m lane (sets it to "armed, waiting")
  earlier than the 15m cross would — but the ENTRY still fires on the 15m rules (15m touch within
  window on the 15m dynamic 8 EMA). So 5m leads, 15m confirms and enters. This gives the 15m-quality
  entry, primed earlier, without taking raw 5m noise. Clearly label this state on the cockpit
  ("15m armed early via 5m lead").

=== PART 2 — dynamic 8 EMA on 5m (same method as Prototype/15m) ===
- Add a 5m resample anchored at 09:15 IST (mirror resample_nse_15m -> resample_nse_5m in
  nse_spot_candles_8510.py; 300s buckets). Compute the dynamic 8 EMA on the 5m forming bar exactly
  like 15m: forming bar close = live spot, recompute via ema4_ema8, every 3s live / 30s backtest grid.
- No new EMA formula, no rounding. Same ema_tv (adjust=False, alpha 2/9 for the 8). dynamic_touch_tol
  stays 3.0. The 5m 8 EMA must match how the Prototype/15m computes it, just on 5m bars.

=== PART 3 — EMA separation as an option ===
- New config scanner.reversal.ema_separation_enabled (default false; RECOMMEND ON for 5m).
  When ON, only take a Reversal entry when |4EMA - 8EMA| >= scanner.reversal.ema_separation_min_pts
  (default e.g. 5.0) — i.e. the trend is clearly established. This cuts most 5m whipsaw.
- Reuse the existing separation/ribbon logic in the file; expose it as a Reversal toggle without
  changing Stage behaviour.

=== PART 4 — exit: ±20 then candle-low/high trail ===
- Keep reversal_target_pts / reversal_sl_pts = 20 as the base.
- NEW: scanner.reversal.candle_trail_enabled (default false). When ON: once the trade reaches +20
  (activate_pts, default 20), stop the fixed target and TRAIL using flagship_candle_trail_update —
  long: raise stop to each closed candle's LOW; short: lower stop to each closed candle's HIGH;
  monotonic. Trail on the ACTIVE timeframe's candles (5m candles in 5m mode, 15m in 15m / pre-arm
  mode). The -20 stop and opposite-cross exit and 15:20 square-off still apply until the trail arms.
  Reuse the Prototype candle-trail logic exactly (flagship_candle_trail_update); do not write new trail maths.

=== PART 5 — 8600 OI filters on top (reuse, default OFF) ===
- Layer the SAME OI momentum filters built for the Prototype onto the Reversal entry: import
  oi_switch_block / the momentum direction (normalized S, B display-only, fail-OPEN) and gate the
  Reversal entry the same way. New config scanner.reversal.oi_filters mirroring the Prototype's
  (switches default OFF, log_only, all_agree, momentum window 10 / sig 0.15/0.30). Do NOT duplicate
  the momentum maths — import it. Same rule: gate on S, never on level B; fail-OPEN when 8600 stale.

=== PART 6 — traffic control (one trade at a time across Reversal + Prototype) ===
- CRITICAL for real money: the Reversal and the Prototype must never hold conflicting positions.
  Implement a single shared "one live setup at a time" lock across BOTH lanes: whichever lane
  ARMS-AND-TOUCHES FIRST takes the trade; the other lane STANDS DOWN (no entry) while any position
  is open. When flat again, both are eligible. Within "both" timeframe mode, the 5m and 15m Reversal
  lanes share the same lock too (first-to-fire wins).
- Owner's intent: "if Reversal armed and executed, no need of Prototype." So on a tie/overlap,
  Reversal priority is acceptable, but the simplest correct rule is first-to-arm-and-touch wins —
  implement that, and make the "who holds the lock" visible on the cockpit.
- Must be impossible to open a second position while one is open (paper or real). Add a test that
  proves no double-fill across lanes.

=== PART 7 — real-money plumbing (same safety as Prototype) ===
- Route through the SAME execution path as the Prototype: execution_mode LIVE_ALLOWED/PAPER_ONLY,
  aggressive-limit chaser, companion protective SL, sell only when live_filled=1, companion stop
  confirmed out before SELL, resumable EXIT_PENDING recovery. Do not invent a new order path.
- 15:20 square-off applies (reuse check_eod_square_off / the v2.0.23 wiring).
- Off-hours / stale guard applies (reuse the v2.0.31 new_position_entry_allowed: no entry when
  market closed or spot stale; exits always allowed). Paper included.
- Contract selection: current-week CE/PE with premium >= ₹200, deeper-ITM walk, same as Prototype.

=== PART 8 — cockpit (Reversal card, Prototype-grade) ===
- Timeframe toggle (15m / 5m / both), pre_arm_15m_from_5m toggle, ema_separation toggle,
  candle_trail toggle, OI filter toggles (reuse the Prototype's filter UI pattern + present-key guard).
- Live EMA display: 8EMA (live) vs 8EMA (candle) for the active timeframe (reuse v2.0.30 display),
  plain-language step tracker (armed / away / waiting touch / in trade / trailing), and the shared-lock
  state ("Reversal holds the trade" / "standing down — Prototype in trade"). Stale guard shows EMA STALE.

=== PART 9 — backtest ===
- The Reversal (5m and 15m) must run in the Backtest Lab / proto_sweep harness: spot points,
  completed-candle no-look-ahead, held-out PASS/WEAK/FAIL. Add the Reversal factors (timeframe,
  ema_separation, candle_trail, OI filters) to the sweep so each is measured on unseen days before
  the owner turns anything on. Report ±20 vs ±20+trail so he can compare.

GUARDRAILS
- Everything new default-OFF; existing 15m Reversal unchanged when off. Stage 2/3 untouched.
- No protected modules. verify_locks 11/11. Config via a patch script (backup first); preserve
  M terminal8510_config.json. No remote model calls.
- TESTS (analyst/test_reversal_revamp_8510.py): 5m dynamic EMA matches the method; cross->wait->touch
  on 5m and 15m; ema_separation blocks when 4/8 too close; candle trail arms at +20 and rides
  (reuse helper); OI filter gates by S not B; traffic-control proves NO double position across
  Reversal+Prototype (both lanes, both timeframes); off-hours entry blocked; exits allowed off-hours;
  Stage 2/3 unchanged; 15m-default unchanged when new options off. verify_locks 11/11; full pytest
  diffed against the handover baseline.

DELIVERABLE
Commit on agy/v2.0.32-reversal-revamp, tags, push, AGENT_STATUS block. File list + run commands.
State what couldn't be verified (live Dhan/market). Do NOT merge — Harish merges.

CONFIRM before coding: Stage 2/3 untouched and existing 15m Reversal unchanged when options off;
reuse (not duplicate) dynamic EMA / OI momentum / candle trail; 5m/15m/both toggle + 5m-pre-arms-15m;
ema_separation option; ±20 then candle-low/high trail from +20 on the active timeframe; ONE trade at
a time across Reversal+Prototype (first-to-arm-and-touch wins); same real-money execution path,
15:20 square-off and off-hours guard; all new parts default-OFF; backtest on held-out before live.
```

---

## Notes for you (not the prompt)
- **The dynamic 8 EMA is already shared** — the Reversal uses the exact same engine as the Prototype,
  so "same 8 EMA as prototype" needs no new maths; the only new piece is running it on 5m bars.
- **The traffic-control lock is the real-money safety linchpin.** With both lanes live, the one
  thing that must be impossible is two open positions / conflicting CE+PE. The prompt makes AGY prove
  it with a test. Do not go live on "both" mode until that test is green and you've watched it on paper.
- **5m noise is real** — keep EMA separation ON for 5m, and lean on the "5m pre-arms 15m" mode when
  you want early timing with 15m quality.
- **Backtest before real money.** This is a new strategy variant you'll trade with real funds — run
  it through the sweep on held-out days (±20 vs ±20+trail, 5m vs 15m, filters on/off) and paper it
  first. Same rule as always: the held-out points decide, not the idea.
```
