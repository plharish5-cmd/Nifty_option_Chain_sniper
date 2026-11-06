import ast,sqlite3,json,subprocess
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
import pytest
from analyst.recorded_history_8510 import RecordedHistory
ROOT=Path(__file__).resolve().parents[1]

@pytest.fixture
def history(tmp_path):
 ledger=tmp_path/'ledger.db';market=tmp_path/'market.db'
 with sqlite3.connect(ledger) as db:
  db.execute('CREATE TABLE positions(position_id TEXT,symbol TEXT,security_id TEXT,status TEXT,mode TEXT,entry_time TEXT,exit_time TEXT,entry_price REAL,exit_price REAL,qty INTEGER,gross_pnl REAL,net_pnl REAL,strategy_setup TEXT,exit_reason TEXT,source TEXT)')
  db.execute("INSERT INTO positions VALUES ('real-paper','NIFTY 24000 CE','123','CLOSED','PAPER_ONLY','2026-11-05 10:15:00 IST','2026-11-05 11:00:00 IST',209,232,65,1495,1415,'Stage 2 8 EMA','TARGET','AUTO ALGO')")
 with sqlite3.connect(market) as db:
  db.execute('CREATE TABLE candles_1m(sec_id TEXT,ts INTEGER,o REAL,h REAL,l REAL,c REAL,v REAL)')
  ts=int(datetime(2026,11,5,9,15,tzinfo=ZoneInfo('Asia/Kolkata')).timestamp())
  db.execute("INSERT INTO candles_1m VALUES ('13',?,24000,24010,23990,24005,100)",(ts,))
  db.execute('CREATE TABLE chain_snap(expiry TEXT,ts INTEGER,strike REAL,ce_ltp REAL,pe_ltp REAL)')
  db.execute("INSERT INTO chain_snap VALUES ('2026-11-09',?,24000,209,232)",(ts,))
 return RecordedHistory(ledger,market)

def test_real_rows_not_calibrated(history):
 row=history.trades('2026-11-05','2026-11-05')[0]
 assert row['option_entry']==209 and row['option_exit']==232 and row['net_pnl']==1415
 assert row['Exact_Charges_INR']==80 and row['provenance']=='RECORDED_PAPER_LEDGER'
 assert row['EMA_Slope'] is None and row['PCR_At_Entry'] is None

def test_no_fake_date_or_synthetic_candles(history):
 d=history.daily('2026-11-05')
 assert len(d['spot_candles_1m'])==1 and d['future_candles_1m']==[]
 assert d['strategy_signals']=={} and len(d['option_chain'])==1
 assert history.daily('2026-11-04')['trades']==[] and history.daily('2026-11-04')['spot_candles_1m']==[]
 assert history.daily('2026-11-04')['status']=='NO_RECORDED_DATA'

def test_canned_json_not_a_source(history,tmp_path):
 (tmp_path/'2026-11-04.json').write_text(json.dumps({'trades':[{'option_entry':200,'net_pnl':1625}]}))
 assert history.trades('2026-11-04','2026-11-04')==[]

def test_report_does_not_invent_trailing_comparison(history):
 r=history.report('2026-11-04','2026-11-06')
 assert r['total_trades']==1 and r['recorded_net_pnl']==1415
 assert r['fixed_net_pnl'] is None and r['trailing_net_pnl'] is None and r['reconciliation_match'] is None

def test_legacy_both_not_certified_as_independent_fill(history):
 with sqlite3.connect(history.ledger) as db:db.execute("UPDATE positions SET mode='BOTH'")
 assert history.trades()==[]

def test_dates_come_only_from_observations(history):
 assert history.dates()==['2026-11-05']

def test_invalid_range_fails(history):
 with pytest.raises(ValueError):history.report('2026-11-06','2026-11-05')

def test_queries_leave_databases_unchanged(history):
 before=[p.read_bytes() for p in (history.ledger,history.market)]
 history.daily('2026-11-05');history.report('2026-11-01','2026-11-06')
 assert before==[p.read_bytes() for p in (history.ledger,history.market)]

def test_ui_and_server_no_embedded_trade_history():
 h=(ROOT/'Algorithmic_Trading_Workspace_8510.html').read_text();s=(ROOT/'analyst/terminal8510_backend.py').read_text()
 assert 'const allTrades = [' not in h and 'CALIBRATED_BENCHMARK_REPLAY' not in s
 assert "if date_str == \"2026-11-05\"" not in s
 assert '/api/history/recorded' in h and 'No recorded data' in h
 assert 'No recorded summary loaded' in h and 'const scoreNum = row.multi_factor_score || 85' not in h

def test_user_watchlist_decision_and_live_safety_preserved():
 cfg=json.loads((ROOT/'analyst/terminal8510_config.json').read_text())
 assert cfg['algo_strike_selection']['LONG_CE_ITM'] is False
 assert cfg['execution_safety_v194']['live_validation_status']=='NO_GO'

def test_live_execution_and_protection_functions_unchanged():
 path='analyst/terminal8510_backend.py'
 old=subprocess.check_output(['git','-C',str(ROOT),'show','615fa68:'+path],text=True)
 new=(ROOT/path).read_text()
 def source(text):
  tree=ast.parse(text);cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='QuantTerminalBackend')
  return {n.name:ast.get_source_segment(text,n) for n in cls.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))}
 a,b=source(old),source(new)
 for name in ('execute_order_with_chaser','calculate_dhan_charges','trigger_emergency_kill_switch','check_eod_square_off','configure_real_money','stop_live_trading'):
  assert a[name]==b[name],name

def test_config_patch_preserves_unrelated_preferences(tmp_path):
 from analyst.apply_v194d_config import apply
 p=tmp_path/'config.json';before={'super_trail':{'partial_exit_enabled':True},'execution_safety_v194':{'live_validation_status':'NO_GO'},'dhan':{'whitelisted_ip':'EXAMPLE'},'algo_strike_selection':{'LONG_CE_ITM':True,'LONG_PE_ITM':False}}
 p.write_text(json.dumps(before));apply(p);after=json.loads(p.read_text())
 assert after['super_trail']==before['super_trail'] and after['execution_safety_v194']==before['execution_safety_v194'] and after['dhan']==before['dhan']
 assert after['algo_strike_selection']=={'LONG_CE_ITM':False,'LONG_PE_ITM':False}
