"""
test_confluence.py — the confluence board must be unit-testable with no DB,
no Dhan token and no market session. That is the whole point of keeping
confluence.py pure.

The tests that matter most are the negative ones: a refuted engine must never
reach consensus, a stale row must never be counted, and an unrecognised verdict
must stay UNKNOWN rather than being guessed into a direction.
"""
import ast
import sqlite3
import tempfile
import time
from pathlib import Path

import pytest

from app.engine import confluence as cf


# ─────────────────────────────────────────────────────────────────────────────
# TEXT CLASSIFICATION
# ─────────────────────────────────────────────────────────────────────────────

def test_longest_phrase_wins():
    """'bullish reversal' must not degrade to plain 'bullish'."""
    assert cf.classify_text("Stage 3 Bullish Reversal confirmed") == "REVERSAL_UP"
    assert cf.classify_text("Strong Bearish structure") == "BEARISH"
    assert cf.classify_text("Mid-range consolidation (hold / trail)") == "CONSOLIDATION"


def test_unknown_text_is_not_guessed():
    """An unrecognised verdict stays UNKNOWN and votes zero."""
    assert cf.classify_text("zorble flimflam 42") == "UNKNOWN"
    assert cf.classify_text("") == "UNKNOWN"
    assert cf.verdict_bias("UNKNOWN") == 0


def test_bias_mapping_is_symmetric():
    assert cf.verdict_bias("BULLISH") == 1
    assert cf.verdict_bias("BEARISH") == -1
    assert cf.verdict_bias("CONSOLIDATION") == 0


# ─────────────────────────────────────────────────────────────────────────────
# ROW BUILDING
# ─────────────────────────────────────────────────────────────────────────────

def _eng(key="x", evidence="UNTESTED", in_consensus=True):
    return {"key": key, "label": key, "category": "DIRECTION",
            "endpoint": "/api/" + key, "evidence": evidence,
            "in_consensus": in_consensus}


def test_fresh_directional_row_is_counted():
    now = time.time()
    row = cf.build_row(_eng(), {"bias": "BULLISH", "ts": now - 10}, now)
    assert row["verdict"] == "BULLISH"
    assert row["status"] == "FRESH"
    assert row["counted"] is True


def test_stale_row_is_shown_but_never_counted():
    now = time.time()
    row = cf.build_row(_eng(), {"bias": "BULLISH", "ts": now - 600}, now)
    assert row["status"] == "STALE"
    assert row["counted"] is False
    assert "not fresh" in row["reason"]


def test_refuted_engine_is_never_counted_even_when_fresh():
    """Wick zones and Gann are measured failures. They may display; they may not vote."""
    now = time.time()
    row = cf.build_row(_eng(evidence="REFUTED"), {"bias": "BULLISH", "ts": now}, now)
    assert row["verdict"] == "BULLISH"
    assert row["status"] == "FRESH"
    assert row["eligible"] is False
    assert row["counted"] is False
    assert row["reason"] == "refuted"


def test_missing_payload_is_unavailable_not_neutral():
    row = cf.build_row(_eng(), None, time.time())
    assert row["status"] == "UNAVAILABLE"
    assert row["verdict"] == "UNKNOWN"
    assert row["counted"] is False


def test_payload_without_timestamp_is_unknown_age():
    """A payload with no usable ts must not be treated as fresh."""
    row = cf.build_row(_eng(), {"bias": "BULLISH", "updated_at": "15:28:03 IST"}, time.time())
    assert row["status"] == "UNKNOWN_AGE"
    assert row["counted"] is False


def test_levels_are_extracted():
    now = time.time()
    row = cf.build_row(_eng(), {"bias": "BULLISH", "ts": now,
                                "pe_wall": 24400, "ce_wall": 24600}, now)
    assert row["levels"]["support"] == 24400.0
    assert row["levels"]["resistance"] == 24600.0


def test_engine_reporting_not_ok_is_unavailable():
    row = cf.build_row(_eng(), {"ok": False, "error": "no chain yet"}, time.time())
    assert row["status"] == "UNAVAILABLE"
    assert row["reason"] == "no chain yet"


# ─────────────────────────────────────────────────────────────────────────────
# CONSENSUS
# ─────────────────────────────────────────────────────────────────────────────

def _board(payloads, now=None):
    return cf.build_board(payloads, now or time.time())


def test_board_lists_every_registered_engine():
    board = _board({})
    cfg = cf.load_config()
    assert len(board["rows"]) == len(cfg["engines"])
    assert board["consensus"]["gate"] == "ALLOW_ALL"


def test_consensus_requires_minimum_engines():
    """Two bullish engines is not a consensus if the config demands three."""
    now = time.time()
    board = _board({"desk_bias": {"bias": "BULLISH", "ts": now},
                    "pe_ce_matrix": {"bias": "BULLISH", "ts": now}}, now)
    assert board["consensus"]["counted"] == 2
    assert board["consensus"]["state"] == "NO_CONSENSUS"
    assert board["consensus"]["gate"] == "ALLOW_ALL"


