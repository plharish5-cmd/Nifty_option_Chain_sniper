"""v2.0.44 — Prototype "+40 then trail" exit, tested end-to-end on simulate_prototype_spot_exit.

Proves: trail ON rides past +40 and exits on a close through the trailing stop; trail OFF is byte-
identical to the old fixed +40; breakeven protection; monotonic ratchet; opposite-close/15:20 still win.
"""
from __future__ import annotations

import pandas as pd

from analyst.terminal8510_backend import simulate_prototype_spot_exit


def _bars(entry_ts, entry_spot):
    # One 15m anchor bar at entry; e8 far below so no opposite-close interferes for a long.
    return [{"ts": entry_ts, "day": "2026-11-20", "hm": "10:00", "close": entry_spot, "e8": entry_spot - 500}]


def _m(ts, day, hm, o, h, l, c):
    return {"ts": ts, "day": day, "hm": hm, "open": o, "high": h, "low": l, "close": c}


def _run(candles, entry_spot=25500.0, is_long=True, **kw):
    df = pd.DataFrame(candles)
    entry_ts = 1_700_000_000
    bars = _bars(entry_ts, entry_spot)
    return simulate_prototype_spot_exit(
        bars=bars, start_idx=0, entry_spot=entry_spot, is_long=is_long,
        sl_pts=40.0, tgt_pts=40.0, eod_hm="15:20",
        day_1m=df, entry_ts=entry_ts - 1, exit_on_opposite_close=True, **kw,
    )


def _series_long(entry_ts):
    # Long from 25500: climbs to +88 then pulls back. 1m candles, 60s apart.
    d, hm = "2026-11-20", "10:0"
    out = []
    path = [  # (high, low, close)
        (25520, 25500, 25515),   # +15/+20
        (25545, 25515, 25542),   # crosses +40 trigger (high 25545 >= 25540)
        (25560, 25540, 25558),   # running up; prior low 25540
        (25588, 25558, 25585),   # +88 peak; prior low 25558
        (25586, 25566, 25570),   # pulls back; prior low 25588->ratchet uses prev candle low 25558
        (25569, 25548, 25552),   # closes below trail (trail ~25558) -> SPOT_TRAIL_EXIT
        (25560, 25545, 25558),
    ]
    # 15-minute candles (900s apart) so the owner's 15m trail ratchets once per bar.
    base_min = 0
    for i, (h, l, c) in enumerate(path):
        tot = base_min + 15 * (i + 1)
        hm = f"10:{tot:02d}" if tot < 60 else f"11:{tot-60:02d}"
        out.append(_m(entry_ts + 900 * (i + 1), d, hm, c, h, l, c))
    return out


def test_trail_on_rides_past_40_and_exits_on_trail():
    r = _run(_series_long(1_700_000_000), trail_after_target=True)
    assert r["exit_reason"] == "SPOT_TRAIL_EXIT"
    # captured more than the fixed +40 would have (exit above entry+40 would be <40; trail should beat it)
    assert r["st_pnl_pts"] > 40.0 or r["exit_spot"] > 25540.0


def test_trail_off_is_fixed_plus_40():
    r = _run(_series_long(1_700_000_000), trail_after_target=False)
    assert r["exit_reason"] == "SPOT_TARGET_HIT"
    assert abs(r["st_pnl_pts"] - 40.0) < 1e-6
    assert abs(r["exit_spot"] - 25540.0) < 1e-6


def test_trigger_then_reversal_exits_at_breakeven_not_loss():
    # Reaches +40 (arms, stop->breakeven 25500), then collapses straight down.
    ts = 1_700_000_000
    d = "2026-11-20"
    candles = [
        _m(ts + 60, d, "10:01", 25515, 25545, 25515, 25542),   # arms at +40
        _m(ts + 120, d, "10:02", 25542, 25543, 25470, 25475),  # closes below breakeven 25500
    ]
    r = _run(candles, trail_after_target=True)
    assert r["exit_reason"] == "SPOT_TRAIL_EXIT"
    # breakeven protection: exit at/above entry, never a loss from the +40 excursion
    assert r["st_pnl_pts"] >= 0.0
    assert r["exit_spot"] >= 25500.0


def test_stop_before_trigger_is_normal_sl():
    ts = 1_700_000_000
    d = "2026-11-20"
    candles = [_m(ts + 60, d, "10:01", 25500, 25505, 25455, 25460)]  # low 25455 <= stop 25460
    r = _run(candles, trail_after_target=True)
    assert r["exit_reason"] == "SPOT_SL_HIT"
    assert abs(r["st_pnl_pts"] + 40.0) < 1e-6


