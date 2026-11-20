"""v2.0.45 — tests for the 8600 stale banner + staleness transition log.

Display + observability only: these assert what the owner SEES and what gets LOGGED.
They also assert the banner/logging never implies a trade decision (fail-open is unchanged).
"""
from __future__ import annotations

from datetime import datetime

from analyst.desk8600_link_8510 import (
    STALE_FIX_CMD,
    stale_banner,
    log_staleness_transition,
)

NOW = datetime(2026, 11, 20, 11, 46, 0)


def _status(desk_state="LIVE", chain_state="LIVE", bar_age=10, bar_time="11:45",
            desk_detail="", chain_detail=""):
    return {
        "desk8600": {"state": desk_state, "bar_age_sec": bar_age, "bar_time": bar_time,
                     "detail": desk_detail},
        "chain": {"state": chain_state, "detail": chain_detail},
    }


def test_all_live_no_banner():
    assert stale_banner(_status()) is None


def test_stale_banner_has_age_and_restart_command():
    st = _status(desk_state="STALE", bar_age=11 * 60, bar_time="11:35")
    b = stale_banner(st)
    assert b is not None
    assert b["level"] == "warn" and b["state"] == "STALE"
    assert b["age_min"] == 11
    assert STALE_FIX_CMD in b["text"]
    assert "IGNORED (fail-open)" in b["text"]  # tells owner trades still run
    assert "11 min old" in b["text"]


def test_chain_stale_parsed_from_text_when_no_numeric_age():
    st = _status(desk_state="LIVE", chain_state="STALE",
                 chain_detail="latest snapshot is 11 min old")
    b = stale_banner(st)
    assert b is not None and b["age_min"] == 11


def test_not_answering_is_error_and_always_shows():
    st = _status(desk_state="NOT_ANSWERING", bar_age=None)
    b = stale_banner(st)
    assert b is not None and b["level"] == "error"
    assert STALE_FIX_CMD in b["text"]


def test_below_threshold_no_banner():
    # 2 min old with a 3-min threshold → not yet loud
    st = _status(desk_state="STALE", bar_age=2 * 60)
    assert stale_banner(st, staleness_alert_min=3) is None


def test_staleness_log_onset_and_offset(tmp_path):
    log = tmp_path / "desk8600_staleness.log"
    # LIVE -> STALE writes START
    start = log_staleness_transition("LIVE", _status(desk_state="STALE", bar_time="11:20"), NOW, log)
    assert start is not None and start.startswith("STALE START")
    # STALE -> STALE : no transition, nothing written
    none = log_staleness_transition("STALE", _status(desk_state="STALE"), NOW, log)
    assert none is None
    # STALE -> LIVE writes END
    end = log_staleness_transition("STALE", _status(desk_state="LIVE"), NOW, log)
    assert end is not None and end.startswith("STALE END")
    body = log.read_text().strip().splitlines()
    assert len(body) == 2 and body[0].startswith("STALE START") and body[1].startswith("STALE END")


def test_helpers_never_raise_on_garbage():
    assert stale_banner({"desk8600": None, "chain": None}) is None
    assert stale_banner({}) is None
    assert log_staleness_transition(None, {}, NOW, "/nonexistent/dir/x.log") is None
