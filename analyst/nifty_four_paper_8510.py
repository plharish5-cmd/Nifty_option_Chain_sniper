"""Deferred paper-only execution. Reuses scoped PaperBook and existing fee calculator."""
import json
import sqlite3
from pathlib import Path
from analyst.stockmojo_paper_8510 import PaperBook
from analyst.nifty_four_oi_8510 import digest,parse_time


class NextQuotes:
    def __init__(self,root):self.root=Path(root)
    def contract(self,expiry,side,security_id=None):
        p=self.root/'analyst/stockmojo_option_contracts.json'
        try:
            if p.is_symlink():return None
            cat=json.loads(p.read_text())
            if cat.get('review_status')!='REVIEWED' or not cat.get('evidence'):return None
            rows=[x for x in cat['contracts'] if x.get('underlying')=='NIFTY' and x.get('expiry')==expiry and x.get('side')==side and
                  x.get('security_id') and type(x.get('lot_size')) is int and x['lot_size']>0 and type(x.get('strike')) in (int,float)]
            if security_id:rows=[x for x in rows if x['security_id']==security_id]
            # Catalog order/ID selection is fixed before looking at later prices.
            return min(rows,key=lambda x:(x.get('selection_priority',0),x['security_id'])) if rows else None
        except (OSError,ValueError,KeyError,TypeError):return None
    def next(self,contract,after,until):
        db=self.root/'ict_smc_app/backend/data/ict_smc.sqlite3'
        if not contract or not db.exists() or until<=after:return None
        prefix='ce' if contract['side']=='CE' else 'pe'
        try:
            with sqlite3.connect('file:'+str(db)+'?mode=ro',uri=True) as c:
                row=c.execute(f'SELECT ts,{prefix}_bid,{prefix}_ask FROM chain_snap WHERE expiry=? AND strike=? AND ts>? AND ts<=? AND {prefix}_bid>0 AND {prefix}_ask>={prefix}_bid ORDER BY ts LIMIT 1',
                    (contract['expiry'],contract['strike'],after,until)).fetchone()
            return {**contract,'timestamp':row[0],'bid':row[1],'ask':row[2],'verified':True,'source':'RECORDED_CHAIN_SNAP_BID_ASK'} if row else None
        except sqlite3.Error:return None


