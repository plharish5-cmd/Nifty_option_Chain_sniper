"""Independent falsification probes. Passing defect probes confirm defects, not readiness."""
import asyncio,copy,sqlite3,time
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
import pandas as pd
import pytest
from analyst.terminal8510_backend import QuantTerminalBackend
REAL_ENTRY_GUARD=QuantTerminalBackend.new_position_entry_allowed
from analyst.ema8_prototype_8510 import evaluate_ema8_prototype,evaluate_ema8_prototype_intrabar,load_ema8_prototype_cfg
from analyst.test_arm_persistence_8510 import _backend_with_db


def test_orphan_reservation_survives_restart_and_blocks_both_lanes(tmp_path):
    from analyst.ema_lane_entry_gate_8510 import try_reserve_lane_for_signal
    b1=_backend_with_db(tmp_path)
    assert try_reserve_lane_for_signal(b1._ema_lane_lock,'EMA8_PROTOTYPE_LONG',open_position_count=0)[0]
    b1._save_ema_lane_lock_state()
    b2=_backend_with_db(tmp_path)
    with sqlite3.connect(b2.ledger_db) as c:
        assert c.execute("select count(*) from positions where status='OPEN'").fetchone()[0]==0
    for sid in ['EMA8_PROTOTYPE_LONG','CROSSOVER_BULLISH']:
        assert not try_reserve_lane_for_signal(b2._ema_lane_lock,sid,open_position_count=0)[0]


def test_reconstruction_creates_lock_for_simulated_not_actual_position():
    from analyst.arm_reconstruct_8510 import ReconstructResult,apply_reconstructed_state
    from analyst.ema_lane_entry_gate_8510 import try_reserve_lane_for_signal
    b=SimpleNamespace(_ema8_prototype_state={},_mj_sir_state={},_ema_lane_lock={},
        _save_ema8_prototype_state=lambda:None,_save_mj_sir_runtime_state=lambda:None,
        log_ema8_prototype_event=lambda **kw:None)
    apply_reconstructed_state(b,ReconstructResult(ok=True,proto_arm={'day':'2026-11-19','bt_open':{'exit_ts':9999999999}}))
    assert b._ema_lane_lock['holder']=='PROTOTYPE'
    assert not try_reserve_lane_for_signal(b._ema_lane_lock,'EMA8_PROTOTYPE_LONG',open_position_count=0)[0]


def test_reconstruction_erases_real_stop_block_without_trade_reconciliation():
    from analyst.arm_reconstruct_8510 import ReconstructResult,apply_reconstructed_state
    b=SimpleNamespace(_ema8_prototype_state={'sl_blocked_side':'LONG','day':'2026-11-19'},_mj_sir_state={},_ema_lane_lock={},
        _save_ema8_prototype_state=lambda:None,log_ema8_prototype_event=lambda **kw:None)
    apply_reconstructed_state(b,ReconstructResult(ok=True,proto_arm={'day':'2026-11-19','arm':{'direction':'LONG'}}))
    assert 'sl_blocked_side' not in b._ema8_prototype_state
    assert b._ema8_prototype_state['arm']['direction']=='LONG'


def test_sync_startup_reconstruction_blocks_event_loop():
    from analyst.arm_reconstruct_8510 import reconstruct_arms_on_startup
    def slow(*a):
        time.sleep(.08)
        return {'ok':False}
    async def run():
        start=time.monotonic();observed=[]
        async def heartbeat():
            await asyncio.sleep(.005);observed.append(time.monotonic()-start)
        task=asyncio.create_task(heartbeat())
        reconstruct_arms_on_startup(SimpleNamespace(market_db='/does/not/exist'),'2026-11-19')
        await task
        assert observed[0]>=.08
    with patch('analyst.arm_reconstruct_8510._session_open_now',return_value=(True,'2026-11-19')),patch('analyst.arm_reconstruct_8510.backfill_today_session',side_effect=slow):
        asyncio.run(run())


