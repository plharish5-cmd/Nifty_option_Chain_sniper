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

## 5. Mentor Sushen's Exit & Trailing Laws (Strategy 1)

### A. Doji is an EXIT Signal, NOT an Entry Signal
A common retail error is buying a Put simply because a Doji formed, or buying a Call because a Dragonfly Doji appeared. In trending or range-expansion phases, Nifty will break the Doji high and continue rallying.
* **The Core Rule:** Respect the Doji as an **Emergency Invalidation & Exit Signal**:
  * If in a **Call (CE) trade** and a 15m Doji appears at Day High, Upper BB, or Major Resistance: **EXIT ALL CALLS IMMEDIATELY**.
  * If in a **Put (PE) trade** and a 15m Doji appears at Day Low, Lower BB, or Major Support: **EXIT ALL PUTS IMMEDIATELY**.

### B. Opposite Reversal Candlestick Exit
* **When SHORT (PE):** A **Hammer**, **Double Hammer**, or **Dragonfly Doji** (lower wick rejection) closing at Day Low or Lower BB is an **IMMEDIATE PROFIT-BOOKING EXIT** (or exit on break of its high).
* **When LONG (CE):** An **Inverted Hammer**, **Double IH**, or **Shooting Star** (upper wick rejection) closing at Day High or Upper BB is an **IMMEDIATE PROFIT-BOOKING EXIT** (or exit on break of its low).

### C. The Cost-to-Cost (CTC) Breakeven Law
* **The Golden Rule:** As soon as a trade achieves $+15$ to $+20$ points of favorable excursion, immediately move the Stop-Loss to Cost-to-Cost (Entry Price / Trigger).
* **Capital Protection:** A trade that was $+20$ points in profit must **NEVER** turn into a negative loss.

### D. Bollinger Band Targets & Stage 1 Targets
* **Trending Stages (Stage 2 & 3):** Ride runners candle-by-candle until the Upper BB (Long) or Lower BB (Short) is touched.
* **Rangebound Stage 1 (BB Width < 60 pts):** Take profit at **10 to 15 points** (maximum 20 points). Max 2 to 3 attempts.
* **Master Documentation:** For full details and the complete audit of 67 mentor/student charts, see `PA_STRATEGY1_MENTOR_KNOWLEDGEBASE.md`.

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
| **21:00 PM (Prior Night)** | **Evening Data Harvesting** | Collect FII/DII cash & F&O positioning, closing futures basis, advances/declines, and daily candle patterns. |
| **07:30 – 08:00 AM** | **Pre-Market View Building** | Compare GIFT Nifty against NIFTY FUTURES close (±100 pts threshold). Refresh Asian markets, Crude (< or > ₹6,500), and Stoch RSI. Submit View Building Sheet before 08:00 AM. |
| **08:55 – 09:00 AM** | **Trading Room Assembly** | Join mentor live market room. Verify 11-instrument watchlist and chart styling. |
| **09:00 – 11:00 AM** | **Prime Institutional Execution** | **Active Trading Window.** Strictly trade according to pre-market view, S&R levels, and 15m/1m setups. Aim for 20–40 pts. Screen exit at 11:00 AM. |
| **11:00 – 01:15 PM** | **Midday Decay Freeze** | **NO FRESH BUY ENTRIES.** Capital preservation mode. Trailing stops tightened to Cost-to-Cost. |
| **01:30 – 03:00 PM** | **European Open / Trend Resumption** | Secondary execution window. Look for afternoon 15m 8-EMA continuations or SMC Order Block defenses. |
| **15:30 PM** | **Daily Review & Homework** | Mentor posts daily review chart. Reconcile trades, mark S1/S2/S3 & R1/R2/R3, and prepare for tomorrow's View Building. |

---

## 9. Tab-by-Tab Visual Reference

