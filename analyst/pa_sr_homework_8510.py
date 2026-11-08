"""
15m price-action at S1–S4 / R1–R4 + swing levels — paper / homework backtest only.
Patterns: hammer, doji, engulfing, morning/evening star, mother-baby (see config).
Entries: break pattern candle high (long) or low (short) on the next 15m bar.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

def _at_valid_level(low: float, high: float, levels: List[float], tol: float) -> bool:
    for lv in levels:
        if lv is None:
            continue
        if low - tol <= float(lv) <= high + tol:
            return True
    return False


def _detect_single_bar_pattern(o: float, h: float, l: float, c: float) -> Tuple[Optional[str], Optional[str]]:
    rng = max(h - l, 0.001)
    body = abs(c - o)
    uw = h - max(o, c)
    lw = min(o, c) - l
    long_tag, short_tag = None, None
    if (body / rng) <= 0.15:
        long_tag = short_tag = "DOJI"
    if lw >= 2.0 * max(body, 0.5) and uw <= rng * 0.25:
        long_tag = "HAMMER"
    if uw >= 2.0 * max(body, 0.5) and lw <= rng * 0.25:
        short_tag = "INVERTED_HAMMER"
    return long_tag, short_tag


def compute_daily_sr_from_ohlc(daily: pd.DataFrame, spot: float) -> Dict[str, Any]:
    from analyst.terminal8510_backend import compute_daily_sr_from_ohlc as _fn

    return _fn(daily, spot)

log = logging.getLogger("pa_sr_homework")

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CFG: Dict[str, Any] = {
    "enabled": True,
    "paper_only": True,
    "level_tolerance_pts": 25.0,
    "require_below_vwap_for_long": False,
    "require_above_vwap_for_short": False,
    "exit_at_atma": True,
    "swing_lookback_bars": 12,
    "fixed_sl_pts": 40.0,
    "fixed_tgt_pts": 40.0,
    "pattern_trail_green_low_only": True,
}


def load_pa_sr_config(cfg_root: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    out = dict(DEFAULT_CFG)
    if cfg_root and isinstance(cfg_root.get("pa_sr_homework"), dict):
        out.update(cfg_root["pa_sr_homework"])
        return out
    p = ROOT / "analyst" / "terminal8510_config.json"
    try:
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data.get("pa_sr_homework"), dict):
                out.update(data["pa_sr_homework"])
    except Exception as exc:
        log.warning("pa_sr_homework config read failed: %s", exc)
    return out


def extend_sr_s4_r4(daily: pd.DataFrame, spot: float) -> Dict[str, Any]:
    """S1–S3 / R1–R3 from prior day logic; S4/R4 from deeper session extremes."""
    base = compute_daily_sr_from_ohlc(daily, spot)
    if daily is None or daily.empty:
        base["S4"], base["R4"] = None, None
        return base
    d = daily.sort_index()
    hist = d.iloc[:-1] if len(d) > 1 else d
    if len(hist) >= 5:
        base["S4"] = float(hist["low"].tail(10).min())
        base["R4"] = float(hist["high"].tail(10).max())
    else:
        base["S4"] = base.get("S3")
        base["R4"] = base.get("R3")
    return base


def _morning_evening_star(
    o2: float, h2: float, l2: float, c2: float,
    o1: float, h1: float, l1: float, c1: float,
    o0: float, h0: float, l0: float, c0: float,
) -> Tuple[Optional[str], Optional[str]]:
    body2, body1, body0 = abs(c2 - o2), abs(c1 - o1), abs(c0 - o0)
    rng1 = max(h1 - l1, 0.001)
    long_p, short_p = None, None
    # Morning star: red, small, green closing above midpoint of first
    if c2 < o2 and body1 / rng1 <= 0.35 and c0 > o0:
        mid2 = (o2 + c2) / 2.0
        if c0 > mid2 and c0 > (o2 + c2) / 2.0:
            long_p = "MORNING_STAR"
    if c2 > o2 and body1 / rng1 <= 0.35 and c0 < o0:
        mid2 = (o2 + c2) / 2.0
        if c0 < mid2:
            short_p = "EVENING_STAR"
    return long_p, short_p


def detect_pa_patterns_on_bar(
    bars: List[Dict[str, Any]], idx: int
) -> Tuple[Optional[str], Optional[str], float, float]:
    """Returns (long_pattern, short_pattern, pattern_high, pattern_low) for bar idx."""
    if idx < 1 or idx >= len(bars):
        return None, None, 0.0, 0.0
    b = bars[idx]
    bp = bars[idx - 1]
    o0, h0, l0, c0 = float(b["open"]), float(b["high"]), float(b["low"]), float(b["close"])
    o1, h1, l1, c1 = float(bp["open"]), float(bp["high"]), float(bp["low"]), float(bp["close"])
    pa_l, pa_s = _detect_single_bar_pattern(o0, h0, l0, c0)
    body0, body1 = abs(c0 - o0), abs(c1 - o1)
    if c1 < o1 and c0 > o0 and c0 >= o1 and o0 <= c1 and body0 > body1:
        pa_l = "BULLISH_ENGULFING"
    elif c1 > o1 and c0 < o0 and c0 <= o1 and o0 >= c1 and body0 > body1:
        pa_s = "BEARISH_ENGULFING"
    elif h0 <= h1 and l0 >= l1 and body1 > body0:
        if c0 > o0 and c1 < o1:
            pa_l = "BULLISH_MOTHER_BABY"
        elif c0 < o0 and c1 > o1:
            pa_s = "BEARISH_MOTHER_BABY"
    if idx >= 2:
        b2 = bars[idx - 2]
        o2, h2, l2, c2 = [float(b2[k]) for k in ("open", "high", "low", "close")]
        ms, es = _morning_evening_star(o2, h2, l2, c2, o1, h1, l1, c1, o0, h0, l0, c0)
        if ms:
            pa_l = ms
        if es:
            pa_s = es
    return pa_l, pa_s, h0, l0


def recent_swing_high(bars: List[Dict[str, Any]], idx: int, lookback: int) -> Optional[float]:
    if idx <= lookback:
        return None
    window = bars[idx - lookback : idx]
    if not window:
        return None
    highs = [float(x["high"]) for x in window]
    return max(highs)


def session_vwap_up_to_ts(df_1m_day: pd.DataFrame, ts: int) -> Optional[float]:
    if df_1m_day is None or df_1m_day.empty:
        return None
    chunk = df_1m_day[df_1m_day["ts"] <= ts]
    if chunk.empty:
        return None
    vol = chunk["volume"].astype(float).sum()
    if vol <= 0:
        return float(chunk.iloc[-1]["close"])
    vp = (chunk["close"].astype(float) * chunk["volume"].astype(float)).sum()
    return float(vp / vol)


def scan_pa_sr_breakout(
    bars: List[Dict[str, Any]],
    idx: int,
    sr: Dict[str, Any],
    cfg: Dict[str, Any],
    vwap: Optional[float] = None,
) -> List[Tuple[str, str, str, float, float, Dict[str, Any]]]:
    """
    Pattern on bar idx; entry when bar idx+1 breaks high/low.
    Returns list of (sid, name, direction, tgt, sl, meta) like legacy backtest.
    """
    if idx + 1 >= len(bars):
        return []
    pa_l, pa_s, ph, pl = detect_pa_patterns_on_bar(bars, idx)
    if not pa_l and not pa_s:
        return []

    tol = float(cfg.get("level_tolerance_pts", 25.0))
    lookback = int(cfg.get("swing_lookback_bars", 12))
    b = bars[idx]
    lo, hi = float(b["low"]), float(b["high"])
    supports = [sr.get(k) for k in ("S1", "S2", "S3", "S4")]
    resists = [sr.get(k) for k in ("R1", "R2", "R3", "R4")]
    swing_hi = recent_swing_high(bars, idx, lookback)
    if swing_hi is not None:
        resists.append(swing_hi)

    next_b = bars[idx + 1]
    nh, nl = float(next_b["high"]), float(next_b["low"])
    c_pat = float(b["close"])
    out: List[Tuple[str, str, str, float, float, Dict[str, Any]]] = []
    tgt = float(cfg.get("fixed_tgt_pts", 40.0))
    sl = float(cfg.get("fixed_sl_pts", 40.0))

    if pa_l and _at_valid_level(lo, hi, [x for x in supports if x is not None], tol):
        if cfg.get("require_below_vwap_for_long") and vwap is not None and c_pat >= vwap:
            pass
        elif nh > ph:
            name = f"PA Long @ Support [{pa_l}] break {ph:.1f}"
            meta = {
                "flagship": False,
                "pa_homework": True,
                "pattern": pa_l,
                "pattern_high": ph,
                "pattern_low": pl,
                "sr_levels": sr,
                "exit_at_atma": bool(cfg.get("exit_at_atma", True)),
                "trail_green_only": bool(cfg.get("pattern_trail_green_low_only", True)),
            }
            out.append(("PA_SR_LONG", name, "LONG", tgt, sl, meta))

    if pa_s and _at_valid_level(lo, hi, [x for x in resists if x is not None], tol):
        if cfg.get("require_above_vwap_for_short") and vwap is not None and c_pat <= vwap:
            pass
        elif nl < pl:
            name = f"PA Short @ Resistance [{pa_s}] break {pl:.1f}"
            meta = {
                "flagship": False,
                "pa_homework": True,
                "pattern": pa_s,
                "pattern_high": ph,
                "pattern_low": pl,
                "sr_levels": sr,
                "exit_at_atma": bool(cfg.get("exit_at_atma", True)),
                "trail_green_only": bool(cfg.get("pattern_trail_green_low_only", True)),
            }
            out.append(("PA_SR_SHORT", name, "SHORT", tgt, sl, meta))

    return out


def scan_pa_sr_pattern_arm(
    bars: List[Dict[str, Any]],
    idx: int,
    sr: Dict[str, Any],
    cfg: Dict[str, Any],
    vwap: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """Pattern at S/R on closed bar idx — arm for break (live paper)."""
    if idx < 0 or idx >= len(bars):
        return []
    pa_l, pa_s, ph, pl = detect_pa_patterns_on_bar(bars, idx)
    tol = float(cfg.get("level_tolerance_pts", 25.0))
    b = bars[idx]
    lo, hi = float(b["low"]), float(b["high"])
    c_pat = float(b["close"])
    lookback = int(cfg.get("swing_lookback_bars", 12))
    supports = [sr.get(k) for k in ("S1", "S2", "S3", "S4")]
    resists = [sr.get(k) for k in ("R1", "R2", "R3", "R4")]
    swing_hi = recent_swing_high(bars, idx + 1, lookback)
    if swing_hi is not None:
        resists.append(swing_hi)
    out: List[Dict[str, Any]] = []
    hm = str(b.get("hm") or "")
    day = str(b.get("day") or "")

    if pa_l and _at_valid_level(lo, hi, [x for x in supports if x is not None], tol):
        if not (cfg.get("require_below_vwap_for_long") and vwap is not None and c_pat >= vwap):
            out.append(
                {
                    "direction": "LONG",
                    "pattern": pa_l,
                    "pattern_high": ph,
                    "pattern_low": pl,
                    "bar_hm": hm,
                    "day": day,
                }
            )
    if pa_s and _at_valid_level(lo, hi, [x for x in resists if x is not None], tol):
        if not (cfg.get("require_above_vwap_for_short") and vwap is not None and c_pat <= vwap):
            out.append(
                {
                    "direction": "SHORT",
                    "pattern": pa_s,
                    "pattern_high": ph,
                    "pattern_low": pl,
                    "bar_hm": hm,
                    "day": day,
                }
            )
    return out


def live_pa_signal_from_pending(pending: Dict[str, Any], spot: float, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Build scanner signal dict for paper execution."""
    direction = pending["direction"]
    is_long = direction == "LONG"
    sl = float(cfg.get("fixed_sl_pts", 40.0))
    tgt = float(cfg.get("fixed_tgt_pts", 40.0))
    d_win = float(cfg.get("delta_win", 0.59))
    d_loss = float(cfg.get("delta_loss", 0.67))
    pat = pending.get("pattern") or "PA"
    sid = "PA_SR_LONG" if is_long else "PA_SR_SHORT"
    display = "PA @ S/R (Live Paper)"
    return {
        "strategy_id": sid,
        "setup_name": f"{display} [{pat}] break",
        "direction": direction,
        "opt_side": "CE" if is_long else "PE",
        "conviction": "PA HOMEWORK",
        "target_pts": round(tgt * d_win, 2),
        "sl_pts": round(sl * d_loss, 2),
        "index_price": round(spot, 2),
        "details": f"{pat} @ S/R | break spot {spot:.2f} | T1 {tgt}p SL {sl}p | bar {pending.get('bar_hm')}",
        "pa_homework": True,
        "paper_only": True,
        "spot_entry": round(spot, 2),
        "spot_target_pts": tgt,
        "spot_stop_pts": sl,
        "spot_target_level": round(spot + tgt, 2) if is_long else round(spot - tgt, 2),
        "spot_stop_level": round(spot - sl, 2) if is_long else round(spot + sl, 2),
        "advisory_note": "PA S/R live paper — homework path; not validated for real money",
    }


def entry_on_pattern_break(
    bars: List[Dict[str, Any]], idx: int, meta: Dict[str, Any], direction: str
) -> float:
    """Entry price once break confirmed on bar idx (the bar after pattern)."""
    if direction == "LONG":
        brk = float(meta.get("pattern_high") or bars[idx]["high"])
        o = float(bars[idx]["open"])
        return round(max(o, brk), 2)
    brk = float(meta.get("pattern_low") or bars[idx]["low"])
    o = float(bars[idx]["open"])
    return round(min(o, brk), 2)
