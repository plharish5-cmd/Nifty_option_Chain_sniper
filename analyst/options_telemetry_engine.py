"""Causal, deterministic aggregate-OI telemetry. No broker or network imports.
Historical files lack live provenance; volume-based completion is explicitly heuristic.
"""
from __future__ import annotations
import csv
import json
import math
import re
import statistics
import argparse
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

RULES_PATH=Path(__file__).with_name('institutional_telemetry_rules_8510.json')
RULES=json.loads(RULES_PATH.read_text())
WINDOWS=tuple(RULES['windows']);MIN_HISTORY=RULES['min_history'];IST=ZoneInfo('Asia/Kolkata')
REQUIRED={'put_oi':'Put OI Total','call_oi':'Call OI Total','put_volume':'Put Vol','call_volume':'Call Vol','pcr':'PCR Total','future_ltp':'Future LTP','future_oi':'Future Total OI'}
CROWDING_WARNING=RULES['crowding_warning']

class Rows(list):
    errors=()

def finite(v):return isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v)
def number(v):
    try:
        x=float(str(v).replace(',','').strip())
        return x if math.isfinite(x) else None
    except (TypeError,ValueError):return None

def minute(stamp):
    if isinstance(stamp,datetime):return stamp.hour*60+stamp.minute
    if re.fullmatch(r'\d{2}:\d{2}(?::\d{2})?',str(stamp)):
        d=datetime.strptime(str(stamp)[:5],'%H:%M');return d.hour*60+d.minute
    d=datetime.fromisoformat(str(stamp).replace('Z','+00:00'))
    d=d.replace(tzinfo=IST) if d.tzinfo is None else d.astimezone(IST)
    return d.hour*60+d.minute

def canonical(raw):
    r={'time':raw.get('time',raw.get('Time',raw.get('timestamp')))}
    r['_minute']=minute(r['time']);r['_source_timestamp']=None
    if len(str(r['time']))>8:
        d=datetime.fromisoformat(str(r['time']).replace('Z','+00:00'));r['_source_timestamp']=(d.replace(tzinfo=IST) if d.tzinfo is None else d.astimezone(IST)).isoformat()
    for key,title in REQUIRED.items():r[key]=number(raw.get(key,raw.get(title)))
    for key,title in (('put_oi_day_chg','Put OI Change (Day)'),('call_oi_day_chg','Call OI Change (Day)')):r[key]=number(raw.get(key,raw.get(title)))
    r['pe_ce_oi_total']=r['put_oi']-r['call_oi'] if all(finite(r[k]) for k in ('put_oi','call_oi')) else None
    r['pe_ce_oi_day_total']=r['put_oi_day_chg']-r['call_oi_day_chg'] if all(finite(r[k]) for k in ('put_oi_day_chg','call_oi_day_chg')) else None
    return r

def load_completed_rows(path):
    p=Path(path)
    with p.open(encoding='utf-8-sig',newline='') as h:
        raw=json.load(h) if p.suffix.lower()=='.json' else list(csv.DictReader(h,delimiter='\t' if p.suffix.lower()=='.tsv' else ','))
    if isinstance(raw,dict):raw=raw.get('rows',raw.get('data',[]))
    if not isinstance(raw,list):raise ValueError('Expected a row list')
    out=Rows();errors=[];seen={}
    for item in raw:
        if not isinstance(item,dict):errors.append('Malformed row object');continue
        if str(item.get('Time',item.get('time',''))).strip() in ('Prev Day',''):continue
        try:r=canonical(item)
        except (ValueError,TypeError,OverflowError):errors.append('Invalid source timestamp');continue
        key=r['_source_timestamp'] or r['_minute']
        if key in seen and seen[key]!=r:errors.append('Conflicting duplicate timestamp')
        seen[key]=r
    out.extend(sorted(seen.values(),key=lambda r:r['_source_timestamp'] or str(r['time'])))
    out.errors=tuple(errors);return out

def clamp(v,lo=-1,hi=1):return max(lo,min(hi,v))
def sign(v):return 1 if finite(v) and v>0 else -1 if finite(v) and v<0 else 0

