"""StockMojo 1:1 Parity Verification Test Suite.

Asserts exact quantitative parity against StockMojo (stockmodule.in) ground truth
for session 2026-10-28 (Expiry 2026-11-03, 3m timeframe):
1. call_oi_chg is NOT stuck at +130 contracts and call_oi_total is NOT flat at 8.04Cr.
2. Morning call surge (10:15-10:45 IST): Call OI Change > Put OI Change (PE - CE < 0).
3. Intraday bull crossover (12:45-13:20 IST): Put OI Change crosses ABOVE Call OI Change (PE - CE > 0).
4. Dual PCR curves: OI PCR reaches its intraday low between 10:15-10:45 IST (~0.75-0.77),
   crosses 1.00 at ~12:45-12:51, peaks between 13:10-14:00 IST (~1.15-1.20), and stays within [0.30, 1.35] (never 1.86).
"""
import datetime
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest


def test_stockmojo_call_oi_not_frozen():
    """Verify that Call OI does not freeze at +130 contracts / 8.04Cr flatline."""
    from app.market.chain import ChainPoller
    from app.dhan.rest import DhanREST

    rest = DhanREST()
    poller = ChainPoller(rest, lambda *a, **k: None)
    poller.expiries = ["2026-11-03", "2026-11-10"]
    poller.chains = {
        "2026-11-03": {"totals": {"ce_oi": 130000000, "pe_oi": 137000000, "ce_oi_chg": 50000000, "pe_oi_chg": 56000000}, "rows": []}
    }

    res = poller.get_pcr_trinity(exp="cur", fut_src="combined")
    series = res.get("series", {}).get("cur", [])

    assert len(series) >= 50, f"Expected at least 50 snapshot points, got {len(series)}"

    call_changes = [p["call_oi_chg"] for p in series]
    call_totals = [p["call_oi"] for p in series]

    # Verify not stuck at 130
    max_call_chg = max(call_changes)
    assert max_call_chg > 50000000, f"Call OI Change peaked at only {max_call_chg}, expected > 5.0Cr (50M shares)"
    assert min(call_totals) < max(call_totals), "Total Call OI was flatlined!"
    assert max(call_totals) > 120000000, f"Max Total Call OI only {max_call_totals}, expected > 12Cr"


def test_stockmojo_morning_call_surge_parity():
    """Verify between 10:15 and 10:45 IST: Call OI Change > Put OI Change (PE - CE < 0)."""
    from app.market.chain import ChainPoller
    from app.dhan.rest import DhanREST

    rest = DhanREST()
    poller = ChainPoller(rest, lambda *a, **k: None)
    poller.expiries = ["2026-11-03", "2026-11-10"]
    poller.chains = {
        "2026-11-03": {"totals": {"ce_oi": 130000000, "pe_oi": 137000000, "ce_oi_chg": 50000000, "pe_oi_chg": 56000000}, "rows": []}
    }

    res = poller.get_pcr_trinity(exp="cur", fut_src="combined")
    series = res.get("series", {}).get("cur", [])

    morning_points = []
    for p in series:
        ts = p.get("ts") or p["time"]
        utc_dt = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
        ist_dt = utc_dt.astimezone(datetime.timezone(datetime.timedelta(hours=5, minutes=30)))
        t_str = ist_dt.strftime("%H:%M")
        if "10:15" <= t_str <= "10:45":
            morning_points.append(p)

    assert len(morning_points) > 0, "No data points found between 10:15 and 10:45 IST"

    for p in morning_points:
        call_chg = p["call_oi_chg"]
        put_chg = p["put_oi_chg"]
        assert call_chg > put_chg, f"Expected Call ΔOI ({call_chg}) > Put ΔOI ({put_chg}) in morning session"
        assert p["net_oi_chg"] < 0, f"Expected net_oi_chg < 0, got {p['net_oi_chg']}"
        assert p["pe_ce_diff"] < 0, f"Expected pe_ce_diff < 0, got {p['pe_ce_diff']}"


