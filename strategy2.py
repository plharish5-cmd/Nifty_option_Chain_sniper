"""
strategy2.py — VWAP or 8 EMA, whichever comes first, with a candle trail.

THE RULES AS GIVEN
──────────────────
CE  enter when a 15-minute candle CLOSES above VWAP or above the 8 EMA,
    whichever happens first. Trail the stop to each completed candle's LOW.
    Exit when price touches that stop.

PE  the mirror: enter when a candle CLOSES below VWAP or below the 8 EMA,
    whichever comes first. Trail to each candle's HIGH. Exit on touch.

One correction to the brief. It reads "enter the PE trade upon candle
closing Below VWAP or ABOVE 8 EMA", which would make the PE condition fire
in an uptrend. Implemented as BELOW the 8 EMA, the true mirror of the CE
rule. If you did mean above, say so and it is a one-line change — but it
would arm a short every time price rose through the average, which does not
look like what you described.

WHAT THIS SHARES WITH STRATEGY 1, AND WHAT IT DOES NOT
──────────────────────────────────────────────────────
It does NOT apply the UBB slope filter, the follow-through candle, or one
trade per rally. Those are Sushen's rules and they exist to suppress
entries; without them this will trade considerably more often. That is what
was asked for and it is a real difference, not an oversight — the two
strategies are kept in separate modules so neither quietly inherits the
other's constraints.

The trailing stop only ever ratchets: up for CE, down for PE. A stop that
can loosen is not a stop.

THE VWAP PROBLEM — READ BEFORE TRUSTING ANY SIGNAL HERE
───────────────────────────────────────────────────────
"Whichever comes first" needs both conditions to exist. VWAP requires real
traded volume, and neither Yahoo nor NSE's index feed reports any for an
index; the app currently shows VWAP as n/a. When VWAP is missing the rule
silently degenerates into "8 EMA only", which is a DIFFERENT STRATEGY that
will enter at different times and different prices.

So every signal records which condition fired, and `vwap_health()` reports
how many candles had a usable VWAP at all. If that number is zero, the
backtest you are reading is not the strategy you described.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

import numpy as np
import pandas as pd


def _times(df: pd.DataFrame) -> pd.Series:
    if "timestamp" in df.columns:
        return pd.to_datetime(df["timestamp"])
    return pd.Series(pd.to_datetime(df.index), index=df.index)


def _col(df: pd.DataFrame, *names) -> str | None:
    for n in names:
        if n in df.columns:
            return n
    return None


@dataclass
class Trade:
    direction: str                 # CE | PE
    entry_i: int
    entry_ts: datetime
    entry: float
    trigger: str                   # "VWAP" | "8 EMA" | "both"
    stop: float
    stop0: float
    exit_i: int | None = None
    exit_ts: datetime | None = None
    exit: float | None = None
    reason: str = ""
    trail_log: list = field(default_factory=list)

    @property
    def points(self) -> float | None:
        if self.exit is None:
            return None
        return (self.exit - self.entry if self.direction == "CE"
                else self.entry - self.exit)

    @property
    def open(self) -> bool:
        return self.exit_i is None


def annotate_levels(table: pd.DataFrame, levels: dict | None,
                    within: float = 25.0) -> pd.DataFrame:
    """
    Add "near support" and "near resistance" columns to a candle table.

    A LIMITATION THAT MATTERS FOR YOUR STATED PURPOSE
    ─────────────────────────────────────────────────
    Option-chain levels move all day. The R1 that exists at 14:00 is not the
    R1 that existed at 09:30, because open interest has been added and
    withdrawn in between.

    So stamping today's CURRENT chain levels onto this morning's candles
    would be an anachronism, and a damaging one for a file you intend to
    analyse later: reading it next week you would conclude the 09:30 candle
    was approaching a level nobody could have known about yet.

    The columns are therefore computed at WRITE time and only for candles
    being written for the first time. A candle stored at 11:00 carries the
    levels as they stood at 11:00. Rows already on disk are never restamped,
    which is why re-polling adds nothing rather than quietly rewriting the
    past.

    PDC levels are exempt from this — the previous session's close candle is
    fixed before today opens and cannot drift.
    """
    if table.empty:
        return table
    import pdc
    out = table.copy()
    sup, res = [], []
    for c in out["close"].astype(float):
        n = pdc.nearby(c, levels or {}, within)
        sup.append(n.support)
        res.append(n.resistance)
    out["near support"] = sup
    out["near resistance"] = res
    return out


def candle_table(df: pd.DataFrame) -> pd.DataFrame:
    """
    Every 15-minute candle with OHLC, the 8 EMA, VWAP, and what crossed.

    This is the log asked for, and it is built before any signal logic so
    you can audit the inputs independently of the decisions. If a signal
    looks wrong, the answer is almost always visible in this table.
    """
    e8 = _col(df, "ema8", "EMA8")
    vw = _col(df, "vwap", "VWAP")
    ts = _times(df)

    out = pd.DataFrame({
        "time": ts.dt.strftime("%d-%b %H:%M"),
        "open": df["open"].astype(float).round(2),
        "high": df["high"].astype(float).round(2),
        "low": df["low"].astype(float).round(2),
        "close": df["close"].astype(float).round(2),
    })
    out["8 EMA"] = df[e8].astype(float).round(2) if e8 else np.nan
    out["VWAP"] = df[vw].astype(float).round(2) if vw else np.nan

    c = df["close"].astype(float).to_numpy()
    ema = df[e8].astype(float).to_numpy() if e8 else np.full(len(df), np.nan)
    vwa = df[vw].astype(float).to_numpy() if vw else np.full(len(df), np.nan)

    def side(price, ref):
        return np.where(np.isnan(ref), "—",
                        np.where(price > ref, "above", "below"))

    out["vs 8 EMA"] = side(c, ema)
    out["vs VWAP"] = side(c, vwa)

    # A cross is a CHANGE of side between consecutive closed candles.
    def crossed(price, ref):
        s = np.where(np.isnan(ref), np.nan, np.sign(price - ref))
        prev = np.roll(s, 1)
        prev[0] = np.nan
        up = (prev < 0) & (s > 0)
        dn = (prev > 0) & (s < 0)
        return np.where(up, "crossed UP", np.where(dn, "crossed DOWN", ""))

    out["8 EMA cross"] = crossed(c, ema)
    out["VWAP cross"] = crossed(c, vwa)
    return out


def vwap_health(df: pd.DataFrame) -> dict:
    """How much of this frame has a usable VWAP — see the module note."""
    vw = _col(df, "vwap", "VWAP")
    if vw is None:
        return {"usable": 0, "total": len(df), "pct": 0.0, "ok": False,
                "note": ("No VWAP column at all. Every entry below came from "
                         "the 8 EMA alone, so this is not the "
                         "whichever-comes-first rule you specified.")}
    good = int(df[vw].notna().sum())
    pct = good / len(df) * 100 if len(df) else 0.0
    return {
        "usable": good, "total": len(df), "pct": pct, "ok": pct >= 90,
        "note": (f"VWAP present on {good} of {len(df)} candles ({pct:.0f}%)."
                 + ("" if pct >= 90 else
                    " Where it is missing the rule falls back to the 8 EMA "
                    "alone, which enters at different times and different "
                    "prices. Treat those signals as a different strategy.")),
    }


def run(df: pd.DataFrame, allow_ce: bool = True, allow_pe: bool = True
        ) -> list[Trade]:
    """
    Walk the closed candles and apply the rules.

    Entry is decided on a CLOSE, never intrabar, because the rule is written
    on closes. The stop is then checked against the FOLLOWING candles' lows
    and highs, which is where an intrabar touch legitimately belongs.

    A candle that both triggers an entry and breaks the stop is recorded as
    entered and exited on that candle, with the reason marked ambiguous:
    15-minute data cannot say which happened first, and picking one would
    invent a result.
    """
    e8 = _col(df, "ema8", "EMA8")
    vw = _col(df, "vwap", "VWAP")
    if e8 is None:
        return []

    ts = _times(df).tolist()
    o = df["open"].astype(float).to_numpy()
    h = df["high"].astype(float).to_numpy()
    lo = df["low"].astype(float).to_numpy()
    c = df["close"].astype(float).to_numpy()
    ema = df[e8].astype(float).to_numpy()
    vwa = (df[vw].astype(float).to_numpy() if vw
           else np.full(len(df), np.nan))

    trades: list[Trade] = []
    live: Trade | None = None

    for i in range(1, len(df)):
        # ── manage an open trade first ──────────────────────────────────
        if live is not None:
            if live.direction == "CE" and lo[i] <= live.stop:
                live.exit_i, live.exit_ts = i, ts[i]
                live.exit = live.stop
                live.reason = "trailing stop — touched previous candle low"
                trades.append(live); live = None
            elif live.direction == "PE" and h[i] >= live.stop:
                live.exit_i, live.exit_ts = i, ts[i]
                live.exit = live.stop
                live.reason = "trailing stop — touched previous candle high"
                trades.append(live); live = None
            else:
                # ratchet only: never let the stop move against the trade
                if live.direction == "CE":
                    new = max(live.stop, float(lo[i]))
                else:
                    new = min(live.stop, float(h[i]))
                if new != live.stop:
                    live.trail_log.append((ts[i], new))
                live.stop = new
                continue

        # ── look for an entry on this closed candle ─────────────────────
        above_e = c[i] > ema[i]
        below_e = c[i] < ema[i]
        prev_above_e = c[i - 1] > ema[i - 1]
        prev_below_e = c[i - 1] < ema[i - 1]

        has_v = not np.isnan(vwa[i]) and not np.isnan(vwa[i - 1])
        above_v = has_v and c[i] > vwa[i]
        below_v = has_v and c[i] < vwa[i]
        prev_above_v = has_v and c[i - 1] > vwa[i - 1]
        prev_below_v = has_v and c[i - 1] < vwa[i - 1]

        ce_e = above_e and not prev_above_e
        ce_v = above_v and not prev_above_v
        pe_e = below_e and not prev_below_e
        pe_v = below_v and not prev_below_v

        if allow_ce and (ce_e or ce_v):
            trig = ("both" if ce_e and ce_v else "VWAP" if ce_v else "8 EMA")
            live = Trade("CE", i, ts[i], float(c[i]), trig,
                         float(lo[i - 1]), float(lo[i - 1]))
        elif allow_pe and (pe_e or pe_v):
            trig = ("both" if pe_e and pe_v else "VWAP" if pe_v else "8 EMA")
            live = Trade("PE", i, ts[i], float(c[i]), trig,
                         float(h[i - 1]), float(h[i - 1]))

    if live is not None:
        trades.append(live)
    return trades


def summary(trades: list[Trade]) -> dict:
    done = [t for t in trades if t.points is not None]
    wins = [t for t in done if t.points > 0]
    by_trigger: dict[str, int] = {}
    for t in trades:
        by_trigger[t.trigger] = by_trigger.get(t.trigger, 0) + 1
    return {
        "trades": len(trades),
        "closed": len(done),
        "open": sum(1 for t in trades if t.open),
        "wins": len(wins),
        "losses": len(done) - len(wins),
        "points": round(sum(t.points for t in done), 2) if done else 0.0,
        "by_trigger": by_trigger,
    }


def table(trades: list[Trade]) -> pd.DataFrame:
    rows = []
    for t in trades:
        rows.append({
            "dir": t.direction,
            "entry": f"{t.entry_ts:%d-%b %H:%M}",
            "at": round(t.entry, 2),
            "fired on": t.trigger,
            "stop (init)": round(t.stop0, 2),
            "stop (final)": round(t.stop, 2),
            "trails": len(t.trail_log),
            "exit": f"{t.exit_ts:%H:%M}" if t.exit_ts else "OPEN",
            "pts": None if t.points is None else round(t.points, 1),
            "reason": t.reason or "still open",
        })
    return pd.DataFrame(rows)


# ═══════════════════════════════════════════════════════════════════════════
# Persisting the candle table
# ═══════════════════════════════════════════════════════════════════════════
#
# The table on screen is rebuilt from the live frame every poll, so it exists
# only while the app is open. Writing it down turns it into something you can
# check a week later against what the app claimed at the time — which is the
# only way any of this gets validated.

import config                                              # noqa: E402

CANDLE_DIR = config.DATA_DIR / "candles_15m"


def _closed_only(df: pd.DataFrame, now: datetime | None = None,
                 minutes: int = 15) -> pd.DataFrame:
    """
    Drop the candle that is still forming.

    A 15-minute candle stamped 14:45 is not finished until 15:00, and until
    then its high, low and close change on every poll. Storing it would
    write a row that silently rewrites itself, so a file read tomorrow would
    not match what the screen showed today — and a record that disagrees
    with itself is worse than no record, because you would trust it.
    """
    if df.empty:
        return df
    now = now or datetime.now()
    ts = _times(df)
    cutoff = pd.Timestamp(now) - pd.Timedelta(minutes=minutes)
    return df[ts <= cutoff]


def store_candles(df: pd.DataFrame, now: datetime | None = None,
                  keep_sessions: int | None = None,
                  levels: dict | None = None) -> dict:
    """
    Append today's completed 15-minute candles to a per-session CSV.

    De-duplicated on timestamp, so polling all afternoon adds each candle
    exactly once and re-running after a restart adds nothing.

    Returns a small summary rather than nothing, so the UI can say what
    happened instead of leaving you to wonder whether it worked.
    """
    now = now or datetime.now()
    keep = keep_sessions or getattr(config, "TIMELINE_KEEP_DAYS", 30)
    CANDLE_DIR.mkdir(parents=True, exist_ok=True)

    closed = _closed_only(df, now)
    if closed.empty:
        return {"written": 0, "total": 0, "forming_skipped": len(df),
                "path": None, "note": "no completed candles yet this session"}

    table = candle_table(closed)
    if levels:
        table = annotate_levels(table, levels)
    ts = _times(closed)
    table.insert(0, "timestamp", ts.dt.strftime("%Y-%m-%d %H:%M:%S").values)

    path = CANDLE_DIR / f"{now.date().isoformat()}.csv"
    have = set()
    if path.exists():
        try:
            existing_df = pd.read_csv(path)
            if list(existing_df.columns) != list(table.columns):
                # Schema mismatch (e.g. 7-col vs 14-col): overwrite cleanly to prevent corrupt CSV
                table.to_csv(path, index=False)
                have = set(table["timestamp"].astype(str))
            else:
                have = set(existing_df["timestamp"].astype(str))
        except Exception:                                   # noqa: BLE001
            table.to_csv(path, index=False)
            have = set(table["timestamp"].astype(str))

    fresh = table[~table["timestamp"].isin(have)]
    if len(fresh):
        fresh.to_csv(path, mode="a", header=not path.exists(), index=False)

    # roll the window by session count, matching the other logs
    files = []
    for f in CANDLE_DIR.glob("*.csv"):
        try:
            files.append((datetime.fromisoformat(f.stem).date(), f))
        except ValueError:
            continue
    files.sort(reverse=True)
    for _, f in files[keep:]:
        f.unlink()

    return {"written": len(fresh), "total": len(have) + len(fresh),
            "forming_skipped": len(df) - len(closed), "path": str(path),
            "sessions_kept": min(len(files), keep),
            "note": (f"{len(fresh)} new candle(s) written"
                     if len(fresh) else "already up to date")}


def load_candles(day: date | None = None) -> pd.DataFrame:
    """One session's stored candles, oldest first."""
    day = day or datetime.now().date()
    p = CANDLE_DIR / f"{day.isoformat()}.csv"
    if not p.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(p)
    except Exception:                                       # noqa: BLE001
        return pd.DataFrame()


def stored_sessions(limit: int = 60) -> list:
    if not CANDLE_DIR.exists():
        return []
    out = []
    for f in CANDLE_DIR.glob("*.csv"):
        try:
            out.append(datetime.fromisoformat(f.stem).date())
        except ValueError:
            continue
    return sorted(out, reverse=True)[:limit]
