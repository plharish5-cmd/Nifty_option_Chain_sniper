"""Option-chain context for 8510 signals: day OI bias, walls, and three gates.

WHAT IT ANSWERS, for a signal about to be taken:
    1. DIRECTION — since 09:15, have more puts or more calls been added?
       More puts added  -> writers are building a floor  -> bias CE.
       More calls added -> writers are building a ceiling -> bias PE.
       Close together   -> NEUTRAL: nobody is in control (consolidation).
    2. AGREEMENT — over the last 30 minutes, is that bias still growing, or
       is the flow now running against it?
    3. ROOM — how far is the nearest heavy call wall above / put wall below?
       Is spot boxed between two walls?

It reads the option-chain snapshots the desk already records (`chain_snap`:
expiry, ts, strike, ce_oi, pe_oi, ce_vol, pe_vol ...). It does not import or
change any locked module; the PCR Trinity, Smart OI and PE-CE suites are
untouched.

STATUS OF THE IDEA: unproven. Open interest shows that positions were added,
not who added them or why. This module computes and records the context for
every signal so the question "were the filtered-out trades really the
losers?" can be answered from data. Whether a gate may BLOCK a trade is a
separate switch, and it is off unless the owner turns it on.

Fail-open: with no snapshots, a stale snapshot, or any error, the context is
NO_DATA and no gate blocks anything.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

IST = timezone(timedelta(hours=5, minutes=30))

DEFAULT_CFG: Dict[str, Any] = {
    "enabled": True,                 # compute + record. Blocks nothing: blocking is the 8600 board's job.
    "gate_direction": True,
    "gate_agreement": True,
    "gate_room": True,
    "band_pts": 1000.0,              # strikes within spot +/- this are counted
    "baseline_hm": "09:15",          # "fresh change since"
    "dead_zone_frac": 0.20,          # |put add - call add| below 20% of the total = NEUTRAL
    "slope_minutes": 30,
    "slope_frac": 0.10,              # a 30-min move against the bias of >= 10% of the total = FADING
    "wall_top_n": 3,                 # the N heaviest strikes on each side count as walls
    "min_room_pts": 40.0,            # room needed to the wall in the trade's direction
    "box_pts": 100.0,                # walls this close together on both sides = boxed (range)
    "max_snapshot_age_sec": 600,     # older than this = NO_DATA
}


def load_cfg(scanner_cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    out = dict(DEFAULT_CFG)
    out.update((scanner_cfg or {}).get("oi_context") or {})
    return out


def _day_bounds(day: str) -> Tuple[int, int]:
    start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=IST)
    return int(start.timestamp()), int((start + timedelta(days=1)).timestamp())


def _hm_ts(day: str, hm: str) -> int:
    return int(datetime.strptime(f"{day} {hm}", "%Y-%m-%d %H:%M").replace(tzinfo=IST).timestamp())


def load_day_snapshots(db_paths: Iterable[Path], day: str, asof_ts: Optional[int] = None) -> List[Dict[str, Any]]:
    """Rows of `chain_snap` for one IST day from the first database that has any, oldest first.

    Uses the nearest expiry on or after `day` that has rows that day ("this week").
    """
    lo, hi = _day_bounds(day)
    if asof_ts is not None:
        hi = min(hi, int(asof_ts) + 1)
    for path in db_paths:
        try:
            if not Path(path).is_file():
                continue
            with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5) as conn:
                conn.row_factory = sqlite3.Row
                expiries = [
                    str(r[0]) for r in conn.execute(
                        "SELECT DISTINCT expiry FROM chain_snap WHERE ts >= ? AND ts < ? ORDER BY expiry", (lo, hi)
                    )
                ]
                if not expiries:
                    continue
                upcoming = [e for e in expiries if e[:10] >= day] or expiries
                expiry = upcoming[0]
                rows = conn.execute(
                    "SELECT ts, strike, ce_oi, pe_oi, ce_vol, pe_vol FROM chain_snap "
                    "WHERE expiry = ? AND ts >= ? AND ts < ? ORDER BY ts, strike",
                    (expiry, lo, hi),
                ).fetchall()
            if rows:
                return [dict(r) | {"expiry": expiry} for r in rows]
        except sqlite3.Error:
            continue
    return []


def _snapshot_at(by_ts: Dict[int, Dict[float, Dict[str, float]]], ts_sorted: List[int], want: int,
                 first_at_or_after: bool) -> Optional[int]:
    if first_at_or_after:
        for t in ts_sorted:
            if t >= want:
                return t
        return None
    best = None
    for t in ts_sorted:
        if t <= want:
            best = t
        else:
            break
    return best


def compute_context(rows: List[Dict[str, Any]], spot: float, cfg: Dict[str, Any], *, day: str,
                    now_ts: int) -> Dict[str, Any]:
    """The context at `now_ts` from one day's snapshot rows."""
    ctx: Dict[str, Any] = {"status": "NO_DATA", "bias": None, "reason": "", "day": day}
    if not rows or spot <= 0:
        ctx["reason"] = "no option-chain snapshots recorded for this day"
        return ctx

    by_ts: Dict[int, Dict[float, Dict[str, float]]] = {}
    for r in rows:
        t = int(float(r["ts"]))
        if t > now_ts:
            continue
        by_ts.setdefault(t, {})[float(r["strike"])] = {
            "ce_oi": float(r.get("ce_oi") or 0.0), "pe_oi": float(r.get("pe_oi") or 0.0),
            "ce_vol": float(r.get("ce_vol") or 0.0), "pe_vol": float(r.get("pe_vol") or 0.0),
        }
    ts_sorted = sorted(by_ts)
    if not ts_sorted:
        ctx["reason"] = "no snapshot at or before this time"
        return ctx

    base_t = _snapshot_at(by_ts, ts_sorted, _hm_ts(day, str(cfg.get("baseline_hm", "09:15"))), True)
    last_t = ts_sorted[-1]
    if base_t is None or base_t >= last_t:
        ctx["reason"] = "waiting for a second snapshot after the 09:15 baseline"
        return ctx
    age = now_ts - last_t
    if age > int(cfg.get("max_snapshot_age_sec", 600)):
        ctx["reason"] = f"latest snapshot is {age // 60} min old"
        return ctx

    band = float(cfg.get("band_pts", 1000.0))

    def adds(t_from: int, t_to: int) -> Tuple[float, float]:
        a, b = by_ts[t_from], by_ts[t_to]
        call = put = 0.0
        for k, now in b.items():
            if abs(k - spot) > band or k not in a:
                continue
            call += now["ce_oi"] - a[k]["ce_oi"]
            put += now["pe_oi"] - a[k]["pe_oi"]
        return call, put

    call_add, put_add = adds(base_t, last_t)
    net = put_add - call_add                       # PE - CE, as the desk shows it
    total = abs(call_add) + abs(put_add)

    bias: Optional[str] = None
    if total > 0 and abs(net) >= float(cfg.get("dead_zone_frac", 0.20)) * total:
        bias = "CE" if net > 0 else "PE"

    # 30-minute change of the same number
    slope = 0.0
    slope_state = "FLAT"
    prev_t = _snapshot_at(by_ts, ts_sorted, last_t - 60 * int(cfg.get("slope_minutes", 30)), False)
    if prev_t is not None and prev_t > base_t:
        c0, p0 = adds(base_t, prev_t)
        slope = net - (p0 - c0)
        if total > 0 and abs(slope) >= float(cfg.get("slope_frac", 0.10)) * total:
            slope_state = "RISING" if slope > 0 else "FALLING"
    fading = (bias == "CE" and slope_state == "FALLING") or (bias == "PE" and slope_state == "RISING")

    # The fast view, as the 8600 bar-flow table shows it: the last 3-minute bar
    # and the last 15 minutes of the same PE - CE number, plus traded volume.
    def window(minutes: int) -> Tuple[Optional[float], float, float, float, float]:
        t0 = _snapshot_at(by_ts, ts_sorted, last_t - 60 * minutes, False)
        if t0 is None or t0 < base_t or t0 >= last_t:
            return None, 0.0, 0.0, 0.0, 0.0
        c0, p0 = adds(base_t, t0) if t0 > base_t else (0.0, 0.0)
        a, b = by_ts[t0], by_ts[last_t]
        cv = sum(b[k]["ce_vol"] - a[k]["ce_vol"] for k in b if k in a and abs(k - spot) <= band)
        pv = sum(b[k]["pe_vol"] - a[k]["pe_vol"] for k in b if k in a and abs(k - spot) <= band)
        return net - (p0 - c0), call_add - c0, put_add - p0, cv, pv

    flow_3m, call_3m, put_3m, _cv3, _pv3 = window(3)
    impulse_15m, call_15m, put_15m, call_vol_15m, put_vol_15m = window(15)

    # walls from total OI now
    now_chain = by_ts[last_t]
    near = {k: v for k, v in now_chain.items() if abs(k - spot) <= band}
    top_n = int(cfg.get("wall_top_n", 3))
    ce_walls = sorted(sorted(near, key=lambda k: near[k]["ce_oi"], reverse=True)[:top_n])
    pe_walls = sorted(sorted(near, key=lambda k: near[k]["pe_oi"], reverse=True)[:top_n])
    wall_above = min((k for k in ce_walls if k >= spot), default=None)
    wall_below = max((k for k in pe_walls if k <= spot), default=None)
    room_up = (wall_above - spot) if wall_above is not None else None
    room_down = (spot - wall_below) if wall_below is not None else None
    boxed = (
        room_up is not None and room_down is not None
        and (room_up + room_down) <= float(cfg.get("box_pts", 100.0))
    )

    call_vol = sum(v["ce_vol"] for v in near.values())
    put_vol = sum(v["pe_vol"] for v in near.values())

    ctx.update(
        status="OK",
        reason="",
        bias=bias,
        bias_label={"CE": "BULLISH — more puts added (CE only)",
                    "PE": "BEARISH — more calls added (PE only)"}.get(bias or "", "NEUTRAL — neither side in control"),
        call_added=round(call_add, 0),
        put_added=round(put_add, 0),
        net_pe_minus_ce=round(net, 0),
        imbalance_pct=round(100.0 * net / total, 1) if total else 0.0,
        slope_30m=round(slope, 0),
        slope_state=slope_state,
        fading=bool(fading),
        flow_3m=None if flow_3m is None else round(flow_3m, 0),
        call_added_3m=round(call_3m, 0),
        put_added_3m=round(put_3m, 0),
        impulse_15m=None if impulse_15m is None else round(impulse_15m, 0),
        call_added_15m=round(call_15m, 0),
        put_added_15m=round(put_15m, 0),
        call_volume_15m=round(call_vol_15m, 0),
        put_volume_15m=round(put_vol_15m, 0),
        call_wall_above=wall_above,
        put_wall_below=wall_below,
        room_up_pts=None if room_up is None else round(room_up, 1),
        room_down_pts=None if room_down is None else round(room_down, 1),
        boxed=bool(boxed),
        call_volume=round(call_vol, 0),
        put_volume=round(put_vol, 0),
        volume_ratio_put_call=round(put_vol / call_vol, 2) if call_vol > 0 else None,
        expiry=str(rows[0].get("expiry") or ""),
        baseline_time=datetime.fromtimestamp(base_t, IST).strftime("%H:%M"),
        snapshot_time=datetime.fromtimestamp(last_t, IST).strftime("%H:%M:%S"),
        snapshot_age_sec=int(age),
        spot=round(float(spot), 2),
    )
    return ctx


