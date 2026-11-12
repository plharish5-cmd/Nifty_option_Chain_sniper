"""8600 Ticket lane and the four new option-chain reads.

Ticket lane:
 1. Off by default: a ticket is seen and nothing is emitted.
 2. On: a DIP BUY CE ticket gives one LONG / CE signal with the lane's ids and exit points.
 3. A PULLBACK SELL ticket gives a SHORT / PE signal.
 4. The same bar never fires twice; the next bar's ticket fires again.
 5. Non-entry badges (HOLD, BOOK, ADD, NOISE, CONFLICT, RE-FIRE) are not tickets.
 6. A ticket bar older than the limit is not acted on.
 7. High-conviction-only skips an ordinary ticket and takes a high-conviction one.
 8. No new ticket trade while a ticket trade is open.
 9. Route PAPER_ONLY marks the signal paper_only; LIVE_ALLOWED does not.
10. Cockpit updates: valid values saved, bad values ignored.
11. Bridge silent or malformed -> no signal, no exception.
12. Backend: the lane is a fixed spot-or-option exit lane; switching it on adds its
    ids to an enabled allowlist; the scanner loop and route downgrade are wired.

Chain reads:
13. PCR zones follow the table (0.5 / 0.8 / 1.2 / 1.5).
14. Max pain is the strike with the least total payout.
15. Price vs OI: calls premium up + OI up and puts premium down + OI up -> CE;
    the mirror -> PE; mixed -> NEUTRAL.
16. Volume-backed OI: a window with far less OI change per unit volume than the
    day's usual is churn (NEUTRAL); a normal one takes the PE-CE direction.
17. Gamma regime: heavy call OI -> positive (blocks both sides); heavy put OI ->
    negative (blocks neither); implied volatility round-trips.
18. No data / stale -> every read is "no data".
19. Board: the four reads are sources; ROOM-type gamma read blocks via the switch.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analyst import chain_reads_8510 as cr  # noqa: E402
from analyst import desk8600_ticket_lane_8510 as tl  # noqa: E402
from analyst.desk8600_board_8510 import SOURCE_IDS, build_board, judge, load_cfg as board_cfg  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 11, 12, 10, 3, 20, tzinfo=IST)


def bridge(badge="🎯 DIP BUY CE", direction="CE", significant=True, high=False, bar=None, spot=25200.0):
    bar = bar or (NOW - timedelta(seconds=20)).isoformat()
    row = lambda group, sig: dict(signal_group=group, signal_name=badge, verdict=badge, direction=direction,
                                  significant=sig, spot=spot, source_ts=bar)
    return {"rows": [row("State Change", True), row("Actionable Setups", significant), row("High Conviction", high)]}


def on(**kw):
    return {"desk8600_ticket_lane": dict({"enabled": True}, **kw)}


def run(last, cfg, state=None, open_pos=False, now=NOW):
    return tl.evaluate(last, cfg, {} if state is None else state, now, has_open_position=open_pos, spot=25210.0)


# 1
def test_lane_is_off_by_default():
    sigs, st = run(bridge(), {})
    assert sigs == [] and st["state"] == "OFF" and "DIP BUY" in st["detail"]


# 2, 3
def test_dip_buy_gives_long_ce_signal():
    sigs, st = run(bridge(), on())
    assert len(sigs) == 1 and st["state"] == "TICKET"
    s = sigs[0]
    assert s["strategy_id"] == "DESK8600_TICKET_LONG" and s["direction"] == "LONG" and s["opt_side"] == "CE"
    assert s["target_pts"] == 40.0 and s["sl_pts"] == 40.0 and s["index_price"] == 25210.0
    assert s["paper_only"] is False and "Dip Buy Ce" in s["setup_name"]


def test_pullback_sell_gives_short_pe_signal():
    sigs, _ = run(bridge("🎯 PULLBACK SELL", "PE"), on(target_pts=30, sl_pts=25))
    s = sigs[0]
    assert s["strategy_id"] == "DESK8600_TICKET_SHORT" and s["direction"] == "SHORT" and s["opt_side"] == "PE"
    assert s["target_pts"] == 30.0 and s["sl_pts"] == 25.0


# 4
def test_one_signal_per_bar_and_next_bar_fires_again():
    state = {}
    assert len(run(bridge(), on(), state)[0]) == 1
    sigs, st = run(bridge(), on(), state)
    assert sigs == [] and st["state"] == "TAKEN"
    later = NOW + timedelta(minutes=3)
    nxt = bridge(bar=(later - timedelta(seconds=10)).isoformat())
    assert len(run(nxt, on(), state, now=later)[0]) == 1


# 5
def test_non_entry_badges_are_not_tickets():
    for badge, d in (("🛡️ HOLD CE", "CE"), ("🟡 BOOK PE", "NEUTRAL"), ("🔥 ADD PE", "PE"),
                     ("NOISE", "NEUTRAL"), ("⚠️ CONFLICT", "NEUTRAL"), ("🔁 RE-FIRE", "NEUTRAL"), ("WAIT", "NEUTRAL")):
        sigs, st = run(bridge(badge, d), on())
        assert sigs == [] and st["state"] == "WAITING", badge
    assert run(bridge(significant=False), on())[0] == []


# 6
def test_old_ticket_bar_is_not_acted_on():
    sigs, st = run(bridge(bar=(NOW - timedelta(seconds=400)).isoformat()), on())
    assert sigs == [] and "too old" in st["detail"]


# 7
def test_high_conviction_only():
    assert run(bridge(), on(high_conviction_only=True))[0] == []
    sigs, _ = run(bridge(high=True), on(high_conviction_only=True))
    assert len(sigs) == 1 and "HIGH" in sigs[0]["conviction"]


# 8
def test_no_second_trade_while_one_is_open():
    state = {}
    sigs, st = run(bridge(), on(), state, open_pos=True)
    assert sigs == [] and st["state"] == "IN TRADE" and "last_key" not in state


# 9
def test_route_marks_paper_only():
    assert run(bridge(), on(route="PAPER_ONLY"))[0][0]["paper_only"] is True
    assert run(bridge(), on(route="LIVE_ALLOWED"))[0][0]["paper_only"] is False
    assert tl.load_cfg(on(route="nonsense"))["route"] == "LIVE_ALLOWED"


# 10
def test_cockpit_updates():
    sc = {}
    cfg = tl.apply_update(sc, {"enabled": True, "route": "PAPER_ONLY", "target_pts": 55, "sl_pts": "abc"})
    assert cfg["enabled"] and cfg["route"] == "PAPER_ONLY" and cfg["target_pts"] == 55.0 and cfg["sl_pts"] == 40.0
    cfg = tl.apply_update(sc, {"target_pts": 0, "route": "x", "junk": 1, "high_conviction_only": "true"})
    assert cfg["target_pts"] == 55.0 and cfg["route"] == "PAPER_ONLY" and cfg["high_conviction_only"] is True
    assert tl.apply_update(sc, {"enabled": False})["enabled"] is False


# 11
def test_silent_or_malformed_bridge_never_raises():
    for last in (None, {}, {"rows": None}, {"rows": ["x", 3]}, {"rows": [{"signal_group": "Actionable Setups"}]},
                 bridge(bar="not-a-time")):
        sigs, st = run(last, on())
        assert sigs == [] and st["state"] in ("WAITING", "ERROR")


# 12
def test_backend_wiring(tmp_path):
    from analyst import terminal8510_backend as tb
    from analyst.test_live_exit_orders_8510 import FakeBroker, make_test_backend

    assert tb.uses_fixed_spot_or_option_exit("DESK8600_TICKET_LONG")
    b = make_test_backend(tmp_path, FakeBroker(configured=True))
    b.algo_allowlist_cfg = {"enabled": True, "strategy_ids": ["STAGE_2_LONG"]}
    b.market_db = tmp_path / "none.sqlite3"
    out = b.update_desk8600_ticket_lane({"enabled": True, "route": "PAPER_ONLY"})
    assert out["ok"] and out["enabled"] and out["route"] == "PAPER_ONLY" and out["position_open"] is False
    assert {"DESK8600_TICKET_LONG", "DESK8600_TICKET_SHORT", "STAGE_2_LONG"} <= set(b.algo_allowlist_cfg["strategy_ids"])
    assert b.algo_strategy_execution_allowed("DESK8600_TICKET_SHORT")

    class B:
        last = None
    B.last = bridge(bar=(datetime.now(tb.IST) - timedelta(seconds=15)).isoformat())
    b.desk8600_bridge = B
    sigs = b.evaluate_desk8600_ticket_lane(25210.0)
    assert len(sigs) == 1 and sigs[0]["paper_only"] is True
    assert b.evaluate_desk8600_ticket_lane(25210.0) == []          # same bar
    src = (ROOT / "analyst/terminal8510_backend.py").read_text(encoding="utf-8")
    assert "self.evaluate_desk8600_ticket_lane, float(spot)" in src
    assert '_ticket_lane_cfg(self.scanner_cfg)["route"] == "PAPER_ONLY"' in src


# ── chain reads ──────────────────────────────────────────────────────────────
DAY = "2026-11-12"
STRIKES = [25000.0 + 50 * i for i in range(9)]        # 25000 .. 25400
SPOT = 25200.0


def ts(hm):
    return int(datetime.strptime(f"{DAY} {hm}", "%Y-%m-%d %H:%M").replace(tzinfo=IST).timestamp())


def snap(hm, ce_oi=1000.0, pe_oi=1000.0, ce_ltp=100.0, pe_ltp=100.0, ce_vol=0.0, pe_vol=0.0, per=None):
    rows = []
    for k in STRIKES:
        v = dict(ts=ts(hm), strike=k, ce_oi=ce_oi, pe_oi=pe_oi, ce_ltp=ce_ltp, pe_ltp=pe_ltp,
                 ce_vol=ce_vol, pe_vol=pe_vol, expiry="2026-11-17")
        v.update((per or {}).get(k, {}))
        rows.append(v)
    return rows


def reads(rows, now="12:01", spot=SPOT):
    return cr.compute_reads(rows, spot, cr.load_cfg({}), day=DAY, now_ts=ts(now))


# 13
def test_pcr_zones():
    for pe, want in ((400.0, "NEUTRAL"), (700.0, "PE"), (1000.0, "NEUTRAL"), (1300.0, "CE"), (1800.0, "NEUTRAL")):
        out = reads(snap("12:00", ce_oi=1000.0, pe_oi=pe))
        assert out["reads"]["pcr_zone"]["direction"] == want, pe
        assert out["facts"]["pcr"] == round(pe / 1000.0, 2)
    assert "extreme" in reads(snap("12:00", pe_oi=400.0))["reads"]["pcr_zone"]["detail"]


# 14
def test_max_pain():
    chain = {100.0: {"ce_oi": 10.0, "pe_oi": 0.0}, 110.0: {"ce_oi": 5.0, "pe_oi": 5.0}, 120.0: {"ce_oi": 0.0, "pe_oi": 10.0}}
    assert cr.max_pain(chain) == 110.0          # payouts: 100 -> 250, 110 -> 200, 120 -> 250
    assert cr.max_pain({}) is None
    heavy_calls = {100.0: {"ce_oi": 100.0, "pe_oi": 0.0}, 110.0: {"ce_oi": 0.0, "pe_oi": 0.0}, 120.0: {"ce_oi": 0.0, "pe_oi": 1.0}}
    assert cr.max_pain(heavy_calls) == 100.0


# 15
def test_price_vs_oi_buildup():
    bull = snap("11:45") + snap("12:00", ce_oi=1200.0, ce_ltp=120.0, pe_oi=1200.0, pe_ltp=80.0)
    r = reads(bull)["reads"]["price_oi"]
    assert r["direction"] == "CE" and "long buildup" in r["detail"] and "put writing" in r["detail"]
    bear = snap("11:45") + snap("12:00", ce_oi=1200.0, ce_ltp=80.0, pe_oi=1200.0, pe_ltp=120.0)
    r = reads(bear)["reads"]["price_oi"]
    assert r["direction"] == "PE" and "call writing" in r["detail"] and "put buying" in r["detail"]
    mixed = snap("11:45") + snap("12:00", ce_oi=1200.0, ce_ltp=120.0, pe_oi=1200.0, pe_ltp=120.0)
    assert reads(mixed)["reads"]["price_oi"]["direction"] == "NEUTRAL"
    cover = snap("11:45") + snap("12:00", ce_oi=900.0, ce_ltp=120.0, pe_oi=1000.0, pe_ltp=100.0)
    r = reads(cover)["reads"]["price_oi"]
    assert r["direction"] == "NEUTRAL" and "short covering" in r["detail"]          # +1 only: exits count half
    assert reads(snap("12:00"))["reads"]["price_oi"]["direction"] is None


# 16
def day_with_windows(last_oi_step, last_vol_step):
    """Five 15-minute windows: four usual ones (OI +100, volume +1000 per strike), then the last."""
    rows, oi, vol = [], 1000.0, 0.0
    for hm in ("11:00", "11:15", "11:30", "11:45"):
        rows += snap(hm, ce_oi=oi, pe_oi=oi, ce_vol=vol, pe_vol=vol)
        oi += 100.0
        vol += 1000.0
    rows += snap("12:00", ce_oi=oi - 100.0, pe_oi=oi - 100.0 + last_oi_step, ce_vol=vol - 1000.0 + last_vol_step,
                 pe_vol=vol - 1000.0 + last_vol_step)
    return rows


def test_volume_backed_oi_and_churn():
    backed = reads(day_with_windows(last_oi_step=200.0, last_vol_step=1000.0))["reads"]["volume_oi"]
    assert backed["direction"] == "CE" and "backed" in backed["detail"]
    churn = reads(day_with_windows(last_oi_step=20.0, last_vol_step=5000.0))["reads"]["volume_oi"]
    assert churn["direction"] == "NEUTRAL" and "churn" in churn["detail"]
    short = reads(snap("11:45") + snap("12:00", pe_oi=1200.0, pe_vol=10.0))["reads"]["volume_oi"]
    assert short["direction"] is None


# 17
def priced_chain(ce_oi, pe_oi, hm="12:00"):
    t = cr._years_to_expiry("2026-11-17", ts(hm))
    per = {k: {"ce_ltp": cr._bs_price(SPOT, k, 0.13, t, 0.065, True),
               "pe_ltp": cr._bs_price(SPOT, k, 0.13, t, 0.065, False)} for k in STRIKES}
    return snap(hm, ce_oi=ce_oi, pe_oi=pe_oi, per=per)


def test_implied_vol_round_trip_and_gamma_regime():
    t = 5 / 365.0
    price = cr._bs_price(SPOT, 25250.0, 0.14, t, 0.065, True)
    assert abs(cr.implied_vol(SPOT, 25250.0, price, t, 0.065, True) - 0.14) < 1e-4
    assert cr.implied_vol(SPOT, 25000.0, 150.0, t, 0.065, True) is None        # below intrinsic
    pos = reads(priced_chain(ce_oi=5000.0, pe_oi=1000.0))
    assert pos["reads"]["gamma_regime"]["blocks"] == ["CE", "PE"] and pos["facts"]["net_gex"] > 0
    neg = reads(priced_chain(ce_oi=1000.0, pe_oi=5000.0))
    assert neg["reads"]["gamma_regime"]["blocks"] == [] and neg["facts"]["net_gex"] < 0
    assert reads(snap("12:00", ce_ltp=0.0, pe_ltp=0.0))["reads"]["gamma_regime"]["direction"] is None


# 18
def test_no_data_and_stale():
    assert cr.compute_reads([], SPOT, cr.load_cfg({}), day=DAY, now_ts=ts("12:00"))["status"] == "NO_DATA"
    stale = reads(snap("12:00"), now="12:30")
    assert stale["status"] == "NO_DATA" and stale["reads"] == {}
    future = reads(snap("13:00"), now="12:00")
    assert future["status"] == "NO_DATA"


# 19
def test_board_carries_the_new_reads_and_gamma_blocks_when_switched_on():
    for rid in ("pcr_zone", "price_oi", "volume_oi", "gamma_regime"):
        assert rid in SOURCE_IDS
    chain = reads(priced_chain(ce_oi=5000.0, pe_oi=1000.0))
    sc = {"desk8600_board": {"sources": {"gamma_regime": "FILTER"}, "apply_to": ["DESK8600_TICKET"]}}
    board = build_board({}, {}, sc, NOW, chain_reads=chain)
    by_id = {s["id"]: s for s in board["sources"]}
    assert by_id["gamma_regime"]["mode"] == "FILTER" and by_id["gamma_regime"]["direction"] == "ROOM"
    assert by_id["pcr_zone"]["direction"] == "NEUTRAL" and board["facts"]["max_pain"] is not None   # PCR 0.2 = extreme
    v = judge(board["sources"], "LONG", board_cfg(sc), "DESK8600_TICKET_LONG")
    assert v["blocked"] is True and "positive gamma" in " ".join(v["reasons"])
    empty = build_board({}, {}, {}, NOW)
    assert {s["id"]: s for s in empty["sources"]}["price_oi"]["direction"] is None
    assert any(l["prefix"] == "DESK8600_TICKET" for l in empty["lanes"])


# ── cockpit ──────────────────────────────────────────────────────────────────
def test_prototype_view_says_where_the_lane_is():
    from analyst.cockpit_views_8510 import prototype_view

    row = {"enabled": True, "state": "SCANNING", "paper_only_lane": False, "dynamic_e8": 25212.4, "touch_tol_pts": 3.0}
    v = prototype_view(row, {}, 25198.7, "")
    assert v["step"] == 0 and "15-minute candle" in v["headline"] and v["gap_pts"] == -13.7
    assert "Paper now" in v["route"] and v["route_live"] is False
    arm = {"direction": "SHORT", "cross_bar_open_ts": 1794464100, "away_seen": False}
    v = prototype_view(row, {"arm": dict(arm)}, 25198.7, "LIVE")
    assert v["step"] == 2 and "Armed PE" in v["headline"] and "move away" in v["headline"] and v["route_live"] is True
    v = prototype_view(row, {"arm": dict(arm, away_seen=True)}, 25198.7, "LIVE")
    assert v["step"] == 3 and "touch" in v["headline"] and "±3" in v["sub"]
    v = prototype_view(row, {"arm": arm, "open": {"direction": "LONG"}}, 25198.7, "")
    assert v["step"] == 4 and v["headline"] == "In a CE trade"
    v = prototype_view(row, {"sl_blocked_side": "LONG"}, 25198.7, "")
    assert "CE side cancelled" in v["headline"] and "arms PE" in v["headline"]
    v = prototype_view(dict(row, paper_only_lane=True, block_reason="NO TRADE — No entry before 09:30"), {"arm": arm}, None, "LIVE")
    assert "Paper only" in v["route"] and v["route_live"] is False and "09:30" in v["sub"]
    assert prototype_view({"enabled": False}, {}, None)["step"] == -1
    assert prototype_view(None, None, None)["step"] == 0


def test_cockpit_has_the_new_panels_and_no_vendor_name_on_screen():
    import re

    html = (ROOT / "Algorithmic_Trading_Workspace_8510.html").read_text(encoding="utf-8")
    for needle in ('id="protoView"', "renderProtoView(sc.ema8_prototype_view)", 'id="tklHeadline"',
                   "renderTicketLane(data.desk8600_ticket_lane)", "/api/desk8600/ticket-lane", 'id="d86Facts"',
                   'id="positionsSummary"', 'id="ordersSummary"', 'id="tradesViewBody"',
                   'renderTradeAnalysis("replayAnalysis", trades)', 'renderTradeAnalysis("drAnalysis", data.trades || [])'):
        assert needle in html, needle
    visible = re.sub(r"<script.*?</script>|<style.*?</style>|<!--.*?-->|<[^>]+>", " ", html, flags=re.S)
    assert not re.search(r"stock\s*mojo", visible, flags=re.I)
    engine = (ROOT / "engine_8510_live.py").read_text(encoding="utf-8")
    assert engine.count('"/api/desk8600/ticket-lane"') == 2
    src = (ROOT / "analyst/terminal8510_backend.py").read_text(encoding="utf-8")
    assert '"ema8_prototype_view": self._ema8_prototype_view()' in src
    assert 'payload["desk8600_ticket_lane"]' in src


# ── is the 8600 app connected, and live? ─────────────────────────────────────
def test_link_status_states():
    from analyst.desk8600_link_8510 import link_status

    class Br:
        cfg = {"base_url": "http://127.0.0.1:8600", "cache_seconds": 20}
        last = {}

    def with_row(**kw):
        b = Br()
        b.last = {"evaluated_at": NOW.isoformat(), "rows": [dict({"signal_group": "Actionable Setups"}, **kw)]}
        return b

    ok_ctx = {"status": "OK", "snapshot_time": "10:03:00", "snapshot_age_sec": 20, "expiry": "2026-11-17"}
    assert link_status(None, {}, NOW)["desk8600"]["state"] == "OFF"
    assert link_status(Br(), {}, NOW)["desk8600"]["state"] == "STARTING"
    live = link_status(with_row(verdict="NOISE", source_ts=(NOW - timedelta(seconds=40)).isoformat()), ok_ctx, NOW)
    assert live["desk8600"]["state"] == "LIVE" and "40 s ago" in live["desk8600"]["detail"] and live["chain"]["state"] == "LIVE"
    old = link_status(with_row(verdict="NOISE", source_ts=(NOW - timedelta(minutes=30)).isoformat()), {}, NOW)
    assert old["desk8600"]["state"] == "STALE" and old["chain"]["state"] == "NO_DATA"
    down = link_status(with_row(verdict="UNAVAILABLE", reason="URLError: <urlopen error [Errno 111] Connection refused>"), {}, NOW)
    assert down["desk8600"]["state"] == "NOT_ANSWERING" and "Start the 8600 app" in down["desk8600"]["fix"]
    closed = link_status(with_row(verdict="UNAVAILABLE", reason="Source session is not current"), {}, NOW, market_open=False)
    assert closed["desk8600"]["state"] == "NO_SESSION" and "Market is closed" in closed["desk8600"]["fix"]
    stale = link_status(with_row(verdict="UNAVAILABLE", reason="Closed source data stale"), {"status": "NO_DATA", "reason": "latest snapshot is 14 min old"}, NOW)
    assert stale["desk8600"]["state"] == "STALE" and stale["chain"]["state"] == "STALE"


def test_link_is_served_and_shown(tmp_path):
    from analyst.test_live_exit_orders_8510 import FakeBroker, make_test_backend

    b = make_test_backend(tmp_path, FakeBroker(configured=True))
    b.market_db = tmp_path / "none.sqlite3"
    out = b.desk8600_link()
    assert out["desk8600"]["state"] == "OFF" and out["build"]["label"] and len(out["whats_new"]) >= 5
    html = (ROOT / "Algorithmic_Trading_Workspace_8510.html").read_text(encoding="utf-8")
    assert 'id="deskLinkStrip"' in html and "pollDesk8600Direct" in html and "renderDeskLink(data.desk8600_link)" in html
    assert '"/api/desk8600/link"' in (ROOT / "engine_8510_live.py").read_text(encoding="utf-8")


# ── the Prototype's own line in the live banner ──────────────────────────────
def test_prototype_check_status_and_banner_wiring(tmp_path):
    import time as _t
    from analyst.test_live_exit_orders_8510 import FakeBroker, make_test_backend

    b = make_test_backend(tmp_path, FakeBroker(configured=True))
    b._ema8_prototype_state = {}
    st = b.ema8_prototype_check_status()
    assert st["armed"] is False and st["refresh_sec"] == 30.0 and st["tol_pts"] > 0 and "at" not in st
    b._ema8_prototype_state = {"arm": {"direction": "SHORT", "away_seen": True}}
    b._ema8_proto_last_check = {"e8": 25212.4, "spot": 25230.1, "tick_high": 25231.0, "tick_low": 25228.0,
                                "tol_pts": 3.0, "at": "10:31:30", "at_ts": _t.time() - 12, "refresh_sec": 30.0,
                                "armed": True, "direction": "SHORT", "away_seen": True, "verdict": "ARMED PE"}
    st = b.ema8_prototype_check_status()
    assert st["armed"] and st["e8"] == 25212.4 and 11 <= st["age_sec"] <= 13 and 17 <= st["next_in_sec"] <= 19
    src = (ROOT / "analyst/terminal8510_backend.py").read_text(encoding="utf-8")
    assert "self._ema8_proto_last_check = {" in src
    eng = (ROOT / "engine_8510_live.py").read_text(encoding="utf-8")
    assert 'res["prototype_check"] = backend.ema8_prototype_check_status()' in eng
    html = (ROOT / "Algorithmic_Trading_Workspace_8510.html").read_text(encoding="utf-8")
    for needle in ('class="bnr-grid"', 'id="protoLineLive"', 'id="protoLineCheck"', 'id="dhanEmaTick"', "renderProtoLineRow(cross)",
                   'id="bnrLink8510"', 'id="bnrLink8600"', 'id="bnrLinkChain"'):
        assert needle in html, needle
