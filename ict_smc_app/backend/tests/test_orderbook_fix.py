"""
test_orderbook_fix.py — Unit tests for L2 Order Book Engine fixes, Top-5 Depth Balance,
Wall detection (>=15 lots), continuous sparkline, traded volumes, and persistence.
"""
import json
import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = BACKEND_DIR.parent
sys.path.insert(0, str(BACKEND_DIR))
sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("ICT_DATA_DIR", str(BACKEND_DIR / "tests" / ".data"))

import pytest
from app import db, config
from app.engine.orderbook import OrderBook, get_book_cfg


def test_top5_depth_balance_mathematics():
    """Validates that Top-5 Depth Balance is computed directly from 5 bid/ask levels."""
    ob = OrderBook()
    # Mock ladder matching image_f535fd.png:
    # Bid: 22 lots (1430), 1 lot (65), 2 lots (130), 17 lots (1105), 9 lots (585) = 3315 qty = 51 lots
    # Ask: 3 lots (195), 1 lot (65), 1 lot (65), 2 lots (130), 1 lot (65) = 520 qty = 8 lots
    ob.ladder = [
        (22775.30, 1430, 1, 22783.80, 195, 1),
        (22775.20, 65, 1, 22785.00, 65, 1),
        (22775.10, 130, 2, 22785.10, 65, 1),
        (22775.00, 1105, 9, 22785.20, 130, 2),
        (22774.20, 585, 1, 22785.30, 65, 1),
    ]
    ob.tot_buy = 256880
    ob.tot_sell = 212940

    v = ob.view()
    bal = v["balance"]

    assert bal["top5_buy_pct"] == 86.4
    assert bal["buy_pct"] == 86.4
    assert bal["top5_sell_pct"] == 13.6
    assert bal["top5_bid_lots"] == 51.0
    assert bal["top5_ask_lots"] == 8.0
    assert bal["imbalance_ratio"] == 6.4
    assert "BUYERS STACKED (+6.4x Bid Wall)" in bal["verdict"]
    assert bal["exchange_buy_pct"] == 54.7


def test_wall_detection_on_15_plus_lots():
    """Validates that any row with resting lots >= 15 is flagged as a WALL."""
    ob = OrderBook()
    ob.ladder = [
        (22775.30, 1430, 1, 22783.80, 195, 1), # 22 lots -> WALL
        (22775.20, 65, 1, 22785.00, 65, 1),    # 1 lot
        (22775.10, 130, 2, 22785.10, 65, 1),   # 2 lots
        (22775.00, 1105, 9, 22785.20, 130, 2), # 17 lots -> WALL
        (22774.20, 585, 1, 22785.30, 65, 1),   # 9 lots
    ]

    v = ob.view()
    rows = v["ladder"]

    # 22775.30 (22 lots) must be marked as wall
    assert rows[0]["bid_lots"] == 22.0
    assert rows[0]["bid_is_wall"] is True
    assert "HEAVY BID STACK" in rows[0]["guidance_bid"]

    # 22775.20 (1 lot) not wall
    assert rows[1]["bid_is_wall"] is False

    # 22775.00 (17 lots) must be marked as wall
    assert rows[3]["bid_lots"] == 17.0
    assert rows[3]["bid_is_wall"] is True
    assert "HEAVY BID STACK" in rows[3]["guidance_bid"]

    # Ask 3 lots not wall
    assert rows[0]["ask_is_wall"] is False


def test_traded_volumes_and_sparkline_backfill():
    """Validates that traded volume and 60-minute sparkline series are never blank."""
    ob = OrderBook()
    ob.backfill_from_session()
    v = ob.view()

    # Series must have continuous points
    assert "series" in v
    assert len(v["series"]) >= 15
    for pt in v["series"]:
        assert "ts" in pt
        assert "bp" in pt
        assert 0.0 <= pt["bp"] <= 100.0

    # Traded B and S must have numbers
    for row in v["ladder"]:
        assert "bid_traded" in row
        assert "ask_traded" in row
        assert "bid_traded_lots" in row
        assert "ask_traded_lots" in row
        # Must not be zero tuple
        assert len(row["bid_traded"]) == 2
        assert len(row["ask_traded"]) == 2

    # Events must have rich entries (not empty)
    assert "events" in v
    assert len(v["events"]) > 0
    e0 = v["events"][0]
    assert "kind" in e0
    assert "price" in e0
    assert "why" in e0


def test_orderbook_state_persistence(tmp_path):
    """Validates that orderbook state persists to JSON and reloads intact."""
    ob = OrderBook()
    ob.ladder = [(22775.30, 1430, 1, 22783.80, 195, 1)]
    ob.tot_buy = 256880
    ob.tot_sell = 212940
    ob.save_state()

    cache_file = ob._cache_path()
    assert cache_file.exists()

    # Load fresh book
    ob2 = OrderBook()
    assert ob2.tot_buy == 256880
    assert ob2.tot_sell == 212940
    assert len(ob2.ladder) >= 1
    assert ob2.ladder[0][0] == 22775.30
