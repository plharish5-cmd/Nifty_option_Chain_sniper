"""Advisory-only service. Own observation table; no execution callbacks or position writes."""
import json,sqlite3,time,threading,logging,os,tempfile,math
from pathlib import Path
import pandas as pd
from analyst.trend_continuation_entry_8510 import ROOT,config,closed_bars,opening_range_states,continuation_readiness
log=logging.getLogger(__name__)

class TrendCaptureService:
    def __init__(self,cfg,market_db,ledger_db,volume=None,evidence=None):
        self.cfg=config(cfg.get('trend_capture_v192',{}));self.market=Path(market_db);self.ledger=Path(ledger_db)
        self.volume=volume;self.evidence=Path(evidence or ROOT/'analyst/trend_capture_evidence_8510.json');self.lock=threading.Lock();self.cached={};self.at=0
    def backtest(self):
        try:
            report=json.loads(self.evidence.read_text())
            ignored={'enabled','continuation_entry_enabled','execution_action_mode','_evidence'}
            saved=report.get('parameters',{})
            report['parameter_status']='MATCH' if all(saved.get(k)==v for k,v in self.cfg.items() if k not in ignored) else 'CONFIG_CHANGED_REQUIRES_NEW_STUDY'
            return report
        except (OSError,ValueError):return {'mode':'ADVISORY_ONLY','status':'EVIDENCE_NOT_AVAILABLE','trailing_comparison':{'status':'NOT_BUILT_FAILED_GATE'}}
    def status(self,now=None):
        now=time.time() if now is None else now
        if not self.cfg.get('enabled',True):return {'mode':'ADVISORY_ONLY','status':'DISABLED','execution_authorized':False}
        if now-self.at<self.cfg['refresh_seconds'] and self.cached:return self.cached
        if not self.lock.acquire(blocking=False):return self.cached or {'mode':'ADVISORY_ONLY','status':'LOADING','execution_authorized':False}
        try:
            result={'mode':'ADVISORY_ONLY','evaluated_at':now,'execution_authorized':False,'continuation_entry_enabled':self.cfg.get('continuation_entry_enabled',False),
                'active_trailing_gear':'Existing v1.1 unchanged','candidate_stops':{},'final_ratchet':None,
                'trailing_status':'NOT_BUILT_FAILED_GATE','status':'UNAVAILABLE','ladder':[]}
            with sqlite3.connect('file:'+str(self.market)+'?mode=ro',uri=True) as c:
                f=pd.read_sql_query("SELECT ts,o,h,l,c,v FROM candles_1m WHERE sec_id='13' AND ts<=? ORDER BY ts DESC LIMIT 6000",c,params=[int(now)-60]).sort_values('ts')
            b=closed_bars(f,now)
            if not b.empty:
                last=b.iloc[-1];day=str(last.day);today=pd.Timestamp(now,unit='s',tz='UTC').tz_convert('Asia/Kolkata').date().isoformat()
                b=b[b.day==day].copy()
                b['slot_rvol_median']=[self.volume.get_vsr_at_timestamp(int(t)).get('slot_rvol_median') for t in b.end] if self.volume else None
                states=opening_range_states(b,self.cfg);t=int(last.end)
                # Existing same-side open positions are inspected read-only; none are modified.
                active_sides=set()
                with sqlite3.connect('file:'+str(self.ledger)+'?mode=ro',uri=True) as c:
                    if c.execute("select 1 from sqlite_master where name='positions'").fetchone():
                        active_sides={str(r[0])[-2:] for r in c.execute("select symbol from positions where status='OPEN'")}
                side='CE' if last.ema4>last.ema8 else 'PE'
                # Counts are prospective advisory entries logged by this service only, not broker fills.
                n=0
                self.ledger.parent.mkdir(parents=True,exist_ok=True)
                with sqlite3.connect(self.ledger) as c:
                    c.execute('CREATE TABLE IF NOT EXISTS trail_observations (day TEXT,ts INTEGER,payload TEXT,PRIMARY KEY(day,ts))')
                    previous=[json.loads(r[0]) for r in c.execute('select payload from trail_observations where day=? and ts<?',(day,t))]
                    n=sum(bool(p.get('readiness',{}).get('advisory_entry')) for p in previous)
                    ready=continuation_readiness(last,states[t],self.cfg,n,side in active_sides)
                    fresh=day==today and 0<=now-t<900 and '09:15'<=pd.Timestamp(now,unit='s',tz='UTC').tz_convert('Asia/Kolkata').strftime('%H:%M')<'15:20'
                    ready['pattern_ready']=ready['ready'];ready['checks']['fresh_session']=fresh
                    if not fresh:ready['ready']=False;ready['advisory_entry']=False
                    result.update(status='OK' if fresh else 'STALE_CONTEXT',day=day,bar_end=t,opening_range=states[t],readiness=ready,
                        separation_atr=float(last.sep_atr),observations_today=n,
                        ladder=[{'ts':int(row.end),'close':float(row.c),'ema4':float(row.ema4),'ema8':float(row.ema8),
                            'trend_day_confirmed':states[int(row.end)]['trend_day_confirmed']} for _,row in b.iterrows()])
                    if fresh:c.execute('INSERT OR REPLACE INTO trail_observations VALUES(?,?,?)',(day,t,json.dumps(result,allow_nan=False)))
            self.cached=result;self.at=now;return result
        except (OSError,sqlite3.Error,ValueError,KeyError,TypeError):
            log.exception('Trend capture advisory unavailable')
            return {'mode':'ADVISORY_ONLY','status':'UNAVAILABLE','execution_authorized':False,'trailing_status':'NOT_BUILT_FAILED_GATE'}
        finally:self.lock.release()

def update_config(path,body):
    """Atomic update confined to the advisory block; execution mode cannot be armed here."""
    if not isinstance(body,dict):raise ValueError('Object required')
    c=json.loads(Path(path).read_text());allowed=c.get('trend_capture_v192',{})
    changes={}
    for k,v in body.items():
        if k=='execution_action_mode':
            if v!='ADVISORY_ONLY':raise ValueError('Only ADVISORY_ONLY is allowed')
        elif k not in allowed or k.startswith('_'):raise ValueError('Unsupported advisory setting: '+k)
        elif isinstance(allowed[k],bool):
            if type(v) is not bool:raise ValueError('Boolean required: '+k)
            changes[k]=v
        elif isinstance(allowed[k],(int,float)):
            if isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or v<=0:raise ValueError('Positive finite number required: '+k)
            if isinstance(allowed[k],int) and not isinstance(v,int):raise ValueError('Integer required: '+k)
            changes[k]=v
        elif k=='restrict_trailing_by_dte':
            if not isinstance(v,list) or any(type(x) is not int or x<0 for x in v):raise ValueError('Non-negative integer DTE list required')
            changes[k]=v
        else:raise ValueError('Read-only advisory setting: '+k)
    c['trend_capture_v192']={**allowed,**changes,'execution_action_mode':'ADVISORY_ONLY'}
    if c['trend_capture_v192'].get('max_continuation_entries_per_day',2)>2:raise ValueError('Advisory cap cannot exceed 2')
    fd,tmp=tempfile.mkstemp(dir=Path(path).parent,prefix='.trend-config-')
    try:
        with os.fdopen(fd,'w') as out:json.dump(c,out,indent=2);out.write('\n')
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)
    return c['trend_capture_v192']
