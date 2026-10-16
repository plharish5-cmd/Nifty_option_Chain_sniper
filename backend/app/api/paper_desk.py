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

from fastapi import FastAPI, HTTPException
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

def _ensure_schema():
    """Create manual_trades if it doesn't exist yet."""
    with db._lock:
        conn = db._conn
        if conn is None:
            return
        conn.executescript(MANUAL_SCHEMA)
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
    if not done:
        return {"n": 0, "open": len([t for t in rows if t["status"] == "OPEN"])}
    rs = [float(t["r"]) for t in done]
    wins = [r for r in rs if r > 0]
    lot = int(config.rule("paper", "lot_size", 65)) * int(config.rule("paper", "lots", 1))
    return {
        "n": len(done),
        "open": len([t for t in rows if t["status"] == "OPEN"]),
        "wins": len(wins),
        "losses": len([r for r in rs if r < 0]),
        "win_rate": round(100 * len(wins) / len(done), 1),
        "avg_r": round(sum(rs) / len(done), 2),
        "total_r": round(sum(rs), 2),
        "pnl_pts": round(sum(float(t.get("pnl_pts") or 0) for t in done), 1),
        "pnl_rs": round(sum(float(t.get("pnl_pts") or 0) for t in done) * lot, 0),
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
        "INSERT INTO manual_trades (logged_ts, tf, side, signal, entry, sl, tp, note, status) "
        "VALUES (?,?,?,?,?,?,?,?,'OPEN')",
        (ts, b.tf, b.side, b.signal, b.entry, b.sl, b.tp, b.note),
    )
    return {"ok": True}


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
    ts = int(time.time())
    db.execute(
        "UPDATE manual_trades SET exit_px=?, status=?, pnl_pts=?, r=?, closed_ts=? WHERE id=?",
        (b.exit_px, b.status, round(pts, 1), r, ts, trade_id),
    )
    return {"ok": True, "r": r, "pnl_pts": round(pts, 1)}


@app.delete("/api/trades/{trade_id}")
def delete_trade(trade_id: int):
    _ensure_schema()
    db.execute("DELETE FROM manual_trades WHERE id=?", (trade_id,))
    return {"ok": True}


# ── UI ────────────────────────────────────────────────────────────────────────

PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>📋 Paper Trade Desk · 8610</title>
<style>
:root{--bg:#060c18;--bg2:#0b1220;--bg3:#111827;--border:#1e293b;--gold:#f59e0b;--gold2:#fbbf24;
  --bull:#22c55e;--bear:#f43f5e;--slate:#94a3b8;--text:#e2e8f0;--pill:#1e293b}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font:13px/1.5 'Inter',system-ui,sans-serif;min-height:100vh}
h1{font-size:15px;font-weight:700;color:var(--gold);letter-spacing:.04em}
.hdr{background:var(--bg2);border-bottom:1px solid var(--border);padding:10px 16px;display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.badge{background:var(--pill);border:1px solid var(--border);border-radius:6px;padding:3px 9px;font-size:12px;color:var(--slate)}
.badge b{color:var(--text)}
.stat-row{display:flex;flex-wrap:wrap;gap:8px;padding:10px 16px;background:var(--bg2);border-bottom:1px solid var(--border)}
.stat{background:var(--bg3);border:1px solid var(--border);border-radius:8px;padding:6px 14px;min-width:90px}
.stat .lbl{font-size:10px;color:var(--slate);text-transform:uppercase;letter-spacing:.05em}
.stat .val{font-size:18px;font-weight:700;font-variant-numeric:tabular-nums}
.bull{color:var(--bull)}.bear{color:var(--bear)}.gold{color:var(--gold)}.slate{color:var(--slate)}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:12px;padding:12px 16px}
@media(max-width:700px){.cols{grid-template-columns:1fr}}
.card{background:var(--bg2);border:1px solid var(--border);border-radius:10px;overflow:hidden}
.card-h{padding:8px 12px;border-bottom:1px solid var(--border);font-size:12px;font-weight:600;color:var(--gold2);display:flex;align-items:center;justify-content:space-between}
.card-b{padding:10px 12px;overflow-y:auto;max-height:420px}
table{width:100%;border-collapse:collapse;font-size:12px}
th{text-align:left;padding:4px 8px;font-size:10px;text-transform:uppercase;letter-spacing:.04em;color:var(--slate);border-bottom:1px solid var(--border)}
td{padding:5px 8px;border-bottom:1px solid #0f172a;vertical-align:middle}
tr:hover td{background:#0f1929}
.pill{display:inline-block;padding:2px 8px;border-radius:999px;font-size:11px;font-weight:600}
.buy{background:#052e16;color:var(--bull);border:1px solid #16a34a}
.sell{background:#1f0914;color:var(--bear);border:1px solid #e11d48}
.open{background:#1e1b4b;color:#a5b4fc;border:1px solid #4f46e5}
.tp{background:#052e16;color:var(--bull);border:1px solid #16a34a}
.sl{background:#1f0914;color:var(--bear);border:1px solid #e11d48}
.eod{background:#1c1917;color:#a8a29e;border:1px solid #78716c}
.manual-c{background:#1c1c1c;color:#d4d4d4;border:1px solid #525252}
.aplus{background:#422006;color:#fcd34d;border:1px solid #d97706}
button{cursor:pointer;border:none;border-radius:6px;padding:5px 12px;font-size:12px;font-weight:600;transition:opacity .15s}
button:hover{opacity:.85}
.btn-buy{background:#16a34a;color:#fff}.btn-sell{background:#e11d48;color:#fff}
.btn-close{background:#374151;color:#e5e7eb}.btn-del{background:#7f1d1d;color:#fca5a5}
.btn-log{background:var(--gold);color:#111;font-weight:700;padding:6px 16px;border-radius:8px}
form{display:flex;flex-direction:column;gap:8px}
label{font-size:11px;color:var(--slate);margin-bottom:2px}
input,select{background:#0f172a;border:1px solid var(--border);border-radius:6px;color:var(--text);padding:5px 10px;font-size:13px;width:100%}
input:focus,select:focus{outline:2px solid var(--gold);border-color:var(--gold)}
.row2{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.alert-item{padding:6px 0;border-bottom:1px solid #0f172a;font-size:12px}
.alert-item:last-child{border:none}
.at{color:var(--slate);font-size:10px;min-width:36px;display:inline-block}
.ak{display:inline-block;padding:1px 6px;border-radius:4px;font-size:10px;font-weight:700;margin:0 4px;background:#1e293b;color:var(--gold2)}
.use-btn{float:right;background:#1e293b;color:var(--gold2);border:1px solid #f59e0b44;padding:2px 8px;border-radius:5px;font-size:11px;cursor:pointer}
.use-btn:hover{background:#2d3748}
#msg{position:fixed;bottom:20px;right:20px;background:#1e293b;border:1px solid var(--gold);border-radius:8px;
  padding:10px 16px;font-size:13px;color:var(--gold2);display:none;z-index:99}
</style>
</head>
<body>
<div class="hdr">
  <h1>📋 Paper Trade Desk</h1>
  <span class="badge">Port 8610 · Information only — no real orders</span>
  <span class="badge" id="mkt-status">Loading…</span>
  <span style="margin-left:auto;font-size:11px;color:var(--slate)" id="last-update"></span>
</div>

<div class="stat-row" id="stat-row">
  <div class="stat"><div class="lbl">Trades</div><div class="val slate" id="s-n">—</div></div>
  <div class="stat"><div class="lbl">Open</div><div class="val gold" id="s-open">—</div></div>
  <div class="stat"><div class="lbl">Win %</div><div class="val" id="s-wr">—</div></div>
  <div class="stat"><div class="lbl">Avg R</div><div class="val" id="s-ar">—</div></div>
  <div class="stat"><div class="lbl">Total R</div><div class="val" id="s-tr">—</div></div>
  <div class="stat"><div class="lbl">P&L pts</div><div class="val" id="s-pts">—</div></div>
  <div class="stat"><div class="lbl">P&L ₹ (est.)</div><div class="val" id="s-rs">—</div></div>
</div>

<div class="cols">
  <!-- Log a trade -->
  <div class="card">
    <div class="card-h">
      ✏️ Log a paper trade
      <span style="font-size:11px;color:var(--slate)">closed candle only · fixed SL · 1 lot</span>
    </div>
    <div class="card-b">
      <form id="log-form">
        <div class="row2">
          <div>
            <label>Timeframe</label>
            <select id="f-tf"><option>1m</option><option selected>5m</option><option>15m</option><option>1h</option></select>
          </div>
          <div>
            <label>Signal type</label>
            <select id="f-signal">
              <option value="aplus">★ A+ (bias + trigger)</option>
              <option value="bias">⚖ Bias</option>
              <option value="conf">✓3 Confluence</option>
              <option value="ifvg">IFVG</option>
              <option value="crt">CRT entry</option>
              <option value="smt">SMT</option>
              <option value="dol">D.O.L</option>
              <option value="manual">Manual / other</option>
            </select>
          </div>
        </div>
        <div class="row2">
          <div>
            <button type="button" class="btn-buy" id="btn-buy" onclick="setSide('BUY')">▲ BUY (CE)</button>
          </div>
          <div>
            <button type="button" class="btn-sell" id="btn-sell" onclick="setSide('SELL')">▼ SELL (PE)</button>
          </div>
        </div>
        <input type="hidden" id="f-side" value="">
        <div class="row2">
          <div><label>Entry price</label><input type="number" id="f-entry" step="0.5" placeholder="e.g. 24150"></div>
          <div><label>Stop loss</label><input type="number" id="f-sl" step="0.5" placeholder="e.g. 24080"></div>
        </div>
        <div class="row2">
          <div><label>Target (TP)</label><input type="number" id="f-tp" step="0.5" placeholder="e.g. 24250"></div>
          <div><label>Note (optional)</label><input type="text" id="f-note" placeholder="e.g. IFVG below 24100"></div>
        </div>
        <div id="rr-preview" style="font-size:12px;color:var(--slate);min-height:18px"></div>
        <button type="button" class="btn-log" onclick="logTrade()">📝 Log trade</button>
      </form>
    </div>
  </div>

  <!-- Recent chart signals -->
  <div class="card">
    <div class="card-h">
      📣 Recent chart signals
      <span style="font-size:11px;color:var(--slate)">from 8600 alert log</span>
    </div>
    <div class="card-b" id="alert-list">Loading…</div>
  </div>
</div>

<!-- Trades table -->
<div style="padding:0 16px 16px">
  <div class="card">
    <div class="card-h">
      📊 Paper trade log
      <span style="font-size:11px;color:var(--slate)" id="enough-note"></span>
    </div>
    <div class="card-b">
      <table>
        <thead>
          <tr>
            <th>#</th><th>Time</th><th>TF</th><th>Side</th><th>Signal</th>
            <th>Entry</th><th>SL</th><th>TP</th><th>Exit</th><th>Status</th>
            <th>P&L pts</th><th>R</th><th>Note</th><th>Actions</th>
          </tr>
        </thead>
        <tbody id="trade-tbody"></tbody>
      </table>
    </div>
  </div>
</div>

<div id="msg"></div>

<script>
let _side = '';
let _trades = [];

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
  if (!entry || !sl || !tp) { el.textContent = ''; return; }
  const risk = Math.abs(entry - sl);
  const reward = Math.abs(tp - entry);
  if (risk < 0.1) { el.textContent = '⚠ SL too close'; el.style.color = '#f43f5e'; return; }
  const rr = (reward / risk).toFixed(1);
  const ok = parseFloat(rr) >= 1.5;
  el.style.color = ok ? '#22c55e' : '#f59e0b';
  el.textContent = `Risk ${risk.toFixed(1)} pts · Reward ${reward.toFixed(1)} pts · RR ${rr} ${ok ? '✓ good' : '(aim for ≥ 1.5)'}`;
}

['f-entry','f-sl','f-tp'].forEach(id => document.getElementById(id).addEventListener('input', calcRR));

function toast(msg, ok=true) {
  const el = document.getElementById('msg');
  el.textContent = msg;
  el.style.color = ok ? '#4ade80' : '#f87171';
  el.style.display = 'block';
  setTimeout(() => el.style.display = 'none', 3000);
}

async function logTrade() {
  const entry = parseFloat(document.getElementById('f-entry').value);
  const sl = parseFloat(document.getElementById('f-sl').value);
  const tp = parseFloat(document.getElementById('f-tp').value);
  if (!_side) { toast('Select BUY or SELL first', false); return; }
  if (!entry || !sl || !tp) { toast('Fill entry / SL / TP', false); return; }
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
    toast('Error: ' + (e.detail || r.status), false);
  }
}

async function closeTrade(id, status) {
  const px = prompt(`Close trade #${id} as ${status}\nEnter exit price:`);
  if (!px) return;
  const r = await fetch(`/api/trades/${id}/close`, {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ exit_px: parseFloat(px), status })
  });
  const d = await r.json();
  if (r.ok) toast(`✓ Closed at ${px} · R = ${d.r}`);
  else toast('Error: ' + (d.detail || r.status), false);
  refresh();
}

async function deleteTrade(id) {
  if (!confirm(`Delete trade #${id}? Cannot undo.`)) return;
  await fetch(`/api/trades/${id}`, { method: 'DELETE' });
  refresh();
}

function fillFromAlert(side, entry) {
  setSide(side);
  document.getElementById('f-entry').value = entry;
  document.getElementById('f-entry').focus();
  calcRR();
  toast(`Entry pre-filled from signal: ${entry}`);
}

function colorR(r) {
  if (r == null) return '<span class="slate">—</span>';
  const n = parseFloat(r);
  const cls = n > 0 ? 'bull' : n < 0 ? 'bear' : 'slate';
  return `<span class="${cls}">${n > 0 ? '+' : ''}${n.toFixed(2)}R</span>`;
}

function colorPts(pts, side) {
  if (pts == null) return '<span class="slate">—</span>';
  const n = parseFloat(pts);
  const pos = side === 'BUY' ? n > 0 : n < 0;
  return `<span class="${pos ? 'bull' : 'bear'}">${n > 0 ? '+' : ''}${n.toFixed(1)}</span>`;
}

function sigLabel(s) {
  const m = { aplus:'★A+', bias:'⚖Bias', conf:'✓3', ifvg:'IFVG', crt:'CRT', smt:'SMT', dol:'DOL', manual:'manual' };
  return m[s] || s || '—';
}

function renderTrades(trades) {
  const tbody = document.getElementById('trade-tbody');
  if (!trades.length) { tbody.innerHTML = '<tr><td colspan="14" style="color:var(--slate);text-align:center;padding:20px">No trades yet — log your first one above</td></tr>'; return; }
  tbody.innerHTML = trades.map(t => {
    const st = (t.status || 'OPEN').toLowerCase();
    const stPill = `<span class="pill ${st}">${t.status}</span>`;
    const sidePill = `<span class="pill ${t.side === 'BUY' ? 'buy' : 'sell'}">${t.side}</span>`;
    const actions = t.status === 'OPEN'
      ? `<button class="btn-close" onclick="closeTrade(${t.id},'TP')">TP</button>
         <button class="btn-close" onclick="closeTrade(${t.id},'SL')" style="background:#7f1d1d;color:#fca5a5">SL</button>
         <button class="btn-close" onclick="closeTrade(${t.id},'EOD')">EOD</button>
         <button class="btn-close" onclick="closeTrade(${t.id},'MANUAL')" style="margin-left:4px">Exit</button>
         <button class="btn-del" onclick="deleteTrade(${t.id})" style="margin-left:4px">✕</button>`
      : `<button class="btn-del" onclick="deleteTrade(${t.id})">✕</button>`;
    const sigPill = t.signal === 'aplus'
      ? `<span class="pill aplus">${sigLabel(t.signal)}</span>`
      : `<span style="font-size:11px;color:var(--gold2)">${sigLabel(t.signal)}</span>`;
    return `<tr>
      <td style="color:var(--slate)">#${t.id}</td>
      <td style="color:var(--slate);white-space:nowrap">${t.logged}</td>
      <td>${t.tf || '—'}</td>
      <td>${sidePill}</td>
      <td>${sigPill}</td>
      <td class="gold">${t.entry ?? '—'}</td>
      <td class="bear">${t.sl ?? '—'}</td>
      <td class="bull">${t.tp ?? '—'}</td>
      <td>${t.exit_px ?? '<span class="slate">open</span>'}</td>
      <td>${stPill}</td>
      <td>${t.status !== 'OPEN' ? colorPts(t.pnl_pts, t.side) : '<span class="slate">—</span>'}</td>
      <td>${colorR(t.r)}</td>
      <td style="color:var(--slate);font-size:11px;max-width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${t.note || ''}">${t.note || ''}</td>
      <td style="white-space:nowrap">${actions}</td>
    </tr>`;
  }).join('');
}

function renderAlerts(alerts) {
  const el = document.getElementById('alert-list');
  if (!alerts.length) { el.innerHTML = '<div style="color:var(--slate);padding:8px">No signals yet today — they appear here as you trade.</div>'; return; }
  el.innerHTML = alerts.map(a => {
    const isBuy = a.side === 'BUY' || (a.kind || '').includes('BUY');
    const isSell = a.side === 'SELL' || (a.kind || '').includes('SELL');
    const useBtn = (isBuy || isSell)
      ? `<button class="use-btn" onclick="fillFromAlert('${isBuy ? 'BUY' : 'SELL'}',${a.price})">Use →</button>`
      : '';
    return `<div class="alert-item">
      ${useBtn}
      <span class="at">${a.time_str}</span>
      <span class="ak">${(a.kind||'SIG').replace('SIG_','').replace(/_BUY|_SELL|_INFO/,'')}</span>
      <span class="${isBuy ? 'bull' : isSell ? 'bear' : 'slate'}">${a.title || a.kind}</span>
      ${a.price ? `<span style="color:var(--slate);font-size:11px;margin-left:4px">@ ${a.price}</span>` : ''}
    </div>`;
  }).join('');
}

function renderStats(s) {
  if (!s || s.n === 0) {
    ['s-n','s-open','s-wr','s-ar','s-tr','s-pts','s-rs'].forEach(id => {
      const el = document.getElementById(id);
      if (id === 's-open') { el.textContent = s?.open ?? 0; el.className = 'val gold'; }
      else { el.textContent = '—'; el.className = 'val slate'; }
    });
    return;
  }
  const n = document.getElementById('s-n'); n.textContent = s.n; n.className = 'val slate';
  const op = document.getElementById('s-open'); op.textContent = s.open; op.className = 'val gold';
  const wr = document.getElementById('s-wr'); wr.textContent = s.win_rate + '%';
  wr.className = 'val ' + (s.win_rate >= 50 ? 'bull' : 'bear');
  const ar = document.getElementById('s-ar'); ar.textContent = (s.avg_r > 0 ? '+' : '') + s.avg_r + 'R';
  ar.className = 'val ' + (s.avg_r >= 0 ? 'bull' : 'bear');
  const tr = document.getElementById('s-tr'); tr.textContent = (s.total_r > 0 ? '+' : '') + s.total_r + 'R';
  tr.className = 'val ' + (s.total_r >= 0 ? 'bull' : 'bear');
  const pts = document.getElementById('s-pts'); pts.textContent = (s.pnl_pts > 0 ? '+' : '') + s.pnl_pts;
  pts.className = 'val ' + (s.pnl_pts >= 0 ? 'bull' : 'bear');
  const rs = document.getElementById('s-rs');
  rs.textContent = '₹' + s.pnl_rs.toLocaleString('en-IN');
  rs.className = 'val ' + (s.pnl_rs >= 0 ? 'bull' : 'bear');
  document.getElementById('enough-note').textContent = s.note;
}

async function refresh() {
  try {
    const [td, al] = await Promise.all([
      fetch('/api/trades').then(r => r.json()),
      fetch('/api/alerts').then(r => r.json()),
    ]);
    renderStats(td.stats);
    renderTrades(td.trades);
    renderAlerts(al.alerts);
    document.getElementById('last-update').textContent = 'Updated ' + new Date().toLocaleTimeString('en-IN', {hour:'2-digit',minute:'2-digit',second:'2-digit'});
    document.getElementById('mkt-status').innerHTML = `<b>${td.stats.open || 0}</b> open trades`;
  } catch(e) {
    document.getElementById('mkt-status').textContent = 'Connection error — is 8610 running?';
  }
}

refresh();
setInterval(refresh, 10000);
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(content=PAGE)
