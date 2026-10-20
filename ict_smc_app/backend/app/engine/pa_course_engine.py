"""
app/engine/pa_course_engine.py — Institutional Price Action Course Engine (Ch. 2–20).

Implements the 10 missing concepts from the Price Action & Smart Money course:
  1. Strong vs. Weak MSB Classifier + 7-Point Super OB Scorer
  2. Dragon Fruit (OB + FVG touching sweet-spot) & FVG Risk Tiers
  3. Mitigation Blocks (MiB — failed HH/LL internal break)
  4. ROTE (Reverse Optimal Trade Entry 0.70–0.80 from Target to Sweep)
  5. Pure SFP vs. cSFP (Considerable SFP) vs. Gray Accumulation Filter
  6. Supply/Demand Bases: RBR, DBD, RBD, DBR
  7. FTR (Failed To Return / Indecision Candle 'Bagholder' Zone)
  8. TT3 (Three-Tap Range Extremity Hidden Supply/Demand)
  9. GPX (HTF Supply-to-Demand Gameplay Box + VWAP Guidance)
 10. Trap & Inducement Veto (EQH/EQL + Unfilled FVG Magnet Warning)
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

try:
    from ..timeutil import chart_time, hm
except Exception:
    import datetime

    def chart_time(ts: int) -> int:
        return ts + 19800

    def hm(ts: int) -> str:
        return datetime.datetime.fromtimestamp(ts + 19800, tz=datetime.timezone.utc).strftime("%H:%M")

try:
    from .. import config
except Exception:
    config = None  # type: ignore

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG — Tunable Thresholds at Top of File (merged with rules.json if present)
# ─────────────────────────────────────────────────────────────────────────────
PA_CONFIG: Dict[str, Any] = {
    "SWING_LOOKBACK": 3,
    "DISP_BODY_ATR": 1.05,          # Minimum candle body / ATR for strong displacement
    "DISP_BODY_RATIO": 0.58,        # Minimum body / (high - low) ratio
    "CSFP_MAX_PIERCE_ATR": 0.18,    # Max close beyond swing for Considerable SFP (cSFP)
    "GRAY_CLUSTER_BARS": 3,         # 3+ closes around swing = Gray Accumulation (No Trade)
    "EQ_TOLERANCE_ATR": 0.22,       # Equal Highs / Equal Lows tolerance in ATRs
    "DRAGON_FRUIT_GAP_ATR": 0.25,   # Max distance between OB and FVG to qualify as Dragon Fruit
    "BASE_MIN_BARS": 2,             # Min consolidation bars for RBR / DBD / RBD / DBR
    "BASE_MAX_BARS": 12,            # Max consolidation bars for base
    "BASE_MAX_HEIGHT_ATR": 1.65,    # Max height of base in ATRs
    "FTR_MAX_BARS": 3,              # 1 to 3 opposite-color pause candles for FTR
    "PULLBACK_SLOWNESS_RATIO": 1.4, # Pullback slowness ratio (pb_bars / imp_bars)
    "OTE_LOW": 0.70,                # Course Golden Zone start (70%)
    "OTE_HIGH": 0.80,               # Course Golden Zone end (80%)
    "OTE_TP_LOW": 0.20,             # Partial profit zone (20%-30%)
    "OTE_TP_HIGH": 0.30,
    "TRAP_MAGNET_ATR": 1.6,         # Distance above/below OB to scan for EQH/EQL + FVG trap
    "NIFTY_ROUND_STEP": 100,        # Round number magnet step (22500, 22600, etc.)
    "MAX_SPREAD_PTS": 2.0,          # Max allowed option bid-ask spread
    "MIN_OPTION_OI": 50000,         # Minimum strike OI filter
}


def _get_config() -> Dict[str, Any]:
    cfg = dict(PA_CONFIG)
    if config is not None:
        try:
            overrides = config.rules().get("pa_course", {})
            if isinstance(overrides, dict):
                cfg.update(overrides)
        except Exception:
            pass
    return cfg


def _atr(rows: List[Dict[str, float]], period: int = 14) -> float:
    if len(rows) < 2:
        return 15.0
    trs: List[float] = []
    for i in range(1, len(rows)):
        h, l, pc = float(rows[i]["h"]), float(rows[i]["l"]), float(rows[i - 1]["c"])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    tail = trs[-period:] if len(trs) >= period else trs
    return max(sum(tail) / max(len(tail), 1), 1.0)


def _swings(rows: List[Dict[str, float]], lb: int = 3) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    highs: List[Dict[str, Any]] = []
    lows: List[Dict[str, Any]] = []
    n = len(rows)
    if n < lb * 2 + 1:
        return highs, lows
    for i in range(lb, n - lb):
        hi = float(rows[i]["h"])
        lo = float(rows[i]["l"])
        if all(hi >= float(rows[k]["h"]) for k in range(i - lb, i + lb + 1)) and any(
            hi > float(rows[k]["h"]) for k in range(i - lb, i + lb + 1) if k != i
        ):
            highs.append({"i": i, "ts": int(rows[i]["ts"]), "price": round(hi, 1), "c": float(rows[i]["c"]), "o": float(rows[i]["o"])})
        if all(lo <= float(rows[k]["l"]) for k in range(i - lb, i + lb + 1)) and any(
            lo < float(rows[k]["l"]) for k in range(i - lb, i + lb + 1) if k != i
        ):
            lows.append({"i": i, "ts": int(rows[i]["ts"]), "price": round(lo, 1), "c": float(rows[i]["c"]), "o": float(rows[i]["o"])})
    return highs, lows


def _detect_fvgs(rows: List[Dict[str, float]], min_gap: float) -> List[Dict[str, Any]]:
    fvgs: List[Dict[str, Any]] = []
    n = len(rows)
    for i in range(2, n):
        r0, r1, r2 = rows[i - 2], rows[i - 1], rows[i]
        # Bullish FVG: low of bar i > high of bar i-2
        if float(r2["l"]) - float(r0["h"]) >= min_gap:
            bot, top = float(r0["h"]), float(r2["l"])
            filled = any(float(rows[k]["l"]) <= bot for k in range(i + 1, n))
            touched = any(float(rows[k]["l"]) <= top for k in range(i + 1, n))
            fvgs.append({
                "i": i - 1, "ts": int(r1["ts"]), "dir": "BULL",
                "bot": round(bot, 1), "top": round(top, 1), "mid": round((bot + top) / 2, 1),
                "filled": filled, "touched": touched,
            })
        # Bearish FVG: high of bar i < low of bar i-2
        if float(r0["l"]) - float(r2["h"]) >= min_gap:
            top, bot = float(r0["l"]), float(r2["h"])
            filled = any(float(rows[k]["h"]) >= top for k in range(i + 1, n))
            touched = any(float(rows[k]["h"]) >= bot for k in range(i + 1, n))
            fvgs.append({
                "i": i - 1, "ts": int(r1["ts"]), "dir": "BEAR",
                "bot": round(bot, 1), "top": round(top, 1), "mid": round((bot + top) / 2, 1),
                "filled": filled, "touched": touched,
            })
    return fvgs


def _detect_eq_pools(highs: List[Dict[str, Any]], lows: List[Dict[str, Any]], rows: List[Dict[str, float]], tol: float) -> List[Dict[str, Any]]:
    pools: List[Dict[str, Any]] = []
    n = len(rows)
    for idx in range(len(highs) - 1):
        h1, h2 = highs[idx], highs[idx + 1]
        if abs(h1["price"] - h2["price"]) <= tol and h2["i"] - h1["i"] >= 3:
            lvl = round((h1["price"] + h2["price"]) / 2, 1)
            swept = any(float(rows[k]["h"]) > lvl + tol * 0.5 for k in range(h2["i"] + 1, n))
            pools.append({"kind": "EQH", "price": lvl, "i1": h1["i"], "i2": h2["i"], "ts": h2["ts"], "swept": swept})
    for idx in range(len(lows) - 1):
        l1, l2 = lows[idx], lows[idx + 1]
        if abs(l1["price"] - l2["price"]) <= tol and l2["i"] - l1["i"] >= 3:
            lvl = round((l1["price"] + l2["price"]) / 2, 1)
            swept = any(float(rows[k]["l"]) < lvl - tol * 0.5 for k in range(l2["i"] + 1, n))
            pools.append({"kind": "EQL", "price": lvl, "i1": l1["i"], "i2": l2["i"], "ts": l2["ts"], "swept": swept})
    return pools


def _detect_bprs(fvgs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Detects BPR (Balanced Price Range) & Super BPR (collision of 3+ opposing FVGs) — Ch. 24."""
    bprs: List[Dict[str, Any]] = []
    bull_fvgs = [f for f in fvgs if f["dir"] == "BULL"]
    bear_fvgs = [f for f in fvgs if f["dir"] == "BEAR"]

    for b_fvg in bull_fvgs:
        for s_fvg in bear_fvgs:
            ov_bot = max(b_fvg["bot"], s_fvg["bot"])
            ov_top = min(b_fvg["top"], s_fvg["top"])
            if ov_bot < ov_top:
                overlap_count = sum(
                    1 for f in fvgs if max(ov_bot, f["bot"]) < min(ov_top, f["top"])
                )
                is_super = overlap_count >= 3
                t_stamp = max(b_fvg["ts"], s_fvg["ts"])
                bprs.append({
                    "kind": "SUPER BPR" if is_super else "BPR",
                    "top": round(ov_top, 1),
                    "bot": round(ov_bot, 1),
                    "mid": round((ov_top + ov_bot) / 2.0, 1),
                    "bull_fvg_i": b_fvg["i"],
                    "bear_fvg_i": s_fvg["i"],
                    "ts": t_stamp,
                    "hm": hm(t_stamp),
                    "overlaps": overlap_count,
                    "label": f"{'SUPER BPR' if is_super else 'BPR'} S/R Flip [{ov_bot:.1f}–{ov_top:.1f}]",
                })
    unique_bprs: List[Dict[str, Any]] = []
    for b in sorted(bprs, key=lambda x: -x["ts"]):
        if not any(abs(b["mid"] - u["mid"]) <= 2.0 and b["kind"] == u["kind"] for u in unique_bprs):
            unique_bprs.append(b)
    return unique_bprs[:6]


