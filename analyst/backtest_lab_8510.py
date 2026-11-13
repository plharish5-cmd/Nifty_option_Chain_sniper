"""Backtest Lab for the 8510 strategies.

WHY THE OLD BACKTESTER "NEVER WORKED"
    The old page asked the live engine to run the whole date range inside one
    web request, on the engine's own event loop. A multi-month run froze the
    desk (telemetry, scanner, exits) until it finished, the browser gave up
    waiting, and nothing came back. It also mixed real ledger rows with
    simulated ones and reported rupees built from an assumed option delta.

HOW THIS ONE RUNS
    * A run is a JOB in its own process. The live engine only starts it and
      reads its progress file. The job builds its own backend object on a
      private copy of the config with a throw-away ledger, opens the market
      database read-only, and can neither place an order nor touch the live
      desk's state.
    * It walks the range month by month through the app's own
      `run_date_range_backtest` (no second implementation of any strategy),
      so the cockpit shows progress and a run can be cancelled.
    * Everything is reported in NIFTY SPOT POINTS, per trade, after a cost
      per trade that you set. No rupee figure is invented.

WHAT IT REPORTS, AND WHY EACH NUMBER IS THERE
    net / average / win rate / profit factor     the usual
    t-stat and a 95% range for the average       is the average distinguishable from zero, or luck?
    max drawdown, longest losing run, worst day  what you must be able to sit through
    by lane, month, weekday, entry hour, exit    where the result comes from, and whether one month carries it
    HOLD-OUT                                     the last part of the range (default 30% of the days) is kept
                                                 apart. The verdict is read from those days only.
    ambiguous trades                             target and stop inside the same 1-minute candle; booked as
                                                 stops, and the total "if they had been targets" is shown.
    coverage                                     which weekdays in the range have no candles at all.

VERDICT RULE (fixed before any run)
    NOT ENOUGH DATA   fewer than 30 hold-out trades
    PASS              hold-out net > 0 after costs AND hold-out t-stat >= 2 AND the earlier part is also > 0
    WEAK              hold-out net > 0 but t-stat < 2 (could be luck), or the earlier part lost
    FAIL              hold-out net <= 0 after costs

LIMITS YOU SHOULD KNOW
    * Entries and exits are simulated on 1-minute spot candles. Inside a minute the
      order of high and low is unknown.
    * Option premium is not simulated: "40 option points" exits are not tested, only
      the spot side. Slippage is whatever you put in the cost box.
    * The 8600 Board filters and the 8600 Ticket lane are not in the backtest: there
      is no recorded history of those readings long enough to replay.

CLI
    python3 -m analyst.backtest_lab_8510 --from 2026-06-01 --to 2026-11-12
    python3 -m analyst.backtest_lab_8510 --job data/terminal8510/backtests/<id>.spec.json
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any, Callable, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
IST = timezone(timedelta(hours=5, minutes=30))
JOBS_DIR = ROOT / "data/terminal8510/backtests"

LANES: List[Dict[str, str]] = [
    {"id": "EMA8_PROTOTYPE", "label": "8 EMA Prototype", "group": "Equity Guys"},
    {"id": "STAGE_2", "label": "Stage 2 8-EMA (Long)", "group": "Equity Guys"},
    {"id": "STAGE_3", "label": "Stage 3 8-EMA (Short)", "group": "Equity Guys"},
    {"id": "CROSSOVER", "label": "8 EMA Reversal", "group": "Equity Guys"},
    {"id": "FLAGSHIP", "label": "1 HR Flagship", "group": "Equity Guys"},
    {"id": "PA_SR", "label": "PA @ S/R (homework)", "group": "Others"},
]
LANE_IDS = [x["id"] for x in LANES]
LANE_LABEL = {x["id"]: x["label"] for x in LANES}
# one lane -> the engine's own filter word (saves time); several lanes -> "ALL", then filtered here
ENGINE_FILTER = {"EMA8_PROTOTYPE": "EMA8_PROTOTYPE", "STAGE_2": "STAGE_2_LONG", "STAGE_3": "STAGE_3_SHORT",
                 "CROSSOVER": "CROSSOVER", "FLAGSHIP": "H1_FLAGSHIP", "PA_SR": "PA_SR_HOMEWORK"}

DEFAULT_SPEC: Dict[str, Any] = {
    "lanes": ["EMA8_PROTOTYPE", "STAGE_2", "STAGE_3", "CROSSOVER", "FLAGSHIP"],
    "cost_pts": 2.0,
    "holdout_frac": 0.30,
    "min_holdout_trades": 30,
    "pass_t_stat": 2.0,
}


# ── trades ───────────────────────────────────────────────────────────────────
def lane_of(strategy_id: Any, setup: Any = "") -> str:
    s = f"{strategy_id or ''} {setup or ''}".upper()
    if "EMA8_PROTOTYPE" in s or "8 EMA PROTOTYPE" in s:
        return "EMA8_PROTOTYPE"
    if "PA_SR" in s or "HOMEWORK" in s:
        return "PA_SR"
    if "STAGE_2" in s or "STAGE 2" in s:
        return "STAGE_2"
    if "STAGE_3" in s or "STAGE 3" in s:
        return "STAGE_3"
    if "CROSSOVER" in s or "REVERSAL" in s:
        return "CROSSOVER"
    if "FLAGSHIP" in s or "H1_8EMA" in s:
        return "FLAGSHIP"
    return "OTHER"


def _f(v: Any) -> Optional[float]:
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def normalize_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The engine's backtest rows -> the small trade record the lab works with."""
    out: List[Dict[str, Any]] = []
    for r in rows or []:
        pts = _f(r.get("st_pnl_pts"))
        day = str(r.get("entry_day") or r.get("date_only") or "")[:10]
        if pts is None or len(day) != 10:
            continue
        direc = str(r.get("Direction") or r.get("direction") or "").upper()
        short = ("SHORT" in direc or direc in ("PE", "PUT")) and "LONG" not in direc
        out.append({
            "day": day,
            "entry_hm": str(r.get("entry_hm") or "")[:5],
            "exit_day": str(r.get("exit_day") or day)[:10],
            "exit_hm": str(r.get("exit_hm") or "")[:5],
            "lane": lane_of(r.get("strategy_id"), r.get("Setup_Name")),
            "sid": str(r.get("strategy_id") or ""),
            "setup": str(r.get("Setup_Name") or "")[:80],
            "side": "PE" if short else "CE",
            "entry": _f(r.get("Index_Entry")),
            "exit": _f(r.get("Index_Exit")),
            "gross": round(pts, 2),
            "reason": str(r.get("exit_reason") or r.get("Exit_Reason") or "?"),
            "ambiguous": bool(r.get("is_ambiguous")),
            "target_pts": _f(r.get("spot_target_pts")) or 40.0,
        })
    out.sort(key=lambda t: (t["day"], t["entry_hm"], t["lane"]))
    return out


