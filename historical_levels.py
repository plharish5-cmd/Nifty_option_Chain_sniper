"""
historical_levels.py — Historical Daily High/Low and 15m Longest Wicks Engine.

Captures every historical session's:
  - Day High & Day Low
  - 15-minute candle wicks at Day Low & Day High
  - Longest 15m Lower Wick (demand absorption) and Upper Wick (supply rejection)
  - Daily candle wicks
  - Saves a separate master table on PC: data/historical_key_levels.csv and .xlsx

Performs live VLOOKUP for the current day:
  - Matches current Spot, Today's High, and Today's Low against all past levels
  - Flags levels within 20 PTS as high-priority 100-pt reversal opportunities
    (e.g., 03-Sep High 24,025 matching 19-Aug Low 24,025.65 -> 100+ pt rejection;
           01-Sep Low 23,952.55 matching 28-Jul Low 23,954.60 -> 100+ pt bounce)
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from pathlib import Path

import pandas as pd

import config
import levels
import price_action
from ui_utils import render_html, _render_html

log = logging.getLogger(__name__)

CSV_PATH = Path("data/historical_key_levels.csv")
XLSX_PATH = Path("data/historical_key_levels.xlsx")


# ═══════════════════════════════════════════════════════════════════════════
# 1. Database Builder & Caching
# ═══════════════════════════════════════════════════════════════════════════

def build_historical_levels_database(force: bool = False) -> pd.DataFrame:
    """
    Build or load the historical key levels database with 15m wicks.
    Saves to data/historical_key_levels.csv and .xlsx on the user's PC.
    """
    candle_dir = getattr(config, "CANDLE_DIR", Path("data/candles"))
    ohlc_dir = getattr(config, "OHLC_DIR", Path("data/ohlc"))

    # Quick freshness check
    if not force and CSV_PATH.exists() and CSV_PATH.stat().st_size > 500:
        try:
            mtime_csv = CSV_PATH.stat().st_mtime
            candle_files = list(candle_dir.glob("20*.csv")) if candle_dir.exists() else []
            latest_candle_mtime = max((f.stat().st_mtime for f in candle_files), default=0)
            if latest_candle_mtime <= mtime_csv:
                cached_df = pd.read_csv(CSV_PATH)
                if not cached_df.empty and "Day_High" in cached_df.columns:
                    return cached_df
        except Exception as e:
            log.warning(f"Error checking cache: {e}")

    log.info("Building historical key levels database from daily OHLC and 15m candles...")

    # 1. Load multi-year daily OHLC
    try:
        daily_df = levels.load_ohlc(ohlc_dir)
        daily_df["Date_Str"] = pd.to_datetime(daily_df["Date"]).dt.strftime("%Y-%m-%d")
        daily_dict = {
            row["Date_Str"]: {
                "Open": float(row["Open"]),
                "High": float(row["High"]),
                "Low": float(row["Low"]),
                "Close": float(row["Close"]),
            }
            for _, row in daily_df.iterrows()
        }
    except Exception as e:
        log.error(f"Failed to load daily OHLC: {e}")
        daily_dict = {}

    # 2. Process all 1-minute candle files and resample to 15m
    records = []
    processed_dates = set()

    candle_files = sorted(candle_dir.glob("20*.csv")) if candle_dir.exists() else []
    for cf in candle_files:
        dt_str = cf.stem[:10]
        try:
            cdf = pd.read_csv(cf)
            if cdf.empty or "low" not in cdf or "close" not in cdf:
                continue

            cdf["timestamp"] = pd.to_datetime(cdf["timestamp"])
            c15 = price_action.resample(cdf, 15)
            if c15.empty:
                continue

            day_low = float(cdf["low"].min())
            day_high = float(cdf["high"].max())
            day_open = float(cdf["open"].iloc[0])
            day_close = float(cdf["close"].iloc[-1])

            # Calculate wicks on 15m candles
            # Lower wick = min(open, close) - low
            # Upper wick = high - max(open, close)
            c15["body_lo"] = c15[["open", "close"]].min(axis=1)
            c15["body_hi"] = c15[["open", "close"]].max(axis=1)
            c15["lower_wick"] = c15["body_lo"] - c15["low"]
            c15["upper_wick"] = c15["high"] - c15["body_hi"]
            c15["body"] = (c15["body_hi"] - c15["body_lo"]).abs()

            # 15m Candle at Day Low
            lo_matches = c15[c15["low"] == day_low]
            lo_c = lo_matches.iloc[0] if not lo_matches.empty else None

            # 15m Candle at Day High
            hi_matches = c15[c15["high"] == day_high]
            hi_c = hi_matches.iloc[0] if not hi_matches.empty else None

            # Max lower wick anywhere in the session
            idx_max_lw = c15["lower_wick"].idxmax()
            max_lw = c15.loc[idx_max_lw]

            # Max upper wick anywhere in the session
            idx_max_uw = c15["upper_wick"].idxmax()
            max_uw = c15.loc[idx_max_uw]

            # Daily candle bar wicks
            daily_body_lo = min(day_open, day_close)
            daily_body_hi = max(day_open, day_close)
            daily_lw = daily_body_lo - day_low
            daily_uw = day_high - daily_body_hi

            records.append({
                "Date": dt_str,
                "Day_High": round(day_high, 2),
                "Day_Low": round(day_low, 2),
                "Day_Open": round(day_open, 2),
                "Day_Close": round(day_close, 2),
                "Day_Range": round(day_high - day_low, 2),
                "Daily_Lower_Wick": round(daily_lw, 2),
                "Daily_Upper_Wick": round(daily_uw, 2),
                "Low_15m_Time": lo_c["timestamp"].strftime("%H:%M") if lo_c is not None else "",
                "Low_15m_Wick": round(float(lo_c["lower_wick"]), 2) if lo_c is not None else 0.0,
                "Low_15m_Bounce": round(float(lo_c["close"] - lo_c["low"]), 2) if lo_c is not None else 0.0,
                "High_15m_Time": hi_c["timestamp"].strftime("%H:%M") if hi_c is not None else "",
                "High_15m_Wick": round(float(hi_c["upper_wick"]), 2) if hi_c is not None else 0.0,
                "High_15m_Drop": round(float(hi_c["high"] - hi_c["close"]), 2) if hi_c is not None else 0.0,
                "Max_Lower_Wick_Pts": round(float(max_lw["lower_wick"]), 2),
                "Max_Lower_Wick_Low": round(float(max_lw["low"]), 2),
                "Max_Lower_Wick_Time": max_lw["timestamp"].strftime("%H:%M"),
                "Max_Upper_Wick_Pts": round(float(max_uw["upper_wick"]), 2),
                "Max_Upper_Wick_High": round(float(max_uw["high"]), 2),
                "Max_Upper_Wick_Time": max_uw["timestamp"].strftime("%H:%M"),
                "Has_15m_Data": True,
            })
            processed_dates.add(dt_str)
        except Exception as e:
            log.warning(f"Error processing candle file {cf.name}: {e}")

    # 3. Add historical dates from daily OHLC that don't have 1m candle files
    for dt_str, dbar in sorted(daily_dict.items()):
        if dt_str in processed_dates:
            continue
        try:
            d_o, d_h, d_l, d_c = dbar["Open"], dbar["High"], dbar["Low"], dbar["Close"]
            d_body_lo = min(d_o, d_c)
            d_body_hi = max(d_o, d_c)
            d_lw = d_body_lo - d_l
            d_uw = d_h - d_body_hi

            records.append({
                "Date": dt_str,
                "Day_High": round(d_h, 2),
                "Day_Low": round(d_l, 2),
                "Day_Open": round(d_o, 2),
                "Day_Close": round(d_c, 2),
                "Day_Range": round(d_h - d_l, 2),
                "Daily_Lower_Wick": round(d_lw, 2),
                "Daily_Upper_Wick": round(d_uw, 2),
                "Low_15m_Time": "",
                "Low_15m_Wick": round(d_lw, 2),
                "Low_15m_Bounce": round(d_c - d_l, 2),
                "High_15m_Time": "",
                "High_15m_Wick": round(d_uw, 2),
                "High_15m_Drop": round(d_h - d_c, 2),
                "Max_Lower_Wick_Pts": round(d_lw, 2),
                "Max_Lower_Wick_Low": round(d_l, 2),
                "Max_Lower_Wick_Time": "Daily",
                "Max_Upper_Wick_Pts": round(d_uw, 2),
                "Max_Upper_Wick_High": round(d_h, 2),
                "Max_Upper_Wick_Time": "Daily",
                "Has_15m_Data": False,
            })
        except Exception:
            continue

    if not records:
        return pd.DataFrame()

    out_df = pd.DataFrame(records).sort_values("Date").reset_index(drop=True)

    # Save to PC as CSV and XLSX
    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        out_df.to_csv(CSV_PATH, index=False)
        out_df.to_excel(XLSX_PATH, index=False)
        log.info(f"Saved {len(out_df)} historical sessions to {CSV_PATH} & {XLSX_PATH}")
    except Exception as e:
        log.warning(f"Failed writing CSV/XLSX: {e}")

    return out_df


# ═══════════════════════════════════════════════════════════════════════════
# 2. VLOOKUP Engine For Current Day & Live Spot
# ═══════════════════════════════════════════════════════════════════════════

def vlookup_levels(
    spot: float,
    today_high: float | None = None,
    today_low: float | None = None,
    current_date: str | date | None = None,
    tolerance_pts: float = 60.0,
    min_wick_pts: float = 18.0,
    hist_df: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """
    Perform a VLOOKUP of today's price action against all historical Day Highs,
    Day Lows, and 15m Long Wick zones.

    Identifies:
      - Historical Day Lows tested from below (Resistances / S->R Flips, like 03-Sep at 19-Aug Low)
      - Historical Day Lows tested from above (Supports, like 01-Sep at 28-Jul Low)
      - Historical Day Highs tested from below (Resistances)
      - Historical Day Highs tested from above (Supports / R->S Breakout Retests)
      - 15m Long Wicks defending key levels (Demand/Supply Rejections)
    """
    if hist_df is None or hist_df.empty:
        hist_df = build_historical_levels_database()

    if hist_df.empty:
        return pd.DataFrame()

    # Determine current session date string to exclude today from past reference
    if current_date is None:
        current_date_str = date.today().strftime("%Y-%m-%d")
    elif isinstance(current_date, (datetime, pd.Timestamp)):
        current_date_str = current_date.strftime("%Y-%m-%d")
    elif isinstance(current_date, date):
        current_date_str = current_date.strftime("%Y-%m-%d")
    else:
        current_date_str = str(current_date)[:10]

    # Exclude today so we compare against prior history
    prior_df = hist_df[hist_df["Date"] < current_date_str].copy()
    if prior_df.empty:
        return pd.DataFrame()

    rows = []
    seen_levels = set()

    for _, r in prior_df.iterrows():
        dt = str(r["Date"])
        d_high = float(r["Day_High"])
        d_low = float(r["Day_Low"])
        has_15m = bool(r.get("Has_15m_Data", False))

        candidates = []

        # 1. Day Low Level
        lw_desc = f"{r['Low_15m_Wick']:.1f}pt wick @ {r['Low_15m_Time']}" if has_15m and r["Low_15m_Wick"] > 0 else f"{r['Daily_Lower_Wick']:.1f}pt daily wick"
        candidates.append({
            "Level_Price": d_low,
            "Date": dt,
            "Type": "Day Low",
            "Wick_Pts": float(r["Low_15m_Wick"]) if has_15m else float(r["Daily_Lower_Wick"]),
            "Wick_Time": str(r["Low_15m_Time"]) if has_15m else "Daily",
            "Wick_Info": lw_desc,
        })

        # 2. Day High Level
        uw_desc = f"{r['High_15m_Wick']:.1f}pt wick @ {r['High_15m_Time']}" if has_15m and r["High_15m_Wick"] > 0 else f"{r['Daily_Upper_Wick']:.1f}pt daily wick"
        candidates.append({
            "Level_Price": d_high,
            "Date": dt,
            "Type": "Day High",
            "Wick_Pts": float(r["High_15m_Wick"]) if has_15m else float(r["Daily_Upper_Wick"]),
            "Wick_Time": str(r["High_15m_Time"]) if has_15m else "Daily",
            "Wick_Info": uw_desc,
        })

        # 3. 15m Long Lower Wick (if >= min_wick_pts and distinct from day low)
        max_lw_pts = float(r.get("Max_Lower_Wick_Pts", 0.0))
        max_lw_low = float(r.get("Max_Lower_Wick_Low", 0.0))
        if has_15m and max_lw_pts >= min_wick_pts and abs(max_lw_low - d_low) > 2.0:
            candidates.append({
                "Level_Price": max_lw_low,
                "Date": dt,
                "Type": "15m Demand Wick",
                "Wick_Pts": max_lw_pts,
                "Wick_Time": str(r["Max_Lower_Wick_Time"]),
                "Wick_Info": f"{max_lw_pts:.1f}pt buying wick @ {r['Max_Lower_Wick_Time']}",
            })

        # 4. 15m Long Upper Wick (if >= min_wick_pts and distinct from day high)
        max_uw_pts = float(r.get("Max_Upper_Wick_Pts", 0.0))
        max_uw_high = float(r.get("Max_Upper_Wick_High", 0.0))
        if has_15m and max_uw_pts >= min_wick_pts and abs(max_uw_high - d_high) > 2.0:
            candidates.append({
                "Level_Price": max_uw_high,
                "Date": dt,
                "Type": "15m Supply Wick",
                "Wick_Pts": max_uw_pts,
                "Wick_Time": str(r["Max_Upper_Wick_Time"]),
                "Wick_Info": f"{max_uw_pts:.1f}pt selling wick @ {r['Max_Upper_Wick_Time']}",
            })

        for cand in candidates:
            l_price = cand["Level_Price"]
            l_date = cand["Date"]
            l_type = cand["Type"]

            # Deduplicate near identical levels from the same date (< 1.5 pt apart)
            key = (l_date, round(l_price / 2.0) * 2)
            if key in seen_levels:
                continue

            dist_spot = spot - l_price
            abs_dist_spot = abs(dist_spot)

            dist_high = abs(today_high - l_price) if today_high is not None else abs_dist_spot
            dist_low = abs(today_low - l_price) if today_low is not None else abs_dist_spot

            min_dist = min(abs_dist_spot, dist_high, dist_low)

            if min_dist > tolerance_pts:
                continue

            seen_levels.add(key)

            # Polarity & Role
            if l_type == "Day Low":
                if spot >= l_price:
                    role = "Support (Prior Day Low)"
                    bias = "BULLISH BOUNCE"
                    action_setup = f"🎯 100-PT LONG SETUP: Support zone. Watch for lower wick bounce on 5m/15m. Target ~{l_price + 100:,.0f} (+100 pts)."
                else:
                    role = "Resistance (S->R Flip of Prior Low)"
                    bias = "BEARISH REJECTION"
                    action_setup = f"🎯 100-PT SHORT SETUP: Broken low acting as resistance. Watch for upper wick rejection. Target ~{l_price - 100:,.0f} (-100 pts)."
            elif l_type == "Day High":
                if spot <= l_price:
                    role = "Resistance (Prior Day High)"
                    bias = "BEARISH REJECTION"
                    action_setup = f"🎯 100-PT SHORT SETUP: Resistance zone. Watch for upper wick rejection on 5m/15m. Target ~{l_price - 100:,.0f} (-100 pts)."
                else:
                    role = "Support (R->S Breakout Retest)"
                    bias = "BULLISH RETEST"
                    action_setup = f"🎯 100-PT LONG SETUP: Broken high acting as support. Watch for lower wick test. Target ~{l_price + 100:,.0f} (+100 pts)."
            elif "Demand" in l_type:
                role = "Demand Zone (15m Long Lower Wick)"
                bias = "BULLISH DEFENSE"
                action_setup = f"🎯 100-PT LONG SETUP: Institutional demand wick defended here. Watch for bounce. Target ~{l_price + 100:,.0f} (+100 pts)."
            else:
                role = "Supply Zone (15m Long Upper Wick)"
                bias = "BEARISH DEFENSE"
                action_setup = f"🎯 100-PT SHORT SETUP: Institutional supply wick rejected here. Watch for reversal. Target ~{l_price - 100:,.0f} (-100 pts)."

            # Alert Category
            if min_dist <= 10.0:
                alert_status = "🚨 AT LEVEL (<=10 pts)"
                alert_badge = "CRITICAL_TOUCH"
            elif min_dist <= 20.0:
                alert_status = "⚡ NEAR LEVEL (10-20 pts)"
                alert_badge = "HOT_SETUP"
            elif min_dist <= 40.0:
                alert_status = "APPROACHING (20-40 pts)"
                alert_badge = "APPROACHING"
            else:
                alert_status = "WATCHLIST (40-60 pts)"
                alert_badge = "WATCHLIST"

            # Tested By
            if abs_dist_spot == min_dist:
                tested_by = f"Spot ({spot:,.1f})"
            elif dist_high == min_dist:
                tested_by = f"Today High ({today_high:,.1f})"
            else:
                tested_by = f"Today Low ({today_low:,.1f})"

            # Compute days ago and clean formatted date
            try:
                d_date = datetime.strptime(dt, "%Y-%m-%d").date()
                cur_date = datetime.strptime(current_date_str, "%Y-%m-%d").date()
                days_ago = max(0, (cur_date - d_date).days)
                date_display = f"{d_date:%d-%b-%Y}"
            except Exception:
                days_ago = 999
                date_display = dt

            rows.append({
                "Alert": alert_status,
                "Badge": alert_badge,
                "Level_Price": l_price,
                "Type": l_type,
                "Date_Formed": dt,
                "Date_Display": date_display,
                "Days_Ago": days_ago,
                "Role": role,
                "Bias": bias,
                "Wick_Info": cand["Wick_Info"],
                "Wick_Pts": cand["Wick_Pts"],
                "Dist_Spot": round(dist_spot, 1),
                "Min_Dist": round(min_dist, 1),
                "Tested_By": tested_by,
                "Action_Plan": action_setup,
                "_sort_key": min_dist + (0.015 * min(days_ago, 300)),
            })

    if not rows:
        return pd.DataFrame()

    res_df = pd.DataFrame(rows).sort_values("_sort_key").drop(columns="_sort_key").reset_index(drop=True)
    return res_df


def get_critical_alerts(vdf: pd.DataFrame, max_pts: float = 20.0) -> list[dict]:
    """Return all historical level alerts within 20 points."""
    if vdf is None or vdf.empty:
        return []
    hot = vdf[vdf["Min_Dist"] <= max_pts]
    return hot.to_dict(orient="records")


# ═══════════════════════════════════════════════════════════════════════════
# 3. Streamlit UI Components (Cockpit 1st Tab & Chart Levels Tab)
# ═══════════════════════════════════════════════════════════════════════════

def render_cockpit_card(
    spot: float,
    today_high: float | None = None,
    today_low: float | None = None,
    current_date: str | date | None = None,
    hist_df: pd.DataFrame | None = None,
) -> None:
    """
    Renders the Critical Historical Level Alert Card in Tab 0 (Decision Cockpit).
    Fires whenever live spot or today's high/low is within 20 PTS of a past
    Day High, Day Low, or 15m Long Wick level.
    """
    import streamlit as st

    vdf = vlookup_levels(
        spot=spot,
        today_high=today_high,
        today_low=today_low,
        current_date=current_date,
        tolerance_pts=50.0,
        hist_df=hist_df,
    )
    if vdf.empty:
        return

    alerts_20 = get_critical_alerts(vdf, max_pts=20.0)

    if alerts_20:
        top = alerts_20[0]
        tested_lvl = top["Level_Price"]
        dist = top["Min_Dist"]
        dt_disp = top.get("Date_Display", top["Date_Formed"])
        days_ago = top.get("Days_Ago", 0)
        dt = f"{dt_disp} ({days_ago}d ago)" if days_ago > 0 else dt_disp
        ltype = top["Type"]
        role = top["Role"]
        wick_info = top["Wick_Info"]
        plan = top["Action_Plan"]
        tested_by = top["Tested_By"]

        badge_color = "#E8503A" if "Resistance" in role or "Supply" in role else "#0F9D58"

        # Check for multi-day confluence around this exact level (within 4 pts)
        confluence_html = ""
        confluent = [
            a for a in vdf.to_dict(orient="records")
            if abs(a["Level_Price"] - tested_lvl) <= 4.0 and a["Date_Formed"] != top["Date_Formed"]
        ]
        if confluent:
            items = []
            for ca in confluent[:3]:
                c_dt = ca.get("Date_Display", ca["Date_Formed"])
                c_da = ca.get("Days_Ago", 0)
                c_dt_str = f"{c_dt} ({c_da}d ago)" if c_da > 0 else c_dt
                items.append(f"<b>{c_dt_str}</b> {ca['Type']} ({ca['Level_Price']:,.1f})")
            confluence_html = f'<div style="margin-top: 8px; font-size: 13px; color: #FDE68A;"><b>🔥 Multi-Day Confluence:</b> Also matched {" · ".join(items)}</div>'

        # Construct single contiguous HTML string (no blank lines or 4-space markdown indents)
        card_html = (
            f'<div style="background: linear-gradient(135deg, #1C1917 0%, #291D17 100%); border: 2px solid #F59E0B; border-radius: 12px; padding: 16px 20px; margin-bottom: 20px; box-shadow: 0 4px 14px rgba(245, 158, 11, 0.25);">'
            f'<div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px;">'
            f'<div><span style="background: #F59E0B; color: #000; font-weight: 800; font-size: 11px; padding: 3px 8px; border-radius: 4px; letter-spacing: 0.5px;">⚡ 100-PT HISTORICAL LEVEL ALERT (&le; 20 PTS)</span>'
            f'<span style="color: #A8A29E; font-size: 12.5px; margin-left: 8px;">Tested by: <b style="color:#FFF;">{tested_by}</b></span></div>'
            f'<div style="color: #FBBF24; font-size: 15px; font-weight: 700;">Distance: {dist:.1f} pts</div>'
            f'</div>'
            f'<div style="margin-top: 10px; display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap;">'
            f'<span style="font-size: 26px; font-weight: 900; color: #FFF;">{tested_lvl:,.2f}</span>'
            f'<span style="background: rgba(255,255,255,0.08); padding: 4px 10px; border-radius: 6px; font-size: 13.5px; color: #E7E5E4; font-weight: 600;">{dt} {ltype}</span>'
            f'<span style="color: {badge_color}; font-size: 14.5px; font-weight: 700;">• {role}</span>'
            f'</div>'
            f'{confluence_html}'
            f'<div style="margin-top: 8px; font-size: 13px; color: #D6D3D1;"><b>Historical Wick Footprint:</b> {wick_info}</div>'
            f'<div style="margin-top: 10px; background: rgba(0,0,0,0.35); padding: 10px 14px; border-radius: 8px; border-left: 4px solid #F59E0B; color: #FEF3C7; font-size: 13.5px;"><b>Trade Strategy:</b> {plan}</div>'
            f'</div>'
        )
        _render_html(card_html)

        if len(alerts_20) > 1:
            with st.expander(f"📋 See {len(alerts_20) - 1} other historical level(s) within 20 pts", expanded=False):
                sub_rows = []
                for a in alerts_20[1:]:
                    a_dt = a.get("Date_Display", a["Date_Formed"])
                    a_da = a.get("Days_Ago", 0)
                    a_dt_str = f"{a_dt} ({a_da}d ago)" if a_da > 0 else a_dt
                    sub_rows.append({
                        "Date": a_dt_str,
                        "Level": f"{a['Level_Price']:,.2f}",
                        "Type": a["Type"],
                        "Role": a["Role"],
                        "Wick": a["Wick_Info"],
                        "Distance": f"{a['Min_Dist']:.1f} pts",
                        "Strategy": a["Action_Plan"],
                    })
                st.dataframe(pd.DataFrame(sub_rows), use_container_width=True, hide_index=True)
    else:
        # Show nearest historical level so trader is aware
        nearest = vdf.iloc[0]
        n_price = nearest["Level_Price"]
        n_dist = nearest["Min_Dist"]
        n_dt_disp = nearest.get("Date_Display", nearest["Date_Formed"])
        n_da = nearest.get("Days_Ago", 0)
        n_dt = f"{n_dt_disp} ({n_da}d ago)" if n_da > 0 else n_dt_disp
        n_type = nearest["Type"]
        n_role = nearest["Role"]
        st.caption(
            f"🏛️ **Nearest Key Historical Level:** {n_dt} {n_type} at **{n_price:,.2f}** "
            f"({n_dist:.1f} pts away · *{n_role}*). Alerts trigger within 20 pts for 100-pt reversal setups."
        )


def render_chart_levels_section(
    spot: float,
    today_high: float | None = None,
    today_low: float | None = None,
    current_date: str | date | None = None,
    hist_df: pd.DataFrame | None = None,
) -> None:
    """
    Renders the comprehensive Historical Day High/Low & 15m Long Wick
    VLOOKUP Explorer inside the Chart Levels Tab (t11).
    """
    import streamlit as st

    st.markdown("---")
    st.markdown("### 🏛️ Historical Daily High/Low & 15m Long Wick Engine")
    st.caption(
        "Scans all historical daily highs and lows and 15-minute rejection wicks. "
        "When today's price comes within 20 points of a historical level, it flags "
        "high-conviction 100-point reversal setups (e.g. 03-Sep High hitting 19-Aug Low at 24,025)."
    )

    if hist_df is None or hist_df.empty:
        hist_df = build_historical_levels_database()

    if hist_df.empty:
        st.warning("Historical levels database is empty. Check data/ohlc/ or data/candles/.")
        return

    vdf = vlookup_levels(
        spot=spot,
        today_high=today_high,
        today_low=today_low,
        current_date=current_date,
        tolerance_pts=100.0,
        hist_df=hist_df,
    )

    # 1. Summary Metrics
    alerts_20 = [r for _, r in vdf.iterrows() if r["Min_Dist"] <= 20.0] if not vdf.empty else []
    alerts_40 = [r for _, r in vdf.iterrows() if 20.0 < r["Min_Dist"] <= 40.0] if not vdf.empty else []

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Sessions in DB", f"{len(hist_df):,}")
    c2.metric("Within 20 pts (⚡ Alerts)", f"{len(alerts_20)}")
    c3.metric("Within 40 pts (Approaching)", f"{len(alerts_40)}")
    if not vdf.empty:
        top = vdf.iloc[0]
        c4.metric("Nearest Level", f"{top['Level_Price']:,.1f}", f"{top['Min_Dist']:.1f} pts away", delta_color="off")
    else:
        c4.metric("Nearest Level", "None in range")

    # 2. Prominent banner if active alerts <= 20 pts
    if alerts_20:
        st.warning(
            f"⚡ **ACTIVE 100-PT SETUP ALERT:** Current price action is within **20 points** "
            f"of **{len(alerts_20)} historical level(s)**! High-odds reaction zone."
        )

    # 3. Interactive Filter & Table
    tfilter = st.radio(
        "Show Levels:",
        ["Within 20 pts (Active Setups)", "Within 40 pts (Approaching)", "Within 80 pts (Broader Map)", "All In-Range"],
        horizontal=True,
        key="hist_filter_radio",
    )

    if not vdf.empty:
        if tfilter == "Within 20 pts (Active Setups)":
            filtered_df = vdf[vdf["Min_Dist"] <= 20.0].copy()
        elif tfilter == "Within 40 pts (Approaching)":
            filtered_df = vdf[vdf["Min_Dist"] <= 40.0].copy()
        elif tfilter == "Within 80 pts (Broader Map)":
            filtered_df = vdf[vdf["Min_Dist"] <= 80.0].copy()
        else:
            filtered_df = vdf.copy()

        if filtered_df.empty:
            st.info(f"No levels found under filter: **{tfilter}**.")
        else:
            display_cols = [
                "Alert", "Date_Formed", "Type", "Level_Price", "Dist_Spot",
                "Min_Dist", "Tested_By", "Role", "Wick_Info", "Action_Plan"
            ]
            show_df = filtered_df[[c for c in display_cols if c in filtered_df.columns]].copy()
            show_df = show_df.rename(columns={
                "Date_Formed": "Formed On",
                "Level_Price": "Price",
                "Dist_Spot": "vs Spot",
                "Min_Dist": "Min Dist",
                "Tested_By": "Tested By",
                "Wick_Info": "15m Wick Footprint",
                "Action_Plan": "100-pt Trade Strategy",
            })

            def _highlight_status(row):
                alt = row["Alert"]
                if "AT LEVEL" in alt:
                    return ["background-color: #7A1F1A; color: #FFE8E5; font-weight: bold;"] * len(row)
                if "NEAR LEVEL" in alt:
                    return ["background-color: #6B5310; color: #FFF3CC; font-weight: bold;"] * len(row)
                if "APPROACHING" in alt:
                    return ["background-color: #1A365D; color: #EBF8FF;"] * len(row)
                return [""] * len(row)

            st.dataframe(
                show_df.style.apply(_highlight_status, axis=1),
                use_container_width=True,
                hide_index=True,
            )

    # 4. PC File Download Buttons
    dcol1, dcol2 = st.columns(2)
    if CSV_PATH.exists():
        with open(CSV_PATH, "rb") as f:
            dcol1.download_button(
                label="📥 Download Master Table (CSV)",
                data=f.read(),
                file_name="historical_key_levels.csv",
                mime="text/csv",
                use_container_width=True,
            )
    if XLSX_PATH.exists():
        with open(XLSX_PATH, "rb") as f:
            dcol2.download_button(
                label="📥 Download Master Table (Excel .xlsx)",
                data=f.read(),
                file_name="historical_key_levels.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
            )

