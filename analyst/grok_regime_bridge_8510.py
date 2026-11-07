"""Observational regime bridge. Never imports execution or broker code.
All extraction, network and persistence run on the bridge worker, never a tick thread.
"""
from __future__ import annotations
import copy
import json
import logging
import math
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
RULES = ROOT / 'analyst/grok_bridge_rules_8510.json'
log = logging.getLogger(__name__)
REGIMES = ('BULLISH_TREND', 'BEARISH_TREND', 'CONSOLIDATION_CHOP', 'OVEREXTENDED_CLIMAX')
DECISIONS = ('ALLOW_4MA_CONTINUATION', 'WAIT_FOR_8EMA_TOUCH', 'AVOID_CHOP')
BIASES = ('CE_ONLY', 'PE_ONLY', 'NEUTRAL')
SCHEMA = {'type': 'object', 'additionalProperties': False, 'properties': {
    'regime': {'type': 'string', 'enum': list(REGIMES)},
    'far_from_8ema_decision': {'type': 'string', 'enum': list(DECISIONS)},
    'recommended_bias': {'type': 'string', 'enum': list(BIASES)},
    'confidence': {'type': 'integer', 'minimum': 0, 'maximum': 100},
    'reason': {'type': 'string'}},
    'required': ['regime', 'far_from_8ema_decision', 'recommended_bias', 'confidence', 'reason']}

def validate(payload):
    if not isinstance(payload, dict):
        raise ValueError('Classification must be an object')
    for key, values in (('regime', REGIMES), ('far_from_8ema_decision', DECISIONS), ('recommended_bias', BIASES)):
        if payload.get(key) not in values:
            raise ValueError('Invalid '+key)
    confidence = payload.get('confidence')
    if type(confidence) is not int or not 0 <= confidence <= 100:
        raise ValueError('confidence must be integer 0..100')
    reason = payload.get('reason')
    if not isinstance(reason, str) or not reason.strip() or len(reason.split()) > 25 or '\n' in reason or len(reason)>240:
        raise ValueError('reason must be one line, at most 25 words / 240 characters')
    return {k: payload[k] for k in SCHEMA['required']}

