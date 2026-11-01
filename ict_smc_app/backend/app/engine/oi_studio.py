"""
oi_studio.py — StockMojo 1:1 Open Interest & Writer Map Studio Engine.

Provides time-windowed strike-wise Open Interest analysis matching StockMojo:
  - Tab 1: OI Change + Total (Solid base, Hatched increase, Hollow decrease)
  - Tab 2: OI Change (Pure delta above and below zero)
  - Tab 3: Total OI (Clean standing Call OI vs Put OI)
  - Summary Cards: Window OI Change, Total Standing OI, PCR Donut, Sentiment %
  - Full intraday timeline for dual-thumb slider and interactive replay dock.
"""
from __future__ import annotations

import json
from functools import lru_cache
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from .. import config, db
from ..timeutil import IST, now_ist, session_bounds

CONFIG_FILE = Path(__file__).resolve().parent.parent.parent.parent.parent / "analyst" / "analyst_config.json"


def get_cfg() -> Dict[str, Any]:
    try:
        if CONFIG_FILE.exists():
            cfg_data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if "oi_studio" in cfg_data:
                return cfg_data["oi_studio"]
    except Exception:
        pass
    return config.rules().get("oi_studio", {})


def _format_time_label(epoch: int) -> str:
    try:
        dt = datetime.fromtimestamp(epoch, tz=IST)
        return dt.strftime("%I:%M %p").lstrip("0")
    except Exception:
        return "09:15 AM"


def _format_date_short(date_str: str) -> str:
    """Formats '2026-11-03' to '03 Nov'."""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        return dt.strftime("%d %b")
    except Exception:
        return date_str