def test_disabled_prototype_prevents_reversal_startup_reconstruction(monkeypatch):
    b=QuantTerminalBackend.__new__(QuantTerminalBackend)
    b.scanner_cfg={'ema8_prototype':{'enabled':False},'reversal':{'enabled':True}}
    async def stop(_):raise asyncio.CancelledError()
    monkeypatch.setattr(asyncio,'sleep',stop)
    with patch('analyst.arm_reconstruct_8510.reconstruct_arms_on_startup') as recon:
        with pytest.raises(asyncio.CancelledError):asyncio.run(b.start_ema8_prototype_intrabar_worker())
        recon.assert_not_called()


def test_same_trade_can_score_multiple_marked_opportunities():
    from analyst.replay_marked_opportunities_8510 import _match_trade
    trades=[{'hm':'10:31:00','side':'CE','spot':25000}]
    assert _match_trade(trades,'10:30','CE',900) is trades[0]
    assert _match_trade(trades,'10:45','CE',900) is trades[0]


def test_future_exit_changes_current_reconstruction_stop_state():
    from analyst.arm_reconstruct_8510 import _process_touch_signal
    from analyst.test_arm_reconstruct_8510 import T0,DAY
    bars=[{'ts':T0,'day':DAY,'hm':'09:15','e8':100,'close':100}]
    future=pd.DataFrame([{'ts':T0+60,'open':100,'high':101,'low':50,'close':50,'day':DAY,'hm':'09:16'}])
    state={'arm':{'direction':'LONG'}}
    b=SimpleNamespace(scanner_cfg={'ema8_prototype':{'reentry_after_stop_loss':False}})
    sig=('EMA8_PROTOTYPE_LONG','test','LONG',40,40,{'ema8_prototype':True,'entry_ts':T0,'entry_day':DAY,'entry_spot':100})
    _process_touch_signal(backend=b,bars=bars,day_to_1m={DAY:future},proto_state=state,mj_state={},sig=sig,
        eod_hm='15:20',offline_from=T0+30,offline_to=T0+30,now_ts=T0+30)
    assert state['sl_blocked_side']=='LONG'
    assert state['bt_open']['exit_ts']>T0+30


@pytest.mark.parametrize('bullish',[True,False])
@pytest.mark.parametrize('restart',[True,False])
def test_prototype_cross_away_touch_to_actual_paper_order(tmp_path,monkeypatch,bullish,restart):
    from analyst.test_phase2_option_points_8510 import FakeBroker,make_test_backend
    from analyst.nse_spot_candles_8510 import ema4_ema8
    broker=FakeBroker();b=make_test_backend(tmp_path,broker);b._real_money_enabled=False
    monkeypatch.setattr(QuantTerminalBackend,'new_position_entry_allowed',REAL_ENTRY_GUARD)
    b.scanner_cfg={'signal_cooldown_5m_min':0,'skipped_signal_retry_seconds':0,
      'ema8_prototype':{'execution_mode':'PAPER_ONLY','enabled':True},'desk8600_board':{'enabled':False}}
    b._ema_lane_lock={};b.strategy_cooldowns={};b.recent_signals=[];b._skip_retry_at={}
    b.log_signal_observation=lambda *a,**kw:'fixture';b.mark_signal_outcome=lambda *a,**kw:None
    idx=pd.date_range('2026-11-19 09:15',periods=8,freq='15min',tz='Asia/Kolkata')
    step=20. if bullish else -20.
    closes=[25000.]*6+[25000.+step,25000.+step]
    bars=pd.DataFrame({'open':closes,'high':closes,'low':closes,'close':closes,'volume':[100]*8},index=idx)
    bars.iloc[6,bars.columns.get_loc('open')]=25000.
    cfg=load_ema8_prototype_cfg({'ema8_prototype':{'execution_mode':'PAPER_ONLY','retouch_requires_away':True}})
    state={}
    _,st=evaluate_ema8_prototype(bars,bar_ci=6,cfg=cfg,proto_state=state,session_day='2026-11-19')
    assert state['arm']['direction']==('LONG' if bullish else 'SHORT')
    _,e8=ema4_ema8(bars['close']);prev=float(e8.iloc[-2])
    far=prev+(20 if bullish else -20)
    no,_=evaluate_ema8_prototype_intrabar(bars,far,cfg=cfg,proto_state=state,session_day='2026-11-19',tick_high=far,tick_low=far,now_hm='11:00')
    assert no==[] and state['arm']['away_seen']
    if restart:
        from analyst.ema_arm_persistence_8510 import bootstrap_prototype_arm
        b._ema8_prototype_state=state;b._save_ema8_prototype_state()
        fresh=make_test_backend(tmp_path,broker)
        assert fresh._proto_arm_loaded_from_db
        assert bootstrap_prototype_arm(fresh,bars,cfg,'2026-11-19')=='resumed'
        state=fresh._ema8_prototype_state
        for name in ('scanner_cfg','strategy_cooldowns','recent_signals','_skip_retry_at','log_signal_observation','mark_signal_outcome'):
            setattr(fresh,name,getattr(b,name))
        fresh._real_money_enabled=False
        b=fresh
    sigs,_=evaluate_ema8_prototype_intrabar(bars,prev,cfg=cfg,proto_state=state,session_day='2026-11-19',tick_high=prev,tick_low=prev,now_hm='11:00')
    assert len(sigs)==1
    b._ema8_prototype_state=state
    b.cached_telemetry={'spot':{'price':prev,'updated_epoch':time.time(),'is_stale':False},'quotes':{}}
    contract={'symbol':'NIFTY 23 NOV 2026 25000 '+('CE' if bullish else 'PE'),'security_id':'54321','lot_size':65,'ltp':250.,'opt_type':'CE','delta':.5}
    broker.quotes={'54321':250.}
    b._select_ema8_prototype_contract_any_strike=AsyncMock(return_value=(contract,''))
    asyncio.run(b._process_strategy_signals(sigs))
    with sqlite3.connect(b.ledger_db) as c:
        rows=c.execute("select order_status,mode from orders where side='BUY'").fetchall()
        positions=c.execute('select status,entry_price from positions').fetchall()
    assert rows==[('FILLED','PAPER_ONLY')]
    assert positions==[('OPEN',250.)]
    assert broker.placed_orders==[]