class FourPaper:
    def __init__(self,root,risk_rules,feature_rules,fees=None):
        self.root=Path(root);self.risk=risk_rules;self.cfg=feature_rules
        self.book=PaperBook(self.root/'data/nifty_four_oi/paper.sqlite3',risk_rules,fees);self.quotes=NextQuotes(root)
    def connect(self):
        c=self.book.connect();c.execute('CREATE TABLE IF NOT EXISTS four_pending(account TEXT PRIMARY KEY,data TEXT)');c.execute('CREATE TABLE IF NOT EXISTS four_decisions(account TEXT,event TEXT,result TEXT,PRIMARY KEY(account,event))');c.commit();return c
    def pending(self,account):
        with self.connect() as c:
            row=c.execute('SELECT data FROM four_pending WHERE account=?',(account,)).fetchone();return json.loads(row[0]) if row else None
    def save_pending(self,account,data):
        with self.connect() as c:
            if data is None:c.execute('DELETE FROM four_pending WHERE account=?',(account,))
            else:c.execute('INSERT OR REPLACE INTO four_pending VALUES(?,?)',(account,json.dumps(data)))
    def handle(self,account,analysis,at,enabled=False):
        result={'status':'HOLD'}
        for _ in range(self.cfg['max_quote_events_per_tick']):
            before=self.book.snapshot(account)['position'] if enabled else None
            step=self._handle_once(account,analysis,at,enabled)
            if step['status']!='HOLD' or result['status']=='HOLD':result=step
            after=self.book.snapshot(account)['position'] if enabled else None
            if not after or (before and before.get('last_quote_ts',before['entry_time'])==after.get('last_quote_ts',after['entry_time'])):break
        return result
    def next_quote(self,account,contract,after,until):
        if account.startswith('live-paper:'):after=max(after,until-self.risk['quote_age_seconds'])
        return self.quotes.next(contract,after,until)
    def _handle_once(self,account,analysis,at,enabled=False):
        if not enabled:return {'status':'PAPER_DISABLED'}
        if not account.startswith(('replay:four-','live-paper:four-')):raise ValueError('FOUR_PAPER_ACCOUNT_REQUIRED')
        now=parse_time(at);stamp=now.timestamp();cfg=self.cfg;snapshot=self.book.snapshot(account);pos=snapshot['position'];pending=self.pending(account)
        action=analysis['trade_action'];event=analysis.get('event_id');result={'status':'HOLD'}
        # Process already-known protection using observed quotes, independent of new entry eligibility.
        if pos:
            if pending and pending['kind']=='ENTRY':self.save_pending(account,None);pending=None
            contract=self.quotes.contract(pos['expiry'],pos['side'],pos['security_id'])
            after=pos.get('last_quote_ts',parse_time(pos['entry_time']).timestamp())
            q=self.next_quote(account,contract,after,stamp)
            eod=now.strftime('%H:%M')>=cfg['squareoff_time'];exit_requested=action=='EXIT' or eod or bool(pos.get('exit_pending'))
            if exit_requested and (not pending or pending['kind']!='EXIT'):
                pending={'kind':'EXIT','at':stamp,'reason':'END_OF_DAY' if eod else 'SIGNAL_EXIT','signal':analysis,'contract':contract};self.save_pending(account,pending)
            if pending and pending['kind']=='EXIT':
                q=self.next_quote(account,contract,pending['at']+cfg['fill_latency_seconds'],stamp)
                if not q:
                    pos.update(exit_pending=True,price_risk='EXIT_PENDING: no later executable bid; position remains open')
                    with self.connect() as c:c.execute('UPDATE open_positions SET data=? WHERE account=?',(json.dumps(pos),account))
                    return {'status':'EXIT_PENDING','reason':'MISSING_OPTION_QUOTES','price_risk':pos['price_risk']}
            if q:
                reference={**analysis,'signal':'EXIT' if pending and pending['kind']=='EXIT' else 'HOLD','state':'HISTORICAL_REPLAY','quality_flags':[],
                    'event_id':digest({'position':pos['signal_id'],'quote':q['timestamp'],'kind':'PROTECTION'}),
                    'source_time':analysis.get('source_time',pos['entry_source_time']),'completed_at':now.fromtimestamp(q['timestamp'],now.tzinfo).isoformat(),
                    'instrument':{'underlying':'NIFTY','options_expiry':pos['expiry']},'config_version':cfg['version'],
                    'reasons':['Protection of existing verified paper contract; not a new directional entry.'],'factors':analysis.get('factors',{})}
                result=self.book.process(account,reference,q,True)
                current=self.book.snapshot(account)['position']
                if current:
                    current['last_quote_ts']=q['timestamp']
                    with self.connect() as c:c.execute('UPDATE open_positions SET data=? WHERE account=?',(json.dumps(current),account))
                else:
                    if result.get('fill') and pending and pending['kind']=='EXIT':
                        result['fill']['exit_reason']=pending['reason'];result['fill']['paper_processing_at']=now.isoformat()
                        eid=digest({'event':reference['event_id'],'version':digest(self.risk)})
                        with self.connect() as c:
                            c.execute('UPDATE fills SET data=? WHERE account=? AND id=?',(json.dumps(result['fill']),account,eid))
                            c.execute('UPDATE events SET result=? WHERE account=? AND id=?',(json.dumps(result),account,eid))
                    self.save_pending(account,None)
            return result
        risk=self.risk['paper']
        if (type(risk.get('lots')) is not int or risk['lots']<=0 or type(risk.get('max_positions')) is not int or risk['max_positions']<1 or
            any(not isinstance(risk.get(k),(int,float)) or risk[k]<=0 for k in ('capital','daily_loss_limit','premium_stop_points','premium_target_points')) or risk.get('slippage_points',-1)<0):
            return {'status':'BLOCKED','reason':'INVALID_PAPER_RISK_CONFIGURATION'}
        if pending and pending['kind']=='EXIT':self.save_pending(account,None);pending=None
        if pending and pending['kind']=='ENTRY':
            if action in ('EXIT','BLOCKED') or (action in ('BUY_CE','BUY_PE') and pending['contract']['side']!=('CE' if action=='BUY_CE' else 'PE')) or now.strftime('%H:%M')>=cfg['entry_cutoff'] or stamp-pending['at']>cfg['max_fill_delay_seconds']:
                self.save_pending(account,None);return {'status':'CANCELLED','reason':'ELIGIBILITY_CHANGED_OR_ENTRY_WINDOW_CLOSED'}
            q=self.next_quote(account,pending['contract'],pending['at']+cfg['fill_latency_seconds'],min(stamp,pending['at']+cfg['max_fill_delay_seconds']))
            if q:
                reference={**pending['signal'],'completed_at':now.fromtimestamp(q['timestamp'],now.tzinfo).isoformat(),
                    'event_id':digest({'decision':pending['signal']['event_id'],'quote':q['timestamp'],'kind':'ENTRY'})}
                result=self.book.process(account,reference,q,True);self.save_pending(account,None);return result
            result={'status':'ENTRY_PENDING','reason':'MISSING_OPTION_QUOTES'}
        if not pending and action in ('BUY_CE','BUY_PE'):
            with self.connect() as c:
                seen=c.execute('SELECT result FROM four_decisions WHERE account=? AND event=?',(account,event)).fetchone()
                if seen:return {**json.loads(seen[0]),'duplicate':True}
            if now.weekday()>4 or not cfg['session_start']<=now.strftime('%H:%M')<cfg['entry_cutoff']:return {'status':'BLOCKED','reason':'ENTRY_WINDOW_CLOSED'}
            contract=self.quotes.contract(analysis['instrument']['options_expiry'],'CE' if action=='BUY_CE' else 'PE')
            if not contract:result={'status':'BLOCKED','reason':'MISSING_OPTION_QUOTES'}
            else:
                self.save_pending(account,{'kind':'ENTRY','at':stamp,'signal':analysis,'contract':contract})
                result={'status':'ENTRY_PENDING','reason':'WAIT_FOR_NEXT_EXECUTABLE_QUOTE'}
            with self.connect() as c:c.execute('INSERT OR IGNORE INTO four_decisions VALUES(?,?,?)',(account,event,json.dumps(result)))
        return result
