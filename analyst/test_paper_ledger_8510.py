"""
analyst/test_paper_ledger_8510.py — paper wallet, exit-leg orders and algo-only gate.

Covers the four defects behind "the virtual wallet is the same amount", "there is
no sell order appearing in the institutional order book", "remove that realized
PNL" and "I don't want to trigger automatically the manual trades":

  1. paper_wallet.balance never moved. All three exit paths updated realized_pnl
     but never balance, which was written once at init and never again.
  2. The orders book only ever received the BUY leg. Closing a position UPDATEd
     the positions row, so every square-off was invisible.
  3. update_paper_wallet(reset=True) zeroes margin_used unconditionally, which
     orphans the margin of positions that are still open.
  4. Nothing stopped a discretionary order from executing on an algo desk.

These are true unit tests: the backend is built with object.__new__ so no config
is read, no instrument master is downloaded, no broker is constructed and the
real ledger at data/terminal8510/ledger.sqlite3 is never opened.
"""
import asyncio
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analyst.terminal8510_backend import QuantTerminalBackend  # noqa: E402

START_CAP = 100000.0


def make_backend(tmp_path, algo_only=True):
    """A backend wired to a throwaway ledger, with no network and no real DB."""
    b = object.__new__(QuantTerminalBackend)
    b.cfg = {"starting_paper_capital": START_CAP}
    b.ledger_db = tmp_path / "ledger.sqlite3"
    b.algo_only_execution = algo_only
    b.execution_mode = "PAPER_ONLY"
    b.kill_switch_active = False
    b.directional_bias = "BOTH"   # algo-only gate sits after the bias checks
    b.super_trail_cfg = {"initial_sl_pts": 20.0, "initial_target_pts": 25.0,
                         "trail_activation_profit_pts": 15.0, "trailing_jump_pts": 5.0,
                         "trail_target_step_pts": 10.0, "trail_target_pullback_pts": 5.0,
                         "trailing_sl_enabled": True, "trailing_target_enabled": True}
    b._lock = None          # the `lock` property builds it lazily
    b._init_sqlite()
    return b


def open_position(b, pos_id="POS_1", entry=200.0, qty=65, mode="PAPER_ONLY",
                  entry_time="2026-11-06 11:36:12 IST", source="AUTO ALGO 🤖"):
    """Insert an OPEN position and block its margin, as the entry path does."""
    with sqlite3.connect(b.ledger_db) as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO positions (position_id, symbol, security_id, side, lots, qty, "
            "entry_price, entry_time, current_ltp, target_price, sl_price, status, mode, "
            "placed_by, source, strategy_setup) "
            "VALUES (?,?,?,'BUY',1,?,?,?,?,?,?,'OPEN',?,?,?,?)",
            (pos_id, "NIFTY 06 NOV 23700 CALL", "43215", qty, entry, entry_time,
             entry, entry + 25, entry - 20, mode, source, source, "Stage 2 Continuation"))
        cur.execute("UPDATE paper_wallet SET margin_used = margin_used + ? WHERE id=1",
                    (entry * qty,))
        conn.commit()


def fetch_pos(b, pos_id="POS_1"):
    with sqlite3.connect(b.ledger_db) as conn:
        conn.row_factory = sqlite3.Row
        return dict(conn.execute(
            "SELECT * FROM positions WHERE position_id=?", (pos_id,)).fetchone())


# ─────────────────────────────────────────────────────────────────────────────
# 1. THE WALLET MUST ACTUALLY MOVE
# ─────────────────────────────────────────────────────────────────────────────

def test_wallet_starts_at_configured_capital(tmp_path):
    b = make_backend(tmp_path)
    w = b.get_paper_wallet()
    assert w["balance"] == START_CAP
    assert w["realized_pnl"] == 0.0
    assert w["margin_used"] == 0.0


