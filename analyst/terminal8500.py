"""Local terminal: instrument validation, explicit manual orders and durable paper ledger.

No automatic real-money strategy execution. Broker mutations have no automatic retries.
Credentials are server environment variables, never part of telemetry or browser storage.
"""
import csv
import io
import json
import logging
import math
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, date
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
CFG = json.loads(Path(__file__).with_name('terminal8500_config.json').read_text())
IST = ZoneInfo('Asia/Kolkata')
LOG = logging.getLogger('terminal8500')

class Problem(Exception):
    def __init__(self, message, status=400):
        self.status = status
        super().__init__(message)

def positive(value):
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise Problem('Expected a positive finite number')
    if not math.isfinite(v) or v <= 0:
        raise Problem('Expected a positive finite number')
    return v

class Broker:
    def request(self, path, body=None):
        token, client = os.getenv('DHAN_ACCESS_TOKEN'), os.getenv('DHAN_CLIENT_ID')
        if not token or not client:
            raise Problem('DHAN_ACCESS_TOKEN / DHAN_CLIENT_ID missing in daemon environment', 503)
        req = urllib.request.Request('https://api.dhan.co/v2' + path,
            data=None if body is None else json.dumps(body).encode(),
            headers={'access-token': token, 'client-id': client, 'Content-Type': 'application/json'},
            method='GET' if body is None else 'POST')
        try:
            with urllib.request.urlopen(req, timeout=CFG['request_timeout_seconds']) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            # Return structured broker details, never request headers or credentials.
            try:
                error = json.loads(e.read())
            except (ValueError, UnicodeDecodeError):
                error = {'message': 'Non-JSON broker error'}
            raise Problem(json.dumps({'broker_http': e.code, 'broker_error': error}), 502)
        except (urllib.error.URLError, TimeoutError, OSError):
            raise Problem('Broker connection failed'+('; order outcome may be unknown. Do not resubmit blindly.' if path in ('/orders','/super/orders') and body is not None else ''), 502)

    def quote(self, sid):
        r = self.request('/marketfeed/ltp', {'NSE_FNO': [int(sid)]})
        try:
            return positive(r['data']['NSE_FNO'][str(sid)]['last_price'])
        except (KeyError, TypeError):
            raise Problem('No executable option quote returned', 503)

class Instruments:
    def __init__(self, directory):
        self.path = directory / 'instrument_master.csv'
        self.lock = threading.Lock()

    def refresh(self):
        with self.lock:
            try:
                with urllib.request.urlopen(CFG['instrument_url'], timeout=CFG['request_timeout_seconds']) as r:
                    raw = r.read().decode('utf-8-sig')
            except (urllib.error.URLError, TimeoutError, OSError):
                raise Problem('Instrument download failed; existing cache was retained', 503)
            rows = self.parse(raw)
            if not rows:
                raise Problem('Instrument download contained no NIFTY options', 503)
            tmp = self.path.with_suffix('.tmp')
            tmp.write_text(raw); tmp.replace(self.path)
            return len(rows)

    @staticmethod
    def parse(raw):
        out = []
        for r in csv.DictReader(io.StringIO(raw)):
            def get(a, b): return str(r.get(a, r.get(b, '')) or '').strip()
            if get('EXCH_ID', 'SEM_EXM_EXCH_ID') != 'NSE' or get('SEGMENT', 'SEM_SEGMENT') != 'D': continue
            if get('INSTRUMENT', 'SEM_INSTRUMENT_NAME') != 'OPTIDX': continue
            symbol = get('DISPLAY_NAME', 'SEM_CUSTOM_SYMBOL')
            underlying = get('UNDERLYING_SYMBOL', 'SM_SYMBOL_NAME')
            if underlying != 'NIFTY' and not symbol.upper().startswith('NIFTY '): continue
            try:
                expiry = date.fromisoformat(get('SM_EXPIRY_DATE', 'SEM_EXPIRY_DATE')[:10]).isoformat()
                lot_decimal = Decimal(get('LOT_SIZE', 'SEM_LOT_UNITS'))
                lot = int(lot_decimal)
                if lot_decimal != lot: continue
                sid = get('SECURITY_ID', 'SEM_SMST_SECURITY_ID')
                side = get('OPTION_TYPE', 'SEM_OPTION_TYPE')
                strike = float(get('STRIKE_PRICE', 'SEM_STRIKE_PRICE'))
                if not sid.isdigit() or lot <= 0 or side not in ('CE', 'PE'): continue
                out.append(dict(securityId=sid, symbol=symbol, expiry=expiry, strike=strike,
                                optionType=side, lotSize=lot, tickSize=float(get('TICK_SIZE','SEM_TICK_SIZE') or 0) or None, exchangeSegment='NSE_FNO'))
            except (ValueError, ArithmeticError):
                continue
        return out

    def rows(self):
        if not self.path.exists(): raise Problem('Refresh the instrument master first', 503)
        if time.time() - self.path.stat().st_mtime > CFG['max_master_age_hours'] * 3600:
            raise Problem('Instrument master is stale; refresh before selecting contracts', 503)
        return [r for r in self.parse(self.path.read_text()) if r['expiry'] >= datetime.now(IST).date().isoformat()]

    def resolve(self, body):
        rows = self.rows()
        if body.get('securityId'):
            match = [r for r in rows if r['securityId'] == str(body['securityId'])]
        else:
            match = [r for r in rows if r['expiry'] == body.get('expiry') and
                     r['strike'] == positive(body.get('strike')) and r['optionType'] == body.get('optionType')]
        if len(match) != 1: raise Problem('Contract lookup must match exactly one unexpired NIFTY option')
        return match[0]

