"""
test_strike_select.py — Unit tests for Strike Selection & Premium Behaviour Engine.
"""

from datetime import datetime, timedelta
import numpy as np
import pandas as pd
import pytest

from strike_select import (
    load_rules,
    get_iv_regime,
    evaluate_strike_candidates,
    project_theta_decay,
    analyze_wall_intent,
    run_strike_selection,
    StrikeCandidate,
)


def make_mock_chain(spot: float = 23400.0) -> pd.DataFrame:
    """Builds a representative NIFTY option chain DataFrame."""
    strikes = [22800, 22900, 23000, 23100, 23200, 23300, 23400, 23500, 23600, 23700, 23800, 23900, 24000]
    data = []
    for k in strikes:
        dist = k - spot
        # Simulated Call values
        if dist < -400:
            c_ltp, c_delta, c_theta, c_iv = 570.0, 0.95, -6.5, 14.2
        elif dist == 0:
            c_ltp, c_delta, c_theta, c_iv = 133.0, 0.60, -12.5, 13.5
        elif dist > 400:
            c_ltp, c_delta, c_theta, c_iv = 4.0, 0.04, -1.2, 16.8
        else:
            # Interpolated values
            approx_d = 0.5 - (dist / 1000.0)
            c_delta = max(0.05, min(0.95, approx_d))
            c_ltp = max(5.0, 150.0 - dist * 0.4)
            c_theta = -10.0
            c_iv = 13.8

        # Simulated Put values
        if dist > 400:
            p_ltp, p_delta, p_theta, p_iv = 550.0, -0.92, -6.0, 14.5
        elif dist == 0:
            p_ltp, p_delta, p_theta, p_iv = 120.0, -0.40, -11.0, 13.5
        elif dist < -400:
            p_ltp, p_delta, p_theta, p_iv = 5.0, -0.05, -1.5, 16.5
        else:
            p_delta = -max(0.05, min(0.95, 0.5 + (dist / 1000.0)))
            p_ltp = max(5.0, 130.0 + dist * 0.35)
            p_theta = -9.5
            p_iv = 13.8

        data.append({
            "strike": k,
            "ce_oi": 50000 + (30000 if k == 23500 else 0),
            "ce_chg_oi": 5000,
            "ce_vol": 80000,
            "ce_iv": c_iv,
            "ce_ltp": c_ltp,
            "ce_chg": 5.0,
            "ce_bid": c_ltp - 0.5,
            "ce_ask": c_ltp + 0.5,
            "ce_delta": c_delta,
            "ce_gamma": 0.0012,
            "ce_theta": c_theta,
            "ce_vega": 8.5,
            "pe_oi": 60000 + (40000 if k == 23300 else 0),
            "pe_chg_oi": 4000,
            "pe_vol": 75000,
            "pe_iv": p_iv,
            "pe_ltp": p_ltp,
            "pe_chg": -4.0,
            "pe_bid": p_ltp - 0.5,
            "pe_ask": p_ltp + 0.5,
            "pe_delta": p_delta,
            "pe_gamma": 0.0012,
            "pe_theta": p_theta,
            "pe_vega": 8.5,
        })
    return pd.DataFrame(data)


def test_iv_regimes():
    """Verifies classification into sub-10, normal 10-20, and excessive >20."""
    r = load_rules()

    sub = get_iv_regime(8.5, r)
    assert sub.regime_key == "sub_10"
    assert sub.sub_10_warning is True
    assert "BAD" in sub.buyer_verdict
    assert "VERY GOOD" in sub.seller_verdict

    norm = get_iv_regime(14.5, r)
    assert norm.regime_key == "normal_10_20"
    assert norm.sub_10_warning is False
    assert "GOOD" in norm.buyer_verdict
    assert "risky" in norm.seller_verdict

    exc = get_iv_regime(24.0, r)
    assert exc.regime_key == "excessive_above_20"
    assert exc.sub_10_warning is False
    assert "NOT good" in exc.buyer_verdict
    assert "DANGEROUS" in exc.seller_verdict


def test_guards_empty_and_corrupt_data():
    """Ensures safe behavior with None, empty chain, or missing Greeks."""
    # 1. None DataFrame
    res_none = run_strike_selection(None, spot=23400.0)
    assert res_none.guard_warning is not None
    assert len(res_none.candidates) == 0

    # 2. Empty DataFrame
    res_empty = run_strike_selection(pd.DataFrame(), spot=23400.0)
    assert res_empty.guard_warning is not None

    # 3. Spot zero
    res_zero_spot = run_strike_selection(make_mock_chain(), spot=0.0)
    assert res_zero_spot.guard_warning is not None

    # 4. Chain with missing delta columns (falls back safely)
    df_no_greeks = make_mock_chain()
    df_no_greeks = df_no_greeks.drop(columns=["ce_delta", "pe_delta"])
    res_fallback = run_strike_selection(df_no_greeks, spot=23400.0)
    assert len(res_fallback.candidates) > 0


