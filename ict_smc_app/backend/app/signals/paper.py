"""
signals/paper.py — paper trading of the playbook. NO real orders exist anywhere in this codebase.

  * Opens a paper trade only for a TRADE signal on the candle that just closed (entry = that close).
  * One open paper trade at a time — stacking entries is how a paper account flatters a bad day.
  * Outcome is resolved on every CLOSED 1-minute candle after the entry candle:
      stop hit → −1R; target hit → +R:R; a 1m candle that touches BOTH counts as the stop (worst case).
      Still open at square_off (15:15) → closed at that minute's close (EOD).
  * Logs MFE (best R reached) so you can see trades that went your way and came back.
  * Strike mode (9 Oct 2026): entry / stop / target are STILL the NIFTY index levels. The trade also stores the
    strikes selected at decision time and the premium of the strike it would use (long CE for a long, long PE for
    a short): opt_entry = that strike's 1m close at the entry minute, opt_exit = its 1m close at the exit minute,
    opt_pnl = (exit − entry) × lot. Filled as soon as those option candles exist (REST, a few seconds later).
  * Setup D (CRT + TBS): HALF is booked at TP-1 (the CRT 50 %), the stop moves to entry (breakeven) — never before
    TP-1 — and the other half runs to TP-2 (opposite CRT extreme), the breakeven stop, or the 15:15 square-off.
    A 1m candle touching both the stop and TP-1 counts as the stop. Status BE = second half stopped at entry.
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


def maybe_open(s: Signal, strikes: Optional[Dict[str, Any]] = None, mode: str = "INDEX") -> Optional[Dict[str, Any]]:
    """strikes = {"CE": pick record | None, "PE": pick record | None} selected at decision time."""
    allowed = ("A", "C", "D") if config.rule("crt_tbs", "paper", True) else ("A", "C")
    if not config.rule("paper", "enabled", True) or s.status != "TRADE" or s.setup not in allowed:
        return None
    if open_trade() is not None:
        return None
    opened = int(s.ts) + TF_SECONDS[s.tf]                     # the entry candle's CLOSE time
    use = (strikes or {}).get("CE" if s.side == "long" else "PE") if config.rule("paper", "premium_pnl", True) else None
    inst = (use or {}).get("instrument") or {}
    db.execute("INSERT OR IGNORE INTO paper_trades (signal_key, setup, tf, side, opened_ts, entry, sl, tp, risk, rr, "
               "status, mfe_r, notes, instrument_mode, strikes_snapshot, opt_key, opt_sid, confidence, confidence_parts, "
               "tp1, half_done, partial_pts) "
               "VALUES (?,?,?,?,?,?,?,?,?,?, 'OPEN', 0, ?,?,?,?,?,?,?, ?, 0, NULL)",
               (s.key, s.setup, s.tf, s.side, opened, s.entry, s.sl, s.tp, s.risk, s.rr, json.dumps(s.story),
                mode, json.dumps(strikes or {}), inst.get("key"), inst.get("security_id"),
                getattr(s, "confidence", None), json.dumps(getattr(s, "confidence_parts", None)),
                (s.ctx or {}).get("tp1") if s.setup == "D" else None))
    fill_option_prices()
    return open_trade()


def fill_option_prices() -> List[Dict[str, Any]]:
    """Fill opt_entry / opt_exit / opt_pnl from stored option 1m candles. Safe to call any time (idempotent)."""
    from ..market.options import close_at
    lot = int(config.rule("paper", "lot_size", 65)) * int(config.rule("paper", "lots", 1))
    done = []
    for r in db.query("SELECT * FROM paper_trades WHERE opt_sid IS NOT NULL AND "
                      "(opt_entry IS NULL OR (closed_ts IS NOT NULL AND opt_exit IS NULL))"):
        t = dict(r)
        key = f"O:{t['opt_sid']}"
        ent = t["opt_entry"] if t["opt_entry"] is not None else close_at(key, int(t["opened_ts"]) - 60)
        ext = t["opt_exit"]
        if t["closed_ts"] and ext is None:
            ext = close_at(key, int(t["closed_ts"]) - 60)
        pts = rs = None
        if ent is not None and ext is not None:
            pts = round(ext - ent, 2)                             # always a LONG option: CE for longs, PE for shorts
            if t.get("half_done") and t.get("half_ts"):          # setup D: half sold at the TP-1 minute
                half_px = close_at(key, int(t["half_ts"]) - 60)
                if half_px is None:
                    continue                                      # wait for that option candle
                pts = round(0.5 * (half_px - ent) + 0.5 * (ext - ent), 2)
            rs = round(pts * lot, 0)
        db.execute("UPDATE paper_trades SET opt_entry=?, opt_exit=?, opt_pnl_pts=?, opt_pnl_rs=? WHERE id=?",
                   (ent, ext, pts, rs, t["id"]))
        if pts is not None:
            done.append(t)
    return done


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
    half = bool(t.get("half_done"))
    tp1 = t.get("tp1")
    if t["setup"] == "D" and tp1 is not None and not half and not hit_sl:
        if (c["l"] <= tp1) if short else (c["h"] >= tp1):              # TP-1: book half, stop → entry (breakeven)
            part = (t["entry"] - tp1) if short else (tp1 - t["entry"])
            db.execute("UPDATE paper_trades SET half_done=1, partial_pts=?, half_ts=?, sl=?, mfe_r=? WHERE id=?",
                       (round(part, 2), int(c["ts"]) + 60, t["entry"], round(mfe, 2), t["id"]))
            return None
    status = exit_px = None
    if hit_sl:
        status, exit_px = ("BE" if half and abs(t["sl"] - t["entry"]) < 1e-9 else "SL"), t["sl"]
    elif hit_tp:
        status, exit_px = "TP", t["tp"]
    elif at_sq:
        status, exit_px = "EOD", c["c"]
    if status is None:
        db.execute("UPDATE paper_trades SET mfe_r = ? WHERE id = ?", (round(mfe, 2), t["id"]))
        return None
    pts = (t["entry"] - exit_px) if short else (exit_px - t["entry"])
    if half:                                                            # D after TP-1: ½ booked + ½ now
        pts = 0.5 * float(t.get("partial_pts") or 0) + 0.5 * pts
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
        for k, dflt in (("notes", []), ("strikes_snapshot", {}), ("confidence_parts", None)):
            try:
                d[k] = json.loads(d.get(k) or "null") or dflt
            except (ValueError, TypeError):
                d[k] = dflt
        out.append(d)
    return out


def stats() -> Dict[str, Any]:
    done = [t for t in trades(1000) if t["status"] in ("SL", "TP", "EOD", "BE")]
    lot = int(config.rule("paper", "lot_size", 65)) * int(config.rule("paper", "lots", 1))
    if not done:
        return {"n": 0, "lot_qty": lot}
    rs = [float(t["r"] or 0) for t in done]
    wins = [r for r in rs if r > 0]
    return {"n": len(done), "wins": len(wins), "win_rate": round(100 * len(wins) / len(done), 1),
            "avg_r": round(sum(rs) / len(rs), 2), "total_r": round(sum(rs), 2),
            "pnl_pts": round(sum(float(t["pnl_pts"] or 0) for t in done), 1),
            "pnl_rs_proxy": round(sum(float(t["pnl_pts"] or 0) for t in done) * lot, 0), "lot_qty": lot,
            "premium_trades": sum(1 for t in done if t.get("opt_pnl_rs") is not None),
            "premium_pnl_rs": round(sum(float(t.get("opt_pnl_rs") or 0) for t in done), 0),
            "by_setup": {k: sum(1 for t in done if t["setup"] == k) for k in ("A", "C", "D")},
            "note": "Under 30 closed trades is too few to call an edge."}
