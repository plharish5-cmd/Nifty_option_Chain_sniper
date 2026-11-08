"""Writer-flow gate: StockMojo/Dhan PE-CE surge, divergence, paper vs live block modes."""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)


def _mode_blocks(mode: str) -> bool:
    return str(mode or "off").lower() == "block"


def _mode_alerts(mode: str) -> bool:
    return str(mode or "off").lower() in ("alert", "block")


def _pick_surge_flags(
    snap: Dict[str, Any],
    entity: str,
) -> Dict[str, bool]:
    entity = str(entity or "change_oi").lower()
    ch_put = bool(snap.get("significant_put_surge_change"))
    ch_call = bool(snap.get("significant_call_surge_change"))
    tot_put = bool(snap.get("significant_put_surge_total"))
    tot_call = bool(snap.get("significant_call_surge_total"))
    if entity == "total_oi":
        return {"significant_put_surge": tot_put, "significant_call_surge": tot_call}
    if entity == "both":
        return {
            "significant_put_surge": ch_put or tot_put,
            "significant_call_surge": ch_call or tot_call,
        }
    return {"significant_put_surge": ch_put, "significant_call_surge": ch_call}


def evaluate_writer_context(
    snap: Dict[str, Any],
    dhan: Dict[str, Any],
    cfg: Dict[str, Any],
    *,
    stockmojo_master_enabled: bool = True,
) -> Dict[str, Any]:
    entity = str(cfg.get("entity", "change_oi")).lower()
    use_sm = stockmojo_master_enabled and bool(snap.get("available"))
    use_dhan = bool(dhan.get("available")) and bool(cfg.get("use_dhan_fallback", True))
    if not stockmojo_master_enabled:
        use_sm = False
    ctx: Dict[str, Any] = {
        "enabled": bool(cfg.get("enabled", False)),
        "entity": entity,
        "source": "STOCKMOJO_API" if use_sm else ("DHAN_API" if use_dhan else "NONE"),
        "available": use_sm or use_dhan,
        "significant_put_surge": False,
        "significant_call_surge": False,
        "writer_divergence": False,
        "atm_reversal_hint": "NONE",
        "synergy_status": "MIXED",
        "alerts": [],
    }
    if not ctx["enabled"]:
        ctx["mode"] = "disabled"
        return ctx

    if use_sm:
        surges = _pick_surge_flags(snap, entity)
        ctx.update(
            significant_put_surge=surges["significant_put_surge"],
            significant_call_surge=surges["significant_call_surge"],
            writer_divergence=bool(snap.get("writer_divergence")),
            atm_reversal_hint=str(snap.get("atm_reversal_hint") or "NONE"),
            synergy_status=str(snap.get("synergy_status") or "MIXED"),
            put_interval_lakhs=snap.get("put_interval_lakhs"),
            call_interval_lakhs=snap.get("call_interval_lakhs"),
            put_oi_total_lakhs=snap.get("put_oi_total_lakhs"),
            call_oi_total_lakhs=snap.get("call_oi_total_lakhs"),
            entity_mode=entity,
            data_origin="STOCKMOJO",
        )
    elif use_dhan and (stockmojo_master_enabled or cfg.get("use_dhan_when_stockmojo_off", False)):
        dc = float(dhan.get("oi_change_ce") or 0) / 1e5
        dp = float(dhan.get("oi_change_pe") or 0) / 1e5
        surge_put_l = float(cfg.get("surge_put_interval_lakhs", 20))
        surge_call_l = float(cfg.get("surge_call_interval_lakhs", 20))
        ctx.update(
            put_interval_lakhs=round(dp, 2),
            call_interval_lakhs=round(dc, 2),
            significant_put_surge=dp >= surge_put_l and dp > dc,
            significant_call_surge=dc >= surge_call_l and dc > dp,
            synergy_status=str(dhan.get("bias", "MIXED")).upper() + "_SYNERGY"
            if dhan.get("bias") in ("BULLISH", "BEARISH")
            else "MIXED",
        )
    else:
        ctx["available"] = False
        ctx["fail_open"] = bool(cfg.get("fail_open_when_stale", True))
        return ctx

    if ctx["significant_put_surge"]:
        ctx["alerts"].append("SURGE_PUT_WRITERS")
    if ctx["significant_call_surge"]:
        ctx["alerts"].append("SURGE_CALL_WRITERS")
    if ctx.get("writer_divergence"):
        ctx["alerts"].append("WRITER_DIVERGENCE_" + ctx.get("atm_reversal_hint", "NONE"))
    return ctx


def filter_signals_by_writer_gate(
    signals: List[Dict[str, Any]],
    ctx: Dict[str, Any],
    cfg: Dict[str, Any],
    live_armed: bool,
) -> List[Dict[str, Any]]:
    if not cfg.get("enabled") or not signals:
        return signals
    if not ctx.get("available") and cfg.get("fail_open_when_stale", True):
        return signals

    paper = not live_armed
    put_mode = cfg.get("paper_put_surge_mode" if paper else "live_put_surge_mode", "alert")
    call_mode = cfg.get("paper_call_surge_mode" if paper else "live_call_surge_mode", "alert")
    block_put = _mode_blocks(put_mode) and bool(ctx.get("significant_put_surge"))
    block_call = _mode_blocks(call_mode) and bool(ctx.get("significant_call_surge"))
    block_ce_synergy = bool(cfg.get("block_ce_when_bearish", False)) and ctx.get("synergy_status") == "BEARISH_SYNERGY"
    block_pe_synergy = bool(cfg.get("block_pe_when_bullish", False)) and ctx.get("synergy_status") == "BULLISH_SYNERGY"
    if cfg.get("block_ce_on_call_surge", True) and ctx.get("significant_call_surge"):
        block_call = block_call or _mode_blocks(call_mode)
    if cfg.get("block_pe_on_put_surge", True) and ctx.get("significant_put_surge"):
        block_put = block_put or _mode_blocks(put_mode)

    out: List[Dict[str, Any]] = []
    for sig in signals:
        opt = str(sig.get("opt_side", "")).upper()
        note_parts = []
        reject = False
        if opt == "CE" and (block_call or block_ce_synergy):
            reject = block_call or block_ce_synergy
            note_parts.append("CE blocked: call-writer surge / bearish synergy")
        if opt == "PE" and (block_put or block_pe_synergy):
            reject = block_put or block_pe_synergy
            note_parts.append("PE blocked: put-writer surge / bullish synergy")
        if _mode_alerts(put_mode) and ctx.get("significant_put_surge"):
            note_parts.append("Alert: put-writer surge")
        if _mode_alerts(call_mode) and ctx.get("significant_call_surge"):
            note_parts.append("Alert: call-writer surge")
        if ctx.get("writer_divergence"):
            note_parts.append("Divergence: " + str(ctx.get("atm_reversal_hint")))
        if note_parts:
            sig["writer_flow_gate"] = {
                "alerts": list(ctx.get("alerts") or []),
                "notes": note_parts,
                "source": ctx.get("source"),
                "entity": cfg.get("entity"),
            }
            prev = sig.get("advisory_note", "")
            sig["advisory_note"] = (prev + " | " + " | ".join(note_parts)).strip(" |")
            if ctx.get("writer_divergence"):
                sig["play_writer_divergence_beep"] = True
            if ctx.get("significant_put_surge") or ctx.get("significant_call_surge"):
                sig["play_writer_surge_beep"] = True
        if reject:
            log.info("[WRITER FLOW GATE] Blocked %s (%s)", sig.get("setup_name"), note_parts)
            continue
        out.append(sig)
    return out
