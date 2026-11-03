#!/usr/bin/env python3
"""backfill_history.py — pull months of 1-minute history from Dhan into candles_1m.

WHY
The desk only has history from the day it started recording (8 Sep 2026). Nothing caps it below
that: a full year of 1-minute bars for one instrument measures ~6.3 MB on disk, about 71 bytes a
row. The data simply was never fetched. This fetches it.

HOW
Dhan's POST /v2/charts/intraday serves a bounded date range per request (a 90-day cap is
documented by third-party clients; this walks in smaller chunks by default to stay well inside it
and to keep each request cheap). The script steps BACKWARDS from today in chunks, upserting each
one, and stops once Dhan stops returning candles — which is how you discover how far back its
intraday history actually goes, since that depth is not documented anywhere reliable.

Re-running is safe: the underlying writer upserts, so overlapping ranges overwrite rather than
duplicate.

SCOPE
Index only by default (NIFTY = 13, INDIA VIX = 21 on IDX_I). Futures live on NSE_FNO under a
security id that CHANGES EVERY MONTH at expiry, so backfilling them across a year means resolving
each month's contract first — a separate job this script does not attempt. Volume-based layers
(stacked nodes, footprints, CVD) read futures, so they will NOT gain history from this.

SAFETY
Writes to the same SQLite file the live desk writes to. It refuses to run during market hours
unless --force is passed, because a long write loop against a database the desk is polling invites
"database is locked" in the middle of a session.

USAGE
    python3 scripts/backfill_history.py --days 365                 # NIFTY index, one year
    python3 scripts/backfill_history.py --days 365 --sec 21        # INDIA VIX
    python3 scripts/backfill_history.py --days 30 --dry-run        # show the plan, fetch nothing
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "ict_smc_app" / "backend"
sys.path.insert(0, str(BACKEND))

IST = timezone(timedelta(hours=5, minutes=30))


def market_hours_now() -> bool:
    n = datetime.now(IST)
    if n.weekday() >= 5:
        return False
    mins = n.hour * 60 + n.minute
    return (9 * 60 + 10) <= mins <= (15 * 60 + 40)


def check_credentials() -> int:
    """Probe two endpoints to separate two very different failures.

    A live quote and a historical chart authenticate the same way but are entitled separately on
    Dhan. If the quote works and the chart does not, the token is fine and the DATA API is not
    enabled — no amount of re-pasting the token fixes that. If both fail, it is the token.
    """
    import httpx
    from app import config                                    # noqa: E402

    token = config.env("DHAN_ACCESS_TOKEN") or ""
    client = config.env("DHAN_CLIENT_ID") or ""
    print(f"DHAN_ACCESS_TOKEN: {len(token)} chars   DHAN_CLIENT_ID: {len(client)} chars")
    if not token or not client:
        print("  -> missing from .env entirely.")
        return 2
    # A JWT has two dots. Dhan access tokens are JWTs, and the commonest paste error is grabbing
    # only part of one, which looks plausible by length but fails every call.
    print(f"  -> looks like a JWT: {token.count('.') == 2}"
          f"   (expected True; a truncated paste is the usual cause of DH-906)")

    base = config.base_url()
    hdr = {"access-token": token, "client-id": client,
           "Content-Type": "application/json", "Accept": "application/json"}
    with httpx.Client(timeout=15.0) as c:
        try:
            r = c.post(f"{base}/marketfeed/ltp", json={"IDX_I": [13]}, headers=hdr)
            print(f"\nlive quote  /marketfeed/ltp      -> HTTP {r.status_code}  {r.text[:140]}")
            quote_ok = r.status_code == 200
        except Exception as e:
            print(f"\nlive quote  /marketfeed/ltp      -> {type(e).__name__}: {e}")
            quote_ok = False
        try:
            body = {"securityId": "13", "exchangeSegment": "IDX_I", "instrument": "INDEX",
                    "interval": "1", "oi": False,
                    "fromDate": f"{date.today() - timedelta(days=5)} 09:15:00",
                    "toDate": f"{date.today()} 15:30:00"}
            r = c.post(f"{base}/charts/intraday", json=body, headers=hdr)
            print(f"history     /charts/intraday     -> HTTP {r.status_code}  {r.text[:140]}")
            hist_ok = r.status_code == 200
        except Exception as e:
            print(f"history     /charts/intraday     -> {type(e).__name__}: {e}")
            hist_ok = False

    print()
    if quote_ok and hist_ok:
        print("Both work. Re-run the backfill.")
        return 0
    if quote_ok and not hist_ok:
        print("VERDICT: the token is VALID — the live quote went through. Only the historical\n"
              "chart endpoint refuses it, which means the Data API is not enabled on this Dhan\n"
              "account. Re-pasting the token will not change that; it has to be enabled on\n"
              "Dhan's side. Your live desk is unaffected.")
        return 4
    print("VERDICT: the token itself is being rejected — even a live quote fails. Generate a\n"
          "fresh access token on Dhan and write it to .env, then run --check again.")
    return 3


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="probe credentials against both a live quote and a historical chart, "
                         "then stop")
    ap.add_argument("--days", type=int, default=365, help="how far back to walk, in calendar days")
    ap.add_argument("--chunk", type=int, default=60, help="calendar days per request (Dhan caps near 90)")
    ap.add_argument("--sec", default="13", help="security id (13 = NIFTY 50, 21 = INDIA VIX)")
    ap.add_argument("--segment", default="IDX_I")
    ap.add_argument("--instrument", default="INDEX")
    ap.add_argument("--interval", default="1", help="minutes per candle")
    ap.add_argument("--sleep", type=float, default=2.0, help="seconds between requests")
    ap.add_argument("--stop-after-empty", type=int, default=2,
                    help="give up after this many consecutive chunks return nothing")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="run even during market hours")
    a = ap.parse_args()

    if a.check:
        return check_credentials()

    if market_hours_now() and not a.force:
        print("Market is open. This writes to the database the live desk is using, and a long\n"
              "write loop can lock it mid-session. Run it after 15:40, or pass --force.",
              file=sys.stderr)
        return 2

    chunks: list[tuple[date, date]] = []
    today = datetime.now(IST).date()
    end = today
    while (today - end).days < a.days:
        start = max(end - timedelta(days=a.chunk - 1), today - timedelta(days=a.days))
        chunks.append((start, end))
        if start <= today - timedelta(days=a.days):
            break
        end = start - timedelta(days=1)

    print(f"security {a.sec} ({a.segment}/{a.instrument})   interval {a.interval}m")
    print(f"walking back {a.days} days in {len(chunks)} chunks of up to {a.chunk} days, "
          f"newest first, {a.sleep}s apart\n")
    if a.dry_run:
        for i, (s, e) in enumerate(chunks, 1):
            print(f"  {i:>2}. {s} → {e}")
        print("\n--dry-run: nothing fetched.")
        return 0

    from app.engine.htf_wick_ema_rb import backfill_dhan_intraday   # noqa: E402

    total = 0
    empty_streak = 0
    for i, (s, e) in enumerate(chunks, 1):
        try:
            r = backfill_dhan_intraday(a.sec, a.segment, a.instrument, a.interval,
                                       s.isoformat(), e.isoformat())
        except Exception as exc:                     # one bad chunk must not lose the rest
            print(f"  {i:>2}. {s} → {e}   ERROR {type(exc).__name__}: {exc}")
            empty_streak += 1
            if empty_streak >= a.stop_after_empty:
                print("\nstopping: consecutive failures.")
                break
            time.sleep(a.sleep)
            continue

        n = int(r.get("inserted") or r.get("count") or len(r.get("rows") or []) or 0)
        if r.get("ok") is False:
            err = str(r.get("error") or "")
            print(f"  {i:>2}. {s} → {e}   FAILED  {r.get('status_code', '')} {err[:90]}")
            # An auth or entitlement refusal is NOT "the data runs out here". Saying so would
            # send you hunting for missing history when the real problem is the token, so these
            # abort immediately with the actual cause.
            low = err.lower()
            if any(k in low for k in ("dh-906", "invalid token", "invalid_auth", "unauthor",
                                      "dh-901", "forbidden", "access")) or \
                    r.get("status_code") in (401, 403):
                print("\nSTOPPING — Dhan rejected the credentials, so no date range would have\n"
                      "worked. This says nothing about how far back its history goes.\n"
                      "Fix the token first, then re-run. Nothing was written.")
                return 3
            empty_streak += 1
        elif n == 0:
            print(f"  {i:>2}. {s} → {e}   no candles returned")
            empty_streak += 1
        else:
            print(f"  {i:>2}. {s} → {e}   {n:,} candles")
            total += n
            empty_streak = 0

        if empty_streak >= a.stop_after_empty:
            print(f"\nStopping — {empty_streak} chunks in a row came back empty. That is almost\n"
                  f"certainly the edge of Dhan's intraday history, not a bug.")
            break
        time.sleep(a.sleep)

    print(f"\n{total:,} candles written.")
    if total:
        print("Verify with:  python3 scripts/backtest_wick_zones.py --sec", a.sec,
              "   (its header prints the session count and date range)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
