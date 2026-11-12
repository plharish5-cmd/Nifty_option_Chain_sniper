"""v1.9 deterministic Dhan OI / closed-slot volume / advisory score / archiving tests.
No credentials, real orders, network requests, or writes to the 8600 source DB.
"""
import asyncio
import hashlib
import itertools
import json
import sqlite3
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo
import pandas as pd
import pytest
from analyst.dhan_oi_engine import DhanOIEngine, classify_chain, empty_bias
from analyst.futures_volume_engine import FuturesVolumeEngine
from analyst.terminal8510_backend import QuantTerminalBackend, compute_five_factor_confluence

ROOT=Path(__file__).resolve().parent.parent
IST=ZoneInfo('Asia/Kolkata')

@pytest.fixture
def cfg():
    return json.loads((ROOT/'analyst/terminal8510_config.json').read_text())

@pytest.fixture
def volume_db(tmp_path):
    path=tmp_path/'market.sqlite';days=pd.bdate_range('2026-10-19',periods=8)
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE candles_1m(sec_id TEXT,ts INTEGER,v REAL,PRIMARY KEY(sec_id,ts))')
        for index,day in enumerate(days):
            start=int(datetime(day.year,day.month,day.day,9,15,tzinfo=IST).timestamp())
            for minute in range(375):
                vol=1000 if minute<210 or minute>=330 else 100
                front=.9 if index<4 else .1
                for sid,part in [('FUT',front),('FUT2',1-front)]:conn.execute('INSERT INTO candles_1m VALUES(?,?,?)',(sid,start+minute*60,vol*part))
    return path,days

def test_01_slot_normalized_median_vs_ushape_distortion(volume_db):
    db,days=volume_db;engine=FuturesVolumeEngine(db_path=db)
    stamp=int(datetime(days[-1].year,days[-1].month,days[-1].day,13,0,tzinfo=IST).timestamp())
    row=engine.get_vsr_at_timestamp(stamp)
    assert row['slot']=='12:45';assert row['slot_rvol_median']==pytest.approx(1.)
    assert row['volume']==pytest.approx(1500);assert row['vsr_10']<1
    assert row['sessions_covered']==7;assert row['coverage_pct']==35.
    early=int(datetime(days[3].year,days[3].month,days[3].day,13,0,tzinfo=IST).timestamp())
    warm=engine.get_vsr_at_timestamp(early)
    assert warm['slot_rvol_median'] is None;assert warm['slot_status']=='INSUFFICIENT_HISTORY'

def test_no_future_or_partial_slot(volume_db):
    db,days=volume_db;engine=FuturesVolumeEngine(db_path=db)
    stamp=int(datetime(days[-1].year,days[-1].month,days[-1].day,12,50,tzinfo=IST).timestamp())
    assert engine.get_vsr_at_timestamp(stamp)['slot']=='12:30'
    assert engine.get_vsr_at_timestamp(0)['vsr_10'] is None
    with sqlite3.connect(db) as conn:conn.execute("DELETE FROM candles_1m WHERE ts=? AND sec_id='FUT2'",(stamp,))
    engine.ensure_loaded(force=True)
    incomplete=engine.get_vsr_at_timestamp(stamp+600)
    assert incomplete['slot_status']=='INCOMPLETE_BAR';assert incomplete['is_institutional_surge'] is False

@pytest.mark.parametrize('slot,vsr,accel,status,expected',[(1.25,1,1.15,'ADEQUATE',True),(1.25,2,1.14,'ADEQUATE',False),(1.24,3,2,'ADEQUATE',False),(None,1.25,1.15,'INSUFFICIENT_HISTORY',True),(None,1.24,2,'INSUFFICIENT_HISTORY',False),(None,2,2,'INCOMPLETE_BAR',False)])
def test_02_bar_acceleration_and_institutional_surge_gate(volume_db,slot,vsr,accel,status,expected):
    engine=FuturesVolumeEngine(db_path=volume_db[0]);row=engine._format_record(dict(complete=status!='INCOMPLETE_BAR',slot_rvol_median=slot,vsr_10=vsr,bar_accel_ratio=accel,slot_status=status))
    assert row['is_institutional_surge'] is expected

