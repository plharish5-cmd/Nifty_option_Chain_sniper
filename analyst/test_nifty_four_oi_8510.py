"""SYNTHETIC mechanics except explicitly labelled owner arithmetic fragment."""
import copy
import json
from datetime import datetime,timedelta
from pathlib import Path
import pytest
from analyst.nifty_four_oi_8510 import normalize,analyze_four,rules,parse_time,checkpoint,IST
from analyst.nifty_four_collector_8510 import collect_exports
from analyst.nifty_four_paper_8510 import FourPaper
from analyst.nifty_four_workspace_8510 import FourWorkspace,FourCommentary
from analyst.stockmojo_research_8510 import load_rules,number


def metadata():
    return {'timezone':'Asia/Kolkata','instrument':'NIFTY','instrument_status':'VERIFIED','selection_provenance':'SYNTHETIC TEST ONLY','mapping_status':'VERIFIED','mapping_provenance':'SYNTHETIC','option_expiry':'2026-11-09','futures_expiry':'2026-11-23','futures_identity_status':'VERIFIED','source_interval':60,'timestamp_semantics':'END','aggregation_scope':'REQUESTED_RANGE','strike_selection':'24500–25500','unit_status':'CONTRACTS','origin':'AUTHORIZED_EXPORT','retrieved_at_ist':'2026-11-06T16:00:00+05:30',
        'baselines':{'2026-11-06':{'total_call_oi':80000,'total_put_oi':100000,'type':'OPENING_OBSERVATION','provenance':'SYNTHETIC','available_at_ist':'2026-11-06T09:15:00+05:30'}}}


def data(n=20):return [[(datetime(2026,11,6,9,15,tzinfo=IST)+timedelta(minutes=i)).isoformat(),80000+i*100,100000+i*300,25000+i*2] for i in range(n)]
def observations(n=20):return normalize(data(n),metadata())['rows']
def signal(rows=None,at='2026-11-06T09:21:00+05:30',mode='IDEAL_REPLAY',pos=None):return analyze_four(rows or observations(),at,mode=mode,position=pos)


def test_owner_screenshot_arithmetic_provisional_not_verified():
    m=metadata();m.update(mapping_status='PROVISIONAL',unit_status='UNVERIFIED',instrument_status='PROVISIONAL');m['baselines']['2026-11-06'].update(total_call_oi=143996710,total_put_oi=193135540,type='UNVERIFIED',available_at_ist=None)
    a=normalize([[[215],['2026-11-06 15:40:00'],{'2026-11-06':[143996710,193135540]}],['2026-11-06 15:40:00',145332135,240203665,24998.5]],m)
    assert a['metadata_elements_excluded']==1 and len(a['rows'])==1
    r=a['rows'][0];assert r['call_oi_change_today']==1335425 and r['put_oi_change_today']==47068125
    assert r['total_put_oi']/r['total_call_oi']==pytest.approx(1.6527874100,rel=1e-5)
    s=signal(a['rows'],'2026-11-06T15:40:00+05:30');assert s['trade_action']=='BLOCKED' and 'PROVISIONAL_MAPPING' in s['quality_flags']


@pytest.mark.parametrize('instrument',['NIFTYNXT50','BANKNIFTY','SENSEX',None])
def test_other_instruments_excluded(instrument):
    m=metadata();m['instrument']=instrument
    with pytest.raises(ValueError,match='NOT_NIFTY_50'):normalize(data(),m)


@pytest.mark.parametrize('value,expected',[(145332135,145332135),('14.53 Cr',145300000),('13.35 L',1335000),('−2K',-2000),(None,None),('1,,2',None)])
def test_units_no_double_scale(value,expected):assert number(value)==expected


def test_baseline_causality_and_precision():
    m=metadata();m['baselines']['2026-11-06']['available_at_ist']='2026-11-06T15:40:00+05:30'
    rr=normalize(data(),m)['rows'];assert 'BASELINE_NOT_AVAILABLE_CAUSALLY' in rr[0]['quality_flags']
    assert signal(rr)['trade_action']=='BLOCKED'
    assert normalize(data(),metadata())['rows'][1]['total_put_oi']==100300


def test_four_values_and_pcr_no_triple_count():
    s=signal();f=s['factors'];assert f['pcr']==pytest.approx(101800/80600)
    assert f['total_gap']==21200 and f['today_gap']==1200
    assert s['trade_action']=='BUY_CE' and s['score']==100
    assert 'pcr' not in s.get('independent_votes',{})


