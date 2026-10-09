"""
delta/adapters.py — where delta and footprint numbers come from.

    delta = aggressive BUY volume − aggressive SELL volume     (trades that lifted the offer − trades that hit the bid)
    positive → buyers attacking · negative → sellers attacking

WHY ADAPTERS: the NIFTY index (IDX_I 13) has no volume at all, and Dhan's v2 market feed does not say which side
of a trade was the aggressor. So the app cannot compute real delta from what it already receives. Three sources:

  footprint_feed   REAL. A trade aggregator (per 1m candle, per price level) that is complete and tested —
                   only the data SOURCE is a TODO (a vendor with aggressor-tagged NIFTY futures trades).
                   Until trades arrive it returns None → the UI says "Delta unavailable". It never invents data.
  csv              Upload delta per candle (and optionally per price level) exported from your footprint tool.
  mock             Deterministic fake numbers for development / tests. Every output is flagged mock=True and the
                   UI shows a MOCK banner. Never trade from it.

Interface (every adapter):
    status()                                     → {"name", "available", "mock", "message"}
    prepare(tf, t0, t1, instrument=None)         → optional bulk load before a run (CSV: one query, not one per candle)
    get_delta(candle, tf, instrument=None)       → float | None
    get_footprint_levels(candle, tf, instrument=None) → [Level] | None    (price, buy_vol, sell_vol, delta)
`instrument` = market.instruments.Instrument (None = the NIFTY index). Delta of a selected OPTION strike is read
against INDEX zones by delta/engine.py.
`candle` = {"ts": candle start epoch (09:15-aligned), "o","h","l","c"} — CLOSED candles only (the engine enforces it).
"""
from __future__ import annotations

import csv
import io
import math
import random
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .. import config, db
from ..timeutil import IST, TF_SECONDS, bucket_start, session_bounds, to_ist

SECS_TO_TF = {v: k for k, v in TF_SECONDS.items() if k != "1D"}


@dataclass
class Level:
    price: float
    buy_vol: float
    sell_vol: float
    delta: float = 0.0
    imb_buy: bool = False          # set by the engine (ratio test), not by adapters
    imb_sell: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _ikey(instrument) -> str:
    return getattr(instrument, "key", None) or "NIFTY"


class DeltaAdapter:
    name = "base"
    mock = False

    def status(self) -> Dict[str, Any]:
        return {"name": self.name, "available": False, "mock": self.mock, "message": "not implemented"}

    def prepare(self, tf: str, t0: int, t1: int, instrument=None) -> None:
        pass

    def get_delta(self, candle: Dict[str, float], tf: str, instrument=None) -> Optional[float]:
        return None

    def get_footprint_levels(self, candle: Dict[str, float], tf: str, instrument=None) -> Optional[List[Level]]:
        return None


class NullAdapter(DeltaAdapter):
    """'Unavailable' — the honest default: no source of aggressor-tagged volume is connected."""
    name = "none"

    def status(self) -> Dict[str, Any]:
        return {"name": self.name, "available": False, "mock": False,
                "message": "Delta unavailable — no source selected (the NIFTY index has no volume and Dhan does not "
                           "tag buy/sell aggressors). Confidence is structure only."}


