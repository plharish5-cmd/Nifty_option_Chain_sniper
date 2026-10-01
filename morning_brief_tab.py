"""
morning_brief_tab.py — "🌅 MORNING BRIEF" Consolidated Dashboard Tab for NIFTY.

Shows ONE consolidated view of everything needed to make the day's trading decision:
  - Row 1: Header Bar (NIFTY Spot, Time, Expiry, Bullish/Bearish/Neutral color)
  - Row 2: Three Columns:
      Column A: SMART MONEY (PRO, Retail, Spread, Day bias from participant_history.py)
      Column B: TODAY'S CONDITIONS (GEX, IV, Floor, Ceiling from agt_briefing.py)
      Column C: TRADE GATE (5-box checklist, Score, Verdict from compute_pretrade_checklist)
  - Row 3: Action Line (Big, colored, unmissable callout banner)
  - Row 4: Option Chain Snapshot (ATM ± 2 strikes only, 5 strikes total)
  - Row 5: Data Health (Last snapshot timestamp, age, session coverage)
  - Auto-refreshes every 60 seconds.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import streamlit as st

import agt_briefing
import config
import oi_timeline
import participant_history as ph
import rvol_gate
import store

logger = logging.getLogger("morning_brief_tab")


def _get_active_session_data(ctx: Optional[Dict[str, Any]] = None) -> Tuple[date, datetime, float, pd.DataFrame, List[Tuple[datetime, float, pd.DataFrame]], dict]:
    """
    Resolves target_date, latest snapshot (ts, spot, df), all snaps, and rules.
    Prefers context passed from dashboard, falling back to local store.
    """
    rules = agt_briefing.load_briefing_rules()
    now_dt = datetime.now()
    target_date = now_dt.date()

    # Load session snapshots from store
    try:
        all_snaps = store.load_day(target_date)
    except Exception as e:
        logger.warning(f"Error loading session snapshots for {target_date}: {e}")
        all_snaps = []

    if not all_snaps:
        # Fallback to the latest available day folder in snapshots
        snap_dirs = sorted([d for d in config.SNAPSHOT_DIR.glob("*") if d.is_dir()])
        if snap_dirs:
            try:
                target_date = datetime.strptime(snap_dirs[-1].name, "%Y-%m-%d").date()
                all_snaps = store.load_day(target_date)
            except Exception as e:
                logger.debug(f"Error loading fallback snapshot dir: {e}")

    if all_snaps:
        latest_ts, latest_spot, latest_df = all_snaps[-1]
    else:
        latest_ts = now_dt
        latest_spot = 23350.0
        latest_df = pd.DataFrame()

    # Allow dashboard context overrides if provided
    if isinstance(ctx, dict):
        if "spot" in ctx and ctx["spot"] is not None and ctx["spot"] > 0:
            latest_spot = float(ctx["spot"])
        if "chain_df" in ctx and isinstance(ctx["chain_df"], pd.DataFrame) and not ctx["chain_df"].empty:
            latest_df = ctx["chain_df"]
        elif "df" in ctx and isinstance(ctx["df"], pd.DataFrame) and not ctx["df"].empty:
            latest_df = ctx["df"]
        if "now" in ctx and isinstance(ctx["now"], datetime):
            latest_ts = ctx["now"]
            target_date = latest_ts.date()

    return target_date, latest_ts, latest_spot, latest_df, all_snaps, rules


def _ordinal(n: int) -> str:
    """Format integer with proper English ordinal suffix: 1st, 2nd, 3rd, 4th, 22nd, etc."""
    n_abs = abs(int(n))
    if 11 <= (n_abs % 100) <= 13:
        return f"{n}th"
    suffix = {1: "st", 2: "nd", 3: "rd"}.get(n_abs % 10, "th")
    return f"{n}{suffix}"


def _compute_smart_money(target_date: date, rules: dict) -> Dict[str, Any]:
    """Extracts institutional context metrics from participant_history.py."""
    res = {
        "pro_str": "N/A",
        "fii_fut_str": "N/A",
        "fii_hedge_str": "N/A",
        "retail_str": "N/A",
        "spread_str": "N/A",
        "day_bias": "NEUTRAL",
        "bias_color": "#9ca3af",
        "post_expiry": False,
    }
    try:
        tidy_df = ph.load()
        if tidy_df is None or tidy_df.empty:
            return res

        tidy_df = tidy_df.copy()
        tidy_df["date"] = pd.to_datetime(tidy_df["date"]).dt.date
        df_prev = tidy_df[tidy_df["date"] < target_date]
        if df_prev.empty:
            df_prev = tidy_df[tidy_df["date"] <= target_date]
        if df_prev.empty:
            return res

        prev_date = df_prev["date"].max()
        window = int(rules.get("window_sessions", 65))

        # Option OI resets at expiry settlement, so on the session AFTER an
        # expiry (normally Wednesday) every option percentile describes
        # settlement, not positioning. Futures columns stay valid.
        try:
            from agt_briefing import was_prev_session_expiry
            post_expiry = was_prev_session_expiry(prev_date, rules)
        except Exception as err:  # noqa: BLE001
            logger.warning(f"post-expiry check unavailable: {err}")
            post_expiry = False
        res["post_expiry"] = post_expiry
        pcts_df = ph.compute_percentiles(tidy_df, window=window, as_of_date=prev_date, rules=rules)
        div_dict = ph.compute_divergence(df_prev, window=window, rules=rules)

        if pcts_df.empty:
            return res

        inst_cfg = rules.get("institutional_context", {})
        pro_bullish_thresh = float(inst_cfg.get("pro_bias", {}).get("bullish_pct", 70.0))
        pro_bearish_thresh = float(inst_cfg.get("pro_bias", {}).get("bearish_pct", 30.0))
        low_conv = float(inst_cfg.get("percentile_conviction", {}).get("low", 20.0))
        high_conv = float(inst_cfg.get("percentile_conviction", {}).get("high", 80.0))

        # 1. PRO fut_long
        pro_row = pcts_df[(pcts_df["participant"] == "PRO") & (pcts_df["series"] == "fut_long")]
        pro_fut_pct = float(pro_row["percentile_rank"].iloc[0]) if not pro_row.empty else 50.0

        if pro_fut_pct > pro_bullish_thresh:
            pro_bias = "BULLISH"
            res["pro_str"] = f"BULLISH ▲ ({_ordinal(int(pro_fut_pct))} pct)"
        elif pro_fut_pct < pro_bearish_thresh:
            pro_bias = "BEARISH"
            res["pro_str"] = f"BEARISH ▼ ({_ordinal(int(pro_fut_pct))} pct)"
        else:
            pro_bias = "NEUTRAL"
            res["pro_str"] = f"NEUTRAL → ({_ordinal(int(pro_fut_pct))} pct)"

        # 2. FII Futures (net_fut)
        fii_fut_row = pcts_df[(pcts_df["participant"] == "FII") & (pcts_df["series"] == "net_fut")]
        if not fii_fut_row.empty:
            fii_fut_pct = float(fii_fut_row["percentile_rank"].iloc[0])
            if fii_fut_pct <= low_conv:
                res["fii_fut_str"] = f"NET SHORT extreme ({_ordinal(int(fii_fut_pct))} pct)"
            elif fii_fut_pct <= 35.0:
                res["fii_fut_str"] = f"NET SHORT ({_ordinal(int(fii_fut_pct))} pct)"
            elif fii_fut_pct >= high_conv:
                res["fii_fut_str"] = f"NET LONG extreme ({_ordinal(int(fii_fut_pct))} pct)"
            elif fii_fut_pct >= 65.0:
                res["fii_fut_str"] = f"NET LONG ({_ordinal(int(fii_fut_pct))} pct)"
            else:
                res["fii_fut_str"] = f"BALANCED ({_ordinal(int(fii_fut_pct))} pct)"
        else:
            res["fii_fut_str"] = "n/a"

        # 3. FII Hedge (PE long / Net PE)
        fii_net_pe_row = pcts_df[(pcts_df["participant"] == "FII") & (pcts_df["series"] == "net_pe")]
        fii_pe_long_row = pcts_df[(pcts_df["participant"] == "FII") & (pcts_df["series"] == "pe_long")]
        fii_net_pe_pct = float(fii_net_pe_row["percentile_rank"].iloc[0]) if not fii_net_pe_row.empty else 50.0
        fii_pe_long_pct = float(fii_pe_long_row["percentile_rank"].iloc[0]) if not fii_pe_long_row.empty else 50.0
        fii_pe_pct = max(fii_net_pe_pct, fii_pe_long_pct)

        if post_expiry:
            res["fii_hedge_str"] = "n/a — post-expiry (OI reset)"
        elif fii_pe_pct >= high_conv:
            res["fii_hedge_str"] = f"HEAVY (PE long {_ordinal(int(fii_pe_pct))} pct — normal posture)"
        elif fii_pe_pct <= low_conv:
            res["fii_hedge_str"] = f"LIGHT (PE long {_ordinal(int(fii_pe_pct))} pct — unusual)"
        else:
            res["fii_hedge_str"] = f"NORMAL (PE long {_ordinal(int(fii_pe_pct))} pct)"

        # 4. Retail net_ce / net_pe
        ret_net_ce_row = pcts_df[(pcts_df["participant"] == "Retailers") & (pcts_df["series"] == "net_ce")]
        ret_net_pe_row = pcts_df[(pcts_df["participant"] == "Retailers") & (pcts_df["series"] == "net_pe")]
        ret_net_ce_pct = float(ret_net_ce_row["percentile_rank"].iloc[0]) if not ret_net_ce_row.empty else 50.0
        ret_net_pe_pct = float(ret_net_pe_row["percentile_rank"].iloc[0]) if not ret_net_pe_row.empty else 50.0

        ret_extreme = float(inst_cfg.get("retail_trap", {}).get("net_ce_extreme_pct", 90.0))
        ret_pe_extreme = float(inst_cfg.get("retail_trap", {}).get("net_pe_extreme_pct", 90.0))

        retail_trap_side = None
        if post_expiry:
            res["retail_str"] = "n/a — post-expiry (OI reset)"
        elif ret_net_ce_pct > ret_extreme:
            res["retail_str"] = "WRONG SIDE (calls extreme)"
            retail_trap_side = "CE"
        elif ret_net_pe_pct > ret_pe_extreme:
            res["retail_str"] = "WRONG SIDE (puts extreme)"
            retail_trap_side = "PE"
        elif ret_net_ce_pct > high_conv:
            res["retail_str"] = f"CALL HEAVY ({_ordinal(int(ret_net_ce_pct))} pct)"
        elif ret_net_pe_pct > high_conv:
            res["retail_str"] = f"PUT HEAVY ({_ordinal(int(ret_net_pe_pct))} pct)"
        else:
            res["retail_str"] = "BALANCED"

        # 5. Spread
        spreads = div_dict.get("spreads", {})
        pe_spread_pct = float(spreads.get("pe_spread", {}).get("percentile_rank", 50.0) or 50.0)
        ce_spread_pct = float(spreads.get("ce_spread", {}).get("percentile_rank", 50.0) or 50.0)
        fut_spread_pct = float(spreads.get("fut_spread", {}).get("percentile_rank", 50.0) or 50.0)

        def _two_sided(v: float) -> float:
            return max(float(v), 100.0 - float(v))

        spread_cfg = inst_cfg.get("spread", {})
        spread_extreme_thresh = float(spread_cfg.get("extreme_pct", 90.0))
        spread_high_thresh = float(spread_cfg.get("high_pct", 70.0))

        if post_expiry:
            raw_spread_pct = fut_spread_pct
            spread_suffix = " [futures only]"
        else:
            raw_spread_pct = max((pe_spread_pct, ce_spread_pct, fut_spread_pct), key=_two_sided)
            spread_suffix = ""
        spread_pct = _two_sided(raw_spread_pct)

        if spread_pct >= spread_extreme_thresh:
            res["spread_str"] = f"EXTREME ({_ordinal(int(raw_spread_pct))} pct){spread_suffix} — big move coming"
        elif spread_pct >= spread_high_thresh:
            res["spread_str"] = f"HIGH ({_ordinal(int(raw_spread_pct))} pct){spread_suffix} — directional pressure building"
        else:
            res["spread_str"] = f"NORMAL ({_ordinal(int(raw_spread_pct))} pct){spread_suffix}"

        # 6. Day bias calculation (Retail Trap vs Directional Bias)
        if retail_trap_side == "CE" and pro_bias == "BEARISH":
            res["day_bias"] = "TRAP SETUP (Retail Squeeze)"
            res["bias_color"] = "#ef4444"
        elif retail_trap_side == "PE" and pro_bias == "BULLISH":
            res["day_bias"] = "TRAP SETUP (Retail Short Squeeze)"
            res["bias_color"] = "#10b981"
        elif pro_bias == "BEARISH":
            res["day_bias"] = "BEARISH (PE Bias)"
            res["bias_color"] = "#ef4444"
        elif pro_bias == "BULLISH":
            res["day_bias"] = "BULLISH (CE Bias)"
            res["bias_color"] = "#10b981"
        else:
            res["day_bias"] = "NEUTRAL"
            res["bias_color"] = "#9ca3af"

    except Exception as e:
        logger.warning(f"Error computing smart money metrics: {e}")

    return res


def _compute_conditions(
    latest_df: pd.DataFrame,
    latest_spot: float,
    latest_ts: datetime,
    target_date: date,
    rules: dict,
) -> Tuple[Dict[str, Any], Optional[pd.Series], Optional[pd.Series], float]:
    """Computes GEX, IV, and Key Wall Levels (S1/R1)."""
    mult = 65.0 / 100000.0  # display units in Lakhs

    # 1. GEX
    gex_data = agt_briefing.compute_gex_metrics(latest_df, latest_spot, rules=rules)
    gex_regime = gex_data.get("regime", "NEUTRAL")
    if gex_regime == "SHORT":
        gex_str = "SHORT → trending day"
    elif gex_regime == "LONG":
        gex_str = "LONG → walls hold, rangebound"
    else:
        gex_str = "NEUTRAL → mixed signals"

    # 2. IV Context
    iv_data = agt_briefing.compute_iv_context(latest_df, latest_spot, latest_ts, target_date, rules=rules)
    iv_label = iv_data.get("label") or "NORMAL"
    iv_pct = iv_data.get("percentile")
    pct_txt = f"{int(iv_pct)}%" if iv_pct is not None else "N/A"
    if iv_label == "CHEAP":
        iv_str = f"CHEAP ({pct_txt}) — buy now"
    elif iv_label == "EXPENSIVE":
        iv_str = f"EXPENSIVE ({pct_txt}) — avoid buying"
    else:
        iv_str = f"NORMAL ({pct_txt})"

    # 3. Floor (S1) and Ceiling (R1) capped at spot ± 500 points (reusing agt_briefing)
    s1_row, r1_row = agt_briefing.compute_institutional_walls(latest_df, latest_spot, cap_pts=500.0)
    floor_str = "N/A"
    ceiling_str = "N/A"

    if s1_row is not None:
        s1_strike = float(s1_row["strike"])
        s1_oi_l = float(s1_row["pe_oi"]) * mult
        status_icon = "✅ holding" if latest_spot >= s1_strike else "⚠️ breaking"
        floor_str = f"{int(s1_strike):,} PE [{s1_oi_l:.0f}L] {status_icon}"

    if r1_row is not None:
        r1_strike = float(r1_row["strike"])
        r1_oi_l = float(r1_row["ce_oi"]) * mult
        pts_above = int(round(r1_strike - latest_spot))
        ceiling_str = f"{int(r1_strike):,} CE [{r1_oi_l:.0f}L] {pts_above}pts above"


    cond_res = {
        "gex_str": gex_str,
        "gex_data": gex_data,
        "iv_str": iv_str,
        "iv_data": iv_data,
        "floor_str": floor_str,
        "ceiling_str": ceiling_str,
    }
    return cond_res, s1_row, r1_row, mult


def render(ctx: Optional[Dict[str, Any]] = None) -> None:
    """
    Renders the complete '🌅 MORNING BRIEF' Consolidated Dashboard Tab.
    Auto-refreshes every 60 seconds without disrupting state.
    """
    # Auto-refresh every 60 seconds (Rule 2)
    try:
        from streamlit_autorefresh import st_autorefresh
        st_autorefresh(interval=60 * 1000, key="morning_brief_autorefresh")
    except Exception as e:
        logger.debug(f"st_autorefresh unavailable: {e}")

    target_date, latest_ts, latest_spot, latest_df, all_snaps, rules = _get_active_session_data(ctx)

    if latest_df.empty:
        st.info("🌅 MORNING BRIEF: Market closed or no snapshot data available yet.")
        return

    # 1. Resolve Expiry and DTE
    exp_str = getattr(latest_df, "attrs", {}).get("expiry") if hasattr(latest_df, "attrs") else None
    if not exp_str and "expiry" in latest_df.columns:
        val_exp = latest_df["expiry"].dropna()
        if not val_exp.empty:
            exp_str = str(val_exp.iloc[0])

    expiry_display = "N/A"
    if exp_str:
        for fmt in ("%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d"):
            try:
                exp_dt = datetime.strptime(str(exp_str).strip(), fmt)
                exp_d = exp_dt.date()
                today_d = date.today()
                # Before the first poll of the day the newest snapshot is still
                # yesterday's, so a stale expiry would show "(0d)" on a
                # non-expiry day. Roll forward in weekly steps until the expiry
                # is not in the past relative to the real calendar day.
                if exp_d < today_d:
                    exp_weekday = int(rules.get("expiry_mode", {}).get("expiry_weekday", exp_d.weekday()))
                    while exp_d < today_d:
                        exp_d += timedelta(days=7)
                    if exp_d.weekday() != exp_weekday:
                        exp_d += timedelta(days=(exp_weekday - exp_d.weekday()) % 7)
                    expiry_display = f"{exp_d.strftime('%a %d-%b')} ({(exp_d - today_d).days}d, est.)"
                else:
                    cal_dte = max(0, (exp_d - latest_ts.date()).days)
                    expiry_display = f"{exp_d.strftime('%a %d-%b')} ({cal_dte}d)"
                break
            except Exception:
                pass

    # 2. Extract Data
    smart_money = _compute_smart_money(target_date, rules)
    cond, s1_row, r1_row, mult = _compute_conditions(latest_df, latest_spot, latest_ts, target_date, rules)

    # 15-minute OI Flow for Checklist
    d_pe_15m = 0.0
    d_ce_15m = 0.0
    if len(all_snaps) >= 2:
        try:
            n_t = all_snaps[-1][0]
            snaps_15m = [s for s in all_snaps if 0 <= (n_t - s[0]).total_seconds() <= 900]
            if len(snaps_15m) >= 2:
                base_snap = snaps_15m[0][2]
                curr_snap = snaps_15m[-1][2]
                d_pe_15m = float(curr_snap.get("pe_chg_oi", curr_snap.get("pe_oi", 0)).sum() - base_snap.get("pe_chg_oi", base_snap.get("pe_oi", 0)).sum())
                d_ce_15m = float(curr_snap.get("ce_chg_oi", curr_snap.get("ce_oi", 0)).sum() - base_snap.get("ce_chg_oi", base_snap.get("ce_oi", 0)).sum())
        except Exception as e:
            logger.debug(f"Error computing 15m delta OI: {e}")

    chk_data = agt_briefing.compute_pretrade_checklist(
        s1_row=s1_row,
        r1_row=r1_row,
        d_pe_15m=d_pe_15m,
        d_ce_15m=d_ce_15m,
        iv_percentile=cond["iv_data"].get("percentile"),
        gex_data=cond["gex_data"],
        latest_ts=latest_ts,
        target_date=target_date,
        target_time=None,
        rules=rules,
        has_history=len(all_snaps) >= 2,
    )

    score = chk_data.get("score", 3)
    verdict = chk_data.get("verdict", "WAIT ❌")
    boxes = chk_data.get("boxes", [])

    # Action line decision
    day_bias = smart_money["day_bias"]
    now_t = latest_ts.time()
    eod_no_trade_str = rules.get("eod_no_trade_from", rules.get("eod_from", "15:00"))
    try:
        eod_t = datetime.strptime(eod_no_trade_str, "%H:%M").time()
        is_eod = (now_t >= eod_t)
    except Exception:
        is_eod = (now_t >= dtime(15, 0))

    rvol_display = rvol_gate.get_rvol_display(target_date, now_t)
    gate_decision = rvol_gate.get_gate_decision(target_date, now_t)

    is_ce_bias = day_bias in ("BULLISH (CE Bias)", "TRAP SETUP (Retail Short Squeeze)", "CE BUY")
    is_pe_bias = day_bias in ("BEARISH (PE Bias)", "TRAP SETUP (Retail Squeeze)", "PE BUY")

    if is_eod:
        action_text = "➡️ EOD — no new trades, close open positions"
        action_style = "background: #78350f; border: 2px solid #f59e0b; color: #ffedd5;"
        header_color = "#f59e0b"
        header_bg = "linear-gradient(90deg, #451a03 0%, #78350f 100%)"
    elif gate_decision.get("status") == "SUPPRESSED" and (is_ce_bias or is_pe_bias or score >= 5):
        action_text = "⚠️ CHOP ZONE — 77% breakouts fail. Wait for RVOL > 0.85 before any entry."
        action_style = "background: #78350f; border: 2px solid #f59e0b; color: #fef3c7;"
        header_color = "#f59e0b"
        header_bg = "linear-gradient(90deg, #451a03 0%, #78350f 100%)"
    elif score >= 5 and is_ce_bias:
        extra_note = " | 🚀 TREND DAY — hold to full target." if rvol_display.get("trend_day") else ""
        action_text = f"✅ CE BUY SETUP — confirm 8 EMA on chart{extra_note}"
        action_style = "background: #064e3b; border: 2px solid #10b981; color: #ecfdf5;"
        header_color = "#10b981"
        header_bg = "linear-gradient(90deg, #064e3b 0%, #047857 100%)"
    elif score >= 5 and is_pe_bias:
        extra_note = " | 🚀 TREND DAY — hold to full target." if rvol_display.get("trend_day") else ""
        action_text = f"✅ PE BUY SETUP — confirm 8 EMA on chart{extra_note}"
        action_style = "background: #7f1d1d; border: 2px solid #ef4444; color: #fef2f2;"
        header_color = "#ef4444"
        header_bg = "linear-gradient(90deg, #7f1d1d 0%, #b91c1c 100%)"
    elif score == 5 or "CAUTION" in verdict:
        action_text = "⚠️ CAUTION — one condition weak"
        action_style = "background: #78350f; border: 2px solid #f59e0b; color: #fef3c7;"
        header_color = "#f59e0b"
        header_bg = "linear-gradient(90deg, #451a03 0%, #78350f 100%)"
    elif score >= 5:
        action_text = "✅ TRADE GATE PASS — confirm direction on chart"
        action_style = "background: #064e3b; border: 2px solid #10b981; color: #ecfdf5;"
        header_color = "#10b981"
        header_bg = "linear-gradient(90deg, #064e3b 0%, #047857 100%)"
    else:
        action_text = "❌ NO TRADE — wait for clear direction"
        action_style = "background: #7f1d1d; border: 2px solid #ef4444; color: #fef2f2;"
        header_color = "#9ca3af"
        header_bg = "linear-gradient(90deg, #111827 0%, #1f2937 100%)"


    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # ROW 1 — HEADER BAR (Single Line)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    time_str = latest_ts.strftime("%H:%M")
    spot_str = f"{int(round(latest_spot)):,}"
    st.markdown(
        f"""
        <div style="background: {header_bg}; border: 1.5px solid {header_color}; padding: 14px 22px; border-radius: 8px; margin-bottom: 16px; display: flex; justify-content: space-between; align-items: center; box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.3);">
            <div style="font-size: 1.25rem; font-weight: 800; color: #FFFFFF; letter-spacing: 0.5px;">
                NIFTY {spot_str} &nbsp;&nbsp;|&nbsp;&nbsp; {time_str} &nbsp;&nbsp;|&nbsp;&nbsp; Expiry: {expiry_display}
            </div>
            <div style="font-size: 0.85rem; font-weight: 700; color: #FFFFFF; background: rgba(0, 0, 0, 0.4); padding: 4px 12px; border-radius: 6px; border: 1px solid rgba(255, 255, 255, 0.15);">
                🌅 MORNING BRIEF
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # ROW 2 — THREE COLUMNS (The only 3 things you need)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    colA, colB, colC = st.columns(3)

    expiry_block = (
        '<div style="color:#f59e0b; font-size:0.82rem; margin-top:6px;">⚠️ Prev session was EXPIRY — option OI reset at settlement. Futures readings only; option percentiles suppressed.</div>'
        if smart_money.get("post_expiry")
        else ""
    )

    with colA:
        st.markdown(
            (
                '<div style="background: #131722; border: 1px solid #2a2e39; border-radius: 8px; padding: 16px; min-height: 250px;">'
                '<div style="font-weight: 800; color: #60a5fa; font-size: 0.95rem; border-bottom: 1px solid #2a2e39; padding-bottom: 8px; margin-bottom: 12px; letter-spacing: 0.5px;">COLUMN A: SMART MONEY</div>'
                '<div style="font-size: 0.92rem; line-height: 1.9; color: #e5e7eb;">'
                f'<div><span style="color:#9ca3af;">PRO:</span> &nbsp;&nbsp;<b>{smart_money["pro_str"]}</b></div>'
                f'<div><span style="color:#9ca3af;">FII Futures:</span> &nbsp;&nbsp;<b>{smart_money["fii_fut_str"]}</b></div>'
                f'<div><span style="color:#9ca3af;">FII Hedge:</span> &nbsp;&nbsp;<b>{smart_money["fii_hedge_str"]}</b></div>'
                f'<div><span style="color:#9ca3af;">Retail:</span> &nbsp;&nbsp;<b>{smart_money["retail_str"]}</b></div>'
                f'<div><span style="color:#9ca3af;">Spread:</span> &nbsp;&nbsp;<b>{smart_money["spread_str"]}</b></div>'
                f'{expiry_block}'
                '<div style="margin-top: 10px; padding-top: 8px; border-top: 1px dashed #2a2e39;">'
                f'<span style="color:#9ca3af;">Day bias:</span> &nbsp;&nbsp;<span style="font-weight: 800; color: {smart_money["bias_color"]}; font-size: 1.05rem;">{smart_money["day_bias"]}</span>'
                '</div>'
                '</div>'
                '</div>'
            ),
            unsafe_allow_html=True,
        )

    with colB:
        st.markdown(
            (
                '<div style="background: #131722; border: 1px solid #2a2e39; border-radius: 8px; padding: 16px; min-height: 250px;">'
                '<div style="font-weight: 800; color: #f59e0b; font-size: 0.95rem; border-bottom: 1px solid #2a2e39; padding-bottom: 8px; margin-bottom: 12px; letter-spacing: 0.5px;">COLUMN B: TODAY\'S CONDITIONS</div>'
                '<div style="font-size: 0.92rem; line-height: 1.9; color: #e5e7eb;">'
                f'<div><span style="color:#9ca3af;">GEX:</span> &nbsp;&nbsp;<b>{cond["gex_str"]}</b></div>'
                f'<div><span style="color:#9ca3af;">RVOL:</span> &nbsp;&nbsp;<b>{rvol_display["label"]}</b></div>'
                f'<div><span style="color:#9ca3af;">IV:</span> &nbsp;&nbsp;<b>{cond["iv_str"]}</b></div>'
                f'<div><span style="color:#9ca3af;">Floor:</span> &nbsp;&nbsp;<b>{cond["floor_str"]}</b></div>'
                f'<div><span style="color:#9ca3af;">Ceiling:</span> &nbsp;&nbsp;<b>{cond["ceiling_str"]}</b></div>'
                '</div>'
                '</div>'
            ),
            unsafe_allow_html=True,
        )

    with colC:
        chk_lines = []
        for label, status, pass_bool, issue, _ in boxes:
            icon = "✅" if pass_bool else "✗"
            line_txt = f"{icon} {label.replace(':', '')}"
            if "HOLD" in status:
                line_txt = f"⏸ {label.replace(':', '')} (<09:30)"
            elif not pass_bool and issue and "freeze" in issue.lower():
                freeze_end_str = rules.get("chk_box5_freeze_end", "13:15")
                line_txt = f"⏸ Timing (freeze ends {freeze_end_str})"
            chk_lines.append(f"<div>{line_txt}</div>")
        chk_boxes_html = "".join(chk_lines)

        verdict_color = "#10b981" if score == 6 else ("#f59e0b" if score == 5 else "#ef4444")
        verdict_clean = verdict.replace("✅", "").replace("❌", "").replace("⚠️", "").strip()

        st.markdown(
            (
                '<div style="background: #131722; border: 1px solid #2a2e39; border-radius: 8px; padding: 16px; min-height: 250px;">'
                '<div style="font-weight: 800; color: #10b981; font-size: 0.95rem; border-bottom: 1px solid #2a2e39; padding-bottom: 8px; margin-bottom: 12px; letter-spacing: 0.5px;">COLUMN C: TRADE GATE</div>'
                '<div style="font-size: 0.92rem; line-height: 1.8; color: #e5e7eb;">'
                f'{chk_boxes_html}'
                '<div style="margin-top: 10px; padding-top: 8px; border-top: 1px dashed #2a2e39; font-weight: 800; font-size: 1rem;">'
                f'SCORE: {score}/6 — <span style="color: {verdict_color};">{verdict_clean}</span>'
                '</div>'
                '</div>'
                '</div>'
            ),
            unsafe_allow_html=True,
        )

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # ROW 3 — ACTION LINE (Big, Colored, Unmissable)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    st.markdown(
        f"""
        <div style="{action_style} border-radius: 8px; padding: 18px 24px; text-align: center; margin: 18px 0; box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.25);">
            <span style="font-size: 1.35rem; font-weight: 800; letter-spacing: 0.5px;">
                {action_text}
            </span>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # ROW 4 — OPTION CHAIN SNAPSHOT (5 strikes only)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    st.markdown(
        """
        <div style="font-weight: 700; color: #9ca3af; font-size: 0.88rem; margin-bottom: 6px; letter-spacing: 0.5px;">
            OPTION CHAIN SNAPSHOT (ATM ± 2 STRIKES)
        </div>
        """,
        unsafe_allow_html=True,
    )

    atm = float(round(latest_spot / 50.0) * 50.0)
    surrounding_strikes = [atm - 100.0, atm - 50.0, atm, atm + 50.0, atm + 100.0]

    chain_rows = []
    for s in surrounding_strikes:
        s_row = latest_df[latest_df["strike"] == s]
        if not s_row.empty:
            r0 = s_row.iloc[0]
            c_oi = float(r0.get("ce_oi", 0) or 0) * mult
            c_doi = float(r0.get("ce_chg_oi", 0) or 0)
            p_oi = float(r0.get("pe_oi", 0) or 0) * mult
            p_doi = float(r0.get("pe_chg_oi", 0) or 0)
            pcr_s = (p_oi / c_oi) if c_oi > 0 else 0.0
        else:
            c_oi, c_doi, p_oi, p_doi, pcr_s = 0.0, 0.0, 0.0, 0.0, 0.0

        is_atm = (s == atm)
        chain_rows.append({
            "Strike": f"{int(s):,}" + ("  ← ATM" if is_atm else ""),
            "CE OI": f"{c_oi:.1f}L",
            "CE ΔOI": f"{c_doi/1000:+.1f}k",
            "PCR": f"{pcr_s:.2f}",
            "PE OI": f"{p_oi:.1f}L",
            "PE ΔOI": f"{p_doi/1000:+.1f}k",
        })

    df_display = pd.DataFrame(chain_rows)
    st.dataframe(
        df_display,
        use_container_width=True,
        hide_index=True,
    )

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # ROW 5 — DATA HEALTH (One Line)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    cov = oi_timeline.compute_session_coverage(all_snaps, target_date, rules=rules)
    cov_str = f"{cov.occupied_bins}/{cov.total_bins} bins"

    now_sys = datetime.now()
    if target_date == now_sys.date():
        age_sec = max(0.0, (now_sys - latest_ts).total_seconds())
        if age_sec < 60:
            age_txt = f"{int(age_sec)}s ago"
        elif age_sec < 3600:
            age_txt = f"{int(age_sec // 60)}m {int(age_sec % 60)}s ago"
        else:
            age_txt = f"{int(age_sec // 3600)}h ago"
        is_stale = (age_sec > float(rules.get("stale_minutes", 10)) * 60.0)
    else:
        age_txt = "session complete"
        is_stale = False

    health_icon = "✅" if not is_stale else "⚠️"
    st.markdown(
        f"""
        <div style="font-size: 0.85rem; color: #9ca3af; padding: 6px 0; border-top: 1px solid #1f2937; margin-top: 8px;">
            Last snapshot: <b>{latest_ts.strftime('%H:%M:%S')}</b> ({age_txt}) {health_icon} &nbsp;&nbsp;|&nbsp;&nbsp; 
            Coverage: <b>{cov_str}</b> &nbsp;&nbsp;|&nbsp;&nbsp; 
            Auto-refresh: <b>60s</b>
        </div>
        """,
        unsafe_allow_html=True,
    )
