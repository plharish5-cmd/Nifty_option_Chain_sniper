"""Forward-only observations. Own WAL database and bounded worker; no execution API.

`ts` in contract_tick is a receipt-minute bucket; observed_at is the real HTTP
receipt time, NOT a trade timestamp. Neither quotes nor candles are synthesised.
The recorder never backfills past minutes after startup or fills source gaps.
"""
from __future__ import annotations
import csv
import io
import json
import logging
import math
import queue
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
IST = ZoneInfo('Asia/Kolkata')
log = logging.getLogger(__name__)
EMA_COLUMNS = ('date time_ist timeframe nifty_spot_price close_price ma4 ema8 gap_points '
               'gap_vs_atr price_vs_ema8_points price_vs_ma4_points proximity_status '
               'crossover_status support_resistance flip_count_12bar trend signal status').split()
CAPTION = ('Max move is the maximum by construction and cannot support a trading rule on its '
           'own. "T15 before SL" is the tradeable number. Ambiguous rows are minutes where '
           "target and stop both sat inside one bar's range and the order is unknowable. "
           'These are observations, not an edge: n is small and no cost model is applied.')


def stamp(day, hhmm):
    return int(datetime.fromisoformat(day + 'T' + hhmm).replace(tzinfo=IST).timestamp())


def connect_ro(path):
    c = sqlite3.connect('file:' + str(path) + '?mode=ro', uri=True, timeout=.2)
    c.row_factory = sqlite3.Row
    return c


