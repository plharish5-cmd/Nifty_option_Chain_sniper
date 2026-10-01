"""
test_iv_gex_fixes.py — Tests for IV Percentile Stability & GEX Neutral Band.
Validates:
1. Loading configuration from strategy1_rules.json (Rule 4: Zero hardcoding):
   - gex.neutral_band_cr: 25.0
   - iv_context.max_pct_jump_per_poll: 25.0
   - iv_context.min_sessions_required: 3
   - iv_context.reference_mode: 'interpolate'
2. ATM IV interpolation to fixed moneyness (moneyness = 1.0 at strike == spot).
3. Requiring min_sessions_required before emitting a percentile at all ('IV: building history').
4. Sanity guard carrying previous value when consecutive poll jump exceeds max_pct_jump_per_poll.
5. Replay of 2026-09-24 09:20-09:40 asserting no consecutive-poll percentile jump exceeds the guard.
6. GEX neutral band labeling ±25Cr as 'NEUTRAL gamma (no regime)'.
7. Part 4 cascade advice not claiming amplification or dampening in NEUTRAL gamma.
"""

from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
import json
import numpy as np
import pandas as pd
import pytest

import agt_briefing
import config


def test_rules_loaded():
    rules = agt_briefing.load_briefing_rules()
    assert rules.get("gex_neutral_band_cr") == 25.0
    assert rules.get("iv_max_pct_jump_per_poll") == 25.0
    assert rules.get("iv_min_sessions_required") == 3
    assert rules.get("iv_reference_mode") == "interpolate"


def test_extract_reference_atm_iv_interpolation():
    """Verify linear interpolation at exact spot moneyness between straddling strikes."""
    df = pd.DataFrame([
        {"strike": 23200.0, "ce_iv": 10.0, "pe_iv": 10.0},
        {"strike": 23250.0, "ce_iv": 20.0, "pe_iv": 20.0},
    ])
    # Spot halfway between strikes: 23,225 -> (10.0 + 20.0) / 2 = 15.0
    iv_half = agt_briefing.extract_reference_atm_iv(df, 23225.0, mode="interpolate")
    assert iv_half is not None
    assert abs(iv_half - 15.0) < 1e-4

    # Spot closer to 23,250: 23,240 -> 0.2 * 10 + 0.8 * 20 = 18.0
    iv_close = agt_briefing.extract_reference_atm_iv(df, 23240.0, mode="interpolate")
    assert iv_close is not None
    assert abs(iv_close - 18.0) < 1e-4


def test_iv_building_history_when_sessions_insufficient(tmp_path):
    """When fewer than min_sessions_required exist, emit 'building history' and Box 3 does not fail."""
    rules = agt_briefing.load_briefing_rules()
    rules["iv_min_sessions_required"] = 10  # Require 10 sessions

    df = pd.DataFrame([
        {"strike": 23200.0, "ce_iv": 12.0, "pe_iv": 12.0},
        {"strike": 23250.0, "ce_iv": 12.5, "pe_iv": 12.5},
    ])
    ts = datetime(2026, 9, 24, 9, 25, 0)
    d = date(2026, 9, 24)

    # Compute with empty snapshot dir
    empty_dir = tmp_path / "empty_snaps"
    empty_dir.mkdir(parents=True, exist_ok=True)
    res = agt_briefing.compute_iv_context(df, 23225.0, ts, d, rules, base_dir=empty_dir)

    assert res["status"] == "building_history"
    assert res["percentile"] is None
    assert "building history" in res["terminal_str"]
    assert "building history" in res["telegram_str"]

    # Verify checklist does not fail Box 3
    chk = agt_briefing.compute_pretrade_checklist(
        None, None, 0.0, 0.0, res["percentile"], {"status": "ok", "total_gex_cr": 0.0}, ts, d, ts.time(), rules
    )
    b3 = next(b for b in chk["boxes"] if "IV" in b[0])
    assert "PASS" in b3[1] or "N/A" in b3[1]

    # Verify observation log emits '- IV: building history'
    obs_line = agt_briefing.format_observation_entry(
        d, ts, 23225.0, "NEUTRAL", None, res, None, None, 0.001, 0.0, 0.0, 0, chk, "NO TRADE", rules
    )
    assert "- IV: building history" in obs_line


