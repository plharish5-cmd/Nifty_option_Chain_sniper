"""Tests for the RSI / price divergence engine.

The headline test replays 29 Oct 2026. On that session the desk's OI divergence printed five bearish
warnings (09:33, 09:39, 10:00, 11:21, 13:15) while NIFTY ran 22,720 -> 22,953, and the 09:33 one was
ranked A+ 95% and became a short that stopped out in 21 minutes. The correct behaviour for this
engine on that data is to confirm NO bearish reversal: divergences may arm, but price never closed
through a trigger, so every one of them must end ARMED, EXPIRED, INVALIDATED or VETOED.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("ICT_DATA_DIR", str(Path(__file__).resolve().parent / ".data"))

from app.engine import rsi_divergence as rd  # noqa: E402

CFG = {
    "rsi_period": 14, "pivot_window": 3, "min_pivot_gap_bars": 3, "max_pivot_gap_bars": 30,
    "min_price_diff_pts": 8.0, "min_rsi_diff": 3.0, "expiry_bars": 10,
    "structure_buffer_pts": 1.0, "sl_buffer_pts": 12.0, "enabled": True,
}
T0 = 1793240100  # 29 Oct 2026 09:15 IST


def bars_from(closes, step=180, start=T0, spread=6.0):
    """Synthetic OHLC around a close path. h/l straddle the close so pivots track the path."""
    out = []
    for i, c in enumerate(closes):
        prev = closes[i - 1] if i else c
        out.append({"ts": start + i * step, "o": prev, "c": c,
                    "h": max(prev, c) + spread, "l": min(prev, c) - spread})
    return out


# ── RSI correctness ───────────────────────────────────────────────────────────────────────────────

def test_rsi_is_none_until_the_period_is_filled():
    r = rd.wilder_rsi([100.0 + i for i in range(20)], 14)
    assert all(x is None for x in r[:14])
    assert r[14] is not None


def test_rsi_of_a_pure_uptrend_is_100():
    r = rd.wilder_rsi([100.0 + i for i in range(30)], 14)
    assert r[-1] == 100.0, "no down closes at all means avg_loss == 0"


def test_rsi_of_a_pure_downtrend_is_near_zero():
    r = rd.wilder_rsi([200.0 - i for i in range(30)], 14)
    assert r[-1] is not None and r[-1] < 1.0


def test_rsi_stays_in_range_on_noisy_data():
    closes = [100 + (7 * i % 13) - 6 for i in range(120)]
    for v in rd.wilder_rsi([float(x) for x in closes], 14):
        assert v is None or 0.0 <= v <= 100.0


def test_wilder_smoothing_not_simple_average():
    """Guard against the common SMA-of-gains mistake, which gives a different number."""
    closes = [float(x) for x in [10, 11, 10, 12, 11, 13, 12, 14, 13, 15, 14, 16, 15, 17, 16, 18]]
    r = rd.wilder_rsi(closes, 14)
    gains = [max(closes[i] - closes[i - 1], 0.0) for i in range(1, 15)]
    losses = [max(closes[i - 1] - closes[i], 0.0) for i in range(1, 15)]
    sma_rsi = 100 - 100 / (1 + (sum(gains) / 14) / (sum(losses) / 14))
    assert abs(r[15] - sma_rsi) > 0.01, "Wilder and SMA must not coincide here"


# ── pivots ────────────────────────────────────────────────────────────────────────────────────────

def test_pivots_are_local_extremes_only():
    b = bars_from([100, 102, 104, 110, 104, 102, 100, 98, 96, 90, 96, 98, 100], spread=0.0)
    highs, lows = rd.find_pivots(b, 3)
    assert 3 in highs and 9 in lows


def test_a_plateau_yields_one_pivot_not_many():
    b = bars_from([100, 102, 110, 110, 110, 102, 100, 99, 98, 97], spread=0.0)
    highs, _ = rd.find_pivots(b, 3)
    assert len(highs) <= 1, f"a flat top must not mark every bar as a pivot (got {highs})"


# ── classification ────────────────────────────────────────────────────────────────────────────────

def test_the_four_divergences():
    assert rd.classify("high", 120, 100, 60, 70, 8, 3) == "REGULAR_BEARISH"   # HH + lower RSI
    assert rd.classify("high", 100, 120, 70, 60, 8, 3) == "HIDDEN_BEARISH"    # LH + higher RSI
    assert rd.classify("low", 80, 100, 40, 30, 8, 3) == "REGULAR_BULLISH"     # LL + higher RSI
    assert rd.classify("low", 100, 80, 30, 40, 8, 3) == "HIDDEN_BULLISH"      # HL + lower RSI


def test_agreement_is_not_divergence():
    assert rd.classify("high", 120, 100, 80, 70, 8, 3) is None    # HH + higher RSI = agreement
    assert rd.classify("low", 80, 100, 20, 30, 8, 3) is None      # LL + lower RSI = agreement


def test_moves_below_the_thresholds_are_ignored():
    assert rd.classify("high", 101, 100, 60, 70, 8, 3) is None    # 1pt price move
    assert rd.classify("high", 120, 100, 69, 70, 8, 3) is None    # 1pt RSI move


# ── the lifecycle: a watch is not a trade ────────────────────────────────────────────────────────

# A verified regular-bearish shape, which is the 29 Oct session in miniature: a fast first leg to a
# peak (pivot high 22,956 at RSI 100), a pullback to 22,849, then a SLOW grind to a marginally higher
# high (pivot high 22,964 at RSI 78.2). Higher high, lower RSI -- textbook. Price then drifts and
# never closes back through 22,849, so the watch must never become a trade.
_RALLY = [22700, 22716, 22732, 22748, 22764, 22780, 22796, 22812, 22828, 22844, 22860, 22876,
          22892, 22908, 22924, 22940, 22950, 22944, 22900, 22870, 22855, 22862, 22880, 22896,
          22908, 22918, 22930, 22958]
_DRIFT = [22948, 22940, 22944, 22938, 22942, 22936]
_BREAKDOWN = [22900, 22860, 22830, 22800, 22770, 22740]   # closes hard through the 22,849 trigger


def _rally_with_divergence():
    return bars_from([float(x) for x in _RALLY + _DRIFT])


def test_a_divergence_in_an_uptrend_never_confirms_a_short():
    res = rd.compute_rsi_divergence(_rally_with_divergence(), tf="3m", day_regime="none", cfg=CFG)
    assert res["ok"] is True
    shorts = [e for e in res["events"] if e["side"] == "short" and e["is_reversal"]]
    confirmed = [e for e in shorts if e["state"] == "STRUCTURE_CONFIRMED"]
    assert not confirmed, f"price never closed through a trigger, so nothing may confirm: {confirmed}"


def test_the_regime_veto_blocks_a_counter_trend_reversal_watch():
    res = rd.compute_rsi_divergence(_rally_with_divergence(), tf="3m", day_regime="bull", cfg=CFG)
    shorts = [e for e in res["events"] if e["side"] == "short" and e["is_reversal"]]
    assert shorts, "setup check: the data does contain a bearish reversal divergence"
    assert all(e["state"] == "VETOED" for e in shorts), [e["state"] for e in shorts]
    assert all(e["vetoed"] for e in shorts)
    assert res["active"] is None, "a vetoed watch must never surface as the active signal"


def test_a_real_breakdown_does_confirm():
    """The gate must not suppress a divergence that price actually validates."""
    res = rd.compute_rsi_divergence(bars_from([float(x) for x in _RALLY + _BREAKDOWN]), tf="3m",
                                    day_regime="none", cfg=CFG)
    shorts = [e for e in res["events"] if e["side"] == "short" and e["is_reversal"]]
    assert any(e["state"] == "STRUCTURE_CONFIRMED" for e in shorts), [e["state"] for e in shorts]
    done = next(e for e in shorts if e["state"] == "STRUCTURE_CONFIRMED")
    assert done["entry"] < done["structure_trigger"]
    assert done["risk_pts"] > 0


def test_an_armed_watch_expires_rather_than_lingering():
    # Same divergence, then price goes nowhere for far longer than expiry_bars.
    res = rd.compute_rsi_divergence(bars_from([float(x) for x in _RALLY + [22945] * 20]), tf="3m",
                                    day_regime="none", cfg=CFG)
    states = {e["state"] for e in res["events"]}
    assert "ARMED" not in states or res["counts"]["expired"] > 0


def test_every_event_carries_a_trigger_and_an_invalidation():
    res = rd.compute_rsi_divergence(_rally_with_divergence(), tf="3m", day_regime="none", cfg=CFG)
    for e in res["events"]:
        assert e["structure_trigger"] is not None
        assert e["invalidation"] is not None
        assert e["armed_ts"] >= e["pivot_to_ts"], "a pivot cannot be known before it is confirmed"
        if e["side"] == "short":
            assert e["invalidation"] > e["structure_trigger"]
        else:
            assert e["invalidation"] < e["structure_trigger"]


def test_hidden_divergence_is_continuation_never_a_reversal_watch():
    res = rd.compute_rsi_divergence(_rally_with_divergence(), tf="3m", day_regime="bull", cfg=CFG)
    for e in res["events"]:
        if e["type"].startswith("HIDDEN"):
            assert e["is_reversal"] is False
            assert e["vetoed"] is False, "continuation agrees with the day; it is never vetoed"


def test_too_little_data_says_so_rather_than_guessing():
    res = rd.compute_rsi_divergence(bars_from([22700.0 + i for i in range(8)]), tf="3m", cfg=CFG)
    assert res["ok"] is False
    assert "not enough" in res["message"]
    assert res["events"] == []


def test_forming_bars_are_excluded():
    bars = bars_from([22700.0 + i * 10 for i in range(40)])
    bars[-1]["closed"] = False
    res = rd.compute_rsi_divergence(bars, tf="3m", cfg=CFG)
    assert res["total_bars"] == 39


# ── 1m -> tf aggregation (the endpoint's arithmetic, extracted so it is testable) ────────────────

def _m1(vals, start=T0):
    return [{"ts": start + i * 60, "o": float(v), "h": float(v) + 3,
             "l": float(v) - 3, "c": float(v)} for i, v in enumerate(vals)]


def test_three_one_minute_bars_make_one_3m_bar():
    bars = rd.aggregate_1m(_m1([100, 110, 105]), 180, T0 + 10_000)
    assert len(bars) == 1
    b = bars[0]
    assert b["o"] == 100 and b["c"] == 105
    assert b["h"] == 113 and b["l"] == 97, "high/low must span the whole bucket"


def test_the_forming_bucket_is_withheld():
    """Six 1m bars = two 3m buckets, but if the second has not closed yet only the first is used.
    A half-formed final bar flowing in would shift every pivot by one."""
    m1 = _m1([100, 110, 105, 120, 130, 125])
    closed_both = rd.aggregate_1m(m1, 180, T0 + 360)       # both buckets ended
    still_forming = rd.aggregate_1m(m1, 180, T0 + 300)     # second bucket ends at +360
    assert len(closed_both) == 2
    assert len(still_forming) == 1


def test_aggregation_is_order_independent():
    m1 = _m1([100, 110, 105])
    assert rd.aggregate_1m(list(reversed(m1)), 180, T0 + 10_000) == \
           rd.aggregate_1m(m1, 180, T0 + 10_000)


def test_empty_input_aggregates_to_nothing():
    assert rd.aggregate_1m([], 180, T0 + 10_000) == []