* **Tab 0 (🎯 Decision Cockpit):** The unified master confluence dashboard. Synthesizes all tabs in real time: Live Level Striking & Proximity Radar (tells you whether to Buy CE, Buy PE, or Wait when striking key support/resistance/breakout), Dual-Timeframe Candle Confluence (15m vs 5m alignment, 8 EMA, VWAP), All-Tab Confluence Radar (voting across all 7 engines), Spoon-Fed Order Placement Ticket (Contract, LTP, Spot SL, Option SL ₹, Target 1 +20 pts CTC, Target 2), and specialized deep-dive panels (Top/Bottom Reversal Sniper, 1m 3-Drive Scalper, Historical Wicks, and TimesFM AI).
* **Tab 1 (📊 Option Chain & OI Profile):** Visual bar chart of Call and Put Open Interest walls. Displays Net Gamma Exposure (GEX), GEX flip level, and Straddle decay.
* **Tab 2 (Change in OI):** Intraday shifts in institutional positions. Shows whether call writers or put writers are aggressively adding or unwinding contracts.
* **Tab 7 (📈 Price Action):** Multi-timeframe view (1m, 5m, 15m) with true futures VWAP and EMAs (4, 8, 20, 50, 200).
* **Tab 10 (📋 View Building Studio):** Pre-market 17-parameter institutional diagnostic sheet auto-evaluating futures basis, VIX % change, crude economics, GIFT Nifty vs Futures, Daily Stoch RSI market shapes, advances/declines with market-cap trap filter, 5 core sectors, PCR seller psychology, and FII flows.
* **Tab 17 (🎯 PA Strategy 1):** Mentor Sushen's core 4/8 EMA 15-minute system with Bollinger Bands, UBB/LBB slope filters, Cost-to-Cost (CTC) capital protection, Doji/Hammer reversal exits, and 2-trade daily cap.
* **Tab 18 (⚡ VWAP/EMA):** Dynamic VWAP & 8 EMA confluence engine with dual-filter alignment, 4 EMA order protection (eliminates counter-trend traps), CTC ratchet at +20 pts, +40 pt Target booking, and legacy comparison toggle.
* **Tab 19 (SMC - Smart Money Concepts):** Structural analysis (BOS / CHoCH), Dealing Range (Premium vs Discount), Unmitigated Order Blocks, Fair Value Gaps (FVG), Order Flow Speed, and Priyank Sharma's 1-Minute 3-Drive Institutional Scalper.
* **Tab 20 (📓 Trading Journal):** Anti-impulse coach and trade logger to review discipline and track profit consistency.
* **Tab 22 (📐 VAWM & 15m Matrix):** V-A-W-M pattern scanner and the 10 Mentor Session Reversal Setups evaluated on closed 15m candles with the 15m Trade Advisor.
* **Tab 23 (⚔️ WAR Setup):** 30-minute first-candle FII strategy across 5 gap scenarios with < ₹100 option contract rules.
* **Tab 25 (📝 Paper Trading & Impulse Control):** Risk-free virtual trading with ₹1,00,000 capital, real-time Dhan LTP, the Discipline Gate (win rate preview & impulse tagging), active positions book, and interactive equity curve.

---

## 10. The WAR Setup (The 30-Min First-Candle FII Strategy)

Implemented in `war_setup.py` and rendered in **Tab 23**.
* **Core Philosophy:** FII institutional positioning is concentrated in the first 30 minutes (09:15–09:45). The High and Low of Candle 1 establish the intraday battle lines.
* **5 Gap Scenarios:** Evaluates whether market is Gap Up, Gap Down, or Flat, and triggers when the 30-minute High or Low breaks.
* **Strict Premium Rule:** Focuses on Call or Put priced **< ₹100** for optimal gamma acceleration and limited downside.

---

## 11. The 15-Minute Live Trade Advisor & Strike Decision Box

Implemented in `trade_advisor_15m.py` and available in **Tab 0** and **Tab 22**.
* **Answers the 4 Trader Dilemmas:**
  1. *Shall I Buy CE, Buy PE, Wait, or Exit?*
  2. *What is the statistical WIN RATE for this trade right now?*
  3. *Safe to hold, or safe to exit when?*
  4. *What are the exact points to take Stop Loss and Targets (Spot & Option LTP ₹)?*
* **Dynamic Bayesian Probability Model:** Combines 15m candle geometry, 8 EMA / VWAP slope, the 10 Mentor Reversal setups, and Option Chain OI buildup/unwinding into an instant statistical Win Rate % (25% to 86%).

---

## 12. Paper Trading & Impulse Control Terminal

Implemented in `paper_trading.py` and rendered in **Tab 25**.
* **Virtual Capital Account:** Starts with ₹1,00,000. Features 1-click refill (+₹50k, +₹100k) or clean reset.
* **The "Discipline Gate":** Before entering any trade, the system previews the 15m win rate, checks for midday freeze (11:30–13:15), checks for contratrend entries against 8 EMA, flags position sizing (>20% capital), and mandates a setup tag.
* **Active Positions Book:** Real-time P&L (₹ and %) updated live from Dhan option chain, with 1-click individual exit and "Square Off All" panic button.
* **Scorecard & Analytics:** Win Rate %, Profit Factor, Interactive Equity Curve, Strategy vs Impulse breakdown, and persistent CSV logging (`data/paper_trading/trades.csv`).

---

## 13. Pre-Market View Building System & Market Shape Engine

