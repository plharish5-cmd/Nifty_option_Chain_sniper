"""No real HTTP/credentials: all downloader writes are isolated in tmp_path."""
import copy
import json
from datetime import datetime,timedelta
from pathlib import Path
import pytest
import requests
from analyst import stockmojo_downloader_8510 as m

@pytest.fixture(autouse=True)
def no_real_credentials(monkeypatch):
    for k in m.AUTH_KEYS:monkeypatch.delenv(k,raising=False)

@pytest.fixture
def payload():
    return {'c':[[31,31,10]],'t':['2026-11-05 15:40:00','2026-11-06 09:15:00'],
        **{k:[100,200] for k in m.ARRAYS}}

class Response:
    def __init__(self,data,code=200,headers=None):self.data=data;self.status_code=code;self.headers=headers or {}
    def __enter__(self):return self
    def __exit__(self,*args):return False
    def iter_content(self,chunk_size):yield json.dumps(self.data).encode()
class Session:
    def __init__(self,response):self.response=response;self.calls=[];self.trust_env=True;self.closed=False
    def post(self,url,**kwargs):self.calls.append((url,kwargs));return self.response
    def close(self):self.closed=True

def downloader(tmp_path,payload,code=200,headers=None):
    session=Session(Response(payload,code,headers))
    d=m.Downloader(tmp_path,session=session,clock=lambda:datetime(2026,11,7,10,tzinfo=m.IST))
    return d,session

def auth(monkeypatch):
    monkeypatch.setenv(m.AUTH_KEYS[0],'fake-jwt-never-real');monkeypatch.setenv(m.AUTH_KEYS[1],'fake-session-never-real')

def test_authenticated_request_and_daily_archive(tmp_path,payload,monkeypatch,capsys):
    auth(monkeypatch);d,s=downloader(tmp_path,payload);result=d.fetch_once()
    assert result['status']=='DOWNLOADED'
    url,kw=s.calls[0]
    assert url=='https://data.stockmojo.in/live/multi-strike/ce-pe-oi-diff'
    assert kw['json']=={'symbol':7,'expiry':31,'range':10}
    assert kw['headers']['J']=='fake-jwt-never-real' and kw['headers']['T']=='fake-session-never-real'
    assert not kw['allow_redirects'] and not s.trust_env
    raw=Path(result['latest_file']);assert raw.parent.name=='2026-11-07'
    assert json.loads(raw.read_text())==payload
    metadata=json.loads(Path(result['metadata_file']).read_text())
    assert metadata['freshness']=='HISTORICAL_STALE_OR_FUTURE'
    assert metadata['telemetry_ingestion_authorized'] is False
    assert metadata['source_last_ist'].startswith('2026-11-06')
    assert 'fake-jwt' not in capsys.readouterr().out
    assert 'fake-session' not in ''.join(p.read_text() for p in d.output.rglob('*') if p.is_file())

@pytest.mark.parametrize('code',[401,403])
def test_auth_denial_stops_without_retry(tmp_path,payload,monkeypatch,code):
    auth(monkeypatch);d,s=downloader(tmp_path,payload,code)
    assert d.run(once=False,all_hours=True)==2
    assert len(s.calls)==1 and s.closed
    assert not list(d.output.glob('*/*.json'))

@pytest.mark.parametrize('code',[429,503,500,302])
def test_status_backoff_and_no_redirect(tmp_path,payload,monkeypatch,code):
    auth(monkeypatch);d,s=downloader(tmp_path,payload,code,{'Retry-After':'900'})
    result=d.fetch_once();assert result['status']=='HTTP_BACKOFF' and result['retry_in_seconds']>=900
    assert len(s.calls)==1


def test_environment_file_selective_and_no_missing_token_request(tmp_path,payload):
    (tmp_path/'.env').write_text("DHAN_ACCESS_TOKEN=do-not-use\nSTOCKMOJO_JWT_J='private-j'\nSTOCKMOJO_TOKEN_T=private-t\n")
    assert m.credentials(tmp_path)==dict(zip(m.AUTH_KEYS,('private-j','private-t')))
    (tmp_path/'.env').write_text('DHAN_ACCESS_TOKEN=do-not-use\n')
    d,s=downloader(tmp_path,payload)
    assert d.fetch_once()['status']=='AUTH_NOT_CONFIGURED' and not s.calls


def test_private_setup_preserves_other_keys(tmp_path,monkeypatch,capsys):
    (tmp_path/'.env').write_text('DHAN_ACCESS_TOKEN=preserve-exactly\nOTHER=123\n')
    entered=iter(['new-j','new-t'])
    monkeypatch.setattr(m.sys.stdin,'isatty',lambda:True)
    monkeypatch.setattr(m.getpass,'getpass',lambda prompt:next(entered))
    m.configure(tmp_path)
    assert (tmp_path/'.env').read_text().startswith('DHAN_ACCESS_TOKEN=preserve-exactly\nOTHER=123\n')
    assert (tmp_path/'.env').stat().st_mode&0o777==0o600
    assert 'new-j' not in capsys.readouterr().out


def test_market_schedule_and_intervals(tmp_path,payload):
    d,s=downloader(tmp_path,payload)
    assert d.active(datetime(2026,11,6,9,15,tzinfo=m.IST))
    assert not d.active(datetime(2026,11,6,15,31,tzinfo=m.IST))
    assert not d.active(datetime(2026,11,7,10,tzinfo=m.IST))
    assert all(150<=d.interval()<=180 for _ in range(100))


