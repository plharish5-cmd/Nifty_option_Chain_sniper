"""Synthetic mechanics only. No provider/model profitability claims, no active files."""
import copy
import hashlib
import json
import sqlite3
import time
from datetime import datetime,timedelta
from pathlib import Path
import pytest
from analyst.stockmojo_research_8510 import ResearchArchive,analyze,number,load_rules,IST
from analyst.stockmojo_paper_8510 import PaperBook,RecordedQuotes
from analyst.stockmojo_workspace_8510 import Workspace,Commentary,metrics

@pytest.fixture
def rules():return load_rules()


def manifest(symbol=1,underlying='NIFTY'):
    names={'future_ltp':('f','POINTS'),'pcr_total':('pcr','RATIO'),'put_oi_day':('pd','CONTRACTS'),'call_oi_day':('cd','CONTRACTS'),'future_oi_day':('fd','CONTRACTS'),'pe_ce_day':('gap','CONTRACTS')}
    return {'review_status':'REVIEWED','reviewed_by':'SYNTHETIC_TEST','request':{'symbol':symbol,'expiry':2,'range':10},
        'instrument':{'underlying':underlying,'exchange':'NSE','options_expiry':'2026-11-09','futures_expiry':'2026-11-23','futures_contract':'TEST FUT'},
        'evidence':{k:'SYNTHETIC FIXTURE NOT PROVIDER EVIDENCE' for k in ('selection','field_dictionary','timestamps')},'timezone':'Asia/Kolkata','timestamp_semantics':'MINUTE_END','oi_scope':'REQUESTED_RANGE','source_interval_seconds':60,
        'field_map':{k:{'raw_key':v[0],'unit':v[1],'meaning':k,'evidence':'SYNTHETIC'} for k,v in names.items()}}


def archive(root,m=None,start='2026-11-06T09:15:00+05:30',count=12,folder='2026-11-08',stem='one',origin='HTTP_200_RESPONSE'):
    m=m or manifest();directory=root/'data/stockmojo_downloads'/folder;directory.mkdir(parents=True,exist_ok=True)
    stamps=[(datetime.fromisoformat(start)+timedelta(minutes=i)).isoformat() for i in range(count)]
    payload={'t':stamps,'f':[25000+i*10 for i in range(count)],'pcr':[1+i*.01 for i in range(count)],'pd':[1000+i*100 for i in range(count)],'cd':[500+i*20 for i in range(count)],'fd':[50+i*3 for i in range(count)],'gap':[500+i*80 for i in range(count)]}
    raw=json.dumps(payload).encode();(directory/(stem+'.json')).write_bytes(raw)
    meta={'request':m['request'],'raw_sha256':hashlib.sha256(raw).hexdigest(),'origin':origin,'downloaded_at_ist':'2026-11-08T01:55:00+05:30'}
    path=directory/(stem+'.meta.json');path.write_text(json.dumps(meta));md=root/'analyst/stockmojo_verified_manifests';md.mkdir(parents=True,exist_ok=True);(md/(str(m['request']['symbol'])+'.json')).write_text(json.dumps(m));return path


def rows(root):
    archive(root);return ResearchArchive(root).sessions()[0][0]['rows']

@pytest.mark.parametrize('value,expected',[(12000,12000),('1.2L',120000),('−1,200 K',-1200000),('+2Cr',20000000),('bad',None),(True,None),(None,None),('NaN',None),('0',0),('-1.5',-1.5)])
def test_numeric_units(value,expected):assert number(value)==expected


def test_identity_separate_expiries_and_source_date(tmp_path):
    archive(tmp_path);archive(tmp_path,manifest(7,'NIFTYNXT50'),stem='two')
    sessions,errors=ResearchArchive(tmp_path).sessions();assert not errors and len(sessions)==2
    assert {s['instrument']['underlying'] for s in sessions}=={'NIFTY','NIFTYNXT50'}
    assert all(s['date']=='2026-11-06' for s in sessions)
    assert sessions[0]['instrument']['options_expiry']!=sessions[0]['instrument']['futures_expiry']


def test_unknown_units_block(tmp_path,rules):
    m=manifest();m['field_map']['put_oi_day']['unit']='UNKNOWN';archive(tmp_path,m)
    rr=ResearchArchive(tmp_path).sessions()[0][0]['rows'];assert analyze(rr,rules,datetime(2026,11,6,10,tzinfo=IST))['signal']=='BLOCKED'


