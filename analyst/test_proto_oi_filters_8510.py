"""Unit tests — 8 EMA Prototype 8600 OI filter switches (oi_switch_block)."""
from __future__ import annotations

import copy

from analyst.ema8_prototype_8510 import DEFAULT_CFG
from analyst.proto_oi_filters_8510 import (
    DEFAULT_OI_FILTERS,
    oi_switch_block,
    load_oi_filters_cfg,
)
def _reads(**pairs):
    return {k: {"direction": v, "age_sec": 30.0} for k, v in pairs.items()}


def test_all_off_never_blocks():
    cfg = copy.deepcopy(DEFAULT_CFG)
    blocked, det = oi_switch_block(True, cfg, _reads(writer_flow="PE"))
    assert blocked is False
    assert det["switches_on"] == []


def test_stale_never_blocks():
    cfg = copy.deepcopy(DEFAULT_CFG)
    cfg["oi_filters"] = copy.deepcopy(DEFAULT_OI_FILTERS)
    cfg["oi_filters"]["switches"]["writer_flow"] = True
    reads = {"writer_flow": {"direction": "PE", "age_sec": 9999.0}}
    blocked, _ = oi_switch_block(True, cfg, reads)
    assert blocked is False


def test_none_direction_fail_open():
    cfg = copy.deepcopy(DEFAULT_CFG)
    cfg["oi_filters"] = copy.deepcopy(DEFAULT_OI_FILTERS)
    cfg["oi_filters"]["switches"]["total_oi"] = True
    blocked, _ = oi_switch_block(True, cfg, {"total_oi": {"direction": None, "age_sec": None}})
    assert blocked is False


def test_oppose_blocks_all_agree():
    cfg = copy.deepcopy(DEFAULT_CFG)
    cfg["oi_filters"] = copy.deepcopy(DEFAULT_OI_FILTERS)
    cfg["oi_filters"]["switches"]["writer_flow"] = True
    blocked, det = oi_switch_block(True, cfg, _reads(writer_flow="PE"))
    assert blocked is True
    assert "writer_flow" in det["blocked_by"]


def test_agree_ce_for_long():
    cfg = copy.deepcopy(DEFAULT_CFG)
    cfg["oi_filters"] = copy.deepcopy(DEFAULT_OI_FILTERS)
    cfg["oi_filters"]["switches"]["price_oi"] = True
    blocked, _ = oi_switch_block(True, cfg, _reads(price_oi="CE"))
    assert blocked is False


def test_neutral_only_blocks_when_flag():
    cfg = copy.deepcopy(DEFAULT_CFG)
    cfg["oi_filters"] = copy.deepcopy(DEFAULT_OI_FILTERS)
    cfg["oi_filters"]["switches"]["volume_oi"] = True
    cfg["oi_filters"]["neutral_blocks"] = False
    blocked, _ = oi_switch_block(True, cfg, _reads(volume_oi="NEUTRAL"))
    assert blocked is False
    cfg["oi_filters"]["neutral_blocks"] = True
    blocked2, _ = oi_switch_block(True, cfg, _reads(volume_oi="NEUTRAL"))
    assert blocked2 is True


def test_rule_any_agrees():
    cfg = copy.deepcopy(DEFAULT_CFG)
    cfg["oi_filters"] = copy.deepcopy(DEFAULT_OI_FILTERS)
    cfg["oi_filters"]["rule"] = "any_agrees"
    cfg["oi_filters"]["switches"]["writer_flow"] = True
    cfg["oi_filters"]["switches"]["fresh_oi"] = True
    reads = _reads(writer_flow="PE", fresh_oi="CE")
    blocked, _ = oi_switch_block(True, cfg, reads)
    assert blocked is False


def test_rule_majority():
    cfg = copy.deepcopy(DEFAULT_CFG)
    cfg["oi_filters"] = copy.deepcopy(DEFAULT_OI_FILTERS)
    cfg["oi_filters"]["rule"] = "majority"
    cfg["oi_filters"]["switches"]["writer_flow"] = True
    cfg["oi_filters"]["switches"]["total_oi"] = True
    cfg["oi_filters"]["switches"]["fresh_oi"] = True
    reads = _reads(writer_flow="PE", total_oi="PE", fresh_oi="CE")
    blocked, _ = oi_switch_block(True, cfg, reads)
    assert blocked is True


def test_block_mode_config():
    cfg = copy.deepcopy(DEFAULT_CFG)
    cfg["oi_filters"] = copy.deepcopy(DEFAULT_OI_FILTERS)
    cfg["oi_filters"]["mode"] = "block"
    cfg["oi_filters"]["switches"]["writer_flow"] = True
    oi = load_oi_filters_cfg(cfg)
    assert oi["mode"] == "block"


def test_all_off_ignores_opposing_reads():
    cfg = copy.deepcopy(DEFAULT_CFG)
    blocked, _ = oi_switch_block(True, cfg, _reads(writer_flow="PE", total_oi="PE"))
    assert blocked is False


def test_load_oi_filters_momentum_defaults():
    cfg = {"oi_filters": {"momentum": {"window_min": 15}}}
    oi = load_oi_filters_cfg(cfg)
    assert oi["momentum"]["window_min"] == 15
    assert oi["momentum"]["sig_min"] == 0.15
    assert oi["momentum"]["sig_extreme"] == 0.30
    assert oi["momentum"]["require_cumd_confirm"] is False