"""v1.9.7 staged collector, progressive windows and nonblocking interpreter evidence."""
import csv
import json
import threading
import time
from datetime import datetime,timedelta
from pathlib import Path
from types import SimpleNamespace
import pytest
from analyst import options_telemetry_engine as e
from analyst.institutional_file_collector_8510 import InstitutionalFileCollector,atomic_write
from analyst.institutional_qwen_bridge_8510 import InstitutionalTelemetryBridge


def rows(n):
    return [dict(time=(datetime(2026,11,6,9,15)+timedelta(minutes=i)).strftime('%H:%M'),put_oi=10000000+i*200000,call_oi=5000000,put_volume=100000,call_volume=100000,pcr=2+i*.01,future_ltp=24000+i*2,future_oi=10000000+i*50000) for i in range(n)]

@pytest.mark.parametrize('n,active',[(1,[]),(3,[]),(4,['3m']),(6,['3m','5m']),(16,['3m','5m','15m']),(31,['3m','5m','15m','30m']),(61,['3m','5m','15m','30m','60m'])])
def test_progressive_growth_and_both_row_orders(n,active):
    a=e.build_telemetry(rows(n));b=e.build_telemetry(list(reversed(rows(n))))
    assert a==b
    assert a['data_status']==('WARMING_UP' if n<4 else 'VALID')
    assert a['input_telemetry']['data_quality']['active_windows']==active
    weights=a['input_telemetry']['data_quality']['scoring_weights']
    if active:assert sum(weights.values())==pytest.approx(1)
    else:assert a['bias_score_pct']==a['confidence_pct']==0
    assert a['confidence_kind']=='HEURISTIC_NOT_CALIBRATED'


def test_session_current_split_owner_sample():
    p=Path('/home/harish/Downloads/PE_CE_NIFTY_2026-11-07.csv')
    if not p.exists():pytest.skip('Owner historical sample unavailable')
    r=e.build_telemetry(e.load_completed_rows(p))
    assert r['timestamp']=='15:39'
    assert r['session_regime']=='SESSION_STRONG_BULLISH'
    assert r['current_regime']=='NEUTRAL_MIXED'
    assert r['bias_score_pct']==7
    assert r['input_telemetry']['data_quality']['partial_bar_flag']
    assert not r['input_telemetry']['data_quality']['source_date_verified']


def test_current_minute_held_even_without_low_volume():
    r=e.build_telemetry(rows(6),reference_now=datetime(2026,11,6,9,20,30,tzinfo=e.IST))
    assert r['timestamp']=='09:19'
    assert r['input_telemetry']['data_quality']['ongoing_minute_flag']
    assert r['input_telemetry']['data_quality']['partial_latest_row_discarded']


def collector(tmp_path,bridge=None):
    return InstitutionalFileCollector(tmp_path/'ledger.sqlite3',tmp_path/'drops',tmp_path/'Downloads',bridge,clock=lambda:datetime(2026,11,6,9,37,tzinfo=e.IST))


def write_export(c,values):
    c.downloads.mkdir(exist_ok=True)
    p=c.downloads/'PE_CE_NIFTY_2026-11-06.json'
    p.write_text(json.dumps(list(reversed(values))+[{'time':'Prev Day'}]));return p


def test_atomic_incremental_exports_and_sqlite_dedup(tmp_path):
    c=collector(tmp_path);p=write_export(c,rows(4));c.ingest_export(p)
    p=write_export(c,rows(21));r=c.ingest_export(p)
    assert r['rows_collected']==21 and r['last_ingested_hhmm']=='09:35'
    c.ingest_export(p)
    import sqlite3
    with sqlite3.connect(c.ledger_db) as db:
        assert db.execute('SELECT COUNT(*) FROM institutional_collected_rows').fetchone()[0]==21
        assert db.execute('SELECT COUNT(*) FROM institutional_pe_ce_telemetry').fetchone()[0]==21
        outputs=[json.loads(r[0]) for r in db.execute('SELECT output_json FROM institutional_pe_ce_telemetry ORDER BY id')]
    assert outputs[0]['data_status']=='WARMING_UP'
    assert outputs[3]['input_telemetry']['data_quality']['active_windows']==['3m']
    snapshot=c.drop_folder/'PE_CE_NIFTY_2026-11-06.csv'
    assert [r['_minute'] for r in e.load_completed_rows(snapshot)]==list(range(555,576))
    assert not list(c.drop_folder.glob('.*.csv.*'))


