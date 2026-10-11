"""
db.py — SQLite store (WAL mode). Why SQLite and not Redis for v1: one file, zero services to run on a laptop,
survives reboots, and at NIFTY volumes (≈375 one-minute candles a day) it is never the bottleneck.

What is stored — and what deliberately is NOT (mistake #4):
  candles_1m   every closed 1-minute candle per instrument. Higher timeframes are rebuilt from these.
  daily        daily OHLC built from 1m/60m (Dhan's /charts/historical rejects the NIFTY index — DH-905).
  chain_latest the latest full option chain per expiry (so the table survives a refresh / restart).
  chain_snap   per-strike OI/LTP once a minute, kept chain_keep_days days (for intraday OI change charts).
  alerts, paper_trades, kv.
  zones        the zone registry (pools / OBs / FVGs, forward-only states)        — engine/registry.py
  events       delta / footprint events log (unique id → no duplicates)          — delta/engine.py
  delta_bars, footprint_levels   imported CSV delta (per candle start, IST-aligned) — delta/adapters.py
  Raw ticks are NEVER written. A tick only updates the forming 1m candle in memory.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any, Dict, Iterable, List, Optional

from . import config

_lock = threading.RLock()
_conn: Optional[sqlite3.Connection] = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS candles_1m (
  sec_id TEXT NOT NULL, ts INTEGER NOT NULL,
  o REAL NOT NULL, h REAL NOT NULL, l REAL NOT NULL, c REAL NOT NULL, v REAL NOT NULL DEFAULT 0,
  PRIMARY KEY (sec_id, ts)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS daily (
  sec_id TEXT NOT NULL, d TEXT NOT NULL,
  o REAL, h REAL, l REAL, c REAL, PRIMARY KEY (sec_id, d)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS chain_latest (expiry TEXT PRIMARY KEY, ts INTEGER, payload TEXT);
CREATE TABLE IF NOT EXISTS chain_snap (
  expiry TEXT NOT NULL, ts INTEGER NOT NULL, strike REAL NOT NULL,
  ce_oi REAL, pe_oi REAL, ce_ltp REAL, pe_ltp REAL, PRIMARY KEY (expiry, ts, strike)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, key TEXT UNIQUE NOT NULL,
  tf TEXT, kind TEXT, side TEXT, grade TEXT, title TEXT, body TEXT, price REAL, payload TEXT);
CREATE TABLE IF NOT EXISTS paper_trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT, signal_key TEXT UNIQUE NOT NULL, setup TEXT, tf TEXT, side TEXT,
  opened_ts INTEGER, entry REAL, sl REAL, tp REAL, risk REAL, rr REAL,
  status TEXT, closed_ts INTEGER, exit REAL, r REAL, pnl_pts REAL, mfe_r REAL, notes TEXT);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
CREATE INDEX IF NOT EXISTS ix_alerts_ts ON alerts(ts);
CREATE TABLE IF NOT EXISTS zones (
  id TEXT PRIMARY KEY, tf TEXT NOT NULL, type TEXT NOT NULL, side TEXT, low REAL, high REAL,
  created_at INTEGER NOT NULL, mitigated_at INTEGER, invalidated_at INTEGER, state TEXT,
  source_event_id TEXT, payload TEXT, updated INTEGER);
CREATE INDEX IF NOT EXISTS ix_zones_tf ON zones(tf, created_at);
CREATE TABLE IF NOT EXISTS events (
  id TEXT PRIMARY KEY, ts INTEGER NOT NULL, tf TEXT, type TEXT, dir TEXT, zone_id TEXT, payload TEXT);
CREATE INDEX IF NOT EXISTS ix_events_ts ON events(tf, ts);
CREATE TABLE IF NOT EXISTS delta_bars (
  inst TEXT NOT NULL DEFAULT 'NIFTY', ts INTEGER NOT NULL, secs INTEGER NOT NULL DEFAULT 60, buy REAL, sell REAL,
  delta REAL NOT NULL, src TEXT, PRIMARY KEY (inst, ts)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS footprint_levels (
  inst TEXT NOT NULL DEFAULT 'NIFTY', ts INTEGER NOT NULL, price REAL NOT NULL, buy REAL NOT NULL, sell REAL NOT NULL,
  PRIMARY KEY (inst, ts, price)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS fut_oi (ts INTEGER PRIMARY KEY, oi REAL NOT NULL, src TEXT) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS fut_oi_sym (sym TEXT NOT NULL, ts INTEGER NOT NULL, oi REAL NOT NULL, PRIMARY KEY (sym, ts)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS iv_series (
  key TEXT NOT NULL, ts INTEGER NOT NULL, iv REAL, delta REAL, ltp REAL, strike REAL, PRIMARY KEY (key, ts)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS opt_activity (
  ts INTEGER PRIMARY KEY, ce REAL NOT NULL DEFAULT 0, pe REAL NOT NULL DEFAULT 0, raw REAL NOT NULL DEFAULT 0,
  samples INTEGER NOT NULL DEFAULT 0) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS strike_picks (
  ts INTEGER NOT NULL, expiry TEXT, ce_key TEXT, pe_key TEXT, payload TEXT, PRIMARY KEY (ts)) WITHOUT ROWID;
"""


