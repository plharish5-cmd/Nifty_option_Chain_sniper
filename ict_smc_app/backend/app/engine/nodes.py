"""
engine/nodes.py — STACKED volume nodes ("stack the ranges"). Display only — never places orders.

Builds three volume profiles from the basis-adjusted NIFTY future (real volume, index prices):
    D = previous session      W = previous 5 sessions      M = previous 20 sessions
(rolling sessions, not calendar week / month, so the contract roll and holidays never leave a profile empty).
Each profile gives its high-volume peaks (POC + HVNs). Where peaks of 2 or 3 profiles sit within `tol` points of
each other we get a STACKED zone — the levels the market has accepted on several horizons → reversal areas.
    grade A+ = all three agree · A = two agree
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List

from ..timeutil import ist_date, now_ist
from .volume import volume_profile


def _peaks(p: Dict[str, Any]) -> List[float]:
    out = list(p.get("hvn") or [])
    if p.get("poc") is not None:
        out.append(float(p["poc"]))
    return sorted(set(round(x, 2) for x in out))


def stacked(mapped: List[Dict[str, float]], step: float = 5.0, tol: float = 15.0, half: float = 7.5, asof: str | None = None,
            history: int = 5) -> Dict[str, Any]:
    by: Dict[str, List[Dict[str, float]]] = defaultdict(list)
    for r in mapped:
        if (r.get("v") or 0) > 0:
            by[ist_date(r["ts"]).isoformat()].append(r)
    days = sorted(by)
    today = asof or now_ist().date().isoformat()
    past = [d for d in days if d < today]              # today is still developing — never part of a profile
    if not past:
        return {"available": False, "message": "need at least one finished session of NIFTY future volume"}
    spec = {"D": past[-1:], "W": past[-5:], "M": past[-20:]}
    profs: Dict[str, Dict[str, Any]] = {}
    for k, ds in spec.items():
        rows = [r for d in ds for r in by[d]]
        p = volume_profile(rows, step)
        if p:
            profs[k] = {"from": ds[0], "to": ds[-1], "sessions": len(ds), "poc": p["poc"], "vah": p["vah"], "val": p["val"],
                        "peaks": _peaks(p), "hist": p["hist"]}
    # cluster peaks across profiles
    pts = sorted((x, k) for k, p in profs.items() for x in p["peaks"])
    zones: List[Dict[str, Any]] = []
    i = 0
    while i < len(pts):
        grp = [pts[i]]
        j = i + 1
        while j < len(pts) and pts[j][0] - grp[0][0] <= tol:
            grp.append(pts[j]); j += 1
        mem = sorted({k for _, k in grp}, key="DWM".index)
        if len(mem) >= 2:
            xs = [x for x, _ in grp]
            zones.append({"lo": round(min(xs) - half, 2), "hi": round(max(xs) + half, 2), "mid": round(sum(xs) / len(xs), 2),
                          "members": mem, "grade": "A+" if len(mem) == 3 else "A",
                          "label": " ".join(mem) + (" A+" if len(mem) == 3 else " A")})
            i = j
        else:
            i += 1
    # drop the lighter of two overlapping zones
    zones.sort(key=lambda z: (-len(z["members"]), z["mid"]))
    keep: List[Dict[str, Any]] = []
    for z in zones:
        if all(z["hi"] < k["lo"] or z["lo"] > k["hi"] for k in keep):
            keep.append(z)
    keep.sort(key=lambda z: z["mid"])
    by_day: Dict[str, List[Dict[str, Any]]] = {}
    if asof is None:                                    # zones AS THEY WERE on each recent day (no look-ahead)
        for d in [x for x in days if x < today][-history:]:
            r = stacked(mapped, step, tol, half, asof=d)
            if r.get("available"):
                by_day[d] = r["zones"]
    return {"available": True, "zones": keep, "by_day": by_day, "profiles": profs, "today": today,
            "note": "D = previous session · W = previous 5 sessions · M = previous 20 sessions (NIFTY FUT volume on index prices)."}
