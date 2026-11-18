"""Reversal cockpit OI panel — independence + present-key guard (v2.0.35)."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HTML = ROOT / "Algorithmic_Trading_Workspace_8510.html"


def test_reversal_oi_panel_markup_in_reversal_card():
    html = HTML.read_text(encoding="utf-8")
    i = html.index("8 EMA Reversal Up / Down")
    chunk = html[i : i + 4500]
    assert "revOiModeLog" in chunk
    assert "revOiModeBlock" in chunk
    assert "revRuleSel" in chunk
    assert 'class="rev-oi-sw"' in chunk
    assert "revOiBlockLine" in chunk
    assert "protoOiModeLog" not in chunk


def test_reversal_and_prototype_oi_ids_are_disjoint():
    html = HTML.read_text(encoding="utf-8")
    rev_ids = {
        "revOiModeLog",
        "revOiModeBlock",
        "revRuleSel",
        "revNeutralBlocks",
        "revOiBlockLine",
    }
    for rid in rev_ids:
        assert rid in html
    assert html.count('id="revOiModeLog"') == 1
    assert html.count('id="protoOiModeLog"') == 1


def test_present_key_guard_in_apply_reversal_oi_ui():
    html = HTML.read_text(encoding="utf-8")
    assert "function applyReversalOiFilterUI" in html
    body = html.split("function applyReversalOiFilterUI", 1)[1].split("function renderReversalOiFilters", 1)[0]
    assert '"mode" in oi' in body
    assert '"rule" in oi' in body
    assert "id in sw" in body


def test_save_posts_only_reversal_api_path():
    html = HTML.read_text(encoding="utf-8")
    block = html.split("async function saveReversalRevampCfg", 1)[1].split("function wireReversalOiFilters", 1)[0]
    assert 'fetch("/api/scanner/reversal"' in block
    assert "collectReversalOiFiltersFromDom" in block
    assert "ema8_prototype" not in block


def _backend_stub(tmp_path: Path, data: dict):
    from analyst.terminal8510_backend import QuantTerminalBackend

    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(data), encoding="utf-8")
    b = QuantTerminalBackend.__new__(QuantTerminalBackend)
    b.config_path = cfg_path
    b.cfg = dict(data)
    b.scanner_cfg = b.cfg.setdefault("scanner", {})
    b.strategy_status = {"crossover_reversal": {"state": "SCANNING"}}
    b.desk8600_board = lambda: {"sources": []}  # type: ignore
    return b, cfg_path


def test_update_reversal_oi_leaves_prototype_oi_filters_intact(tmp_path: Path):
    proto_oi = {
        "mode": "block",
        "rule": "majority",
        "switches": {"writer_flow": True, "total_oi": False},
    }
    rev_oi = {"mode": "log_only", "switches": {"fresh_oi": True}}
    data = {
        "scanner": {
            "ema8_prototype": {"oi_filters": dict(proto_oi)},
            "reversal": {"oi_filters": dict(rev_oi), "execution_mode": "PAPER_ONLY"},
        },
    }
    b, cfg_path = _backend_stub(tmp_path, data)
    b.update_reversal_revamp_config(
        {
            "oi_filters": {
                "switches": {"writer_flow": True, "total_oi": True},
                "mode": "log_only",
            }
        }
    )
    saved = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert saved["scanner"]["ema8_prototype"]["oi_filters"] == proto_oi
    rev = saved["scanner"]["reversal"]["oi_filters"]
    assert rev["switches"]["writer_flow"] is True
    assert rev["switches"]["total_oi"] is True
    assert rev["switches"]["fresh_oi"] is True
    assert rev["mode"] == "log_only"


def test_sync_reversal_desk_status_adds_oi_filters_key(tmp_path: Path):
    data = {"scanner": {"reversal": {"oi_filters": {"enabled": True, "mode": "log_only"}}}}
    b, _ = _backend_stub(tmp_path, data)
    b.strategy_status["crossover_reversal"] = {"state": "ARMED", "reversal_side": "UP"}
    b._sync_reversal_crossover_desk_status()
    row = b.strategy_status["crossover_reversal"]
    assert "oi_filters" in row
    assert "oi_filter_telemetry" in row