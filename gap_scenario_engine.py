"""
gap_scenario_engine.py — Daily Opening Gap Simulator & Multi-Confluence Engine.

Universally handles ANY trading session (Monday through Friday):
  1. SMC Structure (BSL/SSL Sweeps, Order Blocks, Opening Fair Value Gaps, Dealing Range)
  2. Option Chain Walls (Call/Put Writer Squeeze, Gamma Squeeze Risk, Max Pain Pull)
  3. Price Action & CPR Geometry (Central Pivot Range Width, TC/BC, Virgin Levels)
  4. Google TimesFM 3.0 AI Quantile Envelope (q10 Floor, q50 Median, q90 Cap)
  5. Dynamic Daily Playbook (CE vs PE Decision Tree for 09:15–09:45 AM for any weekday)
"""

from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any

import pandas as pd
import streamlit as st

import config
import confluence as cf
import levels as lv_mod
import price_action as pa
import smc
from ui_utils import render_html, _render_html


# ═══════════════════════════════════════════════════════════════════════════
# 1. Universal Multi-Confluence Computation Engine
# ═══════════════════════════════════════════════════════════════════════════

def evaluate_opening_gap_scenarios(
    spot: float,
    df_chain: pd.DataFrame | None,
    pa_frames: dict | None,
    read: Any,
    vb_daily: pd.DataFrame | None,
    projected_open: float | None = None,
) -> dict[str, Any]:
    """
    Computes complete multi-confluence opening gap modeling for ANY trading day (Mon-Fri).
    Dynamically infers the prior trading day and the upcoming session day.
    """
    # ── 1. Extract Prior Day Baseline Anchors & Dynamic Day Names ───────────
    prev_date = None
    if vb_daily is not None and not vb_daily.empty:
        prev_row = vb_daily.iloc[-1]
        pdh = float(prev_row["High"])
        pdl = float(prev_row["Low"])
        pdc = float(prev_row["Close"])
        prev_date = prev_row.get("Date") or prev_row.get("date")
    else:
        pdc = spot
        pdh = spot + 45.0
        pdl = spot - 45.0

    fr_range = round(pdh - pdl, 1)
    eq_50 = round((pdh + pdl) / 2.0, 1)

    # Dynamic session naming
    if prev_date is not None and hasattr(prev_date, "strftime"):
        prev_day_name = prev_date.strftime("%A")        # e.g., "Friday", "Monday", "Tuesday"
        prev_date_str = prev_date.strftime("%d-%b")       # e.g., "05-Sep"
        weekday_idx = prev_date.weekday() if hasattr(prev_date, "weekday") else 0
        # If Friday (4), next session is Monday; otherwise next calendar trading day
        next_day_name = "Monday" if weekday_idx == 4 else (prev_date + pd.Timedelta(days=1)).strftime("%A")
    else:
        prev_day_name = "Prior Day"
        prev_date_str = ""
        next_day_name = "Next Session"

    # CPR
    cpr_levels, cpr_meta = [], {}
    if vb_daily is not None and len(vb_daily) >= 2:
        try:
            cpr_levels, cpr_meta = lv_mod.central_pivot_range(vb_daily, spot)
        except Exception:
            pass

    cpr_top = cpr_meta.get("top", round(pdc + 20.0, 1))
    cpr_pivot = cpr_meta.get("pivot", round(pdc, 1))
    cpr_bot = cpr_meta.get("bottom", round(pdc - 20.0, 1))
    cpr_width = cpr_meta.get("width", 40.0)
    cpr_verdict = cpr_meta.get("verdict", "NORMAL")
    cpr_hint = cpr_meta.get("hint", "no strong bias from CPR width")

    # Option Chain Walls
    s1 = getattr(read.levels, "s1", round(pdc - 50.0, 0)) if read and hasattr(read, "levels") else round(pdc - 50.0, 0)
    r1 = getattr(read.levels, "r1", round(pdc + 50.0, 0)) if read and hasattr(read, "levels") else round(pdc + 50.0, 0)
    s2 = getattr(read.levels, "s2", round(s1 - 50.0, 0)) if read and hasattr(read, "levels") else round(s1 - 50.0, 0)
    r2 = getattr(read.levels, "r2", round(r1 + 50.0, 0)) if read and hasattr(read, "levels") else round(r1 + 50.0, 0)
    max_pain = getattr(read.levels, "max_pain", pdc) if read and hasattr(read, "levels") else pdc
    pcr = getattr(read.bias, "pcr", 1.0) if read and hasattr(read, "bias") else 1.0

    # SMC Structure
    df_15m = pa_frames.get("15m") if pa_frames else None
    if df_15m is None or len(df_15m) < 10:
        try:
            df_15m = pa.load_history_candles(days=3, timeframe="15m")
        except Exception:
            df_15m = None

    smc_res = smc.read(df_15m, n=2, spot=spot) if df_15m is not None and not df_15m.empty else {}
    stx = smc_res.get("structure")
    stx_trend = stx.trend if stx else "NEUTRAL"
    open_obs = smc_res.get("open_obs", [])
    open_fvgs = smc_res.get("open_fvgs", [])
    open_pools = smc_res.get("open_pools", [])

    # Buy-Side Liquidity (BSL) and Sell-Side Liquidity (SSL)
    bsl_pool = max([p.level for p in open_pools if p.kind == "EQH"], default=pdh)
    ssl_pool = min([p.level for p in open_pools if p.kind == "EQL"], default=pdl)

    # AI Quantile Envelope (TimesFM 3.0)
    df_5m = pa_frames.get("5m") if pa_frames else None
    ai_gate = cf.evaluate_ai_confluence_gate(spot, df_15m=df_15m, df_5m=df_5m)
    q10_floor = ai_gate.q10_floor if ai_gate.q10_floor > 0 else round(pdc - 75.0, 1)
    q50_med = ai_gate.q50_median if ai_gate.q50_median > 0 else round(pdc, 1)
    q90_cap = ai_gate.q90_cap if ai_gate.q90_cap > 0 else round(pdc + 75.0, 1)

    # ── 2. Helper to evaluate a specific Open Price ─────────────────────────
    def _evaluate_single_open(open_price: float, label: str = "") -> dict[str, Any]:
        gap_pts = round(open_price - pdc, 1)
        gap_pct = round((gap_pts / pdc) * 100.0, 2)

        if gap_pts >= 110:
            classification = "MEGA GAP UP"
            badge_color = "#9C27B0"
        elif gap_pts >= 40:
            classification = "MODERATE GAP UP"
            badge_color = "#0F9D58"
        elif gap_pts <= -110:
            classification = "MEGA GAP DOWN"
            badge_color = "#E8503A"
        elif gap_pts <= -40:
            classification = "MODERATE GAP DOWN"
            badge_color = "#FF5722"
        else:
            classification = "FLAT / CONSOLIDATION"
            badge_color = "#00BCD4"

        # A. SMC Impact
        smc_impacts = []
        is_bsl_sweep = open_price > pdh
        is_ssl_sweep = open_price < pdl

        if is_bsl_sweep:
            smc_impacts.append({
                "icon": "🚨", "type": "BSL_SWEEP",
                "headline": f"Buy-Side Liquidity (BSL) Sweep above {prev_day_name} High ({pdh:,.1f})",
                "detail": (
                    f"Price opens {open_price - pdh:,.1f} pts ABOVE {prev_day_name}'s High. "
                    f"Retail short stops triggered at the opening bell. Smart money has instant exit liquidity. "
                    f"High probability of an institutional Judas Swing (Fade back into {prev_day_name}'s range) "
                    f"unless price sustains above {open_price:,.0f} with heavy volume."
                )
            })
        elif is_ssl_sweep:
            smc_impacts.append({
                "icon": "🚨", "type": "SSL_SWEEP",
                "headline": f"Sell-Side Liquidity (SSL) Sweep below {prev_day_name} Low ({pdl:,.1f})",
                "detail": (
                    f"Price opens {pdl - open_price:,.1f} pts BELOW {prev_day_name}'s Low. "
                    f"Retail long stops triggered at the bell. Smart money absorbs panic selling. "
                    f"High probability of a Bear Trap (Mean-reversion bounce back into {prev_day_name}'s range) "
                    f"unless price breaks down with aggressive Call OI build."
                )
            })
        else:
            range_pos = (open_price - pdl) / fr_range if fr_range > 0 else 0.5
            zone_desc = "Premium (>50%)" if range_pos > 0.5 else "Discount (<50%)"
            smc_impacts.append({
                "icon": "⚖️", "type": "INSIDE_RANGE",
                "headline": f"Inside {prev_day_name}'s Dealing Range ({zone_desc} @ {range_pos*100:.0f}%)",
                "detail": (
                    f"Price opens between {prev_day_name}'s Low ({pdl:,.1f}) and High ({pdh:,.1f}). "
                    f"50% Equilibrium sits at {eq_50:,.1f}. No external liquidity pools breached at open. "
                    f"Wait for rotation towards boundary Order Blocks."
                )
            })

        # Opening FVG
        if abs(gap_pts) >= 30:
            smc_impacts.append({
                "icon": "🕳️", "type": "OPENING_FVG",
                "headline": f"Opening Fair Value Gap (Imbalance of {abs(gap_pts):.0f} pts)",
                "detail": (
                    f"An overnight opening gap creates a vacuum between {prev_day_name} Close ({pdc:,.1f}) "
                    f"and Open ({open_price:,.1f}). Statistically, ~70% of Nifty opening gaps retest "
                    f"or fill this void during the first 90 minutes of trading."
                )
            })

        # Order Block Collision
        tested_obs = [ob for ob in open_obs if ob.contains(open_price)]
        if tested_obs:
            for tob in tested_obs:
                smc_impacts.append({
                    "icon": "🧱", "type": "OB_COLLISION",
                    "headline": f"Direct Hit on {tob.direction} Order Block [{tob.low:,.0f}–{tob.high:,.0f}]",
                    "detail": f"Open price lands directly inside a standing {tob.direction} Order Block. Immediate battleground zone for institutional defense."
                })

        # B. Option Chain Wall Stress
        chain_impacts = []
        if open_price >= r1:
            call_sq_pts = open_price - r1
            chain_impacts.append({
                "icon": "🔥", "status": "DANGER_CE_WRITERS",
                "headline": f"Call Writer Trap / Squeeze Risk @ R1 ({r1:,.0f})",
                "detail": (
                    f"Nifty opens {call_sq_pts:.0f} pts above Resistance R1 ({r1:,.0f}). "
                    f"Call sellers are instantly trapped in deep mark-to-market losses. "
                    f"If Nifty holds above {r1:,.0f} past 09:30 AM, forced Call covering will trigger "
                    f"a rapid Gamma blast toward R2 ({r2:,.0f})!"
                )
            })
        elif open_price <= s1:
            put_sq_pts = s1 - open_price
            chain_impacts.append({
                "icon": "🔥", "status": "DANGER_PE_WRITERS",
                "headline": f"Put Writer Trap / Long Panic Risk @ S1 ({s1:,.0f})",
                "detail": (
                    f"Nifty opens {put_sq_pts:.0f} pts below Support S1 ({s1:,.0f}). "
                    f"Put writers face margin distress. If Nifty fails to reclaim {s1:,.0f} by 09:30 AM, "
                    f"cascading long unwinding can drag index sharply toward S2 ({s2:,.0f})!"
                )
            })
        else:
            chain_impacts.append({
                "icon": "🛡️", "status": "CHAIN_COMFORT",
                "headline": f"Option Writers Safe between S1 ({s1:,.0f}) & R1 ({r1:,.0f})",
                "detail": (
                    f"Open price {open_price:,.1f} is comfortably bracketed by major walls. "
                    f"Premium sellers will defend both sides, inducing range decay unless broken."
                )
            })

        # C. CPR & Price Levels Geometry
        cpr_status = "ABOVE_CPR" if open_price > cpr_top else ("BELOW_CPR" if open_price < cpr_bot else "INSIDE_CPR")
        cpr_detail = (
            f"Open is ABOVE CPR Top ({cpr_top:,.1f}). Bullish structural posture. "
            if cpr_status == "ABOVE_CPR" else
            (f"Open is BELOW CPR Bottom ({cpr_bot:,.1f}). Bearish structural posture. "
             if cpr_status == "BELOW_CPR" else
             f"Open is INSIDE Central Pivot Range ({cpr_bot:,.0f}–{cpr_top:,.0f}). Expect two-way whipsaws.")
        )
        cpr_confluence = {
            "status": cpr_status,
            "headline": f"CPR Relation: {cpr_status.replace('_', ' ')} ({cpr_verdict} CPR)",
            "detail": cpr_detail + f" CPR Width is {cpr_width:.0f} pts ({cpr_hint})."
        }

        # D. AI TimesFM Quantile Envelope
        is_above_q90 = open_price >= (q90_cap - 10.0)
        is_below_q10 = open_price <= (q10_floor + 10.0)
        if is_above_q90:
            ai_status = "STATISTICAL_CEILING"
            ai_headline = f"⚠️ AI Quantile Cap Warning (q90 = {q90_cap:,.1f})"
            ai_detail = (
                f"Projected open {open_price:,.1f} is pressing against Google TimesFM 90th percentile ({q90_cap:,.1f}). "
                f"Statistically, only ~13% of sessions sustain uninterrupted expansion beyond q90. "
                f"High mathematical probability of an intraday Mean-Reversion FADE back toward Median ({q50_med:,.1f})!"
            )
        elif is_below_q10:
            ai_status = "STATISTICAL_FLOOR"
            ai_headline = f"🛡️ AI Quantile Floor Support (q10 = {q10_floor:,.1f})"
            ai_detail = (
                f"Projected open {open_price:,.1f} is testing Google TimesFM 10th percentile ({q10_floor:,.1f}). "
                f"Statistically, only ~13% of sessions sustain downward expansion below q10 without a bounce. "
                f"High mathematical probability of an intraday Mean-Reversion BOUNCE back toward Median ({q50_med:,.1f})!"
            )
        else:
            ai_status = "NORMAL_ENVELOPE"
            ai_headline = f"AI Volatility Cone Normal ({q10_floor:,.0f} – {q90_cap:,.0f})"
            ai_detail = f"Open price {open_price:,.1f} is inside standard statistical volatility bounds. Median is {q50_med:,.1f}."

        # E. Actionable Execution Playbook: CE vs PE
        atm_strike = int(round(open_price / 50.0) * 50)

        # Decision logic
        if gap_pts >= 40:
            # GAP UP SCENARIOS
            fade_contract = f"{atm_strike} PE"
            fade_trigger = f"Wait for 5m/15m candle rejection at open. If candle fails to hold above {pdh:,.0f} and prints upper wick, BUY {fade_contract} on break of 5m candle low."
            fade_sl = f"{round(open_price + 20.0, 1)} (Above Opening High)"
            fade_t1 = f"{pdc:,.1f} ({prev_day_name} Close / Gap Fill, +{abs(gap_pts):.0f} pts)"
            fade_t2 = f"{eq_50:,.1f} ({prev_day_name} 50% Equilibrium)"

            breakout_contract = f"{atm_strike} CE"
            breakout_trigger = f"If 1st 15m candle closes with strong green body above {r1:,.0f} and Call OI unwinds, BUY {breakout_contract} on pullback to 15m VWAP / Opening Low."
            breakout_sl = f"{round(open_price - 25.0, 1)} (Below Opening Range Low)"
            breakout_t1 = f"{r2:,.0f} (+{abs(r2 - open_price):.0f} pts)"
            breakout_t2 = f"{round(open_price + 90.0, 0)} (Extended BSL Target)"

            primary_verdict = "FAVOR TRAP FADE (PE)" if (is_above_q90 or cpr_verdict == "WIDE") else "FAVOR EXPANSION (CE) ON PULLBACK"
            primary_reason = (
                f"Open is in Deep Premium ({gap_pts:+.0f} pts). AI q90 Cap ({q90_cap:,.0f}) and BSL sweep favor Judas Swing fade unless Call writers at {r1:,.0f} panic."
            )
        elif gap_pts <= -40:
            # GAP DOWN SCENARIOS
            fade_contract = f"{atm_strike} CE"
            fade_trigger = f"Wait for 5m/15m candle rejection at open. If candle leaves long lower absorption wick below {pdl:,.0f} and reclaims {pdl:,.0f}, BUY {fade_contract} on break of 5m candle high."
            fade_sl = f"{round(open_price - 20.0, 1)} (Below Opening Low)"
            fade_t1 = f"{pdc:,.1f} ({prev_day_name} Close / Gap Fill, +{abs(gap_pts):.0f} pts)"
            fade_t2 = f"{eq_50:,.1f} ({prev_day_name} 50% Equilibrium)"

            breakout_contract = f"{atm_strike} PE"
            breakout_trigger = f"If 1st 15m candle closes with solid red body below {s1:,.0f} and Put OI unwinds, BUY {breakout_contract} on pullback to breakdown retest level."
            breakout_sl = f"{round(open_price + 25.0, 1)} (Above Opening Range High)"
            breakout_t1 = f"{s2:,.0f} (-{abs(open_price - s2):.0f} pts)"
            breakout_t2 = f"{round(open_price - 90.0, 0)} (Extended SSL Target)"

            primary_verdict = "FAVOR BOUNCE FADE (CE)" if (is_below_q10 or cpr_verdict == "WIDE") else "FAVOR BREAKDOWN (PE) ON PULLBACK"
            primary_reason = (
                f"Open is in Deep Discount ({gap_pts:+.0f} pts). AI q10 Floor ({q10_floor:,.0f}) and SSL sweep favor Bear Trap bounce unless Put writers at {s1:,.0f} capitulate."
            )
        else:
            # FLAT SCENARIOS
            fade_contract = f"{atm_strike} CE / PE Scalp"
            fade_trigger = f"Trade mean-reversion between S1 ({s1:,.0f}) and R1 ({r1:,.0f}). Buy at S1 with SL -15 pts; Short at R1 with SL +15 pts."
            fade_sl = "15 pts beyond wall"
            fade_t1 = f"{eq_50:,.1f} ({prev_day_name} Equilibrium)"
            fade_t2 = "Opposing Wall"

            breakout_contract = "Wait for Range Breakout"
            breakout_trigger = f"Do not buy options in the first 30 minutes. Wait for clean 15m close above {r1:,.0f} (CE) or below {s1:,.0f} (PE)."
            breakout_sl = "Opposite side of breakout bar"
            breakout_t1 = "+35 pts"
            breakout_t2 = "+70 pts"

            primary_verdict = "STAND ASIDE / SCALP RANGE ONLY"
            primary_reason = f"Flat open ({gap_pts:+.0f} pts). Nifty remains trapped inside {prev_day_name}'s range ({pdl:,.0f}–{pdh:,.0f}). Option sellers dominate."

        return {
            "label": label or classification,
            "open_price": round(open_price, 1),
            "gap_pts": gap_pts,
            "gap_pct": gap_pct,
            "classification": classification,
            "badge_color": badge_color,
            "atm_strike": atm_strike,
            "primary_verdict": primary_verdict,
            "primary_reason": primary_reason,
            "smc_impacts": smc_impacts,
            "chain_impacts": chain_impacts,
            "cpr_confluence": cpr_confluence,
            "ai_envelope": {
                "status": ai_status,
                "headline": ai_headline,
                "detail": ai_detail,
                "q10": q10_floor, "q50": q50_med, "q90": q90_cap
            },
            "playbook": {
                "fade": {
                    "title": "Strategy 1: The Trap / Judas Swing Fade (Mean-Reversion)",
                    "contract": fade_contract,
                    "trigger": fade_trigger,
                    "stop_loss": fade_sl,
                    "target_1": fade_t1,
                    "target_2": fade_t2,
                },
                "breakout": {
                    "title": "Strategy 2: The Gap-and-Go Expansion (Momentum Runner)",
                    "contract": breakout_contract,
                    "trigger": breakout_trigger,
                    "stop_loss": breakout_sl,
                    "target_1": breakout_t1,
                    "target_2": breakout_t2,
                }
            }
        }

    # Default to spot if projected_open not specified
    target_open = projected_open if (projected_open is not None and projected_open > 0) else spot
    simulated_open = _evaluate_single_open(target_open, "Simulated Open")

    # Pre-computed Standard Scenarios
    standard_scenarios = [
        _evaluate_single_open(round(pdc + 140.0, 1), "🚀 Extreme Gap Up (+140 pts)"),
        _evaluate_single_open(round(pdc + 65.0, 1), "🟢 Moderate Gap Up (+65 pts)"),
        _evaluate_single_open(round(pdc, 1), "⚖️ Flat Open (0 pts)"),
        _evaluate_single_open(round(pdc - 65.0, 1), "🔴 Moderate Gap Down (-65 pts)"),
        _evaluate_single_open(round(pdc - 140.0, 1), "🩸 Extreme Gap Down (-140 pts)"),
    ]

    return {
        "spot": spot,
        "pdc": pdc,
        "pdh": pdh,
        "pdl": pdl,
        "fr_range": fr_range,
        "eq_50": eq_50,
        "prev_day_name": prev_day_name,
        "prev_date_str": prev_date_str,
        "next_day_name": next_day_name,
        "cpr": {
            "top": cpr_top, "pivot": cpr_pivot, "bottom": cpr_bot,
            "width": cpr_width, "verdict": cpr_verdict, "hint": cpr_hint
        },
        "chain": {
            "s1": s1, "r1": r1, "s2": s2, "r2": r2, "max_pain": max_pain, "pcr": pcr
        },
        "smc": {
            "trend": stx_trend, "bsl": bsl_pool, "ssl": ssl_pool,
            "open_obs": len(open_obs), "open_fvgs": len(open_fvgs)
        },
        "ai_gate": {
            "q10": q10_floor, "q50": q50_med, "q90": q90_cap
        },
        "simulated_open": simulated_open,
        "standard_scenarios": standard_scenarios,
    }


