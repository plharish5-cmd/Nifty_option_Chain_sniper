"""Local-only narrative interpreter and recorded-file worker. Advisory, never an order gate."""
from __future__ import annotations
import copy
import hashlib
import json
import os
import queue
import sqlite3
import threading
import time
from pathlib import Path
from urllib.parse import urlparse
from contextlib import contextmanager
from datetime import datetime
import requests
from analyst.options_telemetry_engine import build_telemetry,load_completed_rows,RULES,IST

OLLAMA_MODEL=os.environ.get('OLLAMA_TELEMETRY_MODEL','qwen2.5:3b')
SYSTEM_PROMPT="""You are a LOCAL, advisory-only telemetry narrator. Return ONLY JSON with
summary (brief string) and warnings (array of brief strings). Deterministic classifications,
quality and scores are authoritative; never recalculate or override them. Session regime
uses the 09:15 anchor; current regime uses recent completed windows. Confidence and bias
scores are HEURISTIC_NOT_CALIBRATED, never probabilities of profitable trades.
OI describes outstanding contracts, not seller intent. Say OI-flow proxy or put/call-side
OI dominance; never confirmed writers. Do not promise support, resistance or profits.
WARMING_UP and DATA_UNRELIABLE are non-directional. A suspected partial bar is a
volume/time heuristic, not proof of exchange completion. Missing optional data stays missing.
Preserve crowding uncertainty: Extreme OI dominance can crowd; watch for profit booking
or mean reversion. No orders, commands, HTML, Markdown or trading instructions.
"""
REQUIRED=('timestamp','data_status','day_regime','bias_score_pct','confidence_pct','oi_dominance','futures_structure','participation','confirmation','summary','warnings','session_regime','current_regime','confidence_kind','session_context')


def validate_interpretation(candidate,deterministic):
    if not isinstance(candidate,dict) or any(k not in candidate for k in REQUIRED):raise ValueError('Incomplete local output schema')
    if set(candidate)-set(REQUIRED)-{'input_telemetry'}:raise ValueError('Unexpected local output fields')
    if any(isinstance(candidate[k],bool) or not isinstance(candidate[k],(int,float)) for k in ('bias_score_pct','confidence_pct')):raise ValueError('Invalid numeric schema')
    for key in REQUIRED:
        if key in ('summary','warnings','session_regime','current_regime','confidence_kind','session_context'):continue
        if candidate[key]!=deterministic[key]:raise ValueError('Local output changed deterministic '+key)
    if not isinstance(candidate['summary'],str) or len(candidate['summary'])>1000:raise ValueError('Invalid summary')
    if not isinstance(candidate['warnings'],list) or any(not isinstance(w,str) or len(w)>500 for w in candidate['warnings']) or len(candidate['warnings'])>30:raise ValueError('Invalid warnings')
    text=(candidate['summary']+' '+' '.join(candidate['warnings'])).lower()
    if any(phrase in text for phrase in ('confirmed put writers','confirmed call writers','<script','guaranteed profit','buy now','sell now','place order')):raise ValueError('Unsupported interpretation claim')
    result=copy.deepcopy(deterministic);result['summary']=candidate['summary']
    result['warnings']=list(dict.fromkeys(deterministic['warnings']+candidate['warnings']))
    if deterministic['data_status']=='DATA_UNRELIABLE':
        result['summary']='Data unreliable; no directional conclusion.';result['day_regime']='DATA_UNRELIABLE';result['bias_score_pct']=0;result['confidence_pct']=min(result['confidence_pct'],RULES['scoring']['unreliable_confidence_max'])
    return result


