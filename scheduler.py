"""
Scheduler — runs each job in its own window, once per day where that matters.

    python scheduler.py              # run in the foreground
    python scheduler.py --status     # what has run today, then exit
    python scheduler.py --once       # do one pass and exit (for cron)

THREE JOBS, THREE WINDOWS

  09:00 - 16:00   option chain, levels, signal log, every 3 minutes
  01:00 - 10:00   View Building sources, every 30 minutes
  15:30 - 23:00   daily OHLC append, RETRYING until it succeeds

WHY THE OHLC JOB RETRIES RATHER THAN FIRING ONCE

NSE does not publish the completed daily bar at a predictable time. A single
cron entry at 17:30 works most days and silently does nothing on the days it
is late — and you would not notice until the next morning's levels came out
wrong. So this attempts every ten minutes from 15:30 and STOPS THE MOMENT IT
SUCCEEDS.

The append itself is already idempotent (a repeat updates the row rather than
duplicating it), but a state file makes the intent explicit: once today's bar
is in, the job is done for the day and stops asking.

STATE

data/scheduler_state.json records what has completed today. It resets on date
change, so a machine left running over a weekend picks up correctly on Monday
without anything special.
"""
from __future__ import annotations

import json
import sys
import time as _time
from datetime import date, datetime, time as dtime, timedelta

import config


# ═══════════════════════════════════════════════════════════════════════════
# State
# ═══════════════════════════════════════════════════════════════════════════

def load_state() -> dict:
    """Today's progress. Anything from an earlier date is discarded."""
    today = date.today().isoformat()
    path = config.SCHEDULER_STATE
    if path.exists():
        try:
            st = json.loads(path.read_text())
            if st.get("date") == today:
                return st
        except Exception:                                   # noqa: BLE001
            pass
    return {"date": today, "ohlc_done": False, "ohlc_attempts": 0,
            "ohlc_message": "", "chain_polls": 0, "view_refreshes": 0,
            "last_chain": "", "last_view": ""}


def save_state(st: dict) -> None:
    st["date"] = date.today().isoformat()
    try:
        config.SCHEDULER_STATE.parent.mkdir(parents=True, exist_ok=True)
        config.SCHEDULER_STATE.write_text(json.dumps(st, indent=2))
    except Exception as exc:                                # noqa: BLE001
        log(f"could not save state: {exc}")


def log(msg: str) -> None:
    print(f"[{datetime.now():%d-%b %H:%M:%S}] {msg}", flush=True)


def in_window(now: dtime, window: tuple[dtime, dtime]) -> bool:
    start, end = window
    if start <= end:
        return start <= now <= end
    return now >= start or now <= end          # window crossing midnight


# ═══════════════════════════════════════════════════════════════════════════
# Jobs
# ═══════════════════════════════════════════════════════════════════════════

def job_chain(st: dict) -> None:
    """Fetch the chain, score it, append a row to the signal log."""
    try:
        import nse_client
        import chain_parser as cp
        import analytics as an
        import store

        client = nse_client.NSEClient()
        payload = client.fetch_raw()
        expiry = client.expiries(payload)[config.EXPIRY_INDEX]
        df = cp.parse_json(payload, expiry)
        spot = client.spot(payload)

        read = an.analyse(df, spot)
        now = datetime.now()
        sig = an.make_signal(df, read, now,
                             days_to_expiry=1.0, confirmed=True)
        store.append_log(an.signal_to_row(read, sig, now))
        store.save_snapshot(df, spot, now)

        st["chain_polls"] += 1
        st["last_chain"] = now.strftime("%H:%M:%S")
        log(f"chain  spot {spot:,.2f}  {read.regime}  "
            f"score {read.bias_score:+.1f}  S1 {read.levels.s1} "
            f"R1 {read.levels.r1}  (poll {st['chain_polls']})")
    except Exception as exc:                                # noqa: BLE001
        log(f"chain  FAILED: {exc}")


def job_ohlc(st: dict) -> None:
    """
    Append today's completed bar, then rebuild the master levels.

    Refuses before 15:30 on NSE's own clock, so a partial bar cannot get in.
    """
    if st.get("ohlc_done"):
        return
    try:
        import nse_client
        import market_data as md

        client = nse_client.NSEClient()
        client._bootstrap()
        st["ohlc_attempts"] += 1
        ok, msg = md.backfill_ohlc(client, config.OHLC_BACKFILL_DAYS)
        st["ohlc_message"] = msg

        if not ok:
            log(f"ohlc   not yet (attempt {st['ohlc_attempts']}): {msg}")
            return

        log(f"ohlc   {msg}")
        try:
            import build_master
            build_master.main()
            ohlc = build_master.load_all(config.OHLC_DIR)
            lv = build_master.dated_levels(ohlc)
            out = config.DATA_DIR / f"levels_{lv['date']}.txt"
            out.write_text(build_master.format_copy_paste(lv))
            log(f"master rebuilt · levels written to {out.name}")
        except Exception as exc:                            # noqa: BLE001
            log(f"master rebuild failed: {exc}")

        st["ohlc_done"] = True
        log("ohlc   done for today — will not run again until tomorrow")
    except Exception as exc:                                # noqa: BLE001
        log(f"ohlc   FAILED (attempt {st['ohlc_attempts']}): {exc}")


