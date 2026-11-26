"""v266 — screen toggle for the delta-weighted writer-bias gate persists + round-trips."""
import json
from pathlib import Path
import analyst.desk8600_lane_gating_8510 as g


def _use_tmp_rules(tmp_path, monkeypatch):
    p = tmp_path / "live_sheet_rules.json"
    p.write_text(json.dumps({"desk8600_lane_gating_note": "keep me",
                             "desk8600_lane_gating": {lane: {k: "LOG_ONLY" for k in g.TRACK2_SIGNAL_KEYS} for lane in g.LANES}}))
    monkeypatch.setattr(g, "_LIVE_SHEET_RULES", p)
    return p


def test_set_one_lane_signal_to_block_persists(tmp_path, monkeypatch):
    p = _use_tmp_rules(tmp_path, monkeypatch)
    modes = g.set_lane_gating_mode("prototype", "pe_ce_change", "BLOCK")
    assert modes["prototype"]["pe_ce_change"] == "BLOCK"
    assert modes["reversal"]["pe_ce_change"] == "LOG_ONLY"
    # persisted + other keys preserved
    doc = json.loads(p.read_text())
    assert doc["desk8600_lane_gating"]["prototype"]["pe_ce_change"] == "BLOCK"
    assert doc["desk8600_lane_gating_note"] == "keep me"
    # reload sees it
    assert g.load_desk8600_lane_gating({})["prototype"]["pe_ce_change"] == "BLOCK"


def test_all_mode_block_then_log(tmp_path, monkeypatch):
    _use_tmp_rules(tmp_path, monkeypatch)
    m = g.set_all_lane_gating_mode("BLOCK")
    assert all(m[l][k] == "BLOCK" for l in g.LANES for k in g.TRACK2_SIGNAL_KEYS)
    m = g.set_all_lane_gating_mode("LOG_ONLY")
    assert all(m[l][k] == "LOG_ONLY" for l in g.LANES for k in g.TRACK2_SIGNAL_KEYS)


def test_bad_input_is_noop(tmp_path, monkeypatch):
    _use_tmp_rules(tmp_path, monkeypatch)
    m = g.set_lane_gating_mode("not_a_lane", "pe_ce_change", "BLOCK")
    assert m["prototype"]["pe_ce_change"] == "LOG_ONLY"   # unchanged
    m = g.set_lane_gating_mode("prototype", "pe_ce_change", "NONSENSE")
    assert m["prototype"]["pe_ce_change"] == "LOG_ONLY"   # invalid mode ignored