# ── statistics ───────────────────────────────────────────────────────────────
def _ci95_of_mean(net: List[float], seed: int = 8510, rounds: int = 1000) -> Optional[List[float]]:
    """Bootstrap 95% range for the average net points per trade. None under 10 trades."""
    n = len(net)
    if n < 10:
        return None
    rng = random.Random(seed)
    means = []
    for _ in range(rounds):
        s = 0.0
        for _ in range(n):
            s += net[rng.randrange(n)]
        means.append(s / n)
    means.sort()
    return [round(means[int(0.025 * rounds)], 2), round(means[int(0.975 * rounds) - 1], 2)]


def stats(trades: List[Dict[str, Any]], cost_pts: float) -> Dict[str, Any]:
    """Everything about one set of trades, in spot points after `cost_pts` per trade."""
    n = len(trades)
    out: Dict[str, Any] = {
        "trades": n, "wins": 0, "losses": 0, "win_rate_pct": None, "gross_pts": 0.0, "cost_pts_total": 0.0,
        "net_pts": 0.0, "avg_net_pts": None, "median_net_pts": None, "avg_win_pts": None, "avg_loss_pts": None,
        "profit_factor": None, "t_stat": None, "ci95_avg": None, "max_drawdown_pts": 0.0, "longest_losing_run": 0,
        "best_trade_pts": None, "worst_trade_pts": None, "days_traded": 0, "green_days": 0, "green_days_pct": None,
        "trades_per_day": None, "best_day": None, "worst_day": None, "ambiguous": 0,
        "net_if_ambiguous_were_targets": None,
    }
    if n == 0:
        return out
    net = [t["gross"] - cost_pts for t in trades]
    wins = [x for x in net if x > 0]
    losses = [x for x in net if x <= 0]
    mean = sum(net) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in net) / (n - 1)) if n > 1 else 0.0
    run = peak = dd = 0.0
    streak = worst_streak = 0
    for x in net:
        run += x
        peak = max(peak, run)
        dd = max(dd, peak - run)
        streak = streak + 1 if x <= 0 else 0
        worst_streak = max(worst_streak, streak)
    by_day: Dict[str, float] = {}
    for t, x in zip(trades, net):
        by_day[t["day"]] = by_day.get(t["day"], 0.0) + x
    days = sorted(by_day)
    amb = [t for t in trades if t.get("ambiguous")]
    amb_swing = sum(float(t.get("target_pts") or 40.0) - t["gross"] for t in amb)
    gl = abs(sum(losses))
    best_d = max(days, key=lambda d: by_day[d])
    worst_d = min(days, key=lambda d: by_day[d])
    out.update(
        wins=len(wins), losses=len(losses), win_rate_pct=round(100.0 * len(wins) / n, 1),
        gross_pts=round(sum(t["gross"] for t in trades), 1), cost_pts_total=round(cost_pts * n, 1),
        net_pts=round(sum(net), 1), avg_net_pts=round(mean, 2), median_net_pts=round(median(net), 2),
        avg_win_pts=round(sum(wins) / len(wins), 2) if wins else None,
        avg_loss_pts=round(sum(losses) / len(losses), 2) if losses else None,
        profit_factor=round(sum(wins) / gl, 2) if gl > 0 else None,
        t_stat=round(mean / (sd / math.sqrt(n)), 2) if sd > 0 and n > 1 else None,
        ci95_avg=_ci95_of_mean(net),
        max_drawdown_pts=round(dd, 1), longest_losing_run=worst_streak,
        best_trade_pts=round(max(net), 1), worst_trade_pts=round(min(net), 1),
        days_traded=len(days), green_days=sum(1 for d in days if by_day[d] > 0),
        green_days_pct=round(100.0 * sum(1 for d in days if by_day[d] > 0) / len(days), 1),
        trades_per_day=round(n / len(days), 2),
        best_day={"day": best_d, "net_pts": round(by_day[best_d], 1)},
        worst_day={"day": worst_d, "net_pts": round(by_day[worst_d], 1)},
        ambiguous=len(amb), net_if_ambiguous_were_targets=round(sum(net) + amb_swing, 1) if amb else None,
    )
    return out


