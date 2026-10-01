"""
Central configuration. Everything tunable lives here — nothing is hardcoded
elsewhere. Edit this file, not the logic files.
"""
from datetime import time
from pathlib import Path

# ── Instrument ───────────────────────────────────────────────────────────────
SYMBOL          = "NIFTY"       # index symbol on NSE
LOT_SIZE        = 65            # NIFTY lot size, revised from 75 in Jan-2026.
NIFTY_LOT_SIZE  = LOT_SIZE      # Alias for modules referencing NIFTY_LOT_SIZE
                                # VERIFY THIS against NSE before trading — it
                                # gets revised periodically and every rupee
                                # figure in this app depends on it.
LIQUIDITY_THRESHOLD = 800.0     # Max acceptable slippage/impact cost (Spread × Lot Size in ₹)
                                # Above ₹800 = Illiquid (dangerous for naked option selling)
STRIKE_STEP     = 50            # NIFTY strike interval
EXPIRY_INDEX    = 0             # 0 = nearest expiry (the 1st dropdown entry)

# ── Polling & Active Market Window ──────────────────────────────────────────
POLL_SECONDS          = 60            # 60s for ultra-fresh data during active market
AUTO_REFRESH_START    = time(9, 0)    # 09:00 AM IST (starts pre-market prep; avoid overnight polling on stale chain)
AUTO_REFRESH_END      = time(15, 45)  # 03:45 PM IST (stops auto-pinging after session close)
MARKET_OPEN           = time(9, 15)
MARKET_CLOSE          = time(15, 30)
ALLOW_WEEKEND_STORAGE = False         # STRICT RULE: NEVER store live/trial pings, snapshots or logs on Sat & Sun no matter what!

# ── Scheduler windows (scheduler.py) ─────────────────────────────────────────
# Option chain, levels and the signal log. Active 09:00 to 15:45 IST.
CHAIN_WINDOW          = (AUTO_REFRESH_START, AUTO_REFRESH_END)

# View Building. Runs through the small hours so the sheet is ready when you
# sit down — global markets and ADRs settle overnight, and NSE serves the
# previous session's breadth and flows well before dawn.
VIEW_WINDOW     = (time(1, 0), time(10, 0))
VIEW_REFRESH_MINUTES = 30

# Daily OHLC append. NSE does not publish the completed bar at a fixed time,
# so this RETRIES rather than firing once. It stops for the day the moment it
# succeeds — the append itself is idempotent, but re-running it every ten
# minutes for six hours would be pointless load.
# NSE settles the daily figures a little after the 15:30 bell, so the bar is
# only trustworthy from about 15:40.
OHLC_APPEND_AFTER = time(15, 40)
OHLC_BACKFILL_DAYS = 30       # recompute this many sessions on each run, so a
                              # session missed while the machine was off gets
                              # repaired rather than staying absent forever
OHLC_RETRY_FROM  = time(15, 40)
OHLC_RETRY_UNTIL = time(23, 0)
OHLC_RETRY_MINUTES = 10

SCHEDULER_STATE = None          # set below, after DATA_DIR

# ── Analysis window ──────────────────────────────────────────────────────────
# Strikes beyond this distance from ATM are noise for intraday purposes.
WINDOW_STRIKES  = 5             # ATM +/- 5 strikes = 250 points either side

# Where the analysis window is centred.
#   "live"     re-derive from spot each poll — window follows price
#   "preopen"  lock to the 9:08 pre-open level and hold it all session (MJ's)
# They diverge on trend days, which is exactly when it matters. Neither is
# obviously right; run a few weeks of each and let the signal log decide.
CENTRE_STRIKE_MODE = "live"

# Widen the window when expiry is further out, narrow it near expiry.
WINDOW_SCALES_WITH_EXPIRY = True

# Call/put OI ratio at the centre strike beyond which one side is "heavy".
ATM_WEIGHT_RATIO = 1.5

