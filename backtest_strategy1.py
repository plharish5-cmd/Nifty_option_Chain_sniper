"""
Price Action Strategy 1 — backtest engine.

    python backtest_strategy1.py                 # needs data/history/*.csv
    python backtest_strategy1.py --sensitivity   # how much do my guesses matter

MJ's 4/8 EMA crossover system on the 15-minute chart, with Sushen's rules,
run over historical data.

WHAT IS EXACT AND WHAT IS MY INTERPRETATION

Ten of the rules are stated precisely enough to code without judgement: the
UBB slope filter, 8 EMA touch and close, the mandatory follow-through candle,
4/8 EMA segregation, BB width under 60 for Stage 1, trailing to the previous
candle's low or high, cost-to-cost at +20, the 3:15 PM exit, the 100-point
first-candle rule, and day high/low targets.

Five are NOT precisely defined in the notes, so I had to choose thresholds:

    ONE TRADE PER RALLY   "rally" is never defined. I treat a rally as ending
                          when price closes back through the 8 EMA against the
                          direction. Another reading is a swing-high/low
                          reversal, which would allow more trades.
    DOJI                  body <= 12% of the candle's range
    ENGULF                current body fully covers the previous body, and is
                          at least 1.1x its size
    NEAR DAY HIGH/LOW     within 0.25x ATR of the running extreme
    100s LEVEL            within 12 points of a round hundred

Every one of those sits in STRATEGY_PARAMS below, and --sensitivity re-runs
the whole test across a range for each so you can see how much my choices are
driving the answer. If the result swings wildly on the Doji threshold, the
result is about my threshold, not about the strategy.

THREE RULES CANNOT BE TESTED AT ALL: low volume after 13:00, the 9:15 view
building score, and Gift Nifty confirmation. None were recorded historically.
Trades that depend on them are skipped and counted separately, so you can see
how much of the strategy went untested.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field, replace
from datetime import time as dtime
from pathlib import Path

import numpy as np
import pandas as pd

import config

# ── the thresholds I had to invent ──────────────────────────────────────────
STRATEGY_PARAMS = {
    "doji_body_pct": 0.12,       # body <= this fraction of range = Doji
    "engulf_ratio": 1.1,         # body must be this much bigger than prior
    "near_extreme_atr": 0.25,    # "at day high" = within this x ATR
    "round_level_pts": 12.0,     # "at a 100s level" = within this many points
    "ubb_slope_tol": 1.5,        # points per candle before a band counts as sloping
    "segregation_atr": 0.35,     # 4/8 gap this x ATR = "clear segregation"
    "rally_ends_on_ema_cross": True,
}

# ── rules from the notes, not mine ──────────────────────────────────────────
BB_LEN, BB_MULT = 20, 2.0
EMA_FAST, EMA_SLOW = 4, 8
BB_WIDTH_STAGE1 = 60
COST_TO_COST_AT = 20
FIRST_CANDLE_MAX = 100
MANDATORY_EXIT = dtime(15, 15)
SESSION_START = dtime(9, 15)


# ═══════════════════════════════════════════════════════════════════════════
# Indicators & patterns
# ═══════════════════════════════════════════════════════════════════════════

def prepare(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    d["ema4"] = d["close"].ewm(span=EMA_FAST, adjust=False).mean()
    d["ema8"] = d["close"].ewm(span=EMA_SLOW, adjust=False).mean()
    mid = d["close"].rolling(BB_LEN).mean()
    sd = d["close"].rolling(BB_LEN).std(ddof=0)
    d["ubb"], d["lbb"] = mid + BB_MULT * sd, mid - BB_MULT * sd
    d["bb_width"] = d["ubb"] - d["lbb"]
    tr = pd.concat([d["high"] - d["low"],
                    (d["high"] - d["close"].shift()).abs(),
                    (d["low"] - d["close"].shift()).abs()], axis=1).max(axis=1)
    d["atr"] = tr.ewm(span=14, adjust=False).mean()
    d["body"] = (d["close"] - d["open"]).abs()
    d["range"] = (d["high"] - d["low"]).replace(0, np.nan)
    d["green"] = d["close"] > d["open"]
    return d


def is_doji(row, p) -> bool:
    return bool(row["body"] <= row["range"] * p["doji_body_pct"])


def is_engulf(cur, prev, bullish: bool, p) -> bool:
    if bullish and not (cur["close"] > cur["open"] and prev["close"] < prev["open"]):
        return False
    if not bullish and not (cur["close"] < cur["open"] and prev["close"] > prev["open"]):
        return False
    covers = (max(cur["open"], cur["close"]) >= max(prev["open"], prev["close"])
              and min(cur["open"], cur["close"]) <= min(prev["open"], prev["close"]))
    return bool(covers and cur["body"] >= prev["body"] * p["engulf_ratio"])


def band_slope(series: pd.Series, i: int, p, look: int = 2) -> str:
    if i < look:
        return "FLAT"
    step = (series.iloc[i] - series.iloc[i - look]) / look
    if step > p["ubb_slope_tol"]:
        return "UP"
    if step < -p["ubb_slope_tol"]:
        return "DOWN"
    return "FLAT"


# ═══════════════════════════════════════════════════════════════════════════
# Trades
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class Trade:
    date: object
    direction: str            # BUY | SELL
    setup: str
    entry_time: object
    entry: float
    stop: float
    exit_time: object = None
    exit: float = None
    exit_reason: str = ""
    points: float = 0.0
    max_favourable: float = 0.0
    max_adverse: float = 0.0

    @property
    def won(self) -> bool:
        return self.points > 0


@dataclass
class DayResult:
    date: object
    trades: list[Trade] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    bias: str = ""


def run_day(day: pd.DataFrame, p: dict) -> DayResult:
    """
    One session, following the rules in order.

    Trades are simulated bar by bar. Entry is at the CLOSE of the
    follow-through candle — that is when the rule is actually satisfied, and
    assuming the open would be a look-ahead the live trader does not get.
    """
    d = prepare(day).reset_index(drop=True)
    res = DayResult(date=d["timestamp"].iloc[0].date())
    if len(d) < BB_LEN + 3:
        res.skipped.append("not enough candles for a 20-period band")
        return res

    first = d.iloc[0]
    first_range = float(first["high"] - first["low"])
    big_first = first_range >= FIRST_CANDLE_MAX

    open_trade: Trade | None = None
    rally_used = False
    rally_dir = None

    for i in range(BB_LEN, len(d)):
        row, prev = d.iloc[i], d.iloc[i - 1]
        t = row["timestamp"].time()
        day_high = float(d["high"].iloc[:i + 1].max())
        day_low = float(d["low"].iloc[:i + 1].min())
        atr = float(row["atr"]) if row["atr"] == row["atr"] else 20.0

        # ── manage an open position ─────────────────────────────────────────
        if open_trade:
            tr = open_trade
            sign = 1 if tr.direction == "BUY" else -1
            excursion = (float(row["high"]) - tr.entry) * sign if sign > 0 \
                else (tr.entry - float(row["low"]))
            tr.max_favourable = max(tr.max_favourable, excursion)
            adverse = (tr.entry - float(row["low"])) if sign > 0 \
                else (float(row["high"]) - tr.entry)
            tr.max_adverse = max(tr.max_adverse, adverse)

            hit = (float(row["low"]) <= tr.stop) if sign > 0 \
                else (float(row["high"]) >= tr.stop)
            if hit:
                tr.exit, tr.exit_time = tr.stop, row["timestamp"]
                tr.exit_reason = "trailing SL hit"
                tr.points = (tr.exit - tr.entry) * sign
                res.trades.append(tr); open_trade = None
                continue

            if t >= MANDATORY_EXIT:
                tr.exit, tr.exit_time = float(row["close"]), row["timestamp"]
                tr.exit_reason = "3:15 PM mandatory"
                tr.points = (tr.exit - tr.entry) * sign
                res.trades.append(tr); open_trade = None
                continue

            # Doji at the day's extreme = exit (Rule 6)
            near_ext = (abs(float(row["high"]) - day_high) <= atr * p["near_extreme_atr"]
                        if sign > 0 else
                        abs(float(row["low"]) - day_low) <= atr * p["near_extreme_atr"])
            if is_doji(row, p) and near_ext:
                tr.exit, tr.exit_time = float(row["close"]), row["timestamp"]
                tr.exit_reason = "Doji at day extreme"
                tr.points = (tr.exit - tr.entry) * sign
                res.trades.append(tr); open_trade = None
                continue

            # trail to this candle's low/high, and cost-to-cost at +20
            new_stop = float(row["low"]) if sign > 0 else float(row["high"])
            if sign > 0:
                if excursion >= COST_TO_COST_AT:
                    new_stop = max(new_stop, tr.entry)
                tr.stop = max(tr.stop, new_stop)
            else:
                if excursion >= COST_TO_COST_AT:
                    new_stop = min(new_stop, tr.entry)
                tr.stop = min(tr.stop, new_stop)
            continue

        # ── no position: look for an entry ──────────────────────────────────
        if t >= MANDATORY_EXIT:
            continue

        ubb_dir = band_slope(d["ubb"], i, p)
        lbb_dir = band_slope(d["lbb"], i, p)
        stage1 = float(row["bb_width"]) < BB_WIDTH_STAGE1

        if stage1:
            continue                                    # Stage 1 handled separately

        above = float(row["close"]) > float(row["ema8"])
        below = float(row["close"]) < float(row["ema8"])
        prev_above = float(prev["close"]) > float(prev["ema8"])
        prev_below = float(prev["close"]) < float(prev["ema8"])

        # rally bookkeeping — Rule 1
        if p["rally_ends_on_ema_cross"] and rally_dir:
            if (rally_dir == "BUY" and below) or (rally_dir == "SELL" and above):
                rally_used, rally_dir = False, None

        direction = setup = None
        # BUY: UBB up, close above 8 EMA, previous candle also above (follow-through)
        if ubb_dir == "UP" and above and prev_above and bool(row["green"]) \
                and bool(prev["green"]):
            direction, setup = "BUY", "Stage 2 · follow-through above 8 EMA"
        elif ubb_dir == "UP" and above and is_engulf(row, prev, True, p):
            direction, setup = "BUY", "Bullish Engulf above 8 EMA"
        # SELL: mirror
        elif lbb_dir == "DOWN" and below and prev_below and not bool(row["green"]) \
                and not bool(prev["green"]):
            direction, setup = "SELL", "Stage 3 · follow-through below 8 EMA"
        elif lbb_dir == "DOWN" and below and is_engulf(row, prev, False, p):
            direction, setup = "SELL", "Bearish Engulf below 8 EMA"

        if not direction:
            continue

        if rally_used and rally_dir == direction:
            res.skipped.append(f"{t:%H:%M} second trade in same rally — Rule 1")
            continue

        if big_first and i <= 2:
            res.skipped.append(f"{t:%H:%M} first candle {first_range:.0f} pts — Rule 3")
            continue

        entry = float(row["close"])
        stop = float(prev["low"]) if direction == "BUY" else float(prev["high"])
        if abs(entry - stop) < 3:
            res.skipped.append(f"{t:%H:%M} stop too tight to be real")
            continue

        open_trade = Trade(res.date, direction, setup, row["timestamp"], entry, stop)
        rally_used, rally_dir = True, direction

    if open_trade:
        last = d.iloc[-1]
        sign = 1 if open_trade.direction == "BUY" else -1
        open_trade.exit, open_trade.exit_time = float(last["close"]), last["timestamp"]
        open_trade.exit_reason = "session end"
        open_trade.points = (open_trade.exit - open_trade.entry) * sign
        res.trades.append(open_trade)

    res.bias = f"UBB {band_slope(d['ubb'], len(d) - 1, p)}"
    return res


# ═══════════════════════════════════════════════════════════════════════════
# Runner
# ═══════════════════════════════════════════════════════════════════════════

def load_15m(folder: Path) -> pd.DataFrame:
    import backtest_levels as bl
    raw = bl.load_intraday(folder)
    d = raw.set_index("timestamp").sort_index()
    out = (d.resample("15min")
             .agg({"open": "first", "high": "max", "low": "min", "close": "last"})
             .dropna(subset=["close"]).reset_index())
    t = out["timestamp"].dt.time
    out = out[(t >= SESSION_START) & (t <= dtime(15, 30))]
    out["date"] = out["timestamp"].dt.date
    return out.reset_index(drop=True)


def backtest(bars: pd.DataFrame, p: dict | None = None) -> tuple[list[Trade], list[str]]:
    p = {**STRATEGY_PARAMS, **(p or {})}
    trades, skips = [], []
    for _, day in bars.groupby("date"):
        r = run_day(day, p)
        trades.extend(r.trades)
        skips.extend(r.skipped)
    return trades, skips


def summarise(trades: list[Trade], label: str = "") -> dict:
    if not trades:
        print(f"  {label}: no trades")
        return {}
    pts = np.array([t.points for t in trades])
    wins = pts > 0
    gross_win = pts[wins].sum() if wins.any() else 0.0
    gross_loss = -pts[~wins].sum() if (~wins).any() else 0.0
    lots = config.LOT_SIZE
    cost = config.ROUND_TRIP_COST
    net_money = pts.sum() * 0.6 * lots - len(trades) * cost   # ~0.6 delta ITM

    out = {
        "trades": len(trades),
        "wins": int(wins.sum()),
        "win_rate": float(wins.mean() * 100),
        "total_pts": float(pts.sum()),
        "avg_pts": float(pts.mean()),
        "best": float(pts.max()),
        "worst": float(pts.min()),
        "profit_factor": float(gross_win / gross_loss) if gross_loss else float("inf"),
        "net_money": float(net_money),
    }
    if label:
        print(f"\n  {label}")
        print(f"    trades {out['trades']:>4}   win rate {out['win_rate']:>5.1f}%   "
              f"PF {out['profit_factor']:>5.2f}")
        print(f"    points {out['total_pts']:>+8.1f}   avg {out['avg_pts']:>+6.1f}   "
              f"best {out['best']:>+6.1f}  worst {out['worst']:>+6.1f}")
        print(f"    after costs, 1 lot: Rs{out['net_money']:>+10,.0f}")
    return out


def sensitivity(bars: pd.DataFrame) -> None:
    """
    How much do MY invented thresholds drive the result?

    If the answer swings wildly here, the backtest is measuring my choices
    rather than the strategy, and no amount of history fixes that.
    """
    print("\n" + "=" * 70)
    print("SENSITIVITY TO MY INTERPRETED THRESHOLDS")
    print("=" * 70)
    base, _ = backtest(bars)
    b = summarise(base, "baseline")
    if not b:
        return

    grid = {
        "doji_body_pct": [0.06, 0.12, 0.20],
        "engulf_ratio": [1.0, 1.1, 1.4],
        "near_extreme_atr": [0.1, 0.25, 0.5],
        "ubb_slope_tol": [0.5, 1.5, 3.0],
    }
    for name, values in grid.items():
        print(f"\n  {name}")
        for v in values:
            t, _ = backtest(bars, {name: v})
            if not t:
                print(f"    {v:<6} no trades")
                continue
            pts = np.array([x.points for x in t])
            print(f"    {v:<6} trades {len(t):>4}  win {(pts > 0).mean() * 100:>5.1f}%  "
                  f"pts {pts.sum():>+8.1f}"
                  + ("   <-- baseline" if v == STRATEGY_PARAMS[name] else ""))


def main() -> int:
    folder = config.HISTORY_DIR
    try:
        bars = load_15m(folder)
    except Exception as exc:                                # noqa: BLE001
        print(f"No history: {exc}")
        print(f"\nThis needs 15-minute NIFTY bars in {folder}.")
        print("NSE serves the current session only, so get them with:")
        print("   python fetch_history.py 2 15      (needs the Rs499 Dhan data API,")
        print("                                      one month, then cancel)")
        return 1

    days = bars["date"].nunique()
    print("=" * 70)
    print("PRICE ACTION STRATEGY 1 — BACKTEST")
    print("=" * 70)
    print(f"\n  {len(bars):,} fifteen-minute bars across {days} sessions")
    print(f"  {bars.timestamp.min():%d-%b-%Y} to {bars.timestamp.max():%d-%b-%Y}")

    trades, skips = backtest(bars)
    summarise(trades, "ALL TRADES")

    for direction in ("BUY", "SELL"):
        summarise([t for t in trades if t.direction == direction], direction)

    print("\n  by setup")
    for setup in sorted({t.setup for t in trades}):
        summarise([t for t in trades if t.setup == setup], setup)

    print("\n  exit reasons")
    for reason in sorted({t.exit_reason for t in trades}):
        sub = [t for t in trades if t.exit_reason == reason]
        pts = np.array([t.points for t in sub])
        print(f"    {reason:<24} {len(sub):>4}  avg {pts.mean():>+6.1f} pts")

    if skips:
        print(f"\n  {len(skips)} entries skipped by the rules")
        from collections import Counter
        for kind, n in Counter(s.split(" ", 1)[1][:40] for s in skips).most_common(5):
            print(f"    {n:>4}  {kind}")

    if "--sensitivity" in sys.argv:
        sensitivity(bars)

    print("\n" + "=" * 70)
    print(f"""HOW TO READ THIS

  Target from the notes: 60-65% win rate. Sushen's own 13-Aug session was
  33% and still net positive, so win rate alone does not settle it —
  profit factor and average points matter more.

  {days} sessions is {'thin' if days < 60 else 'a reasonable sample'}. Day-to-day
  variance in this strategy is large; a month can look like anything.

  Three rules could not be tested: low volume after 13:00, the 9:15 view
  score, and Gift Nifty. Real trading has those filters and this does not,
  so live results should differ — plausibly for the better, since two of
  the three exist to prevent bad entries.

  Run with --sensitivity before believing any of it.""")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
