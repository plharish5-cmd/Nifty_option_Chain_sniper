"""Causal archive research. No broker, authentication, global account or model imports."""
from __future__ import annotations
import hashlib
import json
import math
import re
from datetime import datetime, timedelta
from pathlib import Path
from statistics import pstdev
from analyst.stockmojo_archive_adapter_8510 import ArchiveAdapter, parse_time, IST

RULES = Path(__file__).with_name('stockmojo_integration_rules_8510.json')
FIELDS = ('put_oi_day','put_oi_change','call_oi_day','call_oi_change','pe_ce_day',
          'pe_ce_change','pcr_total','future_ltp','future_oi_day','put_oi','call_oi','future_oi','put_volume','call_volume')
UNITS = {'POINTS','CONTRACTS','LOTS','RATIO'}


def number(value):
    """K/L/Cr expand display strings only. Numeric JSON is already raw magnitude."""
    if value is None or isinstance(value,bool):return None
    if isinstance(value,(int,float)):return float(value) if math.isfinite(value) else None
    if not isinstance(value,str):return None
    match=re.fullmatch(r'\s*([+\-−]?\s*(?:\d[\d,]*(?:\.\d*)?|\.\d+))\s*(Cr|L|K)?\s*',value,re.I)
    if not match:return None
    digits=match[1].replace(' ','').lstrip('+-−').split('.')[0]
    if ',' in digits:
        groups=digits.split(',')
        if not (1<=len(groups[0])<=3 and len(groups[-1])==3 and all(len(g) in (2,3) for g in groups[1:-1])):return None
    try:v=float(match[1].replace(',','').replace(' ','').replace('−','-'))*{'':1,'K':1e3,'L':1e5,'CR':1e7}[(match[2] or '').upper()]
    except ValueError:return None
    return v if math.isfinite(v) else None


def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def load_rules():return json.loads(RULES.read_text())