def test_raw_not_scaled_and_provider_increment_distinct(tmp_path):
    m=manifest();m['field_map']['put_oi_change']={'raw_key':'gap','unit':'CONTRACTS','meaning':'put_oi_change','evidence':'fixture'};archive(tmp_path,m)
    rr=ResearchArchive(tmp_path).sessions()[0][0]['rows'];assert rr[0]['put_oi_day']==1000
    s=analyze(rr,now=datetime(2026,11,6,10,tzinfo=IST));assert s['factors']['put_oi_change']!=s['factors']['put_calculated_increment']


def test_pcr_not_derived_from_changes(tmp_path):
    rr=rows(tmp_path);assert rr[0]['pcr_total']==1 and rr[0]['put_oi_day']/rr[0]['call_oi_day']==2


def test_same_download_dedup_conflicts(tmp_path):
    archive(tmp_path);archive(tmp_path,stem='two');ss,_=ResearchArchive(tmp_path).sessions();assert len(ss[0]['rows'])==12
    path=tmp_path/'data/stockmojo_downloads/2026-11-08/two.json';p=json.loads(path.read_text());p['pd'][0]+=1;raw=json.dumps(p).encode();path.write_bytes(raw)
    mp=path.with_name('two.meta.json');m=json.loads(mp.read_text());m['raw_sha256']=hashlib.sha256(raw).hexdigest();mp.write_text(json.dumps(m))
    ss,_=ResearchArchive(tmp_path).sessions();assert 'CONFLICTING_DUPLICATE' in ss[0]['flags']


def test_hash_corruption_invalid(tmp_path):
    p=archive(tmp_path);p.with_name('one.json').write_text('{}');ss,errors=ResearchArchive(tmp_path).sessions();assert not ss and errors[0]['state']=='INVALID'


def test_causal_and_reproducible(tmp_path,rules):
    rr=rows(tmp_path);at=datetime(2026,11,6,9,22,tzinfo=IST)
    a=analyze(rr,rules,at);b=analyze(rr[:8],rules,at);assert a==b and a['signal']=='BUY_CE'
    altered=copy.deepcopy(rr);altered[-1]['put_oi_day']=-100000;assert analyze(altered,rules,at)==a


def test_interval_oi_structure_not_daily_sign(tmp_path):
    rr=rows(tmp_path);rr[-1]['future_oi_day']=rr[-2]['future_oi_day']-1
    s=analyze(rr,now=datetime(2026,11,6,10,tzinfo=IST));assert s['factors']['future_structure']=='SHORT_COVERING'


@pytest.mark.parametrize('mode,origin,expected',[('LIVE_FRESH','HTTP_200_RESPONSE','STALE'),('HISTORICAL_REPLAY','USER_SUPPLIED_SAMPLE_NOT_A_NETWORK_DOWNLOAD','SAMPLE')])
def test_freshness_and_sample(tmp_path,mode,origin,expected):
    archive(tmp_path,origin=origin);rr=ResearchArchive(tmp_path).sessions()[0][0]['rows']
    s=analyze(rr,now=datetime(2026,11,8,10,tzinfo=IST),mode=mode);assert s['state']==expected and s['signal']=='BLOCKED'


def test_gap_and_previous_day_no_join(tmp_path):
    rr=rows(tmp_path);rr[-1]['timestamp']='2026-11-06T10:00:00+05:30';rr[-1]['completed_at']=rr[-1]['timestamp']
    assert 'GAPPED_WINDOW' in analyze(rr,now=datetime(2026,11,6,10,tzinfo=IST))['quality_flags']
    rr[-1]['timestamp']='2026-11-09T09:15:00+05:30';rr[-1]['completed_at']=rr[-1]['timestamp']
    assert 'WARMING_UP' in analyze(rr,now=datetime(2026,11,9,9,15,tzinfo=IST))['quality_flags']


def signal_quote(root,rules):
    rr=rows(root);s=analyze(rr,rules,datetime(2026,11,6,10,tzinfo=IST))
    q={'underlying':'NIFTY','expiry':'2026-11-09','strike':25000,'side':'CE','security_id':'TEST1','lot_size':65,'timestamp':datetime.fromisoformat(s['completed_at']).timestamp(),'bid':199,'ask':200,'verified':True,'source':'RECORDED_CHAIN_SNAP_BID_ASK'}
    return s,q