def test_level_vs_recent_flow_and_pcr_decomposition():
    rr=observations();rr[6]['total_put_oi']=rr[5]['total_put_oi']-50;rr[6]['total_call_oi']=rr[5]['total_call_oi']-100
    s=signal(rr);f=s['factors'];assert f['put_current_flow']=='REDUCING' and f['pcr_change']>0
    assert f['pcr_put_component']<0 and f['pcr_call_component']>0
    assert f['pcr_put_component']+f['pcr_call_component']==pytest.approx(f['pcr_change'])
    assert f['dominant_side_today']=='PUT'


def test_irregular_elapsed_time_and_spike_zero_variation():
    rr=observations(9);rr[6]['completed_at_ist']='2026-11-06T09:22:00+05:30';rr=rr[:7]
    s=signal(rr,'2026-11-06T09:22:00+05:30');assert s['factors']['per_second_rates']['total_put_oi']==pytest.approx(300/120)
    a=signal(observations(10),'2026-11-06T09:24:00+05:30');assert a['factors']['spike_mad']==0 and a['factors']['spike'] is False
    rr=observations(10);rr[-1]['put_oi_change_today']+=100000
    b=signal(rr,'2026-11-06T09:24:00+05:30');assert b['market_state']=='FLOW_SPIKE' and b['trade_action']=='HOLD'


def test_causal_prefix_and_checkpoint():
    rr=observations();a=signal(rr);assert a==signal(rr[:7]);rr[-1]['total_put_oi']=1;assert signal(rr)==a
    assert checkpoint('2026-11-06T09:18:00+05:30') and not checkpoint('2026-11-06T09:19:00+05:30')
    assert signal(at='2026-11-06T09:22:00+05:30')['trade_action']=='HOLD'


def test_receipt_replay_cannot_use_eod_download():
    s=signal(mode='RECEIPT_REPLAY');assert s['quality_flags']==['NO_CAUSALLY_AVAILABLE_OBSERVATIONS']
    assert signal(mode='IDEAL_REPLAY')['trade_action']=='BUY_CE'


def test_cutoff_and_stale_and_zero_denominator():
    rr=observations();s=signal(rr,'2026-11-06T15:40:00+05:30');assert s['trade_action']=='HOLD'
    rr[-1]['total_call_oi']=0;rr[-1]['quality_flags'].append('ZERO_CALL_DENOMINATOR');assert signal(rr,rr[-1]['completed_at_ist'])['trade_action']=='BLOCKED'
    rr=observations()
    for x in rr:x['retrieved_at_ist']=x['completed_at_ist']
    assert 'STALE_SOURCE' in signal(rr,'2026-11-06T15:00:00+05:30',mode='LIVE')['quality_flags']


def write_export(root,payload=None,m=None):
    r=rules();p=root/r['export_folder']/'export.json';p.parent.mkdir(parents=True,exist_ok=True);m=m or metadata();m.pop('retrieved_at_ist',None);p.write_text(json.dumps({'metadata':m,'payload':payload or data()}));return p


def test_collector_dedup_atomic_raw_and_no_credentials(tmp_path):
    p=write_export(tmp_path);at=datetime(2026,11,6,16,tzinfo=IST);a=collect_exports(tmp_path,at);b=collect_exports(tmp_path,at)
    assert a['new_events']==20 and b['new_events']==0
    out=tmp_path/rules()['archive_folder'];assert len((out/'observations.jsonl').read_text().splitlines())==20
    assert list(out.glob('*/*.raw.json')) and a['network_requests']==0
    packet=json.loads(p.read_text());packet['metadata']['j']='SYNTHETIC_SECRET';p.write_text(json.dumps(packet));c=collect_exports(tmp_path,at)
    assert c['errors']==['CREDENTIAL_BEARING_EXPORT_REJECTED'] and 'SYNTHETIC_SECRET' not in (out/'status.json').read_text()


def test_encoded_envelopes_block_without_decoder(tmp_path):
    write_export(tmp_path,{'data':'identical-encoded-string'})
    assert collect_exports(tmp_path)['errors']==['AUTHORIZED_DECODING_UNAVAILABLE']


