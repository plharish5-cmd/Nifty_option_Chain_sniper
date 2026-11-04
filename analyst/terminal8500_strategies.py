"""Causal setup rules shared by replay and live scanner. No broker operations."""
import math
import pandas as pd
from datetime import datetime
from zoneinfo import ZoneInfo
IST=ZoneInfo('Asia/Kolkata')
NAMES=['Stage 2 long','Stage 3 short','Reversal up','Reversal down']

def features(minutes,cfg):
    if not minutes:return pd.DataFrame()
    d=pd.DataFrame(minutes,columns=['ts','o','h','l','c','v'])
    t=pd.to_datetime(d.ts,unit='s',utc=True).dt.tz_convert(IST)
    d['day']=t.dt.strftime('%Y-%m-%d');d['minute']=t.dt.hour*60+t.dt.minute
    d=d[(d.minute>=555)&(d.minute<930)].sort_values('ts').drop_duplicates('ts')
    d['slot']=(d.minute-555)//cfg['bar_minutes']
    b=d.groupby(['day','slot']).agg(ts=('ts','min'),o=('o','first'),h=('h','max'),l=('l','min'),c=('c','last'),n=('ts','size'),last=('ts','max')).reset_index()
    b=b[(b.n==cfg['bar_minutes'])&(b['last']-b.ts==(cfg['bar_minutes']-1)*60)].copy().reset_index(drop=True)
    if b.empty:return b
    b['e4']=b.c.ewm(span=cfg['ema_fast'],adjust=False).mean();b['e8']=b.c.ewm(span=cfg['ema_slow'],adjust=False).mean()
    mid=b.c.rolling(cfg['bb_length']).mean();std=b.c.rolling(cfg['bb_length']).std(ddof=0)
    b['u']=mid+cfg['bb_std']*std;b['lb']=mid-cfg['bb_std']*std
    b['du']=b.u.diff();b['dl']=b.lb.diff();b['width']=b.u-b.lb
    b['sign']=(b.e4>b.e8).astype(int)-(b.e4<b.e8).astype(int)
    b['sign']=b.sign.replace(0,float('nan')).ffill().fillna(0).astype(int)
    b['regime']=(b.sign!=b.sign.shift()).cumsum();b['age']=b.groupby('regime').cumcount()+1;b['cross_ts']=b.groupby('regime').ts.transform('first')
    b['close_ts']=b.ts+cfg['bar_minutes']*60
    return b

def evaluate(b,setup,cfg):
    side=1 if setup in (0,2) else -1
    reasons=[]
    if len(b)<cfg['warmup_bars']:return dict(setup=NAMES[setup],setup_id=setup,side=side,eligible=False,reasons=['Insufficient complete15m bars'])
    p,q=b.iloc[-1],b.iloc[-2]
    if int(p.sign)!=side:reasons.append('EMA4/8 direction disagrees')
    if setup<2:
        if not float(p.width)>cfg['bb_min_width']:reasons.append('BB width not above configured minimum')
        step='du' if side==1 else 'dl'
        if not(side*float(p[step])>0 and side*float(q[step])>0):reasons.append('Relevant band not sloping in direction for two closed candles')
        entry='EMA8_TOUCH';level=float(p.e8);risk=cfg['stage_risk']
    else:
        if int(q.sign)==side:reasons.append('No new closed EMA4/8 crossover')
        entry='NEXT_OPEN';level=None;risk=cfg['reversal_risk']
    return dict(setup=NAMES[setup],setup_id=setup,side=side,eligible=not reasons,
                reasons=reasons or ['Closed-candle conditions pass; awaiting entry trigger'],
                entry_kind=entry,entry_level=level,risk=risk,
                signal_close=int(p.close_ts),regime=str(int(p.cross_ts)),signal_day=p.day,
                event_id=f"s{setup}-{int(p.close_ts)}",e4=float(p.e4),e8=float(p.e8))

def touch_allowed(signal,op,hi,lo):
    if signal['entry_kind']=='NEXT_OPEN':return True
    level=signal['entry_level'];side=signal['side']
    return side*(op-level)>0 and lo<=level<=hi

