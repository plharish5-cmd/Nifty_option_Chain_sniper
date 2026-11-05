#!/usr/bin/env python3
"""
scripts/backtest_mtf_engulfing.py

AGY Part 2 — measure the mentor's MTF/engulfing rules before building anything.
Offline script only: no UI, no engine changes, no live wiring, execution_mode
stays PAPER. Reads candles_1m from the existing ict_smc.sqlite3 (sec_id='13'
NIFTY spot, sec_id='FUT' continuous front-month NIFTY futures with real
volume) and resamples in-memory. Nothing here writes to data/candles_1m/*.csv
or any other new store.

Baseline "EXISTING Setup #1 entries"
-------------------------------------
algo_execution_engine/backtest_report_setup1.json already ran ablation
Variants A-E of Setup #1 over 250 sessions. Variant E ("Institutional 15m:
Bible + Gap + VWAP + EMA Sep") is bit-for-bit the same gate set implemented
in strategy_setup1.Setup1StrategyEvaluator.check_setup1_entry_signal (the
live production entry rule), n=80, 57.5% win rate, net profit factor 0.72,
net expectancy -Rs144/trade. That is the "EXISTING Setup #1 entries" this
brief means, and it is what L1-L5 are layered on top of below. Each layer
re-derives its own predicate against this same n=80 population directly
(not chained through the previous layer's surviving set) so the numbers are
independently scored, per the brief, even though L2 is a logical subset of
L1 and so on.

Setup #1 is a CE-only (long) strategy in this codebase; there is no PE
mirror implemented. L1's "CE only when 1H bullish, PE only when 1H bearish"
therefore reduces to: keep the long entries where 1H is bullish, drop where
1H is bearish. This is noted explicitly in the report, not hidden.

Zero look-ahead
----------------
Every trend/trigger gate below uses the last fully CLOSED higher-timeframe
bar strictly before the entry timestamp (bar_start + bar_seconds <= entry_ts),
the same convention algo_execution_engine/backtest_setup1.py already uses for
its own 15m gate. The exits for the n=80 real trades are untouched -- they
are Variant E's own already-simulated fixed 40/40 bracket exits, reused
as-is; filtering the population does not change any trade's outcome.
For the synthetic random baseline and the direction control (which need
entries that do not exist in Variant E's history), a separate forward-walk
simulator mirrors that same bracket and -- per Mandatory Control #1 -- starts
checking for SL/target strictly on the bar AFTER the entry bar, never on the
entry bar's own remaining range.
"""
from __future__ import annotations

import bisect
import csv
import json
import random
import sqlite3
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from algo_execution_engine.backtest_setup1 import (  # noqa: E402
    BacktestTrade,
    Setup1BacktestSimulator,
    build_15m_data,
    load_historical_1m_data,
)
from algo_execution_engine.strategy_setup1 import add_course_emas, load_config  # noqa: E402

DB_PATH = ROOT / "ict_smc_app/backend/data/ict_smc.sqlite3"
OUT_DIR = Path(__file__).resolve().parent / "out" / "mtf_engulfing"
OUT_DIR.mkdir(parents=True, exist_ok=True)

IST_OFFSET_SQL = "'+5 hours','+30 minutes'"

# Variant E's exact rule dict, copied from algo_execution_engine/backtest_setup1.py
# run_all() so this script never needs to modify or import the private method.
VARIANT_E_RULES = {
    "trend_filter": True, "ubb_filter": True, "first_opp": True, "daily_stop": True,
    "ctc": True, "ctc_brokerage_buffer": True, "fii_window": True, "gap_filter": True,
    "ema_sep": True, "vwap_filter": True,
}
VARIANT_E_NAME = "Variant E (Institutional 15m: Bible + Gap + VWAP + EMA Sep)"


# ─────────────────────────────────────────────────────────────────────────────
# DATA LOADING
# ─────────────────────────────────────────────────────────────────────────────

