"""Acceptance probes for independent review, deliberately capable of FAILing the candidate.
All network I/O is mocked; production configs and orders are never accessed.
"""
import asyncio
from datetime import datetime
from types import SimpleNamespace
import pytest
from analyst import writer_bias_live_reads_8510 as w
from analyst import desk8600_lane_gating_8510 as g
from analyst import dhan_spot_rest_fallback_8510 as f


def test_bullish_supports_ce_not_pe():
    assert w._direction_from_regime('BULLISH')=='CE'


def test_put_delta_not_clamped_to_one_percent():
    # Near-ATM put with positive time value must not be collapsed to 0.01.
    assert abs(w._estimate_delta(25000,25000,150,False,7/365,.065)) > .3


def fixture_data(monkeypatch):
    monkeypatch.setattr(w,'_estimate_delta',lambda *a,**k:.5)
    w._SCORE_HISTORY.clear()
    t=int(datetime(2026,11,26,9,15,tzinfo=w.IST).timestamp())
    point=lambda co,cp,po,pp:{25000.:dict(ce_oi=co,ce_ltp=cp,pe_oi=po,pe_ltp=pp)}
    by={t:point(100,100,100,100),t+60:point(300,80,100,100),t+120:point(300,90,200,90)}
    kw=dict(by_ts=by,ts_sorted=sorted(by),spot=25000,band=1000,day='2026-11-26',now_ts=t+120,last_t=t+120,age_sec=0,scanner_cfg={},momentum_oi={},expiry='2026-12-01')
    return t,by,kw


def test_headline_uses_matched_last_poll_not_since_open(monkeypatch):
    _,_,kw=fixture_data(monkeypatch)
    out=w.apply_writer_bias_to_board_reads(**kw)
    # In the last poll CE OI unchanged, PE OI grows while PE price falls.
    assert out['day_oi']['writer_bias_regime']=='BULLISH'


def test_late_start_not_labelled_0915_baseline(monkeypatch):
    t,by,kw=fixture_data(monkeypatch)
    kw.update(by_ts={k+900:v for k,v in by.items()},ts_sorted=[x+900 for x in sorted(by)],now_ts=t+1020,last_t=t+1020)
    out=w.apply_writer_bias_to_board_reads(**kw)
    assert out['facts'].get('writer_bias_session_baseline') is None


@pytest.mark.parametrize('lane',g.LANES)
def test_slope_warmup_does_not_block(lane):
    out=g.evaluate_lane_gating(lane,True,[dict(id='day_oi',direction='PE',feed_state='LIVE',age_sec=1,
        source_ts='2026-11-26T09:20:00+05:30',writer_bias_score=-.8,writer_bias_slope=None)],{})
    assert not out['blocked']


def test_missing_source_timestamp_does_not_block():
    out=g.evaluate_lane_gating('prototype',True,[dict(id='day_oi',direction='PE',feed_state='LIVE',age_sec=1)],{})
    assert not out['blocked']


def test_runtime_toggle_overrides_nested_old_mode(tmp_path,monkeypatch):
    p=tmp_path/'rules.json';p.write_text('{}');monkeypatch.setattr(g,'_LIVE_SHEET_RULES',p)
    modes=g.set_all_lane_gating_mode('LOG_ONLY')
    cfg=dict(desk8600_lane_gating=modes,writer_bias=dict(desk8600_lane_gating={'prototype':{'pe_ce_change':'BLOCK'}}))
    assert g.load_desk8600_lane_gating(cfg)['prototype']['pe_ce_change']=='LOG_ONLY'


def broker():
    return SimpleNamespace(base_url='https://api.dhan.co/v2',is_configured=lambda:True,_headers=lambda:{},_ltp_cache={},_cache_timestamps={})


def patch_client(monkeypatch,post):
    class Client:
        async def __aenter__(self):return self
        async def __aexit__(self,*a):return False
        async def post(self,*a,**kw):return await post(*a,**kw)
    from analyst import dhan_ipv4_guard_8510 as ip
    monkeypatch.setattr(ip,'ipv4_async_client',lambda timeout:Client())


