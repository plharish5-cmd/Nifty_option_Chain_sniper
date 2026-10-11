"""Futures OI build-up and IV / delta tracking."""
import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("ICT_DATA_DIR", str(Path(__file__).resolve().parent / ".data"))

from app import db  # noqa: E402
from app.engine import oi as oi_mod  # noqa: E402
from app.engine.primitives import Bars, atr  # noqa: E402
from app.market import ivtrack  # noqa: E402

D8 = 1791431100


def an_of(closes):
    rows, pc = [], closes[0]
    for k, c in enumerate(closes):
        rows.append({"ts": D8 + 300 * k, "o": pc, "h": max(pc, c) + 2, "l": min(pc, c) - 2, "c": c})
        pc = c
    b = Bars.from_rows(rows)
    return SimpleNamespace(tf="5m", bars=b, atr=atr(b, 14))


def test_four_build_ups():
    an = an_of([22000, 22020, 22000, 22020, 22000])
    oi = {D8 + 240: 1e7, D8 + 540: 1.01e7, D8 + 840: 1.02e7, D8 + 1140: 1.01e7, D8 + 1440: 1.0e7}
    r = oi_mod.per_bar(an, oi)
    assert "build" not in r[D8]                                   # nothing before it to compare with
    assert [r[D8 + 300 * k]["build"] for k in (1, 2, 3, 4)] == ["long build-up", "short build-up", "short covering", "long unwinding"]


def test_small_change_is_flat_and_new_day_resets():
    an = an_of([22000, 22020])
    r = oi_mod.per_bar(an, {D8 + 240: 1e7, D8 + 540: 1e7 + 100})
    assert r[D8 + 300]["build"] == "flat"
    an2 = SimpleNamespace(tf="5m", bars=Bars.from_rows([{"ts": D8 - 86400, "o": 1, "h": 2, "l": 0, "c": 1},
                                                         {"ts": D8, "o": 1, "h": 30, "l": 0, "c": 25}]), atr=[1, 1])
    r2 = oi_mod.per_bar(an2, {D8 - 86400 + 240: 1e7, D8 + 240: 2e7})
    assert "build" not in r2[D8]                                  # no cross-day ΔOI (rollover / new day)


def test_summary_dominant():
    an = an_of([22000, 21980, 21960, 21940])
    oi = {D8 + 240 + 300 * k: 1e7 * (1 + 0.002 * k) for k in range(4)}
    s = oi_mod.summary(an, oi_mod.per_bar(an, oi))
    assert s["dominant"] == "short build-up" and s["bias"] == "bear" and s["net_doi"] > 0


def test_iv_track_atm_and_strikes_and_trend():
    db.execute("DELETE FROM iv_series")
    T = ivtrack.IVTrack()
    ch = lambda ts, iv, d: {"ts": ts, "atm": 22000.0, "rows": [   # noqa: E731
        {"strike": 22000.0, "ce": {"iv": iv, "delta": d, "ltp": 100}, "pe": {"iv": iv + 1, "delta": d - 1, "ltp": 90}}]}
    picks = {"CE": {"strike": 22000.0, "key": "K-CE"}, "PE": None}
    for k in range(7):
        T.ingest(ch(D8 + 600 * k, 14.0 - 0.3 * k, 0.55 - 0.03 * k), picks, force=True)
    atm = ivtrack.series("ATM", D8)
    assert atm[0]["iv"] == 14.5 and len(atm) == 7
    tr = ivtrack.trend(atm, 60)
    assert tr["word"] == "falling" and abs(tr["chg"] + 1.8) < 1e-6
    d = ivtrack.trend(ivtrack.series("K-CE", D8), 375, "delta")
    assert d["word"] == "falling"
    assert ivtrack.per_bar(atm, "15m")[D8 + 1800]["iv"] == round(14.0 - 0.3 * 4 + 0.5, 2)


def test_oi_line_ema_and_surge():
    closes = [22000 + 2 * k for k in range(30)]
    an = an_of(closes)
    oi = {}
    v = 1e7
    for k in range(30):
        v += 3000 if k != 25 else 40000                     # one candle adds OI far faster than normal
        oi[D8 + 300 * k + 240] = v
    line = oi_mod.oi_line(an, oi_mod.per_bar(an, oi))
    by = {x["ts"]: x for x in line}
    assert by[D8 + 300 * 25]["surge"] and by[D8 + 300 * 25]["surge_x"] >= 2
    assert not any(x["surge"] for x in line if x["ts"] != D8 + 300 * 25)
    assert by[D8 + 300 * 29]["ema"] < by[D8 + 300 * 29]["oi"]        # EMA lags a rising OI


def test_nifty_banknifty_compare():
    bear = {"dominant": "short build-up", "bias": "bear"}
    bull = {"dominant": "short covering", "bias": "bull"}
    assert oi_mod.compare(bear, bear, None, None)["verdict"] == "aligned"
    d = oi_mod.compare(bear, bull, None, None)
    assert d["verdict"] == "diverging" and "more likely to fail" in d["text"]
    assert oi_mod.compare(bear, None, None, None) is None