def _detect_sbb(
    highs: List[Dict[str, Any]],
    lows: List[Dict[str, Any]],
    closed: List[Dict[str, float]],
    fvgs: List[Dict[str, Any]],
    sfp_events: List[Dict[str, Any]],
    atr: float,
    in_premium: bool,
) -> List[Dict[str, Any]]:
    """Detects Breaker Blocks and upgrades to Super Breaker Blocks (SBB — Ch. 23)."""
    sbbs: List[Dict[str, Any]] = []
    n = len(closed)

    # Bearish Breakers: H1 -> L1 (interim HL) -> H2 (sweeps H1) -> breaks below L1
    for i in range(1, len(highs)):
        h1, h2 = highs[i - 1], highs[i]
        if h2["price"] >= h1["price"] and h2["i"] > h1["i"]:
            mid_lows = [l for l in lows if h1["i"] < l["i"] < h2["i"]]
            if not mid_lows:
                continue
            interim_l = mid_lows[-1]
            for j in range(h2["i"] + 1, min(n, h2["i"] + 40)):
                if float(closed[j]["c"]) < interim_l["price"]:
                    bar = closed[interim_l["i"]]
                    top, bot = round(float(bar["h"]), 1), round(float(bar["l"]), 1)
                    sfp_near = any(
                        s["side"] == "SHORT" and abs(s["ts"] - h2["ts"]) <= 3600 * 4
                        for s in sfp_events
                    )
                    impulse_fvgs = [f for f in fvgs if f["dir"] == "BEAR" and h2["i"] <= f["i"] <= j + 2]
                    fvg_touch = any(max(bot, f["bot"]) <= min(top, f["top"]) + atr * 0.25 for f in impulse_fvgs)
                    is_super = sfp_near and fvg_touch and in_premium

                    sbbs.append({
                        "kind": "SUPER BREAKER (SBB)" if is_super else "BREAKER (BRK)",
                        "dir": "BEAR",
                        "side": "SHORT",
                        "top": top,
                        "bot": bot,
                        "break_level": interim_l["price"],
                        "ts": int(closed[j]["ts"]),
                        "hm": hm(int(closed[j]["ts"])),
                        "is_super": is_super,
                        "sfp_confirmed": sfp_near,
                        "fvg_touch": fvg_touch,
                        "in_aligned_zone": in_premium,
                        "label": f"{'SBB Bearish Super Breaker' if is_super else 'Bearish Breaker'} [{bot:.1f}–{top:.1f}]",
                    })
                    break

    # Bullish Breakers: L1 -> H1 (interim LH) -> L2 (sweeps L1) -> breaks above H1
    for i in range(1, len(lows)):
        l1, l2 = lows[i - 1], lows[i]
        if l2["price"] <= l1["price"] and l2["i"] > l1["i"]:
            mid_highs = [h for h in highs if l1["i"] < h["i"] < l2["i"]]
            if not mid_highs:
                continue
            interim_h = mid_highs[-1]
            for j in range(l2["i"] + 1, min(n, l2["i"] + 40)):
                if float(closed[j]["c"]) > interim_h["price"]:
                    bar = closed[interim_h["i"]]
                    top, bot = round(float(bar["h"]), 1), round(float(bar["l"]), 1)
                    sfp_near = any(
                        s["side"] == "LONG" and abs(s["ts"] - l2["ts"]) <= 3600 * 4
                        for s in sfp_events
                    )
                    impulse_fvgs = [f for f in fvgs if f["dir"] == "BULL" and l2["i"] <= f["i"] <= j + 2]
                    fvg_touch = any(max(bot, f["bot"]) <= min(top, f["top"]) + atr * 0.25 for f in impulse_fvgs)
                    is_super = sfp_near and fvg_touch and (not in_premium)

                    sbbs.append({
                        "kind": "SUPER BREAKER (SBB)" if is_super else "BREAKER (BRK)",
                        "dir": "BULL",
                        "side": "LONG",
                        "top": top,
                        "bot": bot,
                        "break_level": interim_h["price"],
                        "ts": int(closed[j]["ts"]),
                        "hm": hm(int(closed[j]["ts"])),
                        "is_super": is_super,
                        "sfp_confirmed": sfp_near,
                        "fvg_touch": fvg_touch,
                        "in_aligned_zone": not in_premium,
                        "label": f"{'SBB Bullish Super Breaker' if is_super else 'Bullish Breaker'} [{bot:.1f}–{top:.1f}]",
                    })
                    break

    return sbbs[-6:]


