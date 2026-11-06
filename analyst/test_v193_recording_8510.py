"""Forward recorder tests: no live credentials or order calls."""
import csv,io,json,sqlite3,time,ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from analyst.market_recorder_8510 import RecordingStore,MarketRecorder,coverage,stamp,EMA_COLUMNS
DAY='2026-11-06';NOW=stamp(DAY,'10:00')
@pytest.fixture
def store(tmp_path):return RecordingStore(tmp_path/'r.db',tmp_path/'csv',{'radar_targets_pts':[10,15,20,25,30],'export_max_days':90})
def ema(at='10:01:00',status='LIVE',tf='1m',day=DAY):
 d={k:None for k in EMA_COLUMNS};d.update(date=day,time_ist=at,timeframe=tf,status=status,ma4=102,ema8=100);return d
def sig(side='CE',tier=3):return dict(tier=tier,signal_name='TEST',direction=side,signal_price=100,nifty_spot_at_signal=100,micro_sl_price=95 if side=='CE' else 105,contract_security_id='9',contract_symbol='TEST',premium_at_signal=20)
def bar(n=0,h=110,l=99,c=108):return dict(ts=NOW+n*60,h=h,l=l,c=c)
def rows(s):return s.radar(DAY)['rows']
def contract():return dict(security_id='9',symbol='TEST',expiry=DAY,strike=100,opt_type='CE')
def test_01_append_not_overwrite(store):
 store.persist_ema([ema()]);e=ema();e['ma4']=999;store.persist_ema([e,ema('10:02:00')]);r=store.reload(DAY);assert len(r)==2 and r[0]['ma4']==102
@pytest.mark.parametrize('status',['PAUSED','STALE'])
def test_02_statuses(store,status):
 store.persist_ema([ema(status=status)]);assert store.reload(DAY)[0]['status']==status
def test_03_restart_dedupe(store):
 store.persist_ema([ema()]);r=RecordingStore(store.path,store.csv_dir,store.cfg);r.persist_ema([ema()]);assert len(r.reload(DAY))==1
 assert len(list(csv.DictReader((store.csv_dir/(DAY+'.csv')).open())))==1
def test_04_reload_two_days(store):
 store.persist_ema([ema(day='2026-11-04'),ema(day='2026-11-05'),ema()]);assert {r['date'] for r in store.reload(DAY)}=={'2026-11-05',DAY}
def test_05_streaming_exports(store):
 from openpyxl import load_workbook
 store.persist_ema([ema('10:02:00','PAUSED'),ema('10:01:00','STALE'),ema(tf='15m'),ema(tf='1H')])
 r=list(csv.DictReader(io.StringIO(''.join(store.export_csv('ema_proximity_history',DAY,DAY)))));assert [x['time_ist'] for x in r]==sorted(x['time_ist'] for x in r) and any(x['status']=='PAUSED' for x in r)
 f=store.export_xlsx('ema_proximity_history',DAY,DAY);wb=load_workbook(f,read_only=True);assert wb.sheetnames==['1m','15m','1H'];wb.close();f.close()
def test_06_limit(store):
 with pytest.raises(ValueError,match='90 days'):store.validate_range('2026-01-01',DAY)
def test_07_fire_fields(store):
 sid=store.fire(sig(),NOW);r=rows(store)[0];assert sid.startswith('RT_20261106100000_') and (r['nifty_spot_at_signal'],r['premium_at_signal'],r['micro_sl_price'],r['risk_points'])==(100,20,95,5)
@pytest.mark.parametrize('side,h,l,ext',[('CE',120,99,120),('PE',101,80,80)])
def test_08_directional_max(store,side,h,l,ext):
 store.fire(sig(side),NOW);store.update_bar(bar(h=h,l=l));assert rows(store)[0]['max_favourable_spot']==ext and rows(store)[0]['max_points_moved']==20
def test_09_keep_max_after_stop(store):
 store.fire(sig(),NOW);store.update_bar(bar(h=140,l=99,c=135));store.update_bar(bar(1,h=110,l=94,c=95));r=rows(store)[0];assert r['max_points_moved']==40 and r['final_points']==-5 and r['hit_sl_later']=='YES'
