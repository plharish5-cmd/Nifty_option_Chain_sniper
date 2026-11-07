"""Private-state adapter tests. No provider or broker traffic; no source config writes."""

import pytest
pytest.skip("Retired in v1.9.6; replacement coverage is test_v196/test_v197", allow_module_level=True)
import asyncio
import json
import sqlite3
import subprocess
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import httpx
import pytest
from analyst import stockmojo_safe_poller_8510 as s
ROOT=Path(__file__).resolve().parents[1]
NOW=datetime(2026,11,6,12,0,tzinfo=s.IST).timestamp()

@pytest.fixture
def rules():return json.loads(s.RULES.read_text())

@pytest.fixture
def env():return {'STOCKMOJO_JWT_J':'fake-j-never-log','STOCKMOJO_TOKEN_T':'fake-t-never-log','STOCKMOJO_REQUEST_JSON':json.dumps({'symbol':1,'expiry':'TEST-ONLY','range':10})}

@pytest.fixture
def payload():
    return {'t':[datetime.fromtimestamp(NOW-180+i*60,s.IST).isoformat() for i in range(4)],'co':[100]*4,'po':[50,60,70,80],'f':[25000,25001,25002,25003]}

@pytest.fixture
def poller(tmp_path,rules,env):
    p=tmp_path/'rules.json';p.write_text(json.dumps(rules))
    return s.StockMojoPoller(tmp_path/'ledger.sqlite3',p,tmp_path/'errors.log',environ=env)

def response(payload,code=200,headers=None):
    return SimpleNamespace(status_code=code,headers=headers or {},json=lambda:payload)

def test_exact_request_and_header_contract(poller,payload):
    calls=[]
    poller.transport=lambda *a,**kw:(calls.append((a,kw)) or response(payload))
    poller.poll_once(NOW)
    a,k=calls[0]
    assert a[0]=='https://data.stockmojo.in/live/multi-strike/ce-pe-oi-diff'
    assert k['headers']['j']=='fake-j-never-log' and k['headers']['t']=='fake-t-never-log'
    assert k['headers']['origin']=='https://stockmojo.in'
    assert k['json']=={'symbol':1,'expiry':'TEST-ONLY','range':10}
    assert k['allow_redirects'] is False
    assert 'fake-j' not in json.dumps(poller.snapshot(NOW))

def test_recompute_totals_pcr_and_timestamp_spaced_slopes(payload):
    rows=s.normalize_payload(payload);r=rows[-1]
    assert r['pe_ce_oi_total']==-20 and r['pcr_total']==.8
    assert r['pe_ce_change_1m']==10 and r['pe_ce_change_3m']==30
    assert r['pe_ce_slope_3m']==10 and r['sentiment']=='PE_CE_RISING'
    sparse={k:[v[0],v[-1]] for k,v in payload.items()}
    r=s.normalize_payload(sparse)[-1]
    assert r['pe_ce_change_1m'] is None and r['pe_ce_change_3m']==30

def test_named_export_undated_is_not_live(payload):
    rows=[{'Time':'12:00','Put OI Total':80,'Call OI Total':100,'PCR Total':9,'Future LTP':25003},
          {'Time':'Prev Day','Put OI Total':70,'Call OI Total':120}]
    with pytest.raises(ValueError):s.normalize_payload(rows)
    r=s.normalize_payload(rows,session_date='2026-11-06')[-1]
    assert r['source_date_verified']==0 and r['pcr_total']==.8

@pytest.mark.parametrize('bad',[{'t':[], 'co':[1], 'po':[]},{'t':['12:00'],'co':[1],'po':[1]},{'t':[NOW],'co':[-1],'po':[1]},{'t':[NOW],'co':[float('nan')],'po':[1]},[],{'unrecognised':True}])
def test_bad_schema_or_units_rejected(bad):
    with pytest.raises((ValueError,KeyError)):s.normalize_payload(bad)

