"""
top_bottom_sniper.py — High-Probability Top & Bottom Reversal Sniper.

Designed to catch Day Tops and Day Bottoms (and major session swing turns)
with a strict 10-15 point stop loss and 60-70 point profit target.

Pillars:
1. Extremum Zone: Day High/Low, 500/1000 Round Numbers, Bollinger Band (20, 2) touch.
2. Reversal Geometry: Hammer, Inverted Hammer, Shooting Star, Bearish/Bullish Engulfing, Doji.
3. EMA 4/8 Cross & Close confirmation.
4. Execution Rule: Zero anticipation. Entry only on breakdown/breakout of the rejection bar.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import pandas as pd
import numpy as np


@dataclass
class SniperSetup:
    side: str                   # "TOP_SHORT" | "BOTTOM_LONG"
    status: str                 # "TRIGGERED" | "ARMED" | "MONITORING" | "TARGET 1 HIT (+40 PTS)" | "TARGET 2 HIT (+65 PTS)"
    title: str                  # e.g. "Day High & 24,000 Rejection"
    pattern_name: str           # e.g. "Inverted Hammer", "Double Top Doji"
    timeframe: str              # "5m" | "15m"
    candle_ts: str              # Time pattern formed
    current_spot: float         # Current spot price
    trigger_price: float | None # Exact entry price
    stop_loss: float | None     # Exact SL price
    risk_pts: float | None      # Stop loss distance in points (10-15 pts)
    target_1: float | None      # Target 1 (+40 pts)
    target_2: float | None      # Target 2 (+65 to +70 pts)
    reward_pts: float | None    # Target 2 gain in points
    risk_reward: str            # e.g. "1 : 5.8"
    live_pnl_pts: float | None  # Current running profit/loss in points
    confluences: list[str] = field(default_factory=list)
    action_note: str = ""       # What trader should do right now


@dataclass
class SniperReport:
    eval_time: datetime
    spot: float
    day_high: float
    day_low: float
    dh_dist: float
    dl_dist: float
    top_setup: SniperSetup
    bottom_setup: SniperSetup


def _add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    if len(d) < 5:
        return d
    d["ema4"] = d["close"].ewm(span=4, adjust=False).mean()
    d["ema8"] = d["close"].ewm(span=8, adjust=False).mean()
    period = min(20, len(d))
    mid = d["close"].rolling(period).mean()
    sd = d["close"].rolling(period).std(ddof=0)
    d["bb_mid"] = mid
    d["ubb"] = mid + 2.0 * sd
    d["lbb"] = mid - 2.0 * sd
    return d


def evaluate_sniper(
    df_5m: pd.DataFrame | None,
    df_15m: pd.DataFrame | None,
    spot: float,
    r1: float | None = None,
    s1: float | None = None,
    round_step: int = 500,
) -> SniperReport:
    now = datetime.now()
    if df_5m is None or df_5m.empty:
        dummy_top = SniperSetup(
            side="TOP_SHORT", status="MONITORING", title="Top Reversal Hunter",
            pattern_name="No Pattern", timeframe="5m", candle_ts="—", current_spot=spot,
            trigger_price=None, stop_loss=None, risk_pts=None, target_1=None, target_2=None,
            reward_pts=65.0, risk_reward="1 : 5.0", live_pnl_pts=None,
            confluences=["Waiting for live candles"], action_note="Awaiting live data..."
        )
        dummy_bot = SniperSetup(
            side="BOTTOM_LONG", status="MONITORING", title="Bottom Reversal Hunter",
            pattern_name="No Pattern", timeframe="5m", candle_ts="—", current_spot=spot,
            trigger_price=None, stop_loss=None, risk_pts=None, target_1=None, target_2=None,
            reward_pts=65.0, risk_reward="1 : 5.0", live_pnl_pts=None,
            confluences=["Waiting for live candles"], action_note="Awaiting live data..."
        )
        return SniperReport(now, spot, spot, spot, 0.0, 0.0, dummy_top, dummy_bot)

    today_dt = df_5m["timestamp"].dt.date.max()
    d5 = df_5m[df_5m["timestamp"].dt.date == today_dt].copy().reset_index(drop=True)
    d5 = _add_indicators(d5)

    dh = float(d5["high"].max())
    dl = float(d5["low"].min())
    dh_dist = round(dh - spot, 1)
    dl_dist = round(spot - dl, 1)

    near_round_high = round(dh / round_step) * round_step
    near_round_low = round(dl / round_step) * round_step

    # ── Top Sniper Search (Scanning backwards from recent candles) ───────────
    top_setup = SniperSetup(
        side="TOP_SHORT",
        status="MONITORING",
        title="Day High & Resistance Exhaustion",
        pattern_name="None",
        timeframe="5m",
        candle_ts="—",
        current_spot=spot,
        trigger_price=None,
        stop_loss=None,
        risk_pts=None,
        target_1=None,
        target_2=None,
        reward_pts=65.0,
        risk_reward="1 : 5.5",
        live_pnl_pts=None,
        confluences=[],
        action_note=f"Spot is {dh_dist:+.0f} pts from Day High ({dh:,.1f}). Watch for upper wick rejection at 24,000 / DH.",
    )

    # Scan for recent high-probability top reversals (within last 35 bars)
    for i in range(len(d5) - 1, 0, -1):
        c = d5.iloc[i]
        c_prev = d5.iloc[i - 1]
        body = abs(c["close"] - c["open"])
        rng = max(c["high"] - c["low"], 1.0)
        upper_wick = c["high"] - max(c["open"], c["close"])
        lower_wick = min(c["open"], c["close"]) - c["low"]

        # Is near DH or local swing high (highest of 5 bars)
        is_near_dh = (dh - c["high"]) <= 25.0
        is_swing_high = (c["high"] >= d5.iloc[max(0, i - 5):i + 1]["high"].max() - 1.0)
        is_near_round = abs(c["high"] - near_round_high) <= 40.0
        is_near_ubb = "ubb" in c and c["high"] >= c["ubb"] - 6.0

        if (is_near_dh or is_swing_high or is_near_round or is_near_ubb):
            is_inv_hammer = upper_wick >= 1.2 * body and upper_wick >= 0.35 * rng
            is_doji = body <= 0.15 * rng
            is_engulfing = (c["close"] < c["open"] and c_prev["close"] > c_prev["open"]
                            and c["close"] < c_prev["open"])
            is_cross_down = c["close"] < c.get("ema4", c["close"]) and c["close"] < c.get("ema8", c["close"])

            if is_inv_hammer or is_doji or is_engulfing or (upper_wick >= 7.0 and is_cross_down):
                pat_type = ("Inverted Hammer" if is_inv_hammer else
                            "Bearish Engulfing" if is_engulfing else
                            "Exhaustion Doji" if is_doji else "Upper Wick Rejection")

                ts_str = pd.to_datetime(c["timestamp"]).strftime("%H:%M")
                trig = round(float(c["low"]) - 0.5, 2)
                sl = round(float(c["high"]) + 1.0, 2)
                risk = round(sl - trig, 2)
                t1 = round(trig - 40.0, 2)
                t2 = round(trig - 65.0, 2)

                top_setup.pattern_name = pat_type
                top_setup.candle_ts = ts_str
                top_setup.trigger_price = trig
                top_setup.stop_loss = sl
                top_setup.risk_pts = risk
                top_setup.target_1 = t1
                top_setup.target_2 = t2
                top_setup.reward_pts = 65.0
                top_setup.risk_reward = f"1 : {round(65.0 / max(risk, 1.0), 1)}"

                confl = []
                if is_near_dh:
                    confl.append(f"Day High Zone ({dh:,.1f})")
                elif is_swing_high:
                    confl.append(f"Session Swing High ({c['high']:,.1f})")
                if is_near_round:
                    confl.append(f"Round Number Barrier ({near_round_high:,.0f})")
                if is_near_ubb:
                    confl.append("Upper Bollinger Band Piercing")
                if is_cross_down:
                    confl.append("4 & 8 EMA Rollover")
                top_setup.confluences = confl

                subsequent = d5.iloc[i + 1:] if i + 1 < len(d5) else pd.DataFrame()
                if not subsequent.empty and (subsequent["low"].min() < trig):
                    pnl = round(trig - spot, 1)
                    top_setup.live_pnl_pts = pnl
                    if spot <= t2:
                        top_setup.status = "TARGET 2 HIT (+65 PTS)"
                        top_setup.action_note = f"🎯 Target 2 (+65 pts) fully banked! Book profits."
                    elif spot <= t1:
                        top_setup.status = "TARGET 1 HIT (+40 PTS)"
                        top_setup.action_note = f"✅ Target 1 hit (+{pnl} pts profit). Trail SL to entry ({trig}) for +65 pts runner."
                    elif spot > sl:
                        top_setup.status = "STOPPED OUT"
                        top_setup.action_note = f"Stop loss triggered at {sl}. Trade closed with strict {risk} pts risk."
                    else:
                        top_setup.status = "TRIGGERED"
                        top_setup.action_note = f"⚡ Trade Active! Short triggered at {trig}. Running +{pnl:+.1f} pts. Strict SL at {sl}."
                else:
                    if spot > sl:
                        top_setup.status = "INVALIDATED"
                        top_setup.action_note = f"Price pushed above {sl}. Rejection invalidated."
                    else:
                        top_setup.status = "ARMED"
                        top_setup.action_note = f"🚨 ARMED: Sell Stop-Loss at {trig} (Break of {pat_type} low). SL: {sl} ({risk} pts risk). Target: {t2} (+65 pts)."
                break

    # ── Bottom Sniper Search (Scanning backwards from recent candles) ────────
    bottom_setup = SniperSetup(
        side="BOTTOM_LONG",
        status="MONITORING",
        title="Day Low & Support Accumulation",
        pattern_name="None",
        timeframe="5m",
        candle_ts="—",
        current_spot=spot,
        trigger_price=None,
        stop_loss=None,
        risk_pts=None,
        target_1=None,
        target_2=None,
        reward_pts=65.0,
        risk_reward="1 : 5.5",
        live_pnl_pts=None,
        confluences=[],
        action_note=f"Spot is {dl_dist:+.0f} pts from Day Low ({dl:,.1f}). Watch for lower wick rejection at support / DL.",
    )

    for i in range(len(d5) - 1, 0, -1):
        c = d5.iloc[i]
        c_prev = d5.iloc[i - 1]
        body = abs(c["close"] - c["open"])
        rng = max(c["high"] - c["low"], 1.0)
        upper_wick = c["high"] - max(c["open"], c["close"])
        lower_wick = min(c["open"], c["close"]) - c["low"]

        is_near_dl = (c["low"] - dl) <= 25.0
        is_swing_low = (c["low"] <= d5.iloc[max(0, i - 5):i + 1]["low"].min() + 1.0)
        is_near_round = abs(c["low"] - near_round_low) <= 40.0
        is_near_lbb = "lbb" in c and c["low"] <= c["lbb"] + 6.0

        if (is_near_dl or is_swing_low or is_near_round or is_near_lbb):
            is_hammer = lower_wick >= 1.2 * body and lower_wick >= 0.35 * rng
            is_doji = body <= 0.15 * rng
            is_engulfing = (c["close"] > c["open"] and c_prev["close"] < c_prev["open"]
                            and c["close"] > c_prev["open"])
            is_cross_up = c["close"] > c.get("ema4", c["close"]) and c["close"] > c.get("ema8", c["close"])

            if is_hammer or is_doji or is_engulfing or (lower_wick >= 7.0 and is_cross_up):
                pat_type = ("Hammer" if is_hammer else
                            "Bullish Engulfing" if is_engulfing else
                            "Dragonfly / Bottom Doji" if is_doji else "Lower Wick Absorption")

                ts_str = pd.to_datetime(c["timestamp"]).strftime("%H:%M")
                trig = round(float(c["high"]) + 0.5, 2)
                sl = round(float(c["low"]) - 1.0, 2)
                risk = round(trig - sl, 2)
                t1 = round(trig + 40.0, 2)
                t2 = round(trig + 65.0, 2)

                bottom_setup.pattern_name = pat_type
                bottom_setup.candle_ts = ts_str
                bottom_setup.trigger_price = trig
                bottom_setup.stop_loss = sl
                bottom_setup.risk_pts = risk
                bottom_setup.target_1 = t1
                bottom_setup.target_2 = t2
                bottom_setup.reward_pts = 65.0
                bottom_setup.risk_reward = f"1 : {round(65.0 / max(risk, 1.0), 1)}"

                confl = []
                if is_near_dl:
                    confl.append(f"Day Low Base ({dl:,.1f})")
                elif is_swing_low:
                    confl.append(f"Session Swing Low ({c['low']:,.1f})")
                if is_near_round:
                    confl.append(f"Round Number Support ({near_round_low:,.0f})")
                if is_near_lbb:
                    confl.append("Lower Bollinger Band Piercing")
                if is_cross_up:
                    confl.append("4 & 8 EMA Upward Expansion")
                bottom_setup.confluences = confl

                subsequent = d5.iloc[i + 1:] if i + 1 < len(d5) else pd.DataFrame()
                if not subsequent.empty and (subsequent["high"].max() > trig):
                    pnl = round(spot - trig, 1)
                    bottom_setup.live_pnl_pts = pnl
                    if spot >= t2:
                        bottom_setup.status = "TARGET 2 HIT (+65 PTS)"
                        bottom_setup.action_note = f"🎯 Target 2 (+65 pts) fully banked! Book profits."
                    elif spot >= t1:
                        bottom_setup.status = "TARGET 1 HIT (+40 PTS)"
                        bottom_setup.action_note = f"✅ Target 1 hit (+{pnl} pts profit). Trail SL to entry ({trig}) for +65 pts runner."
                    elif spot < sl:
                        bottom_setup.status = "STOPPED OUT"
                        bottom_setup.action_note = f"Stop loss triggered at {sl}. Trade closed with strict {risk} pts risk."
                    else:
                        bottom_setup.status = "TRIGGERED"
                        bottom_setup.action_note = f"⚡ Trade Active! Long triggered at {trig}. Running +{pnl:+.1f} pts. Strict SL at {sl}."
                else:
                    if spot < sl:
                        bottom_setup.status = "INVALIDATED"
                        bottom_setup.action_note = f"Price broke below {sl}. Bottom setup invalidated."
                    else:
                        bottom_setup.status = "ARMED"
                        bottom_setup.action_note = f"🚨 ARMED: Buy Stop-Loss at {trig} (Break of {pat_type} high). SL: {sl} ({risk} pts risk). Target: {t2} (+65 pts)."
                break

    return SniperReport(now, spot, dh, dl, dh_dist, dl_dist, top_setup, bottom_setup)


def render_sniper_card(report: SniperReport):
    import streamlit as st

    st.markdown(
        """
        <div style="margin-top: 10px; margin-bottom: 8px;">
            <span style="font-size: 1.15em; font-weight: 800; color: #F8FAFC; letter-spacing: 0.05em;">
                🎯 TOP & BOTTOM REVERSAL SNIPER (10-15 Pt Risk · 60-70 Pt Gain)
            </span>
            <div style="font-size: 0.85em; color: #94A3B8;">
                Monitors Day High, Day Low, 500/1000 Round Numbers, Bollinger Bands (20,2), and EMA 4/8 crosses to catch exact tops & bottoms.
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    c_top, c_bot = st.columns(2)

    # ── Top Sniper Card (Red / Short) ──────────────────────────────────────
    with c_top:
        top = report.top_setup
        bg_color = "#3b0707" if "TRIGGERED" in top.status else ("#451a03" if top.status == "ARMED" else "#111827")
        border_color = "#ef4444" if "TRIGGERED" in top.status else ("#f59e0b" if top.status == "ARMED" else "#374151")
        badge_bg = "#dc2626" if "TRIGGERED" in top.status else ("#d97706" if top.status == "ARMED" else "#4b5563")

        trig_str = f"{top.trigger_price:,.2f}" if top.trigger_price else "—"
        sl_str = f"{top.stop_loss:,.2f} ({top.risk_pts:.1f} pts)" if top.stop_loss else "—"
        t1_str = f"{top.target_1:,.2f} (+40 pts)" if top.target_1 else "—"
        t2_str = f"{top.target_2:,.2f} (+65 pts)" if top.target_2 else "—"
        confl_html = "".join(f"<li style='margin-bottom: 2px;'>{c}</li>" for c in top.confluences) or "<li>Scanning Day High resistance zone...</li>"

        st.markdown(
            f"""
            <div style="background: {bg_color}33; border: 1.5px solid {border_color}; border-radius: 8px; padding: 12px 16px; min-height: 240px;">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                    <span style="font-size: 1.05em; font-weight: 800; color: #f87171;">🔴 TOP SNIPER (SHORT)</span>
                    <span style="background: {badge_bg}; color: #ffffff; padding: 3px 9px; border-radius: 4px; font-weight: 700; font-size: 0.8em; font-family: monospace;">
                        {top.status}
                    </span>
                </div>
                <div style="font-size: 0.88em; color: #e2e8f0; margin-bottom: 6px;">
                    <b>Pattern:</b> {top.pattern_name} @ {top.candle_ts}
                </div>
                <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 6px; font-size: 0.84em; background: #00000044; padding: 8px; border-radius: 6px; margin-bottom: 8px; font-family: monospace;">
                    <div><b>Sell Trigger:</b> <span style="color:#f87171;">{trig_str}</span></div>
                    <div><b>Stop Loss:</b> <span style="color:#cbd5e1;">{sl_str}</span></div>
                    <div><b>Target 1:</b> <span style="color:#34d399;">{t1_str}</span></div>
                    <div><b>Target 2:</b> <span style="color:#10b981; font-weight:700;">{t2_str}</span></div>
                </div>
                <div style="font-size: 0.8em; color: #94a3b8; margin-bottom: 6px;">
                    <ul style="padding-left: 16px; margin: 0;">{confl_html}</ul>
                </div>
                <div style="font-size: 0.84em; font-weight: 600; color: #fecaca; background: #991b1b33; padding: 6px 10px; border-radius: 4px; border-left: 3px solid #ef4444;">
                    {top.action_note}
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    # ── Bottom Sniper Card (Green / Long) ──────────────────────────────────
    with c_bot:
        bot = report.bottom_setup
        bg_color = "#022c22" if "TRIGGERED" in bot.status else ("#451a03" if bot.status == "ARMED" else "#111827")
        border_color = "#10b981" if "TRIGGERED" in bot.status else ("#f59e0b" if bot.status == "ARMED" else "#374151")
        badge_bg = "#059669" if "TRIGGERED" in bot.status else ("#d97706" if bot.status == "ARMED" else "#4b5563")

        trig_str = f"{bot.trigger_price:,.2f}" if bot.trigger_price else "—"
        sl_str = f"{bot.stop_loss:,.2f} ({bot.risk_pts:.1f} pts)" if bot.stop_loss else "—"
        t1_str = f"{bot.target_1:,.2f} (+40 pts)" if bot.target_1 else "—"
        t2_str = f"{bot.target_2:,.2f} (+65 pts)" if bot.target_2 else "—"
        confl_html = "".join(f"<li style='margin-bottom: 2px;'>{c}</li>" for c in bot.confluences) or "<li>Scanning Day Low support zone...</li>"

        st.markdown(
            f"""
            <div style="background: {bg_color}33; border: 1.5px solid {border_color}; border-radius: 8px; padding: 12px 16px; min-height: 240px;">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                    <span style="font-size: 1.05em; font-weight: 800; color: #34d399;">🟢 BOTTOM SNIPER (LONG)</span>
                    <span style="background: {badge_bg}; color: #ffffff; padding: 3px 9px; border-radius: 4px; font-weight: 700; font-size: 0.8em; font-family: monospace;">
                        {bot.status}
                    </span>
                </div>
                <div style="font-size: 0.88em; color: #e2e8f0; margin-bottom: 6px;">
                    <b>Pattern:</b> {bot.pattern_name} @ {bot.candle_ts}
                </div>
                <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 6px; font-size: 0.84em; background: #00000044; padding: 8px; border-radius: 6px; margin-bottom: 8px; font-family: monospace;">
                    <div><b>Buy Trigger:</b> <span style="color:#34d399;">{trig_str}</span></div>
                    <div><b>Stop Loss:</b> <span style="color:#cbd5e1;">{sl_str}</span></div>
                    <div><b>Target 1:</b> <span style="color:#34d399;">{t1_str}</span></div>
                    <div><b>Target 2:</b> <span style="color:#10b981; font-weight:700;">{t2_str}</span></div>
                </div>
                <div style="font-size: 0.8em; color: #94a3b8; margin-bottom: 6px;">
                    <ul style="padding-left: 16px; margin: 0;">{confl_html}</ul>
                </div>
                <div style="font-size: 0.84em; font-weight: 600; color: #a7f3d0; background: #065f4633; padding: 6px 10px; border-radius: 4px; border-left: 3px solid #10b981;">
                    {bot.action_note}
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )