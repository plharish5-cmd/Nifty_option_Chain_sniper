"""Setup D — CRT + TBS. Hand-built candles where the right answer is known in advance."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("ICT_DATA_DIR", str(Path(__file__).resolve().parent / ".data"))

from app import config, db  # noqa: E402
from app.engine import crt_tbs  # noqa: E402
from app.signals import paper  # noqa: E402

D6, D7, D8 = 1791258300, 1791344700, 1791431100       # 6 / 7 / 8 Oct 2026 09:15 IST
PAIR = {"htf": "1h", "ltf": "1m", "name": "Scalping 1h→1m"}


def h(ts, o, hi, lo, c):
    return {"ts": ts, "o": o, "h": hi, "l": lo, "c": c, "closed": True}


def htf_rows(old_high=22060.0, bull=False, flat=False):
    """6–7 Oct + 8 Oct 09:15. 7 Oct 12:15 prints a swing high (or low); 8 Oct 10:15 is the thick CRT candle."""
    rows = []
    for k in range(7):
        rows.append(h(D6 + k * 3600, 21990, 22005, 21975, 21995))
    if not bull:
        shape = [(22000, 22015, 21990, 22010), (22010, 22030, 22000, 22025), (22025, 22045, 22015, 22040),
                 (22040, old_high, 22030, 22035), (22035, 22040, 22015, 22020), (22020, 22025, 22000, 22005),
                 (22005, 22010, 21995, 22000)]
        crt = h(D8 + 3600, 22000, 22100, 21995, 22090)                     # bull, takes the old high → OHP
    else:
        shape = [(22000, 22010, 21985, 21990), (21990, 21995, 21970, 21975), (21975, 21980, 21955, 21960),
                 (21960, 21965, old_high, 21965), (21965, 21985, 21960, 21980), (21980, 21995, 21975, 21990),
                 (21990, 22000, 21985, 21995)]
        crt = h(D8 + 3600, 21995, 22000, 21895, 21905)                     # bear, takes the old low → OLP
    if flat:                                                              # no swing, no gap → no keylevel at all
        shape = [(21995, 22005, 21985, 21995)] * 7
    rows += [h(D7 + k * 3600, *shape[k]) for k in range(7)]
    rows.append(h(D8, 21995, 22010, 21990, 22000 if not bull else 21995))
    rows.append(crt)
    return rows


def ltf_sell(upto_min=None):
    """1m candles 8 Oct 09:15 → 11:23. TBS at 11:19, Model #1 = TBS candle, entry at 11:21 (close below its low)."""
    rows = [h(D8 + m * 60, 22000, 22003, 21997, 22000) for m in range(60)]
    rows += [h(D8 + 3600 + m * 60, 22000 + 1.5 * m, 22002 + 1.5 * m, 21998 + 1.5 * m, 22001.5 + 1.5 * m)
             for m in range(60)]                                          # CRT hour, closes ≤ 22090
    t = D8 + 7200                                                         # 11:15 — the manipulation hour
    rows += [h(t, 22090, 22096, 22088, 22094), h(t + 60, 22094, 22099, 22092, 22097),
             h(t + 120, 22097, 22099.5, 22093, 22095), h(t + 180, 22095, 22099, 22094, 22096),
             h(t + 240, 22096, 22108, 22095, 22106),                      # 11:19 TBS: body closes above CRTH 22100
             h(t + 300, 22106, 22112, 22101, 22103),                      # 11:20 not thick, holds above 22095
             h(t + 360, 22103, 22104, 22090, 22092),                      # 11:21 close < 22095 → ENTRY 22092
             h(t + 420, 22092, 22094, 22080, 22082)]
    if upto_min is not None:
        rows = [r for r in rows if r["ts"] <= D8 + 7200 + upto_min * 60]
    return rows


def ltf_buy():
    rows = [h(D8 + m * 60, 21995, 21998, 21992, 21995) for m in range(60)]
    rows += [h(D8 + 3600 + m * 60, 21995 - 1.5 * m, 21997 - 1.5 * m, 21993 - 1.5 * m, 21993.5 - 1.5 * m)
             for m in range(60)]
    t = D8 + 7200
    rows += [h(t, 21905, 21907, 21899, 21901), h(t + 60, 21901, 21903, 21896, 21898),
             h(t + 120, 21898, 21902, 21892, 21889),                      # 11:17 TBS: close 21889 < CRTL 21895
             h(t + 180, 21889, 21891, 21880, 21882),                      # 11:18 thick sell candle → Model #1 (high 21891)
             h(t + 240, 21882, 21894, 21881, 21893)]                      # 11:19 close > 21891 → ENTRY 21893
    return rows


NOW = 2e9


