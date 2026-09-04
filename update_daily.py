"""
Daily OHLC updater — run after the close, no manual download.

    python update_daily.py            # append today's bar, rebuild master
    python update_daily.py --no-master  # append only

Pulls today's completed daily bar straight from NSE and appends it to
data/ohlc/OHLC.csv, then rebuilds the master level sheet so tomorrow's
supports, resistances, gaps and pivots are ready before you open anything.

WHY IT REFUSES BEFORE 15:30

The high and low are still moving while the session is open. Writing a
partial bar would not error — it would quietly produce wrong levels in the
master sheet, wrong pivots, wrong CPR, and a gap tracker that thinks a gap
filled when it did not. So the script checks NSE's own clock and declines.

AUTOMATING IT

Add to crontab (`crontab -e`) to run at 5:30 PM on weekdays:

    30 17 * * 1-5 cd /path/to/OC && ./.venv/bin/python update_daily.py >> data/update.log 2>&1

Weekends and holidays are harmless: NSE returns the last session's stamp, the
date already exists in the file, and the row is updated rather than
duplicated.
"""
from __future__ import annotations

import sys
from datetime import datetime

import config


def main() -> int:
    do_master = "--no-master" not in sys.argv
    force = "--force" in sys.argv

    print("=" * 66)
    print(f"DAILY OHLC UPDATE — {datetime.now():%d-%b-%Y %H:%M}")
    print("=" * 66)

    try:
        import nse_client
        import market_data as md
        client = nse_client.NSEClient()
        client._bootstrap()
    except Exception as exc:                                # noqa: BLE001
        print(f"\nNSE unreachable: {exc}")
        return 1

    ok, msg = md.backfill_ohlc(client, config.OHLC_BACKFILL_DAYS,
                               force=force)
    print(f"\n  {'OK  ' if ok else 'SKIP'}  {msg}")
    if not ok:
        if "still open" in msg:
            print("\n  Re-run after 15:30, or pass --force if you are certain "
                  "the session has ended.")
        return 0 if "still open" in msg else 1

    if do_master:
        print("\n  rebuilding master level sheet…")
        try:
            import build_master
            rc = build_master.main()
            if rc == 0:
                print("\n  Master sheet rebuilt — tomorrow's S/R, gaps, pivots "
                      "and CPR are ready.")
                try:
                    import build_master as bm
                    ohlc = bm.load_all(config.OHLC_DIR)
                    lv = bm.dated_levels(ohlc)
                    print("\n" + "=" * 66)
                    print(bm.format_copy_paste(lv))
                    print("=" * 66)
                    out = config.DATA_DIR / f"levels_{lv['date']}.txt"
                    out.write_text(bm.format_copy_paste(lv))
                    print(f"\n  also saved to {out}")
                except Exception as exc:                    # noqa: BLE001
                    print(f"  could not print levels: {exc}")
            else:
                print("\n  Master rebuild reported a problem — run "
                      "`python build_master.py` to see it.")
        except Exception as exc:                            # noqa: BLE001
            print(f"\n  Master rebuild failed: {exc}")
            return 1

    print("\n" + "=" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main())
