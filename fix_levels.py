"""
Repair the daily OHLC history, then rebuild master levels.

Tries Dhan, then NSE, then any CSV you dropped into data/ohlc/ by hand.
Reports what each route said rather than failing with one message, because
"stale levels" has had three different causes so far and they need
different fixes.
"""
import sys
from datetime import date

import config
import build_master as bm
import market_data as md


def newest():
    try:
        h = bm.load_all(config.OHLC_DIR, quiet=True)
        return len(h), h["Date"].max().date()
    except Exception as exc:                                # noqa: BLE001
        print(f"  could not read history: {exc}")
        return 0, None


print(f"CANDLE_SOURCE = {getattr(config, 'CANDLE_SOURCE', '?')}")
n0, d0 = newest()
print(f"before: {n0} rows, newest {d0}")
behind = (date.today() - d0).days if d0 else 999
if behind <= 1:
    print("history is current — nothing to repair")
    sys.exit(0)
print(f"{behind} days behind\n")

ok = False

print("route 1 — Dhan (daily endpoint, then hourly aggregate)")
try:
    ok, msg = md.backfill_ohlc_dhan(max(behind + 10, 60))
    print(f"  {'OK' if ok else 'FAILED'} — {msg}")
except Exception as exc:                                    # noqa: BLE001
    print(f"  FAILED — {exc}")

if not ok:
    print("\nroute 2 — NSE indicesHistory")
    try:
        import nse_client as nc
        c = nc.NSEClient()
        c._bootstrap()
        ok, msg = md.backfill_ohlc(c, max(behind + 10, 60), force=True)
        print(f"  {'OK' if ok else 'FAILED'} — {msg}")
    except Exception as exc:                                # noqa: BLE001
        print(f"  FAILED — {exc}")
        if "503" in str(exc):
            print("  (503 is NSE refusing, not your code. It comes and goes.)")

if not ok:
    print("\nroute 3 — manual CSV")
    print("  Both automatic routes failed. Download the missing sessions:")
    print("    https://www.niftyindices.com/reports/historical-data")
    print("  Choose NIFTY 50, set the date range, download the CSV, and put")
    print(f"  it in {config.OHLC_DIR}")
    print("  build_master reads every CSV in that folder, so the filename")
    print("  does not matter and overlapping dates are de-duplicated.")
    print("  Then run this script again — it will skip to the rebuild.")
    sys.exit(1)

bm.main()
n1, d1 = newest()
print(f"\nafter:  {n1} rows, newest {d1}  (+{n1 - n0})")
print("Restart the app; Chart levels will rebuild from the new close.")
