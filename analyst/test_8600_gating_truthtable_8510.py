"""Track 2: 8600 lane gating BLOCK vs LOG_ONLY across prototype / reversal / flagship."""
from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from analyst.decision_audit_8510 import read, record
from analyst.desk8600_lane_gating_8510 import (
    TRACK2_SIGNALS,
    evaluate_lane_gating,
    load_desk8600_lane_gating,
    record_gating_audits,
)

LANES = ("prototype", "reversal", "flagship")
VOTES = ("AGREE", "OPPOSE", "STALE")
MODES = ("BLOCK", "LOG_ONLY")


def _source(vote: str, age: int = 6) -> dict:
    if vote == "STALE":
        return {"direction": None, "feed_state": "STALE", "age_sec": age, "detail": "stale sample"}
    direction = {"AGREE": "CE", "OPPOSE": "PE", "NEUTRAL": "NEUTRAL"}[vote if vote != "STALE" else "NEUTRAL"]
    if vote == "AGREE":
        direction = "CE"
    elif vote == "OPPOSE":
        direction = "PE"
    # Provenance is required to block (fail-open on missing source_ts); a LIVE
    # source must carry one to exercise the real BLOCK path.
    return {
        "direction": direction,
        "feed_state": "LIVE",
        "age_sec": age,
        "source_ts": "2026-11-27T11:00:00+05:30",
        "detail": "puts heavier",
    }


def _board_for_signal(sig_key: str, vote: str) -> list:
    _, board_id, _ = next(x for x in TRACK2_SIGNALS if x[0] == sig_key)
    sources = []
    for sk, bid, _ in TRACK2_SIGNALS:
        src = _source("AGREE" if sk != sig_key else vote)
        sources.append({"id": bid, **src})
    return sources


def _cfg(lane: str, sig_key: str, mode: str) -> dict:
    base = {lane: {sk: "LOG_ONLY" for sk, _, _ in TRACK2_SIGNALS} for lane in LANES}
    base[lane][sig_key] = mode
    return {"desk8600_lane_gating": base}


@pytest.mark.parametrize("lane", LANES)
@pytest.mark.parametrize("sig_key", [s[0] for s in TRACK2_SIGNALS])
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("vote", VOTES)
def test_lane_signal_mode_vote_matrix(lane, sig_key, mode, vote):
    scanner = _cfg(lane, sig_key, mode)
    sources = _board_for_signal(sig_key, vote)
    out = evaluate_lane_gating(lane, bullish=True, sources=sources, scanner_cfg=scanner)
    item = next(x for x in out["signals"] if x["signal_id"] == sig_key)
    if vote == "STALE":
        assert out["blocked"] is False
        assert item["blocks_trade"] is False
        assert "feed stale — not blocking" in item["audit_reason"]
    elif vote == "OPPOSE" and mode == "BLOCK":
        assert out["blocked"] is True
        assert item["blocks_trade"] is True
        assert "entry blocked" in item["audit_reason"]
        assert "Cumulative" in item["audit_reason"] or "Fresh" in item["audit_reason"] or "PE-CE" in item["audit_reason"] or "Writer" in item["audit_reason"]
    elif vote == "OPPOSE" and mode == "LOG_ONLY":
        assert out["blocked"] is False
        assert item["blocks_trade"] is False
        assert "LOG_ONLY" in item["audit_reason"]
        assert "not blocking" in item["audit_reason"]
    else:
        assert out["blocked"] is False
        assert item["blocks_trade"] is False


def test_file_defaults_block_all_lanes():
    cfg = load_desk8600_lane_gating({})
    for lane in LANES:
        for sk, _, _ in TRACK2_SIGNALS:
            assert cfg[lane][sk] == "BLOCK"


def test_scanner_cfg_can_flip_one_signal_to_log_only():
    cfg = load_desk8600_lane_gating({"desk8600_lane_gating": {"prototype": {"pe_ce_change": "LOG_ONLY"}}})
    assert cfg["prototype"]["pe_ce_change"] == "LOG_ONLY"
    assert cfg["prototype"]["fresh_change_oi"] == "BLOCK"


