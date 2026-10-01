"""
test_liquidity_engine.py — Rigorous unit test suite for liquidity_engine.py.
Covers all 12 modules, all dataclasses, defensive guards, and institutional rules.
"""

from datetime import datetime, time, timedelta
import numpy as np
import pandas as pd
import pytest

import liquidity_engine as le
import smc


def make_candles(closes, highs=None, lows=None, opens=None, start_time=None):
    n = len(closes)
    start_time = start_time or datetime(2026, 9, 7, 9, 15)
    ts = [start_time + timedelta(minutes=15 * i) for i in range(n)]
    c = np.array(closes, dtype=float)
    h = np.array(highs, dtype=float) if highs is not None else c + 5.0
    l = np.array(lows, dtype=float) if lows is not None else c - 5.0
    o = np.array(opens, dtype=float) if opens is not None else np.roll(c, 1)
    o[0] = c[0]
    return pd.DataFrame({
        "timestamp": ts,
        "open": o,
        "high": h,
        "low": l,
        "close": c,
        "volume": np.random.randint(1000, 5000, size=n),
    })


def make_daily_ohlc(n_days=60, base=24000.0):
    start = datetime(2026, 7, 1)
    dates = [start + timedelta(days=i) for i in range(n_days)]
    c = base + np.cumsum(np.random.normal(5, 30, n_days))
    h = c + np.random.uniform(20, 60, n_days)
    l = c - np.random.uniform(20, 60, n_days)
    o = l + (h - l) * 0.5
    return pd.DataFrame({
        "Date": dates,
        "Open": o,
        "High": h,
        "Low": l,
        "Close": c,
    })


def test_defensive_guards():
    rules = le.load_rules()

    # 1. None / empty df
    snap_none = le.evaluate_liquidity_terminal(None, spot=24150.0, rules=rules)
    assert len(snap_none.warnings) > 0
    assert snap_none.spot == 24150.0
    assert snap_none.smt_divergence.status_label == "UNAVAILABLE — needs BANKNIFTY feed"

    # 2. Insufficient bars (< 2N + 3)
    short_df = make_candles([24100, 24110, 24120])
    snap_short = le.evaluate_liquidity_terminal(short_df, spot=24120.0, rules=rules)
    assert any("Insufficient bars" in w for w in snap_short.warnings)

    # 3. Missing required columns
    bad_df = pd.DataFrame({"close": [24100] * 20})
    snap_bad = le.evaluate_liquidity_terminal(bad_df, spot=24100.0, rules=rules)
    assert any("Missing required" in w for w in snap_bad.warnings)

    # 4. All-NaN volume
    nan_vol_df = make_candles([24100 + i * 5 for i in range(20)])
    nan_vol_df["volume"] = np.nan
    snap_vol = le.evaluate_liquidity_terminal(nan_vol_df, spot=24200.0, rules=rules)
    assert snap_vol.spot == 24200.0

    # 5. Zero ATR
    zero_df = make_candles([24100.0] * 20, highs=[24100.0] * 20, lows=[24100.0] * 20, opens=[24100.0] * 20)
    snap_zero = le.evaluate_liquidity_terminal(zero_df, spot=24100.0, rules=rules)
    assert snap_zero.atr > 0.0  # Fallback must prevent zero ATR


def test_liquidity_pool_map():
    rules = le.load_rules()
    spot = 24150.0
    closes = [24100, 24120, 24150, 24180, 24160, 24140, 24170, 24190, 24210, 24180, 24160, 24140]
    df = make_candles(closes)
    daily = make_daily_ohlc()

    snap = le.evaluate_liquidity_terminal(df, spot=spot, ohlc_daily=daily, rules=rules)
    pm = snap.pool_map

    assert len(pm.pools) > 0
    # Mandatory provenance check
    assert "INFERRED" in pm.provenance
    for p in pm.pools:
        assert "INFERRED" in p.provenance
        if p.pool_side == "BUY_STOPS":
            assert p.pool_price >= p.level
        else:
            assert p.pool_price <= p.level

    # Nearest above and below must be correctly segregated
    if pm.nearest_above:
        assert pm.nearest_above.pool_price >= spot
    if pm.nearest_below:
        assert pm.nearest_below.pool_price < spot


def test_inducement():
    closes = [24050, 24080, 24100, 24120, 24150, 24110, 24130, 24160, 24140, 24170, 24190, 24180]
    df = make_candles(closes)
    pivots = smc.pivots(df, n=2)
    target = le.PoolItem(
        pool_id="PDH_24250", pool_type="PDH", source="Prior Day High",
        level=24250.0, pool_price=24260.0, pool_side="BUY_STOPS", weight=5.0,
        state="RESTING", touch_count=2, family_agreements=[], timeframe="DAILY", origin_move_atr=2.0
    )
    ind = le.evaluate_inducement(df, pivots, target, spot=24150.0)
    assert isinstance(ind, le.Inducement)
    assert ind.quality_verdict is not None
    assert "INFERRED" in ind.provenance


