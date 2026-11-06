"""
analyst/test_signal_observations_8510.py — the prospective observation log.

WHY THIS MATTERS MORE THAN IT LOOKS
-----------------------------------
Every filter tested on this desk was scored on data that already existed when
the idea was formed. Sixteen measures reached NOT BUILT that way, and the one
survivor (futures volume) has a direction-control statistic of t=2.86 that does
not clear Bonferroni across ~190 comparisons. The only test that cannot be
p-hacked is a prospective one, because the data does not exist yet.

So this table is the instrument. Three properties decide whether it is worth
anything, and all three are tested here:

  1. EVERY signal is recorded, traded or not. Recording only executed signals
     records the survivors, and a survivor-only sample cannot measure a filter.
  2. It GATES NOTHING and can never break execution. A logging failure must be
     swallowed, because a trade must not die for a missing INSERT.
  3. Outcomes are NOT duplicated here. position_id is stored and P&L is joined
     from positions, so there is one source of truth.
"""
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analyst.terminal8510_backend import QuantTerminalBackend  # noqa: E402


def make_backend(tmp_path):
    b = object.__new__(QuantTerminalBackend)
    b.cfg = {"starting_paper_capital": 100000.0}
    b.ledger_db = tmp_path / "ledger.sqlite3"
    b._init_sqlite()
    return b


SIG = {
    "strategy_id": "stage2_8ema_long",
    "setup_name": "Stage 2 8-EMA (Long) [Pullback Touch]",
    "direction": "LONG",
    "opt_side": "CE",
    "conviction": "STANDARD",
    "separation_atr": 0.19,
    "flips_12bar": 1,
}
CONTRACT = {"symbol": "09 Nov 2026 — NIFTY 24600 CE", "security_id": "43215", "ltp": 201.5}
CONF = {
    "score": 4, "grade": "A HIGH CONVICTION (4/5)",
    "pillars": {
        "pillar_1_trend_structure": True,
        "pillar_2_anti_chop_regime": True,
        "pillar_3_slot_volume": True,
        "pillar_4_option_writer_flow": True,
        "pillar_5_pocket_execution": False,
    },
}
VOL = {"slot_rvol_median": 2.74, "vsr_10": 1.31, "bar_accel_ratio": 1.22,
       "slot_status": "ADEQUATE"}
OI = {"bias": "BULLISH", "source": "DHAN_API"}


def rows(b):
    with sqlite3.connect(b.ledger_db) as c:
        c.row_factory = sqlite3.Row
        return [dict(r) for r in c.execute("SELECT * FROM signal_observations")]


# ─────────────────────────────────────────────────────────────────────────────
# 1. IT RECORDS WHAT THE STUDY WILL NEED
# ─────────────────────────────────────────────────────────────────────────────

def test_logs_the_pillar_state_at_signal_time(tmp_path):
    b = make_backend(tmp_path)
    uid = b.log_signal_observation(SIG, CONTRACT, CONF, VOL, OI, spot=24604.95)
    assert uid.startswith("SIG_")
    r = rows(b)
    assert len(r) == 1
    o = r[0]
    assert o["strategy_id"] == "stage2_8ema_long"
    assert o["direction"] == "LONG" and o["opt_side"] == "CE"
    assert o["score"] == 4 and o["grade"].startswith("A HIGH")
    assert o["pillar_1_trend"] == 1 and o["pillar_5_pocket"] == 0
    assert o["slot_rvol_median"] == pytest.approx(2.74)
    assert o["bar_accel_ratio"] == pytest.approx(1.22)
    assert o["slot_status"] == "ADEQUATE"
    assert o["oi_bias"] == "BULLISH" and o["oi_source"] == "DHAN_API"
    assert o["separation_atr"] == pytest.approx(0.19)
    assert o["spot"] == pytest.approx(24604.95)
    assert o["premium"] == pytest.approx(201.5)


def test_slot_is_bucketed_to_15m(tmp_path):
    """The volume baseline is slot-normalised, so observations need the slot."""
    b = make_backend(tmp_path)
    b.log_signal_observation(SIG, CONTRACT, CONF, VOL, OI, spot=24600.0)
    slot = rows(b)[0]["slot_15m"]
    assert len(slot) == 5 and slot[2] == ":"
    assert int(slot[3:]) % 15 == 0, slot


def test_outcome_is_attached_without_duplicating_pnl(tmp_path):
    """position_id is the join key; P&L stays in positions, not copied here."""
    b = make_backend(tmp_path)
    uid = b.log_signal_observation(SIG, CONTRACT, CONF, VOL, OI, spot=24600.0)
    b.mark_signal_outcome(uid, executed=True, position_id="POS_123")
    o = rows(b)[0]
    assert o["executed"] == 1 and o["position_id"] == "POS_123"
    assert not any("pnl" in k.lower() for k in o), "P&L must not be duplicated here"


