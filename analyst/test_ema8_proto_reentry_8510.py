"""8 EMA Prototype — owner rules of 11 Nov 2026 (evening).

 1. Away distance is its own setting (10 points): 8 points away is not enough.
 2. The tick that first sees "away" is never also the touch.
 3. Re-entry: after a trade, the arm stays; the same side is taken again on the
    next away-and-touch. One trade open at a time.
 4. Re-entry needs price to leave the line again: no instant second entry.
 5. An opposite 15m close still removes the arm and arms the other side.
 6. No new entry from 15:20; the arm is kept.
 7. Old one-trade-per-cross memory saved earlier in the day does not block re-entry.
 8. Harish's 11 Nov example: PE armed by a close below, then a PE on each
    return to the line while no trade is open, and none on a candle that
    does not come back to the line.
 9. The live worker's tick range includes the 1-second spot high/low between ticks.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analyst.ema8_prototype_8510 import (  # noqa: E402
    DEFAULT_CFG,
    _arm_new,
    bar_open_timestamp,
    check_retouch,
    evaluate_ema8_prototype,
    load_ema8_prototype_cfg,
)
from analyst.test_live_exit_orders_8510 import FakeBroker, make_test_backend  # noqa: E402

DAY = "2026-11-11"


def _b15(closes: List[float], start: str = "10:00") -> pd.DataFrame:
    idx = pd.date_range(f"{DAY} {start}", periods=len(closes), freq="15min", tz="Asia/Kolkata")
    return pd.DataFrame({"open": closes, "high": [c + 2 for c in closes],
                         "low": [c - 2 for c in closes], "close": closes}, index=idx)


def _cfg(**over: Any) -> Dict[str, Any]:
    return load_ema8_prototype_cfg({"ema8_prototype": over})


def _tick(b15, state, spot, *, cfg=None, hm="11:00", hi=None, lo=None):
    return evaluate_ema8_prototype(
        b15, bar_ci=len(b15) - 1, cfg=cfg or _cfg(), proto_state=state, session_day=DAY,
        live_spot=spot, intrabar=True, tick_high=hi, tick_low=lo, now_hm=hm,
    )


def _armed(direction: str = "LONG", n: int = 10, level: float = 25000.0):
    b15 = _b15([level] * n)                       # flat: dynamic 8 EMA stays near `level`
    state: Dict[str, Any] = {}
    _arm_new(state, direction=direction, cross_bar_open_ts=bar_open_timestamp(b15, n - 2), e8=level)
    return b15, state


def test_defaults_are_the_owner_rules():
    assert DEFAULT_CFG["away_min_pts"] == 10.0
    assert DEFAULT_CFG["reentry_same_side"] is True
    assert DEFAULT_CFG["no_entry_from_hm"] == "15:20"
    assert DEFAULT_CFG["touch_tol_pts"] == 3.0


def test_eight_points_is_not_away_twelve_is():
    b15, state = _armed("LONG")
    _tick(b15, state, 25008.0)
    assert state["arm"]["away_seen"] is False
    sigs, st = _tick(b15, state, 25002.0)          # back at the line, but it never left
    assert sigs == [] and "move away" in st["block_reason"] and "10 pts" in st["block_reason"]
    _tick(b15, state, 25016.0)                     # dynamic line follows a little; still > 10 away
    assert state["arm"]["away_seen"] is True


def test_away_and_touch_cannot_be_the_same_tick():
    arm = {"direction": "LONG", "away_seen": False}
    # one tick whose high is 15 above and low is on the line
    assert check_retouch(arm, bullish=True, e8_val=25000.0, tol=3.0, spot=25001.0,
                         tick_high=25015.0, tick_low=25000.5, away_pts=10.0) is False
    assert arm["away_seen"] is True
    # the return is taken on a later tick
    assert check_retouch(arm, bullish=True, e8_val=25000.0, tol=3.0, spot=25002.0,
                         tick_high=25002.0, tick_low=25002.0, away_pts=10.0) is True
    arm_pe = {"direction": "SHORT", "away_seen": False}
    assert check_retouch(arm_pe, bullish=False, e8_val=25000.0, tol=3.0, spot=24999.0,
                         tick_high=24999.5, tick_low=24985.0, away_pts=10.0) is False
    assert check_retouch(arm_pe, bullish=False, e8_val=25000.0, tol=3.0, spot=24998.0,
                         tick_high=24998.0, tick_low=24998.0, away_pts=10.0) is True


def test_reentry_same_side_after_the_trade_closes():
    b15, state = _armed("LONG")
    _tick(b15, state, 25016.0)
    first, st = _tick(b15, state, 25002.0)
    assert [s["direction"] for s in first] == ["LONG"]
    assert st["phase"] == "ENTRY"
    assert state["arm"]["direction"] == "LONG"            # the arm stays
    assert state["arm"]["away_seen"] is False             # and must leave the line again
    assert "consumed_cross_ts" not in state and "last_traded_direction" not in state

    state["open"] = {"direction": "LONG"}                 # trade is running
    _tick(b15, state, 25020.0)
    blocked, st = _tick(b15, state, 25001.0)
    assert blocked == [] and "already open" in st["block_reason"]

    state.pop("open")                                     # trade closed (profit or loss)
    again, _ = _tick(b15, state, 25001.0)                 # at the line, but has not left it since
    assert again == []
    _tick(b15, state, 25018.0)
    second, _ = _tick(b15, state, 25002.5)
    assert [s["direction"] for s in second] == ["LONG"]   # CE taken again


def test_opposite_close_removes_the_arm_and_arms_the_other_side():
    closes = [25000.0] * 9 + [25012.0, 24985.0]           # last closed candle closes below the line
    b15 = _b15(closes + [24985.0])                        # + running candle
    state: Dict[str, Any] = {"open": {"direction": "LONG"}}
    _arm_new(state, direction="LONG", cross_bar_open_ts=bar_open_timestamp(b15, 9), e8=25001.0)
    sigs, st = evaluate_ema8_prototype(b15, bar_ci=-2, cfg=_cfg(), proto_state=state, session_day=DAY)
    acts = [a["type"] for a in st["actions"]]
    assert "SQUARE_OFF" in acts
    assert state["arm"]["direction"] == "SHORT"
    assert sigs == []


def test_no_new_entry_from_1520_and_the_arm_is_kept():
    b15, state = _armed("SHORT")
    _tick(b15, state, 24984.0, hm="15:10")
    late, st = _tick(b15, state, 24998.0, hm="15:20")
    assert late == [] and "No new entry from 15:20" in st["block_reason"]
    assert state["arm"]["direction"] == "SHORT" and state["arm"]["away_seen"] is True
    ok, _ = _tick(b15, state, 24998.0, hm="15:19")
    assert [s["direction"] for s in ok] == ["SHORT"]


def test_old_one_per_cross_memory_does_not_block_reentry():
    b15, state = _armed("LONG")
    state["consumed_cross_ts"] = state["arm"]["cross_bar_open_ts"]     # saved by the earlier rule
    state["last_traded_direction"] = "LONG"
    _tick(b15, state, 25016.0)
    sigs, _ = _tick(b15, state, 25002.0)
    assert [s["direction"] for s in sigs] == ["LONG"]


def test_legacy_one_trade_per_cross_still_available_by_config():
    cfg = _cfg(reentry_same_side=False)
    b15, state = _armed("LONG")
    _tick(b15, state, 25016.0, cfg=cfg)
    sigs, _ = _tick(b15, state, 25002.0, cfg=cfg)
    assert len(sigs) == 1
    assert state.get("arm") is None and state["last_traded_direction"] == "LONG"


def test_harish_example_pe_reentries_on_each_return_to_the_line():
    """12:00 closes below -> PE armed. PE on each return while flat. None when price stays away."""
    b15, state = _armed("SHORT", level=25300.0)
    taken: List[str] = []

    def run(label: str, path: List[float]) -> None:
        for px in path:
            sigs, _ = _tick(b15, state, px, hm=label)
            if sigs:
                taken.append(label)
                state["open"] = {"direction": "SHORT"}

    run("12:15", [25286.0, 25298.5])            # away 14, back to the line -> PE
    state.pop("open")                           # +40 booked by 12:30
    run("12:45", [25262.0, 25299.0])            # came back again -> PE again
    state.pop("open")
    run("13:00", [25270.0, 25298.0])            # -> PE again
    state.pop("open")
    run("13:15", [25265.0, 25280.0, 25272.0])   # never returns to the line -> no trade
    run("14:00", [25268.0, 25299.5])            # -> PE
    assert state["open"]                        # one trade at a time: the 14:00 trade is running
    run("14:10", [25270.0, 25299.0])            # touch while a trade is open -> nothing
    state.pop("open")
    run("14:30", [25275.0, 25298.0])            # -> PE
    assert taken == ["12:15", "12:45", "13:00", "14:00", "14:30"]


def test_worker_tick_range_includes_spot_seen_between_ticks(tmp_path: Path):
    async def _go():
        b = make_test_backend(tmp_path, FakeBroker(configured=False))
        b.cached_telemetry["spot"] = {"price": 25020.0}

        async def feed():
            for px in (25012.0, 25001.0, 25009.0):      # dips to the line and bounces inside the tick
                await asyncio.sleep(0.35)
                b.cached_telemetry["spot"] = {"price": px}

        await asyncio.gather(b._sleep_tracking_spot("ema8_proto", 2.0), feed())
        hi, lo = b._take_tick_spot_range("ema8_proto", 25009.0)
        # sampled once a second: the dip to 25001 at 0.7s is caught at the 1s sample
        assert lo == 25001.0 and hi == 25009.0
        assert b._take_tick_spot_range("ema8_proto", 25009.0) == (25009.0, 25009.0)   # used once

    asyncio.run(_go())