def test_iv_sanity_guard_triggers_and_carries_previous():
    """If percentile jumps > iv_max_pct_jump_per_poll (25.0), log warning and carry previous value."""
    rules = agt_briefing.load_briefing_rules()
    agt_briefing.reset_session_iv_state()

    df = pd.DataFrame([
        {"strike": 23200.0, "ce_iv": 22.0, "pe_iv": 22.0},
        {"strike": 23250.0, "ce_iv": 23.0, "pe_iv": 23.0},
    ])
    ts = datetime(2026, 9, 24, 9, 25, 0)
    d = date(2026, 9, 24)

    # Pass prev_iv_percentile = 13.0 (CHEAP)
    # Raw percentile for ~22.5% IV against historical baseline (~13%) is > 90%
    res = agt_briefing.compute_iv_context(df, 23225.0, ts, d, rules, prev_iv_percentile=13.0)

    # Guard must have triggered and carried 13.0
    assert res["percentile"] == 13.0
    assert res["label"] == "CHEAP"

    # Box 3 must PASS
    chk = agt_briefing.compute_pretrade_checklist(
        None, None, 0.0, 0.0, res["percentile"], {"status": "ok", "total_gex_cr": 0.0}, ts, d, ts.time(), rules
    )
    b3 = next(b for b in chk["boxes"] if "IV" in b[0])
    assert "PASS" in b3[1]


def test_gex_neutral_band():
    """Verify that total GEX inside [-25Cr, +25Cr] is classified as NEUTRAL with no regime."""
    rules = agt_briefing.load_briefing_rules()
    rules["gex_neutral_band_cr"] = 25.0

    # Create dummy chain with small total GEX (+15Cr and -18Cr)
    # GEX = (ce_oi - pe_oi) * gamma * spot * lot_size
    # 23,250 strike, spot 23250, lot 65
    def make_gex_df(net_cr):
        # target net_cr in Cr -> net_gex = net_cr * 1e7
        # (ce_oi - pe_oi) = net_gex / (gamma * spot * lot_size)
        gamma = 0.001
        spot = 23250.0
        lot = 65
        diff_oi = (net_cr * 1e7) / (gamma * spot * lot)
        return pd.DataFrame([{
            "strike": 23250.0,
            "ce_oi": 100000.0 + diff_oi,
            "pe_oi": 100000.0,
            "ce_gamma": gamma,
        }])

    # Inside neutral band (+15Cr)
    df_15 = make_gex_df(15.0)
    gex_15 = agt_briefing.compute_gex_metrics(df_15, 23250.0, rules)
    assert gex_15["regime"] == "NEUTRAL"
    assert "NEUTRAL gamma — no regime" in gex_15["terminal_str"]
    assert "NEUTRAL" in gex_15["telegram_str"]

    # Inside neutral band (-18Cr)
    df_m18 = make_gex_df(-18.0)
    gex_m18 = agt_briefing.compute_gex_metrics(df_m18, 23250.0, rules)
    assert gex_m18["regime"] == "NEUTRAL"
    assert "NEUTRAL gamma — no regime" in gex_m18["terminal_str"]

    # Outside neutral band (+30Cr) -> SHORT
    df_30 = make_gex_df(30.0)
    gex_30 = agt_briefing.compute_gex_metrics(df_30, 23250.0, rules)
    assert gex_30["regime"] == "SHORT"
    assert "SHORT gamma" in gex_30["terminal_str"]

    # Outside neutral band (-30Cr) -> LONG
    df_m30 = make_gex_df(-30.0)
    gex_m30 = agt_briefing.compute_gex_metrics(df_m30, 23250.0, rules)
    assert gex_m30["regime"] == "LONG"
    assert "LONG gamma" in gex_m30["terminal_str"]


