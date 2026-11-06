"""Dhan option-chain OI, with expiry identity, receipt-time freshness and durable history.
OI measures outstanding positions; the bias is a positioning heuristic, not observed writer intent.
No scraper, browser session, external site or guessed OI fallback.
"""
from __future__ import annotations
import json
import math
import os
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import httpx

IST=ZoneInfo('Asia/Kolkata')
ROOT=Path(__file__).resolve().parent.parent


def empty_bias(reason='No Dhan OI observation',status='FAILED'):
    return dict(total_oi_ce=None,total_oi_pe=None,oi_change_ce=None,oi_change_pe=None,
        pcr_total_oi=None,pcr_oi_change=None,ce_pe_oi_diff=None,bias='UNKNOWN',
        source='DHAN_API',status=status,timestamp=None,timestamp_epoch=None,
        timestamp_basis='API_RECEIPT',available=False,reason=reason,expiry=None)


def classify_chain(response,expiry,observed_at):
    if not isinstance(response,dict) or response.get('status') not in ('success',None):
        return empty_bias('Dhan option-chain response failed')
    data=response.get('data',{});oc=data.get('oc') if isinstance(data,dict) else None
    if not isinstance(oc,dict) or not oc:return empty_bias('No option-chain strikes')
    totals={'ce':0.0,'pe':0.0};changes={'ce':0.0,'pe':0.0}
    for strike,legs in oc.items():
        if not isinstance(legs,dict):return empty_bias('Malformed strike')
        for kind in ('ce','pe'):
            leg=legs.get(kind)
            if not isinstance(leg,dict):return empty_bias('Incomplete CE/PE coverage; aggregate rejected')
            oi,prior=leg.get('oi'),leg.get('previous_oi')
            if not all(isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v) and v>=0 for v in (oi,prior)):
                return empty_bias('Missing or invalid current / previous OI; aggregate rejected')
            totals[kind]+=oi;changes[kind]+=oi-prior
    ce,pe=totals['ce'],totals['pe'];dc,dp=changes['ce'],changes['pe']
    if ce<=0 or pe<=0 or not all(math.isfinite(v) for v in (ce,pe,dc,dp)):return empty_bias('Non-positive or invalid aggregate OI')
    bias='BULLISH' if pe>ce and dp>dc else 'BEARISH' if ce>pe and dc>dp else 'MIXED'
    return dict(total_oi_ce=ce,total_oi_pe=pe,oi_change_ce=dc,oi_change_pe=dp,
        pcr_total_oi=pe/ce,pcr_oi_change=dp/dc if dc>0 and dp>=0 else None,
        pcr_oi_change_note='Undefined when CE change is non-positive or PE change negative',
        ce_pe_oi_diff=ce-pe,bias=bias,source='DHAN_API',status='OK',available=True,
        timestamp=datetime.fromtimestamp(observed_at,IST).isoformat(),timestamp_epoch=observed_at,
        timestamp_basis='API_RECEIPT',expiry=expiry,underlying_security_id=13,
        exchange_segment='IDX_I',strike_count=len(oc),reason='',
        interpretation='OI positioning heuristic; does not identify buyer/seller initiation')


