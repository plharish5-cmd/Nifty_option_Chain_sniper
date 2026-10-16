"""
paper_desk.py — port 8610 manual paper-trade journal.
Run:  .venv/bin/python -m uvicorn app.api.paper_desk:app --host 0.0.0.0 --port 8610

Shares the SAME SQLite file as 8600 (read-only for existing tables).
Adds one new table: manual_trades — for chart-signal paper trades you log by hand.
INFORMATION ONLY — never places real orders.
"""
from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from .. import config, db

# ── extra table (manual_trades) ───────────────────────────────────────────────
MANUAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS manual_trades (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  logged_ts INTEGER NOT NULL,
  tf        TEXT,
  side      TEXT,        -- BUY | SELL
  signal    TEXT,        -- bias / conf / ifvg / crt / smt / aplus / manual
  entry     REAL,
  sl        REAL,
  tp        REAL,
  exit_px   REAL,
  status    TEXT DEFAULT 'OPEN',   -- OPEN | TP | SL | EOD | MANUAL
  pnl_pts   REAL,
  r         REAL,
  note      TEXT,
  closed_ts INTEGER
);
"""

FUNDS_SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_funds (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  balance REAL NOT NULL,
  start   REAL NOT NULL,
  updated_ts INTEGER
);
CREATE TABLE IF NOT EXISTS cash_ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  kind TEXT,            -- deposit | trade
  amount REAL,          -- +deposit / ±net-pnl
  note TEXT,
  balance_after REAL
);
"""

# columns added after the first release — migrate in place (SQLite has no ADD COLUMN IF NOT EXISTS)
_EXTRA_COLS = {"lots": "INTEGER DEFAULT 1", "charges": "REAL", "net_rs": "REAL", "gross_rs": "REAL"}


def _ensure_schema():
    """Create / migrate the paper-desk tables."""
    with db._lock:
        conn = db._conn
        if conn is None:
            return
        conn.executescript(MANUAL_SCHEMA)
        conn.executescript(FUNDS_SCHEMA)
        have = {r[1] for r in conn.execute("PRAGMA table_info(manual_trades)").fetchall()}
        for col, decl in _EXTRA_COLS.items():
            if col not in have:
                conn.execute(f"ALTER TABLE manual_trades ADD COLUMN {col} {decl}")
        # seed the wallet once
        if conn.execute("SELECT COUNT(*) FROM paper_funds").fetchone()[0] == 0:
            start = float(config.rule("paper", "start_balance", 100000))
            conn.execute("INSERT INTO paper_funds (id, balance, start, updated_ts) VALUES (1, ?, ?, ?)",
                         (start, start, int(time.time())))
        conn.commit()


# ── charges — EXACT Dhan F&O options formula ──────────────────────────────────
# Rates verified from Harish's Dhan Global Transaction report (164 rows, Jul–Oct 2026):
#   Brokerage ₹20/order · STT 0.15% sell · NSE txn 0.03503% both · IPFT 0.0005% both
#   SEBI 0.0001% both · Stamp 0.003% buy · GST 18% of (brokerage + txn + IPFT + SEBI).
# All rates live in the `charges` rules so they stay tunable if Dhan changes them.
def _charges(buy_val: float, sell_val: float) -> Dict[str, float]:
    """buy_val / sell_val = PREMIUM turnover per leg in ₹ (option price × qty)."""
    def r(k: str, d: float) -> float:
        return float(config.rule("charges", k, d))
    turnover = buy_val + sell_val
    brokerage = r("brokerage_per_order", 20) * 2                          # ₹20 × (buy + sell)
    stt = sell_val * r("stt_sell", 0.0015)                               # 0.15% sell side only
    exch = turnover * r("nse_txn", 0.0003503)                           # NSE txn 0.03503%
    ipft = turnover * r("ipft", 0.000005)                               # IPFT 0.0005%
    sebi = turnover * r("sebi", 0.000001)                               # SEBI 0.0001%
    stamp = buy_val * r("stamp_buy", 0.00003)                           # stamp 0.003% buy side
    gst = r("gst", 0.18) * (brokerage + exch + ipft + sebi)             # Dhan GST base incl. IPFT
    total = brokerage + stt + exch + ipft + sebi + stamp + gst
    return {"brokerage": round(brokerage, 2), "stt": round(stt, 2), "exchange": round(exch, 2),
            "ipft": round(ipft, 2), "sebi": round(sebi, 2), "stamp": round(stamp, 2),
            "gst": round(gst, 2), "total": round(total, 2)}


def _funds() -> Dict[str, Any]:
    _ensure_schema()
    r = db.query("SELECT * FROM paper_funds WHERE id=1")
    f = dict(r[0]) if r else {"balance": 0, "start": 0}
    deps = db.query("SELECT COALESCE(SUM(amount),0) s FROM cash_ledger WHERE kind='deposit'")
    added = float(deps[0]["s"]) if deps else 0.0
    st = _mt_stats()
    return {"balance": round(float(f["balance"]), 2), "start": round(float(f["start"]), 2),
            "added": round(added, 2), "net_pnl": st.get("net_rs", 0.0), "charges_paid": st.get("charges_rs", 0.0),
            "gross_pnl": st.get("pnl_rs", 0.0), "open": st.get("open", 0)}