def test_watch_schedule_market_hours_and_explicit_historical_mode(tmp_path):
    c=collector(tmp_path);p=write_export(c,rows(4))
    before=datetime(2026,11,6,9,14,tzinfo=e.IST);closed=datetime(2026,11,6,15,31,tzinfo=e.IST)
    assert not c.active(before) and not c.active(closed)
    assert not c.active(datetime(2026,11,7,10,tzinfo=e.IST))
    assert all(135<=c.interval()<=170 for _ in range(50))
    c.collect_once(now=before);assert not c.drop_folder.exists()
    status=c.collect_once();assert status['rows_collected']==4
    assert status['automatic_website_download'] is False
    assert 135<=status['next_poll_in_sec']<=170


def test_conflicting_completed_row_is_quarantined(tmp_path):
    c=collector(tmp_path);p=write_export(c,rows(4));c.ingest_export(p)
    changed=rows(4);changed[1]['future_ltp']+=100;p=write_export(c,changed)
    assert c.ingest_export(p)['conflicting_rows']==1
    import sqlite3
    with sqlite3.connect(c.ledger_db) as db:
        assert json.loads(db.execute('SELECT payload FROM institutional_collected_rows WHERE minute=556').fetchone()[0])['future_ltp']==24002
        assert db.execute('SELECT COUNT(*) FROM institutional_collector_conflicts').fetchone()[0]==1


def test_low_volume_candidate_is_held(tmp_path):
    c=collector(tmp_path);data=rows(21);data[-1].update(put_volume=1,call_volume=1)
    p=write_export(c,data);r=c.ingest_export(p)
    assert r['rows_collected']==20 and r['pending_row_time']=='09:35'


def test_asynchronous_narrative_does_not_block_new_telemetry(tmp_path):
    entered=threading.Event();release=threading.Event()
    def delayed(*a,**kw):
        entered.set();release.wait(3)
        from requests import Timeout
        raise Timeout('simulated')
    b=InstitutionalTelemetryBridge(tmp_path/'ledger.sqlite3',transport=delayed)
    path=tmp_path/'rows.json';path.write_text(json.dumps(rows(4)))
    try:
        start=time.perf_counter();r=b.ingest_file(path);elapsed=(time.perf_counter()-start)*1000
        assert elapsed<25
        assert r['authority_label'].startswith('DETERMINISTIC_AUTHORITATIVE')
        assert entered.wait(1)
        path.write_text(json.dumps(rows(6)));new=b.ingest_file(path)
        assert new['timestamp']=='09:20' and new['narrative_status']=='PENDING'
        release.set()
        deadline=time.time()+3
        while (b.qwen_status()['requests_attempted']<2 or b.snapshot()['narrative_status']=='PENDING') and time.time()<deadline:time.sleep(.01)
        assert b.snapshot()['timestamp']=='09:20'
        assert b.snapshot()['narrative_status']=='FALLBACK'
        status=b.qwen_status();assert status['cold_inference_ms']>=0 and status['warm_inference_ms']>=0
    finally:release.set();b.close()


def test_no_worker_result_can_overwrite_newer_generation(tmp_path):
    entered=threading.Event();release=threading.Event();calls=[]
    def narrative(url,**kw):
        calls.append(kw);entered.set();release.wait(2)
        packet=json.loads(kw['json']['prompt'])
        return SimpleNamespace(status_code=200,json=lambda:{'response':json.dumps(packet['deterministic_result'])})
    b=InstitutionalTelemetryBridge(tmp_path/'ledger.sqlite3',transport=narrative);path=tmp_path/'r.json'
    try:
        path.write_text(json.dumps(rows(4)));b.ingest_file(path);assert entered.wait(1)
        path.write_text(json.dumps(rows(6)));new=b.ingest_file(path,use_local=False);release.set()
        time.sleep(.05)
        assert b.snapshot()['timestamp']==new['timestamp'] and b.snapshot()['narrative_status']=='DISABLED'
        assert calls[0]['json']['keep_alive']=='30m'
        assert calls[0]['json']['options']['num_predict']==220
        assert calls[0]['timeout']==15
    finally:release.set();b.close()


