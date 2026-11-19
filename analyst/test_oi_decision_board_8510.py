"""Display/gate parity; no broker or config writes."""
import copy
import itertools
import json
import subprocess
from pathlib import Path
import pytest
from analyst.oi_decision_board_8510 import build_oi_decision_board,armed_status,enrich_strategy_rows
from analyst.proto_oi_filters_8510 import oi_switch_block,board_reads_from_board,PROTO_OI_READ_IDS
from analyst.ema8_prototype_8510 import load_ema8_prototype_cfg
from analyst.reversal_revamp_8510 import reversal_oi_filters_for_gate

ROOT=Path(__file__).resolve().parents[1]

def config(mode='block',rule='all_agree',neutral=False):
 return {'ema8_prototype':{'oi_filters':{'mode':mode,'rule':rule,'neutral_blocks':neutral,'switches':dict.fromkeys(PROTO_OI_READ_IDS,True)}},
  'reversal':{'oi_filters':{'enabled':True,'mode':mode,'log_only':mode=='log_only','rule':rule,'switches':{k:k in ('writer_flow','fresh_oi') for k in PROTO_OI_READ_IDS}}}}

def source(directions):
 return {'as_of':'10:24:03','sources':[{'id':rid,'direction':d,'age_sec':30,'detail':rid+' live input'}for rid,d in zip(PROTO_OI_READ_IDS,directions)]}

@pytest.mark.parametrize('lane',['prototype','reversal'])
@pytest.mark.parametrize('mode',['block','log_only'])
@pytest.mark.parametrize('rule',['all_agree','any_agrees','majority'])
@pytest.mark.parametrize('neutral',[False,True])
def test_both_sides_match_exact_execution_call(lane,mode,rule,neutral):
 cfg=config(mode,rule,neutral)
 # Exhaust the enabled sources across directional, neutral and missing readings.
 for values in itertools.product(('CE','PE','NEUTRAL',None),repeat=3):
  board=source((*values,'CE','PE'));reads=board_reads_from_board(board)
  gate=reversal_oi_filters_for_gate(cfg) if lane=='reversal' else None
  call=load_ema8_prototype_cfg(cfg) if lane=='prototype' else {'oi_filters': gate or {}}
  view=build_oi_decision_board(lane,cfg,board)
  for bullish,key in ((True,'buy'),(False,'sell')):
   blocked,detail=oi_switch_block(bullish,call,reads)
   assert view[key]['would_block']==blocked
   assert view[key]['gate_detail']==detail
   assert view[key]['actual_block']==(blocked and mode=='block')

@pytest.mark.parametrize('direction,age',[(None,0),('PE',None),('PE',99999),('WRONG',0),('CE','bad')])
def test_stale_read_is_dash_both_sides_and_fail_open(direction,age):
 cfg=config();cfg['ema8_prototype']['oi_filters']['switches']={'writer_flow':True}
 board={'sources':[{'id':'writer_flow','direction':direction,'age_sec':age}]}
 view=build_oi_decision_board('prototype',cfg,board);r=view['rows'][0]
 assert r['vote_buy']==r['vote_sell']=='STALE';assert not view['buy']['actual_block'];assert not view['sell']['actual_block']
 assert 'stale'in r['live_detail']

def test_default_and_off_have_no_votes_and_do_not_write_inputs():
 cfg={};board=source(['CE']*5);before=copy.deepcopy([cfg,board]);view=build_oi_decision_board('prototype',cfg,board)
 assert view['mode']=='log_only';assert view['rows']==[];assert [cfg,board]==before
 cfg=config();cfg['reversal']['oi_filters']['enabled']=False
 view=build_oi_decision_board('reversal',cfg,board);assert not view['enabled'];assert not view['buy']['would_block']

def test_reversal_display_matches_nested_execution_call():
 cfg=config();view=build_oi_decision_board('reversal',cfg,source(['PE']*5))
 assert not any('CONFIG/GATE MISMATCH'in w for w in view['warnings'])
 assert view['buy']['would_block'] and view['mode']=='block'
 cfg['reversal']['oi_filters']['log_only']=True
 cfg['reversal']['oi_filters']['mode']='log_only'
 view=build_oi_decision_board('reversal',cfg,source(['PE']*5));assert view['mode']=='log_only'

def test_per_lane_config_independence_and_new_read_updates():
 cfg=config();board=source(['PE']*5);before=copy.deepcopy(cfg)
 rows={'ema8_prototype':{'state':'ARMED','direction':'LONG'},'crossover_reversal':{'state':'ARMED','reversal_side':'DOWN'}}
 result=enrich_strategy_rows(cfg,rows,board);assert result['ema8_prototype']['oi_decision_board']['buy']['actual_block']
 assert result['crossover_reversal']['oi_decision_board']['buy']['would_block']
 cfg['ema8_prototype']['oi_filters']['switches']=dict.fromkeys(PROTO_OI_READ_IDS,False)
 other=enrich_strategy_rows(cfg,rows,board)
 assert result['crossover_reversal']==other['crossover_reversal'];assert cfg!=before;assert 'oi_decision_board'not in rows['ema8_prototype']
 updated=build_oi_decision_board('prototype',before,source(['CE']*5));assert not updated['buy']['actual_block'];assert updated['sell']['actual_block']

