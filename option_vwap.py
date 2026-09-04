"""
option_vwap.py — real VWAP for an option strike, from Dhan.

WHY THIS REPLACES THE SAMPLED VERSION
─────────────────────────────────────
chain_metrics.sample() approximates VWAP by watching cumulative volume move
between polls. It has to, because NSE's option chain publishes volume and a
last price but no turnover, and a true VWAP cannot be recovered from that.

Dhan's intraday endpoint returns OHLCV per candle for the option contract
itself. With volume attached to each bar, VWAP is just the definition:

    VWAP = Σ(typical price × volume) ÷ Σ(volume)

No sampling, no gaps between polls, no lost volume from before the app
started — the candles cover the whole session whenever you ask for them.
This is the number a desk would quote.

THE COST, AND WHY IT IS FINE
────────────────────────────
One API call per contract. Fetching every strike would be roughly 1,400
calls a session and is not worth doing.

But you do not need every strike. You need the one you are holding and
perhaps the one you are considering — two calls per poll, which is nothing.
So this module deliberately takes ONE strike at a time and makes you say
which, rather than offering a table that quietly costs a fortune to fill.

WHAT IT STILL CANNOT TELL YOU
─────────────────────────────
That the VWAP is worth trading against. An option's VWAP is thinner than an
index's — a single strike can trade a few thousand lots all day, so its VWAP
can be set by a handful of prints and move sharply when one large order
lands. The `bars` and `volume` figures are returned alongside for exactly
this reason: check them before leaning on the line.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

import numpy as np
import pandas as pd

import config

_cache: dict = {}


@dataclass
class OptionVwap:
    strike: int
    side: str                      # CE | PE
    security_id: str
    symbol: str
    vwap: float
    ltp: float
    volume: float
    bars: int
    session_start: datetime | None
    interval: str

    @property
    def gap(self) -> float:
        return self.ltp - self.vwap

    @property
    def side_of(self) -> str:
        return "above" if self.gap > 0 else "below"

    @property
    def thin(self) -> bool:
        """Too little volume behind the average to lean on it."""
        return self.volume < 50_000 or self.bars < 20

    @property
    def note(self) -> str:
        base = (f"{self.symbol} — VWAP {self.vwap:,.2f}, last {self.ltp:,.2f} "
                f"({self.gap:+.2f}, {self.side_of}). {self.bars} bars, "
                f"{self.volume:,.0f} volume.")
        if self.thin:
            base += (" THIN — this average rests on very little trade. A "
                     "single large print can move it, so treat the line as "
                     "indicative rather than as a level.")
        return base


def resolve(strike: int, side: str, expiry: date | None = None,
            underlying: str = "NIFTY") -> tuple[str, str] | None:
    """
    Security id for one option contract, from Dhan's scrip master.

    Matched on the trading symbol rather than on separate strike and expiry
    columns, because the scrip master's column names differ between segments
    and the symbol is the one field that is reliably present. Nearest
    unexpired expiry unless one is given.

    The whole master is cached after the first read — it is a large CSV and
    resolving four contracts should not download it four times.
    """
    import dhan_client as dc

    key = "master"
    if key not in _cache:
        _cache[key] = pd.read_csv(dc.SCRIP_MASTER, low_memory=False)
    df = _cache[key]
    cols = {c.upper().replace(" ", "_"): c for c in df.columns}

    def col(*names):
        for n in names:
            if n in cols:
                return cols[n]
        return None

    seg = col("SEM_EXM_EXCH_ID", "EXCH_ID")
    sym = col("SEM_TRADING_SYMBOL", "TRADING_SYMBOL")
    inst = col("SEM_INSTRUMENT_NAME", "INSTRUMENT_NAME")
    exp = col("SEM_EXPIRY_DATE", "EXPIRY_DATE")
    sid = col("SEM_SMST_SECURITY_ID", "SECURITY_ID")
    if not all((seg, sym, sid)):
        return None

    m = df[(df[seg].astype(str).str.upper() == "NSE")
           & (df[sym].astype(str).str.upper().str.startswith(underlying))]
    if inst:
        m = m[m[inst].astype(str).str.upper().str.contains("OPT", na=False)]
    # exclude the look-alike underlyings that burned the futures resolver
    m = m[~m[sym].astype(str).str.upper().str.contains(
        "BANK|FIN|MID|NXT|FPI|CPSE|REIT", na=False)]

    want = f"-{int(strike)}-{side.upper()}"
    m = m[m[sym].astype(str).str.upper().str.endswith(want)]
    if m.empty:
        return None

    if exp:
        try:
            m = m.copy()
            m["_e"] = pd.to_datetime(m[exp], errors="coerce")
            today = pd.Timestamp(expiry or date.today())
            m = m[m["_e"] >= today].sort_values("_e")
        except Exception:                                   # noqa: BLE001
            pass
    if m.empty:
        return None
    r = m.iloc[0]
    return str(r[sid]).split(".")[0], str(r[sym])


def vwap(strike: int, side: str, interval: str = "5",
         days_back: int = 1) -> OptionVwap | None:
    """
    True session VWAP for one option strike.

    Typical price (H+L+C)/3 weighted by each bar's volume — the standard
    definition, not a close-weighted shortcut. On an option the difference
    matters more than on an index: a strike's bar can have a wide range on
    modest volume, and using the close alone would ignore where inside that
    range the trade actually happened.

    Bars outside market hours are dropped before weighting. Dhan returns
    them, and an out-of-hours print carries volume that never belonged to
    the session.
    """
    import dhan_client as dc

    got = resolve(strike, side)
    if not got:
        return None
    sid, symbol = got

    try:
        d = dc.intraday(sid, "NSE_FNO", "OPTIDX",
                        days_back=days_back, interval=interval)
    except Exception:                                       # noqa: BLE001
        return None
    if d is None or d.empty:
        return None

    ts = pd.to_datetime(d["timestamp"])
    keep = ((ts.dt.time >= config.MARKET_OPEN)
            & (ts.dt.time <= config.MARKET_CLOSE))
    d = d[keep]
    if d.empty:
        return None

    # today only — days_back can bring yesterday with it
    latest = pd.to_datetime(d["timestamp"]).dt.date.max()
    d = d[pd.to_datetime(d["timestamp"]).dt.date == latest]
    if d.empty:
        return None

    tp = (d["high"].astype(float) + d["low"].astype(float)
          + d["close"].astype(float)) / 3.0
    v = d["volume"].astype(float)
    tot = float(v.sum())
    if tot <= 0:
        return None

    return OptionVwap(
        strike=int(strike), side=side.upper(), security_id=sid, symbol=symbol,
        vwap=float((tp * v).sum() / tot), ltp=float(d["close"].iloc[-1]),
        volume=tot, bars=len(d),
        session_start=pd.to_datetime(d["timestamp"]).min().to_pydatetime(),
        interval=f"{interval}m")


def compare(strike: int, sides=("CE", "PE"), interval: str = "5") -> list:
    """Both legs of one strike. Two API calls, not two hundred."""
    out = []
    for s in sides:
        r = vwap(strike, s, interval)
        if r:
            out.append(r)
    return out


def watchlist(centre: int, span: int = 2, interval: str = "5",
              sides=("CE", "PE")) -> pd.DataFrame:
    """
    True VWAP for a small band of strikes around the centre.

    This replaces the sampled NSE approximation rather than sitting beside
    it. Two numbers claiming to be the same thing, one of them known to be
    wrong in three specific ways, is worse than having only the correct one
    — the wrong one gets read whenever it is closer to hand.

    The cost is why the band is small. `span=2` is five strikes, ten
    contracts, ten API calls. That is a deliberate ceiling: it is cheap
    enough to run on demand and expensive enough that nobody leaves it
    polling every three minutes by accident.

    Failures are reported per contract rather than dropping rows silently. A
    strike missing from this table because the fetch failed looks identical
    to a strike with no volume, and those mean opposite things.
    """
    step = getattr(config, "STRIKE_STEP", 50)
    strikes = [int(centre + i * step) for i in range(-span, span + 1)]
    rows = []
    for k in strikes:
        row = {"strike": k}
        for side in sides:
            try:
                r = vwap(k, side, interval)
            except Exception as exc:                        # noqa: BLE001
                row[f"{side} VWAP"] = None
                row[f"{side} note"] = f"error: {str(exc)[:40]}"
                continue
            if r is None:
                row[f"{side} VWAP"] = None
                row[f"{side} note"] = "no data / not resolved"
                continue
            row[f"{side} VWAP"] = round(r.vwap, 2)
            row[f"{side} LTP"] = round(r.ltp, 2)
            row[f"{side} vs VWAP"] = round(r.gap, 2)
            row[f"{side} vol"] = int(r.volume)
            row[f"{side} note"] = "THIN" if r.thin else ""
        rows.append(row)
    return pd.DataFrame(rows)
