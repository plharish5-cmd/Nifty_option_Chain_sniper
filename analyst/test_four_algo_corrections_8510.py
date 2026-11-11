"""Owner rules of 11 Nov 2026 for Stage 2, Stage 3, 8 EMA Reversal and 1 HR Flagship.

 1. Entries are taken on the FIRST touch of the dynamic 8 EMA (running candle,
    recomputed with live spot), within 3 points, never before 09:30.
 2. The closed-candle scanner reports status only; it never fires these lanes.
 3. Reversal touches the dynamic 8 EMA, and the ribbon rule does not block it.
 4. Stage 2/3 and Reversal exit on spot OR option points, whichever is first.
    No Super Trail stop or target.
 5. Flagship: a PE is a short; a long books profit near resistance, a short
    near support; the OR 8 EMA trend option is a saved cockpit setting.
 6. A backtest pass over old candles leaves the live desk exactly as it was.
 7. Backtested trades are counted as wins or losses on points.

Fake broker and private ledger only. Wall clock is taken out of every test.
"""
from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analyst.mj_sir_stage_reversal_8510 import (  # noqa: E402
    _find_cross_index,
    evaluate_mj_sir_four_pack,
    load_mj_cfg,
)
from analyst.nse_spot_candles_8510 import ema4_ema8  # noqa: E402
from analyst.terminal8510_backend import (  # noqa: E402
    QuantTerminalBackend,
    flagship_sr_exit_trigger,
    uses_fixed_spot_or_option_exit,
)
from analyst.test_live_exit_orders_8510 import (  # noqa: E402
    FakeBroker,
    insert_test_position,
    make_test_backend,
)
from analyst.test_phase2_option_points_8510 import (  # noqa: E402
    FakeBroker as EntryFakeBroker,
    make_test_backend as make_entry_backend,
)

DAY = "2026-11-10"
SEC = "54321"


def _frame(closes: List[float]) -> pd.DataFrame:
    idx = pd.date_range(f"{DAY} 09:15", periods=len(closes), freq="15min", tz="Asia/Kolkata")
    return pd.DataFrame(
        {"open": closes, "high": [c + 3 for c in closes], "low": [c - 3 for c in closes],
         "close": closes, "volume": [1000] * len(closes)},
        index=idx,
    )


def _uptrend(n: int = 30) -> pd.DataFrame:
    return _frame([24000.0 + 12.0 * i for i in range(n)])


def _downtrend(n: int = 30) -> pd.DataFrame:
    return _frame([25000.0 - 12.0 * i for i in range(n)])


def _closed_e8(b15: pd.DataFrame) -> float:
    return float(ema4_ema8(b15["close"])[1].iloc[-2])


def _run(b15, state, *, dynamic=None, cfg_over=None):
    cfg = load_mj_cfg({"mj_sir_lecture": cfg_over or {}})
    return evaluate_mj_sir_four_pack(
        b15, bar_ci=-2, cfg=cfg, bb_stage2=False, bb_stage3=False, bb_cross=False,
        party_switch_enabled=False, active_party="NEUTRAL", mj_state=state,
        session_day=DAY, dynamic=dynamic,
    )


def _tick(spot: float, hm: str = "10:05", lo: float | None = None, hi: float | None = None) -> Dict[str, Any]:
    return {"live_spot": spot, "tick_low": lo if lo is not None else spot,
            "tick_high": hi if hi is not None else spot, "now_hm": hm}


def _ids(sigs) -> List[str]:
    return [s["strategy_id"] for s in sigs]


# 1, 2 ────────────────────────────────────────────────────────────────────────
def test_closed_candle_scan_never_fires_stage2():
    b15 = _uptrend()
    sigs, st = _run(b15, {})
    assert "STAGE_2_LONG" not in _ids(sigs)
    assert st["stage2_long"]["state"] == "ARMED"
    assert "dynamic 8 EMA" in st["stage2_long"]["block_reason"]