def _chain_metrics(chain_rows: Optional[Any], spot: float) -> Dict[str, Any]:
    """Computes PCR, Max Pain, OI Support/Resistance Walls, and optimal CE/PE strikes from Dhan option chain."""
    if isinstance(chain_rows, dict):
        chain_rows = chain_rows.get("strikes") or chain_rows.get("rows") or list(chain_rows.values())

    if not chain_rows or not isinstance(chain_rows, list):
        atm = round(spot / 50.0) * 50.0
        return {
            "available": False, "pcr": None, "max_pain": None,
            "call_wall": round(atm + 200, 0), "put_wall": round(atm - 200, 0),
            "best_ce": {"strike": int(atm), "ltp": None, "delta": 0.52, "iv": None},
            "best_pe": {"strike": int(atm), "ltp": None, "delta": -0.52, "iv": None},
            "highlighted_strikes": [int(atm - 50), int(atm), int(atm + 50)],
        }

    total_ce_oi, total_pe_oi = 0.0, 0.0
    best_call_wall = (0.0, spot + 200)
    best_put_wall = (0.0, spot - 200)
    strikes_data: List[Dict[str, Any]] = []

    for r in chain_rows:
        if not isinstance(r, dict):
            continue
        k = float(r.get("strike") or r.get("strike_price") or 0)
        if not k:
            continue
        ce = r.get("ce") or r.get("call") or {}
        pe = r.get("pe") or r.get("put") or {}
        ce_oi = float(ce.get("oi") or r.get("ce_oi") or 0)
        pe_oi = float(pe.get("oi") or r.get("pe_oi") or 0)
        total_ce_oi += ce_oi
        total_pe_oi += pe_oi
        if ce_oi > best_call_wall[0] and k >= spot - 50:
            best_call_wall = (ce_oi, k)
        if pe_oi > best_put_wall[0] and k <= spot + 50:
            best_put_wall = (pe_oi, k)
        strikes_data.append({
            "strike": int(k),
            "ce_oi": ce_oi, "pe_oi": pe_oi,
            "ce_ltp": ce.get("ltp") or ce.get("last_price") or r.get("ce_ltp"),
            "pe_ltp": pe.get("ltp") or pe.get("last_price") or r.get("pe_ltp"),
            "ce_delta": (ce.get("greeks") or {}).get("delta") or ce.get("delta") or 0.5,
            "pe_delta": (pe.get("greeks") or {}).get("delta") or pe.get("delta") or -0.5,
            "ce_iv": ce.get("iv") or ce.get("implied_volatility"),
            "pe_iv": pe.get("iv") or pe.get("implied_volatility"),
        })

    # Max Pain calculation
    max_pain_strike = round(spot / 50.0) * 50.0
    if strikes_data:
        min_loss = float("inf")
        for candidate in strikes_data:
            expiry_p = candidate["strike"]
            loss = sum(
                max(0.0, expiry_p - s["strike"]) * s["ce_oi"] + max(0.0, s["strike"] - expiry_p) * s["pe_oi"]
                for s in strikes_data
            )
            if loss < min_loss:
                min_loss = loss
                max_pain_strike = expiry_p

    atm = int(round(spot / 50.0) * 50)
    ce_pick = next((s for s in strikes_data if s["strike"] == atm), {"strike": atm, "ce_ltp": None, "ce_delta": 0.52, "ce_iv": None})
    pe_pick = next((s for s in strikes_data if s["strike"] == atm), {"strike": atm, "pe_ltp": None, "pe_delta": -0.52, "pe_iv": None})

    return {
        "available": True,
        "pcr": round(total_pe_oi / max(total_ce_oi, 1.0), 2),
        "max_pain": int(max_pain_strike),
        "call_wall": int(best_call_wall[1]),
        "put_wall": int(best_put_wall[1]),
        "best_ce": {"strike": ce_pick["strike"], "ltp": ce_pick.get("ce_ltp"), "delta": ce_pick.get("ce_delta"), "iv": ce_pick.get("ce_iv")},
        "best_pe": {"strike": pe_pick["strike"], "ltp": pe_pick.get("pe_ltp"), "delta": pe_pick.get("pe_delta"), "iv": pe_pick.get("pe_iv")},
        "highlighted_strikes": sorted(list({atm - 50, atm, atm + 50, int(best_call_wall[1]), int(best_put_wall[1])})),
    }


