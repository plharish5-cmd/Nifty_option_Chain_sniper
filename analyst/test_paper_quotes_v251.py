import asyncio
import sqlite3
from unittest.mock import AsyncMock
from analyst.paper_quote_8510 import executable_paper_quote
from analyst.test_live_exit_orders_8510 import FakeBroker, make_test_backend, insert_test_position


def test_depth_is_side_specific_and_missing_never_falls_back():
    broker = FakeBroker()
    broker.get_marketfeed_quote = AsyncMock(return_value={'123': {'bid': 245, 'ask': 246, 'last_price': 200}})
    assert asyncio.run(executable_paper_quote(broker, '123', 'BUY')) == 246
    assert asyncio.run(executable_paper_quote(broker, '123', 'SELL')) == 245
    broker.get_marketfeed_quote.return_value = {'123': {'last_price': 200}}
    assert asyncio.run(executable_paper_quote(broker, '123', 'SELL')) is None
    broker.get_marketfeed_quote.return_value = {'123': {'bid': 250, 'ask': 240}}
    assert asyncio.run(executable_paper_quote(broker, '123', 'SELL')) is None


def test_exit_ignores_old_positive_ltp_and_waits_then_uses_fresh_bid(tmp_path):
    broker = FakeBroker()
    broker.get_marketfeed_quote = AsyncMock(return_value={})
    b = make_test_backend(tmp_path, broker)
    pos = insert_test_position(b, 'PAPER_V251', mode='PAPER_ONLY')
    b.cached_telemetry = {'quotes': {str(pos['security_id']): 200}}
    b._finalize_closed_position = AsyncMock(return_value={'ok': True})
    result = asyncio.run(b.execute_position_exit(pos, 'SPOT_TARGET_HIT', spot_exit=25040))
    assert result['status'] == 'EXIT_PENDING'
    b._finalize_closed_position.assert_not_awaited()
    with sqlite3.connect(b.ledger_db) as c:
        row = c.execute('select status,exit_reason from positions where position_id=?',(pos['position_id'],)).fetchone()
    assert row == ('EXIT_PENDING','SPOT_TARGET_HIT')
    broker.get_marketfeed_quote.return_value = {str(pos['security_id']): {'bid': 270, 'ask': 271}}
    pos['status'] = 'EXIT_PENDING'
    asyncio.run(b.execute_position_exit(pos, 'SPOT_TARGET_HIT', spot_exit=25040))
    assert b._finalize_closed_position.call_args.args[1] == 270
    assert not broker.placed_orders
