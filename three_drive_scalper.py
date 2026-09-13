"""
three_drive_scalper.py — 1-Minute Institutional 3-Drive & Liquidity Sweep Scalper.

Based on Priyank Sharma's (SEBI RA) Price Action & Institutional Framework:
1. Institutional Stop-Loss Hunting:
   - Retail stop-losses cluster below key support (sell stops) or above key resistance (buy stops).
   - Institutions require this liquidity to fill large order sizes without slippage.
2. The 3-Drive Pattern (2 or more fakeouts / liquidity sweeps):
   - Drive 1: Initial test of the key level.
   - Drive 2: Sweep taking out Drive 1 stops with immediate wick rejection.
   - Drive 3: Final sweep taking remaining stops, often displaying RSI Momentum Divergence.
3. Market Structure Shift (MSS):
   - Strict execution rule: Zero premature anticipation on sweeps alone.
   - Wait for a 1-minute close breaking the swing high/low that created the lowest/highest sweep.
4. Risk Management:
   - Stop-Loss: Placed beyond the extreme sweep wick where price has no technical reason to revisit.
   - Target 1: Minimum 1:1 Risk-to-Reward (trail SL to Cost).
   - Target 2: Origin of the move / opposite key level (1:2 to 1:4 RR).
5. Optional Confirmations ("Ingredients"):
   - RSI Divergence (14-period on 1m).
   - Volume expansion on the MSS candle.
"""
from __future__ import annotations

from typing import Any
from dataclasses import dataclass, field
from datetime import datetime
import numpy as np
import pandas as pd
from ui_utils import render_html, _render_html


@dataclass
class DrivePoint:
    drive_num: int              # 1, 2, or 3
    time: str                   # "09:42"
    price: float                # Low or High
    rsi: float                  # RSI value at the point
    is_sweep: bool              # True if broke previous drive's extreme and rejected


@dataclass
class ThreeDriveSetup:
    side: str                   # "BULLISH_LONG" | "BEARISH_SHORT"
    status: str                 # "ARMED_AWAITING_MSS" | "TRIGGERED_MSS_CONFIRMED" | "TARGET_1_HIT" | "TARGET_2_HIT" | "STOPPED_OUT" | "INVALIDATED"
    key_level_name: str         # e.g. "PDL Support", "24,200 Round Strike", "S1 Pivot"
    key_level_price: float
    drives: list[DrivePoint]    # 2 or 3 drive points
    mss_level: float            # Market Structure Shift price
    mss_time: str               # Time MSS was defined/broken
    trigger_price: float        # Exact entry price
    stop_loss: float            # Strict SL below sweep
    risk_pts: float             # Risk distance (8-15 pts)
    target_1: float             # 1:1 RR Target
    target_2: float             # Origin of move / HTF Target
    reward_pts: float           # Target 2 gain
    risk_reward: str            # e.g. "1 : 2.5"
    has_rsi_divergence: bool    # True if RSI diverged across drives
    rsi_divergence_note: str    # Detail of divergence
    volume_expansion: bool      # Volume > 1.3x rolling avg on MSS
    live_pnl_pts: float | None  # Running PnL from trigger
    action_plan: str            # Tactical execution guidance

    def get(self, key: str, default: Any = None) -> Any:
        """Dict-like access compatibility for safety across UI callers."""
        if key == "actionable":
            return self.status in ("ARMED_AWAITING_MSS", "TRIGGERED_MSS_CONFIRMED")
        if key in ("direction", "side"):
            return "CALL" if "BULLISH" in str(self.side) else "PUT"
        if key in ("stop", "stop_loss"):
            return self.stop_loss
        if key in ("target", "target_1"):
            return self.target_1
        return getattr(self, key, default)

    def __getitem__(self, key: str) -> Any:
        res = self.get(key)
        if res is None and not hasattr(self, key):
            raise KeyError(key)
        return res