def test_stage2_fires_on_first_touch_of_dynamic_8ema_only_once():
    b15 = _uptrend()
    e8 = _closed_e8(b15)
    state: Dict[str, Any] = {}

    far, _ = _run(b15, state, dynamic=_tick(e8 + 30.0))          # 30 points above the line
    assert "STAGE_2_LONG" not in _ids(far)

    first, st = _run(b15, state, dynamic=_tick(e8 + 1.0))         # first touch
    assert _ids(first).count("STAGE_2_LONG") == 1
    sig = [s for s in first if s["strategy_id"] == "STAGE_2_LONG"][0]
    assert sig["index_price"] == pytest.approx(e8 + 1.0)          # booked at live spot
    assert sig["target_pts"] == 40.0 and sig["sl_pts"] == 40.0

    again, st2 = _run(b15, state, dynamic=_tick(e8 + 0.5))        # second touch, same setup
    assert "STAGE_2_LONG" not in _ids(again)
    assert "First touch already taken" in st2["stage2_long"]["block_reason"]


def test_stage2_touch_is_three_points_not_twenty():
    b15 = _uptrend()
    e8 = _closed_e8(b15)
    sigs, _ = _run(b15, {}, dynamic=_tick(e8 + 12.0, lo=e8 + 10.0))
    assert "STAGE_2_LONG" not in _ids(sigs)


def test_stage2_does_not_fire_after_price_has_gone_through_the_line():
    b15 = _uptrend()
    e8 = _closed_e8(b15)
    sigs, _ = _run(b15, {}, dynamic=_tick(e8 - 25.0))
    assert "STAGE_2_LONG" not in _ids(sigs)


def test_no_entry_before_0930_and_touch_is_still_available_after():
    b15 = _uptrend()
    e8 = _closed_e8(b15)
    state: Dict[str, Any] = {}
    early, st = _run(b15, state, dynamic=_tick(e8 + 1.0, hm="09:22"))
    assert early == []
    assert "09:30" in st["stage2_long"]["block_reason"]
    later, _ = _run(b15, state, dynamic=_tick(e8 + 1.0, hm="09:30"))
    assert "STAGE_2_LONG" in _ids(later)


def test_stage3_mirror_first_touch():
    b15 = _downtrend()
    e8 = _closed_e8(b15)
    state: Dict[str, Any] = {}
    first, _ = _run(b15, state, dynamic=_tick(e8 - 1.0))
    assert _ids(first).count("STAGE_3_SHORT") == 1
    again, _ = _run(b15, state, dynamic=_tick(e8 - 0.5))
    assert "STAGE_3_SHORT" not in _ids(again)


def test_legacy_closed_candle_mode_still_available_by_config():
    b15 = _uptrend()
    b15.iloc[-2, b15.columns.get_loc("low")] = _closed_e8(b15)      # closed candle touched
    sigs, _ = _run(b15, {}, cfg_over={"dynamic_8ema_entry": False})
    assert "STAGE_2_LONG" in _ids(sigs)


# 3 ───────────────────────────────────────────────────────────────────────────
def _reversal_frame():
    closes = [25000.0 - 15.0 * i for i in range(16)] + [24775.0 + 18.0 * i for i in range(1, 12)]
    full = _frame(closes)
    e4, e8 = ema4_ema8(full["close"])
    k = _find_cross_index(e4, e8, full["close"], True, len(full) - 1)
    assert k is not None and k + 3 <= len(full)
    return full, k