def build_8600_to_8510_feature_packet(market_db, volume_engine=None, now=None, rules=None):
    """Recorded NIFTY closed minutes only; read-only DB; no synthetic history.
    Bucket-edge observations do not establish intrabar touches. OI is not writer intent.
    """
    import pandas as pd
    r = rules or json.loads(RULES.read_text())
    now = time.time() if now is None else now
    packet = {'data_status': 'NO_RECORDED_DATA', 'spot_price': None,
              'ict_smc_desk_summary': [], 'dhan_oi_synergy': None, 'observed_at': now}
    try:
        with sqlite3.connect(Path(market_db).resolve().as_uri()+'?mode=ro', uri=True, timeout=1) as db:
            rows = db.execute("SELECT ts,o,h,l,c FROM candles_1m WHERE sec_id='13' AND ts+60<=? ORDER BY ts DESC LIMIT 3000", (now,)).fetchall()
            if rows:
                frame = pd.DataFrame(rows[::-1], columns=['ts','o','h','l','c'])
                frame.index = pd.to_datetime(frame.ts,unit='s',utc=True).dt.tz_convert('Asia/Kolkata')
                frame = frame.between_time('09:15','15:29')
                bars = frame.resample('15min',origin='start_day',offset='15min').agg({'ts':'count','o':'first','h':'max','l':'min','c':'last'})
                bars = bars[(bars.ts==15) & (bars.index.astype('int64')/1e9+900<=now)]
                if len(bars)>=r['min_15m_bars']:
                    close=bars.c; e4=close.ewm(span=r['ema_fast'],adjust=False).mean();e8=close.ewm(span=r['ema_anchor'],adjust=False).mean()
                    tr=pd.concat([bars.h-bars.l,(bars.h-close.shift()).abs(),(bars.l-close.shift()).abs()],axis=1).max(axis=1)
                    atr=float(tr.rolling(14).mean().iloc[-1]); spot=float(frame.c.iloc[-1]); a=float(e4.iloc[-1]);b=float(e8.iloc[-1])
                    signs=(e4-e8).tail(13).apply(lambda x: 1 if x>0 else -1 if x<0 else 0)
                    flips=int(((signs*signs.shift())<0).sum())
                    ts=float(frame.ts.iloc[-1]); bar_ts=float(bars.index[-1].timestamp())+900
                    fresh = now-ts-60 <=r['source_max_age_seconds'] and now-bar_ts <=900+r['source_max_age_seconds']
                    packet.update(data_status='FRESH' if fresh else 'STALE',source_ts=ts,closed_15m_ts=bar_ts,
                        spot_price=spot,ema_4_15m=a,ema_8_15m=b,atr_14_15m=atr,
                        dist_from_15m_8ema_pts=round(spot-b,2),dist_from_15m_4ma_pts=round(spot-a,2),
                        sep_atr=round(abs(a-b)/max(atr,.01),4),flips_12bars=flips)
            # Same expiry and snapshot on both legs; never combine unsynchronised snapshots.
            snap=db.execute('SELECT expiry,MAX(ts) AS t FROM chain_snap WHERE ts<=? GROUP BY expiry ORDER BY expiry LIMIT 1',(now,)).fetchone()
            if snap:
                sums=db.execute('SELECT SUM(ce_oi),SUM(pe_oi) FROM chain_snap WHERE expiry=? AND ts=?',snap).fetchone()
                prev=db.execute('SELECT MAX(ts) FROM chain_snap WHERE expiry=? AND ts<?',(snap[0],snap[1])).fetchone()[0]
                old=db.execute('SELECT SUM(ce_oi),SUM(pe_oi) FROM chain_snap WHERE expiry=? AND ts=?',(snap[0],prev)).fetchone() if prev else (None,None)
                packet['dhan_oi_synergy']={'expiry':snap[0],'ts':snap[1],'age_seconds':now-snap[1],'ce_oi':sums[0],'pe_oi':sums[1],
                    'ce_change':sums[0]-old[0] if sums[0] is not None and old[0] is not None else None,
                    'pe_change':sums[1]-old[1] if sums[1] is not None and old[1] is not None else None,
                    'note':'Change between recorded snapshots; OI does not establish writer intent.'}
            packet['ict_smc_desk_summary']=[dict(zip(('ts','kind','side','title'),row)) for row in db.execute(
                "SELECT ts,kind,side,title FROM alerts WHERE ts<=? AND ts>=? AND (UPPER(kind) LIKE '%BOS%' OR UPPER(kind) LIKE '%FVG%' OR UPPER(kind) LIKE '%SWEEP%' OR UPPER(kind) LIKE '%ORDER_BLOCK%') ORDER BY ts DESC LIMIT 8",(now,now-900))]
    except (sqlite3.Error, OSError, ValueError, KeyError) as exc:
        packet['extract_error']=type(exc).__name__
        log.warning('Grok read-only extraction unavailable: %s',type(exc).__name__)
    if volume_engine is not None:
        try:
            volume=volume_engine.get_latest_vsr()
            packet.update(slot_rvol_median=volume.get('slot_rvol_median'),bar_accel_ratio=volume.get('bar_accel_ratio'))
        except (sqlite3.Error, OSError, ValueError, KeyError) as exc:
            log.warning('Grok volume unavailable: %s',type(exc).__name__)
    return packet

