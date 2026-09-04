"""
pdc_rebuild.py — verify which candle PDC means, then rebuild every level.

Run this after the definition changed from the 15:15 candle (which contains
the closing call auction) to the 15:00 candle (the last continuous-session
candle). Appending would leave old wrong rows beside new correct ones, so
the file is deleted and refetched.

    python pdc_rebuild.py            # verify, then rebuild from 03-Aug
    python pdc_rebuild.py --check    # verify only, change nothing
"""
import sys
from datetime import date

import pdc


def main() -> int:
    check_only = "--check" in sys.argv
    print("\nBoth candidate candles, most recent sessions:\n")
    try:
        cmp = pdc.compare_candles(12)
    except Exception as exc:                                # noqa: BLE001
        print(f"  could not fetch: {exc}")
        return 1
    if cmp.empty:
        print("  no data returned")
        return 1
    print(cmp.to_string(index=False))
    print("\n  '15:00 close' is what PDC now uses.")
    print("  'auction moved it' shows what the final candle did to the print.")
    print("  Check the newest row against your chart before going further.")

    if check_only:
        print("\n--check given, nothing written.\n")
        return 0

    print("\nRebuilding pdc_levels.csv from 03-Aug...")
    ok, msg = pdc.rebuild(date(2026, 8, 3))
    print(f"  {'OK' if ok else 'FAILED'} — {msg}")
    if not ok:
        return 1

    print("\nPDC levels now:")
    print("  " + pdc.format_copy_paste(4).replace("\n", "\n  "))
    print("\nRestart the app.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
