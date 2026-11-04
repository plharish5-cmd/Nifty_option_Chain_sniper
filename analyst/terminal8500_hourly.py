"""Adapter to the existing flagship evaluator, without editing its source."""
import importlib.util,sys
from pathlib import Path
from datetime import datetime
import pandas as pd
from zoneinfo import ZoneInfo
IST=ZoneInfo('Asia/Kolkata')
_MODULE=None
_HISTORY={}

def existing(root):
    global _MODULE
    if _MODULE is None:
        path=root/'algo_execution_engine/strategy_1h_80ema.py'
        spec=importlib.util.spec_from_file_location('terminal8500_existing_hourly',path)
        mod=importlib.util.module_from_spec(spec);sys.modules[spec.name]=mod;spec.loader.exec_module(mod);_MODULE=mod
    return _MODULE

def evaluate(minutes,spot,now,cfg,root,stopped=False,include_tick=True):
    known=[r for r in minutes if r[0]+60<=now]
    if not known:return dict(setup_id=4,setup='Flagship1H80 EMA',eligible=False,reasons=['No known minute data'])
    mod=existing(root)
    day=datetime.fromtimestamp(now,IST).date().isoformat()
    boundary=int(datetime.fromisoformat(day+'T00:00:00').replace(tzinfo=IST).timestamp())
    priorrows=[r for r in known if r[0]<boundary]
    todayrows=[r for r in known if r[0]>=boundary]
    key=(str(root),day,len(priorrows),priorrows[0][0] if priorrows else 0,priorrows[-1] if priorrows else ())
    if key not in _HISTORY:
        frame=pd.DataFrame(priorrows,columns=['ts','open','high','low','close','volume'])
        _HISTORY.clear();_HISTORY[key]=mod.build_1h_data(frame)
    priorhours=_HISTORY[key]
    d=pd.DataFrame(todayrows,columns=['ts','open','high','low','close','volume'])
    stamp=int(now//60*60)
    if include_tick and (d.empty or stamp>d.iloc[-1].ts):
        d.loc[len(d)]=[stamp,spot,spot,spot,spot,0]
    if d.empty:return dict(setup_id=4,setup='Flagship1H80 EMA',eligible=False,reasons=['No current session data'])
    todayhours=mod.build_1h_data(d)
    hours=pd.concat([priorhours,todayhours],ignore_index=True)
    hours['ema80']=hours['close'].ewm(span=mod.Strategy1h80EmaEvaluator().ema_period,adjust=False).mean()
    if len(hours)<cfg['hourly_warmup_bars']:return dict(setup_id=4,setup='Flagship1H80 EMA',eligible=False,reasons=['Need80 hourly observations'])
    gap=float(d.iloc[0].open-priorrows[-1][4]) if priorrows else None
    evaluator=mod.Strategy1h80EmaEvaluator();evaluator.stage_stopped_today=stopped
    signal=evaluator.check_1h_entry_signal(hours,float(spot),int(now),datetime.fromtimestamp(now,IST).time(),gap_pts=gap)
    h=hours.iloc[-1]
    return dict(setup_id=4,setup=signal.setup_name,eligible=signal.is_valid,reasons=[signal.rejection_reason or 'Existing hourly touch and bounce/rejection rules pass'],
                side=1 if signal.side=='BUY' else -1 if signal.side=='SELL' else 0,
                signal_close=int(h.ts),event_id=f'h80-{int(h.ts)}',regime=str(int(h.ts)),
                entry_kind='HOURLY_CONFIRMED_BOUNCE',risk=evaluator.fixed_sl_pts,target_risk=evaluator.target_pts,
                ema80=float(h.ema80),entry_level=None,signal_day=day,
                note='Reuses existing evaluator including forming1h candle and15:15 tail bucket. Known data only; not a completed-hour-only variant.')

def replay(minutes,day,cfg,root):
    current=[r for r in minutes if datetime.fromtimestamp(r[0],IST).date().isoformat()==day]
    prior=[r for r in minutes if datetime.fromtimestamp(r[0],IST).date().isoformat()<day]
    out=[];used=set();busy=-1;stopped=False
    # Every decision uses a completed minute; fill is the following minute open.
    for j,row in enumerate(current[:-1]):
        ts,op,hi,lo,cl,v=row
        if ts<=busy or stopped:continue
        signal=evaluate(prior+current[:j+1],cl,ts+60,cfg,root,stopped,include_tick=False)
        if not signal['eligible'] or signal['event_id'] in used:continue
        nextrow=current[j+1]
        if nextrow[0]!=ts+60:continue
        entry=nextrow[1];side=signal['side'];risk=signal['risk'];targetrisk=signal['target_risk']
        stop=entry-side*risk;target=entry+side*targetrisk;exitp=None;exitts=None;reason='OPEN_OR_MISSING_EXIT_DATA'
        for k,r in enumerate(current[j+1:]):
            stamp,op,hi,lo,close,_=r
            if datetime.fromtimestamp(stamp,IST).strftime('%H:%M')>=cfg['force_exit']:
                exitp=op;exitts=stamp;reason='SESSION_EXIT';break
            if (lo<=stop if side==1 else hi>=stop):exitp=min(stop,op) if side==1 else max(stop,op);exitts=stamp;reason='STOP';stopped=True;break
            if k>0 and (hi>=target if side==1 else lo<=target):exitp=target;exitts=stamp;reason='TARGET';break
        used.add(signal['event_id']);busy=exitts if exitts else float('inf')
        out.append(dict(setup=signal['setup'],setup_id=4,side=side,signal_close=ts+60,entry_ts=nextrow[0],entry=entry,
                        stop=stop,target=target,exit_ts=exitts,exit=exitp,reason=reason,
                        points=None if exitp is None else side*(exitp-entry),source='existing hourly evaluator, known-minute replay; not an actual fill'))
    return out
