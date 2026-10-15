import os
from pathlib import Path

os.environ.setdefault("ICT_DATA_DIR", str(Path(__file__).resolve().parent / ".data"))

from app import db  # noqa: E402
from app.engine import footprints as fp  # noqa: E402

T0 = 1_790_000_100 - (1_790_000_100 + 19800 - 9 * 3600 - 15 * 60) % 86400 + 3 * 300   # a 09:30 IST start


def _seed(minutes, levels):
    db.execute("DELETE FROM footprint_levels WHERE inst='FUTQR' AND ts>=? AND ts<?", (T0, T0 + 86400))
    db.execute("DELETE FROM delta_bars WHERE inst='FUTQR' AND ts>=? AND ts<?", (T0, T0 + 86400))
    for m, lv in zip(minutes, levels):
        b = sum(x[1] for x in lv); s = sum(x[2] for x in lv)
        db.execute("INSERT OR REPLACE INTO delta_bars (inst, ts, secs, buy, sell, delta, src) VALUES ('FUTQR',?,60,?,?,?,'t')", (m, b, s, b - s))
        for p, bv, sv in lv:
            db.execute("INSERT OR REPLACE INTO footprint_levels (inst, ts, price, buy, sell) VALUES ('FUTQR',?,?,?,?)", (m, p, bv, sv))


def test_ladder_poc_imbalance_and_partial():
    mins = [T0 + 60 * i for i in range(15)]                 # three 5m candles
    rows = [{"ts": m, "o": 100 + i, "h": 102 + i, "l": 99 + i, "c": 101 + i, "closed": True} for i, m in enumerate(mins)]
    lv = [[(100.0, 400.0, 50.0), (105.0, 900.0, 40.0), (110.0, 600.0, 30.0)] for _ in range(10)] + [[] for _ in range(5)]
    _seed(mins, lv)
    out = fp.compute(rows + [{"ts": T0 + 900, "o": 1, "h": 1, "l": 1, "c": 1, "closed": True}], "5m", 16, 5.0)
    assert out["available"] and out["estimated"]
    b0 = out["bars"][0]
    assert b0["poc"] == 105.0 and b0["delta"] > 0 and any(r["bi"] for r in b0["rows"])
    assert out["bars"][2]["rows"] == [] and "no recorded ticks" in out["bars"][2]["read"]


def test_no_data_is_reported_not_invented():
    mins = [T0 + 40000 + 60 * i for i in range(10)]
    rows = [{"ts": m, "o": 1, "h": 2, "l": 0, "c": 1, "closed": True} for m in mins]
    out = fp.compute(rows, "5m", 16, 5.0)
    assert not out["available"] and all(b["rows"] == [] for b in out["bars"])