def test_single_instance_lock(tmp_path,payload):
    a,_=downloader(tmp_path,payload);b,_=downloader(tmp_path,payload);a.acquire()
    try:
        with pytest.raises(ValueError,match='ALREADY_RUNNING'):b.acquire()
    finally:a.close()
    b.acquire();b.close()


def test_duplicate_snapshots_preserved_as_separate_downloads(tmp_path,payload):
    d,_=downloader(tmp_path,payload)
    a=d.archive(payload);b=d.archive(payload)
    assert a['latest_file']!=b['latest_file']
    assert json.loads(Path(b['metadata_file']).read_text())['same_as_previous_download']


def test_day_rollover_and_ordered_diagnostic_csv(tmp_path,payload):
    d,_=downloader(tmp_path,payload)
    payload={**payload,'t':list(reversed(payload['t']))}
    first=d.archive(payload,datetime(2026,11,6,15,30,tzinfo=m.IST))
    second=d.archive(payload,datetime(2026,11,7,9,15,tzinfo=m.IST))
    assert Path(first['latest_file']).parent.name=='2026-11-06'
    assert Path(second['latest_file']).parent.name=='2026-11-07'
    assert Path(second['diagnostic_csv']).read_text().splitlines()[1].startswith('2026-11-05')

@pytest.mark.parametrize('failure',['missing','length','invalid_time','duplicate','nonnumeric','nan'])
def test_invalid_response_never_becomes_market_archive(tmp_path,payload,monkeypatch,failure):
    auth(monkeypatch)
    if failure=='missing':payload.pop('po')
    if failure=='length':payload['po'].pop()
    if failure=='invalid_time':payload['t'][0]='bad'
    if failure=='duplicate':payload['t'][0]=payload['t'][1]
    if failure=='nonnumeric':payload['co'][0]='=HYPERLINK("bad")'
    if failure=='nan':payload['f'][0]=float('nan')
    d,s=downloader(tmp_path,payload);r=d.fetch_once()
    assert r['status']=='DOWNLOAD_ERROR'
    assert not list(d.output.glob('*/*.json'))


def test_network_errors_do_not_echo_sensitive_exception(tmp_path,payload,monkeypatch,capsys):
    auth(monkeypatch);d,s=downloader(tmp_path,payload)
    def fail(*a,**k):raise requests.ConnectionError('fake-jwt-never-real')
    s.post=fail;r=d.fetch_once()
    assert r['error_type']=='ConnectionError' and 'fake-jwt' not in capsys.readouterr().out


def test_credentials_cannot_be_redirected_to_different_service(tmp_path):
    cfg=json.loads(m.RULES.read_text());cfg['endpoint']='https://example.com/collect'
    with pytest.raises(ValueError,match='destination'):m.Downloader(tmp_path,rules=cfg)


def test_size_limit_preserves_no_partial_snapshot(tmp_path,payload,monkeypatch):
    auth(monkeypatch);d,_=downloader(tmp_path,payload);d.rules=copy.deepcopy(d.rules);d.rules['max_response_bytes']=10
    assert d.fetch_once()['status']=='DOWNLOAD_ERROR'
    assert not list(d.output.glob('*/*.json'))


@pytest.mark.parametrize('upper',[False,True])
def test_copied_curl_extracts_headers_without_execution(tmp_path,upper):
    j,t=('J','T') if upper else ('j','t')
    text="curl --url 'https://data.stockmojo.in/live/multi-strike/ce-pe-oi-diff' \\\n -H '"+j+": fake-j' \\\n --header '"+t+": fake-t' --data-raw '{\"symbol\":7,\"expiry\":31,\"range\":10}' ; touch "+str(tmp_path/'never-executed')
    assert m.parse_copied_request(text)==dict(zip(m.AUTH_KEYS,('fake-j','fake-t')))
    assert not (tmp_path/'never-executed').exists()

@pytest.mark.parametrize('text',[
 "curl https://example.com -H 'j: private' -H 't: private'",
 "curl https://data.stockmojo.in/live/multi-strike/ce-pe-oi-diff -H 'j: private'",
 "curl https://data.stockmojo.in/live/multi-strike/ce-pe-oi-diff -H 'j: private' -H 'j: other' -H 't: private'",
 "curl https://data.stockmojo.in/live/multi-strike/ce-pe-oi-diff -H 'j: unfinished"
])
def test_bad_copied_request_rejected_without_echoing_secrets(text):
    with pytest.raises(ValueError) as error:m.parse_copied_request(text)
    assert 'private' not in str(error.value)

@pytest.mark.parametrize('valid',[True,False])
def test_hidden_request_input_always_restores_terminal_echo(tmp_path,monkeypatch,capsys,valid):
    import io
    text="curl https://data.stockmojo.in/live/multi-strike/ce-pe-oi-diff -H 'j: fake-private-j' -H 't: fake-private-t'\nEND\n" if valid else 'not a request\nEND\n'
    terminal=io.StringIO(text);monkeypatch.setattr(terminal,'isatty',lambda:True);monkeypatch.setattr(terminal,'fileno',lambda:12);monkeypatch.setattr(m.sys,'stdin',terminal)
    original=[0,0,0,m.termios.ECHO,0,0,[]];states=[]
    monkeypatch.setattr(m.termios,'tcgetattr',lambda fd:original)
    monkeypatch.setattr(m.termios,'tcsetattr',lambda fd,when,state:states.append(list(state)))
    if valid:m.configure_request(tmp_path)
    else:
        with pytest.raises(ValueError):m.configure_request(tmp_path)
    assert states[-1]==original and states[0][3]&m.termios.ECHO==0
    assert 'fake-private' not in capsys.readouterr().out