# OI %% points by which one side must out-build the other AT THE SAME STRIKE
# before a level is CONFIRMED rather than merely POTENTIAL. Without this
# comparison, "building support" is a claim the data does not support — the
# put column alone cannot distinguish a floor from a fight.
LEVEL_DOMINANCE_PCT = 8.0

# Lakhs of contracts added to ONE strike in a single poll before it counts as
# institutional size. Raise it if the alert fires every poll — a detector that
# alerts constantly is noise with a siren.
ICEBERG_LAKHS = 5.0

# Volume per unit of NEW open interest above which a strike is churn rather
# than positioning. Heavy volume creating little OI means contracts changed
# hands without positions being opened — no committed money behind that wall.
CHURN_RATIO = 3.0

# How many sessions of state-change history to keep. The log is a rolling
# window: a new day is added, the oldest drops off. One session cannot tell
# you whether a read is worth trusting; a month can start to.
TIMELINE_KEEP_DAYS = 90

# Outcome scoring. Horizon and threshold are both arbitrary — twenty points
# in thirty minutes is a plausible intraday move, not a proven one. Change
# them and the scorecard changes; that sensitivity is the point, and any
# result that survives only at one setting was never a result.
OUTCOME_HORIZON_MINS = 30
OUTCOME_THRESHOLD_PTS = 20.0
OUTCOME_MIN_SAMPLE = 8     # below this, no percentage is shown

# How the five course chain-conditions read colour. "ltp" = change in
# premium (NSE's CHNG column); "oi" = change in open interest, the original
# reading, which turned out to be direction-blind. Confirm against MJ's
# screen — the View Building tab shows both so you can compare.
CHAIN_COLOUR_MODE = "ltp"

# Which 15-minute candle defines PDC. 15:00 covers 15:00-15:15 and is the
# last candle of the continuous session; 15:15 covers 15:15-15:30 and
# includes the closing call auction, whose print is not a traded price.
PDC_CANDLE = time(15, 0)

# Which prices the VWAP/EMA tab uses. "futures" = real traded volume and the
# line institutions watch, but prices sit 15-30 points above spot. "index" =
# spot prices weighted by futures volume, so it matches your chart while the
# weighting is a proxy. Compare one against TradingView, then stay with it.
TAB18_VWAP_MODE = "futures"

# GEX dealer-positioning sign. -1 is the US convention (dealers short calls,
# long puts). If Indian index positioning runs the other way — retail buys,
# institutions write — every GEX reading inverts. No evidence either way, so
# it is a setting. Test it in the Timeline log before believing it.
GEX_DEALER_SIGN = -1

# Strikes each side of spot included in GEX and the IV averages. Deep OTM
# strikes trade at five paise, fail the IV solver, and carry negligible
# gamma — including them produced a "46 strikes skipped" warning every poll
# for contributions near zero.
GEX_BAND_PTS = 500

# Alerting. Both numbers exist to keep the thing quiet. Raising the daily
# ceiling is almost always the wrong response to a noisy day — a session
# that produces many aligned trend changes is a session that is chopping.
ALERT_COOLDOWN_MINS = 20
ALERT_MAX_PER_DAY = 8
ALERT_SOUND = True

# Refresh a few seconds after each candle close rather than on a fixed
# timer. The close times are known in advance, so waiting a fixed 180s
# after an arbitrary starting point just adds latency for nothing.
ALIGN_POLL_TO_CANDLES  = True
ALIGN_POLL_MINUTES     = 15     # match your execution timeframe
ALIGN_POLL_LAG_SECONDS = 8      # let the feed settle before reading

# Percentage of the session's bars a VWAP must have accumulated before it is
# trustworthy. Below this it is anchored from when the app started, not from
# 09:15, and sits somewhere no other trader's screen shows.
VWAP_MIN_COVERAGE = 70.0

# How much more a FRESH OI addition counts than an existing position when
# ranking level strength: strength = OI + (OI change x this). The multiplier
# is a judgement about how much more recent positioning matters, not a
# measured quantity — but it beats treating a stale wall and a fresh one as
# identical, which is what raw OI does.
FRESH_OI_WEIGHT = 2.0

