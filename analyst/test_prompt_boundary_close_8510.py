"""v266 bug-CE3: the opposite-close arm-flip fired ~86s late because
_latest_closed_15m_bar returns iloc[-2], and until the new bucket's 1m candle
lands there is no forming row at iloc[-1] — so the just-closed bar stays hidden.
Appending a synthetic forming row from the live spot surfaces it immediately.

These tests prove the forming-row append moves the just-closed bar into the
iloc[-2] slot the instant the clock crosses the 15m boundary.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from analyst.ema8_prototype_8510 import current_nse_15m_open_ts, ensure_forming_b15_bar

IST = ZoneInfo("Asia/Kolkata")
DAY = "2026-11-27"


def _open(hm):
    h, m = (int(x) for x in hm.split(":"))
    return pd.Timestamp(datetime(2026, 11, 27, h, m, tzinfo=IST))


def _b15(rows):
    idx = [_open(hm) for hm, _ in rows]
    data = {"open": [], "high": [], "low": [], "close": [], "volume": []}
    for _, close in rows:
        data["open"].append(close)
        data["high"].append(close + 5)
        data["low"].append(close - 5)
        data["close"].append(close)
        data["volume"].append(0)
    return pd.DataFrame(data, index=idx)


def test_just_closed_bar_hidden_without_forming_row():
    # At 10:00:02 the 10:00 1m candle has not landed, so resample ends at the
    # 09:45 bar (which closed at 10:00). iloc[-2] is the STALE 09:30 bar.
    b15 = _b15([("09:30", 25870.0), ("09:45", 25841.0)])  # 09:45 bar = just closed, bearish
    assert b15.index[-2].strftime("%H:%M") == "09:30"  # stale bar in the -2 slot


def test_forming_row_surfaces_just_closed_bar():
    b15 = _b15([("09:30", 25870.0), ("09:45", 25841.0)])
    now = datetime(2026, 11, 27, 10, 0, 2, tzinfo=IST)
    cur_open = current_nse_15m_open_ts(now)
    assert datetime.fromtimestamp(cur_open, IST).strftime("%H:%M") == "10:00"
    b15b, _ = ensure_forming_b15_bar(b15, bar_open_ts=cur_open, live_spot=25839.0)
    # Now the forming 10:00 row is iloc[-1]; the just-closed 09:45 bar is iloc[-2].
    assert b15b.index[-1].strftime("%H:%M") == "10:00"
    assert b15b.index[-2].strftime("%H:%M") == "09:45"
    assert float(b15b.iloc[-2]["close"]) == 25841.0  # the bar the arm-flip now evaluates


def test_no_double_append_when_bucket_already_present():
    # If the 10:00 1m candle already landed, resample has the 10:00 bucket; the
    # forming append must be a no-op (open_dt == last_open).
    b15 = _b15([("09:30", 25870.0), ("09:45", 25841.0), ("10:00", 25838.0)])
    cur_open = current_nse_15m_open_ts(datetime(2026, 11, 27, 10, 0, 2, tzinfo=IST))
    b15b, _ = ensure_forming_b15_bar(b15, bar_open_ts=cur_open, live_spot=25836.0)
    assert len(b15b) == 3  # unchanged
    assert b15b.index[-2].strftime("%H:%M") == "09:45"


def test_within_bucket_no_new_boundary():
    # Mid-bucket (10:07) the current bucket is still 10:00; nothing to surface
    # beyond what's already there.
    cur_open = current_nse_15m_open_ts(datetime(2026, 11, 27, 10, 7, 0, tzinfo=IST))
    assert datetime.fromtimestamp(cur_open, IST).strftime("%H:%M") == "10:00"
