"""History correctness tests: missingness, causality, combined expiry, and legacy parity."""
import importlib.util
import json
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("position_projection", ROOT / "analyst/strike_positioning.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
CFG = json.loads((ROOT / "analyst/analyst_config.json").read_text())["oi_studio"]
T = int(datetime(2026, 10, 30, 9, 15, tzinfo=ZoneInfo("Asia/Kolkata")).timestamp())


def row(offset, ce=1000000, pe=2000000, expiry="2026-11-03", strike=23000):
    return dict(ts=T + offset, expiry=expiry, strike=strike, ce_oi=ce, pe_oi=pe)


def payload(end=1800):
    return dict(ok=True, window_start_ts=T, window_end_ts=T + end,
                strikes=[dict(strike=23000)])


def build(rows, candles=None, end=1800, exps=None, minutes=15):
    return mod.build_positioning(rows, candles or [], payload(end), exps or ["2026-11-03"], CFG, minutes)


def test_separate_legs_zero_and_negative():
    h = build([row(0), row(900, 900000, 2200000), row(1800, 900000, 2200000)])
    a, b = h["rows"][0]["cells"]
    assert (a["ce"]["delta"], a["pe"]["delta"]) == (-100000, 200000)
    assert (b["ce"]["delta"], b["pe"]["delta"]) == (0, 0)


def test_missing_null_side_does_not_become_zero():
    h = build([row(0), row(900, None, 2200000), row(1800, 900000, 2200000)])
    a, b = h["rows"][0]["cells"]
    assert a["ce"] is None and b["ce"] is None
    assert a["pe"]["delta"] == 200000


def test_stale_snapshot_and_recovery_do_not_fill_gap():
    h = build([row(0), row(1800)])
    assert all(c["ce"] is None and c["pe"] is None for c in h["rows"][0]["cells"])


def test_future_observation_is_not_used():
    h = build([row(0), row(901, 4000000, 9000000)], end=900)
    assert h["rows"][0]["cells"][0]["ce"] is None
    assert not h["rows"][0]["quality"]["ce"]["total_valid"]


def test_combined_expiry_requires_each_component():
    h = build([row(0), row(900), row(0, expiry="2026-11-09")], end=900, exps=["2026-11-03", "2026-11-09"])
    assert h["rows"][0]["cells"][0]["ce"] is None
    h = build([row(0), row(900, 1300000), row(0, expiry="2026-11-09"), row(900, 1200000, expiry="2026-11-09")], end=900, exps=["2026-11-03", "2026-11-09"])
    assert h["rows"][0]["cells"][0]["ce"]["delta"] == 500000


def test_spot_requires_closed_nifty_candle_and_breaks_gaps():
    h = build([row(0), row(900), row(1800)], [dict(ts=T + 840, c=23010), dict(ts=T + 1800, c=99999)])
    assert h["buckets"][0]["spot"]["value"] == 23010
    assert h["buckets"][1]["spot"] is None
    assert h["spot"] is None


def test_partial_window_and_replay_end():
    rows = [row(0), row(900), row(1000), row(1800)]
    h = build(rows, end=1000)
    assert h["buckets"][-1]["end_ts"] == T + 1000
    assert h["buckets"][-1]["partial"]
    assert len(h["buckets"]) == 2


def test_single_snapshot_has_no_invented_change():
    assert not build([row(0)], end=0)["buckets"]


def test_invalid_bucket_is_rejected():
    with pytest.raises(ValueError):
        build([row(0)], minutes=7)


def test_opt_in_preserves_entire_existing_payload(monkeypatch):
    from app.engine import oi_studio
    from app import db
    raw = [row(0), row(900, 1100000, 2300000)]
    raw = [dict(r, ce_ltp=100, pe_ltp=100, ce_vol=1000, pe_vol=1000) for r in raw]
    def query(sql, args=()):
        if "DISTINCT expiry" in sql:
            return [{"expiry": "2026-11-03"}]
        if "SELECT expiry, ts, strike" in sql:
            return raw
        return []
    monkeypatch.setattr(db, "query", query)
    monkeypatch.setattr(db, "candles_1m", lambda *args: [dict(ts=T, c=23000), dict(ts=T+840, c=23020)])
    old = oi_studio.get_oi_studio_data(strike_range="all", cfg=CFG)
    new = oi_studio.get_oi_studio_data(strike_range="all", cfg=CFG, include_positioning=True)
    history = new.pop("positioning")
    assert old == new
    assert history["rows"][0]["quality"]["ce"]["change_valid"]
    assert new["strikes"][0]["call_oi_chg"] == history["rows"][0]["cells"][0]["ce"]["delta"]


def test_database_row_objects_are_supported():
    import sqlite3
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("create table rows (ts int, expiry text, strike real, ce_oi real, pe_oi real)")
    for r in [row(0), row(900, 1100000, 2100000)]:
        c.execute("insert into rows values (?,?,?,?,?)", tuple(r[k] for k in ("ts", "expiry", "strike", "ce_oi", "pe_oi")))
    h = build(c.execute("select * from rows").fetchall(), end=900)
    assert h["rows"][0]["cells"][0]["ce"]["delta"] == 100000
