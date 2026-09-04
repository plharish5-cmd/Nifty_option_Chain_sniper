# NIFTY 50 INSTITUTIONAL SNIPER & PRICE ACTION SYSTEM
## Comprehensive Master Trading Manual & Technical Architecture

---

## 1. Executive Summary & Core Philosophy

This system is an institutional-grade algorithmic and visual trading terminal designed specifically for **NIFTY 50 index options and futures**. It integrates two complementary trading disciplines:
1. **Derivatives Quantitative Mechanics:** NSE Option Chain Open Interest (OI) walls, Put-Call Ratio (PCR), Intraday Flow, GEX (Gamma Exposure), and Max Pain.
2. **Institutional Price Action & SMC:** 15-Minute Structural Maturity, The 8-EMA Follow-Through Law, The 2-Phase Session Gap Road Map, and Priyank Sharma's 1-Minute 3-Drive Liquidity Sweep Scalper.

### The Trader's Goal: The 20–40 Point Law
* **Daily Target:** 20 to 40 index points (representing 10 to 25 points in At-The-Money option premium).
* **The Reality:** 20 clean points per day in NIFTY 50 yields over 400+ points monthly. Consistency in 1 or 2 clean trades per day eliminates overtrading, emotional fatigue, and brokerage erosion.
* **The Golden Rule:** Once Target 1 (+20 pts) is achieved, immediately trail Stop-Loss to Cost (Breakeven). When Target 2 (+40 to +65 pts) is achieved, close the terminal and protect capital.

---

## 2. Technical Architecture & Multi-Timeframe Synthesis

```
┌──────────────────────────────────────────────────────────────────────────┐
│                           TIMEFRAME HIERARCHY                            │
├───────────────────┬───────────────────────────────┬──────────────────────┤
│ Timeframe         │ Primary Purpose               │ Key Indicators       │
├───────────────────┼───────────────────────────────┼──────────────────────┤
│ Daily / HTF       │ Master Levels, CPR, PDC/PDH   │ Pivot Points, CPR    │
│ 1-Hour / 30-Min   │ Mother Trend & Structural Bias│ Major Swing High/Low │
│ 15-Minute (Core)  │ Primary Execution & Direction │ 8 EMA, BB (20,2), DL │
│ 5-Minute          │ Early Rejection & Compression │ BB Width (>60 pts)   │
│ 1-Minute (Scalp)  │ 3-Drive Sweeps & MSS Entries  │ 14 RSI, Volume Spike │
└───────────────────┴───────────────────────────────┴──────────────────────┘
```

### Data Pipeline Architecture
* **Intraday OHLCV Candles:** Fetched via DhanHQ API v2 (with real NIFTY Futures volume for true session VWAP) or free fallback (yfinance).
* **Derivatives Feed:** Live NSE option chain polled every 3 minutes, calculating net delta, IV skew, quadrant migration, and call/put writer dominance.
* **Master Levels Engine:** Automatically tracks Previous Day Close (PDC), Previous Day High (PDH), Previous Day Low (PDL), Camarilla/Floor Pivots (R1–R4, S1–S4), and 100-Point Institutional Strike Barriers.

---

## 3. The 2-Phase Session Gap Road Map (Anti-Trap Architecture)

Retail traders lose the majority of their capital during the market opening (09:15 to 10:15 AM) due to **chasing gaps**. The system divides every gap session into two distinct chronological phases.

### A. The Gap-Up Protocol

```
GAP-UP SESSION ARCHITECTURE
09:15 AM Open (+35 pts or more above PDC)
   │
   ├──► PHASE 1: CHASE TRAP ZONE 🚫
   │    • Market Condition: Spot is extended near Day High.
   │    • Institutional Action: Institutional buyers take profit; market cools down.
   │    • Retail Trap: Amateurs buy Calls at the top out of FOMO.
   │    • System Directive: CALL BUYS ARE LOCKED. Scan for Top Reversals (PE Fade)
   │      or wait patiently for Phase 2.
   │
   └──► PHASE 2: WHOLESALE VALUE DIP-BUY ZONE 💎 (The Pro CE Trade!)
        • Market Condition: Spot pulls back 40 to 80+ points to test major support:
          - PDC (Gap Fill zone)
          - PDL (Previous Day Low)
          - S1 (Pivot Support)
          - 100-Pt Round Strike (e.g. 24,100 / 24,200)
        • Institutional Action: Smart money defends the bullish higher timeframe trend.
        • Entry Trigger: 15m Hammer, Double Hammer, or Bullish Engulfing closing above 8 EMA with Follow-Through.
        • Execution: BUY CE AT WHOLESALE SUPPORT.
        • Risk/Reward: Strict 10-15 pt SL below support; Target 1 = +20 pts; Target 2 = Day High / R1.
```

