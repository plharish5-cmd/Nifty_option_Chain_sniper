"""v1.9.2 causal entry/research/advisory tests. Trailing tests explicitly gated off."""
import json,sqlite3,hashlib,copy
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
import pandas as pd
import pytest
from analyst.trend_continuation_entry_8510 import config,continuation_readiness,opening_range_states,closed_bars,historical_signals
from analyst.trend_capture_research_8510 import distribution,evidence_gate,select_contract,measure_mfe,day_labels
from analyst.trend_capture_service_8510 import TrendCaptureService,update_config
from analyst.trend_capture_evaluation_8510 import master_lot,performance,baseline_replay
IST=ZoneInfo('Asia/Kolkata');ROOT=Path(__file__).resolve().parents[1]

def example_bar(side='CE'):
    return dict(ema4=110.,ema8=100.,sep_atr=.5,alignment3=True,l=108.,h=114.,c=112.) if side=='CE' else dict(ema4=100.,ema8=110.,sep_atr=.5,alignment3=True,l=96.,h=102.,c=98.)

@pytest.mark.parametrize('side',['CE','PE'])
def test_stage2b_pullback_reclaim_and_ema8_integrity(side):
    b=example_bar(side);state={'trend_day_confirmed':True,'or_break_direction':'BULLISH' if side=='CE' else 'BEARISH'}
    r=continuation_readiness(b,state);assert r['ready'];assert not r['advisory_entry'];assert not r['execution_authorized']
    b['c']=90 if side=='CE' else 120
    assert not continuation_readiness(b,state)['ready']

@pytest.mark.parametrize('count,opened,expected',[(0,False,True),(1,False,True),(2,False,False),(0,True,False)])
def test_entry_cap_and_open_direction(count,opened,expected):
    assert continuation_readiness(example_bar(),{'trend_day_confirmed':True,'or_break_direction':'BULLISH'},entries_today=count,same_direction_open=opened)['ready']==expected

@pytest.mark.parametrize('field,value',[('alignment3',False),('sep_atr',.26),('l',125.),('c',99.)])
def test_all_entry_conditions_required(field,value):
    b=example_bar();b[field]=value
    assert not continuation_readiness(b,{'trend_day_confirmed':True,'or_break_direction':'BULLISH'})['ready']

