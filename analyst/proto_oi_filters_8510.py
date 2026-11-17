"""8600 option-chain reads as optional ON/OFF entry filters for the 8 EMA Prototype.

Pure functions only — no I/O, never raises. Stale or missing reads fail OPEN.
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

PROTO_OI_READ_IDS: Tuple[str, ...] = (
    "writer_flow",
    "price_oi",
    "total_oi",
    "fresh_oi",
    "volume_oi",
)

PROTO_OI_READ_LABELS: Dict[str, str] = {
    "writer_flow": "Classic Writer Flow",
    "price_oi": "Price vs OI buildup (15 min, at the money)",
    "total_oi": "Cumulative Total OI",
    "fresh_oi": "Fresh Change in OI",
    "volume_oi": "Volume-backed OI (15 min)",
}

DEFAULT_OI_FILTERS: Dict[str, Any] = {
    "mode": "log_only",
    "rule": "all_agree",
    "neutral_blocks": False,
    "max_age_sec": 420.0,
    "switches": {
        "writer_flow": False,
        "price_oi": False,
        "total_oi": False,
        "fresh_oi": False,
        "volume_oi": False,
    },
    "momentum": {
        "window_min": 10,
        "sig_min": 0.15,
        "sig_extreme": 0.30,
        "require_cumd_confirm": False,
    },
}

OI_FILTER_RULES = ("all_agree", "any_agrees", "majority")
OI_FILTER_MODES = ("log_only", "block")


def load_oi_filters_cfg(proto_cfg: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "mode": DEFAULT_OI_FILTERS["mode"],
        "rule": DEFAULT_OI_FILTERS["rule"],
        "neutral_blocks": DEFAULT_OI_FILTERS["neutral_blocks"],
        "max_age_sec": DEFAULT_OI_FILTERS["max_age_sec"],
        "switches": dict(DEFAULT_OI_FILTERS["switches"]),
        "momentum": dict(DEFAULT_OI_FILTERS["momentum"]),
    }
    raw = (proto_cfg or {}).get("oi_filters")
    if not isinstance(raw, dict):
        return out
    mode = str(raw.get("mode") or out["mode"]).lower()
    out["mode"] = mode if mode in OI_FILTER_MODES else out["mode"]
    rule = str(raw.get("rule") or out["rule"]).lower()
    out["rule"] = rule if rule in OI_FILTER_RULES else out["rule"]
    if "neutral_blocks" in raw:
        out["neutral_blocks"] = bool(raw.get("neutral_blocks"))
    try:
        out["max_age_sec"] = float(raw.get("max_age_sec", out["max_age_sec"]))
    except (TypeError, ValueError):
        pass
    sw = raw.get("switches")
    if isinstance(sw, dict):
        for rid in PROTO_OI_READ_IDS:
            if rid in sw:
                out["switches"][rid] = bool(sw[rid])
    mom = raw.get("momentum")
    if isinstance(mom, dict):
        m = dict(out.get("momentum") or DEFAULT_OI_FILTERS["momentum"])
        if "window_min" in mom:
            m["window_min"] = int(mom["window_min"])
        if "sig_min" in mom:
            m["sig_min"] = float(mom["sig_min"])
        if "sig_extreme" in mom:
            m["sig_extreme"] = float(mom["sig_extreme"])
        if "require_cumd_confirm" in mom:
            m["require_cumd_confirm"] = bool(mom["require_cumd_confirm"])
        if "deadzone_cr_per_min" in mom:
            pass  # retired — ignored
        out["momentum"] = m
    return out


def board_reads_from_board(board: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Map the five read IDs from build_board()['sources']."""
    by_id = {str(s.get("id")): s for s in (board.get("sources") or []) if isinstance(s, dict) and s.get("id")}
    out: Dict[str, Dict[str, Any]] = {}
    for rid in PROTO_OI_READ_IDS:
        s = by_id.get(rid) or {}
        item = {
            "direction": s.get("direction"),
            "age_sec": s.get("age_sec"),
            "detail": str(s.get("detail") or "")[:160],
        }
        for key in ("S", "B", "CumD_lakh", "strength"):
            if s.get(key) is not None:
                item[key] = s.get(key)
        out[rid] = item
    return out


def _vote_for_read(
    direction: Any,
    age_sec: Any,
    *,
    bullish: bool,
    max_age: float,
    neutral_blocks: bool,
) -> str:
    """AGREE | OPPOSE | NEUTRAL | NEUTRAL_BLOCK | STALE — never raises."""
    if direction is None:
        return "STALE"
    try:
        age = float(age_sec) if age_sec is not None else None
    except (TypeError, ValueError):
        age = None
    if age is None or age > max_age:
        return "STALE"
    side = "CE" if bullish else "PE"
    other = "PE" if bullish else "CE"
    d = str(direction).upper()
    if d not in ("CE", "PE", "NEUTRAL"):
        return "STALE"
    if d == "NEUTRAL":
        return "NEUTRAL_BLOCK" if neutral_blocks else "NEUTRAL"
    if d == side:
        return "AGREE"
    if d == other:
        return "OPPOSE"
    return "STALE"


