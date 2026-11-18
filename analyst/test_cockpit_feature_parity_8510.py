"""Cockpit DOM parity — 8600 OI filters + Dhan synergy panels (display wiring only)."""
from __future__ import annotations

from pathlib import Path

from analyst.proto_oi_filters_8510 import PROTO_OI_READ_IDS, PROTO_OI_READ_LABELS

ROOT = Path(__file__).resolve().parent.parent
HTML = ROOT / "Algorithmic_Trading_Workspace_8510.html"

DHAN_OI_IDS = (
    "tab1DhanOICard",
    "tab1TotalOiSummaryText",
    "tab1DayOiSummaryText",
    "tab1PillCallOiChg",
    "tab1PillPutOiChg",
    "tab1PillCallOiTot",
    "tab1PillPutOiTot",
    "tab1_chk_stage2_synergy",
    "tab1_chk_strat2_synergy",
    "tab1_chk_strat3_synergy",
)

LANE_OI_UI = (
    "protoOiModeLog",
    "protoOiModeBlock",
    "protoOiRule",
    "protoOiNeutralBlock",
    "revOiModeLog",
    "revOiModeBlock",
    "revRuleSel",
    "revNeutralBlocks",
    "revOiDecisionBoard",
    "protoOiDecisionBoard",
    "desk8600Board",
    "d86Rows",
)

WIRE_FUNCS = (
    "wireReversalOiFilters",
    "wireEma8PrototypeFilterRadios",
    "renderDesk8600Board",
    "collectReversalOiFiltersFromDom",
)


def test_proto_and_rev_oi_switch_ids_present():
    html = HTML.read_text(encoding="utf-8")
    for rid in PROTO_OI_READ_IDS:
        assert f'data-id="{rid}"' in html, rid
        label_bit = PROTO_OI_READ_LABELS[rid].split("(")[0].strip()[:12]
        assert label_bit[:8] in html or rid in html


def test_lane_oi_control_ids_present():
    html = HTML.read_text(encoding="utf-8")
    for eid in LANE_OI_UI:
        assert f'id="{eid}"' in html, eid


def test_dhan_synergy_and_total_day_oi_fields():
    html = HTML.read_text(encoding="utf-8")
    for eid in DHAN_OI_IDS:
        assert f'id="{eid}"' in html, eid


def test_oi_filter_js_wiring_present():
    html = HTML.read_text(encoding="utf-8")
    for fn in WIRE_FUNCS:
        assert f"function {fn}" in html, fn


def test_workspace_tabs_unchanged():
    html = HTML.read_text(encoding="utf-8")
    for tid in (
        "tabTerminal",
        "tabPositionsOrders",
        "tabFundsWallet",
        "tabReplayExplorer",
        "tab7DailyIngest",
    ):
        assert f'id="{tid}"' in html