def load_1m(sec_id: str) -> pd.DataFrame:
    """Read-only load of candles_1m for a given sec_id. Mirrors
    backtest_setup1.load_historical_1m_data's query/filter exactly, generalized
    to any sec_id (that function hardcodes sec_id='13')."""
    uri = f"file:{DB_PATH}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    query = f"""
        SELECT ts, o as open, h as high, l as low, c as close, v as volume,
               date(ts, 'unixepoch', {IST_OFFSET_SQL}) as session_date,
               time(ts, 'unixepoch', {IST_OFFSET_SQL}) as session_time
        FROM candles_1m
        WHERE sec_id = ?
        ORDER BY ts ASC
    """
    df = pd.read_sql_query(query, conn, params=(sec_id,))
    conn.close()
    df = df[(df["session_time"] >= "09:15:00") & (df["session_time"] <= "15:30:00")].copy()
    return df.reset_index(drop=True)


def resample_ohlc(df_1m: pd.DataFrame, rule: str, extra_agg: Optional[Dict[str, str]] = None) -> pd.DataFrame:
    """Session-date-aware resample to any pandas rule, closed-left/label-left
    (bar timestamp == bar start), matching backtest_setup1.build_15m_data."""
    df = df_1m.copy()
    df["datetime"] = pd.to_datetime(df["ts"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
    df = df.set_index("datetime")
    agg = {"ts": "first", "open": "first", "high": "max", "low": "min", "close": "last",
           "session_date": "first", "session_time": "first"}
    if extra_agg:
        agg.update(extra_agg)
    out = df.resample(rule, closed="left", label="left").agg(agg).dropna(subset=["ts"]).reset_index(drop=True)
    return out


def add_trend(df: pd.DataFrame, fast: int = 4, slow: int = 8) -> pd.DataFrame:
    df = add_course_emas(df, fast, slow)
    df["bullish"] = df[f"ema{fast}"] > df[f"ema{slow}"]
    return df


def add_bull_engulfing(df: pd.DataFrame) -> pd.DataFrame:
    """Classic two-bar bullish engulfing on 'close'. Masked False across a
    session boundary so a cross-day pair (yesterday's last bar vs today's
    first bar) never counts as a trigger."""
    df = df.copy()
    prev_open = df["open"].shift(1)
    prev_close = df["close"].shift(1)
    same_day = df["session_date"] == df["session_date"].shift(1)
    df["bull_engulf"] = (
        same_day
        & (df["close"] > df["open"])
        & (prev_close < prev_open)
        & (df["open"] <= prev_close)
        & (df["close"] >= prev_open)
    ).fillna(False)
    return df


# ─────────────────────────────────────────────────────────────────────────────
# ZERO LOOK-AHEAD LOOKUPS (last CLOSED bar strictly before ts)
# ─────────────────────────────────────────────────────────────────────────────

class ClosedBarIndex:
    """Given a resampled (left-labeled) frame, finds the last bar that has
    FULLY CLOSED by time t: bar_start + bar_seconds <= t. Same convention as
    backtest_setup1._simulate_single_session's closed_15m lookup."""

    def __init__(self, df: pd.DataFrame, bar_seconds: int):
        self.ts = df["ts"].tolist()
        self.records = df.to_dict(orient="records")
        self.bar_seconds = bar_seconds

    def at(self, t: int) -> Optional[Dict[str, Any]]:
        idx = bisect.bisect_right(self.ts, t - self.bar_seconds) - 1
        return self.records[idx] if idx >= 0 else None


# ─────────────────────────────────────────────────────────────────────────────
# GATE PREDICATES — each is independent, applied directly to the Variant E
# trade list, not chained through a previous layer's surviving set.
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Frames:
    f1h: ClosedBarIndex
    f30m: ClosedBarIndex
    f15m: ClosedBarIndex
    f5m_engulf: ClosedBarIndex
    f1m_engulf: ClosedBarIndex
    fut_5m_vol: Optional[ClosedBarIndex]


