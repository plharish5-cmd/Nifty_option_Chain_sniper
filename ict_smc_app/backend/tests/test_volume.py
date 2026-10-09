"""Real-volume layer: futures resolver, basis mapping, volume profile, VWAP, RVOL, recorder, options activity."""
import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("ICT_DATA_DIR", str(Path(__file__).resolve().parent / ".data"))

from datetime import date  # noqa: E402

from app import db  # noqa: E402
from app.delta import adapters  # noqa: E402
from app.delta.recorder import FutRecorder  # noqa: E402
from app.engine import volume  # noqa: E402
from app.engine.primitives import Bars, atr  # noqa: E402
from app.market import activity, futures  # noqa: E402

D7, D8 = 1791344700, 1791431100        # 7 / 8 Oct 2026 09:15 IST

MASTER = """SEM_EXM_EXCH_ID,SEM_SEGMENT,SEM_SMST_SECURITY_ID,SEM_INSTRUMENT_NAME,SEM_TRADING_SYMBOL,SEM_EXPIRY_DATE
NSE,D,35001,FUTIDX,NIFTY-Oct2026-FUT,2026-10-27 14:30:00
NSE,D,35002,FUTIDX,NIFTY-Nov2026-FUT,2026-11-24 14:30:00
NSE,D,35003,FUTIDX,BANKNIFTY-Oct2026-FUT,2026-10-27 14:30:00
NSE,D,35004,FUTIDX,NIFTYNXT50-Oct2026-FUT,2026-10-27 14:30:00
NSE,D,35005,FUTIDX,NIFTY-Sep2026-FUT,2026-09-29 14:30:00
NSE,D,40100,OPTIDX,NIFTY-Oct2026-22000-CE,2026-10-13 14:30:00
"""


def test_scrip_master_only_nifty_futures_nearest_first():
    rows = futures.parse_master(MASTER, today=date(2026, 10, 10))
    assert [r["sid"] for r in rows] == ["35001", "35002"]


def test_mapped_future_sits_on_index_prices():
    spot = [{"ts": D8, "o": 22000, "h": 22010, "l": 21995, "c": 22005}]
    fut = [{"ts": D8, "o": 22080, "h": 22092, "l": 22075, "c": 22086, "v": 1000}]
    m = futures.mapped(spot, fut)[0]
    assert m["basis"] == 81 and m["c"] == 22005 and m["h"] == 22011 and m["v"] == 1000


def test_volume_profile_poc_value_and_nodes():
    rows = []
    for k in range(60):                                  # heavy trade at 22000–22005, thin at 22030, heavy at 22050
        rows.append({"ts": D8 + 60 * k, "o": 22001, "h": 22004, "l": 22001, "c": 22002, "v": 5000})
    for k in range(20):
        rows.append({"ts": D8 + 3600 + 60 * k, "o": 22051, "h": 22054, "l": 22051, "c": 22052, "v": 10000})
    rows.append({"ts": D8 + 7200, "o": 22005, "h": 22050, "l": 22005, "c": 22050, "v": 100})
    p = volume.volume_profile(rows, 5.0, 0.70)
    assert p["poc"] == 22002.5 and p["val"] <= 22001 and p["vah"] >= 22005
    assert any(22045 <= x <= 22055 for x in p["hvn"]) and any(22010 <= x <= 22045 for x in p["lvn"])


def test_vwap_and_sigma():
    rows = [{"ts": D8, "h": 101, "l": 99, "c": 100, "v": 100}, {"ts": D8 + 60, "h": 111, "l": 109, "c": 110, "v": 300}]
    s = volume.vwap_series(rows)
    assert s[-1]["vwap"] == 107.5 and abs(s[-1]["sd"] - 4.33) < 0.01


def test_rvol_time_of_day_no_look_ahead():
    rows = []
    for d, mult in ((D7 - 86400, 1), (D7, 1), (D8, 3)):
        for k in range(15):
            rows.append({"ts": d + 60 * k, "o": 1, "h": 1, "l": 1, "c": 1, "v": 100 * mult})
    rv = volume.rvol(rows, "5m")
    assert rv[D8]["rvol"] == 3.0 and rv[D8]["method"] == "time-of-day"
    assert rv[D7 - 86400]["rvol"] is None                     # nothing earlier to compare with
    cut = volume.rvol([r for r in rows if r["ts"] < D8 + 300], "5m")
    assert cut[D8] == rv[D8]                                  # later bars never change an earlier RVOL


def test_annotate_marks_volume_backed_bos():
    from app.engine.analyze import analyze, to_json
    rows, p = [], 22000.0
    for k in range(40):
        step = 6 if k in (30, 31) else (-2 if k % 3 else 3)
        rows.append({"ts": D8 + 300 * k, "o": p, "h": max(p, p + step) + 1, "l": min(p, p + step) - 1, "c": p + step})
        p += step
    an = analyze(rows, "5m")
    j = to_json(an)
    rv = {int(an.bars.ts[i]): {"rvol": 2.4 if i in (30, 31) else 1.0} for i in range(len(an.bars))}
    volume.annotate(j, an, rv)
    for m in j["markers"]:
        if m.get("rvol") == 2.4:
            assert "V2.4" in m["text"]


def test_recorder_quote_and_tick_rule(tmp_path=None):
    spot = {"v": 22000.0}
    r = FutRecorder(lambda: spot["v"])
    t = float(D8 + 5)
    r.on_packet({"ltp": 22080, "volume": 1000, "bid": 22079.5, "ask": 22080.5}, t)
    assert r.on_packet({"ltp": 22080.5, "volume": 1100, "bid": 22080, "ask": 22081}, t + 1) == "buy"      # at the ask
    assert r.on_packet({"ltp": 22080.0, "volume": 1175, "bid": 22079.5, "ask": 22080.5}, t + 2) == "sell"  # at the bid
    assert r.on_packet({"ltp": 22080.2, "volume": 1200, "bid": 22079.5, "ask": 22081}, t + 3) == "buy"     # up-tick
    rec = r.close_minute(D8)
    assert rec["buy"] == 125 and rec["sell"] == 75 and rec["v"] == 200
    a = adapters.make("fut_quote_rule")
    assert a.get_delta({"ts": D8}, "1m") == 50.0
    assert a.get_delta({"ts": D8}, "1m", instrument=SimpleNamespace(key="X")) is None
    assert a.status()["estimated"] is True
    lv = a.get_footprint_levels({"ts": D8}, "1m")
    assert lv and all(21990 <= x.price <= 22010 for x in lv)         # on INDEX prices, not futures prices


def test_options_activity_delta_weighted_and_live_only():
    db.execute("DELETE FROM opt_activity")
    A = activity.Activity()
    ch = lambda ts, v1, v2: {"ts": ts, "atm": 22000, "expiry": "E", "rows": [   # noqa: E731
        {"strike": 22000, "ce": {"vol": v1, "delta": 0.5}, "pe": {"vol": v1, "delta": -0.5}},
        {"strike": 22500, "ce": {"vol": v2, "delta": 0.05}, "pe": {"vol": v2, "delta": -0.95}}]}
    A.ingest(ch(D8 + 10, 1000, 1000), force=True)           # first sight: adds nothing
    A.ingest(ch(D8 + 20, 1200, 1100), force=True)
    r = activity.rows_since(D8)[0]
    assert r["raw"] == 600 and r["ce"] == 0.5 * 200 + 0.05 * 100 and r["pe"] == 0.5 * 200 + 0.95 * 100
