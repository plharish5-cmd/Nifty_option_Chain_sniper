"""Institutional telemetry tests: generated fixtures, read-only owner sample, tmp_path writes only."""
import ast
import asyncio
import copy
import csv
import json
import subprocess
from datetime import datetime,timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import httpx
import pytest
from analyst import options_telemetry_engine as e
from analyst import institutional_qwen_bridge_8510 as q
ROOT=Path(__file__).resolve().parents[1]

@pytest.fixture
def rows():
 out=[]
 for i in range(100):
  out.append({'time':(datetime(2026,11,6,9,15)+timedelta(minutes=i)).strftime('%H:%M'),
    'put_oi':10000000+i*200000,'call_oi':5000000,'put_volume':100000,'call_volume':100000,
    'pcr':2+i*.01,'future_ltp':24000+i*2,'future_oi':10000000+i*50000,
    'put_oi_day_chg':i*200000,'call_oi_day_chg':0})
 return out

def canon(rows):return [e.canonical(r) for r in rows]

def test_01_window_lengths_off_by_one_fixed(rows):
 assert e.WINDOWS==(3,5,15,30,60) and e.MIN_HISTORY==61
 for m in (5,15,30,60):
  w=e.window_features(canon(rows),m)
  assert w['observations']==m+1 and w['span_minutes']==m
  assert w['price_change']==m*2 and w['put_volume']==100000*m
  assert w['price_range']==m*2 and w['pe_ce_oi_change']==m*200000


def test_02_latest_schema_and_optional_block(rows):
 r=e.build_telemetry(rows);latest=r['input_telemetry']['latest']
 for key in ('future_ltp','future_oi','put_oi','call_oi','put_volume','call_volume','pcr','pe_ce_oi_total','pe_ce_oi_change','pe_ce_oi_day_total'):assert key in latest
 assert latest['pe_ce_oi_change']==200000
 assert latest['pe_ce_oi_day_total']==99*200000
 assert r['input_telemetry']['optional']=={'premium_skew':None,'strike_level_data':None}
 for key in ('pe_ce_oi_change_5m','pe_ce_oi_change_15m','pe_ce_oi_change_30m','pe_ce_oi_change_60m','volume_speed_5m','volume_speed_15m','futures_price_range_15m','futures_price_range_30m','partial_bar_flag','stale_data_seconds'):assert key in r and key in r['input_telemetry']


def test_03_partial_bar_discard_on_2026_11_07_1540():
 path=Path('/home/harish/Downloads/PE_CE_NIFTY_2026-11-07.csv')
 if not path.exists():pytest.skip('Owner-supplied read-only sample is unavailable')
 r=e.build_telemetry(e.load_completed_rows(path));quality=r['input_telemetry']['data_quality']
 assert r['timestamp']=='15:39' and quality['partial_bar_flag'] and quality['partial_latest_row_discarded'] and quality['completed_bar_used']
 assert quality['partial_volume_ratio']==pytest.approx(124020/9632675)
 assert quality['discarded_row_timestamp']=='15:40' and quality['last_input_volume']==124020 and quality['prior_volume_median']==9632675
 assert quality['mode']=='HISTORICAL_FILE' and not quality['source_date_verified']

@pytest.mark.parametrize('failure',['stale','partial','missing','nan','gap','short','live_date','invalid_time','empty','zero_volume_baseline','overflow'])
def test_04_data_quality_gate_emits_data_unreliable(rows,failure):
 kw={}
 if failure=='stale':kw['reference_now']=datetime(2026,11,6,12,tzinfo=e.IST)
 if failure=='partial':rows[-1].update(put_volume=1,call_volume=1);kw['discard_partial_bar']=False
 if failure=='missing':rows[-1].pop('future_oi')
 if failure=='nan':rows[-1]['pcr']=float('nan')
 if failure=='gap':rows.pop(-3)
 if failure=='short':rows=rows[:60]
 if failure=='empty':rows=[]
 if failure=='zero_volume_baseline':
  for row in rows:row.update(put_volume=0,call_volume=0)
 if failure=='overflow':
  for row in rows:row.update(put_volume=1e308,call_volume=1e308)
 if failure=='live_date':kw['live']=True
 if failure=='invalid_time':rows.append({**rows[-1],'time':'bad'})
 r=e.build_telemetry(rows,**kw)
 assert r['data_status']=='DATA_UNRELIABLE' and r['day_regime']=='DATA_UNRELIABLE'
 assert r['bias_score_pct']==0 and r['confidence_pct']<=20 and r['warnings']
 assert r['summary']=='Data unreliable; no directional conclusion.'