def test_paper_off_missing_quotes_and_isolation(tmp_path,rules):
    s,q=signal_quote(tmp_path,rules);b=PaperBook(tmp_path/'paper.sqlite3',rules,lambda a,b,q:83)
    assert b.process('replay:one',s,q)['status']=='PAPER_DISABLED'
    assert b.process('replay:one',s,None,True)['reason']=='MISSING_OPTION_QUOTES'
    assert b.snapshot('replay:one')['position'] is None
    assert b.process('live-paper:one',s,q,True)['status']=='OPEN'
    assert b.snapshot('replay:one')['position'] is None


def test_dedup_restart_and_actual_premium_costs(tmp_path,rules):
    s,q=signal_quote(tmp_path,rules);path=tmp_path/'book.sqlite3';b=PaperBook(path,rules,lambda a,b,q:83)
    assert b.process('replay:one',s,q,True)['status']=='OPEN'
    assert PaperBook(path,rules).process('replay:one',s,q,True)['duplicate']
    s=copy.deepcopy(s);s['event_id']='next';s['source_time']='2026-11-06T09:27:00+05:30';s['completed_at']=s['source_time'];q=copy.deepcopy(q);q.update(timestamp=parse_timestamp(s['source_time']),bid=240,ask=241)
    result=b.process('replay:one',s,q,True);assert result['status']=='CLOSED'
    fill=result['fill'];assert fill['entry_price']==200.1 and fill['exit_price']==239.9 and fill['charges']==83
    assert len(b.snapshot('replay:one')['fills'])==1


def parse_timestamp(s):return datetime.fromisoformat(s).timestamp()


@pytest.mark.parametrize('key,value',[('expiry','2026-11-23'),('underlying','NIFTYNXT50'),('bid',0),('lot_size',0),('timestamp',1),('verified',False)])
def test_bad_quotes_block(tmp_path,rules,key,value):
    s,q=signal_quote(tmp_path,rules);q[key]=value;b=PaperBook(tmp_path/'paper.sqlite3',rules)
    assert b.process('replay:one',s,q,True)['reason']=='MISSING_OPTION_QUOTES'


def test_risk_capital_guard(tmp_path,rules):
    s,q=signal_quote(tmp_path,rules);rules['paper']['capital']=1;b=PaperBook(tmp_path/'paper.sqlite3',rules,lambda a,b,c:80)
    assert b.process('replay:one',s,q,True)['reason']=='CAPITAL_OR_POSITION_LIMIT'


def test_quote_store_readonly_and_no_future_quote(tmp_path,rules):
    s,q=signal_quote(tmp_path,rules);rr=ResearchArchive(tmp_path).sessions()[0][0]['rows']
    assert RecordedQuotes(tmp_path,rules).get(rr[-1],'CE') is None
    cat=tmp_path/'analyst/stockmojo_option_contracts.json';cat.write_text(json.dumps({'review_status':'REVIEWED','evidence':'SYNTHETIC','contracts':[q]}))
    db=tmp_path/'ict_smc_app/backend/data/ict_smc.sqlite3';db.parent.mkdir(parents=True)
    with sqlite3.connect(db) as c:
        c.execute('CREATE TABLE chain_snap(ts INTEGER,expiry TEXT,strike REAL,ce_bid REAL,ce_ask REAL)');c.execute('INSERT INTO chain_snap VALUES(?,?,?,?,?)',(q['timestamp']+1,q['expiry'],q['strike'],q['bid'],q['ask']))
    assert RecordedQuotes(tmp_path,rules).get(rr[-1],'CE') is None


def test_workspace_replay_prefix_and_restart(tmp_path,rules):
    archive(tmp_path);w=Workspace(tmp_path,rules);sid=w.catalog()['sessions'][0]['id'];body={'session':sid,'run':'test','cursor':7}
    out=w.evaluate(body);assert len(out['rows'])==8 and len(out['timeline'])==8
    assert out['paper']['fills']==[] and not out['paper_enabled']
    assert len(Workspace(tmp_path,rules).evaluate(body)['timeline'])==8
    with pytest.raises(ValueError):w.evaluate({**body,'cursor':2})
    out=w.evaluate({**body,'cursor':2,'run':'reset'});assert len(out['rows'])==3


@pytest.mark.parametrize('response',[None,{'response':'{}'}])
def test_qwen_failure_nonblocking(tmp_path,rules,response):
    s,q=signal_quote(tmp_path,rules)
    def transport(*a,**k):
        if response is None:raise TimeoutError('private text must not leak')
        return type('Response',(),{'status_code':200,'json':lambda self:response})()
    c=Commentary(rules,transport);t=time.monotonic();c.get(s,True);assert time.monotonic()-t<.2
    for _ in range(100):
        if not c.running:break
        time.sleep(.005)
    assert c.get(s)['status']=='DETERMINISTIC_FALLBACK'
    assert 'private text' not in json.dumps(c.get(s))


