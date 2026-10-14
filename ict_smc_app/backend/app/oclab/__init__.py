"""oclab — runs the OC folder's Options Lab code (oc_export.py) for the desk's PE − CE table and writer-flow chart.

The OC app has its own virtualenv (plotly, pyarrow, …), so it is called as a subprocess. Results are cached per
(view, settings, day, number of stored snapshot files): a new snapshot always triggers a fresh run, so nothing shown
is ever older than the newest snapshot. Read-only.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Tuple

from .. import config

OC_DIR = Path(os.environ.get("OC_DIR", config.BACKEND_DIR.parent.parent))


def oc_python() -> str:
    p = os.environ.get("OC_PYTHON", "")
    if p:
        return p
    for c in (OC_DIR / ".venv" / "bin" / "python", OC_DIR / "venv" / "bin" / "python"):
        if c.exists():
            return str(c)
    return sys.executable


def _snap_count(day: str | None) -> Tuple[str, int]:
    root = OC_DIR / "data" / "snapshots"
    try:
        if not day:
            days = sorted([p.name for p in root.iterdir() if p.is_dir()], reverse=True)
            day = days[0] if days else ""
        return day, len(list((root / day).glob("*.meta.json"))) if day else -1
    except Exception:                                           # noqa: BLE001
        return day or "", -1


_cache: Dict[tuple, Tuple[int, Dict[str, Any]]] = {}
_locks: Dict[tuple, asyncio.Lock] = {}


async def run(what: str, tf: str, mode: str, n: int, day: str | None) -> Dict[str, Any]:
    script = OC_DIR / "oc_export.py"
    if not script.exists():
        return {"ok": False, "message": f"oc_export.py not found in {OC_DIR} — copy it into your OC folder"}
    d, cnt = _snap_count(day)
    key = (what, tf, mode, n, d)
    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        hit = _cache.get(key)
        if hit and cnt >= 0 and hit[0] == cnt:
            return hit[1]
        args = [oc_python(), str(script), what, "--tf", tf, "--mode", mode, "--n", str(n)] + (["--day", d] if d else [])
        proc = await asyncio.create_subprocess_exec(*args, cwd=str(OC_DIR), stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=120)
        except asyncio.TimeoutError:
            proc.kill()
            return {"ok": False, "message": "Options Lab export timed out (120 s)"}
        try:
            res = json.loads(out.decode() or "{}")
        except Exception:                                       # noqa: BLE001
            res = {"ok": False, "message": (err.decode()[-400:] or "no output from oc_export.py")}
        res["snapshots"] = cnt
        if res.get("ok"):
            _cache[key] = (cnt, res)
        return res