def oi_switch_block(
    bullish: bool,
    cfg: Dict[str, Any],
    board_reads: Dict[str, Dict[str, Any]],
) -> Tuple[bool, Dict[str, Any]]:
    """Return (would_block, detail). All switches OFF => never blocks."""
    oi = load_oi_filters_cfg(cfg)
    switches = oi.get("switches") or {}
    on_ids = [rid for rid in PROTO_OI_READ_IDS if switches.get(rid)]
    max_age = float(oi.get("max_age_sec", 420.0) or 420.0)
    neutral_blocks = bool(oi.get("neutral_blocks"))
    rule = str(oi.get("rule") or "all_agree").lower()
    reads_in = board_reads or {}

    detail: Dict[str, Any] = {
        "mode": oi.get("mode"),
        "rule": rule,
        "neutral_blocks": neutral_blocks,
        "max_age_sec": max_age,
        "switches_on": on_ids,
        "reads": {},
        "agree": 0,
        "oppose": 0,
        "would_block": False,
        "blocked_by": [],
    }

    if not on_ids:
        return False, detail

    agree = 0
    oppose = 0
    blocked_by: List[str] = []

    for rid in PROTO_OI_READ_IDS:
        raw = reads_in.get(rid) or {}
        switch_on = bool(switches.get(rid))
        vote = (
            _vote_for_read(
                raw.get("direction"),
                raw.get("age_sec"),
                bullish=bullish,
                max_age=max_age,
                neutral_blocks=neutral_blocks,
            )
            if switch_on
            else "OFF"
        )
        detail["reads"][rid] = {
            "label": PROTO_OI_READ_LABELS.get(rid, rid),
            "switch_on": switch_on,
            "direction": raw.get("direction"),
            "age_sec": raw.get("age_sec"),
            "vote": vote,
        }
        if not switch_on or vote in ("OFF", "STALE", "NEUTRAL"):
            continue
        if vote == "AGREE":
            agree += 1
        elif vote in ("OPPOSE", "NEUTRAL_BLOCK"):
            oppose += 1
            blocked_by.append(rid)

    if rule == "any_agrees":
        blocked = agree == 0 and oppose > 0
    elif rule == "majority":
        blocked = oppose > agree
    else:
        blocked = oppose > 0

    detail["agree"] = agree
    detail["oppose"] = oppose
    detail["blocked_by"] = blocked_by
    detail["would_block"] = blocked
    return blocked, detail


def backtest_board_reads_at(
    *,
    day: str,
    asof_ts: int,
    market_db: Any,
    bridge_last: Optional[Dict[str, Any]] = None,
    scanner_cfg: Optional[Dict[str, Any]] = None,
) -> Tuple[Dict[str, Dict[str, Any]], str]:
    """Best-effort reads for historical replay. Missing history => stale (fail OPEN).

    Returns (board_reads map, coverage_note).
    """
    from datetime import datetime
    from pathlib import Path
    from zoneinfo import ZoneInfo

    IST = ZoneInfo("Asia/Kolkata")
    note_parts: List[str] = []
    try:
        from analyst.chain_reads_8510 import compute_reads, load_cfg as cr_cfg
        from analyst.ema8_prototype_8510 import load_ema8_prototype_cfg
        from analyst.desk8600_board_8510 import build_board
        from analyst.oi_context_8510 import compute_context, load_cfg as oi_load, load_day_snapshots

        db = Path(market_db) if market_db else None
        rows: List[Dict[str, Any]] = []
        paths = [db] if db and db.exists() else []
        if paths:
            rows = load_day_snapshots(paths, day, asof_ts=asof_ts)
        if not rows:
            note_parts.append(f"no chain snapshots for {day}")
        spot = 0.0
        if rows:
            spot = float(rows[-1].get("spot") or rows[-1].get("underlying") or 0)
        momentum = (load_ema8_prototype_cfg(scanner_cfg or {}).get("oi_filters") or {}).get("momentum")
        cr = (
            compute_reads(rows, spot, cr_cfg(scanner_cfg or {}), day=day, now_ts=asof_ts, momentum_cfg=momentum)
            if rows
            else {
                "status": "NO_DATA",
                "reads": {},
                "reason": "no snapshots",
            }
        )
        if cr.get("status") != "OK":
            note_parts.append(str(cr.get("reason") or "chain reads unavailable"))
        now = datetime.fromtimestamp(int(asof_ts), tz=IST)
        ctx = (
            compute_context(rows, spot, oi_load(scanner_cfg or {}), day=day, now_ts=asof_ts)
            if rows and spot > 0
            else {"status": "NO_DATA", "reason": "no snapshots"}
        )
        if ctx.get("status") != "OK":
            note_parts.append(str(ctx.get("reason") or "oi_context partial"))
        board = build_board(bridge_last or {}, ctx, scanner_cfg, now, chain_reads=cr)
        return board_reads_from_board(board), "; ".join(note_parts) if note_parts else "ok"
    except Exception as exc:
        return {rid: {"direction": None, "age_sec": None} for rid in PROTO_OI_READ_IDS}, f"error: {type(exc).__name__}"