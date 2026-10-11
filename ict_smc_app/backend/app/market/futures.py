"""
market/futures.py — the current-month NIFTY future: the only real source of NIFTY volume.

The index (IDX_I 13) never trades, so its volume is always 0. Every "NIFTY VWAP" or "NIFTY volume profile" on any
platform is built from the future. This module:

  resolve()   picks the contract. Dhan's scrip master PROPOSES (FUTIDX rows whose symbol starts with "NIFTY-",
              nearest unexpired first), the PRICE DISPOSES: a candidate is accepted only if it trades within
              `max_basis_pct` of the index. (Lesson from your own dhan_client.py: a name match once returned a
              contract at 1,580 while NIFTY was at 24,080 and every check still passed.)
              Rollover: in the last `rollover_days` before expiry, if the next month traded MORE volume today,
              the next month is used — the profile must run on the contract that carries the volume.
              Pin a contract with rules futures.security_id or env NIFTY_FUT_ID.
  refresh()   1-minute candles WITH volume from REST into candles_1m under sec_id "FUT" (closed minutes only).
              One continuous key: on a rollover day, re-downloaded minutes simply carry the new contract.
  mapped()    the BASIS-ADJUSTED future: each minute's OHLC minus that minute's basis (fut close − spot close),
              so its volume sits at INDEX prices and every level lands correctly on the NIFTY chart.
"""
from __future__ import annotations

import csv
import io
import logging
import time
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import httpx

from .. import config, db
from ..timeutil import IST, is_closed, now_epoch, now_ist
from .candles import session_filter

log = logging.getLogger("futures")

SEC = "FUT"
SCRIP_MASTER = "https://images.dhan.co/api-data/api-scrip-master.csv"
CACHE = config.DATA_DIR / "scrip_master_futidx_v2.csv"     # NIFTY- and BANKNIFTY- FUTIDX rows
BN_SEC = "BNFUT"
PREFIXES = ("NIFTY-", "BANKNIFTY-")


def _cfg(k: str, d: Any) -> Any:
    return config.rule("futures", k, d)


# ── scrip master ─────────────────────────────────────────────────────────────
def _col(cols: Dict[str, str], *names: str) -> Optional[str]:
    for n in names:
        if n in cols:
            return cols[n]
    return None


def parse_master(text: str, today: Optional[date] = None, prefix: str = "NIFTY-") -> List[Dict[str, Any]]:
    """NIFTY index futures from the scrip master, nearest expiry first. Only FUTIDX rows on NSE whose trading
    symbol is exactly NIFTY-<month>-FUT (BANKNIFTY / FINNIFTY / NIFTYNXT50 … are excluded by the 'NIFTY-' prefix)."""
    today = today or now_ist().date()
    rd = csv.DictReader(io.StringIO(text))
    cols = {c.upper().strip().replace(" ", "_"): c for c in (rd.fieldnames or [])}
    exch, inst = _col(cols, "SEM_EXM_EXCH_ID", "EXCH_ID"), _col(cols, "SEM_INSTRUMENT_NAME", "INSTRUMENT_NAME")
    sym, exp = _col(cols, "SEM_TRADING_SYMBOL", "TRADING_SYMBOL"), _col(cols, "SEM_EXPIRY_DATE", "EXPIRY_DATE")
    sid = _col(cols, "SEM_SMST_SECURITY_ID", "SECURITY_ID")
    if not all([exch, inst, sym, exp, sid]):
        raise ValueError(f"scrip master columns unexpected: {list(cols)[:12]}")
    out = []
    for r in rd:
        if str(r[exch]).upper() != "NSE" or str(r[inst]).upper() != "FUTIDX":
            continue
        s = str(r[sym]).upper()
        if not s.startswith(prefix):
            continue
        try:
            d = datetime.strptime(str(r[exp])[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        if d >= today:
            out.append({"sid": str(r[sid]).split(".")[0], "symbol": str(r[sym]), "expiry": d.isoformat()})
    return sorted(out, key=lambda x: x["expiry"])


async def load_master(prefix: str = "NIFTY-") -> List[Dict[str, Any]]:
    """Downloads the (≈30 MB) scrip master at most once a day; keeps only the NIFTY FUTIDX rows on disk."""
    fresh = CACHE.exists() and time.time() - CACHE.stat().st_mtime < 20 * 3600
    if not fresh:
        url = config.env("DHAN_SCRIP_MASTER_URL") or _cfg("scrip_master_url", SCRIP_MASTER)
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=15.0)) as cl:
            r = await cl.get(url)
            r.raise_for_status()
        rows = [x for p in PREFIXES for x in parse_master(r.text, prefix=p)]
        with CACHE.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["SEM_EXM_EXCH_ID", "SEM_INSTRUMENT_NAME", "SEM_TRADING_SYMBOL", "SEM_EXPIRY_DATE", "SEM_SMST_SECURITY_ID"])
            for x in rows:
                w.writerow(["NSE", "FUTIDX", x["symbol"], x["expiry"], x["sid"]])
    return parse_master(CACHE.read_text(), prefix=prefix)