def test_early_data_gaps_still_fail_quality():
    data=rows(6);data.pop(2)
    r=e.build_telemetry(data)
    assert r['data_status']=='DATA_UNRELIABLE' and r['bias_score_pct']==0


def test_session_anchor_cannot_be_invented():
    r=e.build_telemetry(rows(61)[1:])
    assert r['session_regime']=='SESSION_NOT_AVAILABLE'


def test_collector_restart_republishes_saved_evidence_without_duplicates(tmp_path):
    c=collector(tmp_path);p=write_export(c,rows(4));c.ingest_export(p)
    published=[]
    fake=SimpleNamespace(snapshot=lambda:{},publish_collected=lambda result,rowid:published.append((result,rowid)))
    restarted=collector(tmp_path,fake);restarted.ingest_export(p)
    assert len(published)==1 and published[0][0]['timestamp']=='09:18'
    import sqlite3
    with sqlite3.connect(c.ledger_db) as db:assert db.execute('SELECT COUNT(*) FROM institutional_pe_ce_telemetry').fetchone()[0]==4


def test_session_continuity_not_invented_across_old_gaps():
    data=rows(100);data.pop(10)
    r=e.build_telemetry(data)
    assert r['data_status']=='VALID'
    assert r['session_regime']=='SESSION_NOT_AVAILABLE'


def test_atomic_write_failure_preserves_existing_snapshot(tmp_path,monkeypatch):
    import os
    folder=tmp_path/'atomic';folder.mkdir();target=folder/'snapshot.csv';target.write_bytes(b'old complete file')
    def fail(*args):raise OSError('simulated replace failure')
    monkeypatch.setattr(os,'replace',fail)
    with pytest.raises(OSError):atomic_write(target,b'new file')
    assert target.read_bytes()==b'old complete file'
    assert sorted(p.name for p in folder.iterdir())==['snapshot.csv']


def test_slim_narrative_response_preserves_all_deterministic_fields(tmp_path):
    seen=[]
    def response(url,**kwargs):
        seen.append(kwargs['json'])
        return SimpleNamespace(status_code=200,json=lambda:{'response':json.dumps({'summary':'Observed session trend and current flow differ.','warnings':[]})})
    from analyst.institutional_qwen_bridge_8510 import interpret_local
    deterministic=e.build_telemetry(rows(61));result,status=interpret_local(deterministic,response)
    assert status=='OK'
    for key,value in deterministic.items():
        if key not in ('summary','warnings'):assert result[key]==value
    assert seen[0]['options']['num_ctx']==1536 and seen[0]['options']['num_predict']==220


def test_collector_status_api_has_no_execution_authority(tmp_path,monkeypatch):
    import asyncio,httpx,engine_8510_live as engine
    c=collector(tmp_path);monkeypatch.setattr(engine.backend,'institutional_file_collector',c,raising=False)
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=engine.app),base_url='http://testserver') as client:
            result=(await client.get('/api/institutional_telemetry/collector_status')).json()
            assert result['automatic_website_download'] is False
            assert result['drop_folder_path']==str(c.drop_folder)
            assert 'order' not in result
    asyncio.run(run())


def test_prefix_calculation_never_holds_shared_ledger_writer_lock(tmp_path,monkeypatch):
    import sqlite3
    from analyst import institutional_file_collector_8510 as module
    c=collector(tmp_path);path=write_export(c,rows(6));original=module.build_telemetry;checks=[]
    def checked(*args,**kwargs):
        with sqlite3.connect(c.ledger_db,timeout=.01) as db:
            db.execute('BEGIN IMMEDIATE');db.rollback();checks.append(True)
        return original(*args,**kwargs)
    monkeypatch.setattr(module,'build_telemetry',checked)
    c.ingest_export(path)
    assert len(checks)==7


def test_current_or_future_minutes_never_finalized_by_collector(tmp_path):
    c=collector(tmp_path);path=write_export(c,rows(21))
    result=c.ingest_export(path,datetime(2026,11,6,9,17,30,tzinfo=e.IST))
    assert result['rows_collected']==2
    assert result['last_ingested_hhmm']=='09:16' and result['pending_row_time']=='09:17'