def test_bullish_consensus_gates_to_ce_only():
    now = time.time()
    board = _board({k: {"bias": "BULLISH", "ts": now} for k in
                    ("desk_bias", "pe_ce_matrix", "pcr_trinity", "smart_oi")}, now)
    assert board["consensus"]["state"] == "BULLISH"
    assert board["consensus"]["gate"] == "CE_ONLY"
    assert board["consensus"]["net_bias"] == 4


def test_bearish_consensus_gates_to_pe_only():
    now = time.time()
    board = _board({k: {"bias": "BEARISH", "ts": now} for k in
                    ("desk_bias", "pe_ce_matrix", "pcr_trinity", "smart_oi")}, now)
    assert board["consensus"]["gate"] == "PE_ONLY"


def test_disagreement_opens_the_gate():
    """Two up, two down nets to zero: no consensus, nothing blocked."""
    now = time.time()
    board = _board({"desk_bias": {"bias": "BULLISH", "ts": now},
                    "pe_ce_matrix": {"bias": "BULLISH", "ts": now},
                    "pcr_trinity": {"bias": "BEARISH", "ts": now},
                    "smart_oi": {"bias": "BEARISH", "ts": now}}, now)
    assert board["consensus"]["net_bias"] == 0
    assert board["consensus"]["gate"] == "ALLOW_ALL"


def test_refuted_engines_cannot_create_consensus():
    """Three REFUTED engines screaming bullish must still gate nothing."""
    now = time.time()
    board = _board({k: {"bias": "BULLISH", "ts": now} for k in
                    ("htf_wick_ema_rb", "gann_pivots_gaps", "rsi_divergence")}, now)
    assert board["consensus"]["counted"] == 0
    assert board["consensus"]["gate"] == "ALLOW_ALL"
    assert board["health"]["refuted_shown"] == 3


# ─────────────────────────────────────────────────────────────────────────────
# GATE BEHAVIOUR — must fail OPEN
# ─────────────────────────────────────────────────────────────────────────────

def test_gate_blocks_only_the_opposing_side():
    now = time.time()
    board = _board({k: {"bias": "BULLISH", "ts": now} for k in
                    ("desk_bias", "pe_ce_matrix", "pcr_trinity", "smart_oi")}, now)
    assert cf.gate_allows(board, "CE")[0] is True
    assert cf.gate_allows(board, "PE")[0] is False


def test_gate_fails_open_on_empty_board():
    """No data must never halt the desk."""
    assert cf.gate_allows(_board({}), "CE")[0] is True
    assert cf.gate_allows(_board({}), "PE")[0] is True
    assert cf.gate_allows({}, "CE")[0] is True
    assert cf.gate_allows(None, "PE")[0] is True


# ─────────────────────────────────────────────────────────────────────────────
# SCOREBOARD
# ─────────────────────────────────────────────────────────────────────────────

def test_signal_log_round_trip():
    now = time.time()
    board = _board({"desk_bias": {"bias": "BULLISH", "ts": now}}, now)
    with tempfile.TemporaryDirectory() as d:
        db = str(Path(d) / "t.sqlite3")
        written = cf.log_board(db, board, spot=24536.6)
        assert written == len(board["rows"])
        with sqlite3.connect(db) as conn:
            n = conn.execute("SELECT COUNT(*) FROM signal_log").fetchone()[0]
            assert n == len(board["rows"])
            gate = conn.execute("SELECT gate FROM consensus_log").fetchone()[0]
            assert gate in ("ALLOW_ALL", "CE_ONLY", "PE_ONLY")
            spot = conn.execute("SELECT spot FROM signal_log LIMIT 1").fetchone()[0]
            assert spot == pytest.approx(24536.6)


def test_log_board_never_raises_on_bad_path():
    board = _board({})
    assert cf.log_board("/nonexistent/dir/x.sqlite3", board) == 0


def test_render_table_runs():
    txt = cf.render_table(_board({}))
    assert "CONSENSUS:" in txt and "ENGINE" in txt


# ─────────────────────────────────────────────────────────────────────────────
# PYTHON 3.9 COMPATIBILITY — the desk runs 3.9.23
# ─────────────────────────────────────────────────────────────────────────────

def test_no_pep604_unions_in_confluence():
    """`str | None` parses on 3.9 but explodes when evaluated. This crash-looped
    the desk once already; the guard stays."""
    src = Path(cf.__file__).read_text(encoding="utf-8")
    assert "from __future__ import annotations" in src
    tree = ast.parse(src)
    hits = []
    for node in ast.walk(tree):
        ann = []
        if isinstance(node, (ast.AnnAssign, ast.arg)) and node.annotation:
            ann.append(node.annotation)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.returns:
            ann.append(node.returns)
        for a in ann:
            for sub in ast.walk(a):
                if isinstance(sub, ast.BinOp) and isinstance(sub.op, ast.BitOr):
                    hits.append(getattr(node, "lineno", "?"))
    assert not hits, "PEP 604 unions at lines {}".format(hits)
