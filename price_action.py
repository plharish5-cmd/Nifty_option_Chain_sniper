"""
Price action: candles, EMAs, VWAP, opening range — and the daily CSV store.

THE FORMING-CANDLE RULE, WHICH MATTERS MOST

If you decide on 5-minute closes, then a 5-minute candle that has not closed
carries no decision. At minute 4 it can look like a textbook hammer and close
as a bearish engulfing. So every timeframe here marks its last candle FORMING,
and every signal and trend read uses CLOSED candles only.

That is the whole reason to poll at 1 minute while deciding on 5 or 15: the
extra polls are not four extra chances to decide, they are visibility into a
candle you are not yet allowed to act on. Acting on an unformed candle is one
of the most reliable ways to lose money with a good setup.

WHY 1-MINUTE ONLY FROM THE API

One call per minute, resampled locally into 5m and 15m. Guarantees the three
views agree with each other. Polling three intervals separately lets them
disagree mid-candle, which is a miserable bug to chase and produces
contradictory screens.

VOLUME AND VWAP

An index has no volume — it is a calculated number, not a traded instrument.
Real NIFTY VWAP is computed from NIFTY FUTURES volume. So candles come from
the index (matching your chart) while VWAP comes from futures. When futures
are unavailable, VWAP is reported as unavailable rather than silently
computed from zeros, which would produce a line indistinguishable from a
simple average and quietly wrong.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime
from pathlib import Path

import numpy as np
import pandas as pd

import config

TIMEFRAMES = {"1m": 1, "5m": 5, "15m": 15, "30m": 30}

import logging
_log = logging.getLogger(__name__)

_WRITES_ENABLED = False


def enable_writes(owner: str) -> None:
    """Called ONCE by scheduler.py at startup. Nothing else."""
    global _WRITES_ENABLED
    _WRITES_ENABLED = True
    _log.info(f"price_action: writes enabled by {owner}")


def disable_writes() -> None:
    """Explicitly disable writes (e.g. for testing)."""
    global _WRITES_ENABLED
    _WRITES_ENABLED = False


def writes_enabled() -> bool:
    return _WRITES_ENABLED


# ═══════════════════════════════════════════════════════════════════════════
# Resampling
# ═══════════════════════════════════════════════════════════════════════════

def resample(df: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """
    1-minute candles -> N-minute candles, anchored to 09:15.

    A `bars` column records how many 1-minute rows actually went into each
    candle. It exists because a resampled candle degrades silently: the
    CLOSE is right as long as any bar near the end of the window survived,
    while the HIGH and LOW are only as good as the coverage. Feed it nine
    minutes out of fifteen and you get a candle whose close matches the
    exchange exactly and whose wicks are too short — which looks like a
    rendering problem and is actually missing data.

    Yahoo's 1-minute feed for Indian index tickers drops bars routinely, so
    this is the normal case rather than the exceptional one. Prefer native
    candles from the source where they are available; use `bars` to know how
    far to trust these when they are not.
    """
    if df.empty:
        return df
    d = df.set_index("timestamp").sort_index()
    agg = {"open": "first", "high": "max", "low": "min",
           "close": "last", "volume": "sum"}
    res = d.resample(f"{minutes}min", origin=d.index[0].normalize()
                     + pd.Timedelta(hours=9, minutes=15), label="left")
    out = res.agg(agg).dropna(subset=["close"])
    out["bars"] = res["close"].count().reindex(out.index).fillna(0).astype(int)
    out["expected_bars"] = minutes
    return out.reset_index()


def coverage_report(df: pd.DataFrame) -> dict:
    """
    How complete are these candles, and which ones are suspect?

    Returns the worst offenders rather than an average, because an average
    of 93% hides the one candle built from four minutes that put a wick in
    the wrong place.
    """
    if df.empty or "bars" not in df.columns:
        return {"known": False}
    exp = int(df["expected_bars"].iloc[0]) if "expected_bars" in df else 0
    if not exp:
        return {"known": False}
    full = int((df["bars"] >= exp).sum())
    pct = df["bars"].sum() / (exp * len(df)) * 100
    thin = df[df["bars"] < exp * 0.75]
    return {
        "known": True, "candles": len(df), "complete": full,
        "coverage_pct": float(pct), "thin": len(thin),
        "worst": [(str(r["timestamp"])[11:16], int(r["bars"]))
                  for _, r in thin.tail(5).iterrows()],
        "trustworthy": pct >= 90 and len(thin) == 0,
    }


def mark_forming(df: pd.DataFrame, minutes: int,
                 now: datetime | None = None) -> pd.DataFrame:
    """
    Flag the last candle as still forming if its window has not elapsed.

    Everything downstream filters on `closed` — this is the single guard that
    stops the app signalling on a candle that can still change its mind.
    """
    if df.empty:
        return df.assign(closed=[])
    now = now or datetime.now()
    d = df.copy()
    end_of_last = d["timestamp"].iloc[-1] + pd.Timedelta(minutes=minutes)
    d["closed"] = True
    if now < end_of_last:
        d.iloc[-1, d.columns.get_loc("closed")] = False
    return d


# ═══════════════════════════════════════════════════════════════════════════
# Indicators
# ═══════════════════════════════════════════════════════════════════════════

def add_emas(df: pd.DataFrame, spans=(8, 10, 20)) -> pd.DataFrame:
    d = df.copy()
    for s in spans:
        d[f"ema{s}"] = d["close"].ewm(span=s, adjust=False).mean()
    return d


def add_vwap_self(df: pd.DataFrame, price: str = "hlc3") -> pd.DataFrame:
    """
    Session-anchored VWAP from the candles' OWN volume.

    Uses the per-bar volume the NSE source now supplies (cumulative day volume
    differenced between polls). Typical price defaults to hlc3, matching the
    "VWAP hlc3 Session" indicator on your TradingView chart.

    HONEST LABEL: this is INDEX volume — the summed traded volume of all 50
    constituents — not NIFTY futures volume. Whether it matches your chart
    depends on what TradingView's feed uses for the index. Compare the two
    once; if they line up, trust it, and if they do not, this is a different
    line and should be read as such rather than assumed to be the VWAP other
    traders are watching.

    Anchoring: VWAP restarts each session, and it can only accumulate from the
    first bar this app actually saw. Start the app at 10:30 and the line is
    anchored at 10:30, which is not VWAP. `vwap_bars` reports how many bars
    fed it so you can tell.
    """
    d = df.copy()
    d["vwap"] = np.nan
    d["vwap_bars"] = 0
    if "volume" not in d or d["volume"].sum() <= 0:
        return d

    tp = ((d["high"] + d["low"] + d["close"]) / 3.0 if price == "hlc3"
          else d["close"])
    day = d["timestamp"].dt.date
    pv = (tp * d["volume"]).groupby(day).cumsum()
    vv = d["volume"].groupby(day).cumsum()
    d["vwap"] = pv / vv.replace(0, np.nan)
    d["vwap_bars"] = d.groupby(day).cumcount() + 1
    return d


@dataclass
class VwapRead:
    timeframe: str
    vwap: float | None
    close: float
    bars: int
    prev_side: str = ""          # above | below | at
    side: str = ""
    crossed: bool = False

    @property
    def distance(self) -> float | None:
        return None if self.vwap is None else self.close - self.vwap

    @property
    def status(self) -> str:
        if self.vwap is None:
            return "no volume yet"
        if self.crossed:
            return f"JUST CROSSED {self.prev_side.upper()} → {self.side.upper()}"
        return f"{self.side.upper()} VWAP"

    @property
    def note(self) -> str:
        if self.vwap is None:
            return "needs per-bar volume — it accumulates as the app runs"
        if self.bars < 10:
            return (f"only {self.bars} bars have fed this VWAP — anchored late, "
                    f"so treat it as indicative")
        d = self.distance
        if abs(d) < 5:
            return "price is sitting ON the line — the decision point"
        return f"{abs(d):,.0f} pts {'above' if d > 0 else 'below'}"


def vwap_read(df: pd.DataFrame, timeframe: str) -> VwapRead:
    """Above, below, or crossing — judged on CLOSED bars only."""
    closed = df[df["closed"]] if "closed" in df else df
    if closed.empty or "vwap" not in closed or closed["vwap"].isna().all():
        c = float(closed["close"].iloc[-1]) if len(closed) else float("nan")
        return VwapRead(timeframe, None, c, 0)

    last = closed.iloc[-1]
    v = float(last["vwap"])
    c = float(last["close"])
    side = "above" if c > v else ("below" if c < v else "at")

    prev_side = side
    if len(closed) > 1:
        p_ = closed.iloc[-2]
        if pd.notna(p_["vwap"]):
            pc, pv_ = float(p_["close"]), float(p_["vwap"])
            prev_side = "above" if pc > pv_ else ("below" if pc < pv_ else "at")

    return VwapRead(timeframe, v, c, int(last.get("vwap_bars", 0)),
                    prev_side, side, crossed=(prev_side != side))


def add_vwap(candles: pd.DataFrame, futures: pd.DataFrame | None) -> pd.DataFrame:
    """
    Session-anchored VWAP from futures volume, mapped onto the index candles.

    Returns the frame with a `vwap` column of NaN when futures volume is
    unavailable — deliberately NOT a volume-less average, which would look
    like a VWAP line and mislead you.
    """
    d = candles.copy()
    d["vwap"] = np.nan
    if futures is None or futures.empty or futures["volume"].sum() <= 0:
        return d

    f = futures.copy()
    f["tp"] = (f["high"] + f["low"] + f["close"]) / 3.0
    f["day"] = f["timestamp"].dt.date
    # Plain transforms rather than groupby.apply — apply returns a frame here
    # and the intent is per-row cumulative sums within each session.
    f["pv"] = f["tp"] * f["volume"]
    f["cum_pv"] = f.groupby("day")["pv"].cumsum()
    f["cum_v"] = f.groupby("day")["volume"].cumsum()
    f["vwap"] = f["cum_pv"] / f["cum_v"].replace(0, np.nan)

    merged = pd.merge_asof(
        d.sort_values("timestamp"),
        f[["timestamp", "vwap"]].sort_values("timestamp"),
        on="timestamp", direction="backward", suffixes=("", "_f"))
    d["vwap"] = merged["vwap_f"].values if "vwap_f" in merged else merged["vwap"].values
    return d


def add_atr(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """
    Average True Range — for stop sizing, not direction.

    On a small account this is the number that should set your position size:
    stop distance in points decides how many lots you can afford to be wrong
    about, and ATR tells you how wide a stop needs to be to survive normal
    noise on this timeframe.
    """
    d = df.copy()
    prev_close = d["close"].shift(1)
    tr = pd.concat([
        d["high"] - d["low"],
        (d["high"] - prev_close).abs(),
        (d["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    d["atr"] = tr.ewm(span=period, adjust=False).mean()
    return d


def opening_range(one_min: pd.DataFrame, minutes: int | None = None) -> dict:
    """
    High and low of the opening window — the day's first real reference.

    A break of the opening range with volume is one of the few genuinely
    mechanical intraday setups: it is where overnight positioning finishes
    resolving and the day's actual participants take over.
    """
    minutes = minutes or config.OPENING_RANGE_MINUTES
    if one_min.empty:
        return {}
    today = one_min["timestamp"].dt.date.max()
    d = one_min[one_min["timestamp"].dt.date == today]
    start = pd.Timestamp.combine(today, dtime(9, 15))
    win = d[(d.timestamp >= start) & (d.timestamp < start + pd.Timedelta(minutes=minutes))]
    if win.empty:
        return {}
    return {"high": float(win.high.max()), "low": float(win.low.min()),
            "minutes": minutes, "complete": len(win) >= minutes}


# ═══════════════════════════════════════════════════════════════════════════
# Trend read per timeframe
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class TrendRead:
    timeframe: str
    close: float
    ema8: float | None
    ema10: float | None
    ema20: float | None
    vwap: float | None
    atr: float | None
    vol_last: float | None = None
    vol_avg: float | None = None
    trend: str = "NO DATA"            # UP | DOWN | FLAT | NO DATA
    above_vwap: bool | None = None
    forming: bool = False
    bars_closed: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def volume_read(self) -> str | None:
        """
        Is the move backed by participation?

        A trend on shrinking volume is drifting; a trend on expanding volume
        has real money behind it. This is the classic confirmation check, and
        it is the one thing the volume field genuinely earns its place for.
        """
        if not self.vol_last or not self.vol_avg or self.vol_avg <= 0:
            return None
        r = self.vol_last / self.vol_avg
        if r >= 1.5:
            return f"volume {r:.1f}x average — move has participation"
        if r <= 0.6:
            return (f"volume {r:.1f}x average — thin. A trend on shrinking "
                    f"volume is drifting, not driving.")
        return f"volume {r:.1f}x average — unremarkable"

    @property
    def summary(self) -> str:
        bits = [f"{self.timeframe}: {self.trend}"]
        if self.above_vwap is not None:
            bits.append("above VWAP" if self.above_vwap else "below VWAP")
        if self.atr:
            bits.append(f"ATR {self.atr:.0f}pt")
        return " · ".join(bits)


def trend_read(df: pd.DataFrame, timeframe: str) -> TrendRead:
    """Reads CLOSED candles only. The forming one is shown, never used."""
    forming = bool(len(df) and not df["closed"].iloc[-1])
    closed = df[df["closed"]]
    if closed.empty:
        return TrendRead(timeframe, np.nan, None, None, None, None, None,
                         "NO DATA", None, forming, 0,
                         ["No closed candle on this timeframe yet."])

    last = closed.iloc[-1]
    e8, e10 = last.get("ema8"), last.get("ema10")
    e20, vw, atr = last.get("ema20"), last.get("vwap"), last.get("atr")
    c = float(last["close"])

    notes: list[str] = []
    if pd.notna(e8) and pd.notna(e10):
        if c > e8 > e10:
            trend = "UP"
        elif c < e8 < e10:
            trend = "DOWN"
        else:
            trend = "FLAT"
            notes.append("Price and the 8/10 EMAs are tangled — no trend to "
                         "trade on this timeframe.")
    else:
        trend = "NO DATA"

    above = bool(c > vw) if pd.notna(vw) else None
    if above is None:
        src = getattr(config, "CANDLE_SOURCE", "yfinance")
        if src == "yfinance":
            notes.append("VWAP unavailable on the free source — Yahoo reports "
                         "no usable volume for Indian index tickers, and real "
                         "NIFTY VWAP needs futures volume. Everything else on "
                         "this tab is unaffected.")
        else:
            notes.append("VWAP unavailable — the futures feed returned no "
                         "volume. Run diagnose_dhan.py to check the futures "
                         "lookup and your Data API subscription.")

    if pd.notna(e8) and abs(c - e8) < (atr or 1e9) * 0.25:
        notes.append(f"Price is sitting on the 8 EMA ({e8:,.0f}) — this is the "
                     f"pullback entry your course rules describe, where the "
                     f"stop is tight enough to size properly.")

    if forming:
        notes.append(f"The current {timeframe} candle is still FORMING. It is "
                     f"shown but excluded from this read — wait for the close.")

    # volume confirmation, when the source supplies real per-bar volume
    vl = va = None
    if "volume" in closed and closed["volume"].sum() > 0:
        vl = float(closed["volume"].iloc[-1])
        va = float(closed["volume"].tail(20).mean())

    r = TrendRead(timeframe, c,
                  float(e8) if pd.notna(e8) else None,
                  float(e10) if pd.notna(e10) else None,
                  float(e20) if pd.notna(e20) else None,
                  float(vw) if pd.notna(vw) else None,
                  float(atr) if pd.notna(atr) else None,
                  vl, va, trend, above, forming, len(closed), notes)
    vr = r.volume_read
    if vr and r.trend in ("UP", "DOWN"):
        r.notes.append(vr.capitalize() if vr[0].islower() else vr)
    return r


def build_all(one_min: pd.DataFrame, futures: pd.DataFrame | None,
              now: datetime | None = None,
              native: dict[str, pd.DataFrame] | None = None
              ) -> dict[str, pd.DataFrame]:
    """
    1-minute candles -> a fully indicator-loaded frame per timeframe.

    `native` may carry exchange-aggregated candles keyed by timeframe label.
    Where one is supplied it is used instead of resampling, because a
    resampled candle is only as good as its 1-minute coverage and Yahoo's
    is routinely incomplete. Each frame records which route it took in
    `attrs["candle_route"]` so the screen can say so rather than leaving you
    to notice the wicks look wrong.
    """
    out: dict[str, pd.DataFrame] = {}
    attrs = dict(one_min.attrs or {})
    native = native or {}
    for label, mins in TIMEFRAMES.items():
        nat = native.get(label)
        if mins == 1:
            d, route = one_min, "source 1m"
        elif nat is not None and not nat.empty:
            d, route = nat.copy(), f"native {label}"
        else:
            d, route = resample(one_min, mins), f"resampled from 1m"
        d.attrs.update(attrs)          # resample drops attrs; carry them over
        d.attrs["candle_route"] = route
        d = add_emas(d)
        d = add_atr(d)
        if futures is not None and not futures.empty:
            d = add_vwap(d, futures if mins == 1 else resample(futures, mins))
        else:
            # no futures feed — fall back to the candles' own (index) volume
            d = add_vwap_self(d)
        out[label] = mark_forming(d, mins, now)
    return out


# ═══════════════════════════════════════════════════════════════════════════
# Cross-check against the OI bias
# ═══════════════════════════════════════════════════════════════════════════

def cross_check(reads: dict[str, TrendRead], regime: str) -> tuple[str, str]:
    """
    Compare the OI-derived regime with what price is actually doing.

    Returns (verdict, explanation). The disagreement cases are the point: OI
    lags price, so when the chain says bullish and price is below VWAP and
    under the 8 EMA on the 5m, the chain is describing positioning that price
    has already moved away from.
    """
    d5 = reads.get("5m")
    d15 = reads.get("15m")
    if not d5 or d5.trend == "NO DATA":
        return "NO DATA", "Not enough closed candles to cross-check yet."

    chain_bull = regime in ("BULLISH", "BREAKOUT_UP")
    chain_bear = regime in ("BEARISH", "BREAKDOWN")
    price_bull = d5.trend == "UP" and (d5.above_vwap is not False)
    price_bear = d5.trend == "DOWN" and (d5.above_vwap is not True)

    align15 = ""
    if d15 and d15.trend in ("UP", "DOWN"):
        align15 = f" 15m structure is {d15.trend}."

    if chain_bull and price_bull:
        return "ALIGNED BULLISH", (
            "OI flow and price agree. This is the setup you want — chain "
            f"positioning bullish, price above VWAP and holding the 8 EMA.{align15}")
    if chain_bear and price_bear:
        return "ALIGNED BEARISH", (
            "OI flow and price agree to the downside — chain bearish, price "
            f"below VWAP and under the 8 EMA.{align15}")
    if (chain_bull and price_bear) or (chain_bear and price_bull):
        return "CONFLICT", (
            "The chain and price disagree. OI LAGS price, so this usually "
            "means positioning describes a move price has already left. "
            f"Default to no trade until they converge.{align15}")
    if chain_bull or chain_bear:
        return "CHAIN ONLY", (
            "The chain has a direction but price is flat — EMAs tangled or "
            "sitting on VWAP. A direction with no price confirmation is a "
            f"reason to watch, not to enter.{align15}")
    return "NEITHER", ("Neither the chain nor price shows a direction. This is "
                       "the majority of the session and the correct response "
                       "is to do nothing.")


# ═══════════════════════════════════════════════════════════════════════════
# Daily CSV store
# ═══════════════════════════════════════════════════════════════════════════

def store_candles(one_min: pd.DataFrame, day: date | None = None) -> Path | None:
    if not _WRITES_ENABLED:
        return None  # read-only process
    """
    Write today's 1-minute candles to data/candles/YYYY-MM-DD.csv.

    Rewritten in full each poll rather than appended: Dhan returns the whole
    session every call, so a rewrite is naturally idempotent and cannot
    duplicate rows or half-write a candle. One file per session keeps them
    easy to hand to validate.py later.

    Only 1-minute is stored. The 5m and 15m views are derived, so storing them
    would be redundant and could drift out of sync with the source.
    """
    if one_min.empty:
        return None
    day = day or one_min["timestamp"].dt.date.max()
    d = one_min[one_min["timestamp"].dt.date == day].copy()
    if d.empty:
        return None

    folder = config.CANDLE_DIR
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{day:%Y-%m-%d}.csv"

    out = d[["timestamp", "open", "high", "low", "close", "volume"]].copy()
    out["timestamp"] = out["timestamp"].dt.strftime("%Y-%m-%d %H:%M:%S")
    out.to_csv(path, index=False)
    return path


def store_5m_candles(five_min: pd.DataFrame, day: date | None = None) -> Path | None:
    if not _WRITES_ENABLED:
        return None  # read-only process
    """
    Write today's completed 5-minute candles to data/candles_5m/YYYY-MM-DD.csv.
    Includes Open, High, Low, Close, Volume, and session VWAP.
    """
    if five_min is None or five_min.empty:
        return None
    d = five_min.copy()
    if "timestamp" not in d.columns and isinstance(d.index, pd.DatetimeIndex):
        d = d.reset_index()
    d["timestamp"] = pd.to_datetime(d["timestamp"], errors="coerce")
    d = d.dropna(subset=["timestamp"])
    if d.empty:
        return None

    day = day or d["timestamp"].dt.date.max()
    d = d[d["timestamp"].dt.date == day].copy()
    if d.empty:
        return None

    folder = getattr(config, "CANDLE_5M_DIR", config.DATA_DIR / "candles_5m")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{day:%Y-%m-%d}.csv"

    cols = ["timestamp", "open", "high", "low", "close", "volume"]
    for extra in ["vwap", "ema8", "ema10", "ema20", "atr", "closed"]:
        if extra in d.columns:
            cols.append(extra)

    out = d[cols].copy()
    out["timestamp"] = out["timestamp"].dt.strftime("%Y-%m-%d %H:%M:%S")
    out.to_csv(path, index=False)
    return path


def load_stored(day: date | None = None) -> pd.DataFrame:
    """Read back a stored session. Used by the Session tab and validate.py."""
    day = day or date.today()
    path = config.CANDLE_DIR / f"{day:%Y-%m-%d}.csv"
    if not path.exists():
        return pd.DataFrame()
    d = pd.read_csv(path)
    d["timestamp"] = pd.to_datetime(d["timestamp"], errors="coerce")
    return d.dropna(subset=["timestamp"])


def load_stored_5m(day: date | None = None) -> pd.DataFrame:
    """Read back a stored 5-minute session from data/candles_5m/."""
    day = day or date.today()
    folder = getattr(config, "CANDLE_5M_DIR", config.DATA_DIR / "candles_5m")
    path = folder / f"{day:%Y-%m-%d}.csv"
    if not path.exists():
        return pd.DataFrame()
    d = pd.read_csv(path)
    d["timestamp"] = pd.to_datetime(d["timestamp"], errors="coerce")
    return d.dropna(subset=["timestamp"])


def store_15m_candles(fifteen_min: pd.DataFrame, day: date | None = None) -> Path | None:
    if not _WRITES_ENABLED:
        return None  # read-only process
    """Write today's completed 15-minute candles to data/candles_15m/YYYY-MM-DD.csv."""
    if fifteen_min is None or fifteen_min.empty:
        return None
    d = fifteen_min.copy()
    if "timestamp" not in d.columns and isinstance(d.index, pd.DatetimeIndex):
        d = d.reset_index()
    d["timestamp"] = pd.to_datetime(d["timestamp"], errors="coerce")
    d = d.dropna(subset=["timestamp"])
    if d.empty:
        return None

    day = day or d["timestamp"].dt.date.max()
    d = d[d["timestamp"].dt.date == day].copy()
    if d.empty:
        return None

    folder = getattr(config, "CANDLE_15M_DIR", config.DATA_DIR / "candles_15m")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{day:%Y-%m-%d}.csv"
    d.to_csv(path, index=False)
    return path


