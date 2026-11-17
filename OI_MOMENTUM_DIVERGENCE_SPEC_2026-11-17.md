# OI Momentum (Cr-per-minute) Divergence — signal spec for the Prototype filters

**17 Nov 2026 · branch `gemini/v2.0.24-proto-oi-filters` · owner: Harish**
**Supersedes the "direction" definition in `CURSOR_PROMPT_8600_OI_FILTERS_ON_PROTOTYPE_2026-11-17.md`
for the OI-writer reads. Keep ALL other plumbing from that prompt (switches, fail-open,
log-only / Block-on-paper-and-real, combine rule, UI, backtest parity, tests).**

---

## 1. The problem this fixes

The first version read the **absolute level** ("who has more OI now"). On a day where one side
dominates all session, that vetoes the *entire* opposite side for the whole day — including the
correct trades. Example: 17 Nov, puts were bigger all day, so a level filter blocked every PE,
yet the market fell ~150 pts from 12:30–13:10 where PE was right.

Fix: judge by **how fast Call OI and Put OI are changing per minute**, and by whether they
**disagree** (divergence/convergence), not by who is bigger.

## 2. The measurement (plain Cr/min)

Compute both sides as a **crore-per-minute rate** over a short rolling lookback `W` minutes
(config, default 10). From the recorded option-chain snapshots, each snapshot already gives
total Call OI and total Put OI (sum across strikes):

```
calls_crpm = (call_oi_now − call_oi_(now − W min)) / W      # Cr per minute
puts_crpm  = (put_oi_now  − put_oi_(now − W min)) / W        # Cr per minute
```

Use the SAME two numbers for both reads, from two different series:
- **total_oi**  → from Total OI (Call OI, Put OI).
- **fresh_oi / writer_flow** → from OI-Change-since-09:15 (Call OI Change, Put OI Change).
  (Its per-minute rate equals the Total-OI rate; compute from whichever series the read owns so
  the cockpit numbers match the chart the user is looking at.)

## 3. Direction = divergence / convergence (exactly the owner's rule)

Dead-zone threshold `t` in Cr/min (config, default 0.05) so a flat tape stays NEUTRAL.

```
if puts_crpm > +t and calls_crpm < −t:   direction = "CE"   # puts building, calls fleeing  → BUY (divergence)
elif puts_crpm < −t and calls_crpm > +t: direction = "PE"   # puts fleeing, calls building  → SELL (convergence)
else:
    net = puts_crpm − calls_crpm                              # general case when not a clean split
    if net > +t:   direction = "CE"
    elif net < −t: direction = "PE"
    else:          direction = "NEUTRAL"
detail = f"calls {calls_crpm:+.2f} Cr/min · puts {puts_crpm:+.2f} Cr/min → {direction}"
```

The clean opposite-direction case (one rising, one falling) is the strong signal. The `net`
fallback covers "both rising but puts much faster" etc. NEUTRAL never blocks.

## 4. Worked example (owner's own figures, 17 Nov)

- Total OI puts: 41.45 Cr @ 12:35 → 36.0 Cr @ 13:07 → **puts ≈ −0.17 Cr/min (falling)**
- Calls rising over the same window → `calls_crpm > 0`, `puts_crpm < 0` → **direction = PE (convergence)**
- Result: a PE touch in 12:30–13:10 is **allowed** (filter agrees), catching the ~150-pt fall.
- Morning mirror: puts rising + calls falling → **CE**, PE sits out. Grey future-price line
  tracks both — the correlation is why this works.

## 5. What changes in code (nothing else moves)

- **`analyst/chain_reads_8510.py`**: add a momentum computation that, from the snapshot series,
  produces `calls_crpm`, `puts_crpm`, and the divergence `direction` above. Feed this `direction`
  into the existing `total_oi`, `fresh_oi`, and `writer_flow` reads (replace their level-based
  direction). Keep `price_oi` and `volume_oi` as they are.
- The board, the switches, `oi_switch_block`, fail-open, log-only/Block, UI toggles, and the
  backtest path are **unchanged** — they already consume whatever `direction` the read returns.
- **Config** (via the existing `scripts/patch_add_oi_filters_8510.py`, extend it): add under
  `scanner.ema8_prototype.oi_filters`:
  ```
  "momentum": { "window_min": 10, "deadzone_cr_per_min": 0.05 }
  ```
  Both tunable without a code change. If `W` minutes of history is not yet available (early in
  the session, or 8600/recording just started) the read returns `None` → **fails OPEN** (no block),
  same as every other stale read.

## 6. Cockpit

On each OI-filter toggle row, show the two live numbers and the verdict, e.g.
`Total OI: calls +0.31 · puts −0.17 Cr/min → PE`. Update on the Prototype banner cadence.
Keep the "would block / not blocked" line from the base spec.

## 7. Guardrails (unchanged)

- All switches default OFF; momentum is only computed for the reads whose switch is ON (plus
  shown in log-only). Block still acts on **paper AND real**. Fail-OPEN on missing history.
- Faster signals whipsaw in chop — that is what the dead-zone and the **log-only week + points-
  saved-vs-points-cost test** (`OI_Filter_Recommended_Config_2026-11-17.md`, §6) are for. Judge
  in points on held-out days before trusting it live.
- Do not touch PROTECTED MODULES. `scripts/verify_locks.py` must stay PASS 11/11. New unit tests
  for the momentum direction (divergence CE, convergence PE, both-rising net case, dead-zone
  NEUTRAL, insufficient-history None). Full `pytest analyst` diffed against the handover baseline.

## 8. Not in scope (separate issue)

The 13:45-armed / 14:15-entry trade that made +25 then reversed to a loss is a **target/exit**
problem (+40 not hit, gave it back), not an OI-filter problem. Handle with a partial-profit or
trailing rule in a separate change — do not fold it in here.
