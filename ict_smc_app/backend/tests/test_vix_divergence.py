"""Tests for the price / INDIA VIX context engine.

The two cases that carry information are the BROKEN inverse relationship: price up with VIX also up
(protection bought into strength), and price down with VIX down (fear draining out of a decline).
Everything else is either normal or unreadable, and unreadable must say so rather than guess -- the
same rule the institutional-levels placeholder bug taught at the cost of a near-miss.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("ICT_DATA_DIR", str(Path(__file__).resolve().parent / ".data"))

from app.engine import vix_divergence as vx  # noqa: E402

CFG = {"min_price_move_pts": 25.0, "min_vix_move_pct": 1.5, "match_tolerance_s": 300,
       "lookback_bars": 20, "enabled": True}
T0 = 1793240100  # 29 Oct 2026 09:15 IST


def series(values, step=180, start=T0):
    return [{"ts": start + i * step, "c": float(v)} for i, v in enumerate(values)]


def price(values, step=180, start=T0):
    return [{"ts": start + i * step, "o": float(v), "h": float(v) + 5, "l": float(v) - 5,
             "c": float(v)} for i, v in enumerate(values)]


def test_rally_with_falling_vix_is_normal():
    res = vx.compute_vix_divergence(price([22700 + i * 12 for i in range(20)]),
                                    series([16.5 - i * 0.07 for i in range(20)]), cfg=CFG)
    assert res["ok"] is True
    assert res["state"] == vx.NORMAL_RISK_ON
    assert res["price_move_pts"] > 0 and res["vix_move_pct"] < 0


def test_rally_with_RISING_vix_is_the_distribution_warning():
    """The case this engine exists for: price up, protection also being bought."""
    res = vx.compute_vix_divergence(price([22700 + i * 12 for i in range(20)]),
                                    series([15.0 + i * 0.05 for i in range(20)]), cfg=CFG)
    assert res["state"] == vx.DISTRIBUTION_WARNING
    assert res["price_move_pts"] > 0 and res["vix_move_pct"] > 0
    assert "hedged" in res["detail"]
    assert res["is_context_only"] is True, "must never present as an entry"


def test_decline_with_falling_vix_is_capitulation_watch():
    res = vx.compute_vix_divergence(price([22900 - i * 12 for i in range(20)]),
                                    series([19.0 - i * 0.08 for i in range(20)]), cfg=CFG)
    assert res["state"] == vx.CAPITULATION_WATCH
    assert res["price_move_pts"] < 0 and res["vix_move_pct"] < 0


def test_decline_with_rising_vix_is_normal():
    res = vx.compute_vix_divergence(price([22900 - i * 12 for i in range(20)]),
                                    series([15.0 + i * 0.09 for i in range(20)]), cfg=CFG)
    assert res["state"] == vx.NORMAL_RISK_OFF


def test_a_small_price_move_reads_nothing():
    res = vx.compute_vix_divergence(price([22900 + (i % 2) for i in range(20)]),
                                    series([15.0 + i * 0.09 for i in range(20)]), cfg=CFG)
    assert res["state"] == vx.FLAT
    assert "needs" in res["detail"]


def test_a_small_vix_move_is_not_a_warning():
    """VIX drifting a few basis points on a big rally is noise, not hedging."""
    res = vx.compute_vix_divergence(price([22700 + i * 12 for i in range(20)]),
                                    series([15.0 + i * 0.002 for i in range(20)]), cfg=CFG)
    assert res["state"] == vx.NORMAL_RISK_ON


# ── unavailability is a first-class answer, never a guess ────────────────────────────────────────

def test_no_vix_series_reports_unavailable():
    res = vx.compute_vix_divergence(price([22700 + i * 12 for i in range(20)]), [], cfg=CFG)
    assert res["ok"] is False
    assert res["state"] == vx.UNAVAILABLE
    assert res["available"] is False
    assert res["vix_from"] is None and res["vix_to"] is None
    assert "rather than assuming" in res["detail"]


def test_a_stale_vix_series_is_unavailable_not_extrapolated():
    """VIX that stops updating must not have its last value carried forward across the window."""
    p = price([22700 + i * 12 for i in range(20)])
    stale = series([15.0, 15.1, 15.2])          # only the first three bars
    res = vx.compute_vix_divergence(p, stale, cfg=CFG)
    assert res["state"] == vx.UNAVAILABLE
    assert "within" in res["detail"]


def test_vix_is_never_interpolated():
    """_series_at returns an observed value or None; it never invents one between observations."""
    obs = series([15.0, 16.0], step=3600)        # an hour apart
    assert vx._series_at(obs, T0, 300) == 15.0
    assert vx._series_at(obs, T0 + 1800, 300) is None, "mid-gap must be None, not 15.5"


def test_forming_bars_are_excluded():
    p = price([22700 + i * 12 for i in range(20)])
    p[-1]["closed"] = False
    res = vx.compute_vix_divergence(p, series([16.5 - i * 0.07 for i in range(20)]), cfg=CFG)
    assert res["price_to"] == p[-2]["c"]


# ── review findings (AGY, 30 Oct) ──────────────────────────────────────────────────────────────────

def _pb(n=25, base=22500.0, step=10.0):
    return [{"ts": 1761800000 + i * 180, "c": base + i * step, "closed": True} for i in range(n)]


def test_forming_vix_bar_is_ignored():
    """A forming VIX candle must not reach vix_to.

    price_bars were filtered on `closed is not False` but vix_bars were not, so a mid-bar tick moved
    the reported VIX change -- and could flip DISTRIBUTION_WARNING on and off inside one bar.
    """
    pb = _pb()
    t0, t1 = pb[0]["ts"], pb[-1]["ts"]
    closed_only = [{"ts": t0, "c": 12.0, "closed": True}, {"ts": t1, "c": 13.0, "closed": True}]
    with_forming = closed_only + [{"ts": t1, "c": 19.0, "closed": False}]
    a = vx.compute_vix_divergence(pb, closed_only, cfg=CFG)
    b = vx.compute_vix_divergence(pb, with_forming, cfg=CFG)
    assert a["vix_to"] == b["vix_to"], "a forming VIX bar changed the read"
    assert a["state"] == b["state"]


def test_all_vix_bars_forming_is_unavailable():
    """If every VIX bar is still forming there is no closed series, so report unavailable."""
    r = vx.compute_vix_divergence(_pb(), [{"ts": 1761800000, "c": 12.0, "closed": False}], cfg=CFG)
    assert r["state"] == vx.UNAVAILABLE
    assert r["available"] is False