def scale(rows,key,floor):
    values=[b[key]-a[key] for a,b in zip(rows[-61:],rows[-60:]) if finite(a.get(key)) and finite(b.get(key))]
    if not values:return floor
    mid=statistics.median(values)
    return max(statistics.median(abs(v-mid) for v in values)*RULES['mad_factor'],floor)

def consecutive(rows):
    return all(b['_minute']-a['_minute']==1 and (not a.get('_source_timestamp') or not b.get('_source_timestamp') or (datetime.fromisoformat(b['_source_timestamp'])-datetime.fromisoformat(a['_source_timestamp'])).total_seconds()==60) for a,b in zip(rows,rows[1:]))

def window_features(rows,minutes):
    if len(rows)<minutes+1:return {'valid':False,'reason':f'{minutes}m requires {minutes+1} observations'}
    w=rows[-(minutes+1):]
    if not consecutive(w):return {'valid':False,'reason':f'{minutes}m observations contain a time gap'}
    invalid=[key for key in REQUIRED if any(not finite(r.get(key)) for r in w)]
    if invalid:return {'valid':False,'reason':f'{minutes}m missing/nonfinite fields: '+', '.join(invalid)}
    first,last=w[0],w[-1]
    result={'valid':True,'observations':len(w),'span_minutes':last['_minute']-first['_minute'],
      'price_change':last['future_ltp']-first['future_ltp'],'future_oi_change':last['future_oi']-first['future_oi'],
      'put_oi_change':last['put_oi']-first['put_oi'],'call_oi_change':last['call_oi']-first['call_oi'],
      'pcr_change':last['pcr']-first['pcr'],'put_volume':sum(r['put_volume'] for r in w[1:]),'call_volume':sum(r['call_volume'] for r in w[1:]),
      'price_range':max(r['future_ltp'] for r in w)-min(r['future_ltp'] for r in w)}
    result['pe_ce_oi_change']=result['oi_flow_gap']=last['pe_ce_oi_total']-first['pe_ce_oi_total']
    result['pe_ce_day_change_delta']=last['pe_ce_oi_day_total']-first['pe_ce_oi_day_total'] if finite(last.get('pe_ce_oi_day_total')) and finite(first.get('pe_ce_oi_day_total')) else None
    volumes=[];prior=rows[:-minutes]
    for end in range(len(prior),minutes-1,-minutes):
        block=prior[end-minutes:end]
        if consecutive(block) and all(finite(r.get('put_volume')) and finite(r.get('call_volume')) for r in block):volumes.append(sum(r['put_volume']+r['call_volume'] for r in block))
    baseline=statistics.median(volumes) if volumes else None
    result['volume_speed']=(result['put_volume']+result['call_volume'])/baseline if baseline and baseline>0 else None
    result['volume_baseline_blocks']=len(volumes)
    return result

def regime(price,oi,ps,os):
    if abs(price)<ps*RULES['regime_threshold'] or abs(oi)<os*RULES['regime_threshold']:return 'NEUTRAL'
    return 'LONG_BUILDUP' if price>0 and oi>0 else 'SHORT_COVERING' if price>0 else 'SHORT_BUILDUP' if oi>0 else 'LONG_UNWINDING'

def transition_watch(rows):
    """Prove each of five consecutive completed prefix states and a preceding opposite 5m slope."""
    n=RULES['persistence_minutes']
    if len(rows)<MIN_HISTORY+n-1 or not consecutive(rows[-n:]):return False
    direction=None
    for back in range(n):
        prefix=rows[:len(rows)-back];ws={m:window_features(prefix,m) for m in (5,15,30,60)}
        if not ws[5]['valid'] or not ws[15]['valid']:return False
        os=scale(prefix,'pe_ce_oi_total',RULES['oi_floor']);threshold=os*RULES['clear_slope_fraction']
        short=[sign(ws[m]['pe_ce_oi_change']) if abs(ws[m]['pe_ce_oi_change'])>=threshold else 0 for m in (5,15)]
        if short[0]==0 or short[1]!=short[0] or sign(ws[5]['price_change'])!=short[0]:return False
        if direction is not None and direction!=short[0]:return False
        direction=short[0]
        if not any(ws[m]['valid'] and sign(ws[m]['pe_ce_oi_change'])==-direction for m in (30,60)):return False
    # Walk back through the uninterrupted short-slope run until its actual opposite predecessor.
    for end in range(len(rows)-n,5,-1):
        prefix=rows[:end];w=window_features(prefix,5)
        if not w['valid']:return False
        s=sign(w['pe_ce_oi_change'])
        if s!=direction:return s==-direction
    return False

