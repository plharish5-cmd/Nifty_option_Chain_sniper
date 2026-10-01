# Open Research Questions & Empirical Tracking

## 1. Extreme Volume Zone ($\ge 1.50\times$) Revisit
- **Observation:** In the 23-session near-month active futures dataset (`2026-08-24` to `2026-09-24`), cumulative RVOL $\ge 1.50\times$ produced:
  - 78 trades (filtered 129 trades)
  - Win rate: 47.4%
  - Expectancy: **+2.05 pts/trade**
  - Profit Factor: **1.21**
  - Total PnL: **+160.2 pts**
  - Max Drawdown: **-183.3 pts**
- **Assessment:** Promising momentum expansion edge, but sample size is thin (78 trades across 23 days, ~3.4 trades/day).
- **Status:** **HOLD**. Do not alter live `trend_zone` or `extreme_zone` thresholds yet. Revisit empirical distribution and stability once 50 near-month sessions are accumulated.

---

## 2. Daily Heikin-Ashi (HA) Filter Holdout Audit
- **In-Sample Result (Aug 24 – Sep 24, 23 sessions):**
  - Combining RVOL $\ge 0.85$ with Daily HA yielded +1.37 pts expectancy (PF 1.14, Max DD -166 pts).
- **Out-of-Sample Holdout Result (Jun 08 – Aug 21, 54 sessions, 342 trades):**
  - Base (No Filter): -0.54 pts expectancy (36.5% win rate, Max DD -558.1 pts)
  - RVOL $\ge 0.85$ alone: **+0.44 pts expectancy** (38.7% win rate, PF 1.05, +135.0 pts PnL, Max DD -386.5 pts)
  - HA alone: -0.54 pts expectancy (34.3% win rate, no edge over Base)
  - HA + RVOL $\ge 0.85$: +0.36 pts expectancy (36.3% win rate, lower expectancy than 0.85 alone, though Max DD drops to -166.1 pts)
- **Conclusion:** RVOL $\ge 0.85$ successfully survived out-of-sample (+0.44 pts). Daily HA did not produce independent out-of-sample alpha and failed to beat RVOL alone.
- **Status:** Daily Heikin-Ashi is retained strictly as a **display indicator**, not as an execution gate (Box 7 on hold).

---

## 3. 18-Sep 13:20 CE Reference Trade Audit (L3 vs L5)
- **Honest Statement:** "18-Sep 13:20 CE trade (the system's reference trade) has futures RVOL 0.34 — DEAD ZONE under the 0.85 threshold. The trade worked on OI structure (L5), not on institutional futures participation (L3). The RVOL gate correctly blocks it. The reference trade is not in the passing set."

