from analyst.dhan_whitelist_ips_8510 import (
    allowlist_from_cfg,
    match_current_ip,
    parse_public_ipv4,
)


def test_allowlist_includes_primary_and_secondary_defaults():
    allowed = allowlist_from_cfg({}, None)
    assert "49.205.36.145" in allowed
    assert "106.51.60.58" in allowed


def test_match_secondary_when_saved_primary_differs():
    allowed = allowlist_from_cfg({"whitelisted_ip": "49.205.39.195"}, "49.205.39.195")
    ok, against = match_current_ip("106.51.60.58", allowlist=allowed, enabled=True)
    assert ok is True
    assert against == "106.51.60.58"


def test_parse_rejects_non_global():
    assert parse_public_ipv4("127.0.0.1") is None