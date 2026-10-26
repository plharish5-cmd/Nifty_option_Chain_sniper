import argparse
import asyncio
import csv
from datetime import datetime, timezone, timedelta
from pathlib import Path
import sqlite3

IST = timezone(timedelta(hours=5, minutes=30))

async def _fetch_via_hub(days: int):
    import app.api.main as main_mod
    from app.api.kronos_api import _hourly_candles

    # Initialize the Hub singleton if running outside Uvicorn lifespan
    if getattr(main_mod, "_hub", None) is None and getattr(main_mod, "hub", None) is None:
        from app.api.hub import Hub
        h = Hub()
        if hasattr(h, "start"):
            try:
                await h.start()
            except Exception:
                pass
        if hasattr(main_mod, "_hub"):
            main_mod._hub = h
        if hasattr(main_mod, "hub"):
            main_mod.hub = h

    return await _hourly_candles(days=days, force=True)

def _fallback_from_sqlite(db_path: Path):
    """Fallback: read 1h or resampled candles directly from ict_smc.sqlite3 if Dhan REST is offline."""
    if not db_path.exists():
        return []
    con = sqlite3.connect(str(db_path))
    cur = con.cursor()
    tables = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    rows = []
    if "candles" in tables:
        # Try 1h/60m candles first, otherwise aggregate 1m/5m/15m
        q = cur.execute(
            "SELECT time, open, high, low, close, volume FROM candles "
            "WHERE tf IN ('1h', '60m', '60') ORDER BY time ASC"
        ).fetchall()
        if not q:
            q = cur.execute(
                "SELECT (time / 3600) * 3600 AS bucket, open, MAX(high), MIN(low), close, SUM(volume) "
                "FROM candles WHERE tf IN ('1m', '5m', '15m') GROUP BY bucket ORDER BY bucket ASC"
            ).fetchall()
        for t, o, h, l, c, v in q:
            dt_ist = datetime.fromtimestamp(int(t), tz=IST).strftime("%Y-%m-%d %H:%M:%S")
            rows.append({"date": dt_ist, "open": o, "high": h, "low": l, "close": c, "volume": v or 0})
    con.close()
    return rows

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=400)
    p.add_argument("--out", type=str, default="../kronos/NIFTY_1h.csv")
    a = p.parse_args()

    rows = []
    try:
        raw = asyncio.run(_fetch_via_hub(a.days))
        for r in (raw or []):
            t = r.get("time") or r.get("ts") or r.get("date")
            if isinstance(t, (int, float)):
                dt_str = datetime.fromtimestamp(int(t), tz=IST).strftime("%Y-%m-%d %H:%M:%S")
            else:
                dt_str = str(t)
            rows.append({
                "date": dt_str,
                "open": r["open"],
                "high": r["high"],
                "low": r["low"],
                "close": r["close"],
                "volume": r.get("volume", 0) or 0,
            })
    except Exception as e:
        print(f"[export] Hub fetch warning ({e}); falling back to SQLite...")

    if not rows:
        for db_cand in [Path("ict_smc.sqlite3"), Path("data/ict_smc.sqlite3"), Path("../ict_smc.sqlite3")]:
            rows = _fallback_from_sqlite(db_cand)
            if rows:
                print(f"[export] Loaded {len(rows)} hourly bars from {db_cand}")
                break

    if not rows:
        raise RuntimeError("No hourly rows fetched from Hub or SQLite!")

    out_path = Path(a.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["date", "open", "high", "low", "close", "volume"])
        w.writeheader()
        w.writerows(rows)

    print(f"✅ Exported {len(rows)} rows to {out_path.resolve()} ({rows[0]['date']} -> {rows[-1]['date']})")

if __name__ == "__main__":
    main()