def coverage(times, start, end, step=60):
    """Count occupied expected slots; include start/end gaps, never count duplicate polls."""
    expected = max(0, math.ceil((end - start) / step))
    slots = sorted({int((t - start) // step) for t in times if start <= t < end})
    bounds = [-1] + slots + [expected]
    gaps = [(max(0, b-a-1), start+(a+1)*step) for a,b in zip(bounds,bounds[1:])]
    gap, at = max(gaps, default=(0,start))
    return dict(n_snapshots=len(slots), expected_snapshots=expected,
                coverage_pct=round(100*len(slots)/expected,2) if expected else None,
                first_ts=min(times) if times else None, last_ts=max(times) if times else None,
                max_gap_minutes=round(gap*step/60,2), max_gap_at=datetime.fromtimestamp(at,IST).strftime('%H:%M'))


class RecordingStore:
    def __init__(self, path=None, csv_dir=None, cfg=None):
        self.path = Path(path or ROOT/'data/terminal8510/recording.sqlite3')
        self.csv_dir = Path(csv_dir or ROOT/'analyst/proximity_data')
        self.cfg = cfg or {}
        self.last_error = None
        self.last_write = None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as c:
            c.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS chain_snap(
              expiry TEXT, ts REAL, strike REAL, ce_oi REAL, pe_oi REAL,
              ce_ltp REAL, pe_ltp REAL, ce_vol REAL, pe_vol REAL,
              ce_bid REAL, ce_ask REAL, pe_bid REAL, pe_ask REAL,
              spot REAL, source TEXT, PRIMARY KEY(expiry,ts,strike));
            CREATE TABLE IF NOT EXISTS chain_coverage(
              session_date TEXT PRIMARY KEY, n_snapshots INTEGER, first_ts REAL,last_ts REAL,
              expected_snapshots INTEGER,coverage_pct REAL,max_gap_minutes REAL,
              max_gap_at TEXT,strikes_per_snapshot_median REAL,source TEXT);
            CREATE TABLE IF NOT EXISTS contract_tick(
              ts INTEGER,session_date TEXT,session_time TEXT,security_id TEXT,symbol TEXT,
              expiry TEXT,strike REAL,opt_type TEXT,ltp REAL,bid REAL,ask REAL,volume REAL,oi REAL,
              spot REAL,source TEXT,observed_at REAL,exchange_ts TEXT,
              PRIMARY KEY(ts,security_id));
            CREATE INDEX IF NOT EXISTS contract_tick_identity_time ON contract_tick(security_id,ts);
            CREATE INDEX IF NOT EXISTS contract_tick_session ON contract_tick(session_date,security_id,ts);
            CREATE INDEX IF NOT EXISTS chain_snap_time ON chain_snap(ts,strike);
            CREATE TABLE IF NOT EXISTS tracked_contract(
              security_id TEXT PRIMARY KEY, identity_json TEXT,start_ts INTEGER,until_ts INTEGER);
            CREATE TABLE IF NOT EXISTS recorder_state(key TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE IF NOT EXISTS ema_proximity_history(
              date TEXT,time_ist TEXT,timeframe TEXT,nifty_spot_price REAL,close_price REAL,
              ma4 REAL,ema8 REAL,gap_points REAL,gap_vs_atr REAL,price_vs_ema8_points REAL,
              price_vs_ma4_points REAL,proximity_status TEXT,crossover_status TEXT,
              support_resistance TEXT,flip_count_12bar INTEGER,trend TEXT,signal TEXT,status TEXT,
              PRIMARY KEY(date,time_ist,timeframe));
            CREATE TABLE IF NOT EXISTS radar_tier_signals(
              seq INTEGER PRIMARY KEY AUTOINCREMENT, signal_id TEXT UNIQUE,date TEXT,time_ist TEXT,
              signal_time_ist TEXT,tier INTEGER,signal_name TEXT,direction TEXT,signal_price REAL,
              nifty_spot_at_signal REAL,micro_sl_price REAL,risk_points REAL,
              contract_security_id TEXT,contract_symbol TEXT,premium_at_signal REAL,
              current_nifty_spot REAL,points_moved_now REAL,max_favourable_spot REAL,
              max_points_moved REAL DEFAULT 0,max_move_time_ist TEXT,max_adverse_spot REAL,
              max_adverse_points REAL DEFAULT 0,premium_now REAL,max_favourable_premium REAL,
              max_premium_points REAL,premium_gap INTEGER DEFAULT 0,
              outcome TEXT DEFAULT 'RUNNING',hit_sl_later TEXT DEFAULT 'NO',sl_hit_time_ist TEXT,
              final_points REAL,fire_ts REAL,last_bar_ts INTEGER,spot_gap INTEGER DEFAULT 0,
              UNIQUE(date,time_ist,tier,signal_name,direction,signal_price));
            ''')
            for n in self.targets:
                columns={r[1] for r in c.execute('PRAGMA table_info(radar_tier_signals)')}
                for suffix in ('hit_before_sl','hit_time_ist'):
                    name=f'target_{n}_{suffix}'
                    if name not in columns:c.execute(f'ALTER TABLE radar_tier_signals ADD COLUMN {name} TEXT')

        with self.db() as c:
            previous=c.execute("SELECT value FROM recorder_state WHERE key='last_observation_write'").fetchone()
            self.last_write=float(previous[0]) if previous else None

    @property
    def targets(self):
        return tuple(int(n) for n in self.cfg.get('radar_targets_pts',[10,15,20,25,30]))

    def db(self):
        c=sqlite3.connect(self.path, timeout=.2);c.row_factory=sqlite3.Row
        return c

    def written(self,c):
        self.last_write=time.time()
        c.execute("INSERT OR REPLACE INTO recorder_state VALUES('last_observation_write',?)",(str(self.last_write),))

    def safe(self, fn, *args, **kwargs):
        try:
            return fn(*args,**kwargs)
        except Exception as exc:
            self.last_error=type(exc).__name__+': '+str(exc)
            log.warning('8510 recording failed open: %s',self.last_error)
            return None

    def track(self, contract, now, until=None):
        sid=str(contract.get('security_id') or '')
        if not sid or not contract.get('expiry') or contract.get('opt_type') not in ('CE','PE'):return
        day=datetime.fromtimestamp(now,IST).date().isoformat()
        until=max(stamp(day,'15:20'),int(until or now))
        with self.db() as c:
            c.execute('''INSERT INTO tracked_contract VALUES(?,?,?,?) ON CONFLICT(security_id)
              DO UPDATE SET start_ts=CASE WHEN until_ts<? THEN excluded.start_ts ELSE start_ts END,until_ts=max(until_ts,excluded.until_ts)''',
              (sid,json.dumps(contract),int(now)//60*60,until,stamp(day,'00:00')))

    def record_tick(self, contract, quote, observed_at, spot=None, source='DHAN_QUOTE'):
        if source not in ('DHAN_QUOTE','CHAIN_SNAP'):raise ValueError('Unverified quote source')
        ltp=quote.get('last_price',quote.get('ltp'))
        if ltp is None or not math.isfinite(float(ltp)) or float(ltp)<=0:return False
        dt=datetime.fromtimestamp(observed_at,IST);ts=int(observed_at)//60*60
        depth=quote.get('depth') or {}
        def best(side,key):
            arr=depth.get(side) or []
            return arr[0].get('price') if arr else quote.get(key)
        vals=(ts,dt.date().isoformat(),dt.strftime('%H:%M:%S'),str(contract['security_id']),
              contract.get('symbol'),contract.get('expiry'),contract.get('strike'),contract.get('opt_type'),
              float(ltp),best('buy','bid'),best('sell','ask'),quote.get('volume'),quote.get('oi'),spot,
              source,observed_at,quote.get('last_trade_time'))
        with self.db() as c:
            cur=c.execute('INSERT OR IGNORE INTO contract_tick VALUES('+','.join('?'*len(vals))+')',vals)
            if cur.rowcount:self.written(c)
        return True

    def record_chain(self, payload, expiry, observed_at):
        d=payload.get('data',payload);oc=d.get('oc') or {}
        dt=datetime.fromtimestamp(observed_at,IST)
        if not oc or not '09:15'<=dt.strftime('%H:%M')<'15:30':return
        rows=[]
        for k,v in oc.items():
            ce,pe=v.get('ce') or {},v.get('pe') or {}
            rows.append((expiry,observed_at,float(k),ce.get('oi'),pe.get('oi'),ce.get('last_price'),
                         pe.get('last_price'),ce.get('volume'),pe.get('volume'),ce.get('top_bid_price'),
                         ce.get('top_ask_price'),pe.get('top_bid_price'),pe.get('top_ask_price'),
                         d.get('last_price'),'DHAN_CHAIN_RECEIPT'))
        with self.db() as c:
            cur=c.executemany('INSERT OR IGNORE INTO chain_snap VALUES('+','.join('?'*15)+')',rows)
            if cur.rowcount:self.written(c)
        # Coverage counts 3-minute occupied slots, not repeated 30-second chain polls.
        self.chain_ledger(dt.date().isoformat(),observed_at)

    def chain_ledger(self,day,now):
        with self.db() as c:
            rows=c.execute('SELECT ts,count(*) n FROM chain_snap WHERE ts>=? AND ts<? GROUP BY ts',
                           (stamp(day,'09:15'),stamp(day,'15:30'))).fetchall()
            cov=coverage([r['ts'] for r in rows],stamp(day,'09:15'),stamp(day,'15:30'),
                         int(self.cfg.get('chain_refresh_minutes',3)*60))
            import statistics
            cov['strikes_per_snapshot_median']=statistics.median([r['n'] for r in rows]) if rows else None
            vals=(day,*(cov[k] for k in ('n_snapshots','first_ts','last_ts','expected_snapshots',
                  'coverage_pct','max_gap_minutes','max_gap_at','strikes_per_snapshot_median')),'8510_RECEIPT_SLOTS')
            c.execute('INSERT OR REPLACE INTO chain_coverage VALUES('+','.join('?'*len(vals))+')',vals)
        cov['raw_refresh_count']=len(rows)
        cov['elapsed']=coverage([r['ts'] for r in rows],stamp(day,'09:15'),min(stamp(day,'15:30'),max(stamp(day,'09:15'),int(now)//60*60)),int(self.cfg.get('chain_refresh_minutes',3)*60))
        return cov

    def persist_ema(self, rows):
        """DB is authoritative; daily CSV reconciles a crash between DB and append."""
        days=set()
        with self.db() as c:
            before=c.total_changes
            for row in rows:
                c.execute('INSERT OR IGNORE INTO ema_proximity_history VALUES('+','.join('?'*len(EMA_COLUMNS))+')',
                          tuple(row.get(k) for k in EMA_COLUMNS));days.add(row['date'])
            if c.total_changes>before:self.written(c)
        self.csv_dir.mkdir(parents=True,exist_ok=True)
        for day in days:
            path=self.csv_dir/(day+'.csv');seen=set()
            if path.exists():
                with path.open(newline='') as f:
                    seen={(r['date'],r['time_ist'],r['timeframe']) for r in csv.DictReader(f)}
            with self.db() as c, path.open('a',newline='') as f:
                w=csv.DictWriter(f,fieldnames=EMA_COLUMNS)
                if f.tell()==0:w.writeheader()
                for r in c.execute('SELECT * FROM ema_proximity_history WHERE date=? ORDER BY time_ist,timeframe',(day,)):
                    if (r['date'],r['time_ist'],r['timeframe']) not in seen:w.writerow(dict(r))

    def reload(self,today):
        yesterday=(date.fromisoformat(today)-timedelta(days=1)).isoformat()
        with self.db() as c:return [dict(r) for r in c.execute('SELECT * FROM ema_proximity_history WHERE date BETWEEN ? AND ? ORDER BY date,time_ist,timeframe',(yesterday,today))]

    def fire(self,signal,now):
        """No price conversion. Contract and premium may be absent, never fabricated."""
        dt=datetime.fromtimestamp(now,IST);day=dt.date().isoformat()
        if not '09:15'<=dt.strftime('%H:%M')<'15:20':return None
        s=dict(signal);spot=float(s['nifty_spot_at_signal']);sl=float(s['micro_sl_price'])
        side=s['direction'];sign=1 if side=='CE' else -1
        if side not in ('CE','PE') or sign*(spot-sl)<=0:raise ValueError('Micro SL is not protective relative to observed spot')
        vals=dict(date=day,time_ist=dt.strftime('%H:%M'),signal_time_ist=dt.strftime('%H:%M:%S'),
                  tier=int(s['tier']),signal_name=s['signal_name'],direction=side,
                  signal_price=float(s['signal_price']),nifty_spot_at_signal=spot,
                  micro_sl_price=sl,risk_points=round(abs(spot-sl),4),
                  contract_security_id=s.get('contract_security_id'),contract_symbol=s.get('contract_symbol'),
                  premium_at_signal=s.get('premium_at_signal'),current_nifty_spot=spot,points_moved_now=0,
                  max_favourable_spot=spot,max_adverse_spot=spot,fire_ts=now,
                  last_bar_ts=int(now)//60*60-60)
        with self.db() as c:
            cur=c.execute('INSERT OR IGNORE INTO radar_tier_signals('+','.join(vals)+') VALUES('+','.join('?'*len(vals))+')',tuple(vals.values()))
            if not cur.rowcount:return None
            sid='RT_'+dt.strftime('%Y%m%d%H%M%S')+'_'+str(cur.lastrowid)
            c.execute('UPDATE radar_tier_signals SET signal_id=? WHERE seq=?',(sid,cur.lastrowid))
            self.written(c)
        return sid

    def update_bar(self,bar):
        ts=int(bar['ts']);dt=datetime.fromtimestamp(ts+60,IST);day=dt.date().isoformat()
        if dt.strftime('%H:%M')>'15:20':self.freeze(day);return
        with self.db() as c:
            signals=c.execute("SELECT * FROM radar_tier_signals WHERE date=? AND outcome='RUNNING' AND last_bar_ts<? AND fire_ts<=?",(day,ts,ts)).fetchall()
            for raw in signals:
                r=dict(raw);sign=1 if r['direction']=='CE' else -1;spot=r['nifty_spot_at_signal']
                fav=float(bar['h'] if sign==1 else bar['l']);adv=float(bar['l'] if sign==1 else bar['h']);close=float(bar['c'])
                gap=int(ts>max(int(math.ceil(r['fire_ts']/60)*60),r['last_bar_ts']+60)) or r['spot_gap']
                stop=(adv<=r['micro_sl_price'] if sign==1 else adv>=r['micro_sl_price'])
                updates=dict(current_nifty_spot=close,points_moved_now=round(sign*(close-spot),4),last_bar_ts=ts,spot_gap=gap)
                ambiguous=False
                for n in self.targets:
                    key=f'target_{n}_hit_before_sl'
                    if r[key] is not None:continue
                    hit=sign*(fav-spot)>=n
                    if hit or stop:
                        result='AMBIGUOUS' if (hit and stop) or gap else 'YES' if hit else 'NO'
                        updates[key]=result;updates[f'target_{n}_hit_time_ist']=dt.strftime('%H:%M:%S')
                        ambiguous |= result=='AMBIGUOUS'
                # Stop-hit bar cannot supply a newly favourable max with unknown sequencing.
                if not stop and sign*(fav-r['max_favourable_spot'])>0:
                    updates.update(max_favourable_spot=fav,max_points_moved=round(sign*(fav-spot),4),max_move_time_ist=dt.strftime('%H:%M:%S'))
                if sign*(adv-r['max_adverse_spot'])<0:
                    updates.update(max_adverse_spot=adv,max_adverse_points=round(max(0,sign*(spot-adv)),4))
                premium=c.execute('SELECT ltp FROM contract_tick WHERE security_id=? AND ts=?',(r['contract_security_id'],ts)).fetchone()
                pgap=r['premium_gap'] or premium is None or r['premium_at_signal'] is None
                updates.update(premium_gap=int(pgap),premium_now=None if pgap else premium[0],
                               max_favourable_premium=None,max_premium_points=None)
                if not pgap:
                    pm=max(r['premium_at_signal'],r['max_favourable_premium'] or r['premium_at_signal'],premium[0])
                    updates.update(max_favourable_premium=pm,max_premium_points=round(pm-r['premium_at_signal'],4))
                if stop:
                    updates.update(outcome='AMBIGUOUS' if ambiguous or gap else 'HIT_SL',hit_sl_later='YES',sl_hit_time_ist=dt.strftime('%H:%M:%S'),final_points=round(sign*(r['micro_sl_price']-spot),4))
                c.execute('UPDATE radar_tier_signals SET '+','.join(k+'=?' for k in updates)+' WHERE seq=?',(*updates.values(),r['seq']))
                self.written(c)
        if dt.strftime('%H:%M')=='15:20':self.freeze(day)

    def freeze(self,day):
        cutoff=stamp(day,'15:20')
        with self.db() as c:
            signals=c.execute("SELECT * FROM radar_tier_signals WHERE date=? AND outcome='RUNNING'",(day,)).fetchall()
            for raw in signals:
                r=dict(raw);gap=r['spot_gap'] or r['last_bar_ts']<cutoff-60
                values=dict(outcome='EXPIRED_1520',final_points=None if gap else r['points_moved_now'],spot_gap=int(bool(gap)))
                for n in self.targets:
                    key=f'target_{n}_hit_before_sl'
                    if r[key] is None:
                        values[key]='AMBIGUOUS' if gap else 'NO'
                        values[f'target_{n}_hit_time_ist']='15:20:00'
                if gap:values.update(premium_now=None,max_favourable_premium=None,max_premium_points=None,premium_gap=1)
                c.execute('UPDATE radar_tier_signals SET '+','.join(k+'=?' for k in values)+' WHERE seq=?',(*values.values(),r['seq']))
                self.written(c)

    def radar(self,day,limit=100):
        with self.db() as c:
            rows=[dict(r) for r in c.execute('SELECT * FROM radar_tier_signals WHERE date=? ORDER BY seq DESC LIMIT ?',(day,limit))]
            summary=[]
            for tier in (1,2,3):
                x=c.execute('''SELECT count(*) n,avg(max_points_moved) avg_max,avg(risk_points) avg_risk,
                  sum(target_15_hit_before_sl='YES') yes_n,
                  sum(target_15_hit_before_sl='NO' AND hit_sl_later='YES') stop_n,
                  sum(target_15_hit_before_sl='AMBIGUOUS') ambiguous_n,
                  sum(outcome='RUNNING') running_n,sum(outcome='EXPIRED_1520') expired_n
                  FROM radar_tier_signals WHERE date=? AND tier=?''',(day,tier)).fetchone()
                d=dict(x);d['tier']=tier
                for k in ('yes_n','stop_n','ambiguous_n'):d[k+'_pct']=round(100*(d[k] or 0)/d['n'],2) if d['n'] else None
                d['max_risk_ratio']=d['avg_max']/d['avg_risk'] if d['avg_risk'] else None
                summary.append(d)
        return dict(rows=rows,summary=summary,caption=CAPTION)

    def validate_range(self,start,end):
        a,b=date.fromisoformat(start),date.fromisoformat(end)
        if b<a or (b-a).days+1>self.cfg.get('export_max_days',90):raise ValueError('Export range must be ascending and at most 90 days')

    def export_rows(self,table,start,end,timeframe=None):
        if table not in ('ema_proximity_history','radar_tier_signals'):raise ValueError('Unsupported table')
        self.validate_range(start,end)
        with connect_ro(self.path) as c:
            q='SELECT * FROM '+table+' WHERE date BETWEEN ? AND ?';params=[start,end]
            if timeframe:q+=' AND timeframe=?';params.append(timeframe)
            cur=c.execute(q+' ORDER BY date,time_ist'+(',timeframe' if table=='ema_proximity_history' else ',seq'),params)
            yield [d[0] for d in cur.description]
            while True:
                rows=cur.fetchmany(256)
                if not rows:break
                for r in rows:yield list(r)

    def export_csv(self,table,start,end):
        for row in self.export_rows(table,start,end):
            s=io.StringIO(newline='');csv.writer(s).writerow(row);yield s.getvalue()

    def export_xlsx(self,table,start,end):
        """Write-only sheets spooled to disk; never hold a date range in memory."""
        import tempfile
        from openpyxl import Workbook
        self.validate_range(start,end)
        wb=Workbook(write_only=True)
        for tf in ('1m','15m','1H') if table=='ema_proximity_history' else (None,):
            ws=wb.create_sheet(tf or 'Tier Signals')
            for row in self.export_rows(table,start,end,tf):ws.append(row)
        f=tempfile.TemporaryFile();wb.save(f);f.seek(0)
        return f


def market_coverage(market_db,day,now=None,refresh_minutes=3):
    now=time.time() if now is None else now;start=stamp(day,'09:15');end=stamp(day,'15:30')
    elapsed=min(end,max(start,int(now)//60*60));out={}
    with connect_ro(market_db) as c:
        for sec,label in (('13','spot'),('FUT','fut'),('FUT2','fut2')):
            times=[r[0] for r in c.execute('SELECT ts FROM candles_1m WHERE sec_id=? AND ts>=? AND ts<?',(sec,start,end))]
            out[label]=coverage(times,start,end)
            out[label]['elapsed']=coverage(times,start,elapsed)
        times=[r[0] for r in c.execute('SELECT DISTINCT ts FROM chain_snap WHERE ts>=? AND ts<?',(start,end))]
        out['chain_legacy']=coverage(times,start,end,int(refresh_minutes*60))
        out['chain_legacy']['raw_refresh_count']=len(times)
        out['chain_legacy']['elapsed']=coverage(times,start,elapsed,int(refresh_minutes*60))
    return out


class MarketRecorder:
    """Worker owns network, DB and calculations. Producers only enqueue; never wait."""
    def __init__(self,backend,store=None):
        self.backend=backend;self.cfg=backend.cfg['recording_v193']
        self.store=store or RecordingStore(cfg=self.cfg)
        self.queue=queue.Queue(maxsize=self.cfg.get('queue_size',256))
        self.stop_event=threading.Event();self.thread=None;self.dropped=0
        self.health={'status':'STARTING','forward_only':True};self.today_history=[]
        self.radars={'rows':[],'summary':[],'caption':CAPTION}
        self.started_at=time.time();self.last_cycle_minute=None;self.last_cycle_at=None;self.spot=None;self.quote_backoff=0;self.unresolved_contracts=[]
        day=datetime.now(IST).date().isoformat()
        self.today_history=self.store.safe(self.store.reload,day) or []
        if self.today_history:self.store.safe(self.store.persist_ema,self.today_history)

    def enqueue(self,kind,*args):
        try:self.queue.put_nowait((kind,args));return True
        except queue.Full:
            self.dropped+=1;log.warning('8510 recording queue full; observation dropped');return False

    def start(self):
        if self.thread:return
        self.thread=threading.Thread(target=self.run,name='8510-forward-recorder',daemon=True);self.thread.start()

    def stop(self):self.stop_event.set()

    def run(self):
        while not self.stop_event.is_set():
            try:
                kind,args=self.queue.get(timeout=1)
                if kind=='chain':self.store.safe(self.store.record_chain,*args)
                elif kind=='signal':self.store.safe(self.record_signal,*args)
                elif kind=='radar':self.store.safe(self.record_radar,*args)
                self.queue.task_done()
            except queue.Empty:
                pass  # Timed wake-up, not a swallowed writer failure.
            except Exception:
                log.exception('8510 recorder event failed open')
            minute=int(time.time())//60
            if minute!=self.last_cycle_minute:
                self.last_cycle_minute=minute
                self.store.safe(self.cycle,time.time())

    def record_signal(self,contract,now):
        identity=next((v for v in self.backend.inst_mgr.nifty_options.values()
                       if str(v['security_id'])==str(contract.get('security_id'))),contract)
        self.store.track(identity,now)
        if self.cfg.get('contract_tick_enabled'):self.store.safe(self.quotes,[identity])

    def record_radar(self,r,spot,now):
        if not self.cfg.get('radar_tier_track'):return
        tier=r.get('tier','')
        if not tier.startswith('TIER_') or not r.get('micro_sl_price'):return
        # Live telemetry only; reject cached prior-session or stale-forming observations.
        observed=r.get('bar_ts')
        if not observed or abs(now-observed)>self.cfg['freshness_seconds']:return
        side='CE' if r.get('ema_4',0)>=r.get('ema_8',0) else 'PE'
        dt=datetime.fromtimestamp(now,IST)
        name=r.get('action_recommendation') or tier
        # One condition occurrence per tier/direction/minute, not every fluctuating live price.
        # The requested full unique key remains in storage; this also survives mid-minute restart.
        with self.store.db() as c:
            exists=c.execute('SELECT 1 FROM radar_tier_signals WHERE date=? AND time_ist=? AND tier=? AND direction=? LIMIT 1',
                (dt.date().isoformat(),dt.strftime('%H:%M'),int(tier.split('_')[1]),side)).fetchone()
        if exists:return
        contract=self.atm_contract(spot,side,dt.date().isoformat())
        premium=None
        if contract:
            self.store.track(contract,now)
            with self.store.db() as c:
                q=c.execute('SELECT ltp,observed_at FROM contract_tick WHERE security_id=? ORDER BY ts DESC LIMIT 1',(str(contract['security_id']),)).fetchone()
                if q and 0<=now-q['observed_at']<=self.cfg['premium_freshness_seconds']:premium=q['ltp']
        fired=self.store.fire(dict(tier=int(tier.split('_')[1]),signal_name=r.get('action_recommendation') or tier,
           direction=side,signal_price=r['close_price'],nifty_spot_at_signal=spot,micro_sl_price=r['micro_sl_price'],
           contract_security_id=contract.get('security_id') if contract else None,
           contract_symbol=contract.get('symbol') if contract else None,premium_at_signal=premium),now)
        if fired and contract and self.cfg.get('contract_tick_enabled'):self.store.safe(self.quotes,[contract])

    def atm_contract(self,spot,side,day,strike=None):
        m=self.backend.inst_mgr
        expiry=m.get_nearest_expiry(day)
        if expiry<day:return None
        if strike is None:strike=round(spot/self.cfg['strike_step'])*self.cfg['strike_step']
        return m.resolve_option(expiry,strike,side)

    def collect_contracts(self,now,spot):
        day=datetime.fromtimestamp(now,IST).date().isoformat()
        # Read-only position scan includes existing open positions on restart and exit tails.
        identities={str(v['security_id']):v for v in self.backend.inst_mgr.nifty_options.values()}
        self.unresolved_contracts=[]
        with connect_ro(self.backend.ledger_db) as c:
            for p in c.execute("SELECT security_id,status,entry_time,exit_time FROM positions WHERE status='OPEN' OR exit_time>=?",(day,)):
                sid=str(p['security_id']);identity=identities.get(sid)
                if not identity:
                    self.unresolved_contracts.append(sid);continue
                try:
                    et=datetime.fromisoformat(str(p['exit_time']).replace(' IST','')).replace(tzinfo=IST).timestamp() if p['exit_time'] else None
                except ValueError:et=None
                if p['status']=='OPEN' and now>stamp(day,'15:30'):continue
                if p['status']=='OPEN' or et and 0<=now-et<=self.cfg['contract_tick_minutes_after_exit']*60:
                    self.store.track(identity,now,min(now+60,stamp(day,'15:30')) if p['status']=='OPEN' else et+self.cfg['contract_tick_minutes_after_exit']*60)
        with connect_ro(self.backend.ledger_db) as c:
            if c.execute("SELECT 1 FROM sqlite_master WHERE name='signal_observations'").fetchone():
                for row in c.execute('SELECT DISTINCT security_id FROM signal_observations WHERE session_date=?',(day,)):
                    identity=identities.get(str(row[0]))
                    if identity and now<=stamp(day,'15:20'):self.store.track(identity,now)
        if spot:
            atm=round(spot/self.cfg['strike_step'])*self.cfg['strike_step']
            for offset in (-self.cfg['track_atm_plus_minus'],0,self.cfg['track_atm_plus_minus']):
                for side in ('CE','PE'):
                    identity=self.atm_contract(spot,side,day,atm+offset)
                    if identity:self.store.track(identity,now)
        with self.store.db() as c:
            return [json.loads(r[0]) for r in c.execute('SELECT identity_json FROM tracked_contract WHERE until_ts>=? AND start_ts<=?',(now,now))]

    def quotes(self,contracts):
        if not contracts or not self.backend.broker.is_configured() or time.time()<self.quote_backoff:return
        with self.store.db() as c:
            seen={r[0] for r in c.execute('SELECT security_id FROM contract_tick WHERE ts=?',(int(time.time())//60*60,))}
        contracts=[c for c in contracts if str(c.get('security_id','')).isdigit() and str(c['security_id']) not in seen]
        if not contracts:return
        import httpx
        # Read-only endpoint; independent worker timeout/backoff never touches execution state.
        with httpx.Client(timeout=self.cfg['quote_timeout_seconds']) as client:
            response=client.post(self.backend.broker.base_url+'/marketfeed/quote',
                headers=self.backend.broker._headers(),json={'NSE_FNO':[int(c['security_id']) for c in contracts]})
            if response.status_code==429:
                self.quote_backoff=time.time()+self.cfg['quote_backoff_seconds'];response.raise_for_status()
            response.raise_for_status();payload=response.json().get('data',{}).get('NSE_FNO',{})
            received=time.time()
            for contract in contracts:
                quote=payload.get(str(contract['security_id']))
                if quote:self.store.record_tick(contract,quote,received,self.spot)

    def persist_closed(self,now,spot,status):
        day=datetime.fromtimestamp(now,IST).date().isoformat();closed=int(now)//60*60-60
        # Skip pre-start history: recording begins at the first forward close after startup.
        if closed+60<self.started_at:return
        rows=[]
        from analyst.ema_proximity_engine import EmaProximityEngine
        engine=EmaProximityEngine(db_path=Path(self.backend.market_db), replay_dir=self.store.csv_dir/'_no_replay_substitution')
        for tf,duration in (('1m',60),('15m',900),('1H',3600)):
            data=engine.compute_matrix(date_str=day,timeframe=tf,live_spot=None)
            candidates=[r for r in data.get('rows',[]) if r['ts']+duration<=closed+60 and r['date_iso']==day]
            r=max(candidates,key=lambda r:r['ts']) if candidates else None
            stale=not r or (tf=='1m' and r['ts']!=closed)
            if r:
                with connect_ro(self.backend.market_db) as c:
                    count=c.execute("SELECT count(*) FROM candles_1m WHERE sec_id='13' AND ts>=? AND ts<?",(r['ts'],r['ts']+duration)).fetchone()[0]
                local_bar=datetime.fromtimestamp(r['ts'],IST)
                aligned=(local_bar.minute % (duration//60)==0)
                stale=stale or count!=duration//60 or not aligned
            if r and r['ts']+duration<stamp(day,'09:15'):r=None;stale=True
            ma4=r['ema_4'] if r else None;ema8=r['ema_8'] if r else None;cp=r['close_price'] if r else None
            state=r.get('proximity_state','') if r else ''
            rows.append(dict(zip(EMA_COLUMNS,[day,datetime.fromtimestamp(closed+60,IST).strftime('%H:%M:%S'),tf,spot,cp,ma4,ema8,
               round(ma4-ema8,4) if r else None,round((ma4-ema8)/r['atr_14'],4) if r else None,
               round(cp-ema8,4) if r else None,round(cp-ma4,4) if r else None,
               'TOUCHING' if 'TOUCH' in state or 'TIER_3' in state else 'NEAR' if 'TIER_1' in state else 'FAR' if r else None,
               'CROSS_UP' if r and 'UPSIDE_CROSS' in r['crossover_state'] else 'CROSS_DOWN' if r and 'DOWNSIDE_CROSS' in r['crossover_state'] else 'NO_CROSS',
               'SUPPORT' if r and ('SUPPORT' in r['sr_interaction'] or 'BOUNCE' in r['sr_interaction']) else 'RESISTANCE' if r and ('RESISTANCE' in r['sr_interaction'] or 'REJECT' in r['sr_interaction']) else 'NEUTRAL',
               r.get('flips_last_12_bars') if r else None,'BULLISH' if r and ma4>=ema8 else 'BEARISH' if r else None,
               'Stage 2' if r and 'STAGE_2' in r['sr_interaction'] else 'Stage 3' if r and 'STAGE_3' in r['sr_interaction'] else 'Crossover' if r and r['crossover_state'] in ('8_EMA_REVERSAL_UPSIDE_CROSS','8_EMA_REVERSAL_DOWNSIDE_CROSS') else 'None',
               'STALE' if stale else status])))
        self.store.persist_ema(rows)
        self.today_history=self.store.reload(day)

    def cycle(self,now):
        self.last_cycle_at=now
        dt=datetime.fromtimestamp(now,IST);day=dt.date().isoformat();hhmm=dt.strftime('%H:%M')
        # Freeze prior-session rows too after a restart, without retrospective outcomes.
        with self.store.db() as c:old=[r[0] for r in c.execute("SELECT DISTINCT date FROM radar_tier_signals WHERE date<? AND outcome='RUNNING'",(day,))]
        for previous in old:self.store.freeze(previous)
        if dt.weekday()<5 and '09:15'<=hhmm<='15:30':
            with connect_ro(self.backend.market_db) as c:
                row=c.execute("SELECT ts,c FROM candles_1m WHERE sec_id='13' AND ts<? ORDER BY ts DESC LIMIT 1",(int(now)//60*60,)).fetchone()
            fresh=row and 0<=now-row['ts']<=self.cfg['freshness_seconds']
            cached=self.backend.broker._ltp_cache.get('13') if hasattr(self.backend.broker,'_ltp_cache') else None
            cached_at=getattr(self.backend.broker,'_cache_timestamps',{}).get('13',0)
            cached_fresh=cached and 0<=now-cached_at<=self.cfg['freshness_seconds']
            self.spot=float(cached) if cached_fresh else row['c'] if fresh else None
            # Quote recording continues for held/previously signalled contracts even with stale spot.
            contracts=self.collect_contracts(now,self.spot)
            if self.cfg.get('contract_tick_enabled'):self.store.safe(self.quotes,contracts)
            if self.cfg.get('ema_proximity_persist') and hhmm>'09:15':
                self.store.safe(self.persist_closed,now,self.spot,'LIVE' if self.backend.paper_execution_enabled and not getattr(self.backend,'kill_switch_active',False) else 'PAUSED')
            with connect_ro(self.backend.market_db) as c:
                bars=c.execute("SELECT ts,h,l,c FROM candles_1m WHERE sec_id='13' AND ts>=? AND ts<? ORDER BY ts",(max(int(self.started_at)//60*60,stamp(day,'09:15')),int(now)//60*60)).fetchall()
            for bar in bars:self.store.safe(self.store.update_bar,dict(bar))
        if dt.weekday()<5 and hhmm>'15:30':
            contracts=self.collect_contracts(now,None)
            if self.cfg.get('contract_tick_enabled'):self.store.safe(self.quotes,contracts)
        if hhmm>='15:20':self.store.freeze(day)
        self.radars=self.store.radar(day)
        health=market_coverage(self.backend.market_db,day,now,self.cfg['chain_refresh_minutes'])
        with self.store.db() as c:
            ticks=c.execute('SELECT security_id,ts FROM contract_tick WHERE session_date=?',(day,)).fetchall()
            tracked=c.execute('SELECT security_id,start_ts,until_ts FROM tracked_contract WHERE start_ts<? AND until_ts>=?',(stamp(day,'23:59'),stamp(day,'09:15'))).fetchall()
            held=[]
            for r in tracked:
                start=max(r['start_ts'],stamp(day,'09:15'));end=min(int(now)//60*60,int(r['until_ts'])//60*60+60)
                held.append(dict(security_id=r['security_id'],**coverage([q['ts'] for q in ticks if q['security_id']==r['security_id']],start,max(start,end))))
            chain=self.store.chain_ledger(day,now)
        health.update(chain=chain,contracts=held,tracked=len(held),contract_ticks=len(ticks),unresolved_contracts=self.unresolved_contracts,
           contract_missing_minutes=sum((r['expected_snapshots']-r['n_snapshots']) for r in held),
           status='RECORDING' if '09:15'<=hhmm<'15:30' else 'OUTSIDE_SESSION',forward_only=True,
           last_write=self.store.last_write,last_error=self.store.last_error,queue_dropped=self.dropped,
           saved_rows_today=sum(r['date']==day for r in self.today_history),last_saved=max((r['time_ist'] for r in self.today_history if r['date']==day),default=None),
           session_date=day,source='8510 recording database; legacy coverage shown separately',
           coverage_warn_pct=self.cfg['coverage_warn_pct'],coverage_alert_pct=self.cfg['coverage_alert_pct'])
        self.health=health
