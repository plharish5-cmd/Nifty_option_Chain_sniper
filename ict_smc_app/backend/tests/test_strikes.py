"""Strike mode: Priyanka/MJ selector parity + determinism, hysteresis, instrument resolver, strike-flow delta,
Options Flow Proxy labelling, CSV close-time import per option, paper premium P&L, DB migration."""
import json
import math
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("ICT_DATA_DIR", str(Path(__file__).resolve().parent / ".data"))

from app import db  # noqa: E402
from app.delta import adapters, confluence, engine, proxy  # noqa: E402
from app.delta.adapters import CsvAdapter, parse_csv, store_csv  # noqa: E402
from app.engine.primitives import Bars, atr  # noqa: E402
from app.engine.registry import Zone  # noqa: E402
from app.market import instruments as instr  # noqa: E402
from app.signals import paper  # noqa: E402
from app.signals.playbook import Signal  # noqa: E402
from app.strikes import priyanka as pk  # noqa: E402
from app.strikes.selector import StrikeBook, select_strikes  # noqa: E402

T915 = 1791431100            # Thu 8 Oct 2026 09:15 IST
EXP = "2026-10-13"


# ── a deterministic Dhan-shaped chain (Black-Scholes) ─────────────────────────
def _ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _bs(S, K, T, iv, call):
    d1 = (math.log(S / K) + 0.5 * iv * iv * T) / (iv * math.sqrt(T))
    d2 = d1 - iv * math.sqrt(T)
    pdf = math.exp(-d1 * d1 / 2) / math.sqrt(2 * math.pi)
    p = S * _ncdf(d1) - K * _ncdf(d2) if call else K * _ncdf(-d2) - S * _ncdf(-d1)
    dl = _ncdf(d1) if call else _ncdf(d1) - 1
    return p, dl, pdf / (S * iv * math.sqrt(T)), -(S * pdf * iv) / (2 * math.sqrt(T)) / 365, S * pdf * math.sqrt(T) / 100


def chain(spot=22250.0, T=3 / 365):
    rows = []
    for K in range(21800, 22751, 50):
        iv = 0.13 + 0.00003 * abs(K - spot)
        sides = {}
        for side, call, sid0 in (("ce", True, 40000), ("pe", False, 50000)):
            p, dl, g, th, vg = _bs(spot, K, T, iv, call)
            oi = 1e6 * math.exp(-((K - spot) / 300) ** 2) * (1.3 if call else 1.0)
            sides[side] = {"ltp": round(p, 2), "chg": round(-3 if call else 4, 2), "oi": oi, "oi_chg": oi * (0.2 if call else -0.1),
                           "oi_chg_pct": 20.0 if call else -10.0, "iv": round(100 * iv, 2), "vol": oi * 2,
                           "delta": round(dl, 3), "gamma": round(g, 5), "theta": round(th, 2), "vega": round(vg, 2),
                           "bid": round(p - 0.3, 2), "ask": round(p + 0.3, 2), "sid": sid0 + K}
        rows.append({"strike": float(K), **sides})
    return {"expiry": EXP, "spot": spot, "rows": rows, "call_wall": 22500.0, "put_wall": 22000.0, "max_pain": 22200.0}


def snapshot(spot=22250.0):
    return {"expiry": EXP, "spot": spot, "chain": chain(spot), "open": 22300.0, "prev_close": 22330.0}


# ── verbatim copy of OC strike_advisor_pro.scenarios / pick (constants inline) — the parity reference ──────
def oc_scenarios(ch, spot, side, move, sl, mins, div_win, div_loss, window=300):
    p = side.lower()
    x = ch[(ch.strike - spot).abs() <= window].copy()
    g = lambda c: x[f"{p}_{c}"] if f"{p}_{c}" in x else pd.Series(np.nan, index=x.index)   # noqa: E731
    out = pd.DataFrame({"strike": x.strike.astype(int), "ltp": g("ltp"), "bid": g("bid"), "ask": g("ask"),
                        "delta": g("delta"), "gamma": g("gamma"), "theta": g("theta"), "vega": g("vega"),
                        "iv": g("iv"), "oi": g("oi")})
    out = out[(out.ltp > 1) & out.delta.notna()]
    if out.empty:
        return out
    sgn = 1 if side == "CE" else -1
    out["itm"] = (spot - out.strike) * sgn
    D, G, T, V = out.delta.abs(), out.gamma.fillna(0), out.theta.fillna(0), out.vega.fillna(0)
    tcost = T * mins / 375.0
    out["win"] = D * move + 0.5 * G * move ** 2 + tcost + V * div_win
    out["loss"] = -D * sl + 0.5 * G * sl ** 2 + tcost + V * div_loss
    out["rr"] = out.win / out.loss.abs().replace(0, np.nan)
    out["ret_pct"] = 100 * out.win / out.ltp
    out["theta_pct_day"] = 100 * T.abs() / out.ltp
    atm_iv = float(out.loc[(out.strike - spot).abs().idxmin(), "iv"]) if out.iv.notna().any() else np.nan
    out["ivskew"] = out.iv - atm_iv
    out["spread"] = 100 * (out.ask - out.bid) / out.ltp
    out["course"] = (out.strike % 100 == 0) & out.itm.between(50, 250) & out.ltp.between(120, 320)
    out["kind"] = np.where(out.itm > 25, "ITM", np.where(out.itm < -25, "OTM", "ATM"))
    out["prem_tgt"] = out.ltp + out.win
    out["prem_sl"] = out.ltp + out.loss
    return out.sort_values("strike")


def oc_pick(sc):
    if sc is None or sc.empty:
        return None, None
    ok = sc[(sc.spread.fillna(0) <= 3) & (sc.win > 0)].copy()
    ok["fit"] = ok.rr * (1 - (ok.ltp - 200).abs() / 400).clip(0.2, 1)
    course = ok[ok.course]
    itm = ok[ok.kind == "ITM"]
    best = (course.sort_values(["fit", "oi"], ascending=False).iloc[0] if len(course) else
            itm.sort_values("fit", ascending=False).iloc[0] if len(itm) else None)
    fast = ok[(ok.itm <= 25) & (ok.itm >= -100)].sort_values("ret_pct", ascending=False)
    return best, (fast.iloc[0] if len(fast) else None)


def test_priyanka_port_matches_oc_strike_advisor_exactly():
    ch = pk.chain_frame(chain())
    for side in ("CE", "PE"):
        for spot in (22250.0, 22233.0, 22281.5):
            a = pk.scenarios(ch, spot, side, 40, 40, 30, 0.0, -0.5)
            b = oc_scenarios(ch, spot, side, 40, 40, 30, 0.0, -0.5)
            pd.testing.assert_frame_equal(a.reset_index(drop=True), b.reset_index(drop=True))
            (ab, af), (bb, bf) = pk.pick(a), oc_pick(b)
            assert int(ab.strike) == int(bb.strike) and int(af.strike) == int(bf.strike)


def test_selector_is_deterministic_and_explains_itself():
    r1, r2 = select_strikes(snapshot()), select_strikes(snapshot())
    assert json.dumps(r1, sort_keys=True, default=str) == json.dumps(r2, sort_keys=True, default=str)
    best_ce = next(x for x in r1["calls"] if x["role"] == "best")
    best_pe = next(x for x in r1["puts"] if x["role"] == "best")
    # MJ course pick: ITM 50–250, multiple of 100, premium 120–320
    for rec, sgn in ((best_ce, 1), (best_pe, -1)):
        k = rec["instrument"]["strike"]
        assert k % 100 == 0 and 50 <= (22250 - k) * sgn <= 250 and 120 <= rec["ltp"] <= 320 and rec["course"]
        assert rec["instrument"]["security_id"] in (str(40000 + k), str(50000 + k))
    assert best_ce["instrument"]["label"] == f"NIFTY 13 Oct {best_ce['instrument']['strike']} CE"
    assert best_ce["why"].startswith(f"{best_ce['instrument']['strike']} CE best course pick: Δ ")
    assert all("R:R" in x for x in r1["reasons"] if "no strike" not in x)


def test_selector_optional_filters_default_off():
    from app import config
    S = config.rules()["strikes"]
    old = dict(S["filters"])
    try:
        S["filters"]["delta_range_ce"] = [0.25, 0.45]
        r = select_strikes(snapshot())
        assert all(0.25 <= x["delta"] <= 0.45 for x in r["calls"])
    finally:
        S["filters"].clear()
        S["filters"].update(old)
    assert select_strikes(snapshot())["calls"][0]["delta"] > 0.45      # back to the OC rule (ITM course pick)


