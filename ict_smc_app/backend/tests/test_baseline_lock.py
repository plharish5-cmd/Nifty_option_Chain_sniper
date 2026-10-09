"""8 Oct 2026 replay baseline lock — playbook TRADE/SKIP/INFO, entries, stops, targets and reasons must never
change unless the playbook itself is deliberately changed (then update BASELINE in the same commit, on purpose).

Captured 9 Oct 2026 on the code BEFORE the strike / option-flow work, so that work is proven not to touch it.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("ICT_DATA_DIR", str(Path(__file__).resolve().parent / ".data"))

from oct8_data import fifteen_min, five_min  # noqa: E402

from app.delta import adapters, confluence, engine  # noqa: E402
from app.engine import registry  # noqa: E402
from app.engine.analyze import analyze  # noqa: E402
from app.signals.playbook import evaluate  # noqa: E402

BASELINE = [
    ("B|5m|bear|1791448200", "INFO", 22211.9, None, None, []),
    ("B|5m|bull|1791450300", "INFO", 22248.6, None, None, []),
    ("B|5m|bear|1791452100", "INFO", 22186.1, None, None, []),
    ("C|5m|short|1791452100", "SKIP", 22231.8, 22257.9, 22179.9, ["15:30 is outside the trade windows"]),
]


def _sig(s):
    return (s.key, s.status, s.entry, s.sl, s.tp, s.reasons)


def test_oct8_playbook_baseline_with_15m_bias():
    assert [_sig(s) for s in evaluate(analyze(five_min(), "5m"), analyze(fifteen_min(), "15m"))] == BASELINE


def test_oct8_playbook_baseline_without_bias():
    assert [_sig(s) for s in evaluate(analyze(five_min(), "5m"), None)] == BASELINE


def test_oct8_baseline_survives_every_flow_source():
    """Delta / proxy / mock confluence may change confidence only — never a trigger."""
    an, b15 = analyze(five_min(), "5m"), analyze(fifteen_min(), "15m")
    zs = registry.build(an)
    for ad in (adapters.make("none"), adapters.MockAdapter(), adapters.FootprintFeedAdapter()):
        sigs = confluence.apply(evaluate(an, b15), engine.compute(an, zs, ad, now=2e9))
        assert [_sig(s) for s in sigs] == BASELINE
    assert not [s for s in evaluate(an, b15) if s.status == "TRADE"]