def _apply_to_funds(net_rs: float, note: str):
    with db._lock:
        conn = db._conn
        if conn is None:
            return
        row = conn.execute("SELECT balance FROM paper_funds WHERE id=1").fetchone()
        bal = (float(row[0]) if row else 0.0) + net_rs
        conn.execute("UPDATE paper_funds SET balance=?, updated_ts=? WHERE id=1", (bal, int(time.time())))
        conn.execute("INSERT INTO cash_ledger (ts, kind, amount, note, balance_after) VALUES (?,?,?,?,?)",
                     (int(time.time()), "trade", round(net_rs, 2), note, round(bal, 2)))
        conn.commit()


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # touch the main db so connection is established, then add our table
    try:
        db.query("SELECT 1")
        _ensure_schema()
    except Exception:
        pass
    yield


app = FastAPI(title="ICT·SMC Paper Desk", lifespan=_lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ── helpers ───────────────────────────────────────────────────────────────────

def _mt_rows(limit: int = 200) -> List[Dict[str, Any]]:
    from ..timeutil import to_ist
    rows = db.query("SELECT * FROM manual_trades ORDER BY id DESC LIMIT ?", (limit,))
    out = []
    for r in rows:
        d = dict(r)
        d["logged"] = to_ist(d["logged_ts"]).strftime("%d %b %H:%M") if d.get("logged_ts") else ""
        d["closed"] = to_ist(d["closed_ts"]).strftime("%H:%M") if d.get("closed_ts") else ""
        out.append(d)
    return out


def _mt_stats() -> Dict[str, Any]:
    rows = _mt_rows(10000)
    done = [t for t in rows if t["status"] in ("TP", "SL", "EOD", "MANUAL") and t.get("r") is not None]
    openn = len([t for t in rows if t["status"] == "OPEN"])
    if not done:
        return {"n": 0, "open": openn, "charges_rs": 0.0, "net_rs": 0.0, "pnl_rs": 0.0}
    rs = [float(t["r"]) for t in done]
    wins = [r for r in rs if r > 0]
    lot = int(config.rule("paper", "lot_size", 65))
    gross_rs = round(sum(float(t.get("gross_rs") if t.get("gross_rs") is not None else (float(t.get("pnl_pts") or 0) * lot * int(t.get("lots") or 1))) for t in done), 0)
    charges_rs = round(sum(float(t.get("charges") or 0) for t in done), 0)
    net_rs = round(sum(float(t["net_rs"]) if t.get("net_rs") is not None else (float(t.get("pnl_pts") or 0) * lot * int(t.get("lots") or 1)) for t in done), 0)
    return {
        "n": len(done),
        "open": openn,
        "wins": len(wins),
        "losses": len([r for r in rs if r < 0]),
        "win_rate": round(100 * len(wins) / len(done), 1),
        "avg_r": round(sum(rs) / len(done), 2),
        "total_r": round(sum(rs), 2),
        "pnl_pts": round(sum(float(t.get("pnl_pts") or 0) for t in done), 1),
        "pnl_rs": gross_rs,
        "charges_rs": charges_rs,
        "net_rs": net_rs,
        "lot": lot,
        "enough": len(done) >= 30,
        "note": "Need 30+ closed trades before win rate means anything." if len(done) < 30 else "✓ Enough trades to judge.",
    }


def _recent_alerts(limit: int = 30) -> List[Dict[str, Any]]:
    from ..timeutil import to_ist
    rows = db.query(
        "SELECT * FROM alerts WHERE kind LIKE 'SIG_%' OR grade='trade' ORDER BY ts DESC LIMIT ?", (limit,)
    )
    out = []
    for r in rows:
        d = dict(r)
        d["time_str"] = to_ist(d["ts"]).strftime("%H:%M") if d.get("ts") else ""
        out.append(d)
    return out


# ── REST ──────────────────────────────────────────────────────────────────────

@app.get("/api/trades")
def get_trades():
    _ensure_schema()
    return {"trades": _mt_rows(200), "stats": _mt_stats()}


@app.get("/api/alerts")
def get_alerts():
    return {"alerts": _recent_alerts(40)}


class LogBody(BaseModel):
    tf: str = "5m"
    side: str        # BUY | SELL
    signal: str = "manual"
    entry: float
    sl: float
    tp: float
    note: str = ""
    lots: int = 1


@app.post("/api/trades")
def log_trade(b: LogBody):
    _ensure_schema()
    if b.side not in ("BUY", "SELL"):
        raise HTTPException(400, "side must be BUY or SELL")
    if b.entry <= 0 or b.sl <= 0 or b.tp <= 0:
        raise HTTPException(400, "entry / sl / tp must be positive")
    risk = abs(b.entry - b.sl)
    if risk < 0.1:
        raise HTTPException(400, "SL too close to entry (risk < 0.1 pts)")
    ts = int(time.time())
    db.execute(
        "INSERT INTO manual_trades (logged_ts, tf, side, signal, entry, sl, tp, note, status, lots) "
        "VALUES (?,?,?,?,?,?,?,?,'OPEN',?)",
        (ts, b.tf, b.side, b.signal, b.entry, b.sl, b.tp, b.note, max(1, int(b.lots))),
    )
    rid = db.query("SELECT last_insert_rowid() AS id")[0]["id"]
    return {"ok": True, "id": rid}


class CloseBody(BaseModel):
    exit_px: float
    status: str = "MANUAL"   # TP | SL | EOD | MANUAL


@app.post("/api/trades/{trade_id}/close")
def close_trade(trade_id: int, b: CloseBody):
    _ensure_schema()
    rows = db.query("SELECT * FROM manual_trades WHERE id=?", (trade_id,))
    if not rows:
        raise HTTPException(404, "trade not found")
    t = dict(rows[0])
    if t["status"] != "OPEN":
        raise HTTPException(400, "trade already closed")
    short = t["side"] == "SELL"
    risk = abs(float(t["entry"]) - float(t["sl"])) or 1.0
    pts = (float(t["entry"]) - b.exit_px) if short else (b.exit_px - float(t["entry"]))
    r = round(pts / risk, 2)
    lot_size = int(config.rule("paper", "lot_size", 65))
    lots = int(t.get("lots") or 1)
    qty = lot_size * lots
    gross_rs = round(pts * qty, 2)
    # charges are on PREMIUM turnover. Point-based trades use an assumed ATM premium (tune in rules).
    prem = float(config.rule("charges", "assumed_premium", 120))
    ch = _charges(prem * qty, prem * qty)
    net_rs = round(gross_rs - ch["total"], 2)
    ts = int(time.time())
    db.execute(
        "UPDATE manual_trades SET exit_px=?, status=?, pnl_pts=?, r=?, closed_ts=?, gross_rs=?, charges=?, net_rs=? WHERE id=?",
        (b.exit_px, b.status, round(pts, 1), r, ts, gross_rs, ch["total"], net_rs, trade_id),
    )
    _apply_to_funds(net_rs, f"{t['side']} {t.get('signal') or ''} #{trade_id} · {b.status} · {r:+.2f}R")
    return {"ok": True, "r": r, "pnl_pts": round(pts, 1), "gross_rs": gross_rs, "charges": ch, "net_rs": net_rs}


@app.delete("/api/trades/{trade_id}")
def delete_trade(trade_id: int):
    _ensure_schema()
    db.execute("DELETE FROM manual_trades WHERE id=?", (trade_id,))
    return {"ok": True}


# ── funds / wallet ────────────────────────────────────────────────────────────
@app.get("/api/funds")
def funds():
    return _funds()


class AddBody(BaseModel):
    amount: float


@app.post("/api/funds/add")
def add_funds(b: AddBody):
    _ensure_schema()
    if b.amount == 0:
        raise HTTPException(400, "amount must be non-zero")
    with db._lock:
        conn = db._conn
        row = conn.execute("SELECT balance FROM paper_funds WHERE id=1").fetchone()
        bal = (float(row[0]) if row else 0.0) + b.amount
        conn.execute("UPDATE paper_funds SET balance=?, updated_ts=? WHERE id=1", (bal, int(time.time())))
        conn.execute("INSERT INTO cash_ledger (ts, kind, amount, note, balance_after) VALUES (?,?,?,?,?)",
                     (int(time.time()), "deposit", round(b.amount, 2), "added paper money" if b.amount > 0 else "withdrew", round(bal, 2)))
        conn.commit()
    return _funds()


class ResetBody(BaseModel):
    balance: float = 100000


@app.post("/api/funds/reset")
def reset_funds(b: ResetBody):
    """Reset the wallet to a fresh balance (does not delete trades)."""
    _ensure_schema()
    with db._lock:
        conn = db._conn
        conn.execute("UPDATE paper_funds SET balance=?, start=?, updated_ts=? WHERE id=1", (b.balance, b.balance, int(time.time())))
        conn.execute("DELETE FROM cash_ledger")
        conn.commit()
    return _funds()


@app.get("/api/report.csv")
def report_csv():
    """Download the full trade book as CSV — orders, P&L, charges, net."""
    import csv
    import io
    rows = db.query("SELECT * FROM manual_trades ORDER BY id ASC")
    buf = io.StringIO()
    cols = ["id", "logged", "closed", "tf", "side", "signal", "lots", "entry", "sl", "tp", "exit_px",
            "status", "pnl_pts", "r", "gross_rs", "charges", "net_rs", "note"]
    w = csv.writer(buf)
    w.writerow(cols)
    from ..timeutil import to_ist
    for r in rows:
        d = dict(r)
        d["logged"] = to_ist(d["logged_ts"]).strftime("%Y-%m-%d %H:%M") if d.get("logged_ts") else ""
        d["closed"] = to_ist(d["closed_ts"]).strftime("%Y-%m-%d %H:%M") if d.get("closed_ts") else ""
        w.writerow([d.get(c, "") for c in cols])
    st = _mt_stats()
    w.writerow([])
    w.writerow(["SUMMARY"])
    for k in ["n", "wins", "losses", "win_rate", "avg_r", "total_r", "pnl_rs", "charges_rs", "net_rs"]:
        w.writerow([k, st.get(k, "")])
    from datetime import date
    headers = {"Content-Disposition": f'attachment; filename="paper_trades_{date.today().isoformat()}.csv"'}
    return Response(content=buf.getvalue(), media_type="text/csv", headers=headers)


# ── UI ────────────────────────────────────────────────────────────────────────

PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>📋 Paper Desk · ICT·SMC · 8610</title>
<style>
:root{--bg:#0a0e14;--bg2:#11161e;--bg3:#1a1f28;--border:#2d3748;--gold:#f59e0b;--gold2:#fbbf24;
  --bull:#22c55e;--bear:#dc2626;--slate:#94a3b8;--text:#e2e8f0;--dim:#64748b;--pill:#1e293b}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font:13px/1.5 'Inter',system-ui,sans-serif;min-height:100vh;padding:16px}
.hdr{margin-bottom:20px}
.hdr-top{display:flex;align-items:baseline;gap:12px;margin-bottom:4px}
h1{font-size:18px;font-weight:700;letter-spacing:.02em;color:#fff}
.hdr-sub{font-size:13px;color:var(--dim);font-weight:500}
.hdr-info{display:flex;gap:16px;margin-top:8px;padding-top:8px;border-top:1px solid var(--border);flex-wrap:wrap}
.info-pill{display:flex;align-items:baseline;gap:6px;font-size:12px}
.info-pill .lbl{color:var(--slate)}
.info-pill .val{font-weight:600;color:var(--text)}
.info-pill.bull .val{color:var(--bull)}
.info-pill.bear .val{color:var(--bear)}
.status-live{color:#10b981;font-weight:700}
.funds-bar{background:linear-gradient(135deg,#0d1b2a,#11161e);border:1px solid var(--border);border-radius:10px;padding:14px 16px;margin-bottom:14px;display:flex;flex-wrap:wrap;align-items:center;gap:24px}
.fund .lbl{font-size:10px;color:var(--slate);text-transform:uppercase;letter-spacing:.04em}
.fund .val{font-size:20px;font-weight:700;font-variant-numeric:tabular-nums;color:var(--text)}
.fund-actions{margin-left:auto;display:flex;gap:8px;flex-wrap:wrap}
.btn-add{background:var(--bull);color:#fff}.btn-csv{background:#334155;color:#e2e8f0}.btn-reset{background:#3f1d1d;color:#fca5a5}
.summary-box{background:var(--bg2);border:1px solid var(--border);border-radius:10px;padding:16px;margin-bottom:20px;display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:16px}
.stat-item{display:flex;flex-direction:column}
.stat-item .lbl{font-size:11px;color:var(--slate);text-transform:uppercase;letter-spacing:.04em;margin-bottom:4px}
.stat-item .val{font-size:20px;font-weight:700;font-variant-numeric:tabular-nums;color:var(--text)}
.stat-item.bull .val{color:var(--bull)}.stat-item.bear .val{color:var(--bear)}.stat-item.gold .val{color:var(--gold2)}
.tabs{display:flex;gap:0;border-bottom:1px solid var(--border);margin-bottom:20px;overflow-x:auto}
.tab{padding:12px 20px;border-bottom:2px solid transparent;cursor:pointer;color:var(--slate);font-weight:500;white-space:nowrap;transition:all .2s}
.tab:hover{color:var(--text)}
.tab.active{color:var(--gold2);border-bottom-color:var(--gold)}
.main-grid{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-bottom:20px}
@media(max-width:1100px){.main-grid{grid-template-columns:1fr}}
.card{background:var(--bg2);border:1px solid var(--border);border-radius:10px;overflow:hidden;display:flex;flex-direction:column}
.card-h{padding:12px 16px;border-bottom:1px solid var(--border);font-size:13px;font-weight:600;color:var(--gold2);display:flex;align-items:center;justify-content:space-between}
.card-b{padding:16px;overflow-y:auto;flex:1}
.form-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.form-group{display:flex;flex-direction:column;gap:6px}
.form-group label{font-size:11px;color:var(--slate);text-transform:uppercase;letter-spacing:.04em;font-weight:500}
input,select{background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--text);padding:8px 12px;font-size:13px;width:100%}
input:focus,select:focus{outline:none;border-color:var(--gold);box-shadow:0 0 0 2px rgba(245,158,11,.1)}
.btns{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:12px}
button{cursor:pointer;border:none;border-radius:6px;padding:8px 16px;font-size:13px;font-weight:600;transition:all .15s}
button:hover{opacity:.9}
.btn-buy{background:var(--bull);color:#fff}.btn-buy:hover{background:#16a34a}
.btn-sell{background:var(--bear);color:#fff}.btn-sell:hover{background:#b91c1c}
.btn-log{background:var(--gold);color:#111;grid-column:1/-1;padding:10px;font-weight:700;margin-top:8px}
.btn-close{background:#059669;color:#fff;font-size:11px}.btn-del{background:#7f1d1d;color:#fca5a5;font-size:11px}
.rr-info{font-size:12px;color:var(--slate);margin-top:8px;padding:8px;background:var(--bg);border-radius:6px;min-height:18px}
.rr-info.good{color:var(--bull)}.rr-info.warn{color:var(--gold)}
.alert-list{display:flex;flex-direction:column;gap:8px;max-height:300px;overflow-y:auto}
.alert-item{background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:8px 10px;display:flex;align-items:center;justify-content:space-between;gap:8px;font-size:12px}
.alert-time{color:var(--slate);font-size:11px;min-width:35px}
.alert-kind{display:inline-block;padding:2px 8px;border-radius:4px;font-size:10px;font-weight:700;background:var(--pill);color:var(--gold2)}
.trades-table{width:100%;border-collapse:collapse;font-size:12px}
.trades-table th{text-align:left;padding:8px;font-size:10px;text-transform:uppercase;letter-spacing:.04em;color:var(--slate);border-bottom:1px solid var(--border);font-weight:600}
.trades-table td{padding:8px;border-bottom:1px solid var(--border);vertical-align:middle}
.trades-table tbody tr:hover{background:var(--bg)}
.badge{display:inline-block;padding:2px 8px;border-radius:4px;font-size:10px;font-weight:700}
.badge.buy{background:#052e16;color:var(--bull);border:1px solid #16a34a}
.badge.sell{background:#7f1d1d;color:var(--bear);border:1px solid #dc2626}
.badge.open{background:#1e1b4b;color:#a5b4fc;border:1px solid #4f46e5}
.badge.tp{background:#052e16;color:var(--bull)}
.badge.sl{background:#7f1d1d;color:var(--bear)}
.msg{position:fixed;bottom:20px;right:20px;background:var(--bg2);border:1px solid var(--gold);border-radius:8px;
  padding:12px 16px;font-size:13px;color:var(--gold2);display:none;z-index:99;box-shadow:0 4px 12px rgba(0,0,0,.3)}
.empty{text-align:center;padding:20px;color:var(--slate);font-size:12px}
</style>
</head>
<body>
<div class="hdr">
  <div class="hdr-top">
    <h1>📋 PAPER DESK</h1>
    <div class="hdr-sub">ICT·SMC</div>
  </div>
  <div class="hdr-info">
    <div class="info-pill"><span class="lbl">Port</span><span class="val">8610</span></div>
    <div class="info-pill"><span class="lbl">Status</span><span class="val status-live">● Information only</span></div>
    <div class="info-pill" id="mkt-status"><span class="lbl">Market</span><span class="val">Loading…</span></div>
    <div class="info-pill bull"><span class="lbl">Booked</span><span class="val" id="h-booked">+₹0</span></div>
  </div>
</div>

<div class="funds-bar" id="funds-bar">
  <div class="fund"><div class="lbl">💰 Balance</div><div class="val" id="f-balance">—</div></div>
  <div class="fund"><div class="lbl">Net P&L (after charges)</div><div class="val" id="f-net">—</div></div>
  <div class="fund"><div class="lbl">Charges paid</div><div class="val" id="f-charges">—</div></div>
  <div class="fund"><div class="lbl">Gross P&L</div><div class="val" id="f-gross">—</div></div>
  <div class="fund-actions">
    <button class="btn-add" onclick="addMoney()">+ Add paper money</button>
    <button class="btn-csv" onclick="window.open('/api/report.csv','_blank')">⭳ Download CSV</button>
    <button class="btn-reset" onclick="resetFunds()">Reset wallet</button>
  </div>
</div>

<div class="summary-box" id="summary-box">
  <div class="stat-item"><div class="lbl">Trades closed</div><div class="val" id="s-n">—</div></div>
  <div class="stat-item gold"><div class="lbl">Open trades</div><div class="val" id="s-open">—</div></div>
  <div class="stat-item"><div class="lbl">Win rate</div><div class="val" id="s-wr">—</div></div>
  <div class="stat-item"><div class="lbl">Avg R</div><div class="val" id="s-ar">—</div></div>
  <div class="stat-item bull"><div class="lbl">Total R</div><div class="val" id="s-tr">—</div></div>
  <div class="stat-item"><div class="lbl">P&L pts</div><div class="val" id="s-pts">—</div></div>
  <div class="stat-item bull"><div class="lbl">P&L ₹</div><div class="val" id="s-rs">—</div></div>
</div>

<div class="tabs">
  <div class="tab active" onclick="switchTab('positions')">☑ Positions</div>
  <div class="tab" onclick="switchTab('signals')">📣 Recent signals</div>
  <div class="tab" onclick="switchTab('journal')">📊 Journal</div>
</div>

<div id="positions-tab" style="display:block">
  <div class="main-grid">
    <!-- Log a trade -->
    <div class="card">
      <div class="card-h">✏️ Log a paper trade</div>
      <div class="card-b">
        <form id="log-form">
          <div class="form-grid">
            <div class="form-group">
              <label>Timeframe</label>
              <select id="f-tf"><option>1m</option><option selected>5m</option><option>15m</option><option>1h</option></select>
            </div>
            <div class="form-group">
              <label>Signal type</label>
              <select id="f-signal">
                <option value="aplus">★ A+ (bias + trigger)</option>
                <option value="bias">⚖ Bias</option>
                <option value="conf">✓3 Confluence</option>
                <option value="ifvg">IFVG</option>
                <option value="crt">CRT entry</option>
                <option value="smt">SMT</option>
                <option value="dol">D.O.L</option>
                <option value="gamma">γ ✓✓✓</option>
                <option value="nodes">● ENTRY</option>
                <option value="flow">🧭 Order flow</option>
                <option value="manual">Manual / other</option>
              </select>
            </div>
          </div>
          <div class="btns">
            <button type="button" class="btn-buy" id="btn-buy" onclick="setSide('BUY')">▲ BUY (CE)</button>
            <button type="button" class="btn-sell" id="btn-sell" onclick="setSide('SELL')">▼ SELL (PE)</button>
          </div>
          <input type="hidden" id="f-side" value="">
          <div class="form-grid" style="margin-top:12px">
            <div class="form-group"><label>Entry</label><input type="number" id="f-entry" step="0.5" placeholder="24150"></div>
            <div class="form-group"><label>Stop loss</label><input type="number" id="f-sl" step="0.5" placeholder="24080"></div>
          </div>
          <div class="form-grid">
            <div class="form-group"><label>Target (TP)</label><input type="number" id="f-tp" step="0.5" placeholder="24250"></div>
            <div class="form-group"><label>Note</label><input type="text" id="f-note" placeholder="Optional"></div>
          </div>
          <div class="rr-info" id="rr-preview"></div>
          <button type="button" class="btn-log" onclick="logTrade()">Log trade</button>
        </form>
      </div>
    </div>

    <!-- Open positions -->
    <div class="card">
      <div class="card-h">📊 Open positions</div>
      <div class="card-b" id="open-trades" style="min-height:200px">
        <div class="empty">No open trades</div>
      </div>
    </div>
  </div>
</div>

<div id="signals-tab" style="display:none">
  <div class="card">
    <div class="card-h">📣 Recent chart signals from 8600</div>
    <div class="card-b">
      <div class="alert-list" id="alert-list">
        <div class="empty">Loading signals…</div>
      </div>
    </div>
  </div>
</div>

<div id="journal-tab" style="display:none">
  <div class="card">
    <div class="card-h">📊 All trades journal <span style="font-size:11px;color:var(--slate)" id="enough-note"></span></div>
    <div class="card-b" style="overflow-x:auto">
      <table class="trades-table">
        <thead>
          <tr>
            <th>#</th><th>Time</th><th>TF</th><th>Side</th><th>Signal</th>
            <th>Entry</th><th>SL</th><th>TP</th><th>Exit</th><th>Status</th><th>P&L</th><th>R</th><th>Actions</th>
          </tr>
        </thead>
        <tbody id="trade-tbody"></tbody>
      </table>
    </div>
  </div>
</div>

<div id="msg" class="msg"></div>

<script>
let _side = '';
let _trades = [];

function switchTab(tab) {
  document.querySelectorAll('[id$="-tab"]').forEach(el => el.style.display = 'none');
  document.querySelectorAll('.tab').forEach(el => el.classList.remove('active'));
  document.getElementById(tab + '-tab').style.display = 'block';
  document.querySelector(`.tab[onclick="switchTab('${tab}')"]`)?.classList.add('active');
}

function setSide(s) {
  _side = s;
  document.getElementById('f-side').value = s;
  document.getElementById('btn-buy').style.opacity = s === 'BUY' ? '1' : '0.4';
  document.getElementById('btn-sell').style.opacity = s === 'SELL' ? '1' : '0.4';
  calcRR();
}

function calcRR() {
  const entry = parseFloat(document.getElementById('f-entry').value);
  const sl = parseFloat(document.getElementById('f-sl').value);
  const tp = parseFloat(document.getElementById('f-tp').value);
  const el = document.getElementById('rr-preview');
  if (!entry || !sl || !tp) { el.textContent = ''; el.className = 'rr-info'; return; }
  const risk = Math.abs(entry - sl);
  const reward = Math.abs(tp - entry);
  if (risk < 0.1) { el.textContent = '⚠️ SL too close to entry'; el.className = 'rr-info'; return; }
  const rr = (reward / risk).toFixed(1);
  const ok = parseFloat(rr) >= 1.5;
  el.className = 'rr-info ' + (ok ? 'good' : 'warn');
  el.textContent = `Risk ${risk.toFixed(1)} pts · Reward ${reward.toFixed(1)} pts · RR ${rr} ${ok ? '✓' : '(aim ≥ 1.5)'}`;
}

['f-entry','f-sl','f-tp'].forEach(id => document.getElementById(id).addEventListener('input', calcRR));

function toast(msg) {
  const el = document.getElementById('msg');
  el.textContent = msg;
  el.style.display = 'block';
  setTimeout(() => el.style.display = 'none', 3000);
}

async function logTrade() {
  const entry = parseFloat(document.getElementById('f-entry').value);
  const sl = parseFloat(document.getElementById('f-sl').value);
  const tp = parseFloat(document.getElementById('f-tp').value);
  if (!_side) { toast('⚠️ Select BUY or SELL first'); return; }
  if (!entry || !sl || !tp) { toast('⚠️ Fill entry / SL / TP'); return; }
  const body = {
    tf: document.getElementById('f-tf').value,
    side: _side,
    signal: document.getElementById('f-signal').value,
    entry, sl, tp,
    note: document.getElementById('f-note').value,
  };
  const r = await fetch('/api/trades', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) });
  if (r.ok) {
    toast('✓ Trade logged!');
    document.getElementById('f-entry').value = '';
    document.getElementById('f-sl').value = '';
    document.getElementById('f-tp').value = '';
    document.getElementById('f-note').value = '';
    document.getElementById('rr-preview').textContent = '';
    refresh();
  } else {
    const e = await r.json();
    toast('⚠️ ' + (e.detail || 'Error'));
  }
}

async function closeTrade(id, status) {
  const px = prompt(`Close trade #${id}\nExit price:`);
  if (!px) return;
  const r = await fetch(`/api/trades/${id}/close`, {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ exit_px: parseFloat(px), status })
  });
  if (r.ok) {
    const d = await r.json();
    toast(`✓ Closed @ ${px} · R = ${d.r}`);
    refresh();
  } else {
    const e = await r.json();
    toast('⚠️ ' + (e.detail || 'Error'));
  }
}

async function deleteTrade(id) {
  if (!confirm(`Delete trade #${id}?`)) return;
  await fetch(`/api/trades/${id}`, { method: 'DELETE' });
  refresh();
}

function fillFromAlert(side, entry) {
  switchTab('positions');
  setSide(side);
  document.getElementById('f-entry').value = entry;
  calcRR();
  toast(`Pre-filled: ${side} @ ${entry}`);
}

function colorR(r) {
  if (r == null) return '<span style="color:var(--slate)">—</span>';
  const n = parseFloat(r);
  const cls = n > 0 ? 'bull' : n < 0 ? 'bear' : 'slate';
  return `<span style="color:var(--${cls})">${n > 0 ? '+' : ''}${n.toFixed(2)}R</span>`;
}

function sigLabel(s) {
  const m = { aplus:'★A+', bias:'⚖ Bias', conf:'✓3', ifvg:'IFVG', crt:'CRT', smt:'SMT', dol:'DOL', gamma:'γ✓✓✓', nodes:'●ENTRY', flow:'🧭 Flow', manual:'manual' };
  return m[s] || s || '—';
}

function renderOpenTrades(trades) {
  const open = trades.filter(t => t.status === 'OPEN');
  const el = document.getElementById('open-trades');
  if (!open.length) { el.innerHTML = '<div class="empty">No open trades</div>'; return; }
  el.innerHTML = open.map(t => {
    const risk = Math.abs(parseFloat(t.entry) - parseFloat(t.sl)) || 1;
    const upside = Math.abs(parseFloat(t.tp) - parseFloat(t.entry));
    const rr = (upside / risk).toFixed(1);
    return `<div style="background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:12px;margin-bottom:8px;">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;">
        <div style="display:flex;gap:8px;align-items:center;">
          <span class="badge ${t.side === 'BUY' ? 'buy' : 'sell'}">${t.side}</span>
          <span style="font-size:12px;color:var(--slate)">#${t.id} · ${t.logged} · ${t.tf}</span>
        </div>
        <span style="font-size:11px;color:var(--slate);font-weight:500">${sigLabel(t.signal)}</span>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr 1fr 1fr;gap:8px;font-size:12px;margin-bottom:8px;">
        <div><span style="color:var(--slate);font-size:10px">Entry</span><div style="font-weight:700;color:var(--gold2)">${t.entry}</div></div>
        <div><span style="color:var(--slate);font-size:10px">SL</span><div style="font-weight:700;color:var(--bear)">${t.sl}</div></div>
        <div><span style="color:var(--slate);font-size:10px">Target</span><div style="font-weight:700;color:var(--bull)">${t.tp}</div></div>
        <div><span style="color:var(--slate);font-size:10px">RR</span><div style="font-weight:700;color:var(--gold2)">${rr}</div></div>
      </div>
      ${t.note ? `<div style="font-size:11px;color:var(--slate);margin-bottom:8px;font-style:italic">"${t.note}"</div>` : ''}
      <div style="display:flex;gap:4px;flex-wrap:wrap;">
        <button class="btn-close" onclick="closeTrade(${t.id},'TP')" style="background:var(--bull);color:#fff">✓ TP</button>
        <button class="btn-close" onclick="closeTrade(${t.id},'SL')" style="background:var(--bear);color:#fff">✕ SL</button>
        <button class="btn-close" onclick="closeTrade(${t.id},'EOD')" style="background:#64748b;color:#fff">EOD</button>
        <button class="btn-close" onclick="closeTrade(${t.id},'MANUAL')" style="background:#475569;color:#fff">Exit</button>
        <button class="btn-del" onclick="deleteTrade(${t.id})" style="margin-left:auto">✕</button>
      </div>
    </div>`;
  }).join('');
}

function renderTrades(trades) {
  const tbody = document.getElementById('trade-tbody');
  const all = trades.filter(t => t.status !== 'OPEN');
  if (!all.length) { tbody.innerHTML = '<tr><td colspan="13" class="empty" style="padding:20px">No closed trades yet</td></tr>'; return; }
  tbody.innerHTML = all.map(t => {
    const st = (t.status || 'OPEN').toLowerCase();
    const sidePill = `<span class="badge ${t.side === 'BUY' ? 'buy' : 'sell'}">${t.side}</span>`;
    const actions = `<div style="display:flex;gap:4px">
      <button class="btn-del" onclick="deleteTrade(${t.id})" style="padding:2px 6px;font-size:10px">✕</button>
    </div>`;
    return `<tr>
      <td style="color:var(--slate);font-weight:500">#${t.id}</td>
      <td style="color:var(--slate);font-size:11px">${t.logged}</td>
      <td>${t.tf || '—'}</td>
      <td>${sidePill}</td>
      <td><span style="font-size:11px;color:var(--gold2)">${sigLabel(t.signal)}</span></td>
      <td style="color:var(--gold2);font-weight:500">${t.entry ?? '—'}</td>
      <td style="color:var(--bear);font-weight:500">${t.sl ?? '—'}</td>
      <td style="color:var(--bull);font-weight:500">${t.tp ?? '—'}</td>
      <td style="color:var(--text);font-weight:500">${t.exit_px ?? '—'}</td>
      <td><span class="badge ${st}">${t.status}</span></td>
      <td>${t.pnl_pts ? (t.pnl_pts > 0 ? '<span style="color:var(--bull)">+' : '<span style="color:var(--bear)">') + t.pnl_pts.toFixed(1) + '</span>' : '—'}</td>
      <td>${colorR(t.r)}</td>
      <td>${actions}</td>
    </tr>`;
  }).join('');
}

function renderAlerts(alerts) {
  const el = document.getElementById('alert-list');
  if (!alerts.length) { el.innerHTML = '<div class="empty">No signals yet</div>'; return; }
  el.innerHTML = alerts.map(a => {
    const isBuy = a.side === 'BUY' || (a.kind || '').includes('BUY');
    const isSell = a.side === 'SELL' || (a.kind || '').includes('SELL');
    const useBtn = (isBuy || isSell)
      ? `<button class="btn-close" onclick="fillFromAlert('${isBuy ? 'BUY' : 'SELL'}',${a.price})" style="font-size:10px;padding:3px 8px">Use →</button>`
      : '';
    return `<div class="alert-item">
      <span class="at">${a.time_str}</span>
      <span class="alert-kind">${(a.kind||'SIG').replace(/SIG_|_BUY|_SELL|_INFO/g,'')}</span>
      <span style="flex:1;color:var(--text);font-size:12px">${a.title || a.kind}</span>
      ${a.price ? `<span style="color:var(--slate);font-size:11px">@ ${a.price.toFixed(2)}</span>` : ''}
      ${useBtn}
    </div>`;
  }).join('');
}

function renderStats(s) {
  if (!s || s.n === 0) {
    ['s-n','s-open','s-wr','s-ar','s-tr','s-pts','s-rs'].forEach(id => {
      document.getElementById(id).textContent = id === 's-open' ? (s?.open ?? 0) : '—';
    });
    document.getElementById('h-booked').textContent = '₹0';
    return;
  }
  document.getElementById('s-n').textContent = s.n;
  document.getElementById('s-open').textContent = s.open;
  document.getElementById('s-wr').textContent = s.n ? (s.win_rate + '%') : '—';
  document.getElementById('s-ar').textContent = s.n ? ((s.avg_r > 0 ? '+' : '') + s.avg_r + 'R') : '—';
  document.getElementById('s-tr').textContent = s.n ? ((s.total_r > 0 ? '+' : '') + s.total_r + 'R') : '—';
  document.getElementById('s-pts').textContent = s.n ? ((s.pnl_pts > 0 ? '+' : '') + s.pnl_pts) : '—';
  document.getElementById('s-rs').textContent = s.n ? ('₹' + s.pnl_rs.toLocaleString('en-IN')) : '—';
  document.getElementById('h-booked').textContent = s.n ? ('₹' + s.pnl_rs.toLocaleString('en-IN')) : '₹0';
  document.getElementById('enough-note').textContent = s.note;
}

function renderFunds(f) {
  if (!f) return;
  const rs = (v) => '₹' + Math.round(v).toLocaleString('en-IN');
  const col = (v) => v > 0 ? 'var(--bull)' : v < 0 ? 'var(--bear)' : 'var(--text)';
  document.getElementById('f-balance').textContent = rs(f.balance);
  const net = document.getElementById('f-net'); net.textContent = (f.net_pnl >= 0 ? '+' : '') + rs(f.net_pnl); net.style.color = col(f.net_pnl);
  document.getElementById('f-charges').textContent = rs(f.charges_paid);
  const g = document.getElementById('f-gross'); g.textContent = (f.gross_pnl >= 0 ? '+' : '') + rs(f.gross_pnl); g.style.color = col(f.gross_pnl);
}

async function addMoney() {
  const v = prompt('Add paper money (₹):', '50000');
  if (!v) return;
  const amount = parseFloat(v.replace(/[^0-9.-]/g, ''));
  if (!amount) return;
  await fetch('/api/funds/add', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ amount }) });
  toast('✓ Added ₹' + amount.toLocaleString('en-IN'));
  refresh();
}

async function resetFunds() {
  const v = prompt('Reset wallet to balance (₹) — this clears the cash ledger, not trades:', '100000');
  if (!v) return;
  const balance = parseFloat(v.replace(/[^0-9.-]/g, '')) || 100000;
  await fetch('/api/funds/reset', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ balance }) });
  toast('✓ Wallet reset to ₹' + balance.toLocaleString('en-IN'));
  refresh();
}

async function refresh() {
  try {
    const [td, al, fn] = await Promise.all([
      fetch('/api/trades').then(r => r.json()),
      fetch('/api/alerts').then(r => r.json()),
      fetch('/api/funds').then(r => r.json()).catch(() => null),
    ]);
    renderStats(td.stats);
    renderOpenTrades(td.trades);
    renderTrades(td.trades);
    renderAlerts(al.alerts);
    renderFunds(fn);
  } catch(e) {
    console.error(e);
  }
}

refresh();
setInterval(refresh, 8000);
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(content=PAGE)
