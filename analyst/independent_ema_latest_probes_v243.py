"""Supplemental independent probes of f33276e; confirmations of residual defects."""
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
import pytest
pytest.importorskip('analyst.ema_lane_reconcile_8510', reason='Supplement targets f33276e, absent from reviewed 0fcda9a')
from analyst.ema_lane_reconcile_8510 import reconcile_lane_lock_on_startup
from analyst.missed_trade_watchdog_8510 import note_signals_submitted,run_watchdog_timeouts


def test_reconciler_releases_on_failed_position_read():
    def unavailable():raise RuntimeError('private fixture ledger unavailable')
    b=SimpleNamespace(_ema_lane_lock={'holder':'PROTOTYPE'},get_active_positions=unavailable,_save_ema_lane_lock_state=lambda:None)
    assert reconcile_lane_lock_on_startup(b)
    assert not b._ema_lane_lock.get('holder')


def test_reconciler_does_not_consult_pending_broker_order():
    broker=SimpleNamespace(get_orders=AsyncMock(return_value=[{'orderStatus':'TRANSIT','filledQty':0}]))
    b=SimpleNamespace(_ema_lane_lock={'holder':'PROTOTYPE','note':'submission outcome unknown'},
        get_active_positions=lambda:[],_save_ema_lane_lock_state=lambda:None,broker=broker)
    assert reconcile_lane_lock_on_startup(b)
    broker.get_orders.assert_not_awaited()


def test_repeated_unfilled_signal_resets_watchdog_deadline_forever():
    b=SimpleNamespace(scanner_cfg={'miss_watchdog':{'enabled':True,'qualification_timeout_sec':12}},
        get_active_positions=lambda:[],_ema8_prototype_state={})
    sig={'strategy_id':'EMA8_PROTOTYPE_LONG','direction':'LONG'}
    with patch('analyst.missed_trade_watchdog_8510._bar_dedupe_key',return_value='same-bar'), \
         patch('analyst.missed_trade_watchdog_8510._legitimate_block_reason',return_value=None), \
         patch('analyst.missed_trade_watchdog_8510._armed_idle_heartbeat'), \
         patch('analyst.missed_trade_watchdog_8510.append_miss_event') as events, \
         patch('analyst.missed_trade_watchdog_8510.try_telegram_alert'):
        for now in range(0,31,3):
            with patch('analyst.missed_trade_watchdog_8510.time.time',return_value=now):
                note_signals_submitted(b,[sig]);run_watchdog_timeouts(b)
        events.assert_not_called()
        assert b._miss_watchdog['pending']['same-bar']['t0']==30
        with patch('analyst.missed_trade_watchdog_8510.time.time',return_value=45):run_watchdog_timeouts(b)
        events.assert_called_once()