def inputs(cfg):
    now=time.time()
    return ({'fresh':True,'timeframe':'15m','side':'CE','ema4':104.,'ema8':100.,'band_slope':1.,'opportunity':1,'atr':20.,'flips_last_12_bars':2,'price_fresh':True,'abs_gap_pts':4.,'wick_touched_ema8':False},
        {'status':'OK','source':'DHAN_API','available':True,'bias':'BULLISH','timestamp_epoch':now},
        {'slot_rvol_median':1.3,'vsr_10':1.3,'bar_accel_ratio':1.2,'available':True,'slot_status':'ADEQUATE','bar_ts':now-900})

@pytest.mark.parametrize('mask',range(32))
def test_03_five_factor_confluence_scorer(cfg,mask):
    t,oi,v=inputs(cfg)
    if not mask&1:t['opportunity']=3
    if not mask&2:t['flips_last_12_bars']=3
    if not mask&4:v['slot_rvol_median']=1
    if not mask&8:oi['status']='FAILED'
    if not mask&16:t['abs_gap_pts']=100
    score=compute_five_factor_confluence(t,oi,v,cfg)
    assert score['score']==bin(mask).count('1');assert score['max_score']==5
    assert score['execution_authorized'] is False
    assert score['pillar_4_option_writer_flow'] is bool(mask&8)

def test_stale_or_incomplete_pillars_do_not_pass(cfg):
    t,oi,v=inputs(cfg);oi['timestamp_epoch']=time.time()-1000
    assert not compute_five_factor_confluence(t,oi,v,cfg)['pillar_4_option_writer_flow']
    t['timeframe']='1m';assert not compute_five_factor_confluence(t,oi,v,cfg)['pillar_1_trend_structure']
    assert compute_five_factor_confluence({}, {}, {}, cfg)['score']==0

def test_04_post_market_auto_archive_integration(tmp_path,cfg,monkeypatch):
    engine=QuantTerminalBackend.__new__(QuantTerminalBackend);engine.cfg=cfg;engine.ledger_db=tmp_path/'ledger.sqlite';engine._archive_lock=threading.Lock()
    source=tmp_path/'source.sqlite'
    with sqlite3.connect(source) as conn:conn.execute('CREATE TABLE preserved(value TEXT)');conn.execute("INSERT INTO preserved VALUES('unchanged')")
    before=hashlib.sha256(source.read_bytes()).hexdigest();calls=[]
    def run(command,**kwargs):
        calls.append((command,kwargs))
        for folder in ('volume','chain','candles'):
            dst=tmp_path/'data/archive'/folder/'2026-11-05.parquet';dst.parent.mkdir(parents=True,exist_ok=True);dst.write_bytes(b'mocked archive')
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr('analyst.terminal8510_backend.ROOT',tmp_path)
    monkeypatch.setattr('analyst.terminal8510_backend.subprocess.run',run)
    now=datetime(2026,11,5,15,35,tzinfo=IST)
    result=engine.run_post_market_archivers('2026-11-05',now)
    assert result['ok'];assert len(calls)==2
    assert all(x[0][0]==sys.executable and x[1]['timeout']==60 and x[1]['check'] is False for x in calls)
    assert engine.run_post_market_archivers('2026-11-05',now)['ok'];assert len(calls)==2
    assert hashlib.sha256(source.read_bytes()).hexdigest()==before
    assert engine.run_post_market_archivers('2026-11-06',now)['status']=='NOT_DUE'

def test_archive_failure_does_not_raise(tmp_path,cfg,monkeypatch):
    engine=QuantTerminalBackend.__new__(QuantTerminalBackend);engine.cfg=cfg;engine.ledger_db=tmp_path/'ledger.sqlite';engine._archive_lock=threading.Lock()
    def fail(*args,**kwargs):raise TimeoutError('test')
    monkeypatch.setattr('analyst.terminal8510_backend.subprocess.run',fail)
    result=engine.run_post_market_archivers('2026-11-05',datetime(2026,11,5,15,35,tzinfo=IST))
    assert result['status']=='FAILED';assert not result['ok']

def test_05_no_stockmojo_references():
    for name in ['analyst/terminal8510_backend.py','analyst/dhan_oi_engine.py','engine_8510_live.py']:
        text=(ROOT/name).read_text().lower()
        assert all(word not in text for word in ['stockmojo','stock_mojo','stockmodule'])
    assert 'DHAN_API' in (ROOT/'analyst/dhan_oi_engine.py').read_text()
    assert 'pillFiveFactorConfluence' in (ROOT/'Algorithmic_Trading_Workspace_8510.html').read_text()