def test_10_target_first(store):
 store.fire(sig(),NOW);store.update_bar(bar(h=116));store.update_bar(bar(1,h=110,l=94));assert rows(store)[0]['target_15_hit_before_sl']=='YES'
def test_11_stop_first(store):
 store.fire(sig(),NOW);store.update_bar(bar(h=105,l=94));store.update_bar(bar(1,h=125));assert rows(store)[0]['target_15_hit_before_sl']=='NO'
def test_12_ambiguous(store):
 store.fire(sig(),NOW);store.update_bar(bar(h=116,l=94));r=rows(store)[0];assert r['target_15_hit_before_sl']=='AMBIGUOUS' and r['outcome']=='AMBIGUOUS' and r['max_points_moved']==0
def test_13_two_tiers_restart(store):
 store.fire(sig(tier=1),NOW);store.fire(sig(tier=3),NOW);r=RecordingStore(store.path,store.csv_dir,store.cfg);assert r.fire(sig(tier=1),NOW+1) is None and len(rows(r))==2 and len({x['signal_id'] for x in rows(r)})==2
def test_14_freeze(store):
 store.fire(sig(),NOW);store.update_bar(bar());store.freeze(DAY);before=rows(store);store.update_bar(bar(10,h=1000,c=500));assert rows(store)==before and before[0]['outcome']=='EXPIRED_1520'
def test_15_minute_receipt(store):
 store.track(contract(),NOW)
 for n in range(3):store.record_tick(contract(),{'last_price':20+n},NOW+n*60+17)
 with store.db() as c:r=c.execute('SELECT * FROM contract_tick ORDER BY ts').fetchall()
 assert len(r)==3 and r[0]['observed_at']-r[0]['ts']==17 and r[1]['ltp']==21
def test_16_premium_gap(store):
 store.fire(sig(),NOW);store.update_bar(bar(h=120));r=rows(store)[0];assert r['premium_now'] is None and r['max_premium_points'] is None and r['premium_gap']==1
def test_17_coverage():
 r=coverage([NOW,NOW+60,NOW+60,NOW+240],NOW,NOW+300);assert r['coverage_pct']==60 and r['max_gap_minutes']==2 and r['max_gap_at']=='10:02'
def test_18_failure_swallowed(store):
 assert store.safe(Mock(side_effect=sqlite3.OperationalError('disk full'))) is None and 'disk full' in store.last_error
def test_19_tariff_deltas():
 from scripts import backtest_stop_width_8510 as m
 s=Path(m.__file__).read_text();assert 'calculate_trade_charges' in s and '400.0' not in s and '0.50' not in s
 cfg=json.loads((m.ROOT/'analyst/terminal8510_config.json').read_text())['recording_v193'];assert (cfg['study_win_delta'],cfg['study_loss_delta'])==(.59,.67)
 json.dumps(m.metrics([{'spot_points':10,'status':'TARGET','matched_net':10,'day':DAY,'scenario_gross':20},{'spot_points':-10,'status':'INITIAL_SL','matched_net':-20,'day':DAY,'scenario_gross':-10}]),allow_nan=False)
def test_20_no_order_calls():
 from analyst import market_recorder_8510 as m
 from scripts import backtest_stop_width_8510 as b
 for p in [Path(m.__file__),Path(b.__file__)]:
  assert not any(isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr in {'place_order','modify_order','cancel_order','execute_order_with_chaser','submit_order'} for n in ast.walk(ast.parse(p.read_text())))
def test_queue_bounded(store):
 r=MarketRecorder(SimpleNamespace(cfg={'recording_v193':{'queue_size':1}}),store);assert r.enqueue('x');t=time.monotonic();assert not r.enqueue('y') and time.monotonic()-t<.1 and r.dropped==1
def test_partial_fire_minute_excluded(store):
 store.fire(sig(),NOW+20);store.update_bar(bar(h=120,l=94));assert rows(store)[0]['outcome']=='RUNNING';store.update_bar(bar(1,h=116));assert rows(store)[0]['target_15_hit_before_sl']=='YES'
def test_spot_gap_conservative(store):
 store.fire(sig(),NOW);store.update_bar(bar(2,h=116));assert rows(store)[0]['target_15_hit_before_sl']=='AMBIGUOUS'