def test_reversal_fires_on_dynamic_8ema_touch_inside_window_and_ignores_ribbon():
    full, k = _reversal_frame()
    b15 = full.iloc[: k + 3]                    # running candle = cross + 2: first window candle
    e8 = _closed_e8(b15)
    state: Dict[str, Any] = {}
    # ribbon forced "attached": Stage lanes are blocked, the Reversal is not
    sigs, st = _run(b15, state, dynamic=_tick(e8 + 1.0), cfg_over={"ribbon_max_pts": 10_000.0})
    assert _ids(sigs) == ["CROSSOVER_BULLISH"]
    sig = sigs[0]
    assert sig["target_pts"] == 20.0 and sig["sl_pts"] == 20.0
    assert sig["index_price"] == pytest.approx(e8 + 1.0)
    assert "dynamic 8 EMA" in sig["setup_name"]
    again, _ = _run(b15, state, dynamic=_tick(e8 + 0.5), cfg_over={"ribbon_max_pts": 10_000.0})
    assert again == []


def test_reversal_waits_two_candles_and_expires_after_the_window():
    full, k = _reversal_frame()
    wait = full.iloc[: k + 2]                   # running candle = cross + 1
    sigs, st = _run(wait, {}, dynamic=_tick(_closed_e8(wait) + 1.0))
    assert "CROSSOVER_BULLISH" not in _ids(sigs)
    assert "Wait" in st["crossover_reversal"]["block_reason"]

    if k + 5 <= len(full):
        late = full.iloc[: k + 5]               # running candle = cross + 4: past the window
        sigs, st = _run(late, {}, dynamic=_tick(_closed_e8(late) + 1.0))
        assert "CROSSOVER_BULLISH" not in _ids(sigs)


# 4 ───────────────────────────────────────────────────────────────────────────
def test_which_lanes_use_the_fixed_exit():
    for sid in ("STAGE_2_LONG", "STAGE_3_SHORT", "CROSSOVER_BULLISH", "CROSSOVER_BEARISH",
                "EMA8_PROTOTYPE_LONG"):
        assert uses_fixed_spot_or_option_exit(sid)
    for sid in ("FLAGSHIP_1H_LONG", "H1_8EMA_SUPPORT_LONG", "PA_SR_LONG", "", None):
        assert not uses_fixed_spot_or_option_exit(sid)


def test_stage2_entry_uses_flat_40_option_points_and_no_super_trail(tmp_path: Path):
    async def _go():
        broker = EntryFakeBroker(configured=True)
        b = make_entry_backend(tmp_path, broker)
        res = await b.execute_order_with_chaser(
            contract={"symbol": "NIFTY 25000 CE", "security_id": SEC, "lot_size": 65,
                      "ltp": 250.0, "delta": 0.55},
            side="BUY", strategy_name="MJ Sir Stage 2 8-EMA (Long) — first touch",
            strategy_id="STAGE_2_LONG", target_pts=40.0, sl_pts=40.0, is_algo_trade=True,
            spot_context={"strategy_id": "STAGE_2_LONG", "spot_entry": 25000.0,
                          "spot_target_level": 25040.0, "spot_stop_level": 24960.0,
                          "spot_target_pts": 40.0, "spot_stop_pts": 40.0},
            enforce_market_hours=False,
        )
        assert res["opt_target_pts"] == 40.0 and res["opt_sl_pts"] == 40.0
        with sqlite3.connect(b.ledger_db) as conn:
            conn.row_factory = sqlite3.Row
            pos = dict(conn.execute("SELECT * FROM positions").fetchone())
        assert pos["initial_target"] == pytest.approx(pos["entry_price"] + 40.0)
        assert pos["initial_sl"] == pytest.approx(pos["entry_price"] - 40.0)
        assert pos["trail_sl_enabled"] == 0 and pos["trail_target_enabled"] == 0
        assert pos["spot_target_level"] == 25040.0 and pos["strategy_id"] == "STAGE_2_LONG"

    asyncio.run(_go())


def _monitor_backend(tmp_path: Path):
    b = make_test_backend(tmp_path, FakeBroker(configured=False))
    b.cfg["eod_square_off_time"] = "23:59"                 # wall clock out of the test
    b.scanner_cfg = {"flagship_eod_square_off": "23:59"}
    b._latest_closed_15m_bar = lambda: None
    return b