def interpret_local(result,transport=None):
    url=RULES['local_url'];parts=urlparse(url)
    if parts.hostname not in ('127.0.0.1','localhost','::1') or parts.port!=11434:raise ValueError('Interpreter must be loopback-only')
    fallback={**copy.deepcopy(result),'interpreter_source':'DETERMINISTIC_PYTHON_ENGINE'}
    reference={k:result[k] for k in REQUIRED}
    raw=result['input_telemetry']
    packet={'timestamp':raw['timestamp'],'data_quality':{k:raw['data_quality'].get(k) for k in ('mode','partial_bar_flag','partial_latest_row_discarded','source_date_verified','active_windows')},
      'latest':{k:raw['latest'].get(k) for k in ('future_ltp','pe_ce_oi_total','pcr')},
      'windows':{k:{f:w.get(f) for f in ('valid','price_change','pe_ce_oi_change','futures_regime')} for k,w in raw['windows'].items()},
      'optional':{k:'NOT_AVAILABLE' if v is None else 'AVAILABLE' for k,v in raw['optional'].items()},
      'deterministic_result':{k:result[k] for k in ('data_status','session_regime','current_regime','bias_score_pct','confidence_pct','confidence_kind','oi_dominance','futures_structure','participation')},
      'required_warnings':result['warnings']}
    payload={'model':OLLAMA_MODEL,'system':SYSTEM_PROMPT,'prompt':json.dumps(packet,allow_nan=False),'format':'json','stream':False,'keep_alive':RULES['qwen']['keep_alive'],'options':{'temperature':0.0,'num_predict':RULES['qwen']['num_predict'],'num_ctx':RULES['qwen']['num_ctx']}}
    try:
        response=(transport or requests.post)(url,json=payload,timeout=RULES['timeout_seconds'],allow_redirects=False)
        if response.status_code!=200:raise ValueError('Local interpreter HTTP status')
        candidate=json.loads(response.json()['response'])
        if isinstance(candidate,dict) and set(candidate)=={'summary','warnings'}:candidate={**reference,**candidate}
        validated=validate_interpretation(candidate,result)
        return {**validated,'interpreter_source':'QWEN_2.5_3B_LOCAL'},'OK'
    except (requests.RequestException,OSError,ValueError,TypeError,KeyError,json.JSONDecodeError) as exc:
        return fallback,type(exc).__name__


