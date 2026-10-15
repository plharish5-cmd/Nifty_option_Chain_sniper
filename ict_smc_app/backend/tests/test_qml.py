import numpy as np
from app.engine.primitives import Bars
from app.engine.qml_sbr import compute, line_pivots


def _bars(closes, wick=1.0):
    rows, prev = [], closes[0]
    for k, c in enumerate(closes):
        o = prev
        rows.append({"ts": 1_700_000_000 + 300 * k, "o": o, "h": max(o, c) + wick, "l": min(o, c) - wick, "c": c})
        prev = c
    return Bars.from_rows(rows)


def _run(closes, **kw):
    b = _bars(closes, **kw)
    return compute(b, np.full(len(b), 10.0), 2, lambda i: int(b.ts[i]), lambda ts: str(ts))


def test_line_pivots_use_closes_and_alternate():
    c = np.array([1, 2, 5, 2, 1, 0, 1, 3, 6, 3, 2], float)
    P = line_pivots(c, 2)
    assert [p["kind"] for p in P] == ["H", "L", "H"] and P[0]["price"] == 5 and P[2]["price"] == 6


def test_bearish_qml_armed_then_triggered():
    # H 120 → L 100 → HH 130 → close below 100 (BOS) → pull back to 120 and close red below it → TRIGGERED
    up = [100, 105, 110, 115, 120, 115, 110, 105, 100, 105, 110, 120, 125, 130, 125, 120, 110, 100, 95, 92, 96, 104,
          112, 119.5, 114, 108, 102]
    r = _run(up)
    q = [s for s in r["setups"] if s["kind"] == "QML"]
    assert q and q[-1]["dir"] == "bear" and q[-1]["level"] == 120
    assert q[-1]["status"] == "TRIGGERED" and q[-1]["sl"] > 130 and q[-1]["tp2"] < q[-1]["entry"]
    assert any(m["kind"] == "QML_TRIG" for m in r["markers"])


def test_bearish_qml_invalid_on_displacement_through_level():
    up = [100, 105, 110, 115, 120, 115, 110, 105, 100, 105, 110, 120, 125, 130, 125, 120, 110, 100, 95, 92, 96, 104,
          112, 126, 131, 135]
    q = [s for s in _run(up)["setups"] if s["kind"] == "QML" and s["dir"] == "bear"]
    assert q and q[-1]["status"] == "INVALID"


def test_rbs_triggered_on_retest_of_broken_peak():
    c = [100, 104, 108, 112, 110, 106, 104, 106, 110, 116, 120, 118, 114, 112.5, 115, 119]
    s = [x for x in _run(c)["setups"] if x["kind"] == "RBS"]
    assert s and s[-1]["level"] == 112 and s[-1]["status"] in ("TRIGGERED", "TP1 HIT", "TP2 HIT")
