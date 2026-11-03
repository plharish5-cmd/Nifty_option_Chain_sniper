"""Causal replay gates and preservation of original daily change inputs."""
import json
import sqlite3
import sys
from pathlib import Path
import pandas as pd
import pytest
sys.path.insert(0,str(Path(__file__).parent))
import oi_walls_replay as module
from desk_time_machine import epoch

@pytest.fixture
def archive(tmp_path, monkeypatch):
    monkeypatch.setattr(module,'ROOT',tmp_path)
    (tmp_path/'analyst').mkdir()
    (tmp_path/'analyst/analyst_config.json').write_text(json.dumps({'time_machine':{'open':'09:15','end':'15:40'}}))
    folder=tmp_path/'data/snapshots/2026-10-30';folder.mkdir(parents=True)
    for minute,oi in [('09:59',1000),('10:01',9000)]:
        p=folder/(minute.replace(':','')+'.meta.json')
        p.write_text(json.dumps({'timestamp':'2026-10-30T'+minute,'expiry':'03-Nov-2026','spot':23000}))
        pd.DataFrame([{'strike':23000,'expiry':'03-Nov-2026','ce_oi':oi,'pe_oi':2000,'ce_chg_oi':-200,'pe_chg_oi':300}]).to_parquet(p.with_suffix('').with_suffix('.parquet'))
    db=tmp_path/'ict_smc_app/backend/data/ict_smc.sqlite3';db.parent.mkdir(parents=True)
    with sqlite3.connect(db) as c:
        c.execute('CREATE TABLE candles_1m(sec_id TEXT,ts INT,o REAL,h REAL,l REAL,c REAL)')
        for minute in ('09:59','10:00'):
            c.execute('INSERT INTO candles_1m VALUES(?,?,?,?,?,?)',('13',int(epoch('2026-10-30T'+minute)),23000,23010,22990,23005))
    return int(epoch('2026-10-30T10:00'))

def test_cutoff_preserves_daily_change(archive):
    d=module.replay('2026-10-30',archive)
    assert d['ok'] and d['ts']<archive
    assert d['rows'][0]['ce']=={'oi':1000,'oi_chg':-200}
    assert len(d['candles'])==1
    assert all(c['ts']+60<=archive for c in d['candles'])

def test_no_future_snapshot_or_expiry_fallback(archive):
    assert not module.replay('2026-10-30',int(epoch('2026-10-30T09:15')))['ok']
    assert not module.replay('2026-10-30',archive,'2026-11-09')['ok']

def test_outside_session_rejected(archive):
    with pytest.raises(ValueError):
        module.replay('2026-10-30',int(epoch('2026-10-30T08:00')))

def test_incomplete_daily_change_not_replaced_with_zero(archive):
    p=next((module.ROOT/'data/snapshots/2026-10-30').glob('0959.parquet'))
    d=pd.read_parquet(p);d['ce_chg_oi']=float('nan');d.to_parquet(p)
    assert not module.replay('2026-10-30',archive)['ok']
