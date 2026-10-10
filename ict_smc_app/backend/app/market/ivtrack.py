"""
market/ivtrack.py — IV and delta over time, from the option chain the desk already polls (nearest expiry).

Once a minute it stores: ATM IV (average of the ATM call and put IV), and IV / delta / LTP of the strikes selected
in strike mode. LIVE ONLY — Dhan keeps no history of IV or greeks, so the lines start the first minute the desk
runs in market hours.

Reading (context, never a trigger):
  ATM IV falling while price trends → orderly, "clean" move (option sellers comfortable)
  ATM IV rising                     → fear / uncertainty: wider swings, stops get hunted, premiums richer
  selected strike's |delta| falling → that strike is drifting out of the money — it moves less per NIFTY point
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .. import config, db
from ..timeutil import TF_SECONDS, bucket_start, market_open_now


class IVTrack:
    def __init__(self) -> None:
        self.last_min: Optional[int] = None

    def ingest(self, ch: Dict[str, Any], picks: Dict[str, Optional[Dict[str, Any]]], force: bool = False) -> int:
        """picks: {'CE': {'strike', 'key'} | None, 'PE': …}. Returns rows written."""
        if not (force or market_open_now()) or not ch.get("rows"):
            return 0
        m = bucket_start(int(ch["ts"]), "1m")
        rows = {r["strike"]: r for r in ch["rows"]}
        atm = ch.get("atm")
        out = []
        r = rows.get(atm)
        if r and r["ce"].get("iv") and r["pe"].get("iv"):
            out.append(("ATM", m, round((r["ce"]["iv"] + r["pe"]["iv"]) / 2, 2), None, None, atm))
        for side, p in (picks or {}).items():
            if not p or p.get("strike") not in rows:
                continue
            x = rows[p["strike"]][side.lower()]
            out.append((p["key"], m, x.get("iv"), x.get("delta"), x.get("ltp"), p["strike"]))
        if out:
            db.execmany("INSERT OR REPLACE INTO iv_series (key, ts, iv, delta, ltp, strike) VALUES (?,?,?,?,?,?)", out)
        self.last_min = m
        return len(out)


def series(key: str, since: int) -> List[Dict[str, Any]]:
    return [dict(r) for r in db.query("SELECT ts, iv, delta, ltp, strike FROM iv_series WHERE key=? AND ts>=? ORDER BY ts",
                                      (key, int(since)))]


def per_bar(rows: List[Dict[str, Any]], tf: str) -> Dict[int, Dict[str, Any]]:
    """tf bucket → the last reading inside it (the value at the candle's close)."""
    out: Dict[int, Dict[str, Any]] = {}
    for r in rows:
        out[bucket_start(r["ts"], tf)] = r
    return out


def trend(rows: List[Dict[str, Any]], minutes: int, field: str = "iv") -> Optional[Dict[str, Any]]:
    """Change of `field` over the last `minutes` of readings (same session)."""
    rows = [r for r in rows if r.get(field) is not None]
    if len(rows) < 2:
        return None
    end = rows[-1]
    start = next((r for r in rows if r["ts"] >= end["ts"] - minutes * 60 and r["ts"] // 86400 == end["ts"] // 86400), None)
    if not start or start is end:
        return None
    ch = end[field] - start[field]
    thr = float(config.rule("iv", "trend_min_pts", 0.5)) if field == "iv" else float(config.rule("iv", "delta_min_chg", 0.08))
    word = "flat"
    if ch <= -thr:
        word = "falling"
    elif ch >= thr:
        word = "rising"
    return {"from": start[field], "to": end[field], "chg": round(ch, 3), "word": word, "minutes": (end["ts"] - start["ts"]) // 60}
