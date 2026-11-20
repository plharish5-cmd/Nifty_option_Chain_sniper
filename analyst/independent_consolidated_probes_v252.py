"""Explicit reviewer probes of observed defects; not acceptance tests preserving them."""
import asyncio
import inspect
import pandas as pd
from unittest.mock import AsyncMock
from analyst.terminal8510_backend import QuantTerminalBackend, simulate_prototype_spot_exit
from analyst.test_phase2_option_points_8510 import FakeBroker, make_test_backend

GOOD = dict(dhan_connected=True, token_configured=True, is_expired=False,
            ip_matched=True, current_ip='49.205.36.145', whitelisted_ip='49.205.36.145')


def test_observed_preflight_ignores_actual_normalized_funds_schema(tmp_path):
    b = make_test_backend(tmp_path, FakeBroker(configured=True))
    b.broker.get_masked_config = lambda: GOOD
    b.broker.get_fundlimit = AsyncMock(return_value={'ok':True,'margin_avail':25047.70,'raw':{'availabelBalance':25047.70}})
    result = asyncio.run(b.real_order_preflight(est_premium=200))
    assert result['funds_available'] == 0
    assert result['verdict'] == 'NOT_READY'
    assert not b.broker.placed_orders


def test_observed_test_button_is_rejected_by_actual_algo_only_gate(tmp_path):
    b = make_test_backend(tmp_path, FakeBroker(configured=True))
    b._select_ema8_prototype_contract_any_strike = AsyncMock(return_value=({'id':'54321','security_id':'54321','symbol':'NIFTY 25000 CE','ltp':250,'lot_size':65},''))
    result = asyncio.run(b.place_real_test_order('CE','PLACE'))
    assert result.get('algo_only_blocked') is True
    assert not b.broker.placed_orders
    assert result['routed_real'] is True  # misleading label despite local rejection


def test_confirmation_blocks_before_contract_resolution(tmp_path):
    b = make_test_backend(tmp_path, FakeBroker(configured=True))
    b._select_ema8_prototype_contract_any_strike = AsyncMock()
    result = asyncio.run(b.place_real_test_order('CE','NO'))
    assert not result['ok']
    b._select_ema8_prototype_contract_any_strike.assert_not_awaited()
    assert not b.broker.placed_orders


def test_observed_live_monitor_has_no_prototype_trail_wiring():
    source=inspect.getsource(QuantTerminalBackend.get_active_positions)
    assert 'SPOT_TARGET_HIT' in source
    assert 'trail_after_target' not in source


def test_observed_close_through_gap_books_trail_not_observed_close():
    entry_ts=1700000000
    bars=[dict(ts=entry_ts,day='2026-11-20',hm='10:00',close=100,e8=1)]
    # Trail arms at140, ratchets to120, then next candle gaps below to110/105.
    frame=pd.DataFrame([
        dict(ts=entry_ts+60,day='2026-11-20',hm='10:01',open=100,high=150,low=120,close=145),
        dict(ts=entry_ts+120,day='2026-11-20',hm='10:02',open=110,high=115,low=100,close=105),
    ])
    r=simulate_prototype_spot_exit(bars=bars,start_idx=0,entry_spot=100,is_long=True,
        sl_pts=40,tgt_pts=40,eod_hm='15:20',day_1m=frame,entry_ts=entry_ts,
        exit_on_opposite_close=True,trail_after_target=True)
    assert r['exit_reason']=='SPOT_TRAIL_EXIT'
    assert r['exit_spot']==120  # not traded anywhere in exit candle (high115)


def test_entry_and_exit_with_distinct_realistic_depth_in_private_ledger(tmp_path):
    from analyst.test_live_exit_fixes_8510 import _enter, _only_position
    broker=FakeBroker(configured=True)
    b=make_test_backend(tmp_path,broker)
    b.scanner_cfg={'ema8_prototype':{'execution_mode':'PAPER_ONLY'}}
    broker.get_marketfeed_quote=AsyncMock(return_value={'54321':{'bid':249,'ask':251}})
    async def run():
        result=await _enter(b)
        assert result['ok'], result
        pos=_only_position(b)
        assert pos['entry_price']==251
        broker.get_marketfeed_quote.return_value={'54321':{'bid':279,'ask':281}}
        closed=await b._close_position_with_broker(pos,'SPOT_TARGET_HIT',spot_exit=25040)
        assert closed['ok'],closed
        import sqlite3
        with sqlite3.connect(b.ledger_db) as c:
            row=c.execute('select entry_price,exit_price,gross_pnl from positions').fetchone()
        assert row==(251,279,1820)
        assert broker.placed_orders==[]
    asyncio.run(run())


def test_observed_depth_alone_does_not_repair_legacy_live_fixture(tmp_path,monkeypatch):
    from analyst import test_phase2_option_points_8510 as original
    async def depth(self,payload):
        return {str(sid):{'bid':250,'ask':250} for ids in payload.values() for sid in ids}
    monkeypatch.setattr(original.FakeBroker,'get_marketfeed_quote',depth,raising=False)
    import pytest
    with pytest.raises(AssertionError):
        original.test_prototype_buy_option_points_40(tmp_path)


def test_observed_preflight_ignores_kill_switch_if_balance_shape_is_patched(tmp_path):
    b=make_test_backend(tmp_path,FakeBroker(configured=True))
    b.kill_switch_active=True
    b.broker.get_masked_config=lambda:GOOD
    b.broker.get_fundlimit=AsyncMock(return_value={'availabelBalance':25047.70})
    result=asyncio.run(b.real_order_preflight(est_premium=200))
    assert result['verdict']=='READY_REAL'
    assert not b.broker.placed_orders


def test_mocked_live_entry_with_explicit_lane_optin_and_depth(tmp_path):
    from analyst.test_live_exit_fixes_8510 import _enter, _only_position
    broker=FakeBroker(configured=True)
    broker.get_marketfeed_quote=AsyncMock(return_value={'54321':{'bid':249,'ask':251}})
    b=make_test_backend(tmp_path,broker)
    b.scanner_cfg={'ema8_prototype':{'enabled':True,'execution_mode':'LIVE_ALLOWED','live_trade_enabled':True}}
    async def run():
        r=await _enter(b)
        assert r['ok'],r
        assert any(o['transactionType']=='BUY' for o in broker.placed_orders)
        pos=_only_position(b)
        assert pos['live_filled']==1
        assert pos['entry_price']==250 # fake broker actual fill; ask must not override
    asyncio.run(run())


def test_engine_imports_in_network_blocked_paper_test_harness():
    # Python3.9 asyncio.run in preceding probes closes the default loop.
    # Give module-level lock construction a loop, as in a fresh entry process.
    loop=asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        import engine_8510_live as engine
        assert engine.app is not None
        assert engine.backend.execution_mode=='PAPER_ONLY'
    finally:
        loop.close()
        asyncio.set_event_loop(None)