@pytest.mark.parametrize('when,expected',[
    ('2026-11-06T09:14:59',False),('2026-11-06T09:15:00',True),('2026-11-06T15:29:59',True),
    ('2026-11-06T15:30:00',False),('2026-11-07T12:00:00',False),('2026-11-08T12:00:00',False)])
def test_market_boundary_and_weekends(rules,when,expected):
    stamp=datetime.fromisoformat(when).replace(tzinfo=s.IST).timestamp()
    assert s.market_open(stamp,rules)==expected
    assert not s.market_open(NOW,rules,holidays=['2026-11-06'])

def test_jitter_and_server_backoff(rules):
    class Low:
        def uniform(self,a,b):return a
    class High:
        def uniform(self,a,b):return b
    assert s.next_delay(rules,rng=Low())==170
    assert s.next_delay(rules,rng=High())==195
    assert s.next_delay(rules,1)==360
    assert s.next_delay(rules,20)==3600
    assert s.next_delay(rules,1,retry_after='7200',now=NOW)==7200
    assert s.next_delay(rules,1,retry_after='Fri, 06 Nov 2026 08:30:00 GMT',now=NOW)==7200

@pytest.mark.parametrize('code',[401,403])
def test_access_denial_stops_requests_without_bypass(poller,code):
    calls=[];poller.transport=lambda *a,**kw:(calls.append(1) or response(None,code))
    poller.poll_once(NOW);poller.poll_once(NOW+200)
    assert len(calls)==1 and poller.snapshot(NOW)['halted']
    assert poller.snapshot(NOW)['poll_status']=='ACCESS_DENIED_'+str(code)

@pytest.mark.parametrize('code',[429,503])
def test_rate_limit_and_unavailable_respect_retry_after(poller,code):
    poller.transport=lambda *a,**kw:response(None,code,{'Retry-After':'7200'})
    assert poller.poll_once(NOW)==7200
    assert not poller.snapshot(NOW)['available']

def test_missing_auth_body_and_closed_market_make_no_request(poller):
    poller.transport=lambda *a,**kw:pytest.fail('must not call provider')
    poller.environ.pop('STOCKMOJO_JWT_J')
    poller.poll_once(NOW);assert poller.snapshot(NOW)['configuration_status']=='MISSING_CREDENTIALS'
    poller.environ['STOCKMOJO_JWT_J']='fake'
    poller.environ.pop('STOCKMOJO_REQUEST_JSON')
    poller.poll_once(NOW);assert poller.snapshot(NOW)['configuration_status']=='REQUEST_BODY_REQUIRED'
    poller.poll_once(datetime(2026,11,7,12,tzinfo=s.IST).timestamp())
    assert poller.snapshot(NOW)['poll_status']=='MARKET_CLOSED'

def test_upsert_cached_read_and_expiry(poller,payload):
    poller.transport=lambda *a,**kw:response(payload)
    poller.poll_once(NOW);poller.poll_once(NOW+1)
    with poller.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM stockmojo_pe_ce_live').fetchone()[0]==4
    with patch.object(poller,'connect',side_effect=AssertionError('memory read cannot query SQL')),patch.object(poller,'request_parts',side_effect=AssertionError('memory read cannot parse credentials')):
        r=poller.snapshot(NOW)
        assert r['available'] and r['bias']=='BULLISH' and r['pe_ce_oi_total']<0
        assert not poller.snapshot(NOW+241)['available']

def test_stale_future_or_wrong_day_response_not_persisted(poller,payload):
    payload['t']=[datetime.fromtimestamp(NOW-86400+i*60,s.IST).isoformat() for i in range(4)]
    poller.transport=lambda *a,**kw:response(payload)
    poller.poll_once(NOW)
    with poller.connect() as db:assert db.execute('SELECT COUNT(*) FROM stockmojo_pe_ce_live').fetchone()[0]==0
    assert not poller.snapshot(NOW)['available']

