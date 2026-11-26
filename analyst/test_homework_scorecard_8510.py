"""Homework-vs-algo scorecard: matching + every verdict class."""
from datetime import datetime
from zoneinfo import ZoneInfo

from analyst.homework_scorecard_8510 import (
    AlgoTrade, HomeworkTrade, build_spot_index, enrich_algo_nifty_points,
    match_and_score, normalize_algo_trades, parse_homework_csv, render_scorecard,
    spot_at,
)

D = "2026-11-26"
IST = ZoneInfo("Asia/Kolkata")


def _ts(hm):
    h, m = hm.split(":")
    return int(datetime(2026, 11, 26, int(h), int(m), tzinfo=IST).timestamp())


def _hw(side, et, el, xt, xl, strat="prototype"):
    return HomeworkTrade(date=D, strategy=strat, side=side, entry_time=et,
                         entry_level=el, exit_time=xt, exit_level=xl)


def _algo(side, eh, xh, reason, nifty=None, optpts=20.0):
    return AlgoTrade(side=side, entry_hm=eh, exit_hm=xh, option_points=optpts,
                     net_pnl=optpts * 65, exit_reason=reason, nifty_points=nifty)


def test_nifty_points_sign():
    assert _hw("CE", "09:15", 25768, "11:00", 25927).nifty_points == 159.0
    assert _hw("PE", "14:00", 25878, "14:30", 25838).nifty_points == 40.0


def test_match_when_algo_captures():
    hw = [_hw("CE", "09:15", 25768, "11:00", 25927)]
    algo = [_algo("CE", "09:16", "11:00", "SPOT_TRAIL_EXIT", nifty=150.0)]
    card = match_and_score(hw, algo)
    assert card.verdicts[0].verdict == "MATCH"
    assert card.matched_count == 1 and card.missed_count == 0


def test_missed_when_no_algo_trade():
    hw = [_hw("CE", "09:15", 25768, "11:00", 25927)]
    card = match_and_score(hw, [])
    assert card.verdicts[0].verdict == "MISSED"
    assert "gate" in card.verdicts[0].detail.lower()


def test_premature_sl_when_homework_won_but_algo_stopped():
    hw = [_hw("CE", "09:15", 25768, "11:00", 25927)]
    algo = [_algo("CE", "09:16", "09:45", "SPOT_SL_HIT", nifty=-40.0)]
    card = match_and_score(hw, algo)
    assert card.verdicts[0].verdict == "PREMATURE_SL"


def test_under_captured_when_algo_exits_early():
    hw = [_hw("CE", "09:15", 25768, "11:00", 25927)]  # +159
    algo = [_algo("CE", "09:16", "09:40", "SPOT_TRAIL_EXIT", nifty=30.0)]  # kept 30 of 159
    card = match_and_score(hw, algo)
    assert card.verdicts[0].verdict == "UNDER_CAPTURED"


def test_wrong_side():
    hw = [_hw("CE", "12:30", 25904, "13:00", 25890)]
    algo = [_algo("PE", "12:31", "13:00", "SPOT_TARGET_HIT", nifty=20.0)]
    card = match_and_score(hw, algo)
    assert card.verdicts[0].verdict == "WRONG_SIDE"


def test_extra_algo_trade_listed():
    hw = [_hw("CE", "09:15", 25768, "11:00", 25927)]
    algo = [
        _algo("CE", "09:16", "11:00", "SPOT_TRAIL_EXIT", nifty=150.0),
        _algo("PE", "13:30", "14:00", "SPOT_SL_HIT", nifty=-40.0),
    ]
    card = match_and_score(hw, algo)
    assert len(card.extra_algo) == 1 and card.extra_algo[0].side == "PE"


def test_match_outside_tolerance_is_missed():
    hw = [_hw("CE", "09:15", 25768, "11:00", 25927)]
    algo = [_algo("CE", "10:30", "11:00", "SPOT_TRAIL_EXIT", nifty=150.0)]  # 75m away
    card = match_and_score(hw, algo, tol_min=20)
    assert card.verdicts[0].verdict == "MISSED"


def test_normalize_from_ledger_row_shape():
    rows = [{
        "direction": "LONG", "symbol": "NIFTY26NOV25800CE",
        "entry_time": "26 Nov 2026 09:16:03 IST", "exit_time": "26 Nov 2026 11:00:10 IST",
        "Option_Points": 55.0, "net_pnl": 3200.0, "exit_reason": "SPOT_TRAIL_EXIT",
        "index_entry": 25768.0, "index_exit": 25918.0,
    }]
    at = normalize_algo_trades(rows)[0]
    assert at.side == "CE" and at.entry_hm == "09:16" and at.nifty_points == 150.0


def test_render_runs():
    hw = [_hw("CE", "09:15", 25768, "11:00", 25927)]
    algo = [_algo("CE", "09:16", "11:00", "SPOT_TRAIL_EXIT", nifty=150.0)]
    txt = render_scorecard(match_and_score(hw, algo))
    assert "Homework vs Algo" in txt and "MATCH" in txt


def test_spot_index_and_lookup():
    candles = [
        {"ts": _ts("09:15"), "close": 25768.0},
        {"ts": _ts("11:00"), "close": 25927.0},
        {"ts": _ts("14:00"), "close": 25838.0},
    ]
    idx = build_spot_index(candles)
    assert spot_at(idx, "09:15") == 25768.0
    assert spot_at(idx, "11:01", tol_min=3) == 25927.0   # nearest within tolerance
    assert spot_at(idx, "12:30", tol_min=3) is None      # nothing close


def test_enrich_fills_nifty_points():
    candles = [
        {"ts": _ts("09:16"), "close": 25770.0},
        {"ts": _ts("11:00"), "close": 25920.0},
    ]
    idx = build_spot_index(candles)
    algo = [_algo("CE", "09:16", "11:00", "SPOT_TRAIL_EXIT", nifty=None)]
    enrich_algo_nifty_points(algo, idx)
    assert algo[0].nifty_points == 150.0  # 25920 - 25770


def test_enrich_turns_unknown_into_under_captured():
    # Algo entered the morning CE but held to EOD for little NIFTY gain vs homework +159.
    candles = [{"ts": _ts("09:16"), "close": 25770.0}, {"ts": _ts("15:20"), "close": 25805.0}]
    idx = build_spot_index(candles)
    hw = [_hw("CE", "09:15", 25768, "11:00", 25927)]  # +159
    algo = [_algo("CE", "09:16", "15:20", "EOD_SQUARE_OFF_15:20", nifty=None)]
    enrich_algo_nifty_points(algo, idx)  # algo kept 35 NIFTY
    card = match_and_score(hw, algo)
    assert card.verdicts[0].verdict == "UNDER_CAPTURED"
    assert "35" in card.verdicts[0].detail


def test_parse_csv(tmp_path):
    p = tmp_path / "hw.csv"
    p.write_text(
        "date,strategy,side,entry_time,entry_level,ema8_at_entry,exit_time,exit_level,notes\n"
        "2026-11-26,prototype,CE,09:15,25768,25760,11:00,25927,morning trail\n"
        "2026-11-26,reversal,PE,14:00,25878,25880,14:30,25838,afternoon\n",
        encoding="utf-8",
    )
    hw = parse_homework_csv(str(p))
    assert len(hw) == 2
    assert hw[0].side == "CE" and hw[0].nifty_points == 159.0
    assert hw[1].side == "PE" and hw[1].nifty_points == 40.0
