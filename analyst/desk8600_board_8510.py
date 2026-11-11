"""The 8600 board: every option-chain read the desk has, one switch per read.

WHAT IT IS
    One list of "sources". Each source says CE, PE or NEUTRAL right now:

      from the 8600 app (read through the existing read-only signal bridge,
      refreshed every 20 seconds, on closed 3-minute bars):
        tape_3m          the 3-minute tape verdict (the bar-flow table's action tone)
        ticket           a DIP BUY / PULLBACK SELL ticket on the latest 3-minute bar
        high_conviction  the same, only when the 8600 app marks it high conviction
        writer_flow      Classic Writer Flow (put change minus call change, and balance)
        fresh_oi         Fresh Change in OI (PCR trinity)
        total_oi         Cumulative Total OI (PCR trinity)
        pcr_futures      PCR OI vs Futures (PCR trinity)

      from the recorded option-chain snapshots (analyst/oi_context_8510.py):
        day_oi           OI added since 09:15, puts against calls
        oi_15m           the last 15 minutes of that number
        oi_3m            the last 3 minutes of that number
        walls            room to the heavy call strike above / put strike below

    The owner switches each source to FILTER from the cockpit. A signal from a
    lane the board applies to is then judged against the switched-on sources:

      rule NO_OPPOSE  skip when any switched-on source points the other way
      rule MAJORITY   skip unless more switched-on sources agree than oppose
      neutral_blocks  also skip when a switched-on source is neutral
                      (with `ticket` on, this means: trade only on an 8600 ticket)

    Every signal is recorded with every source's reading whether or not any
    switch is on, so the record can show which sources would have helped.

WHAT IT IS NOT
    It places no orders and creates no signals. It never raises. A source that
    is missing or stale is "no data" and never causes a skip. Nothing in the
    locked 8600 modules is imported or changed.

STATUS: unproven. All switches are off until the owner turns them on.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

SOURCES: List[Dict[str, str]] = [
    {"id": "tape_3m", "label": "3-min tape verdict", "origin": "8600", "group": "State Change"},
    {"id": "ticket", "label": "8600 ticket (Dip Buy / Pullback Sell)", "origin": "8600", "group": "Actionable Setups"},
    {"id": "high_conviction", "label": "8600 high-conviction ticket", "origin": "8600", "group": "High Conviction"},
    {"id": "writer_flow", "label": "Classic Writer Flow", "origin": "8600", "group": "Classic Writer Flow"},
    {"id": "fresh_oi", "label": "Fresh Change in OI", "origin": "8600", "group": "Fresh Change in OI"},
    {"id": "total_oi", "label": "Cumulative Total OI", "origin": "8600", "group": "Cumulative Total OI"},
    {"id": "pcr_futures", "label": "PCR OI vs Futures", "origin": "8600", "group": "PCR OI vs Futures"},
    {"id": "day_oi", "label": "Day OI added since 09:15 (PE − CE)", "origin": "chain"},
    {"id": "oi_15m", "label": "15-min OI impulse (PE − CE)", "origin": "chain"},
    {"id": "oi_3m", "label": "3-min OI bar (PE − CE)", "origin": "chain"},
    {"id": "walls", "label": "OI walls — room to target", "origin": "chain"},
]
SOURCE_IDS = [s["id"] for s in SOURCES]
TICKET_SOURCES = ("ticket", "high_conviction")

LANES: List[Dict[str, str]] = [
    {"prefix": "EMA8_PROTOTYPE", "label": "8 EMA Prototype"},
    {"prefix": "STAGE_2", "label": "Stage 2"},
    {"prefix": "STAGE_3", "label": "Stage 3"},
    {"prefix": "CROSSOVER", "label": "8 EMA Reversal"},
    {"prefix": "FLAGSHIP", "label": "1H Flagship"},
    {"prefix": "H1_8EMA", "label": "1H 8 EMA"},
]
LANE_PREFIXES = [x["prefix"] for x in LANES]
RULES = ("NO_OPPOSE", "MAJORITY")

DEFAULT_CFG: Dict[str, Any] = {
    "enabled": True,                       # build + record
    "sources": {},                         # id -> "FILTER"; anything else / missing = off
    "rule": "NO_OPPOSE",
    "neutral_blocks": False,
    "apply_to": ["EMA8_PROTOTYPE"],
    "max_age_sec": 420,                    # a closed 3-minute bar is at most ~3.5 minutes old
    "flow_min_lots": 0.0,                  # |3m / 15m PE-CE| below this = NEUTRAL (0 = sign only)
}


def load_cfg(scanner_cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    out = dict(DEFAULT_CFG)
    raw = (scanner_cfg or {}).get("desk8600_board") or {}
    out.update(raw)
    out["sources"] = {k: "FILTER" for k, v in (raw.get("sources") or {}).items()
                      if k in SOURCE_IDS and str(v).upper() == "FILTER"}
    out["rule"] = str(out.get("rule") or "NO_OPPOSE").upper()
    if out["rule"] not in RULES:
        out["rule"] = "NO_OPPOSE"
    out["apply_to"] = [p for p in (out.get("apply_to") or []) if p in LANE_PREFIXES]
    out["neutral_blocks"] = bool(out.get("neutral_blocks"))
    return out


def apply_update(scanner_cfg: Dict[str, Any], updates: Dict[str, Any]) -> Dict[str, Any]:
    """Apply a cockpit change to scanner_cfg['desk8600_board'] in place. Unknown keys are ignored.

    Accepted: {"source": id, "on": bool} | {"rule": "NO_OPPOSE"|"MAJORITY"} |
              {"neutral_blocks": bool} | {"lane": prefix, "on": bool} | {"all_sources": bool}
    """
    cur = dict((scanner_cfg or {}).get("desk8600_board") or {})
    sources = {k: v for k, v in (cur.get("sources") or {}).items() if k in SOURCE_IDS}
    lanes = [p for p in (cur.get("apply_to") if "apply_to" in cur else DEFAULT_CFG["apply_to"]) if p in LANE_PREFIXES]
    u = updates or {}
    on = u.get("on") is True or str(u.get("on")).lower() == "true"
    if u.get("source") in SOURCE_IDS:
        if on:
            sources[u["source"]] = "FILTER"
        else:
            sources.pop(u["source"], None)
    if "all_sources" in u:
        sources = {i: "FILTER" for i in SOURCE_IDS} if (u["all_sources"] is True) else {}
    if str(u.get("rule") or "").upper() in RULES:
        cur["rule"] = str(u["rule"]).upper()
    if "neutral_blocks" in u:
        cur["neutral_blocks"] = u["neutral_blocks"] is True or str(u["neutral_blocks"]).lower() == "true"
    if u.get("lane") in LANE_PREFIXES:
        if on and u["lane"] not in lanes:
            lanes.append(u["lane"])
        if not on and u["lane"] in lanes:
            lanes.remove(u["lane"])
    cur["sources"] = sources
    cur["apply_to"] = lanes
    scanner_cfg["desk8600_board"] = cur
    return load_cfg(scanner_cfg)


def _lakh(v: Any) -> str:
    try:
        return f"{float(v) / 1e5:+.2f}L"
    except (TypeError, ValueError):
        return "—"


def _age(stamp: Any, now: datetime) -> Optional[float]:
    try:
        return max(0.0, (now - datetime.fromisoformat(str(stamp))).total_seconds())
    except (TypeError, ValueError):
        return None


def _bridge_sources(last: Dict[str, Any], now: datetime, max_age: float) -> Dict[str, Dict[str, Any]]:
    rows = {str(r.get("signal_group")): r for r in ((last or {}).get("rows") or []) if isinstance(r, dict)}
    out: Dict[str, Dict[str, Any]] = {}
    for src in SOURCES:
        if src["origin"] != "8600":
            continue
        r = rows.get(src["group"])
        item: Dict[str, Any] = {"direction": None, "detail": "8600 app not answering", "age_sec": None}
        if r is not None:
            age = _age(r.get("source_ts"), now)
            verdict = str(r.get("verdict") or "")
            if verdict == "UNAVAILABLE" or age is None:
                item["detail"] = str(r.get("reason") or "not available")[:120]
            elif age > max_age:
                item.update(age_sec=round(age), detail=f"last closed bar is {int(age // 60)} min old")
            else:
                d = str(r.get("direction") or "NEUTRAL").upper()
                d = d if d in ("CE", "PE") else "NEUTRAL"
                if src["id"] in TICKET_SOURCES and not bool(r.get("significant")):
                    item.update(direction="NEUTRAL", detail=f"no ticket on the latest bar ({verdict})")
                else:
                    item.update(direction=d, detail=verdict)
                item["age_sec"] = round(age)
        out[src["id"]] = item
    return out


def _chain_sources(ctx: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    ok = (ctx or {}).get("status") == "OK"
    why = str((ctx or {}).get("reason") or "no option-chain snapshots")
    age = (ctx or {}).get("snapshot_age_sec") if ok else None
    floor = float(cfg.get("flow_min_lots", 0.0) or 0.0)

    def flow(value: Any, call: Any, put: Any) -> Dict[str, Any]:
        if not ok or value is None:
            return {"direction": None, "detail": why if not ok else "not enough snapshots yet", "age_sec": age}
        v = float(value)
        d = "NEUTRAL" if abs(v) <= floor else ("CE" if v > 0 else "PE")
        return {"direction": d, "detail": f"PE−CE {_lakh(v)} (puts {_lakh(put)}, calls {_lakh(call)})", "age_sec": age}

    if ok:
        out["day_oi"] = {
            "direction": ctx.get("bias") or "NEUTRAL",
            "detail": f"PE−CE {_lakh(ctx.get('net_pe_minus_ce'))} ({ctx.get('imbalance_pct')}%) "
                      f"(puts {_lakh(ctx.get('put_added'))}, calls {_lakh(ctx.get('call_added'))})",
            "age_sec": age,
        }
    else:
        out["day_oi"] = {"direction": None, "detail": why, "age_sec": None}
    out["oi_15m"] = flow((ctx or {}).get("impulse_15m"), (ctx or {}).get("call_added_15m"), (ctx or {}).get("put_added_15m"))
    out["oi_3m"] = flow((ctx or {}).get("flow_3m"), (ctx or {}).get("call_added_3m"), (ctx or {}).get("put_added_3m"))

    walls: Dict[str, Any] = {"direction": None, "detail": why, "age_sec": age, "blocks": []}
    if ok:
        need = float(cfg.get("min_room_pts", 40.0) or 40.0)
        up, down = ctx.get("room_up_pts"), ctx.get("room_down_pts")
        blocks: List[str] = []
        if ctx.get("boxed"):
            blocks = ["CE", "PE"]
        else:
            if up is not None and up < need:
                blocks.append("CE")
            if down is not None and down < need:
                blocks.append("PE")
        fmt = lambda wall, room: "none near" if wall is None else f"{float(wall):.0f} ({float(room):.0f} pts)"
        walls.update(
            direction="ROOM",
            blocks=blocks,
            detail=f"call wall {fmt(ctx.get('call_wall_above'), up)} · put wall {fmt(ctx.get('put_wall_below'), down)}"
                   + (" · BOXED" if ctx.get("boxed") else ""),
        )
    out["walls"] = walls
    return out


def judge(sources: List[Dict[str, Any]], direction: str, cfg: Dict[str, Any], strategy_id: str = "") -> Dict[str, Any]:
    """What the switched-on sources say about a LONG (CE) or SHORT (PE) signal."""
    side = "CE" if str(direction).upper() in ("LONG", "CE", "BUY_CE") else "PE"
    other = "PE" if side == "CE" else "CE"
    on = cfg.get("sources") or {}
    votes: List[Dict[str, Any]] = []
    agree: List[str] = []
    against: List[str] = []
    neutral: List[str] = []
    all_against: List[str] = []
    for s in sources:
        d = s.get("direction")
        if d is None:
            v = "NO_DATA"
        elif d == "ROOM":
            v = "AGAINST" if side in (s.get("blocks") or []) else "OK"
        elif d == side:
            v = "AGREE"
        elif d == other:
            v = "AGAINST"
        else:
            v = "NEUTRAL"
        used = s["id"] in on
        votes.append({"id": s["id"], "direction": d, "verdict": v, "used": used})
        if v == "AGAINST":
            all_against.append(s["id"])
        if not used:
            continue
        if v == "AGREE":
            agree.append(s["label"])
        elif v == "AGAINST":
            against.append(f"{s['label']}: {s.get('detail')}")
        elif v == "NEUTRAL":
            neutral.append(s["label"])

    reasons: List[str] = []
    rule = cfg.get("rule", "NO_OPPOSE")
    if rule == "MAJORITY":
        if against and len(agree) <= len(against):
            reasons.append(f"{len(against)} against vs {len(agree)} agreeing — " + "; ".join(against))
    elif against:
        reasons.extend(against)
    if cfg.get("neutral_blocks") and neutral:
        reasons.append("neutral: " + ", ".join(neutral))

    sid = str(strategy_id or "").upper()
    applies = (not sid) or any(sid.startswith(p) for p in (cfg.get("apply_to") or []))
    return {
        "side": side,
        "votes": votes,
        "agree": len(agree),
        "against": len(against),
        "neutral": len(neutral),
        "sources_against_all": all_against,       # every source, switched on or not — for the record
        "reasons": reasons,
        "would_block": bool(reasons),
        "applies": applies,
        "blocked": bool(reasons) and applies and bool(sid),
    }


def build_board(bridge_last: Optional[Dict[str, Any]], oi_ctx: Optional[Dict[str, Any]],
                scanner_cfg: Optional[Dict[str, Any]], now: datetime) -> Dict[str, Any]:
    """The whole board for the cockpit and for judging signals. Never raises."""
    cfg = load_cfg(scanner_cfg)
    try:
        merged_cfg = dict(cfg, min_room_pts=((scanner_cfg or {}).get("oi_context") or {}).get("min_room_pts", 40.0))
        vals = _bridge_sources(bridge_last or {}, now, float(cfg.get("max_age_sec", 420)))
        vals.update(_chain_sources(oi_ctx or {}, merged_cfg))
    except Exception as exc:  # a broken read must never stop a trade
        vals = {i: {"direction": None, "detail": f"error: {type(exc).__name__}", "age_sec": None} for i in SOURCE_IDS}
    sources = []
    for src in SOURCES:
        v = vals.get(src["id"]) or {"direction": None, "detail": "", "age_sec": None}
        sources.append({"id": src["id"], "label": src["label"], "origin": src["origin"],
                        "mode": "FILTER" if src["id"] in cfg["sources"] else "OFF", **v})
    return {
        "enabled": bool(cfg.get("enabled", True)),
        "as_of": now.strftime("%H:%M:%S"),
        "bridge_evaluated_at": (bridge_last or {}).get("evaluated_at"),
        "sources": sources,
        "rule": cfg["rule"],
        "neutral_blocks": cfg["neutral_blocks"],
        "apply_to": cfg["apply_to"],
        "lanes": LANES,
        "filters_on": len(cfg["sources"]),
        "preview": {"CE": judge(sources, "LONG", cfg), "PE": judge(sources, "SHORT", cfg)},
    }