def test_no_late_fire(store):assert store.fire(sig(),stamp(DAY,'15:20')) is None
def test_zero_missing_quote_distinct(store):
 assert not store.record_tick(contract(),{'last_price':None},NOW);assert store.record_tick(contract(),{'last_price':20,'volume':0,'oi':0},NOW)
 with store.db() as c:r=c.execute('SELECT * FROM contract_tick').fetchone()
 assert r['volume']==0 and r['oi']==0
def test_csv_crash_reconcile(store):
 store.persist_ema([ema()]);(store.csv_dir/(DAY+'.csv')).unlink();store.persist_ema([ema()]);assert len(list(csv.DictReader((store.csv_dir/(DAY+'.csv')).open())))==1
def test_study_same_bar_target_stop():
 import pandas as pd
 from scripts.backtest_stop_width_8510 import replay
 s=dict(spot_entry=100,side='CE',day=DAY,ts=NOW,setup='STAGE_2');m=pd.DataFrame([dict(ts=NOW,o=100,h=141,l=79,c=110)]);b=pd.DataFrame(columns=['day','end','c','ema8']);assert replay(s,m,b,20)['status']=='AMBIGUOUS'
def test_retention():
 s=(Path(__file__).resolve().parents[1]/'ict_smc_app/backend/app/db.py').read_text().split('def prune()')[1];assert 'DELETE FROM chain_snap' not in s and "sec_id NOT IN ('13','FUT','FUT2')" in s

def test_held_exit_tail_and_master_identity(store,tmp_path):
 from types import SimpleNamespace
 ledger=tmp_path/'ledger.db';ident=contract()
 with sqlite3.connect(ledger) as c:
  c.execute('CREATE TABLE positions(security_id TEXT,status TEXT,entry_time TEXT,exit_time TEXT)')
  c.execute('CREATE TABLE signal_observations(session_date TEXT,security_id TEXT)')
  c.execute('INSERT INTO positions VALUES(?,?,?,?)',('9','OPEN',DAY+' 09:30:00 IST',None))
 cfg={'contract_tick_minutes_after_exit':30,'track_atm_plus_minus':100,'strike_step':50}
 backend=SimpleNamespace(cfg={'recording_v193':cfg},ledger_db=ledger,inst_mgr=SimpleNamespace(nifty_options={'x':ident}))
 r=MarketRecorder(backend,store)
 assert r.collect_contracts(NOW,None)==[ident]
 with sqlite3.connect(ledger) as c:c.execute('UPDATE positions SET status=?,exit_time=?',('CLOSED',DAY+' 15:25:00 IST'))
 assert r.collect_contracts(stamp(DAY,'15:40'),None)==[ident]
 assert r.collect_contracts(stamp(DAY,'16:00'),None)==[]

def test_last_write_does_not_claim_read_or_empty_cycle(store):
 assert store.last_write is None
 store.safe(store.reload,DAY);store.safe(store.freeze,DAY);assert store.last_write is None
 store.persist_ema([ema()]);before=store.last_write
 store.safe(store.reload,DAY);store.safe(store.freeze,DAY);assert store.last_write==before

def test_chain_refresh_slot_coverage(store):
 d={'data':{'last_price':100,'oc':{'100':{'ce':{'oi':10,'last_price':5},'pe':{'oi':20,'last_price':6}}}}}
 store.record_chain(d,DAY,NOW+10);store.record_chain(d,DAY,NOW+40)
 with store.db() as c:
  assert c.execute('SELECT count(*) FROM chain_snap').fetchone()[0]==2
  row=c.execute('SELECT * FROM chain_coverage').fetchone()
  assert row['n_snapshots']==1 and row['expected_snapshots']==125

def test_clean_premium_observations(store):
 store.fire(sig(),NOW);store.record_tick(contract(),{'last_price':22},NOW+17);store.update_bar(bar())
 r=rows(store)[0];assert r['premium_now']==22 and r['max_premium_points']==2 and r['premium_gap']==0