def test_displacement():
    rules = le.load_rules()
    # Create an impulsive bullish candle
    closes = [24000, 24010, 24020, 24030, 24040, 24120, 24130]
    highs = [24005, 24015, 24025, 24035, 24045, 24125, 24135]
    lows = [23995, 24005, 24015, 24025, 24038, 24042, 24115]
    opens = [24000, 24005, 24015, 24025, 24035, 24040, 24120]
    df = make_candles(closes, highs=highs, lows=lows, opens=opens)
    gaps = smc.fvgs(df)

    disp = le.evaluate_displacement(df, gaps, atr=20.0, rules=rules)
    assert isinstance(disp, le.Displacement)
    assert 0 <= disp.score <= 4
    assert disp.direction in ("BULLISH", "BEARISH", "NEUTRAL")


def test_dealing_range():
    rules = le.load_rules()
    closes = [24000, 24050, 24100, 24150, 24200, 24150, 24100, 24080, 24120, 24180, 24150]
    highs = [c + 10.0 for c in closes]
    lows = [c - 10.0 for c in closes]
    highs[4] = 24250.0  # Peak swing high
    lows[0] = 23950.0   # Low swing low
    df = make_candles(closes, highs=highs, lows=lows)
    pivots = smc.pivots(df, n=2)

    dr = le.evaluate_dealing_range(pivots, spot=24050.0, rules=rules)
    assert isinstance(dr, le.DealingRangeAnalysis)
    assert dr.zone in ("PREMIUM", "DISCOUNT", "EQUILIBRIUM")
    assert "0.500" in dr.bands
    assert "0.618" in dr.bands
    assert "0.790" in dr.bands
    # Must enforce standalone entry rule in wording
    assert "never a standalone entry" in dr.note.lower() or "never standalone entry" in dr.provenance.lower()


def test_breaker_and_mitigation():
    closes = [24100, 24150, 24200, 24180, 24140, 24100, 24050, 24020, 24000, 24030, 24060]
    df = make_candles(closes)
    pivots = smc.pivots(df, n=2)
    st = smc.structure(df, pivots)
    gaps = smc.fvgs(df)
    obs = smc.order_blocks(df, st, gaps)

    bm = le.evaluate_breaker_mitigation(df, obs, pivots)
    assert isinstance(bm, le.BreakerMitigation)
    for it in bm.items:
        assert it.block_type in ("BREAKER", "MITIGATION")
        assert it.state in ("ACTIVE", "RETESTED", "WAITING")


def test_bpr_and_ce():
    # Build overlapping bullish and bearish FVGs
    closes = [24000, 24050, 24100, 24080, 24020, 23980, 24010]
    highs = [24010, 24060, 24110, 24090, 24030, 23990, 24020]
    lows = [23990, 24040, 24070, 24060, 24000, 23960, 23990]
    df = make_candles(closes, highs=highs, lows=lows)
    gaps = smc.fvgs(df)

    res = le.evaluate_bpr_and_ce(df, gaps)
    assert isinstance(res, le.BPRAndCE)
    assert isinstance(res.bprs, list)
    assert isinstance(res.ces, list)


def test_session_model():
    rules = le.load_rules()
    closes = [24100, 24120, 24150, 24180, 24170, 24160, 24140]
    df = make_candles(closes, start_time=datetime(2026, 9, 7, 9, 15))

    sess = le.evaluate_session_model(df, rules=rules)
    assert isinstance(sess, le.SessionModel)
    assert len(sess.windows) == 7
    # 15:15-15:30 closing auction must be present and excluded from trading
    auction_win = next(w for w in sess.windows if w.start_time == "15:15")
    assert "Exclude entirely" in auction_win.character


def test_judas_swing():
    rules = le.load_rules()
    # Initial rally 09:15-10:00, then dump below open
    closes = [24000, 24030, 24060, 24070, 24050, 24010, 23980, 23950, 23930]
    highs = [c + 10 for c in closes]
    lows = [c - 10 for c in closes]
    highs[3] = 24100.0  # +100 pt spike
    df = make_candles(closes, highs=highs, lows=lows, start_time=datetime(2026, 9, 7, 9, 15))
    gaps = smc.fvgs(df)
    disp = le.evaluate_displacement(df, gaps, atr=25.0, rules=rules)

    judas = le.evaluate_judas_swing(df, disp, atr=25.0, rules=rules)
    assert isinstance(judas, le.JudasSwing)
    assert judas.status in ("CONFIRMED", "UNCONFIRMED", "NONE")


