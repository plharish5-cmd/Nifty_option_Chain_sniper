"""Live-exit fixes on top of AGY's Phase 1 close path.

What each test pins down:
 1. A position whose real BUY never filled is closed on paper: no SELL goes to Dhan.
 2. End to end: desk armed, Dhan rejects the BUY -> the paper position is stored
    as not-live, and its exit sends nothing to Dhan.
 3. Prototype route PAPER_ONLY with the desk armed -> zero broker orders.
 4. A BUY that part-fills before its cancel is recorded and protected for the
    filled quantity only.
 5. The companion stop cannot be confirmed cancelled -> no SELL, EXIT_FAILED.
 6. Restart while an exit order is working -> that order is resumed, no second SELL.
 7. An EXIT_PENDING position with no running close is picked up by the monitor.
 8. The exit order is modified only when the price moved.
 9. At the modification allowance the order is swapped for a fresh one, and two
    exit orders are never working at once.
10. A partial exit fill followed by a dead order keeps the position open for the
    rest, and the ledger finally closes at the quantity-weighted average.
11. EXPIRED is handled as a dead order, not polled forever.

Every test uses a fake broker and a private ledger in tmp_path.
"""
from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analyst.test_live_exit_orders_8510 import (  # noqa: E402
    FakeBroker,
    insert_test_position,
    make_test_backend,
)
from analyst.test_phase2_option_points_8510 import (  # noqa: E402
    FakeBroker as EntryFakeBroker,
    make_test_backend as make_entry_backend,
)

SEC = "54321"


class WorkingBroker(FakeBroker):
    """Orders stay PENDING until the test says otherwise."""

    def __init__(self) -> None:
        super().__init__(configured=True)
        self.cancel_ok = True
        self.cancel_takes_effect = True

    async def place_order(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        self.order_counter += 1
        oid = f"DHAN_{self.order_counter}"
        record = dict(payload)
        record.update(orderId=oid, orderStatus="PENDING", filledQty=0)
        self.orders.append(record)
        self.placed_orders.append(record)
        return {"ok": True, "order_id": oid}

    async def cancel_order(self, order_id: str) -> Dict[str, Any]:
        self.cancelled_orders.append(str(order_id))
        if not self.cancel_ok:
            return {"ok": False, "error": "DH-906 cancel rejected"}
        if self.cancel_takes_effect:
            for o in self.orders:
                if str(o.get("orderId")) == str(order_id) and o["orderStatus"] in ("PENDING", "TRANSIT", "PART_TRADED"):
                    o["orderStatus"] = "CANCELLED"
        return {"ok": True, "order_id": order_id}

    def limit_sells(self) -> List[Dict[str, Any]]:
        return [o for o in self.placed_orders
                if o.get("transactionType") == "SELL" and o.get("orderType") == "LIMIT"]

    def working(self) -> List[Dict[str, Any]]:
        return [o for o in self.limit_sells() if o["orderStatus"] in ("PENDING", "TRANSIT", "PART_TRADED")]

    def fill(self, order: Dict[str, Any], price: float, qty: int | None = None) -> None:
        order.update(orderStatus="TRADED", averageTradedPrice=price,
                     filledQty=int(qty if qty is not None else order["quantity"]))


def _row(b, pos_id: str) -> Dict[str, Any]:
    with sqlite3.connect(b.ledger_db) as conn:
        conn.row_factory = sqlite3.Row
        return dict(conn.execute("SELECT * FROM positions WHERE position_id=?", (pos_id,)).fetchone())


def _set(b, pos_id: str, **cols: Any) -> None:
    sets = ", ".join(f"{k}=?" for k in cols)
    with sqlite3.connect(b.ledger_db) as conn:
        conn.execute(f"UPDATE positions SET {sets} WHERE position_id=?", (*cols.values(), pos_id))


def _stop(broker: FakeBroker, oid: str = "SL_INIT_101", status: str = "TRANSIT") -> Dict[str, Any]:
    o = {"orderId": oid, "orderStatus": status, "transactionType": "SELL",
         "orderType": "STOP_LOSS_LIMIT", "quantity": 65, "filledQty": 0}
    broker.orders.append(o)
    return o


async def _until(cond, timeout: float = 3.0) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not cond():
        if loop.time() > end:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.005)