def test_collector_forward_only_and_stale_rows(store,tmp_path,monkeypatch):
 import analyst.ema_proximity_engine as m
 class Fake:
  def __init__(self,**kw):assert '_no_replay_substitution' in str(kw['replay_dir'])
  def compute_matrix(self,**kw):return {'rows':[]}
 monkeypatch.setattr(m,'EmaProximityEngine',Fake)
 backend=SimpleNamespace(cfg={'recording_v193':{}},market_db=tmp_path/'none')
 r=MarketRecorder(backend,store);r.started_at=NOW+5
 r.persist_closed(NOW,100,'PAUSED');assert not store.reload(DAY)
 r.persist_closed(NOW+60,None,'PAUSED');x=store.reload(DAY)
 assert len(x)==3 and {z['status'] for z in x}=={'STALE'} and all(z['nifty_spot_price'] is None for z in x)

def test_gap_open_stop_is_not_better_fictional_fill():
 import pandas as pd
 from scripts.backtest_stop_width_8510 import replay
 s=dict(spot_entry=100,side='CE',day=DAY,ts=NOW,setup='STAGE_2');m=pd.DataFrame([dict(ts=NOW,o=70,h=145,l=65,c=110)]);b=pd.DataFrame(columns=['day','end','c','ema8']);r=replay(s,m,b,20);assert r['spot_points']==-30 and r['status']=='INITIAL_SL'

def test_expiry_pending_no_not_stop_first_when_complete(store):
 t=stamp(DAY,'15:19');store.fire(sig(),t)
 store.update_bar(dict(ts=t,h=104,l=99,c=101))
 r=rows(store)[0];assert r['outcome']=='EXPIRED_1520' and r['target_15_hit_before_sl']=='NO' and r['final_points']==1
 assert store.radar(DAY)['summary'][2]['stop_n']==0

def test_expiry_missing_tail_is_ambiguous_and_final_unknown(store):
 store.fire(sig(),NOW);store.update_bar(bar());store.freeze(DAY)
 r=rows(store)[0];assert r['outcome']=='EXPIRED_1520' and r['target_15_hit_before_sl']=='AMBIGUOUS' and r['final_points'] is None and r['premium_now'] is None

def test_live_price_changes_dont_multiply_one_tier_event(store):
 backend=SimpleNamespace(cfg={'recording_v193':{'radar_tier_track':True,'freshness_seconds':120,'premium_freshness_seconds':60,'contract_tick_enabled':False}})
 r=MarketRecorder(backend,store);r.atm_contract=lambda *args:None
 base={'tier':'TIER_1_APPROACHING','bar_ts':NOW,'micro_sl_price':95,'ema_4':102,'ema_8':100,'close_price':100,'action_recommendation':'approach'}
 r.record_radar(base,100,NOW);r.record_radar({**base,'close_price':101},101,NOW+1)
 restarted=MarketRecorder(backend,store);restarted.atm_contract=lambda *args:None
 restarted.record_radar({**base,'close_price':102},102,NOW+2)
 assert len(rows(store))==1

def test_persistent_tables_have_time_indexes(store):
 with store.db() as c:names={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='index'")}
 assert {'contract_tick_identity_time','contract_tick_session','chain_snap_time'}<=names

def test_risk_uses_observed_spot_anchor(store):
 s=sig();s['nifty_spot_at_signal']=102;store.fire(s,NOW)
 r=rows(store)[0];assert r['signal_price']==100 and r['risk_points']==7 and r['nifty_spot_at_signal']==102

def test_open_contract_does_not_poll_all_night(store,tmp_path):
 ledger=tmp_path/'night.db'
 with sqlite3.connect(ledger) as c:
  c.execute('CREATE TABLE positions(security_id TEXT,status TEXT,entry_time TEXT,exit_time TEXT)')
  c.execute('CREATE TABLE signal_observations(session_date TEXT,security_id TEXT)')
  c.execute('INSERT INTO positions VALUES(?,?,?,?)',('9','OPEN',DAY+' 09:30:00 IST',None))
 backend=SimpleNamespace(cfg={'recording_v193':{'contract_tick_minutes_after_exit':30,'track_atm_plus_minus':100,'strike_step':50}},ledger_db=ledger,inst_mgr=SimpleNamespace(nifty_options={'x':contract()}))
 r=MarketRecorder(backend,store);assert r.collect_contracts(stamp(DAY,'19:00'),None)==[]