def breakdown(trades: List[Dict[str, Any]], cost_pts: float, key: Callable[[Dict[str, Any]], str],
              order: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for t in trades:
        k = key(t)
        if k:
            groups.setdefault(k, []).append(t)
    keys = [k for k in (order or []) if k in groups] + sorted(k for k in groups if k not in (order or []))
    rows = []
    for k in keys:
        s = stats(groups[k], cost_pts)
        rows.append({"key": k, "trades": s["trades"], "win_rate_pct": s["win_rate_pct"], "net_pts": s["net_pts"],
                     "avg_net_pts": s["avg_net_pts"], "profit_factor": s["profit_factor"], "t_stat": s["t_stat"],
                     "max_drawdown_pts": s["max_drawdown_pts"]})
    return rows


def equity_curve(trades: List[Dict[str, Any]], cost_pts: float, max_points: int = 600) -> List[Dict[str, Any]]:
    """Running net total, one point per trade (thinned to at most `max_points`)."""
    run = 0.0
    pts = [{"i": 0, "day": trades[0]["day"] if trades else "", "net": 0.0}]
    for i, t in enumerate(trades, 1):
        run += t["gross"] - cost_pts
        pts.append({"i": i, "day": t["day"], "net": round(run, 1)})
    if len(pts) <= max_points:
        return pts
    step = len(pts) / float(max_points)
    keep = sorted({int(k * step) for k in range(max_points)} | {len(pts) - 1})
    return [pts[k] for k in keep]


def split_days(days: List[str], holdout_frac: float) -> Tuple[List[str], List[str]]:
    """Earlier days, held-out later days. Never splits inside a day."""
    days = sorted(days)
    frac = min(0.8, max(0.0, float(holdout_frac or 0.0)))
    k = int(round(len(days) * frac))
    if frac <= 0 or k <= 0 or k >= len(days):
        return days, []
    return days[:-k], days[-k:]


def verdict(earlier: Dict[str, Any], held: Dict[str, Any], spec: Dict[str, Any]) -> Dict[str, Any]:
    need = int(spec.get("min_holdout_trades", 30))
    t_need = float(spec.get("pass_t_stat", 2.0))
    n, net, t = int(held.get("trades") or 0), float(held.get("net_pts") or 0.0), held.get("t_stat")
    if n < need:
        return {"grade": "NOT ENOUGH DATA", "tone": "neutral",
                "text": f"Only {n} trades on the held-out days; {need} are needed before the result means anything. "
                        "Use a longer range."}
    if net <= 0:
        return {"grade": "FAIL", "tone": "bad",
                "text": f"On the held-out days it lost {abs(net):.1f} points after costs over {n} trades. "
                        "As configured, this does not have an edge on days it was not looked at."}
    if (t is not None and t >= t_need) and float(earlier.get("net_pts") or 0.0) > 0:
        return {"grade": "PASS", "tone": "good",
                "text": f"Held-out days: +{net:.1f} points after costs over {n} trades, t-stat {t:.2f} (≥ {t_need:g}), "
                        "and the earlier days were positive too. The result is unlikely to be luck alone."}
    why = []
    if t is None or t < t_need:
        why.append(f"t-stat {t if t is not None else '—'} is under {t_need:g}, so the profit is within what luck produces")
    if float(earlier.get("net_pts") or 0.0) <= 0:
        why.append("the earlier days lost money, so the two halves disagree")
    return {"grade": "WEAK", "tone": "warn",
            "text": f"Held-out days: +{net:.1f} points after costs over {n} trades, but " + "; and ".join(why) + "."}


def build_report(trades: List[Dict[str, Any]], spec: Dict[str, Any], coverage: Dict[str, Any]) -> Dict[str, Any]:
    cost = float(spec.get("cost_pts", 2.0))
    days_with_data = list(coverage.get("days") or sorted({t["day"] for t in trades}))
    earlier_days, held_days = split_days(days_with_data, float(spec.get("holdout_frac", 0.30)))
    held_set = set(held_days)
    earlier = [t for t in trades if t["day"] not in held_set]
    held = [t for t in trades if t["day"] in held_set]
    s_all, s_earlier, s_held = stats(trades, cost), stats(earlier, cost), stats(held, cost)
    weekday = lambda t: ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][date.fromisoformat(t["day"]).weekday()]
    by_day: Dict[str, Dict[str, Any]] = {}
    for t in trades:
        d = by_day.setdefault(t["day"], {"day": t["day"], "trades": 0, "net_pts": 0.0, "held_out": t["day"] in held_set})
        d["trades"] += 1
        d["net_pts"] = round(d["net_pts"] + t["gross"] - cost, 1)
    lanes_run = [l for l in LANE_IDS if l in (spec.get("lanes") or [])]
    per_lane = []
    for l in lanes_run:
        lt = [t for t in trades if t["lane"] == l]
        le, lh = [t for t in lt if t["day"] not in held_set], [t for t in lt if t["day"] in held_set]
        sa, se, sh = stats(lt, cost), stats(le, cost), stats(lh, cost)
        per_lane.append({"lane": l, "label": LANE_LABEL.get(l, l), "all": sa, "earlier": se, "held_out": sh,
                         "verdict": verdict(se, sh, spec) if held_days else None})
    return {
        "spec": spec, "coverage": {k: v for k, v in coverage.items() if k != "days"},
        "split": {"earlier_days": len(earlier_days), "held_out_days": len(held_days),
                  "earlier_from": earlier_days[0] if earlier_days else None, "earlier_to": earlier_days[-1] if earlier_days else None,
                  "held_from": held_days[0] if held_days else None, "held_to": held_days[-1] if held_days else None,
                  "split_trade_index": len(earlier)},
        "all": s_all, "earlier": s_earlier, "held_out": s_held,
        "verdict": verdict(s_earlier, s_held, spec) if held_days else
                   {"grade": "NO HOLD-OUT", "tone": "neutral", "text": "No days were held out, so nothing here is a test."},
        "per_lane": per_lane,
        "by_month": breakdown(trades, cost, lambda t: t["day"][:7]),
        "by_weekday": breakdown(trades, cost, weekday, ["Mon", "Tue", "Wed", "Thu", "Fri"]),
        "by_hour": breakdown(trades, cost, lambda t: (t["entry_hm"][:2] + ":00") if t["entry_hm"] else ""),
        "by_exit": breakdown(trades, cost, lambda t: t["reason"]),
        "by_side": breakdown(trades, cost, lambda t: t["side"], ["CE", "PE"]),
        "equity": equity_curve(trades, cost),
        "days": sorted(by_day.values(), key=lambda d: d["day"]),
    }