def order_payload(contract, body):
    lots = body.get('lots', 1)
    if isinstance(lots, bool) or not isinstance(lots, int) or lots < 1: raise Problem('lots must be a positive integer')
    side = body.get('transactionType', 'BUY')
    if side not in ('BUY', 'SELL'): raise Problem('transactionType must be BUY or SELL')
    product = body.get('productType', 'INTRADAY')
    if product not in ('INTRADAY', 'MARGIN'): raise Problem('NIFTY options require INTRADAY or MARGIN, not CNC')
    correlation = body.get('correlationId', '')
    if not correlation or len(correlation) > 30 or any(not(c.isascii() and (c.isalnum() or c in '_-')) for c in correlation):
        raise Problem('Supply a unique correlationId: 1–30 ASCII letters, numbers, underscores or hyphens')
    return dict(dhanClientId=os.getenv('DHAN_CLIENT_ID', ''), correlationId=correlation,
                transactionType=side, exchangeSegment='NSE_FNO', productType=product,
                orderType='MARKET', validity='DAY', securityId=contract['securityId'],
                quantity=lots*contract['lotSize'], disclosedQuantity=0, price=0, triggerPrice=0,
                afterMarketOrder=False)

class Engine:
    def __init__(self, root=ROOT, broker=None):
        self.root = root
        self.data = root / 'data' / 'terminal8500'
        self.data.mkdir(parents=True, exist_ok=True)
        self.db = self.data / 'ledger.sqlite3'
        self.broker = broker or Broker()
        self.master = Instruments(self.data)
        self.lock = threading.RLock()
        self.enabled = CFG['paper_enabled_on_start']
        self.status = 'Paper scanner paused'
        self.last_spot = None
        with self.connect() as c:
            c.execute('CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, value TEXT NOT NULL)')
            c.execute('CREATE TABLE IF NOT EXISTS intents (id TEXT PRIMARY KEY, state TEXT NOT NULL, response TEXT)')
        self.quit = threading.Event()

    def connect(self): return sqlite3.connect(self.db, timeout=10)

    def paper(self):
        with self.connect() as c:
            return [json.loads(r[0]) for r in c.execute('SELECT value FROM events ORDER BY rowid DESC')]

    def save(self, t):
        with self.connect() as c:
            c.execute('INSERT INTO events VALUES (?,?) ON CONFLICT(id) DO UPDATE SET value=excluded.value',
                      (t['id'], json.dumps(t)))

    def account(self):
        funds = self.broker.request('/fundlimit')
        orders = self.broker.request('/orders')
        return dict(status='PREFLIGHT_OK', auth_status='Dhan authenticated', funds=funds,
                    margin_text=str(funds.get('availabelBalance', funds.get('availableBalance', 'Unavailable'))),
                    orderbook_text=f'{len(orders)} orders returned' if isinstance(orders, list) else 'Unexpected orderbook response')

    def submit(self, body):
        if os.getenv('ENABLE_DHAN_LIVE_ORDERS') != '1' or body.get('confirmLive') is not True:
            raise Problem('Live order routing disabled. Server flag and explicit confirmation are both required.', 403)
        contract = self.master.resolve(body)
        payload = order_payload(contract, body)
        if body.get('lots',1)>CFG['max_live_lots']:raise Problem('Configured live lot limit exceeded')
        now=datetime.now(IST)
        if now.weekday()>=5 or not CFG['market_open']<=now.strftime('%H:%M')<CFG['market_close']:
            raise Problem('Live manual route accepts regular-session orders only',403)
        if not payload['dhanClientId']: raise Problem('DHAN_CLIENT_ID missing', 503)
        # A committed intent precedes network submission, so restart/repeated clicks never duplicate it.
        with self.lock, self.connect() as c:
            try:
                c.execute('INSERT INTO intents VALUES (?,?,?)', (payload['correlationId'], 'SUBMITTING', None))
            except sqlite3.IntegrityError:
                raise Problem('Correlation ID already used. Reconcile the existing order; no retry sent.', 409)
        try:
            response = self.broker.request('/orders', payload)
        except Problem:
            with self.connect() as c:
                c.execute('UPDATE intents SET state=? WHERE id=?', ('UNKNOWN_RECONCILE', payload['correlationId']))
            raise
        with self.connect() as c:
            c.execute('UPDATE intents SET state=?,response=? WHERE id=?',
                      ('BROKER_RESPONSE', json.dumps(response), payload['correlationId']))
        return dict(status='BROKER_RESPONSE', response=response,
                    note='Submission is not a fill. Verify order status and protection in broker terminal.')

    def snapshot(self):
        source = self.root / 'ict_smc_app/backend/data/ict_smc.sqlite3'
        if not source.exists(): raise Problem('Local NIFTY candle database missing', 503)
        with sqlite3.connect(f'file:{source}?mode=ro', uri=True) as c:
            rows = c.execute("SELECT ts,o,h,l,c FROM candles_1m WHERE sec_id='13' ORDER BY ts DESC LIMIT ?", (CFG['source_history_minutes'],)).fetchall()[::-1]
        if not rows: raise Problem('No NIFTY candles recorded', 503)
        now = time.time()
        if not 0 <= now - rows[-1][0] <= CFG['max_spot_age_seconds']:
            raise Problem('NIFTY source stale: scanner waiting for fresh recorded minutes', 503)
        groups = {}
        opening=sum(int(x)*w for x,w in zip(CFG['market_open'].split(':'),(60,1)))
        closing=sum(int(x)*w for x,w in zip(CFG['market_close'].split(':'),(60,1)))
        interval=CFG['candle_minutes']
        for ts, op, hi, lo, close in rows:
            dt = datetime.fromtimestamp(ts, IST)
            if not all(math.isfinite(float(v)) and float(v)>0 for v in (op,hi,lo,close)): continue
            if hi < max(op,close) or lo > min(op,close): continue
            minute = dt.hour*60+dt.minute
            if opening <= minute < closing and ts+60 <= now:
                key = (dt.date().isoformat(), (minute-opening)//interval)
                groups.setdefault(key, []).append((ts,close))
        bars = []
        for key, g in sorted(groups.items()):
            if len(g)==interval and len({x[0] for x in g})==interval and g[-1][0]-g[0][0]==(interval-1)*60:
                bars.append((g[0][0]+interval*60, float(g[-1][1])))
        return rows[-1], bars

    def scan_once(self):
        now = datetime.now(IST)
        if now.weekday() >= 5 or not CFG['market_open'] <= now.strftime('%H:%M') < CFG['market_close']:
            self.status = 'Market closed; no paper fills generated'
            return
        tick, bars = self.snapshot()
        spot = positive(tick[4]); self.last_spot=spot
        # Existing paper positions remain managed when new-entry scanning is paused.
        for t in self.paper():
            if t['status'] != 'OPEN': continue
            quote = self.broker.quote(t['securityId'])
            move = (spot-t['entrySpot'])*t['side']
            t.update(currentPremium=quote, currSpot=spot, grossPnl=(quote-t['entryPremium'])*t['quantity'],
                     quoteReceivedAt=now.isoformat())
            if t['day'] != now.date().isoformat() or now.strftime('%H:%M') >= CFG['force_exit'] or move<=-CFG['paper_spot_stop_points'] or move>=CFG['paper_spot_target_points']:
                t.update(status='CLOSED', exitPremium=quote, exitTime=now.isoformat(),
                         reason='SESSION_EXIT' if t['day'] != now.date().isoformat() or now.strftime('%H:%M') >= CFG['force_exit'] else 'SPOT_TARGET' if move>0 else 'SPOT_STOP')
            self.save(t)
        if not self.enabled:
            self.status = 'Paper entries paused; open positions monitored'
            return
        if len(bars)<CFG['warmup_bars']: raise Problem('Insufficient complete 15-minute bars for EMA warmup', 503)
        if not 0 <= time.time()-bars[-1][0] <= CFG['signal_max_age_seconds']:
            self.status='Waiting for a newly closed 15-minute candle'
            return
        fast=slow=bars[0][1];signs=[]
        for _,cl in bars:
            fast += 2/(CFG['ema_fast']+1)*(cl-fast)
            slow += 2/(CFG['ema_slow']+1)*(cl-slow)
            signs.append(1 if fast>slow else -1 if fast<slow else signs[-1] if signs else 0)
        if signs[-1]==signs[-2] or signs[-1]==0:
            self.status='Fresh source; no completed EMA4/8 crossover'
            return
        side=signs[-1]; event=f'EMA48-{bars[-1][0]}-{side}'
        if any(t['id']==event for t in self.paper()): self.status='Crossover already processed';return
        if now.strftime('%H:%M')>CFG['last_entry']: self.status='Last entry time passed';return
        if sum(t['status']=='OPEN' for t in self.paper())>=CFG['max_paper_positions']:
            self.status='Paper position limit reached';return
        contracts=[r for r in self.master.rows() if r['optionType']==('CE' if side==1 else 'PE')]
        if not contracts: raise Problem('No eligible option contract in instrument master',503)
        expiry=min(r['expiry'] for r in contracts)
        contract=min((r for r in contracts if r['expiry']==expiry),key=lambda r:abs(r['strike']-spot))
        premium=self.broker.quote(contract['securityId'])
        self.save(dict(id=event,day=now.date().isoformat(),entryTime=now.isoformat(),
                       setup='15m EMA4/8 crossover paper',side=side,entrySpot=spot,currSpot=spot,
                       entryPremium=premium,currentPremium=premium,grossPnl=0,status='OPEN',
                       quantity=contract['lotSize']*CFG['paper_lots'],source='local recorded 1m → closed15m + Dhan LTP',
                       signalClose=bars[-1][0],**contract))
        self.status='Paper option filled at sampled Dhan LTP; bid/ask slippage and fees excluded'

    def loop(self):
        while not self.quit.is_set():
            try:
                with self.lock: self.scan_once()
            except Exception as e:
                self.status=str(e) if isinstance(e,Problem) else 'Scanner failure; check daemon log'
                if not isinstance(e,Problem): LOG.exception('Paper scanner failed')
            self.quit.wait(CFG['poll_seconds'])

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): LOG.info('%s %s', self.command, urlsplit(self.path).path)
    def send_json(self, data, status=200):
        raw=json.dumps(data,allow_nan=False).encode()
        self.send_response(status);self.send_header('Content-Type','application/json')
        self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(raw)))
        self.end_headers();self.wfile.write(raw)

    def do_GET(self): self.handle_request(False)
    def do_POST(self): self.handle_request(True)

    def handle_request(self, post):
        e=self.server.engine
        try:
            host=self.headers.get('Host','')
            allowed={f'127.0.0.1:{self.server.server_port}',f'localhost:{self.server.server_port}'}
            if host not in allowed: raise Problem('Invalid Host',403)
            if post:
                origin=self.headers.get('Origin')
                if origin and origin not in {'http://'+h for h in allowed}:raise Problem('Cross-origin request denied',403)
                if self.headers.get('Content-Type','').split(';')[0]!='application/json':raise Problem('Use application/json',415)
                size=int(self.headers.get('Content-Length','0'))
                if not 0<size<=CFG['max_body_bytes']:raise Problem('Invalid request size',413)
                body=json.loads(self.rfile.read(size))
                if not isinstance(body,dict):raise Problem('JSON object required')
            path=urlsplit(self.path).path;query=parse_qs(urlsplit(self.path).query)
            if hasattr(e,'route') and e.route(self,post,body if post else {},query,path):return
            if not post and path in ('/','/workspace','/Algorithmic_Trading_Workspace.html'):
                raw=(e.root/'Algorithmic_Trading_Workspace.html').read_bytes()
                self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8')
                self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw);return
            if not post and path=='/api/algo/status':
                return self.send_json(dict(mode='AUTOMATIC_LIVE' if getattr(getattr(e,'execution',None),'armed',False) else 'PAPER',token_verified=False,credentials_present=bool(os.getenv('DHAN_ACCESS_TOKEN')),
                    last_spot=e.last_spot,paper_enabled=e.enabled,scanner_status=e.status,
                    live_manual_enabled=os.getenv('ENABLE_DHAN_LIVE_ORDERS')=='1',automatic_live_enabled=bool(getattr(getattr(e,'execution',None),'armed',False))))
            if not post and path=='/api/algo/paper': return self.send_json(e.paper())
            if not post and path=='/api/algo/instruments': return self.send_json(e.master.rows())
            if not post and path=='/api/algo/account': return self.send_json(e.account())
            if post and path=='/api/algo/instruments/refresh':return self.send_json({'contracts':e.master.refresh()})
            if post and path=='/api/algo/preview':
                contract=e.master.resolve(body);return self.send_json({'contract':contract,'payload':order_payload(contract,body),'submitted':False})
            if post and path=='/api/algo/orders':return self.send_json(e.submit(body))
            if not post and path=='/api/algo/orders':return self.send_json(e.broker.request('/orders'))
            if not post and path=='/api/algo/reconcile':
                cid=query.get('correlationId',[''])[0]
                if not cid or any(not(c.isascii() and(c.isalnum() or c in '_-')) for c in cid):raise Problem('Invalid correlationId')
                return self.send_json(e.broker.request('/orders/external/'+cid))
            if post and path=='/api/algo/config':
                if body.get('enable_live_real_money') is True:raise Problem('Automatic real-money scanner not enabled by this repair',403)
                if 'enable_parallel_paper_trade' in body:
                    if not isinstance(body['enable_parallel_paper_trade'],bool):raise Problem('Expected boolean')
                    e.enabled=body['enable_parallel_paper_trade']
                return self.send_json({'status':'UPDATED','paper_enabled':e.enabled})
            if post and path=='/api/algo/telegram_test':
                token=body.get('bot_token') or os.getenv('TELEGRAM_BOT_TOKEN','')
                chat=body.get('chat_id') or os.getenv('TELEGRAM_CHAT_ID','')
                if not token or not chat:raise Problem('Telegram token/chat not configured',503)
                payload=json.dumps({'chat_id':chat,'text':str(body.get('text','Terminal alert'))}).encode()
                req=urllib.request.Request('https://api.telegram.org/bot'+token+'/sendMessage',data=payload,headers={'Content-Type':'application/json'},method='POST')
                try:
                    with urllib.request.urlopen(req,timeout=CFG['request_timeout_seconds']) as response: result=json.load(response)
                except (urllib.error.URLError,TimeoutError,OSError,ValueError):
                    raise Problem('Telegram dispatch failed',502)
                if result.get('ok') is not True:raise Problem('Telegram rejected dispatch',502)
                return self.send_json({'status':'TELEGRAM_DISPATCHED'})
            if post and path=='/api/algo/preflight':return self.send_json(e.account())
            if post and path=='/api/algo/token':raise Problem('Set credentials in the daemon environment; browser token storage removed',403)
            if path.startswith('/api/algo/pe_ce_'):
                import pe_ce_bridge
                if not post and path=='/api/algo/pe_ce_live':return self.send_json(pe_ce_bridge.fetch_pe_ce_3tier_hierarchy())
                if not post and path=='/api/algo/pe_ce_gate':return self.send_json(pe_ce_bridge.evaluate_pe_ce_parameter_gate(query.get('dir',['LONG'])[0]))
                if post and path=='/api/algo/pe_ce_config':
                    pe_ce_bridge.BRIDGE_CONFIG.update(body);return self.send_json({'status':'PE_CE_CONFIG_UPDATED','bridge_config':pe_ce_bridge.BRIDGE_CONFIG})
            raise Problem('Endpoint not found',404)
        except Problem as ex:self.send_json({'error':str(ex)},ex.status)
        except (ValueError,TypeError):self.send_json({'error':'Invalid request values'},400)
        except Exception:
            LOG.exception('Request failed');self.send_json({'error':'Internal error; check daemon log'},500)

def main():
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    engine=Engine()
    server=ThreadingHTTPServer(('127.0.0.1',CFG['port']),Handler);server.engine=engine
    thread=threading.Thread(target=engine.loop,daemon=True);thread.start()
    LOG.info('Terminal on http://127.0.0.1:%s; paper entries %s; automatic live OFF',CFG['port'],'enabled' if engine.enabled else 'paused')
    try:server.serve_forever()
    finally:engine.quit.set();server.server_close();thread.join(timeout=CFG['request_timeout_seconds']*2)
