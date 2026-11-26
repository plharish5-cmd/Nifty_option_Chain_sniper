"""v266 #2: the PE-CE headline (day_oi) must key off the MATCHED-POLL baseline
(last vs previous poll), not SESSION_SINCE_0915. Regression test builds a board
where the two baselines DISAGREE and asserts the headline follows matched-poll.
"""
from datetime import datetime, timedelta, timezone

from analyst.writer_bias_live_reads_8510 import apply_writer_bias_to_board_reads

IST = timezone(timedelta(hours=5, minutes=30))
DAY = "2026-11-26"


def _ts(hm: str) -> int:
    h, m = (int(x) for x in hm.split(":"))
    return int(datetime.strptime(DAY, "%Y-%m-%d").replace(tzinfo=IST, hour=h, minute=m).timestamp())


def _board():
    spot = 24000.0
    strikes = [23900.0, 24000.0, 24100.0]
    t0, t1, t2 = _ts("09:15"), _ts("10:00"), _ts("10:03")
    by_ts = {}
    for k in strikes:
        # t0 base: balanced
        by_ts.setdefault(t0, {})[k] = {"ce_oi": 100, "ce_ltp": 200, "pe_oi": 100, "pe_ltp": 200}
        # t1: PE wrote heavily since open (OI up, price down) -> since-open = PE-writing
        by_ts.setdefault(t1, {})[k] = {"ce_oi": 100, "ce_ltp": 200, "pe_oi": 900, "pe_ltp": 100}
        # t2: in the last poll CE wrote heavily (OI up, price down) -> matched-poll = CE-writing
        by_ts.setdefault(t2, {})[k] = {"ce_oi": 800, "ce_ltp": 150, "pe_oi": 900, "pe_ltp": 100}
    return by_ts, [t0, t1, t2], spot, t2


def test_headline_follows_matched_poll_not_since_open():
    by_ts, ts_sorted, spot, last_t = _board()
    out = apply_writer_bias_to_board_reads(
        by_ts=by_ts,
        ts_sorted=ts_sorted,
        spot=spot,
        band=250.0,
        day=DAY,
        now_ts=last_t,
        last_t=last_t,
        age_sec=1,
        scanner_cfg={},
        momentum_oi={},
        expiry="",
    )
    day_oi = out["day_oi"]
    assert day_oi is not None
    # Matched poll = CE-writing = BEARISH = PE entry direction.
    assert day_oi.get("writer_bias_regime") == "BEARISH", day_oi
    assert day_oi.get("direction") == "PE", day_oi
    # Matched-poll net is reported, and the since-09:15 leg is kept SEPARATE
    # (opposite sign: PE wrote over the session), not driving the headline.
    assert day_oi.get("writer_bias_net") is not None
    assert "matched-poll" in day_oi.get("detail", "")
    assert day_oi.get("writer_bias_net_since_0915") is not None
