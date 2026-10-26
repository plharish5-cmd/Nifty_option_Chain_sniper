"""
test_oi_matrix.py — Unit tests for Institutional Open Interest Matrix & Strike Map 2.0.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("ICT_DATA_DIR", str(Path(__file__).resolve().parent / ".data"))

from app.engine import oi_matrix


def test_classify_buildup():
    # Long Buildup: Price Rising & OI Rising
    code, label, meaning = oi_matrix.classify_buildup(chg=15.0, oi_chg=50000.0, is_ce=True)
    assert code == "LONG_BUILDUP"
    assert label == "Long Buildup"

    # Short Buildup: Price Falling & OI Rising
    code, label, meaning = oi_matrix.classify_buildup(chg=-12.0, oi_chg=85000.0, is_ce=False)
    assert code == "SHORT_BUILDUP"
    assert label == "Short Buildup"
    assert "Put Writers" in meaning

    # Short Covering: Price Rising & OI Falling
    code, label, meaning = oi_matrix.classify_buildup(chg=20.0, oi_chg=-45000.0, is_ce=True)
    assert code == "SHORT_COVERING"
    assert label == "Short Covering"
    assert "Squeeze" in meaning or "Covering" in meaning

    # Long Unwinding: Price Falling & OI Falling
    code, label, meaning = oi_matrix.classify_buildup(chg=-8.0, oi_chg=-30000.0, is_ce=False)
    assert code == "LONG_UNWINDING"
    assert label == "Long Unwinding"

    # Neutral
    code, label, meaning = oi_matrix.classify_buildup(chg=0.0, oi_chg=0.0, is_ce=True)
    assert code == "NEUTRAL"


def test_compute_max_pain():
    strikes = [
        {"strike": 22400.0, "ce_oi": 100000, "pe_oi": 500000},
        {"strike": 22500.0, "ce_oi": 300000, "pe_oi": 400000},
        {"strike": 22600.0, "ce_oi": 600000, "pe_oi": 300000},
        {"strike": 22700.0, "ce_oi": 800000, "pe_oi": 100000},
    ]
    mp = oi_matrix.compute_max_pain(strikes)
    assert 22400.0 <= mp <= 22700.0


def test_sentiment_score():
    totals_bullish = {
        "pe_oi_chg": 2000000,
        "ce_oi_chg": 500000,
        "pcr_oi": 1.35,
    }
    strikes_bullish = [
        {"pe_buildup": "SHORT_BUILDUP", "pe_oi_chg": 1500000, "ce_buildup": "SHORT_COVERING", "ce_oi_chg": -200000},
        {"pe_buildup": "SHORT_BUILDUP", "pe_oi_chg": 500000, "ce_buildup": "SHORT_BUILDUP", "ce_oi_chg": 500000},
    ]
    sent_bull = oi_matrix.compute_sentiment_score(totals_bullish, strikes_bullish)
    assert sent_bull["bias"] == "BULLISH"
    assert sent_bull["score"] >= 55.0

    totals_bearish = {
        "pe_oi_chg": 200000,
        "ce_oi_chg": 2500000,
        "pcr_oi": 0.65,
    }
    strikes_bearish = [
        {"pe_buildup": "SHORT_COVERING", "pe_oi_chg": -800000, "ce_buildup": "SHORT_BUILDUP", "ce_oi_chg": 1800000},
    ]
    sent_bear = oi_matrix.compute_sentiment_score(totals_bearish, strikes_bearish)
    assert sent_bear["bias"] == "BEARISH"
    assert sent_bear["score"] <= 45.0


def test_build_oi_matrix_scopes():
    mock_rows = []
    base_k = 22700.0
    for i in range(-15, 16):
        k = base_k + i * 50
        mock_rows.append({
            "strike": k,
            "ce": {"oi": 500000 + i * 10000, "oi_chg": 50000, "ltp": max(10, 100 - i * 10), "chg": -2.0, "vol": 10000},
            "pe": {"oi": 400000 - i * 10000, "oi_chg": 80000, "ltp": max(10, 100 + i * 10), "chg": 3.0, "vol": 15000},
        })

    chain_dict = {
        "day": "2026-10-22",
        "expiry": "2026-10-27",
        "spot": 22710.0,
        "atm": 22700.0,
        "step": 50.0,
        "rows": mock_rows,
    }

    # ATM ±10 Scope
    res_10 = oi_matrix.build_oi_matrix(chain_dict, scope="atm_10")
    assert res_10["ok"] is True
    assert len(res_10["strikes"]) == 21  # 10 down + atm + 10 up
    assert res_10["scope"] == "atm_10"
    assert "22,200" in res_10["strike_range_label"]

    # ATM ±5 Scope
    res_5 = oi_matrix.build_oi_matrix(chain_dict, scope="atm_5")
    assert len(res_5["strikes"]) == 11  # 5 down + atm + 5 up
    assert res_5["scope"] == "atm_5"

    # Full Chain
    res_full = oi_matrix.build_oi_matrix(chain_dict, scope="full")
    assert len(res_full["strikes"]) == 31
    assert "Full Chain" in res_full["strike_range_label"]


def test_build_oi_matrix_both_expiries():
    cur_rows = [
        {"strike": 22600.0, "ce": {"oi": 1000, "oi_chg": 100, "ltp": 50, "chg": -1, "vol": 200},
         "pe": {"oi": 2000, "oi_chg": 200, "ltp": 60, "chg": 2, "vol": 300}},
        {"strike": 22700.0, "ce": {"oi": 3000, "oi_chg": 300, "ltp": 40, "chg": -2, "vol": 400},
         "pe": {"oi": 4000, "oi_chg": 400, "ltp": 70, "chg": 3, "vol": 500}},
    ]
    next_rows = [
        {"strike": 22600.0, "ce": {"oi": 500, "oi_chg": 50, "ltp": 80, "chg": -1, "vol": 100},
         "pe": {"oi": 1000, "oi_chg": 100, "ltp": 90, "chg": 1, "vol": 150}},
        {"strike": 22700.0, "ce": {"oi": 1500, "oi_chg": 150, "ltp": 70, "chg": -2, "vol": 200},
         "pe": {"oi": 2000, "oi_chg": 200, "ltp": 100, "chg": 2, "vol": 250}},
    ]
    cur_ch = {"day": "2026-10-22", "expiry": "2026-10-27", "spot": 22700.0, "step": 50.0, "rows": cur_rows}
    next_ch = {"day": "2026-10-22", "expiry": "2026-11-03", "spot": 22700.0, "step": 50.0, "rows": next_rows}

    res_both = oi_matrix.build_oi_matrix(cur_ch, next_chain_dict=next_ch, expiry_mode="both", scope="full")
    assert res_both["ok"] is True
    assert res_both["selected_expiry"] == "both"
    # Verify sum for 22600 CE OI: 1000 + 500 = 1500
    st_22600 = next(s for s in res_both["strikes"] if s["strike"] == 22600.0)
    assert st_22600["ce_oi"] == 1500
    assert st_22600["pe_oi"] == 3000


def test_enriched_strike_map_fields():
    """Verify that strike arrays supply call_oi, call_oi_chg, put_oi, put_oi_chg, buildup, max_pain, spot."""
    mock_rows = [
        {"strike": 22600.0, "ce": {"oi": 50000, "oi_chg": 5000, "ltp": 80.0, "chg": -4.0, "vol": 1000},
         "pe": {"oi": 30000, "oi_chg": -2000, "ltp": 40.0, "chg": -1.5, "vol": 800}},
        {"strike": 22700.0, "ce": {"oi": 40000, "oi_chg": -3000, "ltp": 50.0, "chg": 2.0, "vol": 1200},
         "pe": {"oi": 60000, "oi_chg": 8000, "ltp": 65.0, "chg": -3.0, "vol": 1500}},
    ]
    chain_dict = {
        "day": "2026-10-22",
        "expiry": "2026-10-27",
        "spot": 22680.0,
        "step": 50.0,
        "rows": mock_rows,
    }
    res = oi_matrix.build_oi_matrix(chain_dict, scope="full")
    assert res["ok"] is True
    assert "strike_map" in res
    assert len(res["strike_map"]) == 2

    s0 = res["strike_map"][0]
    assert s0["strike"] == 22600.0
    assert s0["call_oi"] == 50000
    assert s0["call_oi_chg"] == 5000
    assert s0["put_oi"] == 30000
    assert s0["put_oi_chg"] == -2000
    assert s0["buildup"] in ("Short Buildup", "Long Buildup", "Short Covering", "Long Unwinding")
    assert s0["max_pain"] > 0
    assert s0["spot"] == 22680.0
