"""Read-only IPv4 cross-check. Never changes broker registration or routing."""
import asyncio
import ipaddress
import time
from datetime import datetime, timezone

_cache = None
_lock = asyncio.Lock()

async def public_ip_status(saved_ip, force=False):
    global _cache
    async with _lock:
        if force or not _cache or time.monotonic() - _cache[0] >= 30:
            proc = None
            try:
                proc = await asyncio.create_subprocess_exec(
                    'curl', '-4', '-f', '-sS', '--max-time', '6',
                    'https://api.ipify.org', stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL)
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=8)
                if proc.returncode:
                    raise ValueError('lookup failed')
                address = ipaddress.IPv4Address(out.decode().strip())
                if not address.is_global:
                    raise ValueError('not public IPv4')
                result = {'current_ip': str(address), 'lookup_ok': True}
            except (OSError, ValueError, asyncio.TimeoutError):
                if proc and proc.returncode is None:
                    proc.kill()
                    await proc.wait()
                result = {'current_ip': None, 'lookup_ok': False}
            result['checked_at'] = datetime.now(timezone.utc).isoformat()
            _cache = (time.monotonic(), result)
        result = dict(_cache[1])
    saved = str(saved_ip or '').strip()
    try:
        saved = str(ipaddress.IPv4Address(saved))
        if not ipaddress.IPv4Address(saved).is_global:
            saved = ''
    except ValueError:
        saved = ''
    result.update(saved_ip=saved or None, source='api.ipify.org / curl -4',
                  status=('CHECK_FAILED' if not result['lookup_ok'] else
                          'NOT_CONFIGURED' if not saved else
                          'MATCH' if saved == result['current_ip'] else 'MISMATCH'),
                  broker_registration_verified=False)
    return result
