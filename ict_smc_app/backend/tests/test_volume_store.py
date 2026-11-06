"""
test_volume_store.py — the maths must be testable with no DB and no market.

The tests that matter are the ones proving slot-normalisation actually removes
the intraday U-shape, and that the thing fails closed on thin history instead of
returning a confident 1.0.
"""
import time

import pytest

from app.engine import volume_store as vs


def _row(day, t, vol):
    return {"ts": 0, "session_date": day, "session_time": t, "volume": vol}


# ─────────────────────────────────────────────────────────────────────────────
# SLOT BUCKETING
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("t,tf,expected", [
    ("12:47:00", 15, "12:45"),
    ("09:15:00", 15, "09:15"),
    ("15:29:00", 15, "15:15"),
    ("10:04:00", 5, "10:00"),
    ("13:31:00", 30, "13:30"),
    ("11:59:00", 60, "11:00"),
])
def test_slot_key(t, tf, expected):
    assert vs.slot_key(t, tf) == expected


def test_slot_key_survives_garbage():
    assert vs.slot_key("", 15) == "00:00"
    assert vs.slot_key("not a time", 15) == "00:00"


# ─────────────────────────────────────────────────────────────────────────────
# THE POINT OF THE WHOLE MODULE
# ─────────────────────────────────────────────────────────────────────────────

def test_slot_normalisation_removes_the_intraday_u_shape():
    """A midday bar at its NORMAL midday level must read ~1.0, not 'low'.

    With a flat SMA across the day it would look like a volume drought simply
    because midday is quiet. This is the whole reason the module exists.
    """
    rows = []
    for day in ("2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06"):
        rows.append(_row(day, "09:15:00", 100000))   # heavy open
        rows.append(_row(day, "12:45:00", 10000))    # quiet midday
        rows.append(_row(day, "15:15:00", 90000))    # heavy close
    base = vs.build_slot_baseline(rows, 15)
    midday = vs.slot_vsr(10000, base["12:45"])
    assert midday["ok"] is True
    assert midday["ratio"] == pytest.approx(1.0, abs=0.01)
    # And a genuine midday surge is caught, even though it is far below the open.
    assert vs.slot_vsr(25000, base["12:45"])["ratio"] == pytest.approx(2.5, abs=0.01)


def test_real_surge_is_detected():
    hist = [1000, 1100, 900, 1050, 1000, 950]
    assert vs.slot_vsr(2000, hist)["ratio"] == pytest.approx(2.0, abs=0.05)


def test_median_baseline_resists_one_expiry_spike():
    """One expiry-day spike must not suppress every later reading."""
    hist = [1000, 1000, 1000, 1000, 50000]  # last is an expiry blowout
    r = vs.slot_vsr(1000, hist)
    assert r["median"] == pytest.approx(1000, abs=1)
    assert r["ratio"] == pytest.approx(1.0, abs=0.01)


def test_fails_closed_on_thin_history():
    """Never a fabricated 1.0 when there is not enough baseline."""
    r = vs.slot_vsr(1000, [1000, 1100])
    assert r["ok"] is False
    assert r["ratio"] is None
    assert "need" in r["reason"]


def test_fails_closed_on_zero_baseline():
    r = vs.slot_vsr(1000, [0, 0, 0, 0, 0, 0])
    assert r["ok"] is False


def test_expiry_sessions_can_be_excluded_from_baseline():
    rows = [_row("2026-11-02", "12:45:00", 1000),
            _row("2026-11-03", "12:45:00", 1000),
            _row("2026-11-04", "12:45:00", 99000)]  # expiry
    base = vs.build_slot_baseline(rows, 15, exclude_sessions=["2026-11-04"])
    assert base["12:45"] == [1000, 1000]


# ─────────────────────────────────────────────────────────────────────────────
# ROLLOVER
# ─────────────────────────────────────────────────────────────────────────────

def test_combined_sums_front_and_next_month():
    """Front-month volume collapses before expiry as it migrates to the next
    contract. Summing both stops that reading as a participation collapse."""
    front = [_row("2026-11-20", "12:45:00", 200)]
    nxt = [_row("2026-11-20", "12:45:00", 1800)]
    front[0]["ts"] = nxt[0]["ts"] = 1700000000
    merged = vs.combined(front, nxt)
    assert len(merged) == 1
    assert merged[0]["volume"] == 2000


def test_combined_handles_one_side_missing():
    front = [dict(_row("2026-11-05", "12:45:00", 500), ts=1)]
    assert vs.combined(front, [])[0]["volume"] == 500
    assert vs.combined([], front)[0]["volume"] == 500
    assert vs.combined([], []) == []


# ─────────────────────────────────────────────────────────────────────────────
# COVERAGE — must report gaps honestly
# ─────────────────────────────────────────────────────────────────────────────

def test_coverage_reports_partial_sessions():
    rows = [_row("2026-11-05", "09:{:02d}:00".format(m), 10) for m in range(0, 50)]
    rows += [_row("2026-11-04", "09:{:02d}:00".format(m), 10) for m in range(0, 2)]
    cov = vs.coverage_from_rows(rows)
    assert cov["n_sessions"] == 2
    assert cov["first_session"] == "2026-11-04"
    assert cov["last_session"] == "2026-11-05"
    # both are far below a 375-minute session, so both must be flagged
    assert set(cov["partial_sessions"]) == {"2026-11-04", "2026-11-05"}
    assert cov["mean_coverage_pct"] < 10


def test_coverage_of_nothing_is_not_an_error():
    cov = vs.coverage_from_rows([])
    assert cov["n_sessions"] == 0 and cov["mean_coverage_pct"] == 0.0


# ─────────────────────────────────────────────────────────────────────────────
# LABELS — thresholds come from config, never baked in
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("ratio,expected", [
    (2.4, "SURGE"), (1.3, "ELEVATED"), (1.0, "NORMAL"), (0.4, "DRY"), (None, "UNKNOWN"),
])
def test_classify_vsr(ratio, expected):
    assert vs.classify_vsr(ratio) == expected


def test_thresholds_are_caller_supplied():
    assert vs.classify_vsr(1.3, {"surge": 1.25}) == "SURGE"
