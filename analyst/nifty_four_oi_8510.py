"""NIFTY-only four-input research. Structured authorized exports; no decoder/broker/auth."""
from __future__ import annotations
import json
import math
from datetime import datetime,timedelta
from pathlib import Path
from statistics import median
from typing import Any, Optional
from analyst.stockmojo_research_8510 import number,digest,parse_time,IST

RULES=Path(__file__).with_name('nifty_four_oi_rules_8510.json')
PRIMARY=('total_put_oi','total_call_oi','put_oi_change_today','call_oi_change_today')
def rules():return json.loads(RULES.read_text())


def normalize(payload,meta,cfg=None):
    """Meta is a public review record, never credential headers or opaque request bodies.
    Explicit named four-value exports or decoded four-column chart rows only.
    No decoding encoded envelopes; header identifiers are never treated as instrument IDs.
    """
    r=cfg or rules();flags=[]
    if meta.get('timezone')!='Asia/Kolkata':flags.append('UNVERIFIED_SOURCE_TIMEZONE')
    if meta.get('instrument')!='NIFTY':raise ValueError('NOT_NIFTY_50')
    if not meta.get('option_expiry') or not meta.get('futures_expiry'):flags.append('UNVERIFIED_EXPIRIES')
    for name in ('option_expiry','futures_expiry'):
        try:datetime.strptime(meta[name],'%Y-%m-%d')
        except (KeyError,ValueError,TypeError):flags.append('INVALID_'+name.upper())
    if meta.get('instrument_status')!='VERIFIED' or not meta.get('selection_provenance'):flags.append('UNVERIFIED_INSTRUMENT')
    if meta.get('mapping_status')!='VERIFIED' or not meta.get('mapping_provenance'):flags.append('PROVISIONAL_MAPPING')
    if meta.get('unit_status') not in ('CONTRACTS','LOTS'):flags.append('UNVERIFIED_UNITS')
    if meta.get('unit_status')=='LOTS' and (type(meta.get('contracts_per_lot')) is not int or meta['contracts_per_lot']<=0):flags.append('UNVERIFIED_LOT_SIZE')
    if not meta.get('aggregation_scope') or not meta.get('strike_selection'):flags.append('UNVERIFIED_SCOPE')
    if type(meta.get('source_interval')) is not int or meta.get('source_interval',0)<=0 or meta.get('timestamp_semantics') not in ('START','END'):flags.append('UNVERIFIED_SOURCE_TIME')
    retrieved=parse_time(meta.get('retrieved_at_ist'))
    if retrieved.tzinfo is None:raise ValueError('RETRIEVAL_TIMEZONE_REQUIRED')
    if isinstance(payload,dict) and isinstance(payload.get('data'),str):raise ValueError('AUTHORIZED_DECODING_UNAVAILABLE')
    supplied=payload.get('observations') if isinstance(payload,dict) else payload
    if not isinstance(supplied,list):raise ValueError('STRUCTURED_OBSERVATIONS_REQUIRED')
    out=[];headers=0;seen={};last_input=None;chronology=False
    for item in supplied:
        rowflags=list(flags);calculated=False
        if isinstance(item,dict) and 'source_time_ist' in item:
            t=item['source_time_ist'];values={k:number(item.get(k)) for k in (*PRIMARY,'futures_ltp')}
        elif isinstance(item,list) and len(item)==4 and isinstance(item[0],str):
            try:parse_time(item[0],'Asia/Kolkata')
            except ValueError:headers+=1;continue
            t=item[0];mapping=meta.get('column_mapping',r['field_mapping'])
            if mapping!=r['field_mapping']:rowflags.append('UNSUPPORTED_COLUMN_MAPPING')
            values={'total_call_oi':number(item[1]),'total_put_oi':number(item[2]),'futures_ltp':number(item[3]),'put_oi_change_today':None,'call_oi_change_today':None};calculated=True
        else:headers+=1;continue
        ts=parse_time(t,'Asia/Kolkata');completed=ts+timedelta(seconds=meta.get('source_interval',60)) if meta.get('timestamp_semantics')=='START' else ts
        if last_input and ts<last_input:chronology=True
        last_input=ts
        baseline=meta.get('baselines',{}).get(ts.date().isoformat(),{})
        if calculated:
            bp=number(baseline.get('total_put_oi'));bc=number(baseline.get('total_call_oi'))
            if bp is not None and bc is not None:
                values['put_oi_change_today']=values['total_put_oi']-bp if values['total_put_oi'] is not None else None
                values['call_oi_change_today']=values['total_call_oi']-bc if values['total_call_oi'] is not None else None
            if baseline.get('type') not in ('OPENING_OBSERVATION','PREVIOUS_CLOSE','PROVIDER_REFERENCE') or not baseline.get('provenance'):rowflags.append('UNVERIFIED_BASELINE')
            try:
                available=parse_time(baseline['available_at_ist'])
                if available.tzinfo is None or available>completed:rowflags.append('BASELINE_NOT_AVAILABLE_CAUSALLY')
            except (KeyError,ValueError,TypeError):rowflags.append('BASELINE_AVAILABILITY_UNKNOWN')
        if not calculated:
            if baseline.get('type') not in ('OPENING_OBSERVATION','PREVIOUS_CLOSE','PROVIDER_REFERENCE') or not baseline.get('provenance'):rowflags.append('UNVERIFIED_BASELINE')
            try:
                if parse_time(baseline['available_at_ist'])>completed:rowflags.append('BASELINE_NOT_AVAILABLE_CAUSALLY')
            except (KeyError,TypeError,ValueError):rowflags.append('BASELINE_AVAILABILITY_UNKNOWN')
        if any(values.get(k) is None for k in PRIMARY):rowflags.append('MISSING_FOUR_INPUTS')
        if any(values.get(k) is not None and values[k]<0 for k in ('total_put_oi','total_call_oi')):rowflags.append('NEGATIVE_TOTAL_OI')
        if values.get('total_call_oi')==0:rowflags.append('ZERO_CALL_DENOMINATOR')
        if meta.get('futures_identity_status')!='VERIFIED':values['futures_ltp']=None
        if values['futures_ltp'] is not None and values['futures_ltp']<=0:rowflags.append('INVALID_PRICE')
        if not calculated and baseline.get('type') in ('OPENING_OBSERVATION','PREVIOUS_CLOSE','PROVIDER_REFERENCE'):
            for side in ('put','call'):
                b=number(baseline.get('total_'+side+'_oi'))
                if b is not None and values.get('total_'+side+'_oi') is not None and values.get(side+'_oi_change_today') is not None:
                    if abs(values['total_'+side+'_oi']-b-values[side+'_oi_change_today'])>r['baseline_rounding_tolerance']:rowflags.append('NAMED_BASELINE_ARITHMETIC_MISMATCH')
        converted=values.copy()
        if meta.get('unit_status')=='LOTS' and not 'UNVERIFIED_LOT_SIZE' in rowflags:
            converted={k:v*meta['contracts_per_lot'] if k in PRIMARY and v is not None else v for k,v in values.items()}
        obs={**converted,'source_time_ist':ts.isoformat(),'completed_at_ist':completed.isoformat(),
            'retrieved_at_ist':retrieved.isoformat(),'instrument':'NIFTY','instrument_status':meta.get('instrument_status','UNVERIFIED'),
            'option_expiry':meta.get('option_expiry'),'futures_expiry':meta.get('futures_expiry'),'source_interval':meta.get('source_interval'),
            'aggregation_scope':meta.get('aggregation_scope'),'strike_selection':meta.get('strike_selection'),
            'baseline':baseline,'baseline_type':baseline.get('type','UNVERIFIED'),'baseline_provenance':baseline.get('provenance'),
            'change_origin':'CALCULATED_FROM_EXPLICIT_BASELINE' if calculated else 'PROVIDER_NAMED_FIELDS',
            'unit_status':meta.get('unit_status','UNVERIFIED'),'normalized_unit':'CONTRACTS' if meta.get('unit_status') in ('CONTRACTS','LOTS') else 'UNVERIFIED','origin':meta.get('origin','UNKNOWN'),'raw_values':values,
            'freshness':'PENDING_OR_FUTURE' if completed>retrieved else 'STALE_AT_RETRIEVAL' if (retrieved-completed).total_seconds()>r['fresh_seconds'] else 'RECENT_SOURCE_NOT_IDENTITY_PROOF',
            'mapping_status':meta.get('mapping_status','PROVISIONAL'),'quality_flags':list(dict.fromkeys(rowflags))}
        obs['verification_id']=digest({k:meta.get(k) for k in ('instrument_status','selection_provenance','mapping_status','mapping_provenance','futures_identity_status','timezone')})
        obs['identity_id']=digest({k:obs.get(k) for k in ('instrument','option_expiry','futures_expiry','aggregation_scope','strike_selection','unit_status','baseline_type','baseline_provenance','verification_id')})
        obs['event_id']=digest({'identity':obs['identity_id'],'source':obs['source_time_ist'],'values':converted,'baseline':baseline})
        key=(obs['identity_id'],obs['source_time_ist'])
        if key in seen and seen[key]['event_id']!=obs['event_id']:raise ValueError('CONFLICTING_DUPLICATE')
        seen[key]=obs
    out=sorted(seen.values(),key=lambda x:x['source_time_ist'])
    # An unannounced change in the baseline is never silently stitched into a session.
    for a,b in zip(out,out[1:]):
        if a['source_time_ist'][:10]==b['source_time_ist'][:10] and a['identity_id']!=b['identity_id']:
            b['quality_flags'].append('SCOPE_EXPIRY_BASELINE_CHANGE')
        if a['source_time_ist'][:10]==b['source_time_ist'][:10] and any(b.get(k)==0 and a.get(k) not in (0,None) for k in ('put_oi_change_today','call_oi_change_today')):b['quality_flags'].append('POSSIBLE_DAILY_RESET')
    return {'rows':out,'metadata_elements_excluded':headers,'duplicates_collapsed':len(supplied)-headers-len(out),'chronology_reordered':chronology}