# ── resolve ──────────────────────────────────────────────────────────────────
async def _today_rows(rest, sid: str) -> List[Dict[str, float]]:
    to = now_ist()
    frm = (to - timedelta(days=4)).replace(hour=9, minute=0, second=0, microsecond=0)
    return session_filter(await rest.intraday(sid, frm, to, "1", "NSE_FNO", "FUTIDX"))


async def resolve(rest, index_close: float, prefix: str = "NIFTY-") -> Tuple[Optional[Dict[str, Any]], str]:
    """→ ({sid, symbol, expiry, basis, why}, explanation) or (None, explanation). prefix "BANKNIFTY-" for BANKNIFTY."""
    bank = prefix.startswith("BANK")
    pinned = (config.env("BANKNIFTY_FUT_ID") or _cfg("banknifty_security_id", None)) if bank else \
        (config.env("NIFTY_FUT_ID") or _cfg("security_id", None))
    if pinned:
        return {"sid": str(pinned), "symbol": "pinned", "expiry": None, "basis": None,
                "why": "pinned (rules futures.security_id / env NIFTY_FUT_ID)"}, "pinned contract"
    try:
        cands = (await load_master(prefix))[: int(_cfg("max_candidates", 3))]
    except Exception as e:                                   # noqa: BLE001
        return None, f"could not read Dhan's scrip master: {str(e)[:160]}"
    if not cands:
        return None, f"no {prefix.rstrip('-')} FUTIDX rows in the scrip master"
    lines, ok = [], []
    tol = float(_cfg("max_basis_pct", 2.5))
    for c in cands:
        try:
            rows = await _today_rows(rest, c["sid"])
        except Exception as e:                               # noqa: BLE001
            lines.append(f"{c['symbol']} ({c['sid']}): fetch failed — {str(e)[:60]}")
            continue
        if not rows:
            lines.append(f"{c['symbol']} ({c['sid']}): no candles")
            continue
        last = rows[-1]["c"]
        drift = 100 * (last - index_close) / index_close if index_close else 0.0
        if index_close and abs(drift) > tol:
            lines.append(f"{c['symbol']} ({c['sid']}): REJECTED — trades at {last:,.1f} vs index {index_close:,.1f} ({drift:+.2f} %)")
            continue
        day = max(r["ts"] for r in rows) // 86400
        vol = sum(r.get("v", 0) for r in rows if r["ts"] // 86400 == day)
        ok.append(dict(c, basis=round(last - index_close, 2) if index_close else None, vol_today=vol))
        lines.append(f"{c['symbol']} ({c['sid']}): ok — {last:,.1f}, basis {last - index_close:+.1f}, volume today {vol:,.0f}")
    if not ok:
        return None, "; ".join(lines)
    pick = ok[0]
    days_left = (date.fromisoformat(pick["expiry"]) - now_ist().date()).days
    if len(ok) > 1 and days_left <= int(_cfg("rollover_days", 4)) and ok[1]["vol_today"] > pick["vol_today"]:
        pick = ok[1]
        lines.append(f"rollover: {pick['symbol']} traded more today, {days_left} day(s) before the front expiry")
    pick["why"] = "; ".join(lines)
    return pick, pick["why"]


# ── candles ──────────────────────────────────────────────────────────────────
async def refresh(rest, sid: str, days: int = 1, sec: str = SEC) -> int:
    to = now_ist()
    frm = (to - timedelta(days=max(0, days - 1))).replace(hour=9, minute=0, second=0, microsecond=0)
    rows = session_filter(await rest.intraday(sid, frm, to, "1", "NSE_FNO", "FUTIDX", oi=True))
    now = now_epoch()
    rows = [r for r in rows if r["ts"] <= now and is_closed(r["ts"], "1m", now)]
    db.upsert_candles(sec, rows)
    oi = [(int(r["ts"]), r["oi"], "rest") for r in rows if r.get("oi")]
    if oi and sec == SEC:
        db.execmany("INSERT OR REPLACE INTO fut_oi (ts, oi, src) VALUES (?,?,?)", oi)
    elif oi:
        db.execmany("INSERT OR REPLACE INTO fut_oi_sym (sym, ts, oi) VALUES (?,?,?)", [(sec, t, v) for t, v, _ in oi])
    return len(rows)


async def index_close(rest, security_id: str) -> Optional[float]:
    """Last close of an index (e.g. BANKNIFTY = IDX_I 25) — used once, to price-check its future."""
    to = now_ist()
    rows = await rest.intraday(str(security_id), to - timedelta(days=4), to, "60", "IDX_I", "INDEX")
    return float(rows[-1]["c"]) if rows else None


def sym_oi_rows(sec: str, days: int = 5) -> Dict[int, float]:
    since = int((now_ist() - timedelta(days=days + 3)).timestamp())
    return {int(r["ts"]): float(r["oi"]) for r in
            db.query("SELECT ts, oi FROM fut_oi_sym WHERE sym=? AND ts>=? ORDER BY ts", (sec, since))}


def sym_rows_1m(sec: str, days: int = 5) -> List[Dict[str, float]]:
    since = int((now_ist() - timedelta(days=days + 3)).timestamp())
    return session_filter(db.candles_1m(sec, since))


def oi_rows(days: int = 5) -> Dict[int, float]:
    """minute ts → futures open interest at the end of that minute (REST; live feed fills the newest minute)."""
    since = int((now_ist() - timedelta(days=days + 3)).timestamp())
    return {int(r["ts"]): float(r["oi"]) for r in db.query("SELECT ts, oi FROM fut_oi WHERE ts>=? ORDER BY ts", (since,))}


def rows_1m(days: int = 5) -> List[Dict[str, float]]:
    since = int((now_ist() - timedelta(days=days + 3)).timestamp())
    return session_filter(db.candles_1m(SEC, since))


def mapped(spot_1m: List[Dict[str, float]], fut_1m: List[Dict[str, float]]) -> List[Dict[str, float]]:
    """Basis-adjusted future: minute by minute, fut OHLC − (fut close − spot close); volume kept. Minutes missing
    on either side are dropped (no guessed basis). Each row also carries 'basis' and the raw future close."""
    fb = {int(r["ts"]): r for r in fut_1m}
    out = []
    for s in spot_1m:
        f = fb.get(int(s["ts"]))
        if f is None:
            continue
        b = float(f["c"]) - float(s["c"])
        out.append({"ts": int(s["ts"]), "o": f["o"] - b, "h": f["h"] - b, "l": f["l"] - b, "c": f["c"] - b,
                    "v": float(f.get("v") or 0.0), "basis": round(b, 2), "fut_c": f["c"]})
    return out
