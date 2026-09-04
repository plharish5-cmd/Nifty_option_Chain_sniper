"""
eod.py — run this after 15:45 and everything is current.

One command instead of three. It fetches today's daily bar, rebuilds the
master levels from it, and records the 15:15 close as PDC, reporting what
each step did rather than failing quietly.

The dashboard does all of this by itself on the first load after 15:45. This
exists for the times it did not — a laptop that was shut, a token that had
expired, an evening when NSE was returning 503 — and so there is always one
thing to type that fixes the whole chain.

    python eod.py            # today
    python eod.py 2026-09-01 # a specific date, for catching up
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta

import config


def line(ch="-", n=68):
    print(ch * n)


def main() -> int:
    target = date.today()
    if len(sys.argv) > 1:
        try:
            target = date.fromisoformat(sys.argv[1])
        except ValueError:
            print("usage: python eod.py [YYYY-MM-DD]")
            return 1

    print()
    line("=")
    print(f"END OF DAY UPDATE — {target:%d-%b-%Y}")
    line("=")

    # ── 1. where are we now ────────────────────────────────────────────
    import build_master as bm
    try:
        hist = bm.load_all(config.OHLC_DIR, quiet=True)
        newest = hist["Date"].max().date()
        print(f"\n1. history currently ends {newest:%d-%b-%Y} "
              f"({len(hist)} sessions)")
    except Exception as exc:                                # noqa: BLE001
        print(f"\n1. could not read history: {exc}")
        newest = None

    behind = (target - newest).days if newest else 999
    if behind <= 0:
        print("   already current")
    else:
        print(f"   {behind} day(s) behind")

    # ── 2. daily bar ───────────────────────────────────────────────────
    print("\n2. daily OHLC")
    ok = behind <= 0
    if not ok:
        import market_data as md
        try:
            ok, msg = md.backfill_ohlc_dhan(max(behind + 10, 30))
            print(f"   dhan: {'OK' if ok else 'failed'} — {msg}")
        except Exception as exc:                            # noqa: BLE001
            print(f"   dhan: failed — {exc}")
        if not ok:
            try:
                import nse_client as nc
                c = nc.NSEClient()
                c._bootstrap()
                ok, msg = md.backfill_ohlc(c, max(behind + 10, 30), force=True)
                print(f"   nse:  {'OK' if ok else 'failed'} — {msg}")
            except Exception as exc:                        # noqa: BLE001
                print(f"   nse:  failed — {exc}")
                if "503" in str(exc):
                    print("         (NSE refusing — their end, not yours)")

    # ── 3. rebuild levels ──────────────────────────────────────────────
    # Unconditionally, even when nothing new was fetched. A fresh CSV and
    # stale master_levels.csv is a state that has caught us out before: the
    # data was right, the levels were not, and nothing said so.
    print("\n3. rebuilding master levels")
    try:
        bm.main()
        hist = bm.load_all(config.OHLC_DIR, quiet=True)
        last = hist.iloc[-1]
        print(f"   levels now from {last['Date']:%d-%b-%Y} "
              f"close {last['Close']:,.2f}")
    except Exception as exc:                                # noqa: BLE001
        print(f"   failed — {exc}")

    # ── 4. PDC ─────────────────────────────────────────────────────────
    print("\n4. PDC (15:15 close per session)")
    try:
        import pdc
        pok, pmsg = pdc.backfill(since=target - timedelta(days=30))
        print(f"   {'OK' if pok else 'failed'} — {pmsg}")
        if pok:
            df = pdc.load()
            tail = df.tail(3)
            for _, r in tail.iterrows():
                print(f"     {r['session']}  close {r['close_close']:,.2f}")
    except Exception as exc:                                # noqa: BLE001
        print(f"   failed — {exc}")

    line("=")
    print("Restart the app to pick up the new levels.")
    line("=")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
