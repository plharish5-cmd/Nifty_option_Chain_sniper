"""
engine/institutional_levels.py — Institutional ICT Reference Levels & True Breakout Engine.

Computes:
  1. PDH (Previous Day High), PDL (Previous Day Low), PDC (Previous Day Close), PDO (Previous Day Open)
  2. Session Open (09:15 AM IST Cash Open) and Midnight Open
  3. Breakout & Sweep Status:
     - Case A: PDH/PDL Liquidity Sweep (Judas Swing / False Breakout)
     - Case B: True PDH/PDL Breakout Expansion (RVOL >= 1.5x + Net PE-CE writer support)
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
from datetime import datetime, date

from .. import config, db
from ..timeutil import now_ist, session_bounds

log = logging.getLogger("institutional_levels")


def get_cfg() -> Dict[str, Any]:
    return config.rules().get("institutional_levels", {})


def compute_institutional_levels(
    sec_id: str = "13",
    intraday_candles: Optional[List[Dict[str, Any]]] = None,
    daily_candles: Optional[List[Dict[str, Any]]] = None,
    override_rvol: Optional[float] = None,
    override_pe_ce_delta_l: Optional[float] = None,
    cfg: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Computes ICT Reference Levels and detects Judas Swings vs True Breakout Expansions.
    """
    c = cfg or get_cfg()
    rvol_breakout_thresh = float(c.get("rvol_breakout_threshold", 1.5))
    rvol_sweep_thresh = float(c.get("rvol_sweep_threshold", 1.0))
    pe_ce_writer_support_thresh = float(c.get("pe_ce_writer_support_thresh_l", 50.0))  # 50 Lakhs

    today_dt = now_ist().date()
    today_start, today_end = session_bounds(today_dt)
    today_iso = today_dt.isoformat()

    # 1. Fetch or parse daily records
    daily_rows = daily_candles if daily_candles is not None else db.daily(sec_id, limit=10)

    # Defaults from config
    def_pdc = float(c.get("default_pdc", 22698.85))
    def_pdh = float(c.get("default_pdh", 22778.05))
    def_pdl = float(c.get("default_pdl", 22497.70))
    def_pdo = float(c.get("default_pdo", 22678.00))

    prev_day = None
    today_daily = None

    if daily_rows:
        # Sort by date
        sorted_daily = sorted(daily_rows, key=lambda x: str(x.get("d", "")))
        for r in sorted_daily:
            r_d = str(r.get("d", ""))
            if r_d == today_iso:
                today_daily = r
            elif r_d < today_iso:
                prev_day = r

        if not prev_day and len(sorted_daily) >= 2:
            prev_day = sorted_daily[-2]
        elif not prev_day and len(sorted_daily) == 1:
            prev_day = sorted_daily[0]

    # Where each level came from. The defaults in rules.json are one specific past session's OHLC, so
    # a silent fallback does not degrade gracefully -- it asserts a STALE level as fact. With 29 Oct
    # closing at 22,936.8, already above the default PDH of 22,778.05, a fallback on any later morning
    # would have the desk open declaring price above yesterday's high and able to fire
    # "PDH TRUE BREAKOUT EXPANSION — DO NOT FADE" against a level from days earlier. A confident wrong
    # signal in the feature built to stop the trader fading breakouts is worse than no signal, so the
    # provenance is tracked and the classifier refuses to run on it.
    have_prev = bool(prev_day and prev_day.get("h") and prev_day.get("l") and prev_day.get("c"))
    levels_source = "daily" if have_prev else "fallback"
    prev_day_date = str(prev_day.get("d", "")) if have_prev else None

    pdh = float(prev_day["h"]) if prev_day and prev_day.get("h") else def_pdh
    pdl = float(prev_day["l"]) if prev_day and prev_day.get("l") else def_pdl
    pdc = float(prev_day["c"]) if prev_day and prev_day.get("c") else def_pdc
    pdo = float(prev_day["o"]) if prev_day and prev_day.get("o") else def_pdo

    # Ensure PDH > PDL sanity
    if pdh <= pdl:
        pdh = max(pdh, def_pdh)
        pdl = min(pdl, def_pdl)

    # 2. Fetch or parse intraday bars
    if intraday_candles is None:
        intraday_candles = db.candles_1m(sec_id, today_start)
        if not intraday_candles and sec_id != "FUT":
            intraday_candles = db.candles_1m("FUT", today_start)

    today_bars = sorted(intraday_candles or [], key=lambda b: int(b.get("ts", 0)))

    # Session Open (09:15 AM IST)
    session_open = pdo
    if today_daily and today_daily.get("o"):
        session_open = float(today_daily["o"])
    elif today_bars:
        session_open = float(today_bars[0].get("o", today_bars[0].get("c", pdo)))

    # Midnight Open (First print of the calendar day, or PDC/Session Open fallback)
    midnight_open = pdc
    if today_bars:
        midnight_open = float(today_bars[0].get("o", session_open))

    # 3. Determine current price and session range extremes
    if today_bars:
        high_today = max(float(b.get("h", b.get("c", session_open))) for b in today_bars)
        low_today = min(float(b.get("l", b.get("c", session_open))) for b in today_bars)
        last_bar = today_bars[-1]
        last_close = float(last_bar.get("c", session_open))
        last_high = float(last_bar.get("h", last_close))
        last_low = float(last_bar.get("l", last_close))
        bar_rvol = float(last_bar.get("rvol") or 1.0)
    else:
        high_today = float(today_daily["h"]) if today_daily and today_daily.get("h") else session_open
        low_today = float(today_daily["l"]) if today_daily and today_daily.get("l") else session_open
        last_close = float(today_daily["c"]) if today_daily and today_daily.get("c") else session_open
        last_high = high_today
        last_low = low_today
        bar_rvol = 1.0

    current_rvol = override_rvol if override_rvol is not None else bar_rvol

    # 4. Resolve option writer support (Net PE-CE flow)
    pe_ce_delta_l = override_pe_ce_delta_l if override_pe_ce_delta_l is not None else 0.0
    if override_pe_ce_delta_l is None:
        try:
            from . import pe_ce_matrix
            mat = pe_ce_matrix.compute_pe_ce_matrix(tf="3m", scope="atm_10")
            if mat and mat.get("rows"):
                top_row = [r for r in mat["rows"] if r.get("time_label") != "Prev Day"]
                if top_row:
                    pe_ce_delta_l = float(top_row[0].get("pe_ce_day_diff", 0)) / 1e5
        except Exception as e_mat:
            log.debug("pe_ce_matrix delta fetch fallback: %s", e_mat)

    # 5. Breakout & Sweep Status Classification
    status = "INSIDE_RANGE"
    status_label = "⚖️ Inside Yesterday's Range"
    alert_text = ""
    alert_kind = ""
    action_tone = "flat"
    is_sweep = False
    is_breakout = False
    details = f"Spot {last_close:,.1f} trading between PDL ({pdl:,.1f}) and PDH ({pdh:,.1f})."

    # No real previous session => no classification. Not INSIDE_RANGE either, because that is itself a
    # claim about where price sits relative to a level we do not actually have.
    if not have_prev:
        status = "LEVELS_UNAVAILABLE"
        status_label = "⚠️ PDH/PDL unavailable — no previous session data"
        alert_text = ""
        alert_kind = ""
        action_tone = "warn"
        details = (
            f"Previous-session daily candle missing, so PDH/PDL/PDC fell back to the placeholder values "
            f"in rules.json ({def_pdh:,.1f} / {def_pdl:,.1f}). These are NOT yesterday's levels. No "
            f"breakout, breakdown or sweep is being classified until real daily data is available."
        )

    # CASE A: PDH Sweep Rejection (Judas Swing / False Breakout)
    # Price wicked past PDH, but current close is back inside PDH on low/exhausting volume
    elif (high_today > pdh or last_high > pdh) and last_close < pdh:
        is_sweep = True
        status = "PDH_SWEEP_REJECTION"
        status_label = "⚠️ PDH Liquidity Sweep (Judas Swing)"
        alert_text = "⚠️ PDH LIQUIDITY SWEEP (FALSE BREAKOUT) — Watch for reversion inside value area."
        alert_kind = "SIG_PDH_SWEEP"
        action_tone = "warn"
        details = (
            f"Price spiked above PDH {pdh:,.1f} (Session High {high_today:,.1f}) "
            f"and closed back below it at {last_close:,.1f} on {current_rvol:.1f}x RVOL. "
            f"Liquidity pool swept; potential Judas Swing."
        )

    # CASE B: True PDH Breakout Expansion
    # Price cleanly closed above PDH with high RVOL and supportive option writers
    elif last_close > pdh:
        pe_ce_supportive = pe_ce_delta_l >= pe_ce_writer_support_thresh
        if current_rvol >= rvol_breakout_thresh and pe_ce_supportive:
            is_breakout = True
            status = "PDH_TRUE_BREAKOUT"
            status_label = "🚀 PDH True Breakout Expansion"
            alert_text = "🚀 PDH TRUE BREAKOUT EXPANSION — External liquidity cleared; momentum continuation active. DO NOT FADE."
            alert_kind = "SIG_PDH_BREAKOUT"
            action_tone = "bull"
            details = (
                f"Clean close above PDH ({last_close:,.1f} > {pdh:,.1f}) confirmed by "
                f"{current_rvol:.1f}x RVOL (>= 1.5x) and supportive Net PE-CE writer delta "
                f"(+{pe_ce_delta_l:.1f}L >= +{pe_ce_writer_support_thresh:.0f}L). Momentum expansion active."
            )
        else:
            status = "PDH_ABOVE"
            status_label = f"🟢 Above PDH (+{last_close - pdh:.1f} pts)"
            alert_text = f"NIFTY holding above PDH {pdh:,.1f} @ {last_close:,.1f} (RVOL {current_rvol:.1f}x, PE−CE {pe_ce_delta_l:+.1f}L)."
            alert_kind = "SIG_PDH_ABOVE"
            action_tone = "bull"
            details = f"Price above PDH {pdh:,.1f}. Awaiting full 1.5x RVOL + 50L writer expansion confluence."

    # CASE C: PDL Sweep Rejection (Judas Swing / False Breakdown)
    # Price wicked past PDL, but current close is back above PDL on low volume
    elif (low_today < pdl or last_low < pdl) and last_close > pdl:
        is_sweep = True
        status = "PDL_SWEEP_REJECTION"
        status_label = "⚠️ PDL Liquidity Sweep (Judas Swing)"
        alert_text = "⚠️ PDL LIQUIDITY SWEEP (FALSE BREAKDOWN) — Watch for reversion inside value area."
        alert_kind = "SIG_PDL_SWEEP"
        action_tone = "warn"
        details = (
            f"Price flushed below PDL {pdl:,.1f} (Session Low {low_today:,.1f}) "
            f"and closed back above it at {last_close:,.1f} on {current_rvol:.1f}x RVOL. "
            f"Sell-side liquidity swept; potential Judas Swing bottom."
        )

    # CASE D: True PDL Breakdown Expansion
    # Price cleanly closed below PDL with high RVOL and Call writer pressure
    elif last_close < pdl:
        pe_ce_supportive = pe_ce_delta_l <= -pe_ce_writer_support_thresh
        if current_rvol >= rvol_breakout_thresh and pe_ce_supportive:
            is_breakout = True
            status = "PDL_TRUE_BREAKDOWN"
            status_label = "🩸 PDL True Breakdown Expansion"
            alert_text = "🩸 PDL TRUE BREAKDOWN EXPANSION — Sell-side liquidity cleared; downward momentum active. DO NOT FADE."
            alert_kind = "SIG_PDL_BREAKDOWN"
            action_tone = "bear"
            details = (
                f"Clean close below PDL ({last_close:,.1f} < {pdl:,.1f}) confirmed by "
                f"{current_rvol:.1f}x RVOL (>= 1.5x) and aggressive Call writer surge "
                f"({pe_ce_delta_l:.1f}L <= -{pe_ce_writer_support_thresh:.0f}L). Downward breakdown active."
            )
        else:
            status = "PDL_BELOW"
            status_label = f"🔴 Below PDL ({last_close - pdl:.1f} pts)"
            alert_text = f"NIFTY trading below PDL {pdl:,.1f} @ {last_close:,.1f} (RVOL {current_rvol:.1f}x, PE−CE {pe_ce_delta_l:+.1f}L)."
            alert_kind = "SIG_PDL_BELOW"
            action_tone = "bear"
            details = f"Price below PDL {pdl:,.1f}. Awaiting full 1.5x RVOL + 50L Call writer expansion confluence."

    return {
        "ok": True,
        "date": today_iso,
        "levels": {
            "pdh": round(pdh, 2),
            "pdl": round(pdl, 2),
            "pdc": round(pdc, 2),
            "pdo": round(pdo, 2),
            "session_open": round(session_open, 2),
            "midnight_open": round(midnight_open, 2),
        },
        "current_price": round(last_close, 2),
        "high_today": round(high_today, 2),
        "low_today": round(low_today, 2),
        # Provenance: the frontend must be able to tell a real level from a placeholder.
        "levels_source": levels_source,          # "daily" | "fallback"
        "levels_available": have_prev,
        "prev_day_date": prev_day_date,
        "status": status,
        "status_label": status_label,
        "alert_text": alert_text,
        "alert_kind": alert_kind,
        "action_tone": action_tone,
        "is_sweep": is_sweep,
        "is_breakout": is_breakout,
        "rvol": round(current_rvol, 2),
        "pe_ce_delta_l": round(pe_ce_delta_l, 2),
        "details": details,
    }
