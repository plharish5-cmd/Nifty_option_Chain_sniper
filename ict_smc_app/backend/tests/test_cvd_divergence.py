"""Causal replay, lifecycle and live data-isolation acceptance tests."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("ICT_DATA_DIR", "/tmp/ict-cvd-tests")

import pytest
from app.engine import cvd_divergence as engine

T0 = 1793063700
STEP = 180


def fixture(bull=False):
    # Full two-sided pivots at 2 and 6. Second pivot becomes known at 8.
    highs = [100, 102, 110, 104, 103, 105, 115, 108, 106, 102, 101, 100]
    lows = [96, 97, 100, 96, 95, 98, 101, 99, 97, 90, 89, 88]
    closes = [98, 100, 108, 100, 99, 102, 112, 104, 101, 92, 91, 90]
    flow = [0, 100, 1000, 800, 700, 600, 400, 350, 300, 100, 0, -100]
    bars = [dict(ts=T0+i*STEP, o=closes[i], h=h, l=lows[i], c=closes[i], v=1000,
                 cvd=flow[i], vwap=100) for i, h in enumerate(highs)]
    if bull:
        for b in bars:
            b.update(o=200-b['o'], h=200-b['l'], l=200-b['h'], c=200-b['c'], cvd=-b['cvd'])
    return bars


def run(bars, **cfg):
    return engine.compute_cvd_divergence(price_series=bars, as_of=T0+100*STEP,
                                          cfg=dict(price_range_mult=0, delta_scale_mult=0, **cfg))


@pytest.mark.parametrize('bull', [False, True])
def test_confirmed_pivots_and_risk_only_after_confirmation(bull):
    bars = fixture(bull)
    assert run(bars[:8])['active_divergence'] is None
    watch = run(bars[:9])['active_divergence']
    assert watch['state'] == 'WATCH'
    assert watch['pivot_ts'] == bars[6]['ts']
    assert watch['confirmed_at'] == bars[8]['ts'] + STEP
    assert watch['entry'] is None and watch['tp'] is None
    res = run(bars[:10])
    event = res['active_divergence']
    assert event['state'] == 'VWAP_CONFIRMED'
    assert event['key'] == watch['key']
    sign = 1 if bull else -1
    assert sign * (event['entry'] - event['sl']) > 0
    assert sign * (event['tp'] - event['entry']) > 0
    assert abs(event['tp']-event['entry']) / event['risk_pts'] == pytest.approx(event['rr'])
    assert [t['state'] for t in res['transitions']] == ['WATCH', 'STRUCTURE_CONFIRMED', 'VWAP_CONFIRMED']


def test_append_does_not_rewrite_past_transitions():
    bars = fixture()
    for n in range(1, len(bars)):
        before = run(bars[:n])
        after = run(bars)
        assert before['transitions'] == [t for t in after['transitions'] if t['ts'] <= bars[n-1]['ts']+STEP]


def test_invalidation_wins_over_confirmation():
    bars = fixture()[:10]
    bars[-1]['h'] = 130
    res = run(bars)
    assert res['active_divergence'] is None
    assert res['recent_divergences'][0]['state'] == 'INVALIDATED'
    assert res['recent_divergences'][0]['entry'] is None


def test_expiry_and_disabled():
    res = run(fixture()[:10], expiry_bars=1)
    assert res['recent_divergences'][0]['state'] == 'EXPIRED'
    assert not run(fixture(), enabled=False)['ok']


def test_missing_delta_never_generates_proxy():
    bars = fixture()
    del bars[2]['cvd']
    res = run(bars)
    assert not res['ok']
    assert 'Explicit delta' in res['message']


def test_open_bar_excluded_and_no_duplicates():
    bars = fixture()[:9]
    assert engine.compute_cvd_divergence(price_series=bars, as_of=bars[-1]['ts']+STEP-1)['active_divergence'] is None
    bars[-1]['closed'] = False
    assert run(bars)['active_divergence'] is None
    assert not run([fixture()[0], fixture()[0]])['ok']


def test_structure_then_vwap_or_vwap_already_crossed():
    bars = fixture()[:10]
    bars[-1]['vwap'] = 85
    res = run(bars)
    assert res['active_divergence']['state'] == 'STRUCTURE_CONFIRMED'
    bars.append(dict(bars[-1], ts=bars[-1]['ts']+STEP, vwap=100))
    assert run(bars)['active_divergence']['state'] == 'VWAP_CONFIRMED'
    bars = fixture()[:10]
    for b in bars:
        b['vwap'] = 120
    assert run(bars)['active_divergence']['state'] == 'VWAP_CONFIRMED'


def test_vwap_from_input_volume_not_resampled_typical_price():
    bars = fixture()[:2]
    for b in bars:
        del b['vwap']
    bars[0]['v'] = 10
    bars[1]['v'] = 30
    res = run(bars)
    expected = sum((b['h']+b['l']+b['c'])/3*b['v'] for b in bars)/40
    assert res['current_vwap'] == pytest.approx(expected)


def mock_live(monkeypatch, missing=False, contract_changed=False):
    bars = fixture()
    minutes = [dict(ts=T0+i*60, o=100, h=101, l=99, c=100, v=10) for i in range(9)]
    monkeypatch.setattr(engine, 'session_bounds', lambda day: (float(T0), float(T0+22500)))
    monkeypatch.setattr(engine.db, 'query', lambda sql, args: minutes if 'sec_id=?' in sql and args[0] == 'FUT' else pytest.fail('Unscoped price query'))
    monkeypatch.setattr(engine.db, 'kv_get', lambda key: 'changed' if contract_changed and key.startswith('cvd_contract:') else '123')
    monkeypatch.setattr(engine.db, 'kv_set', lambda *args: None)
    class Adapter:
        def prepare(self, *args):
            pass
        def get_delta(self, row, tf):
            return None if missing and row['ts'] == T0+60 else 2
    monkeypatch.setattr(engine.adapters, 'make', lambda name: Adapter() if name == 'fut_quote_rule' else pytest.fail('Wrong adapter'))
    return minutes


def test_live_adapter_missing_data_staleness_and_rollover(monkeypatch):
    mock_live(monkeypatch)
    res = engine.compute_cvd_divergence(as_of=T0+540)
    assert res['ok'] and res['current_cvd'] == 18
    assert res['data_quality']['source'] == 'fut_quote_rule'
    assert res['current_vwap'] == 100
    stale = engine.compute_cvd_divergence(as_of=T0+900)
    assert stale['ok'] and not stale['alerts_enabled']
    assert stale['current_price'] == 100 and stale['transitions'] == []
    assert stale['active_divergence'] is None
    closed = engine.compute_cvd_divergence(as_of=T0+22500)
    assert closed['ok'] and not closed['alerts_enabled']
    assert 'market closed' in closed['message']
    mock_live(monkeypatch, missing=True)
    recovered = engine.compute_cvd_divergence(as_of=T0+540)
    assert recovered['ok'] and recovered['current_cvd'] == 14
    assert recovered['data_quality']['coverage'] == pytest.approx(8/9)
    assert len(recovered['data_quality']['missing_delta_minutes']) == 1
    assert 're-anchored' in recovered['message']
    mock_live(monkeypatch, contract_changed=True)
    assert 'Contract changed' in engine.compute_cvd_divergence(as_of=T0+540)['message']


def test_quote_rule_adapter_instrument_filter(monkeypatch):
    queries = []
    def query(sql, args):
        queries.append((sql,args))
        return []
    monkeypatch.setattr(engine.db, 'query', query)
    adapter = engine.adapters.make('fut_quote_rule')
    adapter.prepare('1m', T0, T0+60)
    assert all('inst=?' in sql and args[0] == 'FUTQR' for sql,args in queries)


def test_invalid_tf_and_nonfinite_data():
    assert not engine.compute_cvd_divergence(tf='2m')['ok']
    bars = fixture()
    bars[0]['cvd'] = float('nan')
    assert not run(bars)['ok']


def test_hub_reuses_persistent_alert_deduplication(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from app.api.hub import Hub
    from app.signals import alerts
    payload = run(fixture()[:10])
    sent, stored = [], {}
    async def broadcast(value):
        sent.append(value)
    def query(sql, args=()):
        return [stored[args[0]]] if args[0] in stored else []
    def execute(sql, row):
        stored[row['key']] = row
    monkeypatch.setattr(engine, 'compute_cvd_divergence', lambda **kw: payload)
    monkeypatch.setattr(alerts.db, 'query', query)
    monkeypatch.setattr(alerts.db, 'execute', execute)
    monkeypatch.setattr(alerts, 'now_epoch', lambda: T0+10*STEP)
    monkeypatch.setattr(alerts.config, 'rules', lambda: {'alerts': {'telegram': False}})
    monkeypatch.setattr(engine, 'get_cfg', lambda: {'notify_watch': False, 'alert_max_age_s': 120})
    def hub():
        # No broker, network or subscriptions: exercise only the CVD dispatch method.
        return SimpleNamespace(alerts=SimpleNamespace(broadcast=broadcast), broadcast=broadcast)
    asyncio.run(Hub.check_cvd_divergence_alerts(hub()))
    assert len(stored) == 2  # structure + VWAP, WATCH suppressed
    asyncio.run(Hub.check_cvd_divergence_alerts(hub()))
    assert len([x for x in sent if x['type'] == 'alert']) == 2


from datetime import datetime
P0 = int(datetime.fromisoformat('2026-10-27T09:15:00+05:30').timestamp())


@pytest.fixture
def provenance_db(monkeypatch):
    import sqlite3
    from app import db
    connection=sqlite3.connect(':memory:',isolation_level=None)
    connection.row_factory=sqlite3.Row
    connection.executescript(db.SCHEMA)
    monkeypatch.setattr(db,'_conn',connection)
    yield connection
    connection.close()


def test_provenance_contract_binding_and_assignment_accounting(provenance_db):
    from app.delta.recorder import ProvenanceRecorder,quality_gate,assignment_quality
    import json
    recorder=ProvenanceRecorder()
    for sid in ['OCT','NOV']:
        for i in range(60):
            ts=P0+i
            recorder.on_packet(dict(sid=sid,ltp=100+i*.1,volume=1000+i*10,bid=99+i*.1,ask=101+i*.1,_received_at=ts),ts)
    recorder.close_minute(P0)
    rows=provenance_db.execute('SELECT contract,payload FROM cvd_observations').fetchall()
    assert {r['contract'] for r in rows}=={'OCT','NOV'}
    for record in rows:
        r=json.loads(record['payload'])
        assert quality_gate(r)==[]
        assert r['v']==590
        assert r['quote_v']+r['tick_v']+r['unclassified_v']==r['v']
        assert sum(assignment_quality(r)['assignment_pct'].values())==pytest.approx(100,abs=.02)
    assert quality_gate({'delta':123})==['missing_provenance']


def test_provenance_immutable_conflict_and_timestamp_gate(provenance_db):
    from app.delta.recorder import ProvenanceRecorder,quality_gate
    import json
    def packet(i,vol=None):
        return dict(sid='OCT',ltp=100,volume=1000+i*10 if vol is None else vol,bid=99,ask=100,_received_at=P0+i)
    recorder=ProvenanceRecorder()
    for i in range(60):recorder.on_packet(packet(i),P0+i)
    recorder.close_minute(P0)
    before=provenance_db.execute('SELECT payload,digest FROM cvd_observations').fetchone()
    recorder.on_packet(packet(59,9999),P0+59)
    after=provenance_db.execute('SELECT payload,digest,conflict FROM cvd_observations').fetchone()
    assert after['payload']==before['payload'] and after['digest']==before['digest']
    assert 'conflicting_observation' in quality_gate(dict(json.loads(after['payload']),conflict=after['conflict']))
    another=ProvenanceRecorder()
    another.on_packet(packet(2),P0+2)
    another.on_packet(packet(1),P0+1)
    assert another.minutes[('2026-10-27','OCT',P0)]['bad_timestamp']


def test_provenance_gap_and_stale_diagnostics_overlap(provenance_db):
    from app.delta.recorder import ProvenanceRecorder,quality_gate
    recorder=ProvenanceRecorder()
    for i in [0,10,59]:
        recorder.on_packet(dict(sid='OCT',ltp=100+i,volume=1000+i*10,bid=99,ask=101,_received_at=P0+i),P0+i)
    row=next(iter(recorder.minutes.values()))
    assert row['gap_v']==row['v'] and row['stale_v']==row['v']
    assert 'packet_gap' in quality_gate(row) and 'stale_quote' in quality_gate(row)


def research_module():
    import importlib.util
    path=Path(__file__).resolve().parents[3]/'analyst'/'cvd_study.py'
    spec=importlib.util.spec_from_file_location('cvd_study_test',path)
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    return mod


def test_control_randomization_and_session_intervals_are_reproducible():
    import json,random
    study=research_module()
    plan=json.loads((Path(study.__file__).with_name('cvd_study_plan.json')).read_text())
    plan.update(random_repetitions=200,bootstrap_repetitions=200)
    events=[];days=[f'd{i}' for i in range(12)]
    for day in days:
        for i in range(8):
            events.append(dict(day=day,ts=i,match_key=(day,1,1,1,1,30),net_r=2. if i<2 else -1.,selected={'matched_cvd':i<2}))
    a=study.evaluate(events,'matched_cvd',plan,random.Random(4),days)
    b=study.evaluate(events,'matched_cvd',plan,random.Random(4),days)
    assert a==b and a['matched']==24 and a['clears_predeclared_test']
    assert a['bonferroni_p']>=a['randomization_p']
    for event in events:event['net_r']=0.
    flat=study.evaluate(events,'matched_cvd',plan,random.Random(4),days)
    assert not flat['clears_predeclared_test'] and flat['randomization_p']==1.
    for event in events:event['selected']['matched_cvd']=True
    assert study.evaluate(events,'matched_cvd',plan,random.Random(4),days)['status']=='insufficient_matched_controls'


def test_shared_research_gate_rejects_partial_sessions(provenance_db):
    import hashlib
    study=research_module()
    plan={'development_end':'2026-10-27','holdout_start':'2026-10-28','holdout_sessions':10}
    assert study.eligible_sessions(provenance_db,'development',plan,{})==({},{})
    import json
    provenance_db.execute('INSERT INTO cvd_observations (session,contract,ts,source,payload,digest) VALUES (?,?,?,?,?,?)',('2026-10-27','OCT',P0,'fut_quote_rule_v2',json.dumps(dict(session='2026-10-27',contract='OCT',ts=P0,source='fut_quote_rule_v2')),hashlib.sha256(json.dumps(dict(session='2026-10-27',contract='OCT',ts=P0,source='fut_quote_rule_v2')).encode()).hexdigest()))
    clean,rejected=study.eligible_sessions(provenance_db,'development',plan,{})
    assert not clean and rejected['session_incomplete']==1
    clean,rejected=study.eligible_sessions(provenance_db,'final',plan,{})
    assert not clean and not rejected


def test_complete_session_gate_and_immutable_sql_payload(provenance_db):
    import json,hashlib,sqlite3
    from app.delta.recorder import provenance_config,quality_gate
    study=research_module();cfg=provenance_config()
    base=dict(session='2026-10-27',contract='OCT',source=cfg['source'],schema_version=cfg['schema_version'],
              o=100,h=101,l=99,c=100,v=100,quote_v=80,tick_v=20,unclassified_v=0,
              stale_v=0,gap_v=0,at_mid_v=5,max_gap_s=1,packets=60,delta=50,bad_timestamp=False)
    for i in range(375):
        ts=P0+i*60;row=dict(base,ts=ts,first_event=ts,last_event=ts+59)
        assert quality_gate(row)==[]
        payload=json.dumps(row)
        provenance_db.execute('INSERT INTO cvd_observations (session,contract,ts,source,payload,digest) VALUES (?,?,?,?,?,?)',
                             (base['session'],base['contract'],ts,base['source'],payload,hashlib.sha256(payload.encode()).hexdigest()))
    plan={'development_end':'2026-10-27','holdout_start':'2026-10-28','holdout_sessions':10}
    clean,rejected=study.eligible_sessions(provenance_db,'development',plan,cfg)
    assert len(clean['2026-10-27'])==375 and not rejected
    with pytest.raises(sqlite3.IntegrityError,match='immutable'):
        provenance_db.execute("UPDATE cvd_observations SET payload='{}'")
    provenance_db.execute('UPDATE cvd_observations SET conflict=1 WHERE ts=?',(P0,))
    clean,rejected=study.eligible_sessions(provenance_db,'development',plan,cfg)
    assert not clean and rejected['conflicting_observation']==1


def test_research_outcomes_use_next_open_and_stop_first():
    study=research_module()
    event=dict(sign=1,entry=100,stop=90,risk=10)
    cfg=dict(rr=2,roundtrip_cost_points=2)
    assert study.outcome([dict(o=100,h=125,l=85,c=110)],event,cfg)['net_points']==-12
    assert study.outcome([dict(o=85,h=125,l=80,c=110)],event,cfg)['net_points']==-17


def test_final_lock_prevents_reading_holdout_again(tmp_path,monkeypatch):
    import json,hashlib,sys
    study=research_module()
    source=Path(study.__file__)
    plan=json.loads(source.with_name('cvd_study_plan.json').read_text())
    copy=tmp_path/'cvd_study.py';copy.write_text(source.read_text())
    planpath=tmp_path/'plan.json';planpath.write_text(json.dumps(plan))
    registry=tmp_path/'cvd_study_registry';registry.mkdir()
    (registry/(hashlib.sha256(plan['study_id'].encode()).hexdigest()+'.final')).write_text('consumed')
    monkeypatch.setattr(study,'__file__',str(copy))
    monkeypatch.setattr(sys,'argv',['cvd_study.py','--db',str(tmp_path/'does-not-exist.sqlite3'),
        '--output',str(tmp_path/'results'),'--plan',str(planpath),'--phase','final','--unlock-final'])
    with pytest.raises(ValueError,match='holdout not read'):
        study.main()
    ledger=[json.loads(line) for line in (registry/'trials.jsonl').read_text().splitlines()]
    assert ledger[-1]['status']=='failed'


def test_gap_cannot_create_cross_segment_divergence_or_slope(monkeypatch):
    bars=fixture()[:9]
    original=engine._custom(bars,None,None,STEP,T0+100*STEP)
    # Without the split these bars produce the test's bearish divergence.
    assert run(bars)['active_divergence'] is not None
    for i,b in enumerate(original):b['segment']=0 if i<4 else 1
    monkeypatch.setattr(engine,'_live',lambda *a: (original,{'source':'test','alerts_enabled':True}))
    result=engine.compute_cvd_divergence(as_of=T0+100*STEP,cfg={'price_range_mult':0,'delta_scale_mult':0})
    assert result['active_divergence'] is None and not result['recent_divergences']
    assert result['cvd_slope_delta']==original[-1]['cvd']-original[-4]['cvd']


def test_missing_latest_delta_suspends_alerts_and_keeps_recorded_values(monkeypatch):
    minutes=mock_live(monkeypatch)
    class Adapter:
        def prepare(self,*a):pass
        def get_delta(self,row,tf):return None if row['ts']==minutes[-1]['ts'] else 2
    monkeypatch.setattr(engine.adapters,'make',lambda _:Adapter())
    result=engine.compute_cvd_divergence(as_of=T0+540)
    assert result['ok'] and not result['alerts_enabled']
    assert result['current_price']==100 and result['transitions']==[]
    assert 'Delta gap' in result['message']


def test_decreasing_snapshot_volume_preserves_legacy_minute_buffer(monkeypatch):
    from app.delta.recorder import FutRecorder
    recorder=FutRecorder(lambda:100.)
    monkeypatch.setattr(recorder.audit,'on_packet',lambda *a:None)
    packet=dict(sid='NOV',ltp=101.,volume=100,bid=100.,ask=101.)
    recorder.on_packet(packet,P0)
    recorder.on_packet(dict(packet,volume=120),P0+1)
    recorder.on_packet(dict(packet,volume=110),P0+2)
    assert recorder.prev_vol==120
    assert recorder.min[P0]['v']==20
    recorder.on_packet(dict(packet,volume=130),P0+3)
    assert recorder.min[P0]['v']==30
