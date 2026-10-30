"""
test_gift_nifty_feed.py — Unit tests for Live Dhan API Feed Connector for GIFT Nifty.
Validates:
1. DhanFeedBridge configuration and initialization from rules.json.
2. Ingestion of live ticks (Code 2 LTP, Code 6 Prev Close).
3. Exact JSON payload schema matching GET /api/desk/gift-nifty-live.
4. Redundant fallback handling and stale detection.
5. Integration with FastAPI test client.
6. Elimination of epoch timestamp leakage in gap_sniper.
"""
import os
import sys
from pathlib import Path
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("ICT_DATA_DIR", str(Path(__file__).resolve().parent / ".data"))

from app import config
from app.api.main import app
from app.engine.gift_nifty_feed import DhanFeedBridge, get_gift_nifty_bridge, now_ist_iso
from app.engine import gap_sniper


def test_dhan_feed_bridge_init():
    """Validates default initialization according to rules.json."""
    bridge = DhanFeedBridge()
    assert bridge.symbol == "GIFT Nifty"
    assert bridge.security_id == "5024"
    assert bridge.exchange_segment == "IDX_I"
    assert bridge.fallback_segment == "NSE_IX"
    assert bridge.poll_interval_s == 3.0
    assert bridge.stale_threshold_s == 10.0
    assert bridge.ltp > 10000.0
    assert bridge.prev_close > 10000.0


def test_dhan_feed_bridge_tick_updates():
    """Validates real-time tick ingestion and change calculations."""
    bridge = DhanFeedBridge()
    # Set prev close first
    bridge.update_from_prev("5024", 22771.20)
    assert bridge.prev_close == 22771.20

    # Simulate live tick at 22,975.40 (+204.2 pts)
    bridge.update_from_feed("5024", 22975.40, 1793326500.0)
    assert bridge.ltp == 22975.40
    assert bridge.change == pytest.approx(204.2, abs=0.05)
    assert bridge.change_pct == pytest.approx(0.90, abs=0.05)
    assert bridge.source == "dhan_feed"
    assert bridge.status == "live_feed"
    assert bridge.is_stale() is False

    # Check payload structure
    payload = bridge.get_quote()
    assert payload["ok"] is True
    assert payload["symbol"] == "GIFT Nifty"
    assert payload["ltp"] == 22975.40
    assert payload["change"] == pytest.approx(204.2, abs=0.05)
    assert payload["change_pct"] == pytest.approx(0.90, abs=0.05)
    assert "T" in payload["timestamp"] and "+05:30" in payload["timestamp"]


def test_dhan_feed_bridge_ignores_abnormal_ticks():
    """Ensures abnormal ticks (like epoch timestamps > 100000 or 0) are rejected."""
    bridge = DhanFeedBridge()
    original_ltp = bridge.ltp

    # Try feeding epoch timestamp
    bridge.update_from_feed("5024", 1793326233.0, 1793326233.0)
    assert bridge.ltp == original_ltp  # Ignored

    # Try feeding 0
    bridge.update_from_feed("5024", 0.0, 1793326233.0)
    assert bridge.ltp == original_ltp  # Ignored

    # Try feeding wrong SID
    bridge.update_from_feed("13", 22950.0, 1793326233.0)
    assert bridge.ltp == original_ltp  # Ignored


def test_fastapi_gift_nifty_live_endpoint():
    """Validates GET /api/desk/gift-nifty-live endpoint contract."""
    client = TestClient(app)
    resp = client.get("/api/desk/gift-nifty-live")
    assert resp.status_code == 200
    data = resp.json()

    assert data["ok"] is True
    assert data["symbol"] == "GIFT Nifty"
    assert isinstance(data["ltp"], (int, float))
    assert 10000.0 < data["ltp"] < 50000.0
    assert isinstance(data["change"], (int, float))
    assert isinstance(data["change_pct"], (int, float))
    assert isinstance(data["timestamp"], str)
    assert "+05:30" in data["timestamp"]


def test_gap_sniper_uses_clean_gift_nifty_price():
    """Validates gap_sniper does NOT use an epoch timestamp as GIFT Nifty LTP."""
    class MockFeed:
        last_tick = {"5024": 1793326233.0}  # simulated timestamp in last_tick

    class MockHub:
        feed = MockFeed()
        candles = None

    res = gap_sniper.compute_gap_confluence(hub=MockHub())
    assert res["ok"] is True
    # gift_nifty_ltp must be an actual price around 22,000-24,000, NOT 1,793,326,233!
    assert 10000.0 < res["gift_nifty_ltp"] < 50000.0
    assert res["gift_nifty_ltp"] != pytest.approx(1793326233.0, abs=100.0)
