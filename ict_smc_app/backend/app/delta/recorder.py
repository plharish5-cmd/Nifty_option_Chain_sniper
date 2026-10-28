"""
delta/recorder.py — ESTIMATED delta and footprint from the live NIFTY future (Dhan Full packets).

Dhan does not tag the aggressor of a trade. What a Full packet does carry: last price, cumulative day volume and the
best bid / ask. Between two packets, the volume that printed (Δ cumulative volume) is classified like this —
the classic Lee-Ready idea:
    price ≥ the PREVIOUS packet's ask   → buyers lifted the offer      (quote rule)
    price ≤ the PREVIOUS packet's bid   → sellers hit the bid          (quote rule)
    in between                           → up-tick = buy, down-tick = sell, unchanged = same as the last tick
                                                                         (tick rule)
    nothing to compare yet               → unclassified (counted, never guessed)

HONEST LIMITS — shown on the status line and labelled "Estimated" everywhere:
  * Dhan streams SNAPSHOTS, not every trade: several trades between two packets are lumped into one print.
  * History starts the minute the recorder starts. There is no backfill of delta (Dhan has none).
  * Prices are mapped to the INDEX by subtracting the live basis (future − spot) at that moment, so the
    footprint levels sit on the NIFTY chart, in `delta.fut_level_step`-point rows.

It also builds the future's own 1-minute OHLCV from the same packets, so volume is ready the moment a minute
closes; the REST candles fetched a few seconds later overwrite it with Dhan's official numbers.

Stored in delta_bars / footprint_levels with inst = "FUTQR" (src "fut_quote_rule") — read by FutQuoteRuleAdapter.
"""
from __future__ import annotations

import logging
import json
import hashlib
import math
import time
from pathlib import Path
from functools import lru_cache
from collections import defaultdict
from typing import Any, Callable, Dict, List, Optional

from .. import config, db
from ..market import futures as fut_mod
from ..timeutil import bucket_start, ist_date, session_bounds

log = logging.getLogger("recorder")

INST = "FUTQR"
SRC = "fut_quote_rule"


class FutRecorder:
    def __init__(self, spot: Callable[[], Optional[float]]) -> None:
        self.audit = ProvenanceRecorder()
        self.spot = spot
        self.reset()
        self.packets = 0
        self.counts = {"quote": 0.0, "tick": 0.0, "unclassified": 0.0}
        self.first_ts: Optional[int] = None

    def reset(self) -> None:
        self.prev_vol: Optional[float] = None
        self.prev_bid: Optional[float] = None
        self.prev_ask: Optional[float] = None
        self.last_px: Optional[float] = None
        self.last_side: Optional[str] = None
        self.basis: Optional[float] = None
        self.day: Optional[str] = None
        self.min: Dict[int, Dict[str, Any]] = {}             # minute → {"lv": {px: [buy, sell]}, buy, sell, ohlcv}

    # ── packets ──────────────────────────────────────────────────────────
    def on_packet(self, p: Dict[str, Any], t: float) -> Optional[str]:
        """Returns the side it classified ('buy' / 'sell' / None)."""
        self.audit.on_packet(p, t)
        self.packets += 1
        vol, px = p.get("volume"), p.get("ltp")
        if vol is None or not px:
            return None
        d = ist_date(t).isoformat()
        if d != self.day:
            self.reset()
            self.day = d
        if self.prev_vol is not None and vol < self.prev_vol:
            log.warning("Ignoring decreasing intraday cumulative volume for contract %s", p.get("sid", "unknown"))
            return None
        side = None
        m = bucket_start(t, "1m")
        rec = self.min.setdefault(m, {"lv": defaultdict(lambda: [0.0, 0.0]), "buy": 0.0, "sell": 0.0, "unc": 0.0,
                                      "o": px, "h": px, "l": px, "c": px, "v": 0.0, "contract": str(p.get("sid", ""))})
        rec["h"], rec["l"], rec["c"] = max(rec["h"], px), min(rec["l"], px), px
        if rec.get("contract") != str(p.get("sid", "")):
            rec["contract"] = ""  # mixed-contract minute is never certified
        if p.get("oi"):
            rec["oi"] = float(p["oi"])
        if self.prev_vol is not None and vol > self.prev_vol:
            dv = float(vol - self.prev_vol)
            rec["v"] += dv
            how = "tick"
            if self.prev_ask is not None and px >= self.prev_ask:
                side, how = "buy", "quote"
            elif self.prev_bid is not None and px <= self.prev_bid:
                side, how = "sell", "quote"
            elif self.last_px is not None and px > self.last_px:
                side = "buy"
            elif self.last_px is not None and px < self.last_px:
                side = "sell"
            else:
                side = self.last_side
            spot = self.spot()
            if side is None or not spot:
                self.counts["unclassified"] += dv
                rec["unc"] += dv
            else:
                self.counts[how] += dv
                step = float(config.rule("delta", "fut_level_step", 5.0))
                raw = px - spot                               # live basis, smoothed so tick noise between the
                if abs(raw) < 0.05 * spot:                    # two feeds doesn't smear the levels
                    self.basis = raw if self.basis is None else self.basis + 0.05 * (raw - self.basis)
                lvl = round(((px - self.basis) // step) * step, 2) if self.basis is not None else None
                if lvl is None:
                    self.counts["unclassified"] += dv
                    rec["unc"] += dv
                else:
                    rec["lv"][lvl][0 if side == "buy" else 1] += dv
                    rec["buy" if side == "buy" else "sell"] += dv
                    if self.first_ts is None:
                        self.first_ts = m
        self.prev_vol = float(vol)
        if px != self.last_px and side is not None:
            self.last_side = side
        if self.last_px is None or px != self.last_px:
            self.last_px = px
        if p.get("bid") and p.get("ask"):
            self.prev_bid, self.prev_ask = float(p["bid"]), float(p["ask"])
        return side

    # ── minute close ─────────────────────────────────────────────────────
    def close_minute(self, m: int) -> Optional[Dict[str, Any]]:
        """Persist minute m (and anything older still in memory). Returns minute m's record summary."""
        self.audit.close_minute(m)
        out = None
        for k in sorted(x for x in self.min if x <= m):
            r = self.min.pop(k)
            if r["buy"] + r["sell"] > 0:
                db.execute("INSERT OR REPLACE INTO delta_bars (inst, ts, secs, buy, sell, delta, src) VALUES (?,?,?,?,?,?,?)",
                           (INST, k, 60, r["buy"], r["sell"], r["buy"] - r["sell"], SRC))
                db.execute("DELETE FROM footprint_levels WHERE inst=? AND ts=?", (INST, k))
                db.execmany("INSERT OR REPLACE INTO footprint_levels (inst, ts, price, buy, sell) VALUES (?,?,?,?,?)",
                            [(INST, k, p, bv, sv) for p, (bv, sv) in r["lv"].items()])
            if r["v"] > 0:                                   # provisional FUT candle; REST overwrites it later
                have = db.query("SELECT 1 FROM candles_1m WHERE sec_id=? AND ts=?", (fut_mod.SEC, k))
                if not have:
                    db.upsert_candles(fut_mod.SEC, [{"ts": k, "o": r["o"], "h": r["h"], "l": r["l"], "c": r["c"], "v": r["v"]}])
            if r.get("oi") and r.get("contract"):
                db.execute("INSERT OR REPLACE INTO fut_oi_samples (sec,contract,ts,source,oi,observed_at) VALUES (?,?,?,?,?,?)",
                           (fut_mod.SEC,r["contract"],k,"feed",r["oi"],int(time.time())))
            if r.get("oi"):                                  # provisional OI; REST (oi=True) replaces it
                db.execute("INSERT OR IGNORE INTO fut_oi (ts, oi, src) VALUES (?,?,?)", (k, r["oi"], "feed"))
            if k == m:
                out = {"ts": k, "buy": r["buy"], "sell": r["sell"], "unclassified": r["unc"], "v": r["v"]}
        return out

    def status(self) -> Dict[str, Any]:
        tot = sum(self.counts.values())
        share = {k: round(100 * v / tot, 1) for k, v in self.counts.items()} if tot else {}
        return {"packets": self.packets, "classified_share": share, "since": self.first_ts, "provenance": self.audit.status()}


def stored_span() -> Dict[str, Any]:
    r = db.query("SELECT COUNT(*) n, MIN(ts) a, MAX(ts) b FROM delta_bars WHERE inst=?", (INST,))[0]
    return {"minutes": r["n"] or 0, "first": r["a"], "last": r["b"]}


@lru_cache(maxsize=1)
def provenance_config() -> Dict[str, Any]:
    path = Path(__file__).resolve().parents[4] / 'analyst' / 'analyst_config.json'
    return json.loads(path.read_text())['cvd_research']


def quality_gate(row: Dict[str, Any], cfg: Optional[Dict[str, Any]] = None) -> List[str]:
    """Shared research gate. Fail closed; old FUTQR rows cannot acquire provenance by inference."""
    c = cfg or provenance_config()
    errors = []
    required = ('session', 'contract', 'schema_version', 'ts', 'source', 'o', 'h', 'l', 'c', 'v',
                'quote_v', 'tick_v', 'unclassified_v', 'stale_v', 'gap_v',
                'first_event', 'last_event', 'max_gap_s', 'packets', 'delta', 'bad_timestamp')
    if any(k not in row for k in required):
        return ['missing_provenance']
    if not row['contract'] or row['source'] != c['source'] or row['schema_version'] != c['schema_version']:
        errors.append('unbound_contract_or_source')
    if row.get('conflict'):
        errors.append('conflicting_observation')
    numeric = ('ts', 'o', 'h', 'l', 'c', 'v', 'quote_v', 'tick_v', 'unclassified_v',
               'stale_v', 'gap_v', 'first_event', 'last_event', 'max_gap_s', 'packets', 'delta')
    if not all(isinstance(row[k], (float, int)) and math.isfinite(row[k]) for k in numeric):
        return errors + ['invalid_numeric']
    if row['bad_timestamp']:
        errors.append('timestamp_discontinuity')
    ts = row['ts']
    bounds = tuple(map(int, session_bounds(ist_date(ts))))
    if row['session'] != ist_date(ts).isoformat() or ts != bucket_start(ts, '1m') or not bounds[0] <= ts < bounds[1]:
        errors.append('invalid_session')
    if row['v'] <= 0 or min(row[k] for k in ('quote_v','tick_v','unclassified_v','stale_v','gap_v')) < 0:
        return errors + ['invalid_volume']
    v = row['v']
    if not math.isclose(row['quote_v']+row['tick_v']+row['unclassified_v'], v, rel_tol=1e-9, abs_tol=1e-6):
        errors.append('assignment_accounting')
    if abs(row['delta']) > row['quote_v']+row['tick_v']+1e-6:
        errors.append('invalid_delta')
    if row['h'] < max(row['o'],row['c']) or row['l'] > min(row['o'],row['c']) or row['l'] <= 0:
        errors.append('invalid_price')
    if not ts <= row['first_event'] <= row['last_event'] < ts+60:
        errors.append('event_outside_minute')
    if row['packets'] < c['min_packets'] or row['first_event']-ts > c['boundary_tolerance_s'] or ts+60-row['last_event'] > c['boundary_tolerance_s']:
        errors.append('partial_minute')
    for field, limit, label in [('unclassified_v','max_unclassified_share','unclassified'),('stale_v','max_stale_share','stale_quote'),('gap_v','max_gap_share','packet_gap')]:
        if row[field]/v > c[limit]:
            errors.append(label)
    if row['max_gap_s'] > c['packet_gap_s']:
        errors.append('packet_gap')
    return sorted(set(errors))


def assignment_quality(row: Dict[str, Any]) -> Dict[str, Any]:
    v = row.get('v', 0)
    return {'denominator': 'observed positive cumulative-volume increments',
            'assignment_pct': {k: round(100*row.get(k,0)/v,2) if v else None
                               for k in ('quote_v','tick_v','unclassified_v')},
            'overlapping_diagnostics_pct': {k: round(100*row.get(k,0)/v,2) if v else None
                                            for k in ('at_mid_v','stale_v','gap_v')},
            'note': 'Assignment shares, not a classification error rate; diagnostics overlap.'}


class ProvenanceRecorder:
    """Shadow-only, contract-bound snapshot observations; never changes legacy signals.

    Minute payloads are first-write immutable. A nonidentical retry sets a conflict
    flag rather than replacing history. Raw packets and credentials are not persisted.
    OHLC and delta come from the SAME contract snapshots, independent of FUT backfills.
    """
    def __init__(self, cfg=None):
        self.cfg = cfg or provenance_config()
        self.previous = {}
        self.minutes = {}
        self.finalized = set()
        self.written = 0
        self.rejected = 0

    def on_packet(self, packet, event_ts):
        sid = str(packet.get('sid', ''))
        received = packet.get('_received_at', time.time())
        price, vol = packet.get('ltp'), packet.get('volume')
        if not sid or not all(isinstance(x,(int,float)) and math.isfinite(x) for x in (event_ts,received,price,vol)) or price <= 0 or vol < 0:
            self.rejected += 1
            return
        day = ist_date(event_ts).isoformat()
        start, end = session_bounds(ist_date(event_ts))
        if not start <= event_ts < end:
            return
        minute = bucket_start(event_ts,'1m')
        key = (day,sid,minute)
        if key in self.finalized:
            # Already-finalized time may not silently change the research tape.
            db.execute('UPDATE cvd_observations SET conflict=1 WHERE session=? AND contract=? AND ts=? AND source=?',
                       (day,sid,minute,self.cfg['source']))
            self.rejected += 1
            return
        r = self.minutes.setdefault(key,dict(session=day,contract=sid,ts=minute,source=self.cfg['source'],
                    schema_version=self.cfg['schema_version'],o=price,h=price,l=price,c=price,v=0.,delta=0.,
                    quote_v=0.,tick_v=0.,unclassified_v=0.,at_mid_v=0.,stale_v=0.,gap_v=0.,
                    first_event=event_ts,last_event=event_ts,first_received=received,last_received=received,max_gap_s=0.,packets=0,bad_timestamp=False))
        prev = self.previous.get(sid)
        if prev and prev['day'] != day:
            prev = None
        if prev and (event_ts < prev['event'] or received < prev['received']):
            r['bad_timestamp'] = True
            self.rejected += 1
            return
        r.update(h=max(r['h'],price),l=min(r['l'],price),c=price,last_event=event_ts,last_received=received,packets=r['packets']+1)
        if abs(received-event_ts) > self.cfg['max_exchange_lag_s']:
            r['bad_timestamp'] = True
        side = None
        if prev:
            gap = received-prev['received']
            r['max_gap_s'] = max(r['max_gap_s'],gap)
            dv = vol-prev['volume']
            if dv < 0:
                r['bad_timestamp'] = True
            elif dv > 0:
                r['v'] += dv
                if gap > self.cfg['packet_gap_s']:
                    r['gap_v'] += dv
                quote = prev.get('quote')
                stale = quote is None or received-prev.get('quote_received',received) > self.cfg['quote_max_age_s']
                if stale:
                    r['stale_v'] += dv
                if quote and price == (quote[0]+quote[1])/2:
                    r['at_mid_v'] += dv
                how = 'tick_v'
                if not stale and quote and quote[0] < quote[1] and price >= quote[1]:
                    side,how = 1,'quote_v'
                elif not stale and quote and quote[0] < quote[1] and price <= quote[0]:
                    side,how = -1,'quote_v'
                elif price != prev['price']:
                    side = 1 if price > prev['price'] else -1
                else:
                    side = prev.get('side')
                r[how if side else 'unclassified_v'] += dv
                r['delta'] += (side or 0)*dv
        bid,ask = packet.get('bid'),packet.get('ask')
        quote = (bid,ask) if all(isinstance(x,(int,float)) and math.isfinite(x) and x>0 for x in (bid,ask)) and bid<=ask else None
        self.previous[sid] = dict(day=day,event=event_ts,received=received,volume=vol,price=price,
                                  side=side or (prev.get('side') if prev else None),
                                  quote=quote or (prev.get('quote') if prev else None),
                                  quote_received=received if quote else prev.get('quote_received',received) if prev else received)

    def close_minute(self, minute):
        for key in sorted(k for k in self.minutes if k[2] <= minute):
            r = self.minutes.pop(key)
            payload = json.dumps(r,sort_keys=True,separators=(',',':'))
            digest = hashlib.sha256(payload.encode()).hexdigest()
            args = (r['session'],r['contract'],r['ts'],r['source'])
            db.execute('INSERT OR IGNORE INTO cvd_observations (session,contract,ts,source,payload,digest) VALUES (?,?,?,?,?,?)',args+(payload,digest))
            db.execute('UPDATE cvd_observations SET conflict=1 WHERE session=? AND contract=? AND ts=? AND source=? AND digest<>?',args+(digest,))
            self.finalized.add(key)
            self.written += 1

    def status(self):
        return dict(source=self.cfg['source'],estimated=True,minutes_written=self.written,
                    rejected_packets=self.rejected,note='Shadow research only; historical rows are not backfilled.')


def provenance_status():
    rows = db.query('SELECT session,contract,ts,payload,conflict FROM cvd_observations ORDER BY ts DESC LIMIT 375')
    valid=0;reasons=defaultdict(int);totals=defaultdict(float)
    for record in rows:
        r = dict(json.loads(record['payload']),conflict=record['conflict'])
        errors=quality_gate(r)
        valid += not errors
        for error in errors:
            reasons[error]+=1
        for k in ('v','quote_v','tick_v','unclassified_v','at_mid_v','stale_v','gap_v'):
            totals[k]+=r.get(k,0)
    return dict(estimated=True,shadow_only=True,minutes=len(rows),eligible_minutes=valid,
                contracts=sorted({r['contract'] for r in rows}),gate_rejections=dict(reasons),
                quality=assignment_quality(totals),message='Historical FUTQR rows are unverified; only new contract-bound observations qualify.')
