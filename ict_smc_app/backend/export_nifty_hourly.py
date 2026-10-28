import argparse
import asyncio
import csv
from datetime import datetime, timezone, timedelta
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))

def _extract_field(r, *names, default=0.0):
    if isinstance(r, dict):
        for n in names:
            if n in r and r[n] is not None:
                return r[n]
    for n in names:
        if hasattr(r, n):
            val = getattr(r, n)
            if val is not None:
                return val
    if isinstance(r, (list, tuple)) and len(r) >= 5:
        idx_map = {"time": 0, "t": 0, "open": 1, "o": 1, "high": 2, "h": 2, "low": 3, "l": 3, "close": 4, "c": 4, "volume": 5, "v": 5}
        for n in names:
            if n in idx_map and idx_map[n] < len(r):
                return r[idx_map[n]]
    return default

async def _fetch_via_hub(days: int):
    import app.api.main as main_mod
    from app.api.kronos_api import _hourly_candles

    h = getattr(main_mod, "_hub", None) or getattr(main_mod, "hub", None)
    if h is None:
        from app.api.hub import Hub
        h = Hub()
        if hasattr(h, "start"):
            try:
                await h.start()
            except Exception:
                pass
        main_mod._hub = h
        if hasattr(main_mod, "hub"):
            main_mod.hub = h

    raw = await _hourly_candles(days=days, force=True)
    if isinstance(raw, dict):
        for k in ("candles", "rows", "data", "bars"):
            if k in raw and isinstance(raw[k], list):
                raw = raw[k]
                break
    return raw, h

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=400)
    p.add_argument("--out", type=str, default="../kronos/NIFTY_1h.csv")
    a = p.parse_args()

    raw, h = asyncio.run(_fetch_via_hub(a.days))
    if not raw:
        raise RuntimeError("No candles returned from _hourly_candles()")

    # Print sample keys/type of first item for transparency
    first = raw[0]
    print(f"[export] Fetched {len(raw)} raw items. Sample item type={type(first).__name__}: {first}")

    rows = []
    for r in raw:
        t = _extract_field(r, "time", "ts", "t", "date", "datetime", "timestamp", default=None)
        if t is None:
            continue
        if isinstance(t, (int, float)):
            ts_sec = int(t / 1000) if t > 1e11 else int(t)
            dt_str = datetime.fromtimestamp(ts_sec, tz=IST).strftime("%Y-%m-%d %H:%M:%S")
        else:
            dt_str = str(t)

        o = float(_extract_field(r, "open", "o"))
        hi = float(_extract_field(r, "high", "h"))
        lo = float(_extract_field(r, "low", "l"))
        c = float(_extract_field(r, "close", "c"))
        v = float(_extract_field(r, "volume", "vol", "v", default=0.0) or 0.0)

        rows.append({
            "date": dt_str,
            "open": o,
            "high": hi,
            "low": lo,
            "close": c,
            "volume": v,
        })

    out_path = Path(a.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["date", "open", "high", "low", "close", "volume"])
        w.writeheader()
        w.writerows(rows)

    print(f"✅ Exported {len(rows)} hourly candles to {out_path.resolve()} ({rows[0]['date']} -> {rows[-1]['date']})")

if __name__ == "__main__":
    main()
