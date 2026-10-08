"""pytest — run from backend/:  .venv/bin/python -m pytest -q"""
import os
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("ICT_DATA_DIR", str(Path(__file__).resolve().parent / ".data"))

from oct8_data import fifteen_min, five_min  # noqa: E402

from app.dhan.feed import decode  # noqa: E402
from app.engine.analyze import analyze, to_json  # noqa: E402
from app.engine.primitives import Bars, pivots  # noqa: E402
from app.market.candles import resample  # noqa: E402
from app.signals.playbook import evaluate  # noqa: E402
from app.timeutil import bucket_start, hm  # noqa: E402

T0 = five_min()[0]["ts"]


def _rows(spec, t0=T0, step=300):
    return [{"ts": t0 + step * k, "o": o, "h": h, "l": l, "c": c, "v": 0, "closed": True}
            for k, (o, h, l, c) in enumerate(spec)]


# ── worked example: Thu 8 Oct 2026 ───────────────────────────────────────────
def test_oct8_structure_and_liquidity():
    an = analyze(five_min(), "5m")
    ev = [(hm(e.ts), e.kind, e.dir) for e in an.events]
    assert ("14:00", "BOS", "bear") in ev
    assert ("14:35", "CHOCH", "bull") in ev          # the close above 22,244.7 that cancelled the 14:30 sweep
    assert ("15:05", "CHOCH", "bear") in ev
    liq = [(hm(x.ts), x.kind, round(x.level, 1)) for x in an.liq]
    assert ("14:30", "SWEEP", 22244.7) in liq
    assert ("15:10", "FALSE_BREAKOUT", 22190.0) in liq


def test_oct8_no_tiny_fvg_and_ote():
    an = analyze(five_min(), "5m")
    assert all(g.size >= 3.0 for g in an.fvgs)        # the 0.3-pt 'killzone' from the old tab is gone
    r = an.rng
    assert r["dir"] == "bear" and abs(r["high"] - 22255.9) < 0.01 and abs(r["low"] - 22179.9) < 0.01
    assert 22226 < r["ote"]["0.618"] < 22228 and r["zone"] == "premium"


def test_oct8_playbook_gives_no_trade():
    sigs = evaluate(analyze(five_min(), "5m"), analyze(fifteen_min(), "15m"))
    assert not [s for s in sigs if s.status == "TRADE"]
    c = [s for s in sigs if s.setup == "C"]
    assert c and c[0].status == "SKIP" and any("outside the trade windows" in x for x in c[0].reasons)


# ── synthetic textbook setup A (short) ───────────────────────────────────────
SHORT_A = [
    (22000, 22010, 21995, 22005), (22005, 22020, 22000, 22015), (22015, 22030, 22010, 22025),
    (22025, 22050, 22020, 22045),                    # swing high 22,050 (confirmed 2 bars later)
    (22045, 22046, 22030, 22035), (22035, 22040, 22025, 22030),
    (22030, 22035, 22015, 22020),                    # swing low 22,015
    (22020, 22038, 22018, 22036), (22036, 22045, 22030, 22040),
    (22040, 22058, 22035, 22045),                    # SWEEP: wick 22,058 above 22,050, close back below
    (22045, 22047, 22020, 22022),                    # displacement down
    (22022, 22024, 22005, 22008),                    # MSS (close < 22,015) + bearish FVG 22,024–22,035
    (22008, 22035, 22006, 22033),                    # retest into the FVG, closes in premium → entry 22,033
    (22027, 22028, 21990, 21992),
]


def _window_t0():
    # 13:30 IST on 8 Oct → setup lands inside the 13:30–14:45 trade window
    return bucket_start(T0 + 20 * 60, "5m")


def test_setup_a_fires_on_textbook_short():
    rows = _rows(SHORT_A[:13], t0=_window_t0())
    sigs = [s for s in evaluate(analyze(rows, "5m"), None) if s.setup == "A"]
    assert sigs, "setup A should fire"
    s = sigs[-1]
    assert s.side == "short" and abs(s.entry - 22033) < 1e-6 and abs(s.sl - 22060) < 1e-6
    assert s.status == "TRADE", s.reasons
    assert abs(s.risk - 27) < 1e-6 and s.rr >= 1.5


def test_setup_a_rejects_entry_in_discount():
    spec = list(SHORT_A[:13])
    spec[12] = (22008, 22030, 22006, 22027)          # closes below the raid range's 50 % → not premium
    s = [x for x in evaluate(analyze(_rows(spec, t0=_window_t0()), "5m"), None) if x.setup == "A"][-1]
    assert s.status == "SKIP" and "entry not in premium" in s.reasons


def test_setup_a_cancelled_by_close_above_sweep():
    spec = list(SHORT_A[:13])
    spec[10] = (22045, 22056, 22040, 22052)          # closes above the swept 22,050 → raid failed
    sigs = [s for s in evaluate(analyze(_rows(spec, t0=_window_t0()), "5m"), None) if s.setup == "A"]
    assert not sigs


def test_setup_a_needs_closed_retest():
    rows = _rows(SHORT_A[:12], t0=_window_t0())      # retest candle not closed yet → nothing
    assert not [s for s in evaluate(analyze(rows, "5m"), None) if s.setup == "A"]


# ── plumbing ─────────────────────────────────────────────────────────────────
def test_pivots_need_confirmation():
    b = Bars.from_rows(_rows([(1, 2, 0, 1), (1, 3, 0, 2), (2, 5, 1, 4), (4, 4.5, 3, 3), (3, 4, 2, 3)]))
    p = [s for s in pivots(b, 2) if s.kind == "H"]
    assert p and p[0].i == 2 and p[0].confirm_i == 4


def test_resample_aligns_to_0915():
    t915 = bucket_start(T0, "1m") - (bucket_start(T0, "1m") - bucket_start(T0 - 4 * 3600, "1h")) % 3600
    rows = [{"ts": T0 + 60 * k, "o": 1, "h": 2, "l": 0.5, "c": 1, "v": 0} for k in range(30)]
    out = resample(rows, "15m", now=T0 + 3600)
    assert all(((r["ts"] + 19800) % 900) == ((9 * 3600 + 15 * 60) % 900) for r in out)
    assert all(r["closed"] for r in out)


def test_feed_decode_multi_packet():
    msg = (struct.pack("<BHBI", 2, 16, 0, 13) + struct.pack("<fi", 22231.8, 1791453300)
           + struct.pack("<BHBI", 6, 16, 0, 13) + struct.pack("<fi", 22216.2, 0)
           + struct.pack("<BHBI", 50, 10, 0, 0) + struct.pack("<h", 807))
    p = decode(msg)
    assert [x["code"] for x in p] == [2, 6, 50]
    assert abs(p[0]["ltp"] - 22231.8) < 0.01 and p[0]["sid"] == "13" and p[0]["seg"] == "IDX_I"
    assert p[2]["reason"] == 807


def test_to_json_is_chart_ready():
    j = to_json(analyze(five_min(), "5m"))
    assert j["zones"] and j["markers"] and j["range"]
    assert all(z["from"] <= z["to"] for z in j["zones"])
