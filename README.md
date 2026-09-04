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

| Panel | What it tells you |
|---|---|
| **Market state** | BULLISH / BEARISH / RANGEBOUND / CONSOLIDATION / BREAKOUT / BREAKDOWN, with the bias score |
| **Signal** | BUY CE, BUY PE, or NO TRADE — plus the specific strike, premium, cost per lot, and delta |
| **Metrics** | Spot, S1/S2, R1/R2, PCR, max pain, IV skew |
| **OI profile** | Bar chart of call and put OI by strike — the walls, visually |
| **Change in OI** | Today's OI change per strike. This is the live signal, not the absolute level |
| **Session** | Spot vs S1 vs R1 over the day, with bias score and PCR beneath. The migration of the walls is the strongest intraday signal here |
| **Score breakdown** | Every component of the bias score and its contribution, so nothing is a black box |

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
CAPITAL   = 20_000   # your actual trading capital
LOT_SIZE  = 65       # verify against NSE — it gets revised
```

`LOT_SIZE` moved from 75 to 65 in the January 2026 series. Every rupee figure
in the app depends on it.

---

## The log is the point

Every poll appends a row to `data/signal_log.csv` and saves the full chain to
`data/snapshots/YYYY-MM-DD/`. Two columns in the log are left blank on
purpose: `spot_plus_30min` and `outcome`.

Fill them in. After a couple of months you will have a real dataset and can
answer, on your own data, whether the score predicts anything — instead of
having an opinion about it. If a component turns out not to earn its weight,
delete it from `analytics.analyse()`.

That is the entire reason for building this rather than renting one.

---

## Files

```
config.py         all tunables and thresholds — edit here, not in logic files
nse_client.py     session bootstrap, retries, throttling
chain_parser.py   NSE JSON and NSE CSV -> one dataframe shape; spot inference
analytics.py      levels, quadrants, bias score, regime, signal, greeks
store.py          snapshot persistence and cross-poll session state
dashboard.py      streamlit UI and charts
selftest.py       offline end-to-end run against the sample file
```

---

## What this is not

It is a decision-support tool. It tells you where the levels are and what the
positioning flow is doing. It does not tell you when to click buy — that comes
from your price-action setup. A chain read is a reason to have an opinion and
a direction filter; it is not an entry trigger, and the OI signal lags price.

The chain also cannot show you intent. Heavy call OI at a strike might be a
naked directional write, one leg of a spread, or a hedge against a cash
position. Those mean completely different things and look identical in the OI
column.
