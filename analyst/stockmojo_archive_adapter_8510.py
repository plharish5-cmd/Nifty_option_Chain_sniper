"""Read-only validation of archived compact arrays. Never imports trading or auth code."""
from __future__ import annotations
import argparse
import hashlib
import json
import math
from datetime import datetime,timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

IST=ZoneInfo('Asia/Kolkata')
RULES_PATH=Path(__file__).with_name('archive_adapter_rules_8510.json')
RAW_KEYS=('co','po','cv','pv','f','fo','cc','pc')
MAPPED={'co':'call_oi','po':'put_oi','cv':'call_volume','pv':'put_volume','f':'future_ltp','fo':'future_oi'}
MEANINGS={'co':'OPEN_INTEREST','po':'OPEN_INTEREST','cv':'VOLUME','pv':'VOLUME','f':'FUTURES_PRICE','fo':'FUTURES_OPEN_INTEREST'}


def parse_time(value,timezone=None):
    d=datetime.fromisoformat(str(value).replace('Z','+00:00'))
    if d.tzinfo is None:
        if timezone is None:return d
        d=d.replace(tzinfo=ZoneInfo(timezone))
    return d.astimezone(IST)


def finite(value):
    return type(value) in (int,float) and math.isfinite(value)


class ArchiveAdapter:
    def __init__(self,root,rules=None,clock=None):
        self.root=Path(root).resolve();self.rules=rules or json.loads(RULES_PATH.read_text())
        self.archive_root=self.root/self.rules['archive_relative']
        self.archive_root.resolve().relative_to(self.root)
        self.clock=clock or (lambda:datetime.now(IST))
    def _read(self,path,limit):
        path=Path(path)
        path.resolve().relative_to(self.archive_root.resolve())
        if path.is_symlink() or any(p.is_symlink() for p in path.parents if p!=self.archive_root.parent):raise ValueError('SYMLINK_ARCHIVE_REJECTED')
        if path.stat().st_size>limit:raise ValueError('FILE_TOO_LARGE')
        return path.read_bytes()
    def _verification(self,manifest,meta):
        errors=[]
        if not isinstance(manifest,dict):return ['NO_REVIEWED_PROVIDER_MANIFEST']
        if manifest.get('review_status')!='REVIEWED' or not manifest.get('reviewed_by'):errors.append('PROVIDER_REVIEW_REQUIRED')
        if manifest.get('request')!=meta.get('request'):errors.append('REQUEST_ID_MAPPING_NOT_VERIFIED')
        for section in ('instrument','fields','evidence'):
            if not isinstance(manifest.get(section),dict):return ['MALFORMED_VERIFICATION_'+section.upper()]
        instrument=manifest.get('instrument',{})
        for key in ('underlying','exchange','futures_contract'):
            if not isinstance(instrument.get(key),str) or not instrument[key].strip():errors.append('UNVERIFIED_'+key.upper())
        try:datetime.strptime(instrument['options_expiry'],'%Y-%m-%d')
        except (KeyError,ValueError,TypeError):errors.append('UNVERIFIED_OPTIONS_EXPIRY')
        try:ZoneInfo(manifest['timezone'])
        except (KeyError,ValueError,TypeError):errors.append('UNVERIFIED_TIMEZONE')
        if manifest.get('timestamp_semantics') not in ('MINUTE_START','MINUTE_END'):errors.append('UNVERIFIED_MINUTE_COMPLETION_SEMANTICS')
        if manifest.get('oi_scope') not in ('REQUESTED_RANGE','FULL_CHAIN'):errors.append('UNVERIFIED_OI_SCOPE')
        evidence=manifest.get('evidence',{})
        for key in ('selection','field_dictionary','timestamps'):
            if not isinstance(evidence.get(key),str) or not evidence[key].strip():errors.append('MISSING_'+key.upper()+'_EVIDENCE')
        fields=manifest.get('fields',{})
        for key in MAPPED:
            field=fields.get(key,{})
            if not isinstance(field,dict):errors.append('MALFORMED_FIELD_'+key);continue
            if field.get('meaning')!=MEANINGS[key]:errors.append('UNVERIFIED_MEANING_'+key)
            unit=field.get('unit')
            if key=='f':
                if unit!='POINTS':errors.append('UNVERIFIED_UNIT_f')
            elif unit=='LOTS':
                if type(field.get('contracts_per_lot')) is not int or field['contracts_per_lot']<=0:errors.append('UNVERIFIED_LOT_SIZE_'+key)
            elif unit!='CONTRACTS':errors.append('UNVERIFIED_UNIT_'+key)
            if key in ('cv','pv') and field.get('aggregation') not in ('PER_MINUTE','CUMULATIVE'):errors.append('UNVERIFIED_VOLUME_AGGREGATION_'+key)
        return list(dict.fromkeys(errors))
    def inspect(self,metadata_path,manifest=None,mode=None,asof=None):
        now=asof or self.clock()
        if now.tzinfo is None:raise ValueError('Reference time must be timezone aware')
        now=now.astimezone(IST)
        result={'schema_version':1,'status':'INVALID','mode':'UNKNOWN','observation_state':'INVALID','states':[],
            'raw_rows':[],'normalized_rows':[],'verification_errors':[],
            'strategy_signals_allowed':False,'paper_trade_writes_allowed':False,
            'paper_connection':'DISABLED','normalization_ready':False,
            'verification_basis':'LOCAL_REVIEW_ATTESTATION_REQUIRED; not provider-signed evidence'}
        try:
            path=Path(metadata_path)
            if not path.name.endswith('.meta.json'):raise ValueError('METADATA_SUFFIX_REQUIRED')
            raw_path=path.with_name(path.name[:-10]+'.json')
            meta=json.loads(self._read(path,self.rules['max_metadata_bytes']))
            if not isinstance(meta,dict):raise ValueError('METADATA_OBJECT_REQUIRED')
            request=meta.get('request')
            if not isinstance(request,dict) or set(request)!={'symbol','expiry','range'} or any(type(v) is not int for v in request.values()):raise ValueError('INVALID_PUBLIC_REQUEST_METADATA')
            content=self._read(raw_path,self.rules['max_raw_bytes'])
            if hashlib.sha256(content).hexdigest()!=meta.get('raw_sha256'):raise ValueError('RAW_HASH_MISMATCH')
            payload=json.loads(content)
            if not isinstance(payload,dict) or not isinstance(payload.get('t'),list) or not payload['t']:raise ValueError('MISSING_TIME_ARRAY')
            count=len(payload['t'])
            if any(not isinstance(payload.get(k),list) or len(payload[k])!=count for k in RAW_KEYS):raise ValueError('ARRAY_LENGTH_MISMATCH')
            rows={};duplicates=0
            for i,stamp in enumerate(payload['t']):
                parsed=parse_time(stamp)
                row={'t':stamp,**{k:payload[k][i] for k in RAW_KEYS}}
                if any(row[k] is not None and not finite(row[k]) for k in RAW_KEYS):raise ValueError('NONFINITE_OR_NONNUMERIC_RAW_FIELD')
                key=parsed.isoformat()
                if key in rows:
                    if any(rows[key][k]!=row[k] for k in RAW_KEYS):raise ValueError('CONFLICTING_DUPLICATE_OBSERVATION')
                    duplicates+=1
                rows[key]=row
            ordered=sorted(rows.values(),key=lambda r:parse_time(r['t']))
            retrieved=parse_time(meta['downloaded_at_ist'])
            if retrieved.tzinfo is None:raise ValueError('UNVERIFIED_RETRIEVAL_TIMEZONE')
            origin=meta.get('origin')
            actual_mode='SAMPLE' if origin=='USER_SUPPLIED_SAMPLE_NOT_A_NETWORK_DOWNLOAD' else 'NETWORK_ARCHIVE' if origin=='HTTP_200_RESPONSE' else 'UNKNOWN'
            if mode is not None:
                if mode!='REPLAY':raise ValueError('ONLY_REPLAY_MODE_OVERRIDE_ALLOWED')
                actual_mode='REPLAY' if actual_mode!='SAMPLE' else 'SAMPLE'
            errors=self._verification(manifest,meta)
            result.update(mode=actual_mode,evaluation_mode=mode or 'ARCHIVE_INSPECTION',evaluation_asof_ist=now.isoformat(),raw_rows=ordered,raw_keys=['t',*RAW_KEYS],opaque_provider_metadata_present='c' in payload,
                request=meta.get('request'),retrieved_at_ist=retrieved.isoformat(),raw_observation_count=count,
                unique_observation_count=len(ordered),duplicate_observations_collapsed=duplicates,
                verification_errors=errors,raw_data_asof=ordered[-1]['t'])
            states=[]
            if actual_mode=='SAMPLE':states.append('SAMPLE')
            if actual_mode=='REPLAY':states.append('REPLAY')
            if actual_mode=='UNKNOWN':states.append('UNKNOWN_ORIGIN')
            if errors:states.append('UNVERIFIED')
            if not errors:
                fields=manifest['fields'];converted=[];future_rows=0;previous={}
                for row in ordered:
                    ts=parse_time(row['t'],manifest['timezone'])
                    completed=ts+timedelta(minutes=1) if manifest['timestamp_semantics']=='MINUTE_START' else ts
                    if completed>now:
                        future_rows+=1;continue
                    n={'timestamp_ist':ts.isoformat(),'completed_at_ist':completed.isoformat(),
                        'underlying':manifest['instrument']['underlying'],'oi_scope':manifest['oi_scope']}
                    for key,target in MAPPED.items():
                        value=row[key]
                        if not finite(value) or value<0:raise ValueError('INVALID_VERIFIED_REQUIRED_FIELD')
                        field=fields[key];mult=field['contracts_per_lot'] if field.get('unit')=='LOTS' else 1
                        scaled=value*mult
                        if not finite(scaled):raise ValueError('NORMALIZATION_OVERFLOW')
                        if key in ('cv','pv') and field['aggregation']=='CUMULATIVE':
                            old=previous.get((ts.date(),key));previous[(ts.date(),key)]=scaled
                            scaled=scaled-old if old is not None else None
                            if scaled is not None and scaled<0:raise ValueError('CUMULATIVE_VOLUME_RESET_WITHIN_SESSION')
                        n[target]=scaled
                    n['pcr']=n['put_oi']/n['call_oi'] if n['call_oi']>0 else None
                    converted.append(n)
                if converted:
                    # Never join Prev Day into intraday windows.
                    selected_day=parse_time(converted[-1]['timestamp_ist']).date()
                    converted=[r for r in converted if parse_time(r['timestamp_ist']).date()==selected_day]
                    evaluated=parse_time(converted[-1]['completed_at_ist']);age=(now-evaluated).total_seconds()
                    if age>self.rules['max_age_seconds']:states.append('STALE')
                    if not (now.weekday() in self.rules['weekdays'] and self.rules['session_start']<=now.strftime('%H:%M')<=self.rules['session_end'] and selected_day==now.date()):states.append('OFF_SESSION')
                    ready=all(finite(converted[-1].get(k)) for k in (*MAPPED.values(),'pcr'))
                    if not ready:states.append('MISSING_NORMALIZED_FIELDS')
                    result.update(normalized_rows=converted,evaluated_asof_ist=evaluated.isoformat(),source_age_seconds=age,normalization_ready=ready)
                else:states.append('NO_COMPLETED_OBSERVATIONS')
                if future_rows:states.append('PENDING_OR_FUTURE_ROWS')
                result['pending_or_future_rows']=future_rows
                if manifest['instrument']['underlying']!=self.rules['target_underlying']:states.append('INSTRUMENT_MISMATCH')
            if actual_mode!='REPLAY' and (retrieved-now).total_seconds()>self.rules['max_clock_skew_seconds']:states.append('FUTURE_RETRIEVAL_TIME')
            result['states']=list(dict.fromkeys(states))
            result['status']='UNVERIFIED' if errors else states[0] if states else 'VERIFIED_READ_ONLY'
            result['observation_state']='UNVERIFIED' if errors else 'SAMPLE' if actual_mode=='SAMPLE' else 'REPLAY' if actual_mode=='REPLAY' else 'STALE' if 'STALE' in states else 'LIVE_READ_ONLY' if actual_mode=='NETWORK_ARCHIVE' and not states and result['normalization_ready'] else result['status']
            result['network_origin_basis']='Local archiver metadata; not independently authenticated by this adapter'
            return result
        except (OSError,ValueError,TypeError,KeyError,OverflowError) as exc:
            result.update(status='INVALID',states=['INVALID'],normalization_ready=False,normalized_rows=[],error_type=type(exc).__name__,error_code=str(exc) if isinstance(exc,ValueError) and str(exc).isupper() else 'ARCHIVE_VALIDATION_FAILED')
            return result
    def latest(self,manifest=None,mode=None,asof=None):
        if not self.archive_root.exists():return {'status':'NO_ARCHIVES','observation_state':'NO_ARCHIVES','strategy_signals_allowed':False,'paper_trade_writes_allowed':False,'paper_connection':'DISABLED'}
        paths=sorted(p for p in self.archive_root.rglob('*.meta.json') if not p.is_symlink())
        if not paths:return {'status':'NO_ARCHIVES','strategy_signals_allowed':False,'paper_trade_writes_allowed':False,'paper_connection':'DISABLED'}
        return self.inspect(paths[-1],manifest,mode,asof)


def main():
    p=argparse.ArgumentParser();p.add_argument('--root',default=str(Path(__file__).resolve().parents[1]));p.add_argument('--metadata');p.add_argument('--verification');p.add_argument('--replay-asof');args=p.parse_args()
    adapter=ArchiveAdapter(args.root)
    if args.verification and (Path(args.verification).suffix.lower()!='.json' or Path(args.verification).is_symlink()):p.error('Verification must be a regular public JSON manifest, never an .env file or symlink')
    manifest=json.loads(Path(args.verification).read_text()) if args.verification else None
    asof=parse_time(args.replay_asof) if args.replay_asof else None
    result=adapter.inspect(args.metadata,manifest,'REPLAY' if asof else None,asof) if args.metadata else adapter.latest(manifest,'REPLAY' if asof else None,asof)
    summary={k:v for k,v in result.items() if k not in ('raw_rows','normalized_rows')};summary['normalized_observation_count']=len(result.get('normalized_rows',[]))
    print(json.dumps(summary,indent=2,allow_nan=False))

if __name__=='__main__':main()
