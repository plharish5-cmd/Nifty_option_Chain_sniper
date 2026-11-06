"""8510 tests use private configs, ledgers and credentials, never the desk state.

Installed before test collection imports engine_8510_live (which constructs a
backend at module scope). Production data files are never copied back.
"""
import os,sys,json,shutil,tempfile,sqlite3,socket
from pathlib import Path
import pytest

SOURCE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(SOURCE))
_SESSION=tempfile.TemporaryDirectory(prefix='8510-pytest-')
SANDBOX=Path(_SESSION.name)
_ORIGINAL_ENV=dict(os.environ)
for key in ('DHAN_ACCESS_TOKEN','DHAN_API_KEY','DHAN_API_SECRET','TOKEN_EXPIRY_UTC'):
    os.environ[key]=''
os.environ['DHAN_CLIENT_ID']='TEST-CLIENT'
from analyst import terminal8510_backend as terminal
terminal.ROOT=SANDBOX
terminal.REPLAY_DATA_DIR=SANDBOX/'analyst/replay_data'

def config_at(root):
    cfg=json.loads((SOURCE/'analyst/terminal8510_config.json').read_text())
    cfg['ledger_db_path']=str(root/'ledger.sqlite3')
    cfg['market_db_path']=str(SOURCE/'ict_smc_app/backend/data/ict_smc.sqlite3')
    cfg['instrument_master_path']=str(SOURCE/'data/terminal8500/instrument_master.csv')
    cfg['DHAN_CLIENT_ID']='TEST-CLIENT'
    cfg['dhan']['client_id']='TEST-CLIENT'
    for k in ('DHAN_ACCESS_TOKEN','DHAN_API_KEY','DHAN_API_SECRET'):cfg.pop(k,None)
    for k in ('access_token','api_key','api_secret'):cfg['dhan'].pop(k,None)
    path=root/'analyst/terminal8510_config.json';path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(cfg,indent=2))
    (root/'.env').write_text('DHAN_CLIENT_ID=TEST-CLIENT\nDHAN_ACCESS_TOKEN=\n')
    return path
config_at(SANDBOX)
# Block real broker/network requests even during module-level collection.
import httpx,requests
_HTTP=httpx.Client.request;_ASYNC=httpx.AsyncClient.request;_REQUEST=requests.Session.request

def external(url):
    from urllib.parse import urlparse
    return urlparse(str(url)).hostname not in ('127.0.0.1','localhost','::1','test','testserver',None)
def guarded(client,method,url,*a,**k):
    if external(url) and not isinstance(client._transport,httpx.MockTransport):raise OSError('External HTTP blocked in unit tests')
    return _HTTP(client,method,url,*a,**k)
async def guarded_async(client,method,url,*a,**k):
    if external(url) and not isinstance(client._transport,httpx.MockTransport):raise OSError('External HTTP blocked in unit tests')
    return await _ASYNC(client,method,url,*a,**k)
def guarded_requests(client,method,url,*a,**k):
    if external(url):raise OSError('External HTTP blocked in unit tests')
    return _REQUEST(client,method,url,*a,**k)
httpx.Client.request=guarded;httpx.AsyncClient.request=guarded_async;requests.Session.request=guarded_requests

@pytest.fixture(autouse=True)
def private_8510_state(tmp_path,monkeypatch,request):
    root=tmp_path/'desk';path=config_at(root)
    monkeypatch.setattr(terminal,'ROOT',root)
    monkeypatch.setattr(terminal,'REPLAY_DATA_DIR',root/'analyst/replay_data')
    for key in ('DHAN_ACCESS_TOKEN','DHAN_API_KEY','DHAN_API_SECRET','TOKEN_EXPIRY_UTC'):monkeypatch.setenv(key,'')
    monkeypatch.setenv('DHAN_CLIENT_ID','TEST-CLIENT')
    # Legacy unittest modules use an imported CONFIG_PATH constant.
    if hasattr(request.module,'CONFIG_PATH'):
        value=getattr(request.module,'CONFIG_PATH')
        if isinstance(value,Path) and value.name=='terminal8510_config.json':monkeypatch.setattr(request.module,'CONFIG_PATH',path)
    # The radar module owns a singleton whose default path is otherwise the
    # shipped config, even when the enclosing backend uses a private config.
    from analyst import ema_proximity_engine as radar
    monkeypatch.setattr(radar,'DEFAULT_CONFIG_PATH',path)
    monkeypatch.setattr(radar,'DEFAULT_REPLAY_DIR',root/'analyst/replay_data')
    monkeypatch.setattr(radar.ema_proximity_engine,'config_path',path)
    monkeypatch.setattr(radar.ema_proximity_engine,'replay_dir',root/'analyst/replay_data')
    monkeypatch.setattr(radar.ema_proximity_engine,'db_path',SOURCE/'ict_smc_app/backend/data/ict_smc.sqlite3')
    engine=sys.modules.get('engine_8510_live')
    names=getattr(getattr(request.function,'__code__',None),'co_names',())
    uses_app=engine is not None and (getattr(request.module,'app',None) is engine.app or isinstance(getattr(request.module,'backend',None),terminal.QuantTerminalBackend) or 'engine_8510_live' in names or 'TestClient' in names)
    if uses_app:
        backend=terminal.QuantTerminalBackend(config_file=str(path))
        monkeypatch.setattr(engine,'backend',backend)
        if isinstance(getattr(request.module,'backend',None),terminal.QuantTerminalBackend):
            monkeypatch.setattr(request.module,'backend',backend)
    # Refuse any missed direct write to the shipped config, rather than
    # restoring it afterwards and disguising a test's side effect.
    import io,builtins
    original_io=io.open;original_open=builtins.open
    protected=(SOURCE/'analyst/terminal8510_config.json').resolve()
    def safe_open(original,file,mode='r',*args,**kwargs):
        if isinstance(file,(str,bytes,os.PathLike)) and any(c in mode for c in 'wax+') and Path(file).resolve()==protected:
            raise AssertionError('Test attempted to write shipped 8510 config; use tmp_path')
        return original(file,mode,*args,**kwargs)
    monkeypatch.setattr(io,'open',lambda file,mode='r',*args,**kwargs:safe_open(original_io,file,mode,*args,**kwargs))
    monkeypatch.setattr(builtins,'open',lambda file,mode='r',*args,**kwargs:safe_open(original_open,file,mode,*args,**kwargs))
    yield

def pytest_sessionfinish(session,exitstatus):
    httpx.Client.request=_HTTP;httpx.AsyncClient.request=_ASYNC;requests.Session.request=_REQUEST
    terminal.ROOT=SOURCE;terminal.REPLAY_DATA_DIR=SOURCE/'analyst/replay_data'
    for k in tuple(os.environ):
        if k.startswith('DHAN_') or k=='TOKEN_EXPIRY_UTC':os.environ.pop(k,None)
    for k,v in _ORIGINAL_ENV.items():
        if k.startswith('DHAN_') or k=='TOKEN_EXPIRY_UTC':os.environ[k]=v
    _SESSION.cleanup()