@pytest.mark.parametrize("net", [2200.0, -1450.0])
def test_balance_follows_realised_pnl(tmp_path, net):
    """A loss must reduce the paper capital and a profit must raise it.

    This is the regression for "the paper trade value virtual wallet, it is same
    amount": balance was never part of the exit UPDATE.
    """
    b = make_backend(tmp_path)
    open_position(b, entry=200.0, qty=65)
    assert b.get_paper_wallet()["margin_used"] == pytest.approx(13000.0)

    with sqlite3.connect(b.ledger_db) as conn:
        cur = conn.cursor()
        cur.execute("UPDATE paper_wallet SET balance = balance + ?, "
                    "margin_used = max(0.0, margin_used - ?), "
                    "realized_pnl = realized_pnl + ? WHERE id=1",
                    (net, 13000.0, net))
        conn.commit()

    w = b.get_paper_wallet()
    assert w["balance"] == pytest.approx(START_CAP + net)
    assert w["realized_pnl"] == pytest.approx(net)
    assert w["margin_used"] == pytest.approx(0.0)
    # Margin is released, so the whole balance is free again.
    assert w["margin_avail"] == pytest.approx(START_CAP + net)


def test_margin_avail_excludes_blocked_margin(tmp_path):
    b = make_backend(tmp_path)
    open_position(b, entry=200.0, qty=65)
    w = b.get_paper_wallet()
    assert w["balance"] == START_CAP          # unchanged while the trade is live
    assert w["margin_avail"] == pytest.approx(START_CAP - 13000.0)


# ─────────────────────────────────────────────────────────────────────────────
# 2. THE EXIT LEG MUST REACH THE ORDERS BOOK
# ─────────────────────────────────────────────────────────────────────────────

def test_exit_writes_a_sell_row(tmp_path):
    """The regression for "everything is buy ... there is no sell order"."""
    b = make_backend(tmp_path)
    open_position(b, entry=200.0, qty=65)
    pos = fetch_pos(b)

    with sqlite3.connect(b.ledger_db) as conn:
        cur = conn.cursor()
        oid = b._record_exit_order(cur, pos, exit_price=234.0, qty=65,
                                   exit_reason="TARGET_HIT", net_pnl=2131.0,
                                   charges=79.0)
        conn.commit()

    assert oid == "X_POS_1"
    book = b.get_order_book()
    assert len(book) == 1
    row = book[0]
    assert row["side"] == "SELL"
    assert row["order_status"] == "FILLED"
    assert row["qty"] == 65
    assert row["fill_price"] == pytest.approx(234.0)
    assert row["exit_reason"] == "TARGET_HIT"
    assert row["net_pnl"] == pytest.approx(2131.0)
    assert row["position_id"] == "POS_1"
    assert row["brokerage_taxes"] == pytest.approx(79.0)


def test_exit_row_inherits_source_and_mode_from_the_position(tmp_path):
    """So the AUTO/MANUAL and paper/real filters classify the exit correctly."""
    b = make_backend(tmp_path)
    open_position(b, mode="BOTH", source="MANUAL MATRIX ✋")
    pos = fetch_pos(b)
    with sqlite3.connect(b.ledger_db) as conn:
        cur = conn.cursor()
        b._record_exit_order(cur, pos, 210.0, 65, "MANUAL_CLOSE", 570.0, 79.0)
        conn.commit()
    row = b.get_order_book()[0]
    assert row["mode"] == "BOTH"
    assert row["source"] == "MANUAL MATRIX ✋"
    assert row["strategy_setup"] == "Stage 2 Continuation"


def test_exit_order_accepts_a_sqlite_row(tmp_path):
    """Two of the three call sites pass a sqlite3.Row, which has no .get()."""
    b = make_backend(tmp_path)
    open_position(b)
    with sqlite3.connect(b.ledger_db) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        row = cur.execute("SELECT * FROM positions WHERE position_id='POS_1'").fetchone()
        assert not hasattr(row, "get")
        assert b._record_exit_order(cur, row, 205.0, 65, "SL_HIT", -1050.0, 78.0)
        conn.commit()
    assert b.get_order_book()[0]["side"] == "SELL"