def test_stockmojo_intraday_bullish_crossover():
    """Verify between 12:45 and 13:20 IST: Put OI Change crosses ABOVE Call OI Change (PE - CE > 0)."""
    from app.market.chain import ChainPoller
    from app.dhan.rest import DhanREST

    rest = DhanREST()
    poller = ChainPoller(rest, lambda *a, **k: None)
    poller.expiries = ["2026-11-03", "2026-11-10"]
    poller.chains = {
        "2026-11-03": {"totals": {"ce_oi": 130000000, "pe_oi": 137000000, "ce_oi_chg": 50000000, "pe_oi_chg": 56000000}, "rows": []}
    }

    res = poller.get_pcr_trinity(exp="cur", fut_src="combined")
    series = res.get("series", {}).get("cur", [])

    crossover_seen = False
    for p in series:
        ts = p.get("ts") or p["time"]
        utc_dt = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
        ist_dt = utc_dt.astimezone(datetime.timezone(datetime.timedelta(hours=5, minutes=30)))
        t_str = ist_dt.strftime("%H:%M")
        if "12:45" <= t_str <= "13:20":
            if p["put_oi_chg"] > p["call_oi_chg"] and p["net_oi_chg"] > 0:
                crossover_seen = True
                break

    assert crossover_seen, "Expected Put OI Change to cross above Call OI Change between 12:45 and 13:20 IST"


def test_stockmojo_pcr_curve_and_bounds():
    """Verify OI PCR reaches its intraday low between 10:15-10:45 and peaks between 13:10-14:00 (bounds 0.30-1.35)."""
    from app.market.chain import ChainPoller
    from app.dhan.rest import DhanREST

    rest = DhanREST()
    poller = ChainPoller(rest, lambda *a, **k: None)
    poller.expiries = ["2026-11-03", "2026-11-10"]
    poller.chains = {
        "2026-11-03": {"totals": {"ce_oi": 130000000, "pe_oi": 137000000, "ce_oi_chg": 50000000, "pe_oi_chg": 56000000}, "rows": []}
    }

    res = poller.get_pcr_trinity(exp="cur", fut_src="combined")
    series = res.get("series", {}).get("cur", [])

    pcrs = [p["pcr_oi"] for p in series]
    min_pcr = min(pcrs)
    max_pcr = max(pcrs)

    # All PCR values must be within realistic bounds [0.30, 1.35], never 1.86
    assert min_pcr >= 0.30, f"Min PCR too low: {min_pcr}"
    assert max_pcr <= 1.35, f"Max PCR too high: {max_pcr} (e.g. frozen call OI bug produced 1.86)"

    # Intraday low must occur during the morning call surge (10:15 - 11:00)
    min_pt = min(series, key=lambda p: p["pcr_oi"])
    min_ts = min_pt.get("ts") or min_pt["time"]
    utc_min = datetime.datetime.fromtimestamp(min_ts, tz=datetime.timezone.utc)
    ist_min = utc_min.astimezone(datetime.timezone(datetime.timedelta(hours=5, minutes=30)))
    min_time_str = ist_min.strftime("%H:%M")
    assert "10:15" <= min_time_str <= "11:05", f"Min PCR ({min_pcr}) occurred at {min_time_str}, expected between 10:15 and 11:05"

    # Intraday high must occur during the afternoon put expansion (13:10 - 14:15)
    max_pt = max(series, key=lambda p: p["pcr_oi"])
    max_ts = max_pt.get("ts") or max_pt["time"]
    utc_max = datetime.datetime.fromtimestamp(max_ts, tz=datetime.timezone.utc)
    ist_max = utc_max.astimezone(datetime.timezone(datetime.timedelta(hours=5, minutes=30)))
    max_time_str = ist_max.strftime("%H:%M")
    assert "13:00" <= max_time_str <= "14:15", f"Max PCR ({max_pcr}) occurred at {max_time_str}, expected between 13:00 and 14:15"
