"""
tests/test_kronos_forecast.py — NIFTY Kronos forecast engine (no torch, no network).

These lock the honest-reporting rules: counts are never relabelled as probabilities, index volume
is never forecast, and a unanimous-but-wrong past forecast is scored as wrong.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("ICT_DATA_DIR", str(Path(__file__).resolve().parent / ".data"))

from app.engine import kronos_forecast as kf
from app.timeutil import IST_OFFSET_S


def _ts(day: int, hhmm: str) -> int:
    """epoch for IST day index + HH:MM."""
    hh, mm = (int(x) for x in hhmm.split(":"))
    return day * 86400 + (hh * 60 + mm) * 60 - IST_OFFSET_S


def _path(closes):
    """[open, high, low, close, volume] per step; only close and volume matter here."""
    return [[c, c + 5, c - 5, c, 1000.0] for c in closes]


def test_filter_hourly_drops_the_1515_stub():
    day = 20700
    rows = [
        {"ts": _ts(day, "09:15"), "o": 1, "h": 1, "l": 1, "c": 100.0},
        {"ts": _ts(day, "14:15"), "o": 1, "h": 1, "l": 1, "c": 101.0},
        {"ts": _ts(day, "15:15"), "o": 1, "h": 1, "l": 1, "c": 102.0},   # 15-minute stub, not an hour
    ]
    kept = kf.filter_hourly(rows)
    assert [r["c"] for r in kept] == [100.0, 101.0]

    with_stub = kf.filter_hourly(rows, include_stub=True)
    assert len(with_stub) == 3


def test_next_session_ts_stays_in_market_hours_and_skips_weekend():
    # 20749 = 2026-10-23, a Friday ((day + 3) % 7 == 4 is Python's Friday)
    fri = 20749
    assert (fri + 3) % 7 == 4
    out = kf.next_session_ts(_ts(fri, "14:15"), 3)
    assert len(out) == 3
    from app.timeutil import hm, to_ist
    assert hm(out[0]) == "09:15"
    assert to_ist(out[0]).weekday() == 0          # Monday, not Saturday
    assert [hm(t) for t in out] == ["09:15", "10:15", "11:15"]


def test_summarize_paths_counts_runs_and_never_calls_them_probability():
    last = 22750.0
    hist = [22700.0, 22710.0, 22730.0, 22740.0, 22750.0]
    paths = [_path([22760, 22770, 22800])] * 22 + [_path([22740, 22720, 22700])] * 8

    s = kf.summarize_paths(paths, last, hist)
    assert s["ok"] is True
    assert s["runs"] == 30
    assert s["up_count"] == 22 and s["down_count"] == 8
    assert s["up_of"] == "22 of 30"
    assert s["range_low"] == 22700.0 and s["range_high"] == 22800.0
    # mean end = (22*22800 + 8*22700)/30 = 22773.33
    assert s["mean_end"] == 22773.33
    # index candles carry no volume, so no expected volume is published
    assert s["has_real_volume"] is False
    assert s["expected_volume"] is None

    v = kf.verdict(s)
    assert "22 of 30" in v["text"]
    assert "not a" in v["text"]                    # explicitly disclaims the probability reading
    assert "coin flip" in v["caveat"]


def test_summarize_reports_volume_only_when_volume_is_real():
    last = 22750.0
    hist = [22700.0, 22750.0]
    paths = [_path([22760, 22770])] * 4
    s = kf.summarize_paths(paths, last, hist, has_real_volume=True)
    assert s["has_real_volume"] is True
    assert s["expected_volume"] == 2000.0          # 2 steps x 1000 per path


def test_sixty_percent_of_runs_is_not_a_lean():
    """18/30 is only 60% of runs — too thin to call a direction, so it reads UNDECIDED."""
    last = 22750.0
    hist = [22700.0, 22750.0]
    paths = [_path([22760, 22780])] * 18 + [_path([22740, 22720])] * 12
    s = kf.summarize_paths(paths, last, hist)
    assert s["up_count"] == 18
    assert s["undecided"] is True
    assert "UNDECIDED" in kf.verdict(s)["label"]


def test_undecided_split_is_flagged_not_dressed_as_a_lean():
    last = 22750.0
    hist = [22700.0, 22750.0]
    paths = [_path([22760, 22780])] * 15 + [_path([22740, 22720])] * 15
    s = kf.summarize_paths(paths, last, hist)
    assert s["undecided"] is True
    v = kf.verdict(s)
    assert "UNDECIDED" in v["label"]
    assert v["tone"] == "flat"


def test_split_for_stepback_and_reveal_marks_a_unanimous_miss_as_wrong():
    day = 20700
    rows = []
    for d in range(70):
        for i, slot in enumerate(kf.FULL_HOUR_SLOTS):
            px = 22000.0 + d * 10 + i
            rows.append({"ts": _ts(day + d, slot), "o": px, "h": px + 5, "l": px - 5, "c": px})

    split = kf.split_for_stepback(rows, hist=360, pred=6, step_back=6)
    assert split["ok"] is True
    assert len(split["context"]) == 360
    assert len(split["actual"]) == 6
    assert split["is_live"] is False

    last = float(split["context"][-1]["c"])
    # every run says DOWN, hard — but the real series grinds up
    paths = [_path([last - 50, last - 90, last - 130, last - 170, last - 200, last - 240])] * 30
    s = kf.summarize_paths(paths, last, [float(r["c"]) for r in split["context"]])
    assert s["down_count"] == 30

    rv = kf.score_reveal(s, split["actual"])
    assert rv["ok"] is True
    assert rv["actual_up"] is True
    assert rv["direction_hit"] is False
    assert rv["outside_band"] == 6                 # real path never entered the band
    assert "unanimous" in rv["note"]


def test_split_refuses_when_history_is_short():
    rows = [{"ts": _ts(20700, s), "o": 1, "h": 1, "l": 1, "c": 1.0} for s in kf.FULL_HOUR_SLOTS]
    split = kf.split_for_stepback(rows, hist=360, pred=24, step_back=0)
    assert split["ok"] is False
    assert "need 360" in split["message"]


def test_resample_1m_to_1h_sums_volume_into_session_slots():
    """Index price bars carry no volume; futures 1m rows must fold into the same hourly slots."""
    day = 20700
    rows = []
    for m in range(0, 120):                       # 09:15 -> 11:14
        ts = _ts(day, "09:15") + m * 60
        rows.append({"ts": ts, "o": 100.0 + m, "h": 105.0 + m, "l": 95.0 + m, "c": 101.0 + m, "v": 10.0})
    out = kf.resample_1m_to_1h(rows)
    assert [kf.hm(r["ts"]) if hasattr(kf, "hm") else None for r in out] or True
    from app.timeutil import hm
    assert [hm(r["ts"]) for r in out] == ["09:15", "10:15"]
    assert out[0]["v"] == 600.0 and out[1]["v"] == 600.0      # 60 minutes x 10 each
    assert out[0]["o"] == 100.0                               # first minute's open
    assert out[0]["c"] == 160.0                               # 60th minute's close
    assert out[0]["h"] == 164.0 and out[0]["l"] == 95.0


def test_attach_volume_merges_futures_volume_onto_index_bars():
    day = 20700
    price = [{"ts": _ts(day, s), "o": 1.0, "h": 1.0, "l": 1.0, "c": 1.0, "v": 0.0}
             for s in kf.FULL_HOUR_SLOTS]
    vol = [{"ts": _ts(day, s), "v": 5000.0} for s in kf.FULL_HOUR_SLOTS]

    att = kf.attach_volume(price, vol, source="combined-future")
    assert att["coverage_pct"] == 100.0
    assert att["source"] == "combined-future"
    assert all(r["v"] == 5000.0 for r in att["rows"])


def test_attach_volume_reports_partial_coverage_honestly():
    """A half-covered volume column must be visible as such — gaps would read as quiet hours."""
    day = 20700
    price = [{"ts": _ts(day, s), "o": 1.0, "h": 1.0, "l": 1.0, "c": 1.0} for s in kf.FULL_HOUR_SLOTS]
    vol = [{"ts": _ts(day, s), "v": 5000.0} for s in kf.FULL_HOUR_SLOTS[:3]]

    att = kf.attach_volume(price, vol)
    assert att["covered"] == 3 and att["total"] == 6
    assert att["coverage_pct"] == 50.0
    # the uncovered bars are explicitly zero, never silently carried forward
    assert [r["v"] for r in att["rows"]] == [5000.0, 5000.0, 5000.0, 0.0, 0.0, 0.0]


def test_attach_volume_with_no_futures_data_reports_source_none():
    day = 20700
    price = [{"ts": _ts(day, s), "o": 1.0, "h": 1.0, "l": 1.0, "c": 1.0} for s in kf.FULL_HOUR_SLOTS]
    att = kf.attach_volume(price, [])
    assert att["coverage_pct"] == 0.0
    assert att["source"] == "none"
