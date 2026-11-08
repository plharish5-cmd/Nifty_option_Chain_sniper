"""Writer-flow gate and StockMojo normalize payload smoke tests."""
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from analyst.stockmojo_safe_poller_8510 import analyze_writer_momentum, normalize_payload
from analyst.writer_flow_gate_8510 import evaluate_writer_context, filter_signals_by_writer_gate

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 11, 9, 12, 0, tzinfo=IST).timestamp()


def test_normalize_payload_compact_arrays():
    payload = {
        "t": [datetime.fromtimestamp(NOW - 180 + i * 60, IST).isoformat() for i in range(4)],
        "co": [100, 100, 100, 100],
        "po": [50, 60, 70, 80],
        "f": [25000, 25001, 25002, 25003],
        "fo": [0, 0, 0, 0],
        "cv": [0, 0, 0, 0],
        "pv": [0, 0, 0, 0],
        "cc": [0, 0, 0, 0],
        "pc": [0, 0, 0, 0],
    }
    rows = normalize_payload(payload)
    latest = rows[-1]
    assert latest["pe_ce_oi_total"] == -20
    assert abs(latest["pcr_total"] - 0.8) < 0.001
    assert latest["pe_ce_change_1m"] == 10
    assert latest["pe_ce_change_3m"] == 30


def test_entity_both_combines_change_and_total_surge():
    snap = {
        "available": True,
        "significant_put_surge_change": False,
        "significant_call_surge_change": True,
        "significant_put_surge_total": False,
        "significant_call_surge_total": False,
        "writer_divergence": False,
        "synergy_status": "BEARISH_SYNERGY",
    }
    cfg = {"enabled": True, "entity": "both"}
    ctx = evaluate_writer_context(snap, {}, cfg, stockmojo_master_enabled=True)
    assert ctx["significant_call_surge"] is True


def test_surge_blocks_ce_on_call_surge_when_live_block():
    snap = {
        "available": True,
        "significant_call_surge_change": True,
        "significant_put_surge_change": False,
        "synergy_status": "BEARISH_SYNERGY",
        "writer_divergence": False,
    }
    cfg = {
        "enabled": True,
        "live_call_surge_mode": "block",
        "live_put_surge_mode": "alert",
        "block_ce_on_call_surge": True,
        "fail_open_when_stale": True,
    }
    ctx = evaluate_writer_context(snap, {}, cfg)
    signals = [{"opt_side": "CE", "setup_name": "Test CE", "strategy_id": "STAGE_2"}]
    out = filter_signals_by_writer_gate(signals, ctx, cfg, live_armed=True)
    assert out == []
