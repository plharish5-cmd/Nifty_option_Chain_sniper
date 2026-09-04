"""
red_bar_strategy.py — Dr. Devendra Pratap Singh's Advanced Red Bar Trading Strategy.

CORE METHODOLOGY:
─────────────────
1. The "X-Candle" Principle:
   - First 5-minute candle (09:15-09:20) is an emotional trap ("X-candle"). Never trade it impulsively.
   - Extract the Point of Balance (POB) Zone using Fibonacci mean and 6% standard deviations:
       * POB Mean  = Low + 0.50 * Range
       * POB Lower = Low + 0.44 * Range
       * POB Upper = Low + 0.56 * Range
   - If price holds below POB Lower -> Sellers dominate (Bearish).
   - If price holds above POB Upper -> Buyers dominate (Bullish).
   - Trade ONLY on RETEST of the POB zone.

2. The 7 Setup Matrix:
   - Setup 1 (Opening POB Retest): 09:20 - 10:15. Pullback into [0.44, 0.56] zone with directional rejection.
   - Setup 2 (Red Bar Theory): 09:20 - 11:15. Institutional follow-up Red Bar (body >= 50% of range, range >= 20 pts).
     Retest of its POB zone from below -> Buy PE.
   - Setup 3 (Mean Deflection): 10:30 - 12:30. 150-min law (5 EMA on 30m == 30 EMA on 5m).
     When price is deflected >= 35 pts away for 4+ candles, mark the "Barricade".
     A break back through the barricade triggers a mean-reversion trade to the 30 EMA.
   - Setup 4 (Parallel Channel Squeeze): 11:30 - 12:15. Channel drawn on morning swing highs/lows.
     Breakout triggers pre-Europe expansion.
   - Setup 5 (Asian Liquidity Hunt): 12:00 - 13:45. Asian Range (09:15 - 12:45) High and Low.
     European session opens ~12:30. Liquidity sweep of Asian High or Low triggers sharp reversal.
   - Setup 6 (CPR Static Mean): Daily CPR as gravitational anchor.
   - Setup 7 (Institutional Order Zones): Prior unmitigated session footprints.

3. Option Buying SOP:
   - Contract Selection: At-The-Money (ATM) contract (Delta ~0.50) to capture gamma expansion without excessive theta burn.
   - Stop Loss: Strictly technical (Candle extreme or POB line). Never arbitrary.
   - Risk-to-Reward: Target 1 (1:1), Target 2 (1:1.5), Target 3 (1:2).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from typing import Dict, List, Optional, Tuple
import numpy as np
import pandas as pd

import config

NIFTY_LOT_SIZE = getattr(config, "NIFTY_LOT_SIZE", 75)
MIN_RISK_PTS = 5.0
MAX_RISK_PTS = 45.0
BUFFER_PTS = 2.0


@dataclass
class POBZone:
    candle_high: float
    candle_low: float
    candle_range: float
    pob_mean: float      # 0.50
    pob_lower: float     # 0.44
    pob_upper: float     # 0.56

    @classmethod
    def from_candle(cls, high: float, low: float) -> POBZone:
        rng = max(high - low, 0.05)
        return cls(
            candle_high=high,
            candle_low=low,
            candle_range=rng,
            pob_mean=round(low + 0.50 * rng, 2),
            pob_lower=round(low + 0.44 * rng, 2),
            pob_upper=round(low + 0.56 * rng, 2),
        )


@dataclass
class RedBarSignal:
    timestamp: datetime
    setup_name: str       # SETUP_1_POB_RETEST | SETUP_2_RED_BAR | SETUP_3_EMA_DEFLECTION | SETUP_5_ASIAN_SWEEP
    direction: str        # BULLISH | BEARISH
    action: str           # BUY_CE | BUY_PE
    entry_price: float
    stop_loss: float
    target_1: float       # 1:1 R:R
    target_2: float       # 1:1.5 R:R
    target_3: float       # 1:2.0 R:R
    risk_pts: float
    reward_t1_pts: float
    reward_t2_pts: float
    reward_t3_pts: float
    pob_zone: Optional[POBZone] = None
    notes: str = ""

    def summary(self) -> str:
        arrow = "🟢 CALL (BUY_CE)" if self.direction == "BULLISH" else "🔴 PUT (BUY_PE)"
        return (
            f"[{self.timestamp:%H:%M}] {arrow} | {self.setup_name}\n"
            f"  Entry: {self.entry_price:,.1f} | SL: {self.stop_loss:,.1f} (Risk: {self.risk_pts:,.1f} pts)\n"
            f"  T1 (1:1): {self.target_1:,.1f} | T2 (1:1.5): {self.target_2:,.1f} | T3 (1:2): {self.target_3:,.1f}\n"
            f"  {self.notes}"
        )


def calculate_pob(high: float, low: float) -> POBZone:
    """Computes the Point of Balance zone (0.44, 0.50, 0.56) for a given candle."""
    return POBZone.from_candle(high, low)


def detect_setup_1_pob_retest(df: pd.DataFrame) -> List[RedBarSignal]:
    """
    Setup 1: First 5-Minute "X-Candle" POB Retest.
    - Candle 1 (09:15) sets the POB.
    - Price breaks out of POB, establishing directional bias.
    - A subsequent candle pulls back to touch [POB_lower, POB_upper] and rejects.
    """
    signals: List[RedBarSignal] = []
    if df is None or len(df) < 6:
        return signals

    c1 = df.iloc[0]
    pob = calculate_pob(float(c1["high"]), float(c1["low"]))
    if pob.candle_range < 10.0:
        return signals  # Ignore ultra-narrow noise candles

    trend = None
    # Look across candles 2 to 14 (09:20 to 10:25)
    max_scan = min(15, len(df))
    for i in range(1, max_scan):
        row = df.iloc[i]
        c = float(row["close"])
        h = float(row["high"])
        l = float(row["low"])
        t = pd.to_datetime(row["timestamp"])

        if trend is None:
            if c < pob.pob_lower:
                trend = "BEARISH"
            elif c > pob.pob_upper:
                trend = "BULLISH"
        else:
            # Bearish Retest: touches POB zone from below, rejects and closes below pob_lower
            if trend == "BEARISH" and h >= pob.pob_lower and c <= pob.pob_lower:
                entry = c
                sl = max(h, pob.pob_upper) + BUFFER_PTS
                risk = sl - entry
                if MIN_RISK_PTS <= risk <= MAX_RISK_PTS:
                    signals.append(RedBarSignal(
                        timestamp=t,
                        setup_name="SETUP_1_POB_RETEST",
                        direction="BEARISH",
                        action="BUY_PE",
                        entry_price=round(entry, 2),
                        stop_loss=round(sl, 2),
                        target_1=round(entry - 1.0 * risk, 2),
                        target_2=round(entry - 1.5 * risk, 2),
                        target_3=round(entry - 2.0 * risk, 2),
                        risk_pts=round(risk, 2),
                        reward_t1_pts=round(1.0 * risk, 2),
                        reward_t2_pts=round(1.5 * risk, 2),
                        reward_t3_pts=round(2.0 * risk, 2),
                        pob_zone=pob,
                        notes=f"Bearish retest of Candle 1 POB ({pob.pob_mean:,.1f}) from below."
                    ))
                    break  # One Setup 1 per day

            # Bullish Retest: touches POB zone from above, bounces and closes above pob_upper
            elif trend == "BULLISH" and l <= pob.pob_upper and c >= pob.pob_upper:
                entry = c
                sl = min(l, pob.pob_lower) - BUFFER_PTS
                risk = entry - sl
                if MIN_RISK_PTS <= risk <= MAX_RISK_PTS:
                    signals.append(RedBarSignal(
                        timestamp=t,
                        setup_name="SETUP_1_POB_RETEST",
                        direction="BULLISH",
                        action="BUY_CE",
                        entry_price=round(entry, 2),
                        stop_loss=round(sl, 2),
                        target_1=round(entry + 1.0 * risk, 2),
                        target_2=round(entry + 1.5 * risk, 2),
                        target_3=round(entry + 2.0 * risk, 2),
                        risk_pts=round(risk, 2),
                        reward_t1_pts=round(1.0 * risk, 2),
                        reward_t2_pts=round(1.5 * risk, 2),
                        reward_t3_pts=round(2.0 * risk, 2),
                        pob_zone=pob,
                        notes=f"Bullish retest of Candle 1 POB ({pob.pob_mean:,.1f}) from above."
                    ))
                    break

    return signals


def detect_setup_2_red_bar(df: pd.DataFrame) -> List[RedBarSignal]:
    """
    Setup 2: Institutional Follow-up Red Bar Theory.
    - An institutional bearish candle (body >= 50% of range, range >= 20 pts).
    - Occurs below Candle 1 POB.
    - Entry on pullback to Red Bar's POB (0.50 mean).
    """
    signals: List[RedBarSignal] = []
    if df is None or len(df) < 10:
        return signals

    c1 = df.iloc[0]
    c1_pob = calculate_pob(float(c1["high"]), float(c1["low"]))

    # Scan candles 2 to 22 (09:20 to 11:00)
    max_scan = min(22, len(df))
    for i in range(1, max_scan):
        row = df.iloc[i]
        o = float(row["open"])
        c = float(row["close"])
        h = float(row["high"])
        l = float(row["low"])
        rng = h - l
        body = o - c

        # Institutional Red Bar criteria
        if body > 0 and (body / max(rng, 0.05)) >= 0.50 and rng >= 20.0 and c < c1_pob.pob_mean:
            rb_pob = calculate_pob(h, l)
            # Scan next 2 to 7 candles for retest of rb_pob
            for j in range(i + 1, min(i + 8, len(df))):
                sub_r = df.iloc[j]
                sub_h = float(sub_r["high"])
                sub_c = float(sub_r["close"])
                sub_t = pd.to_datetime(sub_r["timestamp"])

                # Retest of Red Bar POB from below
                if sub_h >= rb_pob.pob_lower and sub_c <= rb_pob.pob_upper:
                    entry = sub_c
                    sl = h + BUFFER_PTS
                    risk = sl - entry
                    if MIN_RISK_PTS <= risk <= MAX_RISK_PTS:
                        signals.append(RedBarSignal(
                            timestamp=sub_t,
                            setup_name="SETUP_2_RED_BAR",
                            direction="BEARISH",
                            action="BUY_PE",
                            entry_price=round(entry, 2),
                            stop_loss=round(sl, 2),
                            target_1=round(entry - 1.0 * risk, 2),
                            target_2=round(entry - 1.5 * risk, 2),
                            target_3=round(entry - 2.0 * risk, 2),
                            risk_pts=round(risk, 2),
                            reward_t1_pts=round(1.0 * risk, 2),
                            reward_t2_pts=round(1.5 * risk, 2),
                            reward_t3_pts=round(2.0 * risk, 2),
                            pob_zone=rb_pob,
                            notes=f"Retest of Institutional Red Bar at {h:,.1f}-{l:,.1f} (POB {rb_pob.pob_mean:,.1f})."
                        ))
                        return signals  # One Red Bar trade per session
            break

    return signals


def detect_setup_3_ema_deflection(df: pd.DataFrame) -> List[RedBarSignal]:
    """
    Setup 3: Mean Reversion / Deflection from 150-Min Mean (30 EMA on 5m / 5 EMA on 30m).
    - Price deflects >= 35 pts away from 30 EMA for 4+ consecutive candles.
    - Mark the Barricade (extreme high of lowest candles for calls, extreme low for puts).
    - Breakout above/below barricade triggers mean reversion trade targeting the 30 EMA.
    """
    signals: List[RedBarSignal] = []
    if df is None or len(df) < 15:
        return signals

    if "ema30" not in df.columns:
        df = df.copy()
        df["ema30"] = df["close"].ewm(span=30, adjust=False).mean()

    max_scan = min(50, len(df))
    for i in range(8, max_scan):
        sub = df.iloc[max(0, i - 4):i + 1]
        t = pd.to_datetime(df.iloc[i]["timestamp"])

        # Downward deflection (Price plunged far below 30 EMA)
        if (sub["high"] < sub["ema30"]).all():
            dist = float(sub["ema30"].iloc[-1] - sub["close"].iloc[-1])
            if dist >= 35.0:
                barricade = float(sub["high"].iloc[-2:].max())
                if i + 1 < len(df):
                    next_r = df.iloc[i + 1]
                    next_c = float(next_r["close"])
                    next_ema = float(next_r["ema30"])
                    next_t = pd.to_datetime(next_r["timestamp"])

                    if next_c > barricade:
                        entry = next_c
                        sl = float(sub["low"].min()) - BUFFER_PTS
                        risk = entry - sl
                        target = next_ema
                        reward = target - entry
                        if MIN_RISK_PTS <= risk <= MAX_RISK_PTS and reward >= 15.0:
                            signals.append(RedBarSignal(
                                timestamp=next_t,
                                setup_name="SETUP_3_EMA_DEFLECTION",
                                direction="BULLISH",
                                action="BUY_CE",
                                entry_price=round(entry, 2),
                                stop_loss=round(sl, 2),
                                target_1=round(target, 2),
                                target_2=round(entry + 1.5 * risk, 2),
                                target_3=round(entry + 2.0 * risk, 2),
                                risk_pts=round(risk, 2),
                                reward_t1_pts=round(reward, 2),
                                reward_t2_pts=round(1.5 * risk, 2),
                                reward_t3_pts=round(2.0 * risk, 2),
                                notes=f"Downward deflection of {dist:.1f} pts from 30 EMA. Barricade {barricade:,.1f} broken."
                            ))
                            break

        # Upward deflection (Price rocketed far above 30 EMA)
        elif (sub["low"] > sub["ema30"]).all():
            dist = float(sub["close"].iloc[-1] - sub["ema30"].iloc[-1])
            if dist >= 35.0:
                barricade = float(sub["low"].iloc[-2:].min())
                if i + 1 < len(df):
                    next_r = df.iloc[i + 1]
                    next_c = float(next_r["close"])
                    next_ema = float(next_r["ema30"])
                    next_t = pd.to_datetime(next_r["timestamp"])

                    if next_c < barricade:
                        entry = next_c
                        sl = float(sub["high"].max()) + BUFFER_PTS
                        risk = sl - entry
                        target = next_ema
                        reward = entry - target
                        if MIN_RISK_PTS <= risk <= MAX_RISK_PTS and reward >= 15.0:
                            signals.append(RedBarSignal(
                                timestamp=next_t,
                                setup_name="SETUP_3_EMA_DEFLECTION",
                                direction="BEARISH",
                                action="BUY_PE",
                                entry_price=round(entry, 2),
                                stop_loss=round(sl, 2),
                                target_1=round(target, 2),
                                target_2=round(entry - 1.5 * risk, 2),
                                target_3=round(entry - 2.0 * risk, 2),
                                risk_pts=round(risk, 2),
                                reward_t1_pts=round(reward, 2),
                                reward_t2_pts=round(1.5 * risk, 2),
                                reward_t3_pts=round(2.0 * risk, 2),
                                notes=f"Upward deflection of {dist:.1f} pts from 30 EMA. Barricade {barricade:,.1f} broken."
                            ))
                            break

    return signals


def detect_setup_5_asian_sweep(df: pd.DataFrame) -> List[RedBarSignal]:
    """
    Setup 5: Asian Liquidity Sweep (12:00 - 13:45).
    - Asian Session: 09:15 to 12:45. Mark Asian High and Asian Low.
    - European Session Open (12:30 - 13:45) sweeps Asian High or Low liquidity and reclaims the range.
    - Squeeze reversal trade with 1:1.5 R:R targeting the opposite boundary.
    """
    signals: List[RedBarSignal] = []
    if df is None or len(df) < 40:
        return signals

    ts_series = pd.to_datetime(df["timestamp"])
    times = ts_series.dt.time

    # Asian session
    asian_mask = times <= time(12, 45)
    asian_df = df[asian_mask]
    if len(asian_df) < 35:
        return signals

    h_asia = float(asian_df["high"].max())
    l_asia = float(asian_df["low"].min())
    rng_asia = h_asia - l_asia
    if rng_asia < 40.0:
        return signals  # Asian range too tight

    # European entry window: 12:45 to 14:15
    post_mask = (times > time(12, 45)) & (times <= time(14, 15))
    post_df = df[post_mask]

    for idx, row in post_df.iterrows():
        c = float(row["close"])
        h = float(row["high"])
        l = float(row["low"])
        t = pd.to_datetime(row["timestamp"])

        # Bullish sweep of Asian Low (pierces L_asia, reclaims inside)
        if l < l_asia and c > l_asia:
            entry = c
            sl = l - BUFFER_PTS
            risk = entry - sl
            if MIN_RISK_PTS <= risk <= 35.0:
                signals.append(RedBarSignal(
                    timestamp=t,
                    setup_name="SETUP_5_ASIAN_SWEEP",
                    direction="BULLISH",
                    action="BUY_CE",
                    entry_price=round(entry, 2),
                    stop_loss=round(sl, 2),
                    target_1=round(entry + 1.0 * risk, 2),
                    target_2=round(entry + 1.5 * risk, 2),
                    target_3=round(entry + 2.0 * risk, 2),
                    risk_pts=round(risk, 2),
                    reward_t1_pts=round(1.0 * risk, 2),
                    reward_t2_pts=round(1.5 * risk, 2),
                    reward_t3_pts=round(2.0 * risk, 2),
                    notes=f"Liquidity sweep of Asian Low ({l_asia:,.1f}) at {t:%H:%M}. Wick low: {l:,.1f}."
                ))
                break  # One sweep per session

        # Bearish sweep of Asian High (pierces H_asia, reclaims inside)
        elif h > h_asia and c < h_asia:
            entry = c
            sl = h + BUFFER_PTS
            risk = sl - entry
            if MIN_RISK_PTS <= risk <= 35.0:
                signals.append(RedBarSignal(
                    timestamp=t,
                    setup_name="SETUP_5_ASIAN_SWEEP",
                    direction="BEARISH",
                    action="BUY_PE",
                    entry_price=round(entry, 2),
                    stop_loss=round(sl, 2),
                    target_1=round(entry - 1.0 * risk, 2),
                    target_2=round(entry - 1.5 * risk, 2),
                    target_3=round(entry - 2.0 * risk, 2),
                    risk_pts=round(risk, 2),
                    reward_t1_pts=round(1.0 * risk, 2),
                    reward_t2_pts=round(1.5 * risk, 2),
                    reward_t3_pts=round(2.0 * risk, 2),
                    notes=f"Liquidity sweep of Asian High ({h_asia:,.1f}) at {t:%H:%M}. Wick high: {h:,.1f}."
                ))
                break

    return signals


def scan_all_red_bar_setups(df: pd.DataFrame) -> List[RedBarSignal]:
    """Scans for all active Red Bar setups across a session's 5-minute candle series."""
    all_signals: List[RedBarSignal] = []
    all_signals.extend(detect_setup_1_pob_retest(df))
    all_signals.extend(detect_setup_2_red_bar(df))
    all_signals.extend(detect_setup_3_ema_deflection(df))
    all_signals.extend(detect_setup_5_asian_sweep(df))
    # Sort signals by timestamp
    all_signals.sort(key=lambda s: s.timestamp)
    return all_signals


