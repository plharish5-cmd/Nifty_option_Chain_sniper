"""
top_bottom_sniper.py — High-Probability Top & Bottom Reversal Sniper.

Incorporates the master price action rules from Sushen Sir & MJU's LTA Session:
1. 15-Minute Structural Maturity: Primary scanning on 15m to eliminate 1m/5m noise.
2. The 2-Phase Session Architecture:
   - Gap-Up Phase 1 (Chasing Trap): Spot extended near high -> Do NOT chase Calls at the top.
   - Gap-Up Phase 2 (Value Dip Buy): Pullback to proven Support (PDC gap fill / PDL / S1 / 100-pt strike)
     with 15m Hammer / 8-EMA bounce -> HIGH CONVICTION CE BUY at wholesale price!
   - Gap-Down Phase 1 (Panic Trap): Spot depressed near low -> Do NOT chase Puts at the bottom.
   - Gap-Down Phase 2 (Supply Rip Fade): 60-100 pt relief rally into tough Resistance (PDH / PDC / R1 / 100-pt barrier)
     with 15m Inverted Hammer / 8-EMA rollover -> HIGH CONVICTION PE BUY at resistance peak!
3. Follow-Through Law: 8-EMA close is NOT enough; next bar must break the trigger price.
4. "Respect the Doji" Exit Rule: Doji is an EXIT signal, not entry.
   - Doji at High = Exit all Call positions immediately (Target: Day Low).
   - Doji at Low = Exit all Put positions immediately (Target: Day High).
5. 100-Point Institutional Strikes: 24,000, 24,100, 24,200, 24,300 tracked as psychological barriers.
6. Disciplined 20-40 Point Targets:
   - Target 1 (+20 pts): Trail Stop Loss to Cost (Breakeven).
   - Target 2 (+40 to +65 pts): Full exit. Daily goal met.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import pandas as pd
import numpy as np


@dataclass
class DojiExitAlert:
    side: str                   # "EXIT_CE" | "EXIT_PE"
    title: str
    message: str
    candle_ts: str
    level: float
    invalidation: float
    target: float


@dataclass
class SniperSetup:
    side: str                   # "TOP_SHORT" | "BOTTOM_LONG"
    status: str                 # "TRIGGERED" | "ARMED" | "MONITORING" | "TARGET 1 HIT (+20 PTS)" | "TARGET 2 HIT (+40/65 PTS)" | "STOPPED OUT" | "INVALIDATED"
    title: str                  # e.g. "Gap-Up Dip Buy at Proven Support (CE)"
    pattern_name: str           # e.g. "15m Hammer + 8 EMA Bounce", "Inverted Hammer"
    timeframe: str              # "15m" | "5m"
    candle_ts: str              # Time pattern formed
    current_spot: float         # Current spot price
    trigger_price: float | None # Exact entry price
    stop_loss: float | None     # Exact SL price
    risk_pts: float | None      # Stop loss distance in points (10-15 pts)
    target_1: float | None      # Target 1 (+20 pts -> Trail to Cost)
    target_2: float | None      # Target 2 (+40 to +65 pts -> Bank Profit)
    reward_pts: float | None    # Target 2 gain in points
    risk_reward: str            # e.g. "1 : 3.5"
    live_pnl_pts: float | None  # Current running profit/loss in points
    follow_through: str         # "CONFIRMED" | "PENDING_NEXT_BAR" | "FAILED" | "NONE"
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
    gap_pts: float
    gap_type: str               # "GAP_UP" | "GAP_DOWN" | "FLAT"
    phase_code: str             # "PHASE_1_CHASE_TRAP" | "PHASE_2_SUPPORT_DIP" | "PHASE_2_RESISTANCE_RIP" | "BALANCED"
    phase_title: str
    phase_badge: str
    phase_badge_color: str
    phase_action: str
    key_support: float | None
    key_support_name: str
    key_resistance: float | None
    key_resistance_name: str
    top_setup: SniperSetup
    bottom_setup: SniperSetup
    doji_alert: DojiExitAlert | None = None


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
    r2: float | None = None,
    s2: float | None = None,
    pdh: float | None = None,
    pdl: float | None = None,
    pdc: float | None = None,
    round_step: int = 100,
) -> SniperReport:
    now = datetime.now()

    # Primary timeframe selection: 15m preferred for noise reduction, 5m fallback
    use_15m = (df_15m is not None and not df_15m.empty and len(df_15m) >= 3)
    tf_label = "15m" if use_15m else "5m"
    d_raw = df_15m if use_15m else df_5m

    if d_raw is None or d_raw.empty:
        dummy_top = SniperSetup(
            side="TOP_SHORT", status="MONITORING", title="Top Reversal Hunter",
            pattern_name="No Pattern", timeframe="15m", candle_ts="—", current_spot=spot,
            trigger_price=None, stop_loss=None, risk_pts=None, target_1=None, target_2=None,
            reward_pts=40.0, risk_reward="1 : 3.0", live_pnl_pts=None, follow_through="NONE",
            confluences=["Waiting for live candles"], action_note="Awaiting live market data..."
        )
        dummy_bot = SniperSetup(
            side="BOTTOM_LONG", status="MONITORING", title="Bottom Reversal Hunter",
            pattern_name="No Pattern", timeframe="15m", candle_ts="—", current_spot=spot,
            trigger_price=None, stop_loss=None, risk_pts=None, target_1=None, target_2=None,
            reward_pts=40.0, risk_reward="1 : 3.0", live_pnl_pts=None, follow_through="NONE",
            confluences=["Waiting for live candles"], action_note="Awaiting live market data..."
        )
        return SniperReport(
            eval_time=now, spot=spot, day_high=spot, day_low=spot,
            dh_dist=0.0, dl_dist=0.0, gap_pts=0.0, gap_type="FLAT",
            phase_code="BALANCED", phase_title="Awaiting Data",
            phase_badge="STANDBY", phase_badge_color="#4b5563",
            phase_action="Awaiting live market data.",
            key_support=None, key_support_name="Support",
            key_resistance=None, key_resistance_name="Resistance",
            top_setup=dummy_top, bottom_setup=dummy_bot, doji_alert=None
        )

    today_dt = d_raw["timestamp"].dt.date.max()
    d_today = d_raw[d_raw["timestamp"].dt.date == today_dt].copy().reset_index(drop=True)
    if d_today.empty:
        d_today = d_raw.iloc[-10:].copy().reset_index(drop=True)

    d_today = _add_indicators(d_today)

    dh = float(d_today["high"].max())
    dl = float(d_today["low"].min())
    dh_dist = round(dh - spot, 1)
    dl_dist = round(spot - dl, 1)
    open_price = float(d_today["open"].iloc[0])

    # ── Gap & Two-Phase Session Calculation ──────────────────────────────────
    gap_pts = round(open_price - pdc, 1) if (pdc is not None and pdc > 0) else 0.0
    if gap_pts >= 35.0:
        gap_type = "GAP_UP"
    elif gap_pts <= -35.0:
        gap_type = "GAP_DOWN"
    else:
        gap_type = "FLAT"

    # Identify Nearest Key Support & Resistance
    support_candidates = []
    if pdc is not None:
        support_candidates.append((pdc, "PDC (Gap Fill)"))
    if pdl is not None:
        support_candidates.append((pdl, "PDL (Prev Day Low)"))
    if s1 is not None:
        support_candidates.append((s1, "S1 (Pivot Support)"))
    round_sup = (int(spot) // round_step) * round_step
    support_candidates.append((float(round_sup), f"{round_sup:,.0f} Round Strike"))

    # Closest support below or near spot
    support_candidates = sorted(support_candidates, key=lambda x: abs(spot - x[0]))
    key_sup_val, key_sup_name = support_candidates[0]

    res_candidates = []
    if pdh is not None:
        res_candidates.append((pdh, "PDH (Prev Day High)"))
    if pdc is not None and pdc > open_price:
        res_candidates.append((pdc, "PDC (Gap Fill)"))
    if r1 is not None:
        res_candidates.append((r1, "R1 (Pivot Resistance)"))
    round_res = ((int(spot) // round_step) + 1) * round_step
    res_candidates.append((float(round_res), f"{round_res:,.0f} Round Strike"))

    res_candidates = sorted(res_candidates, key=lambda x: abs(spot - x[0]))
    key_res_val, key_res_name = res_candidates[0]

    # Two-Phase Logic
    pullback_from_dh = dh - spot
    rally_from_dl = spot - dl

    if gap_type == "GAP_UP":
        # Has spot pulled back to major support (PDC, PDL, S1, or >= 45 pts from DH)?
        is_near_sup = (abs(spot - key_sup_val) <= 25.0) or (pullback_from_dh >= 45.0)
        if is_near_sup:
            phase_code = "PHASE_2_SUPPORT_DIP"
            phase_title = "PHASE 2: GAP-UP RETRACEMENT TO SUPPORT (PRO VALUE DIP BUY)"
            phase_badge = "💎 PRO CE BUY ZONE"
            phase_badge_color = "#10b981"
            phase_action = (
                f"Market cooled {pullback_from_dh:+.0f} pts from Day High. Spot is testing {key_sup_name} ({key_sup_val:,.1f}). "
                "DO NOT chase at the high — BUY CE HERE if 15m defends support with 8 EMA bounce & follow-through! "
                "Target: Day High / R1."
            )
        else:
            phase_code = "PHASE_1_CHASE_TRAP"
            phase_title = "PHASE 1: EXTENDED GAP-UP OPEN (ANTI-CHASING PROTOCOL)"
            phase_badge = "🚫 NO CALLS AT TOP"
            phase_badge_color = "#f59e0b"
            phase_action = (
                f"Gap Up +{gap_pts:.0f} pts. Spot is elevated near Day High ({dh:,.1f}). "
                "DO NOT BUY CALLS AT THE TOP into institutional profit taking. "
                f"Wait for pullback to {key_sup_name} ({key_sup_val:,.1f}) OR scan for Top Short (PE) if rejection forms."
            )
    elif gap_type == "GAP_DOWN":
        # Has spot rallied 60-100+ pts into resistance (PDH, PDC, R1)?
        is_near_res = (rally_from_dl >= 60.0) or (abs(spot - key_res_val) <= 25.0)
        if is_near_res:
            phase_code = "PHASE_2_RESISTANCE_RETEST"
            phase_title = "PHASE 2: RELIEF RALLY INTO HEAVY RESISTANCE (PRO FADE THE RIP)"
            phase_badge = "🎯 PRO PE BUY ZONE"
            phase_badge_color = "#ef4444"
            phase_action = (
                f"Market rallied +{rally_from_dl:.0f} pts from Day Low into {key_res_name} ({key_res_val:,.1f}). "
                "Tough barrier to break. DO NOT short the panic bottom — BUY PE HERE if 15m rejects resistance "
                "with 8 EMA rollover & follow-through! Target: Day Low."
            )
        else:
            phase_code = "PHASE_1_PANIC"
            phase_title = "PHASE 1: PANIC GAP-DOWN OPEN (ANTI-CHASING PROTOCOL)"
            phase_badge = "🚫 NO PUTS AT BOTTOM"
            phase_badge_color = "#f59e0b"
            phase_action = (
                f"Gap Down {gap_pts:.0f} pts. Spot is depressed near Day Low ({dl:,.1f}). "
                "DO NOT BUY PUTS AT THE BOTTOM into short-covering bounces. "
                f"Wait for relief rally to {key_res_name} ({key_res_val:,.1f}) OR scan for Bottom Long (CE) if absorption forms."
            )
    else:
        phase_code = "BALANCED"
        phase_title = "BALANCED MARKET: STANDARD 15m PRICE ACTION"
        phase_badge = "⚖️ RANGE SNIPER"
        phase_badge_color = "#3b82f6"
        phase_action = "Flat open. Standard 15m 8 EMA structure and Day High / Day Low boundary rules apply."

    # ── "Respect the Doji" Exit Advisory Scanner ─────────────────────────────
    doji_alert: DojiExitAlert | None = None
    recent_bars = d_today.tail(3)
    for _, rb in recent_bars.iterrows():
        b_rng = max(rb["high"] - rb["low"], 1.0)
        b_body = abs(rb["close"] - rb["open"])
        if b_body <= 0.18 * b_rng:
            rb_ts = pd.to_datetime(rb["timestamp"]).strftime("%H:%M")
            # Doji at High
            if (dh - rb["high"] <= 20.0) or (abs(rb["high"] - key_res_val) <= 15.0) or ("ubb" in rb and rb["high"] >= rb["ubb"] - 5.0):
                doji_alert = DojiExitAlert(
                    side="EXIT_CE",
                    title="⚠️ RESPECT THE DOJI: EXIT CALL (CE) POSITIONS NOW",
                    message=(
                        f"15m Doji printed at Day High / Resistance ({rb['high']:,.1f}) at {rb_ts}. "
                        "Course Rule: Doji is an EXIT signal, not entry! If you hold CE, take profits immediately. "
                        f"Target: Day Low ({dl:,.1f}) or PDC. Invalidation is Doji High ({rb['high']:,.1f})."
                    ),
                    candle_ts=rb_ts,
                    level=float(rb["high"]),
                    invalidation=float(rb["high"] + 1.5),
                    target=float(dl),
                )
                break
            # Doji at Low
            elif (rb["low"] - dl <= 20.0) or (abs(rb["low"] - key_sup_val) <= 15.0) or ("lbb" in rb and rb["low"] <= rb["lbb"] + 5.0):
                doji_alert = DojiExitAlert(
                    side="EXIT_PE",
                    title="⚠️ RESPECT THE DOJI: EXIT PUT (PE) POSITIONS NOW",
                    message=(
                        f"15m Doji printed at Day Low / Support ({rb['low']:,.1f}) at {rb_ts}. "
                        "Course Rule: Doji is an EXIT signal, not entry! If you hold PE, take profits immediately. "
                        f"Target: Day High ({dh:,.1f}) or PDC. Invalidation is Doji Low ({rb['low']:,.1f})."
                    ),
                    candle_ts=rb_ts,
                    level=float(rb["low"]),
                    invalidation=float(rb["low"] - 1.5),
                    target=float(dh),
                )
                break

    # ── Top Sniper (Short PE Setup) ──────────────────────────────────────────
    top_title = "Gap-Down Rip Fade at Tough Resistance (PE)" if phase_code == "PHASE_2_RESISTANCE_RETEST" else "Day Top Reversal & Resistance Exhaustion (PE)"
    top_setup = SniperSetup(
        side="TOP_SHORT", status="MONITORING", title=top_title, pattern_name="None",
        timeframe=tf_label, candle_ts="—", current_spot=spot, trigger_price=None,
        stop_loss=None, risk_pts=None, target_1=None, target_2=None, reward_pts=40.0,
        risk_reward="1 : 3.5", live_pnl_pts=None, follow_through="NONE", confluences=[],
        action_note=f"Spot is {dh_dist:+.0f} pts from Day High ({dh:,.1f}). Watch for 15m Inverted Hammer / 8 EMA rollover.",
    )

    # Scan for top reversal
    for i in range(len(d_today) - 1, 0, -1):
        c = d_today.iloc[i]
        c_prev = d_today.iloc[i - 1]
        body = abs(c["close"] - c["open"])
        rng = max(c["high"] - c["low"], 1.0)
        upper_wick = c["high"] - max(c["open"], c["close"])
        lower_wick = min(c["open"], c["close"]) - c["low"]

        # Reversal candle geometry
        is_inv_hammer = upper_wick >= 1.2 * body and upper_wick >= 0.35 * rng
        is_doji = body <= 0.16 * rng
        is_engulfing = (c["close"] < c["open"] and c_prev["close"] > c_prev["open"] and c["close"] < c_prev["open"])
        is_ema_rollover = c["close"] < c.get("ema8", c["close"]) and upper_wick >= 6.0

        # Location confluence
        is_near_dh = (dh - c["high"]) <= 25.0
        is_near_res_barrier = (abs(c["high"] - key_res_val) <= 25.0)
        round_100 = round(c["high"] / round_step) * round_step
        is_near_100 = abs(c["high"] - round_100) <= 20.0
        is_near_ubb = "ubb" in c and c["high"] >= c["ubb"] - 6.0
        is_swing_hi = (c["high"] >= d_today.iloc[max(0, i - 4):i + 1]["high"].max() - 1.0)

        if (is_near_dh or is_near_res_barrier or is_near_100 or is_near_ubb or is_swing_hi) and (is_inv_hammer or is_doji or is_engulfing or is_ema_rollover):
            pat_name = ("15m Inverted Hammer" if is_inv_hammer else
                        "15m Bearish Engulfing" if is_engulfing else
                        "15m Top Exhaustion Doji" if is_doji else "15m 8-EMA Rejection Wick")
            ts_str = pd.to_datetime(c["timestamp"]).strftime("%H:%M")
            trig = round(float(c["low"]) - 0.5, 2)
            sl = round(float(c["high"]) + 1.0, 2)
            risk = round(sl - trig, 2)
            t1 = round(trig - 20.0, 2)
            t2 = round(trig - 40.0, 2)

            top_setup.pattern_name = pat_name
            top_setup.candle_ts = ts_str
            top_setup.trigger_price = trig
            top_setup.stop_loss = sl
            top_setup.risk_pts = risk
            top_setup.target_1 = t1
            top_setup.target_2 = t2
            top_setup.reward_pts = 40.0
            top_setup.risk_reward = f"1 : {round(40.0 / max(risk, 1.0), 1)}"

            confl = []
            if is_near_dh:
                confl.append(f"Day High Zone ({dh:,.1f})")
            if is_near_res_barrier:
                confl.append(f"Major Resistance: {key_res_name} ({key_res_val:,.1f})")
            if is_near_100:
                confl.append(f"100-Pt Round Strike ({round_100:,.0f})")
            if is_near_ubb:
                confl.append("Upper Bollinger Band Rejection")
            if is_ema_rollover:
                confl.append("15m 8-EMA Rollover & Close Below")
            if phase_code == "PHASE_2_RESISTANCE_RETEST":
                confl.append(f"Gap-Down Relief Exhaustion (+{rally_from_dl:.0f} pts from Low)")
            top_setup.confluences = confl

            # Follow-Through Check
            subsequent = d_today.iloc[i + 1:] if i + 1 < len(d_today) else pd.DataFrame()
            if not subsequent.empty and (subsequent["low"].min() < trig):
                top_setup.follow_through = "CONFIRMED"
                pnl = round(trig - spot, 1)
                top_setup.live_pnl_pts = pnl
                if spot <= t2:
                    top_setup.status = "TARGET 2 HIT (+40 PTS)"
                    top_setup.action_note = f"🏆 Daily Target 2 (+40 pts) reached! Bank profits and protect capital. Done for the day!"
                elif spot <= t1:
                    top_setup.status = "TARGET 1 HIT (+20 PTS)"
                    top_setup.action_note = f"✅ Target 1 Hit (+20 pts). Follow course rule: TRAIL SL TO COST ({trig:,.1f}) to guarantee zero-risk runner!"
                elif spot > sl:
                    top_setup.status = "STOPPED OUT"
                    top_setup.action_note = f"Stop loss hit at {sl:,.1f}. Trade closed with strict {risk:.1f} pts risk."
                else:
                    top_setup.status = "TRIGGERED"
                    top_setup.action_note = f"⚡ Trade Active! Breakdown below {trig:,.1f} confirmed with follow-through. Running +{pnl:+.1f} pts. Strict SL at {sl:,.1f}."
            else:
                if spot > sl:
                    top_setup.status = "INVALIDATED"
                    top_setup.action_note = f"Price pushed above high {sl:,.1f}. Rejection invalidated."
                else:
                    top_setup.status = "ARMED"
                    top_setup.follow_through = "PENDING_NEXT_BAR"
                    top_setup.action_note = f"🚨 ARMED: {pat_name} printed. Awaiting 15m follow-through breakdown below {trig:,.1f}. SL: {sl:,.1f} ({risk:.1f} pts). Target 1: {t1:,.1f} (+20 pts)."
            break

    # ── Bottom Sniper (Long CE Setup) ────────────────────────────────────────
    bot_title = "Gap-Up Dip Buy at Proven Support (CE)" if phase_code == "PHASE_2_SUPPORT_DIP" else "Day Bottom Reversal & Support Accumulation (CE)"
    bottom_setup = SniperSetup(
        side="BOTTOM_LONG", status="MONITORING", title=bot_title, pattern_name="None",
        timeframe=tf_label, candle_ts="—", current_spot=spot, trigger_price=None,
        stop_loss=None, risk_pts=None, target_1=None, target_2=None, reward_pts=40.0,
        risk_reward="1 : 3.5", live_pnl_pts=None, follow_through="NONE", confluences=[],
        action_note=f"Spot is {dl_dist:+.0f} pts from Day Low ({dl:,.1f}). Watch for 15m Hammer / 8 EMA bounce.",
    )

    # Scan for bottom reversal
    for i in range(len(d_today) - 1, 0, -1):
        c = d_today.iloc[i]
        c_prev = d_today.iloc[i - 1]
        body = abs(c["close"] - c["open"])
        rng = max(c["high"] - c["low"], 1.0)
        upper_wick = c["high"] - max(c["open"], c["close"])
        lower_wick = min(c["open"], c["close"]) - c["low"]

        # Reversal candle geometry
        is_hammer = lower_wick >= 1.2 * body and lower_wick >= 0.35 * rng
        is_doji = body <= 0.16 * rng
        is_engulfing = (c["close"] > c["open"] and c_prev["close"] < c_prev["open"] and c["close"] > c_prev["open"])
        is_ema_bounce = c["close"] > c.get("ema8", c["close"]) and lower_wick >= 6.0

        # Location confluence
        is_near_dl = (c["low"] - dl) <= 25.0
        is_near_sup_barrier = (abs(c["low"] - key_sup_val) <= 25.0)
        round_100 = round(c["low"] / round_step) * round_step
        is_near_100 = abs(c["low"] - round_100) <= 20.0
        is_near_lbb = "lbb" in c and c["low"] <= c["lbb"] + 6.0
        is_swing_lo = (c["low"] <= d_today.iloc[max(0, i - 4):i + 1]["low"].min() + 1.0)

        if (is_near_dl or is_near_sup_barrier or is_near_100 or is_near_lbb or is_swing_lo) and (is_hammer or is_doji or is_engulfing or is_ema_bounce):
            pat_name = ("15m Hammer" if is_hammer else
                        "15m Bullish Engulfing" if is_engulfing else
                        "15m Bottom Accumulation Doji" if is_doji else "15m 8-EMA Absorption Wick")
            ts_str = pd.to_datetime(c["timestamp"]).strftime("%H:%M")
            trig = round(float(c["high"]) + 0.5, 2)
            sl = round(float(c["low"]) - 1.0, 2)
            risk = round(trig - sl, 2)
            t1 = round(trig + 20.0, 2)
            t2 = round(trig + 40.0, 2)

            bottom_setup.pattern_name = pat_name
            bottom_setup.candle_ts = ts_str
            bottom_setup.trigger_price = trig
            bottom_setup.stop_loss = sl
            bottom_setup.risk_pts = risk
            bottom_setup.target_1 = t1
            bottom_setup.target_2 = t2
            bottom_setup.reward_pts = 40.0
            bottom_setup.risk_reward = f"1 : {round(40.0 / max(risk, 1.0), 1)}"

            confl = []
            if is_near_dl:
                confl.append(f"Day Low Base ({dl:,.1f})")
            if is_near_sup_barrier:
                confl.append(f"Major Support: {key_sup_name} ({key_sup_val:,.1f})")
            if is_near_100:
                confl.append(f"100-Pt Round Strike ({round_100:,.0f})")
            if is_near_lbb:
                confl.append("Lower Bollinger Band Bounce")
            if is_ema_bounce:
                confl.append("15m 8-EMA Bounce & Close Above")
            if phase_code == "PHASE_2_SUPPORT_DIP":
                confl.append(f"Gap-Up Dip Support Retest (Pullback from High {pullback_from_dh:+.0f} pts)")
            bottom_setup.confluences = confl

            # Follow-Through Check
            subsequent = d_today.iloc[i + 1:] if i + 1 < len(d_today) else pd.DataFrame()
            if not subsequent.empty and (subsequent["high"].max() > trig):
                bottom_setup.follow_through = "CONFIRMED"
                pnl = round(spot - trig, 1)
                bottom_setup.live_pnl_pts = pnl
                if spot >= t2:
                    bottom_setup.status = "TARGET 2 HIT (+40 PTS)"
                    bottom_setup.action_note = f"🏆 Daily Target 2 (+40 pts) reached! Bank profits and protect capital. Done for the day!"
                elif spot >= t1:
                    bottom_setup.status = "TARGET 1 HIT (+20 PTS)"
                    bottom_setup.action_note = f"✅ Target 1 Hit (+20 pts). Follow course rule: TRAIL SL TO COST ({trig:,.1f}) to guarantee zero-risk runner!"
                elif spot < sl:
                    bottom_setup.status = "STOPPED OUT"
                    bottom_setup.action_note = f"Stop loss hit at {sl:,.1f}. Trade closed with strict {risk:.1f} pts risk."
                else:
                    bottom_setup.status = "TRIGGERED"
                    bottom_setup.action_note = f"⚡ Trade Active! Breakout above {trig:,.1f} confirmed with follow-through. Running +{pnl:+.1f} pts. Strict SL at {sl:,.1f}."
            else:
                if spot < sl:
                    bottom_setup.status = "INVALIDATED"
                    bottom_setup.action_note = f"Price dropped below low {sl:,.1f}. Bottom setup invalidated."
                else:
                    bottom_setup.status = "ARMED"
                    bottom_setup.follow_through = "PENDING_NEXT_BAR"
                    bottom_setup.action_note = f"🚨 ARMED: {pat_name} printed. Awaiting 15m follow-through breakout above {trig:,.1f}. SL: {sl:,.1f} ({risk:.1f} pts). Target 1: {t1:,.1f} (+20 pts)."
            break

    return SniperReport(
        eval_time=now, spot=spot, day_high=dh, day_low=dl,
        dh_dist=dh_dist, dl_dist=dl_dist, gap_pts=gap_pts, gap_type=gap_type,
        phase_code=phase_code, phase_title=phase_title,
        phase_badge=phase_badge, phase_badge_color=phase_badge_color,
        phase_action=phase_action,
        key_support=key_sup_val, key_support_name=key_sup_name,
        key_resistance=key_res_val, key_resistance_name=key_res_name,
        top_setup=top_setup, bottom_setup=bottom_setup, doji_alert=doji_alert,
    )


def render_sniper_card(report: SniperReport):
    import streamlit as st

    # ── 1. Header & Two-Phase Session Road Map ───────────────────────────────
    st.markdown(
        f"""
        <div style="background: #0f172a; border: 1.5px solid #334155; border-radius: 8px; padding: 12px 16px; margin-top: 10px; margin-bottom: 12px;">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px;">
                <span style="font-size: 1.05em; font-weight: 800; color: #F8FAFC; letter-spacing: 0.04em;">
                    🧭 2-PHASE SESSION ROAD MAP & GAP ANTI-TRAP PROTOCOL
                </span>
                <span style="background: {report.phase_badge_color}; color: #ffffff; padding: 3px 10px; border-radius: 4px; font-weight: 800; font-size: 0.82em; font-family: monospace;">
                    {report.phase_badge}
                </span>
            </div>
            <div style="font-size: 0.88em; color: #cbd5e1; margin-bottom: 6px;">
                <b>{report.phase_title}</b> · Open Gap: <b>{report.gap_pts:+.0f} pts</b> | Nearest Support: <b>{report.key_support_name} ({report.key_support:,.0f})</b> | Resistance: <b>{report.key_resistance_name} ({report.key_resistance:,.0f})</b>
            </div>
            <div style="font-size: 0.84em; color: #e2e8f0; background: #1e293b; padding: 8px 12px; border-radius: 6px; border-left: 3px solid {report.phase_badge_color};">
                {report.phase_action}
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # ── 2. "Respect the Doji" Emergency Exit Flasher ─────────────────────────
    if report.doji_alert:
        d_al = report.doji_alert
        border_col = "#eab308" if d_al.side == "EXIT_CE" else "#38bdf8"
        bg_col = "#713f1233" if d_al.side == "EXIT_CE" else "#0369a133"
        st.markdown(
            f"""
            <div style="background: {bg_col}; border: 1.5px solid {border_col}; border-radius: 8px; padding: 10px 14px; margin-bottom: 12px;">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px;">
                    <span style="font-size: 0.95em; font-weight: 800; color: {border_col};">
                        {d_al.title}
                    </span>
                    <span style="background: {border_col}; color: #000; padding: 2px 8px; border-radius: 4px; font-weight: 800; font-size: 0.78em; font-family: monospace;">
                        {d_al.candle_ts} PRINT
                    </span>
                </div>
                <div style="font-size: 0.84em; color: #fef08a;">
                    {d_al.message}
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    # ── 3. Two Sniper Cards (Short PE vs Long CE) ────────────────────────────
    c_top, c_bot = st.columns(2)

    # Top Sniper Card (Red / Short)
    with c_top:
        top = report.top_setup
        bg_color = "#3b0707" if "TRIGGERED" in top.status else ("#451a03" if top.status == "ARMED" else "#111827")
        border_color = "#ef4444" if "TRIGGERED" in top.status else ("#f59e0b" if top.status == "ARMED" else "#374151")
        badge_bg = "#dc2626" if "TRIGGERED" in top.status else ("#d97706" if top.status == "ARMED" else "#4b5563")

        trig_str = f"{top.trigger_price:,.2f}" if top.trigger_price else "—"
        sl_str = f"{top.stop_loss:,.2f} ({top.risk_pts:.1f} pts)" if top.stop_loss else "—"
        t1_str = f"{top.target_1:,.2f} (+20 pts · Trail Cost)" if top.target_1 else "—"
        t2_str = f"{top.target_2:,.2f} (+40 pts · Full Exit)" if top.target_2 else "—"
        confl_html = "".join(f"<li style='margin-bottom: 2px;'>{c}</li>" for c in top.confluences) or "<li>Scanning resistance & 100-pt strikes...</li>"

        st.markdown(
            f"""
            <div style="background: {bg_color}33; border: 1.5px solid {border_color}; border-radius: 8px; padding: 12px 16px; min-height: 250px;">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                    <span style="font-size: 1.02em; font-weight: 800; color: #f87171;">🔴 {top.title}</span>
                    <span style="background: {badge_bg}; color: #ffffff; padding: 3px 9px; border-radius: 4px; font-weight: 700; font-size: 0.8em; font-family: monospace;">
                        {top.status}
                    </span>
                </div>
                <div style="font-size: 0.86em; color: #e2e8f0; margin-bottom: 6px;">
                    <b>Pattern:</b> {top.pattern_name} @ {top.candle_ts} | <b>Follow-Through:</b> {top.follow_through}
                </div>
                <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 6px; font-size: 0.82em; background: #00000044; padding: 8px; border-radius: 6px; margin-bottom: 8px; font-family: monospace;">
                    <div><b>Sell Trigger:</b> <span style="color:#f87171;">{trig_str}</span></div>
                    <div><b>Stop Loss:</b> <span style="color:#cbd5e1;">{sl_str}</span></div>
                    <div><b>Target 1:</b> <span style="color:#34d399;">{t1_str}</span></div>
                    <div><b>Target 2:</b> <span style="color:#10b981; font-weight:700;">{t2_str}</span></div>
                </div>
                <div style="font-size: 0.8em; color: #94a3b8; margin-bottom: 6px;">
                    <ul style="padding-left: 16px; margin: 0;">{confl_html}</ul>
                </div>
                <div style="font-size: 0.82em; font-weight: 600; color: #fecaca; background: #991b1b33; padding: 6px 10px; border-radius: 4px; border-left: 3px solid #ef4444;">
                    {top.action_note}
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    # Bottom Sniper Card (Green / Long)
    with c_bot:
        bot = report.bottom_setup
        bg_color = "#022c22" if "TRIGGERED" in bot.status else ("#451a03" if bot.status == "ARMED" else "#111827")
        border_color = "#10b981" if "TRIGGERED" in bot.status else ("#f59e0b" if bot.status == "ARMED" else "#374151")
        badge_bg = "#059669" if "TRIGGERED" in bot.status else ("#d97706" if bot.status == "ARMED" else "#4b5563")

        trig_str = f"{bot.trigger_price:,.2f}" if bot.trigger_price else "—"
        sl_str = f"{bot.stop_loss:,.2f} ({bot.risk_pts:.1f} pts)" if bot.stop_loss else "—"
        t1_str = f"{bot.target_1:,.2f} (+20 pts · Trail Cost)" if bot.target_1 else "—"
        t2_str = f"{bot.target_2:,.2f} (+40 pts · Full Exit)" if bot.target_2 else "—"
        confl_html = "".join(f"<li style='margin-bottom: 2px;'>{c}</li>" for c in bot.confluences) or "<li>Scanning support & 100-pt strikes...</li>"

        st.markdown(
            f"""
            <div style="background: {bg_color}33; border: 1.5px solid {border_color}; border-radius: 8px; padding: 12px 16px; min-height: 250px;">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                    <span style="font-size: 1.02em; font-weight: 800; color: #34d399;">🟢 {bot.title}</span>
                    <span style="background: {badge_bg}; color: #ffffff; padding: 3px 9px; border-radius: 4px; font-weight: 700; font-size: 0.8em; font-family: monospace;">
                        {bot.status}
                    </span>
                </div>
                <div style="font-size: 0.86em; color: #e2e8f0; margin-bottom: 6px;">
                    <b>Pattern:</b> {bot.pattern_name} @ {bot.candle_ts} | <b>Follow-Through:</b> {bot.follow_through}
                </div>
                <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 6px; font-size: 0.82em; background: #00000044; padding: 8px; border-radius: 6px; margin-bottom: 8px; font-family: monospace;">
                    <div><b>Buy Trigger:</b> <span style="color:#34d399;">{trig_str}</span></div>
                    <div><b>Stop Loss:</b> <span style="color:#cbd5e1;">{sl_str}</span></div>
                    <div><b>Target 1:</b> <span style="color:#34d399;">{t1_str}</span></div>
                    <div><b>Target 2:</b> <span style="color:#10b981; font-weight:700;">{t2_str}</span></div>
                </div>
                <div style="font-size: 0.8em; color: #94a3b8; margin-bottom: 6px;">
                    <ul style="padding-left: 16px; margin: 0;">{confl_html}</ul>
                </div>
                <div style="font-size: 0.82em; font-weight: 600; color: #a7f3d0; background: #065f4633; padding: 6px 10px; border-radius: 4px; border-left: 3px solid #10b981;">
                    {bot.action_note}
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )