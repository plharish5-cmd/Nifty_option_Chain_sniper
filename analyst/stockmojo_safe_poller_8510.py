"""Optional authenticated OI observation adapter, not a WAF/Cloudflare bypass.
No credential extraction, synthetic history or calls from an execution thread.
"""
from __future__ import annotations
import hashlib
import json
import logging
import math
import os
import random
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, time as clock_time
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import requests

ROOT = Path(__file__).resolve().parent.parent
RULES = ROOT / 'analyst/stockmojo_poller_rules_8510.json'
IST = ZoneInfo('Asia/Kolkata')


def quantity(value):
    if isinstance(value, bool):
        raise ValueError('Boolean is not OI')
    if isinstance(value, int):
        n = value
    else:
        x = float(value)
        if not math.isfinite(x) or not x.is_integer():
            raise ValueError('OI must be finite raw integer contracts; no inferred units')
        n = int(x)
    if n < 0 or n > 9223372036854775807:
        raise ValueError('OI outside supported range')
    return n


def source_time(value, session_date=None):
    """HH:MM exports require an explicit import date; that is not live proof."""
    verified = True
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        stamp = number / 1000 if number > 1e12 else number
        dt = datetime.fromtimestamp(stamp, IST)
    elif isinstance(value, str):
        text = value.strip()
        if len(text) in (5, 8) and text[2] == ':':
            if session_date is None:
                raise ValueError('Time-only export: source date is unverified')
            text = session_date + 'T' + text
            verified = False
        dt = datetime.fromisoformat(text.replace('Z', '+00:00'))
        dt = dt.replace(tzinfo=IST) if dt.tzinfo is None else dt.astimezone(IST)
    else:
        raise ValueError('Missing source timestamp')
    if not 2000 <= dt.year <= 2100:
        raise ValueError('Source timestamp outside supported range')
    return int(dt.timestamp()), verified


def normalize_payload(payload, session_date=None):
    """Accept exported named rows or the legacy t/co/po/f array contract.
    Array compatibility is not a claim that the undocumented endpoint is stable.
    Compute deltas only at exact observed 60/180-second spacing, never interpolate.
    """
    data = payload.get('data', payload) if isinstance(payload, dict) else payload
    if isinstance(data, dict) and all(k in data for k in ('t', 'co', 'po')):
        times, calls, puts = data['t'], data['co'], data['po']
        if not all(isinstance(v, list) for v in (times, calls, puts)) or not len(times) == len(calls) == len(puts):
            raise ValueError('Mismatched OI arrays')
        futures = data.get('f', [])
        if not isinstance(futures, list) or (futures and len(futures) != len(times)):
            raise ValueError('Mismatched futures array')
        records = [{'Time': t, 'Put OI Total': puts[i], 'Call OI Total': calls[i],
                    'Future LTP': futures[i] if futures else None} for i, t in enumerate(times)]
    elif isinstance(data, list):
        records = data
    else:
        raise ValueError('Unsupported response schema; capture the redacted response contract')
    by_ts = {}
    for item in records:
        if not isinstance(item, dict):
            raise ValueError('Expected OI row object')
        if item.get('Time') == 'Prev Day':
            continue  # Export baseline has no dated observation; never fabricate its date.
        stamp, verified = source_time(item.get('timestamp', item.get('Time')), session_date)
        put, call = quantity(item['Put OI Total']), quantity(item['Call OI Total'])
        future = item.get('Future LTP')
        if future is not None:
            future = float(future)
            if not math.isfinite(future) or future < 0:
                raise ValueError('Invalid futures price')
        row = dict(timestamp=stamp, ist_time=datetime.fromtimestamp(stamp, IST).isoformat(),
                   put_oi_total=put, call_oi_total=call, pe_ce_oi_total=put-call,
                   pcr_total=put/call if call else None, future_ltp=future,
                   source_date_verified=int(verified))
        if stamp in by_ts and by_ts[stamp] != row:
            raise ValueError('Conflicting duplicate timestamp')
        by_ts[stamp] = row
    if not by_ts:
        raise ValueError('Empty response')
    for stamp, row in sorted(by_ts.items()):
        for minutes in (1, 3):
            old = by_ts.get(stamp-minutes*60)
            delta = row['pe_ce_oi_total']-old['pe_ce_oi_total'] if old else None
            row['pe_ce_change_'+str(minutes)+'m'] = delta
            row['pe_ce_slope_'+str(minutes)+'m'] = delta/minutes if delta is not None else None
        delta = row['pe_ce_change_3m']
        row['sentiment'] = 'PE_CE_RISING' if delta is not None and delta > 0 else 'PE_CE_FALLING' if delta is not None and delta < 0 else 'FLAT' if delta == 0 else 'INSUFFICIENT_HISTORY'
    return [by_ts[t] for t in sorted(by_ts)]


def market_open(now, rules, holidays=()):
    dt = datetime.fromtimestamp(now, IST)
    return dt.weekday() < 5 and dt.date().isoformat() not in holidays and clock_time.fromisoformat(rules['market_open']) <= dt.time() < clock_time.fromisoformat(rules['market_close'])


def next_delay(rules, failures=0, retry_after=None, now=None, rng=None):
    rng = rng or random
    if failures:
        delay = min(rules['backoff_max_seconds'], rules['interval_seconds']*2**min(failures, 10))
        if retry_after:
            try:
                server_delay = float(retry_after)
            except ValueError:
                try:
                    server_delay = parsedate_to_datetime(retry_after).timestamp()-(time.time() if now is None else now)
                except (TypeError, ValueError, OverflowError):
                    server_delay = 0
            if math.isfinite(server_delay):
                delay = max(delay, server_delay)
        return max(0, delay)
    return rules['interval_seconds']+rng.uniform(rules['jitter_min_seconds'], rules['jitter_max_seconds'])


class StockMojoPoller:
    COLUMNS = ('timestamp', 'ist_time', 'put_oi_total', 'call_oi_total', 'pe_ce_oi_total', 'pcr_total', 'future_ltp', 'sentiment',
               'source_date_verified', 'pe_ce_change_1m', 'pe_ce_change_3m', 'pe_ce_slope_1m', 'pe_ce_slope_3m')

    def __init__(self, ledger_db, rules_path=RULES, log_path=None, holidays=(), transport=None, environ=None):
        self.rules = json.loads(Path(rules_path).read_text())
        self.ledger_db = Path(ledger_db)
        self.holidays = tuple(holidays)
        self.environ = os.environ if environ is None else environ
        self.transport = transport or requests.post
        self._cache = {}; self._status = 'NOT_STARTED'; self._halted = False
        self._failures = 0; self._stop = threading.Event(); self._thread = None
        self._poll_lock = threading.Lock(); self._last_logged = None
        try:
            self.request_parts()
            self._configured = True; self._configuration_status = 'READY'
        except ValueError as exc:
            self._configured = False; self._configuration_status = str(exc)
        self.logger = logging.getLogger('stockmojo-poller-'+str(id(self))); self.logger.propagate = False
        if log_path:
            handler = logging.FileHandler(log_path)
            handler.setFormatter(logging.Formatter('%(asctime)s StockMojo %(message)s'))
            self.logger.addHandler(handler); self.logger.setLevel(logging.WARNING)
        else:
            self.logger.addHandler(logging.NullHandler())
        self.ledger_db.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS stockmojo_pe_ce_live (
                scope TEXT NOT NULL,timestamp INTEGER NOT NULL,ist_time TEXT NOT NULL,
                put_oi_total INTEGER NOT NULL,call_oi_total INTEGER NOT NULL,pe_ce_oi_total INTEGER NOT NULL,
                pcr_total REAL,future_ltp REAL,sentiment TEXT,source_date_verified INTEGER NOT NULL,
                pe_ce_change_1m INTEGER,pe_ce_change_3m INTEGER,pe_ce_slope_1m REAL,pe_ce_slope_3m REAL,
                received_at REAL NOT NULL,PRIMARY KEY(scope,timestamp))''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.ledger_db, timeout=1)
        try:
            with db:
                yield db
        finally:
            db.close()

    def request_parts(self):
        j = self.environ.get('STOCKMOJO_JWT_J', '').strip(); t = self.environ.get('STOCKMOJO_TOKEN_T', '').strip()
        if not j or not t:
            raise ValueError('MISSING_CREDENTIALS')
        try:
            body = json.loads(self.environ.get('STOCKMOJO_REQUEST_JSON', ''))
        except (ValueError, TypeError):
            raise ValueError('REQUEST_BODY_REQUIRED') from None
        if not isinstance(body, dict) or not body or any(k.lower() in ('j','t','authorization','cookie','token') for k in body):
            raise ValueError('INVALID_REQUEST_BODY')
        # This adapter contributes only to the NIFTY score. Do not silently mix symbols.
        if type(body.get('symbol')) is not int or body['symbol'] != 1:
            raise ValueError('NIFTY_SYMBOL_CONTRACT_REQUIRED')
        if not body.get('expiry') or type(body.get('range')) is not int or body['range'] <= 0:
            raise ValueError('EXPIRY_AND_RANGE_REQUIRED')
        headers = {'origin':'https://stockmojo.in', 'content-type':'application/json', 'accept':'application/json', 'j':j, 't':t}
        scope = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(',',':')).encode()).hexdigest()
        return headers, body, scope

    def configured(self):
        try:
            self.request_parts()
            return True
        except ValueError:
            return False

    def _failure(self, status, halted=False):
        self._status = status; self._halted = halted; self._failures += 1
        if status != self._last_logged:
            self.logger.warning('%s; response body and credentials withheld', status)
            self._last_logged = status

    def persist(self, rows, scope, now):
        # A provider row cannot replace a newer observation with the same key.
        cols = self.COLUMNS
        with self.connect() as db:
            db.executemany('INSERT INTO stockmojo_pe_ce_live (scope,'+','.join(cols)+',received_at) VALUES ('+','.join('?' for _ in range(len(cols)+2))+') ON CONFLICT(scope,timestamp) DO UPDATE SET '+','.join(k+'=excluded.'+k for k in cols if k!='timestamp')+',received_at=excluded.received_at WHERE excluded.received_at>=stockmojo_pe_ce_live.received_at',
                [(scope,)+tuple(r[k] for k in cols)+(now,) for r in rows])
        row = max(rows, key=lambda r:r['timestamp'])
        self._cache = {**row, 'scope':scope, 'received_at':now}

    def poll_once(self, now=None):
        now = time.time() if now is None else now
        if not self.rules['enabled']:
            self._status = 'DISABLED'; return self.rules['non_market_wait_seconds']
        if self._halted:
            return self.rules['non_market_wait_seconds']
        if not market_open(now, self.rules, self.holidays):
            self._status = 'MARKET_CLOSED'; return self.rules['non_market_wait_seconds']
        if not self._poll_lock.acquire(blocking=False):
            return self.rules['non_market_wait_seconds']
        try:
            try:
                headers, body, scope = self.request_parts()
                self._configured = True; self._configuration_status = 'READY'
                if self._cache and self._cache.get('scope') != scope:
                    self._cache = {}
            except ValueError as exc:
                self._configured = False; self._configuration_status = str(exc)
                self._status = str(exc); return self.rules['non_market_wait_seconds']
            try:
                response = self.transport(self.rules['endpoint'], headers=headers, json=body, timeout=self.rules['request_timeout_seconds'], allow_redirects=False)
                code = response.status_code
                if code in (401,403):
                    self._failure('ACCESS_DENIED_'+str(code), halted=True)
                    return self.rules['non_market_wait_seconds']
                if code in (429,503):
                    self._failure('HTTP_'+str(code))
                    return next_delay(self.rules, self._failures, response.headers.get('Retry-After'), now)
                if code != 200:
                    self._failure('HTTP_'+str(code))
                    return next_delay(self.rules, self._failures)
                rows = normalize_payload(response.json())
                # Never accept a future, wrong-session or undated response as a live feed.
                latest = rows[-1]
                age = now-latest['timestamp']
                if not latest['source_date_verified'] or age < 0 or age > self.rules['max_source_age_seconds'] or datetime.fromtimestamp(latest['timestamp'],IST).date()!=datetime.fromtimestamp(now,IST).date():
                    raise ValueError('STALE_OR_UNVERIFIED_SOURCE')
                self.persist(rows, scope, now)
                self._status='OK';self._failures=0;self._last_logged=None
                return next_delay(self.rules)
            except (requests.RequestException, OSError, sqlite3.Error, ValueError, KeyError, TypeError, OverflowError) as exc:
                # No request/response repr: those may contain session credentials.
                self._failure('UNAVAILABLE_'+type(exc).__name__)
                return next_delay(self.rules, self._failures)
        finally:
            self._poll_lock.release()

    def snapshot(self, now=None):
        """Memory-only reader for telemetry and confluence; no SQL/network."""
        now = time.time() if now is None else now
        r = self._cache
        age = now-r['timestamp'] if r else None
        fresh = bool(r and self._status=='OK' and self.rules['enabled'] and market_open(now,self.rules,self.holidays)
                     and r['source_date_verified'] and age is not None and 0<=age<=self.rules['max_source_age_seconds'])
        flow = r.get('pe_ce_change_3m')
        bias = 'BULLISH' if fresh and flow is not None and flow>0 else 'BEARISH' if fresh and flow is not None and flow<0 else 'NEUTRAL'
        return {**r,'source':'STOCKMOJO_API','status':self._status if not r or fresh else 'STALE_OR_UNAVAILABLE',
                'available':fresh,'configured':self._configured,'enabled':self.rules['enabled'],
                'configuration_status':self._configuration_status,'use_for_pillar4':self.rules['use_for_pillar4'],'age_seconds':age,
                'timestamp_epoch':r.get('timestamp'),'bias':bias,
                'basis':'Exact observed 3m change in PE-CE, not total OI sign; positioning context, not writer intent.',
                'halted':self._halted,'poll_status':self._status,'max_age_seconds':self.rules['max_source_age_seconds']}

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        def run():
            while not self._stop.is_set():
                try:
                    delay=self.poll_once()
                except Exception:
                    self._failure('WORKER_FAILURE')
                    delay=self.rules['backoff_max_seconds']
                self._stop.wait(delay)
        self._thread=threading.Thread(target=run, name='stockmojo-oi-observer', daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
