"""analyst/test_matrix_scalp_8510.py — the Tier 3 spot scalp, recorded forward.

WHAT THIS STRATEGY IS FOR
-------------------------
To find out whether Tier 3 proximity signals can be scalped. NOT to assume they
can. Every trade is a PAPER trade recorded forward in time, which is the only
test of a signal that hindsight cannot flatter: the data does not exist yet when
the rule is fixed.

WHAT IS ALREADY KNOWN, from the engine's own forward_mfe_15m / forward_mae_15m
over 2026-11-04 and 2026-11-05 (353 Tier 3 bars):
  - The moves are real: median forward 15m MFE 15.8 pts, 52% reached +15,
    42% reached +20. On the 06 Nov session, 72.7% reached +15.
  - Whether they are CAPTURABLE is unresolved: first passage brackets to
    28.8%-51.6% against a 49.7% break-even, straddling it, because 85 of 372
    windows breached both target and stop inside the same 15 minutes and minute
    OHLC cannot order them.

So these tests assert that the INSTRUMENT is correct and safe. None of them
asserts that the strategy is profitable, because that is not known.

THE THREE THINGS THAT MUST HOLD
-------------------------------
1. It can never place a real order. It rides the v1.9.4b routing gate, which
   only downgrades, and paper execution stays permanently on.
2. It records the SPOT context, because the rule is written in spot points
   while the executor works in option premium points. Getting that conversion
   wrong is the same class of error as the Rs8.13-vs-Rs32.50 per-leg/per-trade
   mix-up, so it is asserted numerically here.
3. It reuses the existing ledger, so an open trade appears in Active Positions
   and a closed one in Orders History with no parallel table.
"""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analyst.terminal8510_backend import QuantTerminalBackend  # noqa: E402