def chain(ce,pe,dc,dp):return {'status':'success','data':{'oc':{'24000':{'ce':{'oi':ce,'previous_oi':ce-dc},'pe':{'oi':pe,'previous_oi':pe-dp}}}}}

@pytest.mark.parametrize('ce,pe,dc,dp,bias',[(100,200,10,20,'BULLISH'),(200,100,20,10,'BEARISH'),(200,100,10,20,'MIXED'),(100,100,10,10,'MIXED')])
def test_06_dhan_oi_bias(ce,pe,dc,dp,bias):
    s=classify_chain(chain(ce,pe,dc,dp),'2026-12-31',time.time());assert s['bias']==bias;assert s['source']=='DHAN_API'
    assert s['pcr_total_oi']==pe/ce;assert s['ce_pe_oi_diff']==ce-pe

def test_missing_previous_oi_and_zero_fail_closed():
    data=chain(100,200,10,20);del data['data']['oc']['24000']['ce']['previous_oi']
    assert classify_chain(data,'2026-12-31',time.time())['bias']=='UNKNOWN'
    assert classify_chain(chain(0,0,0,0),'2026-12-31',time.time())['bias']=='UNKNOWN'

def test_api_failure_stale_and_asof_no_lookahead(tmp_path,cfg,monkeypatch):
    responses=[chain(100,200,10,20)]
    def transport(*args):return responses[0]
    engine=DhanOIEngine(cfg['dhan_oi'],tmp_path/'ledger.sqlite',expiry_provider=lambda:'2026-12-31',transport=transport)
    first=engine.get_dhan_oi_bias();assert first['status']=='OK'
    before=engine.get_snapshot_at(first['timestamp_epoch']-1);assert before['bias']=='UNKNOWN'
    same=engine.get_snapshot_at(first['timestamp_epoch']);assert same['bias']=='BULLISH'
    assert engine.compatibility_snapshot(same)['pe_minus_ce_day']==10
    engine._last_attempt=0
    def fail(*args):raise OSError('mock failure')
    engine.transport=fail;s=engine.get_dhan_oi_bias();assert s['bias']=='UNKNOWN';assert s['status']=='STALE';assert not s['available']
    assert engine.cached_snapshot(first['timestamp_epoch']+10000)['status']=='STALE'

def test_telemetry_integration(tmp_path,cfg,monkeypatch):
    cfg['ledger_db_path']=str(tmp_path/'ledger.sqlite');cfg['market_db_path']=str(tmp_path/'market.sqlite')
    with sqlite3.connect(cfg['market_db_path']) as conn:conn.execute('CREATE TABLE candles_1m(sec_id TEXT,ts INTEGER,o REAL,h REAL,l REAL,c REAL,v REAL)')
    p=tmp_path/'config.json';p.write_text(json.dumps(cfg));engine=QuantTerminalBackend(str(p))
    monkeypatch.setattr(engine.inst_mgr,'ensure_loaded',lambda:None)
    monkeypatch.setattr(engine.inst_mgr,'get_active_future_contract',lambda:None)
    monkeypatch.setattr(engine.inst_mgr,'get_nearest_expiry',lambda:'2026-12-31')
    monkeypatch.setattr(engine.inst_mgr,'resolve_option',lambda *args:None)
    monkeypatch.setattr(engine.broker,'get_marketfeed_ltp',AsyncMock(return_value={}))
    monkeypatch.setattr(engine.broker,'get_fundlimit',AsyncMock(return_value={}))
    monkeypatch.setattr(engine.dhan_oi,'get_dhan_oi_bias',lambda:empty_bias())
    data=asyncio.run(engine.get_live_telemetry())
    assert 'five_factor_confluence' in data;assert data['five_factor_confluence']['score']==0
    assert data['dhan_oi']['source']=='DHAN_API'
    assert 'futures_volume_surge' not in data          # removed by the owner, 12 Nov 2026


