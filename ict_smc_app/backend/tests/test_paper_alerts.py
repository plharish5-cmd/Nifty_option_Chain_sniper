"""Paper-trade outcome rules and alert dedupe, on a throw-away SQLite file."""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

os.environ["ICT_DATA_DIR"] = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402
from app.signals import paper  # noqa: E402
from app.signals.alerts import Alerts  # noqa: E402
from app.signals.playbook import Signal  # noqa: E402

T = 1791448800            # 8 Oct 2026 14:10 IST


def _sig(key="A|5m|short|1|2", status="TRADE"):
    return Signal(key, "A", "Raid → MSS → FVG", "5m", "short", 10, T, status, entry=22033.0, sl=22060.0, tp=21979.0,
                  risk=27.0, rr=2.0, target_src="2R", story=["test"])


def _reset():
    db.execute("DELETE FROM paper_trades")
    db.execute("DELETE FROM alerts")


def test_paper_target_and_stop_order():
    _reset()
    t = paper.maybe_open(_sig())
    assert t and t["status"] == "OPEN" and t["opened_ts"] == T + 300
    assert paper.maybe_open(_sig("A|5m|short|9|9")) is None              # one open trade at a time
    assert paper.on_1m_close({"ts": T + 240, "o": 1, "h": 99999, "l": 0, "c": 1}) is None   # before entry close: ignored
    assert paper.on_1m_close({"ts": T + 300, "o": 22033, "h": 22040, "l": 22010, "c": 22015}) is None
    done = paper.on_1m_close({"ts": T + 360, "o": 22015, "h": 22020, "l": 21975, "c": 21980})
    assert done["status"] == "TP" and done["r"] == 2.0 and done["mfe_r"] >= 2.0


def test_paper_same_candle_counts_as_stop():
    _reset()
    paper.maybe_open(_sig("A|5m|short|3|3"))
    done = paper.on_1m_close({"ts": T + 300, "o": 22033, "h": 22061, "l": 21970, "c": 22000})
    assert done["status"] == "SL" and done["r"] == -1.0


def test_paper_square_off():
    _reset()
    paper.maybe_open(_sig("A|5m|short|4|4"))
    t1514 = 1791452640    # 15:14 IST candle → closes 15:15 = square-off
    done = paper.on_1m_close({"ts": t1514, "o": 22030, "h": 22031, "l": 22020, "c": 22025})
    assert done["status"] == "EOD" and abs(done["pnl_pts"] - 8.0) < 1e-6


def test_alert_dedupe_and_cooldown():
    _reset()
    sent = []

    async def bc(m):
        sent.append(m)

    al = Alerts(bc)
    asyncio.run(al.handle([_sig("A|x|1")]))
    asyncio.run(al.handle([_sig("A|x|1")]))                      # same key → never twice
    asyncio.run(al.handle([_sig("A|x|2")]))                      # same setup/side/tf inside cooldown → downgraded
    grades = [m["alert"]["grade"] for m in sent]
    assert grades == ["trade", "skip"]
