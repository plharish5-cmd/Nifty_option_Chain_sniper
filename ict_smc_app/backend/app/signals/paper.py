"""
signals/paper.py — paper trading of the playbook. NO real orders exist anywhere in this codebase.

  * Opens a paper trade only for a TRADE signal on the candle that just closed (entry = that close).
  * One open paper trade at a time — stacking entries is how a paper account flatters a bad day.
  * Outcome is resolved on every CLOSED 1-minute candle after the entry candle:
      stop hit → −1R; target hit → +R:R; a 1m candle that touches BOTH counts as the stop (worst case).
      Still open at square_off (15:15) → closed at that minute's close (EOD).
  * Logs MFE (best R reached) so you can see trades that went your way and came back.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from .. import config, db
from ..timeutil import TF_SECONDS, now_epoch, parse_hm, to_ist
from .playbook import Signal


def open_trade() -> Optional[Dict[str, Any]]:
    r = db.query("SELECT * FROM paper_trades WHERE status = 'OPEN' ORDER BY id DESC LIMIT 1")
    return dict(r[0]) if r else None


def maybe_open(s: Signal) -> Optional[Dict[str, Any]]:
    if not config.rule("paper", "enabled", True) or s.status != "TRADE" or s.setup not in ("A", "C"):
        return None
    if open_trade() is not None:
        return None
    opened = int(s.ts) + TF_SECONDS[s.tf]                     # the entry candle's CLOSE time
    db.execute("INSERT OR IGNORE INTO paper_trades (signal_key, setup, tf, side, opened_ts, entry, sl, tp, risk, rr, "
               "status, mfe_r, notes) VALUES (?,?,?,?,?,?,?,?,?,?, 'OPEN', 0, ?)",
               (s.key, s.setup, s.tf, s.side, opened, s.entry, s.sl, s.tp, s.risk, s.rr, json.dumps(s.story)))
    return open_trade()


def on_1m_close(c: Dict[str, float]) -> Optional[Dict[str, Any]]:
    """c = a CLOSED 1m NIFTY candle. Returns the trade if it closed on this candle."""
    t = open_trade()
    if not t or c["ts"] < t["opened_ts"]:
        return None
    short = t["side"] == "short"
    risk = float(t["risk"]) or 1.0
    fav = (t["entry"] - c["l"]) if short else (c["h"] - t["entry"])
    mfe = max(float(t["mfe_r"] or 0), fav / risk)
    hit_sl = c["h"] >= t["sl"] if short else c["l"] <= t["sl"]
    hit_tp = c["l"] <= t["tp"] if short else c["h"] >= t["tp"]
    sq = parse_hm(config.rule("session", "square_off", "15:15"))
    at_sq = to_ist(c["ts"] + 60).time() >= sq
    status = exit_px = None
    if hit_sl:
        status, exit_px = "SL", t["sl"]
    elif hit_tp:
        status, exit_px = "TP", t["tp"]
    elif at_sq:
        status, exit_px = "EOD", c["c"]
    if status is None:
        db.execute("UPDATE paper_trades SET mfe_r = ? WHERE id = ?", (round(mfe, 2), t["id"]))
        return None
    pts = (t["entry"] - exit_px) if short else (exit_px - t["entry"])
    db.execute("UPDATE paper_trades SET status=?, closed_ts=?, exit=?, r=?, pnl_pts=?, mfe_r=? WHERE id=?",
               (status, int(c["ts"]) + 60, exit_px, round(pts / risk, 2), round(pts, 1), round(mfe, 2), t["id"]))
    r = db.query("SELECT * FROM paper_trades WHERE id = ?", (t["id"],))
    return dict(r[0])


def trades(limit: int = 100) -> List[Dict[str, Any]]:
    out = []
    for r in db.query("SELECT * FROM paper_trades ORDER BY id DESC LIMIT ?", (limit,)):
        d = dict(r)
        d["opened"] = to_ist(d["opened_ts"]).strftime("%d %b %H:%M") if d["opened_ts"] else ""
        d["closed"] = to_ist(d["closed_ts"]).strftime("%H:%M") if d["closed_ts"] else ""
        try:
            d["notes"] = json.loads(d["notes"] or "[]")
        except ValueError:
            d["notes"] = []
        out.append(d)
    return out


def stats() -> Dict[str, Any]:
    done = [t for t in trades(1000) if t["status"] in ("SL", "TP", "EOD")]
    lot = int(config.rule("paper", "lot_size", 65)) * int(config.rule("paper", "lots", 1))
    if not done:
        return {"n": 0, "lot_qty": lot}
    rs = [float(t["r"] or 0) for t in done]
    wins = [r for r in rs if r > 0]
    return {"n": len(done), "wins": len(wins), "win_rate": round(100 * len(wins) / len(done), 1),
            "avg_r": round(sum(rs) / len(rs), 2), "total_r": round(sum(rs), 2),
            "pnl_pts": round(sum(float(t["pnl_pts"] or 0) for t in done), 1),
            "pnl_rs_proxy": round(sum(float(t["pnl_pts"] or 0) for t in done) * lot, 0), "lot_qty": lot,
            "by_setup": {k: sum(1 for t in done if t["setup"] == k) for k in ("A", "C")},
            "note": "Under 30 closed trades is too few to call an edge."}
