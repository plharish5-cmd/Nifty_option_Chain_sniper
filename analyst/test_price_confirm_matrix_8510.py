"""v2.0.33 — price confirm on S gate + 8600 matrix recorder (no gating)."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from analyst.chain_reads_8510 import compute_momentum_oi_reads, _hm_ts
from analyst.desk8600_matrix_recorder_8510 import (
    CREATE_SQL,
    TABLE,
    count_matrix_rows_today,
    record_matrix_snapshot,
    snapshot_from_bridge,
)
from analyst.oi_price_confirm_8510 import (
    apply_price_confirm_to_momentum_read,
    price_agrees_with_momentum,
)
from analyst.proto_oi_filters_8510 import load_oi_filters_cfg


def _bars_bull_10m(base_ts: int) -> list:
    out = []
    c = 25000.0
    for i in range(11):
        h = c + 5 + i
        out.append({"ts": float(base_ts + i * 60), "open": c, "high": h, "low": c - 2, "close": c + i * 2})
    return out


def _bars_bear_10m(base_ts: int) -> list:
    out = []
    c = 25100.0
    for i in range(11):
        lo = c - 5 - i
        out.append({"ts": float(base_ts + i * 60), "open": c, "high": c + 2, "low": lo, "close": c - i * 2})
    return out


def test_price_confirm_off_unchanged_from_baseline_shape():
    cfg = load_oi_filters_cfg({"oi_filters": {"momentum": {"require_price_confirm": False}}})
    assert cfg["momentum"]["require_price_confirm"] is False


def test_price_agrees_bull_and_bear():
    t0 = 1_700_000_000
    assert price_agrees_with_momentum("CE", _bars_bull_10m(t0)) == (True, "agrees")
    assert price_agrees_with_momentum("PE", _bars_bear_10m(t0)) == (True, "agrees")
    assert price_agrees_with_momentum("CE", _bars_bear_10m(t0)) == (False, "diverges")


def test_momentum_downgrade_on_diverge_fail_open_neutral():
    item = {"direction": "CE", "strength": "STRONG", "detail": "Total OI: S +0.20"}
    bull = _bars_bear_10m(1_700_000_000)
    out = apply_price_confirm_to_momentum_read(item, require=True, window_bars=bull)
    assert out["direction"] == "NEUTRAL"
    assert out["price_confirm"] == "diverges"


def test_momentum_kept_when_agrees():
    item = {"direction": "PE", "strength": "STRONG", "detail": "x"}
    out = apply_price_confirm_to_momentum_read(item, require=True, window_bars=_bars_bear_10m(1_700_000_000))
    assert out["direction"] == "PE"
    assert out["price_confirm"] == "agrees"


def test_compute_momentum_with_price_confirm_integration():
    day = "2026-11-17"
    base = _hm_ts(day, "09:15")
    t_prev = base + 75 * 60
    t_now = t_prev + 10 * 60
    by_ts = {
        base: {25000.0: {"ce_oi": 39e7, "pe_oi": 42e7}},
        t_prev: {25000.0: {"ce_oi": 38e7, "pe_oi": 44e7}},
        t_now: {25000.0: {"ce_oi": 43e7, "pe_oi": 34e7}},
    }
    out_off = compute_momentum_oi_reads(
        by_ts, sorted(by_ts), spot=25000.0, band=1000.0, day=day, now_ts=t_now, last_t=t_now, age_sec=120,
        momentum_cfg={"require_price_confirm": False},
    )
    out_on = compute_momentum_oi_reads(
        by_ts, sorted(by_ts), spot=25000.0, band=1000.0, day=day, now_ts=t_now, last_t=t_now, age_sec=120,
        momentum_cfg={"require_price_confirm": True},
        spot_window_bars=_bars_bull_10m(t_prev),
    )
    assert out_off["total_oi"]["direction"] == "PE"
    assert out_on["total_oi"]["direction"] == "NEUTRAL"
    assert out_on["total_oi"].get("price_confirm") == "diverges"


def test_matrix_snapshot_and_record_idempotent(tmp_path: Path):
    bridge = {
        "evaluated_at": "2026-11-17T10:12:00+05:30",
        "rows": [
            {
                "signal_group": "State Change",
                "verdict": "Confirmed ▲ — writers + price agree",
                "direction": "CE",
                "spot": 25123.5,
                "source_ts": "2026-11-17T10:09:00+05:30",
            },
            {"signal_group": "PCR OI vs Futures", "direction": "NEUTRAL", "verdict": "NEUTRAL_EQUILIBRIUM"},
        ],
    }
    snap = snapshot_from_bridge(bridge)
    assert snap is not None
    assert snap["provenance"] == "recorded_live"
    assert snap["spot"] == 25123.5
    assert snap["confirmed_flag"] == 1
    assert snap["pcr"] is None
    assert snap["call_angle_deg"] is None
    db = tmp_path / "rec.sqlite3"
    assert record_matrix_snapshot(db, snap) is True
    assert record_matrix_snapshot(db, snap) is False
    assert count_matrix_rows_today(db, "2026-11-17") == 1
    with sqlite3.connect(db) as conn:
        conn.execute(CREATE_SQL)
        row = conn.execute(f"SELECT raw_rows_json FROM {TABLE}").fetchone()
        assert json.loads(row[0])[0]["signal_group"] == "State Change"


def test_matrix_recorder_never_imports_oi_switch_block():
    src = Path(__file__).resolve().parent.joinpath("desk8600_matrix_recorder_8510.py").read_text()
    assert "oi_switch_block" not in src