def analyze_pa_course(
    rows: List[Dict[str, float]],
    tf: str = "5m",
    spot: Optional[float] = None,
    chain_rows: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Main entry point: evaluates all 10 missing course concepts on closed candles."""
    closed = [r for r in rows if r.get("closed", True)]
    if len(closed) < 30:
        return {"tf": tf, "available": False, "signal": "NO TRADE", "reason": "Insufficient closed candles"}

    cfg = _get_config()
    atr = _atr(closed)
    cur_spot = float(spot or closed[-1]["c"])
    highs, lows = _swings(closed, lb=int(cfg["SWING_LOOKBACK"]))
    fvgs = _detect_fvgs(closed, min_gap=atr * 0.22)
    eq_pools = _detect_eq_pools(highs, lows, closed, tol=atr * cfg["EQ_TOLERANCE_ATR"])
    chain_info = _chain_metrics(chain_rows, cur_spot)
    n = len(closed)

    # ─────────────────────────────────────────────────────────────────────────
    # 1. Strong vs. Weak MSB + 7-Point Super OB + Dragon Fruit + Trap Veto
    # ─────────────────────────────────────────────────────────────────────────
    msb_list: List[Dict[str, Any]] = []
    # Check recent Bullish MSB (breaking LH that created deepest LL)
    if len(lows) >= 2 and len(highs) >= 2:
        deep_ll = min(lows[-6:], key=lambda x: x["price"])
        prior_lhs = [h for h in highs if h["i"] < deep_ll["i"]]
        origin_lh = prior_lhs[-1] if prior_lhs else highs[-2]
        lvl = origin_lh["price"]
        for j in range(deep_ll["i"] + 1, n):
            rj = closed[j]
            body = abs(float(rj["c"]) - float(rj["o"]))
            rng = max(float(rj["h"]) - float(rj["l"]), 0.1)
            if float(rj["h"]) > lvl:
                strong_close = float(rj["c"]) > lvl + atr * 0.10 and body >= atr * cfg["DISP_BODY_ATR"] and (body / rng) >= cfg["DISP_BODY_RATIO"]
                # Find origin red candle (Bullish OB)
                ob_bar = closed[deep_ll["i"]]
                for k in range(j - 1, max(deep_ll["i"] - 2, 0), -1):
                    if float(closed[k]["c"]) < float(closed[k]["o"]):
                        ob_bar = closed[k]
                        break
                ob_top = max(float(ob_bar["o"]), float(ob_bar["c"])) if abs(float(ob_bar["c"]) - float(ob_bar["o"])) > atr * 0.35 else float(ob_bar["h"])
                ob_bot = float(ob_bar["l"])
                ob_mid = round((ob_top + ob_bot) / 2.0, 1)

                # Check Dragon Fruit (Bullish FVG touching/adjacent to OB)
                leg_fvgs = [f for f in fvgs if f["dir"] == "BULL" and deep_ll["i"] <= f["i"] <= j + 1]
                dragon_fruit = any(abs(f["bot"] - ob_top) <= atr * cfg["DRAGON_FRUIT_GAP_ATR"] for f in leg_fvgs)

                # Check Key Level / Round Number / OI Put Wall confluence
                near_round = (ob_mid % cfg["NIFTY_ROUND_STEP"]) <= atr * 0.4 or (cfg["NIFTY_ROUND_STEP"] - (ob_mid % cfg["NIFTY_ROUND_STEP"])) <= atr * 0.4
                near_put_wall = abs(ob_mid - chain_info["put_wall"]) <= atr * 0.8

                # First-Time-Back (FTB) touch count
                touches = sum(1 for k in range(j + 1, n) if float(closed[k]["l"]) <= ob_top and float(closed[k]["h"]) >= ob_bot)

                # 7-Point Super OB Score
                imp_bars = max(1, j - deep_ll["i"])
                pb_bars = max(1, (n - 1) - j)
                slowness_ratio = float(cfg.get("PULLBACK_SLOWNESS_RATIO", 1.4))
                checks = {
                    "strong_msb": bool(strong_close),
                    "one_shot_no_chop": imp_bars <= 6,
                    "has_imbalance_fvg": len(leg_fvgs) > 0,
                    "dragon_fruit_ob_fvg": dragon_fruit,
                    "fast_pump_slow_pullback": (pb_bars / imp_bars) >= slowness_ratio,
                    "key_level_or_round_num": bool(near_round or near_put_wall),
                    "first_time_back_ftb": touches <= 1,
                }
                score = sum(1 for v in checks.values() if v)

                # Trap & Inducement Check (EQL + unfilled FVG sitting directly below OB)
                trap_below = any(
                    p["kind"] == "EQL" and not p["swept"] and 0 < (ob_bot - p["price"]) <= atr * cfg["TRAP_MAGNET_ATR"]
                    for p in eq_pools
                )

                msb_list.append({
                    "dir": "BULL",
                    "kind": "STRONG MSB" if strong_close else "WEAK MSB (Wick Struggle)",
                    "ts": int(rj["ts"]), "hm": hm(int(rj["ts"])), "time": chart_time(int(rj["ts"])),
                    "break_level": lvl,
                    "entry_zone": {"top": round(lvl, 1), "bot": round(deep_ll["price"], 1)},
                    "ob": {"top": round(ob_top, 1), "bot": round(ob_bot, 1), "mid": ob_mid, "touches": touches},
                    "dragon_fruit": dragon_fruit,
                    "super_ob_score": score,
                    "super_ob_checks": checks,
                    "trap_warning": "⚠ TRAP VETO: Unswept EQL magnet sits directly below OB!" if trap_below else None,
                })
                break

    # Check recent Bearish MSB (breaking HL that created deepest HH)
    if len(highs) >= 2 and len(lows) >= 2:
        deep_hh = max(highs[-6:], key=lambda x: x["price"])
        prior_hls = [l for l in lows if l["i"] < deep_hh["i"]]
        origin_hl = prior_hls[-1] if prior_hls else lows[-2]
        lvl = origin_hl["price"]
        for j in range(deep_hh["i"] + 1, n):
            rj = closed[j]
            body = abs(float(rj["c"]) - float(rj["o"]))
            rng = max(float(rj["h"]) - float(rj["l"]), 0.1)
            if float(rj["l"]) < lvl:
                strong_close = float(rj["c"]) < lvl - atr * 0.10 and body >= atr * cfg["DISP_BODY_ATR"] and (body / rng) >= cfg["DISP_BODY_RATIO"]
                ob_bar = closed[deep_hh["i"]]
                for k in range(j - 1, max(deep_hh["i"] - 2, 0), -1):
                    if float(closed[k]["c"]) > float(closed[k]["o"]):
                        ob_bar = closed[k]
                        break
                ob_bot = min(float(ob_bar["o"]), float(ob_bar["c"])) if abs(float(ob_bar["c"]) - float(ob_bar["o"])) > atr * 0.35 else float(ob_bar["l"])
                ob_top = float(ob_bar["h"])
                ob_mid = round((ob_top + ob_bot) / 2.0, 1)

                leg_fvgs = [f for f in fvgs if f["dir"] == "BEAR" and deep_hh["i"] <= f["i"] <= j + 1]
                dragon_fruit = any(abs(ob_bot - f["top"]) <= atr * cfg["DRAGON_FRUIT_GAP_ATR"] for f in leg_fvgs)
                near_round = (ob_mid % cfg["NIFTY_ROUND_STEP"]) <= atr * 0.4 or (cfg["NIFTY_ROUND_STEP"] - (ob_mid % cfg["NIFTY_ROUND_STEP"])) <= atr * 0.4
                near_call_wall = abs(ob_mid - chain_info["call_wall"]) <= atr * 0.8
                touches = sum(1 for k in range(j + 1, n) if float(closed[k]["h"]) >= ob_bot and float(closed[k]["l"]) <= ob_top)

                imp_bars = max(1, j - deep_hh["i"])
                pb_bars = max(1, (n - 1) - j)
                slowness_ratio = float(cfg.get("PULLBACK_SLOWNESS_RATIO", 1.4))
                checks = {
                    "strong_msb": bool(strong_close),
                    "one_shot_no_chop": imp_bars <= 6,
                    "has_imbalance_fvg": len(leg_fvgs) > 0,
                    "dragon_fruit_ob_fvg": dragon_fruit,
                    "fast_pump_slow_pullback": (pb_bars / imp_bars) >= slowness_ratio,
                    "key_level_or_round_num": bool(near_round or near_call_wall),
                    "first_time_back_ftb": touches <= 1,
                }
                score = sum(1 for v in checks.values() if v)
                trap_above = any(
                    p["kind"] == "EQH" and not p["swept"] and 0 < (p["price"] - ob_top) <= atr * cfg["TRAP_MAGNET_ATR"]
                    for p in eq_pools
                )
                msb_list.append({
                    "dir": "BEAR",
                    "kind": "STRONG MSB" if strong_close else "WEAK MSB (Wick Struggle)",
                    "ts": int(rj["ts"]), "hm": hm(int(rj["ts"])), "time": chart_time(int(rj["ts"])),
                    "break_level": lvl,
                    "entry_zone": {"top": round(deep_hh["price"], 1), "bot": round(lvl, 1)},
                    "ob": {"top": round(ob_top, 1), "bot": round(ob_bot, 1), "mid": ob_mid, "touches": touches},
                    "dragon_fruit": dragon_fruit,
                    "super_ob_score": score,
                    "super_ob_checks": checks,
                    "trap_warning": "⚠ TRAP VETO: Unswept EQH magnet sits directly above Supply OB!" if trap_above else None,
                })
                break

    # ─────────────────────────────────────────────────────────────────────────
    # 2. Pure SFP vs. cSFP vs. Gray-Area Accumulation (Ch. 11)
    # ─────────────────────────────────────────────────────────────────────────
    sfp_events: List[Dict[str, Any]] = []
    for sh in highs[-6:]:
        lvl = sh["price"]
        cluster_closes = 0
        for j in range(sh["i"] + 2, n):
            rj = closed[j]
            h, c, o = float(rj["h"]), float(rj["c"]), float(rj["o"])
            if abs(c - lvl) <= atr * 0.15:
                cluster_closes += 1
            if h > lvl and c < lvl and c < o:
                sfp_events.append({
                    "kind": "PURE SFP (BEAR)", "side": "SHORT", "level": lvl,
                    "sweep_high": round(h, 1), "ts": int(rj["ts"]), "hm": hm(int(rj["ts"])),
                    "note": "Wick swept major swing high and closed back below.",
                })
                break
            if 0 < (c - lvl) <= atr * cfg["CSFP_MAX_PIERCE_ATR"] and j + 1 < n:
                rn = closed[j + 1]
                if float(rn["c"]) < lvl and float(rn["c"]) < float(rn["o"]):
                    sfp_events.append({
                        "kind": "cSFP (CONSIDERABLE SFP BEAR)", "side": "SHORT", "level": lvl,
                        "sweep_high": round(max(h, float(rn["h"])), 1), "ts": int(rn["ts"]), "hm": hm(int(rn["ts"])),
                        "note": "Tiny close above swing high immediately reversed on next bar (stop-hunt).",
                    })
                    break
        if cluster_closes >= cfg["GRAY_CLUSTER_BARS"]:
            sfp_events.append({
                "kind": "GRAY ACCUMULATION (NO TRADE)", "side": "NEUTRAL", "level": lvl,
                "sweep_high": lvl, "ts": sh["ts"], "hm": hm(sh["ts"]),
                "note": f"{cluster_closes} bars stalling on swing high — order accumulation gray zone.",
            })

    for sl_pt in lows[-6:]:
        lvl = sl_pt["price"]
        for j in range(sl_pt["i"] + 2, n):
            rj = closed[j]
            l, c, o = float(rj["l"]), float(rj["c"]), float(rj["o"])
            if l < lvl and c > lvl and c > o:
                sfp_events.append({
                    "kind": "PURE SFP (BULL)", "side": "LONG", "level": lvl,
                    "sweep_low": round(l, 1), "ts": int(rj["ts"]), "hm": hm(int(rj["ts"])),
                    "note": "Wick swept major swing low and closed back above.",
                })
                break
            if 0 < (lvl - c) <= atr * cfg["CSFP_MAX_PIERCE_ATR"] and j + 1 < n:
                rn = closed[j + 1]
                if float(rn["c"]) > lvl and float(rn["c"]) > float(rn["o"]):
                    sfp_events.append({
                        "kind": "cSFP (CONSIDERABLE SFP BULL)", "side": "LONG", "level": lvl,
                        "sweep_low": round(min(l, float(rn["l"])), 1), "ts": int(rn["ts"]), "hm": hm(int(rn["ts"])),
                        "note": "Tiny close below swing low immediately reversed up on next bar.",
                    })
                    break

    # ─────────────────────────────────────────────────────────────────────────
    # 3. Mitigation Blocks (MiB — Ch. 16: Failed HH / Failed LL break)
    # ─────────────────────────────────────────────────────────────────────────
    mib_zones: List[Dict[str, Any]] = []
    if len(highs) >= 2:
        h_prev, h_curr = highs[-2], highs[-1]
        if h_curr["price"] < h_prev["price"]:  # Failed to make new HH!
            mid_lows = [l for l in lows if h_prev["i"] < l["i"] < h_curr["i"]]
            if mid_lows:
                neck = mid_lows[-1]
                broke = next((closed[k] for k in range(h_curr["i"] + 1, n) if float(closed[k]["c"]) < neck["price"]), None)
                if broke:
                    origin_bar = closed[neck["i"]]
                    mib_zones.append({
                        "kind": "BEARISH MITIGATION BLOCK (MiB)", "side": "SHORT",
                        "top": round(float(origin_bar["h"]), 1), "bot": round(float(origin_bar["l"]), 1),
                        "bos_hm": hm(int(broke["ts"])),
                        "note": f"Failed HH ({h_curr['price']} < {h_prev['price']}) + breakdown below {neck['price']}.",
                    })
    if len(lows) >= 2:
        l_prev, l_curr = lows[-2], lows[-1]
        if l_curr["price"] > l_prev["price"]:  # Failed to make new LL!
            mid_highs = [h for h in highs if l_prev["i"] < h["i"] < l_curr["i"]]
            if mid_highs:
                neck = mid_highs[-1]
                broke = next((closed[k] for k in range(l_curr["i"] + 1, n) if float(closed[k]["c"]) > neck["price"]), None)
                if broke:
                    origin_bar = closed[neck["i"]]
                    mib_zones.append({
                        "kind": "BULLISH MITIGATION BLOCK (MiB)", "side": "LONG",
                        "top": round(float(origin_bar["h"]), 1), "bot": round(float(origin_bar["l"]), 1),
                        "bos_hm": hm(int(broke["ts"])),
                        "note": f"Failed LL ({l_curr['price']} > {l_prev['price']}) + breakout above {neck['price']}.",
                    })

    # ─────────────────────────────────────────────────────────────────────────
    # 4. ROTE — Reverse Optimal Trade Entry (Ch. 9: Target -> Sweep Fib 0.70–0.80)
    # ─────────────────────────────────────────────────────────────────────────
    rote_setups: List[Dict[str, Any]] = []
    if sfp_events and (highs and lows):
        last_sfp = sfp_events[-1]
        if last_sfp["side"] == "SHORT" and lows:
            target_low = min(lows[-5:], key=lambda x: x["price"])["price"]
            sweep_hi = float(last_sfp.get("sweep_high", last_sfp["level"]))
            rng = sweep_hi - target_low
            if rng >= atr * 2.0:
                eq_50 = round(target_low + rng * 0.50, 1)
                ote_70 = round(target_low + rng * cfg["OTE_LOW"], 1)
                ote_80 = round(target_low + rng * cfg["OTE_HIGH"], 1)
                tp1 = round(target_low + rng * cfg["OTE_TP_LOW"], 1)
                rote_setups.append({
                    "side": "SHORT", "badge": "BUY PE",
                    "sweep_extreme": sweep_hi, "target_liquidity": target_low,
                    "eq_50": eq_50, "ote_zone": [ote_70, ote_80],
                    "sl": round(sweep_hi + atr * 0.35, 1), "tp1_partial": tp1, "tp2_final": target_low,
                    "strike": f"{chain_info['best_pe']['strike']} PE",
                    "rule": f"Sweep @ {sweep_hi} -> Wait break below EQ {eq_50} -> Short ROTE {ote_70}–{ote_80} targeting EQL {target_low}.",
                })
        elif last_sfp["side"] == "LONG" and highs:
            target_high = max(highs[-5:], key=lambda x: x["price"])["price"]
            sweep_lo = float(last_sfp.get("sweep_low", last_sfp["level"]))
            rng = target_high - sweep_lo
            if rng >= atr * 2.0:
                eq_50 = round(sweep_lo + rng * 0.50, 1)
                ote_70 = round(target_high - rng * cfg["OTE_LOW"], 1)
                ote_80 = round(target_high - rng * cfg["OTE_HIGH"], 1)
                tp1 = round(target_high - rng * cfg["OTE_TP_LOW"], 1)
                rote_setups.append({
                    "side": "LONG", "badge": "BUY CE",
                    "sweep_extreme": sweep_lo, "target_liquidity": target_high,
                    "eq_50": eq_50, "ote_zone": [ote_80, ote_70],
                    "sl": round(sweep_lo - atr * 0.35, 1), "tp1_partial": tp1, "tp2_final": target_high,
                    "strike": f"{chain_info['best_ce']['strike']} CE",
                    "rule": f"Sweep @ {sweep_lo} -> Wait break above EQ {eq_50} -> Buy ROTE {ote_80}–{ote_70} targeting EQH {target_high}.",
                })

    # ─────────────────────────────────────────────────────────────────────────
    # 5. FTR (Failed to Return / Indecision Candle 'Bagholder' Zone — Ch. 20)
    #    & RBR / DBD / RBD / DBR Supply-Demand Bases (Ch. 15)
    # ─────────────────────────────────────────────────────────────────────────
    ftr_zones: List[Dict[str, Any]] = []
    sd_bases: List[Dict[str, Any]] = []
    for i in range(5, n - 3):
        leg1 = float(closed[i - 1]["c"]) - float(closed[i - 3]["o"])
        # Check 1-2 bar opposite pause followed by strong continuation
        r_pause = closed[i]
        leg2 = float(closed[i + 2]["c"]) - float(closed[i + 1]["o"])
        if leg1 >= atr * 1.4 and float(r_pause["c"]) < float(r_pause["o"]) and leg2 >= atr * 1.3 and float(closed[i + 2]["c"]) > float(closed[i - 1]["h"]):
            top, bot = round(float(r_pause["h"]), 1), round(float(r_pause["l"]), 1)
            ftb_count = sum(1 for k in range(i + 3, n) if float(closed[k]["l"]) <= top)
            ftr_zones.append({
                "kind": "BULLISH FTR (RBR Demand)", "side": "LONG",
                "top": top, "bot": bot, "hm": hm(int(r_pause["ts"])),
                "status": "FRESH (Await FTB)" if ftb_count == 0 else ("FTB ACTIVE" if ftb_count == 1 else "USED"),
            })
        elif leg1 <= -atr * 1.4 and float(r_pause["c"]) > float(r_pause["o"]) and leg2 <= -atr * 1.3 and float(closed[i + 2]["c"]) < float(closed[i - 1]["l"]):
            top, bot = round(float(r_pause["h"]), 1), round(float(r_pause["l"]), 1)
            ftb_count = sum(1 for k in range(i + 3, n) if float(closed[k]["h"]) >= bot)
            ftr_zones.append({
                "kind": "BEARISH FTR (DBD Supply)", "side": "SHORT",
                "top": top, "bot": bot, "hm": hm(int(r_pause["ts"])),
                "status": "FRESH (Await FTB)" if ftb_count == 0 else ("FTB ACTIVE" if ftb_count == 1 else "USED"),
            })

    # ─────────────────────────────────────────────────────────────────────────
    # 6. BPR (Balanced Price Range) & Super BPR (Ch. 24)
    #    & SBB (Super Breaker Blocks — Ch. 23)
    # ─────────────────────────────────────────────────────────────────────────
    bpr_zones = _detect_bprs(fvgs)

    # ─────────────────────────────────────────────────────────────────────────
    # 7. GPX — Gameplay Box (HTF Supply-to-Demand Corridor + Premium/Discount)
    # ─────────────────────────────────────────────────────────────────────────
    win = closed[-60:]
    range_hi = round(max(float(r["h"]) for r in win), 1)
    range_lo = round(min(float(r["l"]) for r in win), 1)
    range_eq = round((range_hi + range_lo) / 2.0, 1)
    in_premium = cur_spot > range_eq
    zone_label = "PREMIUM ZONE (Expensive — Look for Shorts / PE)" if in_premium else "DISCOUNT ZONE (Cheap — Look for Longs / CE)"

    sbb_setups = _detect_sbb(highs, lows, closed, fvgs, sfp_events, atr, in_premium)

    # ─────────────────────────────────────────────────────────────────────────
    # 8. Master Actionable Signal Synthesis (BUY CE / BUY PE / NO TRADE)
    # ─────────────────────────────────────────────────────────────────────────
    signal = "NO TRADE"
    reason = f"Spot {cur_spot:.1f} inside {zone_label}. Waiting for FTB into Dragon Fruit OB / ROTE / SFP confirmation."
    active_msb = msb_list[-1] if msb_list else None

    if active_msb:
        ob = active_msb["ob"]
        if active_msb["trap_warning"]:
            signal = "NO TRADE (TRAP VETO)"
            reason = active_msb["trap_warning"]
        elif active_msb["dir"] == "BULL" and not in_premium and ob["bot"] - atr * 0.3 <= cur_spot <= ob["top"] + atr * 0.4:
            signal = f"BUY CE ({chain_info['best_ce']['strike']} CE)"
            reason = f"Discount Zone + {active_msb['kind']} (Super OB {active_msb['super_ob_score']}/7, Dragon Fruit={active_msb['dragon_fruit']}) retest @ {ob['mid']}."
        elif active_msb["dir"] == "BEAR" and in_premium and ob["bot"] - atr * 0.4 <= cur_spot <= ob["top"] + atr * 0.3:
            signal = f"BUY PE ({chain_info['best_pe']['strike']} PE)"
            reason = f"Premium Zone + {active_msb['kind']} (Super OB {active_msb['super_ob_score']}/7, Dragon Fruit={active_msb['dragon_fruit']}) retest @ {ob['mid']}."

    # ─────────────────────────────────────────────────────────────────────────
    # 9. Dynamic Trade Management Metrics (Ch. 21 & 22)
    # ─────────────────────────────────────────────────────────────────────────
    trade_mgmt = {
        "entry": None,
        "sl": None,
        "r_dist": None,
        "be_1r": None,
        "directive": "Risk Management: Move SL to Breakeven at 1R once triggered, take partials at first imbalance.",
    }
    if signal.startswith("BUY CE") and active_msb:
        ob = active_msb["ob"]
        entry = cur_spot
        sl = round(ob["bot"] - atr * 0.35, 1)
        r_dist = max(entry - sl, 5.0)
        be_1r = round(entry + r_dist, 1)
        trade_mgmt = {
            "entry": round(entry, 1),
            "sl": sl,
            "r_dist": round(r_dist, 1),
            "be_1r": be_1r,
            "directive": f"Risk Management: Move SL to Breakeven at 1R ({be_1r:.1f}), take partial profits at first imbalance.",
        }
    elif signal.startswith("BUY PE") and active_msb:
        ob = active_msb["ob"]
        entry = cur_spot
        sl = round(ob["top"] + atr * 0.35, 1)
        r_dist = max(sl - entry, 5.0)
        be_1r = round(entry - r_dist, 1)
        trade_mgmt = {
            "entry": round(entry, 1),
            "sl": sl,
            "r_dist": round(r_dist, 1),
            "be_1r": be_1r,
            "directive": f"Risk Management: Move SL to Breakeven at 1R ({be_1r:.1f}), take partial profits at first imbalance.",
        }

    return {
        "tf": tf,
        "available": True,
        "spot": round(cur_spot, 1),
        "atr": round(atr, 1),
        "signal": signal,
        "reason": reason,
        "trade_management": trade_mgmt,
        "gpx_box": {
            "range_high": range_hi,
            "range_eq": range_eq,
            "range_low": range_lo,
            "zone": "PREMIUM" if in_premium else "DISCOUNT",
            "ote_short_zone": [round(range_lo + (range_hi - range_lo) * 0.70, 1), round(range_lo + (range_hi - range_lo) * 0.80, 1)],
            "ote_long_zone": [round(range_hi - (range_hi - range_lo) * 0.80, 1), round(range_hi - (range_hi - range_lo) * 0.70, 1)],
        },
        "msb_setups": msb_list[-6:],
        "sfp_events": sfp_events[-6:],
        "mib_zones": mib_zones[-4:],
        "rote_setups": rote_setups[-4:],
        "ftr_zones": ftr_zones[-6:],
        "eq_pools": eq_pools[-8:],
        "bpr_zones": bpr_zones,
        "sbb_setups": sbb_setups,
        "chain_metrics": chain_info,
        "config": cfg,
    }
