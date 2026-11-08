"""Hermetic test configuration: no production DB, environment files or real HTTP."""
import copy,json
import pytest

@pytest.fixture(autouse=True)
def isolated_engine_environment(tmp_path,monkeypatch):
    from algo_execution_engine import strategy_setup1 as setup,strategy_1h_8ema as hourly,order_gateway as gateway,server
    cfg=json.loads(setup.CONFIG_FILE.read_text());cfg['execution']['sqlite_db_path']=str(tmp_path/'engine.sqlite3');cfg['execution']['instrument_master_path']=str(tmp_path/'master.csv')
    path=tmp_path/'config.json';path.write_text(json.dumps(cfg));monkeypatch.setattr(setup,'CONFIG_FILE',path)
    monkeypatch.setattr(hourly,'load_config',lambda:copy.deepcopy(cfg));monkeypatch.setattr(gateway,'load_env_if_needed',lambda:None)
    server.engine=None
    monkeypatch.delenv('DHAN_ACCESS_TOKEN',raising=False);monkeypatch.delenv('DHAN_CLIENT_ID',raising=False);monkeypatch.delenv('GEMINI_API_KEY',raising=False)
    monkeypatch.setenv('ALGO_SHARED_SECRET','test-secret')
    import httpx
    async def reject_http(*args,**kwargs):raise httpx.ConnectError('External network disabled in tests')
    monkeypatch.setattr(httpx.AsyncClient,'request',reject_http)
    yield cfg
    server.engine=None