def test_strikebook_switches_only_on_3m_close_and_clear_improvement():
    bk = StrikeBook()
    r = select_strikes(snapshot(22250.0))
    t = T915 + 3600 + 30                                     # 10:15:30 — first minute of a 3m candle
    bk.update(r, t)
    first = bk.current()["CE"]["instrument"]["key"]
    moved = select_strikes(snapshot(22420.0))                # spot jumps → a different course strike is best
    assert next(x for x in moved["calls"] if x["role"] == "best")["instrument"]["key"] != first
    bk.update(moved, t + 60)                                 # 10:16:30 — same 3m bucket → hold
    assert bk.current()["CE"]["instrument"]["key"] == first
    bk.update(moved, t + 150)                                # 10:18:00 — new 3m bucket, just closed → may switch
    assert bk.current()["CE"]["instrument"]["key"] != first
    assert any("switched" in x for x in bk.log)
    bk.set_manual("CE", {"instrument": instr.option(EXP, 22100, "CE", "42100").to_dict(), "role": "manual"})
    bk.update(select_strikes(snapshot(22600.0)), t + 330)
    assert bk.current()["CE"]["instrument"]["strike"] == 22100        # manual pick is pinned


def test_instrument_identity_and_resolver():
    ch = {EXP: chain()}
    o = instr.resolve(instr.option(EXP, 22400, "pe"), ch)
    assert o.key == "NIFTY|2026-10-13|22400|PE" and o.security_id == "72400" and o.segment == "NSE_FNO"
    assert instr.parse_key(o.key).key == o.key and instr.parse_key("NIFTY").type == "INDEX"
    try:
        instr.resolve(instr.option("2026-10-20", 22400, "CE"), ch)
        assert False, "unknown expiry must not resolve"
    except LookupError:
        pass


# ── strike flow on index zones ────────────────────────────────────────────────
T0 = 1791442200
STEP = 300
SUPPLY_OB = Zone("OB-test", "ORDER_BLOCK", "5m", "SUPPLY", 100.0, 110.0, T0, "fresh", label="test supply OB",
                 extra={"kind": "OB"})


def _an(spec):
    rows = [{"ts": T0 + STEP * k, "o": o, "h": h, "l": l, "c": c} for k, (o, h, l, c) in enumerate(spec)]
    b = Bars.from_rows(rows)
    return SimpleNamespace(tf="5m", bars=b, atr=atr(b, 14), liq=[])