# ── A · real footprint feed (aggregator ready, source TODO) ───────────────────
class FootprintFeedAdapter(DeltaAdapter):
    """Feed it aggressor-tagged trades with ingest_trade(); it builds 1m buckets × price levels in memory.

    TODO (data source — pick one, then call self.ingest_trade for every trade):
      * a footprint/order-flow vendor that streams NIFTY futures trades with aggressor side;
      * your own recorder of NIFTY FUT (NSE_FNO) trades with a tick-rule classifier — label that adapter
        'tick_rule' and keep `mock=False` but note in status() that it is an estimate;
      * NOT Dhan's v2 feed as-is: its packets carry LTQ / volume / total bid-ask quantity, not the aggressor.
    """
    name = "footprint_feed"

    def __init__(self, level_step: float = 0.05) -> None:
        self.level_step = level_step
        self._by: Dict[str, Dict[int, Dict[float, List[float]]]] = defaultdict(
            lambda: defaultdict(lambda: defaultdict(lambda: [0.0, 0.0])))
        self.trades = 0

    def ingest_trade(self, ts: float, price: float, qty: float, aggressor: str, instrument=None) -> None:
        """aggressor: 'buy' (lifted the offer) | 'sell' (hit the bid). Unknown aggressor → do not ingest.
        instrument: the option (or index future) the trade printed on; None = NIFTY."""
        if aggressor not in ("buy", "sell") or qty <= 0:
            return
        b = bucket_start(ts, "1m")
        p = round(round(price / self.level_step) * self.level_step, 2)
        self._by[_ikey(instrument)][b][p][0 if aggressor == "buy" else 1] += qty
        self.trades += 1

    def status(self) -> Dict[str, Any]:
        if not self.trades:
            return {"name": self.name, "available": False, "mock": False,
                    "message": "Delta unavailable — no aggressor-tagged trades connected yet (see FootprintFeedAdapter TODO)."}
        return {"name": self.name, "available": True, "mock": False, "message": f"{self.trades} trades aggregated"}

    def _minutes(self, candle: Dict[str, float], tf: str, instrument=None) -> List[int]:
        step = TF_SECONDS[tf]
        t = int(candle["ts"])
        bars = self._by.get(_ikey(instrument), {})
        return [m for m in range(t, t + step, 60) if m in bars]

    def get_delta(self, candle, tf, instrument=None):
        mins = self._minutes(candle, tf, instrument)
        if not mins or len(mins) < config.rule("delta", "min_coverage", 0.8) * TF_SECONDS[tf] / 60:
            return None
        bars = self._by[_ikey(instrument)]
        return float(sum(v[0] - v[1] for m in mins for v in bars[m].values()))

    def get_footprint_levels(self, candle, tf, instrument=None):
        mins = self._minutes(candle, tf, instrument)
        if not mins:
            return None
        bars = self._by[_ikey(instrument)]
        agg: Dict[float, List[float]] = defaultdict(lambda: [0.0, 0.0])
        for m in mins:
            for p, (bv, sv) in bars[m].items():
                agg[p][0] += bv
                agg[p][1] += sv
        return [Level(p, bv, sv, bv - sv) for p, (bv, sv) in sorted(agg.items())]


# ── B · CSV import ───────────────────────────────────────────────────────────
class CsvError(ValueError):
    pass


def parse_ts(raw: str) -> int:
    """Epoch s / ms, 'YYYY-MM-DD HH:MM[:SS]' (taken as IST), or ISO-8601 with an offset ('…+05:30', '…Z')."""
    s = str(raw).strip()
    if not s:
        raise CsvError("empty timestamp")
    try:
        x = float(s)
        if x > 1e12:
            x /= 1000.0
        if x < 1e9:
            raise CsvError(f"timestamp {s} is not epoch seconds/ms")
        return int(x)
    except ValueError:
        pass
    s2 = s.replace("Z", "+00:00").replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S%z", "%Y-%m-%d %H:%M%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                "%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M"):
        try:
            d = datetime.strptime(s2, fmt)
            if d.tzinfo is None:
                d = d.replace(tzinfo=IST)            # policy: naive times are IST (NSE clock)
            return int(d.timestamp())
        except ValueError:
            continue
    raise CsvError(f"cannot read timestamp '{s}'")


def _num(row: Dict[str, str], k: str, line: int) -> float:
    try:
        v = float(str(row[k]).replace(",", "").strip())
    except (KeyError, ValueError):
        raise CsvError(f"line {line}: '{k}' is not a number ({row.get(k)!r})")
    if math.isnan(v) or math.isinf(v):
        raise CsvError(f"line {line}: '{k}' is NaN/inf")
    return v


def align(ts: int, bar_seconds: int, ts_is: str, misaligned: str) -> Tuple[Optional[int], str]:
    """App policy: candles are identified by their START, on a grid anchored at 09:15 IST.
    Returns (aligned start ts | None, note) — note is '' (exact), 'floored', 'outside session', 'misaligned'."""
    if ts_is == "end":
        ts -= bar_seconds
    tf = SECS_TO_TF.get(bar_seconds)
    if tf is None:
        raise CsvError(f"bar_seconds must be one of {sorted(SECS_TO_TF)}")
    o, c = session_bounds(to_ist(ts).date())
    if not (o <= ts < c):
        return None, "outside session"
    b = bucket_start(ts, tf)
    if b == ts:
        return b, ""
    if misaligned == "floor":
        return b, "floored"
    return None, "misaligned"


