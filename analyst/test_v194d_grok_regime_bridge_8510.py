"""Advisory bridge verification; every database/cache/config write uses tmp_path."""
import asyncio
import ast
import json
import sqlite3
import subprocess
import time
from pathlib import Path
from unittest.mock import patch
import httpx
import pytest
from analyst import grok_regime_bridge_8510 as g
ROOT=Path(__file__).resolve().parents[1]

@pytest.fixture
def rules():
    return json.loads(g.RULES.read_text())

@pytest.fixture
def packet():
    return dict(data_status='FRESH',spot_price=100,ema_4_15m=98,ema_8_15m=75,sep_atr=.5,flips_12bars=0,
                dist_from_15m_8ema_pts=25,dist_from_15m_4ma_pts=2,slot_rvol_median=1.5)

@pytest.fixture
def bridge(tmp_path,rules):
    p=tmp_path/'rules.json';p.write_text(json.dumps(rules))
    return g.GrokRegimeBridge(tmp_path/'ledger.sqlite3',tmp_path/'market.sqlite3',p,tmp_path/'cache.json')

def test_01_chop_classification(packet,rules):
    packet['sep_atr']=.1
    r=g.classify_8600_signal(packet,True,rules)
    assert r['regime']=='CONSOLIDATION_CHOP' and r['far_from_8ema_decision']=='AVOID_CHOP'

@pytest.mark.parametrize('bear',[False,True])
def test_02_far_from_8ema_4ma_continuation(packet,rules,bear):
    if bear:packet.update(ema_4_15m=75,ema_8_15m=98)
    r=g.classify_8600_signal(packet,True,rules)
    assert r['regime']==('BEARISH_TREND' if bear else 'BULLISH_TREND')
    assert r['far_from_8ema_decision']=='ALLOW_4MA_CONTINUATION'

def test_03_overextended_climax_blocks_chasing(packet,rules):
    packet.update(dist_from_15m_8ema_pts=40,slot_rvol_median=.8)
    r=g.classify_8600_signal(packet,True,rules)
    assert r['regime']=='OVEREXTENDED_CLIMAX' and r['far_from_8ema_decision']=='WAIT_FOR_8EMA_TOUCH'

def test_04_grok_webhook_ingest_and_sqlite_logging(bridge,packet,monkeypatch):
    import engine_8510_live as engine
    monkeypatch.setattr(engine.backend,'grok_regime_bridge',bridge,raising=False)
    monkeypatch.setenv('GROK_WEBHOOK_SECRET','test-only-secret')
    monkeypatch.setattr(g,'build_8600_to_8510_feature_packet',lambda *a,**kw:packet)
    payload=g.classify_8600_signal(packet,True,bridge.rules)
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=engine.app),base_url='http://testserver') as client:
            assert (await client.post('/api/grok_bridge/webhook',json=payload)).status_code==401
            r=await client.post('/api/grok_bridge/webhook',json=payload,headers={'Authorization':'Bearer test-only-secret'})
            assert r.status_code==200 and r.json()['source']=='GROK_BOT_WEBHOOK'
            status=await client.get('/api/grok_bridge/status')
            assert len(status.json()['rows'])==1
    asyncio.run(run())
    with bridge.connect() as db:
        assert db.execute('SELECT execution_gate_mode FROM grok_signal_classifications').fetchone()[0]=='ADVISORY_ONLY'
    assert bridge.cache_path.exists()

def test_05_advisory_only_never_mutates_execution_or_live_guards(bridge,packet,monkeypatch):
    from analyst.terminal8510_backend import QuantTerminalBackend
    monkeypatch.setattr(g,'build_8600_to_8510_feature_packet',lambda *a,**kw:packet)
    monkeypatch.delenv('XAI_API_KEY',raising=False)
    with patch.object(QuantTerminalBackend,'live_execution_decision',side_effect=AssertionError('not an execution dependency')):
        assert bridge.evaluate(True)['execution_gate_mode']=='ADVISORY_ONLY'
    with pytest.raises(ValueError):bridge.update_config({'execution_gate_mode':'LIVE'})
    assert 'terminal8510_backend' not in Path(g.__file__).read_text()
    # All pre-existing backend methods except the new cached accessor are byte-identical.
    old=subprocess.check_output(['git','show','606332e:analyst/terminal8510_backend.py'],cwd=ROOT,text=True)
    new=(ROOT/'analyst/terminal8510_backend.py').read_text()
    def functions(s):
        return {n.name:ast.dump(n) for n in ast.walk(ast.parse(s)) if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))}
    a,b=functions(old),functions(new)
    assert all(b[k]==v for k,v in a.items())

