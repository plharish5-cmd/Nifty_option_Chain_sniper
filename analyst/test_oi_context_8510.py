"""Option-chain context for 8510 signals.

What each test pins down:
 1. More calls added than puts since 09:15 -> bias PE; a CE signal fails the
    direction gate, a PE signal passes.
 2. More puts added -> bias CE.
 3. Adds close together -> NEUTRAL, and both sides fail the direction gate.
 4. The 30-minute flow running against the bias is reported as fading and
    fails the agreement gate for the bias side.
 5. Walls: nearest heavy call strike above / put strike below, room in points,
    too little room fails the room gate, and close walls on both sides = boxed.
 6. No snapshots, one snapshot only, or a stale snapshot -> NO_DATA and no
    gate fails (fail-open).
 7. A gate that is switched off never produces a reason.
 8. The loader reads `chain_snap`, picks this week's expiry and honours as-of.
 9. Config: defaults, the nested block, and the cockpit's flat key.
10. Backend: the context reaches the Prototype state as `oi_bias` and is
    recorded per signal. Blocking is the 8600 board's job (test_desk8600_board_8510).
11. The 3-minute bar and the 15-minute impulse of the PE - CE number.

Every test uses private databases in tmp_path.
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analyst.oi_context_8510 import (  # noqa: E402
    DEFAULT_CFG,
    compute_context,
    evaluate_gates,
    load_cfg,
    load_day_snapshots,
    recorded_days,
)

IST = timezone(timedelta(hours=5, minutes=30))
DAY = "2026-11-11"
SPOT = 25200.0
STRIKES = [25000.0, 25100.0, 25200.0, 25300.0, 25400.0]


def ts(hm: str, day: str = DAY) -> int:
    return int(datetime.strptime(f"{day} {hm}", "%Y-%m-%d %H:%M").replace(tzinfo=IST).timestamp())


def snap(hm: str, ce: Dict[float, float], pe: Dict[float, float], day: str = DAY) -> List[dict]:
    return [
        {"ts": ts(hm, day), "strike": k, "ce_oi": ce.get(k, 0.0), "pe_oi": pe.get(k, 0.0),
         "ce_vol": 10.0, "pe_vol": 20.0, "expiry": "2026-11-17"}
        for k in STRIKES
    ]


def flat(v: float) -> Dict[float, float]:
    return {k: v for k in STRIKES}


def day_rows(call_add: float, put_add: float, mid: Tuple[float, float] = None) -> List[dict]:
    """09:15 baseline, optional 11:30 snapshot, 12:00 latest. Adds are per strike."""
    rows = snap("09:15", flat(1000.0), flat(1000.0))
    if mid is not None:
        rows += snap("11:30", flat(1000.0 + mid[0]), flat(1000.0 + mid[1]))
    rows += snap("12:00", flat(1000.0 + call_add), flat(1000.0 + put_add))
    return rows


def ctx_of(rows, spot=SPOT, now="12:01", **over):
    cfg = dict(DEFAULT_CFG, **over)
    return compute_context(rows, spot, cfg, day=DAY, now_ts=ts(now)), cfg


# 1
def test_more_calls_added_is_pe_bias_and_ce_signal_fails_direction():
    ctx, cfg = ctx_of(day_rows(call_add=500.0, put_add=100.0))
    assert ctx["status"] == "OK" and ctx["bias"] == "PE"
    assert ctx["call_added"] == 2500 and ctx["put_added"] == 500
    assert ctx["net_pe_minus_ce"] == -2000
    ce = evaluate_gates(ctx, "LONG", cfg)
    pe = evaluate_gates(ctx, "SHORT", cfg)
    assert ce["direction"]["pass"] is False and ce["would_block"] is True
    assert pe["direction"]["pass"] is True


# 2
def test_more_puts_added_is_ce_bias():
    ctx, cfg = ctx_of(day_rows(call_add=100.0, put_add=500.0))
    assert ctx["bias"] == "CE"
    assert evaluate_gates(ctx, "LONG", cfg)["direction"]["pass"] is True
    assert evaluate_gates(ctx, "SHORT", cfg)["direction"]["pass"] is False


# 3
def test_adds_close_together_is_neutral_and_fails_both_sides():
    ctx, cfg = ctx_of(day_rows(call_add=500.0, put_add=450.0))
    assert ctx["bias"] is None and "NEUTRAL" in ctx["bias_label"]
    assert evaluate_gates(ctx, "LONG", cfg)["direction"]["pass"] is False
    assert evaluate_gates(ctx, "SHORT", cfg)["direction"]["pass"] is False


# 4
def test_flow_against_the_bias_is_fading_and_fails_agreement():
    # 11:30: puts far ahead. 12:00: still CE bias, but calls gained on puts in the last 30 minutes.
    ctx, cfg = ctx_of(day_rows(call_add=300.0, put_add=600.0, mid=(50.0, 600.0)))
    assert ctx["bias"] == "CE"
    assert ctx["slope_state"] == "FALLING" and ctx["fading"] is True
    g = evaluate_gates(ctx, "LONG", cfg)
    assert g["direction"]["pass"] is True and g["agreement"]["pass"] is False
    assert g["would_block"] is True


def test_flow_with_the_bias_passes_agreement():
    ctx, cfg = ctx_of(day_rows(call_add=100.0, put_add=600.0, mid=(100.0, 300.0)))
    assert ctx["bias"] == "CE" and ctx["slope_state"] == "RISING" and ctx["fading"] is False
    assert evaluate_gates(ctx, "LONG", cfg)["agreement"]["pass"] is True


# 5
def walls_rows(ce_wall: float, pe_wall: float) -> List[dict]:
    ce0, pe0 = flat(1000.0), flat(1000.0)
    ce1 = {k: 1100.0 for k in STRIKES}
    pe1 = {k: 1500.0 for k in STRIKES}
    ce1[ce_wall] = 9000.0
    pe1[pe_wall] = 9000.0
    return snap("09:15", ce0, pe0) + snap("12:00", ce1, pe1)


def test_walls_and_room_gate():
    ctx, cfg = ctx_of(walls_rows(25300.0, 25000.0), spot=25280.0, wall_top_n=1)
    assert ctx["call_wall_above"] == 25300.0 and ctx["put_wall_below"] == 25000.0
    assert ctx["room_up_pts"] == 20.0 and ctx["room_down_pts"] == 280.0
    assert ctx["boxed"] is False
    ce = evaluate_gates(ctx, "LONG", cfg)
    assert ce["room"]["pass"] is False and "25300" in ce["room"]["why"]
    assert evaluate_gates(ctx, "SHORT", cfg)["room"]["pass"] is True


def test_close_walls_on_both_sides_is_boxed():
    ctx, cfg = ctx_of(walls_rows(25300.0, 25200.0), spot=25250.0, wall_top_n=1)
    assert ctx["boxed"] is True
    assert evaluate_gates(ctx, "LONG", cfg)["room"]["pass"] is False
    assert evaluate_gates(ctx, "SHORT", cfg)["room"]["pass"] is False


# 6
def test_no_data_cases_fail_open():
    cfg = dict(DEFAULT_CFG)
    empty = compute_context([], SPOT, cfg, day=DAY, now_ts=ts("12:00"))
    one = compute_context(snap("09:15", flat(1.0), flat(1.0)), SPOT, cfg, day=DAY, now_ts=ts("09:16"))
    stale, _ = ctx_of(day_rows(500.0, 100.0), now="12:30")
    for c in (empty, one, stale):
        assert c["status"] == "NO_DATA" and c["bias"] is None and c["reason"]
        g = evaluate_gates(c, "LONG", cfg)
        assert g["would_block"] is False and g["reasons"] == []


def test_snapshots_after_now_are_not_read():
    rows = day_rows(call_add=500.0, put_add=100.0) + snap("13:00", flat(1000.0), flat(9000.0))
    ctx, _ = ctx_of(rows, now="12:01")
    assert ctx["bias"] == "PE" and ctx["snapshot_time"].startswith("12:00")


# 7
def test_switched_off_gate_gives_no_reason():
    ctx, cfg = ctx_of(day_rows(call_add=500.0, put_add=100.0), gate_direction=False, gate_room=False)
    g = evaluate_gates(ctx, "LONG", cfg)
    assert g["direction"]["pass"] is False          # still reported
    assert g["would_block"] is False and g["reasons"] == []


# 8
def make_db(path: Path, rows: List[dict], extra_expiry: str = "") -> None:
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE chain_snap (expiry TEXT, ts REAL, strike REAL, ce_oi REAL, pe_oi REAL, "
                     "ce_ltp REAL, pe_ltp REAL, ce_vol REAL, pe_vol REAL)")
        for r in rows:
            conn.execute("INSERT INTO chain_snap VALUES (?,?,?,?,?,?,?,?,?)",
                         (r["expiry"], r["ts"], r["strike"], r["ce_oi"], r["pe_oi"], 0, 0, r["ce_vol"], r["pe_vol"]))
            if extra_expiry:
                conn.execute("INSERT INTO chain_snap VALUES (?,?,?,?,?,?,?,?,?)",
                             (extra_expiry, r["ts"], r["strike"], 7.0, 7.0, 0, 0, 1, 1))


def test_loader_reads_nearest_expiry_and_respects_asof(tmp_path):
    db = tmp_path / "m.sqlite3"
    make_db(db, day_rows(500.0, 100.0), extra_expiry="2026-11-24")
    rows = load_day_snapshots([tmp_path / "missing.sqlite3", db], DAY)
    assert len(rows) == 2 * len(STRIKES) and {r["expiry"] for r in rows} == {"2026-11-17"}
    early = load_day_snapshots([db], DAY, asof_ts=ts("10:00"))
    assert len(early) == len(STRIKES)
    assert load_day_snapshots([db], "2026-11-12") == []
    assert recorded_days([db]) == [(DAY, 2)]


# 9
def test_config_defaults_nested_block_and_cockpit_key():
    assert load_cfg(None)["enabled"] is True and load_cfg({})["slope_minutes"] == 30
    assert load_cfg({"oi_context": {"min_room_pts": 60}})["min_room_pts"] == 60


def test_three_minute_bar_and_fifteen_minute_impulse():
    rows = (snap("09:15", flat(1000.0), flat(1000.0))
            + snap("11:45", flat(1100.0), flat(1500.0))      # net +2000 by 11:45
            + snap("11:57", flat(1100.0), flat(1700.0))      # net +3000
            + snap("12:00", flat(1200.0), flat(1700.0)))     # net +2500: last bar went to the calls
    ctx, _ = ctx_of(rows)
    assert ctx["bias"] == "CE"
    assert ctx["flow_3m"] == -500 and ctx["call_added_3m"] == 500 and ctx["put_added_3m"] == 0
    assert ctx["impulse_15m"] == 500 and ctx["put_added_15m"] == 1000


# 10
def _backend(tmp_path, call_add, put_add):
    from analyst import terminal8510_backend as tb
    from analyst.test_live_exit_orders_8510 import FakeBroker, make_test_backend

    b = make_test_backend(tmp_path, FakeBroker(configured=True))
    now = datetime.now(tb.IST)
    day = now.strftime("%Y-%m-%d")
    base = int(datetime.strptime(f"{day} 09:15", "%Y-%m-%d %H:%M").replace(tzinfo=IST).timestamp())
    last = int(now.timestamp()) - 30
    if last <= base:                      # run before 09:15 IST: put the baseline at midnight
        base = int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=IST).timestamp())
        b.scanner_cfg = {"oi_context": {"baseline_hm": "00:00"}}
    rows = []
    for t, c, p in ((base, 1000.0, 1000.0), (last, 1000.0 + call_add, 1000.0 + put_add)):
        rows += [{"ts": t, "strike": k, "ce_oi": c, "pe_oi": p, "ce_vol": 1, "pe_vol": 1,
                  "expiry": "2099-01-01"} for k in (24900.0, 25000.0, 25100.0)]
    b.market_db = tmp_path / "market.sqlite3"
    make_db(b.market_db, rows)
    b._oi_context_db_paths = lambda: [b.market_db]
    b._ema8_prototype_state = {}
    return b


def test_backend_context_feeds_prototype_state_and_logs_without_blocking(tmp_path):
    b = _backend(tmp_path, call_add=500.0, put_add=100.0)
    if int(datetime.now(IST).timestamp()) - 30 <= int(
        datetime.strptime(datetime.now(IST).strftime("%Y-%m-%d"), "%Y-%m-%d").replace(tzinfo=IST).timestamp()
    ):
        return                                             # the first 30 seconds of the day: nothing to compare
    ctx = b.oi_context()
    assert ctx["status"] == "OK" and ctx["bias"] == "PE"
    assert b._ema8_prototype_state["oi_bias"] == "PE"

    cfg = load_cfg(b.scanner_cfg)
    sig = {"strategy_id": "EMA8_PROTOTYPE_CE", "direction": "LONG"}
    gates = evaluate_gates(ctx, "LONG", cfg)
    assert gates["would_block"] is True
    b.log_signal_oi_context("uid-1", sig, ctx, gates, False)
    with sqlite3.connect(b.ledger_db) as conn:
        row = conn.execute("SELECT signal_uid, bias, gate_direction_pass, would_block, blocked, reasons "
                           "FROM signal_oi_context").fetchone()
    assert row[:5] == ("uid-1", "PE", 0, 1, 0) and "OI bias is PE" in row[5]


def test_backend_no_snapshots_is_no_data_and_clears_bias(tmp_path):
    b = _backend(tmp_path, 500.0, 100.0)
    b._oi_context_db_paths = lambda: [tmp_path / "nothing.sqlite3"]
    b._ema8_prototype_state = {"oi_bias": "CE"}
    ctx = b.oi_context()
    assert ctx["status"] == "NO_DATA" and "oi_bias" not in b._ema8_prototype_state


def test_backend_wiring_is_present():
    src = (ROOT / "analyst/terminal8510_backend.py").read_text(encoding="utf-8")
    assert 'payload["oi_context"]' in src
    assert "self.log_signal_oi_context(" in src and "oi_filter_block" not in src