def parse_csv(text: str, bar_seconds: int = 60, ts_is: str = "start", misaligned: str = "floor") -> Dict[str, Any]:
    """Validate + align. Two shapes (header row required, names case-insensitive):
         bars:    timestamp, delta                    | timestamp, buy_vol, sell_vol
         levels:  timestamp, price, buy_vol, sell_vol (bar delta = Σ level deltas when no bar rows exist)
    Returns {"kind", "bars": {ts: (buy, sell, delta)}, "levels": {ts: {price: (buy, sell)}}, "report"}."""
    if ts_is not in ("start", "end") or misaligned not in ("floor", "reject"):
        raise CsvError("ts_is must be start|end and misaligned floor|reject")
    rdr = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    if not rdr.fieldnames:
        raise CsvError("no header row")
    cols = {c.strip().lower(): c for c in rdr.fieldnames if c}
    alias = {"time": "timestamp", "datetime": "timestamp", "ts": "timestamp", "buy": "buy_vol", "sell": "sell_vol",
             "ask_vol": "buy_vol", "bid_vol": "sell_vol", "buy_volume": "buy_vol", "sell_volume": "sell_vol"}
    for a, t in alias.items():
        if a in cols and t not in cols:
            cols[t] = cols[a]
    if "timestamp" not in cols:
        raise CsvError(f"missing 'timestamp' column (got {list(cols)})")
    has_bs = "buy_vol" in cols and "sell_vol" in cols
    if "price" in cols:
        if not has_bs:
            raise CsvError("a levels file needs price, buy_vol and sell_vol")
        kind = "levels"
    elif "delta" in cols or has_bs:
        kind = "bars"
    else:
        raise CsvError("need 'delta' or 'buy_vol'+'sell_vol' (plus 'price' for per-level footprint)")

    bars: Dict[int, Tuple[Optional[float], Optional[float], float]] = {}
    levels: Dict[int, Dict[float, List[float]]] = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0]))
    rep = {"rows": 0, "accepted": 0, "floored": 0, "outside_session": 0, "misaligned": 0, "duplicates": 0,
           "errors": [], "first": None, "last": None}
    for n, raw in enumerate(rdr, start=2):
        rep["rows"] += 1
        row = {k: raw.get(v) for k, v in cols.items()}
        try:
            t0 = parse_ts(row["timestamp"])
            t, note = align(t0, bar_seconds, ts_is, misaligned)
            if t is None:
                rep["outside_session" if note == "outside session" else "misaligned"] += 1
                continue
            if note == "floored":
                rep["floored"] += 1
            if kind == "levels":
                p = round(_num(row, "price", n), 2)
                bv, sv = _num(row, "buy_vol", n), _num(row, "sell_vol", n)
                if bv < 0 or sv < 0:
                    raise CsvError(f"line {n}: negative volume")
                levels[t][p][0] += bv
                levels[t][p][1] += sv
            else:
                if has_bs:
                    bv, sv = _num(row, "buy_vol", n), _num(row, "sell_vol", n)
                    d = bv - sv
                else:
                    bv = sv = None
                    d = _num(row, "delta", n)
                if t in bars:
                    rep["duplicates"] += 1                   # same candle twice → last row wins
                bars[t] = (bv, sv, d)
            rep["accepted"] += 1
            rep["first"] = t if rep["first"] is None else min(rep["first"], t)
            rep["last"] = t if rep["last"] is None else max(rep["last"], t)
        except CsvError as e:
            if len(rep["errors"]) < 20:
                rep["errors"].append(str(e))
    if not rep["accepted"]:
        raise CsvError("no usable rows: " + "; ".join(rep["errors"][:5] or ["all rows outside the session or misaligned"]))
    if kind == "levels":
        for t, lv in levels.items():
            b = sum(v[0] for v in lv.values())
            s = sum(v[1] for v in lv.values())
            bars.setdefault(t, (b, s, b - s))
    return {"kind": kind, "bar_seconds": bar_seconds, "bars": bars,
            "levels": {t: {p: tuple(v) for p, v in lv.items()} for t, lv in levels.items()}, "report": rep}


def store_csv(parsed: Dict[str, Any], instrument=None) -> Dict[str, Any]:
    """instrument: which contract the CSV describes (None = NIFTY index; an Instrument for an option strike)."""
    secs = int(parsed["bar_seconds"])
    ik = _ikey(instrument)
    db.execmany("INSERT OR REPLACE INTO delta_bars (inst, ts, secs, buy, sell, delta, src) VALUES (?,?,?,?,?,?,?)",
                [(ik, t, secs, bv, sv, d, "csv") for t, (bv, sv, d) in parsed["bars"].items()])
    if parsed["levels"]:
        ts_list = list(parsed["levels"])
        for t in ts_list:                                     # a re-upload of a candle replaces its levels
            db.execute("DELETE FROM footprint_levels WHERE inst=? AND ts=?", (ik, t))
        db.execmany("INSERT OR REPLACE INTO footprint_levels (inst, ts, price, buy, sell) VALUES (?,?,?,?,?)",
                    [(ik, t, p, bv, sv) for t, lv in parsed["levels"].items() for p, (bv, sv) in lv.items()])
    r = dict(parsed["report"])
    r["instrument"] = ik
    r["kind"], r["bar_seconds"] = parsed["kind"], secs
    r["stored_bars"], r["stored_level_candles"] = len(parsed["bars"]), len(parsed["levels"])
    return r