def load_history_candles(days: int = 60, timeframe: str = "1m") -> pd.DataFrame:
    """
    Load up to N days of continuous stored Dhan candles from data/candles,
    data/candles_5m, or data/candles_15m.
    """
    folder = (
        getattr(config, "CANDLE_5M_DIR", config.DATA_DIR / "candles_5m") if timeframe == "5m"
        else getattr(config, "CANDLE_15M_DIR", config.DATA_DIR / "candles_15m") if timeframe == "15m"
        else getattr(config, "CANDLE_DIR", config.DATA_DIR / "candles")
    )
    if not folder.exists():
        return pd.DataFrame()
    files = sorted(folder.glob("20*.csv"))
    if not files:
        return pd.DataFrame()
    selected = files[-days:]
    frames = []
    for f in selected:
        try:
            df = pd.read_csv(f)
            if not df.empty and "timestamp" in df:
                frames.append(df)
        except Exception:
            continue
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    out["timestamp"] = pd.to_datetime(out["timestamp"], errors="coerce")
    return out.dropna(subset=["timestamp", "close"]).sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)



def session_stats(one_min: pd.DataFrame) -> dict:
    """
    Day so far — open, high, low, last, range, and where in it we sit.

    Prefers the exchange snapshot when one is attached (NSE source sets
    candles.attrs["snapshot"]). This matters more than it sounds: NSE's price
    series samples about once a minute, so the extremes derived from it MISS
    the true day high and low — measured on live data the derived low was 59
    points above the real one. Position-in-range and any stop placed off the
    day's low would both be wrong. The snapshot reports the true extremes.
    """
    if one_min.empty:
        return {}
    today = one_min["timestamp"].dt.date.max()
    d = one_min[one_min["timestamp"].dt.date == today]
    if d.empty:
        return {}

    snap = (one_min.attrs or {}).get("snapshot") or {}
    o = float(snap.get("open") or d.open.iloc[0])
    h = float(snap.get("high") or d.high.max())
    l = float(snap.get("low") or d.low.min())
    c = float(snap.get("last") or d.close.iloc[-1])
    src = "exchange" if snap.get("high") else "derived from samples"
    rng = h - l
    return {"open": o, "high": h, "low": l, "last": c, "range": rng,
            "pos_in_range": (c - l) / rng if rng else 0.5,
            "bars": len(d), "from_open": c - o, "extremes_source": src,
            "prev_close": float(snap.get("previousClose") or 0) or None}
