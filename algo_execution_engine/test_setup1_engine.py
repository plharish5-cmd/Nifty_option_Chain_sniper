"""Owner-authorized replacement for retired5m assumptions; originals in0650c1c."""
import asyncio
import pandas as pd
from datetime import time as dtime
from algo_execution_engine.strategy_setup1 import Setup1StrategyEvaluator,ActivePosition,prepare_frame,load_config
from algo_execution_engine.ema8_catcher import Ema8CatchingState,update_or_arm_state,check_intrabar_touch,should_enter_or_skip
from algo_execution_engine.server import app
from fastapi.testclient import TestClient

def frame():
 return pd.DataFrame([dict(ts=1000+i*900,open=23000+i*12-2,high=23000+i*12+5,low=23000+i*12-5,close=23000+i*12,volume=1000)for i in range(30)])
def position():
 return ActivePosition(trade_id='p',symbol='NIFTY',security_id='123',option_type='CE',strike=25000,quantity=65,entry_spot=25000,entry_option_price=200,entry_time='2026-11-06 10:00:00',entry_ts=1,spot_sl=24960,spot_target=25040,option_sl=176,option_target=224,delta=.6)
def test_pure_15m_trend():
 e=Setup1StrategyEvaluator();ok,info=e.evaluate_15m_preconditions(frame());assert ok;assert info['ema4']>info['ema8'];assert info['ubb_slope']=='UP';assert not hasattr(e,'evaluate_5m_preconditions')
def test_first_touch_counted_even_vwap_rejects():
 e=Setup1StrategyEvaluator();df=frame();f=prepare_frame(df,e.cfg);ema=float(f.iloc[-2].ema8)
 s=e.check_setup1_entry_signal(df,ema,1000,dtime(10,30),current_vwap=ema+100)
 assert not s.is_valid;assert e.rally.touch_count==1
 # Repeating same physical bar cannot reset rally/counter or create second opportunity.
 e.check_setup1_entry_signal(df,ema,1001,dtime(10,30),current_vwap=ema+100);assert e.rally.touch_count==1
 df2=pd.concat([df,pd.DataFrame([dict(df.iloc[-1],ts=int(df.iloc[-1].ts)+900,close=float(df.iloc[-1].close)+12)])],ignore_index=True)
 f=prepare_frame(df2,e.cfg);ema=float(f.iloc[-2].ema8)
 s=e.check_setup1_entry_signal(df2,ema,1100,dtime(10,45));assert not s.is_valid;assert e.rally.touch_count==2

def test_symmetric_touch_uses_frozen_closed_ema():
 e=Setup1StrategyEvaluator();df=frame();ema=float(prepare_frame(df,e.cfg).iloc[-2].ema8)
 assert e.evaluate_8ema_touch(df,ema+2)[0];assert e.evaluate_8ema_touch(df,ema-2)[0]
 assert not e.evaluate_8ema_touch(df,ema+4)[0];assert not e.evaluate_8ema_touch(df,ema-4)[0]
def test_ctc25_buffer9_precedes_target40_and_never_loosens():
 e=Setup1StrategyEvaluator();p=position();ex,_,sl=e.evaluate_active_position(p,25025,200)
 assert not ex;assert p.cost_to_cost_active;assert sl==25009;p.spot_sl=sl
 ex,_,sl=e.evaluate_active_position(p,25020,200);assert not ex;assert sl is None
 ex,reason,_=e.evaluate_active_position(p,25040,200);assert ex and 'Target' in reason

def test_catcher_not_consumed_until_success_and_chase_terminal():
 s=Ema8CatchingState('15m');update_or_arm_state(s,1,25000,25002,1);check_intrabar_touch(s,25010,24999,25008,2)
 assert should_enter_or_skip(s,25008,2)[0];assert not s.action_taken_for_bar
 assert should_enter_or_skip(s,25008,3)[0] # gateway may have lacked quote on previous tick.
 s.action_taken_for_bar=True;assert not should_enter_or_skip(s,25008,4)[0]
 s=Ema8CatchingState('15m');update_or_arm_state(s,1,25000,25002,1);check_intrabar_touch(s,25020,24999,25019,2)
 assert not should_enter_or_skip(s,25019,2)[0];assert s.action_taken_for_bar

def test_admin_and_router_ports():
 client=TestClient(app);assert client.get('/health').json()['port']==8500
 assert client.get('/api/algo/status').status_code==200
 assert client.post('/api/algo/mode',json={'mode':'PAUSED'}).status_code==401
 assert client.post('/api/algo/mode',json={'mode':'PAUSED'},headers={'X-Algo-Secret':'test-secret'}).status_code==200
 for path in ('/status','/positions','/stage3-4-dashboard'):assert client.get('/api/algo'+path).status_code==200
 assert client.post('/api/algo/reset-circuit-breaker',headers={'X-Algo-Secret':'test-secret'}).status_code==200
 assert client.post('/api/algo/reset-circuit-breaker',headers={'X-Algo-Secret':'test-secret'}).status_code==400
