"""State handoff and recorded baseline regressions; no broker/network calls."""
import asyncio
from unittest.mock import AsyncMock
from analyst.terminal8510_backend import QuantTerminalBackend
from analyst.chain_reads_8510 import compute_momentum_oi_reads, _hm_ts


def test_square_off_preserves_evaluators_new_opposite_arm():
    async def run():
        b = QuantTerminalBackend.__new__(QuantTerminalBackend)
        b._ema8_prototype_state = {'arm': {'direction': 'LONG', 'cross_bar_open_ts': 100}}
        b._square_off_open_ema8_prototype_positions = AsyncMock()
        b._save_ema8_prototype_state = lambda: None
        b.log_ema8_prototype_event = lambda **kw: None
        b._handle_ema8_prototype_actions([
            {'type': 'SQUARE_OFF', 'direction': 'SHORT', 'reason': 'OPPOSITE_8EMA_CLOSE'},
            {'type': 'ARM', 'direction': 'LONG'},
        ])
        await asyncio.sleep(0)
        assert b._ema8_prototype_state['arm']['direction'] == 'LONG'
        b._square_off_open_ema8_prototype_positions.assert_awaited_once()
    asyncio.run(run())


def test_recorded_open_seconds_are_not_missing_baseline():
    day = '2026-11-19'
    start = _hm_ts(day, '09:15') + 16
    times = [start, start + 600]
    rows = {times[0]: {25000.: {'ce_oi': 1000000, 'pe_oi': 1000000}},
            times[1]: {25000.: {'ce_oi': 1500000, 'pe_oi': 500000}}}
    out = compute_momentum_oi_reads(rows, times, spot=25000, band=1000, day=day,
                                   now_ts=times[-1], last_t=times[-1], age_sec=0)
    assert out['total_oi']['direction'] == 'PE'
    assert out['total_oi']['baseline_ts'] == start
    assert out['fresh_oi']['baseline_kind'] == 'FIRST_RECORDED_SESSION_SNAPSHOT'
    assert '09:15:16' in out['fresh_oi']['detail']


def test_late_start_is_explicit_not_claimed_as_open():
    day = '2026-11-19'
    start = _hm_ts(day, '10:00')
    times = [start, start + 600]
    rows = {t: {25000.: {'ce_oi': 1000000 + i*500000, 'pe_oi': 1000000-i*500000}}
            for i,t in enumerate(times)}
    out = compute_momentum_oi_reads(rows,times,spot=25000,band=1000,day=day,
                                  now_ts=times[-1],last_t=times[-1],age_sec=0)
    assert out['fresh_oi']['baseline_ts']==start
    assert '10:00:00' in out['fresh_oi']['detail']
    assert 'since 09:15' not in out['fresh_oi']['detail']


def test_timeout_is_not_reported_as_app_stopped():
    from types import SimpleNamespace
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from analyst.desk8600_link_8510 import link_status
    now=datetime(2026,11,19,13,0,tzinfo=ZoneInfo('Asia/Kolkata'))
    bridge=SimpleNamespace(cfg={'enabled':True,'base_url':'http://127.0.0.1:8600'},
        last={'evaluated_at':now.isoformat(),'rows':[{'signal_group':'Actionable Setups',
             'verdict':'UNAVAILABLE','reason':'timeout: timed out'}]})
    out=link_status(bridge,{},now,market_open=True)
    assert out['desk8600']['state']=='TIMEOUT'
    assert 'may be busy' in out['desk8600']['headline']


def test_recovery_uses_closed_session_cross_not_forming_or_past_entry():
    from analyst.prototype_arm_recovery_8510 import recover_missing_arm
    from analyst.test_four_algo_corrections_8510 import _frame, DAY
    bars=_frame([100.]*8+[90.,110.,115.])
    state={}
    assert recover_missing_arm(bars,state=state,cfg={'enabled':True,'reentry_same_side':True},session_day=DAY)
    assert state['arm']['direction']=='LONG'
    assert state['arm']['cross_bar_open_ts']==int(bars.index[-2].timestamp())
    assert 'open' not in state
    assert not recover_missing_arm(bars,state=state,cfg={},session_day=DAY)
    for block in [{'sl_blocked_side':'LONG'},{'consumed_cross_ts':10},{'open':{'direction':'SHORT'}}]:
        assert not recover_missing_arm(bars,state=block,cfg={},session_day=DAY)
    assert not recover_missing_arm(bars,state={},cfg={},session_day='2026-11-19')