Codified comprehensively in [VIEW_BUILDING_MENTOR_KNOWLEDGEBASE.md](file:///home/harish/Documents/Swathi%20K%20N/1%20Trading/option%20chain/OC/VIEW_BUILDING_MENTOR_KNOWLEDGEBASE.md) based on Mentor Manjunath's master live training sessions:

1. **The 17 Quantitative Pre-Market Parameters:**
   - **#1 & #2 (Basis):** Nifty & Bank Nifty Futures vs Spot. Premium = Institutional demand (+1); Discount = Institutional exit/shorting (-1).
   - **#3 (India VIX):** Day-over-day change. $<-4\%$ is Bullish (+2); $>+4\%$ is Bearish (-2); $\pm 4\%$ is Neutral (0).
   - **#4 (Crude Oil):** Benchmark ₹6,500/bbl ($65/bbl). $<6500$ = Bullish (+1); $>6500$ = Bearish (-1).
   - **#5 (GIFT Nifty Gap Prediction):** Measured against **NIFTY FUTURES CLOSE** at **07:30 AM IST** (NEVER spot!). Difference $\ge +100$ pts = Gap Up; $\le -100$ pts = Gap Down; between $-100$ and $+100$ pts = Flat Open.
   - **#6 (Daily Trend):** Candle above 8 EMA + rising UBB = +2 Bullish; below 8 EMA + falling LBB = -2 Bearish.
   - **#7 (Stochastic RSI):** Evaluated strictly on **Daily chart** (intraday shape) and **Weekly chart** (investing). $<20$ = Oversold (V-Shape); $>80$ = Overbought (A-Shape). Weekly near 0 = Wealth creation entry for 10%–23%+ multi-month rally.
   - **#8 & #9 (S1–S3 & R1–R3):** Swing levels and gaps defining bounce and reversal destinations.
   - **#10 & #11 (Advances/Declines):** Sourced from Moneycontrol for Nifty 50 and Bank Nifty. **Trap Filter:** Check top 2 lines of market-cap heavyweights (Reliance, HDFC, ICICI, SBI, TCS) — if $\ge 4\text{–}5$ are RED, ignore raw counts and do NOT score Bullish!
   - **#12 (5 Foundation Sectors):** IT, Banking (Highest Priority), Infrastructure, Auto, Metals. $\ge 3$ positive = +2; $\ge 3$ negative = -2.
   - **#13 (PCR):** Total Put OI / Total Call OI. $<0.5$ = Oversold (V-Shape bounce); $>1.4$ = Overbought (A-Shape rollover).
   - **#14 (FII/DII Confluence):** FII Cash + Index Futures + Stock Futures. Buying Cash/Stock Futures while selling Index Futures creates a **two-sided volatile / W-pattern day** rather than a 1-sided dump.
   - **#15 (Option Chain Defended Levels):** S&R defended, quadrant flows, and market state classification.
   - **#16 & #17 (Global Markets & US ADRs):** US futures + Asian morning cues; top 5 Indian ADRs (Infosys, Wipro, ICICI, HDFC, Dr. Reddy's).

2. **The 4 Market Shapes Anticipated:**
   - **V-Shape (Oversold):** Stoch RSI $<20$ and/or $\text{PCR} < 0.5$ &rarr; Early drop into S1/S2 &rarr; Demand absorption &rarr; Massive +100 to +200 pt rally to R1.
   - **A-Shape (Overbought):** Stoch RSI $>80$ and/or $\text{PCR} > 1.4$ &rarr; Early rally into R1/R2 &rarr; Supply exhaustion &rarr; Deep +100 to +200 pt collapse to S1.
   - **W-Shape (Two-Sided Double Bottom):** Initial drop &rarr; 8 EMA bounce &rarr; Double bottom retest &rarr; Afternoon breakout.
   - **M-Shape (Two-Sided Double Top):** Initial push &rarr; 8 EMA pullback &rarr; Double top failure &rarr; Afternoon breakdown.

3. **Mandatory 11-Asset Morning Watchlist:**
   `Nifty 50`, `Nifty Current Month Fut`, `Bank Nifty`, `Bank Nifty Current Month Fut`, `India VIX`, `Crude Oil Fut`, `GIFT Nifty`, `Nifty BeES`, `Bank BeES`, `Gold BeES`, `Silver BeES`.

4. **TradingView Visual Discipline:**
   - Bollinger Bands: Median SMA removed/hidden; Upper = Dark Red, Lower = Dark Green.
   - EMA Cross: Fast = 4 EMA (Green), Slow = 8 EMA (Red); cross marks disabled.
   - Candles: Solid Black Borders on all candles for sharp visual edge recognition.

---
*Document Version: 4.0 · Comprehensive Master Trading Manual · NIFTY Option Chain Sniper Terminal*