def test_concurrent_requests_share_the_throttle(monkeypatch):
    f._reset_throttle_for_tests();calls=[]
    async def post(*a,**kw):
        calls.append(kw['json']);await asyncio.sleep(.01)
        return SimpleNamespace(status_code=200,json=lambda:{'data':{'IDX_I':{'13':{'last_price':25000}}}})
    patch_client(monkeypatch,post)
    async def run():
        b=broker()
        await asyncio.gather(f.entry_rest_ltp_lookup(b,{'IDX_I':[13]}),f.entry_rest_ltp_lookup(b,{'IDX_I':[13]}))
    asyncio.run(run())
    assert len(calls)<=1


def test_429_attempts_also_obey_throttle(monkeypatch):
    f._reset_throttle_for_tests();calls=[]
    async def post(*a,**kw):
        calls.append(1);return SimpleNamespace(status_code=429,json=lambda:{})
    patch_client(monkeypatch,post)
    async def run():
        b=broker()
        await f.entry_rest_ltp_lookup(b,{'IDX_I':[13]})
        await f.entry_rest_ltp_lookup(b,{'IDX_I':[13]})
    asyncio.run(run());assert len(calls)<=1


def test_spot_refresh_does_not_renew_an_old_option_quote(monkeypatch):
    f._reset_throttle_for_tests();clock=[100.];calls=[]
    monkeypatch.setattr(f.time,'monotonic',lambda:clock[0])
    async def post(*a,**kw):
        ids=kw['json'];seg=next(iter(ids));sid=str(ids[seg][0]);calls.append(sid)
        px=200 if sid=='999' else 25000
        return SimpleNamespace(status_code=200,json=lambda:{'data':{seg:{sid:{'last_price':px}}}})
    patch_client(monkeypatch,post)
    async def run():
        b=broker()
        await f.entry_rest_ltp_lookup(b,{'NSE_FNO':[999]})
        clock[0]=110
        await f.entry_rest_ltp_lookup(b,{'IDX_I':[13]})
        clock[0]=110.1
        px,err=await f.recover_option_ltp_for_entry(b,'999')
        assert px is None or len(calls)==3
    asyncio.run(run())


def test_entry_guard_still_blocks_208_second_spot(monkeypatch):
    f._reset_throttle_for_tests()
    import time
    async def post(*a,**kw):return SimpleNamespace(status_code=429,json=lambda:{})
    patch_client(monkeypatch,post)
    async def run():
        b=SimpleNamespace(broker=broker(),cached_telemetry={'spot':{'price':25000,'updated_epoch':time.time()-208}})
        ok,reason=await f.recover_nifty_spot_for_entry(b,market_open=True)
        assert not ok and 'STALE' in reason
    asyncio.run(run())


def test_ws_loop_reconnects_after_three_timeouts(monkeypatch):
    import websocket
    from analyst.terminal8500_market import Feed
    feed=Feed(dict(feed_socket_timeout_seconds=.001,feed_retry_seconds=.001,feed_max_consecutive_timeouts=3),lambda *a:None)
    feed.select('spot','IDX_I','13');calls=[]
    class Socket:
        def send(self,*a):return None
        def recv(self):raise websocket.WebSocketTimeoutException()
        def close(self):return None
    def connect(*a,**kw):
        calls.append(1)
        if len(calls)==2:feed.quit.set()
        return Socket()
    monkeypatch.setenv('DHAN_ACCESS_TOKEN','test');monkeypatch.setenv('DHAN_CLIENT_ID','test')
    monkeypatch.setattr(websocket,'create_connection',connect)
    feed.run();assert len(calls)==2

@pytest.mark.parametrize('lane',g.LANES)
@pytest.mark.parametrize('board_id',[x[1] for x in g.TRACK2_SIGNALS])
@pytest.mark.parametrize('age',[1,None])
def test_explicit_stale_never_blocks_all_lanes_signals(lane,board_id,age):
    out=g.evaluate_lane_gating(lane,True,[dict(id=board_id,direction='PE',feed_state='STALE',age_sec=age)],{})
    assert not out['blocked']
