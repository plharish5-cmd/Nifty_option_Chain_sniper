"""v2.0.46 — paper exit must book a FRESH quote, never the stale stored LTP (the -90/-84 fix)."""
from __future__ import annotations

from analyst.terminal8510_backend import select_paper_exit_ltp


def test_prefers_fresh_broker_quote():
    assert select_paper_exit_ltp(282.5, 215.55) == 282.5


def test_falls_back_to_live_cache_when_no_fresh():
    assert select_paper_exit_ltp(0.0, 281.0) == 281.0


def test_pends_when_neither_available():
    # 0.0 signals the caller to leave the position pending — no invented P&L.
    assert select_paper_exit_ltp(0.0, 0.0) == 0.0
    assert select_paper_exit_ltp(None, None) == 0.0


def test_ignores_negative_or_garbage():
    assert select_paper_exit_ltp(-1.0, 0.0) == 0.0
    assert select_paper_exit_ltp("x", "y") == 0.0
    assert select_paper_exit_ltp("x", 200.0) == 200.0


def test_does_not_use_stored_row_ltp_by_contract():
    # The function only ever sees fresh + cache; the stale stored row LTP is never passed in,
    # which is the whole point of the fix. A dead feed (both zero) pends rather than booking stale.
    assert select_paper_exit_ltp(0, 0) == 0.0
