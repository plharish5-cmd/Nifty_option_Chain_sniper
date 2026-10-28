"""
engine/desk_bias.py — Live Prop-Desk Bias Synthesis Engine.

Integrates the 4 core pillars:
  1. Trinity / PCR (pcr_oi, net_oi_chg, regime verdict)
  2. Writer Flow (Put ΔOI vs Call ΔOI, unwinding vs writing)
  3. Structural OI Walls (Nearest Put Support floor and Call Resistance ceiling)
  4. PE-CE Institutional Matrix (Stage 3 Acceleration / Spike detection)
  5. Expiry Playbook (Straddle decay, Pin Box bounds, Gamma Pin state)
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from .. import config, db
from ..timeutil import hm, now_epoch, now_ist, session_bounds

log = logging.getLogger("desk_bias")


def get_cfg() -> Dict[str, Any]:
    return config.rules().get("desk_bias", {})


def _fmt_cr(v: float, sign: bool = True) -> str:
    if v is None:
        return "—"
    s = "+" if v > 0 and sign else ("-" if v < 0 else "")
    av = abs(v)
    if av >= 1e7:
        return f"{s}{av / 1e7:.2f}Cr"
    if av >= 1e5:
        return f"{s}{av / 1e5:.1f}L"
    if av >= 1e3:
        return f"{s}{av / 1e3:.0f}K"
    return f"{s}{av:.0f}"


def get_structural_walls(spot: float, expiry: Optional[str] = None) -> tuple[float, float]:
    """
    Finds the nearest high-OI Put support wall (below spot)
    and Call resistance ceiling (above spot) from the latest chain snapshot.
    """
    today_dt = now_ist().date()
    today_start, today_end = session_bounds(today_dt)

    # Query latest snapshot
    ts_row = db.query(
        "SELECT MAX(ts) as max_ts FROM chain_snap WHERE ts >= ? AND ts <= ?",
        (today_start, today_end),
    )
    max_ts = ts_row[0]["max_ts"] if ts_row and ts_row[0]["max_ts"] else None

    if not max_ts:
        ts_row = db.query("SELECT MAX(ts) as max_ts FROM chain_snap")
        max_ts = ts_row[0]["max_ts"] if ts_row and ts_row[0]["max_ts"] else None

    if not max_ts:
        atm = round(spot / 50.0) * 50.0
        return atm - 100.0, atm + 100.0

    rows = db.query("""
        SELECT strike, ce_oi, pe_oi
        FROM chain_snap
        WHERE ts = ?
        ORDER BY strike ASC
    """, (max_ts,))

    if not rows:
        atm = round(spot / 50.0) * 50.0
        return atm - 100.0, atm + 100.0

    put_walls = [r for r in rows if float(r["strike"]) <= spot]
    call_walls = [r for r in rows if float(r["strike"]) >= spot]

    pe_wall = max(put_walls, key=lambda r: float(r["pe_oi"] or 0.0))["strike"] if put_walls else spot - 100.0
    ce_wall = max(call_walls, key=lambda r: float(r["ce_oi"] or 0.0))["strike"] if call_walls else spot + 100.0

    return float(pe_wall), float(ce_wall)


def extract_supporting_spike(matrix_rows: List[Dict[str, Any]], min_step_diff: int = 500000) -> str:
    """
    Scans recent Matrix 2.0 ledger rows for the latest institutional impulse or stage escalation.
    """
    if not matrix_rows:
        return "Orderly institutional flow across session"

    for r in matrix_rows:
        t_label = r.get("time_label", "")
        if t_label == "Prev Day":
            continue
        stage = r.get("stage_label", "")
        step_diff = int(r.get("pe_ce_step_diff", 0))

        if "Stage 3" in stage or "Acceleration" in stage:
            diff_str = _fmt_cr(step_diff)
            return f"Matrix {stage.split('(')[0].strip()} at {t_label} ({diff_str})"
        elif "Stage 1" in stage or "Breakout" in stage or "Breakdown" in stage:
            diff_str = _fmt_cr(step_diff)
            return f"Matrix Breakout at {t_label} ({diff_str})"
        elif abs(step_diff) >= min_step_diff:
            side = "PE Writers Surge" if step_diff > 0 else "CE Writers Surge"
            return f"{side} at {t_label} ({_fmt_cr(step_diff)})"

    # Default to top row stage
    top = matrix_rows[0]
    return f"Latest flow: {top.get('stage_label', 'Equilibrium')} at {top.get('time_label', '')}"


def compute_live_desk_bias(
    opt_expiry: Optional[str] = None,
    fut_contract: Optional[str] = None,
    hub: Optional[Any] = None,
    cfg: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Computes unified LIVE PROP-DESK BIAS synthesis.
    Combines Trinity PCR, Writer Flow, OI Walls, Matrix 2.0 Spikes, and Straddle Physics.
    """
    c = cfg or get_cfg()
    call_cap_net_oi = int(c.get("call_capitulation_net_oi", 5000000))
    put_cap_net_oi = int(c.get("put_capitulation_net_oi", -5000000))
    theta_chop_net_oi = int(c.get("theta_chop_net_oi", 2000000))
    theta_chop_min_decay = float(c.get("theta_chop_min_decay", 8.0))
    spike_min_step = int(c.get("spike_min_step_oi", 500000))

    # 1. Source Trinity / PCR
    from . import pcr_trinity
    from ..market.chain import ChainPoller

    trinity: Dict[str, Any] = {}
    if hub and hasattr(hub, "chain") and hasattr(hub.chain, "get_pcr_trinity"):
        trinity = hub.chain.get_pcr_trinity(exp=opt_expiry, fut_src=fut_contract or "combined", hub=hub)
    else:
        # Standalone poller fallback
        from ..dhan.rest import DhanREST
        poller = ChainPoller(DhanREST(), lambda *a, **k: None)
        trinity = poller.get_pcr_trinity(exp=opt_expiry, fut_src=fut_contract or "combined", hub=hub)

    series = trinity.get("pcr_oi_series") or []
    last_pt = series[-1] if series else {}

    spot = float(last_pt.get("fut_close") or trinity.get("fut_close") or 22750.0)
    pcr_oi = float(last_pt.get("pcr_oi") or trinity.get("pcr_oi") or 1.0)
    call_oi_chg = float(last_pt.get("call_oi_chg") or trinity.get("call_oi_chg") or 0.0)
    put_oi_chg = float(last_pt.get("put_oi_chg") or trinity.get("put_oi_chg") or 0.0)
    net_oi_chg = put_oi_chg - call_oi_chg

    # 2. Source VWAP
    today_start, _ = session_bounds(now_ist().date())
    fut_candles = db.candles_1m("FUT", today_start)
    if not fut_candles:
        fut_candles = db.candles_1m("13", today_start)
    tot_vol = sum(float(b.get("v") or 0.0) for b in fut_candles)
    tot_pv = sum(float(b.get("c") or 0.0) * float(b.get("v") or 0.0) for b in fut_candles)
    vwap = round(tot_pv / max(tot_vol, 1.0), 1) if tot_vol > 0 else spot

    # 3. Source OI Walls
    pe_wall, ce_wall = get_structural_walls(spot, opt_expiry)

    # 4. Source PE-CE Matrix for supporting spike
    from . import pe_ce_matrix
    matrix_data = pe_ce_matrix.compute_pe_ce_matrix(expiry=opt_expiry, tf="3m", fut_candles=fut_candles)
    supporting_spike = extract_supporting_spike(matrix_data.get("rows", []), spike_min_step)

    # 5. Source Expiry Straddle Breakevens & Decay
    from . import expiry_playbook
    expiry_data = expiry_playbook.compute_expiry_cockpit_telemetry(expiry=opt_expiry)
    straddle = expiry_data.get("straddle") or {}
    decay_pct = float(straddle.get("straddle_decay_pct") or 0.0)
    lower_be = float(straddle.get("lower_breakeven") or (spot - 100.0))
    upper_be = float(straddle.get("upper_breakeven") or (spot + 100.0))
    is_pinned = bool(expiry_data.get("pin", {}).get("is_pinned", False))

    call_cr_str = _fmt_cr(call_oi_chg)
    put_cr_str = _fmt_cr(put_oi_chg)
    net_cr_str = _fmt_cr(net_oi_chg)

    # ──────────────── Synthesis Logic ────────────────
    # Scenario 1: Call Capitulation / Short-Covering Squeeze
    if call_oi_chg < 0 and net_oi_chg >= call_cap_net_oi:
        bias_title = "🟢 AGGRESSIVE BUY DIPS / CE"
        bias_theme = "bullish"
        bias_color = "#10b981"
        narrative = (
            f"Call writers are capitulating (Call Δ at {call_cr_str}) while Put writers hold a {put_cr_str} floor. "
            f"Target the next Call Wall at {ce_wall:,.0f}. ({supporting_spike})."
        )
        target_wall = f"CE Wall {ce_wall:,.0f}"
        risk_note = f"Risk: Tighten SL to VWAP ({vwap:,.1f})"

    # Scenario 2: Put Capitulation / Long Unwinding
    elif put_oi_chg < 0 and net_oi_chg <= put_cap_net_oi:
        bias_title = "🔴 AGGRESSIVE SELL RALLIES / PE"
        bias_theme = "bearish"
        bias_color = "#ef4444"
        narrative = (
            f"Put writers are capitulating (Put Δ at {put_cr_str}) while Call writers cap the ceiling at {ce_wall:,.0f}. "
            f"Target the next Put Wall at {pe_wall:,.0f}. ({supporting_spike})."
        )
        target_wall = f"PE Wall {pe_wall:,.0f}"
        risk_note = f"Risk: Tighten SL above VWAP ({vwap:,.1f})"

    # Scenario 3: Gamma Pin / Theta Chop
    elif abs(net_oi_chg) < theta_chop_net_oi and (decay_pct >= theta_chop_min_decay or is_pinned):
        bias_title = "⚖️ THETA CHOP / GAMMA PIN"
        bias_theme = "neutral"
        bias_color = "#f59e0b"
        narrative = (
            f"Market is pinned inside Straddle Breakeven Box ({lower_be:,.0f}–{upper_be:,.0f}) with -{decay_pct:.1f}% decay. "
            f"Stand aside or trade iron condors until decisive breakout."
        )
        target_wall = f"Pin Box: {lower_be:,.0f} ↔ {upper_be:,.0f}"
        risk_note = "Risk: Avoid buying naked options inside pin box"

    # Scenario 4: Standard Bullish Trend
    elif net_oi_chg > 0:
        bias_title = "🟢 BULLISH BIAS (BUY DIPS / CE)"
        bias_theme = "bullish"
        bias_color = "#10b981"
        narrative = (
            f"Put writers dominant ({put_cr_str} vs {call_cr_str}). Support floor fortified at {pe_wall:,.0f}. "
            f"Target next Call Wall at {ce_wall:,.0f} on pullbacks. ({supporting_spike})."
        )
        target_wall = f"CE Wall {ce_wall:,.0f}"
        risk_note = f"Risk: Trail SL below VWAP ({vwap:,.1f})"

    # Scenario 5: Standard Bearish Trend
    elif net_oi_chg < 0:
        bias_title = "🔴 BEARISH BIAS (SELL RALLIES / PE)"
        bias_theme = "bearish"
        bias_color = "#ef4444"
        narrative = (
            f"Call writers dominant ({call_cr_str} vs {put_cr_str}). Resistance ceiling held at {ce_wall:,.0f}. "
            f"Target next Put Wall at {pe_wall:,.0f} on rallies. ({supporting_spike})."
        )
        target_wall = f"PE Wall {pe_wall:,.0f}"
        risk_note = f"Risk: Trail SL above VWAP ({vwap:,.1f})"

    # Scenario 6: Balanced / Pre-market
    else:
        bias_title = "⚖️ BALANCED / EQUILIBRIUM"
        bias_theme = "neutral"
        bias_color = "#64748b"
        narrative = "Options market in equilibrium. Awaiting initial institutional writer commitment."
        target_wall = f"Range: {pe_wall:,.0f} ↔ {ce_wall:,.0f}"
        risk_note = "Risk: Wait for trend ignition"

    time_str = hm(now_epoch())

    return {
        "ok": True,
        "bias_title": bias_title,
        "bias_theme": bias_theme,
        "bias_color": bias_color,
        "narrative": narrative,
        "target_wall": target_wall,
        "risk_note": risk_note,
        "supporting_spike": supporting_spike,
        "spot": round(spot, 1),
        "vwap": round(vwap, 1),
        "pcr": round(pcr_oi, 2),
        "net_oi_chg_cr": net_cr_str,
        "put_oi_chg_cr": put_cr_str,
        "call_oi_chg_cr": call_cr_str,
        "pe_wall": round(pe_wall),
        "ce_wall": round(ce_wall),
        "updated_at": f"{time_str} IST",
        "opt_expiry": opt_expiry or "cur",
    }