def gate_1h(frames: Frames, ts: int) -> bool:
    bar = frames.f1h.at(ts)
    return bool(bar and bar["bullish"])


def gate_1h_30m_15m(frames: Frames, ts: int) -> bool:
    b1h, b30, b15 = frames.f1h.at(ts), frames.f30m.at(ts), frames.f15m.at(ts)
    return bool(b1h and b30 and b15 and b1h["bullish"] and b30["bullish"] and b15["bullish"])


def gate_l3(frames: Frames, ts: int) -> bool:
    if not gate_1h_30m_15m(frames, ts):
        return False
    bar = frames.f5m_engulf.at(ts)
    return bool(bar and bar["bull_engulf"])


def gate_l4(frames: Frames, ts: int) -> bool:
    if not gate_l3(frames, ts):
        return False
    bar = frames.f1m_engulf.at(ts)
    return bool(bar and bar["bull_engulf"])


def gate_volume(frames: Frames, ts: int) -> bool:
    """5m FUT volume on the last closed 5m bar before ts > its own trailing
    20-bar average (computed only from PRIOR closed bars, zero look-ahead
    relative to that bar). Design choice, documented in the report: volume
    confirmation is scored on the 5m timeframe regardless of which trigger
    layer (L1-L4) it rides on, since it is meant to confirm the broader move,
    not the micro-trigger bar."""
    if frames.fut_5m_vol is None:
        return False
    idx = bisect.bisect_right(frames.fut_5m_vol.ts, ts - frames.fut_5m_vol.bar_seconds) - 1
    if idx < 20:
        return False
    recs = frames.fut_5m_vol.records
    cur_vol = recs[idx]["volume"]
    avg20 = sum(recs[i]["volume"] for i in range(idx - 20, idx)) / 20.0
    return avg20 > 0 and cur_vol > avg20


LAYER_GATES = {
    "L1 (1H trend alone)": gate_1h,
    "L2 (1H+30m+15m alignment)": gate_1h_30m_15m,
    "L3 (L2 + 5m engulfing)": gate_l3,
    "L4 (L3 + 1m engulfing)": gate_l4,
}


# ─────────────────────────────────────────────────────────────────────────────
# GENERIC FORWARD-WALK SIMULATOR (fixed 40/40 bracket, CTC, EOD square-off)
# Used ONLY for the synthetic random baseline and the direction control --
# the real n=80 trades already carry their own exits from Variant E.
# ─────────────────────────────────────────────────────────────────────────────