def checkpoint(stamp,cfg=None):
    r=cfg or rules();t=parse_time(stamp);base=t.replace(hour=int(r['session_start'][:2]),minute=int(r['session_start'][3:]),second=0,microsecond=0)
    return t>=base and int((t-base).total_seconds())%r['checkpoint_seconds']==0 and t.second==0


def analyze_four(rows,decision_time,cfg=None,mode='IDEAL_REPLAY',position=None):
    r=cfg or rules();now=parse_time(decision_time)
    if now.tzinfo is None:raise ValueError('AWARE_DECISION_TIME_REQUIRED')
    allowed=[x for x in rows if parse_time(x['completed_at_ist'])<=now and (mode=='IDEAL_REPLAY' or parse_time(x['retrieved_at_ist'])<=now)]
    base={'market_state':'BLOCKED','trade_action':'BLOCKED','signal':'BLOCKED','score':0,'confidence_kind':'HEURISTIC_NOT_CALIBRATED','factors':{},'quality_flags':[],
          'reasons':[],'contradictions':[],'breadth':'UNAVAILABLE_DISABLED','paper_block':'REQUIRES_VERIFIED_OPTION_QUOTES','mode':mode}
    if not allowed:return {**base,'quality_flags':['NO_CAUSALLY_AVAILABLE_OBSERVATIONS']}
    last=allowed[-1];ts=parse_time(last['completed_at_ist']);flags=list(last['quality_flags'])
    same=[x for x in allowed if x['source_time_ist'][:10]==last['source_time_ist'][:10] and x['identity_id']==last['identity_id']]
    recent=[x for x in same if (ts-parse_time(x['completed_at_ist'])).total_seconds()<=r['window_seconds']]
    if last['instrument']!='NIFTY':flags.append('NOT_NIFTY_50')
    if (last.get('option_expiry') or '9999-12-31')<ts.date().isoformat() or (last.get('futures_expiry') or '9999-12-31')<ts.date().isoformat():flags.append('EXPIRED_CONTRACT_CONTEXT')
    if last['origin'] not in ('AUTHORIZED_EXPORT','HTTP_STRUCTURED_RESPONSE'):flags.append('SAMPLE_OR_UNKNOWN_ORIGIN')
    if mode=='LIVE' and ((now-ts).total_seconds()>r['fresh_seconds'] or now.date()!=ts.date()):flags.append('STALE_SOURCE')
    if mode!='IDEAL_REPLAY' and (now-ts).total_seconds()>r['fresh_seconds']:flags.append('LATE_ARRIVAL_NO_RETROACTIVE_TRADE')
    if any(x['quality_flags'] for x in recent):flags.append('UNVERIFIED_WINDOW')
    if any((parse_time(b['completed_at_ist'])-parse_time(a['completed_at_ist'])).total_seconds()>r['max_gap_seconds'] for a,b in zip(recent,recent[1:])):flags.append('GAPPED_WINDOW')
    if any(any(x.get(k) is None for k in PRIMARY) for x in recent):flags.append('MISSING_FOUR_INPUTS')
    factors={k:last.get(k) for k in PRIMARY};reasons=[];contradictions=[];state='INSUFFICIENT_DATA';action='HOLD';score=0
    if 'MISSING_FOUR_INPUTS' not in flags and last['total_call_oi']>0:
        p=last['total_put_oi'];c=last['total_call_oi'];dtgap=last['put_oi_change_today']-last['call_oi_change_today'];pcr=p/c
        factors.update(pcr=pcr,total_gap=p-c,total_dominance=(p-c)/max(p+c,1),today_gap=dtgap,
            pcr_band=sum(pcr>=b for b in r['pcr_bands']),dominant_side_total='PUT' if p>c else 'CALL' if c>p else 'BALANCED',
            dominant_side_today='PUT' if dtgap>0 else 'CALL' if dtgap<0 else 'BALANCED')
        reasons.append('Total positioning is '+factors['dominant_side_total']+'-heavy; today change level is '+factors['dominant_side_today']+'-heavy. This does not identify writers.')
        if len(recent)>=2:
            prev=recent[-2];first=recent[0];elapsed=(ts-parse_time(prev['completed_at_ist'])).total_seconds()
            if elapsed<=0:flags.append('DUPLICATE_SOURCE_TIME')
            else:
                changes={k:last[k]-prev[k] for k in PRIMARY};rates={k:changes[k]/elapsed for k in PRIMARY};old_pcr=prev['total_put_oi']/prev['total_call_oi'] if prev['total_call_oi']>0 else None
                gap_delta=changes['put_oi_change_today']-changes['call_oi_change_today'];direction=1 if gap_delta>=r['min_oi_change'] else -1 if gap_delta<=-r['min_oi_change'] else 0
                changes_list=[((b['put_oi_change_today']-a['put_oi_change_today'])-(b['call_oi_change_today']-a['call_oi_change_today']))/(parse_time(b['completed_at_ist'])-parse_time(a['completed_at_ist'])).total_seconds() for a,b in zip(recent,recent[1:])]
                trailing=changes_list[:-1];center=median(trailing) if trailing else 0;mad=median([abs(v-center) for v in trailing]) if trailing else 0
                spike= len(trailing)>=r['spike_min_history'] and abs(changes_list[-1]-center)>r['spike_multiplier']*max(mad,r['min_oi_change']/r['checkpoint_seconds'])
                accelerations={}
                if len(recent)>=3:
                    before=recent[-3];seconds=(parse_time(prev['completed_at_ist'])-parse_time(before['completed_at_ist'])).total_seconds()
                    accelerations={k:(rates[k]-(prev[k]-before[k])/seconds)/elapsed for k in PRIMARY} if seconds>0 else {}
                persist=0
                for v in reversed(changes_list):
                    if v*direction>0:persist+=1
                    else:break
                price=None
                if last.get('futures_ltp') and first.get('futures_ltp'):price=last['futures_ltp']/first['futures_ltp']-1
                factors.update(interval_changes=changes,per_second_rates=rates,accelerations=accelerations,pcr_change=pcr-old_pcr if old_pcr is not None else None,
                    pcr_slope_per_second=(pcr-old_pcr)/elapsed if old_pcr is not None else None,
                    pcr_put_component=(p-prev['total_put_oi'])/c,
                    pcr_call_component=prev['total_put_oi']*(1/c-1/prev['total_call_oi']) if prev['total_call_oi']>0 else None,
                    flow_direction=direction,persistence=persist,spike=spike,spike_mad=mad,price_return=price,
                    put_current_flow='ADDING' if changes['total_put_oi']>0 else 'REDUCING' if changes['total_put_oi']<0 else 'FLAT',
                    call_current_flow='ADDING' if changes['total_call_oi']>0 else 'REDUCING' if changes['total_call_oi']<0 else 'FLAT')
                reasons.append('Recent Put total '+factors['put_current_flow']+', Call total '+factors['call_current_flow']+'. Daily change levels and current rates are separate.')
                total_sign=1 if factors['total_dominance']>r['dominance_threshold'] else -1 if factors['total_dominance']< -r['dominance_threshold'] else 0
                if total_sign and direction and total_sign!=direction:contradictions.append('TOTAL_LEVEL_OPPOSES_RECENT_FLOW')
                if price is None:contradictions.append('PRICE_CONFIRMATION_UNAVAILABLE')
                elif direction and price*direction<0:contradictions.append('PRICE_OPPOSES_FLOW')
                if len(recent)>=r['warmup_rows']:
                    state='BULLISH_BIAS' if direction>0 else 'BEARISH_BIAS' if direction<0 else 'MIXED'
                    if direction and total_sign and total_sign!=direction:state='BULLISH_REVERSAL_CANDIDATE' if direction>0 else 'BEARISH_REVERSAL_CANDIDATE'
                    if spike:state='FLOW_SPIKE'
                    confirmed=direction and persist>=r['persistence'] and price is not None and price*direction>=r['min_price_return'] and not spike
                    # One OI family, one auxiliary price confirmation. PCR is not another vote.
                    if confirmed:action='BUY_CE' if direction>0 else 'BUY_PE';score=direction*100
                    if position:
                        sign=1 if position['side']=='CE' else -1
                        opposite=sum(v*sign<0 for v in changes_list[-r['persistence']:])==r['persistence']
                        if gap_delta*sign<=-r['exit_min_oi_change'] and opposite or price is not None and price*sign<=-r['price_exit_return']:action='EXIT'
    if state=='INSUFFICIENT_DATA':action='HOLD'
    if not checkpoint(last['completed_at_ist'] if mode=='IDEAL_REPLAY' else now.isoformat(),r):action='HOLD';reasons.append('NOT_A_COMPLETED_THREE_MINUTE_CHECKPOINT')
    hh=now.strftime('%H:%M') if mode!='IDEAL_REPLAY' else ts.strftime('%H:%M')
    if hh>=r['squareoff_time'] and position:action='EXIT';reasons.append('END_OF_DAY_EXIT_REQUIRED')
    elif hh<r['session_start'] or hh>=r['entry_cutoff'] or ts.weekday()>4:
        if action in ('BUY_CE','BUY_PE'):action='HOLD'
        reasons.append('ENTRY_WINDOW_CLOSED')
    if flags:state='BLOCKED';action='BLOCKED'
    result={**base,'market_state':state,'trade_action':action,'signal':action,'score':score,'factors':factors,'quality_flags':sorted(set(flags)),
        'reasons':reasons,'contradictions':contradictions,'source_time':last['source_time_ist'],'completed_at':last['completed_at_ist'] if mode=='IDEAL_REPLAY' else now.isoformat(),
        'instrument':{'underlying':'NIFTY','options_expiry':last['option_expiry'],'futures_expiry':last['futures_expiry']},
        'state':'LIVE_FRESH' if mode=='LIVE' and not flags else 'HISTORICAL_REPLAY','event_id':digest({'source':last['event_id'],'decision':last['completed_at_ist'] if mode=='IDEAL_REPLAY' else now.isoformat(),'mode':mode}),'config_version':r['version'],
        'four_inputs':factors|{'unit_status':last['unit_status']},'metadata':{k:last.get(k) for k in ('baseline_type','baseline_provenance','source_interval','retrieved_at_ist','aggregation_scope','option_expiry','futures_expiry','unit_status')}}
    result['input_id']=digest({'events':[x['event_id'] for x in recent],'config':r,'mode':mode,'state':state,'action':action,'flags':flags})
    return result


def desk8600_regime_from_banner(banner: Any) -> Optional[str]:
    """Parse 8600 / trend panel regime banner into BULLISH | BEARISH | None."""
    text = str(banner or "").upper()
    if "BEARISH REGIME" in text or "STRONG BEARISH" in text:
        return "BEARISH"
    if "BULLISH REGIME" in text or "STRONG BULLISH" in text:
        return "BULLISH"
    return None


def classify_fresh_oi_regime(
    s: float,
    bias: float,
    *,
    desk8600_regime: Optional[str] = None,
) -> str:
    """Fresh Change in OI regime for 8510 badges and 8600 lane gating alignment."""
    regime = str(desk8600_regime or "").upper()
    if s >= 0.15 or bias >= 0.20:
        return "BULLISH"
    if regime == "BULLISH" and bias > 0:
        return "BULLISH"
    if s <= -0.15 or bias <= -0.20:
        return "BEARISH"
    if regime == "BEARISH" and bias < 0:
        return "BEARISH"
    return "NEUTRAL"


def fresh_oi_regime_to_direction(regime: str) -> str:
    return {"BULLISH": "CE", "BEARISH": "PE"}.get(str(regime or "").upper(), "NEUTRAL")
