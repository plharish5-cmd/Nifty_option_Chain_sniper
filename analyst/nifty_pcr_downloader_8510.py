"""Owner-captured /live/oi/pcr recorder. Reuses the existing downloader lock,
credentials, bounded HTTP client, backoff and loop. No broker or second service.
"""
import argparse
import hashlib
import json
import signal
from pathlib import Path
from datetime import datetime
from analyst.stockmojo_downloader_8510 import Downloader, atomic
from analyst.nifty_four_collector_8510 import collect_exports

RULES=Path(__file__).with_name('nifty_pcr_download_rules_8510.json')
ENDPOINT='https://data.stockmojo.in/live/oi/pcr'

def packet_for(payload,cfg):
    if not isinstance(payload,list) or len(payload)<2 or not isinstance(payload[0],list) or len(payload[0])!=4:
        raise ValueError('EXPECTED_DECODED_PCR_ARRAY')
    header=payload[0]
    if header[0]!=cfg['payload']['expiries'] or not isinstance(header[3],dict):
        raise ValueError('EXPIRY_OR_BASELINE_MISMATCH')
    previous=None
    for row in payload[1:]:
        if not isinstance(row,list) or len(row)!=4 or not isinstance(row[0],str):raise ValueError('INVALID_PCR_ROW')
        datetime.strptime(row[0],'%Y-%m-%d %H:%M:%S')
        if previous and row[0]<=previous:raise ValueError('NON_MONOTONIC_PCR_ROWS')
        if any(type(v) is not int or v<0 for v in row[1:3]):raise ValueError('EXACT_INTEGER_OI_REQUIRED')
        if type(row[3]) not in (int,float) or not 0<row[3]<1e9:raise ValueError('INVALID_FUTURES_PRICE')
        previous=row[0]
    baselines={}
    for day,pair in header[3].items():
        datetime.strptime(day,'%Y-%m-%d')
        if not isinstance(pair,list) or len(pair)!=2 or any(type(v) is not int or v<0 for v in pair):raise ValueError('INVALID_BASELINE')
        baselines[day]={'total_call_oi':pair[0],'total_put_oi':pair[1], 'type':'PROVIDER_REFERENCE',
          'provenance':'DATE_KEYED_HEADER_OF_OWNER_CAPTURED_PCR_RESPONSE','available_at_ist':None}
    return {'metadata':{'instrument':'NIFTY','instrument_status':'VERIFIED',
      'selection_provenance':cfg['selection_provenance'],'mapping_status':'VERIFIED',
      'mapping_provenance':cfg['mapping_provenance'],'timezone':'Asia/Kolkata',
      'option_expiry':cfg['option_expiry'],'futures_expiry':None,'futures_identity_status':'UNVERIFIED',
      'source_interval':60,'timestamp_semantics':'END','aggregation_scope':'SELECTED_EXPIRY',
      'strike_selection':'ALL','unit_status':'UNVERIFIED','baselines':baselines,
      'origin':'HTTP_STRUCTURED_RESPONSE'},'payload':payload}

class PCRDownloader(Downloader):
    def __init__(self,root,session=None,clock=None,rng=None,cfg=None):
        super().__init__(root,session=session,clock=clock,rng=rng)
        self.pcr_cfg=cfg or json.loads(RULES.read_text())
        if self.pcr_cfg['endpoint']!=ENDPOINT or self.pcr_cfg['payload']['symbol']!=1 or self.pcr_cfg['payload']['dt'] is not None:
            raise ValueError('UNSUPPORTED_PCR_REQUEST')
        self.rules={**self.rules,**self.pcr_cfg}
    def active(self,now):
        special=self.rules['special_sessions'].get(now.date().isoformat())
        if special:return special[0]<=now.strftime('%H:%M')<=special[1]
        return super().active(now)
    def archive(self,payload,received=None,origin='HTTP_200_RESPONSE',etag=None):
        now=received or self.clock();packet=packet_for(payload,self.pcr_cfg)
        raw=json.dumps(payload,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode()
        if len(raw)>self.rules['max_response_bytes']:raise ValueError('RESPONSE_TOO_LARGE')
        sha=hashlib.sha256(raw).hexdigest()
        folder=self.root/'data/stockmojo_four_oi'/now.date().isoformat()
        name=now.strftime('%Y%m%d_%H%M%S_%f')+'_'+sha[:16]
        target=folder/(name+'.pcr.json');atomic(target,raw)
        atomic(folder/(name+'.pcr.meta.json'),json.dumps({'received_at_ist':now.isoformat(),
          'endpoint':ENDPOINT,'request':self.rules['payload'],'raw_sha256':sha,
          'source_last_ist':payload[-1][0],'row_count':len(payload)-1,'etag':etag,
          'unit_status':'UNVERIFIED','baseline_historical_availability':'UNVERIFIED'},indent=2).encode())
        exports=self.root/'analyst/nifty_oi_exports'
        atomic(exports/('pcr_'+sha+'.json'),json.dumps(packet,allow_nan=False).encode())
        imported=collect_exports(self.root,now)
        if imported['errors']:raise ValueError('PCR_IMPORT_FAILED')
        latest=payload[-1];base=payload[0][3][latest[0][:10]]
        self.last_success={'last_success_at_ist':now.isoformat(),'last_success_file':str(target),'last_success_source_time_ist':latest[0]}
        return self.status('DOWNLOADED',endpoint=ENDPOINT,latest_file=str(target),row_count=len(payload)-1,
          source_last_ist=latest[0],recorded_events=imported['recorded_events'],
          exact_values={'total_call_oi':latest[1],'total_put_oi':latest[2],
            'call_oi_change_today':latest[1]-base[0],'put_oi_change_today':latest[2]-base[1],
            'pcr_total':latest[2]/latest[1] if latest[1] else None},
          production_signal_feed_ready=False,
          remaining_verification=['ECONOMIC_UNITS','BASELINE_HISTORICAL_AVAILABILITY','FUTURES_CONTRACT_IDENTITY'])

def main():
    p=argparse.ArgumentParser();p.add_argument('--root',default=str(Path(__file__).resolve().parents[1]));p.add_argument('--once',action='store_true');a=p.parse_args()
    d=PCRDownloader(a.root)
    for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,lambda *_:d.stop.set())
    try:code=d.run(once=a.once)
    except (ValueError,OSError):
        print(json.dumps({"status":"START_FAILED","reason":"COLLECTOR_LOCK_OR_LOCAL_IO"}));code=2
    raise SystemExit(code)
if __name__=='__main__':main()
