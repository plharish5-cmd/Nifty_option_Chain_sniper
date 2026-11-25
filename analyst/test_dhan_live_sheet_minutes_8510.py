from analyst.dhan_live_sheet_minutes_8510 import build_minute_row


def _ch(ce_chg, pe_chg, spot=25000):
    return {
        "fetched_at": 1_762_000_000.0,
        "expiry": "2026-11-27",
        "spot": spot,
        "rows": [
            {
                "strike": 25000,
                "ce": {"oi": 1000, "oi_chg": ce_chg, "ltp": 100, "vol": 10},
                "pe": {"oi": 1200, "oi_chg": pe_chg, "ltp": 90, "vol": 20},
            }
        ],
    }


def test_winner_from_net_delta_oi():
    row = build_minute_row(_ch(5, 15), minute="10:05", market_open=True, stale_after=30.0)
    assert row["net_delta_oi_raw"] == 10
    assert row["winner_min"] == "PUT WRITERS"
    assert row["ce_vol_minus_pe_vol"] == -10


def test_ad_and_futures_unavailable_by_default():
    row = build_minute_row(_ch(0, 0), minute="10:06", market_open=False, stale_after=30.0)
    assert row["nifty_ad"] == "unavailable"
    assert row["feed_state"] == "STALE"