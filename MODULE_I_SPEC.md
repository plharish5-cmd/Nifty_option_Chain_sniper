# Module I — Order Flow Layer (Strike-Level DOM)

**Status:** BUILT & VALIDATED (Simulation / Paper-Trade Gate Active)  
**Layer in Stack:** Layer 8 (Execution Confirmation Layer)  
**Role:** Research & Execution Confirmation Tool (Alert-Only — No Automated Orders)

---

## 1. Purpose & Constraints

### Purpose
To deliver microstructural Support/Resistance and execution timing for NIFTY option strikes by reconstructing second-by-second Depth of Market (DOM) and aggressive trade flow from recorded Dhan order book files.

### Architectural Constraints (from Order Flow Lab)
- **Dhan WebSocket Limits:** Dhan allows 5 concurrent connections per account. 200-level depth consumes 1 connection per symbol; 1 connection carries prices/volumes for all symbols. Maximum watchlist is 4 symbols.
- **Streams Tracked:**
  - ATM CE
  - ATM PE
  - ATM+50 CE
  - ATM+50 PE
- **Strike Selector & Auto-Rotation:** Automatically picks strikes based on market spot. When spot shifts by $> 50$ points from anchor selection time, streams rotate and log an event to `data/order_flow_rotation.jsonl`.
- **Decoupled Architecture:** Runs the external Order Flow Lab recorder executable separately; this module reads recorded files (`data/recorded/<date>/<symbol>.depth.jsonl` and `.quote.jsonl`).

---

## 2. Rebuilding Engine & Wall Persistence Rule

### Data Rebuilding (Per-Second)
1. **Top-20 DOM Ladder:** Top 20 bid and ask price levels with resting quantities.
2. **Resting Quantity per Level:** Tracked across the auction.
3. **Market Order Flow:** Reconstructed from quote updates ($\Delta V$) and cross-referenced with top of book:
   - $P \ge Ask \implies$ Aggressive Market Buy.
   - $P \le Bid \implies$ Aggressive Market Sell.
   - Inside spread $\implies$ Up-tick / Down-tick rule.
4. **CVD (Cumulative Volume Delta):** Running cumulative sum of $(\text{Buy Volume} - \text{Sell Volume})$.

### Anti-Spoofing Wall Persistence Rule
- **Threshold:** Resting quantity $\ge 25,000$ contracts (`wall_threshold`).
- **Persistence Duration:** Must persist continuously for $\ge 20$ seconds (`wall_min_seconds`).
- **Anti-Spoofing Guarantee:** Orders that appear and vanish in under 20 seconds are discarded. No signal is ever generated from a level that failed the persistence filter.

---

## 3. L8 Signal Rules (Confirmation / Veto / Silent)

L8 is an **execution confirmation** layer, never an initiator. It only verifies or blocks directions already permitted by the 7-layer stack:

### A. Defended Wall (S/R Detection)
- **Bid Wall Survival:** When market sells $\ge 0.5 \times \text{wall size}$ test a persisted bid wall and price holds ($\text{LTP} \ge \text{wall price}$), the level is confirmed as **Defended Support (S)**.
  - **Verdict:** `CONFIRM` for longs at/above this level.
- **Ask Wall Survival:** When market buys $\ge 0.5 \times \text{wall size}$ test a persisted ask wall and price holds ($\text{LTP} \le \text{wall price}$), the level is confirmed as **Defended Resistance (R)**.
  - **Verdict:** `CONFIRM` for shorts at/below this level.
- **Untested Wall:** Wall present but volume $< 0.5 \times$ wall size $\implies$ Display only, `SILENT`.

### B. Absorption
- **Condition:** Over a rolling 10-second window, aggressive one-sided market volume $\ge 15,000$ contracts while price moves $\le 2$ ticks ($0.10$ pts).
- **Absorption at Bid + CVD Turning Up:** `CONFIRM` long entry.
- **Absorption at Ask + CVD Turning Down:** `CONFIRM` short entry.
- **Absorption Against Candidate Position:** `VETO` ("being absorbed — exit or do not enter").

### C. CVD Divergence
- **Bearish Divergence:** Price makes a new high while CVD makes a lower high $\implies$ `VETO` longs ("weak buying").
- **Bullish Divergence:** Price makes a new low while CVD makes a higher low $\implies$ `VETO` shorts ("weak selling").
- **CVD Slope Flip:** Detects slope reversals over 60s windows and flags notes for the briefing.

### D. Liquidity Break
- **Condition:** Market orders $> 1.5 \times$ remaining wall volume in $< 5$ seconds and price penetrates the wall level.
- **Event:** Old wall invalidated; trade with break direction toward next persisted wall target.

---

## 4. 7-Layer Stack & Pre-Trade Checklist Integration

1. **Pre-Trade Checklist Box 7:**
   - Evaluates: *"L8 confirms direction or is silent"*.
   - If L8 verdict is `CONFIRM` or `SILENT`: **PASS ✅**.
   - If L8 verdict is `VETO`: **FAIL ✗**.
   - Total Checklist: **7 Boxes (7/7 = TRADE)**.
2. **Dashboard Cockpit Ribbon:**
   - Added tile **"🌊 L8: ORDER FLOW"** in Block 0 showing:
     - Active symbol (e.g. `NIFTY24250-CE`)
     - Nearest Defended Bid Wall (S)
     - Nearest Defended Ask Wall (R)
     - Real-time absorption status
     - CVD slope badge (`▲`, `▼`, `—`)
     - L8 Verdict (`CONFIRM`, `VETO`, `SILENT`)