@pytest.mark.parametrize('bearish',[False,True])
def test_05_directional_volume_scoring_bearish_vs_bullish(rows,bearish):
 if bearish:
  for i,row in enumerate(rows):row.update(future_ltp=25000-i*2,put_oi=5000000,call_oi=10000000+i*200000,pcr=1-i*.005,put_oi_day_chg=0,call_oi_day_chg=i*200000)
 for row in rows[-15:]:row.update(put_volume=250000,call_volume=250000)
 r=e.build_telemetry(rows);parts=r['confirmation']['score_components']
 assert r['participation']['volume_speed']>=1.5
 assert parts['volume_points']==(-10 if bearish else 10)
 assert (r['bias_score_pct']<0)==bearish
 assert sum(parts.values())==pytest.approx(r['bias_score_pct'])


def test_06_transition_reversal_watch_requires_5m_persistence():
 def series(new_count):
  rows=[];net=0;price=25000
  for i in range(80+new_count):
   net+=-100000 if i<80 else 500000;price+=-1 if i<80 else 10
   rows.append({'time':(datetime(2026,11,6,9,15)+timedelta(minutes=i)).strftime('%H:%M'),'put_oi':30000000+net,'call_oi':30000000,
    'put_volume':100000,'call_volume':100000,'pcr':1+net/100000000,'future_ltp':price,'future_oi':10000000+i*50000})
  return rows
 single=e.build_telemetry(series(1));persistent=e.build_telemetry(series(8))
 assert single['day_regime']!='TRANSITION_REVERSAL_WATCH'
 assert persistent['day_regime']=='TRANSITION_REVERSAL_WATCH'
 assert persistent['confirmation']['transition_persistence_proven']
 broken=series(8);broken.pop(-3)
 assert e.build_telemetry(broken)['day_regime']!='TRANSITION_REVERSAL_WATCH'


def test_07_confidence_availability_penalties_and_crowding_rule(rows):
 a=e.build_telemetry(rows);b=e.build_telemetry(rows,premium_skew={'observed':True},strike_level_data={'observed':True})
 assert a['confirmation']['confidence_penalties']['premium_skew']==8 and a['confirmation']['confidence_penalties']['strike_level_data']==7
 assert b['confidence_pct']-a['confidence_pct']==15
 assert a['oi_dominance']['crowding_risk'] and e.CROWDING_WARNING in a['warnings']


def test_08_proxy_wording_never_claims_confirmed_writers_without_strike_data(rows):
 r=e.build_telemetry(rows);text=json.dumps(r).lower()
 assert 'put-side oi dominance' in text and 'oi-flow proxy' in text
 assert 'confirmed put writers' not in text and 'confirmed call writers' not in text


def test_09_qwen_local_adapter_and_offline_fallback(rows):
 deterministic=e.build_telemetry(rows);calls=[]
 def local(url,**kwargs):
  calls.append((url,kwargs));return SimpleNamespace(status_code=200,json=lambda:{'response':json.dumps({k:deterministic[k] for k in q.REQUIRED})})
 interpreted,status=q.interpret_local(deterministic,local)
 assert status=='OK' and interpreted['interpreter_source']=='QWEN_2.5_3B_LOCAL'
 url,k=calls[0];assert url=='http://127.0.0.1:11434/api/generate'
 assert k['json']['format']=='json' and k['json']['stream'] is False and k['json']['options']['temperature']==0
 assert k['timeout']==3.0 and k['json']['model']=='qwen2.5:3b'
 for key in ('timestamp','data_quality','latest','windows','optional'):assert key in json.loads(k['json']['prompt'])
 def offline(*a,**kw):raise q.requests.ConnectionError('offline')
 fallback,status=q.interpret_local(deterministic,offline)
 assert fallback['interpreter_source']=='DETERMINISTIC_PYTHON_ENGINE' and fallback['bias_score_pct']==deterministic['bias_score_pct']