def test_armed_waiting_and_in_trade_do_not_infer_from_oi():
 s=armed_status('prototype',{'armed':True},{'arm':{'direction':'SHORT','away_seen':False}},[])
 assert s['side']=='PE';assert s['waiting_for']=='waiting for away from 8 EMA'
 s=armed_status('prototype',{'armed':True},{'arm':{'direction':'LONG','away_seen':True}},[]);assert s['waiting_for']=='waiting for touch'
 assert armed_status('reversal',{'state':'ARMED','reversal_side':'UP'},{},[])['side']=='CE'
 assert armed_status('reversal',{'state':'ARMED'},{},[])['side']is None
 assert armed_status('prototype',{'state':'PAUSED'},{},[])['state']=='OFF'
 assert armed_status('reversal',{}, {},[{'strategy_id':'CROSSOVER_BULL','status':'OPEN','direction':'LONG'}])['state']=='IN_TRADE'

def test_context_is_explanatory_not_an_extra_vote():
 cfg=config();cfg['ema8_prototype']['oi_filters']['momentum']={'require_price_confirm':True}
 board=source(['CE']*5);board['sources'][0].update(S=.21,price_confirm='agree')
 view=build_oi_decision_board('prototype',cfg,board)
 assert view['buy']['agree']==5;assert len(view['context_rows'])>=2;assert not any(r['counted']for r in view['context_rows'])

def test_display_only_wiring_and_present_key_guard():
 html=(ROOT/'Algorithmic_Trading_Workspace_8510.html').read_text();backend=(ROOT/'analyst/terminal8510_backend.py').read_text()
 for rid in ('protoOiDecisionBoard','revOiDecisionBoard'):assert html.count('id="'+rid+'"')==1
 assert 'Object.prototype.hasOwnProperty.call(lane, "oi_decision_board")'in html
 assert 'renderOiDecisionBoard("protoOiDecisionBoard", ep)'in html
 assert 'renderOiDecisionBoard("revOiDecisionBoard", sc.strategies.crossover_reversal)'in html
 assert '"strategies": strategies'in backend;assert 'self.get_scanner_status(positions=positions)'in backend
 a=html.index('function renderOiDecisionBoard(');b=html.index('function collectReversalOiFiltersFromDom()',a);code=html[a:b]
 assert '.checked'not in code and 'fetch('not in code and 'innerHTML'not in code

def test_backend_get_scanner_status_uses_one_read_snapshot(tmp_path):
 from analyst.terminal8510_backend import QuantTerminalBackend
 b=QuantTerminalBackend.__new__(QuantTerminalBackend);b.scanner_cfg=config();b.strategy_status={'ema8_prototype':{'state':'ARMED','direction':'SHORT'},'crossover_reversal':{'state':'ARMED','reversal_side':'UP'}}
 b.scanner_enabled=True;b.scanner_running=True;b.active_party='';b.directional_bias='';b.last_crossover_event={};b.kill_switch_active=False;b.recent_signals=[];b.algo_strike_selection={};b.algo_allowlist_cfg={};b._ema8_prototype_state={'arm':{'direction':'SHORT'}}
 b.get_ema8_prototype_log=lambda **k:[];b._ema8_prototype_view=lambda:{};b.get_scanner_gate_config=lambda:{};b._matrix_scalp_scanner_active=lambda:False;b._pa_sr_live_scanner_active=lambda:False
 calls=[]
 def read():calls.append(1);return source(['CE']*5)
 b.desk8600_board=read;before=copy.deepcopy(b.strategy_status)
 result=b.get_scanner_status(positions=[])
 assert len(calls)==1;assert b.strategy_status==before
 assert result['strategies']['ema8_prototype']['oi_decision_board']['armed']['side']=='PE'


def test_malformed_optional_slope_cannot_hide_the_board():
 cfg=config();board=source(['CE']*5);board['sources'][0]['S']='not-a-number'
 view=build_oi_decision_board('prototype',cfg,board)
 assert view['rows'][0]['S'] is None
 assert not view['buy']['would_block']


def test_reversal_missing_switches_preserve_existing_inheritance():
 cfg=config();cfg['reversal']['oi_filters']['switches']={'writer_flow':True}
 first=build_oi_decision_board('reversal',cfg,source(['CE']*5))
 cfg['ema8_prototype']['oi_filters']['switches']['total_oi']=False
 second=build_oi_decision_board('reversal',cfg,source(['CE']*5))
 assert 'total_oi' in [r['id'] for r in first['rows']]
 assert 'total_oi' not in [r['id'] for r in second['rows']]
 gd1 = first['buy']['gate_detail']
 gd2 = second['buy']['gate_detail']
 assert gd1.get('switches_on') != gd2.get('switches_on')