class CsvAdapter(DeltaAdapter):
    """Reads delta_bars / footprint_levels. A 5m candle's delta = Σ rows whose start falls inside it, accepted only
    when the rows cover ≥ min_coverage of the candle (a 5m candle with 2 of 5 minutes is 'unavailable')."""
    name = "csv"

    def __init__(self) -> None:
        self._rows: Dict[int, Tuple[int, float]] = {}
        self._lv: Dict[int, Dict[float, List[float]]] = {}
        self._loaded: Optional[Tuple[str, int, int]] = None

    def status(self):
        r = db.query("SELECT COUNT(*) n, MIN(ts) a, MAX(ts) b, COUNT(DISTINCT inst) k FROM delta_bars WHERE src='csv'")[0]
        if not r["n"]:
            return {"name": self.name, "available": False, "mock": False,
                    "message": "Delta unavailable — no CSV uploaded yet (Delta / Footprint tab → Upload CSV)."}
        a, b = to_ist(r["a"]).strftime("%d %b %H:%M"), to_ist(r["b"]).strftime("%d %b %H:%M")
        return {"name": self.name, "available": True, "mock": False,
                "message": f"{r['n']} CSV rows for {r['k']} instrument(s) · {a} → {b} IST"}

    def prepare(self, tf, t0, t1, instrument=None):
        ik = _ikey(instrument)
        t1 = int(t1) + TF_SECONDS[tf]
        self._rows = {int(r["ts"]): (int(r["secs"]), float(r["delta"])) for r in
                      db.query("SELECT ts, secs, delta FROM delta_bars WHERE inst=? AND ts>=? AND ts<?", (ik, int(t0), t1))}
        lv: Dict[int, Dict[float, List[float]]] = defaultdict(dict)
        for r in db.query("SELECT ts, price, buy, sell FROM footprint_levels WHERE inst=? AND ts>=? AND ts<?",
                          (ik, int(t0), t1)):
            lv[int(r["ts"])][float(r["price"])] = [float(r["buy"]), float(r["sell"])]
        self._lv = dict(lv)
        self._loaded = (ik, int(t0), t1)

    def _inside(self, candle, tf, instrument=None) -> List[int]:
        ik = _ikey(instrument)
        if self._loaded is None or self._loaded[0] != ik or not (self._loaded[1] <= candle["ts"] < self._loaded[2]):
            self.prepare(tf, int(candle["ts"]), int(candle["ts"]), instrument)
        t, step = int(candle["ts"]), TF_SECONDS[tf]
        return [k for k in self._rows if t <= k < t + step]

    def get_delta(self, candle, tf, instrument=None):
        ks = self._inside(candle, tf, instrument)
        if not ks:
            return None
        cover = sum(min(self._rows[k][0], TF_SECONDS[tf]) for k in ks) / TF_SECONDS[tf]
        if cover < float(config.rule("delta", "min_coverage", 0.8)):
            return None
        return float(sum(self._rows[k][1] for k in ks))

    def get_footprint_levels(self, candle, tf, instrument=None):
        self._inside(candle, tf, instrument)
        t, step = int(candle["ts"]), TF_SECONDS[tf]
        ks = [k for k in self._lv if t <= k < t + step]
        if not ks:
            return None
        agg: Dict[float, List[float]] = defaultdict(lambda: [0.0, 0.0])
        for k in ks:
            for p, (bv, sv) in self._lv[k].items():
                agg[p][0] += bv
                agg[p][1] += sv
        return [Level(p, bv, sv, bv - sv) for p, (bv, sv) in sorted(agg.items())]