def local_classify(p,r):
    required=('sep_atr','flips_12bars','dist_from_15m_8ema_pts','dist_from_15m_4ma_pts','ema_4_15m','ema_8_15m')
    good=all(type(p.get(k)) in (float,int) and math.isfinite(p[k]) for k in required)
    regime='CONSOLIDATION_CHOP';decision='WAIT_FOR_8EMA_TOUCH';bias='NEUTRAL';reason='Missing or stale inputs; wait for recorded closed candles.'
    if good and p.get('data_status')=='FRESH':
        sep=p['sep_atr'];dist=abs(p['dist_from_15m_8ema_pts']);v=p.get('slot_rvol_median')
        regime='BULLISH_TREND' if p['ema_4_15m']>p['ema_8_15m'] else 'BEARISH_TREND'
        bias='CE_ONLY' if regime=='BULLISH_TREND' else 'PE_ONLY'
        reason='Wait for the 8 EMA pocket; positioning is context only.'
        if sep<r['chop_atr_ceiling'] or p['flips_12bars']>=r['max_flips']:
            regime='CONSOLIDATION_CHOP';decision='AVOID_CHOP';bias='NEUTRAL';reason='Tight EMA spread or repeated crosses; avoid chasing.'
        elif dist>r['far_from_8ema_threshold_pts'] and sep>r['goldilocks_atr_ceiling'] and v is not None and v>=r['min_slot_rvol_for_4ma_continuation'] and abs(p['dist_from_15m_4ma_pts'])<=r['near_4ema_pts']:
            decision='ALLOW_4MA_CONTINUATION';reason='Near 4 EMA with broad separation and relative volume; advisory continuation hypothesis.'
        elif dist>r['overextended_from_8ema_pts'] and (v is None or v<r['weak_volume']):
            regime='OVEREXTENDED_CLIMAX';decision='WAIT_FOR_8EMA_TOUCH';reason='Extended from 8 EMA without strong relative volume; wait.'
    return dict(regime=regime,far_from_8ema_decision=decision,recommended_bias=bias,confidence=0,reason=reason,source='LOCAL_DETERMINISTIC_FALLBACK',confidence_note='Rule output; confidence is not calibrated and is displayed as unscored.')

