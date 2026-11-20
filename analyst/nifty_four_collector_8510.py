"""Structured-export extension of the EXISTING downloader loop; never another poller.
No encoded response decoder, new provider endpoint or credential acquisition.
"""
import argparse
import json
import hashlib
import os
import tempfile
from datetime import datetime
from pathlib import Path
from analyst.nifty_four_oi_8510 import normalize,rules,IST,digest

META_KEYS={'timezone','instrument','instrument_status','selection_provenance','mapping_status','mapping_provenance','option_expiry','futures_expiry',
 'futures_identity_status','source_interval','timestamp_semantics','aggregation_scope','strike_selection','baselines','unit_status','contracts_per_lot','column_mapping','origin'}


def atomic(path,data):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True);fd,tmp=tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as f:f.write(data);f.flush();os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)


def contains_credentials(value):
    if isinstance(value,dict):return any(str(k).lower() in ('j','authorization','cookie','jwt','session_token','access_token') or contains_credentials(v) for k,v in value.items())
    if isinstance(value,list):return any(contains_credentials(x) for x in value)
    return False


def collect_exports(root,now=None,cfg=None):
    r=cfg or rules();root=Path(root).resolve();now=now or datetime.now(IST);folder=root/r['export_folder'];out=root/r['archive_folder']
    folder.resolve().relative_to(root);out.resolve().relative_to(root)
    if folder.is_symlink() or out.is_symlink() or (out/'observations.jsonl').is_symlink():raise ValueError('SYMLINK_COLLECTION_PATH_REJECTED')
    out.mkdir(parents=True,exist_ok=True);status={'acquisition':'BLOCKED_AUTHORIZED_STRUCTURED_EXPORT_REQUIRED','instrument':'NIFTY','received_at_ist':now.isoformat(),'new_events':0,'files':0,'errors':[],'network_requests':0}
    existing={}
    ledger=out/'observations.jsonl'
    if ledger.exists():
        for line in ledger.read_text().splitlines():
            row=json.loads(line);existing[(row['identity_id'],row['source_time_ist'])]=row
    seenpath=out/'imports.json';seen=json.loads(seenpath.read_text()) if seenpath.exists() else []
    for path in sorted(folder.glob('*.json')):
        try:
            if path.is_symlink() or path.stat().st_size>r['maximum_export_bytes']:raise ValueError('INVALID_EXPORT_FILE')
            raw=path.read_bytes();h=hashlib.sha256(raw).hexdigest()
            if h in seen:continue
            packet=json.loads(raw)
            if contains_credentials(packet):raise ValueError('CREDENTIAL_BEARING_EXPORT_REJECTED')
            if not isinstance(packet,dict) or set(packet)-{'metadata','payload'} or not isinstance(packet.get('metadata'),dict):raise ValueError('STRUCTURED_EXPORT_PACKET_REQUIRED')
            if set(packet['metadata'])-META_KEYS:raise ValueError('UNSUPPORTED_METADATA_KEYS')
            # Do not archive credential-bearing envelopes as if they were chart data.
            payload=packet.get('payload')
            if isinstance(payload,dict) and set(payload)-{'observations','data'}:raise ValueError('UNSUPPORTED_PAYLOAD_KEYS')
            if isinstance(payload,dict) and 'data' in payload:raise ValueError('AUTHORIZED_DECODING_UNAVAILABLE')
            meta={**packet['metadata'],'retrieved_at_ist':now.isoformat()};result=normalize(payload,meta,r)
            if not result['rows']:raise ValueError('NO_STRUCTURED_OBSERVATIONS')
            candidate=dict(existing)
            completed=[row for row in result['rows'] if datetime.fromisoformat(row['completed_at_ist'])<=now]
            status['pending_source_rows']=status.get('pending_source_rows',0)+len(result['rows'])-len(completed)
            for row in completed:
                key=(row['identity_id'],row['source_time_ist']);old=candidate.get(key)
                if old and old['event_id']!=row['event_id']:raise ValueError('CONFLICTING_RECORDED_OBSERVATION')
                if old is None:candidate[key]=row
            # Retain exact original bytes and a public sidecar before committing JSONL.
            day=out/now.date().isoformat();target=day/(h+'.raw.json');atomic(target,raw)
            public={'raw_sha256':h,'retrieved_at_ist':now.isoformat(),'instrument':'NIFTY',**{k:result[k] for k in ('metadata_elements_excluded','duplicates_collapsed','chronology_reordered')},'origin':meta.get('origin'),'row_count':len(result['rows'])}
            atomic(day/(h+'.meta.json'),json.dumps(public,indent=2).encode())
            status['new_events']+=len(candidate)-len(existing);existing=candidate
            # Atomic replacement of an append-only logical event log, never edit old events.
            atomic(ledger,(''.join(json.dumps(row,allow_nan=False)+'\n' for row in existing.values())).encode())
            if len(completed)==len(result['rows']):seen.append(h)
            atomic(seenpath,json.dumps(seen).encode());status['files']+=1
        except (OSError,ValueError,TypeError,KeyError,OverflowError) as exc:
            status['errors'].append(str(exc) if isinstance(exc,ValueError) and str(exc).isupper() else 'INVALID_STRUCTURED_EXPORT')
    status['recorded_events']=len(existing)
    if existing:
        latest=max(x['source_time_ist'] for x in existing.values());status.update(source_last_ist=latest,source_age_seconds=(now-datetime.fromisoformat(latest)).total_seconds())
    if existing:status['acquisition']='STRUCTURED_EXPORT_COLLECTION_ONLY; LIVE_REQUEST_MAPPING_UNVERIFIED'
    atomic(out/'status.json',json.dumps(status,indent=2).encode());return status


def main():
    p=argparse.ArgumentParser();p.add_argument('--root',default=str(Path(__file__).resolve().parents[1]));args=p.parse_args()
    # Same flock as network downloader; no concurrent importer/collector writers.
    from analyst.stockmojo_downloader_8510 import Downloader
    d=Downloader(args.root)
    try:d.acquire();print(json.dumps(collect_exports(args.root),indent=2))
    finally:d.close()
if __name__=='__main__':main()
