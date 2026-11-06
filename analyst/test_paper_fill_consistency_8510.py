"""
analyst/test_paper_fill_consistency_8510.py — the two ledgers must agree.

TWO DEFECTS THIS LOCKS DOWN
---------------------------
1. THE ORDER AND THE POSITION DISAGREED ABOUT THE SAME FILL.
   execute_order_with_chaser set `fill_price = signal_ltp` up front, and the
   paper block then computed `paper_fill_price = signal_ltp + sim_slippage`
   and used it ONLY for the position row. So the orders book recorded a fill at
   the signal price while the position recorded a worse one, for the same trade.
   P&L came from the position, so the slippage was real in the P&L and invisible
   in the orders book — which is why scripts/report_slippage.py read a constant
   zero across 369 fills and nearly concluded that execution was free.

2. PAPER SLIPPAGE WAS UNSEEDED RANDOMNESS.
   sim_slippage = np.random.choice([0.05, 0.10, 0.15, 0.20]) with no seed meant
   the same signal produced different P&L on every re-run, so replay and
   backtest reconciliation could never match the live ledger.

The values are still ASSUMED — nothing here measures real slippage, and it
cannot be measured while execution_mode is PAPER_ONLY. These tests only ensure
the assumption is applied consistently, reproducibly, and from config.
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analyst.terminal8510_backend import QuantTerminalBackend  # noqa: E402

HISTORICAL = [0.05, 0.10, 0.15, 0.20]


def backend(cfg=None):
    b = object.__new__(QuantTerminalBackend)
    b.cfg = cfg if cfg is not None else {}
    return b


# ─────────────────────────────────────────────────────────────────────────────
# 1. REPRODUCIBILITY — replay must reconcile
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("oid", ["ORD_1762428000000", "ORD_1", "P_ORD_99", "x"])
def test_same_order_id_always_gives_the_same_slippage(oid):
    b = backend()
    first = b._simulated_slippage_pts(oid)
    assert all(b._simulated_slippage_pts(oid) == first for _ in range(25))


def test_a_fresh_backend_reproduces_the_same_value():
    """No global RNG state: a restarted process must agree with the ledger."""
    oid = "ORD_1762428123456"
    assert backend()._simulated_slippage_pts(oid) == backend()._simulated_slippage_pts(oid)


def test_different_orders_get_different_values():
    """Deterministic must not mean constant, or every fill slips identically."""
    b = backend()
    vals = {b._simulated_slippage_pts("ORD_%d" % i) for i in range(200)}
    assert len(vals) > 1, "slippage collapsed to a single value"


def test_spread_covers_the_configured_values():
    b = backend()
    seen = {b._simulated_slippage_pts("ORD_%d" % i) for i in range(2000)}
    assert seen == set(HISTORICAL), seen


def test_mean_matches_the_documented_figure():
    """The config evidence note claims mean 0.125 pts = Rs8.13 on lot 65."""
    b = backend()
    vals = [b._simulated_slippage_pts("ORD_%d" % i) for i in range(4000)]
    mean = sum(vals) / len(vals)
    assert mean == pytest.approx(0.125, abs=0.01)
    assert mean * 65 == pytest.approx(8.13, abs=0.65)


# ─────────────────────────────────────────────────────────────────────────────
# 2. VALUES COME FROM CONFIG, NOT CODE
# ─────────────────────────────────────────────────────────────────────────────

def test_config_values_are_honoured():
    b = backend({"paper_execution": {"simulated_slippage_pts": [0.5]}})
    assert b._simulated_slippage_pts("ORD_X") == 0.5


def test_absent_key_falls_back_to_the_historical_values():
    assert backend({})._simulated_slippage_pts("ORD_X") in HISTORICAL
    assert backend({"paper_execution": {}})._simulated_slippage_pts("ORD_X") in HISTORICAL


def test_explicit_empty_list_means_zero_not_the_defaults():
    """`or` would have conflated an empty list with an absent key and silently
    reinstated slippage someone had deliberately turned off."""
    b = backend({"paper_execution": {"simulated_slippage_pts": []}})
    assert b._simulated_slippage_pts("ORD_X") == 0.0


def test_random_mode_still_available():
    b = backend({"paper_execution": {"simulated_slippage_pts": HISTORICAL,
                                     "deterministic_per_order": False}})
    vals = {b._simulated_slippage_pts("ORD_SAME") for _ in range(200)}
    assert len(vals) > 1, "deterministic_per_order=False should vary"


def test_the_shipped_config_declares_the_assumption():
    """The values must be in config WITH an evidence note, so nobody later
    mistakes an invented constant for a measurement."""
    cfg = json.loads((ROOT / "analyst" / "terminal8510_config.json").read_text())
    pe = cfg.get("paper_execution")
    assert pe, "paper_execution block missing from config"
    assert pe["simulated_slippage_pts"] == HISTORICAL
    ev = pe.get("_evidence", "")
    assert "ASSUMED" in ev.upper()
    assert "32.50" in ev, "the evidence must name the conflicting backtest figure"
    assert "PAPER_ONLY" in ev


# ─────────────────────────────────────────────────────────────────────────────
# 3. THE ORDERS BOOK MUST RECORD THE PRICE THE POSITION PAID
# ─────────────────────────────────────────────────────────────────────────────

def test_paper_only_overrides_the_order_fill_price():
    """Source-level guard: under PAPER_ONLY the order row must take
    paper_fill_price, otherwise slippage reads zero in the orders book while the
    position carries it. Checked in source because reaching the real code path
    needs a broker, an instrument master and a market session."""
    src = (ROOT / "analyst" / "terminal8510_backend.py").read_text()
    assert 'if mode == "PAPER_ONLY":\n                    fill_price = paper_fill_price' in src, \
        "the PAPER_ONLY fill_price override is missing"


def test_slippage_column_is_derived_from_the_recorded_fill():
    src = (ROOT / "analyst" / "terminal8510_backend.py").read_text()
    assert "slippage_pts = round(fill_price - signal_ltp, 2)" in src


def test_both_mode_keeps_the_live_fill_authoritative():
    """Under BOTH there is a real Dhan fill and a paper fill; the order row must
    keep the live one, so the override is scoped to PAPER_ONLY only."""
    src = (ROOT / "analyst" / "terminal8510_backend.py").read_text()
    assert 'if mode == "PAPER_ONLY":' in src
    assert 'if mode in ("PAPER_ONLY", "BOTH"):\n                    fill_price = paper_fill_price' not in src


def test_no_unseeded_random_choice_remains_for_slippage():
    src = (ROOT / "analyst" / "terminal8510_backend.py").read_text()
    assert "np.random.choice([0.05, 0.10, 0.15, 0.20])" not in src, \
        "the hardcoded unseeded draw is back"
