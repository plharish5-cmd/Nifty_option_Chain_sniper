"""
analyst/test_writer_flow_bridge_8510.py — 8600-sourced writer flow for the 8510 desk.

The two things that actually matter here:

  1. The snapshot keys match StockMojoScraper.get_snapshot() exactly, because
     128 UI references and the telemetry payload depend on that shape. A silent
     key rename would blank panels rather than error.

  2. It FAILS CLOSED. This codebase already shipped a trend watchlist with three
     indicators hardcoded to BULLISH that nothing ever overwrote, which made a
     bearish regime arithmetically unreachable. A bridge that guessed a bias when
     8600 was down would be the same bug again, so every failure path is tested
     to return "NO DATA" and never a direction.

No network: the pure mapper is tested with literal rows, and the failure paths
are driven by pointing the bridge at a closed port.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analyst.writer_flow_bridge import (  # noqa: E402
    WriterFlowBridge,
    classify_writer_bias,
    empty_snapshot,
    snapshot_from_rows,
)

# The exact contract the 8510 telemetry and UI read.
REQUIRED_KEYS = {
    "pe_minus_ce_day", "pe_minus_ce_interval", "call_oi_change_day",
    "put_oi_change_day", "total_call_oi", "total_put_oi", "writer_bias",
    "cw_vs_pw_ratio", "pcr", "tag", "badge", "signal_hint", "writer_signal",
    "last_sync_time", "futures_volume_surge", "is_surge",
}


def row(t="12:45", put_day=500000, call_day=200000, put_step=9000,
        call_step=4000, pcr=1.18):
    """One row in 8600's 12-column parity shape."""
    return {
        "Time": t,
        "Put OI Change (Day)": put_day,
        "Put OI Change": put_step,
        "Call OI Change (Day)": call_day,
        "Call OI Change": call_step,
        "PE-CE OI Change (Day)": put_day - call_day,
        "PE-CE OI Change": put_step - call_step,
        "PCR Total": pcr,
        "Future LTP": 24640.1,
        "Future OI Change (Day)": 12000,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 1. CONTRACT — the UI depends on these exact keys
# ─────────────────────────────────────────────────────────────────────────────

def test_snapshot_has_every_key_the_ui_reads():
    snap = snapshot_from_rows([row()])
    assert REQUIRED_KEYS.issubset(snap), REQUIRED_KEYS - set(snap)


def test_empty_snapshot_has_the_same_keys():
    """A no-data snapshot must not be a different shape, or panels blank out."""
    assert REQUIRED_KEYS.issubset(empty_snapshot("test"))


def test_values_map_from_the_8600_columns():
    snap = snapshot_from_rows([row(put_day=500000, call_day=200000, pcr=1.18)])
    assert snap["available"] is True
    assert snap["put_oi_change_day"] == 500000
    assert snap["call_oi_change_day"] == 200000
    assert snap["pe_minus_ce_day"] == 300000
    assert snap["pe_minus_ce_interval"] == 5000      # 9000 - 4000
    assert snap["pcr"] == 1.18
    assert snap["last_sync_time"] == "12:45"
    assert snap["source"] == "8600_ICT_SMC_DESK"


def test_newest_row_is_used():
    """8600 orders newest-first."""
    rows = [row(t="13:00", put_day=1), row(t="12:45", put_day=999999)]
    assert snapshot_from_rows(rows)["last_sync_time"] == "13:00"
    assert snapshot_from_rows(rows)["put_oi_change_day"] == 1


def test_totals_are_not_faked_from_change_columns():
    """8600's parity rows carry OI CHANGE. Reporting change as absolute OI
    would be a quiet lie, so these stay 0 and say so."""
    snap = snapshot_from_rows([row()])
    assert snap["total_call_oi"] == 0.0
    assert snap["total_put_oi"] == 0.0
    assert "OI CHANGE" in snap["_note"]


# ─────────────────────────────────────────────────────────────────────────────
# 2. DIRECTION CONVENTION
# ─────────────────────────────────────────────────────────────────────────────

def test_put_writers_dominant_is_bullish():
    """PE - CE > 0 means put writers adding faster. Put writing is support."""
    snap = snapshot_from_rows([row(put_day=500000, call_day=200000)])
    assert snap["writer_bias"] == "BULLISH"
    assert snap["writer_signal"] == "PUT WRITERS DOMINANT"
    assert snap["badge"] == "bull"


def test_call_writers_dominant_is_bearish():
    snap = snapshot_from_rows([row(put_day=100000, call_day=600000)])
    assert snap["writer_bias"] == "BEARISH"
    assert snap["writer_signal"] == "CALL WRITERS DOMINANT"
    assert snap["badge"] == "bear"


def test_equal_writing_is_neutral_not_bullish():
    snap = snapshot_from_rows([row(put_day=300000, call_day=300000)])
    assert snap["writer_bias"] == "NEUTRAL"


@pytest.mark.parametrize("diff,expected", [
    (1.0, "BULLISH"), (-1.0, "BEARISH"), (0.0, "NEUTRAL"),
])
def test_classify_writer_bias(diff, expected):
    assert classify_writer_bias(diff)["writer_bias"] == expected


def test_bias_threshold_is_caller_supplied():
    """Thresholds belong in config, not baked into the module."""
    assert classify_writer_bias(50.0, threshold=100.0)["writer_bias"] == "NEUTRAL"
    assert classify_writer_bias(150.0, threshold=100.0)["writer_bias"] == "BULLISH"


def test_ratio_does_not_divide_by_zero():
    assert snapshot_from_rows([row(put_day=0, call_day=5000)])["cw_vs_pw_ratio"] == 0.0


# ─────────────────────────────────────────────────────────────────────────────
# 3. FAILS CLOSED — never invents a direction
# ─────────────────────────────────────────────────────────────────────────────

def test_no_rows_is_no_data_not_neutral_bias():
    snap = snapshot_from_rows([])
    assert snap["available"] is False
    assert snap["writer_bias"] == "NO DATA"


def test_unreachable_8600_returns_no_data():
    """Port 9 is the discard service: reliably refuses, so this needs no mock."""
    b = WriterFlowBridge({"base_url": "http://127.0.0.1:9", "timeout_seconds": 0.5,
                          "cache_seconds": 0})
    snap = b.get_snapshot()
    assert snap["available"] is False
    assert snap["writer_bias"] == "NO DATA"
    assert snap["pe_minus_ce_day"] == 0.0
    assert snap["reason"]
    # The shape must still be complete so the UI does not break.
    assert REQUIRED_KEYS.issubset(snap)


def test_garbage_rows_do_not_raise_or_invent_a_bias():
    snap = snapshot_from_rows([{"Time": "oops", "Put OI Change (Day)": "abc"}])
    assert snap["put_oi_change_day"] == 0.0
    assert snap["writer_bias"] == "NEUTRAL"      # 0 - 0, honestly neutral
    assert snap["pcr"] == 0.0


def test_pe_ce_falls_back_to_put_minus_call_when_column_missing():
    r = row(put_day=400000, call_day=100000)
    del r["PE-CE OI Change (Day)"]
    assert snapshot_from_rows([r])["pe_minus_ce_day"] == 300000


def test_pe_ce_table_shim_fails_closed():
    b = WriterFlowBridge({"base_url": "http://127.0.0.1:9", "timeout_seconds": 0.5})
    res = b.get_pe_ce_table(limit=10)
    assert res["ok"] is False and res["rows"] == []


def test_no_background_daemon_is_started():
    """Stock Mojo's daemon polled a dead feed every 60s. This one is on demand."""
    b = WriterFlowBridge({})
    b.start_background_polling()
    assert not hasattr(b, "_poll_task")


# ─────────────────────────────────────────────────────────────────────────────
# 4. CACHING — the chain only refreshes every ~3 minutes
# ─────────────────────────────────────────────────────────────────────────────

def test_second_call_is_served_from_cache():
    b = WriterFlowBridge({"base_url": "http://127.0.0.1:9", "timeout_seconds": 0.5,
                          "cache_seconds": 60})
    calls = {"n": 0}

    def counting_fetch():
        calls["n"] += 1
        return [row()]

    b._fetch_rows = counting_fetch
    first = b.get_snapshot()
    second = b.get_snapshot()
    assert calls["n"] == 1
    assert first is second
    assert b.get_snapshot(force=True)["available"] is True
    assert calls["n"] == 2


def test_url_is_built_from_config():
    b = WriterFlowBridge({"base_url": "http://127.0.0.1:8600/",
                          "pe_ce_matrix": "/api/pe_ce_matrix"})
    assert b.url == "http://127.0.0.1:8600/api/pe_ce_matrix"


def test_display_only_default_is_true():
    """Writer flow must not gate trades until scored against chain_snap history."""
    assert WriterFlowBridge({}).display_only is True
