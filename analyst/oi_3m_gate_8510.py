"""Independent 3-minute OI Significance gate (separate from ema8_prototype.oi_filters).

Reuses OI Momentum v2 maths from analyst.chain_reads_8510 (S = dD/T, Option A) with
window_min default 3. This measures only the last ~3 minutes of writer pressure, so it
stays NEUTRAL on slow drift and only fires on a sharp recent burst. It is a stricter,
noise-resistant CONFIRM, and typically appears ~1 minute AFTER the 10-min S — not an
early-warning.

Pure functions. No I/O. Never raises. direction None / NEUTRAL → fail-open (no block).
B (level bias) is never used in the gate.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

DEFAULT_OI_3M_FEATURE: Dict[str, Any] = {
    "enabled": False,
    "gate": {
        "enabled": False,
        "window_min": 3,
        "sig_min": 0.15,
        "sig_extreme": 0.30,
        "mode": "log_only",
    },
    "context_panel": {
        "enabled": False,
        "show": [
            "pcr_zone",
            "walls",
            "day_oi",
            "pcr_futures",
            "ticket",
            "high_conviction",
            "pcr_trinity",
            "smart_oi",
            "pe_ce_matrix",
            "cvd",
        ],
    },
}

OI_3M_GATE_MODES = ("log_only", "block")


def load_oi_3m_feature_cfg(scanner_cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    out = {
        "enabled": bool(DEFAULT_OI_3M_FEATURE["enabled"]),
        "gate": dict(DEFAULT_OI_3M_FEATURE["gate"]),
        "context_panel": dict(DEFAULT_OI_3M_FEATURE["context_panel"]),
    }
    raw = (scanner_cfg or {}).get("oi_3m_feature")
    if not isinstance(raw, dict):
        return out
    if "enabled" in raw:
        out["enabled"] = bool(raw["enabled"])
    gate = raw.get("gate")
    if isinstance(gate, dict):
        g = dict(out["gate"])
        if "enabled" in gate:
            g["enabled"] = bool(gate["enabled"])
        if "window_min" in gate:
            g["window_min"] = int(gate["window_min"])
        if "sig_min" in gate:
            g["sig_min"] = float(gate["sig_min"])
        if "sig_extreme" in gate:
            g["sig_extreme"] = float(gate["sig_extreme"])
        if "mode" in gate:
            m = str(gate["mode"] or "log_only").lower()
            g["mode"] = m if m in OI_3M_GATE_MODES else "log_only"
        out["gate"] = g
    cp = raw.get("context_panel")
    if isinstance(cp, dict):
        c = dict(out["context_panel"])
        if "enabled" in cp:
            c["enabled"] = bool(cp["enabled"])
        if isinstance(cp.get("show"), list):
            c["show"] = [str(x) for x in cp["show"]]
        out["context_panel"] = c
    return out


def momentum_cfg_from_gate(gate: Dict[str, Any]) -> Dict[str, Any]:
    return {
      "window_min": max(1, int(gate.get("window_min", 3))),
      "sig_min": float(gate.get("sig_min", 0.15)),
      "sig_extreme": float(gate.get("sig_extreme", 0.30)),
      "require_cumd_confirm": False,
  }


def compute_oi_3m_read(
    rows: List[Dict[str, Any]],
    spot: float,
    *,
    day: str,
    now_ts: int,
    gate_cfg: Dict[str, Any],
    chain_reads_cfg: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Compute normalized-S read on total OI with a 3m (configurable) window. Fail-open on gaps."""
    miss = {"direction": None, "detail": "no data", "S": None, "B": None, "CumD_lakh": None}
    if not rows or spot <= 0:
        return {**miss, "detail": "no chain snapshots"}
    from analyst.chain_reads_8510 import compute_momentum_oi_reads, load_cfg

    by_ts: Dict[int, Dict[float, Dict[str, float]]] = {}
    for r in rows:
        try:
            t = int(float(r["ts"]))
        except (TypeError, ValueError, KeyError):
            continue
        if t > now_ts:
            continue
        by_ts.setdefault(t, {})[float(r["strike"])] = {
            k: float(r.get(k) or 0.0) for k in ("ce_oi", "pe_oi", "ce_vol", "pe_vol", "ce_ltp", "pe_ltp")
        }
    ts_sorted = sorted(by_ts)
    if not ts_sorted:
        return {**miss, "detail": "no snapshot at or before eval time"}
    last_t = ts_sorted[-1]
    age = max(0, now_ts - last_t)
    cr_cfg = load_cfg({"chain_reads": chain_reads_cfg or {}})
    mom = momentum_cfg_from_gate(gate_cfg)
    try:
        mom_reads = compute_momentum_oi_reads(
            by_ts,
            ts_sorted,
            spot=spot,
            band=float(cr_cfg.get("band_pts", 1000.0)),
            day=day,
            now_ts=now_ts,
            last_t=last_t,
            age_sec=int(age),
            momentum_cfg=mom,
        )
    except Exception as exc:
        return {**miss, "detail": f"error: {type(exc).__name__}"}
    tot = dict(mom_reads.get("total_oi") or miss)
    tot["window_min"] = mom["window_min"]
    return tot


def _opposes_touch(bullish: bool, direction: Any) -> bool:
    d = str(direction or "").upper()
    if d not in ("CE", "PE"):
        return False
    side = "CE" if bullish else "PE"
    return d != side


def oi_3m_gate_decision(
    bullish: bool,
    feature_cfg: Dict[str, Any],
    read: Optional[Dict[str, Any]],
) -> Tuple[bool, Dict[str, Any]]:
    """Return (would_block_trade, detail). log_only never blocks; None/NEUTRAL never blocks."""
    feat_on = bool(feature_cfg.get("enabled"))
    gate = feature_cfg.get("gate") if isinstance(feature_cfg.get("gate"), dict) else {}
    gate_on = feat_on and bool(gate.get("enabled"))
    mode = str(gate.get("mode") or "log_only").lower()
    r = read if isinstance(read, dict) else {}
    direction = r.get("direction")
    strength = r.get("strength")
    oppose = _opposes_touch(bullish, direction)
    would = gate_on and oppose
    blocks = would and mode == "block"
    detail: Dict[str, Any] = {
        "feature_enabled": feat_on,
        "gate_enabled": gate_on,
        "mode": mode,
        "direction": direction,
        "strength": strength,
        "S": r.get("S"),
        "B": r.get("B"),
        "CumD_lakh": r.get("CumD_lakh"),
        "detail": r.get("detail"),
        "would_block": would,
        "blocks_trade": blocks,
        "opposes_touch": oppose,
    }
    if blocks:
        detail["reason"] = f"oi_3m {direction} opposes {'LONG' if bullish else 'SHORT'}"
    elif would and mode == "log_only":
        detail["reason"] = "log_only — would block"
    return blocks, detail


def format_oi_3m_line(read: Optional[Dict[str, Any]]) -> str:
    r = read if isinstance(read, dict) else {}
    if r.get("S") is None:
        return "3m: — (no data)"
    s = float(r["S"])
    b = r.get("B")
    cd = r.get("CumD_lakh")
    d = r.get("direction") or "—"
    st = r.get("strength")
    bs = f"{float(b):+.2f}" if b is not None else "—"
    cds = f"{float(cd):+.0f}L" if cd is not None else "—"
    tail = f" {st}" if st and st != "NEUTRAL" else ""
    return f"3m S {s:+.2f} · bias {bs} · CumD {cds} → {d}{tail}"