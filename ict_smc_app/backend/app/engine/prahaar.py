"""
engine/prahaar.py — Prahaar Indicator & ATM Option-Buying Engine.
Directional multi-indicator execution engine for NIFTY options combining:
  1. EMA Filter (Fast EMA 9/13 & Slow EMA 21/50 for trend alignment)
  2. MACD Filter (12, 26, 9 for momentum crossovers & histogram slope)
  3. ADX Filter (14-period Wilder's ADX >= 22 to filter out low-volatility chop)
  4. India VIX Filter (Optional toggle: checks expanding / stable VIX context)
  5. Dynamic ATM Strike Recommendations (50-point NIFTY step)

All thresholds loaded from rules.json ("prahaar" section).
Observation and alert-only (never places orders).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np

from .. import config
from ..timeutil import chart_time, hm
from .primitives import Bars

log = logging.getLogger(__name__)


def compute_ema(values: np.ndarray, period: int) -> np.ndarray:
    """Standard Exponential Moving Average."""
    out = np.empty_like(values, dtype=float)
    if len(values) == 0:
        return out
    alpha = 2.0 / (period + 1.0)
    out[0] = float(values[0])
    for i in range(1, len(values)):
        out[i] = alpha * float(values[i]) + (1.0 - alpha) * out[i - 1]
    return out


def compute_macd(
    close: np.ndarray, fast_period: int = 12, slow_period: int = 26, signal_period: int = 9
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Calculate MACD Line, Signal Line, and Histogram."""
    fast_ema = compute_ema(close, fast_period)
    slow_ema = compute_ema(close, slow_period)
    macd_line = fast_ema - slow_ema
    signal_line = compute_ema(macd_line, signal_period)
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def compute_adx(
    high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Wilder's DMI / ADX computation."""
    n = len(close)
    adx = np.zeros(n, dtype=float)
    plus_di = np.zeros(n, dtype=float)
    minus_di = np.zeros(n, dtype=float)
    if n < period + 1:
        return adx, plus_di, minus_di

    prev_c = np.r_[close[0], close[:-1]]
    tr = np.maximum(high - low, np.maximum(np.abs(high - prev_c), np.abs(low - prev_c)))

    up_move = high - np.r_[high[0], high[:-1]]
    down_move = np.r_[low[0], low[:-1]] - low

    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    smooth_tr = np.zeros(n, dtype=float)
    smooth_plus_dm = np.zeros(n, dtype=float)
    smooth_minus_dm = np.zeros(n, dtype=float)

    smooth_tr[period] = float(np.sum(tr[1 : period + 1]))
    smooth_plus_dm[period] = float(np.sum(plus_dm[1 : period + 1]))
    smooth_minus_dm[period] = float(np.sum(minus_dm[1 : period + 1]))

    for i in range(period + 1, n):
        smooth_tr[i] = smooth_tr[i - 1] - (smooth_tr[i - 1] / period) + tr[i]
        smooth_plus_dm[i] = smooth_plus_dm[i - 1] - (smooth_plus_dm[i - 1] / period) + plus_dm[i]
        smooth_minus_dm[i] = smooth_minus_dm[i - 1] - (smooth_minus_dm[i - 1] / period) + minus_dm[i]

    dx = np.zeros(n, dtype=float)
    for i in range(period, n):
        str_val = smooth_tr[i] if smooth_tr[i] > 1e-9 else 1e-9
        p_di = 100.0 * (smooth_plus_dm[i] / str_val)
        m_di = 100.0 * (smooth_minus_dm[i] / str_val)
        plus_di[i] = p_di
        minus_di[i] = m_di
        di_sum = p_di + m_di
        dx[i] = (100.0 * abs(p_di - m_di) / di_sum) if di_sum > 1e-4 else 0.0

    if n >= 2 * period:
        adx[2 * period - 1] = float(np.mean(dx[period : 2 * period]))
        for i in range(2 * period, n):
            adx[i] = (adx[i - 1] * (period - 1) + dx[i]) / period
    elif n > period:
        for i in range(period, n):
            adx[i] = float(np.mean(dx[period : i + 1]))

    return adx, plus_di, minus_di


@dataclass
class PrahaarSignal:
    candle_idx: int
    time: int
    time_str: str
    action: str              # "BUY ATM CALL" | "BUY ATM PUT"
    dir: str                 # "bull" | "bear"
    strike: int              # e.g. 24500
    option_type: str         # "CE" | "PE"
    option_symbol: str       # "24500 CE" | "24500 PE"
    entry: float
    sl: float
    tp: float
    rr: float
    adx: float
    macd: float
    macd_signal: float
    macd_hist: float
    fast_ema: float
    slow_ema: float
    vix: Optional[float] = None
    vix_ok: bool = True
    vix_reason: str = ""
    confluence_rank: str = "STANDARD"  # "STANDARD" | "A+" | "MASTER_SNIPER"
    reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "candle_idx": self.candle_idx,
            "time": self.time,
            "time_str": self.time_str,
            "action": self.action,
            "dir": self.dir,
            "strike": self.strike,
            "option_type": self.option_type,
            "option_symbol": self.option_symbol,
            "entry": round(self.entry, 2),
            "sl": round(self.sl, 2),
            "tp": round(self.tp, 2),
            "rr": round(self.rr, 2),
            "adx": round(self.adx, 2),
            "macd": round(self.macd, 2),
            "macd_signal": round(self.macd_signal, 2),
            "macd_hist": round(self.macd_hist, 2),
            "fast_ema": round(self.fast_ema, 2),
            "slow_ema": round(self.slow_ema, 2),
            "vix": round(self.vix, 2) if self.vix is not None else None,
            "vix_ok": self.vix_ok,
            "vix_reason": self.vix_reason,
            "confluence_rank": self.confluence_rank,
            "reasons": self.reasons,
        }