def run(htf, ltf):
    return crt_tbs.evaluate_pair(PAIR, htf, ltf, NOW)


def test_sell_setup_end_to_end():
    r = run(htf_rows(), ltf_sell())
    cr = r["ranges"][-1]
    assert (cr["crth"], cr["crtl"], cr["mid"]) == (22100.0, 21995.0, 22047.5)
    assert cr["biases"] == ["bear"] and any(k["kind"] == "OHP" and k["price"] == 22060 for k in cr["keylevels"])
    assert cr["aplus"] and cr["status"] == "entered"
    (s,) = r["signals"]
    assert (s.setup, s.side, s.status, s.tf) == ("D", "short", "TRADE", "1m")
    assert s.ts == D8 + 7200 + 360 and s.entry == 22092.0                 # 11:21 close
    assert s.sl == 22114.0 and s.tp == 21995.0 and s.ctx["tp1"] == 22047.5 # extreme high 22112 + 2
    assert s.risk == 22.0 and s.ctx["rr1"] == 2.02
    assert s.ctx["tbs_ts"] == D8 + 7200 + 240 and s.ctx["m1_ts"] == D8 + 7200 + 240
    kinds = [e["kind"] for e in r["events"]]
    assert kinds == ["CRT_TBS", "CRT_M1", "CRT_ENTRY"]


def test_buy_setup_mirror():
    r = run(htf_rows(old_high=21950.0, bull=True), ltf_buy())
    (s,) = r["signals"]
    assert s.side == "long" and s.entry == 21893.0
    assert s.ctx["m1_ts"] == D8 + 7200 + 180                              # the thick SELL candle after the TBS
    assert s.sl == 21878.0 and s.tp == 22000.0 and s.ctx["tp1"] == 21947.5


def test_no_look_ahead_entry_only_after_its_candle_closes():
    assert run(htf_rows(), ltf_sell(upto_min=5))["signals"] == []         # 11:21 candle not closed yet
    (s,) = run(htf_rows(), ltf_sell(upto_min=6))["signals"]
    assert s.ts == D8 + 7200 + 360
    forming = ltf_sell(upto_min=6)
    forming[-1] = dict(forming[-1], closed=False)                          # same candle, still forming → ignored
    assert run(htf_rows(), forming)["signals"] == []


def test_wick_poke_is_not_a_tbs():
    rows = ltf_sell()
    i = next(k for k, x in enumerate(rows) if x["ts"] == D8 + 7200 + 240)
    rows[i] = h(rows[i]["ts"], 22096, 22108, 22095, 22099)                # wick above CRTH, body closes inside
    rows[i + 1] = h(rows[i + 1]["ts"], 22099, 22100, 22094, 22097)
    rows[i + 2] = h(rows[i + 2]["ts"], 22097, 22099, 22085, 22088)
    r = run(htf_rows(), rows)
    assert r["signals"] == [] and not [e for e in r["events"] if e["kind"] == "CRT_TBS"]


def test_keylevel_is_required():
    r = run(htf_rows(flat=True), ltf_sell())                              # same thick candle, nothing purged / tapped
    assert all(x["k_ts"] != D8 + 3600 for x in r["ranges"]) and r["signals"] == []


def test_low_rr_and_late_window_become_skip_with_reasons():
    C = config.rules()["crt_tbs"]
    old = C["min_rr_tp1"]
    try:
        C["min_rr_tp1"] = 3.0
        (s,) = run(htf_rows(), ltf_sell())["signals"]
        assert s.status == "SKIP" and any("R:R to TP-1" in x for x in s.reasons)
    finally:
        C["min_rr_tp1"] = old


def test_markers_align_to_chart_candles():
    r = run(htf_rows(), ltf_sell())
    m5 = crt_tbs.markers(r, "5m", 0)
    from app.timeutil import chart_time
    assert {m["time"] for m in m5} == {chart_time(D8 + 7200), chart_time(D8 + 7200 + 300)}   # 11:19 → 11:15 bar, 11:21 → 11:20 bar
    assert len(m5) == 3


def test_paper_books_half_at_tp1_then_breakeven():
    db.execute("DELETE FROM paper_trades")
    (s,) = run(htf_rows(), ltf_sell())["signals"]
    t = paper.maybe_open(s)
    assert t and t["setup"] == "D" and t["tp1"] == 22047.5
    m0 = D8 + 7200 + 420
    assert paper.on_1m_close(h(m0, 22092, 22095, 22060, 22065)) is None
    assert paper.on_1m_close(h(m0 + 60, 22065, 22066, 22045, 22050)) is None   # TP-1 22047.5 → half booked
    tr = paper.open_trade()
    assert tr["half_done"] == 1 and tr["sl"] == 22092.0 and tr["partial_pts"] == 44.5
    done = paper.on_1m_close(h(m0 + 120, 22050, 22093, 22049, 22090))         # back to entry → breakeven stop
    assert done["status"] == "BE" and done["exit"] == 22092.0
    assert done["pnl_pts"] == 22.2 and done["r"] == round(22.25 / 22.0, 2)    # ½ × 44.5 + ½ × 0
    db.execute("DELETE FROM paper_trades")