def _status(b, pos_id: str) -> Dict[str, Any]:
    with sqlite3.connect(b.ledger_db) as conn:
        conn.row_factory = sqlite3.Row
        return dict(conn.execute("SELECT * FROM positions WHERE position_id=?", (pos_id,)).fetchone())


async def _pass(b, ltp: float, spot: float) -> None:
    b.cached_telemetry["quotes"][SEC] = ltp
    b.get_active_positions({SEC: ltp}, spot_price=spot)
    for _ in range(40):
        await asyncio.sleep(0.005)
        if not getattr(b, "_close_tasks", {}):
            break


@pytest.mark.parametrize("sid,symbol,tgt,stop", [
    ("STAGE_2_LONG", "NIFTY 25000 CE", 25040.0, 24960.0),
    ("STAGE_3_SHORT", "NIFTY 25000 PE", 24960.0, 25040.0),
])
def test_stage_position_exits_on_spot_or_option_whichever_first(tmp_path: Path, sid, symbol, tgt, stop):
    async def _go():
        b = _monitor_backend(tmp_path)
        insert_test_position(b, "P1", mode="PAPER_ONLY", sl_order_id="", strategy_id=sid,
                             symbol=symbol, spot_target=tgt, spot_stop=stop)
        # +25 and -22 on the option used to end the trade under Super Trail.
        await _pass(b, 225.0, 25000.0)
        assert _status(b, "P1")["status"] == "OPEN"
        await _pass(b, 178.0, 25000.0)
        assert _status(b, "P1")["status"] == "OPEN"
        # option +40 comes first
        await _pass(b, 240.5, 25000.0)
        row = _status(b, "P1")
        assert row["status"] == "CLOSED" and row["exit_reason"] == "OPTION_TARGET_HIT"

        insert_test_position(b, "P2", mode="PAPER_ONLY", sl_order_id="", strategy_id=sid,
                             symbol=symbol, spot_target=tgt, spot_stop=stop)
        await _pass(b, 215.0, tgt)                           # spot target comes first
        row = _status(b, "P2")
        assert row["status"] == "CLOSED" and row["exit_reason"] == "SPOT_TARGET_HIT"

        insert_test_position(b, "P3", mode="PAPER_ONLY", sl_order_id="", strategy_id=sid,
                             symbol=symbol, spot_target=tgt, spot_stop=stop)
        await _pass(b, 190.0, stop)                          # spot stop
        row = _status(b, "P3")
        assert row["status"] == "CLOSED" and row["exit_reason"] == "SPOT_SL_HIT"

    asyncio.run(_go())


def test_reversal_position_uses_its_own_20_points(tmp_path: Path):
    async def _go():
        b = _monitor_backend(tmp_path)
        insert_test_position(b, "R1", mode="PAPER_ONLY", sl_order_id="",
                             strategy_id="CROSSOVER_BULLISH", spot_target=25020.0, spot_stop=24980.0)
        with sqlite3.connect(b.ledger_db) as conn:
            conn.execute("UPDATE positions SET initial_target=220.0, initial_sl=180.0 WHERE position_id='R1'")
        await _pass(b, 210.0, 25010.0)
        assert _status(b, "R1")["status"] == "OPEN"
        await _pass(b, 210.0, 25020.0)
        assert _status(b, "R1")["exit_reason"] == "SPOT_TARGET_HIT"

    asyncio.run(_go())


# 5 ───────────────────────────────────────────────────────────────────────────
def test_flagship_sr_exit_direction():
    sr = {"S1": 24990.0, "R1": 25060.0, "R4": 25200.0, "S4": 24800.0}
    assert flagship_sr_exit_trigger(True, 25050.0, sr) == "R1_NEAR_EXIT"
    assert flagship_sr_exit_trigger(True, 24995.0, sr) is None       # long at support stays in
    assert flagship_sr_exit_trigger(False, 24995.0, sr) == "S1_NEAR_EXIT"
    assert flagship_sr_exit_trigger(False, 25050.0, sr) is None
    assert flagship_sr_exit_trigger(True, 25195.0, sr) == "R4_NEAR_EXIT"
    assert flagship_sr_exit_trigger(False, 24805.0, sr) == "S4_NEAR_EXIT"