def simulate_fixed_bracket(
    bars_by_session: Dict[str, List[Dict[str, Any]]],
    entries: Sequence[Dict[str, Any]],
    direction: int,
    cfg: Dict[str, Any],
) -> List[SimpleNamespace]:
    strat = cfg.get("strategy", {})
    bt = cfg.get("backtest", {})
    fixed_sl = float(strat.get("fixed_sl_pts", 40.0))
    target_pts = float(strat.get("target_pts", 40.0))
    ctc_trigger = float(strat.get("cost_to_cost_trigger_pts", 40.0))
    ctc_buffer = float(strat.get("cost_to_cost_brokerage_buffer_pts", 9.0))
    square_off = str(bt.get("square_off_time", "15:20"))
    lot_size = int(bt.get("lot_size", 65))
    brokerage = float(bt.get("brokerage_inr_per_trade", 400.0))
    delta_win = float(bt.get("itm_delta_win", 0.6))
    delta_loss = float(bt.get("itm_delta_loss", 0.5))

    out: List[SimpleNamespace] = []
    for e in entries:
        bars = bars_by_session.get(e["session_date"])
        if not bars:
            continue
        entry_spot = e["entry_spot"]
        entry_ts = e["entry_ts"]
        target = entry_spot + direction * target_pts
        trailed_sl = entry_spot - direction * fixed_sl
        cost_to_cost_active = False
        exit_spot = exit_ts = exit_reason = None

        # Mandatory Control #1: forward walk starts strictly AFTER the entry
        # bar's own close (ts > entry_ts), never on the entry bar itself.
        for b in bars:
            if b["ts"] <= entry_ts:
                continue
            moved_favor = direction * (b["high"] - entry_spot) if direction == 1 else direction * (entry_spot - b["low"]) * -1
            moved_favor = (b["high"] - entry_spot) if direction == 1 else (entry_spot - b["low"])
            if not cost_to_cost_active and moved_favor >= ctc_trigger:
                cost_to_cost_active = True
                trailed_sl = max(trailed_sl, entry_spot + ctc_buffer) if direction == 1 else min(trailed_sl, entry_spot - ctc_buffer)

            hit_sl = (b["low"] <= trailed_sl) if direction == 1 else (b["high"] >= trailed_sl)
            hit_tp = (b["high"] >= target) if direction == 1 else (b["low"] <= target)

            if hit_sl and hit_tp:
                exit_spot, exit_reason = trailed_sl, "SL/TP same bar (conservative: SL assumed first)"
            elif hit_sl:
                exit_spot, exit_reason = trailed_sl, "Stop-Loss"
            elif hit_tp:
                exit_spot, exit_reason = target, "Target"
            if exit_spot is not None:
                exit_ts = b["ts"]
                break
            if b["session_time"] >= square_off:
                exit_spot, exit_ts, exit_reason = b["close"], b["ts"], "EOD square-off"
                break

        if exit_spot is None:
            last = bars[-1]
            exit_spot, exit_ts, exit_reason = last["close"], last["ts"], "EOD (no forward bars left)"

        spot_pnl = (exit_spot - entry_spot) if direction == 1 else (entry_spot - exit_spot)
        opt_pts = spot_pnl * delta_win if spot_pnl > 0 else spot_pnl * delta_loss
        net_inr = opt_pts * lot_size - brokerage
        out.append(SimpleNamespace(
            session_date=e["session_date"], entry_ts=entry_ts, exit_ts=exit_ts,
            entry_spot=entry_spot, exit_spot=exit_spot, spot_pnl_pts=spot_pnl,
            asym_net_inr=net_inr, exit_reason=exit_reason, variant="synthetic",
        ))
    return out


def time_matched_random_entries(
    real_trades: Sequence[Any],
    all_session_dates: List[str],
    bars_by_session: Dict[str, List[Dict[str, Any]]],
    seed: int,
) -> List[Dict[str, Any]]:
    """Preserves each real signal's exact clock minute-of-day, randomizes only
    which trading day it fires on. This is Mandatory Control #2: an unmatched
    baseline flatters real entries because late-session bars have no runway
    before the close truncates the horizon; matching time-of-day removes that
    bias."""
    rng = random.Random(seed)
    entries = []
    for t in real_trades:
        clock = _time_of_day(t.entry_ts)
        day = rng.choice(all_session_dates)
        bars = bars_by_session.get(day, [])
        match = next((b for b in bars if b["session_time"] >= clock), None)
        if match is None:
            continue
        entries.append({"session_date": day, "entry_ts": match["ts"], "entry_spot": match["close"]})
    return entries


def _time_of_day(ts: int) -> str:
    import datetime as _dt
    from zoneinfo import ZoneInfo
    return _dt.datetime.fromtimestamp(ts, ZoneInfo("Asia/Kolkata")).strftime("%H:%M:%S")


# ─────────────────────────────────────────────────────────────────────────────
# STATISTICS — n, win rate, pts, INR, profit factor, bootstrap, halves, etc.
# ─────────────────────────────────────────────────────────────────────────────

