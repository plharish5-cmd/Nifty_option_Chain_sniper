"""CRT phase cockpit is display-only: it labels phases from evaluate_pair output and never mutates it."""
import copy

from app.engine import crt_phase
from tests.test_crt_tbs import htf_rows, ltf_sell, ltf_buy, run


def _snap(res):
    return copy.deepcopy({k: v for k, v in res.items() if k != "signals"}), [s.to_dict() for s in res["signals"]]


def test_sell_reaches_entry_phase_and_does_not_mutate():
    res = run(htf_rows(), ltf_sell())
    before = _snap(res)
    rows = crt_phase.phases(res, ltf_sell())
    assert _snap(res) == before
    r = rows[-1]
    assert r["phase"] in ("LTF MSS CONFIRMED", "50% CRM HIT", "OPPOSING EXTREME HIT", "INVALIDATED")
    assert r["trade"] and r["trade"]["side"].startswith("SHORT")
    assert r["sweep"] is not None and r["progress"] >= 0
    assert [s["phase"] for s in r["steps"]][:2] == ["ACCUMULATION", "TBS SWEEP"]


def test_buy_mirror():
    res = run(htf_rows(bull=True), ltf_buy())
    r = crt_phase.phases(res, ltf_buy())[-1]
    assert r["trade"] and r["trade"]["side"].startswith("LONG")


def test_no_sweep_stays_accumulation_or_invalid():
    res = run(htf_rows(), ltf_sell(upto_min=1))
    for r in crt_phase.phases(res, []):
        assert r["phase"] in ("ACCUMULATION", "INVALIDATED") and r["trade"] is None