# Backward-compatible alias
evaluate_monday_scenarios = evaluate_opening_gap_scenarios


# ═══════════════════════════════════════════════════════════════════════════
# 2. Executive Streamlit Tab Renderer
# ═══════════════════════════════════════════════════════════════════════════

def render_gap_scenario_tab(
    spot: float,
    df_chain: pd.DataFrame | None,
    pa_frames: dict | None,
    read: Any,
    vb_daily: pd.DataFrame | None,
    CTX: Any = None,
    now: datetime | None = None,
):
    """
    Renders the universal Daily Opening Gap Simulator & Multi-Confluence Cockpit.
    """
    initial_data = evaluate_opening_gap_scenarios(spot, df_chain, pa_frames, read, vb_daily, projected_open=spot)
    p_day = initial_data["prev_day_name"]
    n_day = initial_data["next_day_name"]

    _render_html(
        f"""
        <div style="background:#0F1824;border-left:5px solid #00BCD4;border-radius:6px;padding:12px 16px;margin-bottom:14px;">
            <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:6px;">
                <span style="font-size:16px;font-weight:800;color:#00BCD4;letter-spacing:0.5px;">
                    🌅 DAILY OPENING GAP SIMULATOR & MULTI-CONFLUENCE ENGINE
                </span>
                <span style="font-size:11px;color:#8FA6B8;font-weight:600;background:#162332;padding:3px 8px;border-radius:4px;">
                    UNIVERSAL OPENING PLAYBOOK ({p_day.upper()} → {n_day.upper()})
                </span>
            </div>
            <div style="font-size:13px;color:#C7D2DF;margin-top:4px;line-height:1.4;">
                Fuses <b>SMC Structure</b> (BSL/SSL Sweeps, Order Blocks), <b>Option Chain Walls</b> (Call/Put Squeezes),
                <b>CPR Geometry</b>, and <b>Google TimesFM AI Quantile Cones</b> to generate an unambiguous
                CE vs PE decision tree for <b>EVERY TRADING DAY</b> (Monday through Friday) at 09:15–09:45 AM.
            </div>
        </div>
        """
    )

    # ── Master Prior Day Anchors Barometer ──────────────────────────────────
    m1, m2, m3, m4, m5, m6 = st.columns(6)
    m1.metric(f"{p_day} Close (PDC)", f"{initial_data['pdc']:,.1f}")
    m2.metric(f"{p_day} High (BSL)", f"{initial_data['pdh']:,.1f}")
    m3.metric(f"{p_day} Low (SSL)", f"{initial_data['pdl']:,.1f}")
    m4.metric(f"{p_day} Equilibrium", f"{initial_data['eq_50']:,.1f}")
    m5.metric(f"CPR ({initial_data['cpr']['verdict']})", f"{initial_data['cpr']['pivot']:,.0f}", f"{initial_data['cpr']['width']:.0f} pts wide")
    m6.metric("AI Envelope (q10–q90)", f"{initial_data['ai_gate']['q50']:,.0f}", f"{initial_data['ai_gate']['q10']:,.0f} – {initial_data['ai_gate']['q90']:,.0f}")

    st.divider()

    # ── Interactive Simulator Control ───────────────────────────────────────
    st.markdown(f"### 🎛️ Interactive {n_day} Open Simulator (GIFT Nifty / Scenario Dial)")
    st.caption(f"Select a standard institutional scenario preset or type the exact GIFT Nifty projected open price for {n_day}:")

    c_sel, c_num = st.columns([2, 1])

    preset_options = [
        "🚀 Extreme Gap Up (+140 pts)",
        "🟢 Moderate Gap Up (+65 pts)",
        "⚖️ Flat Open (0 pts)",
        "🔴 Moderate Gap Down (-65 pts)",
        "🩸 Extreme Gap Down (-140 pts)",
        "🎯 Custom Price Input"
    ]

    with c_sel:
        preset_choice = st.pills("Institutional Presets", preset_options, default=preset_options[1], key="gap_preset_pill")

    pdc = initial_data["pdc"]
    preset_price_map = {
        "🚀 Extreme Gap Up (+140 pts)": round(pdc + 140.0, 1),
        "🟢 Moderate Gap Up (+65 pts)": round(pdc + 65.0, 1),
        "⚖️ Flat Open (0 pts)": round(pdc, 1),
        "🔴 Moderate Gap Down (-65 pts)": round(pdc - 65.0, 1),
        "🩸 Extreme Gap Down (-140 pts)": round(pdc - 140.0, 1),
    }

    default_price = preset_price_map.get(preset_choice, spot)

    with c_num:
        sim_price_input = st.number_input(
            f"Projected {n_day} Open Price",
            min_value=float(round(pdc - 500.0, 0)),
            max_value=float(round(pdc + 500.0, 0)),
            value=float(default_price),
            step=5.0,
            format="%.1f",
            key="gap_sim_price_num"
        )

    # Re-evaluate with selected simulated price
    data = evaluate_opening_gap_scenarios(spot, df_chain, pa_frames, read, vb_daily, projected_open=sim_price_input)
    sim = data["simulated_open"]

    # ── Executive Scenario Verdict Banner ───────────────────────────────────
    v_col = sim["badge_color"]
    _render_html(
        f"""
        <div style="background:#0D1926;border:2px solid {v_col};border-radius:8px;padding:14px 18px;margin:15px 0 20px 0;">
            <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;">
                <div>
                    <span style="font-size:18px;font-weight:800;color:#FFF;">
                        SIMULATED {n_day.upper()} OPEN: {sim['open_price']:,.1f}
                    </span>
                    <span style="background:{v_col};color:#FFF;font-weight:800;font-size:12px;padding:3px 9px;border-radius:4px;margin-left:10px;">
                        {sim['gap_pts']:+.1f} PTS ({sim['gap_pct']:+.2f}%) · {sim['classification']}
                    </span>
                </div>
                <div style="font-size:14px;font-weight:700;color:{v_col};">
                    🎯 PRIMARY VERDICT: {sim['primary_verdict']}
                </div>
            </div>
            <div style="color:#D5DFEA;font-size:13.5px;margin-top:8px;line-height:1.45;">
                <b>Macro Assessment:</b> {sim['primary_reason']}
            </div>
        </div>
        """
    )

    # ── 4-Column Multi-Confluence Grid ──────────────────────────────────────
    st.markdown("#### 🏛️ Multi-Pillar Institutional Confluence Breakdown")

    col_smc, col_chain, col_cpr, col_ai = st.columns(4)

    with col_smc:
        smc_content = "".join(f"<div style='margin-bottom:6px;'><b>{imp['icon']} {imp['headline']}</b><div style='font-size:12px;color:#8FA6B8;'>{imp['detail']}</div></div>" for imp in sim["smc_impacts"])
        _render_html(
            f"""
            <div style="background:#121D2B;border-top:3px solid #00BCD4;padding:10px 12px;border-radius:4px;min-height:220px;">
                <div style="font-weight:700;color:#00BCD4;font-size:13px;margin-bottom:8px;">
                    🏛️ SMC STRUCTURE & LIQUIDITY
                </div>
                {smc_content}
            </div>
            """
        )

    with col_chain:
        chain_content = "".join(f"<div style='margin-bottom:6px;'><b>{imp['icon']} {imp['headline']}</b><div style='font-size:12px;color:#8FA6B8;'>{imp['detail']}</div></div>" for imp in sim["chain_impacts"])
        _render_html(
            f"""
            <div style="background:#121D2B;border-top:3px solid #FF9800;padding:10px 12px;border-radius:4px;min-height:220px;">
                <div style="font-weight:700;color:#FF9800;font-size:13px;margin-bottom:8px;">
                    📊 OPTION CHAIN SQUEEZE
                </div>
                {chain_content}
                <div style="font-size:11px;color:#8FA6B8;margin-top:6px;">Walls: S1 {data['chain']['s1']:,.0f} | R1 {data['chain']['r1']:,.0f} · Max Pain: {data['chain']['max_pain']:,.0f}</div>
            </div>
            """
        )

    with col_cpr:
        c_conf = sim["cpr_confluence"]
        _render_html(
            f"""
            <div style="background:#121D2B;border-top:3px solid #2FBF71;padding:10px 12px;border-radius:4px;min-height:220px;">
                <div style="font-weight:700;color:#2FBF71;font-size:13px;margin-bottom:8px;">
                    📐 CPR & PRICE LEVELS
                </div>
                <div style="margin-bottom:6px;"><b>{c_conf['headline']}</b><div style='font-size:12px;color:#8FA6B8;'>{c_conf['detail']}</div></div>
                <div style="font-size:11px;color:#8FA6B8;margin-top:6px;">CPR Top: {data['cpr']['top']:,.1f} | Pivot: {data['cpr']['pivot']:,.1f} | Bottom: {data['cpr']['bottom']:,.1f}</div>
            </div>
            """
        )

    with col_ai:
        ai_env = sim["ai_envelope"]
        _render_html(
            f"""
            <div style="background:#121D2B;border-top:3px solid #AB47BC;padding:10px 12px;border-radius:4px;min-height:220px;">
                <div style="font-weight:700;color:#AB47BC;font-size:13px;margin-bottom:8px;">
                    🤖 AI QUANTILE ENVELOPE
                </div>
                <div style="margin-bottom:6px;"><b>{ai_env['headline']}</b><div style='font-size:12px;color:#8FA6B8;'>{ai_env['detail']}</div></div>
                <div style="font-size:11px;color:#8FA6B8;margin-top:6px;">Quantile Floor (q10): {ai_env['q10']:,.1f} | Cap (q90): {ai_env['q90']:,.1f}</div>
            </div>
            """
        )

    st.divider()

    # ── Actionable Opening Playbook (CE vs PE Decision Tree) ───────────────
    st.markdown(f"### 🎯 {n_day} Morning Master Execution Rulebook (09:15 – 09:45 AM)")
    st.caption("Never buy options blindly at 09:15:05 AM! Watch how the 1st 15-minute candle interacts with the gap boundaries:")

    pb_fade = sim["playbook"]["fade"]
    pb_breakout = sim["playbook"]["breakout"]

    c_pb1, c_pb2 = st.columns(2)

    with c_pb1:
        _render_html(
            f"""
            <div style="background:#102219;border-left:5px solid #0F9D58;padding:14px 16px;border-radius:6px;min-height:240px;">
                <div style="font-size:15px;font-weight:800;color:#0F9D58;margin-bottom:6px;">
                    {pb_fade['title']}
                </div>
                <div style="color:#FFF;font-size:13px;margin-bottom:8px;">
                    <b>Contract to Watch:</b> <span style="background:#1B3D2B;color:#2FBF71;padding:2px 7px;border-radius:3px;font-weight:700;">{pb_fade['contract']}</span>
                </div>
                <div style="color:#D5DFEA;font-size:13px;line-height:1.45;margin-bottom:8px;">
                    <b>Trigger Condition:</b> {pb_fade['trigger']}
                </div>
                <div style="color:#8FA6B8;font-size:12.5px;border-top:1px solid #1C3D28;padding-top:6px;">
                    • <b>Invalidation Stop:</b> <span style="color:#FFF;">{pb_fade['stop_loss']}</span><br>
                    • <b>Target 1:</b> <span style="color:#2FBF71;">{pb_fade['target_1']}</span><br>
                    • <b>Target 2 (Runner):</b> <span style="color:#00E676;">{pb_fade['target_2']}</span>
                </div>
            </div>
            """
        )

    with c_pb2:
        _render_html(
            f"""
            <div style="background:#261214;border-left:5px solid #E8503A;padding:14px 16px;border-radius:6px;min-height:240px;">
                <div style="font-size:15px;font-weight:800;color:#E8503A;margin-bottom:6px;">
                    {pb_breakout['title']}
                </div>
                <div style="color:#FFF;font-size:13px;margin-bottom:8px;">
                    <b>Contract to Watch:</b> <span style="background:#421B1F;color:#FF5252;padding:2px 7px;border-radius:3px;font-weight:700;">{pb_breakout['contract']}</span>
                </div>
                <div style="color:#D5DFEA;font-size:13px;line-height:1.45;margin-bottom:8px;">
                    <b>Trigger Condition:</b> {pb_breakout['trigger']}
                </div>
                <div style="color:#8FA6B8;font-size:12.5px;border-top:1px solid #441E22;padding-top:6px;">
                    • <b>Invalidation Stop:</b> <span style="color:#FFF;">{pb_breakout['stop_loss']}</span><br>
                    • <b>Target 1:</b> <span style="color:#FF8A80;">{pb_breakout['target_1']}</span><br>
                    • <b>Target 2 (Runner):</b> <span style="color:#FF5252;">{pb_breakout['target_2']}</span>
                </div>
            </div>
            """
        )

    # ── Institutional Prop-Desk Discipline Note ─────────────────────────────
    st.info(
        "🧠 **The Golden 15-Minute Rule (Institutional Prop Desk Mandate):**\n\n"
        "1. **09:15 to 09:20 AM** is algorithmic order matching and stop-hunting. **NEVER execute market orders here.**\n"
        "2. Let the **09:15–09:30 AM 15-minute candle close**. If it leaves a long wick piercing the high/low and closes back inside, the **Judas Swing Trap** is confirmed -> Fade the gap!\n"
        "3. If the 15m candle closes with a full, solid body beyond S1/R1 and Option Chain confirms heavy unwinding, the **Gap & Go Expansion** is confirmed -> Buy on the pullback to VWAP!"
    )

    st.divider()

    # ── Manish & Saransh Option Seller Trap & Gap-Down Execution ────────────
    try:
        import gap_trap_strategy as gts
        gts.render_gap_trap_dashboard(
            spot=spot,
            open_price=sim_price_input if "sim_price_input" in locals() else spot,
            df_chain=df_chain,
            df_5m=pa_frames.get("5m") if (pa_frames and "5m" in pa_frames) else None,
            current_time=now if "now" in locals() else None,
        )
    except Exception as _gts_err:
        st.caption(f"Option Seller Trap module unavailable: {_gts_err}")

    st.divider()

    # ── Pre-Computed 5-Scenario Comparison Table ────────────────────────────
    st.markdown("#### 📋 Matrix Comparison Across All 5 Standard Scenarios")
    st.caption(f"Review how our multi-confluence system handles every potential opening situation for {n_day}:")

    comp_rows = []
    for sc in data["standard_scenarios"]:
        comp_rows.append({
            "Scenario Preset": sc["label"],
            "Projected Open": f"{sc['open_price']:,.1f}",
            "Gap Points": f"{sc['gap_pts']:+.1f} pts ({sc['gap_pct']:+.2f}%)",
            "SMC Structural Event": sc["smc_impacts"][0]["headline"] if sc["smc_impacts"] else "Inside Range",
            "Option Wall Threat": sc["chain_impacts"][0]["headline"] if sc["chain_impacts"] else "Balanced",
            "AI Quantile Confluence": sc["ai_envelope"]["status"].replace("_", " "),
            "Best Setup": sc["primary_verdict"],
            "Watch Contract": sc["playbook"]["fade"]["contract"] if "FADE" in sc["primary_verdict"] else sc["playbook"]["breakout"]["contract"],
        })

    st.dataframe(pd.DataFrame(comp_rows), use_container_width=True, hide_index=True)
