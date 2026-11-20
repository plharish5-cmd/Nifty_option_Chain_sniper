"""Isolated paper adapter: existing tariff and chain_snap quotes, never broker APIs.
Legacy place_paper_order/exit_paper_position use global account files and wall time;
replay instead persists an atomic account/fill audit in this scoped ledger.
"""
import json
import sqlite3
from pathlib import Path
from analyst.stockmojo_research_8510 import digest,number,parse_time


class RecordedQuotes:
    def __init__(self,root,rules):self.root=Path(root);self.rules=rules
    def get(self,observation,side,contract_id=None):
        # Existing chain_snap has no underlying column: it is the NIFTY store.
        if observation.get('instrument',{}).get('underlying')!=self.rules['quote_database_underlying']:return None
        path=self.root/'analyst/stockmojo_option_contracts.json'
        if not path.exists() or path.is_symlink():return None
        try:
            catalog=json.loads(path.read_text())
            if catalog.get('review_status')!='REVIEWED' or not catalog.get('evidence'):return None
            candidates=[x for x in catalog['contracts'] if x.get('underlying')==observation['instrument']['underlying'] and x.get('expiry')==observation['instrument']['options_expiry'] and x.get('side')==side]
            if contract_id:candidates=[x for x in candidates if x.get('security_id')==contract_id]
            db=self.root/'ict_smc_app/backend/data/ict_smc.sqlite3'
            if not db.exists():return None
            stamp=parse_time(observation['completed_at']).timestamp();prefix='ce' if side=='CE' else 'pe';found=[]
            with sqlite3.connect('file:'+str(db)+'?mode=ro',uri=True) as c:
                for contract in candidates:
                    if type(contract.get('lot_size')) is not int or contract['lot_size']<=0 or not contract.get('security_id'):continue
                    row=c.execute(f'SELECT ts,{prefix}_bid,{prefix}_ask FROM chain_snap WHERE expiry=? AND strike=? AND ts<=? AND ts>=? ORDER BY ts DESC LIMIT 1',
                        (contract['expiry'],contract['strike'],stamp,stamp-self.rules['quote_age_seconds'])).fetchone()
                    if row and row[1] is not None and row[2] is not None and 0<row[1]<=row[2]:
                        found.append({**contract,'timestamp':row[0],'bid':row[1],'ask':row[2],'verified':True,'source':'RECORDED_CHAIN_SNAP_BID_ASK'})
            # No invented ATM calculation; deterministic lowest-spread candidate, then ID.
            return min(found,key=lambda q:((q['ask']-q['bid'])/q['ask'],q['security_id'])) if found else None
        except (OSError,ValueError,KeyError,TypeError,sqlite3.Error):return None


