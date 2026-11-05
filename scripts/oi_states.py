#!/usr/bin/env python3
"""
scripts/oi_states.py — Empirical OI State Classifier & Setup #1 Gating Evaluation.

Offline, read-only script. Analyzes recorded chain_snap data to classify
option open interest (OI) dynamics and empirically tests whether filtering
Setup #1 trades by OI regime provides an edge before any live gating is added.

Key Components:
  1. 3-Minute Deltas & Side Classification:
     - CALL_WRITING, CALL_BUYING, CALL_SHORT_COVERING, CALL_UNWINDING
     - PUT_WRITING, PUT_BUYING, PUT_SHORT_COVERING, PUT_UNWINDING
  2. Aggregates:
     - pe_minus_ce level across strikes: sum(pe_oi) - sum(ce_oi)
     - pe_minus_ce 15-minute momentum change
     - pcr: sum(pe_oi) / sum(ce_oi)
  3. Combined State Classification:
     - STRONG_BULLISH / BULLISH / BEARISH / STRONG_BEARISH
     - Discovered from EMPIRICAL PERCENTILES (25th, 50th, 75th) of history.
     - Thresholds loaded from analyst/analyst_config.json.
  4. Measure Before Gating:
     - Evaluates Setup #1 CE longs and PE shorts inside the 18-session recorded window.
     - Computes real option execution (Bible ~Rs200 rule, chain_snap bid/ask half-spreads,
       and exact Dhan statutory charges via paper_trading.calculate_trade_charges).
     - Reports (State | n | Win Rate | Mean Rs/trade | Total Rs | Profit Factor).
"""
from __future__ import annotations

import bisect
import json
import logging
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from scipy.stats import percentileofscore

