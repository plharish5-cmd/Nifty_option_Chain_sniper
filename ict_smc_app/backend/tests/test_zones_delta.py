"""Zone registry + delta/footprint module: closed-candle guard, absorption rules, CSV alignment, no-dup zones."""
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("ICT_DATA_DIR", str(Path(__file__).resolve().parent / ".data"))

from oct8_data import five_min  # noqa: E402

from app import db  # noqa: E402
from app.delta import adapters, confluence, engine  # noqa: E402
from app.delta.adapters import CsvAdapter, CsvError, DeltaAdapter, parse_csv, store_csv  # noqa: E402
from app.engine import registry  # noqa: E402
from app.engine.analyze import analyze  # noqa: E402
from app.engine.primitives import Bars, atr  # noqa: E402
from app.engine.registry import Zone  # noqa: E402
from app.signals.playbook import evaluate  # noqa: E402

T0 = 1791442200          # Thu 8 Oct 2026 12:20 IST (a 5m candle start on the 09:15 grid)
STEP = 300


class FixedDelta(DeltaAdapter):
    """Deltas by bar index — test double."""
    name = "fixed"

    def __init__(self, deltas):
        self.d = deltas

    def status(self):
        return {"name": self.name, "available": True, "mock": False, "message": "test"}

    def get_delta(self, candle, tf):
        return self.d.get((int(candle["ts"]) - T0) // STEP)


def _an(spec):
    rows = [{"ts": T0 + STEP * k, "o": o, "h": h, "l": l, "c": c} for k, (o, h, l, c) in enumerate(spec)]
    b = Bars.from_rows(rows)
    return SimpleNamespace(tf="5m", bars=b, atr=atr(b, 14), liq=[])


def _baseline_bars(n=20):
    return [(95, 97, 93, 95.5) if k % 2 else (95.5, 97, 94, 95) for k in range(n)]


def _quiet(n=20):
    return {k: (100.0 if k % 2 else -100.0) for k in range(n)}


SUPPLY_FVG = Zone("FVG-test", "FVG", "5m", "SUPPLY", 100.0, 110.0, T0, "fresh", label="test supply FVG")


def _clear():
    for t in ("zones", "events", "delta_bars", "footprint_levels"):
        db.execute(f"DELETE FROM {t}")


# ── closed candles only ───────────────────────────────────────────────────────
def test_delta_events_ignore_forming_candle():
    spec = _baseline_bars() + [(104, 109, 103, 104.5)]
    an = _an(spec)
    d = {**_quiet(), 20: 5000.0}
    last = T0 + 20 * STEP
    forming = engine.compute(an, [SUPPLY_FVG], FixedDelta(d), now=last + 120)   # 2 min into the 5m candle
    assert all(e["ts"] != last for e in forming["events"])
    assert all(x["ts"] != last for x in forming["bars"])
    closed = engine.compute(an, [SUPPLY_FVG], FixedDelta(d), now=last + STEP)
    assert any(e["ts"] == last and e["event_type"] == "DELTA_AGGRESSION" for e in closed["events"])


def test_unavailable_adapter_degrades_gracefully():
    an = _an(_baseline_bars())
    r = engine.compute(an, [SUPPLY_FVG], adapters.FootprintFeedAdapter(), now=2e9)
    assert r["available"] is False and r["events"] == [] and "unavailable" in r["message"].lower()
    sigs = confluence.apply([SimpleNamespace(setup="A", tf="5m", ts=T0, side="short", ctx={})], r)
    assert sigs[0].confidence == 0.5 and "unavailable" in sigs[0].confluence[0].lower()


# ── absorption ────────────────────────────────────────────────────────────────
def test_absorption_rule1_high_delta_low_progress():
    an = _an(_baseline_bars() + [(104, 109, 103, 104.5)])     # inside supply FVG, body 0.5 of a 6-pt range
    r = engine.compute(an, [SUPPLY_FVG], FixedDelta({**_quiet(), 20: 5000.0}), now=2e9)
    ab = [e for e in r["events"] if e["event_type"] == "DELTA_ABSORPTION" and e["rule"] == "rule1_low_progress"]
    assert len(ab) == 1 and ab[0]["direction"] == "bear" and ab[0]["zone_id"] == "FVG-test"
    assert "absorbed by sellers" in ab[0]["explanation"]


def test_absorption_rule1_needs_a_zone_and_low_progress():
    strong = _an(_baseline_bars() + [(100.5, 108.6, 100.4, 108.5)])     # big body, closes at the high
    r = engine.compute(strong, [SUPPLY_FVG], FixedDelta({**_quiet(), 20: 5000.0}), now=2e9)
    assert not [e for e in r["events"] if e["rule"] == "rule1_low_progress"]
    nozone = _an(_baseline_bars() + [(84, 89, 83, 84.5)])
    r = engine.compute(nozone, [SUPPLY_FVG], FixedDelta({**_quiet(), 20: 5000.0}), now=2e9)
    assert not [e for e in r["events"] if e["event_type"] == "DELTA_ABSORPTION"]


def test_absorption_rule2_failure_to_continue():
    base = _baseline_bars()
    entry = (99, 108, 99, 107.5)                              # enters the zone, strong body → no rule 1
    fail = _an(base + [entry, (107.5, 108.5, 105, 106)])     # next close 106 < 107.5 + follow
    r = engine.compute(fail, [SUPPLY_FVG], FixedDelta({**_quiet(), 20: 5000.0, 21: 50.0}), now=2e9)
    ab = [e for e in r["events"] if e["rule"] == "rule2_no_follow_through"]
    assert len(ab) == 1 and ab[0]["ts"] == T0 + 21 * STEP and ab[0]["direction"] == "bear"
    assert not [e for e in r["events"] if e["rule"] == "rule1_low_progress"]
    go = _an(base + [entry, (107.5, 116, 107, 115)])          # follow-through → no absorption
    r = engine.compute(go, [SUPPLY_FVG], FixedDelta({**_quiet(), 20: 5000.0, 21: 50.0}), now=2e9)
    assert not [e for e in r["events"] if e["rule"] == "rule2_no_follow_through"]
    early = engine.compute(fail, [SUPPLY_FVG], FixedDelta({**_quiet(), 20: 5000.0, 21: 50.0}),
                           now=T0 + 21 * STEP + 60)          # next candle still forming → rule 2 must wait
    assert not [e for e in early["events"] if e["rule"] == "rule2_no_follow_through"]


# ── CSV adapter: schema + IST alignment ───────────────────────────────────────
def test_csv_timestamp_alignment_policies():
    t915 = 1791431100                                         # 8 Oct 2026 09:15 IST
    text = ("timestamp,delta\n"
            "2026-10-08 09:15,120\n"                          # naive → IST
            "2026-10-08T03:46:00Z,-80\n"                      # UTC → 09:16 IST
            f"{(t915 + 120) * 1000},40\n"                     # epoch ms → 09:17
            "2026-10-08 09:18:30,10\n"                        # off-grid → floored to 09:18
            "2026-10-08 08:00,999\n"                          # pre-open → rejected
            "2026-10-08 15:30,999\n")                         # 15:30 is the close, not a candle start → rejected
    p = parse_csv(text, bar_seconds=60)
    assert sorted(p["bars"]) == [t915, t915 + 60, t915 + 120, t915 + 180]
    assert p["bars"][t915 + 60][2] == -80
    rep = p["report"]
    assert rep["floored"] == 1 and rep["outside_session"] == 2 and rep["accepted"] == 4
    with pytest.raises(CsvError):
        parse_csv("timestamp,delta\n2026-10-08 09:18:30,10\n", bar_seconds=60, misaligned="reject")
    p5 = parse_csv("timestamp,buy_vol,sell_vol\n2026-10-08 09:20,500,200\n", bar_seconds=300, ts_is="end")
    assert list(p5["bars"]) == [t915] and p5["bars"][t915][2] == 300      # end-stamped 09:20 → candle 09:15
    with pytest.raises(CsvError):
        parse_csv("time,price\n2026-10-08 09:15,1\n")


def test_csv_adapter_coverage_and_levels():
    _clear()
    t915 = 1791431100
    store_csv(parse_csv("timestamp,delta\n" + "".join(f"{t915 + 60 * k},{10 * (k + 1)}\n" for k in range(5)), 60))
    store_csv(parse_csv(f"timestamp,price,buy_vol,sell_vol\n{t915 + 300},22000,30,10\n{t915 + 300},22000.5,5,25\n", 60))
    a = CsvAdapter()
    assert a.get_delta({"ts": t915}, "5m") == 150                         # 5 of 5 minutes
    assert a.get_delta({"ts": t915 + 300}, "5m") is None                  # 1 of 5 minutes < min_coverage
    assert a.get_delta({"ts": t915 + 300}, "1m") == 0                     # Σ levels: (30-10)+(5-25)
    lv = a.get_footprint_levels({"ts": t915 + 300}, "1m")
    assert [(x.price, x.delta) for x in lv] == [(22000.0, 20.0), (22000.5, -20.0)]
    _clear()


# ── zone registry ─────────────────────────────────────────────────────────────
def test_registry_no_duplicates_after_reconnect_backfill():
    _clear()
    rows = five_min()
    an = analyze(rows, "5m")
    win = (int(an.bars.ts[0]), int(an.bars.ts[-1]))
    first = registry.reconcile("5m", registry.build(an), win)
    n = len(registry.load("5m"))
    assert n == len(first) and len({z.id for z in first}) == n
    again = analyze([dict(r) for r in rows], "5m")                        # the REST re-download of the same day
    registry.reconcile("5m", registry.build(again), win, after_repair=True)
    assert len(registry.load("5m")) == n
    _clear()


def test_registry_forward_only_and_void():
    _clear()
    an = analyze(five_min(), "5m")
    win = (int(an.bars.ts[0]), int(an.bars.ts[-1]))
    zs = registry.reconcile("5m", registry.build(an), win)
    fvg = next(z for z in zs if z.type == "FVG" and z.mitigated_at is not None)
    older = Zone(**{**fvg.to_dict(), "state": "fresh", "mitigated_at": None, "extra": {}})   # a recompute that 'forgot'
    registry.reconcile("5m", [older], win)
    s = registry.load("5m")[fvg.id]
    assert s.state == fvg.state and s.mitigated_at == fvg.mitigated_at       # never rewritten backwards
    keep = [z for z in registry.build(an) if z.id != fvg.id]                  # corrected candles: gap gone
    registry.reconcile("5m", keep, win, after_repair=False)
    assert registry.load("5m")[fvg.id].state == fvg.state                     # only a REST repair can void
    registry.reconcile("5m", keep, win, after_repair=True)
    v = registry.load("5m")[fvg.id]
    assert v.state == "void" and "void_reason" in v.extra
    assert len(registry.load("5m")) == len(zs)                                # voided, never deleted
    _clear()


def test_confluence_never_changes_trades():
    an = analyze(five_min(), "5m")
    plain = evaluate(an, None)
    zs = registry.build(an)
    scored = confluence.apply(evaluate(an, None), engine.compute(an, zs, adapters.MockAdapter(), now=2e9))
    key = lambda s: (s.key, s.status, s.entry, s.sl, s.tp, s.reasons)        # noqa: E731
    assert [key(s) for s in plain] == [key(s) for s in scored]
    assert all(s.confidence is not None for s in scored if s.setup in ("A", "C"))
    assert all(s.confidence is None for s in scored if s.setup == "B")


def test_rule2_event_reports_entry_candle_delta():
    an = _an(_baseline_bars() + [(99, 108, 99, 107.5), (107.5, 108.5, 105, 106)])
    r = engine.compute(an, [SUPPLY_FVG], FixedDelta({**_quiet(), 20: 5000.0, 21: 50.0}), now=2e9)
    ab = next(e for e in r["events"] if e["rule"] == "rule2_no_follow_through")
    assert ab["delta"] == 5000.0 and ab["next_delta"] == 50.0 and ab["entry_ts"] == T0 + 20 * STEP


def test_flat_stalled_candle_never_absorbs():
    an = _an(_baseline_bars() + [(104.0, 104.0, 104.0, 104.0)])            # feed stall inside the zone
    r = engine.compute(an, [SUPPLY_FVG], FixedDelta({**_quiet(), 20: 5000.0}), now=2e9)
    assert not [e for e in r["events"] if e["event_type"] in ("DELTA_ABSORPTION", "FOOTPRINT_IMBALANCE")]
