"""Shared clock provenance and optional OI session replay regression tests."""
import importlib.util
from pathlib import Path
import json
import sqlite3
import pytest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('tm_test',ROOT/'analyst/desk_time_machine.py')
tm=importlib.util.module_from_spec(spec);spec.loader.exec_module(tm)


def setup(tmp_path,monkeypatch):
    monkeypatch.setattr(tm,'ROOT',tmp_path)
    (tmp_path/'analyst').mkdir()
    (tmp_path/'analyst/analyst_config.json').write_text(json.dumps({'time_machine':{'open':'09:15','end':'15:40'}}))
    return tmp_path


def test_never_borrows_future_snapshot():
    r=tm.select_at([100,200],150)
    assert r['actual_ts']==100
    assert not tm.select_at([200],150)['available']


def test_fractional_capture_time_remains_exact():
    stamp=tm.epoch('2026-10-30T09:15:32.123456')
    r=tm.select_at([stamp],stamp)
    assert r['actual_local']=='2026-10-30T09:15:32.123456'
    assert not tm.select_at([stamp],stamp-.1)['available']


def test_midnight_is_not_a_session_snapshot(tmp_path,monkeypatch):
    root=setup(tmp_path,monkeypatch)
    base=root/'data/snapshots/2026-10-30';base.mkdir(parents=True)
    (base/'a.meta.json').write_text(json.dumps({'timestamp':'2026-10-30T00:01:00','expiry':'2026-11-03'}))
    assert not tm.metadata()['ok']


def test_day_cutoff_does_not_fall_back_to_other_day(tmp_path,monkeypatch):
    root=setup(tmp_path,monkeypatch)
    base=root/'data/snapshots/2026-10-30';base.mkdir(parents=True)
    (base/'a.meta.json').write_text(json.dumps({'timestamp':'2026-10-30T10:00:00','expiry':'2026-11-03'}))
    assert not tm.metadata('2026-10-29')['ok']
    r=tm.metadata('2026-10-30',tm.epoch('2026-10-30T09:15:00'))
    assert not r['views']['time_series']['available']
    assert not r['views']['gamma_review']['available']
    with pytest.raises(ValueError):tm.metadata('2026-10-30',tm.epoch('2026-10-31T10:00:00'))


def test_db_1540_endpoint_and_zero_data_are_distinct(tmp_path,monkeypatch):
    root=setup(tmp_path,monkeypatch)
    p=root/'ict_smc_app/backend/data';p.mkdir(parents=True)
    with sqlite3.connect(p/'ict_smc.sqlite3') as db:
        db.execute('create table chain_snap (ts integer)')
        db.execute('insert into chain_snap values (?)',(tm.epoch('2026-10-30T15:39:00'),))
    r=tm.metadata('2026-10-30')
    assert r['end_ts']==tm.epoch('2026-10-30T15:40:00')
    assert r['views']['wmap']['available']
    assert r['views']['wmap']['actual_ts']<r['selected_ts']
    assert not r['views']['gamma_review']['available']


def test_explicit_oi_day_never_calls_latest_fallback(monkeypatch):
    from app.engine import oi_studio
    seen=[]
    def query(sql,args=()):seen.append(sql);return []
    monkeypatch.setattr(oi_studio.db,'query',query)
    r=oi_studio.get_oi_studio_data(session_day='2026-10-30',end_ts=int(tm.epoch('2026-10-30T09:15:00')))
    assert not r['ok']
    assert len(seen)==1 and 'chain_latest' not in seen[0]
