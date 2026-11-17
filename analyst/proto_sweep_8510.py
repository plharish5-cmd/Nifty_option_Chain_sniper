"""Staged filter sweep + Monte Carlo for the 8 EMA Prototype (reuses backtest_lab_8510).

See claude/Backtest_Lab_Guide_2026-11-13.md and project handover docs. Rank configs on
held-out days only; Benjamini–Hochberg FDR at q=0.10; block-bootstrap by trading day.

CLI:
    python3 -m analyst.proto_sweep_8510 --from YYYY-MM-DD --to YYYY-MM-DD --stage all
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import random
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

ROOT = Path(__file__).resolve().parent.parent
IST = timezone(timedelta(hours=5, minutes=30))
SWEEPS_DIR = ROOT / "data/terminal8510/sweeps"

from analyst import backtest_lab_8510 as lab  # noqa: E402
from analyst.oi_3m_gate_8510 import DEFAULT_OI_3M_FEATURE  # noqa: E402
from analyst.proto_oi_filters_8510 import DEFAULT_OI_FILTERS, PROTO_OI_READ_IDS  # noqa: E402


def _deepcopy_default_oi_filters() -> Dict[str, Any]:
    sw = dict(DEFAULT_OI_FILTERS["switches"])
    mom = dict(DEFAULT_OI_FILTERS["momentum"])
    return {
        "mode": "log_only",
        "rule": "all_agree",
        "neutral_blocks": False,
        "max_age_sec": DEFAULT_OI_FILTERS["max_age_sec"],
        "switches": sw,
        "momentum": mom,
    }


def baseline_proto_overrides() -> Dict[str, Any]:
    """Every prototype filter OFF — reference configuration."""
    return {
        "oi_filters": _deepcopy_default_oi_filters(),
        "candle_trail": {
            "enabled": False,
            "activate_pts": 40.0,
            "candle_tf_min": 15,
            "use_completed_candles": True,
        },
        "oi_3m_feature": {
            "enabled": False,
            "gate": {"enabled": False, "mode": "log_only"},
        },
    }


def _oi_block_mode(switch_id: str, *, mode: str = "block") -> Dict[str, Any]:
    oi = _deepcopy_default_oi_filters()
    oi["mode"] = mode
    for k in PROTO_OI_READ_IDS:
        oi["switches"][k] = k == switch_id
    return {"oi_filters": oi}


def _momentum_switches_block() -> Dict[str, Any]:
    oi = _deepcopy_default_oi_filters()
    oi["mode"] = "block"
    for k in ("writer_flow", "total_oi", "fresh_oi"):
        oi["switches"][k] = True
    return {"oi_filters": oi}


def _momentum_switches_log() -> Dict[str, Any]:
    oi = _deepcopy_default_oi_filters()
    oi["mode"] = "log_only"
    for k in ("writer_flow", "total_oi", "fresh_oi"):
        oi["switches"][k] = True
    return {"oi_filters": oi}


def stage1_variants() -> List[Tuple[str, str, Dict[str, Any]]]:
    """(id, label, proto_overrides) — one factor vs baseline."""
    out: List[Tuple[str, str, Dict[str, Any]]] = []
    out.append(("momentum_block", "Momentum reads (block)", _momentum_switches_block()))
    out.append(("momentum_log", "Momentum reads (log only)", _momentum_switches_log()))
    for rid in PROTO_OI_READ_IDS:
        out.append((f"oi_{rid}", f"OI switch: {rid} (block)", _oi_block_mode(rid, mode="block")))
    oi3 = {
        "oi_3m_feature": {
            "enabled": True,
            "gate": {**DEFAULT_OI_3M_FEATURE["gate"], "enabled": True, "mode": "block"},
        }
    }
    out.append(("oi_3m_gate", "oi_3m gate (block)", oi3))
    vol = baseline_proto_overrides()
    vol["oi_filters"] = _deepcopy_default_oi_filters()
    vol["oi_filters"]["momentum"]["require_volume_confirm"] = True
    vol["oi_filters"]["mode"] = "block"
    for k in ("writer_flow", "total_oi", "fresh_oi"):
        vol["oi_filters"]["switches"][k] = True
    out.append(("require_volume_confirm", "require_volume_confirm + momentum block", vol))
    cumd = baseline_proto_overrides()
    cumd["oi_filters"] = _deepcopy_default_oi_filters()
    cumd["oi_filters"]["momentum"]["require_cumd_confirm"] = True
    cumd["oi_filters"]["mode"] = "block"
    for k in ("writer_flow", "total_oi", "fresh_oi"):
        cumd["oi_filters"]["switches"][k] = True
    out.append(("require_cumd_confirm", "require_cumd_confirm + momentum block", cumd))
    for tf in (15, 5, 3):
        tr = baseline_proto_overrides()
        tr["candle_trail"] = {
            "enabled": True,
            "activate_pts": 40.0,
            "candle_tf_min": tf,
            "use_completed_candles": True,
        }
        out.append((f"candle_trail_{tf}m", f"Candle trail ({tf}m)", tr))
    for rule in ("all_agree", "any_agrees", "majority"):
        r = _oi_block_mode("total_oi", mode="block")
        r["oi_filters"]["rule"] = rule
        r["oi_filters"]["switches"]["fresh_oi"] = True
        out.append((f"rule_{rule}", f"Combine rule: {rule} (total+fresh block)", r))
    nb = _oi_block_mode("volume_oi", mode="block")
    nb["oi_filters"]["neutral_blocks"] = True
    out.append(("neutral_blocks", "neutral_blocks ON + volume_oi block", nb))
    return out


def merge_overrides(base: Dict[str, Any], extra: Dict[str, Any]) -> Dict[str, Any]:
    """Shallow-merge two proto_override dicts for combo configs."""
    out: Dict[str, Any] = {}
    for key in ("oi_filters", "candle_trail", "oi_3m_feature"):
        a = base.get(key)
        b = extra.get(key)
        if not a and not b:
            continue
        merged: Dict[str, Any] = dict(a or {})
        if isinstance(b, dict):
            merged.update({k: v for k, v in b.items() if k not in ("switches", "momentum", "gate")})
            if key == "oi_filters":
                if isinstance(b.get("switches"), dict):
                    merged["switches"] = {**(merged.get("switches") or {}), **b["switches"]}
                if isinstance(b.get("momentum"), dict):
                    merged["momentum"] = {**(merged.get("momentum") or {}), **b["momentum"]}
            if key == "oi_3m_feature" and isinstance(b.get("gate"), dict):
                merged["gate"] = {**(merged.get("gate") or {}), **b["gate"]}
        out[key] = merged
    return out


def trade_key(t: Dict[str, Any]) -> str:
    ent = t.get("entry")
    return f"{t['day']}|{t.get('entry_hm','')}|{t.get('side','')}|{ent:.2f}" if ent else f"{t['day']}|{t.get('entry_hm','')}"


def trades_on_days(trades: List[Dict[str, Any]], days: Set[str]) -> List[Dict[str, Any]]:
    return [t for t in trades if t["day"] in days]


def split_trade_sets(
    trades: List[Dict[str, Any]], days: List[str], holdout_frac: float
) -> Tuple[List[str], List[str], List[Dict[str, Any]], List[Dict[str, Any]]]:
    earlier_days, held_days = lab.split_days(days, holdout_frac)
    held_set = set(held_days)
    train = [t for t in trades if t["day"] not in held_set]
    test = [t for t in trades if t["day"] in held_set]
    return earlier_days, held_days, train, test


def trade_delta_vs_baseline(
    baseline: List[Dict[str, Any]], variant: List[Dict[str, Any]], cost: float
) -> Dict[str, float]:
    bk = {trade_key(t): t for t in baseline}
    vk = {trade_key(t): t for t in variant}
    removed = [bk[k] for k in bk if k not in vk]
    added = [vk[k] for k in vk if k not in bk]
    net = lambda ts: sum(t["gross"] - cost for t in ts)
    rem_net = net(removed)
    return {
        "trades_removed": len(removed),
        "trades_added": len(added),
        "points_from_removed": round(rem_net, 2),
        "points_saved": round(sum(t["gross"] - cost for t in removed if t["gross"] - cost < 0), 2),
        "points_cost": round(sum(t["gross"] - cost for t in removed if t["gross"] - cost > 0), 2),
        "points_from_added": round(net(added), 2),
    }


def benjamini_hochberg(p_values: Sequence[float], q: float = 0.10) -> List[bool]:
    """Return per-hypothesis reject flags (True = pass FDR at level q)."""
    m = len(p_values)
    if m == 0:
        return []
    indexed = sorted(enumerate(p_values), key=lambda x: x[1])
    reject = [False] * m
    max_i = -1
    for rank, (i, p) in enumerate(indexed, start=1):
        if p <= (rank / m) * q:
            max_i = rank - 1
    if max_i < 0:
        return reject
    for rank in range(max_i + 1):
        reject[indexed[rank][0]] = True
    return reject


def daily_net(trades: List[Dict[str, Any]], cost: float) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for t in trades:
        out[t["day"]] = out.get(t["day"], 0.0) + t["gross"] - cost
    return out


def block_bootstrap_total(
    trades: List[Dict[str, Any]],
    days: Sequence[str],
    cost: float,
    *,
    n_iter: int = 2000,
    seed: int = 851029,
) -> Dict[str, Optional[float]]:
    """Resample whole trading days with replacement; return total-net percentiles."""
    by_day: Dict[str, List[float]] = {}
    for t in trades:
        if t["day"] in days:
            by_day.setdefault(t["day"], []).append(t["gross"] - cost)
    day_list = [d for d in days if d in by_day]
    if not day_list:
        return {"p5": None, "p50": None, "p95": None, "n_days": 0}
    rng = random.Random(seed)
    totals: List[float] = []
    for _ in range(n_iter):
        total = 0.0
        for _ in range(len(day_list)):
            d = rng.choice(day_list)
            total += sum(by_day[d])
        totals.append(total)
    totals.sort()
    pct = lambda q: totals[int(q * (len(totals) - 1))]
    return {
        "p5": round(pct(0.05), 2),
        "p50": round(pct(0.50), 2),
        "p95": round(pct(0.95), 2),
        "n_days": len(day_list),
    }


def permutation_p_excess(
    test_variant: List[Dict[str, Any]],
    test_baseline: List[Dict[str, Any]],
    cost: float,
    *,
    n_iter: int = 2000,
    seed: int = 851029,
) -> float:
    """One-sided p-value: variant test net > baseline test net under sign-null."""
    obs = sum(t["gross"] - cost for t in test_variant) - sum(t["gross"] - cost for t in test_baseline)
    nets = [t["gross"] - cost for t in test_variant]
    if not nets:
        return 1.0
    rng = random.Random(seed)
    nulls: List[float] = []
    base_net = sum(t["gross"] - cost for t in test_baseline)
    for _ in range(n_iter):
        flipped = sum(n if rng.random() > 0.5 else -n for n in nets)
        nulls.append(flipped - base_net)
    nulls.sort()
    # empirical p (variant beats null)
    count = sum(1 for x in nulls if x >= obs)
    return (count + 1) / (n_iter + 1)


def random_entry_floor_net(
    test_trades: List[Dict[str, Any]],
    cost: float,
    *,
    n_iter: int = 2000,
    seed: int = 851029,
) -> Dict[str, float]:
    """Random-sign null with same trade magnitudes (sanity floor)."""
    nets = [t["gross"] - cost for t in test_trades]
    if not nets:
        return {"p50": 0.0, "p95": 0.0}
    rng = random.Random(seed + 7)
    sims: List[float] = []
    for _ in range(n_iter):
        sims.append(sum(n if rng.random() > 0.5 else -n for n in nets))
    sims.sort()
    return {
        "p50": round(sims[len(sims) // 2], 2),
        "p95": round(sims[int(0.95 * (len(sims) - 1))], 2),
    }


def chain_snapshot_coverage(market_db: Path, from_day: str, to_day: str) -> Dict[str, Any]:
    from analyst.oi_context_8510 import recorded_days

    paths = [market_db] if market_db.exists() else []
    all_days = recorded_days(paths)
    in_range = [(d, n) for d, n in all_days if from_day <= d <= to_day]
    return {
        "chain_days_total": len(all_days),
        "chain_days_in_range": len(in_range),
        "snapshots_in_range": sum(n for _, n in in_range),
        "thin_history_warning": len(in_range) < 20,
    }


@dataclass
class ConfigResult:
    config_id: str
    label: str
    stage: str
    proto_overrides: Dict[str, Any] = field(default_factory=dict)
    train: Dict[str, Any] = field(default_factory=dict)
    test: Dict[str, Any] = field(default_factory=dict)
    delta_test_pts: float = 0.0
    delta_train_pts: float = 0.0
    trade_delta: Dict[str, float] = field(default_factory=dict)
    bootstrap: Dict[str, Optional[float]] = field(default_factory=dict)
    perm_p: float = 1.0
    random_floor_p95: float = 0.0
    overfit_flag: bool = False
    fdr_pass: bool = False
    beats_random_floor: bool = False
    verdict: str = "NOT PROVEN"


def evaluate_config(
    config_id: str,
    label: str,
    stage: str,
    overrides: Dict[str, Any],
    baseline_trades: List[Dict[str, Any]],
    variant_trades: List[Dict[str, Any]],
    *,
    earlier_days: List[str],
    held_days: List[str],
    cost: float,
    baseline_test_net: float,
) -> ConfigResult:
    held_set = set(held_days)
    train_b = trades_on_days(baseline_trades, set(earlier_days))
    test_b = trades_on_days(baseline_trades, held_set)
    train_v = trades_on_days(variant_trades, set(earlier_days))
    test_v = trades_on_days(variant_trades, held_set)
    st_train_b = lab.stats(train_b, cost)
    st_test_b = lab.stats(test_b, cost)
    st_train_v = lab.stats(train_v, cost)
    st_test_v = lab.stats(test_v, cost)
    res = ConfigResult(
        config_id=config_id,
        label=label,
        stage=stage,
        proto_overrides=overrides,
        train=st_train_v,
        test=st_test_v,
        delta_test_pts=round(float(st_test_v.get("net_pts") or 0) - float(st_test_b.get("net_pts") or 0), 2),
        delta_train_pts=round(float(st_train_v.get("net_pts") or 0) - float(st_train_b.get("net_pts") or 0), 2),
        trade_delta=trade_delta_vs_baseline(test_b, test_v, cost),
        bootstrap=block_bootstrap_total(test_v, held_days, cost),
        perm_p=permutation_p_excess(test_v, test_b, cost),
        random_floor_p95=random_entry_floor_net(test_v, cost)["p95"],
    )
    res.overfit_flag = res.delta_train_pts > 0 and res.delta_test_pts <= 0
    res.beats_random_floor = float(st_test_v.get("net_pts") or 0) > res.random_floor_p95
    # verdict filled after FDR
    _ = st_train_b  # baseline train retained for reporting
    return res


def fdr_verdicts(results: List[ConfigResult], baseline_test_net: float, *, q: float = 0.10) -> None:
    """Set fdr_pass and verdict on each result (not baseline)."""
    configs = [r for r in results if r.config_id != "baseline"]
    pvals = [r.perm_p for r in configs]
    flags = benjamini_hochberg(pvals, q=q)
    for r, ok in zip(configs, flags):
        r.fdr_pass = bool(ok)
        test_net = float(r.test.get("net_pts") or 0)
        beats_baseline = test_net > baseline_test_net
        p5 = r.bootstrap.get("p5")
        robust = p5 is not None and p5 > 0
        if beats_baseline and ok and robust and r.beats_random_floor:
            r.verdict = "PASS"
        elif beats_baseline and not ok:
            r.verdict = "NOT PROVEN"
        elif not beats_baseline:
            r.verdict = "FAIL"
        else:
            r.verdict = "NOT PROVEN"


def build_candidate_combos(winners: List[Tuple[str, str, Dict[str, Any]]], max_combos: int = 10) -> List[Tuple[str, str, Dict[str, Any]]]:
    """At most ~10 combos from Stage-1/2 winners."""
    if not winners:
        return []
    base = baseline_proto_overrides()
    combos: List[Tuple[str, str, Dict[str, Any]]] = []
    if len(winners) >= 2:
        a_id, a_lbl, a_ov = winners[0]
        b_id, b_lbl, b_ov = winners[1]
        merged = merge_overrides(a_ov, b_ov)
        combos.append((f"combo_{a_id}+{b_id}", f"{a_lbl} + {b_lbl}", merged))
    if len(winners) >= 3:
        merged3 = merge_overrides(winners[0][2], merge_overrides(winners[1][2], winners[2][2]))
        ids = "+".join(w[0] for w in winners[:3])
        combos.append((f"combo_{ids}", "Top-3 merge", merged3))
    for w_id, w_lbl, w_ov in winners[: min(5, len(winners))]:
        if len(combos) >= max_combos:
            break
        combos.append((f"combo_solo_{w_id}", f"Solo winner: {w_lbl}", merge_overrides(base, w_ov)))
    return combos[:max_combos]


def run_sweep(
    from_day: str,
    to_day: str,
    *,
    stage: str = "all",
    cost_pts: float = 2.0,
    holdout_frac: float = 0.30,
    out_dir: Optional[Path] = None,
    on_progress: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    def prog(msg: str) -> None:
        if on_progress:
            on_progress(msg)

    cfg_path = ROOT / "analyst/terminal8510_config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
    market_raw = cfg.get("market_db_path") or "data/terminal8510/recording.sqlite3"
    market_db = Path(market_raw) if Path(market_raw).is_absolute() else ROOT / market_raw

    cov_spot = lab.coverage_of(market_db, from_day, to_day)
    chain_cov = chain_snapshot_coverage(market_db, from_day, to_day)
    days = cov_spot.get("days") or []
    if not days:
        raise RuntimeError(f"No spot candle days between {from_day} and {to_day}")

    prog("Stage 0 — baseline (all filters OFF)")
    base_ov = baseline_proto_overrides()
    baseline_trades, _ = lab.run_ema8_prototype_trades(
        from_day, to_day, cost_pts=cost_pts, proto_overrides=base_ov, on_progress=prog
    )
    earlier_days, held_days, train_b, test_b = split_trade_sets(baseline_trades, days, holdout_frac)
    st_base_train = lab.stats(train_b, cost_pts)
    st_base_test = lab.stats(test_b, cost_pts)
    baseline_test_net = float(st_base_test.get("net_pts") or 0)

    results: List[ConfigResult] = [
        evaluate_config(
            "baseline",
            "Baseline (filters OFF)",
            "0",
            base_ov,
            baseline_trades,
            baseline_trades,
            earlier_days=earlier_days,
            held_days=held_days,
            cost=cost_pts,
            baseline_test_net=baseline_test_net,
        )
    ]
    baseline_res = results[0]
    baseline_res.verdict = "REFERENCE"
    baseline_res.bootstrap = block_bootstrap_total(test_b, held_days, cost_pts)

    to_run: List[Tuple[str, str, str, Dict[str, Any]]] = []

    if stage in ("all", "1", "stage1"):
        for cid, lbl, ov in stage1_variants():
            to_run.append(("1", cid, lbl, ov))

    stage1_results: List[ConfigResult] = []
    winners: List[Tuple[str, str, Dict[str, Any]]] = []

    for stg, cid, lbl, ov in to_run:
        prog(f"Running {cid} …")
        trades, _ = lab.run_ema8_prototype_trades(from_day, to_day, cost_pts=cost_pts, proto_overrides=ov, on_progress=prog)
        cr = evaluate_config(
            cid, lbl, stg, ov, baseline_trades, trades,
            earlier_days=earlier_days, held_days=held_days, cost=cost_pts, baseline_test_net=baseline_test_net,
        )
        stage1_results.append(cr)
        if cr.delta_test_pts > 0:
            winners.append((cid, lbl, ov))

    results.extend(stage1_results)

    if stage in ("all", "2", "stage2") and len(winners) >= 2:
        prog("Stage 2 — pairwise interactions")
        for i in range(len(winners)):
            for j in range(i + 1, len(winners)):
                a_id, a_lbl, a_ov = winners[i]
                b_id, b_lbl, b_ov = winners[j]
                merged = merge_overrides(a_ov, b_ov)
                cid = f"pair_{a_id}+{b_id}"
                lbl = f"Pair: {a_lbl} + {b_lbl}"
                trades, _ = lab.run_ema8_prototype_trades(
                    from_day, to_day, cost_pts=cost_pts, proto_overrides=merged, on_progress=prog
                )
                cr = evaluate_config(
                    cid, lbl, "2", merged, baseline_trades, trades,
                    earlier_days=earlier_days, held_days=held_days, cost=cost_pts, baseline_test_net=baseline_test_net,
                )
                results.append(cr)

    if stage in ("all", "3", "stage3"):
        prog("Stage 3 — candidate combos")
        for cid, lbl, ov in build_candidate_combos(winners):
            trades, _ = lab.run_ema8_prototype_trades(from_day, to_day, cost_pts=cost_pts, proto_overrides=ov, on_progress=prog)
            cr = evaluate_config(
                cid, lbl, "3", ov, baseline_trades, trades,
                earlier_days=earlier_days, held_days=held_days, cost=cost_pts, baseline_test_net=baseline_test_net,
            )
            results.append(cr)

    fdr_verdicts(results, baseline_test_net)

    ranked = sorted(
        [r for r in results if r.config_id != "baseline"],
        key=lambda r: (r.verdict != "PASS", -r.delta_test_pts),
    )
    pass_list = [r.config_id for r in ranked if r.verdict == "PASS"]
    if pass_list:
        rec = pass_list[0]
        summary_en = (
            f"Baseline test = {baseline_test_net:.1f} pts. PASS after FDR @ q=0.10: {', '.join(pass_list)}. "
            f"Recommended: {rec}."
        )
    else:
        summary_en = (
            f"Baseline test = {baseline_test_net:.1f} pts. No config PASS after held-out FDR correction — "
            "keep filters OFF (log-only watch) until more recorded chain-days exist."
        )

    limitations = (
        "Spot points only (no option premium simulation). "
        f"N recorded chain-days in range: {chain_cov['chain_days_in_range']} "
        f"({chain_cov['snapshots_in_range']} snapshots). "
        + (
            "WARNING: fewer than ~20 chain-days — OI filter verdicts are not trustworthy."
            if chain_cov["thin_history_warning"]
            else ""
        )
    )

    payload = {
        "from": from_day,
        "to": to_day,
        "cost_pts": cost_pts,
        "holdout_frac": holdout_frac,
        "coverage": {
            "spot_trading_days": len(days),
            "spot": cov_spot,
            "chain": chain_cov,
        },
        "baseline_test_net_pts": baseline_test_net,
        "baseline_train_net_pts": float(st_base_train.get("net_pts") or 0),
        "summary_en": summary_en,
        "limitations": limitations,
        "pass_configs": pass_list,
        "results": [
            {
                "config_id": r.config_id,
                "label": r.label,
                "stage": r.stage,
                "verdict": r.verdict,
                "overfit_flag": r.overfit_flag,
                "train_net_pts": r.train.get("net_pts"),
                "test_net_pts": r.test.get("net_pts"),
                "delta_test_pts": r.delta_test_pts,
                "delta_train_pts": r.delta_train_pts,
                "test_trades": r.test.get("trades"),
                "test_win_rate_pct": r.test.get("win_rate_pct"),
                "test_expectancy": r.test.get("avg_net_pts"),
                "test_max_dd": r.test.get("max_drawdown_pts"),
                "test_t_stat": r.test.get("t_stat"),
                "bootstrap_p5_pts": r.bootstrap.get("p5"),
                "bootstrap_p50_pts": r.bootstrap.get("p50"),
                "bootstrap_p95_pts": r.bootstrap.get("p95"),
                "perm_p": round(r.perm_p, 4),
                "fdr_pass": r.fdr_pass,
                "trade_delta": r.trade_delta,
            }
            for r in results
        ],
        "ranked_non_baseline": [r.config_id for r in ranked],
    }

    ts = datetime.now(IST).strftime("%Y%m%d-%H%M%S")
    out = out_dir or (SWEEPS_DIR / ts)
    out.mkdir(parents=True, exist_ok=True)
    json_path = out / "sweep_result.json"
    json_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    csv_path = out / "sweep_ranked.csv"
    fields = [
        "config_id", "label", "stage", "verdict", "train_net_pts", "test_net_pts", "delta_test_pts",
        "test_expectancy", "test_win_rate_pct", "test_max_dd", "bootstrap_p5_pts", "perm_p", "fdr_pass",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for row in payload["results"]:
            w.writerow(row)

    md_path = out / "sweep_report.md"
    lines = [
        f"# Prototype filter sweep {from_day} .. {to_day}",
        "",
        f"**Spot trading days:** {len(days)} · **Chain snapshot days in range:** {chain_cov['chain_days_in_range']}",
        "",
        "## Summary",
        "",
        summary_en,
        "",
        "## LIMITATIONS",
        "",
        limitations,
        "",
        "## Ranked table (held-out test emphasis)",
        "",
        "| config | train pts | test pts | Δ test | expectancy | win% | max DD | boot p5 | perm p | FDR | verdict |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|:---:|:---|",
    ]
    for row in payload["results"]:
        if row["config_id"] == "baseline":
            continue
        lines.append(
            f"| {row['config_id']} | {row['train_net_pts']} | {row['test_net_pts']} | {row['delta_test_pts']} | "
            f"{row['test_expectancy']} | {row['test_win_rate_pct']} | {row['test_max_dd']} | "
            f"{row['bootstrap_p5_pts']} | {row['perm_p']} | {row['fdr_pass']} | {row['verdict']} |"
        )
    lines.extend(["", f"Baseline test net: **{baseline_test_net:.1f}** pts", ""])
    md_path.write_text("\n".join(lines), encoding="utf-8")

    payload["output_dir"] = str(out)
    payload["files"] = {"json": str(json_path), "csv": str(csv_path), "md": str(md_path)}
    return payload


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="8510 Prototype filter sweep (staged + FDR + bootstrap)")
    ap.add_argument("--from", dest="from_day", required=True)
    ap.add_argument("--to", dest="to_day", required=True)
    ap.add_argument("--stage", default="all", help="all | stage1 | stage2 | stage3 | 0")
    ap.add_argument("--cost-pts", type=float, default=2.0)
    ap.add_argument("--holdout", type=float, default=0.30)
    ap.add_argument("--out", dest="out_dir", default="", help="output directory (default data/terminal8510/sweeps/<ts>)")
    a = ap.parse_args(argv)

    out = Path(a.out_dir) if a.out_dir else None
    if a.stage in ("0", "baseline"):
        print("Stage 0 only: use full sweep or backtest_lab for baseline.", file=sys.stderr)
        return 2

    def on_progress(msg: str) -> None:
        print(msg, flush=True)

    t0 = time.time()
    try:
        res = run_sweep(
            a.from_day,
            a.to_day,
            stage=a.stage,
            cost_pts=a.cost_pts,
            holdout_frac=a.holdout,
            out_dir=out,
            on_progress=on_progress,
        )
    except Exception as exc:
        print(f"Sweep failed: {exc}", file=sys.stderr)
        return 1

    print("\n" + res["summary_en"])
    print("\n" + res["limitations"])
    print(f"\nWrote: {res['files']['md']}")
    print(f"Elapsed {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())