# 1 ───────────────────────────────────────────────────────────────────────────
def test_position_without_real_buy_sends_no_sell(tmp_path: Path):
    async def _run():
        broker = FakeBroker(configured=True)
        b = make_test_backend(tmp_path, broker)
        b.cached_telemetry["quotes"][SEC] = 215.0
        pos = insert_test_position(b, "POS_PAPER_IN_BOTH", mode="BOTH", sl_order_id="")
        _set(b, "POS_PAPER_IN_BOTH", live_filled=0, live_fill_qty=0)

        res = await b._close_position_with_broker(pos, "SPOT_TARGET_HIT", spot_exit=25041.0)

        assert res["ok"] is True
        assert broker.placed_orders == []
        assert broker.cancelled_orders == []
        row = _row(b, "POS_PAPER_IN_BOTH")
        assert row["status"] == "CLOSED"
        assert row["exit_price"] == pytest.approx(215.0)

    asyncio.run(_run())


# 2, 3, 4 share the entry call ─────────────────────────────────────────────────
async def _enter(b, **overrides: Any) -> Dict[str, Any]:
    kwargs: Dict[str, Any] = dict(
        contract={"symbol": "NIFTY 25000 CE", "security_id": SEC, "lot_size": 65,
                  "ltp": 250.00, "delta": 0.55},
        side="BUY",
        strategy_name="8 EMA Prototype (Long)",
        strategy_id="EMA8_PROTOTYPE_LONG",
        target_pts=40.0,
        sl_pts=40.0,
        is_algo_trade=True,
        spot_context={"strategy_id": "EMA8_PROTOTYPE_LONG", "spot_entry": 25000.0,
                      "spot_target_level": 25040.0, "spot_stop_level": 24960.0,
                      "spot_target_pts": 40.0, "spot_stop_pts": 40.0},
        enforce_market_hours=False,
    )
    kwargs.update(overrides)
    return await b.execute_order_with_chaser(**kwargs)


def _only_position(b) -> Dict[str, Any]:
    with sqlite3.connect(b.ledger_db) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM positions").fetchall()
    assert len(rows) == 1
    return dict(rows[0])


def test_rejected_live_buy_is_stored_not_live_and_exit_sends_nothing(tmp_path: Path):
    class RejectBuy(EntryFakeBroker):
        async def place_order(self, payload):
            if payload.get("transactionType") == "BUY":
                return {"ok": False, "error": "RMS: insufficient funds"}
            return await super().place_order(payload)

    async def _run():
        broker = RejectBuy(configured=True)
        b = make_entry_backend(tmp_path, broker)
        if b.execution_mode != "BOTH":
            pytest.skip("fixture desk is not armed")
        await _enter(b)

        pos = _only_position(b)           # the parallel paper leg
        assert pos["mode"] == "BOTH"
        assert pos["live_filled"] == 0
        assert (pos["sl_order_id"] or "") == ""

        b.cached_telemetry["quotes"][SEC] = 291.0
        res = await b._close_position_with_broker(pos, "OPTION_TARGET_HIT", spot_exit=25030.0)
        assert res["ok"] is True
        assert broker.placed_orders == []          # nothing was ever sent to sell
        assert _only_position(b)["status"] == "CLOSED"

    asyncio.run(_run())


def test_filled_live_buy_is_stored_live(tmp_path: Path):
    async def _run():
        broker = EntryFakeBroker(configured=True)
        b = make_entry_backend(tmp_path, broker)
        if b.execution_mode != "BOTH":
            pytest.skip("fixture desk is not armed")
        await _enter(b)
        pos = _only_position(b)
        assert pos["live_filled"] == 1
        assert pos["live_fill_qty"] == 65
        assert pos["live_order_id"]

    asyncio.run(_run())


def test_prototype_paper_only_route_sends_no_broker_orders(tmp_path: Path):
    async def _run():
        broker = EntryFakeBroker(configured=True)
        b = make_entry_backend(tmp_path, broker)
        if b.execution_mode != "BOTH":
            pytest.skip("fixture desk is not armed")
        b.scanner_cfg = {"ema8_prototype": {"execution_mode": "PAPER_ONLY"}}

        res = await _enter(b)

        assert broker.placed_orders == []
        assert "PAPER_ONLY" in res.get("live_note", "")
        pos = _only_position(b)
        assert pos["mode"] == "PAPER_ONLY"
        assert pos["live_filled"] == 0

    asyncio.run(_run())