class DhanOIEngine:
    def __init__(self,cfg=None,db_path=None,expiry_provider=None,headers_provider=None,transport=None):
        config=json.loads((ROOT/'analyst/terminal8510_config.json').read_text())
        self.cfg={**config['dhan_oi'],**(cfg or {})};self.db=Path(db_path or ROOT/'data/terminal8510/ledger.sqlite3')
        self.base_url=config.get('dhan_base_url','https://api.dhan.co/v2')
        self.expiry_provider=expiry_provider;self.headers_provider=headers_provider
        self.transport=transport;self.lock=threading.RLock();self._last_attempt=0.0
        self._snapshot=empty_bias();self.db.parent.mkdir(parents=True,exist_ok=True)
        with self.connect() as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS dhan_oi_snapshots(ts REAL,expiry TEXT,payload TEXT,PRIMARY KEY(ts,expiry))')
    def connect(self):return sqlite3.connect(self.db,timeout=10)
    def _post(self,path,payload):
        if self.transport:return self.transport(path,payload)
        headers=self.headers_provider() if self.headers_provider else {'access-token':os.environ.get('DHAN_ACCESS_TOKEN',''),'client-id':os.environ.get('DHAN_CLIENT_ID','')}
        if not headers.get('access-token'):raise ValueError('Dhan credentials unavailable')
        with httpx.Client(timeout=self.cfg['request_timeout_seconds']) as client:
            response=client.post(self.base_url+path,json=payload,headers=headers);response.raise_for_status();return response.json()
    def get_dhan_oi_bias(self,force=False):
        with self.lock:
            now=time.time()
            # Even forced UI refreshes respect the option-chain poll budget.
            if now-self._last_attempt<self.cfg['poll_seconds']:return self.cached_snapshot(now)
            self._last_attempt=now
            try:
                expiry=self.cfg.get('expiry') or (self.expiry_provider() if self.expiry_provider else None)
                if not expiry:
                    exp_response=self._post('/optionchain/expirylist',{'UnderlyingScrip':13,'UnderlyingSeg':'IDX_I'})
                    today=datetime.fromtimestamp(now,IST).date().isoformat()
                    expiries=exp_response.get('data',[])
                    if not isinstance(expiries,list):raise ValueError('Expiry response malformed')
                    expiry=next((e for e in sorted(expiries) if e>=today),None)
                if not expiry:raise ValueError('No verified unexpired option-chain expiry')
                datetime.strptime(expiry,'%Y-%m-%d')
                if expiry<datetime.fromtimestamp(now,IST).date().isoformat():raise ValueError('Expired option-chain identity')
                result=classify_chain(self._post('/optionchain',{'UnderlyingScrip':13,'UnderlyingSeg':'IDX_I','Expiry':expiry}),expiry,now)
                if result['status']!='OK':self._snapshot=result;return self.cached_snapshot(now)
                with self.connect() as conn:
                    conn.execute('INSERT OR REPLACE INTO dhan_oi_snapshots VALUES(?,?,?)',(now,expiry,json.dumps(result,allow_nan=False)))
                self._snapshot=result
            except Exception as ex:
                # Retain the original observation for display, but never its directional vote after a failure.
                self._snapshot={**self._snapshot,'status':'STALE' if self._snapshot.get('timestamp') else 'FAILED',
                    'bias':'UNKNOWN','available':False,'reason':'Dhan OI fetch failed ('+type(ex).__name__+')'}
            return self.cached_snapshot(now)
    def cached_snapshot(self,now=None):
        now=time.time() if now is None else now
        result=dict(self._snapshot);stamp=result.get('timestamp_epoch')
        if stamp is not None:
            result['age_seconds']=max(0,now-stamp)
            if now-stamp>self.cfg['max_age_seconds'] or now<stamp or datetime.fromtimestamp(now,IST).date()!=datetime.fromtimestamp(stamp,IST).date():
                result.update(status='STALE',available=False,bias='UNKNOWN',reason='Dhan OI observation stale')
        return result
    def get_snapshot_at(self,timestamp):
        boundary=datetime.fromtimestamp(timestamp,IST).replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
        with self.connect() as conn:
            record=conn.execute('SELECT payload FROM dhan_oi_snapshots WHERE ts>=? AND ts<=? ORDER BY ts DESC LIMIT 1',(boundary,timestamp)).fetchone()
        if not record:return empty_bias('No recorded Dhan API OI for this time; no historical substitution')
        snap=json.loads(record[0]);age=timestamp-snap['timestamp_epoch']
        if age>self.cfg['max_age_seconds']:snap.update(status='STALE',bias='UNKNOWN',available=False,reason='Historical Dhan OI sample is stale')
        return snap
    def get_session_observations(self,day):
        start=datetime.fromisoformat(day+'T00:00:00').replace(tzinfo=IST).timestamp()
        end=datetime.fromisoformat(day+'T23:59:59').replace(tzinfo=IST).timestamp()
        with self.connect() as conn:
            rows=conn.execute('SELECT payload FROM dhan_oi_snapshots WHERE ts>=? AND ts<=? ORDER BY ts',(start,end)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def get_snapshot(self,force=False):
        # UI compatibility data is derived from the one Dhan snapshot, never another feed.
        s=self.get_dhan_oi_bias(force)
        return self.compatibility_snapshot(s)
    def compatibility_snapshot(self,s):
        available=s.get('available',False)
        return {**s,'total_call_oi':s.get('total_oi_ce'),'total_put_oi':s.get('total_oi_pe'),
            'call_oi_change_day':s.get('oi_change_ce'),'put_oi_change_day':s.get('oi_change_pe'),
            'pe_minus_ce_day':s['oi_change_pe']-s['oi_change_ce'] if s.get('oi_change_pe') is not None else None,
            'pe_minus_ce_interval':None,'pcr':s.get('pcr_total_oi'),
            'writer_bias':s['bias'] if available else 'NO DATA','writer_signal':s['bias'] if available else 'NO DATA',
            'tag':s['bias'],'badge':'bull' if s['bias']=='BULLISH' else 'bear' if s['bias']=='BEARISH' else 'neutral',
            'signal_hint':s.get('reason',''),'last_sync_time':s.get('timestamp'),'cw_vs_pw_ratio':s.get('pcr_total_oi'),
            'futures_volume_surge':None,'is_surge':False}
    def evaluate_synergy_status(self):
        s=self.cached_snapshot();bias=s['bias']
        return dict(synergy_status=bias+'_SYNERGY' if s['available'] and bias in ('BULLISH','BEARISH') else 'MIXED',
                    combined_bias=bias,total_oi_bias=bias,oi_change_bias=bias,source='DHAN_API',status=s['status'],available=s['available'])
    def get_crore_spread_metrics(self):
        s=self.cached_snapshot();v=s.get('ce_pe_oi_diff')
        return dict(available=s['available'],source='DHAN_API',pe_minus_ce_cr=-v/1e7 if v is not None else None)
    def get_multi_strike_status(self):
        return dict(available=False,source='DHAN_API',reason='Per-strike display not enabled in this summary')
    def get_pe_ce_table(self,limit=60):
        with self.connect() as conn:
            records=conn.execute('SELECT payload FROM dhan_oi_snapshots ORDER BY ts DESC LIMIT ?', (min(max(int(limit),1),self.cfg['history_limit']),)).fetchall()
        rows=[];snap=self.cached_snapshot()
        for record in records:
            s=json.loads(record[0])
            # A table never mixes expiries or sessions into artificial interval deltas.
            if snap.get('expiry') and s['expiry']!=snap['expiry']:continue
            if snap.get('timestamp') and s['timestamp'][:10]!=snap['timestamp'][:10]:continue
            rows.append(dict(Time=datetime.fromisoformat(s['timestamp']).strftime('%H:%M:%S'),
                **{'Put OI Change (Day)':s['oi_change_pe'],'Call OI Change (Day)':s['oi_change_ce'],
                   'PE-CE OI Change (Day)':s['oi_change_pe']-s['oi_change_ce'],'PCR Total':s['pcr_total_oi'],
                   'Put OI Change':None,'Call OI Change':None,'PE-CE OI Change':None,
                   'Future LTP':None,'Future OI Change (Day)':None}))
        synergy=self.evaluate_synergy_status()
        return dict(ok=True,source='DHAN_API',status=snap['status'],available=snap['available'],rows=rows,
            combined_rows=rows,oi_change_day_rows=rows,total_oi_rows=rows,synergy=synergy,last_sync_time=snap.get('timestamp'))

_DEFAULT=None

def get_dhan_oi_bias(engine=None):
    global _DEFAULT
    if engine is None:
        if _DEFAULT is None:_DEFAULT=DhanOIEngine()
        engine=_DEFAULT
    return engine.get_dhan_oi_bias()