# ─────────────────────────────────────────────────────────────────────────────
# 2. UNTRADED SIGNALS MUST BE RECORDED TOO — the whole point
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("reason", [
    "DUPLICATE_POSITION_OPEN", "NO_ENABLED_CONTRACT",
    "Algo-Only mode: manual orders are blocked",
    "Directional Bias: LONG_ONLY active",
])
def test_signals_that_were_not_traded_are_still_recorded(tmp_path, reason):
    """A survivor-only sample cannot measure a filter. Skips must be visible."""
    b = make_backend(tmp_path)
    uid = b.log_signal_observation(SIG, CONTRACT, CONF, VOL, OI, spot=24600.0)
    b.mark_signal_outcome(uid, executed=False, skip_reason=reason)
    o = rows(b)[0]
    assert o["executed"] == 0
    assert o["skip_reason"] == reason
    # The pillar state is still there, so skipped signals remain analysable.
    assert o["score"] == 4


def test_executed_and_skipped_can_be_compared(tmp_path):
    b = make_backend(tmp_path)
    for i, (ex, why) in enumerate([(True, ""), (False, "DUPLICATE_POSITION_OPEN"),
                                   (False, "NO_ENABLED_CONTRACT")]):
        s = dict(SIG, strategy_id="strat_{}".format(i))
        uid = b.log_signal_observation(s, CONTRACT, CONF, VOL, OI, spot=24600.0)
        b.mark_signal_outcome(uid, executed=ex, skip_reason=why)
    with sqlite3.connect(b.ledger_db) as c:
        taken = c.execute("SELECT COUNT(*) FROM signal_observations WHERE executed=1").fetchone()[0]
        missed = c.execute("SELECT COUNT(*) FROM signal_observations WHERE executed=0").fetchone()[0]
    assert (taken, missed) == (1, 2)


# ─────────────────────────────────────────────────────────────────────────────
# 3. IT MUST NEVER BREAK A TRADE
# ─────────────────────────────────────────────────────────────────────────────

def test_missing_confluence_does_not_raise(tmp_path):
    b = make_backend(tmp_path)
    uid = b.log_signal_observation(SIG, CONTRACT, None, None, None, spot=None)
    assert uid
    o = rows(b)[0]
    assert o["score"] is None
    assert o["pillar_1_trend"] is None, "absent pillars must be NULL, not False"


def test_unwritable_ledger_is_swallowed(tmp_path):
    """A logging failure must not stop a trade."""
    b = make_backend(tmp_path)
    b.ledger_db = tmp_path / "nonexistent-dir" / "ledger.sqlite3"
    uid = b.log_signal_observation(SIG, CONTRACT, CONF, VOL, OI, spot=24600.0)
    assert uid, "must still return a uid"
    b.mark_signal_outcome(uid, True, "POS_1")   # must not raise either


def test_mark_outcome_with_empty_uid_is_a_noop(tmp_path):
    b = make_backend(tmp_path)
    b.mark_signal_outcome("", True, "POS_1")
    # A no-op must not even create the table, and must not raise.
    with sqlite3.connect(b.ledger_db) as c:
        n = c.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
                      "AND name='signal_observations'").fetchone()[0]
    assert n == 0


def test_garbage_pillar_values_do_not_raise(tmp_path):
    b = make_backend(tmp_path)
    bad = {"score": "not-a-number", "pillars": {"pillar_3_slot_volume": "yes"}}
    uid = b.log_signal_observation(SIG, CONTRACT, bad, VOL, OI, spot=24600.0)
    assert uid
    # score was unparseable so the row is skipped rather than written wrong;
    # either behaviour is acceptable as long as nothing raised.


# ─────────────────────────────────────────────────────────────────────────────
# 4. IT GATES NOTHING
# ─────────────────────────────────────────────────────────────────────────────

def test_logging_returns_no_decision(tmp_path):
    """The return value is an id, never a boolean a caller could gate on."""
    b = make_backend(tmp_path)
    uid = b.log_signal_observation(SIG, CONTRACT, CONF, VOL, OI, spot=24600.0)
    assert isinstance(uid, str)
    assert not isinstance(uid, bool)


def test_table_is_created_on_demand(tmp_path):
    """No migration step required; the first write creates it."""
    b = make_backend(tmp_path)
    with sqlite3.connect(b.ledger_db) as c:
        before = c.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
            "AND name='signal_observations'").fetchone()[0]
    b.log_signal_observation(SIG, CONTRACT, CONF, VOL, OI, spot=24600.0)
    with sqlite3.connect(b.ledger_db) as c:
        after = c.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
            "AND name='signal_observations'").fetchone()[0]
    assert before == 0 and after == 1
