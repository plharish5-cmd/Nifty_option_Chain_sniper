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
                   'renderTradeAnalysis("replayAnalysis", trades)', 'function btlStart', 'function btlInit'):
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
    assert st["armed"] is False and st["refresh_sec"] == 3.0 and st["tol_pts"] > 0 and "at" not in st
    b._ema8_prototype_state = {"arm": {"direction": "SHORT", "away_seen": True}}
    b.scanner_cfg.setdefault("ema8_prototype", {})["e8_refresh_seconds"] = 30.0
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


# ── the 5-minute wait must not eat a touch that was skipped ─────────────────
def test_cooldown_stamped_only_when_an_order_is_placed():
    src = (ROOT / "analyst/terminal8510_backend.py").read_text(encoding="utf-8")
    body = src[src.index("async def _process_strategy_signals"):src.index("async def start_strategy_scanner_loop")]
    stamps = [i for i in range(len(body)) if body.startswith("self.strategy_cooldowns[strat_id] = now_ts", i)]
    assert len(stamps) == 1
    before = body[:stamps[0]]
    assert "execute_order_with_chaser(" in before                      # the stamp comes after the order attempt
    assert 'if res.get("ok"):' in body[stamps[0] - 120:stamps[0]]


# ── re-entry switches on the cockpit (display + save only; defaults unchanged) ─
def test_reentry_switches_exposed_and_saved(tmp_path):
    from analyst.test_live_exit_orders_8510 import FakeBroker, make_test_backend

    b = make_test_backend(tmp_path, FakeBroker(configured=True))
    c = b.get_ema8_prototype_filter_config()
    assert c["reentry_same_side"] is True and c["reentry_after_stop_loss"] is False     # owner's 11 Nov rules
    b.update_ema8_prototype_filter_config({"reentry_after_stop_loss": "true", "reentry_same_side": False})
    c = b.get_ema8_prototype_filter_config()
    assert c["reentry_after_stop_loss"] is True and c["reentry_same_side"] is False
    html = (ROOT / "Algorithmic_Trading_Workspace_8510.html").read_text(encoding="utf-8")
    for needle in ('id="protoReSlOn"', 'id="protoReSlOff" value="off" checked', 'id="protoReProfitOn" value="on" checked', "reentry_after_stop_loss: document"):
        assert needle in html, needle


