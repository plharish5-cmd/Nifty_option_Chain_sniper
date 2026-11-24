"""Read-only IPv4 cross-check. Never changes broker registration or routing."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional, Set

import ipaddress

from analyst.dhan_whitelist_ips_8510 import (
    allowlist_from_cfg,
    ip_whitelist_enabled,
    match_current_ip,
    parse_public_ipv4,
)

_cache = None
_lock = asyncio.Lock()


async def public_ip_status(
    saved_ip: Any,
    force: bool = False,
    *,
    dhan_cfg: Optional[Dict[str, Any]] = None,
    allowlist: Optional[Iterable[str]] = None,
    ip_whitelist_enabled_flag: Optional[bool] = None,
) -> Dict[str, Any]:
    global _cache
    async with _lock:
        if force or not _cache or time.monotonic() - _cache[0] >= 30:
            proc = None
            try:
                proc = await asyncio.create_subprocess_exec(
                    "curl",
                    "-4",
                    "-f",
                    "-sS",
                    "--max-time",
                    "6",
                    "https://api.ipify.org",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=8)
                if proc.returncode:
                    raise ValueError("lookup failed")
                address = ipaddress.IPv4Address(out.decode().strip())
                if not address.is_global:
                    raise ValueError("not public IPv4")
                lookup = {"current_ip": str(address), "lookup_ok": True}
            except (OSError, ValueError, asyncio.TimeoutError):
                if proc and proc.returncode is None:
                    proc.kill()
                    await proc.wait()
                lookup = {"current_ip": None, "lookup_ok": False}
            lookup["checked_at"] = datetime.now(timezone.utc).isoformat()
            _cache = (time.monotonic(), lookup)
        result = dict(_cache[1])

    saved = parse_public_ipv4(saved_ip)
    dhan_cfg = dhan_cfg or {}
    enabled = (
        ip_whitelist_enabled_flag
        if ip_whitelist_enabled_flag is not None
        else ip_whitelist_enabled(dhan_cfg)
    )
    if allowlist is None:
        allowed: Set[str] = allowlist_from_cfg(dhan_cfg, saved)
    else:
        allowed = {p for p in (parse_public_ipv4(x) for x in allowlist) if p}
        if saved:
            allowed.add(saved)

    matched, matched_against = match_current_ip(
        result.get("current_ip"), allowlist=allowed, enabled=enabled
    )

    if not result.get("lookup_ok"):
        status = "CHECK_FAILED"
    elif not enabled:
        status = "MATCH"
    elif not saved and not allowed:
        status = "NOT_CONFIGURED"
    elif matched:
        status = "MATCH"
    else:
        status = "MISMATCH"

    result.update(
        saved_ip=saved,
        allowlist_ips=sorted(allowed),
        ip_whitelist_enabled=enabled,
        matched_against=matched_against,
        source="api.ipify.org / curl -4",
        status=status,
        broker_registration_verified=False,
    )
    return result