def test_no_broker_imports_or_order_endpoints():
    root=Path(__file__).parent
    for name in ('stockmojo_research_8510.py','stockmojo_paper_8510.py','stockmojo_workspace_8510.py'):
        text=(root/name).read_text();assert 'dhanhq' not in text and 'order/submit' not in text


def test_tariff_reuses_existing_engine(tmp_path,rules):
    from paper_trading import calculate_trade_charges
    b=PaperBook(tmp_path/'book.sqlite3',rules)
    assert b._charges(200,240,65)==calculate_trade_charges(200,240,65,cfg=rules['fees'])['total_charges']


def test_private_reconfiguration_updates_payload_without_leaking(tmp_path,monkeypatch,capsys):
    import io
    from analyst import stockmojo_downloader_8510 as d
    rules=json.loads(d.RULES.read_text());path=tmp_path/'analyst/stockmojo_download_rules_8510.json';path.parent.mkdir();path.write_text(json.dumps(rules));env=tmp_path/'.env';env.write_text('OTHER=preserved\n')
    class PrivateInput(io.StringIO):
        def isatty(self):return True
        def fileno(self):return 0
    request="curl 'https://data.stockmojo.in/live/multi-strike/ce-pe-oi-diff' -H 'j: synthetic-private-j' -H 't: synthetic-private-t' --data-raw '{\"symbol\":3,\"expiry\":44,\"range\":10}'\nEND\n"
    monkeypatch.setattr(d.sys,'stdin',PrivateInput(request));monkeypatch.setattr(d.termios,'tcgetattr',lambda _: [0,0,0,8,0,0,[]]);monkeypatch.setattr(d.termios,'tcsetattr',lambda *a:None)
    d.configure_request(tmp_path,True)
    saved=json.loads(path.read_text());assert saved['payload']=={'symbol':3,'expiry':44,'range':10}
    assert saved['interval_min_seconds']==150 and saved['interval_max_seconds']==180
    assert 'OTHER=preserved' in env.read_text() and env.stat().st_mode&0o777==0o600
    output=capsys.readouterr().out;assert 'synthetic-private' not in output and 'UNVERIFIED' in saved['instrument_label']


def test_named_export_explicit_date_and_rounding(tmp_path):
    path=archive(tmp_path);raw=json.dumps([{'Time':'09:15','Put OI Change (Day)':'1.2L','Call OI Change (Day)':'1L','PE-CE OI Change (Day)':'0.2L','PCR Total':1.3,'Future LTP':25000,'Future OI Change (Day)':500}]).encode()
    path.with_name('one.json').write_bytes(raw);meta=json.loads(path.read_text());meta['raw_sha256']=hashlib.sha256(raw).hexdigest();path.write_text(json.dumps(meta))
    assert ResearchArchive(tmp_path).sessions()[1]
    meta['source_trading_date']='2026-11-06';path.write_text(json.dumps(meta))
    m=manifest();mapping={'put_oi_day':'Put OI Change (Day)','call_oi_day':'Call OI Change (Day)','pe_ce_day':'PE-CE OI Change (Day)','pcr_total':'PCR Total','future_ltp':'Future LTP','future_oi_day':'Future OI Change (Day)'}
    for k,v in mapping.items():m['field_map'][k]['raw_key']=v
    (tmp_path/'analyst/stockmojo_verified_manifests/1.json').write_text(json.dumps(m))
    ss,e=ResearchArchive(tmp_path).sessions();assert not e and ss[0]['rows'][0]['put_oi_day']==120000
    assert ss[0]['rows'][0]['verified']


def test_qwen_success_cached_and_no_future_fields(tmp_path,rules):
    s,q=signal_quote(tmp_path,rules);seen=[]
    def transport(*a,**kw):
        seen.append(kw['json']);candidate={k:[] for k in ('supporting_evidence','opposing_evidence','missing_information','risk_notes')};candidate.update(summary='Synthetic description',interpretation='Python remains authoritative')
        return type('R',(),{'status_code':200,'json':lambda self:{'response':json.dumps(candidate)}})()
    c=Commentary(rules,transport);c.get(s,True)
    for _ in range(100):
        if not c.running:break
        time.sleep(.005)
    for _ in range(5):c.get(s,True)
    assert len(seen)==1 and c.get(s)['status']=='QWEN_LOCAL_ADVISORY'
    packet=json.loads(seen[0]['prompt']);assert packet['source_time']==s['source_time'] and 'rows' not in packet