def test_duplicate_conflicts_and_reorder():
    d=data();a=normalize(list(reversed(d))+[d[0]],metadata());assert a['duplicates_collapsed']==1 and a['chronology_reordered']
    d.append([d[0][0],1,2,3])
    with pytest.raises(ValueError,match='CONFLICTING_DUPLICATE'):normalize(d,metadata())


def test_scope_expiry_change_never_joins(tmp_path):
    p=write_export(tmp_path);collect_exports(tmp_path)
    m=metadata();m['option_expiry']='2026-11-16';write_export(tmp_path,m=m);collect_exports(tmp_path)
    assert len(FourWorkspace(tmp_path).catalog()['sessions'])==2


def test_service_ideal_and_receipt_replay_and_restart(tmp_path):
    write_export(tmp_path);collect_exports(tmp_path,datetime(2026,11,8,tzinfo=IST));w=FourWorkspace(tmp_path);sid=w.catalog()['sessions'][0]['id'];body={'session':sid,'run':'test','cursor':6}
    a=w.evaluate(body);b=FourWorkspace(tmp_path).evaluate(body);assert a['signal']==b['signal'] and len(a['rows'])==7
    receipt=w.evaluate({**body,'run':'receipt','mode':'RECEIPT_REPLAY'});assert receipt['rows']==[] and receipt['signal']['trade_action']=='BLOCKED'


def test_deferred_paper_fills_missing_quotes_and_idempotency(tmp_path):
    s=signal();p=FourPaper(tmp_path,load_rules(),rules(),lambda a,b,c:80);contract={'underlying':'NIFTY','expiry':'2026-11-09','strike':25000,'side':'CE','security_id':'TEST','lot_size':65}
    p.quotes.contract=lambda *a:contract
    available=[];p.quotes.next=lambda c,a,b:next((q for q in available if a<q['timestamp']<=b),None)
    at='2026-11-06T09:21:00+05:30';assert p.handle('replay:four-test',s,at,True)['status']=='ENTRY_PENDING'
    assert p.book.snapshot('replay:four-test')['position'] is None
    assert p.handle('replay:four-test',s,at,True)['reason']=='MISSING_OPTION_QUOTES'
    available.append({**contract,'timestamp':parse_time(at).timestamp()+5,'bid':199,'ask':200,'verified':True,'source':'RECORDED_CHAIN_SNAP_BID_ASK'})
    assert p.handle('replay:four-test',s,'2026-11-06T09:21:10+05:30',True)['status']=='OPEN'
    p.handle('replay:four-test',s,'2026-11-06T09:21:10+05:30',True)
    assert p.book.snapshot('replay:four-test')['position']['entry_price']==200.1
    assert p.book.snapshot('live-paper:four-test')['position'] is None
    available.clear();out=p.handle('replay:four-test',s,'2026-11-06T15:25:00+05:30',True);assert out['status']=='EXIT_PENDING'
    assert p.book.snapshot('replay:four-test')['position']['exit_pending'] is True


def test_no_fill_at_same_event_or_outside_window(tmp_path):
    p=FourPaper(tmp_path,load_rules(),rules(),lambda a,b,c:0);s=signal()
    assert p.handle('replay:four-test',s,'2026-11-06T15:40:00+05:30',True)['reason']=='ENTRY_WINDOW_CLOSED'
    assert p.handle('replay:four-test',s,'2026-11-06T09:21:00+05:30',False)['status']=='PAPER_DISABLED'


def test_qwen_nine_field_schema_and_fallback():
    s=signal();seen=[]
    def transport(*a,**kw):
        seen.append(kw['json']);return type('R',(),{'status_code':200,'json':lambda self:{'response':'{}'}})()
    c=FourCommentary(load_rules(),transport);c.get(s,True)
    import time
    for _ in range(100):
        if not c.running:break
        time.sleep(.005)
    assert c.get(s)['status']=='DETERMINISTIC_FALLBACK' and 'dominant_side_today' in c.get(s)
    packet=json.loads(seen[0]['prompt']);assert 'four_inputs' in packet and 'rows' not in packet


def test_incomplete_export_rows_are_held_not_finalized(tmp_path):
    write_export(tmp_path);now=datetime(2026,11,6,9,20,tzinfo=IST)
    a=collect_exports(tmp_path,now);assert a['new_events']==6 and a['pending_source_rows']==14
    b=collect_exports(tmp_path,now+timedelta(minutes=3));assert b['new_events']==3


