"""
writers_ratio_backtest.py — Quantitative Honest Backtest for Writers Ratio (Support / Resistance Labels).

Strict anti-lookahead architecture:
1. The entry timestamp is strictly `pivot.confirmed_i` (NEVER `pivot.i`).
2. The entry price is strictly `close[confirmed_i]`.
3. MFE and MAE are measured forward from `close[confirmed_i]`.
4. Wilson 95% confidence intervals are reported for every win rate.
5. Below n=30, prints INSUFFICIENT SAMPLE.
6. Baseline is computed with the ratio ignored; EDGE = bucket win rate - baseline.
7. Fixed threshold at 1.35; sensitivity ladder reported for 1.0, 1.2, 1.35, 1.5, 1.8, 2.0.
8. Forbidden output guard: if any R:R > 4.0:1 or 100% win rate or zero MAE is detected, stops with error.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime, date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

import config
import store
import smc
import writers_ratio as wr

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("writers_ratio_backtest")

RESULTS_CSV = config.DATA_DIR / "writers_ratio_backtest_results.csv"
SCORECARD_JSON = config.DATA_DIR / "writers_ratio_scorecard.json"


def wilson_interval(k: int, n: int, z: float = 1.95996) -> Tuple[float, float, float]:
    """Computes Wilson score 95% confidence interval for k successes out of n trials."""
    if n <= 0:
        return 0.0, 0.0, 0.0
    p = k / n
    denominator = 1.0 + (z**2) / n
    centre = (p + (z**2) / (2.0 * n)) / denominator
    half_width = (z * math.sqrt((p * (1.0 - p) / n) + (z**2) / (4.0 * (n**2)))) / denominator
    lower = max(0.0, centre - half_width)
    upper = min(1.0, centre + half_width)
    return p * 100.0, lower * 100.0, upper * 100.0


def run_honest_backtest(cfg: Optional[Dict[str, Any]] = None) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """Runs the complete honest backtest across all available historical sessions."""
    rules = cfg or wr.load_rules()
    target_pts = float(rules.get("mfe_target_pts", 25.0))
    stop_pts = float(rules.get("mae_stop_pts", 25.0))
    swing_n = int(rules.get("swing_n", 2))
    one_way_th = float(rules.get("one_way_day_threshold", 0.70))

    snap_days = set(d.name for d in config.SNAPSHOT_DIR.iterdir() if d.is_dir() and len(list(d.glob("*.parquet"))) >= 30)
    candle_days = set(f.stem for f in config.CANDLE_15M_DIR.glob("*.csv"))
    sessions = sorted(list(snap_days.intersection(candle_days)))

    log.info(f"Running honest backtest on {len(sessions)} sessions (dates {sessions[0] if sessions else 'N/A'} to {sessions[-1] if sessions else 'N/A'})...")

    records: List[Dict[str, Any]] = []

    for sess_str in sessions:
        d = datetime.strptime(sess_str, "%Y-%m-%d").date()
        c_file = config.CANDLE_15M_DIR / f"{sess_str}.csv"
        try:
            c_df = pd.read_csv(c_file)
        except Exception as e:
            log.warning(f"Failed to read {c_file}: {e}")
            continue

        c_df["timestamp"] = pd.to_datetime(c_df["timestamp"])
        c_df = c_df.sort_values("timestamp").reset_index(drop=True)
        # Filter to market hours 09:15 to 15:30
        c_df = c_df[(c_df["timestamp"].dt.time >= config.MARKET_OPEN) & (c_df["timestamp"].dt.time <= config.MARKET_CLOSE)].reset_index(drop=True)
        n_bars = len(c_df)
        if n_bars < 8:
            continue

        # Check for one-way trending day (>70% candles same color)
        green = (c_df["close"] > c_df["open"]).sum()
        red = (c_df["close"] < c_df["open"]).sum()
        is_one_way = (green / n_bars >= one_way_th) or (red / n_bars >= one_way_th)

        snaps = store.load_day(d)
        if not snaps or len(snaps) < 2:
            continue

        pivs = smc.pivots(c_df, n=swing_n)

        for p in pivs:
            c_i = p.confirmed_i
            # Pivot MUST be confirmed before the final candle of the day to evaluate forward price action
            if c_i >= n_bars - 1:
                continue

            entry_time = c_df.loc[c_i, "timestamp"]
            entry_price = float(c_df.loc[c_i, "close"])
            pivot_time = p.at
            pivot_price = float(p.price)
            lag_bars = c_i - p.i

            # Compute flow strictly at confirmation bar
            wf = wr.evaluate_pivot_writer_flow(p, c_df, snaps, rules=rules)
            if wf.label == "NO_READ" and "Missing snapshots" in wf.note:
                continue

            # Forward evaluation starting from bar confirmed_i + 1
            fwd = c_df.iloc[c_i + 1 :].reset_index(drop=True)
            if fwd.empty:
                continue

            win_25 = False
            loss_25 = False
            mfe_val = 0.0
            mae_val = 0.0

            if p.kind == "LOW":
                # Long trade
                for f_idx in range(len(fwd)):
                    bar_h = float(fwd.loc[f_idx, "high"])
                    bar_l = float(fwd.loc[f_idx, "low"])

                    fav = bar_h - entry_price
                    adv = entry_price - bar_l

                    if fav > mfe_val:
                        mfe_val = fav
                    if adv > mae_val:
                        mae_val = adv

                    if not win_25 and not loss_25:
                        if adv >= stop_pts and fav < target_pts:
                            loss_25 = True
                        elif fav >= target_pts and adv < stop_pts:
                            win_25 = True

                # 30-minute price change (2 candles forward)
                fwd_30m = float(fwd.loc[1, "close"] - entry_price) if len(fwd) >= 2 else float(fwd.loc[0, "close"] - entry_price)

                records.append({
                    "session": sess_str,
                    "is_one_way": is_one_way,
                    "kind": "SWING_LOW",
                    "pivot_price": pivot_price,
                    "pivot_time": pivot_time.strftime("%H:%M"),
                    "entry_time": entry_time.strftime("%H:%M"),
                    "entry_price": entry_price,
                    "lag_bars": lag_bars,
                    "d_ce": wf.ce_oi_delta,
                    "d_pe": wf.pe_oi_delta,
                    "ratio": wf.ratio,
                    "strength": wf.strength,
                    "label": wf.label,
                    "churn_flag": wf.churn_flag,
                    "win_25": win_25,
                    "loss_25": loss_25,
                    "mfe_25": mfe_val,
                    "mae_25": mae_val,
                    "fwd_30m": fwd_30m,
                    "note": wf.note,
                })

            elif p.kind == "HIGH":
                # Short trade
                for f_idx in range(len(fwd)):
                    bar_h = float(fwd.loc[f_idx, "high"])
                    bar_l = float(fwd.loc[f_idx, "low"])

                    fav = entry_price - bar_l
                    adv = bar_h - entry_price

                    if fav > mfe_val:
                        mfe_val = fav
                    if adv > mae_val:
                        mae_val = adv

                    if not win_25 and not loss_25:
                        if adv >= stop_pts and fav < target_pts:
                            loss_25 = True
                        elif fav >= target_pts and adv < stop_pts:
                            win_25 = True

                fwd_30m = float(entry_price - fwd.loc[1, "close"]) if len(fwd) >= 2 else float(entry_price - fwd.loc[0, "close"])

                records.append({
                    "session": sess_str,
                    "is_one_way": is_one_way,
                    "kind": "SWING_HIGH",
                    "pivot_price": pivot_price,
                    "pivot_time": pivot_time.strftime("%H:%M"),
                    "entry_time": entry_time.strftime("%H:%M"),
                    "entry_price": entry_price,
                    "lag_bars": lag_bars,
                    "d_ce": wf.ce_oi_delta,
                    "d_pe": wf.pe_oi_delta,
                    "ratio": wf.ratio,
                    "strength": wf.strength,
                    "label": wf.label,
                    "churn_flag": wf.churn_flag,
                    "win_25": win_25,
                    "loss_25": loss_25,
                    "mfe_25": mfe_val,
                    "mae_25": mae_val,
                    "fwd_30m": fwd_30m,
                    "note": wf.note,
                })

    df_out = pd.DataFrame(records)
    RESULTS_CSV.parent.mkdir(parents=True, exist_ok=True)
    df_out.to_csv(RESULTS_CSV, index=False)
    log.info(f"Saved raw backtest output to {RESULTS_CSV}")

    # Build Complete Scorecard Summary
    scorecard = build_scorecard_summary(df_out, rules, sessions)
    SCORECARD_JSON.write_text(json.dumps(scorecard, indent=2))
    log.info(f"Saved scorecard summary to {SCORECARD_JSON}")

    # FORBIDDEN OUTPUTS INTEGRITY CHECK
    verify_backtest_integrity(scorecard)

    return df_out, scorecard


def build_scorecard_summary(df: pd.DataFrame, rules: Dict[str, Any], sessions: List[str]) -> Dict[str, Any]:
    """Generates the comprehensive scorecard with baselines, edges, Wilson intervals, and sensitivity ladders."""
    min_sample = int(rules.get("min_sample_size", 30))
    fixed_th = float(rules.get("ratio_threshold", 1.35))
    ladder_ths = rules.get("ratio_ladder", [1.0, 1.2, 1.35, 1.5, 1.8, 2.0])

    scorecard: Dict[str, Any] = {
        "metadata": {
            "sessions_count": len(sessions),
            "date_range": f"{sessions[0]} to {sessions[-1]}" if sessions else "N/A",
            "total_pivots": len(df),
            "swing_lows_count": len(df[df["kind"] == "SWING_LOW"]),
            "swing_highs_count": len(df[df["kind"] == "SWING_HIGH"]),
            "fixed_threshold": fixed_th,
            "min_sample_size": min_sample,
            "generated_at": datetime.now().isoformat(),
        },
        "swing_lows": {},
        "swing_highs": {},
        "threshold_ladder": [],
        "per_day_breakdown": [],
    }

    # 1. SWING LOWS
    df_sl = df[df["kind"] == "SWING_LOW"]
    n_sl_base = len(df_sl)
    k_sl_base = int(df_sl["win_25"].sum()) if n_sl_base > 0 else 0
    p_sl_base, low_sl_base, high_sl_base = wilson_interval(k_sl_base, n_sl_base)

    scorecard["swing_lows"]["baseline"] = {
        "n": n_sl_base,
        "wins": k_sl_base,
        "win_rate": p_sl_base,
        "wilson_lower": low_sl_base,
        "wilson_upper": high_sl_base,
        "avg_mfe": float(df_sl["mfe_25"].mean()) if n_sl_base > 0 else 0.0,
        "avg_mae": float(df_sl["mae_25"].mean()) if n_sl_base > 0 else 0.0,
        "reward_risk": (float(df_sl["mfe_25"].mean()) / max(float(df_sl["mae_25"].mean()), 0.01)) if n_sl_base > 0 else 0.0,
        "avg_30m": float(df_sl["fwd_30m"].mean()) if n_sl_base > 0 else 0.0,
    }

    # SUPPORT Bucket (ratio >= fixed_th)
    df_supp = df_sl[df_sl["label"] == "SUPPORT"]
    n_supp = len(df_supp)
    k_supp = int(df_supp["win_25"].sum()) if n_supp > 0 else 0
    p_supp, low_supp, high_supp = wilson_interval(k_supp, n_supp)
    edge_supp = p_supp - p_sl_base

    scorecard["swing_lows"]["support"] = {
        "n": n_supp,
        "wins": k_supp,
        "win_rate": p_supp,
        "wilson_lower": low_supp,
        "wilson_upper": high_supp,
        "edge_vs_baseline": edge_supp,
        "is_sufficient_sample": n_supp >= min_sample,
        "avg_mfe": float(df_supp["mfe_25"].mean()) if n_supp > 0 else 0.0,
        "avg_mae": float(df_supp["mae_25"].mean()) if n_supp > 0 else 0.0,
        "reward_risk": (float(df_supp["mfe_25"].mean()) / max(float(df_supp["mae_25"].mean()), 0.01)) if n_supp > 0 else 0.0,
        "avg_30m": float(df_supp["fwd_30m"].mean()) if n_supp > 0 else 0.0,
    }

    # CONTESTED Lows
    df_sl_cont = df_sl[df_sl["label"] == "CONTESTED"]
    n_sl_cont = len(df_sl_cont)
    k_sl_cont = int(df_sl_cont["win_25"].sum()) if n_sl_cont > 0 else 0
    p_sl_cont, low_sl_cont, high_sl_cont = wilson_interval(k_sl_cont, n_sl_cont)
    scorecard["swing_lows"]["contested"] = {
        "n": n_sl_cont,
        "wins": k_sl_cont,
        "win_rate": p_sl_cont,
        "wilson_lower": low_sl_cont,
        "wilson_upper": high_sl_cont,
        "edge_vs_baseline": p_sl_cont - p_sl_base,
        "is_sufficient_sample": n_sl_cont >= min_sample,
        "avg_mfe": float(df_sl_cont["mfe_25"].mean()) if n_sl_cont > 0 else 0.0,
        "avg_mae": float(df_sl_cont["mae_25"].mean()) if n_sl_cont > 0 else 0.0,
        "reward_risk": (float(df_sl_cont["mfe_25"].mean()) / max(float(df_sl_cont["mae_25"].mean()), 0.01)) if n_sl_cont > 0 else 0.0,
        "avg_30m": float(df_sl_cont["fwd_30m"].mean()) if n_sl_cont > 0 else 0.0,
    }

    # 2. SWING HIGHS
    df_sh = df[df["kind"] == "SWING_HIGH"]
    n_sh_base = len(df_sh)
    k_sh_base = int(df_sh["win_25"].sum()) if n_sh_base > 0 else 0
    p_sh_base, low_sh_base, high_sh_base = wilson_interval(k_sh_base, n_sh_base)

    scorecard["swing_highs"]["baseline"] = {
        "n": n_sh_base,
        "wins": k_sh_base,
        "win_rate": p_sh_base,
        "wilson_lower": low_sh_base,
        "wilson_upper": high_sh_base,
        "avg_mfe": float(df_sh["mfe_25"].mean()) if n_sh_base > 0 else 0.0,
        "avg_mae": float(df_sh["mae_25"].mean()) if n_sh_base > 0 else 0.0,
        "reward_risk": (float(df_sh["mfe_25"].mean()) / max(float(df_sh["mae_25"].mean()), 0.01)) if n_sh_base > 0 else 0.0,
        "avg_30m": float(df_sh["fwd_30m"].mean()) if n_sh_base > 0 else 0.0,
    }

    # RESISTANCE Bucket (ratio >= fixed_th)
    df_res = df_sh[df_sh["label"] == "RESISTANCE"]
    n_res = len(df_res)
    k_res = int(df_res["win_25"].sum()) if n_res > 0 else 0
    p_res, low_res, high_res = wilson_interval(k_res, n_res)
    edge_res = p_res - p_sh_base

    scorecard["swing_highs"]["resistance"] = {
        "n": n_res,
        "wins": k_res,
        "win_rate": p_res,
        "wilson_lower": low_res,
        "wilson_upper": high_res,
        "edge_vs_baseline": edge_res,
        "is_sufficient_sample": n_res >= min_sample,
        "avg_mfe": float(df_res["mfe_25"].mean()) if n_res > 0 else 0.0,
        "avg_mae": float(df_res["mae_25"].mean()) if n_res > 0 else 0.0,
        "reward_risk": (float(df_res["mfe_25"].mean()) / max(float(df_res["mae_25"].mean()), 0.01)) if n_res > 0 else 0.0,
        "avg_30m": float(df_res["fwd_30m"].mean()) if n_res > 0 else 0.0,
    }

    # CONTESTED Highs
    df_sh_cont = df_sh[df_sh["label"] == "CONTESTED"]
    n_sh_cont = len(df_sh_cont)
    k_sh_cont = int(df_sh_cont["win_25"].sum()) if n_sh_cont > 0 else 0
    p_sh_cont, low_sh_cont, high_sh_cont = wilson_interval(k_sh_cont, n_sh_cont)
    scorecard["swing_highs"]["contested"] = {
        "n": n_sh_cont,
        "wins": k_sh_cont,
        "win_rate": p_sh_cont,
        "wilson_lower": low_sh_cont,
        "wilson_upper": high_sh_cont,
        "edge_vs_baseline": p_sh_cont - p_sh_base,
        "is_sufficient_sample": n_sh_cont >= min_sample,
        "avg_mfe": float(df_sh_cont["mfe_25"].mean()) if n_sh_cont > 0 else 0.0,
        "avg_mae": float(df_sh_cont["mae_25"].mean()) if n_sh_cont > 0 else 0.0,
        "reward_risk": (float(df_sh_cont["mfe_25"].mean()) / max(float(df_sh_cont["mae_25"].mean()), 0.01)) if n_sh_cont > 0 else 0.0,
        "avg_30m": float(df_sh_cont["fwd_30m"].mean()) if n_sh_cont > 0 else 0.0,
    }

    # 3. THRESHOLD SENSITIVITY LADDER
    for t in ladder_ths:
        t_val = float(t)
        sub_l = df_sl[df_sl["ratio"] >= t_val]
        n_l = len(sub_l)
        k_l = int(sub_l["win_25"].sum()) if n_l > 0 else 0
        p_l, low_l, high_l = wilson_interval(k_l, n_l)

        sub_h = df_sh[df_sh["ratio"] >= t_val]
        n_h = len(sub_h)
        k_h = int(sub_h["win_25"].sum()) if n_h > 0 else 0
        p_h, low_h, high_h = wilson_interval(k_h, n_h)

        scorecard["threshold_ladder"].append({
            "threshold": t_val,
            "is_fixed_default": abs(t_val - fixed_th) < 1e-4,
            "swing_low_n": n_l,
            "swing_low_win_rate": p_l,
            "swing_low_lower": low_l,
            "swing_low_upper": high_l,
            "swing_low_edge": p_l - p_sl_base,
            "swing_high_n": n_h,
            "swing_high_win_rate": p_h,
            "swing_high_lower": low_h,
            "swing_high_upper": high_h,
            "swing_high_edge": p_h - p_sh_base,
        })

    # 4. PER-DAY BREAKDOWN
    for sess_str in sessions:
        sub_day = df[df["session"] == sess_str]
        n_day = len(sub_day)
        is_ow = bool(sub_day["is_one_way"].iloc[0]) if n_day > 0 else False
        wins_day = int(sub_day["win_25"].sum()) if n_day > 0 else 0
        scorecard["per_day_breakdown"].append({
            "session": sess_str,
            "pivots_count": n_day,
            "wins_count": wins_day,
            "is_one_way_day": is_ow,
            "win_rate": (wins_day / n_day * 100.0) if n_day > 0 else 0.0,
            "note": "⚠️ One-Way Trending Day (>70% single colour candles)" if is_ow else "Balanced Session",
        })

    return scorecard


def verify_backtest_integrity(scorecard: Dict[str, Any]) -> None:
    """
    Defensive Integrity Guard:
    Checks for forbidden look-ahead signatures:
    - Any Reward:Risk > 4.0:1
    - Any Win Rate == 100.0%
    - Any Average MAE == 0.0 points
    """
    anomalies: List[str] = []

    for k in ["swing_lows", "swing_highs"]:
        for stance in ["baseline", "support", "resistance", "contested"]:
            data = scorecard[k].get(stance)
            if not data or data.get("n", 0) == 0:
                continue
            rr = data.get("reward_risk", 0.0)
            wr_val = data.get("win_rate", 0.0)
            mae = data.get("avg_mae", 99.0)

            if rr > 4.0:
                anomalies.append(f"Forbidden Reward:Risk {rr:.2f}:1 (>4.0:1) in {k}.{stance}")
            if wr_val >= 99.9 and data["n"] >= 5:
                anomalies.append(f"Forbidden 100% Win Rate in {k}.{stance} (N={data['n']})")
            if mae <= 0.01 and data["n"] >= 5:
                anomalies.append(f"Forbidden Zero Adverse Excursion (MAE=0.0) in {k}.{stance}")

    if anomalies:
        log.error("LOOK-AHEAD BIAS ANOMALIES DETECTED IN BACKTEST RESULTS:")
        for a in anomalies:
            log.error(f"  ❌ {a}")
        raise ValueError(f"Backtest integrity failed: {anomalies}")
    else:
        log.info("✅ Defensive Backtest Integrity Verification Passed: Zero Look-Ahead Bias Detected.")


if __name__ == "__main__":
    df_res, sc = run_honest_backtest()
    print("\n" + "="*85)
    print("HONEST SCORECARD REPORT (WITHOUT LOOK-AHEAD BIAS)")
    print("="*85)
    print(f"Dataset Provenance: {sc['metadata']['sessions_count']} sessions ({sc['metadata']['date_range']}) | Total Confirmed Pivots: {sc['metadata']['total_pivots']}")
    print(f"Fixed Threshold: {sc['metadata']['fixed_threshold']:.2f}x (Defined in writers_ratio_rules.json in advance)")
    print("-" * 85)
    
    sl_base = sc["swing_lows"]["baseline"]
    sl_supp = sc["swing_lows"]["support"]
    sl_cont = sc["swing_lows"]["contested"]
    
    print(f"SWING LOWS (N={sl_base['n']}):")
    print(f"  BASELINE (Ratio Ignored):   Win Rate: {sl_base['win_rate']:.1f}% [Wilson 95%: {sl_base['wilson_lower']:.1f}% - {sl_base['wilson_upper']:.1f}%] | R:R {sl_base['reward_risk']:.2f}:1")
    supp_warn = " [INSUFFICIENT SAMPLE (<30)]" if not sl_supp["is_sufficient_sample"] else ""
    print(f"  SUPPORT  (PE/CE >= 1.35):   Win Rate: {sl_supp['win_rate']:.1f}% [Wilson 95%: {sl_supp['wilson_lower']:.1f}% - {sl_supp['wilson_upper']:.1f}%] | EDGE: {sl_supp['edge_vs_baseline']:+.1f}% | R:R {sl_supp['reward_risk']:.2f}:1 (N={sl_supp['n']}){supp_warn}")
    print(f"  CONTESTED (Opposed):        Win Rate: {sl_cont['win_rate']:.1f}% [Wilson 95%: {sl_cont['wilson_lower']:.1f}% - {sl_cont['wilson_upper']:.1f}%] | EDGE: {sl_cont['edge_vs_baseline']:+.1f}% | R:R {sl_cont['reward_risk']:.2f}:1 (N={sl_cont['n']})")
    
    sh_base = sc["swing_highs"]["baseline"]
    sh_res = sc["swing_highs"]["resistance"]
    sh_cont = sc["swing_highs"]["contested"]
    
    print(f"\nSWING HIGHS (N={sh_base['n']}):")
    print(f"  BASELINE (Ratio Ignored):   Win Rate: {sh_base['win_rate']:.1f}% [Wilson 95%: {sh_base['wilson_lower']:.1f}% - {sh_base['wilson_upper']:.1f}%] | R:R {sh_base['reward_risk']:.2f}:1")
    res_warn = " [INSUFFICIENT SAMPLE (<30)]" if not sh_res["is_sufficient_sample"] else ""
    print(f"  RESISTANCE (CE/PE >= 1.35): Win Rate: {sh_res['win_rate']:.1f}% [Wilson 95%: {sh_res['wilson_lower']:.1f}% - {sh_res['wilson_upper']:.1f}%] | EDGE: {sh_res['edge_vs_baseline']:+.1f}% | R:R {sh_res['reward_risk']:.2f}:1 (N={sh_res['n']}){res_warn}")
    print(f"  CONTESTED (Opposed):        Win Rate: {sh_cont['win_rate']:.1f}% [Wilson 95%: {sh_cont['wilson_lower']:.1f}% - {sh_cont['wilson_upper']:.1f}%] | EDGE: {sh_cont['edge_vs_baseline']:+.1f}% | R:R {sh_cont['reward_risk']:.2f}:1 (N={sh_cont['n']})")
    
    print("\nTHRESHOLD SENSITIVITY LADDER (Fixed 1.35x chosen in advance; ladder shown for sensitivity):")
    df_lad = pd.DataFrame(sc["threshold_ladder"])
    print(df_lad[["threshold", "is_fixed_default", "swing_low_n", "swing_low_win_rate", "swing_low_edge", "swing_high_n", "swing_high_win_rate", "swing_high_edge"]].to_string(index=False))
    print("=" * 85)