# ── market data (read-only) ──────────────────────────────────────────────────
def _ro(db: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)


def trading_days(market_db: Path, from_day: str = "", to_day: str = "") -> List[str]:
    """Days that have NIFTY spot 1-minute candles."""
    if not Path(market_db).exists():
        return []
    with _ro(Path(market_db)) as conn:
        rows = conn.execute(
            "SELECT DISTINCT date(ts,'unixepoch','+330 minutes') d FROM candles_1m WHERE sec_id='13' ORDER BY d"
        ).fetchall()
    days = [r[0] for r in rows if r[0]]
    return [d for d in days if (not from_day or d >= from_day) and (not to_day or d <= to_day)]


def coverage_of(market_db: Path, from_day: str, to_day: str) -> Dict[str, Any]:
    days = trading_days(market_db, from_day, to_day)
    have = set(days)
    missing: List[str] = []
    try:
        d, end = date.fromisoformat(from_day), date.fromisoformat(to_day)
        while d <= end:
            if d.weekday() < 5 and d.isoformat() not in have:
                missing.append(d.isoformat())
            d += timedelta(days=1)
    except ValueError:
        pass
    return {"from": from_day, "to": to_day, "days_with_data": len(days), "first_day": days[0] if days else None,
            "last_day": days[-1] if days else None, "weekdays_without_data": len(missing),
            "missing_sample": missing[:12], "days": days,
            "note": "Weekdays without data include market holidays; a long run of them means candles were never pulled."}


