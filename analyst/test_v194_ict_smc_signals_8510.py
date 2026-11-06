"""No broker calls; all mutations use temporary state or isolated test worktree."""
import asyncio,json,sqlite3
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
import pytest,httpx
from analyst.ict_smc_signals_8510 import SignalBridge,GROUPS,classify_rows,pece_groups
from analyst.terminal8510_backend import QuantTerminalBackend
ROOT=Path(__file__).resolve().parents[1]

def cfg():
 return {'enabled':True,'cache_seconds':0,'timeout_seconds':.1,'base_url':'http://127.0.0.1:8600','max_source_age_seconds':300,'timeframe':'3m','significance':{k:True for k in GROUPS}}

def row(ts='10:00',**kw):
 return dict(ts=ts,close=23000.,pe_day=10000000.,ce_day=1000000.,pe_chg=3000000.,ce_chg=0.,pe_ce_day=9000000.,pe_ce_chg=3000000.,read=['Put writing','bull'],pro=['','bull','●●',''],**kw)

def test_paper_and_algo_gates_permanent():
 b=object.__new__(QuantTerminalBackend);b.cfg={};b.paper_execution_enabled=False;b.algo_only_execution=False;b.execution_mode='ALGO_LIVE_ONLY'
 assert b.paper_execution_enabled is True and b.algo_only_execution is True and b.execution_mode=='PAPER_ONLY'

def test_real_money_session_only_and_no_go(tmp_path):
 b=object.__new__(QuantTerminalBackend);b.cfg={'execution_safety_v194':{'live_validation_status':'NO_GO','typed_confirmation':'ENABLE REAL MONEY'}};b.config_path=tmp_path/'config.json'
 assert not b.configure_real_money(True,'ENABLE REAL MONEY')['ok'];assert b.execution_mode=='PAPER_ONLY'
 b.cfg['execution_safety_v194']['live_validation_status']='APPROVED'
 assert not b.configure_real_money(True,'wrong')['ok']
 assert b.configure_real_money(True,'ENABLE REAL MONEY')['ok'] and b.paper_execution_enabled
 assert json.loads(b.config_path.read_text())['real_money_enabled'] is False
 b.execution_mode='PAPER_ONLY';assert b.execution_mode=='PAPER_ONLY'

def test_forged_http_orders_rejected_and_legacy_mode_forbidden():
 asyncio.run(_forged_http())

async def _forged_http():
 from engine_8510_live import app
 async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://testserver') as c:
  for p in ('/api/order/submit','/api/orders/place','/api/paper/order'):
   r=await c.post(p,json={'side':'BUY','is_algo_trade':True});assert r.status_code==403
  for payload in ({'algo_only':False},{'paper_enabled':False},{'mode':'ALGO_LIVE_ONLY'}):assert (await c.post('/api/mode',json=payload)).status_code==400
  assert (await c.post('/api/ict-smc-signals/config',json={'use_for_algo_trades':True})).status_code==409

def test_manual_ui_paths_removed_selection_retained():
 h=(ROOT/'Algorithmic_Trading_Workspace_8510.html').read_text()
 for x in ('submitRouterOrder','submitMatrixOrder','chkAlgoOnlySwitch','chkPaperExecutionSwitch','toggleAlgoOnly','togglePaperExecutionSwitch'):assert x not in h
 assert 'toggleAlgoStrike' in h or 'toggleStrikeSelection' in h
 assert 'ICT SMC Desk Signals' in h

@pytest.mark.parametrize('fetch',[lambda ep: (_ for _ in ()).throw(OSError('unreachable')),lambda ep: {'rows':[{}]}])
def test_outage_or_malformed_fail_closed_and_log_every_group(tmp_path,fetch):
 b=SignalBridge(cfg(),tmp_path/'log.sqlite',fetch)
 result=b.evaluate(datetime(2026,11,6,10,5,tzinfo=ZoneInfo('Asia/Kolkata')))
 assert len(result['rows'])==len(GROUPS)
 assert all(r['verdict']=='UNAVAILABLE' and r['direction']=='NEUTRAL' and not r['significant'] for r in result['rows'])
 with sqlite3.connect(b.db_path) as db:assert db.execute('select count(*) from ict_smc_signal_log where significant=0').fetchone()[0]==len(GROUPS)
 assert result['use_for_algo_trades'] is False

def test_stale_and_previous_day_rejected():
 now=datetime(2026,11,6,10,5,tzinfo=ZoneInfo('Asia/Kolkata'))
 with pytest.raises(ValueError):pece_groups({'day':'2026-11-05','rows':[row()]},now,cfg())
 with pytest.raises(ValueError):pece_groups({'day':'2026-11-06','rows':[row('09:30')]},now,cfg())

def test_config_significance_controls_display_only():
 now=datetime(2026,11,6,10,5,tzinfo=ZoneInfo('Asia/Kolkata'));c=cfg();payload={'day':'2026-11-06','rows':[row()]}
 before=pece_groups(payload,now,c);c['significance']={k:False for k in GROUPS};after=pece_groups(payload,now,c)
 assert any(x['significant'] for x in before) and not any(x['significant'] for x in after)
 assert [x['verdict'] for x in before]==[x['verdict'] for x in after]

def test_no_lookahead_and_ambiguity():
 from scripts.backtest_ict_smc_signals_8510 import score_path
 import pandas as pd
 prefix=[row('10:00'),row('09:57')];a=classify_rows(prefix)
 future=row('15:00');future['pe_day']=1e12
 assert classify_rows(prefix)==a  # no global mutable state from later evaluations
 classify_rows([future]+prefix);assert classify_rows(prefix)==a
 assert score_path(pd.DataFrame([dict(ts=0,o=100,h=125,l=75,c=100)]),100,1,20,20)['status']=='AMBIGUOUS'

def test_research_tariff_and_control_contract():
 s=(ROOT/'scripts/backtest_ict_smc_signals_8510.py').read_text();q=json.loads((ROOT/'analyst/ict_smc_research_8510.json').read_text())
 assert 'calculate_trade_charges' in s and 'master_lot' in s and 'opposite' in s and 'CHAIN COVERAGE FOR THIS BACKTEST' in s
 assert q['measured_delta_scenarios']==[.59,.67] and q['hypotheses']==128
 assert '400.0' not in s and '0.50' not in s

def test_classifier_port_evidence():
 from analyst.verify_ict_smc_parity_8510 import verify
 evidence=verify()
 assert evidence['recorded_timestamps']>=200 and min(evidence['agreement'].values())>=95
 assert evidence['mismatches']==[]
