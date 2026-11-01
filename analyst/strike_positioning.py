"""Read-only history projection for Writer map; never substitutes absent OI or futures for spot."""
from bisect import bisect_right
from datetime import datetime, time
from math import isfinite
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def build_positioning(raw_rows, spot_candles, payload, expiries, cfg, bucket_minutes=None):
    policy = cfg["positioning"]
    minutes = bucket_minutes or policy["bucket_minutes"]
    if minutes not in policy["bucket_options"]:
        raise ValueError("Unsupported positioning bucket")
    if not payload.get("ok"):
        return {"ok": False, "message": "No recorded chain snapshots", "buckets": [], "rows": []}
    start, end = payload["window_start_ts"], payload["window_end_ts"]
    session_open = int(datetime.combine(datetime.fromtimestamp(end, IST).date(), time(9, 15), IST).timestamp())
    start = max(start, session_open)
    if start > end:
        return {"ok": False, "message": "Window start is after the last recorded snapshot", "rows": [], "buckets": []}
    if start >= end:
        edges = [start]
    else:
        step = minutes * 60
        edges = [start]
        edge = session_open + ((start - session_open) // step + 1) * step
        while edge < end:
            edges.append(edge)
            edge += step
        edges.append(end)
    # Keep each observed side separately; a NULL or negative observation is not zero.
    series = {}
    for raw in raw_rows:
        r = dict(raw)
        for side in ("ce", "pe"):
            key = (r["expiry"], float(r["strike"]), side)
            value = r.get(side + "_oi")
            value = float(value) if value is not None else None
            if value is not None and (not isfinite(value) or value < 0):
                value = None
            series.setdefault(key, []).append((int(r["ts"]), value))
    indexed = {}
    for key, values in series.items():
        values.sort()
        indexed[key] = ([v[0] for v in values], values)

    def observed(strike, side, ts):
        total, stamps = 0.0, []
        for exp in expiries:
            times, values = indexed.get((exp, float(strike), side), ([], []))
            at = bisect_right(times, ts) - 1
            if at < 0:
                return None
            stamp, value = values[at]
            if value is None or ts - stamp > policy["max_snapshot_age_seconds"]:
                return None
            total += value
            stamps.append(stamp)
        return {"oi": total, "oldest_ts": min(stamps), "latest_ts": max(stamps)}

    # Candles are timestamped at START: close is only known after the minute ends.
    spots = []
    for c in spot_candles:
        value = c.get("c", c.get("close"))
        if value is not None and isfinite(float(value)) and float(value) > 0:
            spots.append((int(c["ts"]) + 60, float(value), int(c["ts"])))
    spots.sort()
    spot_times = [v[0] for v in spots]

    def spot_at(ts):
        i = bisect_right(spot_times, ts) - 1
        if i < 0 or ts - spots[i][0] > policy["max_spot_age_seconds"]:
            return None
        return {"value": spots[i][1], "observed_ts": spots[i][0], "candle_ts": spots[i][2]}

    buckets = []
    for a, b in zip(edges, edges[1:]):
        buckets.append({"start_ts": a, "end_ts": b,
                        "label": datetime.fromtimestamp(b, IST).strftime("%H:%M"),
                        "partial": (b - session_open) % (minutes * 60) != 0,
                        "spot": spot_at(b)})
    rows = []
    for s in sorted(payload["strikes"], key=lambda s: -s["strike"]):
        strike = s["strike"]
        quality = {}
        for side in ("ce", "pe"):
            a = observed(strike, side, payload["window_start_ts"])
            b = observed(strike, side, end)
            quality[side] = {"total_valid": b is not None, "change_valid": a is not None and b is not None}
        cells = []
        for bucket in buckets:
            cell = {}
            for side in ("ce", "pe"):
                a = observed(strike, side, bucket["start_ts"])
                b = observed(strike, side, bucket["end_ts"])
                cell[side] = None if a is None or b is None else {
                    "delta": round(b["oi"] - a["oi"], 1),
                    "start_oi": a["oi"], "end_oi": b["oi"],
                    "start_observed_ts": a["oldest_ts"], "end_observed_ts": b["oldest_ts"]}
            cells.append(cell)
        rows.append({"strike": strike, "quality": quality, "cells": cells})
    return {"ok": True, "bucket_minutes": minutes, "buckets": buckets, "rows": rows,
            "session_date": datetime.fromtimestamp(end, IST).date().isoformat(),
            "asof_ts": end, "spot": spot_at(end),
            "source": "Recorded chain_snap OI and NIFTY 1m closed candles",
            "baseline": "First recorded snapshot (same baseline as existing histogram)",
            "max_snapshot_age_seconds": policy["max_snapshot_age_seconds"],
            "stale_after_seconds": policy["stale_after_seconds"],
            "note": "Snapshot endpoint changes; not individual trades. Partial buckets remain provisional. Missing endpoints are never zero."}