def evaluate_gates(ctx: Dict[str, Any], direction: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """What each gate says about a LONG (CE) or SHORT (PE) signal.

    Returns {"direction": {"pass": bool, "why": str}, "agreement": {...},
             "room": {...}, "would_block": bool, "reasons": [...]}.
    A gate that is switched off, or a context without data, never fails.
    """
    side = "CE" if str(direction).upper() in ("LONG", "CE", "BUY_CE") else "PE"
    out: Dict[str, Any] = {"side": side}
    if ctx.get("status") != "OK":
        for g in ("direction", "agreement", "room"):
            out[g] = {"pass": True, "why": "no data — not judged"}
        out["would_block"] = False
        out["reasons"] = []
        return out

    bias = ctx.get("bias")
    if bias is None:
        d = {"pass": False, "why": "OI is neutral — neither side in control"}
    elif bias != side:
        d = {"pass": False, "why": f"OI bias is {bias}, signal is {side}"}
    else:
        d = {"pass": True, "why": f"OI bias {bias} agrees"}

    if ctx.get("fading") and bias == side:
        a = {"pass": False, "why": f"30-minute flow is {ctx.get('slope_state')} against the {bias} bias"}
    else:
        a = {"pass": True, "why": f"30-minute flow {ctx.get('slope_state')}"}

    need = float(cfg.get("min_room_pts", 40.0))
    room = ctx.get("room_up_pts") if side == "CE" else ctx.get("room_down_pts")
    wall = ctx.get("call_wall_above") if side == "CE" else ctx.get("put_wall_below")
    if ctx.get("boxed"):
        r = {"pass": False, "why": f"boxed between call wall {ctx.get('call_wall_above'):.0f} "
                                   f"and put wall {ctx.get('put_wall_below'):.0f}"}
    elif room is not None and room < need:
        r = {"pass": False, "why": f"only {room:.0f} pts to the {'call' if side == 'CE' else 'put'} wall "
                                   f"at {wall:.0f} (need {need:.0f})"}
    else:
        r = {"pass": True, "why": "no wall inside the target" if room is None else f"{room:.0f} pts of room"}

    out.update(direction=d, agreement=a, room=r)
    reasons = [
        g_res["why"]
        for name, g_res in (("direction", d), ("agreement", a), ("room", r))
        if bool(cfg.get(f"gate_{name}", True)) and not g_res["pass"]
    ]
    out["reasons"] = reasons
    out["would_block"] = bool(reasons)
    return out


def recorded_days(db_paths: Iterable[Path]) -> List[Tuple[str, int]]:
    """(IST day, number of snapshots) for every day with recorded chain snapshots."""
    out: Dict[str, int] = {}
    for path in db_paths:
        try:
            if not Path(path).is_file():
                continue
            with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5) as conn:
                for d, n in conn.execute(
                    "SELECT date(ts, 'unixepoch', '+330 minutes') d, COUNT(DISTINCT CAST(ts AS INTEGER)) "
                    "FROM chain_snap GROUP BY d ORDER BY d"
                ):
                    out[str(d)] = max(out.get(str(d), 0), int(n))
        except sqlite3.Error:
            continue
    return sorted(out.items())


if __name__ == "__main__":
    root = Path(__file__).resolve().parent.parent
    days = recorded_days([root / "data/terminal8510/recording.sqlite3",
                          root / "ict_smc_app/backend/data/ict_smc.sqlite3"])
    print(f"{len(days)} day(s) with recorded option-chain snapshots")
    for d, n in days:
        print(f"  {d}  {n} snapshots")
