# OI Momentum v2 — normalized Bias/Significance (refines the Cr/min spec)

**17 Nov 2026 · branch `gemini/v2.0.24-proto-oi-filters` · owner: Harish**
**Refines `OI_Momentum_Divergence_Spec_2026-11-17.md`. Same feature, same plumbing — only the
direction maths is upgraded from raw Cr/min to a total-normalized Significance score. All other
behaviour (switches, fail-open, log-only / Block-on-paper-and-real, UI, backtest parity, tests)
is UNCHANGED.**

---

## 1. Why change the Cr/min version

The Cr/min direction worked but its threshold was absolute (Cr per minute), so it drifted with
the day's volume and needed hand-tuning. Normalizing the momentum by total OI makes the threshold
**scale-free** — the same number means the same thing whether total OI is 5 L or 50 Cr.

## 2. Variables (per read, from the recorded chain snapshot series)

At snapshot t, with C = total Call-writer OI, P = total Put-writer OI (lakhs or Cr, consistent):

```
T(t)  = P + C                      # total pressure
D(t)  = P − C                      # net bias (level)
B(t)  = D / T                      # normalized LEVEL bias, in [−1, +1]   (CONTEXT ONLY — see §4)
dD    = D(t) − D(t−Δ)              # change in net bias over the window
S(t)  = dD / T                     # normalized SIGNIFICANCE (momentum)   <-- the gating signal
CumD  = D(t) − D(session_open)     # integral of net bias since 09:15     (optional confirm)
```

Normalize the window to per-minute so the threshold is cadence-independent: if snapshots are
Δ minutes apart, use `S = (dD / Δ) / T` (or compute dD over a fixed `window_min`, default 10,
then divide by window_min). Keep it consistent between live and backtest.

## 3. Direction = sign of S (the momentum), with thresholds

```
if S >= +sig_extreme:   direction = "CE", strength = "EXTREME"   # extreme bullish momentum
elif S >= +sig_min:     direction = "CE", strength = "STRONG"
elif S <= −sig_extreme: direction = "PE", strength = "EXTREME"
elif S <= −sig_min:     direction = "PE", strength = "STRONG"
else:                   direction = "NEUTRAL"                     # |S| < sig_min = noise, never blocks
detail = f"S {S:+.2f} (C {C:.0f} P {P:.0f}) → {direction} {strength}"
```
Defaults (config): `sig_min = 0.15`, `sig_extreme = 0.30`. These replace the Cr/min dead-zone.

This is still the divergence rule in normalized form: dP>0,dC<0 drives S positive (CE);
dP<0,dC>0 drives S negative (PE); both same-sign tends toward 0 (NEUTRAL).

## 4. B (level) is CONTEXT ONLY — do not gate on it

**Critical.** B is the standing level. On a day where one side dominates all session (e.g. 17 Nov,
puts bigger all day), B stays one sign the whole day and would block the entire opposite side —
the exact crudeness this whole feature removed. So:
- **Gate on S** (and optionally CumD). Never let B veto a trade.
- Show B on the cockpit as a context number (e.g. "bias +0.42") but it must NOT enter
  oi_switch_block's agree/oppose decision.
- Do NOT port the spreadsheet's `IF(S..., IF(B>0.2,"BULL"...))` level-fallback into the gate.
  When S is in the NEUTRAL band, the read is NEUTRAL (no block), full stop.

## 5. Optional CumD confirm (second switch, default off)

Add an optional sub-switch `require_cumd_confirm` (default false): when on, a CE signal needs
S bullish AND CumD > 0 (day has net put-writing); PE needs S bearish AND CumD < 0. This raises
conviction on trend days without using the raw level B. Default off so it changes nothing.

## 6. Config (extend the existing momentum block; via the patch script, back up first)

```
"momentum": {
   "window_min": 10,
   "sig_min": 0.15,
   "sig_extreme": 0.30,
   "require_cumd_confirm": false
}
```
Remove/retire `deadzone_cr_per_min` (superseded by sig_min on the normalized S), or keep it
ignored for back-compat. If window history is insufficient → direction None → fail OPEN.

## 7. What changes in code

- `analyst/chain_reads_8510.py`: in the momentum computation already added, replace the raw
  `calls_crpm/puts_crpm` direction with S = (dD/window)/T and the §3 thresholds; also expose
  B and CumD in the read's `facts`/detail for display. Keep feeding `direction` to total_oi,
  fresh_oi, writer_flow. price_oi/volume_oi untouched.
- Everything else (board override, oi_switch_block, fail-open, log-only/Block, backtest,
  toggles) is unchanged — they consume `direction` as before.
- Cockpit: show `S +0.21 · bias +0.42 · CumD +6` per writer read; the gate uses S only.

## 8. Worked check (owner's figures)

- 09:15 C=10 P=10 → T=20, D=0, B=0, S=0 → NEUTRAL.
- 09:20 C=15 P=5  → T=20, D=−10, dD=−10, S=−0.50 → **PE EXTREME** (correct; Total unchanged but
  momentum extreme bearish).
- 09:21→09:22 reversal: S swings to +0.23 → **CE STRONG** — early long signal, as in the tracker.
- 17 Nov 12:30–13:10: puts falling + calls rising → dD negative → S negative → **PE allowed**,
  catching the 150-pt drop; B stayed positive all day but is display-only, so it does not block.

## 9. Guardrails (unchanged)

Default all switches OFF; Block acts on paper AND real; fail-OPEN on stale/insufficient history;
PROTECTED MODULES untouched; verify_locks 11/11; new unit tests for S thresholds, NEUTRAL band,
B-not-gating, CumD confirm on/off, insufficient-history None; full pytest diffed against the
handover baseline. Judge live value by the log-only week + points-saved-vs-points-cost test —
cleaner maths is not automatically more points.
