"""
strikes/context.py — Focused Strike Panel + Context Verdict for the SELECTED CE / PE. Context only: it can nudge
Setup A/C confidence by a small configured amount; it can never create, block or change a trade.

Build / unwind on ONE strike (price change vs yesterday's close, OI change vs yesterday's OI — Dhan chain fields):
    premium ↑ OI ↑  long build      buyers opening
    premium ↓ OI ↑  short build     writers opening (selling that option)
    premium ↓ OI ↓  long unwind     buyers closing
    premium ↑ OI ↓  short covering  writers closing (buying back)
What that means for NIFTY:
    CE long build / CE short covering   → bullish        CE short build / CE long unwind   → bearish
    PE long build / PE short covering   → bearish        PE short build / PE long unwind   → bullish
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..market.chain import build_class

_READ = {
    ("CE", "Long build"): ("bull", "call buyers opening — upside being bought"),
    ("CE", "Short build"): ("bear", "call writers adding — they expect NIFTY to stay below this strike (ceiling)"),
    ("CE", "Long unwind"): ("bear", "call buyers exiting — upside interest fading"),
    ("CE", "Short covering"): ("bull", "call writers running — the ceiling is giving way"),
    ("PE", "Long build"): ("bear", "put buyers opening — downside being bought"),
    ("PE", "Short build"): ("bull", "put writers adding — they expect NIFTY to hold above this strike (floor)"),
    ("PE", "Long unwind"): ("bull", "put buyers exiting — downside interest fading"),
    ("PE", "Short covering"): ("bear", "put writers running — the floor is giving way"),
}


def focus(right: str, row: Optional[Dict[str, Any]], label: str) -> Dict[str, Any]:
    if not row:
        return {"label": label, "available": False, "line": f"{label}: not in the latest chain snapshot."}
    kind = build_class(float(row.get("chg") or 0), float(row.get("oi_chg") or 0))
    lean, why = _READ.get((right, kind), ("neutral", "no OI change"))
    pct = row.get("oi_chg_pct")
    line = (f"{label}: {kind.lower() if kind != '—' else 'no change'} — premium {row.get('chg', 0):+,.1f}, "
            f"OI {row.get('oi_chg', 0):+,.0f}{f' ({pct:+.1f} %)' if pct is not None else ''} → {why}.")
    return {"label": label, "available": True, "kind": kind, "lean": lean, "line": line,
            "ltp": row.get("ltp"), "chg": row.get("chg"), "oi": row.get("oi"), "oi_chg": row.get("oi_chg"),
            "oi_chg_pct": pct, "iv": row.get("iv"), "delta": row.get("delta"), "gamma": row.get("gamma"),
            "theta": row.get("theta"), "vega": row.get("vega"), "vol": row.get("vol"),
            "bid": row.get("bid"), "ask": row.get("ask")}


def verdict(side: Optional[str], foci: List[Dict[str, Any]]) -> Dict[str, Any]:
    """side = 'long' | 'short' | None (current playbook direction). Returns supportive / opposing / neutral."""
    if side not in ("long", "short"):
        return {"verdict": "neutral", "side": side, "lines": ["No playbook direction yet — context shown, not scored."]}
    want = "bull" if side == "long" else "bear"
    sup = [f["line"] for f in foci if f.get("available") and f["lean"] == want]
    opp = [f["line"] for f in foci if f.get("available") and f["lean"] not in (want, "neutral")]
    v = "supportive" if len(sup) > len(opp) else "opposing" if len(opp) > len(sup) else "neutral"
    lines = [f"Selected strikes are {v} for a {side} (context only, never a trigger)."]
    lines += [f"✓ {x}" for x in sup] + [f"✕ {x}" for x in opp]
    return {"verdict": v, "side": side, "lines": lines}