def test_flagship_pe_is_a_short_and_is_not_closed_at_entry(tmp_path: Path):
    async def _go():
        b = _monitor_backend(tmp_path)
        insert_test_position(b, "F1", mode="PAPER_ONLY", sl_order_id="",
                             strategy_id="FLAGSHIP_1H_SHORT", symbol="NIFTY 25000 PE",
                             spot_target=24960.0, spot_stop=25040.0)
        await _pass(b, 200.0, 25000.0)                       # at entry: used to close at once
        assert _status(b, "F1")["status"] == "OPEN"
        await _pass(b, 205.0, 24985.0)                       # moving in favour
        assert _status(b, "F1")["status"] == "OPEN"
        await _pass(b, 180.0, 25041.0)                       # spot through the stop above
        row = _status(b, "F1")
        assert row["status"] == "CLOSED" and row["exit_reason"] == "FLAGSHIP_SPOT_TRAIL_SL"

    asyncio.run(_go())


def test_flagship_long_stays_in_at_support_and_books_at_resistance(tmp_path: Path):
    async def _go():
        b = _monitor_backend(tmp_path)
        b.strategy_status = {"daily_sr": {"levels": {"S1": 24995.0, "R1": 25035.0}}}
        insert_test_position(b, "F2", mode="PAPER_ONLY", sl_order_id="",
                             strategy_id="FLAGSHIP_1H_LONG", symbol="NIFTY 25000 CE",
                             spot_target=25040.0, spot_stop=24960.0)
        await _pass(b, 200.0, 25000.0)                       # 5 points from S1
        assert _status(b, "F2")["status"] == "OPEN"
        await _pass(b, 212.0, 25022.0)                       # within 15 of R1
        row = _status(b, "F2")
        assert row["status"] == "CLOSED" and row["exit_reason"] == "R1_NEAR_EXIT"

    asyncio.run(_go())


def test_or_8ema_trend_option_is_a_saved_gate_setting(tmp_path: Path):
    b = make_test_backend(tmp_path, FakeBroker(configured=False))
    b.cfg["scanner"] = b.scanner_cfg = {}
    assert b.get_scanner_gate_config()["flagship_or_8ema_require_trend"] is True     # default: follow the trend
    out = b.update_scanner_gate_config({"flagship_or_8ema_require_trend": False})
    assert out["flagship_or_8ema_require_trend"] is False
    assert b.scanner_cfg["flagship_or_8ema_require_trend"] is False
    import json
    assert json.loads(Path(b.config_path).read_text())["scanner"]["flagship_or_8ema_require_trend"] is False


def test_cockpit_has_the_or_trend_option_and_new_card_text():
    html = (ROOT / "Algorithmic_Trading_Workspace_8510.html").read_text(encoding="utf-8")
    assert 'id="chkFlagshipOrTrend"' in html
    assert "OR 8 EMA entry must agree with Daily + 1H trend" in html
    assert "first touch</b> of dynamic 8 EMA" in html
    assert "touch <b>dynamic</b> 8 EMA" in html
    assert "touch <b>fixed</b> 8 EMA" not in html


