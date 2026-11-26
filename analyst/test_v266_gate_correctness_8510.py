"""v266 — fixes for ChatGPT's independent-review defects: reversed direction,
put-delta clamp, missing-provenance block, toggle overlay."""
import analyst.writer_bias_live_reads_8510 as wb
from analyst.desk8600_lane_gating_8510 import evaluate_lane_gating

_ALL_BLOCK = {"prototype": {"pe_ce_change": "BLOCK", "fresh_change_oi": "BLOCK",
                            "cumulative_total_oi": "BLOCK", "classic_writer_flow": "BLOCK"}}


def test_direction_not_reversed():
    assert wb._direction_from_regime("BULLISH") == "CE"
    assert wb._direction_from_regime("BEARISH") == "PE"
    assert wb._direction_from_regime("NEUTRAL") == "NEUTRAL"
    assert wb._direction_from_regime(None) is None


def test_put_delta_not_floored_to_001():
    d = wb._estimate_delta(spot=25000.0, strike=25000.0, ltp=120.0, call=False, t_years=5/365, r=0.065)
    assert 0.2 <= d <= 0.8, f"put delta collapsed to {d}"
    dc = wb._estimate_delta(spot=25000.0, strike=25000.0, ltp=120.0, call=True, t_years=5/365, r=0.065)
    assert 0.2 <= dc <= 0.8


def test_missing_source_ts_never_blocks():
    # CE entry (bullish=True); a fresh-looking PE (oppose) source but NO source_ts must NOT block.
    sources = {"day_oi": {"direction": "PE", "age_sec": 1.0, "feed_state": "LIVE", "source_ts": None}}
    res = evaluate_lane_gating("prototype", True, sources, {"desk8600_lane_gating": _ALL_BLOCK})
    assert res["blocked"] is False
    assert not any(s.get("blocks_trade") for s in res["signals"])


def test_valid_source_ts_with_fresh_oppose_blocks():
    sources = {"day_oi": {"direction": "PE", "age_sec": 1.0, "feed_state": "LIVE",
                          "source_ts": "2026-11-26T10:00:00+05:30"}}
    res = evaluate_lane_gating("prototype", True, sources, {"desk8600_lane_gating": _ALL_BLOCK})
    assert res["blocked"] is True
    assert any(s.get("blocks_trade") for s in res["signals"])
