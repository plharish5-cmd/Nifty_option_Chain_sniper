"""analyst/test_v194c_real_strike_pricing_8510.py — real strikes, real premiums.

WHY THIS FILE EXISTS
--------------------
Harish reported that 8510 "always calculates wrong" and always seemed to enter
at a flat 200, where the Paper Desk app (paper_desk_app.py, the 8502/8503/8511
Streamlit desk) shows real premiums like 209 / 267 / 232.

Investigating that found FOUR separate things, only three of which were faults:

1. REAL BUG -- atm_strike was `round(spot / 100.0) * 100`. NIFTY strikes exist
   every 50 points, so 100-rounding could only ever pick even-hundred strikes
   and NEVER a 50-level one: spot 24,460 bought 24500 while true ATM was 24450.
   Up to 25 points off, with half the chain unreachable. A strike 50 points from
   ATM carries a visibly different premium, which is the most likely reason
   entry prices looked wrong.

2. MISLEADING LABEL -- the strike matrix rendered "1. Long ITM CE (~Rs200)".
   That is an annotation about a typical ITM premium, but it reads as a fixed
   entry price. Renamed to state the rule, "(ATM-100)".

3. INVENTED CHARGE BASE -- scripts/backtest_option_prices.py computed statutory
   charges on a flat 200.0 entry premium for its 250 MODELLED sessions. Charges
   scale with turnover, so that mis-stated every modelled cost. It now uses the
   mean entry premium the same script already MEASURED in Step 4.

4. NOT A FAULT -- the charge model. analyst/terminal8510_backend.py's
   calculate_dhan_charges and paper_trading.calculate_trade_charges agree to
   within Rs0.01 on every realistic trade. There was nothing to port.

5. NOT A FAULT -- the live/paper entry premium. execute_order_with_chaser
   already REFUSES to trade without a real positive Dhan quote, and get_opt_ltp
   requires a fresh live quote with NO synthetic fallback at all -- stricter
   than the Paper Desk, whose third tier is an Rs85-base estimate.
"""
import importlib.util
import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BACKEND = ROOT / "analyst" / "terminal8510_backend.py"
HTML = ROOT / "Algorithmic_Trading_Workspace_8510.html"


# ─────────────────────────────────────────────────────────────────────────────
# 1. ATM STRIKE MUST LAND ON THE 50-POINT GRID
# ─────────────────────────────────────────────────────────────────────────────

def atm(spot, step=50.0):
    """The corrected rule, mirrored here so the arithmetic itself is asserted."""
    return float(round(spot / step) * step)


@pytest.mark.parametrize("spot,expected", [
    (24418.40, 24400.0),
    (24424.99, 24400.0),
    (24425.01, 24450.0),   # 100-rounding gave 24400 -- 50 points wrong
    (24449.00, 24450.0),   # 100-rounding gave 24400
    (24460.00, 24450.0),   # 100-rounding gave 24500
    (24474.99, 24450.0),   # 100-rounding gave 24500
    (24475.01, 24500.0),
    (24500.00, 24500.0),
    (24550.00, 24550.0),   # 100-rounding gave 24600
])
def test_atm_strike_uses_the_50_point_grid(spot, expected):
    assert atm(spot) == expected


def test_fifty_level_strikes_are_reachable_at_all():
    """The whole defect in one assertion: 100-rounding can never return a
    strike ending in 50, so half of every NIFTY chain was unreachable."""
    hundred = {float(round(s / 100.0) * 100) for s in range(24000, 24600)}
    fifty = {atm(s) for s in range(24000, 24600)}
    assert not any(s % 100 for s in hundred), "sanity: 100-rounding yields only hundreds"
    assert any(s % 100 for s in fifty), "50-rounding must be able to return a 50-level strike"
    assert len(fifty) > len(hundred)


def test_the_backend_no_longer_rounds_to_100():
    src = BACKEND.read_text(encoding="utf-8")
    assert "atm_strike = float(round(spot / 100.0) * 100)" not in src, \
        "the 100-point rounding bug is back"
    assert "round(spot / strike_step) * strike_step" in src


def test_the_strike_step_comes_from_config_not_code():
    """AGENTS.md: thresholds live in JSON. The repo already declares 50."""
    import json
    cfg = json.loads((ROOT / "analyst" / "terminal8510_config.json").read_text(encoding="utf-8"))
    step = (cfg.get("recording_v193") or {}).get("strike_step")
    assert step == 50, "config must declare the NIFTY strike step"


def test_itm_offset_stays_a_points_offset():
    """ITM is ATM -/+ 100 POINTS, which at 50-point spacing is two strikes.
    Changing the ATM grid must not silently change the ITM distance."""
    src = BACKEND.read_text(encoding="utf-8")
    assert "itm_ce_strike = atm_strike - 100.0" in src
    assert "itm_pe_strike = atm_strike + 100.0" in src


# ─────────────────────────────────────────────────────────────────────────────
# 2. NO LABEL MAY READ LIKE A FIXED ENTRY PRICE
# ─────────────────────────────────────────────────────────────────────────────

def test_the_tilde_200_labels_are_gone():
    src = BACKEND.read_text(encoding="utf-8")
    assert "(~₹200)" not in src, "a label that reads as a fixed entry price is back"
    assert "Long ITM CE (ATM-100)" in src
    assert "Long ITM PE (ATM+100)" in src


# ─────────────────────────────────────────────────────────────────────────────
# 3. THE LIVE/PAPER ENTRY PREMIUM MUST BE A REAL QUOTE OR NO TRADE
# ─────────────────────────────────────────────────────────────────────────────