def test_source_three_strike_outcomes():
    """
    Validates exact math from the source transcript:
      - 22,900 CE: ₹570 prem, delta 0.95 -> 38 pts gain -> ₹2,470 (Cost ₹37,050, ROC 6.67%)
      - 23,400 CE: ₹133 prem, delta 0.60 -> 24 pts gain -> ₹1,560 (Cost ₹8,645, ROC 18.05%)
      - 23,900 CE: ₹4 prem,   delta 0.04 -> 1.6 pts gain -> ₹104   (Cost ₹260,   ROC 40.0%)
    """
    df = make_mock_chain(spot=23400.0)
    cands = evaluate_strike_candidates(
        df=df,
        spot=23400.0,
        direction="CE",
        target_pts=40.0,
        stop_pts=40.0,
        lot_size=65,
        capital=20000.0,
    )

    c_map = {c.strike: c for c in cands}

    # Strike 22,900
    c_22900 = c_map[22900]
    assert c_22900.premium == 570.0
    assert c_22900.delta == 0.95
    assert c_22900.expected_premium_gain == 38.0
    assert c_22900.expected_rupees == 2470.0
    assert c_22900.lot_cost == 37050.0
    assert pytest.approx(c_22900.roc_pct, 0.01) == 6.67
    assert c_22900.exceeds_capital_limit is True  # 37050 > 30% of 20k

    # Strike 23,400
    c_23400 = c_map[23400]
    assert c_23400.premium == 133.0
    assert c_23400.delta == 0.60
    assert c_23400.expected_premium_gain == 24.0
    assert c_23400.expected_rupees == 1560.0
    assert c_23400.lot_cost == 8645.0
    assert pytest.approx(c_23400.roc_pct, 0.01) == 18.05

    # Strike 23,900
    c_23900 = c_map[23900]
    assert c_23900.premium == 4.0
    assert c_23900.delta == 0.04
    assert c_23900.expected_premium_gain == 1.6
    assert c_23900.expected_rupees == 104.0
    assert c_23900.lot_cost == 260.0
    assert pytest.approx(c_23900.roc_pct, 0.01) == 40.0
    assert c_23900.exceeds_capital_limit is False

    # 24x outcome ratio check between 22900 (₹2470) and 23900 (₹104)
    outcome_ratio = c_22900.expected_rupees / c_23900.expected_rupees
    assert pytest.approx(outcome_ratio, 0.5) == 23.75  # ~24x!


def test_theta_decay_calendar_steps():
    """Verifies multi-session decay modeling and intrinsic floor preservation."""
    df = make_mock_chain(spot=23400.0)
    cands = evaluate_strike_candidates(df=df, spot=23400.0, direction="CE", lot_size=65)
    c_map = {c.strike: c for c in cands}

    # ITM option: 22900 CE (Spot 23400 -> Intrinsic = 500)
    c_itm = c_map[22900]
    now = datetime(2026, 9, 11, 11, 45)  # Friday morning
    proj_itm = project_theta_decay(c_itm, spot=23400.0, now=now, dte_calendar_days=4.0, lot_size=65)

    assert len(proj_itm.steps) >= 4
    # All projected steps must never fall below intrinsic 500
    for s in proj_itm.steps:
        assert s.projected_premium >= 500.0

    # Weekend warning check
    assert "Weekend Decay Warning" in proj_itm.weekend_warning


def test_wall_intent_and_oi_correction():
    """Verifies 4-participant restatement and contract distinction."""
    df = make_mock_chain(spot=23400.0)
    wall = analyze_wall_intent(df, spot=23400.0)

    assert wall.max_call_oi_strike == 23500
    assert wall.max_put_oi_strike == 23300
    assert "Fact Check: Open Interest measures outstanding contracts" in wall.oi_contract_distinction
    assert len(wall.strikes) > 0


def test_strike_select_tab_render():
    """Verifies that strike_select_tab.render(ctx) executes cleanly."""
    from unittest.mock import MagicMock, patch
    import context
    import strike_select_tab

    df = make_mock_chain(spot=23400.0)
    ctx = context.Ctx(
        df=df,
        spot=23400.0,
        dte=4.0,
        now=datetime(2026, 9, 11, 10, 0),
    )

    # Mock Streamlit UI calls so render runs headless
    def mock_cols(spec):
        n = spec if isinstance(spec, int) else len(spec)
        return [MagicMock() for _ in range(n)]

    with patch("streamlit.markdown"), \
         patch("streamlit.caption"), \
         patch("streamlit.info"), \
         patch("streamlit.warning"), \
         patch("streamlit.error"), \
         patch("streamlit.metric"), \
         patch("streamlit.columns", side_effect=mock_cols), \
         patch("streamlit.radio", return_value="CE"), \
         patch("streamlit.number_input", side_effect=[40, 40]), \
         patch("streamlit.slider", return_value=(200, 300)), \
         patch("streamlit.selectbox", return_value=23400), \
         patch("streamlit.dataframe"), \
         patch("streamlit.plotly_chart"), \
         patch("streamlit.write"):
        strike_select_tab.render(ctx)

