"""v2.0.47 — real-order preflight readiness aggregation (places nothing)."""
from __future__ import annotations

from analyst.real_order_preflight_8510 import build_order_preflight

GOOD = {
    "dhan_connected": True, "token_configured": True, "is_expired": False,
    "ip_matched": True, "current_ip": "49.205.39.195", "whitelisted_ip": "49.205.39.195",
    "token_valid_till": "21 Nov 2026", "days_left": "1 Days Left", "last_status": "CONNECTED",
}


def test_all_green_and_live_armed_is_ready_real():
    r = build_order_preflight(GOOD, funds_balance=25047.0, est_order_cost=16335.0,
                              desk_live_armed=True, lane_live=True)
    assert r["ready_to_place"] is True and r["would_be_real"] is True
    assert r["verdict"] == "READY_REAL"
    assert r["blockers"] == []


def test_all_green_but_paper_desk_is_ready_paper():
    r = build_order_preflight(GOOD, 25047.0, 16335.0, desk_live_armed=False)
    assert r["ready_to_place"] is True and r["would_be_real"] is False
    assert r["verdict"] == "READY_PAPER"


def test_ip_mismatch_blocks_with_dh905_hint():
    cfg = dict(GOOD, ip_matched=False, current_ip="1.2.3.4")
    r = build_order_preflight(cfg, 25047.0, 16335.0, desk_live_armed=True)
    assert r["ready_to_place"] is False and r["verdict"] == "NOT_READY"
    ip_check = next(c for c in r["checks"] if c["key"] == "ip")
    assert "DH-905" in ip_check["detail"]
    assert "Machine IP allowlisted" in r["blockers"]


def test_expired_token_blocks():
    cfg = dict(GOOD, is_expired=True)
    r = build_order_preflight(cfg, 25047.0, 16335.0)
    assert not r["ready_to_place"]
    assert "Access token valid" in r["blockers"]


def test_insufficient_funds_blocks():
    r = build_order_preflight(GOOD, funds_balance=5000.0, est_order_cost=16335.0, desk_live_armed=True)
    assert not r["ready_to_place"]
    assert "Sufficient funds" in r["blockers"]


def test_zero_funds_reads_as_feed_problem():
    r = build_order_preflight(GOOD, funds_balance=0.0, est_order_cost=16335.0)
    funds_check = next(c for c in r["checks"] if c["key"] == "funds")
    assert "funds read" in funds_check["detail"].lower() or "0" in funds_check["detail"]


def test_garbage_inputs_never_raise():
    r = build_order_preflight(None, None, None)
    assert r["verdict"] == "NOT_READY"
    assert isinstance(r["checks"], list)


def test_kill_switch_blocks_ready(  ):
    r = build_order_preflight(GOOD, 25047.0, 16335.0, desk_live_armed=True,
                              lane_live=True, kill_switch_active=True)
    assert r["ready_to_place"] is False
    assert "Kill switch off" in r["blockers"]


def test_summary_does_not_promise_broker_acceptance():
    r = build_order_preflight(GOOD, 25047.0, 16335.0, desk_live_armed=True, lane_live=True)
    # must not claim the broker WILL accept; only that local checks pass
    assert "ACCEPTED and routed" not in r["summary"]
    assert "likely" in r["summary"].lower() or "local checks" in r["summary"].lower()