def compute_oi_studio_payload(
    snapshots: List[Dict[str, Any]],
    prev_day_baseline: Optional[Dict[float, Dict[str, float]]] = None,
    spot_price: float = 22700.0,
    start_ts: Optional[int] = None,
    end_ts: Optional[int] = None,
    strike_range: str = "10",
    min_strike: Optional[float] = None,
    max_strike: Optional[float] = None,
    lot_size: int = 65,
    cfg: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Computes StockMojo 1:1 Strike-wise OI Studio data between any [start_ts, end_ts] window:
    - Tab 1: OI Change + Total (base OI, increase hatched segment, decrease hollow segment)
    - Tab 2: OI Change (delta OI & % change between start_ts and end_ts)
    - Tab 3: Total OI (standing Call OI vs Put OI at end_ts)
    - Summary Cards: Window OI Change (Call vs Put), Total OI (Call vs Put), PCR Donut, Sentiment %
    - Max Pain calculation across full chain at end_ts
    """
    c = cfg or get_cfg()
    lot_size = int(c.get("lot_size", lot_size or 65))

    if not snapshots:
        return {
            "ok": False,
            "message": "No snapshot data available for OI Studio.",
            "strikes": [],
            "timeline": [],
            "summary": {},
        }

    # 1. Build full intraday timeline for the slider & replay dock
    timeline = [
        {
            "ts": int(s["ts"]),
            "time_label": s.get("time_label") or _format_time_label(int(s["ts"])),
            "spot": float(s.get("spot") or spot_price),
        }
        for s in snapshots
    ]
    first_snap = snapshots[0]
    last_snap = snapshots[-1]

    # Resolve end_snap from end_ts
    end_snap = last_snap
    if end_ts is not None:
        prior = [s for s in snapshots if int(s["ts"]) <= int(end_ts)]
        if prior:
            end_snap = prior[-1]

    # Resolve start_snap from start_ts
    use_prev_baseline = (start_ts is None) or (int(start_ts) <= int(first_snap["ts"]) + 60)
    start_snap = first_snap
    if start_ts is not None and not use_prev_baseline:
        after = [s for s in snapshots if int(s["ts"]) >= int(start_ts)]
        if after:
            start_snap = after[0]

    active_spot = float(end_snap.get("spot") or spot_price or 22700.0)
    atm_strike = round(active_spot / 50.0) * 50.0

    # 2. Compute Max Pain at end_snap across all strikes
    end_strikes_map: Dict[float, Dict[str, float]] = end_snap.get("strikes", {})
    start_strikes_map: Dict[float, Dict[str, float]] = (
        prev_day_baseline if (use_prev_baseline and prev_day_baseline) else start_snap.get("strikes", {})
    )

    all_strike_keys = sorted(end_strikes_map.keys())
    max_pain_strike = atm_strike
    if all_strike_keys:
        min_pain_val = float("inf")
        for candidate_s in all_strike_keys:
            pain = 0.0
            for k in all_strike_keys:
                c_oi = float(end_strikes_map[k].get("call_oi", 0.0))
                p_oi = float(end_strikes_map[k].get("put_oi", 0.0))
                if candidate_s > k:
                    pain += (candidate_s - k) * c_oi
                elif candidate_s < k:
                    pain += (k - candidate_s) * p_oi
            if pain < min_pain_val:
                min_pain_val = pain
                max_pain_strike = candidate_s

    # 3. Filter strikes by strike_range or [min_strike, max_strike]
    if min_strike is not None and max_strike is not None:
        lo_s, hi_s = float(min_strike), float(max_strike)
    elif strike_range == "all":
        lo_s = all_strike_keys[0] if all_strike_keys else atm_strike - 1000
        hi_s = all_strike_keys[-1] if all_strike_keys else atm_strike + 1000
    elif strike_range == "atm":
        lo_s, hi_s = atm_strike - 100, atm_strike + 100
    else:
        n_steps = int(strike_range) if str(strike_range).isdigit() else 10
        lo_s, hi_s = atm_strike - (n_steps * 50), atm_strike + (n_steps * 50)

    filtered_keys = [k for k in all_strike_keys if lo_s <= k <= hi_s]

    strike_rows = []
    sum_call_total = 0.0
    sum_put_total = 0.0
    sum_call_chg = 0.0
    sum_put_chg = 0.0

    for k in filtered_keys:
        e_row = end_strikes_map.get(k, {})
        s_row = start_strikes_map.get(k, {})

        call_total = float(e_row.get("call_oi", 0.0))
        put_total = float(e_row.get("put_oi", 0.0))

        # Baseline resolution
        if use_prev_baseline and ("call_oi_chg" in e_row) and not prev_day_baseline:
            call_chg = float(e_row.get("call_oi_chg", 0.0))
            put_chg = float(e_row.get("put_oi_chg", 0.0))
            call_base = max(0.0, call_total - call_chg)
            put_base = max(0.0, put_total - put_chg)
        else:
            call_base = float(s_row.get("call_oi", call_total))
            put_base = float(s_row.get("put_oi", put_total))
            call_chg = call_total - call_base
            put_chg = put_total - put_base

        call_chg_pct = round((call_chg / call_base) * 100.0, 1) if call_base > 0 else (100.0 if call_chg > 0 else 0.0)
        put_chg_pct = round((put_chg / put_base) * 100.0, 1) if put_base > 0 else (100.0 if put_chg > 0 else 0.0)

        # Tab 1: OI Change + Total segments
        call_solid = call_base if call_chg >= 0 else call_total
        call_inc_hatched = max(0.0, call_chg)
        call_dec_hollow = abs(min(0.0, call_chg))

        put_solid = put_base if put_chg >= 0 else put_total
        put_inc_hatched = max(0.0, put_chg)
        put_dec_hollow = abs(min(0.0, put_chg))

        strike_rows.append({
            "strike": int(k) if k == int(k) else k,
            "call_oi_total": round(call_total, 1),
            "put_oi_total": round(put_total, 1),
            "call_oi_base": round(call_base, 1),
            "put_oi_base": round(put_base, 1),
            "call_oi_chg": round(call_chg, 1),
            "put_oi_chg": round(put_chg, 1),
            "call_chg_pct": call_chg_pct,
            "put_chg_pct": put_chg_pct,
            "call_solid": round(call_solid, 1),
            "call_inc_hatched": round(call_inc_hatched, 1),
            "call_dec_hollow": round(call_dec_hollow, 1),
            "put_solid": round(put_solid, 1),
            "put_inc_hatched": round(put_inc_hatched, 1),
            "put_dec_hollow": round(put_dec_hollow, 1),
            "net_pe_ce_total": round(put_total - call_total, 1),
            "net_pe_ce_chg": round(put_chg - call_chg, 1),
            "strike_pcr": round(put_total / max(call_total, 1.0), 2),
        })

        sum_call_total += call_total
        sum_put_total += put_total
        sum_call_chg += call_chg
        sum_put_chg += put_chg

    pcr_total = round(sum_put_total / max(sum_call_total, 1.0), 2)
    pcr_chg = round(sum_put_chg / max(sum_call_chg, 1.0), 2) if sum_call_chg > 0 else 1.0
    total_combined = sum_put_total + sum_call_total
    put_share_pct = round((sum_put_total / total_combined) * 100.0) if total_combined > 0 else 50
    call_share_pct = 100 - put_share_pct

    # Market Sentiment Score & Direction
    chg_weight = float(c.get("sentiment_chg_weight", 0.60))
    tot_weight = float(c.get("sentiment_total_weight", 0.40))
    bull_thresh = float(c.get("sentiment_bullish_threshold", 58))
    bear_thresh = float(c.get("sentiment_bearish_threshold", 42))

    chg_denom = abs(sum_put_chg) + abs(sum_call_chg)
    chg_ratio = ((sum_put_chg - sum_call_chg) / chg_denom) if chg_denom > 0 else 0.0
    tot_ratio = ((sum_put_total - sum_call_total) / total_combined) if total_combined > 0 else 0.0

    sentiment_score = int(round(max(0.0, min(100.0, 50.0 + (chg_ratio * (chg_weight * 50.0) + tot_ratio * (tot_weight * 50.0))))))
    if sentiment_score >= bull_thresh:
        sentiment_label = "Bullish"
        sentiment_sub = "Put writers dominating support"
    elif sentiment_score <= bear_thresh:
        sentiment_label = "Bearish"
        sentiment_sub = "Call writers capping upside"
    else:
        sentiment_label = "Neutral"
        sentiment_sub = "Balanced market conditions"

    return {
        "ok": True,
        "spot": round(active_spot, 2),
        "atm_strike": int(atm_strike),
        "max_pain": int(max_pain_strike),
        "lot_size": lot_size,
        "min_strike": int(lo_s),
        "max_strike": int(hi_s),
        "all_min_strike": int(all_strike_keys[0]) if all_strike_keys else int(atm_strike - 1000),
        "all_max_strike": int(all_strike_keys[-1]) if all_strike_keys else int(atm_strike + 1000),
        "window_start_ts": int(start_snap["ts"]),
        "window_start_label": "09:15 AM" if use_prev_baseline else start_snap.get("time_label", "09:15 AM"),
        "window_end_ts": int(end_snap["ts"]),
        "window_end_label": end_snap.get("time_label", _format_time_label(int(end_snap["ts"]))),
        "last_updated": end_snap.get("time_label", _format_time_label(int(end_snap["ts"]))),
        "strikes": strike_rows,
        "timeline": timeline,
        "summary": {
            "call_oi_chg": round(sum_call_chg, 1),
            "put_oi_chg": round(sum_put_chg, 1),
            "call_oi_total": round(sum_call_total, 1),
            "put_oi_total": round(sum_put_total, 1),
            "pcr": pcr_total,
            "pcr_chg": pcr_chg,
            "put_share_pct": put_share_pct,
            "call_share_pct": call_share_pct,
            "sentiment_score": sentiment_score,
            "sentiment_label": sentiment_label,
            "sentiment_sub": sentiment_sub,
        },
    }


def get_oi_studio_data(
    expiry_mode: str = "this_wk",
    start_ts: Optional[int] = None,
    end_ts: Optional[int] = None,
    strike_range: str = "10",
    min_strike: Optional[float] = None,
    max_strike: Optional[float] = None,
    cfg: Optional[Dict[str, Any]] = None,
    include_positioning: bool = False,
    bucket_minutes: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Main database query and orchestration for GET /api/chain/oi-studio.
    Resolves session snapshots, expiries, spot prices, and baseline state.
    """
    c = cfg or get_cfg()
    today_dt = now_ist().date()
    today_start, today_end = session_bounds(today_dt)
    today_iso = today_dt.isoformat()

    # 1. Resolve active expiries in DB
    active_rows = db.query(
        "SELECT DISTINCT expiry FROM chain_snap WHERE expiry >= ? AND ts >= ? ORDER BY expiry ASC",
        (today_iso, today_start),
    )
    if not active_rows:
        active_rows = db.query(
            "SELECT DISTINCT expiry FROM chain_latest WHERE expiry >= ? ORDER BY expiry ASC",
            (today_iso,),
        )
    if not active_rows:
        active_rows = db.query(
            "SELECT DISTINCT expiry FROM chain_snap WHERE expiry >= ? ORDER BY expiry ASC",
            (today_iso,),
        )
    if not active_rows:
        fallback_rows = db.query(
            "SELECT DISTINCT expiry FROM chain_snap ORDER BY expiry DESC LIMIT 2"
        )
        active_exps = sorted([r["expiry"] for r in fallback_rows]) if fallback_rows else ["2026-11-03"]
    else:
        active_exps = [r["expiry"] for r in active_rows]

    cur_exp = active_exps[0] if len(active_exps) > 0 else "2026-11-03"
    next_exp = active_exps[1] if len(active_exps) > 1 else cur_exp

    norm_mode = expiry_mode.lower().strip()
    if norm_mode in ("this_wk", "cur", "this"):
        target_exps = [cur_exp]
        active_mode = "this_wk"
    elif norm_mode in ("next_wk", "next"):
        target_exps = [next_exp]
        active_mode = "next_wk"
    elif norm_mode in ("both", "both_wk", "combined"):
        target_exps = [cur_exp, next_exp] if next_exp != cur_exp else [cur_exp]
        active_mode = "both"
    else:
        target_exps = [expiry_mode]
        active_mode = "custom"

    # 2. Query session snapshots
    placeholders = ",".join("?" for _ in target_exps)
    snap_rows = db.query(f"""
        SELECT expiry, ts, strike, ce_oi, pe_oi, ce_ltp, pe_ltp, ce_vol, pe_vol
        FROM chain_snap
        WHERE expiry IN ({placeholders}) AND ts >= ? AND ts <= ?
        ORDER BY ts ASC, strike ASC
    """, (*target_exps, today_start, today_end))

    # Fallback to latest session in DB if market not open today
    if not snap_rows:
        latest_ts_row = db.query(f"""
            SELECT ts FROM chain_snap
            WHERE expiry IN ({placeholders})
            ORDER BY ts DESC LIMIT 1
        """, (*target_exps,))
        if latest_ts_row:
            latest_dt = datetime.fromtimestamp(latest_ts_row[0]["ts"], tz=IST).date()
            s_start, s_end = session_bounds(latest_dt)
            snap_rows = db.query(f"""
                SELECT expiry, ts, strike, ce_oi, pe_oi, ce_ltp, pe_ltp, ce_vol, pe_vol
                FROM chain_snap
                WHERE expiry IN ({placeholders}) AND ts >= ? AND ts <= ?
                ORDER BY ts ASC, strike ASC
            """, (*target_exps, s_start, s_end))
            today_start, today_end = s_start, s_end

    # 3. Fetch spot price candles for mapping spot to snapshots
    spot_candles = db.candles_1m("13", today_start)
    if not spot_candles:
        spot_candles = db.candles_1m("FUT", today_start)

    spot_by_ts: Dict[int, float] = {}
    last_spot = 22700.0
    for sc in (spot_candles or []):
        t_c = int(sc["ts"])
        sp = float(sc.get("c") or sc.get("close") or last_spot)
        spot_by_ts[t_c] = sp
        last_spot = sp

    def _lookup_spot(t: int) -> float:
        if t in spot_by_ts:
            return spot_by_ts[t]
        prior_ts = [k for k in spot_by_ts if k <= t]
        if prior_ts:
            return spot_by_ts[max(prior_ts)]
        return last_spot

    # 4. Group rows into coherent snapshot timeline objects
    # Handle multi-expiry merge for 'both' mode
    snapshots_dict: Dict[int, Dict[str, Any]] = {}
    # Running state per expiry per strike to handle timestamps where expiries logged at slight offset
    latest_per_exp: Dict[str, Dict[float, Dict[str, float]]] = {exp: {} for exp in target_exps}

    # Unique timestamps sorted
    distinct_ts = sorted({int(r["ts"]) for r in snap_rows})

    # Group raw rows by ts
    raw_by_ts: Dict[int, List[Dict[str, Any]]] = {}
    for r in snap_rows:
        raw_by_ts.setdefault(int(r["ts"]), []).append(r)

    processed_snapshots: List[Dict[str, Any]] = []

    for t in distinct_ts:
        rows_at_t = raw_by_ts[t]
        for r in rows_at_t:
            exp = r["expiry"]
            stk = float(r["strike"])
            latest_per_exp[exp][stk] = {
                "call_oi": float(r["ce_oi"] if r["ce_oi"] is not None else 0.0),
                "put_oi": float(r["pe_oi"] if r["pe_oi"] is not None else 0.0),
                "call_vol": float(r["ce_vol"] if r["ce_vol"] is not None else 0.0),
                "put_vol": float(r["pe_vol"] if r["pe_vol"] is not None else 0.0),
            }

        # Combine across active expiries
        all_stks = sorted({s for exp_map in latest_per_exp.values() for s in exp_map.keys()})
        merged_strikes: Dict[float, Dict[str, float]] = {}
        for s in all_stks:
            tot_c_oi = sum(latest_per_exp[exp][s]["call_oi"] for exp in target_exps if s in latest_per_exp[exp])
            tot_p_oi = sum(latest_per_exp[exp][s]["put_oi"] for exp in target_exps if s in latest_per_exp[exp])
            tot_c_vol = sum(latest_per_exp[exp][s]["call_vol"] for exp in target_exps if s in latest_per_exp[exp])
            tot_p_vol = sum(latest_per_exp[exp][s]["put_vol"] for exp in target_exps if s in latest_per_exp[exp])
            merged_strikes[s] = {
                "call_oi": tot_c_oi,
                "put_oi": tot_p_oi,
                "call_vol": tot_c_vol,
                "put_vol": tot_p_vol,
            }

        processed_snapshots.append({
            "ts": t,
            "time_label": _format_time_label(t),
            "spot": _lookup_spot(t),
            "strikes": merged_strikes,
        })

    # Fallback to chain_latest if no snapshots are available
    if not processed_snapshots:
        latest_recs = db.query(f"SELECT expiry, ts, payload FROM chain_latest WHERE expiry IN ({placeholders})", (*target_exps,))
        if latest_recs:
            combined_strikes: Dict[float, Dict[str, float]] = {}
            max_t = 0
            for l_rec in latest_recs:
                t_l = int(l_rec["ts"])
                if t_l > max_t:
                    max_t = t_l
                data = json.loads(l_rec["payload"])
                for r in data.get("rows", []):
                    stk = float(r["strike"])
                    prev = combined_strikes.setdefault(stk, {"call_oi": 0.0, "put_oi": 0.0})
                    prev["call_oi"] += float(r.get("ce", {}).get("oi") or 0.0)
                    prev["put_oi"] += float(r.get("pe", {}).get("oi") or 0.0)
            if max_t > 0:
                processed_snapshots.append({
                    "ts": max_t,
                    "time_label": _format_time_label(max_t),
                    "spot": last_spot,
                    "strikes": combined_strikes,
                })

    payload = compute_oi_studio_payload(
        snapshots=processed_snapshots,
        spot_price=last_spot,
        start_ts=start_ts,
        end_ts=end_ts,
        strike_range=strike_range,
        min_strike=min_strike,
        max_strike=max_strike,
        cfg=c,
    )

    if include_positioning:
        # Optional projection only: legacy payload and its calculations stay untouched.
        helper = _positioning_builder()
        nif = db.candles_1m("13", today_start)
        payload["positioning"] = helper(snap_rows, nif or [], payload, target_exps, c, bucket_minutes)

    # Attach Expiries Metadata for frontend tab buttons
    payload["expiries_meta"] = {
        "cur_expiry": cur_exp,
        "cur_expiry_label": f"This Wk ({_format_date_short(cur_exp)})",
        "next_expiry": next_exp,
        "next_expiry_label": f"Next Wk ({_format_date_short(next_exp)})",
        "both_label": "Both (Combined)",
        "active_mode": active_mode,
        "active_expiry_str": f"{cur_exp} + {next_exp}" if active_mode == "both" else (next_exp if active_mode == "next_wk" else cur_exp),
    }

    return payload


@lru_cache(maxsize=1)
def _positioning_builder():
    import importlib.util
    path = CONFIG_FILE.parent / "strike_positioning.py"
    spec = importlib.util.spec_from_file_location("desk_strike_positioning", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.build_positioning