def test_arm_survives_actual_abrupt_child_process_exit(tmp_path):
    import subprocess,sys
    from pathlib import Path
    code="""
from pathlib import Path
from analyst.test_arm_persistence_8510 import _backend_with_db
import sys,time
b=_backend_with_db(Path(sys.argv[1]))
b._ema8_prototype_state={'day':'2026-11-19','arm':{'direction':'LONG','cross_bar_open_ts':1795060800,'cross_e8':25000.,'away_seen':True}}
b._save_ema8_prototype_state()
print('READY',flush=True)
time.sleep(60)
"""
    child=subprocess.Popen([sys.executable,'-c',code,str(tmp_path)],cwd=str(Path(__file__).parents[1]),stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True)
    try:
        assert child.stdout.readline().strip()=='READY'
        child.kill();child.wait(timeout=10)
        b=_backend_with_db(tmp_path)
        assert b._ema8_prototype_state['arm']=={'direction':'LONG','cross_bar_open_ts':1795060800,'cross_e8':25000.,'away_seen':True}
    finally:
        if child.poll() is None:child.kill();child.wait()


def _execution_backend(tmp_path,monkeypatch):
    from analyst.test_phase2_option_points_8510 import FakeBroker,make_test_backend
    broker=FakeBroker();b=make_test_backend(tmp_path,broker);b._real_money_enabled=False
    monkeypatch.setattr(QuantTerminalBackend,'new_position_entry_allowed',REAL_ENTRY_GUARD)
    b.scanner_cfg={'signal_cooldown_5m_min':0,'skipped_signal_retry_seconds':0,
      'reversal':{'enabled':True,'execution_mode':'PAPER_ONLY'},'desk8600_board':{'enabled':False}}
    b._ema_lane_lock={};b.strategy_cooldowns={};b.recent_signals=[];b._skip_retry_at={}
    b.log_signal_observation=lambda *a,**kw:'fixture';b.mark_signal_outcome=lambda *a,**kw:None
    b.cached_telemetry={'spot':{'price':25000.,'updated_epoch':time.time(),'is_stale':False},'quotes':{}}
    contract={'symbol':'NIFTY 23 NOV 2026 25000 CE','security_id':'54321','lot_size':65,'ltp':250.,'opt_type':'CE','delta':.5}
    broker.quotes={'54321':250.}
    b._select_ema8_prototype_contract_any_strike=AsyncMock(return_value=(contract,''))
    return b,broker,contract