def test_chronological_comparison_does_not_fit(tmp_path,rules):
    from analyst.stockmojo_research_8510 import chronological_comparison
    rr=rows(tmp_path);results=chronological_comparison(rr,rules)
    assert len(results)==6 and {x['partition'] for x in results}=={'TRAIN','VALIDATION','TEST'}
    assert all(x['option_expectancy'] is None and x['profit_factor'] is None for x in results)


def test_paper_loss_and_cooldown_guards(tmp_path,rules):
    s,q=signal_quote(tmp_path,rules);b=PaperBook(tmp_path/'book.sqlite3',rules,lambda a,b,c:0)
    b.process('replay:a',s,q,True)
    exit_s=copy.deepcopy(s);exit_s.update(event_id='stop',completed_at='2026-11-06T09:27:00+05:30',source_time='2026-11-06T09:27:00+05:30');q.update(timestamp=parse_timestamp(exit_s['completed_at']),bid=180,ask=181)
    assert b.process('replay:a',exit_s,q,True)['status']=='CLOSED'
    entry=copy.deepcopy(exit_s);entry.update(event_id='retry',completed_at='2026-11-06T09:28:00+05:30');q.update(timestamp=parse_timestamp(entry['completed_at']))
    assert b.process('replay:a',entry,q,True)['reason']=='COOLDOWN'
    rules['paper']['daily_loss_limit']=1;entry.update(event_id='loss',completed_at='2026-11-06T09:32:00+05:30');q.update(timestamp=parse_timestamp(entry['completed_at']))
    assert b.process('replay:a',entry,q,True)['reason']=='DAILY_PAPER_LOSS_LIMIT'


def test_replay_rerender_and_crash_recovery_keep_original_signal(tmp_path,rules):
    archive(tmp_path);w=Workspace(tmp_path,rules);ss=w.catalog()['sessions'];sid=ss[0]['id']
    def quote(row,side,contract_id=None):
        return {'underlying':'NIFTY','expiry':'2026-11-09','strike':25000,'side':side,'security_id':'TEST1','lot_size':65,'timestamp':parse_timestamp(row['completed_at']),'bid':200,'ask':201,'verified':True,'source':'RECORDED_CHAIN_SNAP_BID_ASK'}
    w.quotes.get=quote;w.book.fees=lambda a,b,c:80
    body={'session':sid,'run':'recovery','cursor':7,'paper_enabled':True};first=w.evaluate(body)
    second=w.evaluate(body);assert first['signal']==second['signal'] and first['timeline']==second['timeline']
    with w.book.connect() as c:
        c.execute('UPDATE replay_progress SET cursor=?,timeline=? WHERE account=?',(6,json.dumps(first['timeline'][:7]),'replay:recovery'))
    # Simulates crash after atomic paper event commit, before cursor commit.
    recovered=w.evaluate(body);assert recovered['signal']==first['signal']
    with w.book.connect() as c:assert c.execute('SELECT count(*) FROM open_positions').fetchone()[0]==1


def test_nxt50_cannot_read_nifty_quote_store(tmp_path,rules):
    rr=rows(tmp_path);rr[-1]['instrument']={**rr[-1]['instrument'],'underlying':'NIFTYNXT50'}
    assert RecordedQuotes(tmp_path,rules).get(rr[-1],'CE') is None


def test_actual_research_router_without_trading_app_startup(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from analyst.stockmojo_workspace_8510 import router_for
    archive(tmp_path);app=FastAPI();app.include_router(router_for(tmp_path))
    with TestClient(app) as client:
        catalog=client.get('/api/stockmojo/research/sessions');assert catalog.status_code==200
        sid=catalog.json()['sessions'][0]['id']
        res=client.post('/api/stockmojo/research/evaluate',json={'session':sid,'run':'api-test','cursor':7});assert res.status_code==200
        assert res.json()['paper_enabled'] is False and len(res.json()['rows'])==8
        assert client.post('/api/stockmojo/research/evaluate',json={'session':sid,'run':'../../escape'}).status_code==400
        script=client.get('/api/stockmojo/research/panel.js');assert script.status_code==200 and 'textContent' in script.text