def test_paper_stop_before_tp1_is_full_loss():
    db.execute("DELETE FROM paper_trades")
    (s,) = run(htf_rows(), ltf_sell())["signals"]
    paper.maybe_open(s)
    m0 = D8 + 7200 + 420
    done = paper.on_1m_close(h(m0, 22092, 22120, 22040, 22100))               # touches SL and TP-1 → stop first
    assert done["status"] == "SL" and done["r"] == -1.0
    db.execute("DELETE FROM paper_trades")


def test_nse_4h_candles_are_session_aligned():
    from app.timeutil import bucket_start, is_closed
    assert bucket_start(D8 + 3 * 3600, "4h") == D8                               # 12:15 → the 09:15 candle
    assert bucket_start(D8 + 4 * 3600 + 60, "4h") == D8 + 4 * 3600                # 13:16 → the 13:15 candle
    assert not is_closed(D8 + 4 * 3600, "4h", D8 + 6 * 3600)                     # 15:15: still forming
    assert is_closed(D8 + 4 * 3600, "4h", D8 + 6 * 3600 + 900)                   # 15:30: closed (2h15m candle)
    assert crt_tbs._end(D8 + 4 * 3600, "4h") == D8 + 6 * 3600 + 900


def test_inside_count_is_capped_not_99():
    (s,) = run(htf_rows(), ltf_sell())["signals"]
    assert s.ctx["inside"] == 7 and "7+ candles inside — not A+" in s.story[2]


def test_optional_bias_filter_uses_trend_known_at_entry():
    from types import SimpleNamespace as NS
    bear_15m = NS(tf="15m", events=[NS(ts=D8, dir="bear"), NS(ts=D8 + 7200 + 900, dir="bull")])   # bull BOS closes 11:45
    C = config.rules()["crt_tbs"]
    assert crt_tbs.trend_at(bear_15m, D8 + 7200 + 420) == "bear"          # at 11:22 only the bear BOS is known
    (s,) = crt_tbs.evaluate_pair(PAIR, htf_rows(), ltf_sell(), NOW, bias_an=bear_15m)["signals"]
    assert s.status == "TRADE" and s.ctx["bias_trend"] == "bear"            # short WITH the trend: fine
    bull_15m = NS(tf="15m", events=[NS(ts=D8, dir="bull")])
    (s,) = crt_tbs.evaluate_pair(PAIR, htf_rows(), ltf_sell(), NOW, bias_an=bull_15m)["signals"]
    assert s.status == "TRADE"                                              # filter off by default → course rules only
    C["require_bias_alignment"] = True
    try:
        (s,) = crt_tbs.evaluate_pair(PAIR, htf_rows(), ltf_sell(), NOW, bias_an=bull_15m)["signals"]
        assert s.status == "SKIP" and any("against the 15m structure" in x for x in s.reasons)
    finally:
        C["require_bias_alignment"] = False


# ── concepts from the second batch of course cards ─────────────────────────────
def _at(rows, ts):
    return next(k for k, x in enumerate(rows) if x["ts"] == ts)


def test_model2_fvg_entry_when_no_close_beyond_model1():
    rows = ltf_sell(upto_min=4)                                              # up to the 11:19 TBS
    t = D8 + 7200
    rows += [h(t + 300, 22106, 22109, 22100, 22101),                         # 11:20
             h(t + 360, 22101, 22102, 22096, 22097),                         # 11:21 holds above Model #1 low 22095
             h(t + 420, 22097, 22098, 22095.5, 22096)]                       # 11:22 high 22098 < 11:20 low 22100 → bear FVG
    (s,) = run(htf_rows(), rows)["signals"]
    assert s.ctx["model"] == "M2" and s.ts == t + 420 and s.entry == 22096.0
    assert s.sl == 22111.0 and "Model #2 (FVG)" in s.story[3]