def select_atm_option(
    chain_df: Optional[pd.DataFrame],
    spot: float,
    direction: str,
) -> Dict[str, Any]:
    """
    Selects the optimal At-The-Money (ATM) option contract for option buying.
    Dr. Devendra: ATM strike provides Delta ~0.50, optimal gamma acceleration,
    and balanced capital risk.
    """
    atm_strike = round(spot / 50.0) * 50.0
    is_ce = direction == "BUY_CE"
    opt_type = "CE" if is_ce else "PE"
    symbol = f"NIFTY {int(atm_strike)} {opt_type}"

    res = {
        "strike": atm_strike,
        "type": opt_type,
        "symbol": symbol,
        "ltp": 0.0,
        "oi": 0,
        "iv": 0.0,
        "delta": 0.50,
        "lot_size": NIFTY_LOT_SIZE,
    }

    if chain_df is not None and not chain_df.empty:
        col_ltp = "ce_ltp" if is_ce else "pe_ltp"
        col_oi = "ce_oi" if is_ce else "pe_oi"
        col_iv = "ce_iv" if is_ce else "pe_iv"

        row = chain_df[chain_df["strike"] == atm_strike]
        if not row.empty:
            res["ltp"] = float(row[col_ltp].iloc[0] or 0.0)
            res["oi"] = int(row.get(col_oi, 0).iloc[0] or 0)
            res["iv"] = float(row.get(col_iv, 0.0).iloc[0] or 0.0)
        else:
            # Pick closest strike with valid LTP
            valid = chain_df[chain_df[col_ltp] > 0]
            if not valid.empty:
                closest = valid.iloc[(valid["strike"] - atm_strike).abs().argsort()[:1]]
                res["strike"] = float(closest["strike"].iloc[0])
                res["symbol"] = f"NIFTY {int(res['strike'])} {opt_type}"
                res["ltp"] = float(closest[col_ltp].iloc[0] or 0.0)
                res["oi"] = int(closest.get(col_oi, 0).iloc[0] or 0)
                res["iv"] = float(closest.get(col_iv, 0.0).iloc[0] or 0.0)

    # Fallback estimate if LTP is missing from chain
    if res["ltp"] <= 0.0:
        res["ltp"] = 85.0  # realistic proxy for Nifty ATM option

    return res


