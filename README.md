# NIFTY Option Chain Scanner

Polls the NSE option chain every 3 minutes, works out where support and
resistance are, classifies the market as bullish / bearish / rangebound /
consolidation, and suggests a **buy-side** trade (CE or PE) or tells you to
stay out. Option selling is deliberately not supported — it needs lakhs in
margin per lot and has no ceiling on loss.

---

## Setup in VS Code

```bash
# 1. open the folder in VS Code, then in the terminal:
python -m venv .venv

# macOS / Linux
source .venv/bin/activate
# Windows
.venv\Scripts\activate

pip install -r requirements.txt
```

Select the `.venv` interpreter in VS Code: `Ctrl+Shift+P` → *Python: Select
Interpreter* → pick the one inside `.venv`.

## Run

```bash
streamlit run dashboard.py
```

Opens at `http://localhost:8501`. It refreshes itself every 3 minutes.

**Before market hours, tick "Demo mode" in the sidebar.** That runs the whole
pipeline against `data/sample_chain.csv` with no network at all, so you can
see the screens and test logic changes at any hour.

To check the logic without the UI:

```bash
python selftest.py
```

---

## Important: NSE will block you from a datacenter

NSE's WAF rejects cloud and VPS IP ranges outright, and no amount of header
spoofing gets around it. This must run from your home connection. If you move
it to the PowerEdge, that is fine — the box is on your residential line.

Turn off any VPN. The client bootstraps cookies from the option-chain page
before hitting the API and re-bootstraps automatically when they go stale, but
it cannot fix a blocked IP.

---

## What you get

| Panel / Tab | What it tells you |
|---|---|
| **🎯 Decision Cockpit (Tab 0)** | Unified trading cockpit with 2-Phase Session Road Map, 15m Top/Bottom Reversals, live 1m 3-Drive Scalper alerts, and the interactive 15m Live Trade Advisor. |
| **📝 Paper Trading & Impulse Control (Tab 25)** | Full virtual trading station starting with ₹1,00,000 capital. Real-time Dhan LTP execution, lot size selector, open positions book, live P&L, 1-click square-off, equity curve, and the "Discipline Gate" with 15m win rate checks and impulse trade tagging. |
| **⚡ 15-Minute Live Trade Advisor** | Interactive strike decision terminal (in Tab 0 & Tab 22). Answers *"Shall I buy CE, PE, Hold, Wait, or Exit?"* with statistical Win Rate % at that moment, exact Stop Loss points (Spot & Option LTP ₹), and exit checklists. |
| **⚔️ WAR Setup (FII 30m) (Tab 23)** | The 30-min first candle FII strategy across 5 gap scenarios with price < ₹100 filters, strict trailing rules, and interactive trade card. |
| **📐 VAWM & 15m Matrix (Tab 22)** | Interactive V-A-W-M pattern detector and the 10 Mentor Session Reversal Setups strictly evaluated on closed 15m candles. |
| **🌊 1m 3-Drive Institutional Scalper** | Priyank Sharma's 3-Drive liquidity sweep & MSS scalper with RSI divergence and volume expansion. |
| **📊 Option Chain & Change in OI** | Live NSE/Dhan option chain with Open Interest walls, intraday OI shifts, PCR, Net Gamma Exposure (GEX), and straddle decay. |
| **Session & Timeline** | Spot vs S1/R1 migration over the day, wall break detection, and bias scoring. |

---

## How the bias score works

Six components, each returning a signed contribution. They sum to the score;
`>= +3` reads bullish, `<= -3` bearish.

| Component | Range | What it measures |
|---|---|---|
| `pcr` | ±2 | Put-call ratio. Directional in the middle, **contrarian** above 1.5 and below 0.55 |
| `ce_oi_flow` | ±2 | Magnitude-weighted call-side quadrant flow across ATM ± 5 |
| `pe_oi_flow` | ±2 | Same for puts |
| `wall_migration` | ±2 | Have S1 / R1 moved since the session baseline |
| `position` | ±1 | Where spot sits between S1 and R1 |
| `wall_break` | ±3 | OI at a wall collapsing while price presses into it — the fastest move on the board |

