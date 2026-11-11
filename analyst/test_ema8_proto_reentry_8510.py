"""8 EMA Prototype — owner rules of 11 Nov 2026 (evening).

 0. Final answers: away distance 2 points; no entry before 09:30; re-entry
    after a PROFIT exit only — a stop-loss cancels that side until the
    opposite side has been armed. OI-based CE/PE choice is a placeholder.
 1. Away distance is its own setting: with 10 configured, 8 away is not enough.
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
    assert DEFAULT_CFG["away_min_pts"] == 2.0                 # "GO AHEAD 2"
    assert DEFAULT_CFG["reentry_same_side"] is True
    assert DEFAULT_CFG["reentry_after_stop_loss"] is False
    assert DEFAULT_CFG["no_entry_before_hm"] == "09:30"
    assert DEFAULT_CFG["oi_direction_filter_enabled"] is False
    assert DEFAULT_CFG["no_entry_from_hm"] == "15:20"
    assert DEFAULT_CFG["touch_tol_pts"] == 3.0


def test_eight_points_is_not_away_twelve_is():
    cfg = _cfg(away_min_pts=10.0)
    b15, state = _armed("LONG")
    _tick(b15, state, 25008.0, cfg=cfg)
    assert state["arm"]["away_seen"] is False
    sigs, st = _tick(b15, state, 25002.0, cfg=cfg)  # back at the line, but it never left
    assert sigs == [] and "move away" in st["block_reason"] and "10 pts" in st["block_reason"]
    _tick(b15, state, 25016.0, cfg=cfg)             # dynamic line follows a little; still > 10 away
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


# ── final answers of 11 Nov ──────────────────────────────────────────────────
from analyst.ema8_prototype_8510 import (  # noqa: E402
    _maybe_flip_arm,
    is_stop_loss_exit,
    note_prototype_exit,
)


def test_default_away_is_two_points():
    b15, state = _armed("LONG")
    _tick(b15, state, 25001.5)                     # 1.5 above: not away
    assert state["arm"]["away_seen"] is False
    _tick(b15, state, 25004.0)                     # more than 2 above the moving line: away
    assert state["arm"]["away_seen"] is True
    sigs, _ = _tick(b15, state, 25002.0)           # back within 3 on a later tick
    assert [s["direction"] for s in sigs] == ["LONG"]


def test_no_entry_before_0930_then_allowed():
    b15, state = _armed("SHORT")
    _tick(b15, state, 24990.0, hm="09:20")
    early, st = _tick(b15, state, 24999.0, hm="09:29")
    assert early == [] and "No entry before 09:30" in st["block_reason"]
    assert state["arm"]["direction"] == "SHORT" and state["arm"]["away_seen"] is True
    ok, _ = _tick(b15, state, 24999.0, hm="09:30")
    assert [s["direction"] for s in ok] == ["SHORT"]


@pytest.mark.parametrize("reason,is_sl", [
    ("SPOT_TARGET_HIT", False), ("OPTION_TARGET_HIT", False), ("EOD_SQUARE_OFF_15:20", False),
    ("OPPOSITE_8EMA_CLOSE", False),
    ("SPOT_SL_HIT", True), ("OPTION_SL_HIT", True), ("BROKER_SL_FILLED", True),
])
def test_which_exits_are_stop_losses(reason, is_sl):
    assert is_stop_loss_exit(reason) is is_sl


def test_profit_exit_keeps_the_arm_and_reenters():
    b15, state = _armed("SHORT")
    _tick(b15, state, 24990.0)
    first, _ = _tick(b15, state, 24999.0)
    assert len(first) == 1
    assert note_prototype_exit(state, _cfg(), "SHORT", "SPOT_TARGET_HIT") is False
    assert note_prototype_exit(state, _cfg(), "SHORT", "OPTION_TARGET_HIT") is False
    assert state["arm"]["direction"] == "SHORT" and "sl_blocked_side" not in state
    _tick(b15, state, 24960.0)
    again, _ = _tick(b15, state, 24999.0)
    assert [s["direction"] for s in again] == ["SHORT"]          # PE again


@pytest.mark.parametrize("reason", ["SPOT_SL_HIT", "OPTION_SL_HIT", "BROKER_SL_FILLED"])
def test_stop_loss_cancels_that_side_until_the_opposite_side_is_armed(reason):
    b15, state = _armed("SHORT")
    _tick(b15, state, 24990.0)
    first, _ = _tick(b15, state, 24999.0)
    assert len(first) == 1

    assert note_prototype_exit(state, _cfg(), "SHORT", reason) is True
    assert "arm" not in state and state["sl_blocked_side"] == "SHORT"
    # price comes back to the line: no PE
    _tick(b15, state, 24990.0)
    none, st = _tick(b15, state, 24999.0)
    assert none == [] and st["phase"].startswith(("EXITED", "WAITING FOR CLOSE"))

    # a later bear cross cannot re-arm PE while it is cancelled
    _maybe_flip_arm(state, False, True, 111, 25000.0)
    assert "arm" not in state and state["sl_blocked_side"] == "SHORT"
    # a close above arms CE and lifts the cancel
    _maybe_flip_arm(state, True, False, 222, 25000.0)
    assert state["arm"]["direction"] == "LONG" and "sl_blocked_side" not in state
    # after that, a close below can arm PE again
    _maybe_flip_arm(state, False, True, 333, 25000.0)
    assert state["arm"]["direction"] == "SHORT"


def test_stop_loss_on_ce_mirror():
    state: Dict[str, Any] = {}
    _arm_new(state, direction="LONG", cross_bar_open_ts=1, e8=25000.0)
    assert note_prototype_exit(state, _cfg(), "LONG", "OPTION_SL_HIT") is True
    assert "arm" not in state and state["sl_blocked_side"] == "LONG"
    _maybe_flip_arm(state, True, False, 2, 25000.0)
    assert "arm" not in state
    _maybe_flip_arm(state, False, True, 3, 25000.0)
    assert state["arm"]["direction"] == "SHORT" and "sl_blocked_side" not in state


def test_stop_loss_does_not_remove_an_arm_for_the_other_side():
    state: Dict[str, Any] = {}
    _arm_new(state, direction="LONG", cross_bar_open_ts=1, e8=25000.0)   # already flipped to CE
    assert note_prototype_exit(state, _cfg(), "SHORT", "SPOT_SL_HIT") is True
    assert state["arm"]["direction"] == "LONG"


def test_reentry_after_stop_loss_can_be_switched_on():
    state: Dict[str, Any] = {}
    _arm_new(state, direction="SHORT", cross_bar_open_ts=1, e8=25000.0)
    assert note_prototype_exit(state, _cfg(reentry_after_stop_loss=True), "SHORT", "SPOT_SL_HIT") is False
    assert state["arm"]["direction"] == "SHORT"


def test_live_close_applies_the_rule_and_saves_it(tmp_path: Path):
    from analyst.test_live_exit_orders_8510 import insert_test_position

    async def _go():
        b = make_test_backend(tmp_path, FakeBroker(configured=False))
        b.cached_telemetry["quotes"]["54321"] = 160.0
        _arm_new(b._ema8_prototype_state, direction="SHORT", cross_bar_open_ts=1, e8=25000.0)
        pos = insert_test_position(b, "P_SL", mode="PAPER_ONLY", sl_order_id="",
                                   strategy_id="EMA8_PROTOTYPE_SHORT", symbol="NIFTY 25000 PE",
                                   spot_target=24960.0, spot_stop=25040.0)
        await b._close_position_with_broker(pos, "SPOT_SL_HIT", spot_exit=25040.0)
        assert "arm" not in b._ema8_prototype_state
        assert b._ema8_prototype_state["sl_blocked_side"] == "SHORT"

        b._ema8_prototype_state = {}                       # restart
        b._load_ema8_prototype_state()
        assert b._ema8_prototype_state.get("sl_blocked_side") == "SHORT"

        # a profit exit leaves the arm alone
        _arm_new(b._ema8_prototype_state, direction="LONG", cross_bar_open_ts=2, e8=25000.0)
        b._ema8_prototype_state.pop("sl_blocked_side", None)
        pos2 = insert_test_position(b, "P_TG", mode="PAPER_ONLY", sl_order_id="",
                                    strategy_id="EMA8_PROTOTYPE_LONG", symbol="NIFTY 25000 CE")
        b.cached_telemetry["quotes"]["54321"] = 241.0
        await b._close_position_with_broker(pos2, "OPTION_TARGET_HIT", spot_exit=25030.0)
        assert b._ema8_prototype_state["arm"]["direction"] == "LONG"
        assert "sl_blocked_side" not in b._ema8_prototype_state

    asyncio.run(_go())


def test_oi_placeholder_is_inert_until_switched_on_and_fed():
    b15, state = _armed("LONG")
    state["oi_bias"] = "PE"                                # a bias exists, but the filter is OFF
    _tick(b15, state, 25005.0)
    sigs, _ = _tick(b15, state, 25002.0)
    assert len(sigs) == 1

    cfg = _cfg(oi_direction_filter_enabled=True)
    b15, state = _armed("LONG")                            # ON, nothing fed -> still no effect
    _tick(b15, state, 25005.0, cfg=cfg)
    sigs, _ = _tick(b15, state, 25002.0, cfg=cfg)
    assert len(sigs) == 1

    b15, state = _armed("LONG")                            # ON and fed PE -> CE is held back
    state["oi_bias"] = "PE"
    _tick(b15, state, 25005.0, cfg=cfg)
    sigs, st = _tick(b15, state, 25002.0, cfg=cfg)
    assert sigs == [] and "OI direction filter" in st["block_reason"]
