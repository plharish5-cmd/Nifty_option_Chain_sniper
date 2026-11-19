"""Per-lane live routing."""
from __future__ import annotations

from analyst.lane_live_routing_8510 import cockpit_route_tuple, order_will_be_paper


class _Be:
    execution_mode = "PAPER_ONLY"
    _real_money_enabled = False
    kill_switch_active = False
    scanner_cfg = {
        "ema8_prototype": {"execution_mode": "LIVE_ALLOWED", "live_trade_enabled": True},
    }


def test_prototype_paper_until_desk_armed():
    assert order_will_be_paper(_Be(), "EMA8_PROTOTYPE_LONG") is True


def test_prototype_live_when_armed_and_opt_in():
    b = _Be()
    b.execution_mode = "BOTH"
    b._real_money_enabled = True
    assert order_will_be_paper(b, "EMA8_PROTOTYPE_LONG") is False


def test_cockpit_label_opt_in_missing():
    b = _Be()
    b.scanner_cfg["ema8_prototype"]["live_trade_enabled"] = False
    paper, cap, label = cockpit_route_tuple(b, "prototype")
    assert paper is True
    assert "Real money for this lane" in label