Quadrant logic (price change × OI change) is applied per strike and then
**inverted for puts**, which is the step most people get wrong: call writing is
bearish for the index, put writing is bullish.

---

## Guard rails

The scanner refuses to emit a signal when any of these apply. They come from
your own course notes:

- First 15 minutes — opening OI is chaotic
- Midday freeze (11:30–13:15) — theta acceleration warning; blocks premature buys
- After 3:00 PM — no fresh entries
- A regime flip must hold for 2 consecutive polls before it is reported
- Maximum 3 attempts in a rangebound market
- Expiry day raises a warning, not a block
- A strike is rejected if OI < 1000 or the bid-ask spread exceeds 3% of premium
- A warning fires if one lot costs more than 30% of your configured capital

---

## Set this before you use it

In `config.py`:

```python
CAPITAL   = 100_000   # your configured paper / live capital
LOT_SIZE  = 75        # NIFTY lot size
```

---

## Files

```
dashboard.py            main streamlit application and multi-tier ribbon
paper_trading.py        paper trading account manager, order execution, & analytics
paper_trading_tab.py    paper trading terminal UI, discipline gate, & journal
trade_advisor_15m.py    15m live trade advisor & strike decision box (win rate & SL)
war_setup.py            WAR Setup (30m FII First-Candle) strategy engine
war_tab.py              WAR Setup UI terminal
session_reversal_matrix.py 15m session reversal matrix (10 mentor setups)
chart_patterns.py       V-A-W-M chart pattern recognition engine
chart_patterns_tab.py   VAWM patterns and 15m matrix UI tab
three_drive_scalper.py  Priyank Sharma 1m 3-Drive institutional scalper
dhan_client.py          DhanHQ v2 API client for live intraday futures & option quotes
analytics.py            levels, quadrants, bias score, regime, signal, greeks
chain_parser.py         NSE JSON and NSE CSV -> one dataframe shape; spot inference
store.py                snapshot persistence and cross-poll session state
selftest.py             offline end-to-end run against sample data
```

---

## Single-Writer Architecture & Data Persistence Limits

### 1. Capability Flag Single-Writer Model
To eliminate dual-writer race conditions, schema divergence, and data corruption:
- `scheduler.py` is the **ONLY authorized writer** of option chain snapshots (`data/snapshots/`), 5-minute candles (`data/candles_5m/`), and daily observation logs (`data/observations/`).
- `dashboard.py` is strictly a read-only presentation process. `store.py` and `price_action.py` enforce an explicit capability flag (`_WRITES_ENABLED = False` by default). Writes are enabled exclusively when `scheduler.py` invokes `store.enable_writes("scheduler")`.
- `agt_briefing.py` run from the command line is strictly **read-only**: it computes and renders terminal outputs without appending to observation logs or dispatching Telegram notifications.

### 2. Hard Data Limitation: No Intraday Historical Option Chain API
> [!IMPORTANT]
> **Permanent Data Source Constraint:**  
> Dhan serves only the **CURRENT** option chain snapshot. There is **no historical intraday option chain / OI endpoint** provided by the broker.  
> If `scheduler.py` was not running during a market window, that window's open interest, GEX, option walls, PCR, and writer dominance are **permanently unrecoverable**.  
> The system strictly reports these as **unrecoverable data gaps**. Under no circumstances are missing option chain snapshots interpolated or carried forward from stale data. 5-minute price action candles (OHLCV), however, are fully recoverable and self-healed via historical candle backfill from Dhan.

---

## What this is not

It is a complete algorithmic decision-support and simulated execution system. It provides high-probability setups, exact spoon-fed entries, stop losses, targets, and paper execution to eliminate emotional impulse trading. Always manage risk and preserve trading capital.