def test_06_timeout_falls_back_cleanly_under_5ms_cache(bridge,packet,monkeypatch):
    monkeypatch.setenv('XAI_API_KEY','fake-key')
    monkeypatch.setattr(g.urllib.request,'urlopen',lambda *a,**k:(_ for _ in ()).throw(TimeoutError()))
    r=g.classify_8600_signal(packet,rules=bridge.rules)
    assert r['source']=='LOCAL_DETERMINISTIC_FALLBACK' and r['provider_error']=='TimeoutError'
    bridge.persist(r,packet,time.perf_counter())
    with patch.object(bridge,'connect',side_effect=AssertionError('cache cannot access SQL')):
        start=time.perf_counter()
        for _ in range(100):bridge.get_cached_grok_regime()
        assert (time.perf_counter()-start)/100<.005

def test_07_ui_shutter_and_zero_forbidden_strings():
    html=(ROOT/'Algorithmic_Trading_Workspace_8510.html').read_text()
    assert 'id="grokRegimeBridgeCard"' in html and 'grokBridgeCollapsed' in html
    assert "try {card.open=" in html and 'catch(e){card.open=false;}' in html
    assert '80-ema' not in html.lower() and '80 ema' not in html.lower() and 'recording health' not in html.lower()
    # Rendered data uses textContent rather than HTML interpretation.
    section=html[html.index('<div id="pillGrokRegimeBridge"'):html.index('<section class="card" id="trendCaptureV192"')]
    assert 'innerHTML' not in section

def test_08_locks_and_config_not_polluted():
    result=subprocess.run(['python3','scripts/verify_locks.py'],cwd=ROOT,capture_output=True,text=True)
    assert result.returncode==0 and '11' in result.stdout
    old=json.loads(subprocess.check_output(['git','show','606332e:analyst/terminal8510_config.json'],cwd=ROOT,text=True))
    new=json.loads((ROOT/'analyst/terminal8510_config.json').read_text())
    assert {k:v for k,v in new.items() if k!='grok_regime_bridge_v194d'}==old

@pytest.mark.parametrize('status',['STALE','NO_RECORDED_DATA'])
def test_missing_or_stale_never_allows_continuation(packet,rules,status):
    packet['data_status']=status
    assert g.classify_8600_signal(packet,True,rules)['far_from_8ema_decision']=='WAIT_FOR_8EMA_TOUCH'

@pytest.mark.parametrize('bad',[{'confidence':True},{'confidence':101},{'reason':'bad\nline'},{'recommended_bias':'BUY_NOW'},{'reason':'word '*26}])
def test_webhook_schema_rejects_bad_values(packet,rules,bad):
    payload={**g.local_classify(packet,rules),**bad}
    with pytest.raises(ValueError):g.validate(payload)

def test_disabled_config_and_concurrency(bridge,packet,monkeypatch):
    monkeypatch.delenv('XAI_API_KEY',raising=False)
    monkeypatch.setattr(g,'build_8600_to_8510_feature_packet',lambda *a,**kw:packet)
    bridge._worker_lock.acquire()
    try:assert not bridge.evaluate(True).get('ts')
    finally:bridge._worker_lock.release()
    bridge.update_config({'enabled':False})
    assert not bridge.evaluate(True).get('ts')
    with pytest.raises(ValueError):bridge.ingest_grok_bot_webhook(g.local_classify(packet,bridge.rules))

