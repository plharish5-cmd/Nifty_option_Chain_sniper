"""Read-only existing candle source plus isolated Dhan backfill/tick cache."""
import json,math,os,sqlite3,struct,threading,time
from datetime import datetime,date,timedelta
from urllib.parse import urlencode
from zoneinfo import ZoneInfo
IST=ZoneInfo('Asia/Kolkata')

class CandleStore:
    def __init__(self,root):
        self.source=root/'ict_smc_app/backend/data/ict_smc.sqlite3'
        self.cache=root/'data/terminal8500/market.sqlite3';self.cache.parent.mkdir(parents=True,exist_ok=True)
        with sqlite3.connect(self.cache) as c:
            c.execute('CREATE TABLE IF NOT EXISTS minutes(sec_id TEXT,ts INTEGER,o REAL,h REAL,l REAL,c REAL,v REAL,source TEXT,PRIMARY KEY(sec_id,ts))')
    def read(self,sec_id='13',until=None,days=120):
        end=until if until is not None else int(time.time());start=end-days*86400
        merged={};conflicts=[]
        for path,table in ((self.source,'candles_1m'),(self.cache,'minutes')):
            if not path.exists():continue
            with sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True) as c:
                rows=c.execute('SELECT ts,o,h,l,c,v FROM '+table+' WHERE sec_id=? AND ts>=? AND ts<? ORDER BY ts',(sec_id,start,end)).fetchall()
            for row in rows:
                ts,op,hi,lo,cl,v=row
                if not all(math.isfinite(float(x)) and float(x)>0 for x in (op,hi,lo,cl)) or hi<max(op,cl) or lo>min(op,cl):continue
                if ts in merged and any(abs(float(a)-float(b))>0.001 for a,b in zip(merged[ts][1:5],row[1:5])):
                    conflicts.append(ts)
                merged[ts]=row
        # Conflicting observations are excluded from both replay and live gates.
        for ts in conflicts:merged.pop(ts,None)
        return [merged[k] for k in sorted(merged)],dict(source=str(self.source),cache=str(self.cache),conflicting_minutes=len(set(conflicts)))
    def audit(self):
        rows,quality=self.read(days=500)
        counts={}
        for r in rows:
            dt=datetime.fromtimestamp(r[0],IST)
            if '09:15'<=dt.strftime('%H:%M')<'15:30':counts[dt.date().isoformat()]=counts.get(dt.date().isoformat(),0)+1
        return dict(**quality,sessions=counts,latest_minute=rows[-1][0] if rows else None,total_minutes=len(rows))
    def backfill(self,broker,day):
        day=date.fromisoformat(day)
        if day>datetime.now(IST).date():raise ValueError('Future date not allowed')
        payload=dict(securityId='13',exchangeSegment='IDX_I',instrument='INDEX',interval='1',oi=False,
                     fromDate=day.isoformat()+' 09:15:00',toDate=day.isoformat()+' 15:30:00')
        result=broker.request('/charts/intraday',payload)
        keys=['timestamp','open','high','low','close','volume']
        if not all(isinstance(result.get(k),list) for k in keys) or len({len(result[k]) for k in keys})!=1:
            raise ValueError('Dhan history returned invalid candle arrays')
        count=0
        with sqlite3.connect(self.cache) as c:
            for ts,op,hi,lo,cl,v in zip(*(result[k] for k in keys)):
                dt=datetime.fromtimestamp(int(ts),IST)
                if dt.date()!=day or not '09:15'<=dt.strftime('%H:%M')<'15:30' or int(ts)+60>time.time():continue
                if not all(math.isfinite(float(x)) and float(x)>0 for x in (op,hi,lo,cl)) or hi<max(op,cl) or lo>min(op,cl):continue
                c.execute('INSERT OR REPLACE INTO minutes VALUES(?,?,?,?,?,?,?,?)',('13',int(ts),op,hi,lo,cl,v,'dhan_intraday'))
                count+=1
        return dict(day=day.isoformat(),cached_minutes=count,source='Dhan API, isolated8500 cache')


