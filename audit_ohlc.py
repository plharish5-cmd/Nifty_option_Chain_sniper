"""
audit_ohlc.py — find bad rows in the daily OHLC history.

Levels are built from this file. A row dated in the future, or a date that
was parsed day-for-month, silently becomes a swing high or an unfilled gap
and every R and S shifts.

    python audit_ohlc.py           # report only
    python audit_ohlc.py --fix     # quarantine the bad rows

--fix moves offending rows into data/ohlc/_quarantine.csv rather than
deleting them. If the diagnosis is wrong you can put them back, and a file
you can restore is worth more than one byte of disk.
"""
import sys
from datetime import date, timedelta

import pandas as pd

import build_master as bm
import config


def main() -> int:
    fix = "--fix" in sys.argv
    today = date.today()

    print(f"\naudit of {config.OHLC_DIR}\n")
    per_file = {}
    for p in sorted(config.OHLC_DIR.glob("*.csv")):
        if p.name.startswith("_"):
            continue
        try:
            d = bm.load_all(p.parent, quiet=True) if False else pd.read_csv(p)
        except Exception as exc:
            print(f"  {p.name}: unreadable — {exc}")
            continue
        if "Date" not in d.columns:
            continue
        try:
            parsed = bm._parse_dates(d["Date"], p.name)
        except Exception:
            parsed = pd.to_datetime(d["Date"], errors="coerce",
                                    dayfirst=True)
        dts = pd.to_datetime(parsed, errors="coerce").dt.date
        future = [x for x in dts.dropna() if x > today]
        weekend = [x for x in dts.dropna() if x.weekday() >= 5]
        bad_parse = int(dts.isna().sum())
        per_file[p.name] = {
            "rows": len(d), "future": future, "weekend": weekend,
            "unparsed": bad_parse,
            "range": (min(dts.dropna()), max(dts.dropna())) if len(dts.dropna()) else None,
        }
        flag = ""
        if future:
            flag += f"  <-- {len(future)} FUTURE dates, max {max(future)}"
        if weekend:
            flag += f"  <-- {len(weekend)} weekend dates"
        if bad_parse:
            flag += f"  <-- {bad_parse} unparseable"
        rng = per_file[p.name]["range"]
        print(f"  {p.name:<52} {len(d):>5} rows  "
              f"{rng[0] if rng else '?'} to {rng[1] if rng else '?'}{flag}")

    culprits = [n for n, v in per_file.items()
                if v["future"] or v["weekend"] or v["unparsed"]]
    print()
    if not culprits:
        print("  no future dates, weekend rows or parse failures found.")
        return 0

    print(f"  SUSPECT FILES: {', '.join(culprits)}")
    print("\n  A future date usually means the day and month were swapped on")
    print("  import — 09-12-2026 read as 8-Dec instead of 12-Sep. Weekend")
    print("  rows mean the same thing: NSE does not trade on Saturdays.")

    if not fix:
        print("\n  run with --fix to quarantine those rows.\n")
        return 1

    q = config.OHLC_DIR / "_quarantine.csv"
    moved = 0
    for name in culprits:
        p = config.OHLC_DIR / name
        d = pd.read_csv(p)
        parsed = pd.to_datetime(bm._parse_dates(d["Date"], name),
                                errors="coerce")
        dts = parsed.dt.date
        bad = dts.isna() | dts.gt(today) | parsed.dt.weekday.ge(5)
        if not bad.any():
            continue
        d[bad].to_csv(q, mode="a", header=not q.exists(), index=False)
        d[~bad].to_csv(p, index=False)
        moved += int(bad.sum())
        print(f"  {name}: quarantined {int(bad.sum())} row(s)")

    print(f"\n  {moved} row(s) moved to {q}")
    print("  now run:  python build_master.py\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