@pytest.mark.parametrize('bullish',[True,False])
@pytest.mark.parametrize('restart',[True,False])
def test_reversal_technical_trigger_to_actual_paper_order(tmp_path,monkeypatch,bullish,restart):
    from analyst.test_four_algo_corrections_8510 import _reversal_frame,_closed_e8,_tick,DAY
    from analyst.mj_sir_stage_reversal_8510 import evaluate_mj_sir_four_pack,load_mj_cfg
    full,k=_reversal_frame();bars=full.iloc[:k+3].copy()
    if not bullish:
        for col in ['open','close']:bars[col]=50000-bars[col]
        oldhi,oldlo=bars['high'].copy(),bars['low'].copy()
        bars['high']=50000-oldlo;bars['low']=50000-oldhi
    px=_closed_e8(bars)+(1 if bullish else -1)
    b,broker,contract=_execution_backend(tmp_path,monkeypatch)
    mj={}
    if restart:
        far=px+(50 if bullish else -50)
        waiting,_=evaluate_mj_sir_four_pack(bars,bar_ci=-2,cfg=load_mj_cfg({}),
            bb_stage2=False,bb_stage3=False,bb_cross=False,party_switch_enabled=False,
            active_party='NEUTRAL',mj_state=mj,session_day=DAY,dynamic=_tick(far,hm='14:45'),
            scanner_cfg={'reversal':{'enabled':True,'execution_mode':'PAPER_ONLY'}})
        assert not any(s['strategy_id'].startswith('CROSSOVER') for s in waiting)
        b._mj_sir_state=mj;b._save_mj_sir_runtime_state()
        b,broker,contract=_execution_backend(tmp_path,monkeypatch)
        assert b._mj_sir_loaded_from_db
        mj=b._mj_sir_state
    sigs,status=evaluate_mj_sir_four_pack(bars,bar_ci=-2,cfg=load_mj_cfg({}),
        bb_stage2=False,bb_stage3=False,bb_cross=False,party_switch_enabled=False,
        active_party='NEUTRAL',mj_state=mj,session_day=DAY,dynamic=_tick(px,hm='14:45'),
        scanner_cfg={'reversal':{'enabled':True,'execution_mode':'PAPER_ONLY'}})
    sigs=[s for s in sigs if s['strategy_id'].startswith('CROSSOVER')]
    assert len(sigs)==1
    if not bullish:contract.update(symbol='NIFTY 23 NOV 2026 25000 PE',opt_type='PE')
    b.cached_telemetry['spot']['price']=px
    asyncio.run(b._process_strategy_signals(sigs))
    with sqlite3.connect(b.ledger_db) as c:
        assert c.execute("select count(*) from orders where order_status='FILLED' and mode='PAPER_ONLY'").fetchone()[0]==1
        assert c.execute("select count(*) from positions where status='OPEN'").fetchone()[0]==1
    assert broker.placed_orders==[]


def test_existing_monitor_recovers_satisfied_spot_exit(tmp_path,monkeypatch):
    from analyst.test_reversal_crosslane_8510 import _proto_sig
    b,broker,_=_execution_backend(tmp_path,monkeypatch)
    asyncio.run(b._process_strategy_signals([_proto_sig()]))
    with sqlite3.connect(b.ledger_db) as c:
        target=c.execute('select spot_target_level from positions').fetchone()[0]
    b.cached_telemetry['spot']['price']=target+1
    b.cached_telemetry['quotes']={'54321':255.}
    b.get_active_positions(live_quotes={'54321':255.},spot_price=target+1)
    with sqlite3.connect(b.ledger_db) as c:
        row=c.execute('select status,exit_reason from positions').fetchone()
    assert row==('CLOSED','SPOT_TARGET_HIT')