def test_model3_bos_entry_on_close_below_last_swing_low():
    C = config.rules()["crt_tbs"]
    old = C["models"]
    rows = ltf_sell()
    t = D8 + 7200
    rows[_at(rows, t + 60)] = h(t + 60, 22094, 22099, 22080, 22097)        # 11:16 swing low 22080 (confirmed 11:18)
    rows.append(h(t + 480, 22082, 22083, 22070, 22075))                     # 11:23 close 22075 < 22080 → BOS
    try:
        C["models"] = ["M3"]
        (s,) = run(htf_rows(), rows)["signals"]
        assert s.ctx["model"] == "M3" and s.ts == t + 480 and s.entry == 22075.0
        assert "Model #3 (BOS)" in s.story[3] and "22,080.0" in s.story[3]
        C["models"] = ["M1", "M2", "M3"]
        (s,) = run(htf_rows(), rows)["signals"]
        assert s.ctx["model"] == "M1" and s.ts == t + 360                   # Model #1 confirmed first (11:21)
    finally:
        C["models"] = old


def test_rejection_block_keylevel():
    rows = htf_rows(flat=True)
    k = _at(rows, D7 + 3 * 3600)
    rows[k] = h(D7 + 3 * 3600, 21995, 22095, 21990, 21998)                 # swing high with a 92 % upper wick
    cr = run(rows, ltf_sell())["ranges"][-1]
    rb = [x for x in cr["keylevels"] if x["kind"] == "RB"]
    assert rb and rb[0]["dir"] == "bear" and (rb[0]["bottom"], rb[0]["top"]) == (21998.0, 22095.0)


def test_inside_bar_crt_moves_manipulation_to_third_candle():
    hrows = htf_rows() + [h(D8 + 7200, 22090, 22099, 22040, 22060)]         # 11:15 1h candle stays inside 21995–22100
    lrows = [x for x in ltf_sell() if x["ts"] < D8 + 7200]
    lrows += [h(D8 + 7200 + m * 60, 22060, 22070, 22050, 22060) for m in range(60)]   # the inside hour
    lrows += [dict(x, ts=x["ts"] + 3600) for x in ltf_sell() if x["ts"] >= D8 + 7200]  # TBS hour now 12:15
    r = run(hrows, lrows)
    (s,) = r["signals"]
    assert r["ranges"][-1]["inside_bar"] and s.ctx["inside_bar"] and s.ts == D8 + 3 * 3600 + 360
    assert s.story[0].startswith("Inside-bar CRT")
    C = config.rules()["crt_tbs"]
    C["inside_bar_crt"] = False
    try:
        assert run(hrows, lrows)["signals"] == []                             # the 2nd candle had no TBS
    finally:
        C["inside_bar_crt"] = True


# ── per-strategy paper slots + scorecard ──────────────────────────────────────
def _a_signal(ts, key="A|5m|short|1|2"):
    from app.signals.playbook import Signal
    return Signal(key, "A", "Raid → MSS → FVG", "5m", "short", 1, ts, "TRADE", entry=22092.0, sl=22114.0,
                  tp=22048.0, risk=22.0, rr=2.0, target_src="2R", story=["a"])


def test_setups_trade_in_their_own_slots_and_score_separately():
    db.execute("DELETE FROM paper_trades")
    (d,) = run(htf_rows(), ltf_sell())["signals"]
    assert paper.maybe_open(d)["slot"] == "D|1h>1m"
    a = paper.maybe_open(_a_signal(d.ts - 240))
    assert a and a["slot"] == "A"                                         # D open does NOT block A any more
    assert paper.maybe_open(_a_signal(d.ts, key="A|5m|short|9|9")) is None # but a 2nd A waits for the 1st
    assert len(paper.open_trades()) == 2
    m0 = D8 + 7200 + 420
    paper.on_1m_close_all(h(m0, 22092, 22095, 22060, 22065))
    closed = paper.on_1m_close_all(h(m0 + 60, 22065, 22066, 22040, 22045))  # A hits its TP 22048; D books TP-1
    assert [t["setup"] for t in closed] == ["A"] and paper.open_trade("D|1h>1m")["half_done"] == 1
    closed = paper.on_1m_close_all(h(m0 + 120, 22045, 22046, 21990, 21994))  # D reaches TP-2 21995
    assert [(t["setup"], t["status"]) for t in closed] == [("D", "TP")]
    sc = {r["label"]: r for r in paper.scorecard()["rows"]}
    assert sc["A · Raid → MSS → FVG"]["trades"] == 1 and sc["A · Raid → MSS → FVG"]["win_rate"] == 100.0
    dd = sc["D · CRT + TBS (all pairs)"]
    assert dd["trades"] == 1 and dd["total_r"] == round((0.5 * 44.5 + 0.5 * 97) / 22, 2)
    assert sc["D · 1h→1m"]["trades"] == 1 and sc["D · Model #1 order block"]["trades"] == 1
    assert sc["All setups"]["trades"] == 2 and not sc["All setups"]["enough"]
    db.execute("DELETE FROM paper_trades")