# ── Guard rails ──────────────────────────────────────────────────────────────
# These encode the rules from your own course notes. The scanner will refuse
# to emit a trade signal when any of these is active.
SUPPRESS_FIRST_MINUTES = 15     # opening OI is chaotic; no signal before 9:30
NO_ENTRY_AFTER         = time(15, 0)   # your course rule: no entries after 3 PM
CONFIRM_SNAPSHOTS      = 2      # a bias flip must hold this many polls
MAX_TRADES_RANGEBOUND  = 3      # Sushen rule: 95% of losses come from
                                # over-trading a sideways market

# Two-trade circuit breaker. If the first two trades of the day both stop out,
# stop. The notes put over 70% of retail capital destruction down to revenge
# trading after 1 PM. Stricter than the rangebound cap above and applies on
# every kind of day.
MAX_STOPS_PER_DAY      = 2

# Midday freeze for OPTION BUYERS. Theta accelerates while volume drops, so a
# fresh naked buy in this window pays maximum decay for minimum movement.
# Fresh directional entries before 10:30 or after 13:30.
# Freeze REMOVED 29-Sep-2026 at Harish's request (it blocked the 12:00 day-high reversal).
# 00:00-00:00 = never inside market hours. To restore: 11:30 / 13:15 / True.
BUYER_FREEZE_FROM      = time(0, 0)
BUYER_FREEZE_UNTIL     = time(0, 0)
ENFORCE_BUYER_FREEZE   = False

# ── Position sizing / risk ───────────────────────────────────────────────────
CAPITAL              = 20_000   # <-- SET THIS to your actual trading capital
MAX_POSITION_PCT     = 30       # warn if one lot costs more than this % of capital
MAX_SPREAD_PCT       = 3.0      # reject a strike if bid-ask spread exceeds this
                                # % of its premium (illiquid = silent bleed)

# ── Regime thresholds ────────────────────────────────────────────────────────
BULL_SCORE       =  3           # bias score at or above this = bullish
BEAR_SCORE       = -3           # at or below this = bearish
CONSOLIDATION_PCT = 0.75        # (R1-S1)/spot below this % = compression
PCR_BULL         = 1.10
PCR_BEAR         = 0.85
PCR_EXTREME_HI   = 1.50         # above this, PCR inverts and reads contrarian
PCR_EXTREME_LO   = 0.55

# ── Price-structure levels (OHLC history) ────────────────────────────────────
# Drop your NIFTY daily OHLC CSV(s) in this folder. Multiple files are fine —
# they get stitched, deduped by date, and later files win on a collision.
OHLC_DIR             = Path(__file__).parent / "data" / "ohlc"
OHLC_FILE            = "OHLC.csv"          # what you drop in each day; any
                                           # other *.csv in the folder is read
                                           # too, so the name is a convention
                                           # rather than a requirement
OHLC_ARCHIVE_FILE    = "_archive_master.csv"
OHLC_KEEP_ARCHIVE    = True   # merge every load into the archive and write it
                              # back, so replacing OHLC.csv daily never loses
                              # older history. Set False to use only what is
                              # physically in the folder.

USE_GANN             = True   # Square-of-9 bands. They sit at a near-constant
                              # % from spot (0.32/0.65/0.97/1.29 for
                              # 45/90/135/180 deg), so they are a percentage
                              # grid, not discovered structure. Weighted
                              # lowest. Set False to drop them entirely.

USE_PIVOTS           = True   # classic floor-trader pivots + Central Pivot
                              # Range from the previous session
CPR_NARROW_PCT       = 0.20   # CPR width below this % of spot = narrow,
                              # which tends to precede a TRENDING day
CPR_WIDE_PCT         = 0.50   # above this = wide, tends to precede a
                              # SIDEWAYS day

CONFLUENCE_TOLERANCE = 30     # points; max gap between a level and its
                              # nearest neighbour in the same zone
