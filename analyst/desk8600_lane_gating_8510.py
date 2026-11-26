"""Per-lane BLOCK vs LOG_ONLY gating on four 8600-aligned desk board reads.

Pure functions where possible; stale feeds never block (fail-open).
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from analyst.feed_freshness_8510 import load_feed_cfg

_LIVE_SHEET_RULES = Path(__file__).with_name("live_sheet_rules.json")

LANES = ("prototype", "reversal", "flagship")

TRACK2_SIGNALS: Tuple[Tuple[str, str, str], ...] = (
    ("pe_ce_change", "day_oi", "PE-CE change"),
    ("fresh_change_oi", "fresh_oi", "Fresh Change in OI"),
    ("cumulative_total_oi", "total_oi", "Cumulative Total OI"),
    ("classic_writer_flow", "writer_flow", "Classic Writer Flow"),
)

TRACK2_SIGNAL_KEYS = tuple(s[0] for s in TRACK2_SIGNALS)
GATE_MODES = ("BLOCK", "LOG_ONLY")

_DEFAULT_LANE: Dict[str, str] = {k: "LOG_ONLY" for k in TRACK2_SIGNAL_KEYS}

DEFAULT_DESK8600_LANE_GATING: Dict[str, Dict[str, str]] = {
    lane: dict(_DEFAULT_LANE) for lane in LANES
}


def _lane_gating_from_mapping(raw: Any) -> Dict[str, Dict[str, str]]:
    out = {lane: dict(_DEFAULT_LANE) for lane in LANES}
    if not isinstance(raw, dict):
        return out
    for lane in LANES:
        block = raw.get(lane)
        if not isinstance(block, dict):
            continue
        for sig_key, _, _ in TRACK2_SIGNALS:
            mode = str(block.get(sig_key) or out[lane][sig_key]).upper().replace("-", "_")
            if mode == "LOG":
                mode = "LOG_ONLY"
            if mode in GATE_MODES:
                out[lane][sig_key] = mode
    return out


def _file_lane_gating_defaults() -> Dict[str, Dict[str, str]]:
    try:
        doc = json.loads(_LIVE_SHEET_RULES.read_text())
        return _lane_gating_from_mapping(doc.get("desk8600_lane_gating"))
    except Exception:
        return {lane: dict(_DEFAULT_LANE) for lane in LANES}


def load_desk8600_lane_gating(scanner_cfg: Optional[Dict[str, Any]]) -> Dict[str, Dict[str, str]]:
    """File defaults from live_sheet_rules.json; scanner_cfg.desk8600_lane_gating overrides per key."""
    root = _file_lane_gating_defaults()
    raw = dict((scanner_cfg or {}).get("desk8600_lane_gating") or {})
    wb = (scanner_cfg or {}).get("writer_bias") or {}
    if isinstance(wb.get("desk8600_lane_gating"), dict):
        for lane, block in wb["desk8600_lane_gating"].items():
            if lane in LANES and isinstance(block, dict):
                raw.setdefault(lane, {}).update(block)
    merged = _lane_gating_from_mapping({lane: {**root[lane], **(raw.get(lane) or {})} for lane in LANES})
    return merged


def write_lane_gating_modes(modes: Any) -> Dict[str, Dict[str, str]]:
    """Persist the delta-weighted writer-bias gate modes to live_sheet_rules.json so a
    UI/screen toggle survives restart. Preserves all other keys in the file. Returns the
    normalized modes."""
    norm = _lane_gating_from_mapping(modes)
    doc: Dict[str, Any] = {}
    try:
        if _LIVE_SHEET_RULES.exists():
            doc = json.loads(_LIVE_SHEET_RULES.read_text())
            if not isinstance(doc, dict):
                doc = {}
    except Exception:
        doc = {}
    doc["desk8600_lane_gating"] = norm
    _LIVE_SHEET_RULES.write_text(json.dumps(doc, indent=2))
    return norm


def set_lane_gating_mode(lane: str, signal: str, mode: str) -> Dict[str, Dict[str, str]]:
    """Set one lane x signal to BLOCK or LOG_ONLY and persist. No-op on bad input."""
    current = _file_lane_gating_defaults()
    m = str(mode or "").upper().replace("-", "_")
    if m == "LOG":
        m = "LOG_ONLY"
    if lane in LANES and signal in TRACK2_SIGNAL_KEYS and m in GATE_MODES:
        current[lane][signal] = m
    return write_lane_gating_modes(current)


def set_all_lane_gating_mode(mode: str) -> Dict[str, Dict[str, str]]:
    """Set every lane x signal to one mode (a single screen-level BLOCK/LOG switch)."""
    m = str(mode or "").upper().replace("-", "_")
    if m == "LOG":
        m = "LOG_ONLY"
    if m not in GATE_MODES:
        m = "LOG_ONLY"
    return write_lane_gating_modes({lane: {k: m for k in TRACK2_SIGNAL_KEYS} for lane in LANES})


def lane_for_strategy_id(strategy_id: str) -> Optional[str]:
    sid = str(strategy_id or "").upper()
    if sid.startswith("EMA8_PROTOTYPE"):
        return "prototype"
    if sid.startswith("CROSSOVER"):
        return "reversal"
    if "FLAGSHIP" in sid or sid.startswith("H1_") or sid.startswith("PA_1H"):
        return "flagship"
    return None


def _sources_index(sources: Any) -> Dict[str, Dict[str, Any]]:
    if isinstance(sources, dict) and sources and all(isinstance(v, dict) for v in sources.values()):
        return {str(k): dict(v) for k, v in sources.items()}
    out: Dict[str, Dict[str, Any]] = {}
    for s in sources or []:
        if isinstance(s, dict) and s.get("id"):
            out[str(s["id"])] = dict(s)
    return out


def _vote(direction: Any, bullish: bool, feed_state: Any) -> str:
    fs = str(feed_state or "").upper()
    if fs in ("STALE", "WAITING_FOR_SYNC", "WAITING") or direction is None:
        return "STALE"
    side = "CE" if bullish else "PE"
    other = "PE" if bullish else "CE"
    d = str(direction).upper()
    if d not in ("CE", "PE", "NEUTRAL"):
        return "STALE"
    if d == side:
        return "AGREE"
    if d == other:
        return "OPPOSE"
    return "NEUTRAL"


def _format_oppose_label(label: str, source: Dict[str, Any], vote: str) -> str:
    detail = str(source.get("detail") or "").strip()
    age = source.get("age_sec")
    age_txt = f"age {int(age)}s" if age is not None else "age —"
    regime = str(source.get("writer_bias_regime") or "").strip()
    score = source.get("writer_bias_score")
    slope = source.get("writer_bias_slope")
    score_txt = f"{float(score):+.2f}" if isinstance(score, (int, float)) and not isinstance(score, bool) else "—"
    slope_txt = f"{float(slope):+.4f}/s" if isinstance(slope, (int, float)) and not isinstance(slope, bool) else "—"
    if vote == "OPPOSE":
        if regime and regime not in ("UNAVAILABLE", ""):
            return f"{label} {regime} (score {score_txt}, slope {slope_txt}, {age_txt})"
        tail = f" ({detail[:240]}, {age_txt})" if detail else f" ({age_txt})"
        return f"{label} OPPOSE{tail}"
    return f"{label} {vote}"


def evaluate_lane_gating(
    lane: str,
    bullish: bool,
    sources: Any,
    scanner_cfg: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Return blocked flag, human reasons, and per-signal audit lines."""
    lane = str(lane or "").lower()
    if lane not in LANES:
        return {"blocked": False, "block_reason": "", "audit_lines": [], "signals": []}
    modes = load_desk8600_lane_gating(scanner_cfg).get(lane) or dict(_DEFAULT_LANE)
    by_id = _sources_index(sources)
    side_word = "CE" if bullish else "PE"
    blocked_reasons: List[str] = []
    audit_lines: List[str] = []
    signals: List[Dict[str, Any]] = []

    for sig_key, board_id, label in TRACK2_SIGNALS:
        src = by_id.get(board_id) or {}
        age = src.get("age_sec")
        _sts = src.get("source_ts")
        usable = (
            isinstance(age, (int, float))
            and not isinstance(age, bool)
            and math.isfinite(age)
            and 0 <= age <= float(load_feed_cfg(scanner_cfg)["stale_after_seconds"])
            and str(src.get("feed_state") or "").upper() == "LIVE"
            # Require real provenance: a source with no/blank source_ts can NEVER block
            # (missing provenance is treated like STALE — fail-open).
            and _sts is not None
            and str(_sts).strip().lower() not in ("", "n/a", "none")
        )
        vote = _vote(src.get("direction"), bullish, "LIVE" if usable else "STALE")
        mode = str(modes.get(sig_key) or "LOG_ONLY").upper()
        item = {
            "signal_id": sig_key,
            "board_id": board_id,
            "label": label,
            "mode": mode,
            "vote": vote,
            "direction": src.get("direction"),
            "age_sec": src.get("age_sec"),
            "feed_state": src.get("feed_state"),
            "source_ts": src.get("source_ts"),
            "detail": src.get("detail"),
            "writer_bias_score": src.get("writer_bias_score"),
            "writer_bias_slope": src.get("writer_bias_slope"),
            "writer_bias_regime": src.get("writer_bias_regime"),
        }
        if vote == "STALE":
            line = f"{label}: feed stale — not blocking"
            audit_lines.append(line)
            item["audit_reason"] = line
            item["blocks_trade"] = False
        elif vote == "OPPOSE":
            opp = _format_oppose_label(label, src, vote)
            if mode == "BLOCK":
                block_line = f"{side_word} entry blocked — {opp}"
                blocked_reasons.append(block_line)
                audit_lines.append(block_line)
                item["audit_reason"] = block_line
                item["blocks_trade"] = True
            else:
                line = f"LOG_ONLY: {opp}, not blocking"
                audit_lines.append(line)
                item["audit_reason"] = line
                item["blocks_trade"] = False
        elif vote == "AGREE":
            regime = str(src.get("writer_bias_regime") or "").strip()
            score = src.get("writer_bias_score")
            age = src.get("age_sec")
            age_txt = f"age {int(age)}s" if age is not None else "age —"
            if regime and regime not in ("UNAVAILABLE", ""):
                sc = f"{float(score):+.2f}" if isinstance(score, (int, float)) and not isinstance(score, bool) else "—"
                line = f"{side_word} allowed — {label} {regime} (score {sc}, slope —, {age_txt})"
            else:
                line = f"{label} AGREE — allowed ({age_txt})"
            audit_lines.append(line)
            item["audit_reason"] = line
            item["blocks_trade"] = False
        else:
            line = f"{label} NEUTRAL — not blocking"
            audit_lines.append(line)
            item["audit_reason"] = line
            item["blocks_trade"] = False
        signals.append(item)

    block_reason = "; ".join(blocked_reasons) if blocked_reasons else ""
    return {
        "blocked": bool(blocked_reasons),
        "block_reason": block_reason,
        "audit_lines": audit_lines,
        "signals": signals,
        "lane": lane,
        "side": side_word,
    }