def month_chunks(days: List[str]) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    for d in days:
        if out and out[-1][0][:7] == d[:7]:
            out[-1] = (out[-1][0], d)
        else:
            out.append((d, d))
    return out


def day_chart(market_db: Path, day: str) -> Dict[str, Any]:
    """15-minute candles of one day with the 4 and 8 EMA (seeded from the days before)."""
    import pandas as pd
    from analyst.nse_spot_candles_8510 import ema4_ema8, epoch_seconds, resample_nse_15m

    if not Path(market_db).exists():
        return {"ok": False, "error": "market database not found"}
    start = (date.fromisoformat(day) - timedelta(days=12)).isoformat()
    with _ro(Path(market_db)) as conn:
        df = pd.read_sql_query(
            "SELECT ts, o AS open, h AS high, l AS low, c AS close, v AS volume FROM candles_1m WHERE sec_id='13' "
            "AND date(ts,'unixepoch','+330 minutes') BETWEEN ? AND ? ORDER BY ts", conn, params=(start, day))
    if df.empty:
        return {"ok": False, "error": f"no candles for {day}"}
    df["dt"] = pd.to_datetime(df["ts"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
    hm = df["dt"].dt.strftime("%H:%M")
    df = df[(hm >= "09:15") & (hm <= "15:30")]
    b = resample_nse_15m(df.set_index("dt"))
    if b.empty:
        return {"ok": False, "error": f"no session candles for {day}"}
    e4, e8 = ema4_ema8(b["close"])
    b = b.assign(e4=e4, e8=e8)
    b = b[b.index.strftime("%Y-%m-%d") == day]
    if b.empty:
        return {"ok": False, "error": f"no candles for {day}"}
    ts = epoch_seconds(b.index)
    candles = [{"t": int(t), "hm": idx.strftime("%H:%M"), "o": round(float(r.open), 2), "h": round(float(r.high), 2),
                "l": round(float(r.low), 2), "c": round(float(r.close), 2), "e4": round(float(r.e4), 2),
                "e8": round(float(r.e8), 2)} for t, (idx, r) in zip(ts, b.iterrows())]
    return {"ok": True, "day": day, "candles": candles}


# ── job files ────────────────────────────────────────────────────────────────
def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, default=str), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def clean_spec(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Validate what the page sent. Raises ValueError with a message for the user."""
    spec = dict(DEFAULT_SPEC)
    f, t = str(raw.get("from") or raw.get("from_date") or "")[:10], str(raw.get("to") or raw.get("to_date") or "")[:10]
    try:
        df, dt = date.fromisoformat(f), date.fromisoformat(t)
    except ValueError:
        raise ValueError("Give a From and a To date (YYYY-MM-DD).")
    if dt < df:
        raise ValueError("The To date is before the From date.")
    given = raw.get("lanes")
    source = given if given is not None else spec["lanes"]
    lanes = [str(x).upper() for x in source if str(x).upper() in LANE_IDS]
    if not lanes:
        raise ValueError("Pick at least one strategy.")
    try:
        cost = float(raw.get("cost_pts", spec["cost_pts"]))
        hold = float(raw.get("holdout_frac", spec["holdout_frac"]))
    except (TypeError, ValueError):
        raise ValueError("Cost and hold-out must be numbers.")
    if not (0.0 <= cost <= 50.0):
        raise ValueError("Cost per trade must be between 0 and 50 points.")
    if not (0.0 <= hold <= 0.8):
        raise ValueError("Hold-out must be between 0% and 80% of the days.")
    spec.update({"from": f, "to": t, "lanes": [l for l in LANE_IDS if l in lanes], "cost_pts": cost, "holdout_frac": hold,
                 "label": str(raw.get("label") or "")[:80]})
    return spec


def _paths(jobs_dir: Path, job_id: str) -> Dict[str, Path]:
    safe = "".join(c for c in str(job_id) if c.isalnum() or c in "-_")[:64]
    return {k: jobs_dir / f"{safe}.{k}.json" for k in ("spec", "progress", "result")} | {"cancel": jobs_dir / f"{safe}.cancel"}


def _pid_alive(pid: Any) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def job_status(job_id: str, jobs_dir: Path = JOBS_DIR) -> Dict[str, Any]:
    p = _paths(jobs_dir, job_id)
    spec, prog = _read_json(p["spec"]), _read_json(p["progress"])
    if not spec:
        return {"ok": False, "error": "no such run"}
    out = {"ok": True, "id": job_id, "spec": spec, "state": "QUEUED", "done_chunks": 0, "total_chunks": 0,
           "trades_so_far": 0, "net_so_far": 0.0, "message": "starting…"}
    if prog:
        out.update(prog)
        if out["state"] == "RUNNING" and not _pid_alive(prog.get("pid")):
            out.update(state="FAILED", message="The run stopped without finishing (process ended). Start it again.")
    return out


def list_jobs(jobs_dir: Path = JOBS_DIR, limit: int = 25) -> List[Dict[str, Any]]:
    out = []
    for sp in sorted(jobs_dir.glob("*.spec.json"), key=lambda x: x.stat().st_mtime, reverse=True)[:limit]:
        jid = sp.name[: -len(".spec.json")]
        st = job_status(jid, jobs_dir)
        if not st.get("ok"):
            continue
        row = {"id": jid, "from": st["spec"].get("from"), "to": st["spec"].get("to"), "lanes": st["spec"].get("lanes"),
               "cost_pts": st["spec"].get("cost_pts"), "state": st["state"], "started": st.get("started"),
               "label": st["spec"].get("label") or ""}
        res = _read_json(_paths(jobs_dir, jid)["result"]) if st["state"] == "DONE" else None
        if res:
            row.update(trades=res["report"]["all"]["trades"], net_pts=res["report"]["all"]["net_pts"],
                       verdict=res["report"]["verdict"]["grade"])
        out.append(row)
    return out


def running_job(jobs_dir: Path = JOBS_DIR) -> Optional[str]:
    for row in list_jobs(jobs_dir, 10):
        if row["state"] in ("RUNNING", "QUEUED"):
            st = job_status(row["id"], jobs_dir)
            if st["state"] == "RUNNING" or (st["state"] == "QUEUED" and time.time() - float(st["spec"].get("created_ts") or 0) < 60):
                return row["id"]
    return None


def start_job(raw_spec: Dict[str, Any], jobs_dir: Path = JOBS_DIR, spawn: bool = True) -> Dict[str, Any]:
    """Validate, write the spec, start the worker process. One run at a time."""
    try:
        spec = clean_spec(raw_spec)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    busy = running_job(jobs_dir)
    if busy:
        return {"ok": False, "error": "A backtest is already running. Wait for it or cancel it.", "running_id": busy}
    job_id = datetime.now(IST).strftime("%Y%m%d-%H%M%S")
    spec.update(id=job_id, created_ts=time.time(), created=datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"))
    p = _paths(jobs_dir, job_id)
    _write_json(p["spec"], spec)
    if spawn:
        log = open(jobs_dir / f"{job_id}.log", "w", encoding="utf-8")
        kw: Dict[str, Any] = {"cwd": str(ROOT), "stdout": log, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL}
        if os.name == "posix":
            kw["start_new_session"] = True
            kw["preexec_fn"] = lambda: os.nice(10)       # the live desk comes first
        subprocess.Popen([sys.executable, "-m", "analyst.backtest_lab_8510", "--job", str(p["spec"])], **kw)
    return {"ok": True, "id": job_id, "spec": spec}


def cancel_job(job_id: str, jobs_dir: Path = JOBS_DIR) -> Dict[str, Any]:
    p = _paths(jobs_dir, job_id)
    if not p["spec"].exists():
        return {"ok": False, "error": "no such run"}
    p["cancel"].write_text("cancel", encoding="utf-8")
    return {"ok": True, "id": job_id}


def job_result(job_id: str, jobs_dir: Path = JOBS_DIR, with_trades: bool = False) -> Dict[str, Any]:
    res = _read_json(_paths(jobs_dir, job_id)["result"])
    if not res:
        return {"ok": False, "error": "no result yet"}
    if not with_trades:
        res = {k: v for k, v in res.items() if k != "trades"}
    res["ok"] = True
    return res


def job_day(job_id: str, day: str, market_db: Path, jobs_dir: Path = JOBS_DIR) -> Dict[str, Any]:
    """One day of a finished run: its trades and its candles."""
    res = _read_json(_paths(jobs_dir, job_id)["result"])
    if not res:
        return {"ok": False, "error": "no result yet"}
    cost = float(res["report"]["spec"].get("cost_pts", 0.0))
    trades = [dict(t, net=round(t["gross"] - cost, 2)) for t in res.get("trades") or [] if t["day"] == day]
    chart = day_chart(market_db, day)
    return {"ok": True, "day": day, "trades": trades, "chart": chart, "cost_pts": cost}


# ── the worker ───────────────────────────────────────────────────────────────
def _private_backend(tmp: Path):
    """A backend object of this process's own: private config copy, throw-away ledger."""
    from analyst.terminal8510_backend import QuantTerminalBackend

    cfg_path = ROOT / "analyst/terminal8510_config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    market = Path(str(cfg.get("market_db_path") or ""))
    cfg["market_db_path"] = str(market if market.is_absolute() else (ROOT / market))
    cfg["ledger_db_path"] = str(tmp / "ledger.sqlite3")
    private = tmp / "config.json"
    private.write_text(json.dumps(cfg), encoding="utf-8")
    try:
        os.chmod(private, 0o600)
    except OSError:
        pass
    return QuantTerminalBackend(str(private))


_WORKER_BACKEND: Any = None
_WORKER_TMP: Any = None


def _run_chunk(args: Tuple[str, str, str, List[str]]) -> Dict[str, Any]:
    """One month in a worker process (its own private backend, built once per process)."""
    global _WORKER_BACKEND, _WORKER_TMP
    c_from, c_to, engine_filter, lanes = args
    try:
        if _WORKER_BACKEND is None:
            _WORKER_TMP = tempfile.TemporaryDirectory(prefix="bt8510_w_")
            _WORKER_BACKEND = _private_backend(Path(_WORKER_TMP.name))
        res = _WORKER_BACKEND.run_date_range_backtest(c_from, c_to, include_today_paper=False,
                                                      strategy_filter=engine_filter, data_type_filter="SYNTHESIZED")
        if not res.get("ok", True):
            return {"ok": False, "from": c_from, "error": str(res.get("error"))}
        part = [t for t in normalize_rows(res.get("trades") or []) if c_from <= t["day"] <= c_to and t["lane"] in lanes]
        return {"ok": True, "from": c_from, "trades": part}
    except Exception as exc:
        return {"ok": False, "from": c_from, "error": f"{type(exc).__name__}: {exc}"[:300]}


def default_workers(now: Optional[datetime] = None) -> int:
    """One process while the market is open (the live desk comes first); otherwise up to 4."""
    now = now or datetime.now(IST)
    market = now.weekday() < 5 and "09:00" <= now.strftime("%H:%M") <= "15:40"
    return 1 if market else max(1, min(4, (os.cpu_count() or 2) - 1))


def run_job(spec_path: Path, backend: Any = None) -> int:
    spec = _read_json(Path(spec_path))
    if not spec:
        print("spec not readable", file=sys.stderr)
        return 2
    jobs_dir = Path(spec_path).parent
    p = _paths(jobs_dir, spec["id"])
    started = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
    t0 = time.time()
    prog: Dict[str, Any] = {"state": "RUNNING", "pid": os.getpid(), "started": started, "done_chunks": 0,
                            "total_chunks": 0, "trades_so_far": 0, "net_so_far": 0.0, "message": "loading the engine…"}
    _write_json(p["progress"], prog)

    def fail(msg: str) -> int:
        prog.update(state="FAILED", message=msg, elapsed_sec=round(time.time() - t0))
        _write_json(p["progress"], prog)
        return 1

    try:
        with tempfile.TemporaryDirectory(prefix="bt8510_") as td:
            be = backend or _private_backend(Path(td))
            cov = coverage_of(Path(be.market_db), spec["from"], spec["to"])
            days = cov["days"]
            if not days:
                return fail(f"No NIFTY spot candles between {spec['from']} and {spec['to']}. "
                            "Pull the data first (tab: Pull Today's Data).")
            chunks = month_chunks(days)
            lanes = list(spec["lanes"])
            engine_filter = ENGINE_FILTER[lanes[0]] if len(lanes) == 1 else "ALL"
            cost = float(spec["cost_pts"])
            trades: List[Dict[str, Any]] = []
            prog.update(total_chunks=len(chunks), total_days=len(days), message=f"{len(days)} trading days in {len(chunks)} months")
            _write_json(p["progress"], prog)
            def cancelled() -> bool:
                if p["cancel"].exists():
                    prog.update(state="CANCELLED", message="Cancelled.", elapsed_sec=round(time.time() - t0))
                    _write_json(p["progress"], prog)
                    return True
                return False

            def took(part: List[Dict[str, Any]], done: int) -> None:
                trades.extend(part)
                el = time.time() - t0
                prog.update(done_chunks=done, trades_so_far=len(trades),
                            net_so_far=round(sum(t["gross"] - cost for t in trades), 1), elapsed_sec=round(el),
                            eta_sec=round(el / done * (len(chunks) - done)) if done else None,
                            message=f"{done} of {len(chunks)} months done")
                _write_json(p["progress"], prog)

            workers = 1 if backend is not None else int(spec.get("workers") or default_workers())
            workers = max(1, min(workers, len(chunks)))
            prog.update(workers=workers)
            if workers == 1:
                for i, (c_from, c_to) in enumerate(chunks):
                    if cancelled():
                        return 0
                    prog.update(message=f"running {c_from[:7]} ({i + 1} of {len(chunks)})")
                    _write_json(p["progress"], prog)
                    res = be.run_date_range_backtest(c_from, c_to, include_today_paper=False,
                                                     strategy_filter=engine_filter, data_type_filter="SYNTHESIZED")
                    if not res.get("ok", True):
                        return fail(f"The engine failed on {c_from}..{c_to}: {res.get('error')}")
                    took([t for t in normalize_rows(res.get("trades") or [])
                          if c_from <= t["day"] <= c_to and t["lane"] in lanes], i + 1)
            else:
                from concurrent.futures import ProcessPoolExecutor, as_completed

                prog.update(message=f"running {len(chunks)} months on {workers} processes")
                _write_json(p["progress"], prog)
                with ProcessPoolExecutor(max_workers=workers) as pool:
                    futs = [pool.submit(_run_chunk, (a, b, engine_filter, lanes)) for a, b in chunks]
                    done = 0
                    for fut in as_completed(futs):
                        out = fut.result()
                        if not out.get("ok"):
                            pool.shutdown(wait=False, cancel_futures=True)
                            return fail(f"The engine failed on {out.get('from')}: {out.get('error')}")
                        done += 1
                        took(out["trades"], done)
                        if cancelled():
                            pool.shutdown(wait=False, cancel_futures=True)
                            return 0
            trades.sort(key=lambda t: (t["day"], t["entry_hm"], t["lane"]))
            report = build_report(trades, spec, cov)
            _write_json(p["result"], {"id": spec["id"], "report": report, "trades": trades,
                                      "finished": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),
                                      "elapsed_sec": round(time.time() - t0)})
            prog.update(state="DONE", message="Finished.", elapsed_sec=round(time.time() - t0))
            _write_json(p["progress"], prog)
            return 0
    except Exception as exc:
        traceback.print_exc()
        return fail(f"{type(exc).__name__}: {exc}"[:400])


def _print_report(rep: Dict[str, Any]) -> None:
    a, e, h, v = rep["all"], rep["earlier"], rep["held_out"], rep["verdict"]
    c = rep["coverage"]
    print(f"\nRange {c['from']} .. {c['to']} · {c['days_with_data']} trading days · cost {rep['spec']['cost_pts']} pts/trade")
    print(f"VERDICT: {v['grade']} — {v['text']}\n")
    fmt = lambda s: (f"{s['trades']:>5} trades  net {s['net_pts']:>9}  avg {s['avg_net_pts']}  win {s['win_rate_pct']}%  "
                     f"t {s['t_stat']}  maxDD {s['max_drawdown_pts']}")
    print("ALL      ", fmt(a)); print("EARLIER  ", fmt(e)); print("HELD-OUT ", fmt(h))
    for l in rep["per_lane"]:
        print(f"  {l['label']:<24}", fmt(l["all"]), "|", (l["verdict"] or {}).get("grade", ""))


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="8510 Backtest Lab")
    ap.add_argument("--job", help="spec file written by the cockpit")
    ap.add_argument("--from", dest="from_day"); ap.add_argument("--to", dest="to_day")
    ap.add_argument("--lanes", default=",".join(DEFAULT_SPEC["lanes"]))
    ap.add_argument("--cost-pts", type=float, default=DEFAULT_SPEC["cost_pts"])
    ap.add_argument("--holdout", type=float, default=DEFAULT_SPEC["holdout_frac"])
    a = ap.parse_args(argv)
    if a.job:
        return run_job(Path(a.job))
    if not (a.from_day and a.to_day):
        ap.error("give --job, or --from and --to")
    started = start_job({"from": a.from_day, "to": a.to_day, "lanes": a.lanes.split(","), "cost_pts": a.cost_pts,
                         "holdout_frac": a.holdout}, spawn=False)
    if not started.get("ok"):
        print(started.get("error"), file=sys.stderr)
        return 2
    code = run_job(_paths(JOBS_DIR, started["id"])["spec"])
    st = job_status(started["id"])
    if st["state"] != "DONE":
        print(st.get("message"), file=sys.stderr)
        return code or 1
    _print_report(job_result(started["id"])["report"])
    print(f"\nFull result: {_paths(JOBS_DIR, started['id'])['result']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
