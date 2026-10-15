from app.engine import smt


def bars(highs, lows, t0=1_760_000_000, step=300):
    return [{"ts": t0 + i * step, "o": (h + l) / 2, "h": h, "l": l, "c": (h + l) / 2} for i, (h, l) in enumerate(zip(highs, lows))]


def test_bearish_smt_when_bank_fails_to_make_higher_high():
    nh = [10, 11, 15, 11, 10, 12, 18, 12, 11, 10]
    bh = [10, 11, 15, 11, 10, 12, 14, 12, 11, 10]           # bank lower high at the 2nd peak
    lows = [h - 2 for h in nh]
    r = smt.detect(bars(nh, lows), bars(bh, [h - 2 for h in bh]))
    assert [m["dir"] for m in r["markers"]] == ["bear"]
    m = r["markers"][0]
    assert m["ts"] == 1_760_000_000 + 8 * 300            # stamped at confirmation (pivot 6 + n=2), no look-ahead


def test_no_smt_when_both_confirm():
    nh = [10, 11, 15, 11, 10, 12, 18, 12, 11, 10]
    r = smt.detect(bars(nh, [h - 2 for h in nh]), bars(nh, [h - 2 for h in nh]))
    assert r["markers"] == []


def test_bullish_smt_on_lows():
    nl = [20, 19, 15, 19, 20, 18, 12, 18, 19, 20]
    bl = [20, 19, 15, 19, 20, 18, 16, 18, 19, 20]           # bank higher low
    r = smt.detect(bars([x + 2 for x in nl], nl), bars([x + 2 for x in bl], bl))
    assert [m["dir"] for m in r["markers"]] == ["bull"]
