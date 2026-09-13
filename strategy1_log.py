"""
strategy1_log.py — a rolling paper record of what Strategy 1 actually did.

Strategy 1 recomputes from scratch on every poll. That is correct for a live
read and useless for judging the strategy: at 15:30 the tab shows today, and
tomorrow today is gone. Four weeks of alert-only running would leave you
with exactly as much evidence as one afternoon.

This writes each signal down as it reaches a terminal state and keeps a
rolling window of sessions, so the question "does this actually work" has
something to answer it.

WHY THIS IS BETTER EVIDENCE THAN THE TIMELINE TAB
─────────────────────────────────────────────────
The Timeline's outcome scoring asks whether spot moved twenty points in the
next thirty minutes. That is a proxy — a reasonable one for grading a state
label, and the wrong tool for grading a trade. Strategy 1 produces real
entries, real trailing stops and real exits, so the points here are what the
rules would have made, not whether price drifted the right way.

WHAT IT STILL IS NOT
────────────────────
Paper. Every number below assumes the fill you wanted at the price you
wanted, no slippage, no spread, and a stop that triggers exactly where the
candle says. It also records NIFTY points, not option premium: at a delta of
around 0.65 an ITM option captures roughly two-thirds of a move, and theta
takes some of that back. A +40 point session here is not ₹2,600.

Read it for whether the rules produce more good decisions than bad ones. Do
not read it as a P&L.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

import pandas as pd

import config

DIR = config.DATA_DIR / "strategy1_log"
COLUMNS = ["day", "direction", "armed_ts", "entry_ts", "entry", "trigger",
           "stop0", "exit_ts", "exit_price", "exit_reason", "points",
           "ambiguous", "held_candles", "source"]
KEEP = getattr(config, "TIMELINE_KEEP_DAYS", 30)


def _path(d: date) -> "object":
    return DIR / f"{d.isoformat()}.csv"


def _key(sig) -> str:
    """Identity of a signal within a session — the candle it armed on."""
    return f"{getattr(sig, 'armed_ts', '')}|{getattr(sig, 'direction', '')}"


def load_day(d: date | None = None) -> pd.DataFrame:
    p = _path(d or datetime.now().date())
    if not p.exists():
        return pd.DataFrame(columns=COLUMNS)
    try:
        return pd.read_csv(p)
    except Exception:                                       # noqa: BLE001
        return pd.DataFrame(columns=COLUMNS)


def prune(keep: int = KEEP) -> int:
    """Keep the newest `keep` SESSIONS — files, not calendar days."""
    if not DIR.exists():
        return 0
    dated = []
    for f in DIR.glob("*.csv"):
        try:
            dated.append((date.fromisoformat(f.stem), f))
        except ValueError:
            continue
    dated.sort(reverse=True)
    gone = 0
    for _, f in dated[keep:]:
        f.unlink()
        gone += 1
    return gone


def record(res, d: date | None = None) -> int:
    """
    Write any signal that has reached a terminal state and is not already in.

    Only CLOSED signals are written. An open one still has an exit ahead of
    it, and a record that changes after the fact is not a record — you could
    never tell whether a row was the strategy's decision or a later poll's
    revision of it.

    De-duplication is by arming candle, so re-polling the same session all
    afternoon adds nothing.
    """
    d = d or datetime.now().date()
    # WEEKEND IMMUNITY: Never store strategy 1 logs on Saturday (5) or Sunday (6) no matter what!
    if d.weekday() >= 5:
        return 0
    DIR.mkdir(parents=True, exist_ok=True)
    prune()

    existing = load_day(d)
    have = set()
    if len(existing):
        have = {f"{a}|{b}" for a, b in zip(existing["armed_ts"],
                                           existing["direction"])}

    rows = []
    for s in getattr(res, "signals", []) or []:
        if getattr(s, "exit_i", None) is None:
            continue
        k = f"{getattr(s, 'armed_ts', '')}|{getattr(s, 'direction', '')}"
        if k in have:
            continue
        held = None
        if getattr(s, "entry_i", None) is not None:
            held = s.exit_i - s.entry_i
        rows.append({
            "day": d.isoformat(),
            "direction": s.direction,
            "armed_ts": str(s.armed_ts),
            "entry_ts": str(getattr(s, "entry_ts", "")),
            "entry": getattr(s, "trigger", None),
            "trigger": getattr(s, "trigger", None),
            "stop0": getattr(s, "stop0", None),
            "exit_ts": str(getattr(s, "exit_ts", "")),
            "exit_price": getattr(s, "exit_price", None),
            "exit_reason": getattr(s, "exit_reason", ""),
            "points": getattr(s, "points", None),
            "ambiguous": bool(getattr(s, "ambiguous", False)),
            "held_candles": held,
            "source": "live",
        })

    if rows:
        _append(_path(d), pd.DataFrame(rows))
    return len(rows)


def _append(path, fresh: pd.DataFrame) -> None:
    """
    Append rows without assuming the file's columns match today's code.

    A blind mode="a" append writes whatever columns the new frame happens to
    have. Add one field to this module and every existing file becomes
    unreadable on the next poll — pandas reports "Expected 13 fields, saw
    14" and the whole log is lost, including the live sessions that are the
    only evidence worth having.

    That is not hypothetical: adding `source` did exactly this in testing.
    So the existing header wins. Missing columns are filled, unexpected ones
    dropped, and the file is only rewritten with a wider header when it has
    to be — which happens once, safely, rather than corrupting on the way.
    """
    if not path.exists():
        fresh.to_csv(path, index=False)
        return
    try:
        old = pd.read_csv(path)
    except Exception:                                       # noqa: BLE001
        # unreadable already — start clean rather than compound it
        fresh.to_csv(path, index=False)
        return

    if list(old.columns) == list(fresh.columns):
        fresh.to_csv(path, mode="a", header=False, index=False)
        return

    for c in fresh.columns:
        if c not in old.columns:
            old[c] = "live" if c == "source" else None
    for c in old.columns:
        if c not in fresh.columns:
            fresh[c] = None
    pd.concat([old, fresh[old.columns]], ignore_index=True).to_csv(
        path, index=False)


def load_range(days: int = KEEP) -> pd.DataFrame:
    if not DIR.exists():
        return pd.DataFrame(columns=COLUMNS)
    files = []
    for f in DIR.glob("*.csv"):
        try:
            files.append((date.fromisoformat(f.stem), f))
        except ValueError:
            continue
    files.sort(reverse=True)
    frames = [load_day(d) for d, _ in files[:days]]
    frames = [f for f in frames if len(f)]
    if not frames:
        return pd.DataFrame(columns=COLUMNS)
    return pd.concat(frames, ignore_index=True).sort_values("armed_ts")


def scorecard(days: int = KEEP) -> dict:
    """
    The record, with ambiguous trades set aside rather than counted.

    A 15-minute candle that broke both the trigger and the stop cannot be
    scored — the data does not say which came first, and choosing would
    manufacture a result. Those are excluded from wins, losses and points
    alike, and reported separately so the denominator is never in doubt.

    `enough` is the honest gate on all of it. Below about thirty scored
    trades the win rate is noise: a run of four is unremarkable in either
    direction, and a percentage computed from it will look like knowledge.
    """
    df = load_range(days)
    if df.empty:
        return {"trades": 0, "enough": False,
                "note": "nothing logged yet — run alert-only for a few weeks"}

    df = df.copy()
    df["points"] = pd.to_numeric(df["points"], errors="coerce")
    if "source" not in df.columns:
        df["source"] = "live"
    df["source"] = df["source"].fillna("live")
    back = df[df["source"] == "backfill"]
    df = df[df["source"] != "backfill"]
    if df.empty:
        return {"trades": 0, "enough": False, "backfilled": len(back),
                "note": (f"{len(back)} backtested trades on file and no live "
                         f"ones yet. A backtest cannot fill this gap — the "
                         f"parameters were chosen looking at these same "
                         f"sessions. Run alert-only and come back.")}
    amb = df[df["ambiguous"] == True]                       # noqa: E712
    clean = df[(df["ambiguous"] != True) & df["points"].notna()]  # noqa: E712
    wins = clean[clean["points"] > 0]
    sessions = df["day"].nunique()
    n = len(clean)

    out = {
        "sessions": sessions,
        "trades": len(df),
        "scored": n,
        "ambiguous": len(amb),
        "wins": len(wins),
        "losses": n - len(wins),
        "points": round(float(clean["points"].sum()), 1) if n else 0.0,
        "avg_win": round(float(wins["points"].mean()), 1) if len(wins) else 0.0,
        "avg_loss": (round(float(clean[clean["points"] <= 0]["points"].mean()), 1)
                     if n - len(wins) else 0.0),
        "per_session": round(len(df) / sessions, 1) if sessions else 0.0,
        "enough": n >= 30,
    }
    out["win_rate"] = (f"{len(wins) / n * 100:.0f}%" if out["enough"]
                       else f"{len(wins)}/{n} — too few to rate")

    # Costs are the thing paper records quietly omit.
    lot = getattr(config, "LOT_SIZE", 65)
    cost = getattr(config, "ROUND_TRIP_COST", 400)
    out["backfilled"] = len(back)
    out["cost_drag"] = n * cost
    out["gross_rupees"] = round(out["points"] * 0.65 * lot)
    out["net_rupees"] = out["gross_rupees"] - out["cost_drag"]
    return out