def replay(minutes,day,cfg):
    b=features(minutes,cfg);result=[];decisions=[]
    if b.empty:return dict(day=day,trades=[],decisions=[],label='HISTORICAL REPLAY — index points only')
    dayminutes=[x for x in minutes if datetime.fromtimestamp(x[0],IST).date().isoformat()==day]
    byslot={}
    for row in dayminutes:
        dt=datetime.fromtimestamp(row[0],IST);minute=dt.hour*60+dt.minute
        if 555<=minute<930:byslot.setdefault((minute-555)//cfg['bar_minutes'],[]).append(row)
    for setup in range(4):
        busy=-1;used=set();stopped=False
        for slot,rows in sorted(byslot.items()):
            start=rows[0][0];barstart=start-(datetime.fromtimestamp(start,IST).minute%cfg['bar_minutes'])*60
            # Slot anchoring is09:15;15m boundaries are also divisible by15.
            if len(rows)!=cfg['bar_minutes'] or rows[-1][0]-start!=(cfg['bar_minutes']-1)*60:continue
            if datetime.fromtimestamp(start,IST).strftime('%H:%M')>cfg['last_entry']:continue
            prev=b[b.close_ts<=start]
            sig=evaluate(prev,setup,cfg)
            if len(prev):decisions.append({**sig,'evaluated_at':start})
            if not sig['eligible'] or start<=busy or (setup<2 and stopped):continue
            regime=sig['regime']
            if regime in used:continue
            if sig['entry_kind']=='NEXT_OPEN':fill=(0,float(rows[0][1]))
            else:
                level=sig['entry_level'];side=sig['side'];fill=None
                if side*(rows[0][1]-level)<=0:continue
                for j,row in enumerate(rows):
                    if row[3]<=level<=row[2] or side*(row[1]-level)<0:
                        fill=(j,min(level,row[1]) if side==1 else max(level,row[1]));break
            if fill is None:continue
            j,entry=fill;ts=rows[j][0];risk=sig['risk'];side=sig['side'];stop=entry-side*risk;target=entry+side*risk
            future=[x for x in dayminutes if x[0]>=ts and datetime.fromtimestamp(x[0],IST).strftime('%H:%M')<=cfg['force_exit']]
            exitp=None;exitts=None;reason=None
            for k,row in enumerate(future):
                stamp,op,hi,lo,cl,_=row
                if datetime.fromtimestamp(stamp,IST).strftime('%H:%M')>=cfg['force_exit']:
                    exitp=op;exitts=stamp;reason='SESSION_EXIT';break
                hitstop=lo<=stop if side==1 else hi>=stop
                hittarget=hi>=target if side==1 else lo<=target
                if hitstop:exitp=min(stop,op) if side==1 else max(stop,op);exitts=stamp;reason='STOP';break
                if k>0 and hittarget:exitp=target;exitts=stamp;reason='TARGET';break
            if exitp is None:
                # Do not invent liquidation prices when the session/exit data are incomplete.
                reason='OPEN_OR_MISSING_EXIT_DATA'
            result.append(dict(setup=NAMES[setup],setup_id=setup,side=side,signal_close=sig['signal_close'],
                entry_ts=ts,entry=entry,stop=stop,target=target,exit_ts=exitts,exit=exitp,reason=reason,
                points=None if exitp is None else side*(exitp-entry),source='historical minute execution; not an actual fill'))
            used.add(regime);busy=exitts if exitts else float('inf')
            if setup<2 and reason=='STOP':stopped=True
    return dict(day=day,trades=sorted(result,key=lambda x:x['entry_ts']),decisions=decisions,
                label='HISTORICAL REPLAY — index points only; no option P&L inferred',
                minute_count=len(dayminutes),complete_regular_session=len(dayminutes)==375,
                assumptions='Previous closed EMA8 touch; reversal next15m open. Fixed20/40pt risk. Stop-first; entry-minute targets excluded. Setups simulated separately.')
