"""
signals/paper.py — paper trading of the playbook. NO real orders exist anywhere in this codebase.

  * Opens a paper trade only for a TRADE signal on the candle that just closed (entry = that close).
  * One open paper trade per SLOT — stacking entries in one strategy is how a paper account flatters a bad day.
    Slots (10 Oct 2026): A, C, and one per CRT + TBS pair (D|1h>1m, D|4h>5m, D|1D>1h). Each strategy runs its own
    book, so a trade in one never blocks a signal in another — the scorecard compares them fairly.
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


def slot_of(s: Signal) -> str:
    return f"D|{(s.ctx or {}).get('pair', '')}" if s.setup == "D" else s.setup


def open_trades() -> List[Dict[str, Any]]:
    return [dict(r) for r in db.query("SELECT * FROM paper_trades WHERE status = 'OPEN' ORDER BY id")]


def open_trade(slot: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Most recent open trade, or the open trade of one slot. Old rows (no slot) belong to their setup's slot."""
    if slot is None:
        r = db.query("SELECT * FROM paper_trades WHERE status = 'OPEN' ORDER BY id DESC LIMIT 1")
    else:
        r = db.query("SELECT * FROM paper_trades WHERE status = 'OPEN' AND COALESCE(slot, setup) = ? "
                     "ORDER BY id DESC LIMIT 1", (slot,))
    return dict(r[0]) if r else None


def maybe_open(s: Signal, strikes: Optional[Dict[str, Any]] = None, mode: str = "INDEX") -> Optional[Dict[str, Any]]:
    """strikes = {"CE": pick record | None, "PE": pick record | None} selected at decision time."""
    allowed = ("A", "C", "D") if config.rule("crt_tbs", "paper", True) else ("A", "C")
    if not config.rule("paper", "enabled", True) or s.status != "TRADE" or s.setup not in allowed:
        return None
    slot = slot_of(s)
    if open_trade(slot) is not None:
        return None
    opened = int(s.ts) + TF_SECONDS[s.tf]                     # the entry candle's CLOSE time
    use = (strikes or {}).get("CE" if s.side == "long" else "PE") if config.rule("paper", "premium_pnl", True) else None
    inst = (use or {}).get("instrument") or {}
    db.execute("INSERT OR IGNORE INTO paper_trades (signal_key, setup, tf, side, opened_ts, entry, sl, tp, risk, rr, "
               "status, mfe_r, notes, instrument_mode, strikes_snapshot, opt_key, opt_sid, confidence, confidence_parts, "
               "tp1, half_done, partial_pts, slot, pair, model) "
               "VALUES (?,?,?,?,?,?,?,?,?,?, 'OPEN', 0, ?,?,?,?,?,?,?, ?, 0, NULL, ?,?,?)",
               (s.key, s.setup, s.tf, s.side, opened, s.entry, s.sl, s.tp, s.risk, s.rr, json.dumps(s.story),
                mode, json.dumps(strikes or {}), inst.get("key"), inst.get("security_id"),
                getattr(s, "confidence", None), json.dumps(getattr(s, "confidence_parts", None)),
                (s.ctx or {}).get("tp1") if s.setup == "D" else None,
                slot, (s.ctx or {}).get("pair") if s.setup == "D" else None,
                (s.ctx or {}).get("model") if s.setup == "D" else None))
    fill_option_prices()
    return open_trade(slot)


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


def on_1m_close_all(c: Dict[str, float]) -> List[Dict[str, Any]]:
    """c = a CLOSED 1m NIFTY candle. Manages EVERY open trade (one per slot); returns those that closed."""
    out = []
    for t in open_trades():
        r = _manage(t, c)
        if r:
            out.append(r)
    return out


def on_1m_close(c: Dict[str, float]) -> Optional[Dict[str, Any]]:
    """Back-compatible single-result form of on_1m_close_all()."""
    done = on_1m_close_all(c)
    return done[0] if done else None


def _manage(t: Dict[str, Any], c: Dict[str, float]) -> Optional[Dict[str, Any]]:
    if c["ts"] < t["opened_ts"]:
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


def _row(label: str, group: str, ts: List[Dict[str, Any]]) -> Dict[str, Any]:
    rs = [float(t["r"] or 0) for t in ts]
    n = len(ts)
    wins = sum(1 for r in rs if r > 0.0)
    losses = sum(1 for r in rs if r < 0.0)
    return {"group": group, "label": label, "trades": n, "wins": wins, "losses": losses, "flat": n - wins - losses,
            "be_after_tp1": sum(1 for t in ts if t["status"] == "BE"),
            "win_rate": round(100 * wins / n, 1) if n else None,
            "avg_r": round(sum(rs) / n, 2) if n else None, "total_r": round(sum(rs), 2),
            "best_r": round(max(rs), 2) if rs else None, "worst_r": round(min(rs), 2) if rs else None,
            "pnl_pts": round(sum(float(t["pnl_pts"] or 0) for t in ts), 1),
            "premium_rs": round(sum(float(t.get("opt_pnl_rs") or 0) for t in ts), 0),
            "enough": n >= 30}


def scorecard() -> Dict[str, Any]:
    """Closed paper trades by strategy: per setup, per CRT + TBS pair, per entry model.
    win = R > 0 (a D trade that booked TP-1 then stopped at breakeven counts as a win and is also shown as BE)."""
    done = [t for t in trades(100000) if t["status"] in ("SL", "TP", "EOD", "BE")]
    names = {"A": "A · Raid → MSS → FVG", "C": "C · Range sweep", "D": "D · CRT + TBS (all pairs)"}
    rows = [_row(names.get(k, k), "setup", [t for t in done if t["setup"] == k]) for k in ("A", "C", "D")]
    pairs = sorted({t.get("pair") or "?" for t in done if t["setup"] == "D"})
    rows += [_row(f"D · {p.replace('>', '→')}", "pair", [t for t in done if t["setup"] == "D" and (t.get("pair") or "?") == p])
             for p in pairs]
    models = sorted({t.get("model") or "M1" for t in done if t["setup"] == "D"})
    mname = {"M1": "Model #1 order block", "M2": "Model #2 FVG", "M3": "Model #3 BOS"}
    rows += [_row(f"D · {mname.get(m, m)}", "model",
                  [t for t in done if t["setup"] == "D" and (t.get("model") or "M1") == m]) for m in models]
    rows.append(_row("All setups", "total", done))
    return {"rows": rows, "open": open_trades(), "first": done[-1]["opened"] if done else None,
            "note": "A row needs about 30 closed trades before its win rate means anything (marked ✓)."}