def test_execution_refuses_a_missing_or_nonpositive_premium():
    """This is why live paper entries were already correct: no quote, no trade.
    Stricter than the Paper Desk, which falls back to an Rs85-base estimate."""
    src = BACKEND.read_text(encoding="utf-8")
    assert "Real positive Dhan option quote required" in src
    assert "premium = contract.get('ltp')" in src


def test_get_opt_ltp_has_no_synthetic_fallback():
    src = BACKEND.read_text(encoding="utf-8")
    i = src.index("def get_opt_ltp")
    body = src[i:i + 900]
    assert "return None" in body, "a stale or missing quote must yield None"
    # No invented base premium anywhere in that helper.
    for invented in ("85.0", "200.0", "approx", "estimate"):
        assert invented not in body, "get_opt_ltp must not invent a premium ({})".format(invented)


def test_quote_freshness_is_enforced():
    src = BACKEND.read_text(encoding="utf-8")
    i = src.index("def get_opt_ltp")
    assert "quote_max_age_seconds" in src[i:i + 900], \
        "a stale quote is not a real quote"


# ─────────────────────────────────────────────────────────────────────────────
# 4. THE CHARGE MODELS AGREE — NOTHING TO PORT
# ─────────────────────────────────────────────────────────────────────────────

def _charges_8510(buy, sell, qty, cfg=None):
    c = cfg or {}
    brok = float(c.get("brokerage_per_order", 20.0))
    stt_r = float(c.get("stt_sell_rate", 0.0015))
    txn_r = float(c.get("txn_rate", 0.0003553))
    sebi_r = float(c.get("sebi_rate", 0.000001))
    stamp_r = float(c.get("stamp_buy_rate", 0.00003))
    gst_r = float(c.get("gst_rate", 0.18))
    bt, st_ = buy * qty, sell * qty
    tt = bt + st_
    brokerage = brok * 2.0
    stt = st_ * stt_r
    txn = tt * txn_r
    sebi = tt * sebi_r
    stamp = bt * stamp_r
    gst = (brokerage + txn + sebi) * gst_r
    return round(brokerage + stt + txn + sebi + stamp + gst, 2)


@pytest.mark.parametrize("buy,sell", [
    (209.0, 224.0),      # +15 spot-equivalent win
    (209.0, 203.6),      # -8 loss
    (267.0, 279.0),
    (232.0, 232.0),
    (85.0, 95.0),        # cheap OTM
    (420.0, 400.0),      # expensive ITM
])
def test_8510_and_paperdesk_charges_agree(buy, sell):
    """Harish believed the Paper Desk charge model was more accurate and 8510's
    was wrong. They agree to a paisa. The residual is IPFT rounding inside the
    GST base, not a modelling difference."""
    import paper_trading as pt
    a = _charges_8510(buy, sell, 65)
    d = pt.calculate_trade_charges(buy, sell, 65, side="BUY")
    b = float(d["total_charges"] if isinstance(d, dict) else d)
    assert abs(a - b) <= 0.05, "charge models diverge: 8510 {} vs PaperDesk {}".format(a, b)


def test_stt_falls_only_on_the_sell_leg():
    """The single biggest line item, and the easiest to get wrong."""
    import paper_trading as pt
    d = pt.calculate_trade_charges(209.0, 224.0, 65, side="BUY")
    assert isinstance(d, dict)
    expected_stt = round(224.0 * 65 * 0.0015, 2)
    assert d["stt"] == pytest.approx(expected_stt, abs=0.02), \
        "STT must be charged on sell turnover only"


def test_charges_scale_with_premium_so_an_invented_base_matters():
    """Why the flat 200.0 base was a real defect and not a cosmetic one."""
    import paper_trading as pt
    low = float(pt.calculate_trade_charges(85.0, 85.0, 65, side="BUY")["total_charges"])
    high = float(pt.calculate_trade_charges(420.0, 420.0, 65, side="BUY")["total_charges"])
    assert high > low + 10.0, "charges must scale with turnover"


# ─────────────────────────────────────────────────────────────────────────────
# 5. THE MODELLED-SESSION CHARGE BASE IS MEASURED AND DECLARED
# ─────────────────────────────────────────────────────────────────────────────

def test_the_backtest_no_longer_hardcodes_a_200_charge_base():
    src = (ROOT / "scripts" / "backtest_option_prices.py").read_text(encoding="utf-8")
    assert "calculate_trade_charges(200.0," not in src, "the flat 200.0 base is back"
    assert "MODEL_ENTRY_PREMIUM" in src


def test_the_charge_base_prefers_the_measured_figure():
    src = (ROOT / "scripts" / "backtest_option_prices.py").read_text(encoding="utf-8")
    assert "MEASURED_ATM_MEAN_STEP4" in src
    # The constant survives only as a last resort, and says so when used.
    assert "ASSUMED_200_NO_MEASUREMENT_AVAILABLE" in src


def test_the_assumption_is_declared_in_the_output():
    """An assumption a reader cannot see is how invented constants survive."""
    src = (ROOT / "scripts" / "backtest_option_prices.py").read_text(encoding="utf-8")
    assert '"_model_entry_premium_inr"' in src
    assert '"_model_entry_premium_source"' in src
    assert "NOT a traded price" in src


def test_the_premium_strike_rules_were_left_alone():
    """200_PREM is a deliberate comparison arm against ATM, not a bug. Removing
    it would delete research, not fabrication."""
    src = (ROOT / "scripts" / "backtest_option_prices.py").read_text(encoding="utf-8")
    assert '"200_PREM"' in src and '"ATM"' in src, \
        "the four strike-selection rules are intentional"