### B. The Gap-Down Protocol

```
GAP-DOWN SESSION ARCHITECTURE
09:15 AM Open (-35 pts or more below PDC)
   │
   ├──► PHASE 1: PANIC SELL TRAP 🚫
   │    • Market Condition: Spot is depressed near Day Low.
   │    • Institutional Action: Short covering begins; institutional absorption.
   │    • Retail Trap: Amateurs panic and buy Puts at the absolute bottom.
   │    • System Directive: PUT BUYS ARE LOCKED. Scan for Bottom Reversals (CE Bounce)
   │      or wait patiently for Phase 2.
   │
   └──► PHASE 2: HEAVY SUPPLY RESISTANCE RIP-FADE 🎯 (The Pro PE Trade!)
        • Market Condition: Short-covering rally pushes spot up 60 to 100+ points into tough resistance:
          - PDH (Previous Day High)
          - PDC (Gap Fill zone)
          - R1 (Pivot Resistance)
          - 100-Pt Round Barrier (e.g. 24,300)
        • Institutional Action: Institutional sellers defend supply zones to resume the downtrend.
        • Entry Trigger: 15m Inverted Hammer, Shooting Star, or Bearish Engulfing rolling below 8 EMA with Follow-Through.
        • Execution: BUY PE AT TOUGH RESISTANCE PEAK.
        • Risk/Reward: Strict 10-15 pt SL above resistance; Target 1 = +20 pts; Target 2 = Day Low / S1.
```

---

## 4. The 15-Minute Structural Maturity & 8-EMA Follow-Through Law

### Why 1-Minute and 5-Minute Charts Deceive
On 1m and 5m charts, market noise constantly prints fake hammers, shooting stars, and dojis that reverse on the very next bar, leading to multiple stop-outs. As taught in the LTA sessions:
> *"15 minutes is a mature timeframe. Double hammers, double inverted hammers, and EMA closes on 15m represent genuine institutional commitments."*

### The 8-EMA Follow-Through Law
A candle closing above or below the 8 EMA is **not an entry signal on its own**. 
* **The Rule:** An entry is valid **ONLY** if the subsequent candle breaks the high (for long) or low (for short) of the trigger candle.
* **Status States in the App:**
  * `ARMED (PENDING_NEXT_BAR)`: 15m candle closed with favorable geometry. Standing by.
  * `TRIGGERED (CONFIRMED)`: Next candle has broken the trigger price. Enter trade immediately.
  * `INVALIDATED`: Price broke the opposite extreme before confirming follow-through. Zero capital risked.

---

## 5. "Respect the Doji" — The Exit & Profit-Booking Rule

### Doji is an EXIT Signal, NOT an Entry Signal
A common retail error is buying a Put simply because a Doji formed, or buying a Call because a Dragonfly Doji appeared. In trending or range-expansion phases, Nifty will break the Doji high and continue rallying.

* **The Core Rule:** Respect the Doji as an **Emergency Invalidation & Exit Signal**:
  * If you are in a **Call (CE) trade** and a 15m Doji appears at Day High, Upper BB, or Major Resistance: **EXIT ALL CALLS IMMEDIATELY**. The expected target for a top Doji is a pullback to Day Low or PDC.
  * If you are in a **Put (PE) trade** and a 15m Doji appears at Day Low, Lower BB, or Major Support: **EXIT ALL PUTS IMMEDIATELY**. The expected target is a recovery to Day High or PDC.
* **App Banner:** Whenever a Doji prints at an extreme, the system flashes a high-visibility amber/blue alert:
  `⚠️ RESPECT THE DOJI: EXIT CALL (CE) POSITIONS NOW`

---

## 6. Priyank Sharma's 1-Minute 3-Drive Institutional Scalper

Implemented in `three_drive_scalper.py` and rendered in **Tab 19 (SMC)** and **Tab 0 (Decision Cockpit)**.