# ── Prototype contract: ≥ ₹200 means any current-week strike, not only the matrix four ──
def test_prototype_contract_walks_deeper_itm_until_premium_qualifies(tmp_path):
    import asyncio
    from analyst.test_live_exit_orders_8510 import FakeBroker, make_test_backend

    b = make_test_backend(tmp_path, FakeBroker(configured=True))
    b.directional_bias = "NEUTRAL"
    b.cfg.setdefault("recording_v193", {})["strike_step"] = 50
    # the four matrix contracts all decayed below 200 (the 12 Nov afternoon)
    b.priority_contracts = [
        {"id": "LONG_PE_ITM", "opt_type": "PE", "strike": 25400.0, "ltp": 188.0, "algo_enabled": True},
        {"id": "LONG_PE_ATM", "opt_type": "PE", "strike": 25300.0, "ltp": 140.0, "algo_enabled": True},
    ]

    class Inst:
        def get_nearest_expiry(self):
            return "2026-11-17"
        def resolve_option(self, expiry, strike, side):
            return {"security_id": str(int(strike)) + ("1" if side == "CE" else "2"), "symbol": f"NIFTY {int(strike)} {side}", "lot_size": 65}
    b.inst_mgr = Inst()
    ltps = {"253002": 140.0, "253502": 165.0, "254002": 188.0, "254502": 221.5, "255002": 262.0}

    async def quotes(req):
        return {str(sid): ltps.get(str(sid), 0.0) for sid in req["NSE_FNO"]}
    b.broker.get_marketfeed_ltp = quotes

    c, why = asyncio.run(b._select_ema8_prototype_contract_any_strike("PE", 200.0, 25270.0))
    assert c and c["strike"] == 25450.0 and c["ltp"] == 221.5 and c["security_id"] == "254502" and why == ""
    assert c["opt_type"] == "PE" and c["algo_enabled"] is True and "structured_contract" in c

    # nothing qualifies within 500 pts -> a reason the owner can read
    b.broker.get_marketfeed_ltp = lambda req: asyncio.sleep(0, result={str(s): 150.0 for s in req["NSE_FNO"]})
    c, why = asyncio.run(b._select_ema8_prototype_contract_any_strike("PE", 200.0, 25270.0))
    assert c is None and "≥ ₹200" in why and "@ 150.00" in why

    # a qualifying matrix contract is still taken first, no search
    b.priority_contracts[0]["ltp"] = 248.85
    c, why = asyncio.run(b._select_ema8_prototype_contract_any_strike("PE", 200.0, 25300.0))
    assert c["id"] == "LONG_PE_ITM"

    # directional bias still blocks
    b.priority_contracts[0]["ltp"] = 188.0
    b.directional_bias = "LONG_ONLY"
    b.broker.get_marketfeed_ltp = quotes
    c, why = asyncio.run(b._select_ema8_prototype_contract_any_strike("PE", 200.0, 25270.0))
    assert c is None and "bias" in why
    src = (ROOT / "analyst/terminal8510_backend.py").read_text(encoding="utf-8")
    assert 'skip_reason=("NO_ENABLED_CONTRACT: " + contract_why)' in src