CFG = json.loads((ROOT / "analyst" / "terminal8510_config.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def matrix_clock(monkeypatch):
    """Explicit deterministic clock; the production cutoff remains unchanged."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from analyst import terminal8510_backend as terminal
    class Clock(datetime):
        current=datetime(2026,11,6,11,30,tzinfo=ZoneInfo('Asia/Kolkata'))
        @classmethod
        def now(cls,tz=None):
            return cls.current.astimezone(tz) if tz else cls.current.replace(tzinfo=None)
    monkeypatch.setattr(terminal,'datetime',Clock)
    return Clock


def backend(tmp_path, **over):
    b = object.__new__(QuantTerminalBackend)
    ms = dict(CFG.get("matrix_scalp", {}))
    ms.update(over)
    ms["enabled"] = True
    b.cfg = {"matrix_scalp": ms, "starting_paper_capital": 100000.0}
    b.config_path = tmp_path / "config.json"
    b.ledger_db = tmp_path / "ledger.sqlite3"
    b._matrix_scalp_last = {}
    b.ema_proximity_engine = None
    b._init_sqlite()
    return b


# ─────────────────────────────────────────────────────────────────────────────
# 1. SHIPPED CONFIG — off by default, and honest about what it is
# ─────────────────────────────────────────────────────────────────────────────

def test_it_ships_disabled():
    """A strategy with no established edge must not start itself."""
    assert CFG["matrix_scalp"]["enabled"] is False


def test_it_is_tier3_only_as_requested():
    assert CFG["matrix_scalp"]["tier3_only"] is True


def test_the_starting_stop_is_20_spot_points():
    assert CFG["matrix_scalp"]["spot_stop_pts"] == 20.0
    assert CFG["matrix_scalp"]["spot_target_pts"] == 15.0


def test_the_deltas_are_the_measured_ones():
    ms = CFG["matrix_scalp"]
    assert ms["delta_win"] == 0.59
    assert ms["delta_loss"] == 0.67


def test_the_evidence_refuses_to_claim_an_edge():
    """The note must record what is unknown, not sell the idea."""
    ev = " ".join(v for k, v in CFG["matrix_scalp"].items()
                  if k.startswith("_evidence") and isinstance(v, str)).upper()
    assert "NOT VALIDATED" in ev or "NOT ESTABLISHED" in ev
    assert "28.8" in ev and "51.6" in ev, "the first-passage bracket must be recorded"
    assert "49.7" in ev, "the break-even must be recorded"
    assert "STARTING POINT" in ev, "the stop width must not be presented as tuned"


def test_the_unit_conversion_is_documented():
    """Spot points vs option premium points is the trap here."""
    ev = " ".join(v for k, v in CFG["matrix_scalp"].items()
                  if k.startswith("_evidence") and isinstance(v, str))
    assert "SPOT" in ev.upper() and "PREMIUM" in ev.upper()


def test_entries_stop_before_the_eod_square_off():
    assert CFG["matrix_scalp"]["last_entry_ist"] < "15:20"


# ─────────────────────────────────────────────────────────────────────────────
# 2. DIRECTION COMES FROM THE ENGINE, NEVER A GUESS
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("sr,expected", [
    ("STAGE_2_8EMA_SUPPORT_BOUNCE", "LONG"),
    ("1H_8EMA_SUPPORT_TOUCH", "LONG"),
    ("STAGE_3_8EMA_RESISTANCE_REJECT", "SHORT"),
    ("1H_8EMA_RESISTANCE_REJECT", "SHORT"),
    ("NONE", None),
    ("", None),
    ("ENTANGLED_CONSOLIDATION", None),
])
def test_direction_from_sr_interaction(tmp_path, sr, expected):
    """These are the exact labels the engine emits. An unrecognised label must
    yield None, not a default side -- an earlier analysis harness of mine
    inferred direction from crossover_state and ended up sampling 7 of 353
    bars."""
    b = backend(tmp_path)
    assert b._matrix_scalp_direction({"sr_interaction": sr}) == expected


def test_no_signal_without_a_direction(tmp_path):
    b = backend(tmp_path)
    assert b._matrix_scalp_direction({}) is None


# ─────────────────────────────────────────────────────────────────────────────
# 3. SPOT -> PREMIUM CONVERSION, ASSERTED NUMERICALLY
# ─────────────────────────────────────────────────────────────────────────────

class _Eng:
    """Minimal stand-in emitting one row in the engine's real shape."""
    def __init__(self, **row):
        base = {"proximity_state": "TIER_3_CONFIRMATION_TRIGGER",
                "sr_interaction": "STAGE_2_8EMA_SUPPORT_BOUNCE",
                "close_price": 24400.0, "timestamp_ist": "10:30:00",
                "risk_points": 8.5, "gap_pts": 3.2, "gap_atr_ratio": 0.12,
                "spread_4_8_atr": 0.2, "is_whipsaw_chop": False}
        base.update(row)
        self.row = base

    def compute_matrix(self, **_kw):
        return {"ok": True, "rows": [self.row]}


def sig_from(tmp_path, eng, **over):
    b = backend(tmp_path, **over)
    b.ema_proximity_engine = eng
    return b.evaluate_matrix_scalp_tier3(spot_price=eng.row.get("close_price"))


def test_premium_points_are_the_spot_rule_times_the_measured_deltas(tmp_path):
    """15 spot pts target -> 15 x 0.59 = 8.85 premium pts.
       20 spot pts stop   -> 20 x 0.67 = 13.40 premium pts.
    Passing raw spot points as sl_pts would set a stop ~50% too wide, because
    initial_sl = entry_premium - sl_pts."""
    s = sig_from(tmp_path, _Eng())
    assert len(s) == 1
    assert s[0]["target_pts"] == pytest.approx(8.85, abs=0.01)
    assert s[0]["sl_pts"] == pytest.approx(13.40, abs=0.01)
    assert s[0]["spot_target_pts"] == 15.0
    assert s[0]["spot_stop_pts"] == 20.0


def test_spot_levels_are_signed_by_direction(tmp_path):
    long_sig = sig_from(tmp_path, _Eng())[0]
    assert long_sig["spot_target_level"] == pytest.approx(24415.0)
    assert long_sig["spot_stop_level"] == pytest.approx(24380.0)
    short_sig = sig_from(tmp_path, _Eng(
        sr_interaction="STAGE_3_8EMA_RESISTANCE_REJECT"))[0]
    assert short_sig["direction"] == "SHORT" and short_sig["opt_side"] == "PE"
    assert short_sig["spot_target_level"] == pytest.approx(24385.0)
    assert short_sig["spot_stop_level"] == pytest.approx(24420.0)


def test_the_engines_own_stop_is_recorded_even_when_not_used(tmp_path):
    """So the two stop rules can be compared from the data afterwards instead
    of argued about now."""
    s = sig_from(tmp_path, _Eng(risk_points=8.5))[0]
    assert s["engine_risk_pts"] == pytest.approx(8.5)
    assert s["spot_stop_pts"] == 20.0, "fixed width still in force"


def test_engine_micro_sl_can_be_opted_into_and_is_bounded(tmp_path):
    s = sig_from(tmp_path, _Eng(risk_points=9.0), use_engine_micro_sl=True)[0]
    assert s["spot_stop_pts"] == pytest.approx(9.0)
    # A 92-point structural stop must not be adopted silently.
    s2 = sig_from(tmp_path, _Eng(risk_points=92.8), use_engine_micro_sl=True)[0]
    assert s2["spot_stop_pts"] == 20.0, "out-of-range risk must fall back"
    s3 = sig_from(tmp_path, _Eng(risk_points=0.2), use_engine_micro_sl=True)[0]
    assert s3["spot_stop_pts"] == 20.0


# ─────────────────────────────────────────────────────────────────────────────
# 4. FILTERS
# ─────────────────────────────────────────────────────────────────────────────

def test_tier2_is_excluded_when_tier3_only(tmp_path):
    assert sig_from(tmp_path, _Eng(proximity_state="TIER_2_TOUCH_ZONE")) == []


def test_tier1_and_far_are_always_excluded(tmp_path):
    for st in ("TIER_1_APPROACHING", "ZONE_FAR", "WHIPSAW_CHOP_BLOCKED"):
        assert sig_from(tmp_path, _Eng(proximity_state=st)) == [], st


def test_chop_is_excluded(tmp_path):
    assert sig_from(tmp_path, _Eng(is_whipsaw_chop=True)) == []


def test_no_data_yields_no_signal(tmp_path):
    """v1.9.4c removed the fabricated flat-candle fallback, so an unrecorded
    session must produce nothing rather than invented Tier 3 touches."""
    class _NoData:
        row = {"close_price": 24400.0}
        def compute_matrix(self, **_kw):
            return {"ok": False, "status": "NO_DATA", "rows": []}
    b = backend(tmp_path)
    b.ema_proximity_engine = _NoData()
    assert b.evaluate_matrix_scalp_tier3(spot_price=24400.0) == []


def test_disabled_means_silent(tmp_path):
    b = backend(tmp_path)
    b.cfg["matrix_scalp"]["enabled"] = False
    b.ema_proximity_engine = _Eng()
    assert b.evaluate_matrix_scalp_tier3(spot_price=24400.0) == []


def test_a_missing_engine_is_not_an_error(tmp_path):
    b = backend(tmp_path)
    b.ema_proximity_engine = None
    assert b.evaluate_matrix_scalp_tier3(spot_price=24400.0) == []


def test_cooldown_collapses_consecutive_minutes(tmp_path):
    """Hovering at the 8 EMA for four minutes is ONE signal, not four."""
    b = backend(tmp_path)
    b.ema_proximity_engine = _Eng()
    first = b.evaluate_matrix_scalp_tier3(spot_price=24400.0)
    assert len(first) == 1
    for _ in range(4):
        assert b.evaluate_matrix_scalp_tier3(spot_price=24400.0) == []


def test_long_and_short_have_independent_cooldowns(tmp_path):
    b = backend(tmp_path)
    b.ema_proximity_engine = _Eng()
    assert len(b.evaluate_matrix_scalp_tier3(spot_price=24400.0)) == 1
    b.ema_proximity_engine = _Eng(sr_interaction="STAGE_3_8EMA_RESISTANCE_REJECT")
    assert len(b.evaluate_matrix_scalp_tier3(spot_price=24400.0)) == 1


def test_a_nonpositive_spot_is_refused(tmp_path):
    for bad in (0.0, -1.0, None):
        assert sig_from(tmp_path, _Eng(close_price=bad)) == []


# ─────────────────────────────────────────────────────────────────────────────
# 5. IT RIDES THE EXISTING LEDGER AND CANNOT PLACE A REAL ORDER
# ─────────────────────────────────────────────────────────────────────────────

def test_the_position_table_carries_the_spot_columns(tmp_path):
    """Active Positions does SELECT *, so these appear with no UI change."""
    b = backend(tmp_path)
    with sqlite3.connect(b.ledger_db) as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(positions)")}
    for col in ("strategy_id", "spot_entry", "spot_target_level",
                "spot_stop_level", "spot_target_pts", "spot_stop_pts",
                "engine_risk_pts", "sr_interaction"):
        assert col in cols, "{} missing from positions".format(col)


def test_no_parallel_table_was_created(tmp_path):
    """Parallel order records are how the orders book and the position came to
    disagree about the same fill."""
    b = backend(tmp_path)
    with sqlite3.connect(b.ledger_db) as c:
        names = {r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    for forbidden in ("matrix_scalp_trades", "matrix_trades", "scalp_positions"):
        assert forbidden not in names


def test_strategy_id_is_queryable_for_the_study(tmp_path):
    """The whole point is being able to pull these trades back out later."""
    b = backend(tmp_path)
    with sqlite3.connect(b.ledger_db) as c:
        c.execute("INSERT INTO positions (position_id, symbol, side, qty, "
                  "entry_price, entry_time, status, mode, strategy_id, "
                  "spot_entry, spot_stop_pts) VALUES "
                  "('P1','NIFTY 24400 CE','BUY',65,201.5,'2026-11-07 10:30:00 IST',"
                  "'OPEN','PAPER_ONLY','matrix_scalp_long',24400.0,20.0)")
        c.commit()
        rows = [dict(zip([d[0] for d in c.execute("SELECT * FROM positions").description],
                         r)) for r in c.execute(
            "SELECT * FROM positions WHERE strategy_id LIKE 'matrix_scalp%'")]
    assert len(rows) == 1
    assert rows[0]["spot_entry"] == 24400.0
    assert rows[0]["spot_stop_pts"] == 20.0


def test_the_signal_is_marked_as_a_matrix_scalp(tmp_path):
    """The flag is what routes the spot context into the position row."""
    s = sig_from(tmp_path, _Eng())[0]
    assert s["matrix_scalp"] is True
    assert s["strategy_id"].startswith("matrix_scalp")
    assert "Matrix Scalp Tier 3" in s["setup_name"]


def test_the_advisory_note_says_it_is_unvalidated(tmp_path):
    s = sig_from(tmp_path, _Eng())[0]
    assert "PROSPECTIVE" in s["advisory_note"].upper()
    assert "NOT AN OPTIMUM" in s["advisory_note"].upper()


def test_paper_and_algo_only_remain_permanent(tmp_path):
    """The scalp cannot be used as a route to real money."""
    b = backend(tmp_path)
    b.paper_execution_enabled = False
    b.algo_only_execution = False
    assert b.paper_execution_enabled is True
    assert b.algo_only_execution is True


def test_injected_clock_preserves_exact_entry_cutoff(tmp_path,matrix_clock):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    matrix_clock.current=datetime(2026,11,6,15,5,tzinfo=ZoneInfo('Asia/Kolkata'))
    assert len(sig_from(tmp_path,_Eng()))==1
    matrix_clock.current=datetime(2026,11,6,15,6,tzinfo=ZoneInfo('Asia/Kolkata'))
    assert sig_from(tmp_path,_Eng())==[]