def record_gating_audits(
    backend: Any,
    lane: str,
    bullish: bool,
    gating: Dict[str, Any],
    *,
    strategy_id: str,
) -> None:
    """Write decision audit rows; dedup handled inside decision_audit.record."""
    try:
        from analyst.decision_audit_8510 import record
    except Exception:
        return
    sig = {
        "strategy_id": strategy_id,
        "direction": "LONG" if bullish else "SHORT",
    }
    for item in gating.get("signals") or []:
        reason = str(item.get("audit_reason") or "")
        if not reason:
            continue
        stage = "TOUCH_BLOCKED" if item.get("blocks_trade") else "STATE"
        record(backend, sig, stage, reason, lane=lane, state={"desk8600_lane_gating_signal": item})


def apply_execution_lane_gating(
    backend: Any,
    sig: Dict[str, Any],
    board: Dict[str, Any],
) -> bool:
    """Return True if the signal must not proceed (BLOCK mode oppose only). Records audits."""
    sid = str(sig.get("strategy_id") or "")
    lane = lane_for_strategy_id(sid)
    if not lane:
        return False
    bullish = str(sig.get("direction") or "").upper() in ("LONG", "CE", "BUY_CE")
    gating = evaluate_lane_gating(
        lane, bullish, board.get("sources"), getattr(backend, "scanner_cfg", None),
    )
    record_gating_audits(backend, lane, bullish, gating, strategy_id=sid)
    if gating.get("blocked"):
        try:
            backend._record_ema_entry_decision(sig, "BLOCKED", gating.get("block_reason") or "8600 lane gating")
        except Exception:
            pass
        return True
    return False