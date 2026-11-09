"""NSE session-anchored candles match 09:15 bucket labels."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd

from analyst.nse_spot_candles_8510 import (
    EMA_WARMUP_1M_BARS,
    TRADINGVIEW_EMA_CROSS_DEFAULTS,
    ema4_ema8,
    load_tradingview_ema_cross_config,
    resample_nse_15m,
    resample_nse_1h,
)

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "ict_smc_app/backend/data/ict_smc.sqlite3"


def test_tradingview_ema_cross_defaults():
    cfg = load_tradingview_ema_cross_config()
    assert cfg["fast_length"] == 4
    assert cfg["slow_length"] == 8
    assert cfg["fast_source"] == "close"
    assert cfg["momentum_ema_period"] == 12
    assert cfg["direction_ema_period"] == 200
    assert cfg["signal_smoothing"] == 9
    s = pd.Series([100.0, 101.0, 102.0, 103.0, 104.0])
    e4, e8 = ema4_ema8(s, TRADINGVIEW_EMA_CROSS_DEFAULTS)
    assert len(e4) == len(s)


def test_15m_buckets_start_at_0915():
    if not DB.exists():
        return
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    df = pd.read_sql_query(
        "SELECT ts,o,h,l,c FROM candles_1m WHERE sec_id='13' ORDER BY ts DESC LIMIT ?",
        con,
        params=(EMA_WARMUP_1M_BARS,),
    )
    con.close()
    df = df.sort_values("ts").rename(columns=dict(o="open", h="high", l="low", c="close"))
    df["dt"] = pd.to_datetime(df["ts"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
    b15 = resample_nse_15m(df.set_index("dt"))
    assert not b15.empty
    mins = {t.strftime("%H:%M") for t in b15.index}
    assert "09:15" in mins
    assert not any(m.endswith(":07") or m.endswith(":23") for m in mins)


def test_1h_not_wall_clock_10am():
    if not DB.exists():
        return
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    df = pd.read_sql_query(
        "SELECT ts,o,h,l,c FROM candles_1m WHERE sec_id='13' ORDER BY ts DESC LIMIT 3000",
        con,
    )
    con.close()
    df = df.sort_values("ts").rename(columns=dict(o="open", h="high", l="low", c="close"))
    df["dt"] = pd.to_datetime(df["ts"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
    b1 = resample_nse_1h(df.set_index("dt"))
    mins = {t.strftime("%H:%M") for t in b1.index}
    assert "09:15" in mins
    assert "09:00" not in mins