class StrikeDelta(adapters.DeltaAdapter):
    name = "fixed"

    def __init__(self, d, key):
        self.d, self.key = d, key

    def status(self):
        return {"name": self.name, "available": True, "mock": False, "message": "test"}

    def get_delta(self, candle, tf, instrument=None):
        if instrument is None or instrument.key != self.key:
            return None
        return self.d.get((int(candle["ts"]) - T0) // STEP)


def _idx():
    return [(95, 97, 93, 95.5) if k % 2 else (95.5, 97, 94, 95) for k in range(20)] + [(99, 108, 99, 106), (106, 107, 104, 105)]


def _opt_candles(spec):
    return {T0 + STEP * k: {"o": o, "h": h, "l": l, "c": c, "v": v} for k, (o, h, l, c, v) in enumerate(spec)}


def test_strike_delta_rule2_on_next_candle_mapped_to_nifty():
    an = _an(_idx())                                        # bar 20 enters the supply OB, bar 21 is the next close
    ce = instr.option(EXP, 22400, "CE", "62400")
    oc = _opt_candles([(150, 152, 149, 150.5, 1000)] * 20 + [(150, 160, 150, 159, 9000), (159, 160, 157, 158, 3000)])
    ad = StrikeDelta({**{k: (100.0 if k % 2 else -100.0) for k in range(20)}, 20: 6000.0, 21: 40.0}, ce.key)
    r = engine.compute(an, [SUPPLY_OB], ad, now=2e9, flow={"instrument": ce, "candles": oc})
    ab = [e for e in r["events"] if e["rule"] == "rule2_no_follow_through"]
    assert len(ab) == 1 and ab[0]["ts"] == T0 + 21 * STEP and ab[0]["direction"] == "bear"
    assert "22400 CE" in ab[0]["explanation"] and "bearish for NIFTY" in ab[0]["explanation"]
    early = engine.compute(an, [SUPPLY_OB], ad, now=T0 + 21 * STEP + 120, flow={"instrument": ce, "candles": oc})
    assert not [e for e in early["events"] if e["rule"] == "rule2_no_follow_through"]       # next candle forming
    pe = instr.option(EXP, 22100, "PE", "72100")
    rp = engine.compute(an, [SUPPLY_OB], StrikeDelta(ad.d, pe.key), now=2e9, flow={"instrument": pe, "candles": oc})
    agg = [e for e in rp["events"] if e["event_type"] == "DELTA_AGGRESSION"]
    assert agg and agg[0]["direction"] == "bear"           # buyers attacking a PUT = bearish for NIFTY


def test_no_delta_means_unavailable_and_structure_only():
    an = _an(_idx())
    ce = instr.option(EXP, 22400, "CE", "62400")
    r = engine.compute(an, [SUPPLY_OB], adapters.make("none"), now=2e9, flow={"instrument": ce, "candles": {}})
    assert r["available"] is False and r["events"] == []
    s = SimpleNamespace(setup="A", tf="5m", ts=T0 + 21 * STEP, i=21, side="short", ctx={})
    confluence.apply([s], {"available": False, "events": []})
    assert s.confidence == 0.5 and s.confidence_parts == {"structure": 0.5, "options_context": None, "flow": 0.0,
                                                          "flow_source": "none"}
    assert "Delta unavailable" in s.confluence[0]


def test_proxy_is_labelled_and_never_emits_delta_events():
    an = _an(_idx())
    ce = instr.option(EXP, 22400, "CE", "62400")
    pe = instr.option(EXP, 22100, "PE", "72100")
    cec = _opt_candles([(150, 151, 149, 150 + (0.5 if k % 2 else -0.5), 1000) for k in range(20)]
                       + [(150, 160, 150, 159, 20000), (159, 160, 157, 158, 3000)])
    pec = _opt_candles([(120, 121, 119, 120, 1000)] * 22)
    off = proxy.compute(an, [SUPPLY_OB], {"instrument": ce, "candles": cec, "abs_delta": 0.6},
                        {"instrument": pe, "candles": pec, "abs_delta": 0.4}, now=2e9, enabled=False)
    assert off["events"] == [] and "PROXY (not real delta)" in off["message"]
    r = proxy.compute(an, [SUPPLY_OB], {"instrument": ce, "candles": cec, "abs_delta": 0.6},
                      {"instrument": pe, "candles": pec, "abs_delta": 0.4}, now=2e9, enabled=True)
    kinds = {e["event_type"] for e in r["events"]}
    assert kinds and kinds <= {"PROXY_FLOW_SURGE", "PROXY_FLOW_FADE"} and not any(k.startswith("DELTA") for k in kinds)
    assert all(e["explanation"].startswith("PROXY (not real delta) · ") and e["proxy"] for e in r["events"])
    assert "PROXY_FLOW_FADE" in kinds                       # surge into the OB, NIFTY did not follow next candle
    s = SimpleNamespace(setup="A", tf="5m", ts=T0 + 21 * STEP, i=21, side="short", ctx={})
    confluence.apply([s], {"available": False, "events": []}, proxy_res=r)
    assert s.confidence_parts["flow_source"] == "proxy" and any("PROXY (not real delta)" in x for x in s.confluence)
    assert not any(x.startswith("+") and "delta" in x.lower() and "PROXY" not in x for x in s.confluence)


def test_csv_close_time_import_for_an_option_strike():
    for t in ("delta_bars", "footprint_levels"):
        db.execute(f"DELETE FROM {t}")
    ce = instr.option(EXP, 22400, "CE")
    p = parse_csv("timestamp,delta\n2026-10-08 09:16,500\n2026-10-08 09:17,-200\n", bar_seconds=60, ts_is="end")
    assert sorted(p["bars"]) == [T915, T915 + 60]                     # close 09:16 → candle starting 09:15
    rep = store_csv(p, ce)
    assert rep["instrument"] == ce.key
    a = CsvAdapter()
    assert a.get_delta({"ts": T915}, "1m", ce) == 500
    assert a.get_delta({"ts": T915}, "1m") is None                   # the index has no CSV rows
    p5 = parse_csv("timestamp,buy_vol,sell_vol\n2026-10-08 09:20:00,700,300\n", bar_seconds=300, ts_is="end")
    assert list(p5["bars"]) == [T915]                                # 5m close 09:20 → 09:15 candle


def test_paper_records_strike_snapshot_and_premium_pnl():
    db.execute("DELETE FROM paper_trades")
    T = 1791448800                                                    # 14:10
    sig = Signal("A|5m|short|9|99", "A", "Raid → MSS → FVG", "5m", "short", 10, T, "TRADE", entry=22033.0,
                 sl=22060.0, tp=21979.0, risk=27.0, rr=2.0, target_src="2R", story=["t"])
    sig.confidence, sig.confidence_parts = 0.65, {"structure": 0.5, "options_context": 0.05, "flow": 0.1, "flow_source": "delta"}
    pe = instr.option(EXP, 22100, "PE", "72100").to_dict()
    strikes = {"CE": None, "PE": {"instrument": pe, "role": "best", "ltp": 180.0}}
    db.upsert_candles("O:72100", [{"ts": T + 240, "o": 180, "h": 182, "l": 179, "c": 181.0, "v": 1},
                                  {"ts": T + 360, "o": 200, "h": 214, "l": 199, "c": 212.5, "v": 1}])
    t = paper.maybe_open(sig, strikes=strikes, mode="INDEX")
    assert t["opt_key"] == pe["key"] and t["opt_entry"] == 181.0 and t["confidence"] == 0.65
    assert json.loads(t["strikes_snapshot"])["PE"]["instrument"]["strike"] == 22100
    done = paper.on_1m_close({"ts": T + 360, "o": 22015, "h": 22020, "l": 21975, "c": 21980})
    assert done["status"] == "TP" and done["entry"] == 22033.0 and done["sl"] == 22060.0       # index levels
    paper.fill_option_prices()
    row = next(x for x in paper.trades(5) if x["signal_key"] == sig.key)
    assert row["opt_exit"] == 212.5 and row["opt_pnl_pts"] == 31.5 and row["opt_pnl_rs"] == round(31.5 * 65)
    db.execute("DELETE FROM paper_trades")


def test_old_delta_tables_migrate_to_instrument_keys():
    d = tempfile.mkdtemp()
    path = Path(d) / "old.sqlite3"
    c = sqlite3.connect(str(path))
    c.execute("CREATE TABLE delta_bars (ts INTEGER PRIMARY KEY, secs INTEGER NOT NULL DEFAULT 60, buy REAL, sell REAL, "
              "delta REAL NOT NULL, src TEXT) WITHOUT ROWID")
    c.execute("INSERT INTO delta_bars VALUES (1791431100, 60, NULL, NULL, 42.0, 'csv')")
    c.commit()
    db._migrate(c)
    c.executescript(db.SCHEMA)
    rows = c.execute("SELECT inst, ts, delta FROM delta_bars").fetchall()
    assert rows == [("NIFTY", 1791431100, 42.0)]
    c.close()


def test_zero_or_flat_option_candles_never_produce_flow_events():
    an = _an(_idx())
    ce = instr.option(EXP, 22400, "CE", "62400")
    zero = _opt_candles([(0, 0, 0, 0, 1000)] * 22)
    ad = StrikeDelta({**{k: (100.0 if k % 2 else -100.0) for k in range(20)}, 20: 6000.0, 21: 40.0}, ce.key)
    r = engine.compute(an, [SUPPLY_OB], ad, now=2e9, flow={"instrument": ce, "candles": zero})
    assert not [e for e in r["events"] if e["event_type"] in ("DELTA_ABSORPTION", "FOOTPRINT_IMBALANCE", "DELTA_FLIP")]
    p = proxy.compute(an, [SUPPLY_OB], {"instrument": ce, "candles": zero, "abs_delta": 0.6}, None, now=2e9, enabled=True)
    assert p["events"] == [] and p["available"] is False


def test_strikebook_holds_a_still_good_strike_against_a_small_improvement():
    A, B = instr.option(EXP, 22100, "CE", "62100").to_dict(), instr.option(EXP, 22200, "CE", "62200").to_dict()
    res = lambda best, fit, scores: {"calls": [{"instrument": best, "role": "best", "fit": fit, "why": "t"}],   # noqa: E731
                                     "puts": [], "scores": {"CE": scores, "PE": {}}}
    bk, t = StrikeBook(), T915 + 3600 + 30                  # 10:15:30, first minute of a 3m candle
    bk.update(res(A, 1.00, {A["key"]: 1.00}), t)
    bk.update(res(B, 1.10, {A["key"]: 1.00, B["key"]: 1.10}), t + 150)      # +10 % < 15 % → hold A
    assert bk.current()["CE"]["instrument"]["key"] == A["key"]
    bk.update(res(B, 1.20, {A["key"]: 1.00, B["key"]: 1.20}), t + 330)      # +20 % at a fresh 3m close → B
    assert bk.current()["CE"]["instrument"]["key"] == B["key"] and "score 1.20 vs 1.00" in bk.log[-1]
    bk.update(res(A, 1.05, {A["key"]: 1.05}), t + 510)                      # B no longer passes the course rule
    assert bk.current()["CE"]["instrument"]["key"] == A["key"] and "no longer passes" in bk.log[-1]