def test_paper_exit_uses_old_stored_ltp_without_requesting_new_quote(tmp_path,monkeypatch):
    from analyst.test_reversal_crosslane_8510 import _proto_sig
    b,broker,_=_execution_backend(tmp_path,monkeypatch)
    asyncio.run(b._process_strategy_signals([_proto_sig()]))
    broker.get_marketfeed_ltp=AsyncMock(return_value={'54321':260.})
    b.cached_telemetry['quotes']={}
    with sqlite3.connect(b.ledger_db) as c:
        c.row_factory=sqlite3.Row;pos=dict(c.execute('select * from positions').fetchone())
    asyncio.run(b._close_position_with_broker(pos,'TEST_EXIT',spot_exit=25040))
    with sqlite3.connect(b.ledger_db) as c:
        assert c.execute('select exit_price from positions').fetchone()[0]==250.
    broker.get_marketfeed_ltp.assert_not_awaited()


def test_fast_round_trip_inside_poll_interval_is_unobserved():
    from analyst.ema8_prototype_8510 import check_retouch
    arm={'away_seen':True}
    # Real path could be 110 -> 100 -> 110 between observed endpoints.
    observed=[110.,110.]
    assert all(not check_retouch(arm,bullish=True,e8_val=100,tol=3,spot=p,
        tick_high=p,tick_low=p,retouch_requires_away=True) for p in observed)
    assert check_retouch(arm,bullish=True,e8_val=100,tol=3,spot=110,
        tick_high=110,tick_low=100,retouch_requires_away=True)


def test_ist_buckets_anchor_at_0915_for_15m_and_hour():
    from analyst.nse_spot_candles_8510 import resample_nse_15m,resample_nse_1h
    index=pd.date_range('2026-11-19 09:15',periods=121,freq='1min',tz='Asia/Kolkata')
    f=pd.DataFrame({'open':100.,'high':101.,'low':99.,'close':100.,'volume':1.},index=index)
    assert list(resample_nse_15m(f).index.strftime('%H:%M'))[:3]==['09:15','09:30','09:45']
    assert list(resample_nse_1h(f).index.strftime('%H:%M'))==['09:15','10:15','11:15']


def test_reversal_runtime_roundtrips_actual_ledger(tmp_path):
    b1=_backend_with_db(tmp_path)
    expected={'day':'2026-11-19','reversal_fired_key':'2026-11-19:REV_15m_UP:cross',
              'reversal_pre_arm_15m':{'side':'UP','timeframe':'5m','cross_index':'2026-11-19T10:00:00+05:30'}}
    b1._mj_sir_state=copy.deepcopy(expected);b1._save_mj_sir_runtime_state()
    b2=_backend_with_db(tmp_path)
    assert b2._mj_sir_state==expected and b2._mj_sir_loaded_from_db


def test_spot_classification_trusts_old_cached_quote_when_ltt_absent():
    # Execute the actual producer classification branch, isolated from the
    # heavyweight dashboard builder. The source-age input is deliberately old.
    import ast
    from pathlib import Path
    from datetime import datetime,timezone
    from zoneinfo import ZoneInfo
    path=Path(__file__).with_name('terminal8510_backend.py')
    tree=ast.parse(path.read_text())
    node=next(n for n in ast.walk(tree) if isinstance(n,ast.If)
        and isinstance(n.test,ast.BoolOp) and isinstance(n.test.values[0],ast.Compare)
        and isinstance(n.test.values[0].left,ast.Constant) and n.test.values[0].left.value=='13'
        and isinstance(n.test.values[0].ops[0],ast.In)
        and isinstance(n.test.values[0].comparators[0],ast.Name)
        and n.test.values[0].comparators[0].id=='live_quotes')
    broker=SimpleNamespace(_cache_timestamps={'13':time.time()-3600},get_last_trade_time=lambda sid:None)
    ns={'self':SimpleNamespace(broker=broker),'live_quotes':{'13':25000.},'is_open':True,
        'now_dt':datetime.now(ZoneInfo('Asia/Kolkata')),'candle_spot':0.,'candle_spot_ts':0,
        'datetime':datetime,'timezone':timezone,'IST':ZoneInfo('Asia/Kolkata'),'time':time,
        'spot_time_ist':None,'spot_status':'UNAVAILABLE','is_stale':True}
    exec(compile(ast.Module(body=[node],type_ignores=[]),str(path),'exec'),ns)
    assert ns['spot_status']=='LIVE' and ns['is_stale'] is False
    assert time.time()-broker._cache_timestamps['13']>3590