def test_cascade_advice_no_amplification_in_neutral_band():
    """Verify Part 4 cascade warning does NOT claim amplification or dampening when GEX is NEUTRAL."""
    warn_neutral = agt_briefing.format_floor_weakening_warning(
        streak=2, min_consecutive=2, strike=23200.0, pe_delta=-30000.0, gex_regime="NEUTRAL", staged=True
    )
    assert warn_neutral is not None
    assert "amplifies" not in warn_neutral
    assert "dampened" not in warn_neutral
    assert "no regime (moves follow flow)" in warn_neutral

    warn_short = agt_briefing.format_floor_weakening_warning(
        streak=2, min_consecutive=2, strike=23200.0, pe_delta=-30000.0, gex_regime="SHORT", staged=True
    )
    assert warn_short is not None
    assert "amplifies break" in warn_short


def test_replay_0920_0940_no_consecutive_jump_exceeds_guard():
    """Replay 2026-09-24 09:20-09:40 and assert no consecutive-poll percentile jump exceeds iv_max_pct_jump_per_poll."""
    import pyarrow.parquet as pq

    agt_briefing.reset_session_iv_state()
    rules = agt_briefing.load_briefing_rules()
    d = date(2026, 9, 24)

    snap_dir = config.SNAPSHOT_DIR / "2026-09-24"
    if not snap_dir.exists():
        pytest.skip("Snapshot directory not found")

    snaps = sorted(snap_dir.glob("*.parquet"))
    replay_snaps = [s for s in snaps if "092000" <= s.stem[:6] <= "094000"]
    if not replay_snaps:
        pytest.skip("No snapshots found in 09:20-09:40 window")

    prev_pct = None
    max_jump_allowed = float(rules.get("iv_max_pct_jump_per_poll", 25.0))

    for s in replay_snaps:
        stem = s.stem[:6]
        ts = datetime.strptime("2026-09-24 " + stem, "%Y-%m-%d %H%M%S")
        table = pq.read_table(s)
        meta = table.schema.metadata
        spot = None
        if meta and b"PANDAS_ATTRS" in meta:
            attrs = json.loads(meta[b"PANDAS_ATTRS"].decode("utf-8"))
            spot = attrs.get("spot")
        if not spot:
            continue

        df = table.to_pandas()
        iv_data = agt_briefing.compute_iv_context(df, spot, ts, d, rules)
        pct = iv_data.get("percentile")

        if prev_pct is not None and pct is not None:
            jump = abs(pct - prev_pct)
            assert jump <= max_jump_allowed + 1e-6, (
                f"Percentile jump {jump:.2f} exceeded max allowed {max_jump_allowed:.2f} "
                f"at {stem} ({prev_pct:.1f}% -> {pct:.1f}%)"
            )

        # Check Box 3 does not flip during the 09:24-09:28 anomaly window
        if "092400" <= stem <= "092800":
            chk = agt_briefing.compute_pretrade_checklist(
                None, None, 0.0, 0.0, pct, {"status": "ok", "total_gex_cr": 0.0}, ts, d, ts.time(), rules
            )
            b3 = next(b for b in chk["boxes"] if "IV" in b[0])
            assert "FAIL" not in b3[1], f"Box 3 flipped to FAIL at {stem} (pct={pct})"

        prev_pct = pct