def test_real_archivers_read_only_round_trip(tmp_path,cfg,monkeypatch):
    import shutil
    sandbox=tmp_path/'repo';(sandbox/'scripts').mkdir(parents=True)
    for name in ('archive_volume.py','archive_chain_day.py'):
        shutil.copy2(ROOT/'scripts'/name,sandbox/'scripts'/name)
    pure=sandbox/'ict_smc_app/backend/app/engine';pure.mkdir(parents=True)
    shutil.copy2(ROOT/'ict_smc_app/backend/app/engine/volume_store.py',pure/'volume_store.py')
    source=sandbox/'ict_smc_app/backend/data/ict_smc.sqlite3';source.parent.mkdir(parents=True)
    stamp=int(datetime(2026,11,5,9,15,tzinfo=IST).timestamp())
    with sqlite3.connect(source) as conn:
        conn.execute('CREATE TABLE candles_1m(sec_id TEXT,ts INTEGER,o REAL,h REAL,l REAL,c REAL,v REAL)')
        for sid in ('13','FUT','FUT2'):conn.execute('INSERT INTO candles_1m VALUES(?,?,?,?,?,?,?)',(sid,stamp,100,101,99,100,10))
        conn.execute('CREATE TABLE chain_snap(expiry TEXT,ts INTEGER,strike REAL,ce_oi REAL,pe_oi REAL,ce_ltp REAL,pe_ltp REAL,ce_vol REAL,pe_vol REAL,ce_bid REAL,ce_ask REAL,pe_bid REAL,pe_ask REAL)')
        conn.execute('INSERT INTO chain_snap VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',('2026-11-09',stamp,24000,100,200,10,20,10,10,9,11,19,21))
    before=hashlib.sha256(source.read_bytes()).hexdigest()
    engine=QuantTerminalBackend.__new__(QuantTerminalBackend);engine.cfg=cfg;engine.ledger_db=tmp_path/'ledger.sqlite';engine._archive_lock=threading.Lock()
    monkeypatch.setattr('analyst.terminal8510_backend.ROOT',sandbox)
    result=engine.run_post_market_archivers('2026-11-05',datetime(2026,11,5,15,35,tzinfo=IST))
    assert result['ok'],result
    assert all(result['outputs'].values())
    assert hashlib.sha256(source.read_bytes()).hexdigest()==before
    assert len(pd.read_parquet(sandbox/'data/archive/chain/2026-11-05.parquet'))==1
    assert len(pd.read_parquet(sandbox/'data/archive/volume/2026-11-05.parquet'))==3


def test_closed_candle_confluence_context_and_radar_reuse(tmp_path,cfg,monkeypatch):
    db=tmp_path/'market.sqlite';days=pd.bdate_range('2026-11-02',periods=4)
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE candles_1m(sec_id TEXT,ts INTEGER,o REAL,h REAL,l REAL,c REAL,v REAL)')
        for k,day in enumerate(days):
            start=int(datetime(day.year,day.month,day.day,9,15,tzinfo=IST).timestamp())
            for minute in range(375):
                price=24000+k*25+minute/15
                conn.execute('INSERT INTO candles_1m VALUES(?,?,?,?,?,?,?)',('13',start+minute*60,price,price+5,price-5,price,100))
    now=datetime(2026,11,5,13,0,tzinfo=IST).timestamp()
    engine=QuantTerminalBackend.__new__(QuantTerminalBackend);engine.cfg=cfg;engine.market_db=db;engine._confluence_context_cache={};engine._confluence_context_at=0
    engine.ema_proximity_engine=SimpleNamespace(get_latest_radar_status=lambda **kw:{'ok':True,'flips_last_12_bars':0,'abs_gap_pts':4.,'wick_touched_ema8':True})
    t,oi,v=inputs(cfg);oi['timestamp_epoch']=now;v['bar_ts']=now-900
    engine.dhan_oi=SimpleNamespace(cached_snapshot=lambda:oi)
    engine.futures_volume_engine=SimpleNamespace(get_latest_vsr=lambda:v)
    monkeypatch.setattr('analyst.terminal8510_backend.time.time',lambda:now)
    result=engine.get_five_factor_confluence(24099,True)
    assert engine._confluence_context_cache.get('timeframe')=='15m'
    assert engine._confluence_context_cache.get('signal_ts')==now
    assert result['pillar_2_anti_chop_regime'] is True
    assert result['pillar_5_pocket_execution'] is True
    # The volume engine was removed by the owner on 12 Nov 2026: the volume pillar is no
    # longer supplied, so the live score tops out at 4 of 5.
    assert result['score']==4


def test_ui_source_and_atm_labels():
    html=(ROOT/'Algorithmic_Trading_Workspace_8510.html').read_text()
    assert 'DHAN OI PCR:' in html
    assert 'Rounded to 100' in html
    assert 'STOCKMOJO PCR:' not in html
    assert 'Rounded to 50' not in html