def test_other_strategy_route_is_untouched_by_prototype_switch(tmp_path: Path):
    async def _run():
        broker = EntryFakeBroker(configured=True)
        b = make_entry_backend(tmp_path, broker)
        if b.execution_mode != "BOTH":
            pytest.skip("fixture desk is not armed")
        b.scanner_cfg = {"ema8_prototype": {"execution_mode": "PAPER_ONLY"}}

        await _enter(b, strategy_name="Stage 2 8 EMA Touch", strategy_id="STAGE_2_LONG",
                     spot_context=None)

        assert [o["transactionType"] for o in broker.placed_orders][:1] == ["BUY"]

    asyncio.run(_run())


def test_partial_live_buy_is_recorded_and_protected_for_filled_qty(tmp_path: Path):
    class PartialBuy(EntryFakeBroker):
        async def place_order(self, payload):
            res = await super().place_order(payload)
            if payload.get("transactionType") == "BUY":
                self.orders[-1].update(orderStatus="PART_TRADED", filledQty=25,
                                       averageTradedPrice=250.0)
            return res

    async def _run():
        broker = PartialBuy(configured=True)
        b = make_entry_backend(tmp_path, broker)
        if b.execution_mode != "BOTH":
            pytest.skip("fixture desk is not armed")
        b.chasing_cfg["max_chase_retries"] = 1
        await _enter(b)

        pos = _only_position(b)
        assert pos["live_filled"] == 1
        assert pos["live_fill_qty"] == 25
        stops = [o for o in broker.placed_orders if o.get("orderType") == "STOP_LOSS_LIMIT"]
        assert len(stops) == 1 and stops[0]["quantity"] == 25

    asyncio.run(_run())


# 5 ───────────────────────────────────────────────────────────────────────────
def test_stop_not_confirmed_cancelled_means_no_sell(tmp_path: Path):
    async def _run():
        broker = WorkingBroker()
        broker.cancel_takes_effect = False          # Dhan says ok, stop stays live
        _stop(broker)
        broker.quotes[SEC] = 230.0
        b = make_test_backend(tmp_path, broker)
        pos = insert_test_position(b, "POS_STOP_STUCK", mode="LIVE", sl_order_id="SL_INIT_101")

        res = await b._close_position_with_broker(pos, "SPOT_TARGET_HIT", spot_exit=25045.0)

        assert res["status"] == "EXIT_FAILED"
        assert broker.limit_sells() == []
        row = _row(b, "POS_STOP_STUCK")
        assert row["status"] == "EXIT_FAILED"
        assert row["sl_order_id"] == "SL_INIT_101"          # stop still tracked
        assert b.exit_failure_banner["position_id"] == "POS_STOP_STUCK"

    asyncio.run(_run())


def test_stop_cancel_rejected_and_book_unreadable_means_no_sell(tmp_path: Path):
    async def _run():
        broker = WorkingBroker()
        broker.cancel_ok = False                    # and the stop is not in the book
        broker.quotes[SEC] = 230.0
        b = make_test_backend(tmp_path, broker)
        pos = insert_test_position(b, "POS_STOP_UNKNOWN", mode="LIVE", sl_order_id="SL_GONE")

        res = await b._close_position_with_broker(pos, "SPOT_SL_HIT", spot_exit=24955.0)

        assert res["status"] == "EXIT_FAILED"
        assert broker.limit_sells() == []

    asyncio.run(_run())


