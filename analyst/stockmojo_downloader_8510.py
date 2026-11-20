"""Standalone authenticated raw-response archiver. No broker or dashboard imports."""
from __future__ import annotations
import argparse
import csv
import fcntl
import getpass
import hashlib
import io
import json
import math
import uuid
import os
import random
import shlex
import signal
import sys
import tempfile
import threading
import termios
import time
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import requests

IST=ZoneInfo('Asia/Kolkata')
RULES=Path(__file__).with_name('stockmojo_download_rules_8510.json')
AUTH_KEYS=('STOCKMOJO_JWT_J','STOCKMOJO_TOKEN_T')
ARRAYS=('co','po','cv','pv','f','fo','cc','pc')


def atomic(path,content):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(prefix='.'+path.name+'.',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as h:h.write(content);h.flush();os.fsync(h.fileno())
        os.replace(name,path)
    finally:
        if os.path.exists(name):os.unlink(name)


def credentials(root):
    result={k:os.environ.get(k,'').strip() for k in AUTH_KEYS}
    path=Path(root)/'.env'
    if path.exists():
        for line in path.read_text().splitlines():
            line=line.strip().removeprefix('export ')
            key,sep,value=line.partition('=')
            if sep and key in AUTH_KEYS and not result[key]:
                try:
                    parts=shlex.split(value,comments=True);result[key]=parts[0] if len(parts)==1 else ''
                except ValueError:result[key]=''
    if any(not result[k] or '\n' in result[k] or '\r' in result[k] for k in AUTH_KEYS):
        raise ValueError('AUTH_NOT_CONFIGURED: run --configure privately')
    return result


def configure(root):
    if not sys.stdin.isatty():raise ValueError('Private credential entry requires an interactive terminal')
    values={k:getpass.getpass(k+' (hidden): ').strip() for k in AUTH_KEYS}
    if any(not v or '\n' in v or '\r' in v for v in values.values()):raise ValueError('Empty/invalid credential')
    save_credentials(root,values)


def save_credentials(root,values):
    p=Path(root)/'.env';lines=p.read_text().splitlines(keepends=True) if p.exists() else []
    output=[];done=set()
    for line in lines:
        key=line.strip().removeprefix('export ').partition('=')[0]
        if key in values:
            if key not in done:output.append(key+'='+shlex.quote(values[key])+'\n');done.add(key)
        else:output.append(line)
    text=''.join(output)
    if text and not text.endswith('\n'):text+='\n'
    for key in dict.fromkeys((*AUTH_KEYS,*values)):
        if key not in done:text+=key+'='+shlex.quote(values[key])+'\n'
    atomic(p,text.encode());p.chmod(0o600)
    print('Credentials saved privately; unrelated .env entries preserved. Values are never logged.')


def parse_copied_request(text):
    """Parse ONLY; copied shell commands are never executed."""
    try:parts=shlex.split(text.replace('\\\n',' '))
    except ValueError:raise ValueError('Cannot parse copied request; choose Copy as cURL (bash)') from None
    endpoint=json.loads(RULES.read_text())['endpoint']
    if endpoint not in parts:raise ValueError('Copied request is not the configured StockMojo endpoint')
    selected={}
    for i,part in enumerate(parts[:-1]):
        if part not in ('-H','--header'):continue
        name,sep,value=parts[i+1].partition(':');name=name.strip().lower()
        if sep and name in ('j','t'):
            value=value.strip()
            if name in selected and selected[name]!=value:raise ValueError('Conflicting copied authentication headers')
            selected[name]=value
    if any(not selected.get(k) or any(c in selected[k] for c in ('\r','\n')) for k in ('j','t')):
        raise ValueError('Copied request is missing J or T; use the PE-CE request Copy as cURL (bash)')
    return dict(zip(AUTH_KEYS,(selected['j'],selected['t'])))


def configure_request(root, reconfigure=False):
    if not sys.stdin.isatty():raise ValueError('Private request entry requires an interactive terminal')
    print('Google password is NOT needed. Paste Copy as cURL (bash) from the PE-CE network request.\nInput is hidden. After pasting, press Enter, type END, then Enter.',flush=True)
    fd=sys.stdin.fileno();old=termios.tcgetattr(fd);hidden=list(old);hidden[3]&=~termios.ECHO
    text=[];length=0;limit=json.loads(RULES.read_text())['max_credential_input_bytes']
    try:
        termios.tcsetattr(fd,termios.TCSADRAIN,hidden)
        for line in sys.stdin:
            if line.strip()=='END':break
            length+=len(line.encode())
            if length>limit:raise ValueError('Copied request exceeds allowed size')
            text.append(line)
    finally:termios.tcsetattr(fd,termios.TCSADRAIN,old);print('',flush=True)
    copied=''.join(text)
    values=parse_copied_request(copied)
    if reconfigure:
        parts=shlex.split(copied.replace('\\\n',' '))
        bodies=[parts[i+1] for i,x in enumerate(parts[:-1]) if x in ('--data-raw','--data','--data-binary','-d')]
        if len(bodies)!=1:raise ValueError('Exactly one JSON request body required')
        try:payload=json.loads(bodies[0])
        except ValueError:raise ValueError('Invalid JSON request body') from None
        if not isinstance(payload,dict) or set(payload)!={'symbol','expiry','range'} or any(type(v) is not int or v<=0 for v in payload.values()):
            raise ValueError('Expected positive symbol, expiry and range request IDs')
        # Same instance lock as collection: never reconfigure a running downloader.
        d=Downloader(root)
        try:
            d.acquire()
            path=Path(root)/'analyst/stockmojo_download_rules_8510.json'
            rules=json.loads(path.read_text())
            rules.update(payload=payload,instrument_label='UNVERIFIED_REQUEST_MAPPING',expiry_label='UNVERIFIED_OPTIONS_EXPIRY')
            save_credentials(root,values)
            atomic(path,json.dumps(rules,indent=2).encode())
        finally:d.close()
        print('Public request IDs updated; identity remains UNVERIFIED pending provider review.')
    else:save_credentials(root,values)


def inspect_payload(payload):
    if not isinstance(payload,dict) or not isinstance(payload.get('t'),list) or not payload['t']:
        raise ValueError('UNSUPPORTED_RESPONSE_SCHEMA')
    n=len(payload['t'])
    if any(not isinstance(payload.get(k),list) or len(payload[k])!=n for k in ARRAYS):
        raise ValueError('PARALLEL_ARRAY_LENGTH_MISMATCH')
    for key in ARRAYS:
        for value in payload[key]:
            if value is None:continue
            if isinstance(value,bool):raise ValueError('INVALID_NUMERIC_ARRAY')
            try:valid=math.isfinite(float(value))
            except (ValueError,TypeError,OverflowError):valid=False
            if not valid:raise ValueError('INVALID_NUMERIC_ARRAY')
    times=[]
    for value in payload['t']:
        try:
            d=datetime.fromisoformat(str(value).replace('Z','+00:00'))
            times.append(d.replace(tzinfo=IST) if d.tzinfo is None else d.astimezone(IST))
        except (ValueError,TypeError):raise ValueError('INVALID_SOURCE_TIMESTAMP') from None
    if len(set(times))!=n:raise ValueError('DUPLICATE_SOURCE_TIMESTAMP')
    return times


class Downloader:
    def __init__(self,root,rules=None,session=None,clock=None,rng=None):
        self.root=Path(root).expanduser().resolve()
        self.rules=rules or json.loads(RULES.read_text())
        if self.rules['endpoint']!='https://data.stockmojo.in/live/multi-strike/ce-pe-oi-diff':
            raise ValueError('Unexpected destination; credentials restricted to the configured service')
        self.output=self.root/self.rules['output_relative']
        self.session=session or requests.Session()
        self.session.trust_env=False
        self.clock=clock or (lambda:datetime.now(IST));self.rng=rng or random.SystemRandom()
        self.failures=0;self.last_digest=None;self.last_success={};self.stop=threading.Event();self.lock=None
    def acquire(self):
        self.output.mkdir(parents=True,exist_ok=True)
        self.lock=(self.output/'downloader.lock').open('a+')
        try:fcntl.flock(self.lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close();self.lock=None;raise ValueError('DOWNLOADER_ALREADY_RUNNING') from None
        self.lock.seek(0);self.lock.truncate();self.lock.write(str(os.getpid()));self.lock.flush()
    def close(self):
        if self.lock is not None:fcntl.flock(self.lock,fcntl.LOCK_UN);self.lock.close();self.lock=None
        self.session.close()
    def active(self,now):
        r=self.rules
        return now.weekday() in r['weekdays'] and r['session_start']<=now.strftime('%H:%M')<=r['session_end']
    def interval(self):return self.rng.uniform(self.rules['interval_min_seconds'],self.rules['interval_max_seconds'])
    def backoff(self,retry_after=None):
        base=min(self.rules['backoff_max_seconds'],self.rules['backoff_initial_seconds']*(2**min(self.failures,10)))
        if retry_after:
            try:base=max(base,float(retry_after))
            except (ValueError,TypeError):
                try:base=max(base,(parsedate_to_datetime(retry_after)-self.clock()).total_seconds())
                except (ValueError,TypeError,OverflowError):base=max(base,self.rules['backoff_max_seconds'])
        return base
    def status(self,state,**fields):
        value={'status':state,'updated_at_ist':self.clock().isoformat(),'pid':os.getpid(),
            'request':self.rules['payload'],'instrument_label':self.rules['instrument_label'],
            'expiry_label':self.rules['expiry_label'],'output_folder':str(self.output),
            'trading_integration':False,**self.last_success,**fields}
        atomic(self.output/'status.json',json.dumps(value,indent=2,allow_nan=False).encode())
        print(json.dumps(value,allow_nan=False),flush=True)
        return value
    def archive(self,payload,received=None,origin='HTTP_200_RESPONSE',etag=None):
        received=received or self.clock();times=inspect_payload(payload)
        content=json.dumps(payload,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()
        if len(content)>self.rules['max_response_bytes']:raise ValueError('RESPONSE_TOO_LARGE')
        digest=hashlib.sha256(content).hexdigest();request=self.rules['payload']
        day=self.output/received.strftime('%Y-%m-%d')
        name=received.strftime('%Y%m%d_%H%M%S_%f')+'_symbol'+str(request['symbol'])+'_expiry'+str(request['expiry'])+'_'+uuid.uuid4().hex
        raw=day/(name+'.json');table=day/(name+'.csv');meta=day/(name+'.meta.json')
        latest=max(times);age=(received-latest).total_seconds()
        metadata={'downloaded_at_ist':received.isoformat(),'request':request,'instrument_label':self.rules['instrument_label'],
            'expiry_label':self.rules['expiry_label'],'source_first_ist':min(times).isoformat(),
            'source_last_ist':latest.isoformat(),'source_age_seconds':age,
            'freshness':'RECENT_TIMESTAMP_UNVERIFIED' if 0<=age<=self.rules['freshness_max_seconds'] else 'HISTORICAL_STALE_OR_FUTURE',
            'row_count':len(times),'raw_sha256':digest,'same_as_previous_download':digest==self.last_digest,
            'origin':origin,'etag':etag,'column_mapping':'RAW_API_KEYS_ONLY; units/instrument not verified',
            'telemetry_ingestion_authorized':False}
        # Time order is normalized only in the diagnostic CSV. Raw JSON is unchanged.
        stream=io.StringIO();writer=csv.writer(stream);writer.writerow(['source_time_ist',*ARRAYS])
        for i in sorted(range(len(times)),key=times.__getitem__):writer.writerow([times[i].isoformat(),*(payload[k][i] for k in ARRAYS)])
        atomic(raw,content);atomic(table,stream.getvalue().encode());atomic(meta,json.dumps(metadata,indent=2).encode())
        self.last_digest=digest
        self.last_success={'last_success_at_ist':received.isoformat(),'last_success_file':str(raw),'last_success_source_time_ist':latest.isoformat()}
        return self.status('DOWNLOADED' if origin=='HTTP_200_RESPONSE' else 'SUPPLIED_SAMPLE_ARCHIVED',
            latest_file=str(raw),diagnostic_csv=str(table),metadata_file=str(meta),
            source_last_ist=latest.isoformat(),row_count=len(times),freshness=metadata['freshness'])
    def fetch_once(self):
        try:auth=credentials(self.root)
        except ValueError:return self.status('AUTH_NOT_CONFIGURED',retry_in_seconds=None)
        try:
            headers={'Origin':self.rules['origin'],'Referer':self.rules['origin']+'/',
                'Accept':'application/json','Content-Type':'application/json',
                'User-Agent':'LocalMarketArchive/1.0','J':auth[AUTH_KEYS[0]],'T':auth[AUTH_KEYS[1]]}
            with self.session.post(self.rules['endpoint'],headers=headers,json=self.rules['payload'],
                timeout=self.rules['request_timeout_seconds'],allow_redirects=False,stream=True) as response:
                code=response.status_code
                if code in (401,403):return self.status('AUTH_OR_ACCESS_DENIED',http_status=code,retry_in_seconds=None)
                if code!=200:
                    delay=self.backoff(response.headers.get('Retry-After'));self.failures+=1
                    return self.status('HTTP_BACKOFF',http_status=code,retry_in_seconds=delay)
                chunks=[];size=0
                for chunk in response.iter_content(chunk_size=65536):
                    size+=len(chunk)
                    if size>self.rules['max_response_bytes']:raise ValueError('RESPONSE_TOO_LARGE')
                    chunks.append(chunk)
                body=json.loads(b''.join(chunks));result=self.archive(body,etag=response.headers.get('ETag'))
                self.failures=0;return result
        except (requests.RequestException,OSError,ValueError,TypeError,OverflowError) as exc:
            delay=self.backoff();self.failures+=1
            return self.status('DOWNLOAD_ERROR',error_type=type(exc).__name__,retry_in_seconds=delay)
    def run(self,once=False,all_hours=False):
        self.acquire()
        try:
            while not self.stop.is_set():
                now=self.clock()
                if not once and not all_hours and self.rules['market_hours_only'] and not self.active(now):
                    self.status('WAITING_FOR_SESSION',retry_in_seconds=self.interval())
                    self.stop.wait(self.interval());continue
                # Owner-authorized NIFTY structured exports share this process and cadence.
                from analyst.nifty_four_collector_8510 import collect_exports
                try:collect_exports(self.root,now)
                except (OSError,ValueError,TypeError):self.status('NIFTY_EXPORT_COLLECTION_ERROR',error_type='LOCAL_EXPORT_UNAVAILABLE')
                result=self.fetch_once()
                if result['status'] in ('AUTH_NOT_CONFIGURED','AUTH_OR_ACCESS_DENIED'):return 2
                if once:return 0 if result['status']=='DOWNLOADED' else 1
                delay=result.get('retry_in_seconds') or self.interval()
                self.status('WAITING_NEXT_DOWNLOAD',last_result=result['status'],next_attempt_ist=datetime.fromtimestamp(self.clock().timestamp()+delay,IST).isoformat(),retry_in_seconds=delay)
                self.stop.wait(delay)
            return 0
        finally:self.close()


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--root',default=str(Path(__file__).resolve().parents[1]));parser.add_argument('--rules');parser.add_argument('--configure',action='store_true');parser.add_argument('--configure-request',action='store_true');parser.add_argument('--reconfigure-request',action='store_true');parser.add_argument('--once',action='store_true');parser.add_argument('--all-hours',action='store_true');parser.add_argument('--sample',help='Archive a supplied historical response WITHOUT a network call');args=parser.parse_args()
    if args.configure or args.configure_request or args.reconfigure_request:
        try:configure_request(args.root,args.reconfigure_request) if args.configure_request or args.reconfigure_request else configure(args.root)
        except ValueError as exc:parser.exit(2,str(exc)+'; no credential values were logged.\n')
        except (OSError,EOFError):parser.exit(2,'Private setup could not complete; no credentials were logged.\n')
        except KeyboardInterrupt:parser.exit(130,'Private setup cancelled.\n')
        return
    downloader=Downloader(args.root,json.loads(Path(args.rules).read_text()) if args.rules else None)
    for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,lambda *_:downloader.stop.set())
    try:
        if args.sample:
            downloader.acquire()
            try:downloader.archive(json.loads(Path(args.sample).read_text()),origin='USER_SUPPLIED_SAMPLE_NOT_A_NETWORK_DOWNLOAD')
            finally:downloader.close()
            return
        code=downloader.run(args.once,args.all_hours)
    except (ValueError,OSError) as exc:
        print(json.dumps({'status':'START_FAILED','reason':str(exc) if isinstance(exc,ValueError) else type(exc).__name__}),flush=True);code=2
    raise SystemExit(code)

if __name__=='__main__':main()
