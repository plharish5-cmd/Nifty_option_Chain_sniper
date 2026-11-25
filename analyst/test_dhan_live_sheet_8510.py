"""Read-only gate mirror — shares desk8600_lane_gating evaluator with the 8510 engine."""
import copy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from analyst.dhan_live_sheet_gates_8510 import snapshot
from analyst.desk8600_board_8510 import build_board
from analyst.desk8600_lane_gating_8510 import TRACK2_SIGNALS, evaluate_lane_gating


@pytest.mark.parametrize("lane", ["prototype", "reversal", "flagship"])
@pytest.mark.parametrize("signal", [s[0] for s in TRACK2_SIGNALS])
@pytest.mark.parametrize("side", ["CE", "PE"])
def test_mirror_is_actual_evaluator_and_default_log_only(lane, signal, side):
    board_id = next(x[1] for x in TRACK2_SIGNALS if x[0] == signal)
    sources = [
        dict(
            id=board_id,
            direction="PE" if side == "CE" else "CE",
            feed_state="LIVE",
            age_sec=3,
            detail="observed OI",
        )
    ]
    backend = SimpleNamespace(scanner_cfg={}, desk8600_board=lambda: dict(sources=sources))
    result = snapshot(backend)["lanes"][lane][side]
    assert result == evaluate_lane_gating(lane, side == "CE", sources, {})
    assert not result["blocked"]
    backend.scanner_cfg = {"desk8600_lane_gating": {lane: {signal: "BLOCK"}}}
    before = copy.deepcopy(backend.scanner_cfg)
    assert snapshot(backend)["lanes"][lane][side]["blocked"]
    assert backend.scanner_cfg == before


@pytest.mark.parametrize("age", [None, -1, 31, float("nan"), float("inf")])
@pytest.mark.parametrize("state", ["LIVE", "STALE", "ERROR", "WAITING_FOR_SYNC", ""])
def test_invalid_freshness_never_blocks(age, state):
    cfg = {"desk8600_lane_gating": {"prototype": {"pe_ce_change": "BLOCK"}}}
    src = dict(id="day_oi", direction="PE", age_sec=age, feed_state=state, detail="x")
    assert not evaluate_lane_gating("prototype", True, [src], cfg)["blocked"]


def test_board_uses_original_classic_and_snapshot_timestamp():
    now = datetime(2026, 11, 25, 10, tzinfo=timezone.utc)
    stamp = (now - timedelta(seconds=4)).isoformat()
    br = {
        "rows": [
            dict(
                signal_group="Classic Writer Flow",
                source_ts=stamp,
                verdict="observed classic",
                direction="PE",
            )
        ]
    }
    cr = {
        "snapshot_ts": stamp,
        "momentum_oi": {
            "writer_flow": dict(direction="CE", age_sec=1, detail="fresh alias"),
            "fresh_oi": dict(direction="CE", age_sec=1, detail="fresh observation"),
        },
    }
    board = build_board(br, {}, {}, now, chain_reads=cr)
    by = {x["id"]: x for x in board["sources"]}
    assert by["writer_flow"]["direction"] == "PE"
    assert by["writer_flow"]["detail"] == "observed classic"
    assert by["fresh_oi"]["feed_state"] == "LIVE"
    assert by["fresh_oi"]["age_sec"] == 4
    old = build_board(br, {}, {}, now + timedelta(seconds=40), chain_reads=cr)
    by = {x["id"]: x for x in old["sources"]}
    assert by["fresh_oi"]["feed_state"] == "STALE"
    assert not evaluate_lane_gating(
        "prototype",
        False,
        old["sources"],
        {"desk8600_lane_gating": {"prototype": {"fresh_change_oi": "BLOCK"}}},
    )["blocked"]