# Cursor Prompt — 8 EMA Prototype: candle-trail after +40 (ride the trend)

> Paste the box into Cursor (Gemini). Self-contained, names real functions in this repo.
> This is an ADDENDUM to the OI-filter work on `gemini/v2.0.24-proto-oi-filters`. It is a
> separate feature — do not entangle it with the OI filters. Read `EMA8_PROTOTYPE_DESIGN_
> 2026-11-13.md` and `HANDOVER_8510_2026-11-13.md` first.

---

```
ROLE
Edit the 8510 Algo Terminal in this repo. Work on branch gemini/v2.0.25-proto-candle-trail
cut from gemini/v2.0.24-proto-oi-filters (so it carries the OI-filter work). Do not commit
to integrate/v1.9 or main. Tags pre-/post-gemini-v2.0.25. Co-Authored-By footer as usual.

GOAL (owner's mentor rule — exactly this)
Give the 8 EMA Prototype an OPTIONAL "ride the trend" exit so a winner is not capped at +40.
Rule:
  - Long  (CE): AFTER the trade reaches +40 points profit, stop trailing begins. From then on,
    each time a candle CLOSES GREEN (close > open), move the stop UP to that candle's LOW.
    Red candles do NOT move the stop. Exit when price breaks the trailed stop.
  - Short (PE): AFTER +40, each time a candle CLOSES RED (close < open), move the stop DOWN to
    that candle's HIGH. Green candles do not move it. Exit when price breaks the trailed stop.
The stop only ever ratchets in favour, never against (monotonic).

THIS ALREADY EXISTS — REUSE IT
analyst/terminal8510_backend.py has flagship_candle_trail_update(is_long, stop, o, h, l, c):
    long + green  -> max(stop, low)
    short + red   -> min(stop, high)
    else          -> stop
That is precisely the mentor's rule. REUSE this function for the Prototype; do not write a new
trail formula. (If a helper rename makes sense, keep flagship behaviour byte-for-byte unchanged.)

DEFAULT = OFF (no behaviour change unless turned on)
When the new toggle is OFF, the Prototype behaves EXACTLY as today: exit at +40 spot OR +40
option, whichever first, plus opposite 15m close, plus 15:20 square-off. All existing tests must
still pass unchanged.

CONFIG (add under scanner.ema8_prototype; change config ONLY via the existing patch script,
extend scripts/patch_add_oi_filters_8510.py or add scripts/patch_add_candle_trail_8510.py,
back up config first, print a diff):
  "candle_trail": {
     "enabled": false,         # opt-in
     "activate_pts": 40.0,     # profit (spot pts) at which trailing arms = the mentor's "after 40"
     "candle_tf_min": 15,      # which candle to trail; 15 matches the strategy. allow 5 or 3.
     "use_completed_candles": true   # only a CLOSED candle moves the stop (no look-ahead)
  }
Keep every other Prototype key/default unchanged.

BEHAVIOUR WHEN enabled = true
1. From entry until profit reaches activate_pts (40): UNCHANGED — the fixed -40 spot/-40 option
   stop and opposite-close and 15:20 still apply. Do NOT trail yet.
2. The moment max profit >= activate_pts: ARM the trail. From now on:
   - The +40 TARGET exits are DISARMED (both +40 spot and +40 option) — that is the whole point,
     let it run. Keep a disaster floor: the stop starts at the trailed level and may never sit
     worse than the original stop; use flagship_candle_trail_update to ratchet it on each newly
     CLOSED candle_tf_min candle (green-low for long, red-high for short).
   - Exit when live spot breaks the trailed stop (long: spot <= stop; short: spot >= stop).
   - Opposite 15m 8 EMA close and 15:20 square-off STILL exit (unchanged).
3. Monotonic: the trailed stop never moves against the position.

WHERE TO WIRE
- analyst/ema8_prototype_8510.py / the Prototype evaluation: add the armed-trail state to
  proto_state (armed flag, trailed_stop, last_trail_candle_ts). Compute activation from the same
  max-profit the +40 target uses. When candle_trail.enabled, route the exit decision through the
  candle-trail path instead of the fixed +40 target; keep opposite-close and 15:20.
- backtest_ema8_prototype_at_bar: apply the SAME logic on the completed candle_tf_min candles
  (use_completed_candles=true, same no-look-ahead rule already used there). Report in spot points.
  Add trail_steps entries like the existing ones (e.g. "TRAIL armed +40", "trail -> low 25190",
  "trail hit 25182") so replay shows how far it rode.
- terminal8510_backend.py: note uses_fixed_spot_or_option_exit() returns True for EMA8_PROTOTYPE.
  When candle_trail.enabled, the Prototype is NO LONGER a pure fixed-exit lane — branch so the
  trail path is used, WITHOUT changing Stage_2/Stage_3/Reversal/Ticket behaviour.
- LIVE (real money): once armed, push the trailed stop to the broker protective order using the
  SAME modify path the existing Super Trail uses (compute_trailing_sl / the SL-modify plumbing):
  confirm the old stop is out before placing the new one, sell only when live_filled=1 — reuse
  the v2.0.11 close-path safety, do not invent a new one. Paper: move the paper stop the same way.

UI (Algorithmic_Trading_Workspace_8510.html, Prototype card)
- One toggle "Ride the trend — trail green/red candle after +40" + an activate_pts field + a
  candle timeframe selector (15 / 5 / 3 min). Persist to scanner.ema8_prototype.candle_trail via
  the existing save path. Use the v2.0.23 radio pattern (if (!(key in cfg)) return;).
- When a trade is armed, show the live trailed stop and the current locked-in points, e.g.
  "Trailing: stop 25,190 · locked +48". When not armed, show "Trailing after +40 (off/idle)".

GUARDRAILS
- Default OFF. No other gate/limit added. Do NOT touch PROTECTED MODULES; verify_locks.py must
  print PASS 11/11. Config via patch script only; preserve M analyst/terminal8510_config.json.
- TESTS (analyst/test_proto_candle_trail_8510.py): long rides multiple green candles then exits on
  a green-low break; short mirror; red candle does not move a long stop; stop is monotonic;
  activation only at >= activate_pts (no trailing before +40); enabled=false reproduces today's
  +40 exit byte-for-byte; backtest path matches the live rule on the same candles.
- Run scripts/verify_locks.py and PYTHONPATH=. pytest analyst -q --continue-on-collection-errors;
  diff failures against HANDOVER §6 (~826 now on this chain; phase5_eod/phase7 wall-clock after
  15:20 IST are not yours). Browser-check the Prototype card (toggle persists, armed line shows,
  0 console errors).

DELIVERABLE
Commit on gemini/v2.0.25-proto-candle-trail, tags, push, append a dated block to AGENT_STATUS.md.
Give me the file list + run commands. State what you could not verify (no live broker/market
data in sandbox). Do not merge.

CONFIRM before coding: default OFF; arm only at >= activate_pts; green-low for long / red-high
for short via flagship_candle_trail_update; +40 targets disarmed only once armed; opposite-close
and 15:20 still exit.
```

---

## Notes for you (not part of the prompt)
- **What it changes:** today a Prototype winner stops at +40. With this ON, once the trade is
  +40 up it stops *targeting* and starts *trailing the candle* — so a trade that would have
  booked +40 can ride to +70, +90, whatever the trend gives, and only exits when a green candle's
  low (long) / red candle's high (short) is broken.
- **The trade-off, honestly:** a 15-minute candle is wide. The trail gives back from the peak to
  the last green candle's low before it exits — that can be 15–30 pts of give-back on a 15m
  candle. You're trading "guaranteed +40" for "sometimes +80, sometimes +40 minus some give-back."
  That is exactly why `candle_tf_min` is configurable: 5-min candles trail tighter (less
  give-back, more early exits), 15-min trail looser (rides longer, bigger give-back). Backtest
  both on held-out days and compare total points before committing live.
- **It stacks with the OI filters** — different feature, different branch cut from the OI branch,
  so you get both. Entry still gated by OI momentum (if you turn those on); exit now rides.
- **Default OFF** means nothing changes until you flip it, same safety pattern as the OI work.
```