class PrahaarEngine:
    """
    Prahaar Indicator & ATM Option-Buying Directional Engine.
    Processes live or historical NIFTY spot/futures candles.
    """

    def __init__(self, rules_cfg: Optional[Dict[str, Any]] = None):
        cfg = rules_cfg or config.rules().get("prahaar", {})
        self.fast_ema_len = int(cfg.get("fast_ema_len", 9))
        self.slow_ema_len = int(cfg.get("slow_ema_len", 21))
        self.macd_fast = int(cfg.get("macd_fast", 12))
        self.macd_slow = int(cfg.get("macd_slow", 26))
        self.macd_signal = int(cfg.get("macd_signal", 9))
        self.adx_period = int(cfg.get("adx_period", 14))
        self.adx_threshold = float(cfg.get("adx_threshold", 22.0))
        self.vix_filter_enabled = bool(cfg.get("vix_filter_enabled", True))
        self.vix_min = float(cfg.get("vix_min", 10.0))
        self.vix_max = float(cfg.get("vix_max", 28.0))
        self.vix_drop_max = float(cfg.get("vix_drop_max", -1.5))
        self.strike_step = int(cfg.get("strike_step", 50))
        self.sl_atr_mult = float(cfg.get("sl_atr_mult", 1.5))
        self.tp_atr_mult = float(cfg.get("tp_atr_mult", 3.0))
        self.signal_cooldown = int(cfg.get("signal_cooldown_bars", 3))

    def check_vix(self, vix: Optional[float], vix_chg: Optional[float] = None) -> Tuple[bool, str]:
        """Verify India VIX filter for option buying."""
        if not self.vix_filter_enabled:
            return True, "VIX filter toggled off"
        if vix is None:
            return True, "VIX not provided (bypassed)"
        if vix < self.vix_min:
            return False, f"VIX {vix:.2f} < {self.vix_min:.1f} (low premium/expansion risk)"
        if vix > self.vix_max:
            return False, f"VIX {vix:.2f} > {self.vix_max:.1f} (extreme volatility / crush risk)"
        if vix_chg is not None and vix_chg < self.vix_drop_max:
            return False, f"VIX dropping sharply ({vix_chg:+.2f}) — risk of IV crush"
        return True, f"VIX {vix:.2f} supportive for option buying"

    def calculate_atm_strike(self, price: float) -> int:
        """Dynamic ATM strike rounded to nearest step (50 for NIFTY)."""
        return int(round(price / self.strike_step) * self.strike_step)

    def process(
        self,
        bars_or_candles: Union[Bars, List[Dict[str, Any]]],
        atr_arr: Optional[np.ndarray] = None,
        tf: str = "5m",
        vix: Optional[float] = None,
        vix_chg: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Evaluate full Prahaar matrix across closed candles.
        Returns signals, chart markers, diagnostics, and stats.
        """
        if isinstance(bars_or_candles, Bars):
            b = bars_or_candles
        elif isinstance(bars_or_candles, list):
            valid = [c for c in bars_or_candles if c.get("closed", True) is not False]
            if not valid:
                return {"available": False, "message": "No valid closed candles"}
            b = Bars.from_rows(valid)
        else:
            return {"available": False, "message": "Invalid bars format"}

        n = len(b)
        if n < 15:
            return {"available": False, "message": "Insufficient bars (<15) for Prahaar evaluation"}

        # Indicators
        close = b.c
        high = b.h
        low = b.l

        fast_ema = compute_ema(close, self.fast_ema_len)
        slow_ema = compute_ema(close, self.slow_ema_len)
        macd_line, sig_line, macd_hist = compute_macd(close, self.macd_fast, self.macd_slow, self.macd_signal)
        adx, plus_di, minus_di = compute_adx(high, low, close, self.adx_period)

        # ATR fallback
        if atr_arr is None or len(atr_arr) != n:
            pc = np.r_[close[0], close[:-1]]
            tr = np.maximum(high - low, np.maximum(np.abs(high - pc), np.abs(low - pc)))
            eff_atr = np.zeros(n, dtype=float)
            for k in range(n):
                eff_atr[k] = float(np.mean(tr[max(0, k - 13) : k + 1]))
        else:
            eff_atr = atr_arr

        vix_ok, vix_reason = self.check_vix(vix, vix_chg)

        signals: List[PrahaarSignal] = []
        markers: List[Dict[str, Any]] = []
        last_signal_idx: int = -999
        last_signal_dir: str = ""

        # Scan for directional triggers
        for i in range(1, n):
            c_val = float(close[i])
            f_ema = float(fast_ema[i])
            s_ema = float(slow_ema[i])
            m_val = float(macd_line[i])
            m_sig = float(sig_line[i])
            m_hst = float(macd_hist[i])
            prev_m = float(macd_line[i - 1])
            prev_sig = float(sig_line[i - 1])
            prev_hst = float(macd_hist[i - 1])
            cur_adx = float(adx[i])
            p_di = float(plus_di[i])
            m_di = float(minus_di[i])
            cur_atr = max(float(eff_atr[i]), 10.0)

            # Trend & Momentum conditions
            ema_bull = f_ema > s_ema and c_val >= f_ema
            ema_bear = f_ema < s_ema and c_val <= f_ema

            # MACD crossover in recent window (last 3-5 bars) or fresh histogram flip
            recent_cross_bull = False
            recent_cross_bear = False
            for look in range(max(1, i - 4), i + 1):
                if macd_line[look] >= sig_line[look] and macd_line[look - 1] < sig_line[look - 1]:
                    recent_cross_bull = True
                if macd_hist[look] > 0 and macd_hist[look - 1] <= 0:
                    recent_cross_bull = True
                if macd_line[look] <= sig_line[look] and macd_line[look - 1] > sig_line[look - 1]:
                    recent_cross_bear = True
                if macd_hist[look] < 0 and macd_hist[look - 1] >= 0:
                    recent_cross_bear = True

            # Also allow momentum expansion when ADX enters trend strength
            adx_just_trending = cur_adx >= self.adx_threshold and (i == 1 or float(adx[i - 1]) < self.adx_threshold)
            if not recent_cross_bull and m_val > m_sig and m_hst > 0 and (adx_just_trending or (i >= 2 and m_hst > prev_hst)):
                recent_cross_bull = True

            if not recent_cross_bear and m_val < m_sig and m_hst < 0 and (adx_just_trending or (i >= 2 and m_hst < prev_hst)):
                recent_cross_bear = True

            # ADX trend filter
            adx_active = cur_adx >= self.adx_threshold
            adx_bull = adx_active and p_di >= m_di
            adx_bear = adx_active and m_di >= p_di

            is_bull_trigger = ema_bull and recent_cross_bull and adx_bull and vix_ok
            is_bear_trigger = ema_bear and recent_cross_bear and adx_bear and vix_ok

            # Respect cooldown to avoid chart clutter
            if is_bull_trigger and (i - last_signal_idx >= self.signal_cooldown or last_signal_dir != "bull"):
                atm = self.calculate_atm_strike(c_val)
                sl = c_val - (self.sl_atr_mult * cur_atr)
                tp = c_val + (self.tp_atr_mult * cur_atr)
                rr = (tp - c_val) / max(c_val - sl, 1.0)
                ts_sec = int(b.ts[i])
                t_str = hm(ts_sec)

                reasons = [
                    f"EMA {self.fast_ema_len} > {self.slow_ema_len} Trend Bullish ({f_ema:.1f} > {s_ema:.1f})",
                    f"MACD Positive Momentum Crossover ({m_val:.2f} > {m_sig:.2f}, Hist {m_hst:+.2f})",
                    f"ADX Trend Strength Active ({cur_adx:.1f} >= {self.adx_threshold:.0f}, +DI > -DI)",
                    vix_reason,
                ]

                sig = PrahaarSignal(
                    candle_idx=i,
                    time=ts_sec,
                    time_str=t_str,
                    action="BUY ATM CALL",
                    dir="bull",
                    strike=atm,
                    option_type="CE",
                    option_symbol=f"{atm} CE",
                    entry=c_val,
                    sl=sl,
                    tp=tp,
                    rr=rr,
                    adx=cur_adx,
                    macd=m_val,
                    macd_signal=m_sig,
                    macd_hist=m_hst,
                    fast_ema=f_ema,
                    slow_ema=s_ema,
                    vix=vix,
                    vix_ok=vix_ok,
                    vix_reason=vix_reason,
                    reasons=reasons,
                )
                signals.append(sig)

                # Chart marker
                markers.append({
                    "time": chart_time(ts_sec),
                    "kind": "PRAHAAR_BUY_CE",
                    "dir": "bull",
                    "price": c_val,
                    "strike": atm,
                    "option_type": "CE",
                    "text": f"🟢 BUY {atm} CE (Prahaar)",
                    "short": f"🟢 BUY {atm} CE",
                    "at": t_str,
                    "adx": round(cur_adx, 1),
                    "macd_hist": round(m_hst, 2),
                    "full": f"Prahaar BUY ATM CALL: {atm} CE @ {c_val:.1f} · SL {sl:.1f} · TP {tp:.1f} (ADX {cur_adx:.1f})",
                })
                last_signal_idx = i
                last_signal_dir = "bull"

            elif is_bear_trigger and (i - last_signal_idx >= self.signal_cooldown or last_signal_dir != "bear"):
                atm = self.calculate_atm_strike(c_val)
                sl = c_val + (self.sl_atr_mult * cur_atr)
                tp = c_val - (self.tp_atr_mult * cur_atr)
                rr = (c_val - tp) / max(sl - c_val, 1.0)
                ts_sec = int(b.ts[i])
                t_str = hm(ts_sec)

                reasons = [
                    f"EMA {self.fast_ema_len} < {self.slow_ema_len} Trend Bearish ({f_ema:.1f} < {s_ema:.1f})",
                    f"MACD Negative Momentum Crossover ({m_val:.2f} < {m_sig:.2f}, Hist {m_hst:+.2f})",
                    f"ADX Trend Strength Active ({cur_adx:.1f} >= {self.adx_threshold:.0f}, -DI > +DI)",
                    vix_reason,
                ]

                sig = PrahaarSignal(
                    candle_idx=i,
                    time=ts_sec,
                    time_str=t_str,
                    action="BUY ATM PUT",
                    dir="bear",
                    strike=atm,
                    option_type="PE",
                    option_symbol=f"{atm} PE",
                    entry=c_val,
                    sl=sl,
                    tp=tp,
                    rr=rr,
                    adx=cur_adx,
                    macd=m_val,
                    macd_signal=m_sig,
                    macd_hist=m_hst,
                    fast_ema=f_ema,
                    slow_ema=s_ema,
                    vix=vix,
                    vix_ok=vix_ok,
                    vix_reason=vix_reason,
                    reasons=reasons,
                )
                signals.append(sig)

                # Chart marker
                markers.append({
                    "time": chart_time(ts_sec),
                    "kind": "PRAHAAR_BUY_PE",
                    "dir": "bear",
                    "price": c_val,
                    "strike": atm,
                    "option_type": "PE",
                    "text": f"🔴 BUY {atm} PE (Prahaar)",
                    "short": f"🔴 BUY {atm} PE",
                    "at": t_str,
                    "adx": round(cur_adx, 1),
                    "macd_hist": round(m_hst, 2),
                    "full": f"Prahaar BUY ATM PUT: {atm} PE @ {c_val:.1f} · SL {sl:.1f} · TP {tp:.1f} (ADX {cur_adx:.1f})",
                })
                last_signal_idx = i
                last_signal_dir = "bear"

        # Latest bar diagnostic state
        last_c = float(close[-1])
        last_atm = self.calculate_atm_strike(last_c)
        last_f_ema = float(fast_ema[-1])
        last_s_ema = float(slow_ema[-1])
        last_m = float(macd_line[-1])
        last_sig = float(sig_line[-1])
        last_h = float(macd_hist[-1])
        prev_h = float(macd_hist[-2]) if n >= 2 else last_h
        last_adx = float(adx[-1])
        last_p_di = float(plus_di[-1])
        last_m_di = float(minus_di[-1])

        trend = "bullish" if last_f_ema > last_s_ema else "bearish" if last_f_ema < last_s_ema else "neutral"
        momentum = "bullish" if last_m > last_sig else "bearish" if last_m < last_sig else "neutral"
        adx_trending = last_adx >= self.adx_threshold

        current_verdict = "NEUTRAL"
        if trend == "bullish" and momentum == "bullish" and adx_trending and last_p_di > last_m_di and vix_ok:
            current_verdict = "BUY ATM CALL"
        elif trend == "bearish" and momentum == "bearish" and adx_trending and last_m_di > last_p_di and vix_ok:
            current_verdict = "BUY ATM PUT"

        stats = {
            "total_signals": len(signals),
            "bull_signals": sum(1 for s in signals if s.dir == "bull"),
            "bear_signals": sum(1 for s in signals if s.dir == "bear"),
        }

        return {
            "available": True,
            "tf": tf,
            "fast_ema_len": self.fast_ema_len,
            "slow_ema_len": self.slow_ema_len,
            "adx_threshold": self.adx_threshold,
            "latest_state": {
                "close": round(last_c, 2),
                "atm_strike": last_atm,
                "fast_ema": round(last_f_ema, 2),
                "slow_ema": round(last_s_ema, 2),
                "ema_trend": trend,
                "macd": round(last_m, 2),
                "macd_signal": round(last_sig, 2),
                "macd_hist": round(last_h, 2),
                "macd_slope": round(last_h - prev_h, 3),
                "macd_momentum": momentum,
                "adx": round(last_adx, 2),
                "adx_trending": adx_trending,
                "plus_di": round(last_p_di, 2),
                "minus_di": round(last_m_di, 2),
                "vix": round(vix, 2) if vix is not None else None,
                "vix_ok": vix_ok,
                "vix_reason": vix_reason,
                "current_verdict": current_verdict,
            },
            "signals": [s.to_dict() for s in signals],
            "markers": markers,
            "stats": stats,
        }


def compute_prahaar(
    bars: Bars,
    atr_arr: Optional[np.ndarray] = None,
    tf: str = "5m",
    rules_cfg: Optional[Dict[str, Any]] = None,
    vix: Optional[float] = None,
    vix_chg: Optional[float] = None,
) -> Dict[str, Any]:
    """Module-level helper to process bars with PrahaarEngine."""
    engine = PrahaarEngine(rules_cfg=rules_cfg)
    return engine.process(bars, atr_arr=atr_arr, tf=tf, vix=vix, vix_chg=vix_chg)
