"""Read-only historical views: recorded paper ledger and observed market bars.

No embedded trades, synthetic candles, assumed premiums, retrospective signals,
or reconstructed broker executions. Missing filter features remain None.
"""
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import sqlite3, math, logging

IST=ZoneInfo('Asia/Kolkata');log=logging.getLogger(__name__)

def parsed_time(value):
    if not value:return None
    try:
        t=datetime.fromisoformat(str(value).replace(' IST','').replace('Z','+00:00'))
        return t.replace(tzinfo=IST) if t.tzinfo is None else t.astimezone(IST)
    except (ValueError,TypeError):return None

def finite(value):
    try:
        n=float(value)
        return n if math.isfinite(n) else None
    except (ValueError,TypeError):return None

class RecordedHistory:
    def __init__(self, ledger, market):
        self.ledger=Path(ledger);self.market=Path(market);self.issues=[]
    def read(self,path,query,args=()):
        if not path.exists():
            self.issues.append('Missing '+path.name);return []
        try:
            with sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True,timeout=2) as db:
                db.row_factory=sqlite3.Row
                return [dict(r) for r in db.execute(query,args)]
        except sqlite3.Error as exc:
            self.issues.append(type(exc).__name__+': '+str(exc));log.warning('Recorded history unavailable: %s',exc);return []
    def trades(self,start=None,end=None):
        result=[]
        for p in self.read(self.ledger,"SELECT * FROM positions WHERE status='CLOSED'"):
            entry=parsed_time(p.get('entry_time'));exit=parsed_time(p.get('exit_time'))
            values=[finite(p.get(k)) for k in ('entry_price','exit_price','qty','gross_pnl','net_pnl')]
            if not entry or not exit or exit<entry or any(v is None for v in values):continue
            ep,xp,qty,gross,net=values
            if ep<=0 or xp<0 or qty<=0 or not p.get('security_id') or not p.get('symbol'):continue
            day=entry.date().isoformat()
            if start and day<start or end and day>end:continue
            mode=str(p.get('mode',''))
            if mode!='PAPER_ONLY':
                # Legacy BOTH positions mixed simulated and real legs. They are
                # not certified as independent executions by this read-only view.
                continue
            name=p.get('strategy_setup') or p.get('strategy_name') or 'Unlabelled recorded setup'
            # A legacy setup is not relabelled as the current 8-EMA strategy.
            legacy='80' in name or 'ATMA' in name.upper()
            charges=gross-net
            direction='LONG' if 'CE' in p['symbol'].upper() or 'CALL' in p['symbol'].upper() else 'SHORT' if 'PE' in p['symbol'].upper() or 'PUT' in p['symbol'].upper() else 'UNKNOWN'
            duration=str(exit-entry)
            result.append(dict(Trade_ID=p['position_id'],trade_id=p['position_id'],Date=day,date=day,Setup_Name=name,setup_name=name,strategy_setup=name,Strategy=name,Direction=direction,direction=direction,
                Entry_Time=entry.strftime('%H:%M:%S'),Exit_Time=exit.strftime('%H:%M:%S'),entry_ist=entry.isoformat(),exit_ist=exit.isoformat(),entry_time=entry.isoformat(),exit_time=exit.isoformat(),duration=duration,
                Contract_Symbol=p['symbol'],contract_symbol=p['symbol'],symbol=p['symbol'],security_id=str(p['security_id']),Lot_Qty=int(qty),qty=int(qty),option_entry=ep,option_exit=xp,Option_Points=xp-ep,
                Gross_Pnl_INR=gross,gross_pnl=gross,Exact_Charges_INR=charges,dhan_charges=charges,charges=charges,Net_Pnl_INR=net,net_pnl=net,
                Result=p.get('exit_reason') or 'Recorded close',outcome=p.get('exit_reason') or 'Recorded close',exit_reason=p.get('exit_reason'),source=p.get('source') or p.get('placed_by'),
                provenance='RECORDED_PAPER_LEDGER',pricing_basis='Stored simulated fills; not verified broker executions',legacy_setup=legacy,mode=mode,
                **{key:None for key in ('Market_Gap_Pts','EMA_Slope','EMA_Aligned','ADX','R_Squared','MTF_Aligned','Vol_Mult','PCR_At_Entry','index_entry','index_exit')}))
        return sorted(result,key=lambda r:(r['entry_ist'],str(r['Trade_ID'])))
    def candles(self,day,security='13'):
        start=datetime.fromisoformat(day+'T09:15:00').replace(tzinfo=IST);end=start.replace(hour=15,minute=30)
        return [dict(ts=r['ts'],open=r['o'],high=r['h'],low=r['l'],close=r['c'],volume=r['v']) for r in self.read(self.market,'SELECT ts,o,h,l,c,v FROM candles_1m WHERE sec_id=? AND ts>=? AND ts<? ORDER BY ts',(security,int(start.timestamp()),int(end.timestamp())))]
    def dates(self):
        values={r['Date'] for r in self.trades()}
        for r in self.read(self.market,"SELECT DISTINCT date(ts,'unixepoch','+330 minutes') AS day FROM candles_1m WHERE sec_id='13'"):
            if r['day']:values.add(r['day'])
        return sorted(values,reverse=True)
    def daily(self,day):
        datetime.strptime(day,'%Y-%m-%d');trades=self.trades(day,day);spot=self.candles(day);future=self.candles(day,'FUT')
        # Every chain row is from one actual timestamp and expiry, never maxima
        # and average LTP combined across unrelated moments.
        start=int(datetime.fromisoformat(day+'T09:15').replace(tzinfo=IST).timestamp());end=start+int((datetime.fromisoformat(day+'T15:30')-datetime.fromisoformat(day+'T09:15')).total_seconds())
        latest=self.read(self.market,'SELECT max(ts) AS ts FROM chain_snap WHERE ts>=? AND ts<?',(start,end))
        chain=self.read(self.market,'SELECT * FROM chain_snap WHERE ts=? ORDER BY expiry,strike',(latest[0]['ts'],)) if latest and latest[0]['ts'] is not None else []
        net=sum(t['net_pnl'] for t in trades);pretty=datetime.fromisoformat(day).strftime('%d %b %Y')
        status=f"Recorded {len(spot)} spot / {len(future)} futures minutes; gaps retained"
        summary=dict(date=day,date_formatted=pretty,ingest_status=status,paper_trades_count=len(trades),net_pnl_inr=f'₹{net:,.2f}',net_pnl=net,trades_breakdown=trades,signals_summary='Not reconstructed from outcomes',total_oi_bias='UNKNOWN — no verified as-of classification',oi_change_bias='UNKNOWN — no verified as-of classification',combined_oi_bias='UNKNOWN — no verified as-of classification',
            whatsapp_text=f'{pretty} — {status}\nRecorded closed paper trades: {len(trades)}\nStored net paper P&L: ₹{net:,.2f}\nPaper ledger values are not broker fills. Missing signals are UNKNOWN.')
        return dict(ok=True,date=day,date_formatted=pretty,history_schema='recorded-v194d',status='RECORDED' if spot or trades else 'NO_RECORDED_DATA',message='Recorded data only; gaps are not filled',checklist={'spot_candles_1m':str(len(spot))+' observed','future_candles_1m':str(len(future))+' observed','paper_trades':str(len(trades))+' closed ledger rows','option_chain':str(len(chain))+' rows at last recorded snapshot'},spot_candles_1m=spot,future_candles_1m=future,option_chain=chain,resampled={},strategy_signals={},trades=trades,summary=summary,eod_summary=summary,issues=list(self.issues))
    def report(self,start,end,bias='BOTH'):
        datetime.strptime(start,'%Y-%m-%d');datetime.strptime(end,'%Y-%m-%d')
        if start>end:raise ValueError('Start date must be at or before end date')
        rows=self.trades(start,end)
        if bias=='LONG_ONLY':rows=[r for r in rows if r['direction']=='LONG']
        if bias=='SHORT_ONLY':rows=[r for r in rows if r['direction']=='SHORT']
        return dict(ok=True,from_date=start,to_date=end,trades=rows,total_trades=len(rows),rows=rows,provenance='RECORDED_PAPER_LEDGER',message='Recorded closed paper trades; not a counterfactual backtest' if rows else 'No recorded data for this range',recorded_net_pnl=sum(r['net_pnl'] for r in rows),fixed_net_pnl=None,trailing_net_pnl=None,alpha_locked=None,win_rate_fixed=None,win_rate_trailing=None,profit_factor_fixed=None,profit_factor_trailing=None,max_drawdown_fixed=None,max_drawdown_trailing=None,reconciliation=[],reconciliation_match=None,issues=list(self.issues))

    def persist_view(self,payload):
        """Persist derived view separately; never overwrite legacy/raw records."""
        import json
        if not self.ledger.exists():return
        with sqlite3.connect(self.ledger,timeout=2) as db:
            db.execute('CREATE TABLE IF NOT EXISTS recorded_daily_views (date TEXT PRIMARY KEY, schema TEXT NOT NULL, updated_at TEXT NOT NULL, payload TEXT NOT NULL)')
            db.execute('INSERT OR REPLACE INTO recorded_daily_views VALUES (?,?,?,?)',(payload['date'],payload['history_schema'],datetime.now(IST).isoformat(),json.dumps(payload,allow_nan=False)))