def test_10_zero_vendor_strings_and_locks_intact():
 forbidden=('stockmojo','grok','stockmodule.com')
 for name in ('analyst/options_telemetry_engine.py','analyst/institutional_qwen_bridge_8510.py','analyst/terminal8510_backend.py','engine_8510_live.py','Algorithmic_Trading_Workspace_8510.html','analyst/institutional_telemetry_rules_8510.json'):
  text=(ROOT/name).read_text().lower();assert all(word not in text for word in forbidden),name
 result=subprocess.run(['python3','scripts/verify_locks.py'],cwd=ROOT,capture_output=True,text=True);assert result.returncode==0 and '11' in result.stdout
 # Every original backend function is unchanged; advisory integration is in new methods only.
 old=subprocess.check_output(['git','show','606332e:analyst/terminal8510_backend.py'],cwd=ROOT,text=True)
 def funcs(text):return {n.name:ast.dump(n) for n in ast.walk(ast.parse(text)) if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))}
 before,after=funcs(old),funcs((ROOT/'analyst/terminal8510_backend.py').read_text());assert all(after[k]==v for k,v in before.items())

@pytest.mark.parametrize('suffix',['csv','tsv','json'])
def test_file_formats_and_private_ledger(tmp_path,rows,suffix):
 path=tmp_path/('input.'+suffix)
 if suffix=='json':path.write_text(json.dumps(rows))
 else:
  with path.open('w',newline='') as h:
   writer=csv.DictWriter(h,fieldnames=rows[0].keys(),delimiter='\t' if suffix=='tsv' else ',');writer.writeheader();writer.writerows(rows)
 assert len(e.load_completed_rows(path))==len(rows)
 bridge=q.InstitutionalTelemetryBridge(tmp_path/'ledger.sqlite3')
 result=bridge.ingest_file(path,use_local=False)
 assert not result['available'] and result['execution_gate_mode']=='ADVISORY_ONLY'
 with bridge.connect() as db:assert db.execute('SELECT COUNT(*) FROM institutional_pe_ce_telemetry').fetchone()[0]==1
 with patch.object(bridge,'connect',side_effect=AssertionError('cache cannot query SQL')):assert bridge.snapshot()['timestamp']==result['timestamp']


def test_local_invariants_reject_changed_math_and_unsupported_claims(rows):
 r=e.build_telemetry(rows);candidate={k:copy.deepcopy(r[k]) for k in q.REQUIRED}
 candidate['bias_score_pct']+=1
 with pytest.raises(ValueError):q.validate_interpretation(candidate,r)
 candidate['bias_score_pct']=r['bias_score_pct'];candidate['summary']='confirmed put writers'
 with pytest.raises(ValueError):q.validate_interpretation(candidate,r)


def test_optional_chain_same_observation(rows):
 base=datetime(2026,11,6,9,15,tzinfo=e.IST)
 for i,row in enumerate(rows):row['time']=(base+timedelta(minutes=i)).isoformat()
 stamp=(base+timedelta(minutes=99)).timestamp()
 snap={'timestamp':stamp,'ce_ltp':100,'pe_ltp':120,'ce_oi':100000,'pe_oi':200000,'strike':24200}
 r=e.build_telemetry(rows,reference_now=stamp,live=True,chain_snapshot=snap)
 assert r['data_status']=='VALID' and r['confirmation']['premium_skew_status']=='AVAILABLE'
 snap['timestamp']+=30
 assert e.build_telemetry(rows,reference_now=stamp,live=True,chain_snapshot=snap)['confirmation']['premium_skew_status']=='NOT_AVAILABLE'


def test_local_routes_no_order_authorization(tmp_path,rows,monkeypatch):
 import engine_8510_live as app
 path=tmp_path/'history.json';path.write_text(json.dumps(rows));bridge=q.InstitutionalTelemetryBridge(tmp_path/'ledger.sqlite3')
 monkeypatch.setattr(app.backend,'institutional_telemetry_bridge',bridge,raising=False)
 async def run():
  async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app,client=('127.0.0.1',1)),base_url='http://testserver') as client:
   resp=await client.post('/api/institutional_telemetry/ingest_file',json={'path':str(path),'use_local':False});assert resp.status_code==200
   assert resp.json()['execution_gate_mode']=='ADVISORY_ONLY' and not resp.json()['available']
   assert (await client.get('/api/institutional_telemetry/live')).status_code==200
   assert (await client.get('/api/institutional_telemetry/qwen_status')).json()['model']=='qwen2.5:3b'
   assert (await client.post('/api/institutional_telemetry/ingest_file',json={'path':str(path)},headers={'Origin':'https://foreign.example'})).status_code==403
 asyncio.run(run())