def test_extractor_readonly_closed_causal_and_gap(tmp_path,rules):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    start=datetime(2026,11,6,9,15,tzinfo=ZoneInfo('Asia/Kolkata')).timestamp()
    path=tmp_path/'market.sqlite3'
    with sqlite3.connect(path) as db:
        db.executescript('CREATE TABLE candles_1m(sec_id TEXT,ts INTEGER,o REAL,h REAL,l REAL,c REAL);CREATE TABLE chain_snap(expiry TEXT,ts INTEGER,ce_oi REAL,pe_oi REAL);CREATE TABLE alerts(ts INTEGER,kind TEXT,side TEXT,title TEXT);')
        db.executemany('INSERT INTO candles_1m VALUES (?,?,?,?,?,?)',[('13',start+i*60,100+i,102+i,99+i,101+i) for i in range(360)])
    before=path.read_bytes();now=start+330*60
    p=g.build_8600_to_8510_feature_packet(path,now=now,rules=rules)
    assert p['data_status']=='FRESH' and p['spot_price']==430
    assert p['source_ts']+60<=now and p['closed_15m_ts']<=now
    assert path.read_bytes()==before
    assert g.build_8600_to_8510_feature_packet(path,now=start+30*60,rules=rules)['data_status']=='NO_RECORDED_DATA'
    assert g.build_8600_to_8510_feature_packet(path,now=start+500*60,rules=rules)['data_status']=='STALE'

def test_api_valid_json_and_config_advisory(bridge,monkeypatch):
    import engine_8510_live as engine
    monkeypatch.setattr(engine.backend,'grok_regime_bridge',bridge,raising=False)
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=engine.app,client=('127.0.0.1',123)),base_url='http://testserver') as c:
            assert (await c.post('/api/grok_bridge/config',json={'execution_gate_mode':'LIVE'})).status_code==400
            assert (await c.post('/api/grok_bridge/config',json={'enabled':False})).status_code==200
            assert (await c.post('/api/grok_bridge/evaluate',json={})).json()['execution_gate_mode']=='ADVISORY_ONLY'
            assert (await c.post('/api/grok_bridge/config',json={'enabled':True},headers={'Origin':'https://evil.example'})).status_code==403
    asyncio.run(run())

def test_cache_expires_when_source_stops(bridge,packet):
    packet['source_ts']=time.time()-500
    bridge.persist(g.local_classify(packet,bridge.rules),packet,time.perf_counter())
    assert bridge.get_cached_grok_regime()['stale'] is True

def test_live_api_valid_and_malformed_responses(packet,rules,monkeypatch):
    import io
    monkeypatch.setenv('XAI_API_KEY','fake-key')
    payload=g.local_classify(packet,rules)
    monkeypatch.setattr(g.urllib.request,'urlopen',lambda *a,**k:io.BytesIO(json.dumps({'choices':[{'message':{'content':json.dumps(payload)}}]}).encode()))
    assert g.classify_8600_signal(packet,rules=rules)['source']=='GROK_LIVE_API'
    monkeypatch.setattr(g.urllib.request,'urlopen',lambda *a,**k:io.BytesIO(b'invalid-json'))
    assert g.classify_8600_signal(packet,rules=rules)['source']=='LOCAL_DETERMINISTIC_FALLBACK'

def test_extractor_flip_counter_matches_existing_minute_rule(tmp_path,rules):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    import pandas as pd
    start=datetime(2026,11,6,9,15,tzinfo=ZoneInfo('Asia/Kolkata')).timestamp()
    closes=[100+(i%2)*2 for i in range(330)]
    path=tmp_path/'market.sqlite3'
    with sqlite3.connect(path) as db:
        db.executescript('CREATE TABLE candles_1m(sec_id TEXT,ts INTEGER,o REAL,h REAL,l REAL,c REAL);CREATE TABLE chain_snap(expiry TEXT,ts INTEGER,ce_oi REAL,pe_oi REAL);CREATE TABLE alerts(ts INTEGER,kind TEXT,side TEXT,title TEXT);')
        db.executemany('INSERT INTO candles_1m VALUES (?,?,?,?,?,?)',[('13',start+i*60,c,c+1,c-1,c) for i,c in enumerate(closes)])
    packet=g.build_8600_to_8510_feature_packet(path,now=start+330*60,rules=rules)
    series=pd.Series(closes);signs=(series-series.ewm(span=8,adjust=False).mean()).tail(12).apply(lambda v:1 if v>=0 else -1)
    assert packet['flips_12bars']==sum(a!=b for a,b in zip(signs.iloc[:-1],signs.iloc[1:]))
    class Volume:
        def get_latest_vsr(self):return dict(available=True,bar_ts=start,slot_rvol_median=5,bar_accel_ratio=4)
    p=g.build_8600_to_8510_feature_packet(path,Volume(),now=start+330*60,rules=rules)
    assert p['slot_rvol_median'] is None and p['volume_status']=='MISSING_OR_UNALIGNED'
