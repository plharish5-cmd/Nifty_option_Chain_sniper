"""Local exported-file collector. Does NOT download from a website or call a broker."""
from __future__ import annotations
import csv
import hashlib
import io
import json
import logging
import os
import random
import re
import sqlite3
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from analyst.options_telemetry_engine import RULES, IST, REQUIRED, Rows, load_completed_rows, build_telemetry

log=logging.getLogger(__name__)

def atomic_write(path,content):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(prefix='.'+path.name+'.',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as h:h.write(content);h.flush();os.fsync(h.fileno())
        os.replace(name,path)
    finally:
        if os.path.exists(name):os.unlink(name)

class InstitutionalFileCollector:
    def __init__(self,ledger_db,drop_folder=None,downloads=None,bridge=None,clock=None,rng=None):
        self.ledger_db=Path(ledger_db)
        self.drop_folder=Path(drop_folder or Path(__file__).with_name('institutional_drops')).expanduser()
        self.downloads=Path(downloads or Path.home()/'Downloads').expanduser()
        self.bridge=bridge;self.clock=clock or (lambda:datetime.now(IST));self.rng=rng or random.SystemRandom()
        self.cfg=RULES['collector'];self._lock=threading.Lock();self._seen={};self._next=0.;self._status={}
        self.ledger_db.parent.mkdir(parents=True,exist_ok=True)
        with sqlite3.connect(self.ledger_db) as db:
            db.execute('CREATE TABLE IF NOT EXISTS institutional_collected_rows(session_date TEXT,minute INTEGER,payload TEXT,digest TEXT,source_file TEXT,PRIMARY KEY(session_date,minute))')
            db.execute('CREATE TABLE IF NOT EXISTS institutional_collector_conflicts(session_date TEXT,minute INTEGER,digest TEXT,payload TEXT,source_file TEXT,PRIMARY KEY(session_date,minute,digest))')
            db.execute('CREATE TABLE IF NOT EXISTS institutional_collector_events(session_date TEXT,minute INTEGER,telemetry_id INTEGER,PRIMARY KEY(session_date,minute))')
            db.execute('''CREATE TABLE IF NOT EXISTS institutional_pe_ce_telemetry(id INTEGER PRIMARY KEY AUTOINCREMENT,evaluated_at REAL NOT NULL,source_key TEXT NOT NULL,event_time TEXT,mode TEXT,data_status TEXT,day_regime TEXT,bias_score_pct REAL,confidence_pct REAL,interpreter_source TEXT NOT NULL,output_json TEXT NOT NULL,deterministic_json TEXT)''')
    def active(self,now):
        return now.weekday()<5 and self.cfg['session_start']<=now.strftime('%H:%M')<=self.cfg['session_end']
    def interval(self):return self.rng.uniform(self.cfg['poll_min_seconds'],self.cfg['poll_max_seconds'])
    def status(self):
        return {**self._status,'drop_folder_path':str(self.drop_folder),'source_mode':'LOCAL_EXPORT_FILES_ONLY','automatic_website_download':False,'next_poll_in_sec':max(0,round(self._next-self.clock().timestamp(),1))}
    def collect_once(self,now=None,force=False):
        now=now or self.clock();now=now.replace(tzinfo=IST) if now.tzinfo is None else now.astimezone(IST)
        if not force and (not self.active(now) or now.timestamp()<self._next):return self.status()
        if not self._lock.acquire(blocking=False):return self.status()
        try:
            self._next=now.timestamp()+self.interval()
            candidates=[]
            for folder in (self.downloads,self.drop_folder):
                if folder.exists():candidates.extend(p for p in folder.glob(self.cfg['watch_glob']) if p.suffix.lower() in self.cfg['extensions'] and not p.is_symlink())
            for p in sorted(set(candidates),key=lambda p:(p.stat().st_mtime_ns,str(p))):
                named=re.search(r'(\d{4}-\d{2}-\d{2})',p.name)
                if not force and named and named.group(1)!=now.date().isoformat():continue
                stat=p.stat();fingerprint=(stat.st_mtime_ns,stat.st_size)
                # Pending top rows must be reconsidered after their minute closes,
                # even when the downloaded file itself has not changed.
                key=(fingerprint,now.strftime('%Y-%m-%dT%H:%M'))
                if self._seen.get(str(p))==key:continue
                try:
                    self.ingest_export(p,now)
                    self._seen[str(p)]=key
                except (OSError,ValueError,TypeError,sqlite3.Error,csv.Error) as exc:
                    log.warning('Local export rejected (%s): %s',p.name,type(exc).__name__)
                    self._status.update(last_error=type(exc).__name__,latest_file_name=p.name)
            return self.status()
        finally:self._lock.release()
    def ingest_export(self,path,now=None):
        now=now or self.clock();p=Path(path);before=p.stat()
        if before.st_size>self.cfg['max_file_bytes']:raise ValueError('Export exceeds configured size')
        data=load_completed_rows(p);after=p.stat()
        if (before.st_mtime_ns,before.st_size)!=(after.st_mtime_ns,after.st_size):raise ValueError('Export still being written')
        if data.errors:raise ValueError('Malformed/conflicting export rows')
        match=re.search(r'(\d{4}-\d{2}-\d{2})',p.name)
        dates={datetime.fromisoformat(r['_source_timestamp']).date().isoformat() for r in data if r.get('_source_timestamp')}
        if len(dates)>1:raise ValueError('Mixed session dates')
        session=next(iter(dates)) if dates else match.group(1) if match else None
        if not session:raise ValueError('Export needs declared session date in name or timestamps')
        datetime.fromisoformat(session)
        if match and dates and match.group(1)!=session:raise ValueError('Filename/source dates conflict')
        # Never relabel a historical file as live just because it was downloaded today.
        data=Rows(r for r in data if self.cfg['session_start']<=f"{r['_minute']//60:02d}:{r['_minute']%60:02d}"<=self.cfg['session_end'])
        pending=[r for r in data if session==now.date().isoformat() and r['_minute']>=now.hour*60+now.minute]
        if pending:data=Rows(r for r in data if r['_minute']<now.hour*60+now.minute)
        if data:
            assessed=build_telemetry(data,reference_now=now if session==now.date().isoformat() else None)
            quality=assessed['input_telemetry']['data_quality']
            if quality['partial_latest_row_discarded']:pending=[data.pop()]+pending
        with sqlite3.connect(self.ledger_db) as db:
            db.row_factory=sqlite3.Row
            added=0;conflicts=0
            for row in data:
                payload=json.dumps(row,sort_keys=True,allow_nan=False);digest=hashlib.sha256(payload.encode()).hexdigest()
                old=db.execute('SELECT digest FROM institutional_collected_rows WHERE session_date=? AND minute=?',(session,row['_minute'])).fetchone()
                if old:
                    if old[0]!=digest:
                        db.execute('INSERT OR IGNORE INTO institutional_collector_conflicts VALUES(?,?,?,?,?)',(session,row['_minute'],digest,payload,p.name));conflicts+=1
                    continue
                db.execute('INSERT INTO institutional_collected_rows VALUES(?,?,?,?,?)',(session,row['_minute'],payload,digest,p.name));added+=1
            stored=[json.loads(r[0]) for r in db.execute('SELECT payload FROM institutional_collected_rows WHERE session_date=? ORDER BY minute',(session,))]
            evaluated={r[0] for r in db.execute('SELECT minute FROM institutional_collector_events WHERE session_date=?',(session,))}
        latest=None;latest_id=None
        # Pure calculations happen OUTSIDE all writer transactions. A first-day
        # bulk export must not monopolize the shared execution ledger lock.
        for end,row in enumerate(stored,1):
            if row['_minute'] in evaluated:continue
            result=build_telemetry(stored[:end],discard_partial_bar=False)
            result.update(interpreter_source='DETERMINISTIC_PYTHON_ENGINE',narrative_status='DISABLED',collector_session_date=session,source_date_basis='SOURCE_TIMESTAMP' if dates else 'DECLARED_FILENAME_UNVERIFIED')
            encoded=json.dumps(result,allow_nan=False)
            with sqlite3.connect(self.ledger_db) as db:
                if db.execute('SELECT 1 FROM institutional_collector_events WHERE session_date=? AND minute=?',(session,row['_minute'])).fetchone():continue
                cur=db.execute('INSERT INTO institutional_pe_ce_telemetry(evaluated_at,source_key,event_time,mode,data_status,day_regime,bias_score_pct,confidence_pct,interpreter_source,output_json,deterministic_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)',(now.timestamp(),'collector:'+session+':'+str(row['_minute']),result['timestamp'],'HISTORICAL_FILE',result['data_status'],result['day_regime'],result['bias_score_pct'],result['confidence_pct'],result['interpreter_source'],encoded,encoded))
                latest=result;latest_id=cur.lastrowid
                db.execute('INSERT INTO institutional_collector_events VALUES(?,?,?)',(session,row['_minute'],latest_id))
        fields=['time',*REQUIRED,'put_oi_day_chg','call_oi_day_chg']
        buffer=io.StringIO();writer=csv.DictWriter(buffer,fieldnames=fields);writer.writeheader()
        for r in stored:writer.writerow({key:r.get(key) for key in fields})
        target=self.drop_folder/('PE_CE_NIFTY_'+session+'.csv')
        content=buffer.getvalue().encode()
        if not target.exists() or target.read_bytes()!=content:atomic_write(target,content)
        self._status.update(latest_file_name=p.name,session_date=session,rows_collected=len(stored),rows_collected_today=len(stored) if session==now.date().isoformat() else 0,last_row_time_ist=stored[-1]['time'] if stored else None,last_ingested_hhmm=f"{stored[-1]['_minute']//60:02d}:{stored[-1]['_minute']%60:02d}" if stored else None,pending_row_time=pending[0]['time'] if pending else None,conflicting_rows=conflicts,last_error=None,rows_added=added)
        if latest is None and stored and self.bridge is not None and not self.bridge.snapshot().get('timestamp'):
            with sqlite3.connect(self.ledger_db) as db:
                saved=db.execute('SELECT t.id,t.output_json FROM institutional_collector_events e JOIN institutional_pe_ce_telemetry t ON t.id=e.telemetry_id WHERE e.session_date=? ORDER BY e.minute DESC LIMIT 1',(session,)).fetchone()
                if saved:latest_id,encoded=saved;latest=json.loads(encoded)
        if latest is not None and self.bridge is not None:self.bridge.publish_collected(latest,latest_id)
        return self.status()

if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--ledger',required=True);parser.add_argument('--drops');parser.add_argument('--downloads');parser.add_argument('--once',action='store_true');parser.add_argument('--scan-now',action='store_true',help='One explicit historical-file scan outside collection hours');args=parser.parse_args()
    collector=InstitutionalFileCollector(args.ledger,args.drops,args.downloads)
    previous=None
    while True:
        status=collector.collect_once(force=args.scan_now);signature={k:v for k,v in status.items() if k!='next_poll_in_sec'}
        if signature!=previous or args.once:print(json.dumps(status,default=str),flush=True);previous=signature
        if args.once or args.scan_now:break
        try:time.sleep(RULES['watch_seconds'])
        except KeyboardInterrupt:break