def basic_stats(trades: Sequence[Any]) -> Dict[str, Any]:
    n = len(trades)
    if n == 0:
        return {"n": 0, "judgeable": False, "note": "No trades at all"}
    pts = [t.spot_pnl_pts for t in trades]
    inr = [t.asym_net_inr for t in trades]
    wins_pts = [p for p in pts if p > 0]
    losses_pts = [p for p in pts if p <= 0]
    win_inr = [x for x, p in zip(inr, pts) if p > 0]
    loss_inr = [x for x, p in zip(inr, pts) if p <= 0]
    gross_win = sum(win_inr)
    gross_loss = abs(sum(loss_inr))
    if gross_loss > 0:
        pf = gross_win / gross_loss
    else:
        pf = float("inf") if gross_win > 0 else 0.0
    return {
        "n": n,
        "judgeable": n >= 60,
        "win_rate_pct": round(100.0 * len(wins_pts) / n, 2),
        "avg_win_pts": round(statistics.mean(wins_pts), 2) if wins_pts else 0.0,
        "avg_loss_pts": round(statistics.mean(losses_pts), 2) if losses_pts else 0.0,
        "net_inr_per_trade": round(sum(inr) / n, 2),
        "total_net_inr": round(sum(inr), 2),
        "profit_factor": round(pf, 3) if pf != float("inf") else "inf",
    }


def excluding_best_k(trades: Sequence[Any], k: int) -> Optional[Dict[str, Any]]:
    if len(trades) <= k:
        return None
    ordered = sorted(trades, key=lambda t: t.asym_net_inr, reverse=True)
    return basic_stats(ordered[k:])