def clean(v):
    if isinstance(v,dict):return {k:clean(x) for k,x in v.items() if not k.startswith('_')}
    if isinstance(v,list):return [clean(x) for x in v]
    if isinstance(v,float) and not math.isfinite(v):return None
    return v

def build_telemetry(rows,reference_now=None,discard_partial_bar=True,premium_skew=None,strike_level_data=None,live=False,chain_snapshot=None):
    warnings=list(getattr(rows,'errors',()));data=[]
    for raw in rows:
        try:data.append(dict(raw) if '_minute' in raw else canonical(raw))
        except (ValueError,TypeError,OverflowError):warnings.append('Invalid source timestamp')
    data.sort(key=lambda r:r['_source_timestamp'] or str(r['time']))
    partial=False;ratio=None
    if len(data)>=RULES['partial_prior_bars']+1:
        volumes=[r.get('put_volume',0)+r.get('call_volume',0) for r in data[-11:] if finite(r.get('put_volume')) and finite(r.get('call_volume'))]
        if len(volumes)==11:
            baseline=statistics.median(volumes[:-1]);ratio=volumes[-1]/baseline if baseline>0 else None
            partial=ratio is not None and ratio<RULES['partial_ratio']
    discarded=bool(partial and discard_partial_bar)
    if discarded:data=data[:-1]
    latest=dict(data[-1]) if data else {k:None for k in REQUIRED}
    for key in ('pe_ce_oi_total','pe_ce_oi_day_total'):latest.setdefault(key,None)
    latest['pe_ce_oi_change']=latest['pe_ce_oi_total']-data[-2]['pe_ce_oi_total'] if len(data)>1 and consecutive(data[-2:]) and all(finite(r.get('pe_ce_oi_total')) for r in data[-2:]) else None
    stale=0.0;source_verified=bool(latest.get('_source_timestamp'))
    if live and reference_now is None:reference_now=datetime.now(IST)
    if reference_now is not None:
        now=datetime.fromisoformat(reference_now) if isinstance(reference_now,str) else reference_now
        if isinstance(now,(int,float)):now=datetime.fromtimestamp(now,IST)
        now=now.replace(tzinfo=IST) if now.tzinfo is None else now.astimezone(IST)
        if source_verified:stamp=datetime.fromisoformat(latest['_source_timestamp'])
        elif latest.get('time'):
            stamp=now.replace(hour=latest['_minute']//60,minute=latest['_minute']%60,second=0,microsecond=0)
        else:stamp=None
        stale=(now-stamp).total_seconds() if stamp else None
    if live and not source_verified:warnings.append('Live source session date is unverified')
    if stale is None or stale<0:warnings.append('Source timestamp is missing or in the future')
    elif stale>RULES['max_stale_seconds']:warnings.append('Source data stale: more than 120 seconds')
    completed=not partial or discarded
    if not completed:warnings.append('Latest suspected partial bar was not discarded')
    if partial:warnings.append('Low-volume partial-bar heuristic detected; completion is not an exchange flag')
    required=bool(data) and all(all(finite(r.get(k)) and (r[k]>=0 if k not in ('pcr',) else r[k]>=0) for k in REQUIRED) for r in data[-61:])
    if not required:warnings.append('Required fields missing, negative or nonfinite')
    if len(data)<MIN_HISTORY:warnings.append('Insufficient history: need 61 completed observations')
    windows={f'{m}m':window_features(data,m) for m in WINDOWS}
    for m in (5,15):
        if not windows[f'{m}m']['valid']:warnings.append(windows[f'{m}m']['reason'])
    # All four scoring windows need real complete spans; a long-window gap is not padded.
    for m in (30,60):
        if not windows[f'{m}m']['valid']:warnings.append(windows[f'{m}m']['reason'])
    if chain_snapshot and source_verified:
        try:
            ts=datetime.fromisoformat(latest['_source_timestamp']).timestamp()
            if abs(chain_snapshot['timestamp']-ts)<=60 and all(finite(chain_snapshot.get(k)) for k in ('ce_ltp','pe_ltp','ce_oi','pe_oi')):
                strike_level_data=dict(chain_snapshot)
                premium_skew={'ce_ltp':chain_snapshot['ce_ltp'],'pe_ltp':chain_snapshot['pe_ltp'],'difference':chain_snapshot['ce_ltp']-chain_snapshot['pe_ltp'],'basis':'Same observation and strike; not initiator evidence'}
        except (ValueError,TypeError,KeyError):warnings.append('Optional chain snapshot failed alignment')
    ps=scale(data,'future_ltp',RULES['price_floor']);os=max(scale(data,'future_oi',RULES['oi_floor']),scale(data,'pe_ce_oi_total',RULES['oi_floor']));pcs=scale(data,'pcr',RULES['pcr_floor'])
    fp=op=pp=0.0;votes=[];structures={}
    for key,weight in RULES['weights'].items():
        w=windows[key]
        if not w['valid']:structures[key]='NOT_AVAILABLE';continue
        m=int(key[:-1]);structure=regime(w['price_change'],w['future_oi_change'],ps,os);w['futures_regime']=structures[key]=structure
        a=RULES['future_weights'][structure];b=clamp(w['pe_ce_oi_change']/(os*math.sqrt(m)));c=clamp(w['pcr_change']/(pcs*math.sqrt(m)))
        fp+=35*weight*a;op+=30*.75*weight*b;pp+=15*weight*c
        subtotal=35*a+30*.75*b+15*c;votes.append(sign(subtotal) if abs(subtotal)>RULES['direction_threshold'] else 0)
    magnitude=latest.get('pe_ce_oi_day_total') if finite(latest.get('pe_ce_oi_day_total')) else latest.get('pe_ce_oi_total')
    if finite(magnitude):op+=30*.25*clamp(magnitude/RULES['magnitude_scale'])
    majority=1 if votes.count(1)>votes.count(-1) else -1 if votes.count(-1)>votes.count(1) else 0
    agree=votes.count(majority) if majority else 0;agreement=agree/4*100
    ap=majority*(10 if agree==4 else 6 if agree==3 else 0)
    speed=windows['15m'].get('volume_speed');vmag=10 if finite(speed) and speed>=1.25 else 6 if finite(speed) and speed>=.75 else 0
    w15=windows['15m'];churn=w15['valid'] and abs(w15['pe_ce_oi_change'])<os*.25 and abs(w15['future_oi_change'])<os*.25
    if churn:vmag=0
    subtotal=fp+op+pp;ds=1 if subtotal>2 else -1 if subtotal< -2 else 0;vp=ds*vmag
    score=round(clamp(fp+op+pp+ap+vp,-100,100),2)
    gap=latest.get('pe_ce_oi_total');gap_lakh=gap/100000 if finite(gap) else None
    mag=abs(gap_lakh) if gap_lakh is not None else 0
    band='EXTREME' if mag>=30 else 'STRONG' if mag>=20 else 'MODERATE' if mag>=10 else 'BALANCED'
    crowd=mag>=30
    if crowd:warnings.append(CROWDING_WARNING)
    penalties={};cfg=RULES['confidence_penalties']
    if premium_skew is None:penalties['premium_skew']=cfg['premium_skew']
    if strike_level_data is None:penalties['strike_level_data']=cfg['strike_level_data']
    if not finite(speed) or speed<.75:penalties['weak_volume']=cfg['weak_volume']
    if mag<10:penalties['balanced_oi']=cfg['balanced_oi']
    if sum(v in ('SHORT_COVERING','LONG_UNWINDING') for v in structures.values())>=3:penalties['covering_unwinding']=cfg['covering_unwinding'];warnings.append('Covering/unwinding dominates; fresh buildup is not established')
    if agreement<75:penalties['disagreement']=cfg['disagreement']
    confidence=round(clamp(min(95,abs(score)*.7+agreement*.3)-sum(penalties.values()),0,95),2)
    bad=(not required or len(data)<61 or not completed or stale is None or stale<0 or stale>120 or (live and not source_verified) or bool(getattr(rows,'errors',())) or any(not windows[f'{m}m']['valid'] for m in (5,15,30,60)))
    day='BULLISH' if score>=20 else 'BEARISH' if score<=-20 else 'NEUTRAL_MIXED'
    if abs(score)>=55 and agreement>=75 and finite(speed) and speed>=.75 and structures.get('15m')==('LONG_BUILDUP' if score>0 else 'SHORT_BUILDUP'):day='STRONG_'+day
    persistence=transition_watch(data) if not bad else False
    if persistence:day='TRANSITION_REVERSAL_WATCH'
    if bad:score=0;confidence=min(confidence,20);day='DATA_UNRELIABLE'
    dominance='put-side OI dominance' if finite(gap) and gap>0 else 'call-side OI dominance' if finite(gap) and gap<0 else 'balanced OI'
    quality={'completed_bar_used':completed,'partial_bar_flag':partial,'partial_latest_row_discarded':discarded,'partial_volume_ratio':ratio,'required_fields_present':required,'history_rows_used':len(data),'stale_data_seconds':stale,'source_date_verified':source_verified,'mode':'LIVE' if live else 'HISTORICAL_FILE','completion_basis':'Historical/volume heuristic, not exchange completion proof'}
    packet={'timestamp':latest.get('_source_timestamp') or latest.get('time'),'data_quality':quality,'latest':clean(latest),'windows':clean(windows),'optional':{'premium_skew':premium_skew,'strike_level_data':strike_level_data}}
    extras={'pe_ce_oi_total':gap,'pe_ce_oi_change':latest['pe_ce_oi_change'],'pe_ce_gap_lakh':gap_lakh,'volume_speed_5m':windows['5m'].get('volume_speed'),'volume_speed_15m':speed,'futures_price_range_15m':w15.get('price_range'),'futures_price_range_30m':windows['30m'].get('price_range'),'cross_window_agreement_pct':agreement,'partial_bar_flag':partial,'stale_data_seconds':stale}
    for m in (5,15,30,60):extras[f'pe_ce_oi_change_{m}m']=windows[f'{m}m'].get('pe_ce_oi_change')
    packet.update(extras)
    level='NOT_AVAILABLE' if not finite(speed) else 'WEAK' if speed<.75 else 'NORMAL' if speed<1.25 else 'ELEVATED' if speed<1.75 else 'HIGH'
    result={'timestamp':packet['timestamp'],'data_status':'DATA_UNRELIABLE' if bad else 'VALID','day_regime':day,'bias_score_pct':score,'confidence_pct':confidence,
      'oi_dominance':{'interpretation':dominance,'gap_lakh':gap_lakh,'magnitude_band':band,'slope':w15.get('pe_ce_oi_change'),'crowding_risk':crowd},
      'futures_structure':structures,'participation':{'volume_speed':speed,'level':level,'churn':churn},
      'confirmation':{'cross_window_agreement_pct':agreement,'transition_persistence_proven':persistence,'premium_skew_status':'AVAILABLE' if premium_skew is not None else 'NOT_AVAILABLE','strike_level_status':'AVAILABLE' if strike_level_data is not None else 'NOT_AVAILABLE','confidence_penalties':penalties,'score_components':{'futures_points':fp,'pe_ce_points':op,'pcr_points':pp,'agreement_points':ap,'volume_points':vp}},
      'summary':'Data unreliable; no directional conclusion.' if bad else f'{dominance}; {day}; aggregate OI-flow proxy only.',
      'warnings':list(dict.fromkeys(warnings)),'input_telemetry':packet}
    result.update(extras);return clean(result)

def main():
    parser=argparse.ArgumentParser();parser.add_argument('path');parser.add_argument('--reference-now');parser.add_argument('--keep-partial',action='store_true');parser.add_argument('--live',action='store_true');args=parser.parse_args()
    try:result=build_telemetry(load_completed_rows(args.path),reference_now=args.reference_now,discard_partial_bar=not args.keep_partial,live=args.live)
    except (OSError,ValueError,TypeError,csv.Error,json.JSONDecodeError) as exc:parser.error(str(exc))
    print(json.dumps(result,indent=2,allow_nan=False))
if __name__=='__main__':main()