def compute_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI (standard 14-period)."""
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi.fillna(50.0)


def evaluate_three_drive(
    df_1m: pd.DataFrame | None,
    spot: float,
    key_levels: dict[str, float] | None = None,
) -> ThreeDriveSetup | None:
    """
    Evaluates 1-minute candle history for Priyank Sharma's 3-Drive Liquidity Sweep & MSS setup.
    """
    if df_1m is None or df_1m.empty or len(df_1m) < 25:
        return None

    d = df_1m.copy()
    if "timestamp" in d.columns:
        d["timestamp"] = pd.to_datetime(d["timestamp"])
    else:
        d["timestamp"] = pd.to_datetime(d.index)

    # Filter to today's session if multiple days exist
    today_dt = d["timestamp"].dt.date.max()
    d_today = d[d["timestamp"].dt.date == today_dt].copy().reset_index(drop=True)
    if len(d_today) < 20:
        d_today = d.iloc[-60:].copy().reset_index(drop=True)

    d_today["rsi"] = compute_rsi(d_today["close"], period=14)
    if "volume" in d_today.columns and d_today["volume"].sum() > 0:
        d_today["vol_ma"] = d_today["volume"].rolling(15, min_periods=5).mean()
    else:
        d_today["vol_ma"] = 0

    # Build key levels dictionary if not provided
    if not key_levels:
        key_levels = {}
    
    dh = float(d_today["high"].max())
    dl = float(d_today["low"].min())
    key_levels.setdefault("Day High", dh)
    key_levels.setdefault("Day Low", dl)

    # Add nearest 100-pt psychological strikes
    round_below = (int(spot) // 100) * 100
    round_above = round_below + 100
    key_levels.setdefault(f"{round_below:,.0f} Strike", float(round_below))
    key_levels.setdefault(f"{round_above:,.0f} Strike", float(round_above))

    # Identify fractal pivots on 1m (window of 2 on each side)
    pivots_low = []
    pivots_high = []
    h = d_today["high"].to_numpy()
    l = d_today["low"].to_numpy()
    n = 2
    for i in range(n, len(d_today) - n):
        if l[i] <= l[i - 1] and l[i] < l[i - 2] and l[i] <= l[i + 1] and l[i] < l[i + 2]:
            pivots_low.append(i)
        if h[i] >= h[i - 1] and h[i] > h[i - 2] and h[i] >= h[i + 1] and h[i] > h[i + 2]:
            pivots_high.append(i)

    # ── 1. Scan for Bullish 3-Drive (Support Sweep & MSS Breakout) ───────────
    # Look at recent swing lows (at least 2-3 drives)
    if len(pivots_low) >= 2:
        # Check the last 3 swing lows
        recent_pl = pivots_low[-3:] if len(pivots_low) >= 3 else pivots_low[-2:]
        if len(recent_pl) >= 2:
            idx_d1 = recent_pl[0]
            idx_d2 = recent_pl[1]
            idx_d3 = recent_pl[2] if len(recent_pl) >= 3 else None

            p1_low = float(d_today.iloc[idx_d1]["low"])
            p2_low = float(d_today.iloc[idx_d2]["low"])
            p3_low = float(d_today.iloc[idx_d3]["low"]) if idx_d3 is not None else None

            # Check if Drive 2 swept Drive 1 (P2 low <= P1 low + 2.0 pts)
            is_p2_sweep = p2_low <= p1_low + 2.0
            is_p3_sweep = (p3_low is not None and p3_low <= p2_low + 2.0)

            if is_p2_sweep:
                # Find the intermediate high between Drive 1/2 and final drive
                mid_highs = [i for i in pivots_high if i > idx_d1 and (idx_d3 is None or i < idx_d3)]
                if mid_highs:
                    mss_idx = mid_highs[-1]
                    mss_price = float(d_today.iloc[mss_idx]["high"])
                    mss_time = pd.to_datetime(d_today.iloc[mss_idx]["timestamp"]).strftime("%H:%M")

                    # Check proximity of Drive 1/2/3 to a Key HTF Level
                    lowest_drive = min([p for p in [p1_low, p2_low, p3_low] if p is not None])
                    matched_level_name = "Key Support"
                    matched_level_val = lowest_drive
                    min_dist = 999.0
                    for lname, lval in key_levels.items():
                        if "high" in lname.lower() or "r1" in lname.lower() or "r2" in lname.lower() or "pdh" in lname.lower():
                            continue
                        dist = abs(lowest_drive - lval)
                        if dist < min_dist and dist <= 30.0:
                            min_dist = dist
                            matched_level_name = lname
                            matched_level_val = lval

                    # Drives data
                    drives_list = [
                        DrivePoint(1, pd.to_datetime(d_today.iloc[idx_d1]["timestamp"]).strftime("%H:%M"), p1_low, float(d_today.iloc[idx_d1]["rsi"]), False),
                        DrivePoint(2, pd.to_datetime(d_today.iloc[idx_d2]["timestamp"]).strftime("%H:%M"), p2_low, float(d_today.iloc[idx_d2]["rsi"]), True),
                    ]
                    if idx_d3 is not None and p3_low is not None:
                        drives_list.append(DrivePoint(3, pd.to_datetime(d_today.iloc[idx_d3]["timestamp"]).strftime("%H:%M"), p3_low, float(d_today.iloc[idx_d3]["rsi"]), is_p3_sweep))

                    # Check RSI Divergence
                    rsi_d1 = drives_list[0].rsi
                    rsi_last = drives_list[-1].rsi
                    has_bull_div = (drives_list[-1].price <= drives_list[0].price and rsi_last > rsi_d1 + 2.0) or (len(drives_list) == 3 and drives_list[2].price <= drives_list[1].price and drives_list[2].rsi > drives_list[1].rsi + 2.0)
                    div_note = (f"RSI Bullish Divergence (+{rsi_last - rsi_d1:.1f} pts higher vs price lower)"
                                if has_bull_div else "Momentum aligned with structure")

                    # Extreme stop loss (below lowest sweep wick)
                    sl = round(lowest_drive - 1.5, 2)
                    trig = round(mss_price + 0.5, 2)
                    risk = round(trig - sl, 2)

                    # Only proceed if risk is realistic for a 1m scalper (5 to 25 points)
                    if 4.0 <= risk <= 28.0:
                        t1 = round(trig + risk, 2)            # 1:1 Target
                        origin_high = float(d_today.iloc[max(0, idx_d1 - 10):idx_d1]["high"].max()) if idx_d1 > 0 else (trig + 2.0 * risk)
                        t2 = round(max(origin_high, trig + 2.0 * risk), 2)
                        reward = round(t2 - trig, 2)
                        rr_str = f"1 : {round(reward / max(risk, 1.0), 1)}"

                        # Check if MSS has been breached by subsequent bars
                        last_drive_idx = idx_d3 if idx_d3 is not None else idx_d2
                        post_mss_bars = d_today.iloc[last_drive_idx + 1:]

                        vol_exp = False
                        status = "ARMED_AWAITING_MSS"
                        live_pnl = None
                        action = f"🚨 ARMED: {len(drives_list)}-Drive stop hunt completed at {matched_level_name} ({matched_level_val:,.1f}). Wait for 1m candle close above MSS {mss_price:,.1f} to enter."

                        if not post_mss_bars.empty:
                            broke_mss = post_mss_bars["high"].max() >= trig
                            closed_above_mss = (post_mss_bars["close"] >= mss_price).any()

                            if closed_above_mss:
                                status = "TRIGGERED_MSS_CONFIRMED"
                                live_pnl = round(spot - trig, 1)
                                # Check volume on MSS expansion
                                mss_break_row = post_mss_bars[post_mss_bars["close"] >= mss_price].iloc[0]
                                if "vol_ma" in mss_break_row and mss_break_row["vol_ma"] > 0:
                                    vol_exp = mss_break_row["volume"] >= 1.25 * mss_break_row["vol_ma"]

                                if spot >= t2:
                                    status = "TARGET_2_HIT"
                                    action = f"🏆 Target 2 (+{reward:.1f} pts) Hit! Full scalp locked. Institutional order flow completed."
                                elif spot >= t1:
                                    status = "TARGET_1_HIT"
                                    action = f"✅ Target 1 (1:1 RR, +{risk:.1f} pts) Banked! Follow Priyank's rule: TRAIL SL TO COST ({trig:,.1f}) for zero-risk runner."
                                elif spot < sl:
                                    status = "STOPPED_OUT"
                                    action = f"Stop Loss hit at {sl:,.1f}. Invalidation verified (loss capped at {risk:.1f} pts)."
                                else:
                                    action = f"⚡ Trade Active! MSS broken at {mss_price:,.1f}. Running +{live_pnl:+.1f} pts. Trail SL to cost as soon as Target 1 ({t1:,.1f}) is reached."
                            elif spot < sl:
                                status = "INVALIDATED"
                                action = f"Price broke below sweep low ({sl:,.1f}) before MSS confirmation. Setup invalidated."

                        # Return valid Bullish Setup if not ancient
                        if status != "INVALIDATED" and (len(d_today) - last_drive_idx <= 25):
                            return ThreeDriveSetup(
                                side="BULLISH_LONG", status=status, key_level_name=matched_level_name,
                                key_level_price=matched_level_val, drives=drives_list,
                                mss_level=mss_price, mss_time=mss_time, trigger_price=trig,
                                stop_loss=sl, risk_pts=risk, target_1=t1, target_2=t2,
                                reward_pts=reward, risk_reward=rr_str, has_rsi_divergence=has_bull_div,
                                rsi_divergence_note=div_note, volume_expansion=vol_exp,
                                live_pnl_pts=live_pnl, action_plan=action
                            )

    # ── 2. Scan for Bearish 3-Drive (Resistance Sweep & MSS Breakdown) ─────────
    if len(pivots_high) >= 2:
        recent_ph = pivots_high[-3:] if len(pivots_high) >= 3 else pivots_high[-2:]
        if len(recent_ph) >= 2:
            idx_d1 = recent_ph[0]
            idx_d2 = recent_ph[1]
            idx_d3 = recent_ph[2] if len(recent_ph) >= 3 else None

            p1_high = float(d_today.iloc[idx_d1]["high"])
            p2_high = float(d_today.iloc[idx_d2]["high"])
            p3_high = float(d_today.iloc[idx_d3]["high"]) if idx_d3 is not None else None

            is_p2_sweep = p2_high >= p1_high - 2.0
            is_p3_sweep = (p3_high is not None and p3_high >= p2_high - 2.0)

            if is_p2_sweep:
                mid_lows = [i for i in pivots_low if i > idx_d1 and (idx_d3 is None or i < idx_d3)]
                if mid_lows:
                    mss_idx = mid_lows[-1]
                    mss_price = float(d_today.iloc[mss_idx]["low"])
                    mss_time = pd.to_datetime(d_today.iloc[mss_idx]["timestamp"]).strftime("%H:%M")

                    highest_drive = max([p for p in [p1_high, p2_high, p3_high] if p is not None])
                    matched_level_name = "Key Resistance"
                    matched_level_val = highest_drive
                    min_dist = 999.0
                    for lname, lval in key_levels.items():
                        if "low" in lname.lower() or "s1" in lname.lower() or "s2" in lname.lower() or "pdl" in lname.lower():
                            continue
                        dist = abs(highest_drive - lval)
                        if dist < min_dist and dist <= 30.0:
                            min_dist = dist
                            matched_level_name = lname
                            matched_level_val = lval

                    drives_list = [
                        DrivePoint(1, pd.to_datetime(d_today.iloc[idx_d1]["timestamp"]).strftime("%H:%M"), p1_high, float(d_today.iloc[idx_d1]["rsi"]), False),
                        DrivePoint(2, pd.to_datetime(d_today.iloc[idx_d2]["timestamp"]).strftime("%H:%M"), p2_high, float(d_today.iloc[idx_d2]["rsi"]), True),
                    ]
                    if idx_d3 is not None and p3_high is not None:
                        drives_list.append(DrivePoint(3, pd.to_datetime(d_today.iloc[idx_d3]["timestamp"]).strftime("%H:%M"), p3_high, float(d_today.iloc[idx_d3]["rsi"]), is_p3_sweep))

                    rsi_d1 = drives_list[0].rsi
                    rsi_last = drives_list[-1].rsi
                    has_bear_div = (drives_list[-1].price >= drives_list[0].price and rsi_last < rsi_d1 - 2.0) or (len(drives_list) == 3 and drives_list[2].price >= drives_list[1].price and drives_list[2].rsi < drives_list[1].rsi - 2.0)
                    div_note = (f"RSI Bearish Divergence (-{rsi_d1 - rsi_last:.1f} pts lower vs price higher)"
                                if has_bear_div else "Momentum aligned with structure")

                    sl = round(highest_drive + 1.5, 2)
                    trig = round(mss_price - 0.5, 2)
                    risk = round(sl - trig, 2)

                    if 4.0 <= risk <= 28.0:
                        t1 = round(trig - risk, 2)            # 1:1 Target
                        origin_low = float(d_today.iloc[max(0, idx_d1 - 10):idx_d1]["low"].min()) if idx_d1 > 0 else (trig - 2.0 * risk)
                        t2 = round(min(origin_low, trig - 2.0 * risk), 2)
                        reward = round(trig - t2, 2)
                        rr_str = f"1 : {round(reward / max(risk, 1.0), 1)}"

                        last_drive_idx = idx_d3 if idx_d3 is not None else idx_d2
                        post_mss_bars = d_today.iloc[last_drive_idx + 1:]

                        vol_exp = False
                        status = "ARMED_AWAITING_MSS"
                        live_pnl = None
                        action = f"🚨 ARMED: {len(drives_list)}-Drive stop hunt completed at {matched_level_name} ({matched_level_val:,.1f}). Wait for 1m candle close below MSS {mss_price:,.1f} to enter short."

                        if not post_mss_bars.empty:
                            closed_below_mss = (post_mss_bars["close"] <= mss_price).any()

                            if closed_below_mss:
                                status = "TRIGGERED_MSS_CONFIRMED"
                                live_pnl = round(trig - spot, 1)
                                mss_break_row = post_mss_bars[post_mss_bars["close"] <= mss_price].iloc[0]
                                if "vol_ma" in mss_break_row and mss_break_row["vol_ma"] > 0:
                                    vol_exp = mss_break_row["volume"] >= 1.25 * mss_break_row["vol_ma"]

                                if spot <= t2:
                                    status = "TARGET_2_HIT"
                                    action = f"🏆 Target 2 (+{reward:.1f} pts) Hit! Full scalp locked. Institutional order flow completed."
                                elif spot <= t1:
                                    status = "TARGET_1_HIT"
                                    action = f"✅ Target 1 (1:1 RR, +{risk:.1f} pts) Banked! Follow Priyank's rule: TRAIL SL TO COST ({trig:,.1f}) for zero-risk runner."
                                elif spot > sl:
                                    status = "STOPPED_OUT"
                                    action = f"Stop Loss hit at {sl:,.1f}. Invalidation verified (loss capped at {risk:.1f} pts)."
                                else:
                                    action = f"⚡ Trade Active! MSS broken at {mss_price:,.1f}. Running +{live_pnl:+.1f} pts. Trail SL to cost as soon as Target 1 ({t1:,.1f}) is reached."
                            elif spot > sl:
                                status = "INVALIDATED"
                                action = f"Price broke above sweep high ({sl:,.1f}) before MSS confirmation. Setup invalidated."

                        if status != "INVALIDATED" and (len(d_today) - last_drive_idx <= 25):
                            return ThreeDriveSetup(
                                side="BEARISH_SHORT", status=status, key_level_name=matched_level_name,
                                key_level_price=matched_level_val, drives=drives_list,
                                mss_level=mss_price, mss_time=mss_time, trigger_price=trig,
                                stop_loss=sl, risk_pts=risk, target_1=t1, target_2=t2,
                                reward_pts=reward, risk_reward=rr_str, has_rsi_divergence=has_bear_div,
                                rsi_divergence_note=div_note, volume_expansion=vol_exp,
                                live_pnl_pts=live_pnl, action_plan=action
                            )

    return None


def render_three_drive_card(setup: ThreeDriveSetup | None):
    """Renders the 1-Minute 3-Drive Institutional Scalper Card in Streamlit."""
    import streamlit as st

    _render_html(
        """
        <div style="margin-top: 10px; margin-bottom: 8px;">
            <span style="font-size: 1.15em; font-weight: 800; color: #F8FAFC; letter-spacing: 0.04em;">
                ⚡ 1-MIN 3-DRIVE INSTITUTIONAL SCALPER (Priyank Sharma Strategy)
            </span>
            <div style="font-size: 0.85em; color: #94A3B8;">
                Monitors Stop-Loss Hunts (Liquidity Sweeps) at Key HTF Levels + Market Structure Shift (MSS) + RSI Divergence.
            </div>
        </div>
        """
    )

    if setup is None:
        st.info("ℹ️ **Scanning 1m Tape**: No active 3-Drive Stop-Loss Hunt or Market Structure Shift (MSS) within the last 25 minutes. Standing by at Key Levels.")
        return

    is_long = setup.side == "BULLISH_LONG"
    card_border = "#10b981" if is_long else "#ef4444"
    card_bg = "#022c22" if is_long else "#3b0707"
    badge_bg = "#059669" if "TRIGGERED" in setup.status else ("#d97706" if setup.status == "ARMED_AWAITING_MSS" else "#374151")

    # Construct Drive Flowchart HTML
    drive_steps = []
    for d in setup.drives:
        drive_steps.append(f"<span style='background:#1e293b;padding:2px 6px;border-radius:4px;border:1px solid #475569;'><b>D{d.drive_num}</b>: {d.price:,.1f} ({d.time})</span>")
    flow_html = " ──► ".join(drive_steps)
    flow_html += f" ──► <span style='background:{card_border}22;padding:2px 8px;border-radius:4px;border:1px solid {card_border};color:{card_border};font-weight:700;'>MSS @ {setup.mss_level:,.1f}</span>"

    div_badge = f"<span style='background:#3b82f633;color:#60a5fa;border:1px solid #3b82f6;padding:2px 6px;border-radius:4px;font-size:0.78em;font-weight:700;'>⚡ {setup.rsi_divergence_note}</span>" if setup.has_rsi_divergence else ""
    vol_badge = f"<span style='background:#8b5cf633;color:#c084fc;border:1px solid #8b5cf6;padding:2px 6px;border-radius:4px;font-size:0.78em;font-weight:700;'>📊 Volume Surge Confirmed</span>" if setup.volume_expansion else ""

    pnl_str = f" · Live: <b>{setup.live_pnl_pts:+.1f} pts</b>" if setup.live_pnl_pts is not None else ""

    _render_html(
        f"""
        <div style="background: {card_bg}33; border: 1.5px solid {card_border}; border-radius: 8px; padding: 14px 18px; margin-bottom: 12px;">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                <span style="font-size: 1.05em; font-weight: 800; color: {card_border};">
                    {'🟢 BULLISH 3-DRIVE LONG SCALP (CE)' if is_long else '🔴 BEARISH 3-DRIVE SHORT SCALP (PE)'}
                </span>
                <span style="background: {badge_bg}; color: #ffffff; padding: 3px 10px; border-radius: 4px; font-weight: 800; font-size: 0.8em; font-family: monospace;">
                    {setup.status}{pnl_str}
                </span>
            </div>
            
            <div style="font-size: 0.86em; color: #cbd5e1; margin-bottom: 8px;">
                <b>HTF Level Hunted:</b> {setup.key_level_name} ({setup.key_level_price:,.1f}) &nbsp;|&nbsp; {div_badge} &nbsp; {vol_badge}
            </div>

            <div style="font-size: 0.82em; color: #e2e8f0; margin-bottom: 10px; font-family: monospace; overflow-x: auto; white-space: nowrap;">
                <b>Liquidity Flow:</b> {flow_html}
            </div>

            <div style="display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; font-size: 0.84em; background: #00000055; padding: 10px 12px; border-radius: 6px; margin-bottom: 10px; font-family: monospace;">
                <div><b>Entry Trigger:</b><br><span style="color:{card_border};font-weight:700;">{setup.trigger_price:,.2f}</span></div>
                <div><b>Stop Loss:</b><br><span style="color:#cbd5e1;">{setup.stop_loss:,.2f} ({setup.risk_pts:.1f} pts)</span></div>
                <div><b>Target 1 (1:1 RR):</b><br><span style="color:#38bdf8;">{setup.target_1:,.2f} (+{setup.risk_pts:.1f} pts)</span></div>
                <div><b>Target 2 (Origin):</b><br><span style="color:#facc15;font-weight:700;">{setup.target_2:,.2f} ({setup.risk_reward})</span></div>
            </div>

            <div style="font-size: 0.84em; font-weight: 600; color: #f1f5f9; background: #0f172a; padding: 8px 12px; border-radius: 6px; border-left: 3px solid {card_border};">
                {setup.action_plan}
            </div>
        </div>
        """
    )