def chronological_halves(trades: Sequence[Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    ordered = sorted(trades, key=lambda t: t.entry_ts)
    mid = len(ordered) // 2
    return basic_stats(ordered[:mid]), basic_stats(ordered[mid:])


def session_block_bootstrap_ci(trades: Sequence[Any], n_boot: int = 2000, seed: int = 42) -> Optional[Dict[str, Any]]:
    """Mandatory Control #3: resample whole SESSIONS (not individual bars),
    since minutes within one day share a single market path."""
    by_session: Dict[str, List[Any]] = defaultdict(list)
    for t in trades:
        by_session[t.session_date].append(t)
    sessions = list(by_session.keys())
    if not sessions:
        return None
    rng = random.Random(seed)
    means = []
    for _ in range(n_boot):
        sample_days = [rng.choice(sessions) for _ in sessions]
        pooled = [t for d in sample_days for t in by_session[d]]
        if pooled:
            means.append(sum(t.asym_net_inr for t in pooled) / len(pooled))
    if not means:
        return None
    means.sort()
    lo = means[int(0.05 * len(means))]
    hi = means[int(0.95 * len(means)) - 1]
    return {"n_sessions": len(sessions), "n_boot": n_boot, "net_inr_per_trade_90pct_ci": [round(lo, 2), round(hi, 2)],
            "excludes_zero": bool(lo > 0 or hi < 0)}


def write_event_csv(layer_key: str, trades: Sequence[Any]) -> str:
    """Mandatory Control #6: the per-event table with time-to-resolution,
    where a look-ahead bug would be visible, written in full (console summary
    is necessarily truncated)."""
    path = OUT_DIR / f"events_{layer_key}.csv"
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["session_date", "entry_ts", "exit_ts", "time_to_resolution_min",
                    "entry_spot", "exit_spot", "spot_pnl_pts", "net_inr", "exit_reason"])
        for t in sorted(trades, key=lambda x: x.entry_ts):
            ttr_min = round((t.exit_ts - t.entry_ts) / 60.0, 1)
            w.writerow([t.session_date, t.entry_ts, t.exit_ts, ttr_min,
                        getattr(t, "entry_spot", ""), getattr(t, "exit_spot", ""),
                        t.spot_pnl_pts, round(t.asym_net_inr, 2), getattr(t, "exit_reason", "")])
    return str(path.relative_to(ROOT))


# ─────────────────────────────────────────────────────────────────────────────
# PART 3 BAR
# ─────────────────────────────────────────────────────────────────────────────

def part3_bar(stats: Dict[str, Any], ex_best5: Optional[Dict[str, Any]],
              halves: Tuple[Dict[str, Any], Dict[str, Any]], direction_ok: bool) -> Dict[str, Any]:
    h1, h2 = halves
    checks = {
        "net_inr_per_trade_positive": stats.get("n", 0) > 0 and stats["net_inr_per_trade"] > 0,
        "profit_factor_ge_1.30": isinstance(stats.get("profit_factor"), (int, float)) and stats["profit_factor"] >= 1.30
        or stats.get("profit_factor") == "inf",
        "n_ge_60": stats.get("n", 0) >= 60,
        "positive_excluding_best_5": bool(ex_best5 and ex_best5["n"] > 0 and ex_best5["net_inr_per_trade"] > 0),
        "positive_both_halves": bool(h1.get("n", 0) > 0 and h2.get("n", 0) > 0
                                      and h1["net_inr_per_trade"] > 0 and h2["net_inr_per_trade"] > 0),
        "direction_control_clean": direction_ok,
    }
    return {"checks": checks, "earns_implementation": all(checks.values())}


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    cfg = load_config()
    print(f"[1/7] Loading candles_1m for sec_id='13' (NIFTY spot) from {DB_PATH.relative_to(ROOT)} ...")
    df_1m = load_historical_1m_data(str(DB_PATH))
    print(f"      {len(df_1m)} bars across {df_1m['session_date'].nunique()} sessions "
          f"({df_1m['session_date'].min()} .. {df_1m['session_date'].max()})")

    print("[2/7] Generating the EXISTING Setup #1 entries (Variant E, n expected ~80)...")
    sim = Setup1BacktestSimulator(cfg)
    df_15m_engine = build_15m_data(df_1m, cfg)
    map_15m = {r["ts"]: r for r in df_15m_engine.to_dict(orient="records")}
    list_15m_ts = sorted(map_15m.keys())
    sessions = [g for _, g in df_1m.groupby("session_date", sort=True)]
    base_trades: List[BacktestTrade] = sim._simulate_dataset(sessions, map_15m, list_15m_ts, VARIANT_E_NAME, VARIANT_E_RULES)
    print(f"      n={len(base_trades)} (report's own Variant E is n=80 -- should match)")
    if len(base_trades) != 80:
        print(f"      NOTE: got n={len(base_trades)}, not 80. Report's json may be stale or data has grown "
              f"since it was generated; proceeding with the live count.")

    print("[3/7] Building 1H/30m/15m trend frames and 5m/1m engulfing frames (closed-bar, zero look-ahead)...")
    f1h = ClosedBarIndex(add_trend(resample_ohlc(df_1m, "1h")), 3600)
    f30m = ClosedBarIndex(add_trend(resample_ohlc(df_1m, "30min")), 1800)
    f15m = ClosedBarIndex(add_trend(resample_ohlc(df_1m, "15min")), 900)
    f5m_engulf = ClosedBarIndex(add_bull_engulfing(resample_ohlc(df_1m, "5min")), 300)
    df_1m_sorted = df_1m.sort_values("ts").reset_index(drop=True)
    f1m_engulf = ClosedBarIndex(add_bull_engulfing(df_1m_sorted), 60)

    print("[4/7] Loading sec_id='FUT' (continuous front-month NIFTY futures) for volume confirmation...")
    df_fut = load_1m("FUT")
    fut_5m_vol = None
    vol_window = None
    if df_fut.empty:
        print("      NOTE: no FUT rows found -- volume layer (L5) cannot run at all.")
    else:
        vol_start, vol_end = df_fut["session_date"].min(), df_fut["session_date"].max()
        vol_window = (vol_start, vol_end)
        print(f"      FUT 1m rows: {len(df_fut)}, sessions {vol_start} .. {vol_end} "
              f"(sum volume={df_fut['volume'].sum():.0f}). L5 restricted to this window.")
        fut_5m = resample_ohlc(df_fut, "5min", extra_agg={"volume": "sum"})
        fut_5m_vol = ClosedBarIndex(fut_5m, 300)

    frames = Frames(f1h, f30m, f15m, f5m_engulf, f1m_engulf, fut_5m_vol)

    # Correlation between the three trend states, sampled across every 1m bar
    # in the dataset (not just at the n=80 entries) -- the brief expects this
    # to be high, since 1H/30m/15m trend all derive from the same price series.
    print("[5/7] Computing 1H/30m/15m trend-state correlation across the full 1m timeline...")
    sample_ts = df_1m_sorted["ts"].tolist()
    b1h = [1 if (b := f1h.at(t)) and b["bullish"] else 0 for t in sample_ts]
    b30 = [1 if (b := f30m.at(t)) and b["bullish"] else 0 for t in sample_ts]
    b15 = [1 if (b := f15m.at(t)) and b["bullish"] else 0 for t in sample_ts]
    corr_matrix = np.corrcoef([b1h, b30, b15])
    correlation = {
        "1h_vs_30m": round(float(corr_matrix[0, 1]), 3),
        "1h_vs_15m": round(float(corr_matrix[0, 2]), 3),
        "30m_vs_15m": round(float(corr_matrix[1, 2]), 3),
        "note": "Sampled over every 1m bar in the dataset (not just the n=80 entries). "
                "High correlation here means L2's extra gates add little independent "
                "information beyond L1 -- expected, since all three derive from the same price series.",
    }
    print(f"      1h/30m={correlation['1h_vs_30m']}  1h/15m={correlation['1h_vs_15m']}  30m/15m={correlation['30m_vs_15m']}")

    print("[6/7] Scoring layers L1-L4, then L5 (each + volume) over the FUT-restricted window, independently...")
    bars_by_session: Dict[str, List[Dict[str, Any]]] = {
        d: g.sort_values("ts").to_dict(orient="records") for d, g in df_1m_sorted.groupby("session_date")
    }
    all_session_dates = sorted(bars_by_session.keys())

    layers_report: Dict[str, Any] = {}

    def score_layer(label: str, key: str, trades: Sequence[Any], fut_window_only: bool = False) -> None:
        if fut_window_only and vol_window:
            lo, hi = vol_window
            trades = [t for t in trades if lo <= t.session_date <= hi]
        stats = basic_stats(trades)
        ex5 = excluding_best_k(trades, 5)
        ex3 = excluding_best_k(trades, 3)
        ex1 = excluding_best_k(trades, 1)
        h1, h2 = chronological_halves(trades)

        # Mandatory Control #2: time-of-day matched random baseline, same n.
        rand_entries = time_matched_random_entries(trades, all_session_dates, bars_by_session, seed=42)
        baseline_long = simulate_fixed_bracket(bars_by_session, rand_entries, +1, cfg)
        baseline_stats = basic_stats(baseline_long)

        # Mandatory Control #4: same bars, opposite direction, scored against
        # a SHORT baseline's own gap (not against zero / not against the raw
        # long number). If shorting the identical signal bars also beats a
        # matching short baseline, the apparent long edge is generic index
        # drift, not something this setup's gates contributed.
        flip_entries = [{"session_date": t.session_date, "entry_ts": t.entry_ts, "entry_spot": t.entry_spot}
                         for t in trades if hasattr(t, "entry_spot")]
        direction_control = None
        direction_ok = True
        if flip_entries:
            short_real = simulate_fixed_bracket(bars_by_session, flip_entries, -1, cfg)
            short_baseline = simulate_fixed_bracket(bars_by_session, rand_entries, -1, cfg)
            short_real_stats = basic_stats(short_real)
            short_baseline_stats = basic_stats(short_baseline)
            edge_long = stats.get("net_inr_per_trade", 0) - baseline_stats.get("net_inr_per_trade", 0)
            edge_short = short_real_stats.get("net_inr_per_trade", 0) - short_baseline_stats.get("net_inr_per_trade", 0)
            direction_control = {
                "edge_long_over_baseline_inr": round(edge_long, 2),
                "edge_short_flip_over_short_baseline_inr": round(edge_short, 2),
                "short_flip_stats": short_real_stats,
                "short_baseline_stats": short_baseline_stats,
                "interpretation": "direction_ok=False means shorting the identical bars ALSO beats its own "
                                   "baseline -- the apparent long edge is likely generic drift, not this setup's gates.",
            }
            direction_ok = edge_long > 0 and not (edge_short > 0)
        else:
            direction_ok = False

        boot = session_block_bootstrap_ci(trades)
        event_csv = write_event_csv(key, trades) if trades else None

        bar = part3_bar(stats, ex5, (h1, h2), direction_ok)

        layers_report[label] = {
            "stats": stats,
            "excluding_best_1": ex1,
            "excluding_best_3": ex3,
            "excluding_best_5": ex5,
            "chronological_half_1": h1,
            "chronological_half_2": h2,
            "time_matched_random_baseline": baseline_stats,
            "direction_control": direction_control,
            "session_block_bootstrap_90pct_ci_net_inr_per_trade": boot,
            "event_table_csv": event_csv,
            "part3_bar": bar,
        }
        n_show = stats.get("n", 0)
        wr = stats.get("win_rate_pct", "n/a")
        pf = stats.get("profit_factor", "n/a")
        verdict = "EARNS IMPLEMENTATION" if bar["earns_implementation"] else "NOT BUILT"
        print(f"      {label:45s} n={n_show:4d}  win%={wr!s:>6}  pf={pf!s:>6}  -> {verdict}")

    score_layer("L0 (baseline: existing Setup #1 entries, no MTF gate)", "L0", base_trades)
    for label, gate_fn in LAYER_GATES.items():
        filtered = [t for t in base_trades if gate_fn(frames, t.entry_ts)]
        score_layer(label, label.split()[0], filtered)

    if fut_5m_vol is not None:
        for base_label, gate_fn in LAYER_GATES.items():
            key = base_label.split()[0]
            filtered = [t for t in base_trades if gate_fn(frames, t.entry_ts) and gate_volume(frames, t.entry_ts)]
            score_layer(f"L5 ({key} + volume confirmation)", f"{key}V", filtered, fut_window_only=True)
    else:
        layers_report["L5 (volume confirmation)"] = {"note": "Skipped entirely -- no sec_id='FUT' rows in candles_1m."}

    print("[7/7] Writing report JSON...")
    report = {
        "meta": {
            "generated_from": "scripts/backtest_mtf_engulfing.py",
            "baseline_variant": VARIANT_E_NAME,
            "baseline_rule_dict": VARIANT_E_RULES,
            "index_sessions": df_1m["session_date"].nunique(),
            "index_date_range": [df_1m["session_date"].min(), df_1m["session_date"].max()],
            "fut_volume_window": list(vol_window) if vol_window else None,
            "lot_size": cfg.get("backtest", {}).get("lot_size"),
            "brokerage_inr": cfg.get("backtest", {}).get("brokerage_inr_per_trade"),
            "break_even_win_rate_pct_note": "Mentor math: lot 65, delta 0.6 win/0.5 loss, Rs400 brokerage, "
                                             "fixed 40/40 bracket -> break-even win rate ~59.4%.",
        },
        "trend_state_correlation": correlation,
        "layers": layers_report,
    }
    report_path = OUT_DIR / "report.json"
    report_path.write_text(json.dumps(report, indent=2, default=str))
    print(f"      Report: {report_path.relative_to(ROOT)}")
    print(f"      Per-event CSVs: {OUT_DIR.relative_to(ROOT)}/events_*.csv")
    print("\nexecution_mode stays PAPER. Nothing in this script wires into live engine, UI, or order placement.")


if __name__ == "__main__":
    main()