@pytest.mark.parametrize(
    ("strategy_id", "lane"),
    (
        ("EMA8_PROTOTYPE_LONG", "prototype"),
        ("CROSSOVER_REVERSAL_LONG", "reversal"),
        ("FLAGSHIP_1H_LONG", "flagship"),
    ),
)
def test_block_mode_fresh_oppose_blocks_each_strategy(strategy_id, lane):
    from analyst.desk8600_lane_gating_8510 import apply_execution_lane_gating, lane_for_strategy_id

    assert lane_for_strategy_id(strategy_id) == lane
    sources = _board_for_signal("pe_ce_change", "OPPOSE")
    b = SimpleNamespace(
        scanner_cfg={},
        ledger_db=None,
        cfg={},
        _decision_audit_seen={},
        _record_ema_entry_decision=Mock(),
    )
    board = {"sources": sources}
    sig = {"strategy_id": strategy_id, "direction": "LONG"}
    assert apply_execution_lane_gating(b, sig, board) is True


def test_stale_never_blocks_even_in_block_mode(tmp_path: Path):
    scanner = _cfg("prototype", "cumulative_total_oi", "BLOCK")
    sources = _board_for_signal("cumulative_total_oi", "STALE")
    out = evaluate_lane_gating("prototype", True, sources, scanner)
    assert out["blocked"] is False


def test_thousand_ticks_one_audit_row_zero_signal_log_spam(tmp_path: Path):
    b = SimpleNamespace(
        ledger_db=tmp_path / "a.sqlite3",
        scanner_cfg=_cfg("prototype", "fresh_change_oi", "LOG_ONLY"),
        cfg={},
        _decision_audit_seen={},
    )
    b.log_ema8_prototype_event = Mock()
    sources = _board_for_signal("fresh_change_oi", "OPPOSE")
    gating = evaluate_lane_gating("prototype", True, sources, b.scanner_cfg)
    for _ in range(1000):
        record_gating_audits(b, "prototype", True, gating, strategy_id="EMA8_PROTOTYPE_LONG")
    rows = read(b.ledger_db)
    assert len(rows) <= len(TRACK2_SIGNALS)
    b.log_ema8_prototype_event.assert_not_called()


def test_audit_row_exact_text_block(tmp_path: Path):
    b = SimpleNamespace(
        ledger_db=tmp_path / "b.sqlite3",
        scanner_cfg=_cfg("reversal", "cumulative_total_oi", "BLOCK"),
        cfg={},
        _decision_audit_seen={},
    )
    _ts = "2026-11-27T11:00:00+05:30"  # provenance required to block (fail-open without it)
    sources = [
        {"id": "total_oi", "direction": "PE", "feed_state": "LIVE", "age_sec": 6, "source_ts": _ts, "detail": "PE writers heavier"},
        {"id": "day_oi", "direction": "CE", "feed_state": "LIVE", "age_sec": 6, "source_ts": _ts, "detail": "ok"},
        {"id": "fresh_oi", "direction": "CE", "feed_state": "LIVE", "age_sec": 6, "source_ts": _ts, "detail": "ok"},
        {"id": "writer_flow", "direction": "CE", "feed_state": "LIVE", "age_sec": 6, "source_ts": _ts, "detail": "ok"},
    ]
    gating = evaluate_lane_gating("reversal", True, sources, b.scanner_cfg)
    record_gating_audits(b, "reversal", True, gating, strategy_id="CROSSOVER_BULLISH")
    rows = read(b.ledger_db)
    block_rows = [r for r in rows if r["stage"] == "TOUCH_BLOCKED"]
    assert len(block_rows) == 1
    assert "CE entry blocked" in block_rows[0]["reason"]
    assert "Cumulative Total OI" in block_rows[0]["reason"]