def test_background_automation_default_off_and_disarm_protection(tmp_path):
    w=FourWorkspace(tmp_path);assert w.live_tick()['status']=='PAPER_DISABLED'
    write_export(tmp_path);collect_exports(tmp_path,datetime(2026,11,6,9,35,tzinfo=IST));sid=w.catalog()['sessions'][0]['id']
    w.clock=lambda:datetime(2026,11,6,9,36,tzinfo=IST)
    enabled=w.set_automation({'enabled':True,'session':sid});assert enabled['run'].startswith('auto-')
    out=w.live_tick();assert out['paper_enabled'] is True and out['mode']=='LIVE'
    assert w.set_automation({'enabled':False})['enabled'] is False
    assert w.live_tick()['status']=='PAPER_DISABLED'


def test_receipt_live_no_retroactive_old_checkpoint(tmp_path):
    write_export(tmp_path);collect_exports(tmp_path,datetime(2026,11,6,9,35,tzinfo=IST))
    w=FourWorkspace(tmp_path,clock=lambda:datetime(2026,11,6,9,36,tzinfo=IST));sid=w.catalog()['sessions'][0]['id']
    out=w.evaluate({'session':sid,'run':'live','mode':'LIVE','paper_enabled':False})
    assert len(out['timeline'])==1 and out['timeline'][0]['decision_at']=='2026-11-06T09:36:00+05:30'
    assert all(x['decision_at']>='2026-11-06T09:36:00+05:30' for x in out['timeline'])


def test_baseline_and_expiry_scope_not_interchanged():
    m=metadata();m['option_expiry']='2026-11-05';rr=normalize(data(),m)['rows'];assert 'EXPIRED_CONTRACT_CONTEXT' in signal(rr)['quality_flags']
    assert normalize(data(),metadata())['rows'][0]['option_expiry']!=normalize(data(),metadata())['rows'][0]['futures_expiry']


def test_negative_daily_changes_are_valid_observations():
    m=metadata();m['baselines']['2026-11-06']['total_put_oi']=110000
    rr=normalize(data(),m)['rows'];assert rr[6]['put_oi_change_today']<0
    assert not rr[6]['quality_flags']


def test_spike_alone_with_no_price_does_not_buy():
    m=metadata();m['futures_identity_status']='UNVERIFIED';rr=normalize(data(),m)['rows']
    s=signal(rr);assert s['trade_action']=='HOLD' and 'PRICE_CONFIRMATION_UNAVAILABLE' in s['contradictions']


def test_risk_limits_reused_at_deferred_fill(tmp_path):
    s=signal();risk=load_rules();risk['paper']['capital']=1;p=FourPaper(tmp_path,risk,rules(),lambda a,b,c:0)
    contract={'underlying':'NIFTY','expiry':'2026-11-09','strike':25000,'side':'CE','security_id':'TEST','lot_size':65};p.quotes.contract=lambda *x:contract
    p.quotes.next=lambda c,a,b:{**c,'timestamp':a+1,'bid':199,'ask':200,'verified':True,'source':'RECORDED_CHAIN_SNAP_BID_ASK'} if b>a+1 else None
    p.handle('replay:four-cap',s,'2026-11-06T09:21:00+05:30',True)
    assert p.handle('replay:four-cap',s,'2026-11-06T09:21:10+05:30',True)['reason']=='CAPITAL_OR_POSITION_LIMIT'


