"""Dhan static IP allowlist helpers (8510 desk — read-only matching, no orders)."""
from __future__ import annotations

import ipaddress
import os
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

# Owner-registered Dhan console IPs (defaults; overridden via config / env).
DEFAULT_DHAN_WHITELIST_IPS = ("49.205.36.145", "106.51.60.58")


def parse_public_ipv4(raw: Any) -> Optional[str]:
    try:
        addr = ipaddress.IPv4Address(str(raw or "").strip())
        if addr.is_global:
            return str(addr)
    except ValueError:
        pass
    return None


def _split_env_ips() -> List[str]:
    raw = os.environ.get("DHAN_WHITELISTED_IPS") or os.environ.get("DHAN_STATIC_IPS") or ""
    return [p.strip() for p in raw.replace(";", ",").split(",") if p.strip()]


def allowlist_from_cfg(dhan_cfg: Optional[Dict[str, Any]], saved_ip: Optional[str]) -> Set[str]:
    """Union of saved primary, configured list, env list, and shipped defaults."""
    out: Set[str] = set()
    dhan_cfg = dhan_cfg or {}
    for ip in DEFAULT_DHAN_WHITELIST_IPS:
        p = parse_public_ipv4(ip)
        if p:
            out.add(p)
    for ip in _split_env_ips():
        p = parse_public_ipv4(ip)
        if p:
            out.add(p)
    extra = dhan_cfg.get("whitelisted_ips")
    if isinstance(extra, (list, tuple)):
        for ip in extra:
            p = parse_public_ipv4(ip)
            if p:
                out.add(p)
    saved = parse_public_ipv4(saved_ip or dhan_cfg.get("whitelisted_ip"))
    if saved:
        out.add(saved)
    return out


def ip_whitelist_enabled(dhan_cfg: Optional[Dict[str, Any]]) -> bool:
    dhan_cfg = dhan_cfg or {}
    if "ip_whitelist_enabled" in dhan_cfg:
        return bool(dhan_cfg.get("ip_whitelist_enabled"))
    return True


def match_current_ip(
    current_ip: Optional[str],
    *,
    allowlist: Iterable[str],
    enabled: bool = True,
) -> Tuple[bool, Optional[str]]:
    """Return (is_match, matched_against_ip)."""
    curr = parse_public_ipv4(current_ip)
    if not curr:
        return False, None
    if not enabled:
        return True, None
    allowed = {p for p in (parse_public_ipv4(x) for x in allowlist) if p}
    if not allowed:
        return False, None
    if curr in allowed:
        return True, curr
    return False, None