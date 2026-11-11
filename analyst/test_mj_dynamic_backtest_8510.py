"""Stage 2 / Stage 3 / Reversal are backtested with the rules that run live.

 1. The backtest step calls the live evaluator: same signal as a live tick.
 2. No look-ahead: a 1-minute candle is read only once its minute is over.
 3. First touch only: one entry per setup, across ticks and across candles.
 4. Entry is booked at the spot of the touch tick, with its timestamp.
 5. Exit is walked on 1-minute candles after the entry: spot target / stop /
    15:20, and no opposite-close exit for these lanes.
 6. End to end on a synthetic market database: the date-range backtest
    produces Stage rows from the live evaluator, in points, with no rupees.
 7. The backtest never touches live Stage/Reversal memory.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analyst.mj_sir_stage_reversal_8510 import (  # noqa: E402
    backtest_mj_dynamic_at_bar,
    evaluate_mj_sir_four_pack,
    load_mj_cfg,
)
from analyst.nse_spot_candles_8510 import ema4_ema8  # noqa: E402
from analyst.terminal8510_backend import simulate_prototype_spot_exit  # noqa: E402
from analyst.test_live_exit_orders_8510 import FakeBroker, make_test_backend  # noqa: E402

DAY = "2026-11-10"
T0 = int(pd.Timestamp(f"{DAY} 09:15", tz="Asia/Kolkata").timestamp())
N = 24                                   # running candle = index 23 -> opens 15:00
STEP = 12.0


def _b15() -> pd.DataFrame:
    closes = [24000.0 + STEP * i for i in range(N)]
    df = pd.DataFrame({"open": closes, "high": [c + 3 for c in closes],
                       "low": [c - 3 for c in closes], "close": closes})
    df["ts"] = [T0 + 900 * i for i in range(N)]
    df["day"] = DAY
    df["e4"], df["e8"] = ema4_ema8(df["close"])
    return df


def _bars(df: pd.DataFrame) -> List[Dict[str, Any]]:
    return df.to_dict(orient="records")


def _minutes(bar_open: int, closes: List[float], lows: Dict[int, float] | None = None) -> pd.DataFrame:
    rows = []
    for m, c in enumerate(closes):
        lo = (lows or {}).get(m, c - 1.0)
        rows.append({"ts": bar_open + 60 * m, "open": c, "high": c + 1.0, "low": min(lo, c), "close": c})
    return pd.DataFrame(rows)


def _setup():
    df = _b15()
    idx = N - 1
    e8c = float(df["e8"].iloc[idx - 1])                # last closed 8 EMA
    bar_open = int(df["ts"].iloc[idx])
    df.loc[idx, ["low", "high"]] = [e8c - 2.0, e8c + 35.0]   # running candle spans the 1m path
    return df, idx, e8c, bar_open


def test_backtest_step_gives_the_live_signal_and_no_look_ahead():
    df, idx, e8c, bar_open = _setup()
    # minutes 0-2 far above the line; minute 3 comes down to it; later minutes stay there
    closes = [e8c + 30.0, e8c + 28.0, e8c + 25.0, e8c + 1.0] + [e8c + 1.5] * 11
    m1 = _minutes(bar_open, closes)
    state: Dict[str, Any] = {}
    out = backtest_mj_dynamic_at_bar(_bars(df), idx, state, {}, b15_df=df, day_1m=m1)

    assert [o[0] for o in out] == ["STAGE_2_LONG"]            # first touch only
    sid, _name, direc, tgt, sl, meta = out[0]
    assert (direc, tgt, sl) == ("LONG", 40.0, 40.0)
    # Minute 3 runs 180s-240s after the open. It is known at 240s, not at 180s.
    assert meta["entry_ts"] == bar_open + 240
    assert meta["entry_spot"] == pytest.approx(e8c + 1.0)
    assert meta["mj_dynamic"] is True

    # The live evaluator, given that same tick, fires the same lane.
    frame = df[["open", "high", "low", "close"]].copy()
    live, _ = evaluate_mj_sir_four_pack(
        frame, bar_ci=-2, cfg=load_mj_cfg({}), bb_stage2=False, bb_stage3=False, bb_cross=False,
        party_switch_enabled=False, active_party="NEUTRAL", mj_state={}, session_day=DAY,
        dynamic={"live_spot": e8c + 1.0, "tick_low": e8c + 1.0, "tick_high": e8c + 1.0, "now_hm": "15:04"},
    )
    assert [s["strategy_id"] for s in live] == ["STAGE_2_LONG"]
    assert live[0]["index_price"] == pytest.approx(meta["entry_spot"])


def test_first_touch_is_remembered_across_candles_of_the_same_setup():
    df, idx, e8c, bar_open = _setup()
    m1 = _minutes(bar_open, [e8c + 1.0] * 15)
    state: Dict[str, Any] = {}
    bars = _bars(df)
    first = backtest_mj_dynamic_at_bar(bars, idx, state, {}, b15_df=df, day_1m=m1)
    assert len(first) == 1
    again = backtest_mj_dynamic_at_bar(bars, idx, state, {}, b15_df=df, day_1m=m1)
    assert again == []
    fresh = backtest_mj_dynamic_at_bar(bars, idx, {}, {}, b15_df=df, day_1m=m1)
    assert len(fresh) == 1                                    # memory belongs to the run


def test_candle_far_from_the_line_gives_nothing():
    df, idx, e8c, bar_open = _setup()
    df.loc[idx, ["low", "high"]] = [e8c + 40.0, e8c + 60.0]
    m1 = _minutes(bar_open, [e8c + 50.0] * 15)
    assert backtest_mj_dynamic_at_bar(_bars(df), idx, {}, {}, b15_df=df, day_1m=m1) == []


def test_legacy_mode_is_not_handled_here():
    df, idx, e8c, bar_open = _setup()
    m1 = _minutes(bar_open, [e8c + 1.0] * 15)
    cfg = {"mj_sir_lecture": {"dynamic_8ema_entry": False}}
    assert backtest_mj_dynamic_at_bar(_bars(df), idx, {}, cfg, b15_df=df, day_1m=m1) == []


def test_stage_exit_walk_has_no_opposite_close_rule():
    # 15m candles that close below the 8 EMA right after a long entry
    bars = []
    for i in range(4):
        bars.append({"ts": T0 + 900 * i, "day": DAY, "hm": f"{9 + (15 + 15 * i) // 60:02d}:{(15 + 15 * i) % 60:02d}",
                     "open": 25000.0, "high": 25010.0, "low": 24985.0, "close": 24990.0, "e8": 25005.0})
    m1 = pd.DataFrame([{"ts": T0 + 60 * m, "open": 24995.0, "high": 25002.0, "low": 24988.0, "close": 24992.0,
                        "day": DAY, "hm": "09:15"} for m in range(60)])
    kw = dict(bars=bars, start_idx=0, entry_spot=25000.0, is_long=True, tgt_pts=40.0, sl_pts=40.0,
              eod_hm="15:20", day_1m=m1, entry_ts=T0 + 60)
    proto = simulate_prototype_spot_exit(**kw)
    stage = simulate_prototype_spot_exit(**kw, exit_on_opposite_close=False)
    assert proto["exit_reason"] == "OPPOSITE_8EMA_CLOSE"
    assert stage["exit_reason"] != "OPPOSITE_8EMA_CLOSE"


# 6, 7 ────────────────────────────────────────────────────────────────────────
def _market_db(path: Path) -> None:
    """Three sessions of 1-minute candles: a steady rise that dips to the 8 EMA."""
    rows = []
    px = 24000.0
    for d in ("2026-11-06", "2026-11-09", "2026-11-10"):
        day0 = int(pd.Timestamp(f"{d} 09:15", tz="Asia/Kolkata").timestamp())
        for m in range(375):
            cyc = m % 105
            px += 2.0 if cyc < 60 else -1.4                   # rise, then a slow pullback to the 8 EMA
            rows.append(("13", day0 + 60 * m, px, px + 1.5, px - 1.5, px + 0.4, 100))
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE candles_1m (sec_id TEXT, ts INTEGER, o REAL, h REAL, l REAL, c REAL, v REAL)")
        conn.executemany("INSERT INTO candles_1m VALUES (?,?,?,?,?,?,?)", rows)


def test_date_range_backtest_uses_live_rules_and_leaves_live_memory_alone(tmp_path: Path):
    b = make_test_backend(tmp_path, FakeBroker(configured=False))
    b.market_db = str(tmp_path / "market.sqlite3")
    _market_db(Path(b.market_db))
    b.directional_bias = "BOTH"
    b.scanner_cfg = {"bollinger_enabled": False, "ema8_prototype": {"enabled": False}}
    b.cfg["scanner"] = b.scanner_cfg
    b.cfg["pa_sr_homework"] = {"enabled": False}
    b._backtest_flagship_signals_at_bar = lambda *a, **k: []
    b._mj_sir_state = {"day": "live", "stage2_fired_key": "live-key"}
    live_state = b._mj_sir_state

    res = b.run_date_range_backtest("2026-11-06", "2026-11-10", include_today_paper=False,
                                    data_type_filter="SYNTHESIZED")
    assert res.get("ok") is True, res.get("error")
    rows = [r for r in res["trades"] if str(r.get("strategy_id", "")).startswith("STAGE_")]
    assert rows, "no Stage trades produced on rising days with pullbacks to the 8 EMA"

    # the run used its own memory; the live desk's is exactly as it was
    assert b._mj_sir_state is live_state and live_state == {"day": "live", "stage2_fired_key": "live-key"}

    per_day: Dict[str, int] = {}
    for r in rows:
        per_day[r["entry_day"]] = per_day.get(r["entry_day"], 0) + 1
        assert r["source"] == "SYNTHESIZED"
        assert r["entry_hm"][:5] >= "09:30"                       # live rule: no entry before 09:30
        assert "1m walk after entry" in r["Trailing_Details"]     # fixed spot exit, not the candle trail
        assert r["exit_reason"] in ("SPOT_TARGET_HIT", "SPOT_SL_HIT", "EOD_SQUARE_OFF_15:20")
        assert abs(float(r["st_pnl_pts"])) <= 40.0
        assert not r["net_pnl"] and not r["charges"]              # points only, no invented rupees
        assert float(r["dynamic_8ema_entry"]) > 0
        # The touch is the low since the last tick coming within 3 points of the
        # line; the entry is booked at spot on that tick, which can sit a
        # 1-minute candle's range (3 points here) above it. Same as live.
        gap = float(r["Index_Entry"]) - float(r["dynamic_8ema_entry"])
        assert -3.0 - 1e-6 <= gap <= 3.0 + 3.0 + 1e-6
    # first touch only: one entry per 4/8 stack per day here, not one per candle
    assert max(per_day.values()) == 1, per_day
    # and the summary scores them on points
    assert res["super_trail_summary"]["total_trades"] >= len(rows)
    assert res["super_trail_summary"]["win_rate_pct"] > 0