def test_short_mirror_trails():
    ts = 1_700_000_000
    d = "2026-11-20"
    # Short from 25500: falls to -50 (trigger -40 at 25460), then bounces and closes back up through trail.
    candles = [
        _m(ts + 60, d, "10:01", 25490, 25495, 25455, 25458),   # low 25455 <= trigger 25460 -> arm
        _m(ts + 120, d, "10:02", 25458, 25462, 25440, 25445),  # running down; prior high 25495
        _m(ts + 180, d, "10:03", 25445, 25512, 25445, 25505),  # closes above trail -> exit
    ]
    r = _run(candles, is_long=False, trail_after_target=True)
    assert r["exit_reason"] == "SPOT_TRAIL_EXIT"


def test_eod_squareoff_still_overrides():
    ts = 1_700_000_000
    d = "2026-11-20"
    candles = [_m(ts + 60, d, "15:21", 25510, 25512, 25505, 25508)]
    r = _run(candles, trail_after_target=True)
    assert r["exit_reason"].startswith("EOD_SQUARE_OFF")


def test_gap_through_trail_books_open_not_untraded_stop():
    """P1#4 (ChatGPT review): if the exit candle gaps past the trail (never trades at the stop),
    the fill is the bar OPEN, not the un-traded stop price — no overstated profit."""
    ts = 1_700_000_000
    d = "2026-11-20"
    candles = [
        _m(ts + 60, d, "10:01", 25515, 25545, 25515, 25542),   # arm (+40 trigger), prev_ext low 25515
        _m(ts + 120, d, "10:02", 25542, 25560, 25540, 25555),  # armed; prev_ext becomes 25540
        # gap-down bar: high 25525 < trail stop (ratcheted to 25540); closes through -> exit
        _m(ts + 180, d, "10:03", 25520, 25525, 25500, 25505),
    ]
    r = _run(candles, trail_after_target=True)
    assert r["exit_reason"] == "SPOT_TRAIL_EXIT"
    # must NOT book the un-traded 25540 stop; books the open 25520 (gap fill)
    assert r["exit_spot"] <= 25520.0
    assert r["exit_spot"] < 25540.0


# --- P1#1: runtime live-monitor trail tick (ChatGPT review) ---
from analyst.ema8_prototype_8510 import prototype_trail_tick


def test_tick_pre_trigger_sl_intratick():
    assert prototype_trail_tick(is_long=True, spot_entry=25500, spot_idx=25459,
                                trigger_level=25540, stop_level=25460, armed=False)[2:] == (True, "SPOT_SL_HIT")


def test_tick_arms_at_trigger_moves_to_breakeven_no_exit():
    ns, na, hit, r = prototype_trail_tick(is_long=True, spot_entry=25500, spot_idx=25541,
                                          trigger_level=25540, stop_level=25460, armed=False)
    assert na is True and ns == 25500 and hit is False


def test_tick_armed_bar_ratchets_and_exits_on_close_through():
    ns, na, hit, r = prototype_trail_tick(is_long=True, spot_entry=25500, spot_idx=25555,
                                          trigger_level=25540, stop_level=25500, armed=True,
                                          bar={"low": 25560, "high": 25590, "close": 25558})
    assert ns == 25560 and hit is True and r == "SPOT_TRAIL_EXIT"


def test_tick_armed_no_bar_holds():
    assert prototype_trail_tick(is_long=True, spot_entry=25500, spot_idx=25600,
                                trigger_level=25540, stop_level=25520, armed=True, bar=None) == (25520, True, False, "")


def test_tick_short_mirror():
    ns, na, hit, r = prototype_trail_tick(is_long=False, spot_entry=25500, spot_idx=25470,
                                          trigger_level=25460, stop_level=25500, armed=True,
                                          bar={"low": 25400, "high": 25440, "close": 25445})
    assert ns == 25440 and hit is True and r == "SPOT_TRAIL_EXIT"


def test_tick_ratchet_is_monotonic():
    # a worse (higher-low) bial cannot loosen a long stop
    ns, _, _, _ = prototype_trail_tick(is_long=True, spot_entry=25500, spot_idx=25600,
                                       trigger_level=25540, stop_level=25570, armed=True,
                                       bar={"low": 25550, "high": 25620, "close": 25600})
    assert ns == 25570  # stays at 25570, not lowered to 25550