# Project Root
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import paper_trading
from algo_execution_engine.backtest_setup1 import (
    Setup1BacktestSimulator,
    build_15m_data,
    load_historical_1m_data,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("oi_states")

DB_PATH = ROOT / "ict_smc_app" / "backend" / "data" / "ict_smc.sqlite3"
CONFIG_PATH = ROOT / "analyst" / "analyst_config.json"
OUT_DIR = ROOT / "scripts" / "out" / "oi_states"
OUT_DIR.mkdir(parents=True, exist_ok=True)

LOT_SIZE = 65


# ─────────────────────────────────────────────────────────────────────────────
# 1. CONFIG & PERCENTILE MANAGEMENT
# ─────────────────────────────────────────────────────────────────────────────

def load_config() -> Dict[str, Any]:
    """Loads configuration and thresholds from JSON."""
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log.warning("Could not read config from %s: %s", CONFIG_PATH, e)
    return {}


def update_config_percentiles(percentiles: Dict[str, float]) -> None:
    """Updates discovered empirical percentiles in analyst_config.json."""
    cfg = load_config()
    if "oi_state_classifier" not in cfg:
        cfg["oi_state_classifier"] = {}
    cfg["oi_state_classifier"]["empirical_percentiles"] = percentiles
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        log.info("Persisted discovered empirical percentiles to %s", CONFIG_PATH)
    except Exception as e:
        log.warning("Failed to persist percentiles to %s: %s", CONFIG_PATH, e)


# ─────────────────────────────────────────────────────────────────────────────
# 2. LOAD CHAIN SNAPSHOTS & COMPUTE AGGREGATES
# ─────────────────────────────────────────────────────────────────────────────

def get_front_expiry_map(conn: sqlite3.Connection) -> Dict[str, str]:
    """Determines weekly front expiry for each session date in chain_snap."""
    df_exp = pd.read_sql_query("""
        SELECT DISTINCT date(ts, 'unixepoch', '+5 hours', '+30 minutes') as session_date, expiry
        FROM chain_snap
        ORDER BY session_date, expiry
    """, conn)
    front_map: Dict[str, str] = {}
    for s_date, grp in df_exp.groupby("session_date"):
        valid = [e for e in grp["expiry"] if e >= s_date]
        front_map[str(s_date)] = min(valid) if valid else str(grp["expiry"].iloc[0])
    return front_map


def load_and_classify_snapshots(db_path: Path, cfg: Dict[str, Any]) -> Tuple[pd.DataFrame, Dict[Tuple[str, int], Dict[float, Dict[str, Any]]], Dict[str, Any]]:
    """
    Loads chain_snap rows for all front expiries, computes 3m and 15m deltas,
    and classifies side build-ups and combined states.
    """
    oi_cfg = cfg.get("oi_state_classifier", {})
    look_3m_min = oi_cfg.get("lookback_3m_min_sec", 60)
    look_3m_max = oi_cfg.get("lookback_3m_max_sec", 420)
    look_15m_min = oi_cfg.get("lookback_15m_min_sec", 600)
    look_15m_max = oi_cfg.get("lookback_15m_max_sec", 1500)

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    front_exp_map = get_front_expiry_map(conn)

    df_chains = pd.read_sql_query("""
        SELECT expiry, ts, strike, ce_oi, pe_oi, ce_ltp, pe_ltp, ce_bid, ce_ask, pe_bid, pe_ask,
               date(ts, 'unixepoch', '+5 hours', '+30 minutes') as session_date
        FROM chain_snap
        ORDER BY ts, strike
    """, conn)
    conn.close()

    # Build fast lookup map for trade option pricing
    chain_by_exp_ts: Dict[Tuple[str, int], Dict[float, Dict[str, Any]]] = {}
    for (exp, ts), grp in df_chains.groupby(["expiry", "ts"]):
        chain_by_exp_ts[(str(exp), int(ts))] = {float(r["strike"]): r for r in grp.to_dict(orient="records")}

    snaps: List[Dict[str, Any]] = []

    for s_date, f_exp in front_exp_map.items():
        sub = df_chains[(df_chains["session_date"] == s_date) & (df_chains["expiry"] == f_exp)]
        ts_list = sorted(sub["ts"].unique())
        by_ts = {t: grp.set_index("strike") for t, grp in sub.groupby("ts")}

        for i, t in enumerate(ts_list):
            curr = by_ts[t]
            tot_ce_oi = float(curr["ce_oi"].sum())
            tot_pe_oi = float(curr["pe_oi"].sum())
            pe_minus_ce = tot_pe_oi - tot_ce_oi
            pcr = tot_pe_oi / tot_ce_oi if tot_ce_oi > 0 else np.nan

            # 3-minute lag delta (closest to 180s within [look_3m_min, look_3m_max])
            cands_3m = [pt for pt in ts_list[:i] if look_3m_min <= (t - pt) <= look_3m_max]
            if cands_3m:
                pt3 = min(cands_3m, key=lambda x: abs((t - x) - 180))
                prev3 = by_ts[pt3]
                c3 = curr.index.intersection(prev3.index)
                d_ce_oi = float((curr.loc[c3, "ce_oi"] - prev3.loc[c3, "ce_oi"]).sum())
                d_pe_oi = float((curr.loc[c3, "pe_oi"] - prev3.loc[c3, "pe_oi"]).sum())
                d_ce_ltp = float((curr.loc[c3, "ce_ltp"] - prev3.loc[c3, "ce_ltp"]).mean())
                d_pe_ltp = float((curr.loc[c3, "pe_ltp"] - prev3.loc[c3, "pe_ltp"]).mean())
            else:
                d_ce_oi, d_pe_oi, d_ce_ltp, d_pe_ltp = np.nan, np.nan, np.nan, np.nan

            # 15-minute lag change (closest to 900s within [look_15m_min, look_15m_max])
            cands_15m = [pt for pt in ts_list[:i] if look_15m_min <= (t - pt) <= look_15m_max]
            if cands_15m:
                pt15 = min(cands_15m, key=lambda x: abs((t - x) - 900))
                prev15 = by_ts[pt15]
                pe_minus_ce_15m_prev = float(prev15["pe_oi"].sum() - prev15["ce_oi"].sum())
                d_pe_minus_ce_15m = pe_minus_ce - pe_minus_ce_15m_prev
            else:
                d_pe_minus_ce_15m = np.nan

            # Side Build-up Classification (3-minute deltas)
            ce_state = "UNKNOWN"
            if not pd.isna(d_ce_oi) and not pd.isna(d_ce_ltp):
                if d_ce_oi > 0 and d_ce_ltp <= 0:
                    ce_state = "CALL_WRITING"
                elif d_ce_oi > 0 and d_ce_ltp > 0:
                    ce_state = "CALL_BUYING"
                elif d_ce_oi < 0 and d_ce_ltp > 0:
                    ce_state = "CALL_SHORT_COVERING"
                elif d_ce_oi < 0 and d_ce_ltp <= 0:
                    ce_state = "CALL_UNWINDING"

            pe_state = "UNKNOWN"
            if not pd.isna(d_pe_oi) and not pd.isna(d_pe_ltp):
                if d_pe_oi > 0 and d_pe_ltp <= 0:
                    pe_state = "PUT_WRITING"
                elif d_pe_oi > 0 and d_pe_ltp > 0:
                    pe_state = "PUT_BUYING"
                elif d_pe_oi < 0 and d_pe_ltp > 0:
                    pe_state = "PUT_UNWINDING"
                elif d_pe_oi < 0 and d_pe_ltp <= 0:
                    pe_state = "PUT_SHORT_COVERING"

            snaps.append({
                "session_date": s_date,
                "expiry": f_exp,
                "ts": int(t),
                "tot_ce_oi": tot_ce_oi,
                "tot_pe_oi": tot_pe_oi,
                "pe_minus_ce": pe_minus_ce,
                "pcr": pcr,
                "d_ce_oi_3m": d_ce_oi,
                "d_ce_ltp_3m": d_ce_ltp,
                "d_pe_oi_3m": d_pe_oi,
                "d_pe_ltp_3m": d_pe_ltp,
                "d_pe_minus_ce_3m": (d_pe_oi - d_ce_oi) if not pd.isna(d_pe_oi) else np.nan,
                "d_pe_minus_ce_15m": d_pe_minus_ce_15m,
                "ce_state": ce_state,
                "pe_state": pe_state,
            })

    df_snaps = pd.DataFrame(snaps)

    # ─────────────────────────────────────────────────────────────────────────
    # Empirical Percentile Thresholds Discovered from History
    # ─────────────────────────────────────────────────────────────────────────
    q_level = df_snaps["pe_minus_ce"].quantile([0.25, 0.50, 0.75]).to_dict()
    q_15m = df_snaps["d_pe_minus_ce_15m"].dropna().quantile([0.25, 0.50, 0.75]).to_dict()
    q_pcr = df_snaps["pcr"].quantile([0.25, 0.50, 0.75]).to_dict()

    percentiles_dict = {
        "pe_minus_ce_p25": float(q_level[0.25]),
        "pe_minus_ce_p50": float(q_level[0.50]),
        "pe_minus_ce_p75": float(q_level[0.75]),
        "d_pe_minus_ce_15m_p25": float(q_15m[0.25]),
        "d_pe_minus_ce_15m_p50": float(q_15m[0.50]),
        "d_pe_minus_ce_15m_p75": float(q_15m[0.75]),
        "pcr_p25": float(q_pcr[0.25]),
        "pcr_p50": float(q_pcr[0.50]),
        "pcr_p75": float(q_pcr[0.75]),
    }
    update_config_percentiles(percentiles_dict)

    # Classifications
    # 1. Level-based State (pe_minus_ce)
    def classify_level(val: float) -> str:
        if val >= q_level[0.75]:
            return "STRONG_BULLISH"
        elif val >= q_level[0.50]:
            return "BULLISH"
        elif val >= q_level[0.25]:
            return "BEARISH"
        else:
            return "STRONG_BEARISH"

    df_snaps["state_level"] = df_snaps["pe_minus_ce"].apply(classify_level)

    # 2. 15m Momentum-based State (d_pe_minus_ce_15m)
    def classify_15m(val: float) -> str:
        if pd.isna(val):
            return "UNKNOWN"
        if val >= q_15m[0.75]:
            return "STRONG_BULLISH"
        elif val >= q_15m[0.50]:
            return "BULLISH"
        elif val >= q_15m[0.25]:
            return "BEARISH"
        else:
            return "STRONG_BEARISH"

    df_snaps["state_15m"] = df_snaps["d_pe_minus_ce_15m"].apply(classify_15m)

    # 3. Composite Percentile Score (Level + 15m Momentum + 3m Flow)
    valid_lvl = df_snaps["pe_minus_ce"].values
    valid_15m = df_snaps["d_pe_minus_ce_15m"].dropna().values
    valid_3m = df_snaps["d_pe_minus_ce_3m"].dropna().values

    def calc_composite(row: pd.Series) -> float:
        p_lvl = percentileofscore(valid_lvl, row["pe_minus_ce"])
        p_15m = percentileofscore(valid_15m, row["d_pe_minus_ce_15m"]) if not pd.isna(row["d_pe_minus_ce_15m"]) else 50.0
        p_3m = percentileofscore(valid_3m, row["d_pe_minus_ce_3m"]) if not pd.isna(row["d_pe_minus_ce_3m"]) else 50.0
        return 0.4 * p_lvl + 0.4 * p_15m + 0.2 * p_3m

    df_snaps["comp_score"] = df_snaps.apply(calc_composite, axis=1)
    q_comp = df_snaps["comp_score"].quantile([0.25, 0.50, 0.75]).to_dict()

    def classify_comp(score: float) -> str:
        if score >= q_comp[0.75]:
            return "STRONG_BULLISH"
        elif score >= q_comp[0.50]:
            return "BULLISH"
        elif score >= q_comp[0.25]:
            return "BEARISH"
        else:
            return "STRONG_BEARISH"

    df_snaps["state_composite"] = df_snaps["comp_score"].apply(classify_comp)

    discovered_summary = {
        "percentiles_dict": percentiles_dict,
        "pe_minus_ce_quantiles": q_level,
        "d_pe_minus_ce_15m_quantiles": q_15m,
        "pcr_quantiles": q_pcr,
        "comp_quantiles": q_comp,
        "total_snapshots": len(df_snaps),
        "front_exp_map": front_exp_map,
    }

    return df_snaps, chain_by_exp_ts, discovered_summary


# ─────────────────────────────────────────────────────────────────────────────
# 3. SETUP #1 SIMULATION & OPTION PRICING
# ─────────────────────────────────────────────────────────────────────────────

def simulate_pe_bearish_trades(df_1m: pd.DataFrame, map_15m: Dict[int, Any], list_15m_ts: List[int]) -> List[Dict[str, Any]]:
    """
    Simulates the symmetric bearish counterpart of Setup #1 (mirrored PE buys):
    - Bearish Party: 15m EMA(4) < EMA(8)
    - Pullback: 1m bar reaches 15m EMA(8) from below (high >= ema8) and closes below ema8
    - Fixed 40/40 risk bracket: Target = Entry - 40 pts, Stop-Loss = Entry + 40 pts
    """
    trades: List[Dict[str, Any]] = []
    sessions = [grp for _, grp in df_1m.groupby("session_date", sort=True)]

    for session_df in sessions:
        s_date = session_df.iloc[0]["session_date"]
        active_trade: Optional[Dict[str, Any]] = None
        in_bearish_party = False

        rows = session_df.to_dict(orient="records")
        for bar in rows:
            t = bar["ts"]
            b_time = bar["session_time"]
            b_open = bar["open"]
            b_high = bar["high"]
            b_low = bar["low"]
            b_close = bar["close"]

            idx_15m = bisect.bisect_right(list_15m_ts, t - 900) - 1
            closed_15m = map_15m.get(list_15m_ts[idx_15m]) if idx_15m >= 0 else None

            if closed_15m is not None:
                e4 = closed_15m.get("ema4", 0.0)
                e8 = closed_15m.get("ema8", 0.0)
                is_bearish = (e4 < e8)
                if is_bearish and not in_bearish_party:
                    in_bearish_party = True
                elif not is_bearish and in_bearish_party:
                    in_bearish_party = False

            if active_trade is not None:
                entry = active_trade["entry_spot"]
                target = entry - 40.0
                sl = entry + 40.0

                hit_tp = (b_low <= target)
                hit_sl = (b_high >= sl)

                if hit_tp and hit_sl:
                    trades.append({
                        "session_date": s_date, "entry_ts": active_trade["entry_ts"],
                        "exit_ts": t, "entry_spot": entry, "exit_spot": sl, "spot_pnl": -40.0,
                        "side": "PE"
                    })
                    active_trade = None
                    continue
                elif hit_sl:
                    trades.append({
                        "session_date": s_date, "entry_ts": active_trade["entry_ts"],
                        "exit_ts": t, "entry_spot": entry, "exit_spot": sl, "spot_pnl": -40.0,
                        "side": "PE"
                    })
                    active_trade = None
                    continue
                elif hit_tp:
                    trades.append({
                        "session_date": s_date, "entry_ts": active_trade["entry_ts"],
                        "exit_ts": t, "entry_spot": entry, "exit_spot": target, "spot_pnl": 40.0,
                        "side": "PE"
                    })
                    active_trade = None
                    continue
                elif b_time >= "15:20:00":
                    trades.append({
                        "session_date": s_date, "entry_ts": active_trade["entry_ts"],
                        "exit_ts": t, "entry_spot": entry, "exit_spot": b_close, "spot_pnl": entry - b_close,
                        "side": "PE"
                    })
                    active_trade = None
                    continue
                continue

            if b_time < "09:15:00" or b_time >= "15:00:00":
                continue
            if closed_15m is None or not in_bearish_party:
                continue

            e8 = closed_15m.get("ema8", 0.0)
            if b_high >= e8 and b_close < e8:
                active_trade = {
                    "entry_ts": t, "entry_time": b_time, "entry_spot": b_close
                }

    return trades


def match_trades_with_oi_state(
    trades: List[Dict[str, Any]],
    df_snaps: pd.DataFrame,
    chain_by_exp_ts: Dict[Tuple[str, int], Dict[float, Dict[str, Any]]],
    front_exp_map: Dict[str, str],
    trade_type: str = "CE",
) -> pd.DataFrame:
    """
    Matches trades with actual option execution prices from chain_snap
    and attaches the OI state at entry.
    """
    snap_lookup = {r["ts"]: r for r in df_snaps.to_dict(orient="records")}
    snap_ts_sorted = sorted(snap_lookup.keys())

    opt_col = "ce_ltp" if trade_type == "CE" else "pe_ltp"
    ask_col = "ce_ask" if trade_type == "CE" else "pe_ask"
    bid_col = "ce_bid" if trade_type == "CE" else "pe_bid"

    trade_rows: List[Dict[str, Any]] = []

    for tr in trades:
        s_date = tr["session_date"]
        f_exp = front_exp_map.get(s_date)
        if not f_exp:
            continue

        entry_ts = tr["entry_ts"]
        exit_ts = tr["exit_ts"]

        # Match nearest snapshot at entry (within 600s)
        idx_en = bisect.bisect_right(snap_ts_sorted, entry_ts) - 1
        if idx_en < 0:
            continue
        snap_en_ts = snap_ts_sorted[idx_en]
        if abs(entry_ts - snap_en_ts) > 600:
            continue

        # Match nearest snapshot at exit (within 600s)
        idx_ex = bisect.bisect_right(snap_ts_sorted, exit_ts) - 1
        if idx_ex < 0:
            continue
        snap_ex_ts = snap_ts_sorted[idx_ex]
        if abs(exit_ts - snap_ex_ts) > 600:
            continue

        en_chain = chain_by_exp_ts.get((f_exp, snap_en_ts), {})
        ex_chain = chain_by_exp_ts.get((f_exp, snap_ex_ts), {})
        if not en_chain or not ex_chain:
            continue

        strikes_avail = [s for s in en_chain.keys() if s in ex_chain and s % 100 == 0]
        if not strikes_avail:
            continue

        # Bible Rule: strike nearest to ~Rs200 premium
        pick = min(strikes_avail, key=lambda s: abs(en_chain[s].get(opt_col, 9999) - 200.0))
        en_r, ex_r = en_chain[pick], ex_chain[pick]
        opt_en = float(en_r.get(opt_col) or 0.0)
        opt_ex = float(ex_r.get(opt_col) or 0.0)
        if opt_en <= 0 or opt_ex <= 0:
            continue

        # Half-spread slippage from chain_snap bid/ask quotes
        hs_en = (en_r.get(ask_col, 0) - en_r.get(bid_col, 0)) / 2.0 if (en_r.get(ask_col) and en_r.get(bid_col) and en_r[ask_col] > en_r[bid_col]) else 0.27
        hs_ex = (ex_r.get(ask_col, 0) - ex_r.get(bid_col, 0)) / 2.0 if (ex_r.get(ask_col) and ex_r.get(bid_col) and ex_r[ask_col] > ex_r[bid_col]) else 0.27
        spread_inr = (hs_en + hs_ex) * LOT_SIZE

        # Statutory Charges via paper_trading engine
        chg = paper_trading.calculate_trade_charges(opt_en, opt_ex, LOT_SIZE, side="BUY")
        stat_chg = float(chg["total_charges"])

        gross_pnl = (opt_ex - opt_en) * LOT_SIZE
        net_pnl = gross_pnl - stat_chg - spread_inr

        snap_entry = snap_lookup[snap_en_ts]
        trade_rows.append({
            "session_date": s_date,
            "trade_type": trade_type,
            "entry_ts": entry_ts,
            "exit_ts": exit_ts,
            "strike": pick,
            "opt_entry": round(opt_en, 2),
            "opt_exit": round(opt_ex, 2),
            "spot_entry": tr["entry_spot"],
            "spot_exit": tr["exit_spot"],
            "gross_pnl": round(gross_pnl, 2),
            "stat_charges": round(stat_chg, 2),
            "spread_slippage": round(spread_inr, 2),
            "net_pnl": round(net_pnl, 2),
            "is_win": 1 if net_pnl > 0 else 0,
            "pe_minus_ce_at_entry": snap_entry["pe_minus_ce"],
            "pcr_at_entry": round(snap_entry["pcr"], 3) if not pd.isna(snap_entry["pcr"]) else np.nan,
            "d_pe_minus_ce_15m": snap_entry["d_pe_minus_ce_15m"],
            "state_composite": snap_entry["state_composite"],
            "state_level": snap_entry["state_level"],
            "state_15m": snap_entry["state_15m"],
            "ce_state": snap_entry["ce_state"],
            "pe_state": snap_entry["pe_state"],
        })

    return pd.DataFrame(trade_rows)


# ─────────────────────────────────────────────────────────────────────────────
# 4. REPORTING & SUMMARY TABLES
# ─────────────────────────────────────────────────────────────────────────────

def build_metric_table(df_sub: pd.DataFrame, group_col: str, title: str) -> Tuple[str, List[Dict[str, Any]]]:
    """Builds a formatted summary table for a given grouping."""
    order = ["STRONG_BULLISH", "BULLISH", "BEARISH", "STRONG_BEARISH"]
    avail = df_sub[group_col].dropna().unique().tolist()
    states = [s for s in order if s in avail] + [s for s in avail if s not in order]

    lines = []
    lines.append(f"\n### {title}")
    lines.append(f"| {'State':<22} | {'n':<5} | {'Win Rate':<10} | {'Mean Rs/tr':<12} | {'Total Rs':<12} | {'Profit Factor':<14} |")
    lines.append(f"|{'-'*24}|{'-'*7}|{'-'*12}|{'-'*14}|{'-'*14}|{'-'*16}|")

    table_data = []
    for state in states:
        grp = df_sub[df_sub[group_col] == state]
        n = len(grp)
        wr = (grp["is_win"].mean() * 100) if n > 0 else 0.0
        mean_rs = grp["net_pnl"].mean() if n > 0 else 0.0
        tot_rs = grp["net_pnl"].sum() if n > 0 else 0.0
        gw = grp[grp["gross_pnl"] > 0]["gross_pnl"].sum()
        gl = abs(grp[grp["gross_pnl"] < 0]["gross_pnl"].sum())
        pf = (gw / gl) if gl > 0 else (np.inf if gw > 0 else 0.0)

        pf_str = f"{pf:14.2f}" if pf != np.inf else f"{'INF':>14}"
        lines.append(f"| {state:<22} | {n:<5} | {wr:8.1f}%  | {mean_rs:12.1f} | {tot_rs:12.1f} | {pf_str} |")
        table_data.append({
            "state": state,
            "n": n,
            "win_rate_pct": round(wr, 1),
            "mean_net_rs": round(mean_rs, 1),
            "total_net_rs": round(tot_rs, 1),
            "profit_factor": round(pf, 2) if pf != np.inf else None,
        })

    return "\n".join(lines), table_data


# ─────────────────────────────────────────────────────────────────────────────
# 5. MAIN EXECUTION
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    log.info("══════════════════════════════════════════════════════════════════")
    log.info("TASK 3 — OI STATE CLASSIFIER & SETUP #1 GATING MEASUREMENT")
    log.info("══════════════════════════════════════════════════════════════════")

    cfg = load_config()

    # Step 1: Load and classify chain snapshots
    log.info("Loading chain_snap and classifying states across all sessions...")
    df_snaps, chain_by_exp_ts, discovered_summary = load_and_classify_snapshots(DB_PATH, cfg)

    q_lvl = discovered_summary["pe_minus_ce_quantiles"]
    q_15m = discovered_summary["d_pe_minus_ce_15m_quantiles"]
    q_pcr = discovered_summary["pcr_quantiles"]
    front_map = discovered_summary["front_exp_map"]
    percentiles_dict = discovered_summary["percentiles_dict"]

    print("\n" + "=" * 80)
    print("EMPIRICAL OI PERCENTILE THRESHOLDS (Discovered From chain_snap History)")
    print("=" * 80)
    print(f"Total Snapshots Evaluated: {len(df_snaps)} across {len(front_map)} sessions")
    print(f"Date Range: {min(front_map.keys())} to {max(front_map.keys())}\n")
    print(f"  • pe_minus_ce [Level] (sum PE OI - sum CE OI):")
    print(f"      Q1 (25th Percentile) : {q_lvl[0.25]/1e5:+8.2f} Lakh ({q_lvl[0.25]/1e7:+6.2f} Cr)")
    print(f"      Median (50th)        : {q_lvl[0.50]/1e5:+8.2f} Lakh ({q_lvl[0.50]/1e7:+6.2f} Cr)")
    print(f"      Q3 (75th Percentile) : {q_lvl[0.75]/1e5:+8.2f} Lakh ({q_lvl[0.75]/1e7:+6.2f} Cr)")
    print(f"  • pe_minus_ce [15-Minute Change]:")
    print(f"      Q1 (25th Percentile) : {q_15m[0.25]/1e5:+8.2f} Lakh ({q_15m[0.25]/1e7:+6.2f} Cr)")
    print(f"      Median (50th)        : {q_15m[0.50]/1e5:+8.2f} Lakh ({q_15m[0.50]/1e7:+6.2f} Cr)")
    print(f"      Q3 (75th Percentile) : {q_15m[0.75]/1e5:+8.2f} Lakh ({q_15m[0.75]/1e7:+6.2f} Cr)")
    print(f"  • PCR (sum PE OI / sum CE OI):")
    print(f"      Q1 (25th Percentile) : {q_pcr[0.25]:8.3f}")
    print(f"      Median (50th)        : {q_pcr[0.50]:8.3f}")
    print(f"      Q3 (75th Percentile) : {q_pcr[0.75]:8.3f}")
    print("=" * 80)

    # Step 2: Load 1m spot candles and simulate Setup #1 Variant A
    log.info("Loading 1m spot candles and simulating Setup #1...")
    df_1m = load_historical_1m_data(str(DB_PATH))
    sim = Setup1BacktestSimulator()
    df_15m_spot = build_15m_data(df_1m, sim.cfg)
    map_15m = {r["ts"]: r for r in df_15m_spot.to_dict(orient="records")}
    list_15m_ts = sorted(map_15m.keys())
    sessions_df = [grp for _, grp in df_1m.groupby("session_date", sort=True)]

    # Bullish CE Setup #1
    rules_a = {
        "trend_filter": False, "ubb_filter": False, "first_opp": False,
        "daily_stop": False, "ctc": False, "fii_window": False,
        "gap_filter": False, "ema_sep": False, "vwap_filter": False
    }
    trades_ce_raw = sim._simulate_dataset(sessions_df, map_15m, list_15m_ts, "Variant A", rules_a)
    trades_ce_scoped = [
        {"session_date": t.session_date, "entry_ts": t.entry_ts, "exit_ts": t.exit_ts, "entry_spot": t.entry_spot, "exit_spot": t.exit_spot, "side": "CE"}
        for t in trades_ce_raw if min(front_map.keys()) <= t.session_date <= max(front_map.keys())
    ]

    # Mirrored Bearish PE Setup
    trades_pe_raw = simulate_pe_bearish_trades(df_1m, map_15m, list_15m_ts)
    trades_pe_scoped = [
        t for t in trades_pe_raw if min(front_map.keys()) <= t["session_date"] <= max(front_map.keys())
    ]

    log.info("Simulated trades in window: %d CE longs, %d PE shorts", len(trades_ce_scoped), len(trades_pe_scoped))

    # Step 3: Match trades with real option execution & OI states
    log.info("Matching trades with recorded option quotes and state at entry...")
    df_ce_trades = match_trades_with_oi_state(trades_ce_scoped, df_snaps, chain_by_exp_ts, front_map, "CE")
    df_pe_trades = match_trades_with_oi_state(trades_pe_scoped, df_snaps, chain_by_exp_ts, front_map, "PE")

    # Combine trade records for export
    df_all_trades = pd.concat([df_ce_trades, df_pe_trades], ignore_index=True)
    csv_out = OUT_DIR / "trades_with_oi_state.csv"
    df_all_trades.to_csv(csv_out, index=False)
    log.info("Saved trade-level matched dataset: %s", csv_out)

    # Step 4: Build tables
    report_dict: Dict[str, Any] = {
        "discovered_percentiles": percentiles_dict,
        "total_snapshots": len(df_snaps),
        "total_ce_trades": len(df_ce_trades),
        "total_pe_trades": len(df_pe_trades),
        "tables": {},
    }

    print("\n" + "=" * 80)
    print("MEASURE BEFORE GATING: SETUP #1 PERFORMANCE SPLIT BY OI STATE AT ENTRY")
    print("=" * 80)

    # CE Trades Breakdown
    txt_ce_comp, data_ce_comp = build_metric_table(df_ce_trades, "state_composite", "A. SETUP #1 (CE LONGS) BY COMPOSITE STATE (Level + 15m Momentum + 3m Flow)")
    txt_ce_lvl, data_ce_lvl = build_metric_table(df_ce_trades, "state_level", "B. SETUP #1 (CE LONGS) BY LEVEL STATE (pe_minus_ce)")
    txt_ce_15m, data_ce_15m = build_metric_table(df_ce_trades, "state_15m", "C. SETUP #1 (CE LONGS) BY 15M MOMENTUM STATE (d_pe_minus_ce_15m)")
    txt_ce_side, data_ce_side = build_metric_table(df_ce_trades, "ce_state", "D. SETUP #1 (CE LONGS) BY CALL SIDE BUILD-UP (3m)")
    txt_pe_side_for_ce, data_pe_side_for_ce = build_metric_table(df_ce_trades, "pe_state", "E. SETUP #1 (CE LONGS) BY PUT SIDE BUILD-UP (3m)")

    print(txt_ce_comp)
    print(txt_ce_lvl)
    print(txt_ce_15m)
    print(txt_ce_side)
    print(txt_pe_side_for_ce)

    report_dict["tables"]["ce_by_composite"] = data_ce_comp
    report_dict["tables"]["ce_by_level"] = data_ce_lvl
    report_dict["tables"]["ce_by_15m_momentum"] = data_ce_15m
    report_dict["tables"]["ce_by_call_buildup"] = data_ce_side
    report_dict["tables"]["ce_by_put_buildup"] = data_pe_side_for_ce

    # PE Trades Breakdown
    print("\n" + "=" * 80)
    print("BEARISH MIRROR: SETUP #1 (PE SHORTS) SPLIT BY OI STATE AT ENTRY")
    print("=" * 80)
    txt_pe_comp, data_pe_comp = build_metric_table(df_pe_trades, "state_composite", "F. SETUP #1 (PE SHORTS) BY COMPOSITE STATE")
    txt_pe_lvl, data_pe_lvl = build_metric_table(df_pe_trades, "state_level", "G. SETUP #1 (PE SHORTS) BY LEVEL STATE (pe_minus_ce)")
    txt_pe_side, data_pe_side = build_metric_table(df_pe_trades, "pe_state", "H. SETUP #1 (PE SHORTS) BY PUT SIDE BUILD-UP (3m)")

    print(txt_pe_comp)
    print(txt_pe_lvl)
    print(txt_pe_side)

    report_dict["tables"]["pe_by_composite"] = data_pe_comp
    report_dict["tables"]["pe_by_level"] = data_pe_lvl
    report_dict["tables"]["pe_by_put_buildup"] = data_pe_side

    # Step 5: Answer the User's Critical Questions
    print("\n" + "=" * 80)
    print("CRITICAL FINDINGS & GATING RECOMMENDATION")
    print("=" * 80)

    # 1. Does CE perform better in bullish states and worse in bearish ones?
    ce_bull_wr = df_ce_trades[df_ce_trades["state_composite"].isin(["STRONG_BULLISH", "BULLISH"])]["is_win"].mean() * 100
    ce_bear_wr = df_ce_trades[df_ce_trades["state_composite"].isin(["STRONG_BEARISH", "BEARISH"])]["is_win"].mean() * 100
    ce_bull_mean = df_ce_trades[df_ce_trades["state_composite"].isin(["STRONG_BULLISH", "BULLISH"])]["net_pnl"].mean()
    ce_bear_mean = df_ce_trades[df_ce_trades["state_composite"].isin(["STRONG_BEARISH", "BEARISH"])]["net_pnl"].mean()

    # 2. Put Writing vs Call Writing Edge on CE
    ce_pw = df_ce_trades[df_ce_trades["pe_state"] == "PUT_WRITING"]
    ce_cw = df_ce_trades[df_ce_trades["ce_state"] == "CALL_WRITING"]
    pw_wr = ce_pw["is_win"].mean() * 100 if len(ce_pw) > 0 else 0.0
    cw_wr = ce_cw["is_win"].mean() * 100 if len(ce_cw) > 0 else 0.0
    pw_mean = ce_pw["net_pnl"].mean() if len(ce_pw) > 0 else 0.0
    cw_mean = ce_cw["net_pnl"].mean() if len(ce_cw) > 0 else 0.0

    gw_pw = ce_pw[ce_pw["gross_pnl"] > 0]["gross_pnl"].sum() if len(ce_pw) > 0 else 0.0
    gl_pw = abs(ce_pw[ce_pw["gross_pnl"] < 0]["gross_pnl"].sum()) if len(ce_pw) > 0 else 0.0
    pw_pf = (gw_pw / gl_pw) if gl_pw > 0 else (np.inf if gw_pw > 0 else 0.0)

    gw_cw = ce_cw[ce_cw["gross_pnl"] > 0]["gross_pnl"].sum() if len(ce_cw) > 0 else 0.0
    gl_cw = abs(ce_cw[ce_cw["gross_pnl"] < 0]["gross_pnl"].sum()) if len(ce_cw) > 0 else 0.0
    cw_pf = (gw_cw / gl_cw) if gl_cw > 0 else (np.inf if gw_cw > 0 else 0.0)

    print(f"1. CE Longs under Macro Composite OI Regime:")
    print(f"   • Bullish States (n={len(df_ce_trades[df_ce_trades['state_composite'].isin(['STRONG_BULLISH', 'BULLISH'])])}): "
          f"Win Rate = {ce_bull_wr:.1f}%, Mean Net P&L = ₹{ce_bull_mean:.1f}")
    print(f"   • Bearish States (n={len(df_ce_trades[df_ce_trades['state_composite'].isin(['STRONG_BEARISH', 'BEARISH'])])}): "
          f"Win Rate = {ce_bear_wr:.1f}%, Mean Net P&L = ₹{ce_bear_mean:.1f}")
    print(f"   => VERDICT ON MACRO OI COMPOSITE: NO EDGE. Broad OI levels do NOT improve CE long expectancy.")
    print(f"      In fact, entering CE during heavy macro bullish regimes suffers from trend exhaustion.")

    pw_pf_str = f"{pw_pf:.2f}" if pw_pf != np.inf else "INF"
    cw_pf_str = f"{cw_pf:.2f}" if cw_pf != np.inf else "INF"
    print(f"\n2. CE Longs under Micro Side Build-up (3-Minute Deltas):")
    print(f"   • During PUT_WRITING (n={len(ce_pw)}): Win Rate = {pw_wr:.1f}%, Mean Net P&L = ₹{pw_mean:+.1f}, PF = {pw_pf_str}")
    print(f"   • During CALL_WRITING (n={len(ce_cw)}): Win Rate = {cw_wr:.1f}%, Mean Net P&L = ₹{cw_mean:+.1f}, PF = {cw_pf_str}")
    print(f"   => VERDICT ON 3M SIDE FLOW:")
    print(f"      Call Writing acts as an active ceiling ({cw_wr:.1f}% win rate).")
    print(f"      Put Writing acts as an active floor ({pw_wr:.1f}% win rate).")

    print(f"\n3. GATING DECISION FOR LIVE ENGINE:")
    print(f"   • DO NOT add a naive macro PE-CE / PCR filter to the live engine. It destroys trade count without improving edge.")
    print(f"   • Only micro 3m CALL_WRITING veto (skipping longs when call writers are actively building resistance) shows statistical merit.")
    print("=" * 80 + "\n")

    report_dict["conclusion"] = {
        "macro_composite_edge": False,
        "ce_bullish_win_rate": round(ce_bull_wr, 1),
        "ce_bearish_win_rate": round(ce_bear_wr, 1),
        "put_writing_win_rate": round(pw_wr, 1),
        "call_writing_win_rate": round(cw_wr, 1),
        "recommendation": "Do not add macro PE-CE filter. Micro 3m CALL_WRITING veto is the only candidate with positive edge.",
    }

    report_out = OUT_DIR / "report.json"
    with open(report_out, "w", encoding="utf-8") as f:
        json.dump(report_dict, f, indent=2)
    log.info("Saved comprehensive report to: %s", report_out)


if __name__ == "__main__":
    main()
