"""Durable broker reconciliation and opt-in protected-entry gate.

Signal stops are index points; live Super Order stops/targets are separate explicitly
configured option-premium prices. No unprotected automatic order fallback exists.
"""
import json,math,os,time,sqlite3,hashlib
from datetime import datetime
from zoneinfo import ZoneInfo
IST=ZoneInfo('Asia/Kolkata')

class Execution:
    def __init__(self,engine,cfg):
        self.e=engine;self.cfg=cfg;self.armed=False;self.arm_day=None;self.status='Automatic live OFF';self.last_sync=0;self.book=[];self.supers=[];self.positions=[];self.funds=None;self.audit=[]
        with self.e.connect() as c:c.execute('CREATE TABLE IF NOT EXISTS auto_intents(id TEXT PRIMARY KEY,day TEXT,payload TEXT,state TEXT,response TEXT)')
    def intents(self):
        with self.e.connect() as c:
            return [dict(id=r[0],day=r[1],payload=json.loads(r[2]),state=r[3],response=json.loads(r[4]) if r[4] else None) for r in c.execute('SELECT * FROM auto_intents ORDER BY rowid DESC')]
    def sync(self):
        try:
            book=self.e.broker.request('/orders');supers=self.e.broker.request('/super/orders');positions=self.e.broker.request('/positions');funds=self.e.broker.request('/fundlimit')
            if not all(isinstance(x,list) for x in (book,supers,positions)) or not isinstance(funds,dict):raise ValueError('Unexpected account response shape')
            self.book,self.supers,self.positions,self.funds=book,supers,positions,funds
            self.audit=[]
            with self.e.connect() as c:
                for cid,state in c.execute('SELECT id,state FROM intents').fetchall():
                    found=[x for x in book if x.get('correlationId')==cid]
                    if len(found)==1:
                        observed=found[0];c.execute('UPDATE intents SET state=?,response=? WHERE id=?',(observed.get('orderStatus','UNKNOWN_RECONCILE'),json.dumps(observed),cid))
                    elif state in ('SUBMITTING','UNKNOWN_RECONCILE','BROKER_RESPONSE'):
                        self.audit.append('Unreconciled manual intent '+cid)
                for intent in self.intents():
                    matches=[x for x in supers if x.get('correlationId')==intent['id']]
                    if len(matches)!=1:
                        if intent['state'] not in ('REJECTED','CANCELLED','CLOSED'):self.audit.append('Unreconciled intent '+intent['id'])
                        continue
                    order=matches[0];qty=int(order.get('quantity',0));filled=int(order.get('filledQty',0));state=order.get('orderStatus','UNKNOWN')
                    if not 0<=filled<=qty or qty!=intent['payload']['quantity']:self.audit.append('Invalid broker quantities '+intent['id'])
                    if state=='TRADED' and filled!=qty:self.audit.append('TRADED quantity mismatch '+intent['id'])
                    if 0<filled<qty:self.audit.append('PARTIAL FILL: block new entries; reconcile remaining quantity in Dhan')
                    if filled and state!='CLOSED':
                        legs=order.get('legDetails',[])
                        stops=[x for x in legs if x.get('legName')=='STOP_LOSS_LEG' and x.get('orderStatus') in ('PENDING','TRANSIT','TRIGGERED','PART_TRADED')]
                        if not stops:self.audit.append('PROTECTION NOT VERIFIED for '+intent['id'])
                        else:
                            covered=stops[0].get('totalQuantity',stops[0].get('totalQuatity'))
                            if covered is None or int(covered)<filled:self.audit.append('STOP COVERAGE NOT VERIFIED for filled quantity')
                    c.execute('UPDATE auto_intents SET state=?,response=? WHERE id=?',(state,json.dumps(order),intent['id']))
            self.last_sync=time.time();self.status='Broker synchronized; acknowledgments are not fills'
            if self.audit:self.armed=False;self.status='New entries halted: '+'; '.join(self.audit)
        except Exception as ex:
            self.armed=False;self.status='Broker reconciliation failed ('+type(ex).__name__+'); entries halted'
            raise
    def blockers(self,require_flat=True):
        out=[];now=datetime.now(IST)
        if os.getenv('ENABLE_DHAN_AUTO_ORDERS')!='1':out.append('ENABLE_DHAN_AUTO_ORDERS is not1')
        if self.cfg.get('automatic_live_enabled') is not True:out.append('Automatic live disabled in server configuration')
        for key in ('option_stop_points','option_target_points'):
            value=self.cfg.get(key)
            if not isinstance(value,(int,float)) or isinstance(value,bool) or not math.isfinite(value) or value<=0:out.append(key+' not explicitly configured')
        if time.time()-self.last_sync>self.cfg['reconcile_seconds']*2:out.append('Broker reconciliation stale')
        out.extend(self.audit)
        with self.e.connect() as c:
            if c.execute("SELECT count(*) FROM intents WHERE state IN ('SUBMITTING','UNKNOWN_RECONCILE','BROKER_RESPONSE')").fetchone()[0]:out.append('Manual order intent needs reconciliation')
        balance=None if self.funds is None else self.funds.get('availabelBalance',self.funds.get('availableBalance'))
        if not isinstance(balance,(int,float)) or not math.isfinite(balance) or balance<=0:out.append('Available cash not verified')
        if any(o.get('orderStatus') in ('TRANSIT','PENDING','PART_TRADED','TRIGGERED') for o in self.book):out.append('Existing pending broker order')
        if require_flat and any(int(p.get('netQty',0))!=0 for p in self.positions):out.append('Existing broker position: no new entry')
        if any(i['state'] not in ('REJECTED','CANCELLED','CLOSED') for i in self.intents()):out.append('Pending/open/unknown automatic intent')
        day=now.date().isoformat();today=[i for i in self.intents() if i['day']==day]
        if len(today)>=self.cfg['max_daily_live_trades']:out.append('Daily attempted-entry limit reached')
        pnl=0.
        for p in self.positions:
            for key in ('realizedProfit','unrealizedProfit'):
                if key not in p:out.append('Broker P&L field missing');continue
                value=float(p[key])
                if not math.isfinite(value):out.append('Broker P&L invalid')
                else:pnl+=value
        if pnl<=-self.cfg['max_daily_live_loss_inr']:out.append('Daily gross loss limit reached')
        return out
    def arm(self,confirmed):
        if confirmed is not True:raise ValueError('Explicit live confirmation required')
        self.sync();blocks=self.blockers()
        if blocks:raise ValueError('; '.join(blocks))
        self.armed=True;self.arm_day=datetime.now(IST).date().isoformat();self.status='ARMED for current session only; option-premium bracket policy'
    def submit(self,signal,contract,quote):
        if not self.armed or self.arm_day!=datetime.now(IST).date().isoformat():return
        blocks=self.blockers()
        if blocks:self.armed=False;self.status='Blocked: '+'; '.join(blocks);return
        quantity=contract['lotSize'] # one metadata-defined lot only
        balance=self.funds.get('availabelBalance',self.funds.get('availableBalance',0))
        if quote*quantity>balance:raise ValueError('Available cash below sampled option premium requirement')
        stop=round(quote-self.cfg['option_stop_points'],2);target=round(quote+self.cfg['option_target_points'],2)
        if stop<=0:raise ValueError('Option stop price would be non-positive')
        # Tick validation is mandatory, not an assumed0.05.
        tick=contract.get('tickSize')
        if not tick or abs(stop/tick-round(stop/tick))>1e-6 or abs(target/tick-round(target/tick))>1e-6:
            raise ValueError('Option bracket is not aligned to instrument tick size')
        cid='auto-'+hashlib.sha256(signal['event_id'].encode()).hexdigest()[:24]
        payload=dict(dhanClientId=os.getenv('DHAN_CLIENT_ID'),correlationId=cid,transactionType='BUY',exchangeSegment='NSE_FNO',
                     productType='INTRADAY',orderType='MARKET',securityId=contract['securityId'],quantity=quantity,
                     price=0,targetPrice=target,stopLossPrice=stop,trailingJump=0)
        with self.e.connect() as c:
            try:c.execute('INSERT INTO auto_intents VALUES(?,?,?,?,?)',(cid,datetime.now(IST).date().isoformat(),json.dumps(payload),'SUBMITTING',None))
            except sqlite3.IntegrityError:self.status='Signal already submitted; no retry';return
        try:
            response=self.e.broker.request('/super/orders',payload)
        except Exception:
            with self.e.connect() as c:c.execute('UPDATE auto_intents SET state=? WHERE id=?',('UNKNOWN_RECONCILE',cid))
            self.armed=False;self.status='Unknown order outcome; no retry; reconcile in Dhan';raise
        with self.e.connect() as c:c.execute('UPDATE auto_intents SET state=?,response=? WHERE id=?',('ACKNOWLEDGED_NOT_FILLED',json.dumps(response),cid))
        self.status='Broker acknowledged protected order; awaiting fills and leg verification'
    def snapshot(self):
        return dict(armed=self.armed,arm_day=self.arm_day,status=self.status,last_sync=self.last_sync,
                    blockers=self.blockers(),intents=self.intents(),orders=self.book,positions=self.positions,
                    exit_basis='OPTION_PREMIUM_SUPER_ORDER: independent of NIFTY signal stop; requires explicit premium policy',
                    option_stop_points=self.cfg['option_stop_points'],option_target_points=self.cfg['option_target_points'])