def decode_packets(raw):
    out=[];offset=0
    while offset+8<=len(raw):
        code,length,segment,sid=struct.unpack_from('<BHBI',raw,offset)
        if length<8 or offset+length>len(raw):raise ValueError('Truncated/invalid Dhan packet length')
        p=raw[offset:offset+length]
        if code in (1,2,4,8):
            minimum=12 if code==1 else 16 if code==2 else 18
            if len(p)<minimum:raise ValueError('Short price packet')
            price=struct.unpack_from('<f',p,8)[0]
            stamp=None if code==1 else struct.unpack_from('<I',p,12 if code==2 else 14)[0]
            if math.isfinite(price) and price>0:
                item=dict(segment=segment,securityId=str(sid),ltp=round(price,4),exchange_ts=stamp,received_at=time.time())
                if code==8 and len(p)>=162:
                    depth=[]
                    for level in range(5):
                        bq,aq,bo,ao,bid,ask=struct.unpack_from('<IIHHff',p,62+level*20)
                        depth.append(dict(bidQuantity=bq,askQuantity=aq,bid=bid if math.isfinite(bid) and bid>0 else None,ask=ask if math.isfinite(ask) and ask>0 else None))
                    item.update(depth=depth,bid=depth[0]['bid'],ask=depth[0]['ask'])
                out.append(item)
        elif code==50:raise ValueError('Broker disconnected market feed')
        offset+=length
    if offset!=len(raw):raise ValueError('Trailing incomplete Dhan packet')
    return out

class Feed:
    def __init__(self,cfg,on_tick):
        self.cfg=cfg;self.on_tick=on_tick;self.lock=threading.RLock();self.quotes={};self.instruments={};self.status='Not connected';self.quit=threading.Event();self.revision=0;self.ws=None
    def select(self,label,segment,sid):
        with self.lock:
            value=(segment,str(sid))
            if self.instruments.get(label)!=value:self.instruments[label]=value;self.revision+=1
    def snapshot(self):
        with self.lock:
            data={}
            for label,(seg,sid) in self.instruments.items():
                q=self.quotes.get((seg,sid));item=dict(q) if q else dict(securityId=sid,ltp=None)
                item['label']=label;item['exchangeSegment']=seg
                item['age_seconds']=time.time()-q['received_at'] if q else None
                item['trade_age_seconds']=time.time()-q['exchange_ts'] if q and q.get('exchange_ts') else None
                item['fresh']=bool(q and item['age_seconds']<=self.cfg['quote_max_age_seconds'] and (not q.get('exchange_ts') or 0<=item['trade_age_seconds']<=self.cfg['quote_max_age_seconds']))
                data[label]=item
            return dict(connection=self.status,quotes=data)
    def ingest(self,raw):
        for q in decode_packets(raw):
            seg='IDX_I' if q['segment']==0 else 'NSE_FNO' if q['segment']==2 else None
            if not seg:continue
            with self.lock:self.quotes[(seg,q['securityId'])]=q
            self.on_tick(seg,q)
    def run(self):
        import websocket
        while not self.quit.is_set():
            token=os.getenv('DHAN_ACCESS_TOKEN');client=os.getenv('DHAN_CLIENT_ID')
            if not token or not client:
                self.status='Credentials missing; no simulated price fallback';self.quit.wait(self.cfg['feed_retry_seconds']);continue
            try:
                url='wss://api-feed.dhan.co?'+urlencode(dict(version=2,token=token,clientId=client,authType=2))
                self.ws=websocket.create_connection(url,timeout=self.cfg['feed_socket_timeout_seconds'])
                with self.lock:
                    rev=self.revision;items=list(set(self.instruments.values()))
                self.ws.send(json.dumps(dict(RequestCode=21,InstrumentCount=len(items),InstrumentList=[dict(ExchangeSegment=s,SecurityId=i) for s,i in items])))
                self.status='CONNECTED — event-driven ticks, not synthetic1sec changes'
                while not self.quit.is_set():
                    with self.lock:
                        if rev!=self.revision:break
                    try:raw=self.ws.recv()
                    except websocket.WebSocketTimeoutException:continue
                    if not raw:raise ValueError('Feed closed')
                    if isinstance(raw,bytes):self.ingest(raw)
            except Exception as ex:
                # Never log exception text: connection URLs contain credentials.
                self.status='DISCONNECTED ('+type(ex).__name__+'); reconnecting; stale entries blocked'
            finally:
                if self.ws:
                    try:self.ws.close()
                    except Exception:self.status='Socket close failed; retrying'
                    self.ws=None
            self.quit.wait(self.cfg['feed_retry_seconds'])
    def stop(self):
        self.quit.set()
        if self.ws:
            try:self.ws.close()
            except Exception:self.status='Feed shutdown error'