# 6 ───────────────────────────────────────────────────────────────────────────
def test_restart_resumes_the_working_exit_order(tmp_path: Path):
    async def _run():
        broker = WorkingBroker()
        broker.quotes[SEC] = 230.0
        b = make_test_backend(tmp_path, broker)
        insert_test_position(b, "POS_RESUME", mode="LIVE", sl_order_id="", status="EXIT_PENDING")
        # State left behind by the run that died: sell already working at Dhan.
        prior = {"orderId": "DHAN_PRIOR", "orderStatus": "PENDING", "transactionType": "SELL",
                 "orderType": "LIMIT", "quantity": 65, "filledQty": 0, "price": 228.0}
        broker.orders.append(prior)
        _set(b, "POS_RESUME", exit_order_id="DHAN_PRIOR", exit_reason="SPOT_TARGET_HIT")

        task = asyncio.create_task(
            b._close_position_with_broker(_row(b, "POS_RESUME"), "SPOT_TARGET_HIT", spot_exit=25044.0)
        )
        await asyncio.sleep(0.08)
        assert broker.limit_sells() == []           # no second sell was sent
        prior.update(orderStatus="TRADED", averageTradedPrice=228.5, filledQty=65)
        res = await asyncio.wait_for(task, 3)

        assert res["ok"] is True
        assert broker.limit_sells() == []
        row = _row(b, "POS_RESUME")
        assert row["status"] == "CLOSED"
        assert row["exit_price"] == pytest.approx(228.5)
        assert row["exit_reason"] == "SPOT_TARGET_HIT"

    asyncio.run(_run())


# 7 ───────────────────────────────────────────────────────────────────────────
def test_monitor_picks_up_orphaned_exit_pending(tmp_path: Path):
    async def _run():
        broker = FakeBroker(configured=True)        # LIMIT orders fill at once
        broker.quotes[SEC] = 230.0
        b = make_test_backend(tmp_path, broker)
        insert_test_position(b, "POS_ORPHAN", mode="LIVE", sl_order_id="", status="EXIT_PENDING")
        _set(b, "POS_ORPHAN", exit_reason="OPPOSITE_8EMA_CLOSE")

        b.get_active_positions({SEC: 230.0})        # one monitor pass
        await _until(lambda: _row(b, "POS_ORPHAN")["status"] == "CLOSED")

        row = _row(b, "POS_ORPHAN")
        assert row["exit_reason"] == "OPPOSITE_8EMA_CLOSE"
        sells = [o for o in broker.placed_orders if o.get("transactionType") == "SELL"]
        assert len(sells) == 1

    asyncio.run(_run())


def test_monitor_does_not_stack_closes_for_one_position(tmp_path: Path):
    async def _run():
        broker = WorkingBroker()
        broker.quotes[SEC] = 230.0
        b = make_test_backend(tmp_path, broker)
        insert_test_position(b, "POS_ONE", mode="LIVE", sl_order_id="", status="EXIT_PENDING")

        for _ in range(5):                          # five monitor passes
            b.get_active_positions({SEC: 230.0})
            await asyncio.sleep(0.02)
        assert len(b._close_tasks) == 1
        assert len(broker.limit_sells()) == 1

        broker.fill(broker.limit_sells()[0], 228.0)
        await _until(lambda: _row(b, "POS_ONE")["status"] == "CLOSED")
        assert len(broker.limit_sells()) == 1

    asyncio.run(_run())


# 8 ───────────────────────────────────────────────────────────────────────────
def test_no_modify_while_price_is_unchanged(tmp_path: Path):
    async def _run():
        broker = WorkingBroker()
        broker.quotes[SEC] = 230.0
        b = make_test_backend(tmp_path, broker)
        pos = insert_test_position(b, "POS_FLAT", mode="LIVE", sl_order_id="")

        task = asyncio.create_task(b._close_position_with_broker(pos, "SPOT_TARGET_HIT", 25045.0))
        await _until(lambda: len(broker.limit_sells()) == 1)
        await asyncio.sleep(0.15)                   # many polls at the same price
        assert broker.modified_orders == []

        broker.quotes[SEC] = 226.0                  # price moves once
        await _until(lambda: len(broker.modified_orders) == 1)
        await asyncio.sleep(0.1)
        assert len(broker.modified_orders) == 1
        assert broker.modified_orders[0]["payload"]["price"] == pytest.approx(224.0)

        broker.fill(broker.limit_sells()[0], 224.0)
        assert (await asyncio.wait_for(task, 3))["ok"] is True

    asyncio.run(_run())


