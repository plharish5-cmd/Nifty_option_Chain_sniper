"""Auction read — hand-built candles where the right answer is known in advance."""
import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("ICT_DATA_DIR", str(Path(__file__).resolve().parent / ".data"))

from app.engine import auction  # noqa: E402
from app.engine.primitives import Bars, atr  # noqa: E402

D7, D8 = 1791344700, 1791431100        # 7 / 8 Oct 2026 09:15 IST


def prev_day_1m():
    """7 Oct: 375 minutes, mostly 21995–22005, a few minutes out at 21980 and 22020."""
    rows = []
    for k in range(375):
        lo, hi = (21995, 22005) if k % 10 else ((21980, 21990) if k % 20 else (22010, 22020))
        rows.append({"ts": D7 + 60 * k, "o": lo, "h": hi, "l": lo, "c": hi, "closed": True})
    return rows


def an_of(closes, wicks=None):
    """8 Oct 5m bars from a close path; o = previous close, h/l = ±4 around the body (or a custom wick)."""
    rows, pc = [], closes[0]
    for k, c in enumerate(closes):
        h, l = max(pc, c) + 4, min(pc, c) - 4
        if wicks and k in wicks:
            h, l = max(h, wicks[k][0]), min(l, wicks[k][1])
        rows.append({"ts": D8 + 300 * k, "o": pc, "h": h, "l": l, "c": c})
        pc = c
    b = Bars.from_rows(rows)
    return SimpleNamespace(tf="5m", bars=b, atr=atr(b, 14))


def kinds(res):
    return [(e["kind"], e["ref"]) for e in res["events"]]


def test_tpo_profile_poc_and_value():
    p = auction.tpo_profile(prev_day_1m(), 5.0, 0.70)
    assert p["poc"] in (21997.5, 22002.5)
    assert p["val"] >= 21990 and p["vah"] <= 22010
    assert p["high"] == 22020 and p["low"] == 21980


def test_acceptance_above_value_with_follow_through():
    an = an_of([22000, 22001, 21999, 22000, 22014, 22020, 22028, 22035])
    res = auction.compute(an, prev_day_1m())
    acc = [e for e in res["events"] if e["kind"] == "AUCTION_ACCEPT" and e["ref"] == "pVAH"]
    assert acc and acc[0]["dir"] == "bull" and acc[0]["i"] == 6
    assert res["read"]["response"]["kind"] == "AUCTION_ACCEPT"
    assert "accepting higher" in res["read"]["decision"]["text"]


def test_break_then_close_back_is_rejection():
    an = an_of([22000, 22001, 21999, 22000, 22014, 22003, 21999, 21998])
    res = auction.compute(an, prev_day_1m())
    rej = [e for e in res["events"] if e["kind"] == "AUCTION_REJECT" and e["ref"] == "pVAH"]
    assert rej and rej[0]["i"] == 5 and rej[0]["dir"] == "bear"
    assert "Failed auction" in res["read"]["decision"]["text"]


def test_wick_probe_rejected():
    an = an_of([22000, 22001, 21999, 22002, 22001], wicks={3: (22026, 21995)})
    res = auction.compute(an, prev_day_1m())
    assert ("AUCTION_REJECT", "PDH") in kinds(res)
    assert any(e.get("probe") for e in res["events"])


def test_breakout_without_follow_through():
    an = an_of([22000, 22001, 21999, 22000, 22013, 22013, 22014, 22013, 22014, 22013, 22014])
    res = auction.compute(an, prev_day_1m())
    assert ("AUCTION_NO_FOLLOW", "pVAH") in kinds(res)


def test_balance_box_and_wait_decision():
    an = an_of([22000, 22002, 21999, 22001, 22000, 22002, 21999, 22001, 22000, 22002])
    res = auction.compute(an, [])
    assert res["boxes"] and res["boxes"][-1]["active"]
    assert "In balance" in res["read"]["decision"]["text"]


def test_no_look_ahead():
    path = [22000, 22001, 21999, 22000, 22014, 22020, 22028, 22035, 22010, 21990, 21985]
    full = auction.compute(an_of(path), prev_day_1m())
    for n in range(4, len(path)):
        part = auction.compute(an_of(path[:n]), prev_day_1m())
        assert [e for e in full["events"] if e["i"] < n] == part["events"]


def test_effort_unavailable_is_honest():
    res = auction.compute(an_of([22000, 22001, 21999, 22000, 22014]), prev_day_1m())
    eff = res["read"]["effort"]
    assert eff["available"] is False and "Delta unavailable" in eff["text"] and eff["z"] is None
    assert "TPO" in res["profile_kind"]


def test_high_effort_low_result_flagged():
    an = an_of([22000, 22001, 21999, 22000, 22001, 22000])
    z = {int(an.bars.ts[-1]): 3.1}
    res = auction.compute(an, [], z, "csv")
    assert res["read"]["effort"]["high"] and "activity without control" in res["read"]["result"]["text"]
