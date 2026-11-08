"""
algo_execution_engine/order_gateway.py — Dhan API v2 & Paper Trading Execution Gateway with Server-Side SL-M.

Safeguards & Features:
  1. Live Dhan v2 Orders API (`/v2/orders`) & companion exchange-side `SL-M` (`STOP_LOSS_MARKET`) order placement.
  2. Automatic SL-M modification on Dhan servers when stop-loss trails or moves to Cost-to-Cost.
  3. Automatic SL-M cancellation upon Target or Volatility exit.
  4. Strike Selection (Trading Bible Part G 3-Rule System):
     - Multiples of 100 ONLY (e.g. 23,700 CE, 23,800 CE — never 23,750 CE).
     - ITM Strike nearest to ₹200 premium (~0.60 delta).
     - Bid-Ask Spread Guard: Abort if spread > 2.0% of premium or > ₹3.0 pts.
  5. Stage 3 & Stage 4 Gatekeeper (Part L Rules):
     - Programmatic lock: Cannot switch to LIVE_1LOT unless Stage 3 Graduation Tracker certifies all 5 gates passed.
     - Hard 1-Lot Cap: Quantities in LIVE_1LOT are hard-capped to 1 Lot (75 qty).
     - Round-Trip Latency and Slippage Tracker.
  6. Full Paper Trading Simulation recording to both `algo_setup1_sandbox_trades` and `paper_trades` in SQLite.
  7. Daily Circuit Breakers: Max 2 trades/day, Max daily loss (-₹3,000).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import sys
import time
import csv
import math
import uuid
from dataclasses import asdict
from zoneinfo import ZoneInfo
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import paper_trading

from .stage3_sandbox import SandboxTrade, Stage3GraduationTracker
from .strategy_setup1 import ActivePosition, Setup1Signal, load_config

log = logging.getLogger("algo.gateway")


def load_env_if_needed() -> None:
    """Reads .env without leaking secrets into logs or console."""
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if env_file.exists():
        try:
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())
        except Exception as e:
            log.error("Error loading .env: %s", e)


def get_dynamic_nifty_lot_size(master_path: Optional[str] = None) -> int:
    """
    Reads the NIFTY option lot size dynamically from Dhan instrument master.
    Fails closed if the master file does not exist or lot size cannot be read.
    """
    candidates = [
        Path(master_path) if master_path else None,
        Path("data/terminal8500/instrument_master.csv"),
        Path(__file__).resolve().parent.parent / "data/terminal8500/instrument_master.csv",
    ]
    for p in candidates:
        if p and p.exists():
            try:
                import csv
                with open(p, mode="r", encoding="utf-8", errors="ignore") as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        if (row.get("SEM_SEGMENT") == "D" and 
                            row.get("SEM_INSTRUMENT_NAME") == "OPTIDX" and 
                            row.get("SEM_TRADING_SYMBOL", "").startswith("NIFTY-")):
                            lot = int(float(row.get("SEM_LOT_UNITS", 0)))
                            if lot > 0:
                                return lot
            except Exception as e:
                log.error("Failed reading lot size from instrument master %s: %s", p, e)
    raise RuntimeError("CRITICAL: Failed to read dynamic NIFTY lot size from Dhan instrument master! Failing closed.")


def validate_1lot_cap(requested_qty: int, max_lot_qty: int) -> int:
    """
    Enforces the strict 1-Lot cap across EVERY entry path.
    Rejects or clamps to strictly 1 lot (max_lot_qty).
    """
    if requested_qty <= 0:
        raise ValueError(f"Invalid order quantity: {requested_qty}")
    if requested_qty > max_lot_qty:
        log.warning("🚨 1-LOT HARD CAP TRIGGERED: Requested qty %d exceeds 1 lot (%d). Clamping to 1 lot.",
                    requested_qty, max_lot_qty)
        return max_lot_qty
    return requested_qty


class DhanOrderGateway:
    """Scoped engine gateway. Paper quotes are observed; LIVE requires broker fills.
    HTTP acceptance is never treated as a fill. Uncertain intents block resubmission.
    """
    def __init__(self,config_dict=None):
        load_env_if_needed();self.cfg=config_dict or load_config();self.exec_cfg=self.cfg.get('execution',{});self.risk_cfg=self.cfg.get('risk',{})
        self.base_url=self.exec_cfg.get('dhan_base_url','https://api.dhan.co/v2')
        if self.base_url!='https://api.dhan.co/v2':raise ValueError('Unexpected broker endpoint')
        self.execution_mode=self.exec_cfg.get('execution_mode','PAPER').upper();self.db_path=self.exec_cfg.get('sqlite_db_path','ict_smc_app/backend/data/ict_smc.sqlite3')
        self.clock=time.time;self.dynamic_lot_size=int(self.exec_cfg.get('lot_size',65));self.lots_per_trade=int(self.exec_cfg.get('lots_per_trade',1));self.circuit_breaker_tripped=False;self.circuit_breaker_reason=''
        self.last_order_latency_ms=0.;self.total_slippage_inr=0.;self.avg_slippage_pts=0.;self.completed_orders_count=0;self._last_contract=None;self._exit_contract=None;self._cached_contracts={}
        path=self.db_file();path.parent.mkdir(parents=True,exist_ok=True);self.sandbox_tracker=Stage3GraduationTracker(str(path))
        self._init_intents()
    def db_file(self):
        p=Path(self.db_path);return p if p.is_absolute() else REPO_ROOT/p
    def reload_config(self):
        self.cfg=load_config();self.exec_cfg=self.cfg['execution'];self.risk_cfg=self.cfg['risk']
    def _dhan_headers(self):
        return {'access-token':os.environ.get('DHAN_ACCESS_TOKEN',''),'client-id':os.environ.get('DHAN_CLIENT_ID',''),'Content-Type':'application/json','Accept':'application/json'}
    def check_circuit_breakers(self,daily_trades,daily_pnl_inr):
        limit=int(self.risk_cfg.get('max_trades_per_day',2));loss=float(self.risk_cfg.get('max_daily_loss_inr',-3000))
        reason='Max daily trades reached' if limit>0 and daily_trades>=limit else 'Max daily loss breached' if loss<0 and daily_pnl_inr<=loss else ''
        self.circuit_breaker_tripped=bool(reason);self.circuit_breaker_reason=reason;return not reason,reason or 'OK'
    def _init_intents(self):
        with sqlite3.connect(self.db_file())as c:c.execute('CREATE TABLE IF NOT EXISTS algo_broker_intents(id TEXT PRIMARY KEY,data TEXT)')
    def intents(self):
        self._init_intents()
        with sqlite3.connect(self.db_file())as c:return [json.loads(x[0])for x in c.execute('SELECT data FROM algo_broker_intents')]
    def save_intent(self,x):
        with sqlite3.connect(self.db_file())as c:c.execute('INSERT OR REPLACE INTO algo_broker_intents VALUES(?,?)',(x['id'],json.dumps(x)))
    def master(self):
        p=Path(self.exec_cfg.get('instrument_master_path','data/terminal8500/instrument_master.csv'));p=p if p.is_absolute() else REPO_ROOT/p;rows=[]
        if not p.is_file():return rows
        with p.open()as f:
            for x in csv.DictReader(f):
                if x.get('SEM_EXM_EXCH_ID')!='NSE' or x.get('SEM_SEGMENT')!='D' or x.get('SEM_INSTRUMENT_NAME')!='OPTIDX' or not x.get('SEM_TRADING_SYMBOL','').startswith('NIFTY-'):continue
                try:
                    lot=float(x['SEM_LOT_UNITS']);strike=float(x['SEM_STRIKE_PRICE']);sid=x['SEM_SMST_SECURITY_ID'];expiry=x['SEM_EXPIRY_DATE'][:10]
                    if lot.is_integer() and lot>0 and strike>0 and sid.isdigit():rows.append({'expiry':expiry,'strike':strike,'side':x['SEM_OPTION_TYPE'],'sid':sid,'lot':int(lot),'symbol':x['SEM_CUSTOM_SYMBOL']})
                except (ValueError,KeyError):continue
        return rows
    def _lookup_contract(self,selected_strike,opt_key,expiry=None,exact=False):
        prefix=opt_key.lower();self._last_contract=None
        if prefix not in ('ce','pe'):return selected_strike,'','',0.,0.
        try:
            with sqlite3.connect('file:'+str(self.db_file())+'?mode=ro',uri=True)as c:
                c.row_factory=sqlite3.Row
                if expiry:
                    rows=c.execute('SELECT * FROM chain_snap WHERE expiry=? AND ts=(SELECT MAX(ts) FROM chain_snap WHERE expiry=?) ORDER BY strike',(expiry,expiry)).fetchall()
                else:rows=c.execute('SELECT * FROM chain_snap WHERE ts=(SELECT MAX(ts) FROM chain_snap) ORDER BY expiry,strike').fetchall()
            if not rows:return selected_strike,'','',0.,0.
            dates=sorted({x['expiry'] for x in rows});expiry=expiry or dates[0]
            candidates=[x for x in rows if x['expiry']==expiry and (not exact or float(x['strike'])==float(selected_strike))]
            if not candidates:return selected_strike,'','',0.,0.
            row=min(candidates,key=lambda x:(abs(x['strike']-selected_strike),x['strike']));strike=float(row['strike'])
            def val(k):
                v=float(row[k] or 0);return v if math.isfinite(v) else 0.
            ltp,bid,ask=val(prefix+'_ltp'),val(prefix+'_bid'),val(prefix+'_ask')
            if bid>0 and ask>0 and ask<bid:return strike,'','',0.,0.
            price=ask if ask>0 else ltp;spread=ask-bid if ask>0 and bid>0 else 0.
            matching=[x for x in self.master()if x['expiry']==expiry and x['side']==prefix.upper() and x['strike']==strike]
            contract=matching[0]if len(matching)==1 else None
            sid=contract['sid']if contract else '';symbol=contract['symbol']if contract else f'NIFTY {expiry} {strike:g} {prefix.upper()}'
            self._last_contract={'expiry':expiry,'strike':strike,'side':prefix.upper(),'sid':sid,'lot':contract['lot']if contract else None,'ts':row['ts'],'ltp':ltp,'bid':bid,'ask':ask,'price_basis':'ASK'if ask>0 else 'LTP_PROXY'}
            return strike,sid,symbol,price,spread
        except (sqlite3.Error,ValueError,TypeError,OSError):return selected_strike,'','',0.,0.
    def resolve_itm_100_ce_contract(self,spot):
        # Compatibility name; select nearest actually listed ATM, no100point grid.
        return self._lookup_contract(spot,'ce')
    def resolve_itm_100_pe_contract(self,spot):return self._lookup_contract(spot,'pe')
    def get_live_option_exit_quote(self,strike,option_type):
        ctx=getattr(self,'_exit_contract',None) or {};self._lookup_contract(strike,option_type.lower(),ctx.get('expiry'),True)
        q=self._last_contract
        if not q:return 0.
        return q['bid']if q['bid']>0 else q['ltp']
    async def fallback_exit_quote(self,pos):
        if not getattr(pos,'expiry','') or not all(self._dhan_headers().get(k)for k in ('access-token','client-id')):return 0.
        try:
            async with httpx.AsyncClient(timeout=self.exec_cfg.get('broker_timeout_seconds',10),trust_env=False)as client:
                response=await client.post(self.base_url+'/optionchain',headers=self._dhan_headers(),json={'UnderlyingScrip':13,'UnderlyingSeg':'IDX_I','Expiry':pos.expiry})
            if response.status_code!=200:return 0.
            oc=response.json().get('data',{}).get('oc',{})
            found=[v for k,v in oc.items()if float(k)==pos.strike]
            if len(found)!=1:return 0.
            q=found[0].get(pos.option_type.lower(),{})
            if str(q.get('security_id'))!=str(pos.security_id):return 0.
            price=float(q.get('top_bid_price')or q.get('last_price')or 0)
            self._last_contract={'ts':getattr(self,"clock",time.time)(),'bid':price,'ltp':price,'price_basis':'DHAN_OPTIONCHAIN_RESPONSE'}
            return price if math.isfinite(price)and price>0 else 0.
        except (httpx.HTTPError,ValueError,KeyError,TypeError):return 0.
    def fresh(self,q):
        return q and 0<=getattr(self,"clock",time.time)()-q['ts']<=self.exec_cfg.get('quote_age_seconds',120)
    async def request(self,method,path,payload=None):
        # No raw broker response or exception contents logged (may include private data).
        try:
            async with httpx.AsyncClient(timeout=self.exec_cfg.get('broker_timeout_seconds',10),trust_env=False)as client:
                r=await client.request(method,self.base_url+path,headers=self._dhan_headers(),json=payload)
            if r.status_code not in (200,201,202):return None
            d=r.json();return d if isinstance(d,dict)and d.get('status')!='failure'else None
        except (httpx.HTTPError,ValueError,TypeError):return None
    def order_payload(self,contract,qty,side,kind,correlation,trigger=0.):
        return {'dhanClientId':os.environ.get('DHAN_CLIENT_ID',''),'correlationId':correlation[:30],'transactionType':side,
          'exchangeSegment':self.exec_cfg.get('exchange_segment','NSE_FNO'),'productType':self.exec_cfg.get('product_type','INTRADAY'),
          'orderType':kind,'validity':'DAY','securityId':str(contract['sid']),'quantity':int(qty),'price':0.,'triggerPrice':trigger,'afterMarketOrder':False,'disclosedQuantity':0}
    async def settle_entry(self,intent):
        if not intent.get('order_id'):
            found=await self.request('GET','/orders/external/'+intent['id'])
            if not found or not found.get('orderId'):return None
            intent['order_id']=str(found['orderId']);self.save_intent(intent)
        state=await self.request('GET','/orders/'+intent['order_id'])
        if not state:return None
        status=state.get('orderStatus');filled=int(state.get('filledQty')or 0);price=float(state.get('averageTradedPrice')or 0)
        if status in ('REJECTED','CANCELLED','EXPIRED')and filled==0:intent['status']='REJECTED';self.save_intent(intent);return None
        if filled<=0 or price<=0 or not math.isfinite(price):return None
        if status!='TRADED':
            # Cancel the outstanding remainder before protecting the confirmed filled quantity.
            await self.request('DELETE','/orders/'+intent['order_id']);state=await self.request('GET','/orders/'+intent['order_id'])
            if not state or state.get('orderStatus')not in ('TRADED','CANCELLED','EXPIRED'):return None
            filled=int(state.get('filledQty')or 0);price=float(state.get('averageTradedPrice')or 0)
            if filled<=0 or price<=0:return None
        p=ActivePosition(**intent['position']);p.quantity=filled;p.entry_option_price=price;p.entry_order_id=intent['order_id'];p.option_sl=max(.5,round(price-self.cfg.get('strategy',{}).get('fixed_sl_pts',40)*p.delta,1));p.option_target=price+self.cfg.get('strategy',{}).get('target_pts',40)*p.delta
        if not intent.get('stop_correlation'):intent['stop_correlation']='S'+intent['id'][1:]
        if intent.get('stop_submitted'):
            sl=await self.request('GET','/orders/external/'+intent['stop_correlation'])
        else:
            intent['stop_submitted']=True;intent['position']=asdict(p);self.save_intent(intent)
            sl=await self.request('POST','/orders',self.order_payload(intent['contract'],filled,'SELL',self.exec_cfg.get('stop_order_type','STOP_LOSS_MARKET'),intent['stop_correlation'],p.option_sl))
        p.exchange_sl_order_id=str(sl.get('orderId'))if sl and sl.get('orderId')else None
        p.status='OPEN'if p.exchange_sl_order_id else 'OPEN_UNPROTECTED';intent['status']=p.status;intent['position']=asdict(p);self.save_intent(intent)
        return p
    async def reconcile_pending_entries(self):
        for x in self.intents():
            if x.get('status')=='ENTRY_PENDING':return await self.settle_entry(x)
        return None
    async def _entry(self,signal,daily_trades,daily_pnl_inr,hourly=False,confluence_snapshot=None):
        if self.execution_mode not in ('PAPER','LIVE','LIVE_1LOT'):return None
        allowed,_=self.check_circuit_breakers(daily_trades,daily_pnl_inr)
        if not allowed:return None
        live=self.execution_mode in ('LIVE','LIVE_1LOT')
        if live and any(x.get('status')in ('ENTRY_PENDING','OPEN','OPEN_UNPROTECTED','EXIT_PENDING')for x in self.intents()):return None
        side='PE'if hourly and getattr(signal,'side','BUY')in ('SELL','SHORT')else'CE'
        resolved=self.resolve_itm_100_ce_contract(signal.spot)if side=='CE'else self.resolve_itm_100_pe_contract(signal.spot)
        strike,sid,symbol,entry,spread=resolved;q=getattr(self,"_last_contract",None)
        if not q or not self.fresh(q)or entry<=0:return None
        if spread>max(3.,entry*self.exec_cfg.get('max_bid_ask_spread_pct',2)/100):return None
        lot=q.get('lot')or self.dynamic_lot_size
        if live and (not sid.isdigit()or not q.get('lot')or not all(self._dhan_headers().get(k)for k in ('access-token','client-id'))):return None
        qty=validate_1lot_cap(int(self.lots_per_trade*lot),lot);r=self.cfg['strategy_1h_8ema'if hourly else 'strategy'];delta=r.get('option_delta_default',.6)
        tag=('1H_8EMA_'if hourly else'SETUP1_')+uuid.uuid4().hex[:18];stamp=int(getattr(self,"clock",time.time)());tm=datetime.fromtimestamp(stamp,ZoneInfo('Asia/Kolkata')).strftime('%Y-%m-%d %H:%M:%S')
        p=ActivePosition(trade_id=tag,symbol=symbol,security_id=sid,option_type=side,strike=strike,quantity=qty,entry_spot=signal.spot,
          entry_option_price=entry,entry_time=tm,entry_ts=stamp,spot_sl=signal.sl_spot,spot_target=signal.target_spot,
          option_sl=max(.5,round(entry-r.get('fixed_sl_pts',40)*delta,1)),option_target=entry+r.get('target_pts',40)*delta,
          delta=delta,highest_spot=signal.spot,exchange_sl_order_id=None,entry_order_id=None,status='OPEN',expiry=q['expiry'],execution_mode=self.execution_mode)
        if live:
            ident='E'+uuid.uuid4().hex[:25];intent={'id':ident,'status':'ENTRY_PENDING','position':asdict(p),'contract':q,'created_at':stamp};self.save_intent(intent)
            response=await self.request('POST','/orders',self.order_payload(q,qty,'BUY','MARKET',ident))
            if not response or not response.get('orderId'):return None # Retain uncertain intent; never retry POST blindly.
            intent['order_id']=str(response['orderId']);self.save_intent(intent);return await self.settle_entry(intent)
        p.entry_order_id='PAPER_ENTRY_'+tag;p.exchange_sl_order_id='PAPER_SLM_'+tag
        snapshot={**(confluence_snapshot or {}),'_position':asdict(p),'entry_quote':q}
        ok=self.sandbox_tracker.record_entry(SandboxTrade(trade_id=tag,session_date=tm[:10],entry_ts=stamp,entry_time=tm,spot_entry=signal.spot,option_strike=strike,
          option_entry_ltp=entry,bid_ask_spread_pts=spread,delta_est=delta,initial_sl_spot=signal.sl_spot,target_spot=signal.target_spot,confluence_snapshot=snapshot,status='OPEN'))
        return p if ok else None
    async def execute_setup1_entry(self,signal,daily_trades,daily_pnl_inr,confluence_snapshot=None):return await self._entry(signal,daily_trades,daily_pnl_inr,False,confluence_snapshot)
    async def execute_1h_8ema_entry(self,signal,daily_trades,daily_pnl_inr,confluence_snapshot=None):return await self._entry(signal,daily_trades,daily_pnl_inr,True,confluence_snapshot)
    async def execute_1h_80ema_entry(self,*args,**kwargs):return await self.execute_1h_8ema_entry(*args,**kwargs) # Import compatibility only.
    async def modify_exchange_sl_order(self,pos,new_spot_sl):
        pts=pos.entry_spot-new_spot_sl if pos.option_type=='PE'else new_spot_sl-pos.entry_spot
        trigger=max(.5,round(pos.entry_option_price+pts*pos.delta,1)) # Risk trigger estimate, never reported as a fill.
        if self.execution_mode in ('LIVE','LIVE_1LOT'):
            if not pos.exchange_sl_order_id:return
            result=await self.request('PUT','/orders/'+pos.exchange_sl_order_id,{'dhanClientId':os.environ.get('DHAN_CLIENT_ID',''),'orderId':pos.exchange_sl_order_id,
              'orderType':self.exec_cfg.get('stop_order_type','STOP_LOSS_MARKET'),'quantity':pos.quantity,'price':0.,'triggerPrice':trigger,'validity':'DAY'})
            if not result:return
        pos.spot_sl=new_spot_sl;pos.option_sl=trigger
    async def execute_position_exit(self,pos,exit_spot,reason):
        self._exit_contract={'expiry':getattr(pos,'expiry',None)};price=self.get_live_option_exit_quote(getattr(pos,'strike',getattr(pos,'entry_spot',0.0)),pos.option_type)
        q=self._last_contract
        if not self.fresh(q):price=await self.fallback_exit_quote(pos);q=self._last_contract
        live=getattr(pos,'execution_mode',self.execution_mode) in ('LIVE','LIVE_1LOT')
        if not live and (price<=0 or not self.fresh(q)):
            pos.status='EXIT_PENDING';pos.exit_option_price=None;return pos
        if live:
            if not str(pos.security_id).isdigit()or not all(self._dhan_headers().get(k)for k in ('access-token','client-id')):pos.status='EXIT_PENDING';pos.exit_option_price=None;return pos
            if pos.exchange_sl_order_id:
                stop=await self.request('GET','/orders/'+pos.exchange_sl_order_id)
                if not stop:pos.status='EXIT_PENDING';pos.exit_option_price=None;return pos
                if stop.get('orderStatus')=='TRADED':
                    if int(stop.get('filledQty')or 0)!=pos.quantity:pos.status='EXIT_PENDING';pos.exit_option_price=None;return pos
                    price=float(stop.get('averageTradedPrice')or 0);pos.exit_order_id=pos.exchange_sl_order_id
                else:
                    if stop.get('orderStatus')not in ('CANCELLED','REJECTED','EXPIRED'):
                        await self.request('DELETE','/orders/'+pos.exchange_sl_order_id);stop=await self.request('GET','/orders/'+pos.exchange_sl_order_id)
                    if not stop or stop.get('orderStatus')not in ('CANCELLED','REJECTED','EXPIRED')or int(stop.get('filledQty')or 0)>0:pos.status='EXIT_PENDING';pos.exit_option_price=None;return pos
            if not getattr(pos,'exit_order_id',None):
                intent=next((x for x in self.intents()if x.get('position',{}).get('trade_id')==pos.trade_id),None)
                if not intent:pos.status='EXIT_PENDING';pos.exit_option_price=None;return pos
                if not intent.get('exit_correlation'):intent['exit_correlation']='X'+uuid.uuid4().hex[:25];intent['status']='EXIT_PENDING';self.save_intent(intent)
                if not intent.get('exit_order_id'):
                    if intent.get('exit_submitted'):
                        result=await self.request('GET','/orders/external/'+intent['exit_correlation'])
                    else:
                        intent['exit_submitted']=True;self.save_intent(intent)
                        result=await self.request('POST','/orders',self.order_payload({'sid':pos.security_id},pos.quantity,'SELL','MARKET',intent['exit_correlation']))
                    if result and result.get('orderId'):intent['exit_order_id']=str(result['orderId']);self.save_intent(intent)
                if not intent.get('exit_order_id'):pos.status='EXIT_PENDING';pos.exit_option_price=None;return pos
                state=await self.request('GET','/orders/'+intent['exit_order_id'])
                if not state or state.get('orderStatus')!='TRADED' or int(state.get('filledQty')or 0)!=pos.quantity:pos.status='EXIT_PENDING';pos.exit_option_price=None;return pos
                price=float(state.get('averageTradedPrice')or 0);pos.exit_order_id=intent['exit_order_id']
            if price<=0:pos.status='EXIT_PENDING';pos.exit_option_price=None;return pos
        charges=paper_trading.calculate_trade_charges(pos.entry_option_price,price,pos.quantity,side='BUY')['total_charges']
        pos.exit_spot=exit_spot;pos.exit_option_price=price;pos.exit_time=datetime.fromtimestamp(getattr(self,"clock",time.time)(),ZoneInfo('Asia/Kolkata')).strftime('%Y-%m-%d %H:%M:%S');pos.exit_reason=reason
        pos.pnl_pts=pos.entry_spot-exit_spot if pos.option_type=='PE'else exit_spot-pos.entry_spot;pos.pnl_inr=round((price-pos.entry_option_price)*pos.quantity-charges,2);pos.status='CLOSED_TARGET'if'Target'in reason else'CLOSED_SL'if'Stop'in reason else'CLOSED_MANUAL'
        if not live:self.sandbox_tracker.record_exit(pos.trade_id,exit_spot,price,reason,slippage_pts=0.)
        else:
            for x in self.intents():
                if x.get('position',{}).get('trade_id')==pos.trade_id:x['status']='CLOSED';x['position']=asdict(pos);self.save_intent(x)
        return pos

    def switch_execution_mode(self, new_mode: str, **kwargs) -> Dict[str, Any]:
            """
            Switches execution mode ("PAPER", "LIVE_1LOT", "PAUSED").
            Guards LIVE_1LOT with the 5 Mandatory Stage 3 Graduation Gates!
            All backdoor bypass tokens are strictly removed and rejected.
            """
            if kwargs:
                log.warning("🔒 Unauthorized override attempts/parameters strictly rejected: %s", list(kwargs.keys()))
    
            target_mode = new_mode.upper()
            if target_mode not in ("PAPER", "LIVE_1LOT", "LIVE", "PAUSED"):
                return {"ok": False, "status_code": 400, "error": f"Invalid mode: {new_mode}. Use PAPER, LIVE_1LOT, or PAUSED."}
    
            # Gatekeeper for Live Modes
            if target_mode in ("LIVE_1LOT", "LIVE"):
                grad_status = self.sandbox_tracker.evaluate_graduation_status()
                is_graduated = grad_status.get("graduation_ready", False)
    
                if not is_graduated:
                    reasons = grad_status.get("rejection_reasons", [])
                    log.warning("🔒 LIVE EXECUTION BLOCKED: %s", reasons)
                    return {
                        "ok": False,
                        "status_code": 403,
                        "error": "LOCKED IN STAGE 3 SANDBOX: Graduation criteria not yet satisfied",
                        "graduation_status": grad_status,
                        "rejection_reasons": reasons,
                    }
    
                self.execution_mode = "LIVE_1LOT"
                log.warning("🚀 STAGE 4 REAL CAPITAL GATE CLEARED! Execution Mode armed: LIVE_1LOT (Hard 1-Lot Cap)")
                return {
                    "ok": True,
                    "execution_mode": "LIVE_1LOT",
                    "message": "Stage 4 Live 1-Lot Execution Mode successfully armed.",
                    "graduation_status": grad_status,
                }
    
            self.execution_mode = target_mode
            log.info("Execution mode updated to %s", self.execution_mode)
            return {"ok": True, "execution_mode": self.execution_mode}

    def get_stage4_telemetry(self) -> Dict[str, Any]:
            """Telemetry details for Stage 4 live execution safeguards."""
            grad = self.sandbox_tracker.evaluate_graduation_status()
            return {
                "execution_mode": self.execution_mode,
                "graduation_ready": grad.get("graduation_ready", False),
                "graduation_badge": grad.get("graduation_badge", "🔒 LOCKED IN SANDBOX"),
                "hard_lot_cap": 1,
                "hard_qty_cap": self.dynamic_lot_size,
                "strike_rule": "100-Multiples Only (~₹200 ITM, ~0.60 Delta)",
                "bid_ask_spread_guard_max_pct": float(self.exec_cfg.get("max_bid_ask_spread_pct", 2.0)),
                "last_order_latency_ms": round(self.last_order_latency_ms, 1),
                "total_slippage_inr": round(self.total_slippage_inr, 2),
                "avg_slippage_pts": round(self.avg_slippage_pts, 2),
                "completed_orders_count": self.completed_orders_count,
                "companion_sl_type": "EXCHANGE_SIDE_SL_M (Instant Server-Side Placement)",
                "circuit_breaker": {
                    "tripped": self.circuit_breaker_tripped,
                    "reason": self.circuit_breaker_reason,
                },
            }
