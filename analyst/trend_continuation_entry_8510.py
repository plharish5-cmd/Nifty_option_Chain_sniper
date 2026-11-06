"""Closed 15m research/advisory signals. Never imports a broker or modifies positions."""
from pathlib import Path
import json
import pandas as pd
ROOT=Path(__file__).resolve().parents[1]

def config(overrides=None):
    base=json.loads((ROOT/'analyst/terminal8510_config.json').read_text()).get('trend_capture_v192',{})
    return {**base,**(overrides or {}),'execution_action_mode':'ADVISORY_ONLY'}

def closed_bars(minutes,asof=None):
    if minutes.empty:return pd.DataFrame()
    f=minutes.copy();f.index=pd.to_datetime(f.ts,unit='s',utc=True).dt.tz_convert('Asia/Kolkata')
    clock=f.index.hour*60+f.index.minute
    f=f[(clock>=555)&(clock<930)]
    b=f.resample('15min').agg({'ts':'nunique','o':'first','h':'max','l':'min','c':'last','v':'sum'})
    b=b[b.ts==15].copy();b['end']=b.index.astype('int64')//10**9+900
    if asof is not None:b=b[b.end<=asof]
    b['day']=b.index.strftime('%Y-%m-%d');b['ema4']=b.c.ewm(span=4,adjust=False).mean();b['ema8']=b.c.ewm(span=8,adjust=False).mean()
    tr=pd.concat([b.h-b.l,(b.h-b.c.shift()).abs(),(b.l-b.c.shift()).abs()],axis=1).max(axis=1)
    b['atr']=tr.ewm(alpha=1/14,adjust=False).mean();b['sep_atr']=(b.ema4-b.ema8).abs()/b.atr.where(b.atr>0)
    mid=b.c.rolling(20).mean();sd=b.c.rolling(20).std();b['upper_slope']=(mid+2*sd).diff();b['lower_slope']=(mid-2*sd).diff();b['width']=4*sd
    b['slope8']=b.ema8.diff();sgn=(b.ema4>b.ema8).astype(int)-(b.ema4<b.ema8).astype(int)
    b['alignment3']=(sgn==sgn.shift())&(sgn==sgn.shift(2))&(sgn!=0)
    return b

def opening_range_states(bars,cfg=None):
    cfg=config(cfg);out={}
    for day,g in bars.groupby('day'):
        opening=g[g.index.strftime('%H:%M')<'10:00']
        valid=len(opening)==3 and list(opening.index.strftime('%H:%M'))==['09:15','09:30','09:45']
        hi=float(opening.h.max()) if valid else None;lo=float(opening.l.min()) if valid else None
        pending=None;confirmed=False;direction=None;break_time=None
        for idx,b in g.iterrows():
            if valid and idx.strftime('%H:%M')>='10:00' and not confirmed:
                side='CE' if b.c>hi else 'PE' if b.c<lo else None
                # A close back inside resets the candidate; only the immediately subsequent bar can hold it.
                if pending and side==pending['side'] and pending['qualifies']:
                    confirmed=True;direction=side;break_time=pending['time']
                elif side:
                    vol=b.get('slot_rvol_median');volume_ok=pd.notna(vol) and vol>=cfg['rvol_threshold']
                    ribbon=b.sep_atr>cfg['wide_separation_atr'] and (b.slope8>0 if side=='CE' else b.slope8<0)
                    pending={'side':side,'time':int(b.end),'qualifies':bool(volume_ok or ribbon)}
                else:pending=None
            out[int(b.end)]={'or_high':hi,'or_low':lo,'or_range_pts':hi-lo if valid else None,'trend_day_confirmed':confirmed,
                'or_break_direction':'BULLISH' if direction=='CE' else 'BEARISH' if direction=='PE' else None,
                'or_break_time':break_time,'or_status':'OBSERVED' if valid else 'MISSING_OPENING_RANGE'}
    return out

def continuation_readiness(bar,state,cfg=None,entries_today=0,same_direction_open=False):
    cfg=config(cfg);side='CE' if bar['ema4']>bar['ema8'] else 'PE';bull=side=='CE'
    tol=cfg['4ema_touch_tolerance_pts']
    reclaim=(bar['l']<=bar['ema4']+tol and bar['h']>=bar['ema4']-tol and bar['c']>bar['ema4']) if bull else (bar['h']>=bar['ema4']-tol and bar['l']<=bar['ema4']+tol and bar['c']<bar['ema4'])
    checks={'alignment3':bool(bar['alignment3']),'wide_separation':bool(bar['sep_atr']>cfg['wide_separation_atr']),
      'pullback_reclaim':bool(reclaim),'ema8_intact':bool(bar['c']>=bar['ema8'] if bull else bar['c']<=bar['ema8']),
      'early_trend_confirmed':bool(state.get('trend_day_confirmed') and state.get('or_break_direction')==('BULLISH' if bull else 'BEARISH')),
      'entry_capacity':entries_today<cfg['max_continuation_entries_per_day'] and not same_direction_open}
    return {'ready':all(checks.values()),'side':side,'checks':checks,'advisory_entry':all(checks.values()) and cfg.get('continuation_entry_enabled',False),'execution_authorized':False}

def historical_signals(bars,cfg=None,include_continuation=True):
    cfg=config(cfg);states=opening_range_states(bars,cfg);signals=[];counts={};busy={}
    for idx,b in bars.iterrows():
        day=b.day;ts=int(b.end);clock=idx.strftime('%H:%M')
        if clock>='15:00':continue
        # Research occupancy ends on initial risk / closed 8EMA violation, never on future EOD labels.
        for side,pos in list(busy.items()):
            if pos['day']!=day or (b.l<=pos['entry']-cfg['initial_spot_sl_pts'] or b.c<b.ema8 if side=='CE' else b.h>=pos['entry']+cfg['initial_spot_sl_pts'] or b.c>b.ema8):busy.pop(side)
        side='CE' if b.ema4>b.ema8 else 'PE';bull=side=='CE'
        stage2=(b.upper_slope>cfg['stage2_band_slope_pts'] and b.width>=cfg['stage2_bb_width_pts'] and b.c>=b.ema8-cfg['stage2_tolerance_pts'] and (abs(b.c-b.ema8)<=cfg['stage2_tolerance_pts'] or abs(b.l-b.ema8)<=cfg['stage2_tolerance_pts'])) if bull else (b.lower_slope<-cfg['stage2_band_slope_pts'] and b.width>=cfg['stage2_bb_width_pts'] and b.c<=b.ema8+cfg['stage2_tolerance_pts'] and (abs(b.c-b.ema8)<=cfg['stage2_tolerance_pts'] or abs(b.h-b.ema8)<=cfg['stage2_tolerance_pts']))
        if stage2:signals.append({'ts':ts,'day':day,'side':side,'spot_entry':float(b.c),'setup':'STAGE_2' if bull else 'STAGE_3','state':states[ts]})
        readiness=continuation_readiness(b,states[ts],cfg,counts.get(day,0),side in busy)
        if include_continuation and readiness['ready']:
            signals.append({'ts':ts,'day':day,'side':side,'spot_entry':float(b.c),'setup':'STAGE_2B','state':states[ts]})
            counts[day]=counts.get(day,0)+1;busy[side]={'entry':float(b.c),'day':day}
    return signals,states
