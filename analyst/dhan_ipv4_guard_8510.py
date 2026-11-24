"""Force IPv4 for Dhan REST/order clients; public IPv4 vs allowlist guard (8510)."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, Optional

import httpx

_DH905_MARKERS = ("DH-905", "INVALID IP", "INVALID_IP")


def ipv4_async_client(timeout: float = 10.0) -> httpx.AsyncClient:
    """Async HTTP client bound to IPv4 so dual-stack hosts do not hit Dhan over v6."""
    transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0")
    return httpx.AsyncClient(timeout=timeout, transport=transport)


def ipv4_sync_client(timeout: float = 10.0) -> httpx.Client:
    transport = httpx.HTTPTransport(local_address="0.0.0.0")
    return httpx.Client(timeout=timeout, transport=transport)


def _explicit_whitelist(broker: Any) -> str:
    return str(
        getattr(broker, "whitelisted_ip", "")
        or os.environ.get("DHAN_WHITELISTED_IP")
        or ""
    ).strip()


def broker_ip_guard_status(broker: Any, *, current_ipv4: Optional[str] = None) -> Dict[str, Any]:
    """IPv4 allowlist snapshot for health / preflight (no secrets)."""
    from analyst.dhan_whitelist_ips_8510 import (
        allowlist_from_cfg,
        ip_whitelist_enabled,
        match_current_ip,
        parse_public_ipv4,
    )

    curr = parse_public_ipv4(current_ipv4 or broker.get_current_ip())
    wl = _explicit_whitelist(broker)
    cfg = getattr(broker, "cfg", None) or {}
    dhan_cfg = cfg.get("dhan") if isinstance(cfg, dict) else {}
    if not isinstance(dhan_cfg, dict):
        dhan_cfg = {}
    enabled = ip_whitelist_enabled(dhan_cfg)
    allowed = allowlist_from_cfg(dhan_cfg, wl)
    matched, matched_against = match_current_ip(curr, allowlist=allowed, enabled=enabled)
    ip_mismatch = bool(enabled and curr and allowed and not matched)
    ip_matched = matched if (curr and enabled) else (not wl) or matched
    return {
        "current_public_ipv4": curr,
        "current_ip": curr,
        "whitelisted_ipv4": wl or None,
        "whitelisted_ip": wl or None,
        "allowlist_ips": sorted(allowed),
        "ip_whitelist_enabled": enabled,
        "matched_against": matched_against,
        "ip_mismatch": ip_mismatch,
        "ip_matched": ip_matched,
    }


def recent_dh905_rejection(ledger_db: Optional[Path]) -> bool:
    """True if any recent order row shows a DH-905 / invalid IP broker rejection."""
    if not ledger_db or not Path(ledger_db).is_file():
        return False
    try:
        with sqlite3.connect(f"file:{ledger_db}?mode=ro", uri=True, timeout=1) as conn:
            row = conn.execute(
                """
                SELECT 1 FROM orders
                WHERE (
                    UPPER(COALESCE(rejection_reason, '')) LIKE '%DH-905%'
                    OR UPPER(COALESCE(rejection_reason, '')) LIKE '%INVALID IP%'
                )
                ORDER BY id DESC LIMIT 1
                """
            ).fetchone()
        return bool(row)
    except sqlite3.Error:
        return False


def ip_guard_payload(backend: Any) -> Dict[str, Any]:
    guard = broker_ip_guard_status(backend.broker)
    guard["dh905_rejection_recent"] = recent_dh905_rejection(getattr(backend, "ledger_db", None))
    guard["ip_alert_active"] = bool(
        guard.get("ip_mismatch") or guard.get("dh905_rejection_recent")
    )
    return guard