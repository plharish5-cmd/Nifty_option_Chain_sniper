"""v1.9.2 causal entry/research/advisory tests. Trailing tests explicitly gated off."""
import json,sqlite3,hashlib,copy
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
import pandas as pd
import pytest
from analyst.trend_continuation_entry_8510 import config,continuation_readiness,opening_range_states,closed_bars,historical_signals
from analyst.trend_capture_research_8510 import distribution,evidence_gate,select_contract,measure_mfe,day_labels
# The live Trend Capture service was removed by the owner on 12 Nov 2026; its tests went with it.
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

@pytest.mark.parametrize('assertion',['ratchet_CE_PE_monotonic','close_only_vs_wick','catastrophe_gap','regime_auto_selector','two_stage_switch','premium_gap_holds_ratchet'])
@pytest.mark.skip(reason='Part 1 evidence gate failed: hybrid trailing engine intentionally NOT BUILT; no passing implementation asserted')
def test_conditional_trailing_assertions_not_built(assertion):
    raise AssertionError('Gate must pass before this test can acquire a trailing implementation')

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

