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


# ── summary for "Read of the market" and "Market story" ──────────────────────────────────────────────────
def _inr(v) -> str:
    if v is None:
        return "—"
    a, s = abs(v), "-" if v < 0 else "+"
    if a >= 1e7:
        return f"{s}{a / 1e7:.2f}Cr"
    if a >= 1e5:
        return f"{s}{a / 1e5:.2f}L"
    if a >= 1e3:
        return f"{s}{a / 1e3:.1f}K"
    return f"{s}{a:.0f}"


_MEAN = {
    "break_up": ("bull", "Resistance broken — call writers covering",
                 "Two closes held above the old call wall and the writers there are buying back: the ceiling is gone.",
                 "BUY CE on the break; stop below the broken level. No fresh entries after 15:00."),
    "break_dn": ("bear", "Support broken — put writers unwinding",
                 "Two closes held below the old put wall and the writers there are exiting: the floor is gone.",
                 "BUY PE on the break; stop above the broken level. No fresh entries after 15:00."),
    "fail": ("wait", "Break failed — back inside", "Price returned inside the old level: the break did not hold.",
             "Exit the break trade. Wait for the next clean setup."),
    "fake": ("wait", "Fake-break risk", "Price closed beyond a wall, but writers there are still ADDING positions.",
             "Do not chase. Wait for the writers to exit (covering / unwinding) before trusting the break."),
    "s_build": ("bull", "Support being built", "Put writers are adding at this strike — they expect price to stay above it.",
                "Buy CE on dips toward it; stop below the strike."),
    "r_build": ("bear", "Resistance being built", "Call writers are adding at this strike — they expect price to stay below it.",
                "Buy PE on rises toward it; stop above the strike."),
}


async def summary() -> Dict[str, Any]:
    pe = await run("pece", "3m", "auto", 10, None)
    fl = await run("flow", "1m", "all", 10, None)
    lines, beats = [], []
    rows = (pe.get("rows") or []) if pe.get("ok") else []
    if rows:
        last = rows[0]                                          # newest first
        old = rows[min(10, len(rows) - 1)]
        d, d10 = last.get("pe_ce_day") or 0.0, (last.get("pe_ce_day") or 0.0) - (old.get("pe_ce_day") or 0.0)
        lean = "put-heavy → bullish lean" if d > 0 else "call-heavy → bearish lean" if d < 0 else "balanced"
        lines.append(f"PE − CE OI (3m, {pe.get('range')}): {_inr(d)} since yesterday ({lean}); last 10 candles {_inr(d10)} → "
                     + ("the leader is gaining." if d * d10 > 0 else "the leader is losing grip.")
                     + (f" PCR {last['pcr']:.2f}." if last.get("pcr") is not None else ""))
        act = (last.get("act") or ["WAIT"])[0]
        ev = next((r for r in rows if r.get("lv") and r["lv"][2] in ("break_up", "break_dn", "fail", "fake")), None)
        lines.append(f"Writers' levels: support {int(last['sup']) if last.get('sup') else '—'} · resistance "
                     f"{int(last['res']) if last.get('res') else '—'} · action now: {act}"
                     + (f" · last level event {ev['ts']}: {ev['lv'][0]}" if ev else "") + ".")
        seen = set()
        for r in reversed(rows):                                # oldest → newest
            lv = r.get("lv")
            if not lv or lv[2] not in _MEAN:
                continue
            key = (lv[2], r.get("sup") if lv[2] == "s_build" else r.get("res") if lv[2] == "r_build" else r["ts"])
            if key in seen:
                continue
            seen.add(key)
            tone, title, meaning, action = _MEAN[lv[2]]
            beats.append({"time": r["ts"], "tone": tone, "title": f"OI · {lv[0]}", "meaning": meaning, "action": action})
    pts = (fl.get("points") or []) if fl.get("ok") else []
    if pts:
        p = pts[-1]
        conf = p.get("conf") or p.get("regime")
        flips = [x for x in pts if x.get("flip") and x.get("conf")]
        sig10 = sum(1 for x in pts[-10:] if x.get("sig"))
        lines.append(f"Call vs put writers (1m, all strikes): calls {_inr(p.get('ce'))} vs puts {_inr(p.get('pe'))} since yesterday; "
                     f"30-min writers balance {p['bal']:+.2f} → " if p.get("bal") is not None else "")
        if lines and lines[-1].endswith("→ "):
            lines[-1] += (f"{'PUT writers lead (bullish)' if conf == 'BULL' else 'CALL writers lead (bearish)' if conf == 'BEAR' else conf}"
                          + (f", trend flip last at {flips[-1]['hm']} → {flips[-1]['conf']}" if flips else "")
                          + (f"; {sig10} unusual writer candle(s) in the last 10 min" if sig10 else "")
                          + (" · EXPIRY UNWIND in progress — both sides closing, not a direction." if fl.get("unwind") else "."))
        for x in flips:
            bull = x["conf"] == "BULL"
            beats.append({"time": x["hm"], "tone": "bull" if bull else "bear",
                          "title": f"Writers flipped {'BULLISH — put writers took the lead' if bull else 'BEARISH — call writers took the lead'}",
                          "meaning": "The 30-minute balance of put vs call OI change has held on the new side for ~15 min (confirmed flip, not a blip).",
                          "action": ("Favour longs (CE) on pull-backs while it holds; don't short into fresh put writing."
                                     if bull else "Favour shorts (PE) on rallies while it holds; don't buy into fresh call writing.")})
    return {"ok": bool(lines), "lines": [x for x in lines if x], "beats": beats,
            "message": pe.get("message") or fl.get("message")}
