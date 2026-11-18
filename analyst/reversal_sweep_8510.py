"""Backtest Lab / proto_sweep factor rows for 8 EMA Reversal (held-out validation)."""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

DEFAULT_REVERSAL_SWEEP: Dict[str, Any] = {
    "timeframe_mode": "15m",
    "ema_separation_enabled": False,
    "candle_trail_enabled": False,
    "candle_trail_activate_pts": 20.0,
    "execution_mode": "PAPER_ONLY",
    "oi_filters": {
        "enabled": False,
        "mode": "log_only",
        "switches": {},
    },
}


def reversal_factor_rows() -> List[Tuple[str, str, Dict[str, Any]]]:
    """(id, label, scanner.reversal overrides) for sweeps."""
    base = dict(DEFAULT_REVERSAL_SWEEP)
    out: List[Tuple[str, str, Dict[str, Any]]] = []
    out.append(("rev_baseline_pm20", "Reversal ±20 spot (15m)", dict(base)))
    for tf in ("5m", "15m", "both"):
        c = dict(base, timeframe_mode=tf)
        out.append((f"rev_tf_{tf}", f"Reversal TF {tf}", c))
    sep = dict(base, ema_separation_enabled=True)
    out.append(("rev_sep_on", "Reversal EMA separation ON", sep))
    tr = dict(base, candle_trail_enabled=True)
    out.append(("rev_trail_on", "Reversal ±20 then candle trail", tr))
    oi_log = dict(
        base,
        oi_filters={
            "enabled": True,
            "mode": "log_only",
            "rule": "all_agree",
            "switches": {"writer_flow": True, "total_oi": True, "fresh_oi": True},
            "require_price_confirm": False,
        },
    )
    out.append(("rev_oi_log", "Reversal OI filters log_only", oi_log))
    oi_block = dict(oi_log, oi_filters={**oi_log["oi_filters"], "mode": "block"})
    out.append(("rev_oi_block", "Reversal OI filters block", oi_block))
    return out


def merge_into_scanner_cfg(scanner_cfg: Dict[str, Any], reversal_overrides: Dict[str, Any]) -> Dict[str, Any]:
    sc = dict(scanner_cfg or {})
    rev = dict((sc.get("reversal") or {}))
    rev.update(reversal_overrides)
    if isinstance(reversal_overrides.get("oi_filters"), dict):
        rev["oi_filters"] = {**(rev.get("oi_filters") or {}), **reversal_overrides["oi_filters"]}
    sc["reversal"] = rev
    return sc