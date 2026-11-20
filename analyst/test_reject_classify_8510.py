"""v2.0.43 — classify an order rejection as LOCAL (app blocked pre-Dhan) vs BROKER (Dhan said no)."""
from __future__ import annotations

from analyst.ema_arm_persistence_8510 import classify_rejection


def test_dhan_invalid_ip_is_broker():
    r = classify_rejection("DH-905: Invalid IP")
    assert r["origin"] == "BROKER"
    assert r["display"].startswith("[BROKER]")
    assert "DH-905" in r["display"]


def test_local_cash_guard_is_local():
    r = classify_rejection("Available cash below sampled option premium requirement")
    assert r["origin"] == "LOCAL"
    assert r["display"].startswith("[LOCAL]")


def test_tick_alignment_is_local():
    assert classify_rejection("Option bracket is not aligned to instrument tick size")["origin"] == "LOCAL"


def test_directional_bias_is_local():
    assert classify_rejection("Directional Bias: LONG_ONLY active (all PE / Short orders blocked)")["origin"] == "LOCAL"


def test_margin_is_broker():
    assert classify_rejection("RMS: insufficient margin")["origin"] == "BROKER"


def test_json_error_payload_parsed_and_tagged():
    r = classify_rejection('{"errorCode":"DH-905","errorMessage":"Invalid IP"}')
    assert r["origin"] == "BROKER" and "DH-905" in r["display"] and "Invalid IP" in r["display"]


def test_unknown_reason_has_no_tag_but_keeps_text():
    r = classify_rejection("something weird happened")
    assert r["origin"] == "UNKNOWN"
    assert r["display"] == "something weird happened"  # text preserved, no misleading tag


def test_empty_is_safe():
    r = classify_rejection(None)
    assert r["origin"] == "UNKNOWN" and r["reason"] == ""