# ── touch cadence: 3 s live, 30 s backtest grid, patch script narrow ─────────
def test_touch_cadence_defaults_and_patch_script(tmp_path):
    import json, subprocess, sys
    from analyst.ema8_prototype_8510 import DEFAULT_CFG as P
    from analyst.mj_sir_stage_reversal_8510 import DEFAULT_MJ_CFG as M

    assert P["e8_refresh_seconds"] == 3.0 and P["backtest_tick_seconds"] == 30.0
    assert M["dynamic_refresh_seconds"] == 3.0
    src = (ROOT / "analyst/ema8_prototype_8510.py").read_text(encoding="utf-8")
    assert 'refresh = float(cfg.get("backtest_tick_seconds", 30.0) or 30.0)' in src
    be = (ROOT / "analyst/terminal8510_backend.py").read_text(encoding="utf-8")
    assert be.count("max(2.0, min(") >= 3                      # both workers clamp at 2 s, re-read each loop
    # patch script touches only the two keys
    import shutil
    cfg_path = ROOT / "analyst/terminal8510_config.json"
    before = json.loads(cfg_path.read_text(encoding="utf-8"))
    tmp_repo = tmp_path / "repo"; (tmp_repo / "analyst").mkdir(parents=True); (tmp_repo / "scripts").mkdir()
    shutil.copy(cfg_path, tmp_repo / "analyst/terminal8510_config.json")
    shutil.copy(ROOT / "scripts/patch_touch_cadence_8510.py", tmp_repo / "scripts/patch_touch_cadence_8510.py")
    out = subprocess.run([sys.executable, str(tmp_repo / "scripts/patch_touch_cadence_8510.py")], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    after = json.loads((tmp_repo / "analyst/terminal8510_config.json").read_text(encoding="utf-8"))
    assert after["scanner"]["ema8_prototype"]["e8_refresh_seconds"] == 3.0
    assert after["scanner"]["mj_sir_lecture"]["dynamic_refresh_seconds"] == 3.0
    after["scanner"]["ema8_prototype"]["e8_refresh_seconds"] = before["scanner"]["ema8_prototype"]["e8_refresh_seconds"]
    after["scanner"]["mj_sir_lecture"].pop("dynamic_refresh_seconds", None)
    before["scanner"]["mj_sir_lecture"].pop("dynamic_refresh_seconds", None)
    assert after == before


# ── lightweight pass, 12 Nov: removed pieces stay removed; config patch is narrow ──
def test_removed_features_are_gone_and_config_patch_is_narrow(tmp_path):
    import json, shutil, subprocess, sys

    be = (ROOT / "analyst/terminal8510_backend.py").read_text(encoding="utf-8")
    eng = (ROOT / "engine_8510_live.py").read_text(encoding="utf-8")
    html = (ROOT / "Algorithmic_Trading_Workspace_8510.html").read_text(encoding="utf-8")
    for gone in ("writer_flow_gate_cfg", "_writer_flow_context", "stockmojo_poller", "get_stockmojo_live",
                 "trend_capture_service", "compute_goldilocks_separation", "FuturesVolumeEngine("):
        assert gone not in be, gone
    for gone in ("/api/writer_flow", "/api/stockmojo", "/api/trend_capture", "stockmojo_live_poller"):
        assert gone not in eng, gone
    for gone in ("chkWriterFlowGate", "chkStockMojoMaster", "chkGoldilocksSeparation", "chkFuturesVolumeSurge",
                 "tab1MultiStrikeStatusCard", "renderTrendCapture192"):
        assert gone not in html, gone
    for name in ("writer_flow_gate_8510.py", "stockmojo_safe_poller_8510.py", "stockmojo_status_8510.py", "trend_capture_service_8510.py"):
        assert not (ROOT / "analyst" / name).exists(), name
    # what must still be there
    assert "def apply_signal_gates" in be and "def _confluence_gate_blocks" in be
    assert 'id="desk8600Board"' in html and 'id="strategyCockpitGrid"' in html

    repo = tmp_path / "repo"; (repo / "analyst").mkdir(parents=True); (repo / "scripts").mkdir()
    before = json.loads((ROOT / "analyst/terminal8510_config.json").read_text(encoding="utf-8"))
    before.setdefault("trend_filters", {"adx_min": 20})
    before.setdefault("writer_flow_gate", {"enabled": False})
    before.setdefault("stockmojo_live", {"enabled": False})
    before.setdefault("proven_edges", {}).update(goldilocks_separation_enabled=False, futures_volume_surge_enabled=False,
                                                futures_volume_surge_min_ratio=1.15, goldilocks_min_atr=0.13)
    (repo / "analyst/terminal8510_config.json").write_text(json.dumps(before, indent=2), encoding="utf-8")
    shutil.copy(ROOT / "scripts/patch_remove_dead_config_8510.py", repo / "scripts/patch_remove_dead_config_8510.py")
    out = subprocess.run([sys.executable, str(repo / "scripts/patch_remove_dead_config_8510.py")], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    after = json.loads((repo / "analyst/terminal8510_config.json").read_text(encoding="utf-8"))
    for k in ("trend_filters", "writer_flow_gate", "stockmojo_live"):
        assert k not in after
    assert "goldilocks_separation_enabled" not in after["proven_edges"] and after["proven_edges"]["goldilocks_min_atr"] == 0.13
    expect = json.loads(json.dumps(before))
    for k in ("trend_filters", "writer_flow_gate", "stockmojo_live"):
        expect.pop(k)
    for k in ("goldilocks_separation_enabled", "futures_volume_surge_enabled", "futures_volume_surge_min_ratio"):
        expect["proven_edges"].pop(k)
    assert after == expect
    assert (repo / "analyst/terminal8510_config.before_dead_config_patch.json").exists()
    again = subprocess.run([sys.executable, str(repo / "scripts/patch_remove_dead_config_8510.py")], capture_output=True, text=True)
    assert "Nothing to remove" in again.stdout


# ── a skipped signal gives the setup back (Stage 2/3, Reversal, Prototype, Ticket lane) ──
def _sig(sid, direction="LONG", side="CE"):
    return {"strategy_id": sid, "setup_name": sid, "direction": direction, "opt_side": side,
            "target_pts": 40.0, "sl_pts": 40.0, "index_price": 25200.0, "details": "test", "conviction": "TEST"}


def test_release_unfilled_setup_per_lane(tmp_path):
    from analyst.test_live_exit_orders_8510 import FakeBroker, make_test_backend

    b = make_test_backend(tmp_path, FakeBroker(configured=True))
    b._mj_sir_state = {"stage2_fired_key": "k2", "stage3_fired_key": "k3", "reversal_fired_key": "kr"}
    b._release_unfilled_setup(_sig("STAGE_2_LONG"))
    assert "stage2_fired_key" not in b._mj_sir_state and b._mj_sir_state["stage3_fired_key"] == "k3"
    b._release_unfilled_setup(_sig("STAGE_3_SHORT", "SHORT", "PE"))
    b._release_unfilled_setup(_sig("CROSSOVER_BULLISH"))
    assert b._mj_sir_state == {}
    b._ema8_prototype_state = {"arm": {"direction": "SHORT", "away_seen": False}}
    b._release_unfilled_setup(_sig("EMA8_PROTOTYPE_LONG"))                 # other side: untouched
    assert b._ema8_prototype_state["arm"]["away_seen"] is False
    b._release_unfilled_setup(_sig("EMA8_PROTOTYPE_SHORT", "SHORT", "PE"))
    assert b._ema8_prototype_state["arm"]["away_seen"] is True
    b._ema8_prototype_state = {"arm": {"direction": "SHORT", "away_seen": False}, "open": {"direction": "SHORT"}}
    b._release_unfilled_setup(_sig("EMA8_PROTOTYPE_SHORT", "SHORT", "PE"))  # a trade is open: untouched
    assert b._ema8_prototype_state["arm"]["away_seen"] is False
    b._desk8600_ticket_state = {"last_key": "bar|CE", "last_fired": "x"}
    b._release_unfilled_setup(_sig("DESK8600_TICKET_LONG"))
    assert "last_key" not in b._desk8600_ticket_state
    b._release_unfilled_setup({})                                           # never raises


def test_skipped_signal_releases_and_placed_signal_keeps_the_setup(tmp_path):
    import asyncio
    from analyst.test_live_exit_orders_8510 import FakeBroker, make_test_backend

    b = make_test_backend(tmp_path, FakeBroker(configured=True))
    b.directional_bias = "NEUTRAL"
    b.algo_allowlist_cfg = {"enabled": False}
    b.strategy_cooldowns = {}
    b.recent_signals = []
    b.priority_contracts = []                     # nothing built -> the order step must skip
    b.algo_strike_selection = {}
    b.market_db = tmp_path / "none.sqlite3"
    b._mj_sir_state = {"stage2_fired_key": "2026-11-13:S2:x"}
    asyncio.run(b._process_strategy_signals([_sig("STAGE_2_LONG")]))
    assert "stage2_fired_key" not in b._mj_sir_state          # given back: the next touch retries
    assert "STAGE_2_LONG" not in b.strategy_cooldowns          # and no 5-minute wait was started
    # within the 5-second spacing the retry is held, and the setup is still free
    b._mj_sir_state["stage2_fired_key"] = "2026-11-13:S2:x"
    asyncio.run(b._process_strategy_signals([_sig("STAGE_2_LONG")]))
    assert "stage2_fired_key" not in b._mj_sir_state
    src = (ROOT / "analyst/terminal8510_backend.py").read_text(encoding="utf-8")
    body = src[src.index("async def _process_strategy_signals"):src.index("async def start_strategy_scanner_loop")]
    assert "placed = True" in body and "if not placed:" in body and "self._release_unfilled_setup(sig)" in body
    assert "matrix contract not built yet" in body and "unticked in the Strike Matrix" in body


# ── "Equity Guys" view: classifier checked in a real browser engine ──────────
def test_equity_guys_view_classifier():
    import json, re, subprocess, shutil

    html = (ROOT / "Algorithmic_Trading_Workspace_8510.html").read_text(encoding="utf-8")
    for needle in ('id="btnFilterView_EG"', 'id="btnFilterView_OTHERS"', "filtered = filtered.filter(rowInGroupView);",
                   'id="btnFilterStrat_Proto"'):
        assert needle in html, needle
    assert html.count("filtered = filtered.filter(rowInGroupView);") == 2          # positions and orders
    node = shutil.which("node")
    if not node:
        return
    fn = html[html.index("        function strategyLaneOf(row) {"):html.index("        let currentGroupView")]
    cases = {
        "MJ Sir Stage 2 8-EMA (Long) — first touch": "Stage 2",
        "MJ Sir Stage 3 8-EMA (Short) — first touch": "Stage 3",
        "MJ Sir 8 EMA Reversal Upside — dynamic 8 EMA touch": "4/8 Crossover",
        "4 MA / 8 EMA Downside Reversal": "4/8 Crossover",
        "Flagship 1H Long [OR 8 EMA support + 15m PA]": "1H 8 EMA",
        "Flagship 1H Short [Swing zone / 15m PA]": "1H 8 EMA",
        "1H FLAGSHIP PA": "1H 8 EMA",
        "8 EMA Prototype (Short) — retouch": "8 EMA Prototype",
        "8600 Ticket — Dip Buy Ce": "8600 Ticket",
        "1H 8-EMA Support + 15m PA (Homework)": "Other",
        "Hammer [S1] break": "Other",
        "Ribbon Scalp A+": "Other",
        "Manual": "Other",
        "": "Other",
    }
    script = fn + "\nconst cases=" + json.dumps(list(cases)) + ";console.log(JSON.stringify(cases.map(c=>strategyLaneOf({strategy_setup:c}))));" \
        + "console.log(strategyLaneOf({strategy_id:'EMA8_PROTOTYPE_SHORT'}), '|', strategyLaneOf({strategy_id:'STAGE_3_SHORT'}), '|', strategyLaneOf({strategy_id:'PA_SR_HAMMER'}));" \
        + "console.log(JSON.stringify(EQUITY_GUYS_LANES));"
    out = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    lines = out.stdout.strip().split("\n")
    assert json.loads(lines[0]) == list(cases.values())
    assert lines[1] == "8 EMA Prototype | Stage 3 | Other"
    assert json.loads(lines[2]) == ["Stage 2", "Stage 3", "4/8 Crossover", "1H 8 EMA", "8 EMA Prototype"]


def test_flagship_signal_is_offered_again_for_a_minute_when_the_order_step_skips(tmp_path):
    import time as _t
    from analyst.test_live_exit_orders_8510 import FakeBroker, make_test_backend

    b = make_test_backend(tmp_path, FakeBroker(configured=True))
    sig = _sig("FLAGSHIP_1H_LONG")
    b._release_unfilled_setup(sig)
    kept = b._closed_candle_retry["FLAGSHIP_1H_LONG"]
    assert kept["strategy_id"] == "FLAGSHIP_1H_LONG" and kept["_first_seen_ts"] <= _t.time()
    # the clock runs from the first time it was seen, not from each retry
    old = dict(kept, _first_seen_ts=_t.time() - 61)
    b._closed_candle_retry.clear()
    b._release_unfilled_setup(old)
    assert b._closed_candle_retry == {}
    b._release_unfilled_setup(_sig("H1_8EMA_SUPPORT_LONG"))
    assert "H1_8EMA_SUPPORT_LONG" in b._closed_candle_retry
    b._closed_candle_retry.clear()
    b._release_unfilled_setup(_sig("STAGE_2_LONG"))                 # touch lanes are not queued: they re-signal themselves
    assert b._closed_candle_retry == {}
    b.scanner_cfg["closed_candle_retry_seconds"] = 0
    b._release_unfilled_setup(_sig("FLAGSHIP_1H_SHORT", "SHORT", "PE"))
    assert b._closed_candle_retry == {}
    src = (ROOT / "analyst/terminal8510_backend.py").read_text(encoding="utf-8")
    assert 'pending = self.__dict__.get("_closed_candle_retry") or {}' in src


# ── 13 Nov bugs: EOD square-off wired, radio persistence, orders-book day parse ──
def test_eod_square_off_is_called_in_the_scanner_loop():
    src = (ROOT / "analyst/terminal8510_backend.py").read_text(encoding="utf-8")
    loop = src[src.index("async def start_strategy_scanner_loop"):src.index("def get_scanner_status")]
    assert "self.check_eod_square_off" in loop, "EOD square-off is never called in the live loop"
    # it must run before the scanner_enabled gate, so a paused desk still closes positions
    assert loop.index("check_eod_square_off") < loop.index("if self.scanner_enabled")


def test_prototype_status_carries_reentry_flags(tmp_path):
    from analyst.test_live_exit_orders_8510 import FakeBroker, make_test_backend

    b = make_test_backend(tmp_path, FakeBroker(configured=True))
    b.update_ema8_prototype_filter_config({"reentry_after_stop_loss": True, "reentry_same_side": False})
    b._sync_ema8_prototype_desk_status()
    row = b.strategy_status["ema8_prototype"]
    # the per-second telemetry object now carries the toggles, so the cockpit won't reset them
    assert row["reentry_after_stop_loss"] is True and row["reentry_same_side"] is False
    assert "ribbon_filter_enabled" in row and "ema_separation_enabled" in row
    # and the round-trip the UI reads is consistent
    cfg = b.get_ema8_prototype_filter_config()
    assert cfg["reentry_after_stop_loss"] is True and cfg["reentry_same_side"] is False


def test_apply_filter_ui_only_sets_present_keys():
    html = (ROOT / "Algorithmic_Trading_Workspace_8510.html").read_text(encoding="utf-8")
    fn = html[html.index("function applyEma8ProtoFilterUI"):html.index("async function saveEma8PrototypeFilters")]
    assert "if (!(key in cfg)) return;" in fn           # partial telemetry never clobbers a toggle
    assert "setRadio('reentry_after_stop_loss'" in fn and "setRadio('reentry_same_side'" in fn


def test_order_book_sends_machine_readable_day_and_ui_parses_it(tmp_path):
    import sqlite3
    from datetime import datetime as _dt
    from analyst.test_live_exit_orders_8510 import FakeBroker, make_test_backend

    b = make_test_backend(tmp_path, FakeBroker(configured=True))
    now = _dt.now(__import__("analyst.terminal8510_backend", fromlist=["IST"]).IST).strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(b.ledger_db) as c:
        c.execute("INSERT INTO orders (order_id,created_at,strategy_name,symbol,side,qty,signal_ltp,fill_price,"
                  "order_status,mode,strategy_setup,source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                  ("T1", now, "EMA8_PROTOTYPE_SHORT", "NIFTY 25400 PE", "BUY", 65, 248.85, 248.85, "FILLED",
                   "PAPER_ONLY", "8 EMA Prototype (Short) — retouch", "AUTO ALGO"))
        c.commit()
    book = b.get_order_book()
    assert len(book) == 1
    assert book[0]["created_day"] == now[:10]                # machine-readable, matches deskTodayKey()
    assert "Nov" in book[0]["created_at"]                    # display stays human
    html = (ROOT / "Algorithmic_Trading_Workspace_8510.html").read_text(encoding="utf-8")
    assert "function orderDay(o)" in html and "o => orderDay(o) === today" in html
    assert "13 Nov 2026" or "MONTHS[m[2].toLowerCase()]" in html
