"""Recorded-session index for the shared clock. Metadata only, no strategy calculations."""
from bisect import bisect_right
from datetime import datetime, time
import json
import logging
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
IST = ZoneInfo("Asia/Kolkata")


def epoch(text):
    d = datetime.fromisoformat(text)
    return (d.replace(tzinfo=IST) if d.tzinfo is None else d).timestamp()


def select_at(points, at):
    points = sorted(set(points))
    i = bisect_right(points, at) - 1
    return {"available": i >= 0, "actual_ts": points[i] if i >= 0 else None,
            "first_ts": points[0] if points else None, "last_ts": points[-1] if points else None,
            "count": len(points), "actual_local": datetime.fromtimestamp(points[i], IST).replace(tzinfo=None).isoformat() if i >= 0 else None}


def metadata(day=None, at=None):
    policy = json.loads((ROOT / "analyst/analyst_config.json").read_text())["time_machine"]
    broken = 0
    sources = {"time_series": {}, "gamma_review": {}, "wmap": {}}
    for name in ("snapshots", "snapshots_multi"):
        for p in (ROOT / "data" / name).glob("????-??-??/*.meta.json"):
            try:
                d = json.loads(p.read_text())
                stamp = epoch(d["timestamp"])
                session = datetime.fromtimestamp(stamp, IST).date().isoformat()
                clock = datetime.fromtimestamp(stamp, IST).strftime("%H:%M")
                if not policy["open"] <= clock <= policy["end"]:
                    continue
                if name == "snapshots":
                    sources["time_series"].setdefault(session, []).append(stamp)
                if d.get("expiry"):
                    sources["gamma_review"].setdefault(session, []).append(stamp)
            except (OSError, ValueError, TypeError, KeyError):
                broken += 1
    if broken:
        logging.getLogger("time_machine").warning("Excluded %s malformed snapshot metadata files", broken)
    db = ROOT / "ict_smc_app/backend/data/ict_smc.sqlite3"
    if db.exists():
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
            for (stamp,) in conn.execute("SELECT DISTINCT ts FROM chain_snap ORDER BY ts"):
                dt = datetime.fromtimestamp(stamp, IST)
                if policy["open"] <= dt.strftime("%H:%M") <= policy["end"]:
                    sources["wmap"].setdefault(dt.date().isoformat(), []).append(stamp)
    days = sorted({d for s in sources.values() for d in s})
    day = day or (days[-1] if days else None)
    if not day or day not in days:
        return {"ok": False, "message": "No recorded data for this session", "days": days, "policy": policy}
    start, end = epoch(day + "T" + policy["open"]), epoch(day + "T" + policy["end"])
    if at is not None and not start <= at <= end:
        raise ValueError("Replay time must be inside the selected session")
    selected = end if at is None else at
    return {"ok": True, "day": day, "days": days, "start_ts": start, "end_ts": end,
            "selected_ts": selected, "policy": policy,
            "views": {k: select_at(v.get(day, []), selected) for k, v in sources.items()}}