class ResearchArchive:
    """Integrity check precedes interpretation; manifests bind public IDs to field semantics."""
    def __init__(self,root,rules=None):
        self.root=Path(root);self.rules=rules or load_rules();self.reader=ArchiveAdapter(root)
        self.manifests=self.root/'analyst/stockmojo_verified_manifests'
    def manifest(self,request):
        # Only locally reviewed public JSON. Never inspect .env or accept client paths.
        for p in sorted(self.manifests.glob('*.json')):
            if p.is_symlink():continue
            try:
                m=json.loads(p.read_text())
                if m.get('request')==request:return m
            except (ValueError,OSError):continue
        return {}
    def read(self,path):
        raw_reader=self.reader
        meta=json.loads(raw_reader._read(path,raw_reader.rules['max_metadata_bytes']))
        raw_path=Path(str(path)[:-10]+'.json')
        content=raw_reader._read(raw_path,raw_reader.rules['max_raw_bytes'])
        if hashlib.sha256(content).hexdigest()!=meta.get('raw_sha256'):raise ValueError('RAW_HASH_MISMATCH')
        payload=json.loads(content)
        if isinstance(payload,list):
            # Named exports carry time-only labels: require an explicit sidecar source date.
            day=meta.get('source_trading_date')
            try:datetime.strptime(day,'%Y-%m-%d')
            except (TypeError,ValueError):raise ValueError('NAMED_EXPORT_SOURCE_DATE_REQUIRED') from None
            if not payload or any(not isinstance(row,dict) or 'Time' not in row for row in payload):raise ValueError('INVALID_NAMED_ROWS')
            names=sorted(set().union(*(row.keys() for row in payload))-{'Time'})
            converted={'t':[],**{k:[] for k in names}}
            for row in payload:
                label=str(row['Time']).strip()
                # Previous-day anchor needs its own explicit date, never the current day.
                rowday=meta.get('previous_trading_date') if label.lower() in ('prev day','previous day') else day
                if rowday is None:continue
                clock='15:30:00' if label.lower() in ('prev day','previous day') else label
                converted['t'].append(rowday+'T'+clock)
                for k in names:converted[k].append(row.get(k))
            payload=converted
        if not isinstance(payload,dict) or not isinstance(payload.get('t'),list):raise ValueError('MISSING_TIME_ARRAY')
        count=len(payload['t']);keys=[k for k,v in payload.items() if k!='c' and isinstance(v,list)]
        if not count or any(len(payload[k])!=count for k in keys):raise ValueError('ARRAY_LENGTH_MISMATCH')
        request=meta.get('request')
        if not isinstance(request,dict) or set(request)!={'symbol','expiry','range'} or any(type(v) is not int or v<=0 for v in request.values()):raise ValueError('INVALID_REQUEST_METADATA')
        retrieved=parse_time(meta['downloaded_at_ist'])
        if retrieved.tzinfo is None:raise ValueError('UNVERIFIED_RETRIEVAL_TIMEZONE')
        m=self.manifest(request);i=m.get('instrument',{});flags=[]
        if meta.get('origin') not in ('HTTP_200_RESPONSE','USER_SUPPLIED_SAMPLE_NOT_A_NETWORK_DOWNLOAD'):flags.append('UNKNOWN_ORIGIN')
        if m.get('review_status')!='REVIEWED' or not m.get('reviewed_by') or not all(m.get('evidence',{}).get(k) for k in ('selection','field_dictionary','timestamps')):flags.append('UNVERIFIED_PROVENANCE')
        for k in ('underlying','exchange','options_expiry','futures_expiry','futures_contract'):
            if not i.get(k):flags.append('UNVERIFIED_'+k.upper())
        for k in ('options_expiry','futures_expiry'):
            try:datetime.strptime(i[k],'%Y-%m-%d')
            except (KeyError,TypeError,ValueError):flags.append('INVALID_'+k.upper())
        if m.get('timezone')!='Asia/Kolkata' or m.get('timestamp_semantics') not in ('MINUTE_START','MINUTE_END'):flags.append('UNVERIFIED_TIME_SEMANTICS')
        if not m.get('oi_scope') or type(m.get('source_interval_seconds')) is not int or m.get('source_interval_seconds',0)<=0:flags.append('UNVERIFIED_SCOPE_INTERVAL')
        specs=m.get('field_map',{});mapped={}
        for name,spec in specs.items():
            if name not in FIELDS or not isinstance(spec,dict):continue
            if spec.get('meaning')!=name or spec.get('unit') not in UNITS or not spec.get('evidence'):flags.append('UNVERIFIED_FIELD_'+name);continue
            if spec['unit']!=('POINTS' if name=='future_ltp' else 'RATIO' if name=='pcr_total' else spec['unit']) or (name not in ('future_ltp','pcr_total') and spec['unit'] not in ('LOTS','CONTRACTS')):flags.append('INCOMPATIBLE_FIELD_UNIT_'+name);continue
            if spec['unit']=='LOTS' and (type(spec.get('contracts_per_lot')) is not int or spec['contracts_per_lot']<=0):flags.append('UNVERIFIED_LOT_SIZE');continue
            if name in ('put_oi_change','call_oi_change','pe_ce_change') and spec.get('interval_seconds')!=m.get('source_interval_seconds'):flags.append('UNVERIFIED_PROVIDER_CHANGE_INTERVAL_'+name)
            if spec.get('raw_key') not in keys:flags.append('MISSING_FIELD_'+name);continue
            mapped[name]=spec
        for key in ('future_ltp','pcr_total','put_oi_day','call_oi_day','future_oi_day'):
            if key not in mapped:flags.append('MISSING_REQUIRED_'+key)
        rows=[]
        for index,stamp in enumerate(payload['t']):
            ts=parse_time(stamp,'Asia/Kolkata');completed=ts+timedelta(seconds=m.get('source_interval_seconds',60)) if m.get('timestamp_semantics')=='MINUTE_START' else ts
            raw={k:payload[k][index] for k in keys if k!='t'}
            row={'timestamp':ts.isoformat(),'completed_at':completed.isoformat(),'raw':raw,'quality_flags':list(dict.fromkeys(flags)),
                 'instrument':i,'request':request,'scope':m.get('oi_scope'),'retrieved_at':meta.get('downloaded_at_ist'),
                 'origin':meta.get('origin'),'interval':m.get('source_interval_seconds'),'manifest_id':digest(m), 'verified':not flags}
            for name,spec in mapped.items():
                v=number(raw.get(spec['raw_key']))
                if v is not None and spec['unit']=='LOTS':v*=spec['contracts_per_lot']
                row[name]=v
                if v is None:row['quality_flags'].append('INVALID_FIELD_'+name)
            if (number(row.get('future_ltp')) is not None and row['future_ltp']<=0) or (number(row.get('pcr_total')) is not None and row['pcr_total']<0):row['quality_flags'].append('INVALID_PRICE_OR_PCR')
            if all(row.get(k) is not None for k in ('put_oi_day','call_oi_day','pe_ce_day')):
                tolerance=number(m.get('rounding_tolerance')) or 0
                if abs(row['put_oi_day']-row['call_oi_day']-row['pe_ce_day'])>tolerance:row['quality_flags'].append('PE_CE_ARITHMETIC_MISMATCH')
            row['quality_flags']=list(dict.fromkeys(row['quality_flags']));row['verified']=not row['quality_flags']
            # Retrieval time is not part of observation identity; revised rows conflict.
            row['event_id']=digest({k:row.get(k) for k in ('timestamp','instrument','request','raw')})
            rows.append(row)
        return rows
    def sessions(self):
        groups={};errors=[]
        for path in sorted(self.reader.archive_root.glob('*/*.meta.json')):
            try:rows=self.read(path)
            except (ValueError,TypeError,KeyError,OSError,OverflowError) as exc:errors.append({'archive':path.name,'state':'INVALID','reason':str(exc) if isinstance(exc,ValueError) and str(exc).isupper() else 'ARCHIVE_VALIDATION_FAILED'});continue
            for row in rows:
                key=digest({'instrument':row['instrument'],'request':row['request'],'date':row['timestamp'][:10],'manifest':row['manifest_id']})[:24]
                g=groups.setdefault(key,{'id':key,'date':row['timestamp'][:10],'instrument':row['instrument'],'request':row['request'],'rows':{},'flags':set()})
                old=g['rows'].get(row['timestamp'])
                if old and old['event_id']!=row['event_id']:g['flags'].add('CONFLICTING_DUPLICATE')
                if old is None or str(row['retrieved_at'])>str(old['retrieved_at']):g['rows'][row['timestamp']]=row
        out=[]
        for g in groups.values():
            g['rows']=sorted(g['rows'].values(),key=lambda x:x['timestamp']);g['flags']=sorted(g['flags'])
            for row in g['rows']:
                row['quality_flags']+=g['flags'];row['verified']=row['verified'] and not g['flags']
            out.append(g)
        return sorted(out,key=lambda x:(x['date'],x['id'])),errors


