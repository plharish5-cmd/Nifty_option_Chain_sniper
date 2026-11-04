"""Read-only8510 policy adapter. Reuses its selectors and tariff functions verbatim.
No writes to the8510 paper account, settings, trades or protected8600 modules.
"""
import json,math,sqlite3,sys,time,uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import pandas as pd
IST=ZoneInfo('Asia/Kolkata')

def clean(value):
    if isinstance(value,dict):return {str(k):clean(v) for k,v in value.items()}
    if isinstance(value,list):return [clean(x) for x in value]
    if hasattr(value,'item'):return clean(value.item())
    if isinstance(value,float) and not math.isfinite(value):return None
    return value

class Paperdesk:
    def __init__(self,engine,cfg):
        self.e=engine;self.cfg=cfg
        reference=Path(cfg.get('paperdesk_reference_root',engine.root))
        if str(reference) not in sys.path:sys.path.insert(0,str(reference))
        import paper_desk,paper_trading
        self.desk,self.pt=paper_desk,paper_trading
        self.directory=reference/'data/paper_trading'
        self.expiry=None;self.latest={'rows':[],'source':'Waiting for8510-compatible option chain','tradable':False}
        self.last_refresh=0
    def settings(self):
        f=self.directory/'desk.json';saved=json.loads(f.read_text()) if f.exists() else {}
        settings={**self.desk.DEFAULTS,**{k:v for k,v in saved.items() if k in self.desk.DEFAULTS}}
        settings['weights']={**self.desk.DEFAULTS['weights'],**(saved.get('weights') or {})}
        return settings
    def rates(self):
        f=self.directory/'paper_config.json';saved=json.loads(f.read_text()) if f.exists() else {}
        rates={**self.pt.DEFAULT_CONFIG,**saved}
        # Same legacy rate migration as8510, in memory only.
        if abs(rates['stt_sell_rate']-.001)<1e-9:rates['stt_sell_rate']=self.desk.STT_2026
        if abs(rates['ipft_rate']-.0000005)<1e-12:rates['ipft_rate']=self.desk.IPFT_RATE
        return rates
    def charges(self,entry,exit,qty):
        return self.pt.calculate_trade_charges(float(entry),float(exit),int(qty),side='BUY',cfg=self.rates())
    def calculation(self,entry,exit,qty):
        entry=float(entry);exit=float(exit)
        if type(qty) is not int or qty<=0 or not all(math.isfinite(x) and x>0 for x in (entry,exit)):raise ValueError('Positive premiums and integer quantity required')
        fees=self.charges(entry,exit,qty);gross=(exit-entry)*qty
        return dict(entryPremium=entry,exitPremium=exit,quantity=qty,premiumPoints=exit-entry,grossPnl=round(gross,2),
                    charges=fees,netPnl=round(gross-fees['total_charges'],2),source='CALCULATION ONLY — same8510 tariff; not an execution')
    def settle(self,t):
        t['grossPnl']=round((t['currentPremium']-t['entryPremium'])*t['quantity'],2)
        if t['status']=='CLOSED':
            fees=self.charges(t['entryPremium'],t['exitPremium'],t['quantity'])
            t.update(charges=fees,chargesTotal=fees['total_charges'],netPnl=round(t['grossPnl']-fees['total_charges'],2))
        else:t.update(charges=None,chargesTotal=None,netPnl=None)
        return t
    def refresh(self,contracts,force=False):
        if not force and time.time()-self.last_refresh<self.cfg['desk_chain_poll_seconds']:return self.latest
        self.last_refresh=time.time();now=self.desk.ist_now();settings=self.settings()
        expiries=sorted({x['expiry'] for x in contracts if not self.desk.expired(x['expiry'],now)})
        expiry=self.expiry if self.expiry in expiries else expiries[0] if expiries else None
        if not expiry:
            self.latest=dict(rows=[],source='No unexpired instrument-master expiry',tradable=False);return self.latest
        chain=self.desk.get_chain(expiry,now)
        if not chain:
            self.latest=dict(rows=[],expiry=expiry,source='No chain data; no invented picks',tradable=False);return self.latest
        actual_expiry=str(chain.get('expiry') or expiry)
        if actual_expiry not in expiries:
            self.latest=dict(rows=[],expiry=actual_expiry,source='Snapshot expiry not in active master; display blocked',tradable=False);return self.latest
        expiry=actual_expiry
        rows=chain['rows'];watch=[];picks=[]
        for side in ('CE','PE'):
            got=self.desk.bucket_rows(rows,chain['spot'],side,float(settings['bucket_min']),float(settings['bucket_max']),float(settings['bucket_aim']))
            for r in got.to_dict('records'):
                match=next((c for c in contracts if c['expiry']==expiry and c['strike']==r['strike'] and c['optionType']==side),None)
                if not match or str(r.get('sid','')).isdigit() and str(r['sid'])!=match['securityId']:continue
                watch.append({**r,**match,'premium':float(r['px']),'spread_pct':r.get('spread_pct')})
            ranked=self.desk.rank(rows,chain['spot'],side,settings)
            picks+=ranked.to_dict('records')
        #8510 dropdown:200-plus first, then distance; stable CE-before-PE ties.
        watch.sort(key=lambda r:(not r['plus'],r['dist']))
        at=chain['at'].replace(tzinfo=IST) if chain['at'].tzinfo is None else chain['at']
        self.latest=clean(dict(rows=watch,course_picks=picks,expiry=expiry,expiries=expiries,spot=chain['spot'],asof=at.isoformat(),
                age_seconds=max(0,time.time()-at.timestamp()),tradable=bool(chain.get('live')),
                source=chain.get('source'),settings={k:settings[k] for k in ('bucket_min','bucket_max','bucket_aim','style','itm_min','itm_max','round100')},
                policy='paper_desk.bucket_rows — identical8510 premium bucket; not nearest ATM',rates_source=str(self.directory/'paper_config.json')))
        return self.latest
    def choose(self,kind,contracts):
        state=self.latest
        if not state.get('tradable') or state.get('age_seconds',float('inf'))>self.cfg['desk_chain_max_age_seconds']:return None
        #Check actual wall time as well as snapshot age recorded at refresh.
        if time.time()-self.last_refresh>self.cfg['desk_chain_max_age_seconds']:return None
        ids={c['securityId']:c for c in contracts}
        return next((ids[r['securityId']] for r in state['rows'] if r['optionType']==kind and r['securityId'] in ids),None)
    def entry_price(self,q):
        return float(q['ask']) if q.get('ask') and q['ask']>0 else float(q['ltp'])
    def exit_price(self,q):
        return float(q['bid']) if q.get('bid') and q['bid']>0 else float(q['ltp'])
    def historical(self,t):
        """As-of snapshots only: choose bucket at entry, value same contract at exit."""
        result=dict(option_status='NO_RECORDED_PREMIUM',entryPremium=None,exitPremium=None,grossPnl=None,netPnl=None,chargesTotal=None)
        if not t.get('exit_ts'):return result
        db=self.e.root/'ict_smc_app/backend/data/ict_smc.sqlite3'
        if not db.exists():return result
        side='CE' if t['side']==1 else 'PE';s=self.settings();stamp=int(t['entry_ts']);day=datetime.fromtimestamp(stamp,IST).date().isoformat()
        with sqlite3.connect('file:'+str(db)+'?mode=ro',uri=True) as c:
            exps=[r[0] for r in c.execute('SELECT DISTINCT expiry FROM chain_snap WHERE ts>=? AND ts<=? AND expiry>=? ORDER BY expiry',(int(datetime.fromisoformat(day+'T00:00:00').replace(tzinfo=IST).timestamp()),stamp,day))]
            if not exps:return result
            expiry=exps[0]
            snap=c.execute('SELECT max(ts) FROM chain_snap WHERE expiry=? AND ts<=? AND ts>=?',(expiry,stamp,stamp-self.cfg['desk_snapshot_max_age_seconds'])).fetchone()[0]
            if snap is None:return result
            prefix='ce' if side=='CE' else 'pe'
            raw=c.execute(f'SELECT strike,{prefix}_ltp,{prefix}_bid,{prefix}_ask FROM chain_snap WHERE expiry=? AND ts=?',(expiry,snap)).fetchall()
            frame=pd.DataFrame([dict(strike=r[0],ltp=r[1],bid=r[2],ask=r[3],side=side) for r in raw])
            if frame.empty:return result
            for column in ('ltp','bid','ask'):frame[column]=pd.to_numeric(frame[column],errors='coerce')
            chosen=self.desk.bucket_rows(frame,t['entry'],side,s['bucket_min'],s['bucket_max'],s['bucket_aim'])
            if chosen.empty:return result
            r=chosen.iloc[0];strike=float(r.strike);entry=self.entry_price(r.to_dict())
            quote=c.execute(f'SELECT ts,{prefix}_ltp,{prefix}_bid,{prefix}_ask FROM chain_snap WHERE expiry=? AND strike=? AND ts<=? AND ts>=? ORDER BY ts DESC LIMIT 1',
                  (expiry,strike,int(t['exit_ts']),int(t['exit_ts'])-self.cfg['desk_snapshot_max_age_seconds'])).fetchone()
            if quote is None:return {**result,'strike':strike,'expiry':expiry,'optionType':side,'entryPremium':entry}
            if quote[1] is None or not math.isfinite(quote[1]) or quote[1]<=0:return result
            exit=self.exit_price(dict(ltp=quote[1],bid=quote[2],ask=quote[3]))
            qty=int(self.desk.LOT);calc=self.calculation(entry,exit,qty)
            return {**calc,'option_status':'RECORDED_CHAIN_REPLAY','strike':strike,'expiry':expiry,'optionType':side,
                    'symbol':self.desk.name(strike,side,expiry),'entry_snapshot_ts':int(snap),'exit_snapshot_ts':int(quote[0]),
                    'source':'Recorded chain bid/ask model replay; not an execution','quantity_basis':'8510 configured LOT_SIZE for historical replay; not a broker fill'}
    def ledger(self):
        rows=[]
        for path in sorted(self.e.data.glob('replay-*.json')):
            r=json.loads(path.read_text());day=r['day']
            for t in r['trades']:
                if t.get('entry_ts') is None:continue
                premium=self.historical(t);reason=t.get('reason','')
                rows.append(dict(Trade_ID='replay-'+day+'-'+str(t['setup_id'])+'-'+str(t['entry_ts']),Date=day,
                    Setup_Name=t['setup'],Setup_Type='1-Hour Higher Timeframe Touch' if t['setup_id']==4 else 'Trend Following' if t['setup_id']<2 else 'Reversal',
                    Direction='LONG' if t['side']==1 else 'SHORT',Result='TARGET HIT' if reason=='TARGET' else 'STOP LOSS HIT' if reason=='STOP' else reason,
                    Index_Points=t.get('points'),Gross_Pnl_INR=premium.get('grossPnl'),Net_Pnl_INR=premium.get('netPnl'),Exact_Charges_INR=premium.get('chargesTotal',premium.get('charges',{}).get('total_charges') if premium.get('charges') else None),
                    Lot_Qty=premium.get('quantity'),Entry_Premium=premium.get('entryPremium'),Exit_Premium=premium.get('exitPremium'),Strike=premium.get('strike'),Expiry=premium.get('expiry'),
                    Premium_Source=premium['option_status'],Source='Recorded replay',Entry_TS=t['entry_ts'],Exit_TS=t.get('exit_ts'),
                    Market_Gap_Pts=None,EMA_Aligned=None,EMA_Slope=None,ADX=None,R_Squared=None,MTF_Aligned=None,Vol_Mult=None,PCR_At_Entry=None))
        for t in self.e.paper():
            rows.append(dict(Trade_ID=t['id'],Date=t['day'],Setup_Name=t['setup'],Setup_Type='Manual' if t.get('manual') else '1-Hour Higher Timeframe Touch' if t.get('setup_id')==4 else 'Trend Following' if t.get('setup_id',2)<2 else 'Reversal',
                Direction='LONG' if t['side']==1 else 'SHORT',Result=t.get('reason',t['status']),Index_Points=(t['currSpot']-t['entrySpot'])*t['side'],
                Gross_Pnl_INR=t['grossPnl'],Net_Pnl_INR=t.get('netPnl'),Exact_Charges_INR=t.get('chargesTotal'),Lot_Qty=t['quantity'],
                Entry_Premium=t['entryPremium'],Exit_Premium=t.get('exitPremium'),Strike=t['strike'],Expiry=t['expiry'],Source='Live paper',Premium_Source='OBSERVED_PAPER_PREMIUM',
                Market_Gap_Pts=None,EMA_Aligned=None,EMA_Slope=None,ADX=None,R_Squared=None,MTF_Aligned=None,Vol_Mult=None,PCR_At_Entry=None))
        return clean(rows)
    def manual_order(self,body):
        contract=self.e.master.resolve(body);lots=body.get('lots',1)
        if type(lots) is not int or not 1<=lots<=self.cfg['manual_paper_max_lots']:raise ValueError('Invalid paper lot count')
        typ=body.get('orderType','MARKET')
        if typ not in ('MARKET','LIMIT'):raise ValueError('Paper order type must be MARKET or LIMIT')
        limit=float(body.get('limitPrice') or 0);sl=float(body.get('stopPoints') or 0);target=float(body.get('targetPoints') or 0)
        if not all(math.isfinite(x) and x>=0 for x in (limit,sl,target)) or typ=='LIMIT' and limit<=0:raise ValueError('Invalid paper limit/stop/target')
        order=dict(id='manual-'+uuid.uuid4().hex[:20],contract=contract,lots=lots,orderType=typ,limitPrice=limit,stopPoints=sl,targetPoints=target,
                   createdAt=datetime.now(IST).isoformat(),status='WAITING',fill=None)
        with self.e.connect() as c:
            c.execute('CREATE TABLE IF NOT EXISTS manual_paper_orders(id TEXT PRIMARY KEY,value TEXT)')
            c.execute('INSERT INTO manual_paper_orders VALUES(?,?)',(order['id'],json.dumps(order)))
        self.e.feed.select('manual_'+contract['securityId'],'NSE_FNO',contract['securityId'])
        self.process_manual(self.e.last_spot,self.e.feed.snapshot(),datetime.now(IST))
        return next(x for x in self.manual_orders() if x['id']==order['id'])
    def manual_orders(self):
        with self.e.connect() as c:
            c.execute('CREATE TABLE IF NOT EXISTS manual_paper_orders(id TEXT PRIMARY KEY,value TEXT)')
            return [json.loads(r[0]) for r in c.execute('SELECT value FROM manual_paper_orders ORDER BY rowid DESC')]
    def order_save(self,o):
        with self.e.connect() as c:c.execute('UPDATE manual_paper_orders SET value=? WHERE id=?',(json.dumps(o),o['id']))
    def process_manual(self,spot,snapshot,now):
        opened=self.desk.market_open(now.replace(tzinfo=None))
        for o in self.manual_orders():
            if o['status']!='WAITING':continue
            c=o['contract']
            if c['expiry']<now.date().isoformat():
                o.update(status='REJECTED',message='Contract expired before paper fill');self.order_save(o);continue
            label='manual_'+c['securityId'];self.e.feed.select(label,'NSE_FNO',c['securityId'])
            q=snapshot['quotes'].get(label)
            #Market orders requested during an open session must not fill much later on an unrelated price.
            elapsed=(now-datetime.fromisoformat(o['createdAt'])).total_seconds()
            was_open=self.desk.market_open(datetime.fromisoformat(o['createdAt']).replace(tzinfo=None))
            if opened and was_open and o['orderType']=='MARKET' and elapsed>self.cfg['manual_paper_market_wait_seconds']:
                o.update(status='REJECTED',message='No fresh quote within market-order window');self.order_save(o);continue
            if not opened or not q or not q.get('fresh') or not spot:continue
            price=self.entry_price(q)
            if o['orderType']=='LIMIT' and price>o['limitPrice']:continue
            if sum(t['status']=='OPEN' for t in self.e.paper())>=self.cfg['max_paper_positions']:continue
            #8510 marketable limit fills at ask; resting limit crossing fills at the limit.
            fill=price if o['orderType']=='MARKET' or elapsed<=self.cfg['manual_paper_immediate_seconds'] else o['limitPrice']
            if o['stopPoints']>=fill and o['stopPoints']>0:
                o.update(status='REJECTED',message='Premium stop would be non-positive');self.order_save(o);continue
            entry_cost=fill*c['lotSize']*o['lots']
            positions=self.e.paper();capital=self.rates()['initial_capital']+sum(t.get('netPnl') or 0 for t in positions if t['status']=='CLOSED')-sum(t['entryPremium']*t['quantity'] for t in positions if t['status']=='OPEN')
            buy_fee=self.desk.leg_charges(fill,c['lotSize']*o['lots'],'BUY',self.rates())['total']
            if entry_cost+buy_fee>capital:
                o.update(status='REJECTED',message='Insufficient independent8500 paper cash');self.order_save(o);continue
            t=dict(id=o['id'],day=now.date().isoformat(),entryTime=now.isoformat(),setup='Manual paper —8510 ticket',setup_id=5,manual=True,side=1 if c['optionType']=='CE' else -1,
                entrySpot=spot,currSpot=spot,entryPremium=fill,currentPremium=fill,quantity=c['lotSize']*o['lots'],grossPnl=0,status='OPEN',
                premiumStop=fill-o['stopPoints'] if o['stopPoints']>0 else None,premiumTarget=fill+o['targetPoints'] if o['targetPoints']>0 else None,
                source='8510 ask/limit paper fill; same8510 fee calculator',**c)
            self.e.save(self.settle(t));o.update(status='FILLED',fill=fill,fillAt=now.isoformat());self.order_save(o)
            self.e.feed.select('position_'+c['securityId'],'NSE_FNO',c['securityId'])
        for t in self.e.paper():
            if not t.get('manual') or t['status']!='OPEN':continue
            label='position_'+t['securityId'];self.e.feed.select(label,'NSE_FNO',t['securityId']);q=snapshot['quotes'].get(label)
            if not opened or not q or not q.get('fresh'):continue
            bid=self.exit_price(q);t.update(currentPremium=bid,currSpot=spot or t['currSpot'])
            reason='SESSION_EXIT' if now.strftime('%H:%M')>=self.settings()['squareoff'] else 'PREMIUM_STOP' if t.get('premiumStop') is not None and bid<=t['premiumStop'] else 'PREMIUM_TARGET' if t.get('premiumTarget') is not None and bid>=t['premiumTarget'] else None
            if reason:t.update(status='CLOSED',exitPremium=bid,exitTime=now.isoformat(),reason=reason)
            self.e.save(self.settle(t))
    def close_manual(self,body):
        t=next((x for x in self.e.paper() if x['id']==body.get('id') and x.get('manual') and x['status']=='OPEN'),None)
        if not t:raise ValueError('Open manual paper position not found')
        if not self.desk.market_open(self.desk.ist_now()):raise ValueError('Market closed; no paper exit fabricated')
        q=self.e.feed.snapshot()['quotes'].get('position_'+t['securityId'])
        if not q or not q.get('fresh'):raise ValueError('Fresh quote required for paper exit')
        price=self.exit_price(q);t.update(status='CLOSED',currentPremium=price,currSpot=self.e.last_spot or t['currSpot'],exitPremium=price,exitTime=datetime.now(IST).isoformat(),reason='MANUAL_EXIT')
        self.e.save(self.settle(t));return t
    def cancel_manual(self,body):
        order=next((x for x in self.manual_orders() if x['id']==body.get('id')),None)
        if not order or order['status']!='WAITING':raise ValueError('Waiting paper order not found; filled positions require an exit')
        order.update(status='CANCELLED',message='Cancelled by user — paper only');self.order_save(order);return order