# 9 ───────────────────────────────────────────────────────────────────────────
def test_order_is_swapped_at_the_modification_allowance(tmp_path: Path):
    async def _run():
        broker = WorkingBroker()
        broker.quotes[SEC] = 230.0
        b = make_test_backend(tmp_path, broker)
        b.cfg["chasing"]["exit_max_modifications_per_order"] = 2
        pos = insert_test_position(b, "POS_SWAP", mode="LIVE", sl_order_id="")

        task = asyncio.create_task(b._close_position_with_broker(pos, "SPOT_SL_HIT", 24950.0))
        await _until(lambda: len(broker.limit_sells()) == 1)
        first = broker.limit_sells()[0]
        price = 230.0
        max_working = 1
        while len(broker.limit_sells()) < 2:        # keep the price falling
            price -= 1.0
            broker.quotes[SEC] = price
            await asyncio.sleep(0.03)
            max_working = max(max_working, len(broker.working()))
            assert price > 150, "order was never swapped"

        assert first["orderStatus"] == "CANCELLED"
        assert len([m for m in broker.modified_orders if m["order_id"] == first["orderId"]]) == 2
        assert max_working == 1                     # never two exits working together

        second = broker.limit_sells()[1]
        broker.fill(second, 200.0)
        assert (await asyncio.wait_for(task, 3))["ok"] is True
        assert _row(b, "POS_SWAP")["exit_price"] == pytest.approx(200.0)

    asyncio.run(_run())


# 10 ──────────────────────────────────────────────────────────────────────────
def test_partial_exit_then_dead_order_then_rest(tmp_path: Path):
    async def _run():
        broker = WorkingBroker()
        broker.quotes[SEC] = 230.0
        b = make_test_backend(tmp_path, broker)
        pos = insert_test_position(b, "POS_PART", mode="LIVE", sl_order_id="")

        task = asyncio.create_task(b._close_position_with_broker(pos, "SPOT_TARGET_HIT", 25045.0))
        await _until(lambda: len(broker.limit_sells()) == 1)
        broker.limit_sells()[0].update(orderStatus="CANCELLED", filledQty=25,
                                       averageTradedPrice=228.0)      # 25 sold, order killed
        res = await asyncio.wait_for(task, 3)

        assert res["status"] == "EXIT_FAILED"
        row = _row(b, "POS_PART")
        assert row["status"] == "EXIT_FAILED"                         # 40 still held
        assert row["exit_filled_qty"] == 25
        stops = [o for o in broker.placed_orders if o.get("orderType") == "STOP_LOSS_LIMIT"]
        assert len(stops) == 1 and stops[0]["quantity"] == 40         # stop for the rest only

        # The monitor's retry: sell the remaining 40.
        broker.quotes[SEC] = 222.0
        task = asyncio.create_task(
            b._close_position_with_broker(_row(b, "POS_PART"), "SPOT_TARGET_HIT", 25045.0)
        )
        await _until(lambda: len(broker.limit_sells()) == 2)
        second = broker.limit_sells()[1]
        assert second["quantity"] == 40
        broker.fill(second, 220.0)
        assert (await asyncio.wait_for(task, 3))["ok"] is True

        row = _row(b, "POS_PART")
        assert row["status"] == "CLOSED"
        assert row["exit_price"] == pytest.approx((25 * 228.0 + 40 * 220.0) / 65, abs=0.01)

    asyncio.run(_run())


# 11 ──────────────────────────────────────────────────────────────────────────
def test_expired_exit_order_is_not_polled_forever(tmp_path: Path):
    async def _run():
        broker = WorkingBroker()
        broker.quotes[SEC] = 230.0
        b = make_test_backend(tmp_path, broker)
        pos = insert_test_position(b, "POS_EXP", mode="LIVE", sl_order_id="")

        task = asyncio.create_task(b._close_position_with_broker(pos, "SPOT_TARGET_HIT", 25045.0))
        await _until(lambda: len(broker.limit_sells()) == 1)
        broker.limit_sells()[0]["orderStatus"] = "EXPIRED"
        res = await asyncio.wait_for(task, 3)

        assert res["status"] == "EXIT_FAILED"
        assert _row(b, "POS_EXP")["status"] == "EXIT_FAILED"

    asyncio.run(_run())