def test_smt_divergence():
    rules = le.load_rules()
    closes = [24000, 24020, 24040, 24060, 24080, 24100, 24120, 24140, 24160, 24180, 24200]
    nifty_df = make_candles(closes)

    # 1. Missing BANKNIFTY feed -> MUST be UNAVAILABLE
    smt_none = le.evaluate_smt_divergence(nifty_df, None, rules=rules)
    assert smt_none.available is False
    assert smt_none.status_label == "UNAVAILABLE — needs BANKNIFTY feed"

    # 2. Available BANKNIFTY feed - Bearish divergence
    # NIFTY makes Higher High, BANKNIFTY makes Lower High
    nifty_bear = make_candles([24000, 24050, 24100, 24080, 24060, 24120, 24100, 24080, 24060, 24050, 24040])
    bnf_bear = make_candles([51000, 51100, 51200, 51150, 51100, 51180, 51140, 51100, 51080, 51050, 51020])
    smt_bear = le.evaluate_smt_divergence(nifty_bear, bnf_bear, rules=rules)
    assert smt_bear.available is True
    assert smt_bear.divergence_type == "BEARISH_SMT"
    assert smt_bear.sentiment == "BEARISH"

    # 3. Timeframe auto-alignment: 1-minute BNF feed with 15-minute NIFTY feed
    # Generate 1m BNF candles
    start_t = datetime(2026, 9, 7, 9, 15)
    ts_1m = [start_t + timedelta(minutes=i) for i in range(150)]
    bnf_1m = pd.DataFrame({
        "timestamp": ts_1m,
        "open": np.linspace(51000, 51200, 150),
        "high": np.linspace(51010, 51210, 150),
        "low": np.linspace(50990, 51190, 150),
        "close": np.linspace(51005, 51205, 150),
        "volume": 100.0,
    })
    smt_aligned = le.evaluate_smt_divergence(nifty_df, bnf_1m, rules=rules)
    assert smt_aligned.available is True


def test_power_of_three():
    rules = le.load_rules()
    closes = [24100, 24105, 24102, 24108, 24104, 24135, 24080, 24050, 24020]
    df = make_candles(closes)
    pivots = smc.pivots(df, n=2)
    disp = le.Displacement(score=3, large_body=True, small_wicks=True, fvg_created=True,
                           two_closes_in_dir=True, direction="BEARISH", candle_idx=7,
                           candle_time=datetime.now(), verdict="CONVICTION", note="")
    pool_map = le.compute_pool_map(df, pivots, spot=24100.0, atr=20.0, rules=rules)

    amd = le.evaluate_power_of_three(df, pool_map, disp, atr=20.0, rules=rules)
    assert isinstance(amd, le.PowerOfThree)
    assert amd.phase in ("ACCUMULATION", "MANIPULATION", "DISTRIBUTION", "COMPLETED", "NONE")


def test_confluence_scoring():
    rules = le.load_rules()
    closes = [24050, 24080, 24120, 24150, 24180, 24200, 24180, 24160, 24140, 24130, 24150]
    df = make_candles(closes)
    daily = make_daily_ohlc()

    snap = le.evaluate_liquidity_terminal(df, spot=24150.0, ohlc_daily=daily, rules=rules)
    confl = snap.confluence

    assert isinstance(confl, le.ConfluenceScore)
    # Rule: Must NOT collapse into percentage, must name every factor
    assert "%" not in confl.verdict
    assert confl.total_factors == len(confl.factors)
    for f in confl.factors:
        assert len(f.name) > 0
        assert f.strength in ("STRONG", "MODERATE")


def test_build_terminal_chart():
    import liquidity_tab as lt
    closes = [24100, 24120, 24150, 24180, 24170, 24160, 24140, 24130, 24150]
    df = make_candles(closes)
    snap = le.evaluate_liquidity_terminal(df, spot=24150.0)

    fig = lt.build_terminal_chart(df, snap)
    assert fig is not None
    assert len(fig.data) >= 1  # Candlestick trace
    assert fig.layout.height == 500


def test_ctx_object_contract():
    from context import Ctx
    import liquidity_tab as lt
    closes = [24100, 24120, 24150, 24180, 24170, 24160, 24140, 24130, 24150]
    df = make_candles(closes)
    daily = make_daily_ohlc()

    ctx = Ctx(
        pa_frames={"15m": df, "5m": df, "1m": df},
        spot=24150.0,
        ohlc=daily,
    )
    # Ensure ctx fields are accessible
    assert ctx.spot == 24150.0
    assert "15m" in ctx.pa_frames

