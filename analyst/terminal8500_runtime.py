"""8500 integration: feed, shared scanners, date replay and broker reconciliation."""
import csv,io,json,logging,math,queue,threading,time,hashlib
from datetime import datetime,date,timedelta
from zoneinfo import ZoneInfo
import terminal8500_strategies as strategies
import terminal8500_hourly as hourly
from terminal8500_market import CandleStore,Feed
from terminal8500_execution import Execution
from terminal8500_paperdesk import Paperdesk
IST=ZoneInfo('Asia/Kolkata')
LOG=logging.getLogger('terminal8500.runtime')

def install(module):
    Original=module.Engine
    class Engine(Original):
        def __init__(self,*args,**kwargs):
            super().__init__(*args,**kwargs)
            self.store=CandleStore(self.root);self.ticks=queue.Queue(maxsize=module.CFG['max_tick_queue'])
            self.feed=Feed(module.CFG,self.on_tick);self.feed.select('spot','IDX_I','13')
            self.paperdesk=Paperdesk(self,module.CFG)
            self.execution=Execution(self,module.CFG);self.status='Paper scanning enabled; waiting for fresh session ticks' if self.enabled else 'Paper entries paused';self.minutes=[];self.bars=None;self.decisions=[];self.quality={}
            self.jobs={};self.next_features=0;self.next_reconcile=0;self.previous_tick=None;self.bar_open={};self.contracts_cache=[]
            self.settings_path=self.data/'scanner_settings.json'
            self.enabled_setups=set(module.CFG['enabled_setups'])
            if self.settings_path.exists():self.enabled_setups=set(json.loads(self.settings_path.read_text())['enabled_setups'])
            self.minute_overlay={};self.replay_lock=threading.Lock();self.master_retry=0;self.history_retry=0
        def submit(self,body):
            if self.execution.armed:raise module.Problem('Disarm automatic execution before using the manual route',403)
            with self.connect() as c:
                if c.execute("SELECT count(*) FROM intents WHERE state IN ('SUBMITTING','UNKNOWN_RECONCILE')").fetchone()[0]:raise module.Problem('Reconcile the previous unknown manual order before a new submission',409)
            return super().submit(body)
        def has_pending_manual(self):
            with self.connect() as c:return bool(c.execute("SELECT count(*) FROM intents WHERE state IN ('SUBMITTING','UNKNOWN_RECONCILE','BROKER_RESPONSE','PENDING','TRANSIT','PART_TRADED','TRIGGERED')").fetchone()[0])
        def on_tick(self,segment,quote):
            if segment=='IDX_I' and quote['securityId']=='13':
                try:self.ticks.put_nowait(quote)
                except queue.Full:self.execution.armed=False;self.status='Tick queue overflow; automatic entries halted'
        def contracts(self):
            self.contracts_cache=self.master.rows()
            spot=self.last_spot
            if spot:
                self.paperdesk.refresh(self.contracts_cache)
                for kind,label in [('CE','entry_ce'),('PE','entry_pe')]:
                    contract=self.paperdesk.choose(kind,self.contracts_cache)
                    if contract:self.feed.select(label,'NSE_FNO',contract['securityId'])
            # Resolve the real futures contract. Never relabel spot/FUT proxy as a contract LTP.
            if not self.master.path.exists():return
            rows=[]
            for r in csv.DictReader(io.StringIO(self.master.path.read_text())):
                get=lambda a,b:str(r.get(a,r.get(b,'')) or '').strip()
                if get('EXCH_ID','SEM_EXM_EXCH_ID')!='NSE' or get('INSTRUMENT','SEM_INSTRUMENT_NAME')!='FUTIDX':continue
                symbol=get('DISPLAY_NAME','SEM_CUSTOM_SYMBOL');under=get('UNDERLYING_SYMBOL','SM_SYMBOL_NAME')
                if under!='NIFTY' and not symbol.startswith('NIFTY '):continue
                expiry=get('SM_EXPIRY_DATE','SEM_EXPIRY_DATE')[:10];sid=get('SECURITY_ID','SEM_SMST_SECURITY_ID')
                if expiry>=datetime.now(IST).date().isoformat() and sid.isdigit():rows.append((expiry,sid))
            if rows:self.feed.select('futures','NSE_FNO',min(rows)[1])
        def refresh_features(self):
            self.minutes,self.quality=self.store.read(days=module.CFG['history_days'])
            now=time.time();known=[r for r in self.minutes if r[0]+60<=now]
            self.bars=strategies.features(known,module.CFG)
            self.decisions=[strategies.evaluate(self.bars,s,module.CFG) for s in range(4)]
            if self.last_spot:
                stopped=any(t.get('setup_id')==4 and t['day']==datetime.now(IST).date().isoformat() and t.get('reason')=='SPOT_STOP' for t in self.paper())
                self.decisions.append(hourly.evaluate(known,self.last_spot,now,module.CFG,self.root,stopped))
            else:self.decisions.append(dict(setup_id=4,setup='Flagship1H80 EMA',eligible=False,reasons=['Waiting for a fresh spot tick']))
            for decision in self.decisions:
                if decision['setup_id'] not in self.enabled_setups:
                    decision['eligible']=False;decision['reasons'].append('Disabled by setup selector')
            self.contracts()
        def paper_exit(self,spot,snapshot,now):
            self.paperdesk.process_manual(spot,snapshot,now)
            for t in self.paper():
                if t['status']!='OPEN' or t.get('manual'):continue
                label='position_'+t['securityId'];self.feed.select(label,'NSE_FNO',t['securityId'])
                q=snapshot['quotes'].get(label)
                if not q or not q['fresh']:self.status='Open paper position: option quote stale/missing; exit not fabricated';continue
                premium=self.paperdesk.exit_price(q);move=(spot-t['entrySpot'])*t['side'];risk=t.get('spot_risk',module.CFG['reversal_risk']);target=t.get('spot_target',risk)
                t.update(currentPremium=premium,currSpot=spot,grossPnl=(premium-t['entryPremium'])*t['quantity'],quoteReceivedAt=now.isoformat())
                if t['day']!=now.date().isoformat() or now.strftime('%H:%M')>=module.CFG['force_exit'] or move<=-risk or move>=target:
                    t.update(status='CLOSED',exitPremium=premium,exitTime=now.isoformat(),reason='SESSION_EXIT' if t['day']!=now.date().isoformat() or now.strftime('%H:%M')>=module.CFG['force_exit'] else 'SPOT_STOP' if move<0 else 'SPOT_TARGET')
                self.save(self.paperdesk.settle(t))
        def process_tick(self,q):
            now=datetime.now(IST);stamp=q.get('exchange_ts') or q['received_at'];spot=q['ltp']
            if not 0<=time.time()-stamp<=module.CFG['quote_max_age_seconds'] or time.time()-q['received_at']>module.CFG['quote_max_age_seconds']:return
            self.last_spot=spot
            snap=self.feed.snapshot();observed=snap['quotes'].get('spot',{})
            if not observed.get('fresh') or now.weekday()>=5 or not module.CFG['market_open']<=now.strftime('%H:%M')<module.CFG['market_close']:return
            self.paper_exit(spot,snap,now)
            opening_history=False
            if self.minutes and now.strftime('%H:%M')==module.CFG['market_open']:
                last=datetime.fromtimestamp(self.minutes[-1][0],IST)
                same_prior=[r for r in self.minutes[-375:] if datetime.fromtimestamp(r[0],IST).date()==last.date()]
                opening_history=last.strftime('%H:%M')=='15:29' and len(same_prior)==375 and 0<(now.date()-last.date()).days<=module.CFG['opening_history_max_days']
            if not self.minutes or (time.time()-self.minutes[-1][0]>module.CFG['max_spot_age_seconds'] and not opening_history):
                self.status='Recorded candle source stale; entries blocked; feed display continues';self.previous_tick=spot;return
            if self.quality.get('conflicting_minutes'):self.status='Conflicting local/API candles; entries blocked';return
            if not self.enabled and not self.execution.armed:self.status='Scanners visible; entry execution paused';self.previous_tick=spot;return
            current_slot=int((now.hour*60+now.minute-555)//module.CFG['bar_minutes'])
            key=(now.date().isoformat(),current_slot);self.bar_open.setdefault(key,spot)
            ledger=self.paper();openpaper=sum(t['status']=='OPEN' for t in ledger)
            for s in sorted(self.decisions,key=lambda x:module.CFG['strategy_priority'].index(x['setup_id'])):
                if not s['eligible']:continue
                sid=s['setup_id']
                if sid not in self.enabled_setups:continue
                cutoff='15:00' if sid==4 else module.CFG['last_entry']
                if now.strftime('%H:%M')>=cutoff:continue
                if any(t.get('event_id')==s['event_id'] or (t.get('setup_id')==sid and t.get('regime')==s['regime']) for t in ledger):continue
                if sid in (0,1,4) and any(t.get('setup_id')==sid and t['day']==now.date().isoformat() and t.get('reason')=='SPOT_STOP' for t in ledger):continue
                if sid<4:
                    anchor=s['signal_close']
                    if current_slot==0 and s['signal_day']<now.date().isoformat():
                        anchor=int(now.replace(hour=9,minute=15,second=0,microsecond=0).timestamp())
                    age=stamp-anchor
                    if not 0<=age<module.CFG['bar_minutes']*60:continue
                    if s['entry_kind']=='NEXT_OPEN':
                        if age>module.CFG['entry_late_seconds']:
                            s['runtime_reason']='Missed entry window; inspect date replay, no retrospective live fill';continue
                    else:
                        level=s['entry_level']
                        if s['side']*(self.bar_open[key]-level)<=0:continue
                        if self.previous_tick is None or not min(self.previous_tick,spot)<=level<=max(self.previous_tick,spot):continue
                kind='CE' if s['side']==1 else 'PE';label='entry_ce' if kind=='CE' else 'entry_pe'
                option=snap['quotes'].get(label)
                if not option or not option['fresh']:s['runtime_reason']='Selected entry option quote not fresh';continue
                contract=self.paperdesk.choose(kind,self.contracts_cache)
                if not contract or contract['securityId']!=option['securityId']:
                    s['runtime_reason']='No fresh8510 bucket contract; no ATM substitution';continue
                entry_premium=self.paperdesk.entry_price(option)
                settings=self.paperdesk.settings()
                if not settings['bucket_min']<=entry_premium<=settings['bucket_max']:
                    s['runtime_reason']='Stream ask moved outside8510 bucket';continue
                if self.execution.armed:self.execution.submit(s,contract,entry_premium)
                if self.enabled and openpaper<module.CFG['max_paper_positions']:
                    self.save(dict(id=s['event_id'],event_id=s['event_id'],regime=s['regime'],setup_id=sid,day=now.date().isoformat(),entryTime=now.isoformat(),
                        setup=s['setup'],side=s['side'],entrySpot=spot,currSpot=spot,entryPremium=entry_premium,currentPremium=entry_premium,grossPnl=0,status='OPEN',
                        quantity=contract['lotSize']*module.CFG['paper_lots'],spot_risk=s['risk'],spot_target=s.get('target_risk',s['risk']),
                        source='8510 bucket selection; ask entry/bid exit; same8510 charges',signalClose=s['signal_close'],**contract))
                    self.feed.select('position_'+contract['securityId'],'NSE_FNO',contract['securityId']);openpaper+=1
                self.status='Processed '+s['setup']+'; paper/live mode shown separately'
            self.previous_tick=spot
        def loop(self):
            feedthread=threading.Thread(target=self.feed.run,daemon=True);feedthread.start()
            try:
                while not self.quit.is_set():
                    try:
                        if time.time()>=self.master_retry and (not self.master.path.exists() or time.time()-self.master.path.stat().st_mtime>module.CFG['max_master_age_hours']*3600):
                            self.master_retry=time.time()+module.CFG['master_retry_seconds']
                            try:self.master.refresh()
                            except module.Problem as ex:self.status=str(ex)
                        if time.time()>=self.next_features:
                            self.next_features=time.time()+module.CFG['feature_refresh_seconds']
                            self.refresh_features()
                            clock=datetime.now(IST)
                            if module.CFG['auto_history_backfill'] and clock.weekday()<5 and module.CFG['market_open']<=clock.strftime('%H:%M')<module.CFG['market_close'] and time.time()>=self.history_retry:
                                expected=int(time.time()//60*60)-60
                                if (not self.minutes or self.minutes[-1][0]<expected) and clock.second>=module.CFG['history_grace_seconds']:
                                    self.history_retry=time.time()+module.CFG['history_retry_seconds']
                                    self.store.backfill(self.broker,clock.date().isoformat())
                                    self.next_features=0
                        while not self.ticks.empty():self.process_tick(self.ticks.get_nowait())
                        if (self.execution.armed or self.execution.intents() or self.has_pending_manual()) and time.time()>=self.next_reconcile:
                            self.next_reconcile=time.time()+module.CFG['reconcile_seconds'];self.execution.sync()
                    except module.Problem as ex:self.status=str(ex)
                    except Exception as ex:
                        self.execution.armed=False;self.status='Worker error ('+type(ex).__name__+'); auto entries halted'
                        LOG.exception('Scanner worker failure')
                    self.quit.wait(module.CFG['worker_seconds'])
            finally:self.feed.stop();feedthread.join(timeout=module.CFG['feed_socket_timeout_seconds']+1)
        def replay_job(self,day,key):
            try:
                with self.replay_lock:
                    parsed=date.fromisoformat(day);end=int(datetime.combine(parsed+timedelta(days=1),datetime.min.time(),IST).timestamp())
                    rows,quality=self.store.read(until=end,days=module.CFG['history_days'])
                    result=strategies.replay(rows,day,module.CFG)
                    result['trades']+=hourly.replay(rows,day,module.CFG,self.root)
                    result['trades'].sort(key=lambda x:x['entry_ts']);result['quality']=quality
                    result['rules']=dict(ema='4/8',reversal_bands=False,stage_bands=True,hourly='existing flagship evaluator')
                    result['gross_index_points']=sum(t['points'] or 0 for t in result['trades'])
                    result['not_option_pnl']=True
                    result['cache_signature']=hashlib.sha256(json.dumps([rows,module.CFG],sort_keys=True).encode()).hexdigest()
                    path=self.data/('replay-'+day+'.json');path.write_text(json.dumps(result,allow_nan=False))
                    self.jobs[key]=dict(status='COMPLETE',result=result)
            except Exception as ex:self.jobs[key]=dict(status='FAILED',error=type(ex).__name__+': '+str(ex))
        def route(self,h,post,body,query,path):
            if not post and path=='/api/algo/terminal':
                h.send_json(dict(paperdesk_parity_version='8510-parity-1',feed=self.feed.snapshot(),scanners=self.decisions,scanner_status=self.status,paper_enabled=self.enabled,
                                 execution=self.execution.snapshot(),quality=self.quality,enabled_setups=sorted(self.enabled_setups)));return True
            if post and path=='/api/algo/scanner/config':
                enabled=body.get('enabled_setups')
                if not isinstance(enabled,list) or any(type(x) is not int or x not in (0,1,2,3,4) for x in enabled):raise module.Problem('Expected setup IDs0–4')
                self.enabled_setups=set(enabled)
                self.settings_path.write_text(json.dumps({'enabled_setups':sorted(self.enabled_setups)}));self.next_features=0
                h.send_json({'enabled_setups':sorted(self.enabled_setups)});return True
            if not post and path=='/api/algo/paperdesk/watch':h.send_json(self.paperdesk.latest);return True
            if post and path=='/api/algo/paperdesk/watch':
                expiry=body.get('expiry')
                if expiry:self.paperdesk.expiry=str(expiry)
                h.send_json(self.paperdesk.refresh(self.master.rows(),force=True));return True
            if not post and path=='/api/algo/ledger':h.send_json(self.paperdesk.ledger());return True
            if post and path=='/api/algo/paperdesk/calculate':
                h.send_json(self.paperdesk.calculation(body.get('entryPremium'),body.get('exitPremium'),body.get('quantity')));return True
            if post and path=='/api/algo/manual/paper':h.send_json(self.paperdesk.manual_order(body));return True
            if not post and path=='/api/algo/manual/paper':h.send_json(self.paperdesk.manual_orders());return True
            if post and path=='/api/algo/manual/paper/cancel':h.send_json(self.paperdesk.cancel_manual(body));return True
            if post and path=='/api/algo/manual/paper/exit':h.send_json(self.paperdesk.close_manual(body));return True
            if not post and path=='/api/algo/history':h.send_json(self.store.audit());return True
            if post and path=='/api/algo/feed/select':
                contract=self.master.resolve(body);self.feed.select('option','NSE_FNO',contract['securityId']);h.send_json(contract);return True
            if post and path=='/api/algo/history/backfill':
                day=date.fromisoformat(str(body.get('day'))).isoformat();key='backfill-'+day
                if self.jobs.get(key,{}).get('status')!='RUNNING':
                    self.jobs[key]={'status':'RUNNING'}
                    def work():
                        try:self.jobs[key]={'status':'COMPLETE','result':self.store.backfill(self.broker,day)}
                        except Exception as ex:self.jobs[key]={'status':'FAILED','error':str(ex)}
                    threading.Thread(target=work,daemon=True).start()
                h.send_json({'job':key,**self.jobs[key]});return True
            if post and path=='/api/algo/replay':
                day=date.fromisoformat(str(body.get('day')))
                if day>datetime.now(IST).date():raise module.Problem('Future replay date rejected')
                key='replay-'+day.isoformat()
                saved=self.data/('replay-'+day.isoformat()+'.json')
                if saved.exists():
                    end=int(datetime.combine(day+timedelta(days=1),datetime.min.time(),IST).timestamp())
                    cachedrows,_=self.store.read(until=end,days=module.CFG['history_days'])
                    sig=hashlib.sha256(json.dumps([cachedrows,module.CFG],sort_keys=True).encode()).hexdigest()
                    cached=json.loads(saved.read_text())
                    if cached.get('cache_signature')==sig:
                        self.jobs[key]={'status':'COMPLETE','result':cached};h.send_json({'job':key,**self.jobs[key]});return True
                if self.jobs.get(key,{}).get('status')!='RUNNING':
                    self.jobs[key]={'status':'RUNNING'};threading.Thread(target=self.replay_job,args=(day.isoformat(),key),daemon=True).start()
                h.send_json({'job':key,**self.jobs[key]});return True
            if not post and path=='/api/algo/job':h.send_json(self.jobs.get(query.get('id',[''])[0],{'status':'NOT_FOUND'}));return True
            if post and path=='/api/algo/live/sync':self.execution.sync();h.send_json(self.execution.snapshot());return True
            if post and path=='/api/algo/live/arm':
                self.execution.arm(body.get('confirmLive'));h.send_json(self.execution.snapshot());return True
            if post and path=='/api/algo/live/disarm':self.execution.armed=False;h.send_json(self.execution.snapshot());return True
            return False
    module.Engine=Engine