MAX_ZONE_WIDTH       = 45     # points; max TOTAL span of a zone. Without this
                              # cap, levels spaced every 30 pts daisy-chain
                              # into one meaningless 500-pt "zone". A zone has
                              # to be tight enough to put a stop behind.
PROXIMITY_POINTS     = 60     # alert when spot comes within this of a zone
LEVEL_NEAR_PTS       = 40     # spot this close to a level = red, decision zone
LEVEL_APPROACH_PTS   = 60     # this close = amber, worth watching
MASTER_PROXIMITY     = 50     # alert when spot nears a master-sheet level
                              # (R1-R4, S1-S4, gaps, Gann, from build_master.py)
RECLAIM_BUFFER       = 15     # points a level must be cleared by to count as
                              # genuinely broken (or reclaimed). Without this,
                              # a retest that merely touches the level reads as
                              # a reclaim and the retest signal destroys itself.
LEVEL_MIN_SEPARATION = 20.0   # points; minimum separation between consecutive stepped pivots
                              # (R1-R4, S1-S4). Levels within 20 pts belong to the same cluster.
GAP_MIN              = 15.0   # points; minimum gap size for NIFTY (noise filter)


# ── Market context (India VIX + institutional flows) ─────────────────────────
USE_VIX              = True
VIX_POLL_WITH_CHAIN  = True   # VIX moves intraday -> same 3-min cadence
VIX_HIGH             = 16.0   # above this: range EXPANSION, violent momentum,
                              # wider straddle boundaries (session thresholds)
VIX_LOW              = 13.0   # below this: low expected range — gaps tend to
                              # fizzle and consolidate rather than run
VIX_SPIKE_PCT        = 6.0    # % up on the day = buying into inflated IV

USE_FLOWS            = True
CRUDE_THRESHOLD      = 6500   # Rs/barrel; View Building #4 boundary. NOTE:
                              # crude has been well above this for months, so
                              # #4 may be scoring a constant -1 and carrying no
                              # information. Worth asking MJ whether the level
                              # should move or the parameter should score on
                              # CHANGE instead.
FII_FLOW_THRESHOLD   = 5000   # contracts; day-on-day change in FII net index
                              # futures beyond this scores +/-2 on View
                              # Building #14. Tune once you have watched a few
                              # weeks of readings.
FLOWS_CACHE_SECONDS  = 6 * 3600   # FII/DII is published ONCE daily after close.
                                  # Polling it every 3 min would be 125
                                  # identical requests for a number that
                                  # changes once. Fetched at startup only.

# ── Price action via Dhan ────────────────────────────────────────────────────
# Token is read from a gitignored .env file, NEVER from this file.
#   DHAN_ACCESS_TOKEN=...
#   DHAN_CLIENT_ID=...
# Dhan tokens expire every 24h by default — paste a fresh one each morning.
# Also enable "Static IP Setting" on the Dhan API page.
USE_PRICE_ACTION       = True

# Where candles come from. "yfinance" is free and needs no auth but carries no
# usable volume, so VWAP is unavailable. "dhan" gives real 1-min candles plus
# futures volume (real VWAP) but the Data API costs Rs499+tax/month — which on
# a Rs20k account is 2.5% of capital monthly. Start free; switch to "dhan"
# once validate.py shows the scanner actually has skill worth paying to feed.
CANDLE_SOURCE          = "dhan"    # "dhan" (paid, real futures VWAP)
                                   # "yfinance" (free, delayed, no volume)
                                   # "nse" (routes via dhan/auto)
YF_SYMBOL              = "^NSEI"       # NIFTY 50 on Yahoo
CANDLE_DAYS_BACK       = 2
DHAN_INDEX_SECURITY_ID = "13"     # NIFTY 50 index, IDX_I segment
DHAN_BANKNIFTY_SECURITY_ID = "25" # BANKNIFTY index, IDX_I segment
DHAN_FUTURES_SECURITY_ID = None   # set this to pin the futures contract
                                  # when the scrip-master matcher picks the
                                  # wrong row. diagnose_dhan.py prints the
                                  # correct id by checking prices.
