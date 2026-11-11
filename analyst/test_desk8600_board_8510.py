"""The 8600 board: one switch per option-chain source.

What each test pins down:
 1. With no switch on, nothing is ever skipped, whatever the sources say.
 2. One source switched on and pointing the other way skips the trade; the
    same source agreeing, neutral or without data does not.
 3. Rule MAJORITY: skipped only when the sources against are not outnumbered.
 4. "Neutral = skip": a switched-on neutral source skips; with the 8600 ticket
    switched on this means a trade is taken only on a same-side ticket.
 5. A ticket row that is not significant reads NEUTRAL, not its tone.
 6. A stale or unavailable 8600 row is "no data" and never skips.
 7. Walls: no room up skips CE only; boxed skips both; room is not a vote.
 8. The board applies only to the lanes selected.
 9. Cockpit updates: source on/off, rule, neutral, lanes, all on/off; junk ignored.
10. Chain sources: day bias, 15-minute impulse and 3-minute bar read CE/PE by sign.
11. Backend: a switched-on opposing source is recorded as blocked; the board
    is in telemetry, the server has the endpoints, the cockpit has the panel.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analyst.desk8600_board_8510 import (  # noqa: E402
    LANE_PREFIXES,
    SOURCE_IDS,
    apply_update,
    build_board,
    judge,
    load_cfg,
)

IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 11, 11, 12, 0, 30, tzinfo=IST)
BAR_END = (NOW - timedelta(seconds=30)).isoformat()


def row(group, direction, significant=False, verdict="X", source_ts=BAR_END):
    return {"signal_group": group, "direction": direction, "significant": significant,
            "verdict": verdict, "source_ts": source_ts, "reason": "r"}


def bridge(tape="PE", ticket=("PE", True), hc=("PE", False), flow="PE", fresh="PE", total="CE", pcr="NEUTRAL"):
    return {"evaluated_at": NOW.isoformat(), "rows": [
        row("State Change", tape, verdict="🎯 PULLBACK SELL"),
        row("Actionable Setups", ticket[0], ticket[1], "🎯 PULLBACK SELL"),
        row("High Conviction", hc[0], hc[1], "🎯 PULLBACK SELL"),
        row("Classic Writer Flow", flow, verdict="BEARISH_DOMINANCE"),
        row("Fresh Change in OI", fresh, verdict="BEARISH_REGIME"),
        row("Cumulative Total OI", total, verdict="POSITIONAL_PUT_EXCESS"),
        row("PCR OI vs Futures", pcr, verdict="NEUTRAL_EQUILIBRIUM"),
    ]}


def ctx(bias="PE", flow_3m=-100000.0, impulse_15m=-900000.0, up=120.0, down=150.0, boxed=False):
    return {"status": "OK", "bias": bias, "net_pe_minus_ce": -4356000.0, "imbalance_pct": -40.0,
            "put_added": 100.0, "call_added": 200.0, "flow_3m": flow_3m, "impulse_15m": impulse_15m,
            "call_added_3m": 1.0, "put_added_3m": 1.0, "call_added_15m": 1.0, "put_added_15m": 1.0,
            "call_wall_above": 25300.0, "put_wall_below": 25000.0, "room_up_pts": up,
            "room_down_pts": down, "boxed": boxed, "snapshot_age_sec": 20}


def board(scanner, br=None, c=None):
    return build_board(br if br is not None else bridge(), c if c is not None else ctx(), scanner, NOW)


def on(*ids, **extra):
    return {"desk8600_board": dict({"sources": {i: "FILTER" for i in ids}}, **extra)}


def verdict(scanner, direction, sid="EMA8_PROTOTYPE_LONG", **kw):
    b = board(scanner, **kw)
    return judge(b["sources"], direction, load_cfg(scanner), sid)


# 1
def test_no_switch_on_never_skips():
    for d in ("LONG", "SHORT"):
        v = verdict({}, d)
        assert v["blocked"] is False and v["reasons"] == [] and v["agree"] == 0 and v["against"] == 0
    # ...but the record still shows which sources were against a CE signal
    assert "tape_3m" in verdict({}, "LONG")["sources_against_all"]


# 2
def test_one_source_against_skips_and_agreeing_neutral_or_missing_does_not():
    assert verdict(on("tape_3m"), "LONG")["blocked"] is True
    assert verdict(on("tape_3m"), "SHORT")["blocked"] is False
    assert verdict(on("pcr_futures"), "LONG")["blocked"] is False            # neutral
    assert verdict(on("tape_3m"), "LONG", br={"rows": []})["blocked"] is False  # 8600 not answering


# 3
def test_majority_rule():
    two_against_one_for = on("tape_3m", "fresh_oi", "total_oi", rule="MAJORITY")
    assert verdict(two_against_one_for, "LONG")["blocked"] is True
    one_against_one_for = on("tape_3m", "total_oi", rule="MAJORITY")
    assert verdict(one_against_one_for, "LONG")["blocked"] is True           # a tie is not a majority
    assert verdict(two_against_one_for, "SHORT")["blocked"] is False         # 2 agree, 1 against
    assert verdict(on("tape_3m", "fresh_oi", "total_oi"), "SHORT")["blocked"] is True   # default rule: any against


# 4
def test_neutral_skip_makes_the_ticket_a_requirement():
    cfg = on("ticket", neutral_blocks=True)
    assert verdict(cfg, "SHORT")["blocked"] is False                          # PE ticket on the latest bar
    assert verdict(cfg, "LONG")["blocked"] is True
    no_ticket = bridge(ticket=("PE", False))
    assert verdict(cfg, "SHORT", br=no_ticket)["blocked"] is True             # no ticket -> neutral -> skip
    assert verdict(on("ticket"), "SHORT", br=no_ticket)["blocked"] is False   # without the option: taken


# 5
def test_insignificant_ticket_reads_neutral():
    b = board({}, br=bridge(ticket=("PE", False)))
    t = next(s for s in b["sources"] if s["id"] == "ticket")
    assert t["direction"] == "NEUTRAL" and "no ticket" in t["detail"]


# 6
def test_stale_or_unavailable_row_is_no_data():
    old = (NOW - timedelta(minutes=12)).isoformat()
    br = bridge()
    br["rows"][0]["source_ts"] = old
    br["rows"][3]["verdict"] = "UNAVAILABLE"
    b = board(on("tape_3m", "writer_flow"), br=br)
    got = {s["id"]: s["direction"] for s in b["sources"]}
    assert got["tape_3m"] is None and got["writer_flow"] is None
    assert judge(b["sources"], "LONG", load_cfg(on("tape_3m", "writer_flow")), "EMA8_PROTOTYPE_LONG")["blocked"] is False


# 7
def test_walls():
    tight_up = ctx(up=20.0)
    assert verdict(on("walls"), "LONG", c=tight_up)["blocked"] is True
    assert verdict(on("walls"), "SHORT", c=tight_up)["blocked"] is False
    boxed = ctx(up=40.0, down=45.0, boxed=True)
    assert verdict(on("walls"), "LONG", c=boxed)["blocked"] and verdict(on("walls"), "SHORT", c=boxed)["blocked"]
    roomy = verdict(on("walls", neutral_blocks=True), "LONG")
    assert roomy["blocked"] is False and roomy["agree"] == 0                  # room is not a vote, nor "neutral"


# 8
def test_applies_only_to_selected_lanes():
    cfg = on("tape_3m")                                                       # default lanes: Prototype only
    assert verdict(cfg, "LONG", sid="STAGE_2_LONG")["blocked"] is False
    assert verdict(cfg, "LONG", sid="STAGE_2_LONG")["would_block"] is True
    cfg["desk8600_board"]["apply_to"] = ["STAGE_2", "FLAGSHIP"]
    assert verdict(cfg, "LONG", sid="STAGE_2_LONG")["blocked"] is True
    assert verdict(cfg, "LONG", sid="EMA8_PROTOTYPE_LONG")["blocked"] is False


# 9
def test_cockpit_updates():
    sc = {}
    assert load_cfg(sc)["sources"] == {} and load_cfg(sc)["apply_to"] == ["EMA8_PROTOTYPE"]
    apply_update(sc, {"source": "oi_3m", "on": True})
    apply_update(sc, {"source": "walls", "on": True})
    apply_update(sc, {"source": "oi_3m", "on": False})
    apply_update(sc, {"source": "made_up", "on": True})
    assert load_cfg(sc)["sources"] == {"walls": "FILTER"}
    apply_update(sc, {"rule": "majority"})
    apply_update(sc, {"rule": "nonsense"})
    apply_update(sc, {"neutral_blocks": True})
    apply_update(sc, {"lane": "STAGE_3", "on": True})
    apply_update(sc, {"lane": "EMA8_PROTOTYPE", "on": False})
    apply_update(sc, {"lane": "NOT_A_LANE", "on": True})
    c = load_cfg(sc)
    assert c["rule"] == "MAJORITY" and c["neutral_blocks"] is True and c["apply_to"] == ["STAGE_3"]
    assert set(apply_update(sc, {"all_sources": True})["sources"]) == set(SOURCE_IDS)
    assert apply_update(sc, {"all_sources": False})["sources"] == {}
    assert set(LANE_PREFIXES) >= {"EMA8_PROTOTYPE", "STAGE_2", "STAGE_3", "CROSSOVER", "FLAGSHIP"}


# 10
def test_chain_sources_read_by_sign_and_missing_context_is_no_data():
    got = {s["id"]: s["direction"] for s in board({}, c=ctx(bias="CE", flow_3m=50.0, impulse_15m=-50.0))["sources"]}
    assert (got["day_oi"], got["oi_3m"], got["oi_15m"], got["walls"]) == ("CE", "CE", "PE", "ROOM")
    none = {s["id"]: s["direction"] for s in board({}, c={"status": "NO_DATA", "reason": "x"})["sources"]}
    assert none["day_oi"] is None and none["oi_3m"] is None and none["walls"] is None
    b = board(on("day_oi"), c=ctx(bias=None))
    assert next(s for s in b["sources"] if s["id"] == "day_oi")["direction"] == "NEUTRAL"
    assert b["preview"]["CE"]["would_block"] is False and b["filters_on"] == 1


# 11
def test_backend_records_and_blocks(tmp_path):
    from analyst.test_live_exit_orders_8510 import FakeBroker, make_test_backend

    class Bridge:
        last = None

    b = make_test_backend(tmp_path, FakeBroker(configured=True))
    b.market_db = tmp_path / "none.sqlite3"
    b._oi_context_db_paths = lambda: []
    b._ema8_prototype_state = {}
    b.desk8600_bridge = Bridge()
    stamp = datetime.now(IST).isoformat()
    Bridge.last = {"evaluated_at": stamp, "rows": [row("State Change", "PE", source_ts=stamp)]}

    out = b.update_desk8600_board({"source": "tape_3m", "on": True})
    assert out["ok"] and out["filters_on"] == 1
    assert json.loads(b.config_path.read_text())["scanner"]["desk8600_board"]["sources"] == {"tape_3m": "FILTER"}

    brd = b.desk8600_board()
    sig = {"strategy_id": "EMA8_PROTOTYPE_LONG", "direction": "LONG"}
    v = judge(brd["sources"], "LONG", load_cfg(b.scanner_cfg), sig["strategy_id"])
    assert v["blocked"] is True
    b.log_signal_desk8600("uid-9", sig, brd, v)
    with sqlite3.connect(b.ledger_db) as conn:
        r = conn.execute("SELECT signal_uid, side, filters_on, against, blocked, readings FROM signal_desk8600").fetchone()
    assert r[:5] == ("uid-9", "CE", 1, 1, 1)
    assert json.loads(r[5])["tape_3m"]["direction"] == "PE" and json.loads(r[5])["day_oi"]["direction"] is None


def test_wiring_is_present():
    src = (ROOT / "analyst/terminal8510_backend.py").read_text(encoding="utf-8")
    assert 'payload["desk8600_board"]' in src and "self.log_signal_desk8600(signal_uid, sig, board, verdict)" in src
    eng = (ROOT / "engine_8510_live.py").read_text(encoding="utf-8")
    assert eng.count('"/api/desk8600/board"') == 2 and "backend.desk8600_bridge" in eng
    html = (ROOT / "Algorithmic_Trading_Workspace_8510.html").read_text(encoding="utf-8")
    assert 'id="d86Rows"' in html and "renderDesk8600Board(data.desk8600_board)" in html