class PaperBook:
    def __init__(self,path,rules,fees=None):
        self.path=Path(path);self.rules=rules;self.fees=fees
    def connect(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        c=sqlite3.connect(self.path,timeout=10);c.row_factory=sqlite3.Row
        c.execute('CREATE TABLE IF NOT EXISTS accounts (id TEXT PRIMARY KEY, cash REAL NOT NULL, realized REAL NOT NULL DEFAULT 0, last_exit TEXT)')
        c.execute('CREATE TABLE IF NOT EXISTS events (account TEXT,id TEXT,result TEXT, PRIMARY KEY(account,id))')
        c.execute('CREATE TABLE IF NOT EXISTS fills (account TEXT,id TEXT,data TEXT, PRIMARY KEY(account,id))')
        c.execute('CREATE TABLE IF NOT EXISTS open_positions (account TEXT PRIMARY KEY,data TEXT)')
        c.commit();return c
    def snapshot(self,account):
        with self.connect() as c:
            a=c.execute('SELECT * FROM accounts WHERE id=?',(account,)).fetchone()
            p=c.execute('SELECT data FROM open_positions WHERE account=?',(account,)).fetchone()
            fills=[json.loads(x[0]) for x in c.execute('SELECT data FROM fills WHERE account=? ORDER BY rowid',(account,))]
            return {'account':dict(a) if a else None,'position':json.loads(p[0]) if p else None,'fills':fills}
    def _charges(self,entry,exit,qty):
        if self.fees:return self.fees(entry,exit,qty)
        from paper_trading import calculate_trade_charges
        return calculate_trade_charges(entry,exit,qty,cfg=self.rules['fees'])['total_charges']
    def previous_signal(self,account,event_id):
        eid=digest({'event':event_id,'version':digest(self.rules)})
        with self.connect() as c:
            row=c.execute('SELECT result FROM events WHERE account=? AND id=?',(account,eid)).fetchone()
            return json.loads(row[0]).get('signal_snapshot') if row else None
    def process(self,account,signal,quote,enabled=False):
        if not (account.startswith('replay:') or account.startswith('live-paper:')):raise ValueError('ISOLATED_PAPER_ACCOUNT_REQUIRED')
        if not enabled:return {'status':'PAPER_DISABLED'}
        if signal['signal']=='BLOCKED':return {'status':'BLOCKED','reason':signal.get('quality_flags')}
        if signal.get('state') not in ('HISTORICAL_REPLAY','LIVE_FRESH'):return {'status':'BLOCKED','reason':'NON_EXECUTABLE_STATE'}
        r=self.rules['paper'];eid=digest({'event':signal['event_id'],'version':digest(self.rules)})
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            old=c.execute('SELECT result FROM events WHERE account=? AND id=?',(account,eid)).fetchone()
            if old:return {**json.loads(old[0]),'duplicate':True}
            c.execute('INSERT OR IGNORE INTO accounts(id,cash) VALUES(?,?)',(account,r['capital']))
            wallet=c.execute('SELECT * FROM accounts WHERE id=?',(account,)).fetchone()
            raw=c.execute('SELECT data FROM open_positions WHERE account=?',(account,)).fetchone();pos=json.loads(raw[0]) if raw else None
            result={'status':'HOLD'};stamp=parse_time(signal['completed_at']);source=signal['source_time'];side='CE' if signal['signal']=='BUY_CE' else 'PE'
            if pos:side=pos['side']
            valid=(quote and quote.get('verified') is True and quote.get('source')=='RECORDED_CHAIN_SNAP_BID_ASK' and
                quote.get('underlying')==signal['instrument']['underlying'] and quote.get('expiry')==signal['instrument']['options_expiry'] and
                quote.get('side')==side and quote.get('security_id') and type(quote.get('lot_size')) is int and quote['lot_size']>0 and
                all(number(quote.get(k)) is not None for k in ('bid','ask','strike','timestamp')) and 0<quote['bid']<=quote['ask'] and
                0<=stamp.timestamp()-quote['timestamp']<=self.rules['quote_age_seconds'])
            if pos and valid and quote['security_id']!=pos['security_id']:valid=False
            if not valid:result={'status':'BLOCKED','reason':'MISSING_OPTION_QUOTES'}
            elif pos:
                bid=max(0,quote['bid']-r['slippage_points']);reason=None
                if bid<=pos['entry_price']-r['premium_stop_points']:reason='PREMIUM_STOP'
                elif bid>=pos['entry_price']+r['premium_target_points']:reason='PREMIUM_TARGET'
                elif signal['signal']=='EXIT' or signal['signal'] in ('BUY_CE','BUY_PE') and side!=('CE' if signal['signal']=='BUY_CE' else 'PE'):reason='SIGNAL_EXIT'
                elif source[11:16]>=self.rules['session_end']:reason='SESSION_END'
                if not reason:
                    pos.update(current_bid=bid,mark_source_time=source,unrealized_gross_pnl=(bid-pos['entry_price'])*pos['quantity'])
                    c.execute('UPDATE open_positions SET data=? WHERE account=?',(json.dumps(pos),account))
                if reason:
                    charges=self._charges(pos['entry_price'],bid,pos['quantity']);gross=(bid-pos['entry_price'])*pos['quantity'];net=gross-charges
                    fill={**pos,'exit_price':bid,'exit_source_time':source,'exit_time':signal['completed_at'],'exit_future_ltp':signal['factors'].get('future_ltp'),
                          'gross_pnl':gross,'charges':charges,'net_pnl':net,'exit_reason':reason,'exit_signal_id':signal['event_id'],'status':'CLOSED'}
                    c.execute('INSERT INTO fills VALUES(?,?,?)',(account,eid,json.dumps(fill)))
                    c.execute('DELETE FROM open_positions WHERE account=?',(account,))
                    c.execute('UPDATE accounts SET cash=cash+?,realized=realized+?,last_exit=? WHERE id=?',(bid*pos['quantity']-charges,net,signal['completed_at'],account))
                    result={'status':'CLOSED','fill':fill}
            elif signal['signal'] in ('BUY_CE','BUY_PE'):
                day=source[:10];loss=sum(json.loads(x[0])['net_pnl'] for x in c.execute('SELECT data FROM fills WHERE account=?',(account,)) if json.loads(x[0])['exit_source_time'][:10]==day)
                cooldown=wallet['last_exit'] and (stamp-parse_time(wallet['last_exit'])).total_seconds()<self.rules['cooldown_seconds']
                entry=quote['ask']+r['slippage_points'];qty=r['lots']*quote['lot_size'];cost=entry*qty
                if loss<=-r['daily_loss_limit']:result={'status':'BLOCKED','reason':'DAILY_PAPER_LOSS_LIMIT'}
                elif cooldown:result={'status':'BLOCKED','reason':'COOLDOWN'}
                elif r['max_positions']<1 or cost+self._charges(entry,entry,qty)>wallet['cash']:result={'status':'BLOCKED','reason':'CAPITAL_OR_POSITION_LIMIT'}
                else:
                    pos={k:quote[k] for k in ('underlying','expiry','strike','side','security_id','lot_size')}
                    pos.update(entry_price=entry,quantity=qty,entry_source_time=source,entry_time=signal['completed_at'],entry_future_ltp=signal['factors'].get('future_ltp'),
                        signal_id=signal['event_id'],configuration=signal['config_version'],fill_assumption='RECORDED_ASK_PLUS_SLIPPAGE; not exchange fill',status='OPEN')
                    c.execute('INSERT INTO open_positions VALUES(?,?)',(account,json.dumps(pos)))
                    c.execute('UPDATE accounts SET cash=cash-? WHERE id=?',(cost,account));result={'status':'OPEN','position':pos}
            result.update(signal_snapshot=signal,signal_id=signal['event_id'],source_time=source,configuration_hash=digest(self.rules),reasons=signal.get('reasons',[]))
            c.execute('INSERT INTO events VALUES(?,?,?)',(account,eid,json.dumps(result)))
            return result