def test_deterministic_evidence_available_while_local_model_waits(tmp_path,rows):
 import threading
 from concurrent.futures import ThreadPoolExecutor
 path=tmp_path/'input.json';path.write_text(json.dumps(rows));entered=threading.Event();release=threading.Event()
 def delayed(*a,**kw):
  entered.set();release.wait(5);raise q.requests.Timeout('unit-test timeout')
 bridge=q.InstitutionalTelemetryBridge(tmp_path/'ledger.sqlite3',transport=delayed)
 with ThreadPoolExecutor(max_workers=1) as pool:
  future=pool.submit(bridge.ingest_file,path)
  assert entered.wait(3)
  try:
   assert bridge.snapshot()['interpreter_source']=='DETERMINISTIC_PYTHON_ENGINE'
   with bridge.connect() as db:assert db.execute('SELECT COUNT(*) FROM institutional_pe_ce_telemetry WHERE deterministic_json IS NOT NULL').fetchone()[0]==1
  finally:release.set()
  assert future.result(timeout=5)['interpreter_source']=='DETERMINISTIC_PYTHON_ENGINE'


def test_advisory_overlay_does_not_break_missing_base_context(monkeypatch):
 from analyst.terminal8510_backend import QuantTerminalBackend
 backend=object.__new__(QuantTerminalBackend)
 monkeypatch.setattr(backend,'get_institutional_telemetry_cached',lambda:{'available':True,'data_status':'VALID','bias_score_pct':-50,'confirmation':{'score_components':{'pe_ce_points':15}}})
 assert backend.institutional_advisory_confluence(None)=={}
 base={'direction':'CE','pillars':{'pillar_4_option_writer_flow':{'passed':False,'label':'Dhan OI'}}}
 result=backend.institutional_advisory_confluence(base)
 assert result['score']==1 and result['execution_authorized'] is False
 assert base['pillars']['pillar_4_option_writer_flow']['passed'] is False


def test_transition_can_use_thirty_minute_prior_at_minimum_history():
 rows=[];net=0;price=25000
 for i in range(61):
  net+=-1000000 if i<=40 else -100000 if i<=52 else 500000
  price+=-1 if i<=52 else 10
  rows.append({'time':(datetime(2026,11,6,9,15)+timedelta(minutes=i)).strftime('%H:%M'),'put_oi':100000000+net,'call_oi':100000000,
   'put_volume':100000,'call_volume':100000,'pcr':1+net/200000000,'future_ltp':price,'future_oi':10000000+i*50000})
 result=e.build_telemetry(rows)
 assert result['data_status']=='VALID'
 assert result['day_regime']=='TRANSITION_REVERSAL_WATCH'


def test_http_advisory_does_not_mutate_execution_cache(monkeypatch):
 import engine_8510_live as engine
 payload={'five_factor_confluence':{'direction':'CE','pillars':{'pillar_4_option_writer_flow':{'passed':False,'label':'Dhan OI'}}}}
 async def telemetry():return payload
 monkeypatch.setattr(engine.backend,'get_live_telemetry',telemetry)
 monkeypatch.setattr(engine.backend,'get_institutional_telemetry_cached',lambda:{'available':True,'data_status':'VALID','confirmation':{'score_components':{'pe_ce_points':15}}})
 async def run():
  async with httpx.AsyncClient(transport=httpx.ASGITransport(app=engine.app),base_url='http://testserver') as client:
   result=(await client.get('/api/telemetry')).json()
   assert result['five_factor_confluence']['pillar_4_option_writer_flow'] is True
 asyncio.run(run())
 assert payload['five_factor_confluence']['pillars']['pillar_4_option_writer_flow']['passed'] is False
 assert 'institutional_telemetry_live' not in payload