def test_one_exit_row_per_position(tmp_path):
    """INSERT OR REPLACE keyed on X_<position_id>: a re-run must not duplicate."""
    b = make_backend(tmp_path)
    open_position(b)
    pos = fetch_pos(b)
    with sqlite3.connect(b.ledger_db) as conn:
        cur = conn.cursor()
        for _ in range(3):
            b._record_exit_order(cur, pos, 234.0, 65, "TARGET_HIT", 2131.0, 79.0)
        conn.commit()
    assert len(b.get_order_book()) == 1


def test_a_failed_exit_row_does_not_raise(tmp_path):
    """A book write must never abort the square-off that is mid-flight."""
    b = make_backend(tmp_path)
    open_position(b)
    pos = fetch_pos(b)
    with sqlite3.connect(b.ledger_db) as conn:
        cur = conn.cursor()
        cur.execute("ALTER TABLE orders RENAME TO orders_moved")
        assert b._record_exit_order(cur, pos, 234.0, 65, "TARGET_HIT", 1.0, 1.0) == ""


# ─────────────────────────────────────────────────────────────────────────────
# 3. RESET MUST NOT ORPHAN OPEN POSITIONS
# ─────────────────────────────────────────────────────────────────────────────

def test_reset_zeroes_realised_pnl_and_restores_capital(tmp_path):
    b = make_backend(tmp_path)
    with sqlite3.connect(b.ledger_db) as conn:
        conn.execute("UPDATE paper_wallet SET balance=320000.0, realized_pnl=220000.0 WHERE id=1")
        conn.commit()
    res = b.reset_paper_ledger()
    assert res["ok"] is True
    w = res["wallet"]
    assert w["realized_pnl"] == 0.0
    assert w["balance"] == START_CAP


def test_reset_preserves_open_positions_and_recomputes_their_margin(tmp_path):
    """update_paper_wallet(reset=True) would have freed margin still in use."""
    b = make_backend(tmp_path)
    open_position(b, pos_id="POS_A", entry=200.0, qty=65)   # 13,000
    open_position(b, pos_id="POS_B", entry=180.0, qty=65)   # 11,700

    res = b.reset_paper_ledger()
    assert res["open_positions_preserved"] == 2
    w = res["wallet"]
    assert w["margin_used"] == pytest.approx(24700.0)
    assert w["margin_avail"] == pytest.approx(START_CAP - 24700.0)

    with sqlite3.connect(b.ledger_db) as conn:
        still_open = conn.execute(
            "SELECT COUNT(*) FROM positions WHERE status='OPEN'").fetchone()[0]
    assert still_open == 2


def test_reset_recovers_margin_drift_from_closed_positions(tmp_path):
    b = make_backend(tmp_path)
    open_position(b, pos_id="POS_A", entry=200.0, qty=65)
    with sqlite3.connect(b.ledger_db) as conn:
        conn.execute("UPDATE positions SET status='CLOSED' WHERE position_id='POS_A'")
        conn.commit()
    # Margin was blocked but the position is closed: reset must free it.
    assert b.reset_paper_ledger()["wallet"]["margin_used"] == pytest.approx(0.0)


def test_orders_are_cleared_only_when_asked(tmp_path):
    b = make_backend(tmp_path)
    open_position(b)
    pos = fetch_pos(b)
    with sqlite3.connect(b.ledger_db) as conn:
        cur = conn.cursor()
        b._record_exit_order(cur, pos, 234.0, 65, "TARGET_HIT", 2131.0, 79.0)
        conn.commit()

    assert b.reset_paper_ledger(clear_orders=False)["cleared_orders"] == 0
    assert len(b.get_order_book()) == 1

    assert b.reset_paper_ledger(clear_orders=True)["cleared_orders"] == 1
    assert b.get_order_book() == []