```
          BULLISH 3-DRIVE LIQUIDITY SWEEP & MSS SCALP
 ────────────────────────────────────────────────────────────────────────
    Key HTF Level (PDL / S1 / 100-pt Strike)
          │
          ├─► Drive 1: Initial touch & local bounce (High 1 formed)
          │
          ├─► Drive 2: SWEEP 1 — Drops below Drive 1, triggers retail sell-stops,
          │            wicks back up. Creates Intermediate High 2 (MSS Line).
          │
          ├─► Drive 3: SWEEP 2 — Final panic flush. 
          │            ⚡ RSI Bullish Divergence (Price Lower Low, RSI Higher Low).
          │
          └─► MARKET STRUCTURE SHIFT (MSS) 🚀:
              1-minute candle CLOSES ABOVE Intermediate High 2.
              👉 ENTER LONG (CE) ON CLOSE.
              • Stop-Loss: Placed 1.5 pts below lowest sweep wick (8–14 pts risk).
              • Target 1: 1:1 RR (+8 to +14 pts) -> Trail SL to Cost immediately.
              • Target 2: Origin of Move before Drive 1 (1:2.5 to 1:4 RR runner).
```

### The Institutional Logic: Why It Works
1. **Liquidity Pools:** Retail stop-losses on longs are market sell orders. Large institutions require large blocks of sell orders to absorb their buy limits without causing slippage.
2. **Zero Anticipation:** Never buy during Drive 2 or Drive 3 sweeps. Wait for the **1-minute close above the MSS level**. The break of structure confirms institutional absorption is complete.
3. **Optional Confirmation ("Ingredients"):**
   * **RSI Divergence:** 14-period RSI on 1m chart fails to make a new low during Drive 3.
   * **Volume Expansion:** The MSS breakout candle shows a volume spike > 1.25x the 15-period rolling average.

---

## 7. 100-Point Institutional Strike Psychology

NIFTY 50 options writers (institutions and large prop desks) deploy massive capital on round 100 strikes:
* `24,000`, `24,100`, `24,200`, `24,300`, `24,400`, `24,500`.
* The system automatically maps these strikes as primary resistance and support barriers alongside classical pivot points. When a 15m reversal or a 1m 3-Drive sweep occurs at a 100-point strike, the setup carries institutional grade confluence.

---

## 8. Daily Operational Playbook: Pre-Market to Close

| Time Window | Operational Phase | Terminal Focus & Action |
|---|---|---|
| **08:30 – 09:07 AM** | **Pre-Market Orientation** | Check GIFT Nifty, overnight global markets, PDC levels, and View Building macro indicators. Identify today's S1/R1 and CPR width. |
| **09:15 – 09:30 AM** | **Opening Auction Freeze** | **ZERO TRADES.** Observe gap size in Tab 0. Check whether Market is in Gap-Up Phase 1 or Gap-Down Phase 1. Let opening volatility settle. |
| **09:30 – 11:30 AM** | **Prime Institutional Execution** | **Golden Trading Window.** Watch for Phase 2 Retracements to Support/Resistance or 1m 3-Drive MSS setups. Aim for 20–40 pts. |
| **11:30 – 01:15 PM** | **Midday Decay Freeze** | **NO FRESH BUY ENTRIES.** Theta accelerates rapidly while volume dries up. Tighten trailing stops on active runners to cost. |
| **01:30 – 03:00 PM** | **European Open / Trend Resumption** | Secondary execution window. Look for afternoon 15m 8-EMA continuations or SMC Order Block defenses. |
| **03:00 – 03:30 PM** | **Cash Close & Auction Window** | **TERMINAL CLOSED.** Avoid the 03:15–03:28 PM cash close auction candles due to rollover distortions. |

---

## 9. Tab-by-Tab Visual Reference

* **Tab 0 (🎯 Decision Cockpit):** The master dashboard. Shows the 2-Phase Session Road Map, Top & Bottom 15m Reversal Cards, the live 1m 3-Drive Scalper alert expander, Master Cockpit bias metrics, Spoon-Fed Red Bar callouts, and the 15-Minute Live Trade Advisor.
* **Tab 1 (📊 Option Chain & OI Profile):** Visual bar chart of Call and Put Open Interest walls. Displays Net Gamma Exposure (GEX), GEX flip level, and Straddle decay.
* **Tab 2 (Change in OI):** Intraday shifts in institutional positions. Shows whether call writers or put writers are aggressively adding or unwinding contracts.
* **Tab 7 (📈 Price Action):** Multi-timeframe view (1m, 5m, 15m) with true futures VWAP and EMAs (4, 8, 20, 50, 200).
* **Tab 19 (SMC - Smart Money Concepts):** Structural analysis (BOS / CHoCH), Dealing Range (Premium vs Discount), Unmitigated Order Blocks, Fair Value Gaps (FVG), Order Flow Speed, and Priyank Sharma's 1-Minute 3-Drive Institutional Scalper.
* **Tab 20 (📓 Trading Journal):** Anti-impulse coach and trade logger to review discipline and track profit consistency.
* **Tab 22 (📐 VAWM & 15m Matrix):** V-A-W-M pattern scanner and the 10 Mentor Session Reversal Setups evaluated on closed 15m candles with the 15m Trade Advisor.
* **Tab 23 (⚔️ WAR Setup):** 30-minute first-candle FII strategy across 5 gap scenarios with < ₹100 option contract rules.
* **Tab 24 (🔴 Red Bar & Asian Sweep):** Dr. Devendra Pratap Singh's Advanced Red Bar strategy with Candle 1 POB (0.44–0.56), 150m mean deflection, and European Open Asian Sweep (Profit Factor 3.02).
* **Tab 25 (📝 Paper Trading & Impulse Control):** Risk-free virtual trading with ₹1,00,000 capital, real-time Dhan LTP, the Discipline Gate (win rate preview & impulse tagging), active positions book, and interactive equity curve.

