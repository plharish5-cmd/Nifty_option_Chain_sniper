"""
gift_nifty_live.py — Direct bridge interface for GIFT Nifty live feed.
Re-exports DhanFeedBridge and get_gift_nifty_bridge from gift_nifty_feed.
"""
from __future__ import annotations

from .gift_nifty_feed import DhanFeedBridge, get_gift_nifty_bridge, now_ist_iso

__all__ = ["DhanFeedBridge", "get_gift_nifty_bridge", "now_ist_iso"]
