"""Hold-out study and the Prototype backtest's look-ahead fix.

 1. Backtest ticks read a 1-minute candle only after its minute is over.
 2. The live default of forming_ohlc_from_1m is unchanged.
 3. The fast dynamic-EMA shortcut equals the full recomputation.
 4. Days are split by date; test days are the last ones and never overlap train.
 5. Scoring: cost per trade, drawdown, losing run, t-stat, ambiguous both ways.
 6. End to end on a synthetic market database: every variant runs through the
    app's own backtest, the pick is made on train days only, the live config
    object is put back untouched, and asking for only the Prototype lane does
    not run the Flagship pass.
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

from analyst import holdout_study_8510 as hs  # noqa: E402
from analyst.ema8_prototype_8510 import (  # noqa: E402
    _arm_new,
    backtest_ema8_prototype_at_bar,
    compute_dynamic_emas_on_bar,
    forming_ohlc_from_1m,
)
from analyst.nse_spot_candles_8510 import ema4_ema8  # noqa: E402
from analyst.test_live_exit_orders_8510 import FakeBroker, make_test_backend  # noqa: E402


# 1, 2 ────────────────────────────────────────────────────────────────────────
def test_completed_only_does_not_read_the_minute_in_progress():
    m1 = pd.DataFrame([
        {"ts": 1000, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5},   # 1000-1060
        {"ts": 1060, "open": 100.5, "high": 130.0, "low": 100.0, "close": 129.0},  # 1060-1120
    ])
    # at t=1090 the second minute is half way: its final high/close are not known yet
    assert forming_ohlc_from_1m(m1, bar_open_ts=1000, tick_ts=1090, completed_only=True) == (101.0, 99.0, 100.5)
    assert forming_ohlc_from_1m(m1, bar_open_ts=1000, tick_ts=1120, completed_only=True) == (130.0, 99.0, 129.0)
    # nothing finished yet in this candle -> last finished close before it
    assert forming_ohlc_from_1m(m1, bar_open_ts=1060, tick_ts=1090, completed_only=True) == (100.5, 100.5, 100.5)
    # live default: the newest row is real-time data and is read
    assert forming_ohlc_from_1m(m1, bar_open_ts=1000, tick_ts=1090) == (130.0, 99.0, 129.0)


def _bars(closes: List[float], t0: int):
    df = pd.DataFrame({"open": closes, "high": [c + 2 for c in closes],
                       "low": [c - 2 for c in closes], "close": closes})
    df["ts"] = [t0 + 900 * i for i in range(len(closes))]
    df["day"] = "2026-11-10"
    df["e4"], df["e8"] = ema4_ema8(df["close"])
    return df, df.to_dict(orient="records")


def test_backtest_entry_waits_for_the_minute_to_finish():
    t0 = int(pd.Timestamp("2026-11-10 10:00", tz="Asia/Kolkata").timestamp())
    df, bars = _bars([25000.0] * 12, t0)
    idx = 11
    bar_open = bars[idx]["ts"]
    state: Dict[str, Any] = {}
    _arm_new(state, direction="LONG", cross_bar_open_ts=bars[idx - 2]["ts"], e8=25000.0)
    # minute 0: away (25010). minute 1: back on the line (25001). rest: on the line
    closes = [25010.0, 25001.0] + [25001.0] * 13
    m1 = pd.DataFrame([{"ts": bar_open + 60 * m, "open": c, "high": c + 0.5, "low": c - 0.5, "close": c}
                       for m, c in enumerate(closes)])
    out = backtest_ema8_prototype_at_bar(bars, idx, state, {"ema8_prototype": {}}, b15_df=df, day_1m=m1)
    assert len(out) == 1
    meta = out[0][5]
    # minute 1 runs 60s-120s. It is known at 120s; reading it at 60s was the look-ahead.
    assert meta["entry_ts"] == bar_open + 120
    assert meta["entry_spot"] == pytest.approx(25001.0)


# 3 ───────────────────────────────────────────────────────────────────────────
def test_fast_dynamic_ema_equals_full_recomputation():
    closes = [25000.0 + 7.0 * ((i * 37) % 11 - 5) for i in range(60)]
    df, bars = _bars(closes, 1_800_000_000)
    idx, spot = 59, 25031.25
    e4_full, e8_full, _h, _l = compute_dynamic_emas_on_bar(df, bar_ci=idx, live_spot=spot)
    e4_fast = bars[idx - 1]["e4"] + (2 / 5) * (spot - bars[idx - 1]["e4"])
    e8_fast = bars[idx - 1]["e8"] + (2 / 9) * (spot - bars[idx - 1]["e8"])
    assert e4_fast == pytest.approx(e4_full, abs=1e-9)
    assert e8_fast == pytest.approx(e8_full, abs=1e-9)


# 4 ───────────────────────────────────────────────────────────────────────────
def test_split_is_by_date_and_never_overlaps():
    days = [f"2026-01-{d:02d}" for d in range(1, 21)]
    train, test = hs.split_days(days, 12, 5)
    assert test == days[-5:] and train == days[3:15]
    assert max(train) < min(test) and not set(train) & set(test)
    with pytest.raises(ValueError):
        hs.split_days(days, 18, 5)


# 5 ───────────────────────────────────────────────────────────────────────────
def test_score_charges_cost_and_measures_drawdown():
    rows = [
        {"entry_day": "d1", "entry_hm": "10:00", "st_pnl_pts": 40.0, "exit_reason": "SPOT_TARGET_HIT"},
        {"entry_day": "d1", "entry_hm": "11:00", "st_pnl_pts": -40.0, "exit_reason": "SPOT_SL_HIT",
         "is_ambiguous": True, "spot_target_pts": 40.0},
        {"entry_day": "d2", "entry_hm": "10:00", "st_pnl_pts": -16.0, "exit_reason": "OPPOSITE_8EMA_CLOSE"},
        {"entry_day": "d2", "entry_hm": "12:00", "st_pnl_pts": 40.0, "exit_reason": "SPOT_TARGET_HIT"},
    ]
    s = hs.score(rows, cost_pts=2.0)
    assert s["trades"] == 4 and s["wins"] == 2 and s["win_rate_pct"] == 50.0
    assert s["gross_pts"] == 24.0
    assert s["net_pts"] == 16.0                      # 24 - 4 x 2
    assert s["avg_net_pts"] == 4.0
    assert s["max_drawdown_pts"] == -60.0            # 38 -> -4 -> -22
    assert s["max_losing_run"] == 2
    assert s["ambiguous"] == 1
    assert s["net_pts_if_ambiguous_were_targets"] == 96.0   # that stop as a target: +80
    assert s["exit_reasons"]["SPOT_TARGET_HIT"] == {"trades": 2, "gross_pts": 80.0}
    assert hs.score([], 2.0)["trades"] == 0


# 6 ───────────────────────────────────────────────────────────────────────────
def _market_db(path: Path, n_days: int = 14) -> List[str]:
    days = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2026-10-19", periods=n_days)]
    rows, px = [], 24000.0
    for i, d in enumerate(days):
        day0 = int(pd.Timestamp(f"{d} 09:15", tz="Asia/Kolkata").timestamp())
        up, dn = (2.0, 1.4) if i % 3 else (1.4, 2.0)          # mostly rising days, some falling
        for m in range(375):
            px += up if m % 105 < 60 else -dn
            rows.append(("13", day0 + 60 * m, px, px + 1.5, px - 1.5, px + 0.4, 100))
    # a 1-hour Muhurat-style session that must be skipped
    extra = int(pd.Timestamp("2026-11-08 18:00", tz="Asia/Kolkata").timestamp())
    rows += [("13", extra + 60 * m, px, px + 1, px - 1, px, 10) for m in range(60)]
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE candles_1m (sec_id TEXT, ts INTEGER, o REAL, h REAL, l REAL, c REAL, v REAL)")
        conn.executemany("INSERT INTO candles_1m VALUES (?,?,?,?,?,?,?)", rows)
    return days


def test_study_end_to_end(tmp_path: Path, monkeypatch):
    b = make_test_backend(tmp_path, FakeBroker(configured=False))
    b.market_db = str(tmp_path / "market.sqlite3")
    days = _market_db(Path(b.market_db))
    b.directional_bias = "BOTH"
    b.scanner_cfg = {"bollinger_enabled": False, "ema8_prototype": {"enabled": True, "touch_tol_pts": 3.0}}
    b.cfg["scanner"] = b.scanner_cfg
    b.cfg["pa_sr_homework"] = {"enabled": False}
    live_cfg = b.scanner_cfg
    snapshot = repr(live_cfg)

    def no_flagship(*a, **k):
        raise AssertionError("Flagship pass must not run when only the Prototype lane is asked for")

    b._backtest_flagship_signals_at_bar = no_flagship
    assert hs.trading_days(Path(b.market_db)) == days          # the 60-minute session is skipped

    monkeypatch.setattr(hs, "MIN_TRADES_TO_CHOOSE", 1)
    variants = {
        "baseline": {},
        "morning only": {"no_entry_from_hm": "11:00"},
        "first cross only": {"reentry_same_side": False},
    }
    study = hs.run_study(b, train_days=9, test_days=4, cost_pts=2.0, variants=variants)

    assert b.scanner_cfg is live_cfg and repr(live_cfg) == snapshot      # live config untouched
    assert study["train_range"] == (days[1], days[9], 9)
    assert study["test_range"] == (days[10], days[13], 4)
    by = {r["variant"]: r for r in study["results"]}
    assert set(by) == set(variants)
    assert by["baseline"]["train"]["trades"] > 0
    # a narrower entry window can only remove entries
    assert by["morning only"]["train"]["trades"] <= by["baseline"]["train"]["trades"]
    assert by["first cross only"]["train"]["trades"] <= by["baseline"]["train"]["trades"]
    # cost is charged: net = gross - 2 per trade
    s = by["baseline"]["train"]
    assert s["net_pts"] == pytest.approx(s["gross_pts"] - 2.0 * s["trades"], abs=0.11)

    # the pick is the best TRAIN average, whatever the test column says
    best_train = max(study["results"], key=lambda r: r["train"]["avg_net_pts"])["variant"]
    assert study["pick"] == best_train
    assert study["verdict"].startswith(("PASS", "FAIL"))

    md = hs.render_markdown(study)
    assert "← pick" in md and "Not testable yet" in md and "OI direction filter" in md
    assert "never used for choosing" in md


def test_study_refuses_when_there_are_not_enough_days(tmp_path: Path):
    b = make_test_backend(tmp_path, FakeBroker(configured=False))
    b.market_db = str(tmp_path / "market.sqlite3")
    _market_db(Path(b.market_db), n_days=5)
    b.scanner_cfg = {}
    with pytest.raises(ValueError, match="Only 5 full sessions"):
        hs.run_study(b, train_days=170, test_days=84)