def test_exception_log_redacts_secrets(poller):
    def failing(*a,**kw):raise s.requests.RequestException('fake-j-never-log fake-t-never-log')
    poller.transport=failing
    assert poller.poll_once(NOW)==360
    for handler in poller.logger.handlers:handler.flush()
    text=(poller.ledger_db.parent/'errors.log').read_text()
    assert 'fake-j' not in text and 'fake-t' not in text and 'RequestException' in text

def test_single_source_pillar4_and_stale_fail_closed(poller,payload,monkeypatch):
    from analyst import terminal8510_backend as b
    cfg=json.loads((ROOT/'analyst/terminal8510_config.json').read_text())
    monkeypatch.setattr(b.time,'time',lambda:NOW)
    poller.transport=lambda *a,**kw:response(payload);poller.poll_once(NOW)
    stock=poller.snapshot(NOW)
    trend={'fresh':True,'timeframe':'15m','side':'CE'}
    contrary={'source':'DHAN_API','status':'OK','available':True,'bias':'BEARISH','timestamp_epoch':NOW}
    r=b.compute_five_factor_confluence(trend,contrary,{},cfg,stock)
    assert r['source']=='STOCKMOJO_API' and r['pillar_4_option_writer_flow'] is True
    assert r['score']==1  # one OI leg, not two
    stale={**stock,'timestamp_epoch':NOW-300}
    assert not b.compute_five_factor_confluence(trend,contrary,{},cfg,stale)['pillar_4_option_writer_flow']
    unconfigured={**stock,'configured':False}
    assert b.compute_five_factor_confluence(trend,contrary,{},cfg,unconfigured)['source']=='DHAN_API'

def test_readonly_api_and_telemetry_metadata(poller,payload,monkeypatch):
    import engine_8510_live as engine
    poller.transport=lambda *a,**kw:response(payload);poller.poll_once(NOW)
    monkeypatch.setattr(engine.backend,'stockmojo_poller',poller,raising=False)
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=engine.app),base_url='http://testserver') as client:
            r=await client.get('/api/stockmojo/live');assert r.status_code==200
            assert r.json()['source']=='STOCKMOJO_API' and 'fake-j' not in r.text
            assert (await client.post('/api/stockmojo/live',json={})).status_code==405
    asyncio.run(run())
    assert 'data["stockmojo_live"] = backend.get_stockmojo_live()' in (ROOT/'engine_8510_live.py').read_text()

def test_source_config_unchanged_and_locks():
    old=json.loads(subprocess.check_output(['git','show','1e9fbff:analyst/terminal8510_config.json'],cwd=ROOT,text=True))
    assert json.loads((ROOT/'analyst/terminal8510_config.json').read_text())==old
    p=subprocess.run(['python3','scripts/verify_locks.py'],cwd=ROOT,capture_output=True,text=True)
    assert p.returncode==0 and '11' in p.stdout
    assert 'pillStockMojoLive' in (ROOT/'Algorithmic_Trading_Workspace_8510.html').read_text()

def test_execution_functions_unchanged_by_source_adapter():
    import ast
    old=subprocess.check_output(['git','show','1e9fbff:analyst/terminal8510_backend.py'],cwd=ROOT,text=True)
    new=(ROOT/'analyst/terminal8510_backend.py').read_text()
    def functions(text):return {node.name:ast.dump(node) for node in ast.walk(ast.parse(text)) if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef))}
    before,after=functions(old),functions(new)
    intended={'compute_five_factor_confluence','get_five_factor_confluence'}
    assert all(after[name]==body for name,body in before.items() if name not in intended)

@pytest.mark.parametrize('body',[{'symbol':2,'expiry':'x','range':10},{'symbol':True,'expiry':'x','range':10},{'symbol':1,'expiry':None,'range':10},{'symbol':1,'expiry':'x','range':0},{'symbol':1,'expiry':'x','range':10,'j':'must-not-be-in-body'}])
def test_request_scope_and_secrets_fail_closed(poller,body):
    poller.environ['STOCKMOJO_REQUEST_JSON']=json.dumps(body)
    with pytest.raises(ValueError):poller.request_parts()