---

## 10. Dr. Devendra Pratap Singh's Advanced Red Bar & Asian Sweep Strategy

Implemented in `red_bar_strategy.py` and rendered in **Tab 24** and **Tab 0**.

### Core Pillars
1. **Point of Balance (POB 0.44 – 0.56):** Never react impulsively to Candle 1 (09:15–09:20). Compute the Point of Balance zone (0.44 to 0.56 ratio of Candle 1's range).
2. **Setup 1 (Morning POB Retest):** Wait for price to pull back to the POB zone. Enter on rejection wick confirmation. (59.0% Win Rate, +87.5 pts across 64 backtested days).
3. **Setup 5 (European Open Asian Liquidity Sweep — 12:45 to 14:15):**
   * Institutions sweep the Asian session High ($H_{\text{asia}}$) or Low ($L_{\text{asia}}$) established between 09:15 and 12:30.
   * When price sweeps the extreme and immediately reclaims back inside the range, enter in the reversal direction.
   * **Institutional Quantitative Verification (64 Historical Sessions):**
     * **Profit Factor: 3.02**
     * **Win Rate: 67.9% (1:1 R:R) / 57.1% (1:1.5 R:R)**
     * **Net Spot Gain: +235.6 points (+₹8,836 per lot)**
     * **Average Risk: Just 10.6 pts (~₹400 / lot)**

---

## 11. The WAR Setup (The 30-Min First-Candle FII Strategy)

Implemented in `war_setup.py` and rendered in **Tab 23**.
* **Core Philosophy:** FII institutional positioning is concentrated in the first 30 minutes (09:15–09:45). The High and Low of Candle 1 establish the intraday battle lines.
* **5 Gap Scenarios:** Evaluates whether market is Gap Up, Gap Down, or Flat, and triggers when the 30-minute High or Low breaks.
* **Strict Premium Rule:** Focuses on Call or Put priced **< ₹100** for optimal gamma acceleration and limited downside.

---

## 12. The 15-Minute Live Trade Advisor & Strike Decision Box

Implemented in `trade_advisor_15m.py` and available in **Tab 0** and **Tab 22**.
* **Answers the 4 Trader Dilemmas:**
  1. *Shall I Buy CE, Buy PE, Wait, or Exit?*
  2. *What is the statistical WIN RATE for this trade right now?*
  3. *Safe to hold, or safe to exit when?*
  4. *What are the exact points to take Stop Loss and Targets (Spot & Option LTP ₹)?*
* **Dynamic Bayesian Probability Model:** Combines 15m candle geometry, 8 EMA / VWAP slope, the 10 Mentor Reversal setups, and Option Chain OI buildup/unwinding into an instant statistical Win Rate % (25% to 86%).

---

## 13. Paper Trading & Impulse Control Terminal

Implemented in `paper_trading.py` and rendered in **Tab 25**.
* **Virtual Capital Account:** Starts with ₹1,00,000. Features 1-click refill (+₹50k, +₹100k) or clean reset.
* **The "Discipline Gate":** Before entering any trade, the system previews the 15m win rate, checks for midday freeze (11:30–13:15), checks for contratrend entries against 8 EMA, flags position sizing (>20% capital), and mandates a setup tag.
* **Active Positions Book:** Real-time P&L (₹ and %) updated live from Dhan option chain, with 1-click individual exit and "Square Off All" panic button.
* **Scorecard & Analytics:** Win Rate %, Profit Factor, Interactive Equity Curve, Strategy vs Impulse breakdown, and persistent CSV logging (`data/paper_trading/trades.csv`).

---
*Document Version: 3.0 · Comprehensive Master Trading Manual · NIFTY Option Chain Sniper Terminal*