def conn() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            _conn = sqlite3.connect(str(config.DB_PATH), check_same_thread=False, isolation_level=None)
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.execute("PRAGMA synchronous=NORMAL")
            _migrate(_conn)
            _conn.executescript(SCHEMA)
            _add_columns(_conn)
        return _conn


def _cols(c: sqlite3.Connection, table: str) -> List[str]:
    return [r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()]


def _migrate(c: sqlite3.Connection) -> None:
    """9 Oct 2026: delta_bars / footprint_levels gained an `inst` key (index vs option strike). Old rows = NIFTY."""
    for t, cols in (("delta_bars", "ts, secs, buy, sell, delta, src"), ("footprint_levels", "ts, price, buy, sell")):
        have = _cols(c, t)
        if have and "inst" not in have:
            c.execute(f"ALTER TABLE {t} RENAME TO {t}_old")
            c.executescript(SCHEMA)
            c.execute(f"INSERT OR IGNORE INTO {t} (inst, {cols}) SELECT 'NIFTY', {cols} FROM {t}_old")
            c.execute(f"DROP TABLE {t}_old")


def _add_columns(c: sqlite3.Connection) -> None:
    """Additive columns on paper_trades (old rows keep NULL = index-only trades)."""
    have = _cols(c, "paper_trades")
    for col, typ in (("instrument_mode", "TEXT"), ("strikes_snapshot", "TEXT"), ("opt_key", "TEXT"),
                     ("opt_sid", "TEXT"), ("opt_entry", "REAL"), ("opt_exit", "REAL"), ("opt_pnl_pts", "REAL"),
                     ("opt_pnl_rs", "REAL"), ("confidence", "REAL"), ("confidence_parts", "TEXT"),
                     ("tp1", "REAL"), ("half_done", "INTEGER"), ("partial_pts", "REAL"), ("half_ts", "INTEGER"),
                     ("slot", "TEXT"), ("pair", "TEXT"), ("model", "TEXT")):
        if col not in have:
            c.execute(f"ALTER TABLE paper_trades ADD COLUMN {col} {typ}")


def execmany(sql: str, rows: Iterable[tuple]) -> None:
    with _lock:
        c = conn()
        c.execute("BEGIN")
        try:
            c.executemany(sql, rows)
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise


def query(sql: str, args: tuple = ()) -> List[sqlite3.Row]:
    with _lock:
        return conn().execute(sql, args).fetchall()


def execute(sql: str, args: tuple = ()) -> sqlite3.Cursor:
    with _lock:
        return conn().execute(sql, args)


# ─── candles ────────────────────────────────────────────────────────────────
def upsert_candles(sec_id: str, rows: List[Dict[str, float]]) -> None:
    """rows: dicts with ts (epoch of candle start), o, h, l, c, v. Replaces any existing candle at ts —
    a REST backfill is the source of truth over a candle we built from ticks during a feed hiccup."""
    if not rows:
        return
    execmany("INSERT OR REPLACE INTO candles_1m (sec_id, ts, o, h, l, c, v) VALUES (?,?,?,?,?,?,?)",
             [(sec_id, int(r["ts"]), r["o"], r["h"], r["l"], r["c"], r.get("v", 0.0)) for r in rows])


