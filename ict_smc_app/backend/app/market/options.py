"""
market/options.py — 1-minute candles for the SELECTED option strikes (and any strike held by an open paper trade).

Source: REST POST /charts/intraday with exchangeSegment NSE_FNO, instrument OPTIDX — Dhan's own candles, with
volume. Fetched a few seconds after each minute closes (`strikes.candle_fetch_delay_s`), so only CLOSED candles
are ever stored; a REST download is also the truth after any reconnect. 2–3 calls a minute — far below the
5 / s data limit. Stored in candles_1m under sec_id "O:<security id>" so they never mix with the index.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Dict, List, Optional

from .. import db
from ..timeutil import is_closed, now_epoch, now_ist
from .candles import resample, session_filter
from .instruments import Instrument


def sec_key(inst: Instrument) -> str:
    if inst.type != "OPTION" or not inst.security_id:
        raise ValueError(f"{inst.label}: no Dhan security id (resolve it from the option chain first)")
    return f"O:{inst.security_id}"


async def refresh(rest, inst: Instrument, days: int = 1) -> int:
    to = now_ist()
    frm = (to - timedelta(days=max(0, days - 1))).replace(hour=9, minute=0, second=0, microsecond=0)
    rows = session_filter(await rest.intraday(inst.security_id, frm, to, "1", "NSE_FNO", "OPTIDX"))
    now = now_epoch()
    rows = [r for r in rows if r["ts"] <= now and is_closed(r["ts"], "1m", now)]
    db.upsert_candles(sec_key(inst), rows)
    return len(rows)


def rows_1m(inst: Instrument, days: int = 3) -> List[Dict[str, float]]:
    since = int((now_ist() - timedelta(days=days + 3)).timestamp())
    return session_filter(db.candles_1m(sec_key(inst), since))


def frame(inst: Instrument, tf: str, days: int = 3, now: Optional[float] = None) -> List[Dict[str, float]]:
    """CLOSED candles of the option at tf, 09:15-aligned like the index."""
    return [c for c in resample(rows_1m(inst, days), tf, now) if c["closed"]]


def by_ts(inst: Instrument, tf: str, days: int = 3) -> Dict[int, Dict[str, float]]:
    try:
        return {int(c["ts"]): c for c in frame(inst, tf, days)}
    except ValueError:
        return {}


def close_at(inst_key_sid: str, minute_ts: int) -> Optional[float]:
    """Close of the option's 1m candle that STARTS at minute_ts (sec_id 'O:<sid>')."""
    r = db.query("SELECT c FROM candles_1m WHERE sec_id=? AND ts=?", (inst_key_sid, int(minute_ts)))
    return float(r[0]["c"]) if r else None
