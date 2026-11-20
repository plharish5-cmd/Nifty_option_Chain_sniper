# Cursor Work Order — v2.0.44: Prototype "+40 then trail" exit (stop capping trend days)

> OWNER DECISION (20 Nov): the 8 EMA Prototype currently exits at a HARD +40 spot target (target_pts
> 40.0, NO trail) — confirmed in ema8_prototype_8510.py. On a trending day it banks +40 and gives the
> rest away (today: ~+88 move, exited +40). Owner wants: keep +40 as the TRIGGER, then TRAIL to ride the
> trend. Implement as a reversible toggle, default ON per owner's choice. Branch gemini/v2.0.44-proto-
> trail off the latest consolidated tip (gemini/v2.0.41-arm-reconstruct, or rebase onto ChatGPT's
> pricing-fix branch once it lands — see COORDINATION). Tags pre-/post-. Push. Do NOT merge. No protected
> modules. verify_locks 11/11. This DOES change real-money exit behaviour — it is the owner's explicit
> request; implement exactly as specced, toggle default ON, reversible.

COORDINATION (do not clobber): ChatGPT is fixing the PAPER PRICING path (fresh per-contract option quote
for entry ask / exit bid; pending fill if no fresh quote). That is the EXIT *PRICE*. This job is the EXIT
*DECISION* (when to close on spot). They compose: the trail decides the moment to exit; the fill uses
ChatGPT's fresh option quote. Do NOT re-touch the paper-pricing/fill code — only the exit-decision logic.
If both branches touch ema8_prototype_8510.py exits, rebase this onto ChatGPT's branch and keep its
pricing changes intact.

```
NEW EXIT MODEL (Prototype only — Reversal/Flagship unchanged here)
Config (DEFAULT_CFG, new keys; default ON per owner):
  "trail_after_target_enabled": True,     # master toggle; False = exact current fixed +40 behaviour
  "trail_trigger_pts": 40.0,              # reuse target_pts value; the move that ARMS the trail
  "trail_move_to_breakeven": True,        # on trigger, move the spot-stop to entry (lock no-loss)
  "trail_ref": "prior_candle",            # "prior_candle" (prior CLOSED 15m low/high) or "ema8" (dynamic 8 EMA)
  "trail_exit_on": "close",               # exit when spot CLOSES through the trail (not a wick)

LONG trade (CE):
  1. From entry until spot reaches entry + trail_trigger_pts: behaviour UNCHANGED — hard stop at
     entry − sl_pts (−40). (If −40 hits first, SPOT_SL_HIT exactly as today.)
  2. When spot reaches entry + trail_trigger_pts (the OLD exit point): DO NOT EXIT. Instead:
       - move the spot-stop up to entry (breakeven) if trail_move_to_breakeven,
       - begin trailing: the stop = the prior CLOSED 15m candle LOW (trail_ref=prior_candle) or the
         dynamic 8 EMA (trail_ref=ema8), whichever is chosen; the stop only ever RATCHETS UP, never down.
  3. EXIT when spot CLOSES below the current trail stop (reason SPOT_TRAIL_EXIT), OR an opposite 15m
     close fires (OPPOSITE_8EMA_CLOSE), OR 15:20 square-off — whichever first. The fixed +40 no longer
     forces the exit.
SHORT trade (PE): mirror — trigger at entry − 40, stop ratchets DOWN to prior candle HIGH / 8 EMA, exit
  on a close ABOVE the trail.

PRESERVE EXACTLY (do not change):
  - The ENTRY rules (arm → away ≥2 → touch ±3), EMA maths, touch cadence.
  - sl_pts −40 as the INITIAL stop before the trigger; one Prototype trade at a time; reentry rules
    (profit re-arms same side, stop-loss cancels side until opposite close); no_entry windows.
  - Hard exits OPPOSITE_8EMA_CLOSE and 15:20 stay as overriding exits.
  - When trail_after_target_enabled = False, behaviour is byte-identical to today's fixed +40.
  - Reuse the existing Reversal candle-trail helper if one exists (don't reimplement trailing); the
    Prototype's trail must ratchet monotonically and use CLOSED candles only (never the forming bar as
    the stop reference — forming-bar low/high can repaint).
  - EXIT FILL PRICE comes from ChatGPT's fresh-quote path, not a stale LTP. This job only decides the
    exit MOMENT; it must call the same fill path (pending if no fresh quote).

GUARDRAILS
- Prototype only. No protected modules. verify_locks 11/11. Config via patch (the 5 keys above);
  preserve M terminal8510_config.json. Real-money exit behaviour changes by owner's explicit decision;
  keep the toggle so it's reversible in one flip.
- Do NOT touch the paper-pricing/fill code (ChatGPT's lane). Do NOT change Reversal or Flagship exits.

TESTS (analyst/test_proto_trail_8510.py)
- Trending LONG: entry, spot runs +88; with trail ON, exit is SPOT_TRAIL_EXIT on the close through the
  prior-candle-low trail ABOVE entry+40 (captures more than +40), not SPOT_TARGET_HIT at +40.
- trail_after_target_enabled = False → identical to current: SPOT_TARGET_HIT at exactly +40.
- Trigger reached then immediate reversal → exit at breakeven (not a loss) when trail_move_to_breakeven.
- Stop ratchets monotonically (never loosens); uses CLOSED candles only.
- Opposite 15m close and 15:20 still override the trail.
- SHORT mirror of the trending case.
- verify_locks 11/11; full pytest diffed vs baseline — no NEW failures.

DELIVERABLE
Commit on gemini/v2.0.44-proto-trail, tags, push, AGENT_STATUS (note rebase status vs ChatGPT's pricing
branch). Paste back a before/after on today's trending CE: fixed +40 banked vs trail-captured (spot
points). State plainly the trail is a toggle, default ON, reversible. Do not merge.

CONFIRM before coding: +40 becomes the TRIGGER not the exit; on trigger move stop to breakeven and trail
by prior CLOSED candle low/high (or 8 EMA), ratchet-only, exit on CLOSE through trail; −40 initial stop,
opposite-close and 15:20 unchanged; toggle default ON and OFF==today's fixed +40; exit FILL uses
ChatGPT's fresh-quote path (don't touch pricing); Prototype only; no entry-rule/EMA changes.
```

---

## For you, Harish — plainly
- **+40 stops being the exit and becomes the "now start trailing" trigger.** Below +40, nothing changes
  (−40 stop as today). The moment you're +40 up, the stop jumps to breakeven (you can't lose from there)
  and then trails the prior candle low — so on a day like today you'd have ridden most of that +88
  instead of banking +40 and watching it run.
- **It's a toggle, default ON.** One flip returns to the old fixed +40 if you ever want it. Your money,
  your switch.
- **This and ChatGPT's pricing fix work together:** this decides *when* to get out (spot trend break);
  ChatGPT's fix makes the exit book the *real* fresh option price instead of the stale ₹215.55 that gave
  you the fake −90. Both landing = correct exits at correct prices.
- **Order of work:** let ChatGPT finish the pricing fix first (it's the active bug), then this trail
  rebased on top. I flagged the coordination in the prompt so they don't collide.