# ── C · deterministic mock (dev / tests only) ─────────────────────────────────
class MockAdapter(DeltaAdapter):
    """Same candle → same numbers, every run. Mostly follows the candle body, sometimes fights it (so absorption
    and flips appear in the UI). Flagged mock everywhere."""
    name = "mock"
    mock = True

    def __init__(self, level_step: float = 5.0, with_levels: bool = True) -> None:
        self.level_step, self.with_levels = level_step, with_levels

    def status(self):
        return {"name": self.name, "available": True, "mock": True,
                "message": "MOCK delta — deterministic fake numbers for development. Never trade from it."}

    def _rng(self, candle, tf, instrument=None) -> random.Random:
        ik = _ikey(instrument)
        return random.Random(f"{int(candle['ts'])}|{tf}" + ("" if ik == "NIFTY" else f"|{ik}"))

    def get_delta(self, candle, tf, instrument=None):
        r = self._rng(candle, tf, instrument)
        rng = max(candle["h"] - candle["l"], 0.05)
        bias = (candle["c"] - candle["o"]) / rng                       # -1 … +1
        vol = 800.0 * TF_SECONDS[tf] / 60 * (0.6 + r.random())
        if r.random() < 0.12:
            bias = -bias * 1.6 if abs(bias) > 0.2 else (1.0 if r.random() < 0.5 else -1.0)   # aggression that fails
        return float(round(vol * (0.75 * bias + 0.25 * (r.random() * 2 - 1))))

    def get_footprint_levels(self, candle, tf, instrument=None):
        if not self.with_levels:
            return None
        r = self._rng(candle, tf, instrument)
        d = self.get_delta(candle, tf, instrument) or 0.0
        st = self.level_step
        lo = math.floor(candle["l"] / st) * st
        prices = []
        p = lo
        while p <= candle["h"] + 1e-9:
            prices.append(round(p, 2))
            p += st
        n = len(prices)
        tot = 800.0 * TF_SECONDS[tf] / 60
        out = []
        for k, px in enumerate(prices):
            w = 1.0 - abs((k + 0.5) / n - 0.5)                         # more volume mid-candle
            v = tot * w / n
            tilt = d / tot * (0.5 + k / max(n - 1, 1)) if d > 0 else d / tot * (1.5 - k / max(n - 1, 1))
            share = min(0.95, max(0.05, 0.5 + 0.5 * tilt + 0.1 * (r.random() - 0.5)))
            out.append(Level(px, round(v * share), round(v * (1 - share)), round(v * share) - round(v * (1 - share))))
        return out


class FutQuoteRuleAdapter(CsvAdapter):
    """ESTIMATED delta / footprint recorded live from the NIFTY future (delta/recorder.py). Index only — strike
    flow (options) has no recorder, so it stays unavailable there."""
    name = "fut_quote_rule"
    estimated = True
    _KEY = type("K", (), {"key": "FUTQR"})()

    def status(self):
        r = db.query("SELECT COUNT(*) n, MIN(ts) a, MAX(ts) b FROM delta_bars WHERE inst='FUTQR'")[0]
        if not r["n"]:
            return {"name": self.name, "available": False, "mock": False, "estimated": True,
                    "message": "Estimated delta (NIFTY FUT, quote rule) — nothing recorded yet. It records live from "
                               "the futures feed during market hours; there is no history before the first session."}
        a, b = to_ist(r["a"]).strftime("%d %b %H:%M"), to_ist(r["b"]).strftime("%d %b %H:%M")
        return {"name": self.name, "available": True, "mock": False, "estimated": True,
                "message": f"ESTIMATED delta — quote/tick rule on NIFTY FUT Full packets (snapshots, not every trade) · "
                           f"{r['n']} minutes recorded · {a} → {b} IST"}

    def prepare(self, tf, t0, t1, instrument=None):
        if instrument is None or instrument is self._KEY:
            super().prepare(tf, t0, t1, self._KEY)

    def get_delta(self, candle, tf, instrument=None):
        return None if instrument is not None else super().get_delta(candle, tf, self._KEY)

    def get_footprint_levels(self, candle, tf, instrument=None):
        return None if instrument is not None else super().get_footprint_levels(candle, tf, self._KEY)


_REGISTRY = {"fut_quote_rule": FutQuoteRuleAdapter, "none": NullAdapter, "footprint_feed": FootprintFeedAdapter, "csv": CsvAdapter, "mock": MockAdapter}
LABELS = {"fut_quote_rule": "Estimated (FUT quote rule)", "none": "Unavailable", "footprint_feed": "Real-feed", "csv": "CSV", "mock": "Mock"}


def make(name: str) -> DeltaAdapter:
    if name not in _REGISTRY:
        raise ValueError(f"unknown delta adapter '{name}' — use one of {sorted(_REGISTRY)}")
    return _REGISTRY[name]()


def names() -> List[str]:
    return ["fut_quote_rule", "none", "csv", "footprint_feed", "mock"]