def analyze(rows, rules=None, now=None, mode='HISTORICAL_REPLAY', position=None):
    """Prefix-only deterministic features. No caller-supplied signals are accepted."""
    r=rules or load_rules();now=now or datetime.now(IST)
    if now.tzinfo is None:raise ValueError('Aware reference time required')
    prefix=[x for x in rows if parse_time(x['completed_at'])<=now]
    if not prefix:return {'signal':'BLOCKED','reasons':['NO_COMPLETED_DATA'],'quality_flags':['NO_COMPLETED_DATA'],'score':0,'factors':{}}
    last=prefix[-1];flags=list(last['quality_flags']);ts=parse_time(last['timestamp']);state=mode
    if last.get('origin')=='USER_SUPPLIED_SAMPLE_NOT_A_NETWORK_DOWNLOAD':state='SAMPLE';flags.append('SAMPLE_NOT_EXECUTABLE')
    if not last.get('verified'):state='UNVERIFIED';flags.append('UNVERIFIED_METADATA')
    if last.get('instrument',{}).get('options_expiry','9999-12-31')<ts.date().isoformat():flags.append('EXPIRED_OPTION_METADATA')
    if ts.weekday()>4 or not r['session_start']<=ts.strftime('%H:%M')<=r['session_end']:flags.append('OFF_SESSION')
    if mode=='LIVE_FRESH':
        if (now-parse_time(last['completed_at'])).total_seconds()>r['fresh_seconds'] or now.date()!=ts.date():state='STALE';flags.append('STALE_SOURCE')
        if now.weekday()>4 or not r['session_start']<=now.strftime('%H:%M')<=r['session_end']:flags.append('OFF_SESSION')
    prefix=[x for x in prefix if x['timestamp'][:10]==last['timestamp'][:10]]
    recent=prefix[-(r['window']+1):]
    if any(not x.get('verified') or x.get('quality_flags') for x in recent):flags.append('UNVERIFIED_WINDOW')
    if len(prefix)<r['min_history']:flags.append('WARMING_UP')
    if any((parse_time(b['timestamp'])-parse_time(a['timestamp'])).total_seconds()>r['gap_max_seconds'] for a,b in zip(recent,recent[1:])):flags.append('GAPPED_WINDOW')
    required=('future_ltp','pcr_total','put_oi_day','call_oi_day','future_oi_day')
    if any(not all(number(x.get(k)) is not None for k in required) for x in recent):flags.append('MISSING_REQUIRED_FIELDS')
    factors={};score=0;opposing=[];reasons=[]
    if 'MISSING_REQUIRED_FIELDS' not in flags and len(recent)>=2:
        first=recent[0];prev=recent[-2];p=last['future_ltp']-first['future_ltp'];oi=last['future_oi_day']-first['future_oi_day']
        ret=p/first['future_ltp'] if first['future_ltp']>0 else 0
        balances=[x['put_oi_day']-x['call_oi_day'] for x in recent];increments=[b-a for a,b in zip(balances,balances[1:])]
        direction=1 if ret>r['price_return_min'] else -1 if ret< -r['price_return_min'] else 0
        flow=1 if balances[-1]>balances[0] else -1 if balances[-1]<balances[0] else 0
        pcr=last['pcr_total']-first['pcr_total'];pcr_vote=1 if pcr>0 and last['pcr_total']>=r['pcr_pivot'] else -1 if pcr<0 and last['pcr_total']<r['pcr_pivot'] else 0
        weights=r['score_weights'];score=weights['price']*direction+weights['flow']*flow+weights['pcr']*pcr_vote
        returns=[b['future_ltp']/a['future_ltp']-1 for a,b in zip(recent,recent[1:]) if a['future_ltp']>0]
        structure=('LONG_BUILDUP' if p>0 and oi>0 else 'SHORT_COVERING' if p>0 and oi<0 else 'SHORT_BUILDUP' if p<0 and oi>0 else 'LONG_UNWINDING' if p<0 and oi<0 else 'FLAT')
        factors={k:last.get(k) for k in FIELDS}
        factors.update(price_return=ret,price_change=p,future_oi_calculated_increment=oi,interval_future_oi=last['future_oi_day']-prev['future_oi_day'],
            interval_future_price=last['future_ltp']-prev['future_ltp'],future_structure=('LONG_BUILDUP' if last['future_ltp']>prev['future_ltp'] and last['future_oi_day']>prev['future_oi_day'] else 'SHORT_COVERING' if last['future_ltp']>prev['future_ltp'] and last['future_oi_day']<prev['future_oi_day'] else 'SHORT_BUILDUP' if last['future_ltp']<prev['future_ltp'] and last['future_oi_day']>prev['future_oi_day'] else 'LONG_UNWINDING' if last['future_ltp']<prev['future_ltp'] and last['future_oi_day']<prev['future_oi_day'] else 'FLAT'),window_future_structure=structure,pe_ce_balance=balances[-1],
            relative_balance=balances[-1]/max(abs(last['put_oi_day'])+abs(last['call_oi_day']),1),balance_change=balances[-1]-balances[0],balance_acceleration=increments[-1]-increments[-2] if len(increments)>1 else None,
            balance_persistence=sum(1 for x in increments if x*flow>0)/max(1,len(increments)),pcr_change=pcr,return_volatility=pstdev(returns) if len(returns)>1 else None,
            put_calculated_increment=last['put_oi_day']-prev['put_oi_day'],call_calculated_increment=last['call_oi_day']-prev['call_oi_day'])
        if flow and direction and flow!=direction:opposing.append('PRICE_OI_DIVERGENCE')
        if pcr_vote and direction and pcr_vote!=direction:opposing.append('PCR_OPPOSES_PRICE')
        for k in ('put_volume','call_volume'):
            factors[k]=last.get(k)
        factors['put_call_volume_ratio']=(last['put_volume']/last['call_volume']) if last.get('put_volume') is not None and last.get('call_volume',0) else None
        factors['volume_semantics']='Diagnostic only: provider aggregation must be verified; ratio does not identify initiator.'
        factors['liquidity']='Option liquidity unknown until bid/ask quotes are verified'
        if any(last.get(k)==0 and prev.get(k) not in (0,None) for k in ('put_oi_day','call_oi_day','future_oi_day')):flags.append('POSSIBLE_INTRADAY_RESET')
        reasons=['Price return, OI-balance change and PCR change are heuristic context; OI does not identify writer intent.']
    signal='BUY_CE' if score>=r['entry_score'] else 'BUY_PE' if score<=-r['entry_score'] else 'HOLD'
    # Entry requires consistent causal price and balance movement in the last N increments.
    if signal.startswith('BUY'):
        n=r['persistence'];tail=recent[-(n+1):];sgn=1 if signal=='BUY_CE' else -1
        if len(tail)<n+1 or any((b['future_ltp']-a['future_ltp'])*sgn<=0 or ((b['put_oi_day']-b['call_oi_day'])-(a['put_oi_day']-a['call_oi_day']))*sgn<=0 for a,b in zip(tail,tail[1:])):signal='HOLD';reasons.append('PERSISTENCE_NOT_MET')
    if position and (score*(1 if position['side']=='CE' else -1)<r['exit_score']):signal='EXIT'
    if flags:signal='BLOCKED'
    return {'signal':signal,'state':state,'event_id':last['event_id'],'source_time':last['timestamp'],'completed_at':last['completed_at'],
        'instrument':last['instrument'],'score':score,'confidence_kind':'HEURISTIC_NOT_CALIBRATED','factors':factors,
        'quality_flags':sorted(set(flags)),'reasons':reasons,'contradictions':opposing,
        'paper_eligibility':'BLOCKED' if flags else 'REQUIRES_VERIFIED_OPTION_QUOTES','quote_block':'MISSING_OPTION_QUOTES',
        'config_version':r['version'],'input_id':digest({'rows':[x['event_id'] for x in prefix],'rules':r,'mode':mode,'position':position,'signal':signal,'state':state,'flags':flags})}