def test_router_nifty_four_schema_and_background_lifecycle(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from analyst.nifty_four_workspace_8510 import router_for_four
    write_export(tmp_path);collect_exports(tmp_path,datetime(2026,11,8,tzinfo=IST));app=FastAPI();app.include_router(router_for_four(tmp_path))
    with TestClient(app) as client:
        catalog=client.get('/four/sessions').json();assert catalog['instrument']=='NIFTY'
        sid=catalog['sessions'][0]['id'];r=client.post('/four/evaluate',json={'session':sid,'run':'api','cursor':6})
        assert r.status_code==200 and 'market_state' in r.json()['signal']
        assert client.post('/four/automation',json={'enabled':False}).json()['enabled'] is False


def test_private_chart_capture_does_not_guess_roles_or_execute(tmp_path,capsys):
    from analyst.nifty_four_private_8510 import parse_chart_request
    from analyst.stockmojo_downloader_8510 import save_credentials
    values=parse_chart_request("curl 'https://data.stockmojo.in/gc/data' -H 'j: SYNTHETIC_PRIVATE_J' -H 't: SYNTHETIC_PRIVATE_T' --data-raw '{\"selected\":\"synthetic\"}'")
    (tmp_path/'.env').write_text('UNRELATED=preserved\n');save_credentials(tmp_path,values)
    assert 'UNRELATED=preserved' in (tmp_path/'.env').read_text()
    assert 'STOCKMOJO_FOUR_REQUEST=' in (tmp_path/'.env').read_text()
    assert (tmp_path/'.env').stat().st_mode&0o777==0o600
    assert 'SYNTHETIC_PRIVATE' not in capsys.readouterr().out
    with pytest.raises(ValueError):parse_chart_request("curl 'https://invalid.example/oi' -H 'j: synthetic' -H 't: synthetic'")


def test_exit_pending_later_quote_closes_with_eod_reason(tmp_path):
    s=signal();p=FourPaper(tmp_path,load_rules(),rules(),lambda a,b,c:80)
    contract={'underlying':'NIFTY','expiry':'2026-11-09','strike':25000,'side':'CE','security_id':'TEST','lot_size':65};p.quotes.contract=lambda *a:contract
    qs=[];p.quotes.next=lambda c,a,b:next((x for x in qs if a<x['timestamp']<=b),None)
    p.handle('replay:four-exit',s,'2026-11-06T09:21:00+05:30',True)
    qs.append({**contract,'timestamp':parse_time('2026-11-06T09:21:05+05:30').timestamp(),'bid':199,'ask':200,'verified':True,'source':'RECORDED_CHAIN_SNAP_BID_ASK'})
    p.handle('replay:four-exit',s,'2026-11-06T09:21:10+05:30',True);qs.clear()
    assert p.handle('replay:four-exit',s,'2026-11-06T15:25:00+05:30',True)['status']=='EXIT_PENDING'
    qs.append({**contract,'timestamp':parse_time('2026-11-06T15:25:05+05:30').timestamp(),'bid':210,'ask':211,'verified':True,'source':'RECORDED_CHAIN_SNAP_BID_ASK'})
    result=p.handle('replay:four-exit',s,'2026-11-06T15:25:10+05:30',True)
    assert result['status']=='CLOSED' and result['fill']['exit_reason']=='END_OF_DAY'
    assert len(p.book.snapshot('replay:four-exit')['fills'])==1


def test_live_quote_selection_does_not_backfill_hours_old_fills(tmp_path):
    p=FourPaper(tmp_path,load_rules(),rules());seen=[];p.quotes.next=lambda c,a,b:seen.append((a,b))
    p.next_quote('live-paper:four-x',{},0,1000);assert seen==[(940,1000)]


def test_qwen_valid_narrative_cached_without_changing_python():
    from analyst.nifty_four_workspace_8510 import TEXT,ARRAYS
    s=signal();candidate={k:[] for k in ARRAYS};candidate.update({k:'Explanation' for k in TEXT});candidate.update(dominant_side_total='PUT',dominant_side_today='PUT');seen=[]
    def transport(*a,**kw):
        seen.append(kw);return type('R',(),{'status_code':200,'json':lambda self:{'response':json.dumps(candidate)}})()
    c=FourCommentary(load_rules(),transport);c.get(s,True)
    import time
    for _ in range(100):
        if not c.running:break
        time.sleep(.005)
    assert c.get(s)['status']=='QWEN_LOCAL_ADVISORY';c.get(s,True);assert len(seen)==1
    assert s['trade_action']=='BUY_CE'


def test_flat_flow_does_not_churn_existing_position():
    rr=observations();rr[6]['put_oi_change_today']=rr[5]['put_oi_change_today']+120;rr[6]['call_oi_change_today']=rr[5]['call_oi_change_today']+100
    s=signal(rr,pos={'side':'CE'});assert s['trade_action']=='HOLD'


def test_invalid_quantity_config_cannot_create_free_cash(tmp_path):
    risk=load_rules();risk['paper']['lots']=-1;p=FourPaper(tmp_path,risk,rules())
    result=p.handle('replay:four-invalid',signal(),'2026-11-06T09:21:00+05:30',True)
    assert result['reason']=='INVALID_PAPER_RISK_CONFIGURATION'
    assert p.book.snapshot('replay:four-invalid')['position'] is None
