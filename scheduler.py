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
    line = f"[{datetime.now():%d-%b %H:%M:%S}] {msg}"
    print(line, flush=True)
    try:
        log_path = config.DATA_DIR / "scheduler.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def in_window(now: dtime, window: tuple[dtime, dtime]) -> bool:
    start, end = window
    if start <= end:
        return start <= now <= end
    return now >= start or now <= end          # window crossing midnight


# ═══════════════════════════════════════════════════════════════════════════
# Jobs
# ═══════════════════════════════════════════════════════════════════════════

_NSE_CLIENT = None
_CONSECUTIVE_CHAIN_FAILURES = 0
_LAST_PAYLOAD_HASH = None


def get_nse_client():
    """Persists a singleton NSEClient instance across polls to reuse cookies."""
    global _NSE_CLIENT
    if _NSE_CLIENT is None:
        import nse_client
        _NSE_CLIENT = nse_client.NSEClient()
    return _NSE_CLIENT


def record_heartbeat(outcome: str, spot: float | None = None,
                     exc: Exception | None = None,
                     skip_reason: str | None = None,
                     drift_minutes: float | None = None) -> None:
    """Writes data/scheduler_heartbeat.json (latest) AND appends to data/scheduler_heartbeat.jsonl (forensics)."""
    global _CONSECUTIVE_CHAIN_FAILURES
    hb_path = config.DATA_DIR / "scheduler_heartbeat.json"
    jsonl_path = config.DATA_DIR / "scheduler_heartbeat.jsonl"
    now_str = datetime.now().isoformat()
    record = {
        "timestamp": now_str,
        "job": "chain",
        "outcome": outcome,
        "spot": spot,
        "consecutive_failures": _CONSECUTIVE_CHAIN_FAILURES,
        "error_class": exc.__class__.__name__ if exc else None,
        "error_message": str(exc) if exc else None,
        "skip_reason": skip_reason,
        "drift_minutes": drift_minutes,
    }
    try:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        # 1. Latest snapshot for fast status / UI staleness checks
        hb_path.write_text(json.dumps(record, indent=2))

        # 2. Append-only ledger for gap forensics
        with open(jsonl_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as e:
        log(f"heartbeat write failed: {e}")


def find_heartbeat_gaps(session_date: date | str, min_gap_minutes: float = 6.0,
                        jsonl_path: Path | None = None) -> list[dict]:
    """
    Reads data/scheduler_heartbeat.jsonl and reports, for a given session date,
    every gap longer than min_gap_minutes with start, end, duration, and cause attribution.
    """
    path = jsonl_path or (config.DATA_DIR / "scheduler_heartbeat.jsonl")
    if not path.exists():
        return []

    target_str = str(session_date)
    records = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                ts_str = rec.get("timestamp", "")
                if ts_str.startswith(target_str):
                    ts = datetime.fromisoformat(ts_str)
                    records.append((ts, rec))
            except Exception:
                continue

    if not records:
        return []

    records.sort(key=lambda x: x[0])
    gaps = []
    for i in range(1, len(records)):
        prev_t, prev_rec = records[i - 1]
        curr_t, curr_rec = records[i]
        diff_mins = (curr_t - prev_t).total_seconds() / 60.0
        if diff_mins >= min_gap_minutes:
            cause = "NO_RECORDED_HEARTBEAT (machine sleep, service stopped, or lid closed)"
            if prev_rec.get("outcome") == "FAILURE":
                cause = f"Repeated failures ({prev_rec.get('error_class')}: {prev_rec.get('error_message')})"
            elif prev_rec.get("outcome") == "SKIPPED":
                cause = f"Skipped ({prev_rec.get('skip_reason')})"

            gaps.append({
                "gap_start": prev_t.strftime("%H:%M:%S"),
                "gap_end": curr_t.strftime("%H:%M:%S"),
                "gap_minutes": round(diff_mins, 1),
                "last_status_before_gap": prev_rec.get("outcome"),
                "resumed_status": curr_rec.get("outcome"),
                "attributed_cause": cause,
            })
    return gaps


def job_chain(st: dict) -> None:
    """Fetch the chain, score it, append a row to the signal log."""
    global _CONSECUTIVE_CHAIN_FAILURES, _LAST_PAYLOAD_HASH
    try:
        import hashlib
        import chain_parser as cp
        import analytics as an
        import store

        client = get_nse_client()
        payload = client.fetch_raw()
        now = datetime.now()

        # Check payload staleness via hash
        payload_bytes = json.dumps(payload, sort_keys=True).encode("utf-8")
        payload_hash = hashlib.sha256(payload_bytes).hexdigest()
        is_stale = (_LAST_PAYLOAD_HASH is not None and payload_hash == _LAST_PAYLOAD_HASH)
        _LAST_PAYLOAD_HASH = payload_hash

        # Check NSE timestamp vs local clock for clock drift (warning only, never raise)
        drift_minutes = None
        payload_ts_str = client.timestamp(payload)
        if payload_ts_str:
            try:
                payload_dt = datetime.strptime(payload_ts_str, "%d-%b-%Y %H:%M:%S")
                if now.weekday() < 5 and dtime(9, 15) <= now.time() <= dtime(15, 30):
                    if payload_dt.date() == now.date():
                        clock_drift_sec = abs((now - payload_dt).total_seconds())
                        drift_minutes = round(clock_drift_sec / 60.0, 1)
                        if clock_drift_sec > 900:  # 15 min limit
                            log(
                                f"CLOCK_DRIFT WARNING: local clock ({now:%H:%M:%S}) and NSE timestamp ({payload_dt:%H:%M:%S}) "
                                f"diverge by {drift_minutes} minutes (>15m limit). Continuing poll."
                            )
                    else:
                        # Payload is from a previous session (e.g. weekend or pre-market cache), not clock drift
                        drift_minutes = None
            except ValueError:
                pass

        expiries = client.expiries(payload)
        expiry = expiries[config.EXPIRY_INDEX] if expiries else None

        # Real continuous fractional calendar DTE
        days_to_expiry = None
        if expiry:
            for fmt in ("%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d"):
                try:
                    exp_dt = datetime.strptime(expiry, fmt).replace(hour=15, minute=30, second=0)
                    days_to_expiry = max(0.0, (exp_dt - now).total_seconds() / 86400.0)
                    break
                except ValueError:
                    pass

        # Capture all option rows present in payload (all expiries)
        df_all = cp.parse_json(payload, expiry="all")
        spot = client.spot(payload)

        # Slice nearest expiry for analysis
        if expiry and "expiry" in df_all.columns:
            norm_exp = cp._norm_exp(expiry)
            df_nearest = df_all[df_all["expiry"] == norm_exp]
            if df_nearest.empty:
                df_nearest = df_all
        else:
            df_nearest = df_all

        read = an.analyse(df_nearest, spot)
        sig = an.make_signal(df_nearest, read, now,
                             days_to_expiry=days_to_expiry, confirmed=True)
        store.append_log(an.signal_to_row(read, sig, now))

        # Dual snapshot persistence:
        # 1. Single-expiry (nearest) slice saved to data/snapshots/ (protects existing pipeline)
        store.save_snapshot(df_nearest, spot, now, expiry=expiry)

        # 2. 3-expiry subset (current weekly, next weekly, monthly) saved to data/snapshots_multi/
        raw_dates = payload.get("records", {}).get("expiryDates") or payload.get("expiryDates") or []
        target_3 = []
        if raw_dates:
            target_3.append(raw_dates[0])
            if len(raw_dates) > 1:
                target_3.append(raw_dates[1])
            try:
                dt0 = datetime.strptime(raw_dates[0], "%d-%b-%Y")
                month_dates = [d for d in raw_dates if datetime.strptime(d, "%d-%b-%Y").month == dt0.month]
                if month_dates and month_dates[-1] not in target_3:
                    target_3.append(month_dates[-1])
                elif len(raw_dates) > 2 and raw_dates[2] not in target_3:
                    target_3.append(raw_dates[2])
            except Exception:
                if len(raw_dates) > 2 and raw_dates[2] not in target_3:
                    target_3.append(raw_dates[2])

        if target_3 and "expiry" in df_all.columns:
            norm_3 = {cp._norm_exp(d) for d in target_3}
            df_3 = df_all[df_all["expiry"].isin(norm_3)]
        else:
            df_3 = df_all
        store.save_snapshot(df_3, spot, now, expiry=expiry, base_dir=config.SNAPSHOT_MULTI_DIR)

        st["chain_polls"] += 1
        st["last_chain"] = now.strftime("%H:%M:%S")
        _CONSECUTIVE_CHAIN_FAILURES = 0
        outcome = "SUCCESS_STALE" if is_stale else "SUCCESS"
        record_heartbeat(outcome, spot=spot, drift_minutes=drift_minutes)

        log(f"chain  spot {spot:,.2f}  {read.regime}  "
            f"score {read.bias_score:+.1f}  S1 {read.levels.s1} "
            f"R1 {read.levels.r1}  (poll {st['chain_polls']})")
    except Exception as exc:                                # noqa: BLE001
        _CONSECUTIVE_CHAIN_FAILURES += 1
        record_heartbeat("FAILURE", exc=exc)
        exc_class = exc.__class__.__name__
        exc_msg = str(exc)
        if _CONSECUTIVE_CHAIN_FAILURES >= 3:
            log(f"chain CRITICAL [{_CONSECUTIVE_CHAIN_FAILURES} consecutive failures] ({exc_class}): {exc_msg}")
        else:
            log(f"chain FAILED (attempt {_CONSECUTIVE_CHAIN_FAILURES}, {exc_class}): {exc_msg}")


def job_ohlc(st: dict) -> None:
    """
    Append today's completed bar, then rebuild the master levels.

    Refuses before 15:30 on NSE's own clock, so a partial bar cannot get in.
    """
    if st.get("ohlc_done"):
        return
    try:
        import market_data as md

        client = get_nse_client()
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
        try:
            rules_path = config.ROOT / "strategy1_rules.json"
            retention = 90
            if rules_path.exists():
                cfg = json.loads(rules_path.read_text())
                retention = cfg.get("greeks", {}).get("multi_expiry_retention_days", 90)
            pruned = store.prune_multi_snapshots(retention)
            if len(pruned) > 0:
                log(f"retention: pruned {len(pruned)} old multi-expiry snapshot folder(s) (> {retention} days): {pruned}")
        except Exception as p_exc:                          # noqa: BLE001
            log(f"retention check failed: {p_exc}")
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

    # Assert system timezone is Asia/Kolkata (UTC+5:30) at startup
    tz_offset = (datetime.now() - datetime.utcnow()).total_seconds()
    if not (19700 <= tz_offset <= 19900):  # 5.5 hours = 19800 seconds
        raise RuntimeError(
            f"Scheduler must run in Asia/Kolkata timezone (UTC+05:30), but detected offset {tz_offset/3600:.2f}h. "
            "Configure system timezone with `timedatectl set-timezone Asia/Kolkata` or set TZ=Asia/Kolkata."
        )

    log("scheduler starting")
    log(f"  chain  {config.CHAIN_WINDOW[0]:%H:%M}-{config.CHAIN_WINDOW[1]:%H:%M}"
        f"  every {config.POLL_SECONDS}s")
    log(f"  view   {config.VIEW_WINDOW[0]:%H:%M}-{config.VIEW_WINDOW[1]:%H:%M}"
        f"  every {config.VIEW_REFRESH_MINUTES}m")
    log(f"  ohlc   {config.OHLC_RETRY_FROM:%H:%M}-"
        f"{config.OHLC_RETRY_UNTIL:%H:%M}  retry every "
        f"{config.OHLC_RETRY_MINUTES}m until it lands")

    try:
        rules_path = config.ROOT / "strategy1_rules.json"
        retention = 90
        if rules_path.exists():
            cfg = json.loads(rules_path.read_text())
            retention = cfg.get("greeks", {}).get("multi_expiry_retention_days", 90)
        pruned = store.prune_multi_snapshots(retention)
        if len(pruned) > 0:
            log(f"startup retention: pruned {len(pruned)} old multi-expiry snapshot folder(s) (> {retention} days): {pruned}")
    except Exception as p_exc:                              # noqa: BLE001
        log(f"startup retention check failed: {p_exc}")

    next_chain = next_view = next_ohlc = datetime.min
    last_skip_hb = datetime.min

    try:
        while True:
            st = load_state()
            now = datetime.now()
            t = now.time()
            is_mkt_day = now.weekday() < 5
            in_chain_win = in_window(t, config.CHAIN_WINDOW)

            if is_mkt_day and in_chain_win:
                if now >= next_chain:
                    job_chain(st)
                    next_chain = now + timedelta(seconds=config.POLL_SECONDS)
            else:
                if (now - last_skip_hb).total_seconds() >= 60:
                    reason = "Weekend (market closed)" if not is_mkt_day else f"Outside window {config.CHAIN_WINDOW[0]:%H:%M}-{config.CHAIN_WINDOW[1]:%H:%M}"
                    record_heartbeat("SKIPPED", skip_reason=reason)
                    last_skip_hb = now

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