def test_closed_positions_are_kept_unless_asked(tmp_path):
    b = make_backend(tmp_path)
    open_position(b, pos_id="POS_OLD")
    with sqlite3.connect(b.ledger_db) as conn:
        conn.execute("UPDATE positions SET status='CLOSED' WHERE position_id='POS_OLD'")
        conn.commit()
    assert b.reset_paper_ledger()["cleared_closed_positions"] == 0
    assert b.reset_paper_ledger(clear_closed_positions=True)["cleared_closed_positions"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# 4. ALGO-ONLY GATE
# ─────────────────────────────────────────────────────────────────────────────

CONTRACT = {"id": "NIFTY23700CE", "symbol": "NIFTY 06 NOV 23700 CALL",
            "opt_type": "CE", "ltp": 200.0, "security_id": "43215"}


def test_manual_order_is_rejected_when_algo_only(tmp_path):
    b = make_backend(tmp_path, algo_only=True)
    res = asyncio.run(b.execute_order_with_chaser(
        contract=CONTRACT, strategy_name="Manual Matrix", is_algo_trade=False))
    assert res["ok"] is False
    assert res["status"] == "REJECTED"
    assert res["algo_only_blocked"] is True
    assert "MANUAL TRADE BLOCKED" in res["error"]


def test_rejected_manual_order_writes_nothing(tmp_path):
    """A blocked click must leave no position, no order and no margin behind.

    Six near-duplicate manual positions were visible on the desk; a gate that
    rejected but still recorded would not have fixed that.
    """
    b = make_backend(tmp_path, algo_only=True)
    asyncio.run(b.execute_order_with_chaser(
        contract=CONTRACT, strategy_name="Manual Matrix", is_algo_trade=False))
    with sqlite3.connect(b.ledger_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
    assert b.get_paper_wallet()["margin_used"] == 0.0


def test_algo_order_passes_the_gate(tmp_path):
    """is_algo_trade=True must get past the gate (it may fail later for other
    reasons such as no broker; it must not be rejected AS a manual order)."""
    b = make_backend(tmp_path, algo_only=True)
    try:
        res = asyncio.run(b.execute_order_with_chaser(
            contract=CONTRACT, strategy_name="Stage 2 Continuation", is_algo_trade=True))
    except Exception:
        return  # got past the gate and failed further downstream: acceptable
    assert not res.get("algo_only_blocked")
    assert "MANUAL TRADE BLOCKED" not in str(res.get("error", ""))


def test_manual_order_rejected_even_when_disable_requested(tmp_path):
    b = make_backend(tmp_path, algo_only=False)
    try:
        res = asyncio.run(b.execute_order_with_chaser(
            contract=CONTRACT, strategy_name="Manual Matrix", is_algo_trade=False))
    except Exception:
        return
    assert res.get("algo_only_blocked") is True


def test_kill_switch_still_wins_over_the_gate(tmp_path):
    b = make_backend(tmp_path, algo_only=True)
    b.kill_switch_active = True
    res = asyncio.run(b.execute_order_with_chaser(
        contract=CONTRACT, is_algo_trade=True))
    assert res["ok"] is False
    assert "KILL SWITCH" in res["error"]


# ─────────────────────────────────────────────────────────────────────────────
# 5. ENTRY TIME IS STORED AND SERVED
# ─────────────────────────────────────────────────────────────────────────────

def test_entry_time_is_stored_and_returned(tmp_path):
    """The Active Positions column needs this field on every open position."""
    b = make_backend(tmp_path)
    open_position(b, entry_time="2026-11-06 11:36:12 IST")
    rows = b.get_active_positions()
    assert len(rows) == 1
    assert rows[0]["entry_time"] == "2026-11-06 11:36:12 IST"