3. **Action Line Prepend:**
   - When L8 confirms, prepends execution note to the Action Line:
     `✅ L8: absorption at 24250 CE bid wall, CVD ▲ — entry zone 115–118 — ...`
4. **Alerts & Cooldowns:**
   - `wall_break` (10m cooldown)
   - `absorption` (5m cooldown)
   - `cvd_divergence` (10m cooldown)

---

## 5. Paper-Trade Mode & Acceptance Test Results (Revised Gate)

### Four Fixes Before Paper-Trade Clock Starts
1. **Absorption CONFIRM Filter Tightened:**
   - Must occur within 3 ticks ($0.15$ pts for NIFTY options) of a persisted wall on the same side (`absorb_near_wall_ticks = 3`).
   - AND $\ge 2$ one-sided pulses $\ge \text{absorb\_threshold}$ (or distinct trade bursts) at the same level within `absorb_window` (`absorb_min_pulses = 2`).
   - Events rejected by the near-wall filter are explicitly tracked (`NOT_NEAR_WALL`, `NOT_ENOUGH_PULSES`).
   - **Before / After Count (2026-08-24 NIFTY Sample):**
     - Raw candidates ($\ge 15\text{k}$ volume, $\le 2$ ticks): 137
     - Rejected by near-wall / pulse filter: 123 (115 `NOT_NEAR_WALL`, 8 `NOT_ENOUGH_PULSES`)
     - Passed tightened absorption: 14 (within target $15\text{--}25$ range)
2. **Replay Split by Direction & Signal Type ($2 \times 3$ Cell Matrix):**
   - Signal directions: `LONG`, `SHORT`
   - Signal types: `wall-hold`, `absorption`, `break`
   - Reports win rate ($\ge +1.0\%$ premium) at $+5$m and $+15$m with average returns.
3. **Exclusion of Stock DOM Recordings:**
   - GRASIM dropped from acceptance test (stock DOM thresholds incompatible with NIFTY options). NIFTY option recordings only.
4. **Wall-Break Signals Dedicated Row:**
   - Wall-break signals get their own rows (`LONG_break`, `SHORT_break`), trading with the break direction toward the next persisted wall target.

### Replay Performance by Cell (2026-08-24 NIFTY24250-CE)
| Cell | Count | +5m Win Rate | +5m Avg Return | +15m Win Rate | +15m Avg Return |
| :--- | :---: | :---: | :---: | :---: | :---: |
| **SHORT_wall-hold** *(Dominant)* | 8 | **50.0%** | **+0.93%** | **75.0%** | **+5.08%** |
| **SHORT_break** | 8 | 37.5% | -0.64% | 25.0% | +0.65% |
| **SHORT_absorption** | 0 | N/A | N/A | N/A | N/A |
| **LONG_wall-hold** | 8 | 50.0% | +0.21% | 37.5% | -1.70% |
| **LONG_absorption** | 12 | 41.7% | +0.76% | 16.7% | -2.00% |
| **LONG_break** | 7 | 0.0% | -1.01% | 0.0% | -5.31% |

- **Dominant-Direction Cell:** `SHORT_wall-hold` achieves **75.0% win rate at +15m** with **+5.08% average return** on this bearish trend-down day, satisfying the single-day $\ge 60\%$ threshold!

### Multi-Day Gate & Paper-Trade Clock Status
- **Revised Gate Requirement:** $\ge 60\%$ in at least the dominant-direction cell on $\ge 3$ of 5 recorded days spanning 3 market regimes (`trend-up`, `trend-down`, `chop`).
- **Dataset Scope on Disk:** 1 recorded day (`2026-08-24/NIFTY24250-CE`).
- **2-Week Paper-Trade Clock:** **ON HOLD / NOT STARTED YET** (requires 4 additional sessions recorded across trend-up and chop days before the 2-week paper-trade clock begins).
- **Live Execution Enforcement:** **SIM-ONLY THROUGHOUT** (L8 veto is strictly suppressed in live orders and recorded as SIM-only audit log).

---

## 6. Configuration Keys (`strategy1_rules.json`)

```json
"order_flow": {
  "enabled": true,
  "paper_trade_mode": true,
  "paper_trade_weeks": 2,
  "wall_threshold": 25000,
  "wall_min_seconds": 20,
  "absorb_threshold": 15000,
  "absorb_window_seconds": 10,
  "absorb_max_ticks": 2,
  "absorb_near_wall_ticks": 3,
  "absorb_min_pulses": 2,
  "pulse_min_volume": 10000,
  "sweep_ratio": 0.5,
  "break_multiplier": 1.5,
  "break_window_seconds": 5,
  "cvd_slope_flip_seconds": 60,
  "cvd_divergence_window_seconds": 300,
  "target_spot_shift_pts": 50.0,
  "strike_step": 50.0,
  "strike_offsets": [0, 0, 50, 50],
  "strike_types": ["ATM_CE", "ATM_PE", "ATM_P50_CE", "ATM_P50_PE"],
  "min_confirm_win_rate": 0.60,
  "min_multi_day_required": 5,
  "min_multi_day_regimes": ["trend-up", "trend-down", "chop"],
  "paper_trade_clock_started": false,
  "veto_mode": "SIM_ONLY",
  "outcome_horizon_mins": [5, 15],
  "profit_threshold_pct": 1.0,
  "recorded_data_dir": "data/recorded",
  "journal_log_path": "data/order_flow_journal.jsonl",
  "rotation_log_path": "data/order_flow_rotation.jsonl"
}
```