DHAN_NATIVE_TF         = True     # fetch 5m/15m from Dhan at that interval
                                  # rather than resampling 1m. True = candles
                                  # match TradingView exactly. False = all
                                  # three timeframes guaranteed to agree with
                                  # each other mid-candle. Cannot have both.
PA_POLL_SECONDS        = 60       # 1-min candles fetched every minute; the 5m
                                  # and 15m views are RESAMPLED locally so all
                                  # three always agree with each other
EMA_SPANS              = (8, 10, 20)   # 8/10 are the ones your course uses
ATR_PERIOD             = 14
OPENING_RANGE_MINUTES  = 15

# ── View Building #7 — which oscillator scores ───────────────────────────────
# Your notes say "RSI/Stoch > 80 ... < 20", which reads as either one firing.
# But your TradingView chart runs STOCH RSI (14,14,3,3), an oscillator of an
# oscillator that pins to 0 and 100 constantly — so "either" makes #7 fire on
# most days, often while RSI itself sits mid-range and disagrees.
#   "rsi"      score on RSI only; Stoch RSI shown as context
# CONFIRMED by you 20-Aug: use STOCH RSI on daily. RSI still displayed.
#   "stochrsi" score on Stoch RSI only
#   "either"   either at an extreme triggers it — as literally written
RSI_SCORE_MODE       = "stochrsi"
STOCH_RSI_OVERSOLD   = 20.0
STOCH_RSI_OVERBOUGHT  = 80.0

# ── Validation assumptions (validate.py) ─────────────────────────────────────
ASSUMED_DELTA        = 0.55   # ATM-ish option, for turning index points into
                              # option P&L in the validation report
ROUND_TRIP_COST      = 400.0  # Rs per lot, round trip. Taken from your course
                              # material, which is more conservative than my
                              # earlier 190 estimate and is the figure your own
                              # break-even maths uses. That maths checks out:
                              # net win Rs1,160 vs net loss Rs1,700 on a 40/40
                              # (delta asymmetry) gives break-even at 59.4% —
                              # exactly the 59% the course states. NOT 50%.

# ── Storage ──────────────────────────────────────────────────────────────────
ROOT             = Path(__file__).resolve().parent
BASE_DIR         = ROOT
DATA_DIR         = ROOT / "data"
SNAPSHOT_DIR     = DATA_DIR / "snapshots"
SNAPSHOT_MULTI_DIR = DATA_DIR / "snapshots_multi"
SESSION_LOG      = DATA_DIR / "session_log.csv"
SIGNAL_LOG       = DATA_DIR / "signal_log.csv"
CANDLE_DIR       = DATA_DIR / "candles"      # one CSV per session, 1-min bars
CANDLE_5M_DIR    = DATA_DIR / "candles_5m"   # one CSV per session, 5-min OHLCV + VWAP
CANDLE_15M_DIR   = DATA_DIR / "candles_15m"  # one CSV per session, 15-min bars
CANDLE_BANKNIFTY_DIR = DATA_DIR / "candles_banknifty" # one CSV per session, 1-min BANKNIFTY bars
SCHEDULER_STATE  = DATA_DIR / "scheduler_state.json"
HISTORY_DIR      = DATA_DIR / "history"      # bulk historical intraday for backtests

# ── Network ──────────────────────────────────────────────────────────────────
# NSE blocks datacenter IPs at the WAF. This must run from a residential
# connection. It also throttles aggressively — do not lower these.
REQUEST_TIMEOUT  = 12
MAX_RETRIES      = 3
RETRY_BACKOFF    = 4            # seconds, doubled each retry
MIN_REQUEST_GAP  = 1.0          # never fire requests closer together than this

# Which API shape to use. None = try each and cache whichever answers.
# Set to "v3" or "legacy" once diagnose.py tells you which one works, to skip
# the probing on every start.
API_STYLE  = "v3"   # confirmed working 19-Aug-2026; legacy endpoint now 404s

