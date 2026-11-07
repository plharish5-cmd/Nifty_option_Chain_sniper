"""Private offline observer tests; no credentials, app startup or outbound HTTP."""
import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from analyst.dry_evidence_8510 import DryEvidence, IST
from datetime import datetime


class DryEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.service=DryEvidence(Path(self.tmp.name)/'evidence.sqlite3')
        self.now=datetime(2026,11,6,10,tzinfo=IST).timestamp()
        self.quote={'bid':200.0,'ask':200.5,'last_price':200.25};self.requests=[];self.recorded=[]
        async def depth(ids):self.requests.append(ids);return {'101':self.quote,'102':self.quote}
        async def forbidden(*args,**kwargs):raise AssertionError('BROKER ORDER METHOD CALLED')
        self.backend=SimpleNamespace(cached_telemetry={},strategy_status={f:dict(name=f,state='SCANNING') for f in self.service.cfg()['families']},
            kill_switch_active=False,scanner_enabled=True,active_party='BULLISH',directional_bias='BOTH',
            cfg={'execution_safety_v194':{'live_validation_status':'NO_GO','live_dry_run':True}},
            algo_strike_selection={'LONG_CE_ATM':True,'LONG_PE_ATM':True,'LONG_CE_ITM':False},
            priority_contracts=[dict(id='LONG_CE_ATM',security_id='101',symbol='TEST CE',lot_size=65),dict(id='LONG_PE_ATM',security_id='102',symbol='TEST PE',lot_size=65)],
            broker=SimpleNamespace(get_marketfeed_quote=depth,place_order=forbidden,modify_order=forbidden,cancel_order=forbidden))
        def record(*args):self.recorded.append(args);return dict(order_id=args[6],intended_limit_price=204.25)
        self.backend._record_dry_run_order=record
        self.refresh(self.now)

    def refresh(self,now):
        self.backend._last_scanner_source_ts=now-30
        self.backend.cached_telemetry=dict(timestamp_ist=datetime.fromtimestamp(now,IST).strftime('%Y-%m-%d %H:%M:%S IST'),
            spot=dict(price=24000,is_stale=False),market_status=dict(is_open=True),
            trend_watchlist=dict(items=[dict(name='ADX',reading='ADX 45.4',status='BULLISH')]),dhan_oi=dict(pcr=1.64))

    def signal(self,sid='STAGE_2_LONG',side='CE'):
        return dict(strategy_id=sid,setup_name=sid,opt_side=side,target_pts=40,sl_pts=40)

    def observe(self,signals,now=None):asyncio.run(self.service.observe(self.backend,signals,now or self.now))

    def test_nogo_can_collect_without_arming_or_orders(self):
        before=copy.deepcopy(self.backend.cfg)
        self.observe([self.signal()])
        self.assertEqual(len(self.recorded),1);self.assertEqual(self.backend.cfg,before)
        self.assertEqual(self.service.status()['families']['stage2_long']['valid_spreads'],1)

    def test_all_four_families_have_separate_measurements(self):
        signals=[self.signal('STAGE_2_LONG'),self.signal('STAGE_3_SHORT','PE'),self.signal('CROSSOVER_BULLISH'),self.signal('H1_CONFLUENCE_SUPPORT')]
        for offset in (0,3,6,9):
            self.refresh(self.now+offset);self.observe(signals,self.now+offset)
        self.assertEqual(len(self.recorded),4)
        for stats in self.service.status()['families'].values():self.assertEqual(stats['valid_spreads'],1)

    def test_blocked_setup_is_observed_not_forced(self):
        self.backend.strategy_status['stage3_short']['state']='BLOCKED_MA_STACK'
        self.observe([])
        self.assertEqual(self.requests,[])
        rows=self.service.status()['rows'];self.assertEqual(len(rows),4)
        self.assertTrue(any(r['reason']=='BLOCKED_MA_STACK' for r in rows))

    def test_missing_crossed_or_nonfinite_depth_never_qualifies(self):
        for i,quote in enumerate([{},dict(bid=201,ask=200,last_price=200),dict(bid=float('nan'),ask=201,last_price=200)]):
            now=self.now+i*301;self.refresh(now);self.quote=quote;self.observe([self.signal()],now)
        self.assertEqual(self.recorded,[])
        self.assertEqual(self.service.status()['families']['stage2_long']['valid_spreads'],0)

    def test_stale_telemetry_and_stale_candles_cannot_measure(self):
        self.backend.cached_telemetry['spot']['is_stale']=True;self.observe([self.signal()])
        self.refresh(self.now+61);self.backend._last_scanner_source_ts=self.now-1000
        self.observe([self.signal()],self.now+61);self.assertEqual(self.requests,[])

    def test_weekend_and_kill_gate_measurements(self):
        self.backend.kill_switch_active=True;self.observe([self.signal()])
        self.backend.kill_switch_active=False;weekend=self.now+86400;self.refresh(weekend)
        self.observe([self.signal()],weekend);self.assertEqual(self.requests,[])

    def test_restart_dedup_and_same_direction_cooldown(self):
        self.observe([self.signal()]);self.service=DryEvidence(self.service.path)
        self.observe([self.signal()]);self.assertEqual(len(self.requests),1)
        self.assertEqual(self.service.status()['families']['stage2_long']['triggers'],1)

    def test_disabled_contract_cannot_measure(self):
        self.backend.algo_strike_selection['LONG_CE_ATM']=False
        self.observe([self.signal()]);self.assertEqual(self.requests,[]);self.assertEqual(self.recorded,[])


if __name__=='__main__':unittest.main()