# 6 ───────────────────────────────────────────────────────────────────────────
def test_backtest_pass_leaves_the_live_desk_untouched(tmp_path: Path):
    b = make_test_backend(tmp_path, FakeBroker(configured=False))
    b.active_party = "BULLISH"
    b._mj_sir_state = {"day": "2026-11-11", "stage2_fired_key": "keep-me"}
    b.strategy_status = {"stage2_long": {"state": "ARMED"}, "daily_sr": {"levels": {"S1": 1.0}}}
    b.last_crossover_event = {"type": "BULLISH_CROSS"}
    live_mj, live_cards = b._mj_sir_state, b.strategy_status
    seen: Dict[str, Any] = {}

    def fake_live_scan(ohlc, spot_price=None):
        seen["historical"] = getattr(b, "_historical_eval", False)
        seen["own_state"] = b._mj_sir_state is not live_mj and b.strategy_status is not live_cards
        b._mj_sir_state["stage2_fired_key"] = "from-2019"            # what the scan used to do
        b.strategy_status["stage2_long"]["state"] = "TRIGGERED"
        b.strategy_status["daily_sr"]["levels"] = {"S1": 99.0}
        b.active_party = "BEARISH"
        b.last_crossover_event = {"type": "BEARISH_CROSS"}
        return [{"strategy_id": "FLAGSHIP_1H_LONG", "setup_name": "x", "direction": "LONG"}]

    b.evaluate_candle_dataframe = fake_live_scan
    idx = pd.date_range("2019-01-01 09:15", periods=130, freq="1min", tz="Asia/Kolkata")
    df = pd.DataFrame({"dt": idx, "ts": [int(t.timestamp()) for t in idx], "open": 1.0, "high": 1.0,
                       "low": 1.0, "close": 1.0, "volume": 1})
    out = b._backtest_flagship_signals_at_bar(df, int(df["ts"].iloc[-1]), 1.0)

    assert seen == {"historical": True, "own_state": True}
    assert out and out[0][5]["sr_levels"] == {"S1": 99.0}             # the pass used its own levels
    assert b._mj_sir_state is live_mj and b._mj_sir_state["stage2_fired_key"] == "keep-me"
    assert b.strategy_status is live_cards
    assert b.strategy_status["stage2_long"]["state"] == "ARMED"
    assert b.strategy_status["daily_sr"]["levels"] == {"S1": 1.0}
    assert b.active_party == "BULLISH"
    assert b.last_crossover_event == {"type": "BULLISH_CROSS"}
    assert getattr(b, "_historical_eval", False) is False


def test_backtest_pass_restores_the_desk_even_if_the_scan_raises(tmp_path: Path):
    b = make_test_backend(tmp_path, FakeBroker(configured=False))
    b.active_party = "BULLISH"
    b._mj_sir_state = {"k": 1}
    live = b._mj_sir_state

    def boom(ohlc, spot_price=None):
        b._mj_sir_state["k"] = 2
        raise RuntimeError("bad candle")

    b.evaluate_candle_dataframe = boom
    idx = pd.date_range("2019-01-01 09:15", periods=130, freq="1min", tz="Asia/Kolkata")
    df = pd.DataFrame({"dt": idx, "ts": [int(t.timestamp()) for t in idx], "open": 1.0, "high": 1.0,
                       "low": 1.0, "close": 1.0, "volume": 1})
    with pytest.raises(RuntimeError):
        b._backtest_flagship_signals_at_bar(df, int(df["ts"].iloc[-1]), 1.0)
    assert b._mj_sir_state is live and live == {"k": 1}
    assert b.active_party == "BULLISH" and getattr(b, "_historical_eval", False) is False


# 7 ───────────────────────────────────────────────────────────────────────────
def test_backtested_trades_are_scored_on_points(tmp_path: Path):
    b = make_test_backend(tmp_path, FakeBroker(configured=False))
    s = b._compute_side_summary([40.0, -40.0, 12.5, -8.0], [0.0, 0.0, 0.0, 0.0])
    assert s["total_trades"] == 4
    assert s["win_rate_pct"] == 50.0                                  # was 0.0
    assert s["avg_win_pts"] == pytest.approx(26.25)
    assert s["avg_loss_pts"] == pytest.approx(-24.0)
    real = b._compute_side_summary([10.0, 10.0], [500.0, -120.0])     # real fills still use rupees
    assert real["win_rate_pct"] == 50.0