def opening_frame(closes):
    idx=pd.date_range('2026-11-06 09:15',periods=len(closes),freq='15min',tz=IST)
    return pd.DataFrame({'day':'2026-11-06','h':[102,104,103]+[max(105,x) for x in closes[3:]],'l':[98,99,100]+[min(99,x) for x in closes[3:]],'c':closes,'sep_atr':.4,'slope8':1.,'slot_rvol_median':None,'end':idx.astype('int64')//10**9+900},index=idx)

@pytest.mark.parametrize('closes,confirmed', [([100,101,102,106,107],True),([100,101,102,106,101],False),([100,101,102,101,102],False)])
def test_opening_range_true_false_and_no_breakout(closes,confirmed):
    b=opening_frame(closes);s=opening_range_states(b);assert not s[int(b.end.iloc[3])]['trend_day_confirmed'];assert s[int(b.end.iloc[-1])]['trend_day_confirmed']==confirmed

def test_no_lookahead_opening_range_and_future_bar():
    b=opening_frame([100,101,102,106,107]);a=opening_range_states(b.iloc[:-1]);z=opening_range_states(b)
    assert all(v==z[k] for k,v in a.items())
    s=opening_range_states(b.iloc[1:]);assert all(not v['trend_day_confirmed'] for v in s.values())

def test_closed_bar_boundary_and_missing_minute():
    start=int(datetime(2026,11,6,9,15,tzinfo=IST).timestamp());f=pd.DataFrame([dict(ts=start+i*60,o=100,h=101,l=99,c=100,v=10) for i in range(15)])
    assert len(closed_bars(f,start+900))==1
    assert closed_bars(f,start+899).empty
    assert closed_bars(f.iloc[:-1],start+900).empty

def test_missing_premium_timestamp_never_selects_future_or_other_strike():
    sig={'ts':1000,'day':'2026-11-06','side':'CE','spot_entry':23045}
    c=pd.DataFrame([dict(ts=1060,expiry='2026-11-09',strike=22900,ce_ltp=200,pe_ltp=210)])
    assert select_contract(sig,c) is None
    c.ts=1000;assert select_contract(sig,c)['entry']==200
    c.strike=22950;assert select_contract(sig,c) is None

def test_mfe_gate_does_not_follow_unrestricted_tail():
    rows=[dict(setup='STAGE_2',day='d',eod_mfe=x,unrestricted_eod_mfe=500) for x in [10,20,30,40]]
    assert not evidence_gate(rows,{'d':{'trend_body':True}})['passes']
    rows[-1]['eod_mfe']=500;rows[-2]['eod_mfe']=200
    assert evidence_gate(rows,{'d':{'trend_body':True}})['passes']

def test_statutory_cost_and_dynamic_master_no_delta_fallback(tmp_path):
    from paper_trading import calculate_trade_charges
    f=tmp_path/'master.csv';f.write_text('SEM_INSTRUMENT_NAME,SEM_TRADING_SYMBOL,SEM_LOT_UNITS\nOPTIDX,NIFTY-Nov2026-23000-CE,50\n')
    lot=master_lot(f);assert lot==50
    costs=calculate_trade_charges(200,240,lot);assert 0<costs['total_charges']<100
    sig={'ts':1000,'day':'2026-11-06','side':'CE','spot_entry':23045,'setup':'STAGE_2B'}
    empty=pd.DataFrame(columns=['ts','expiry','strike','ce_ltp','pe_ltp'])
    sc=json.loads((ROOT/'analyst/terminal8510_config.json').read_text())['super_trail']
    r=baseline_replay(sig,pd.DataFrame(),empty,lot,sc);assert r['net'] is None and r['status']=='NO_EXACT_ENTRY_SNAPSHOT'

def test_advisory_only_never_mutates_positions_or_market(tmp_path):
    market=tmp_path/'market.sqlite';ledger=tmp_path/'ledger.sqlite'
    start=int(datetime(2026,11,6,9,15,tzinfo=IST).timestamp())
    with sqlite3.connect(market) as c:
        c.execute('create table candles_1m(sec_id text,ts integer,o real,h real,l real,c real,v real)')
        c.executemany('insert into candles_1m values(?,?,?,?,?,?,?)',[('13',start+i*60,100+i,102+i,99+i,101+i,10) for i in range(120)])
    with sqlite3.connect(ledger) as c:c.execute('create table positions(symbol text,status text)');c.execute("insert into positions values('NIFTY-X-CE','OPEN')")
    before=hashlib.sha256(market.read_bytes()).hexdigest();s=TrendCaptureService({},market,ledger)
    for _ in range(2):s.at=0;result=s.status(start+120*60+1)
    assert result['mode']=='ADVISORY_ONLY' and not result['execution_authorized']
    assert result['final_ratchet'] is None and result['candidate_stops']=={}
    with sqlite3.connect(ledger) as c:
        assert c.execute('select * from positions').fetchall()==[('NIFTY-X-CE','OPEN')]
        assert c.execute('select count(*) from trail_observations').fetchone()[0]==1
        assert not c.execute("select 1 from sqlite_master where name='orders'").fetchone()
    assert hashlib.sha256(market.read_bytes()).hexdigest()==before
    s.at=0;stale=s.status(start+7*3600)
    assert not stale['readiness']['ready'] and not stale['readiness']['advisory_entry']

def test_config_defaults_safe_and_cannot_arm_execution(tmp_path):
    c=config({'execution_action_mode':'LIVE'});assert c['execution_action_mode']=='ADVISORY_ONLY';assert c['continuation_entry_enabled'] is False
    p=tmp_path/'cfg.json';original={'execution_mode':'PAPER_ONLY','super_trail':{'initial_sl_pts':20},'trend_capture_v192':c};p.write_text(json.dumps(original))
    for body in [{'execution_action_mode':'LIVE'},{'max_continuation_entries_per_day':3},{'initial_sl_pts':10},{'two_stage_switch_pts':float('nan')}]:
        with pytest.raises(ValueError):update_config(p,body)
    out=update_config(p,{'continuation_entry_enabled':True});assert out['execution_action_mode']=='ADVISORY_ONLY'
    final=json.loads(p.read_text());assert final['execution_mode']==original['execution_mode'];assert final['super_trail']==original['super_trail']

@pytest.mark.parametrize('assertion',['ratchet_CE_PE_monotonic','close_only_vs_wick','catastrophe_gap','regime_auto_selector','two_stage_switch','premium_gap_holds_ratchet'])
@pytest.mark.skip(reason='Part 1 evidence gate failed: hybrid trailing engine intentionally NOT BUILT; no passing implementation asserted')
def test_conditional_trailing_assertions_not_built(assertion):
    raise AssertionError('Gate must pass before this test can acquire a trailing implementation')

def test_evidence_ui_never_advertises_proven_trailing():
    html=(ROOT/'Algorithmic_Trading_Workspace_8510.html').read_text()
    assert 'trendCaptureV192' in html and 'ADVISORY ONLY' in html
    assert 'not built — evidence gate did not pass' in html

def test_absent_data_and_undefined_winrate():
    r=performance([],['d']);assert r['net_per_trade'] is None;assert r['break_even_wr'] is None
    assert distribution([])=={'n':0}


def test_mfe_measures_after_actual_exit_without_ignoring_initial_stop():
    start=int(datetime(2026,11,6,10,0,tzinfo=IST).timestamp())
    minutes=pd.DataFrame([dict(ts=start+i*60,day='2026-11-06',o=100+2*i,h=101+2*i,l=99+2*i,c=100+2*i,v=10) for i in range(16)])
    signal={'ts':start,'day':'2026-11-06','side':'CE','spot_entry':100.,'setup':'STAGE_2','actual_exit_ts':start+5*60}
    bars=pd.DataFrame(columns=['day','end','c','ema8']);chain=pd.DataFrame(columns=['ts','expiry','strike','ce_ltp','pe_ltp'])
    row=measure_mfe([signal],minutes,bars,chain)[0]
    assert row['in_trade_mfe']==9 and row['eod_mfe']==31
    signal['actual_exit_ts']=start+5*60+30
    assert measure_mfe([signal],minutes,bars,chain)[0]['in_trade_mfe']==9
    minutes.loc[5,'l']=79
    row=measure_mfe([signal],minutes,bars,chain)[0]
    assert row['eod_mfe']==9 and row['unrestricted_eod_mfe']==31


def test_advisory_api_config_cannot_arm_and_uses_own_service(tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    from types import SimpleNamespace
    import engine_8510_live as server
    settings=config();path=tmp_path/'config.json';path.write_text(json.dumps({'execution_mode':'PAPER_ONLY','trend_capture_v192':settings}))
    service=SimpleNamespace(cfg=settings,at=1,status=lambda:{'mode':'ADVISORY_ONLY','execution_authorized':False},backtest=lambda:{'gate':{'passes':False}})
    monkeypatch.setattr(server.backend,'trend_capture_service',lambda:service)
    monkeypatch.setattr(server.backend,'config_path',path)
    monkeypatch.setattr(server.backend,'cfg',{'execution_mode':'PAPER_ONLY','trend_capture_v192':settings})
    client=TestClient(server.app)
    assert client.get('/api/trend_capture/status').json()['execution_authorized'] is False
    assert client.get('/api/trend_capture/backtest').json()['gate']['passes'] is False
    assert client.get('/api/trend_capture/config').json()['continuation_entry_enabled'] is False
    assert client.post('/api/trend_capture/config',json={'execution_action_mode':'LIVE'}).status_code==400
    assert client.post('/api/trend_capture/config',json={'continuation_entry_enabled':False}).json()['execution_authorized'] is False
    assert json.loads(path.read_text())['execution_mode']=='PAPER_ONLY'
    client.close()
