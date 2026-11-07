"""NO_GO-compatible measurement observer. No order submission/modify/cancel calls."""
import hashlib
import json
import math
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

IST=ZoneInfo('Asia/Kolkata')
CONFIG=Path(__file__).with_suffix('.json')


def family(signal):
    s=signal.get('strategy_id','')
    for prefix,name in [('STAGE_2','stage2_long'),('STAGE_3','stage3_short'),
                        ('CROSSOVER','crossover_reversal'),('H1_','h1_confluence')]:
        if s.startswith(prefix):return name
    return None


def number(value):
    try:
        x=float(value)
        return x if math.isfinite(x) and x>0 else None
    except (TypeError,ValueError):return None


def clean(value):
    if isinstance(value,float) and not math.isfinite(value):return None
    if isinstance(value,dict):return {k:clean(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)):return [clean(v) for v in value]
    return value


class DryEvidence:
    def __init__(self,path,config=CONFIG):
        self.path=Path(path);self.config=Path(config)

    def cfg(self):return json.loads(self.config.read_text())

    @contextmanager
    def db(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        c=sqlite3.connect(self.path,timeout=1)
        c.execute('CREATE TABLE IF NOT EXISTS observations(id TEXT PRIMARY KEY,ts REAL,family TEXT,kind TEXT,payload TEXT)')
        c.execute('CREATE INDEX IF NOT EXISTS obs_family_ts ON observations(family,ts)')
        try:
            with c:yield c
        finally:c.close()

    def save(self,key,now,fam,kind,payload):
        with self.db() as c:
            c.execute('INSERT OR IGNORE INTO observations VALUES(?,?,?,?,?)',
                      (key,now,fam,kind,json.dumps(clean(payload),default=str,allow_nan=False)))

    async def observe(self,backend,signals,now=None):
        now=float(now if now is not None else time.time());cfg=self.cfg()
        if not cfg['enabled']:return
        dt=datetime.fromtimestamp(now,IST);telemetry=backend.cached_telemetry or {}
        try:
            age=now-datetime.strptime(telemetry.get('timestamp_ist'),'%Y-%m-%d %H:%M:%S IST').replace(tzinfo=IST).timestamp()
        except (ValueError,TypeError):age=None
        market=telemetry.get('market_status') or {};spot=telemetry.get('spot') or {}
        fresh=(age is not None and 0<=age<=cfg['telemetry_max_age_seconds']
               and spot.get('is_stale') is False and number(spot.get('price')) is not None)
        candle_ts=getattr(backend,'_last_scanner_source_ts',None)
        candles_fresh=(number(candle_ts) is not None and 0<=now-candle_ts<=cfg['candle_max_age_seconds'])
        session=(dt.weekday()<5 and cfg['session_start']<=dt.strftime('%H:%M')<cfg['session_end']
                 and market.get('is_open') is True)
        context={k:telemetry.get(k) for k in ['timestamp_ist','spot','trend_watchlist','dhan_oi',
            'dhan_oi_synergy','dhan_oi_synergy_config','five_factor_confluence','goldilocks_separation','futures_volume_surge']}
        context['indicator_basis']='Existing scanner snapshot; forming-bar values may be included'
        context['scanner_candle_latest_ts']=candle_ts
        for fam in cfg['families']:
            state=dict(backend.strategy_status.get(fam) or {})
            candidates=[s for s in signals if family(s)==fam]
            reason=('MARKET_CLOSED' if not session else 'STALE_TELEMETRY' if not fresh else
                    'KILL_SWITCH' if backend.kill_switch_active else
                    'SCANNER_PAUSED' if not backend.scanner_enabled else
                    'STALE_CANDLES' if not candles_fresh else
                    'CANDIDATE' if candidates else 'NO_ACTIONABLE_SIGNAL' if state.get('state')=='TRIGGERED' else state.get('state','NO_SIGNAL'))
            payload=dict(setup=state.get('name',fam),state=state,reason=reason,context=context,
                active_party=backend.active_party,directional_bias=backend.directional_bias,
                signals=candidates,mode='OBSERVATION_ONLY',live_validation_status=backend.cfg.get('execution_safety_v194',{}).get('live_validation_status'))
            slot=int(now//cfg['sample_seconds'])
            self.save(f'{dt.date()}:{fam}:{slot}:{reason}',now,fam,'STATE',payload)
            if reason!='CANDIDATE':continue
            for signal in candidates:
                sid=signal['strategy_id']
                with self.db() as c:
                    prior=c.execute("SELECT max(ts) FROM observations WHERE family=? AND kind='TRIGGER'",(fam,)).fetchone()[0]
                    last_quote=c.execute("SELECT max(ts) FROM observations WHERE kind='TRIGGER'").fetchone()[0]
                if prior is not None and now-prior<cfg['signal_cooldown_seconds']:continue
                # Defer another family until the next scanner pass, rather than
                # burst four quote requests and trip the shared Dhan rate limit.
                if last_quote is not None and now-last_quote<cfg['quote_min_interval_seconds']:continue
                key=hashlib.sha256(f'{dt.date()}:{sid}:{int(now)}'.encode()).hexdigest()[:24]
                detail={**payload,'signal':signal,'measurement':'PENDING','qualified':False}
                # Persist attempt before network IO: failures/restarts cannot hammer depth.
                self.save(key,now,fam,'TRIGGER',detail)
                contract=None
                for cid in (f"LONG_{signal['opt_side']}_ATM",f"LONG_{signal['opt_side']}_ITM"):
                    if not backend.algo_strike_selection.get(cid,True):continue
                    contract=next((c for c in backend.priority_contracts if c.get('id')==cid),None)
                    if contract:break
                detail['contract']=contract;q={}
                try:
                    security_id=int(contract['security_id']) if contract else 0
                    qty=int(contract.get('lot_size') or 0) if contract else 0
                    if security_id>0 and qty>0:
                        depth=await backend.broker.get_marketfeed_quote({'NSE_FNO':[security_id]})
                        q=depth.get(str(security_id)) or {}
                    bid=number(q.get('bid'));ask=number(q.get('ask'));ltp=number(q.get('last_price'))
                    qualified=bool(bid and ask and ask>=bid and ltp and qty>0)
                    detail.update(quote=q,quote_received_at=time.time(),measurement='MEASURED_SPREAD_AT_RECEIPT' if qualified else 'NO_VALID_DEPTH_OR_CONTRACT',qualified=qualified,
                        quote_timestamp_basis='HTTP receipt, not exchange timestamp; not measured fill slippage')
                    if qualified:
                        # Existing helper only inserts a measurement row; it never executes.
                        row=backend._record_dry_run_order(contract,'BUY',qty,ltp,
                            signal['setup_name'],'EVIDENCE_'+key,q)
                        detail['measurement_order_id']=row.get('order_id')
                        detail['intended_limit_price']=row.get('intended_limit_price')
                except Exception as exc:
                    detail.update(measurement='QUOTE_OR_RECORD_ERROR',error=type(exc).__name__,qualified=False)
                with self.db() as c:
                    c.execute('UPDATE observations SET payload=? WHERE id=?',(json.dumps(clean(detail),default=str,allow_nan=False),key))

    def status(self):
        cfg=self.cfg();stats={f:dict(states=0,triggers=0,valid_spreads=0) for f in cfg['families']}
        if not self.path.exists():return dict(ok=True,enabled=cfg['enabled'],rows=[],families=stats)
        with self.db() as c:
            raw=c.execute('SELECT ts,family,kind,payload FROM observations ORDER BY ts DESC,rowid DESC LIMIT 100').fetchall()
            for f in cfg['families']:
                stats[f]=dict(states=c.execute("SELECT count(*) FROM observations WHERE family=? AND kind='STATE'",(f,)).fetchone()[0],
                    triggers=c.execute("SELECT count(*) FROM observations WHERE family=? AND kind='TRIGGER'",(f,)).fetchone()[0],
                    valid_spreads=c.execute("SELECT count(*) FROM observations WHERE family=? AND kind='TRIGGER' AND json_extract(payload,'$.qualified')=1",(f,)).fetchone()[0])
        return dict(ok=True,enabled=cfg['enabled'],families=stats,rows=[dict(ts=t,family=f,kind=k,**json.loads(p)) for t,f,k,p in raw],
                    note='Prospective spread observations only. No orders or automatic approval. Missing fields remain unknown.')