def test_iv_carry_max_polls_bound_and_recovery():
    """Verify that after iv_carry_max_polls consecutive carries, guard stops and emits unavailable with Box 3 HOLD."""
    rules = agt_briefing.load_briefing_rules()
    rules["iv_carry_max_polls"] = 5
    rules["iv_max_pct_jump_per_poll"] = 25.0
    agt_briefing.reset_session_iv_state()

    d = date(2026, 9, 24)
    base_ts = datetime(2026, 9, 24, 10, 0, 0)

    # Clean healthy baseline: ~10.2% IV -> ~22nd percentile (CHEAP)
    df_clean = pd.DataFrame([
        {"strike": 23200.0, "ce_iv": 9.1, "pe_iv": 11.3},
        {"strike": 23250.0, "ce_iv": 9.2, "pe_iv": 11.2},
    ])

    # Bad feed anomaly: ~22.5% IV -> ~95th percentile (EXPENSIVE)
    df_bad = pd.DataFrame([
        {"strike": 23200.0, "ce_iv": 22.0, "pe_iv": 22.0},
        {"strike": 23250.0, "ce_iv": 23.0, "pe_iv": 23.0},
    ])

    # 1. Establish baseline
    res0 = agt_briefing.compute_iv_context(df_clean, 23225.0, base_ts, d, rules)
    assert res0["status"] == "ok"
    assert res0["percentile"] is not None
    base_pct = res0["percentile"]

    # 2. Polls 1 to 5 (5 consecutive carries allowed)
    for i in range(1, 6):
        ts = base_ts + timedelta(minutes=i)
        res = agt_briefing.compute_iv_context(df_bad, 23225.0, ts, d, rules)
        assert res["status"] == "ok", f"Poll {i} should carry previous value"
        assert res["percentile"] == base_pct, f"Poll {i} should carry {base_pct}"
        chk = agt_briefing.compute_pretrade_checklist(
            None, None, 0.0, 0.0, res["percentile"], {"status": "ok", "total_gex_cr": 0.0},
            ts, d, ts.time(), rules, iv_status=res.get("status"), iv_data=res
        )
        b3 = next(b for b in chk["boxes"] if "IV" in b[0])
        assert "PASS" in b3[1]

    # 3. Poll 6: Exceeds iv_carry_max_polls (5) -> must stop carrying and emit feed anomaly
    ts_6 = base_ts + timedelta(minutes=6)
    res_6 = agt_briefing.compute_iv_context(df_bad, 23225.0, ts_6, d, rules)
    assert res_6["status"] == "feed_anomaly"
    assert res_6["percentile"] is None
    assert "IV: unavailable (feed anomaly 6 polls)" in res_6["terminal_str"]

    # Box 3 must be HOLD ⏸ (neither PASS ✅ nor FAIL ✗)
    chk_6 = agt_briefing.compute_pretrade_checklist(
        None, None, 0.0, 0.0, res_6["percentile"], {"status": "ok", "total_gex_cr": 0.0},
        ts_6, d, ts_6.time(), rules, iv_status=res_6.get("status"), iv_data=res_6
    )
    b3_6 = next(b for b in chk_6["boxes"] if "IV" in b[0])
    assert b3_6[1] == "HOLD ⏸"
    assert "PASS" not in b3_6[1]
    assert "FAIL" not in b3_6[1]

    # Observation log line must emit '- IV: unavailable (feed anomaly 6 polls)'
    obs_6 = agt_briefing.format_observation_entry(
        d, ts_6, 23225.0, "NEUTRAL", {"status": "ok", "total_gex_cr": 0.0, "regime": "NEUTRAL"},
        res_6, None, None, 0.001, 0.0, 0.0, 0, chk_6, "NO TRADE", rules
    )
    assert "- IV: unavailable (feed anomaly 6 polls)" in obs_6

    # 4. Feed recovery at Poll 7: valid feed returns -> resets carry counter
    ts_7 = base_ts + timedelta(minutes=7)
    res_7 = agt_briefing.compute_iv_context(df_clean, 23225.0, ts_7, d, rules)
    assert res_7["status"] == "ok"
    assert res_7["percentile"] is not None
    assert abs(res_7["percentile"] - base_pct) <= rules["iv_max_pct_jump_per_poll"]
    chk_7 = agt_briefing.compute_pretrade_checklist(
        None, None, 0.0, 0.0, res_7["percentile"], {"status": "ok", "total_gex_cr": 0.0},
        ts_7, d, ts_7.time(), rules, iv_status=res_7.get("status"), iv_data=res_7
    )
    b3_7 = next(b for b in chk_7["boxes"] if "IV" in b[0])
    assert "PASS" in b3_7[1]
