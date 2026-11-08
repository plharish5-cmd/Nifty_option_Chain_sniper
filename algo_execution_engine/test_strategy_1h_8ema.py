import pandas as pd
from datetime import datetime,time as dtime,timedelta
from zoneinfo import ZoneInfo
from algo_execution_engine.strategy_1h_8ema import build_1h_data,Strategy1h8EmaEvaluator
from algo_execution_engine.test_setup1_engine import position

def hours(days=3):
 rows=[]
 for day in range(days):
  start=datetime(2026,11,3+day,9,15,tzinfo=ZoneInfo('Asia/Kolkata'))
  for minute in range(375):
   p=25000+day*10+minute*.1;rows.append(dict(ts=int((start+timedelta(minutes=minute)).timestamp()),open=p,high=p+1,low=p-1,close=p,volume=100))
 return pd.DataFrame(rows)
def test_no_stub_and_frozen_8ema_without_lookahead():
 df=hours();a=build_1h_data(df);assert len(a)==18;assert a['is_closed'].all();assert 'ema8'in a and 'ema80'not in a
 # Change forminghourclose radically: completed-hour EMA must stay fixed.
 prefix=df.iloc[:-100].copy();x=build_1h_data(prefix);prefix.loc[prefix.index[-1],'close']+=1000;y=build_1h_data(prefix)
 assert not x.iloc[-1].is_closed;assert x.iloc[-1].ema8==y.iloc[-1].ema8

def frame(prior=25020,low=24999,high=25020):
 return pd.DataFrame([dict(ts=i*3600,open=25020,high=25030,low=25010,close=prior,ema8=25000)for i in range(8)]+[dict(ts=30000,open=25000,high=high,low=low,close=25009,ema8=25000)])
def test_mutually_exclusive_long_short_and_bounce():
 e=Strategy1h8EmaEvaluator();s=e.check_1h_entry_signal(frame(),25009,30500,dtime(11,30),0);assert s.is_valid and s.side=='BUY'
 s=e.check_1h_entry_signal(frame(prior=24980,high=25001,low=24980),24991,30500,dtime(11,30),0);assert s.is_valid and s.side=='SELL'
 assert not e.check_1h_entry_signal(frame(low=24999),25002,30500,dtime(11,30),0).is_valid
 assert not e.check_1h_entry_signal(frame().iloc[:2],25009,30500,dtime(11,30),0).is_valid

def test_incomplete_hours_not_used_and_gap_cutoff():
 df=hours().drop(index=10);a=build_1h_data(df);assert not a.iloc[0].is_closed
 e=Strategy1h8EmaEvaluator();s=e.check_1h_entry_signal(frame(low=25050,high=25080),25070,30500,dtime(11,30),65);assert not s.is_valid and 'Gap Rule'in s.rejection_reason
 assert not e.check_1h_entry_signal(frame(),25009,30500,dtime(15,1),0).is_valid

def test_hour_ctc25_buffer9_before_target():
 e=Strategy1h8EmaEvaluator();p=position();ex,_,sl=e.evaluate_active_position(p,25025,200);assert not ex;assert sl==25009
 p.spot_sl=sl;ex,reason,_=e.evaluate_active_position(p,25040,200);assert ex and 'Target'in reason