def candles_1m(sec_id: str, since: int, until: Optional[int] = None) -> List[Dict[str, float]]:
    if until is None:
        rows = query("SELECT ts,o,h,l,c,v FROM candles_1m WHERE sec_id=? AND ts>=? ORDER BY ts", (sec_id, since))
    else:
        rows = query("SELECT ts,o,h,l,c,v FROM candles_1m WHERE sec_id=? AND ts>=? AND ts<? ORDER BY ts",
                     (sec_id, since, until))
    return [dict(r) for r in rows]


def last_candle_ts(sec_id: str) -> Optional[int]:
    r = query("SELECT MAX(ts) AS m FROM candles_1m WHERE sec_id=?", (sec_id,))
    return int(r[0]["m"]) if r and r[0]["m"] is not None else None


def upsert_daily(sec_id: str, rows: List[Dict[str, Any]]) -> None:
    execmany("INSERT OR REPLACE INTO daily (sec_id, d, o, h, l, c) VALUES (?,?,?,?,?,?)",
             [(sec_id, r["d"], r["o"], r["h"], r["l"], r["c"]) for r in rows])


def daily(sec_id: str, limit: int = 60) -> List[Dict[str, Any]]:
    rows = query("SELECT d,o,h,l,c FROM daily WHERE sec_id=? ORDER BY d DESC LIMIT ?", (sec_id, limit))
    return [dict(r) for r in rows][::-1]


# ─── option chain ───────────────────────────────────────────────────────────
def save_chain(expiry: str, ts: int, payload: Dict[str, Any]) -> None:
    execute("INSERT OR REPLACE INTO chain_latest (expiry, ts, payload) VALUES (?,?,?)",
            (expiry, ts, json.dumps(payload)))


def load_chains() -> Dict[str, Dict[str, Any]]:
    out = {}
    for r in query("SELECT expiry, ts, payload FROM chain_latest"):
        try:
            out[r["expiry"]] = json.loads(r["payload"])
        except ValueError:
            pass
    return out


def save_chain_snap(expiry: str, ts: int, rows: List[tuple]) -> None:
    execmany("INSERT OR REPLACE INTO chain_snap (expiry, ts, strike, ce_oi, pe_oi, ce_ltp, pe_ltp) "
             "VALUES (?,?,?,?,?,?,?)", [(expiry, ts) + tuple(r) for r in rows])


# ─── kv ─────────────────────────────────────────────────────────────────────
def kv_get(k: str, default: Any = None) -> Any:
    r = query("SELECT v FROM kv WHERE k=?", (k,))
    if not r:
        return default
    try:
        return json.loads(r[0]["v"])
    except ValueError:
        return default


def kv_set(k: str, v: Any) -> None:
    execute("INSERT OR REPLACE INTO kv (k, v) VALUES (?,?)", (k, json.dumps(v)))


# ─── housekeeping ───────────────────────────────────────────────────────────
def prune() -> Dict[str, int]:
    """Run once a day: candles older than candles_keep_days, chain snapshots older than chain_keep_days."""
    now = int(time.time())
    keep_c = int(config.rule("data", "candles_keep_days", 400)) * 86400
    keep_s = int(config.rule("data", "chain_keep_days", 5)) * 86400
    a = execute("DELETE FROM candles_1m WHERE ts < ?", (now - keep_c,)).rowcount
    b = execute("DELETE FROM chain_snap WHERE ts < ?", (now - keep_s,)).rowcount
    keep_d = int(config.rule("delta", "keep_days", 10)) * 86400
    keep_z = int(config.rule("zones", "keep_days", 10)) * 86400
    z = execute("DELETE FROM zones WHERE created_at < ?", (now - keep_z,)).rowcount
    e = execute("DELETE FROM events WHERE ts < ?", (now - keep_d,)).rowcount
    return {"candles_deleted": a, "chain_snaps_deleted": b, "zones_deleted": z, "events_deleted": e}