def classify_8600_signal(feature_packet, force_local=False, rules=None):
    r=rules or json.loads(RULES.read_text());p=feature_packet
    fallback=local_classify(p,r)
    key=os.environ.get('XAI_API_KEY')
    if force_local or not r['enabled'] or r['provider_mode']=='LOCAL_DETERMINISTIC' or not key or p.get('data_status')!='FRESH':
        return fallback
    body={'model':r['grok_model'],'messages':[{'role':'system','content':'Classify this observational packet. Never authorize orders. Missing data means WAIT. Reason <=25 words. Return the required JSON.'},{'role':'user','content':json.dumps(p,allow_nan=False)}],
          'response_format':{'type':'json_schema','json_schema':{'name':'regime','strict':True,'schema':SCHEMA}}}
    req=urllib.request.Request(r['xai_api_base'],data=json.dumps(body).encode(),headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'})
    try:
        with urllib.request.urlopen(req,timeout=r['request_timeout_sec']) as response:
            raw=json.loads(response.read(65536))
        result=validate(json.loads(raw['choices'][0]['message']['content']))
        return {**result,'source':'GROK_LIVE_API','confidence_note':'Model self-report; not measured probability.'}
    except (urllib.error.URLError,TimeoutError,json.JSONDecodeError,KeyError,ValueError,IndexError,TypeError,OSError) as exc:
        log.warning('Grok API unavailable (%s); using local rules',type(exc).__name__)
        return {**fallback,'provider_error':type(exc).__name__}

class GrokRegimeBridge:
    def __init__(self, ledger_db, market_db, rules_path=RULES, cache_path=None, volume_engine=None):
        self.ledger_db=Path(ledger_db);self.market_db=Path(market_db);self.rules_path=Path(rules_path)
        self.cache_path=Path(cache_path) if cache_path else None
        self.rules=json.loads(self.rules_path.read_text());self.volume_engine=volume_engine
        self._cache={};self._worker_lock=threading.Lock();self._last_source=None
        self.ledger_db.parent.mkdir(parents=True,exist_ok=True)
        with self.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS grok_signal_classifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,ts REAL NOT NULL,ist_time TEXT NOT NULL,spot_price REAL,ema_4_15m REAL,ema_8_15m REAL,
            dist_8ema_pts REAL,sep_atr REAL,slot_rvol_median REAL,ict_smc_bias TEXT,regime TEXT NOT NULL,far_from_8ema_decision TEXT NOT NULL,
            recommended_bias TEXT NOT NULL,confidence INTEGER,source TEXT NOT NULL,latency_ms REAL,reason TEXT,execution_gate_mode TEXT DEFAULT 'ADVISORY_ONLY')''')
    def connect(self):
        return sqlite3.connect(self.ledger_db,timeout=1)
    def get_cached_grok_regime(self):
        # No file, SQL or network access, no worker lock on hot path.
        cached=self._cache
        return {**cached,'stale':not cached or time.time()-cached['ts']>self.rules['cache_ttl_seconds'],'enabled':self.rules['enabled'],'execution_gate_mode':'ADVISORY_ONLY'}
    def persist(self,result,packet,started):
        now=time.time();result={**result,'ts':now,'ist_time':datetime.fromtimestamp(now,ZoneInfo('Asia/Kolkata')).isoformat(),'execution_gate_mode':'ADVISORY_ONLY','latency_ms':round((time.perf_counter()-started)*1000,3),'features':packet}
        keys=['ts','ist_time','spot_price','ema_4_15m','ema_8_15m','dist_8ema_pts','sep_atr','slot_rvol_median','ict_smc_bias','regime','far_from_8ema_decision','recommended_bias','confidence','source','latency_ms','reason','execution_gate_mode']
        row={**packet,**result,'dist_8ema_pts':packet.get('dist_from_15m_8ema_pts'),'ict_smc_bias':json.dumps(packet.get('ict_smc_desk_summary',[]))}
        with self.connect() as db:
            db.execute('INSERT INTO grok_signal_classifications ('+','.join(keys)+') VALUES ('+','.join('?' for _ in keys)+')',[row.get(k) for k in keys])
        if self.cache_path:
            tmp=self.cache_path.with_suffix('.tmp');tmp.write_text(json.dumps(result,allow_nan=False));tmp.replace(self.cache_path)
        self._cache=result
        return self.get_cached_grok_regime()
    def evaluate(self, force=False):
        if not self.rules['enabled'] or not self._worker_lock.acquire(blocking=False):
            return self.get_cached_grok_regime()
        try:
            started=time.perf_counter();packet=build_8600_to_8510_feature_packet(self.market_db,self.volume_engine,rules=self.rules)
            source=packet.get('source_ts')
            if not force and source==self._last_source and self._cache:
                return self.get_cached_grok_regime()
            result=classify_8600_signal(packet,rules=self.rules)
            out=self.persist(result,packet,started);self._last_source=source;return out
        finally:
            self._worker_lock.release()
    def ingest_grok_bot_webhook(self,payload):
        result=validate(payload)
        if not self.rules['enabled']:
            raise ValueError('Bridge disabled')
        if not self._worker_lock.acquire(blocking=False):
            raise ValueError('Bridge busy; retry')
        try:
            p=build_8600_to_8510_feature_packet(self.market_db,self.volume_engine,rules=self.rules)
            if p.get('data_status')!='FRESH':
                raise ValueError('Fresh local observations required for webhook')
            return self.persist({**result,'source':'GROK_BOT_WEBHOOK'},p,time.perf_counter())
        finally:
            self._worker_lock.release()
    def status(self):
        with self.connect() as db:
            db.row_factory=sqlite3.Row
            rows=[dict(row) for row in db.execute('SELECT * FROM grok_signal_classifications ORDER BY id DESC LIMIT 20')]
        return {'latest':self.get_cached_grok_regime(),'rows':rows,'config':dict(self.rules)}
    def update_config(self,updates):
        if set(updates)-{'enabled','provider_mode'}:
            raise ValueError('Only enabled/provider_mode may change; bridge remains advisory')
        if 'enabled' in updates and type(updates['enabled']) is not bool:
            raise ValueError('enabled must be boolean')
        if 'provider_mode' in updates and updates['provider_mode'] not in ('AUTO_XAI_OR_LOCAL_DETERMINISTIC','LOCAL_DETERMINISTIC'):
            raise ValueError('Invalid provider_mode')
        if not self._worker_lock.acquire(blocking=False):
            raise ValueError('Bridge busy; retry')
        try:
            r={**self.rules,**updates,'execution_gate_mode':'ADVISORY_ONLY'}
            tmp=self.rules_path.with_suffix('.tmp');tmp.write_text(json.dumps(r,indent=2)+'\n');tmp.replace(self.rules_path);self.rules=r
            self._cache={};self._last_source=None
            return dict(r)
        finally:
            self._worker_lock.release()