def job_view(st: dict) -> None:
    """Warm the View Building sources so the tab opens instantly."""
    try:
        import nse_client
        import market_data as md

        client = nse_client.NSEClient()
        client._bootstrap()
        bits = []

        try:
            g = md.fetch_gift_nifty_nse(client)
            bits.append(f"gift {g.value:,.0f}")
        except Exception:                                   # noqa: BLE001
            bits.append("gift ?")
        try:
            nf = md.fetch_index_futures(client, "NIFTY")
            bits.append(f"fut {nf.futures:,.0f} ({nf.expiry})")
        except Exception:                                   # noqa: BLE001
            bits.append("fut ?")
        try:
            ad = md.fetch_advance_decline(client, "NIFTY 50")
            bits.append(f"breadth {ad['advances']}A/{ad['declines']}D")
        except Exception:                                   # noqa: BLE001
            bits.append("breadth ?")
        try:
            poi = md.fetch_participant_best(client)
            bits.append(f"FII net {poi.fii_net:+,}")
        except Exception:                                   # noqa: BLE001
            bits.append("FII ?")

        st["view_refreshes"] += 1
        st["last_view"] = datetime.now().strftime("%H:%M:%S")
        log(f"view   {' · '.join(bits)}  (refresh {st['view_refreshes']})")
    except Exception as exc:                                # noqa: BLE001
        log(f"view   FAILED: {exc}")


# ═══════════════════════════════════════════════════════════════════════════
# Loop
# ═══════════════════════════════════════════════════════════════════════════

def status() -> int:
    st = load_state()
    print("=" * 62)
    print(f"SCHEDULER STATUS — {st['date']}")
    print("=" * 62)
    print(f"  chain polls      {st['chain_polls']}"
          + (f"   last {st['last_chain']}" if st["last_chain"] else ""))
    print(f"  view refreshes   {st['view_refreshes']}"
          + (f"   last {st['last_view']}" if st["last_view"] else ""))
    print(f"  ohlc appended    {'YES' if st['ohlc_done'] else 'not yet'}"
          f"   ({st['ohlc_attempts']} attempt(s))")
    if st.get("ohlc_message"):
        print(f"    last message: {st['ohlc_message']}")
    now = datetime.now().time()
    print(f"\n  now {now:%H:%M}")
    for name, win in (("chain", config.CHAIN_WINDOW),
                      ("view", config.VIEW_WINDOW)):
        inside = in_window(now, win)
        print(f"    {name:<6} {win[0]:%H:%M}-{win[1]:%H:%M}  "
              f"{'ACTIVE' if inside else 'idle'}")
    ohlc_live = config.OHLC_RETRY_FROM <= now <= config.OHLC_RETRY_UNTIL
    print(f"    ohlc   {config.OHLC_RETRY_FROM:%H:%M}-"
          f"{config.OHLC_RETRY_UNTIL:%H:%M}  "
          f"{'ACTIVE' if ohlc_live and not st['ohlc_done'] else 'idle'}")
    print("=" * 62)
    return 0


def main() -> int:
    if "--status" in sys.argv:
        return status()
    once = "--once" in sys.argv

    log("scheduler starting")
    log(f"  chain  {config.CHAIN_WINDOW[0]:%H:%M}-{config.CHAIN_WINDOW[1]:%H:%M}"
        f"  every {config.POLL_SECONDS}s")
    log(f"  view   {config.VIEW_WINDOW[0]:%H:%M}-{config.VIEW_WINDOW[1]:%H:%M}"
        f"  every {config.VIEW_REFRESH_MINUTES}m")
    log(f"  ohlc   {config.OHLC_RETRY_FROM:%H:%M}-"
        f"{config.OHLC_RETRY_UNTIL:%H:%M}  retry every "
        f"{config.OHLC_RETRY_MINUTES}m until it lands")

    next_chain = next_view = next_ohlc = datetime.min

    try:
        while True:
            st = load_state()
            now = datetime.now()
            t = now.time()

            if in_window(t, config.CHAIN_WINDOW) and now >= next_chain:
                job_chain(st)
                next_chain = now + timedelta(seconds=config.POLL_SECONDS)

            if in_window(t, config.VIEW_WINDOW) and now >= next_view:
                job_view(st)
                next_view = now + timedelta(minutes=config.VIEW_REFRESH_MINUTES)

            if (not st.get("ohlc_done")
                    and config.OHLC_RETRY_FROM <= t <= config.OHLC_RETRY_UNTIL
                    and now >= next_ohlc):
                job_ohlc(st)
                next_ohlc = now + timedelta(minutes=config.OHLC_RETRY_MINUTES)

            save_state(st)
            if once:
                log("single pass complete")
                return 0
            _time.sleep(20)
    except KeyboardInterrupt:
        log("stopped")
        return 0


if __name__ == "__main__":
    sys.exit(main())
