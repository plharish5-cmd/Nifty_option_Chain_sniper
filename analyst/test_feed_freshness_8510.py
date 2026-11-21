"""Adversarial tests for 8600→8510 feed freshness contract (v2.6.1)."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

import pytest

from analyst.desk8600_board_8510 import _bridge_sources
from analyst.feed_freshness_8510 import (
    FEED_FRESH_OI,
    FEED_TOTAL_OI,
    FEED_WRITER_FLOW,
    build_feeds_health,
    classify_state,
    log_stale_throttled,
    strip_direction_for_contract,
)

IST = timezone(timedelta(hours=5, minutes=30))


def _now():
    return datetime(2026, 11, 21, 10, 0, 0, tzinfo=IST)


def test_fresh_feed_is_live():
    now = _now()
    ts = now - timedelta(seconds=8)
    state, age = classify_state(source_ts=ts, stale_after=30, now=now)
    assert state == "LIVE"
    assert age is not None and age <= 12


def test_aged_feed_is_stale_and_strips_direction():
    now = _now()
    ts = now - timedelta(seconds=45)
    state, age = classify_state(source_ts=ts, stale_after=30, now=now)
    assert state == "STALE"
    assert age > 30
    assert strip_direction_for_contract("STALE", "CE") is None
    assert strip_direction_for_contract("WAITING_FOR_SYNC", "PE") is None


def test_missing_sync_is_waiting_not_yesterday():
    state, age = classify_state(source_ts=None, waiting=True, stale_after=30, now=_now())
    assert state == "WAITING_FOR_SYNC"
    assert age is None


def test_bridge_sources_stale_never_direction():
    now = _now()
    ts = (now - timedelta(seconds=50)).strftime("%Y-%m-%dT%H:%M:%S+05:30")
    last = {
        "rows": [
            {
                "signal_group": "Fresh Change in OI",
                "verdict": "BULLISH",
                "direction": "CE",
                "source_ts": ts,
                "significant": True,
            }
        ]
    }
    out = _bridge_sources(last, now, max_age=420, stale_after=30)
    assert out["fresh_oi"]["direction"] is None
    assert out["fresh_oi"]["feed_state"] == "STALE"


def test_build_feeds_health_live_bridge():
    now = _now()
    ts = (now - timedelta(seconds=5)).strftime("%Y-%m-%dT%H:%M:%S+05:30")

    class FakeDhan:
        def get_pe_ce_table(self, _n):
            return {
                "available": True,
                "status": "OK",
                "rows": [{"strike": 25000}],
                "source": "DHAN_API",
                "timestamp": ts,
                "age_seconds": 5,
            }

    bridge = SimpleNamespace(
        last={
            "rows": [
                {"signal_group": "Fresh Change in OI", "verdict": "UP", "direction": "CE", "source_ts": ts},
                {"signal_group": "Cumulative Total OI", "verdict": "UP", "direction": "CE", "source_ts": ts},
                {"signal_group": "Classic Writer Flow", "verdict": "UP", "direction": "CE", "source_ts": ts},
            ]
        }
    )
    backend = SimpleNamespace(cfg={"feed_freshness": {"stale_after_seconds": 30}}, dhan_oi=FakeDhan(), desk8600_bridge=bridge)
    h = build_feeds_health(backend, now=now)
    for feed in h["feeds"]:
        assert feed["state"] == "LIVE", feed
        assert feed["age_sec"] is not None and feed["age_sec"] <= 12


def test_thousand_stale_ticks_no_log_flood():
    with mock.patch.object(logging.getLogger("feed_freshness_8510"), "info") as info:
        for _ in range(1000):
            log_stale_throttled("flood_test", "stale", interval=60.0)
        assert info.call_count <= 2


def test_stale_feed_never_raises_into_execution():
    """Direction consumers must treat STALE as unavailable (no bull/bear vote)."""
    from analyst.desk8600_board_8510 import judge, load_cfg

    now = _now()
    ts = (now - timedelta(seconds=99)).strftime("%Y-%m-%dT%H:%M:%S+05:30")
    last = {
        "rows": [
            {
                "signal_group": "Classic Writer Flow",
                "verdict": "BEARISH",
                "direction": "PE",
                "source_ts": ts,
                "significant": True,
            }
        ]
    }
    board = _bridge_sources(last, now, max_age=420, stale_after=30)
    cfg = load_cfg({"desk8600_board": {"enabled": True, "sources": {"writer_flow": "FILTER"}, "rule": "NO_OPPOSE", "neutral_blocks": False, "apply_to": ["MJ"]}})
    # STALE source: direction None — must not count as AGAINST CE
    sources = [
        {"id": "writer_flow", "label": "Writer", "origin": "8600", "mode": "FILTER", **board["writer_flow"]},
    ]
    ce = judge(sources, "CE", cfg)
    assert ce["would_block"] is False


def test_dual_tables_expose_positional_and_intraday_columns():
    from analyst.stockmojo_scraper import StockMojoScraper

    scraper = StockMojoScraper(db_path=":memory:")
    scraper.total_oi_rows = [{"strike": 25000, "put_oi_total": 1, "call_oi_total": 2}]
    scraper.oi_change_day_rows = [{"strike": 25000, "put_oi_change_day": 1, "call_oi_change_day": 2}]
    dual = scraper.get_dual_tables()
    assert dual.get("positional_columns")
    assert dual.get("intraday_columns")
    assert dual["total_oi_rows"] and dual["oi_change_day_rows"]