# OI Momentum v2 + oi_3m — Handover Pack

**17 Nov 2026 · owner: Harish · one entry point for the whole OI-momentum design.**
Hand this to AGY / Gemini / future-you instead of re-pasting the three docs. It names the
three source docs and inlines the 13:08 validation table (the proof the design is right).

---

## The three documents (read in this order)

1. **`OI_Momentum_Spec_v2_Normalized_2026-11-17.md`** — the maths, LOCKED.
   `S = dD/T` (Option A, over a fixed window found by timestamp, ±2 min band, NO /window_min),
   thresholds 0.15 / 0.30, all OI in lakhs via `_to_lakhs`, B is display-only (never gates),
   optional CumD confirm. Fail-OPEN on stale/insufficient. **Implemented & verified** on
   `gemini/v2.0.24-proto-oi-filters @ b2b23ec` (829/34/42, locks 11/11).

2. **`Cursor_Prompt_OI3m_Separate_Feature_2026-11-17.md`** — the oi_3m gate + 8600 context panel,
   as a SEPARATE feature (own files `oi_3m_gate_8510.py` / `oi_context_panel_8510.py`, own config
   `scanner.oi_3m_feature`, own branch `gemini/v2.0.26`). Reuses the v2 helper by import. oi_3m is
   a STRICTER confirm (3-min window, fires ~1 min AFTER the 10-min, NEUTRAL on drift) — not an
   early-warning. Context panel is display-only and can never gate. Default all-OFF / log_only.

3. **`OI_Filter_Recommended_Config_2026-11-17.md`** — how to run it: start `fresh_oi` + `total_oi`
   ON, Rule all_agree, Neutral OFF, **log-only a week → points-saved-vs-points-cost on held-out
   days → only then Block.**

## The rule that matters most

**Gate on S (momentum) and optionally CumD. NEVER gate on B (level).** On 17 Nov puts were bigger
all day so B stayed positive all day — gating on B would have blocked the correct PE at 13:08.
Level reads (pcr_zone, walls, pcr_futures) are display-only for the same reason.

## 13:08 validation (real 1-min data, 10-min window) — the proof

| Time | C (L) | P (L) | D=P−C | D 10m ago | dD | T | S=dD/T | Direction |
|---|---|---|---|---|---|---|---|---|
| 13:00 | 780 | 1870 | 1090 | 1170 | −80 | 2650 | −0.030 | NEUTRAL |
| 13:04 | 750 | 1760 | 1010 | 1110 | −100 | 2510 | −0.040 | NEUTRAL |
| 13:06 | 790 | 1630 | 840 | 1100 | −260 | 2420 | −0.107 | NEUTRAL |
| 13:07 | 810 | 1610 | 800 | 1110 | −310 | 2420 | −0.128 | NEUTRAL — close |
| **13:08** | 840 | 1560 | 720 | 1090 | −370 | 2400 | **−0.154** | **PE STRONG — first cross** |
| 13:10 | 870 | 1260 | 390 | 1090 | −700 | 2130 | −0.329 | PE EXTREME |
| 13:12 | 930 | 1160 | 230 | 1090 | −860 | 2090 | −0.411 | PE EXTREME — peak |
| 13:19 | 1030 | 1250 | 220 | 500 | −280 | 2280 | −0.123 | NEUTRAL — exit |

Read: 10-min **S fires PE STRONG at 13:08** (Nifty 25411), EXTREME at 13:10 (price → 25382 low),
clean NEUTRAL **exit at 13:19** (price 25410, move over). The flush was **put-writers unwinding**
(P 1870→1260 = −610L) far more than call-writing (C +90L) — S captures both sides in one number.
**B was +0.30 → +0.18 → +0.11 the whole time (always positive)** → level-gating would have blocked
the trade; momentum-gating allowed it. This is why v2 keeps B display-only.

### oi_3m (3-min window) on the same flush — why it's a *confirm*, not a lead
- 13:08: prev=13:05, dD=−200, T=2400 → S=−0.083 → **NEUTRAL** (10-min was already STRONG here)
- 13:09: prev=13:06, dD=−340, T=2200 → S=−0.155 → **PE STRONG**
- 13:10: prev=13:07, dD=−410, T=2130 → S=−0.192 → **PE STRONG**

oi_3m confirms ~1 min later and stays quiet on slow drift — a stricter second vote, not an early
signal.

## Branch map

| Branch | Carries | State |
|---|---|---|
| `gemini/v2.0.24-proto-oi-filters` | OI filters + **OI Momentum v2** | verified, pushed, default-OFF |
| `gemini/v2.0.25-proto-candle-trail` | v2.0.24 + **candle trail after +40** | built, default-OFF |
| `gemini/v2.0.26-oi3m-separate-feature` | **oi_3m gate + 8600 context panel** | prompt ready (not built yet) |

Specs live on `claude/v2.0.23-proto-glitches` + the Trading project. Merge via `integrate/v1.9`,
only when Harish says so.
