"""Remaining EMA audit regressions. Private fixtures and mocked execution only."""
import asyncio
import time
import pytest
from analyst.test_ema_execution_audit_v240_8510 import stub
from analyst.test_reversal_crosslane_8510 import _cross_sig,_proto_sig

@pytest.mark.parametrize('route',['PAPER_ONLY','LIVE_ALLOWED'])
def test_disabled_reversal_never_emits_in_either_route(route):
 from analyst.test_four_algo_corrections_8510 import _reversal_frame,_closed_e8,_tick,DAY
 from analyst.mj_sir_stage_reversal_8510 import load_mj_cfg,evaluate_mj_sir_four_pack
 full,k=_reversal_frame();bars=full.iloc[:k+3]
 sigs,status=evaluate_mj_sir_four_pack(bars,bar_ci=-2,cfg=load_mj_cfg({}),bb_stage2=False,bb_stage3=False,bb_cross=False,
   party_switch_enabled=False,active_party='NEUTRAL',mj_state={},session_day=DAY,dynamic=_tick(_closed_e8(bars)+1),
   scanner_cfg={'reversal':{'enabled':False,'execution_mode':route}})
 assert not any(s['strategy_id'].startswith('CROSSOVER') for s in sigs)
 assert status['crossover_reversal']['state']=='OFF'

def test_cooldown_has_visible_reason(tmp_path):
 b=stub(tmp_path);b.scanner_cfg['signal_cooldown_5m_min']=5
 b.strategy_cooldowns['CROSSOVER_BULLISH']=time.time()-1
 asyncio.run(b._process_strategy_signals([_cross_sig()]))
 assert b.execute_order_with_chaser.await_count==0
 assert 'cooldown' in b._ema_entry_decisions['crossover_reversal']['reason']

def test_exception_in_one_order_does_not_lose_other_lane(tmp_path):
 b=stub(tmp_path);b._select_ema8_prototype_contract_any_strike.side_effect=[RuntimeError('fixture quote failure'),({'symbol':'FIXTURE','security_id':'1'},'')]
 asyncio.run(b._process_strategy_signals([_proto_sig(),_cross_sig()]))
 assert b.execute_order_with_chaser.await_count==1
 assert b._ema_entry_decisions['ema8_prototype']['stage']=='ERROR'
 assert b._ema_entry_decisions['crossover_reversal']['stage']=='ORDER_ACCEPTED'

def test_trail_does_not_use_preentry_candles():
 from datetime import datetime
 from zoneinfo import ZoneInfo
 from analyst.reversal_spot_exit_8510 import trail_bar_belongs_to_position
 def bar(hm):return {'ts':datetime.fromisoformat('2026-11-18T'+hm+':00').replace(tzinfo=ZoneInfo('Asia/Kolkata')).timestamp()}
 assert not trail_bar_belongs_to_position(bar('09:30'),'2026-11-18 10:00:00 IST','15m')
 assert not trail_bar_belongs_to_position(bar('09:45'),'2026-11-18 10:00:00 IST','15m')
 assert trail_bar_belongs_to_position(bar('10:00'),'2026-11-18 10:02:00 IST','15m')
 assert not trail_bar_belongs_to_position(bar('10:00'),'bad timestamp','15m')

@pytest.mark.parametrize('sid,lane,points,side',[
 ('EMA8_PROTOTYPE_LONG','ema8_prototype',40,'CE'),
 ('EMA8_PROTOTYPE_SHORT','ema8_prototype',40,'PE'),
 ('CROSSOVER_BULLISH','reversal',20,'CE'),
 ('CROSSOVER_BEARISH','reversal',20,'PE')])
def test_live_lane_entry_records_mock_fill_and_stop(tmp_path,sid,lane,points,side):
 import sqlite3
 from analyst.test_phase2_option_points_8510 import FakeBroker,make_test_backend
 broker=FakeBroker(configured=True);b=make_test_backend(tmp_path,broker)
 b.scanner_cfg[lane]={'execution_mode':'LIVE_ALLOWED','live_trade_enabled':True}
 sign=1 if side=='CE' else -1
 async def go():
  result=await b.execute_order_with_chaser(
   contract={'symbol':'NIFTY 25000 '+side,'security_id':'54321','lot_size':65,'ltp':250.0},
   side='BUY',strategy_name='Isolated EMA fixture',strategy_id=sid,target_pts=points,sl_pts=points,is_algo_trade=True,
   spot_context={'strategy_id':sid,'spot_entry':25000,'spot_target_level':25000+sign*points,'spot_stop_level':25000-sign*points,'spot_target_pts':points,'spot_stop_pts':points},
   enforce_market_hours=False)
  assert result['ok']
 asyncio.run(go())
 with sqlite3.connect(b.ledger_db) as c:
  c.row_factory=sqlite3.Row;position=dict(c.execute('select * from positions').fetchone())
 assert position['live_filled']==1 and position['live_fill_qty']==65
 assert position['entry_price']==250 and position['spot_entry']==25000
 stops=[o for o in broker.placed_orders if o['orderType']=='STOP_LOSS_LIMIT']
 assert len(stops)==1 and stops[0]['quantity']==65 and stops[0]['triggerPrice']==250-points


def test_unknown_order_exception_does_not_submit_another_lane(tmp_path):
 b=stub(tmp_path);b.execute_order_with_chaser.side_effect=RuntimeError('fixture unknown broker outcome')
 with pytest.raises(RuntimeError):
  asyncio.run(b._process_strategy_signals([_proto_sig(),_cross_sig()]))
 assert b.execute_order_with_chaser.await_count==1
 assert b._ema_entry_decisions['ema8_prototype']['stage']=='ERROR'

def test_prototype_description_uses_actual_cadence(tmp_path):
 from analyst.test_reversal_oi_panel_8510 import _backend_stub
 b,_=_backend_stub(tmp_path,{'scanner':{'ema8_prototype':{'e8_refresh_seconds':3}}})
 b._sync_ema8_prototype_desk_status()
 assert '3s retouch' in b.strategy_status['ema8_prototype']['desc']

@pytest.mark.parametrize('route',['PAPER_ONLY','LIVE_ALLOWED'])
@pytest.mark.parametrize('latest_side',['UP','DOWN'])
def test_newer_opposite_cross_replaces_old_setup(route,latest_side):
 from analyst.test_four_algo_corrections_8510 import _frame,_closed_e8,_tick,DAY
 from analyst.mj_sir_stage_reversal_8510 import load_mj_cfg,evaluate_mj_sir_four_pack
 deltas=[40,40,-80,-80] if latest_side=='DOWN' else [-40,-40,80,80]
 bars=_frame([25000.0]*20+[25000.0+x for x in deltas])
 sigs,status=evaluate_mj_sir_four_pack(bars,bar_ci=-2,cfg=load_mj_cfg({}),bb_stage2=False,bb_stage3=False,bb_cross=False,
  party_switch_enabled=False,active_party='NEUTRAL',mj_state={},session_day=DAY,
  dynamic=_tick(_closed_e8(bars)),scanner_cfg={'reversal':{'execution_mode':route}})
 assert not any(s['strategy_id'].startswith('CROSSOVER') for s in sigs)
 row=status['crossover_reversal']
 assert 'Wait' in row['block_reason']
 assert row['reversal_side']==latest_side