def chronological_comparison(rows,rules=None):
    """Pre-registered variants. No fitting/selection. Forward returns are OUTCOME ONLY.
    Prior partition history may warm later partitions; future observations never form features.
    No option P&L is imputed from futures point returns.
    """
    rules=rules or load_rules();out=[];n=len(rows)
    for variant in rules['lab_variants']:
        r={**rules,'entry_score':variant['entry_score']};results=[]
        for index,row in enumerate(rows[:-1]):
            result=analyze(rows[:index+1],r,parse_time(row['completed_at']))
            part='TRAIN' if index<n//2 else 'VALIDATION' if index<3*n//4 else 'TEST'
            future=rows[index+1]
            ret=None
            if result['signal'] in ('BUY_CE','BUY_PE') and row.get('future_ltp') and future.get('future_ltp') and future['timestamp'][:10]==row['timestamp'][:10]:
                ret=(future['future_ltp']-row['future_ltp'])*(1 if result['signal']=='BUY_CE' else -1)
            results.append((part,result['signal'],ret))
        for part in ('TRAIN','VALIDATION','TEST'):
            sample=[x for x in results if x[0]==part];returns=[x[2] for x in sample if x[2] is not None]
            out.append({'variant':variant['name'],'partition':part,'observations':len(sample),'directional_signals':len(returns),
                'next_observation_directional_points_mean':sum(returns)/len(returns) if returns else None,
                'option_trade_count':None,'option_expectancy':None,'profit_factor':None,'drawdown':None,'costs':None,'turnover':None,
                'status':'DIRECTIONAL_ONLY; MISSING_OPTION_QUOTE_BACKTEST',
                'warning':'Repeated state signals are not independent trades. No fitted winner. Two pre-registered variants; selection after viewing results risks multiple comparisons.'})
    return out