BASE_URL   = "https://www.nseindia.com"
CHAIN_PAGE = f"{BASE_URL}/option-chain"

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

# NSE restructured their web API into a function-dispatch pattern behind this
# prefix. Captured from DevTools on the live site 19-Aug-2026. Every plain
# /api/<name> guess 404s — the NextApi/apiClient segment is required.
NEXTAPI     = f"{BASE_URL}/api/NextApi/apiClient"
INDEX_TRACKER_API = f"{NEXTAPI}/indexTrackerApi"
INDEX_NAME  = "NIFTY 50"
# Note the doubled && in their query strings: functionName=X&&index=Y
# That is genuinely how NSE builds these. Do not "fix" it.

# ── Optional Telegram alerts ─────────────────────────────────────────────────
# Leave blank to disable. Alerts fire only on STATE CHANGES, never on states.
TELEGRAM_TOKEN   = ""
TELEGRAM_CHAT_ID = ""


# ═══════════════════════════════════════════════════════════════════════════
# Missing-key safety net
# ═══════════════════════════════════════════════════════════════════════════
#
# A single absent setting used to raise AttributeError at import time and
# take down all seventeen tabs — not the one feature that needed the key,
# the entire application. That is a bad trade for a number that has an
# obvious default, and it happens easily: config.py and dashboard.py are
# separate files, so any update that adds a key to one and not the other
# leaves the app dead on the next launch.
#
# PEP 562 module __getattr__ runs only when normal lookup has already
# failed, so nothing above is shadowed and no real setting is overridden.
# Keys genuinely unknown still raise, because silently inventing a value for
# a name nobody defined would hide a typo rather than a missing update.

_FALLBACKS = {
    # risk
    "MAX_STOPS_PER_DAY": 2,
    "CAPITAL": 20000,
    "LOT_SIZE": 65,
    "ROUND_TRIP_COST": 400,
    # session windows
    "BUYER_FREEZE_FROM": time(0, 0),
    "BUYER_FREEZE_UNTIL": time(0, 0),
    "ENFORCE_BUYER_FREEZE": False,
    "MARKET_OPEN": time(9, 15),
    "MARKET_CLOSE": time(15, 30),
    # polling and data
    "POLL_SECONDS": 180,
    "STRIKE_STEP": 50,
    "OHLC_APPEND_AFTER": time(15, 40),
    "OHLC_BACKFILL_DAYS": 30,
    # scoring and levels
    "RSI_SCORE_MODE": "stochrsi",
    "VIX_LOW": 13.0,
    "VIX_HIGH": 16.0,
    "CENTRE_STRIKE_MODE": "live",
    "ATM_WEIGHT_RATIO": 1.5,
    "LEVEL_DOMINANCE_PCT": 8.0,
    "LEVEL_NEAR_PTS": 40,
    "LEVEL_APPROACH_PTS": 60,
    # flow detectors
    "ICEBERG_LAKHS": 5.0,
    "CHURN_RATIO": 3.0,
    "VWAP_MIN_COVERAGE": 70.0,
    "CHAIN_COLOUR_MODE": "ltp",
    # timeline and outcomes
    "TIMELINE_KEEP_DAYS": 30,
    "OUTCOME_HORIZON_MINS": 30,
    "OUTCOME_THRESHOLD_PTS": 20.0,
    "OUTCOME_MIN_SAMPLE": 8,
    # alerting
    "ALERT_COOLDOWN_MINS": 20,
    "ALERT_MAX_PER_DAY": 8,
    "ALERT_SOUND": True,
    # paths
    "ROOT": Path(__file__).resolve().parent,
    "BASE_DIR": Path(__file__).resolve().parent,
}

MISSING_KEYS: list[str] = []


def __getattr__(name: str):
    if name in _FALLBACKS:
        if name not in MISSING_KEYS:
            MISSING_KEYS.append(name)
        return _FALLBACKS[name]
    raise AttributeError(f"config has no setting {name!r}")