def get_spoon_fed_signal(
    df_5m: Optional[pd.DataFrame],
    spot: float,
    chain_df: Optional[pd.DataFrame] = None,
    current_time: Optional[datetime] = None,
) -> Dict[str, Any]:
    """
    Produces a complete, definitive 'spoon-fed' trade instruction card for option buyers:
    - EXACT BUY LEVEL
    - EXACT STOP LOSS LEVEL
    - EXACT TARGET 1, 2, 3 LEVELS
    - RECOMMENDED OPTION STRIKE & ESTIMATED RUPEE PNL PER LOT
    - STEP-BY-STEP EXECUTION SOP
    """
    now = current_time or datetime.now()

    if df_5m is None or df_5m.empty or len(df_5m) < 1:
        return {
            "has_signal": False,
            "status": "NO_DATA",
            "headline": "Awaiting Market Feed",
            "action": "NO_TRADE",
            "message": "Market candle feed not yet initialized.",
        }

    # Extract foundational levels
    c1 = df_5m.iloc[0]
    pob = calculate_pob(float(c1["high"]), float(c1["low"]))

    # Asian session range (09:15 - 12:45)
    ts_series = pd.to_datetime(df_5m["timestamp"])
    times = ts_series.dt.time
    asian_df = df_5m[times <= time(12, 45)]
    h_asia = float(asian_df["high"].max()) if not asian_df.empty else float(df_5m["high"].max())
    l_asia = float(asian_df["low"].min()) if not asian_df.empty else float(df_5m["low"].min())

    # Scan for signals
    signals = scan_all_red_bar_setups(df_5m)

    if signals:
        # Prioritize Setup 5 (Asian Sweep - PF 3.02) or Setup 1 (POB Retest - 59% WR) or latest signal
        top_sig = signals[-1]  # Most recent active trigger
        for s in reversed(signals):
            if s.setup_name == "SETUP_5_ASIAN_SWEEP":
                top_sig = s
                break

        is_ce = top_sig.direction == "BULLISH"
        opt_info = select_atm_option(chain_df, spot, top_sig.action)
        opt_ltp = opt_info["ltp"]

        # Option PnL estimates (Delta ~ 0.50)
        opt_risk_pts = round(top_sig.risk_pts * 0.50, 1)
        opt_t1_pts = round(top_sig.reward_t1_pts * 0.50, 1)
        opt_t2_pts = round(top_sig.reward_t2_pts * 0.50, 1)
        opt_t3_pts = round(top_sig.reward_t3_pts * 0.50, 1)

        opt_sl_price = max(round(opt_ltp - opt_risk_pts, 1), 0.5)
        opt_t1_price = round(opt_ltp + opt_t1_pts, 1)
        opt_t2_price = round(opt_ltp + opt_t2_pts, 1)

        risk_rupees = round(opt_risk_pts * NIFTY_LOT_SIZE, 0)
        gain_t1_rupees = round(opt_t1_pts * NIFTY_LOT_SIZE, 0)
        gain_t2_rupees = round(opt_t2_pts * NIFTY_LOT_SIZE, 0)

        setup_display_name = {
            "SETUP_5_ASIAN_SWEEP": "Setup 5: European Open Asian Liquidity Sweep (PF 3.02)",
            "SETUP_1_POB_RETEST": "Setup 1: Candle 1 Point of Balance (POB) Retest (59% WR)",
            "SETUP_2_RED_BAR": "Setup 2: Institutional Follow-up Red Bar Retest",
            "SETUP_3_EMA_DEFLECTION": "Setup 3: 150-Min 30 EMA Mean Deflection Reversion",
        }.get(top_sig.setup_name, top_sig.setup_name)

        return {
            "has_signal": True,
            "status": "ACTIVE_TRADE",
            "timestamp": top_sig.timestamp,
            "setup_name": setup_display_name,
            "action": top_sig.action,
            "action_text": "BUY CALL (CE)" if is_ce else "BUY PUT (PE)",
            "direction": top_sig.direction,
            "spot_current": spot,
            "entry_level": top_sig.entry_price,
            "sl_level": top_sig.stop_loss,
            "target_1": top_sig.target_1,
            "target_2": top_sig.target_2,
            "target_3": top_sig.target_3,
            "risk_pts": top_sig.risk_pts,
            "reward_t1_pts": top_sig.reward_t1_pts,
            "reward_t2_pts": top_sig.reward_t2_pts,
            "reward_t3_pts": top_sig.reward_t3_pts,
            "opt_symbol": opt_info["symbol"],
            "opt_strike": opt_info["strike"],
            "opt_ltp": opt_ltp,
            "opt_sl_price": opt_sl_price,
            "opt_t1_price": opt_t1_price,
            "opt_t2_price": opt_t2_price,
            "opt_risk_pts": opt_risk_pts,
            "opt_t1_pts": opt_t1_pts,
            "opt_t2_pts": opt_t2_pts,
            "risk_rupees": risk_rupees,
            "gain_t1_rupees": gain_t1_rupees,
            "gain_t2_rupees": gain_t2_rupees,
            "pob_mean": pob.pob_mean,
            "pob_lower": pob.pob_lower,
            "pob_upper": pob.pob_upper,
            "asian_high": h_asia,
            "asian_low": l_asia,
            "notes": top_sig.notes,
            "sop_steps": [
                f"1. BUY ACTION: Enter {opt_info['symbol']} around ₹{opt_ltp:.1f} (Spot Entry: {top_sig.entry_price:,.1f}).",
                f"2. STOP LOSS: Hard SL on Spot at {top_sig.stop_loss:,.1f} (Option SL: ₹{opt_sl_price:.1f} | Max Risk: -₹{risk_rupees:,.0f} per lot).",
                f"3. TARGET 1 (1:1 Scalp): Book 50% profit at Spot {top_sig.target_1:,.1f} (Option Target: ₹{opt_t1_price:.1f} | +₹{gain_t1_rupees:,.0f} per lot).",
                f"4. BREAKEVEN RULE: Once Target 1 is hit, immediately move SL to Cost/Entry (₹{opt_ltp:.1f}).",
                f"5. TARGET 2 (Runner): Trail remaining 50% to Spot {top_sig.target_2:,.1f} (Option Target: ₹{opt_t2_price:.1f} | +₹{gain_t2_rupees:,.0f} per lot).",
            ],
        }

    # No active trigger right now -> provide exact watching state & trigger levels
    cur_t = times.iloc[-1]
    if cur_t < time(9, 20):
        headline = "Phase 1: First Candle Trap Identification (09:15 - 09:20)"
        advice = "DO NOT TRADE YET. Let the 09:15 candle close to plot the Point of Balance (POB)."
    elif cur_t <= time(10, 30):
        headline = f"Phase 2: Watching POB Retest Zone ({pob.pob_lower:,.1f} - {pob.pob_upper:,.1f})"
        advice = (
            f"• For BUY CALL (CE): Wait for price to pull back into {pob.pob_upper:,.1f} and reject upwards.\n"
            f"• For BUY PUT (PE): Wait for price to pull back into {pob.pob_lower:,.1f} and reject downwards."
        )
    elif cur_t < time(12, 45):
        headline = f"Phase 3: Asian Range Building (High: {h_asia:,.1f} | Low: {l_asia:,.1f})"
        advice = "Morning positioning is set. Keep hands off the keyboard until European market opens at 12:45 PM."
    elif cur_t <= time(14, 15):
        headline = f"Phase 4: European Liquidity Sweep Window (H: {h_asia:,.1f} | L: {l_asia:,.1f})"
        advice = (
            f"• BUY CALL TRIGGER: If price dips below Asian Low ({l_asia:,.1f}) and immediately wicks/closes back above, BUY ATM CE instantly.\n"
            f"• BUY PUT TRIGGER: If price pierces Asian High ({h_asia:,.1f}) and immediately wicks/closes back below, BUY ATM PE instantly."
        )
    else:
        headline = "Phase 5: Afternoon Wrap-up (No fresh entries)"
        advice = "Market closing window. Close open intraday scalp positions before 15:20."

    return {
        "has_signal": False,
        "status": "WATCHING_LEVELS",
        "headline": headline,
        "advice": advice,
        "spot_current": spot,
        "pob_mean": pob.pob_mean,
        "pob_lower": pob.pob_lower,
        "pob_upper": pob.pob_upper,
        "asian_high": h_asia,
        "asian_low": l_asia,
        "sop_steps": [
            f"• Candle 1 POB Mean: {pob.pob_mean:,.1f} (Zone: {pob.pob_lower:,.1f} - {pob.pob_upper:,.1f})",
            f"• Asian Session High (Resistance Liquidity): {h_asia:,.1f}",
            f"• Asian Session Low (Support Liquidity): {l_asia:,.1f}",
            "• Golden Rule: Never trade breakout wicks blindly. Wait for the 5m candle close and sweep confirmation.",
        ],
    }