class InstitutionalTelemetryBridge:
    def __init__(self,ledger_db,market_db=None,transport=None):
        self.ledger_db=Path(ledger_db);self.market_db=Path(market_db) if market_db else None;self.transport=transport
        self._cache={};self._lock=threading.Lock();self._cache_lock=threading.Lock();self._last_file=None;self._qwen_status='NOT_EVALUATED'
        self._queue=queue.Queue(maxsize=RULES['qwen']['queue_size']);self._stop=threading.Event();self._worker=None;self._generation=0;self._timings={};self._attempts=0
        self.ledger_db.parent.mkdir(parents=True,exist_ok=True)
        with self.connect() as db:db.execute('''CREATE TABLE IF NOT EXISTS institutional_pe_ce_telemetry (
          id INTEGER PRIMARY KEY AUTOINCREMENT,evaluated_at REAL NOT NULL,source_key TEXT NOT NULL,
          event_time TEXT,mode TEXT,data_status TEXT,day_regime TEXT,bias_score_pct REAL,confidence_pct REAL,
          interpreter_source TEXT NOT NULL,output_json TEXT NOT NULL,deterministic_json TEXT)''')
        with self.connect() as db:
            if 'deterministic_json' not in {r[1] for r in db.execute('PRAGMA table_info(institutional_pe_ce_telemetry)')}:
                db.execute('ALTER TABLE institutional_pe_ce_telemetry ADD COLUMN deterministic_json TEXT')
    @contextmanager
    def connect(self):
        db=sqlite3.connect(self.ledger_db,timeout=1)
        try:
            with db:yield db
        finally:db.close()
    def optional_chain(self,rows,now):
        if not self.market_db or not rows:return None
        row=rows[-1];source=row.get('_source_timestamp')
        if not source:return None
        ts=datetime.fromisoformat(source).timestamp()
        if abs(now-ts)>RULES['max_stale_seconds']:return None
        try:
            with sqlite3.connect(self.market_db.resolve().as_uri()+'?mode=ro',uri=True,timeout=1) as db:
                snap=db.execute('SELECT expiry,MAX(ts) AS t FROM chain_snap WHERE ts<=? AND ts>=? GROUP BY expiry ORDER BY t DESC LIMIT 1',(ts,ts-60)).fetchone()
                if not snap:return None
                spot=db.execute("SELECT c FROM candles_1m WHERE sec_id='13' AND ts<=? AND ts>=? ORDER BY ts DESC LIMIT 1",(ts,ts-60)).fetchone()
                if not spot:return None
                atm=spot[0]
                result=db.execute('SELECT strike,ce_ltp,pe_ltp,ce_oi,pe_oi FROM chain_snap WHERE expiry=? AND ts=? ORDER BY ABS(strike-?) LIMIT 1',(snap[0],snap[1],atm)).fetchone()
                if not result:return None
                return dict(zip(('strike','ce_ltp','pe_ltp','ce_oi','pe_oi'),result),timestamp=snap[1],expiry=snap[0],basis='Nearest listed strike to contemporaneous recorded spot')
        except (sqlite3.Error,OSError,ValueError):return None
    def ingest_file(self,path,live=False,reference_now=None,use_local=True):
        if not self._lock.acquire(blocking=False):raise ValueError('Telemetry worker busy; retry')
        try:
            p=Path(path).expanduser().resolve()
            if p.suffix.lower() not in ('.csv','.tsv','.json'):raise ValueError('Use CSV, TSV or JSON')
            rows=load_completed_rows(p)
            now=time.time()
            effective_reference=datetime.fromtimestamp(now,IST) if live else reference_now
            result=build_telemetry(rows,reference_now=effective_reference,live=live)
            if live and result['input_telemetry']['data_quality']['source_date_verified']:
                selected={'_source_timestamp':result['timestamp'],'future_ltp':result['input_telemetry']['latest']['future_ltp']}
                chain=self.optional_chain([selected],now)
                if chain:result=build_telemetry(rows,reference_now=effective_reference,live=live,chain_snapshot=chain)
            # Publish and persist deterministic evidence before waiting on the local model.
            initial={**result,'interpreter_source':'DETERMINISTIC_PYTHON_ENGINE'}
            source=hashlib.sha256(str(p).encode()).hexdigest()
            encoded=json.dumps(initial,allow_nan=False)
            with self.connect() as db:
                cursor=db.execute('INSERT INTO institutional_pe_ce_telemetry (evaluated_at,source_key,event_time,mode,data_status,day_regime,bias_score_pct,confidence_pct,interpreter_source,output_json,deterministic_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                 (now,source,initial['timestamp'],initial['input_telemetry']['data_quality']['mode'],initial['data_status'],initial['day_regime'],initial['bias_score_pct'],initial['confidence_pct'],initial['interpreter_source'],encoded,encoded))
                row_id=cursor.lastrowid
            self._publish(initial,row_id,now,use_local)
            return self.snapshot()
        finally:self._lock.release()
    def _publish(self,result,row_id,now,use_local):
        with self._cache_lock:
            self._generation+=1;generation=self._generation
            self._cache={**result,'evaluated_at':now,'execution_gate_mode':'ADVISORY_ONLY',
                'narrative_status':'PENDING' if use_local else 'DISABLED',
                'authority_label':'DETERMINISTIC_AUTHORITATIVE (Qwen async pending/offline)'}
            self._qwen_status='PENDING' if use_local else 'DISABLED'
            if use_local:
                if self._worker is None or not self._worker.is_alive():
                    self._stop.clear();self._worker=threading.Thread(target=self._narrative_worker,name='institutional-qwen',daemon=True);self._worker.start()
                try:self._queue.put_nowait((generation,result,row_id,now))
                except queue.Full:
                    try:self._queue.get_nowait();self._queue.task_done()
                    except queue.Empty:pass
                    self._queue.put_nowait((generation,result,row_id,now))
    def publish_collected(self,result,row_id):
        self._publish(result,row_id,time.time(),True)
    def _narrative_worker(self):
        while not self._stop.is_set():
            try:item=self._queue.get(timeout=.1)
            except queue.Empty:continue
            generation,result,row_id,now=item
            try:
                start=time.perf_counter();interpreted,status=interpret_local(result,self.transport);elapsed=(time.perf_counter()-start)*1000
                with self._cache_lock:
                    kind='cold_inference_ms' if self._attempts==0 else 'warm_inference_ms';self._timings[kind]=round(elapsed,2);self._attempts+=1
                if self._stop.is_set():continue
                interpreted.update(authority_label='DETERMINISTIC_AUTHORITATIVE (Qwen narrative only)' if status=='OK' else 'DETERMINISTIC_AUTHORITATIVE (Qwen async pending/offline)',narrative_status='READY' if status=='OK' else 'FALLBACK',model_status=status)
                with self.connect() as db:db.execute('UPDATE institutional_pe_ce_telemetry SET interpreter_source=?,output_json=? WHERE id=?',(interpreted['interpreter_source'],json.dumps(interpreted,allow_nan=False),row_id))
                with self._cache_lock:
                    if generation==self._generation:
                        self._cache={**interpreted,'evaluated_at':now,'execution_gate_mode':'ADVISORY_ONLY'};self._qwen_status=status
            except (sqlite3.Error,OSError,ValueError,TypeError) as exc:
                with self._cache_lock:
                    if generation==self._generation:
                        self._qwen_status=type(exc).__name__;self._cache={**self._cache,'narrative_status':'FALLBACK'}
            finally:self._queue.task_done()
    def close(self):
        self._stop.set()
        if self._worker is not None:self._worker.join(timeout=.2)
    def snapshot(self):
        # No SQL, files, model or broker calls on telemetry/order paths.
        with self._cache_lock:result=copy.deepcopy(self._cache)
        if not result:return {'available':False,'data_status':'NO_RECORDED_DATA','execution_gate_mode':'ADVISORY_ONLY'}
        q=result['input_telemetry']['data_quality'];available=False
        if q['mode']=='LIVE' and q['source_date_verified'] and result['data_status']=='VALID':
            age=time.time()-datetime.fromisoformat(result['timestamp']).timestamp();available=0<=age<=RULES['max_stale_seconds']
        if q['mode']=='LIVE':
            age=time.time()-datetime.fromisoformat(result['timestamp']).timestamp() if q['source_date_verified'] else None
            result={**result,'input_telemetry':{**result['input_telemetry'],'data_quality':{**q,'stale_data_seconds':age}}}
        if q['mode']=='LIVE' and not available:
            return {**result,'available':False,'data_status':'DATA_UNRELIABLE','day_regime':'DATA_UNRELIABLE','current_regime':'DATA_UNRELIABLE','bias_score_pct':0,'confidence_pct':min(result['confidence_pct'],RULES['scoring']['unreliable_confidence_max']),'summary':'Live observations unavailable or stale; no directional conclusion.'}
        return {**result,'available':available}
    def qwen_status(self):
        return {'model':OLLAMA_MODEL,'url':RULES['local_url'],'last_status':self._qwen_status,'interpreter_source':self._cache.get('interpreter_source'),'execution_gate_mode':'ADVISORY_ONLY',**self._timings,'requests_attempted':self._attempts,'timing_basis':'First request is cold-labelled; subsequent requests warm-labelled, not proof of model residency'}
    def watch_once(self):
        path=os.environ.get('INSTITUTIONAL_TELEMETRY_INPUT')
        if not path:return
        p=Path(path).expanduser();stat=p.stat();key=(str(p.resolve()),stat.st_mtime_ns,stat.st_size)
        if key==self._last_file:return
        self.ingest_file(p,live=True);self._last_file=key
