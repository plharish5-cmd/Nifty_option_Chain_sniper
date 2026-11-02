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
import os
import sys
import time as _time
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Optional

import config
import store


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
                st.setdefault("last_oi_dominance", "WARMUP")
                st.setdefault("last_oi_alert_msg", "")
                st.setdefault("last_oi_alert_time", "")
                st.setdefault("last_oi_alert_key", "")
                st.setdefault("last_barrier_alert_msg", "")
                st.setdefault("last_barrier_alert_time", "")
                st.setdefault("last_trap_alert_msg", "")
                st.setdefault("last_trap_alert_time", "")
                st.setdefault("last_expiry_pcr", None)
                st.setdefault("last_expiry_pcr_date", "")
                st.setdefault("last_pcr_squeeze_time", "")
                st.setdefault("last_rvol_regime", "")
                st.setdefault("last_rvol_alert_msg", "")
                st.setdefault("last_rvol_alert_time", "")
                st.setdefault("last_rvol_open_alert_key", "")
                st.setdefault("last_rvol_shift_key", "")
                st.setdefault("last_floor_test_msg", "")
                st.setdefault("last_floor_test_time", "")
                st.setdefault("last_ceiling_test_msg", "")
                st.setdefault("last_ceiling_test_time", "")
                st.setdefault("alert_cooldowns", {})
                st.setdefault("rvol_signals_suppressed", 0)
                st.setdefault("rvol_signals_allowed", 0)
                st.setdefault("fast_window_chain_polls", 0)
                st.setdefault("last_rvol_signal_counted_minute", "")
                return st
        except Exception:                                   # noqa: BLE001
            pass
    return {"date": today, "ohlc_done": False, "ohlc_attempts": 0,
            "ohlc_message": "", "chain_polls": 0, "view_refreshes": 0,
            "last_chain": "", "last_view": "",
            "last_oi_dominance": "WARMUP", "last_oi_alert_msg": "",
            "last_oi_alert_time": "", "last_oi_alert_key": "",
            "last_barrier_alert_msg": "", "last_barrier_alert_time": "",
            "last_trap_alert_msg": "", "last_trap_alert_time": "",
            "last_expiry_pcr": None, "last_expiry_pcr_date": "",
            "last_pcr_squeeze_time": "",
            "last_rvol_regime": "", "last_rvol_alert_msg": "",
            "last_rvol_alert_time": "", "last_rvol_open_alert_key": "",
            "last_rvol_shift_key": "",
            "last_floor_test_msg": "", "last_floor_test_time": "",
            "last_ceiling_test_msg": "", "last_ceiling_test_time": "",
            "alert_cooldowns": {},
            "rvol_signals_suppressed": 0,
            "rvol_signals_allowed": 0,
            "fast_window_chain_polls": 0,
            "last_rvol_signal_counted_minute": ""}


def save_state(st: dict) -> None:
    st["date"] = date.today().isoformat()
    try:
        config.SCHEDULER_STATE.parent.mkdir(parents=True, exist_ok=True)
        config.SCHEDULER_STATE.write_text(json.dumps(st, indent=2, default=str))
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


# ═══════════════════════════════════════════════════════════════════════════
# OI Dominance Alerts (Telegram)
# ═══════════════════════════════════════════════════════════════════════════

_SESSION_SNAPSHOTS: list[tuple[datetime, float, Any]] = []
_SESSION_DATE: date | None = None


def _format_spread(spread: float) -> str:
    """Format spread in contracts as +51k / -42k or +500."""
    if abs(spread) >= 1000:
        return f"{spread / 1000:+.0f}k"
    return f"{spread:+.0f}"


def _load_env() -> None:
    """Reads .env into os.environ without external dependencies or logging secrets."""
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    try:
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v
    except Exception as exc:  # noqa: BLE001
        log(f"env load warning: {exc}")


def send_telegram_alert(text: str, parse_mode: Optional[str] = None) -> bool:
    """
    Sends an alert message via Telegram Bot API using credentials from .env.
    Never prints or logs tokens or chat IDs. Catches all errors, logs and continues.
    """
    _load_env()
    tok = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_TOKEN") or os.environ.get("TG_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID") or os.environ.get("TG_CHAT_ID")
    if not (tok and chat_id):
        log("oi_alert: Telegram credentials not configured in .env (TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID); alert omitted.")
        return False
    try:
        import requests
        url = f"https://api.telegram.org/bot{tok}/sendMessage"
        payload = {"chat_id": chat_id, "text": text}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 200:
            if len(text) <= 120:
                summary = text
            elif "\n" in text:
                clean_lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("<pre") and not ln.strip().startswith("═")]
                summary = clean_lines[0] if clean_lines else text.splitlines()[0]
            else:
                summary = text[:100] + "..."
            log(f"oi_alert: Telegram message delivered: {summary}")
            return True
        log(f"oi_alert: Telegram delivery failed (HTTP {resp.status_code}): {resp.text[:150]}")
        return False
    except Exception as exc:  # noqa: BLE001
        log(f"oi_alert: Telegram post exception ({exc.__class__.__name__}): {exc}")
        return False


def _is_session_data_gap(now: datetime, rules_data: dict) -> bool:
    """Returns True if the elapsed time from preceding snapshot exceeds max_gap_seconds."""
    global _SESSION_SNAPSHOTS
    max_gap_sec = float(rules_data.get("data_gap", {}).get("max_gap_seconds", rules_data.get("max_gap_seconds", 180.0)))
    if len(_SESSION_SNAPSHOTS) >= 2:
        prev_snap_ts = _SESSION_SNAPSHOTS[-2][0]
        if (now - prev_snap_ts).total_seconds() > max_gap_sec:
            return True
    elif len(_SESSION_SNAPSHOTS) == 1:
        mkt_open_t = dtime(9, 15)
        if now.time() > mkt_open_t:
            open_dt = datetime.combine(now.date(), mkt_open_t)
            if (now - open_dt).total_seconds() > max_gap_sec:
                return True
    return False


def check_oi_dominance_alert(df: Any, spot: float, now: datetime, st: dict) -> None:
    """
    Evaluates ATM strike intraday OI dominance timeline and dispatches Telegram alert on state shift.
    Rules:
      - Alert only ONCE per state change (BALANCED -> PE_DOMINANT, BALANCED -> CE_DOMINANT, Any -> CROSSOVER)
      - Suppressed during WARMUP (<09:50) and EOD_UNWIND (>=15:00)
      - Suppressed during buyer freeze window (11:30-13:15)
      - All thresholds loaded from strategy1_rules.json under 'oi_alerts'
      - Zero bare asserts, zero unhandled exceptions
    """
    global _SESSION_SNAPSHOTS, _SESSION_DATE
    try:
        if df is None or getattr(df, "empty", True) or spot <= 0:
            return

        today = now.date()
        if _SESSION_DATE != today:
            _SESSION_DATE = today
            _SESSION_SNAPSHOTS = []
            st.setdefault("last_oi_dominance", "WARMUP")
            st["last_oi_alert_key"] = ""

        # Backfill today's snapshots from store on cold start/restart if needed
        if not _SESSION_SNAPSHOTS:
            try:
                import store
                _SESSION_SNAPSHOTS = store.load_day(today)
            except Exception as e:  # noqa: BLE001
                log(f"oi_alert: snapshot load warning: {e}")
                _SESSION_SNAPSHOTS = []

        # Ensure current snapshot is present in _SESSION_SNAPSHOTS
        if not _SESSION_SNAPSHOTS or _SESSION_SNAPSHOTS[-1][0] != now:
            _SESSION_SNAPSHOTS.append((now, float(spot), df.copy()))

        # Filter strictly for market hours
        import oi_timeline
        mkt_snaps = oi_timeline.filter_market_hours(_SESSION_SNAPSHOTS)
        if len(mkt_snaps) < 2:
            return

        # Load alert rules from strategy1_rules.json
        rules_path = Path(__file__).parent / "strategy1_rules.json"
        rules_data = {}
        if rules_path.exists():
            try:
                rules_data = json.loads(rules_path.read_text())
            except Exception as e:  # noqa: BLE001
                log(f"oi_alert: could not read rules JSON: {e}")

        if _is_session_data_gap(now, rules_data):
            return

        alert_cfg = rules_data.get("oi_alerts", {})
        if not alert_cfg.get("enabled", True):
            return

        warmup_str = alert_cfg.get("warmup_until", "09:50")
        eod_str = alert_cfg.get("eod_no_trade_from", alert_cfg.get("eod_from", rules_data.get("eod_no_trade_from", "15:00")))
        freeze_start_str = alert_cfg.get("freeze_start", "11:30")
        freeze_end_str = alert_cfg.get("freeze_end", "13:15")
        step = float(alert_cfg.get("step", rules_data.get("oi_timeline", {}).get("default_strike_step", 100.0)))
        allowed_transitions = alert_cfg.get("allowed_transitions", [
            ["BALANCED", "PE_DOMINANT"],
            ["BALANCED", "CE_DOMINANT"],
            ["*", "CROSSOVER"]
        ])

        warmup_t = datetime.strptime(warmup_str, "%H:%M").time()
        eod_t = datetime.strptime(eod_str, "%H:%M").time()
        freeze_start_t = datetime.strptime(freeze_start_str, "%H:%M").time()
        freeze_end_t = datetime.strptime(freeze_end_str, "%H:%M").time()
        now_t = now.time()

        # Hard silence outside active window (WARMUP & EOD_UNWIND)
        if now_t < warmup_t or now_t >= eod_t:
            st["last_oi_dominance"] = "WARMUP" if now_t < warmup_t else "EOD_UNWIND"
            return

        # Check Expiry Day unwind suppression (after 14:00 on expiry day)
        exp_val = getattr(df, "attrs", {}).get("expiry") if hasattr(df, "attrs") else None
        if not exp_val and "expiry" in df.columns:
            valid_exp = df["expiry"].dropna()
            if not valid_exp.empty:
                exp_val = str(valid_exp.iloc[0])
        exp_date = None
        if exp_val:
            for fmt in ("%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d"):
                try:
                    exp_date = datetime.strptime(str(exp_val).strip(), fmt).date()
                    break
                except ValueError:
                    pass
        is_expiry_day = (exp_date is not None and today == exp_date)
        unwind_str = rules_data.get("expiry_mode", {}).get("unwind_cutoff_time", "14:00")
        try:
            unwind_t = datetime.strptime(unwind_str, "%H:%M").time()
        except Exception:
            unwind_t = dtime(14, 0)

        if is_expiry_day and now_t >= unwind_t:
            st["last_oi_dominance"] = "EXPIRY UNWIND"
            log(f"oi_alert: Expiry unwind active after {unwind_str} — OI dominance alert suppressed")
            return

        # Resolve ATM strike
        atm_strike = round(spot / step) * step
        if "strike" in df.columns:
            avail = sorted(df["strike"].unique())
            if atm_strike not in avail and avail:
                atm_strike = min(avail, key=lambda s: abs(s - spot))

        oi_rules = rules_data.get("oi_timeline", {})
        tl = oi_timeline.evaluate_strike_timeline(
            mkt_snaps=mkt_snaps,
            strike=atm_strike,
            sess_date=today,
            rules=oi_rules,
        )

        if not tl or not tl.series or len(tl.series) < 2:
            return

        prev_state = tl.series[-2].dominance
        new_state = tl.series[-1].dominance

        # Always record latest dominance in state for dashboard/status
        st["last_oi_dominance"] = new_state

        # Check if state actually changed on this bar
        if prev_state == new_state:
            return

        # Check if transition is in the allowed alert list
        transition_matches = False
        for p, n in allowed_transitions:
            if (p == "*" or p == prev_state) and (n == "*" or n == new_state):
                transition_matches = True
                break

        if not transition_matches:
            return

        # Check deduplication key (alert only ONCE per state change)
        alert_key = f"{int(atm_strike)}_{new_state}"
        if alert_key == st.get("last_oi_alert_key"):
            return

        st["last_oi_alert_key"] = alert_key

        # Buyer freeze window suppression (11:30 - 13:15)
        # Suppress notification, alert_key already recorded so it does not fire upon freeze exit
        if freeze_start_t <= now_t <= freeze_end_t:
            log(f"oi_alert: transition {prev_state} → {new_state} triggered at {now_t:%H:%M} but SUPPRESSED (inside buyer freeze window {freeze_start_str}-{freeze_end_str})")
            return

        # Construct alert payload
        latest = tl.latest_stats or tl.series[-1]
        spread_val = getattr(latest, "spread", 0.0)
        pcr_val = getattr(latest, "pcr", 0.0)
        spot_val = getattr(latest, "current_spot", spot) or spot
        clock_str = getattr(latest, "clock_time", None) or now.strftime("%H:%M")

        msg = (
            f"⚡ OI SHIFT | {int(atm_strike)} ATM | {new_state} | "
            f"PCR {pcr_val:.1f} | Spread {_format_spread(spread_val)} | "
            f"Spot {int(round(spot_val))} | {clock_str}"
        )

        st["last_oi_alert_msg"] = msg
        st["last_oi_alert_time"] = now.strftime("%H:%M:%S")

        log(f"oi_alert: {prev_state} → {new_state} -> sending Telegram alert: {msg}")
        send_telegram_alert(msg)

    except Exception as exc:  # noqa: BLE001
        log(f"oi_alert warning: {exc}")


def check_institutional_barrier_alert(df: pd.DataFrame, spot: float, now: datetime, st: dict) -> None:
    """
    Alerts when NIFTY spot approaches the single highest OI institutional wall
    (Solid Most Resistance CE or Solid Most Support PE) where institutions are defending.
    Rules:
      - Spot within approach_pts (default 25 pts) of the barrier strike
      - Strike total OI >= min_wall_contracts (default 50,000)
      - Suppressed during WARMUP (<09:50) and EOD_UNWIND (>=15:00)
      - Suppressed during buyer freeze window (11:30-13:15)
      - Cooldown per strike (default 60 mins) to prevent spamming
      - Log & degrade on any error
    """
    try:
        if df is None or getattr(df, "empty", True) or spot <= 0:
            return

        rules_path = Path(__file__).parent / "strategy1_rules.json"
        rules_data = {}
        if rules_path.exists():
            try:
                rules_data = json.loads(rules_path.read_text())
            except Exception as e:  # noqa: BLE001
                log(f"barrier_alert: could not read rules JSON: {e}")

        cfg = rules_data.get("institutional_barrier_alerts", {})
        if not cfg.get("enabled", True):
            return

        warmup_t = datetime.strptime(cfg.get("warmup_until", "09:50"), "%H:%M").time()
        eod_str = cfg.get("eod_no_trade_from", cfg.get("eod_from", rules_data.get("eod_no_trade_from", "15:00")))
        eod_t = datetime.strptime(eod_str, "%H:%M").time()
        freeze_start_t = datetime.strptime(cfg.get("freeze_start", "11:30"), "%H:%M").time()
        freeze_end_t = datetime.strptime(cfg.get("freeze_end", "13:15"), "%H:%M").time()
        approach_pts = float(cfg.get("approach_pts", 25.0))
        min_wall = float(cfg.get("min_wall_contracts", 50000.0))
        cooldown_sec = int(cfg.get("cooldown_minutes", 60)) * 60

        now_t = now.time()
        if now_t < warmup_t or now_t >= eod_t:
            return

        clock_str = now.strftime("%H:%M")

        # 1. Solid Most Resistance (Max CE OI at or above spot)
        calls_above = df[df["strike"] >= spot]
        if not calls_above.empty and "ce_oi" in calls_above.columns:
            res_row = calls_above.loc[calls_above["ce_oi"].idxmax()]
            res_strike = float(res_row["strike"])
            res_ce_oi = float(res_row.get("ce_oi", 0) or 0)
            res_ce_chg = float(res_row.get("ce_chg_oi", 0) or 0)
            dist_res = res_strike - spot

            if 0.0 <= dist_res <= approach_pts and res_ce_oi >= min_wall:
                cd_key = f"barrier_res_time_{int(res_strike)}"
                last_time = st.get(cd_key, 0)
                if (now.timestamp() - last_time) >= cooldown_sec:
                    st[cd_key] = now.timestamp()
                    if freeze_start_t <= now_t <= freeze_end_t:
                        log(f"barrier_alert: Approaching Resistance {int(res_strike)} CE suppressed in freeze window")
                    else:
                        chg_str = f" ({_format_spread(res_ce_chg)} today)" if res_ce_chg != 0 else ""
                        msg = (
                            f"🧱 INSTITUTIONAL BARRIER | Approaching Solid Resistance {int(res_strike)} CE | "
                            f"Spot {int(round(spot))} ({int(round(dist_res))} pts away) | "
                            f"{_format_spread(res_ce_oi)} CE OI{chg_str} | Writers Defending | {clock_str}"
                        )
                        st["last_barrier_alert_msg"] = msg
                        st["last_barrier_alert_time"] = now.strftime("%H:%M:%S")
                        log(f"barrier_alert: {msg}")
                        send_telegram_alert(msg)

        # 2. Solid Most Support (Max PE OI at or below spot)
        puts_below = df[df["strike"] <= spot]
        if not puts_below.empty and "pe_oi" in puts_below.columns:
            sup_row = puts_below.loc[puts_below["pe_oi"].idxmax()]
            sup_strike = float(sup_row["strike"])
            sup_pe_oi = float(sup_row.get("pe_oi", 0) or 0)
            sup_pe_chg = float(sup_row.get("pe_chg_oi", 0) or 0)
            dist_sup = spot - sup_strike

            if 0.0 <= dist_sup <= approach_pts and sup_pe_oi >= min_wall:
                cd_key = f"barrier_sup_time_{int(sup_strike)}"
                last_time = st.get(cd_key, 0)
                if (now.timestamp() - last_time) >= cooldown_sec:
                    st[cd_key] = now.timestamp()
                    if freeze_start_t <= now_t <= freeze_end_t:
                        log(f"barrier_alert: Approaching Support {int(sup_strike)} PE suppressed in freeze window")
                    else:
                        chg_str = f" ({_format_spread(sup_pe_chg)} today)" if sup_pe_chg != 0 else ""
                        msg = (
                            f"🧱 INSTITUTIONAL BARRIER | Approaching Solid Support {int(sup_strike)} PE | "
                            f"Spot {int(round(spot))} ({int(round(dist_sup))} pts away) | "
                            f"{_format_spread(sup_pe_oi)} PE OI{chg_str} | Writers Defending | {clock_str}"
                        )
                        st["last_barrier_alert_msg"] = msg
                        st["last_barrier_alert_time"] = now.strftime("%H:%M:%S")
                        log(f"barrier_alert: {msg}")
                        send_telegram_alert(msg)

    except Exception as exc:  # noqa: BLE001
        log(f"barrier_alert warning: {exc}")


def check_trend_higher_lows(snaps: list, now: datetime) -> bool:
    """
    Check 2 — Trend Filter:
    Only fire if the last 3 snapshots (9 minutes) show spot making HIGHER lows, not lower lows.
    Implementation:
      Get spot prices at t-9min, t-6min, t-3min.
      If spot[t-3] > spot[t-6] > spot[t-9]:
          trend is turning UP -> ALLOW alert
      Else:
          downtrend intact -> SUPPRESS alert
    """
    if not snaps or len(snaps) < 3:
        return False

    def get_spot_at_offset(target_offset_min: float) -> Optional[float]:
        target_t = now - timedelta(minutes=target_offset_min)
        candidates = [s for s in snaps if s[0] <= target_t]
        if not candidates:
            return None
        if (target_t - candidates[-1][0]).total_seconds() > 180:
            return None
        return candidates[-1][1]

    sp_9m = get_spot_at_offset(9.0)
    sp_6m = get_spot_at_offset(6.0)
    sp_3m = get_spot_at_offset(3.0)

    if sp_9m is None or sp_6m is None or sp_3m is None:
        if len(snaps) >= 4:
            sp_3m = snaps[-2][1]
            sp_6m = snaps[-3][1]
            sp_9m = snaps[-4][1]
        else:
            return False

    return bool(sp_3m > sp_6m > sp_9m)


def check_liquidity_trap_alert(df: pd.DataFrame, spot: float, now: datetime, st: dict) -> None:
    """
    Alerts when a key institutional reference level (PDH, PDL, Session High, Session Low,
    or Solid Institutional Wall) is swept by a wick and reclaimed back inside the range.
    Rules:
      - Sweep penetration between sweep_min_pts (default 2 pts) and sweep_max_pts (default 25 pts)
      - Reclaim confirmed by current spot back inside level by at least reclaim_pts
      - For Bullish Liquidity Traps (false breakdown):
          * CHECK 1: Bounce strength >= min_false_breakdown_reclaim_pts (default 15 pts) within 2 snapshots (6 min)
          * CHECK 2: Trend filter: Higher lows over last 3 snapshots (9 min)
      - Evaluated over recent snapshot window (lookback_snapshots, default 15)
      - Suppressed during WARMUP (<09:50) and EOD_UNWIND (>=15:00)
      - Suppressed during buyer freeze window (11:30-13:15)
      - Cooldown per level (default 30 mins) to prevent spamming
      - Log & degrade on any error
    """
    global _SESSION_SNAPSHOTS
    try:
        if df is None or getattr(df, "empty", True) or spot <= 0 or not _SESSION_SNAPSHOTS:
            return

        rules_path = Path(__file__).parent / "strategy1_rules.json"
        rules_data = {}
        if rules_path.exists():
            try:
                rules_data = json.loads(rules_path.read_text())
            except Exception as e:  # noqa: BLE001
                log(f"trap_alert: could not read rules JSON: {e}")

        cfg = rules_data.get("liquidity_trap_alerts", {})
        if not cfg.get("enabled", True):
            return

        warmup_t = datetime.strptime(cfg.get("warmup_until", "09:50"), "%H:%M").time()
        eod_str = cfg.get("eod_no_trade_from", cfg.get("eod_from", rules_data.get("eod_no_trade_from", "15:00")))
        eod_t = datetime.strptime(eod_str, "%H:%M").time()
        freeze_start_t = datetime.strptime(cfg.get("freeze_start", "11:30"), "%H:%M").time()
        freeze_end_t = datetime.strptime(cfg.get("freeze_end", "13:15"), "%H:%M").time()
        sweep_min = float(cfg.get("sweep_min_pts", 2.0))
        sweep_max = float(cfg.get("sweep_max_pts", 25.0))
        reclaim_pts = float(cfg.get("reclaim_pts", 1.5))
        min_reclaim_pts = float(
            rules_data.get("alerts", {}).get(
                "min_false_breakdown_reclaim_pts",
                cfg.get("min_false_breakdown_reclaim_pts", 15.0)
            )
        )
        lookback = int(cfg.get("lookback_snapshots", 15))
        cooldown_sec = int(cfg.get("cooldown_minutes", 30)) * 60

        now_t = now.time()
        if now_t < warmup_t or now_t >= eod_t:
            return

        if _is_session_data_gap(now, rules_data):
            return

        # Need at least 2 snapshots in session to evaluate a sweep
        if len(_SESSION_SNAPSHOTS) < 2:
            return

        recent_snaps = _SESSION_SNAPSHOTS[-lookback:]
        recent_spots = [s[1] for s in recent_snaps]
        win_high = max(recent_spots)
        win_low = min(recent_spots)
        clock_str = now.strftime("%H:%M")

        # 1. Reference levels from liquidity_sweep_engine
        import liquidity_sweep_engine as lse
        ref_levels = lse.extract_key_liquidity_levels(spot=spot)

        # 2. Add Solid Institutional Wall strikes as candidates
        calls_above = df[df["strike"] >= spot]
        if not calls_above.empty and "ce_oi" in calls_above.columns:
            res_row = calls_above.loc[calls_above["ce_oi"].idxmax()]
            res_strike = float(res_row["strike"])
            ref_levels.append(lse.LiquidityLevelItem(
                name=f"{int(res_strike)} CE Wall",
                price=res_strike,
                level_type="BUY_SIDE_LIQUIDITY",
                distance_pts=res_strike - spot,
                status="INTACT",
                badge_color="#ef4444"
            ))

        puts_below = df[df["strike"] <= spot]
        if not puts_below.empty and "pe_oi" in puts_below.columns:
            sup_row = puts_below.loc[puts_below["pe_oi"].idxmax()]
            sup_strike = float(sup_row["strike"])
            ref_levels.append(lse.LiquidityLevelItem(
                name=f"{int(sup_strike)} PE Wall",
                price=sup_strike,
                level_type="SELL_SIDE_LIQUIDITY",
                distance_pts=spot - sup_strike,
                status="INTACT",
                badge_color="#10b981"
            ))

        # De-duplicate candidate levels if they are at the same price (within 5 pts)
        deduped_levels = []
        for lvl in ref_levels:
            match = next((x for x in deduped_levels if abs(x.price - lvl.price) <= 5.0 and x.level_type == lvl.level_type), None)
            if match:
                if lvl.name not in match.name:
                    match.name = f"{match.name} / {lvl.name}"
            else:
                deduped_levels.append(lvl)

        for lvl in deduped_levels:
            lvl_pr = lvl.price
            if lvl_pr <= 0:
                continue

            # Case A: Bearish Liquidity Trap (Buy Stops Swept at Resistance)
            if "BUY" in lvl.level_type:
                sweep_pts = win_high - lvl_pr
                if sweep_min <= sweep_pts <= sweep_max and spot <= (lvl_pr - reclaim_pts):
                    cd_key = f"trap_alert_BUY_{int(round(lvl_pr))}"
                    last_time = st.get(cd_key, 0)
                    if (now.timestamp() - last_time) >= cooldown_sec:
                        st[cd_key] = now.timestamp()
                        if freeze_start_t <= now_t <= freeze_end_t:
                            log(f"trap_alert: Bearish trap at {lvl.name} suppressed in freeze window")
                        else:
                            msg = (
                                f"🪤 LIQUIDITY TRAP | NIFTY {int(round(spot))} | "
                                f"{lvl.name} Swept ({int(round(lvl_pr))} → High {int(round(win_high))} → Reclaim {int(round(spot))}) | "
                                f"Breakout Buyers Trapped | Bias: FADE (PUT) | {clock_str}"
                            )
                            st["last_trap_alert_msg"] = msg
                            st["last_trap_alert_time"] = now.strftime("%H:%M:%S")
                            log(f"trap_alert: {msg}")
                            send_telegram_alert(msg)
                            return  # Max 1 trap alert per cycle

            # Case B: Bullish Liquidity Trap (Sell Stops Swept at Support / False Breakdown)
            elif "SELL" in lvl.level_type:
                # CHECK 1 — BOUNCE STRENGTH:
                # Only fire if the reclaim moves >= 15 points above the swept level within 2 snapshots (6 minutes).
                sweep_window = [s for s in _SESSION_SNAPSHOTS if 0 <= (now - s[0]).total_seconds() <= 360]
                if len(sweep_window) < 3 and len(_SESSION_SNAPSHOTS) >= 3:
                    sweep_window = _SESSION_SNAPSHOTS[-3:]
                sweep_low = min(s[1] for s in sweep_window)
                sweep_pts = lvl_pr - sweep_low

                if sweep_min <= sweep_pts <= sweep_max and spot >= (lvl_pr + min_reclaim_pts):
                    # CHECK 2 — TREND FILTER:
                    # Only fire if the last 3 snapshots (9 minutes) show spot making HIGHER lows, not lower lows.
                    if not check_trend_higher_lows(_SESSION_SNAPSHOTS, now):
                        log(f"trap_alert: Bullish trap at {lvl.name} suppressed by trend filter (downtrend intact)")
                        continue

                    cd_key = f"trap_alert_SELL_{int(round(lvl_pr))}"
                    last_time = st.get(cd_key, 0)
                    if (now.timestamp() - last_time) >= cooldown_sec:
                        st[cd_key] = now.timestamp()
                        if freeze_start_t <= now_t <= freeze_end_t:
                            log(f"trap_alert: Bullish trap at {lvl.name} suppressed in freeze window")
                        else:
                            msg = (
                                f"🪤 LIQUIDITY TRAP | NIFTY {int(round(spot))} | "
                                f"{lvl.name} Swept ({int(round(lvl_pr))} → Low {int(round(sweep_low))} → Reclaim {int(round(spot))}) | "
                                f"Breakout Sellers Trapped | Bias: FADE (CALL) | {clock_str}"
                            )
                            st["last_trap_alert_msg"] = msg
                            st["last_trap_alert_time"] = now.strftime("%H:%M:%S")
                            log(f"trap_alert: {msg}")
                            send_telegram_alert(msg)
                            return  # Max 1 trap alert per cycle

    except Exception as exc:  # noqa: BLE001
        log(f"trap_alert warning: {exc}")


def check_expiry_pcr_squeeze_alert(
    df: pd.DataFrame,
    spot: float,
    now: datetime,
    st: dict,
    expiry: Optional[str] = None,
) -> None:
    """
    Checks for rapid PCR swings (> 1.0) in a single 3-minute poll on expiry day.
    Dispatches Telegram warning if threshold is breached.
    """
    try:
        if df is None or getattr(df, "empty", True) or spot <= 0:
            return

        today = now.date()
        exp_val = expiry or (getattr(df, "attrs", {}).get("expiry") if hasattr(df, "attrs") else None)
        if not exp_val and "expiry" in df.columns:
            valid_exp = df["expiry"].dropna()
            if not valid_exp.empty:
                exp_val = str(valid_exp.iloc[0])

        exp_date = None
        if exp_val:
            for fmt in ("%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d"):
                try:
                    exp_date = datetime.strptime(str(exp_val).strip(), fmt).date()
                    break
                except ValueError:
                    pass

        is_expiry_day = (exp_date is not None and today == exp_date)
        if not is_expiry_day:
            return

        # ATM strike calculation
        step = 50.0
        atm_strike = round(spot / step) * step
        if "strike" in df.columns:
            avail = sorted(df["strike"].unique())
            if atm_strike not in avail and avail:
                atm_strike = min(avail, key=lambda s: abs(s - spot))

        atm_rows = df[df["strike"] == atm_strike]
        if atm_rows.empty:
            return

        ce_oi = float(atm_rows["ce_oi"].iloc[0] or 0)
        pe_oi = float(atm_rows["pe_oi"].iloc[0] or 0)
        if ce_oi <= 0:
            return
        curr_pcr = pe_oi / ce_oi

        # Load threshold from strategy1_rules.json
        rules_path = Path(__file__).parent / "strategy1_rules.json"
        pcr_swing_threshold = 1.0
        if rules_path.exists():
            try:
                r_data = json.loads(rules_path.read_text())
                pcr_swing_threshold = float(r_data.get("expiry_mode", {}).get("pcr_swing_threshold", 1.0))
                eod_str = r_data.get("eod_no_trade_from", r_data.get("expiry_mode", {}).get("eod_no_trade_from", "15:00"))
                eod_t = datetime.strptime(eod_str, "%H:%M").time()
                if now.time() >= eod_t:
                    return
            except Exception:
                if now.time() >= dtime(15, 0):
                    return

        last_pcr = st.get("last_expiry_pcr")
        last_pcr_date = st.get("last_expiry_pcr_date")

        if last_pcr is not None and last_pcr_date == today.isoformat():
            pcr_diff = curr_pcr - float(last_pcr)
            if abs(pcr_diff) >= pcr_swing_threshold:
                msg = (
                    f"🌀 EXPIRY SQUEEZE\n"
                    f"PCR jumped from {float(last_pcr):.2f} to {curr_pcr:.2f} in 3 minutes\n"
                    f"Writers covering — volatile moves possible\n"
                    f"➡️ Do NOT add positions — wait for clarity"
                )
                log(f"expiry_squeeze alert: PCR {float(last_pcr):.2f} -> {curr_pcr:.2f} (diff {pcr_diff:+.2f}). Sending alert.")
                send_telegram_alert(msg)
                st["last_pcr_squeeze_msg"] = msg
                st["last_pcr_squeeze_time"] = now.strftime("%H:%M:%S")

        st["last_expiry_pcr"] = curr_pcr
        st["last_expiry_pcr_date"] = today.isoformat()
    except Exception as exc:  # noqa: BLE001
        log(f"check_expiry_pcr_squeeze_alert warning: {exc}")


def check_rvol_alerts(now: datetime, st: dict) -> None:
    """
    Monitors intraday RVOL and dispatches Telegram alerts on:
      1. Unusual open volume: RVOL >= 3.0 at 09:30 ("Unusual open volume — potential trend day").
      2. Regime change: CHOP -> NORMAL (signals active again).
      3. Regime change: NORMAL -> TREND (switch to trend-day rules).
    """
    try:
        import rvol_gate
        cfg = rvol_gate.load_rvol_config()
        open_spike_thresh = float(cfg.get("open_spike_rvol", 3.0))

        now_d = now.date()
        now_t = now.time()

        # 1. Fire alert if RVOL >= 3.0 at 09:30
        if dtime(9, 30) <= now_t <= dtime(9, 35):
            alert_open_key = f"{now_d}_open_spike"
            if st.get("last_rvol_open_alert_key") != alert_open_key:
                val_0930 = rvol_gate.compute_rvol(now_d, dtime(9, 30))
                if val_0930 is not None and val_0930 >= open_spike_thresh:
                    st["last_rvol_open_alert_key"] = alert_open_key
                    msg = (
                        f"🚨 *UNUSUAL OPEN VOLUME DETECTED (09:30)*\n\n"
                        f"📊 Live RVOL: *{val_0930:.2f}×* (≥ {open_spike_thresh:.1f}× threshold)\n"
                        f"⚡ *Signal:* Unusual open volume — potential gap-driven trend day in progress.\n"
                        f"🎯 *Playbook:* Hold trades to full target; do not exit early at first wall."
                    )
                    st["last_rvol_alert_msg"] = msg
                    st["last_rvol_alert_time"] = now.strftime("%H:%M:%S")
                    log(f"rvol_alert: {msg}")
                    send_telegram_alert(msg)

        # 2. Fire alert on regime change: CHOP -> NORMAL, NORMAL -> TREND
        val_now = rvol_gate.compute_rvol(now_d, now_t)
        if val_now is not None:
            curr_regime = rvol_gate.get_rvol_regime(val_now)
            prev_regime = st.get("last_rvol_regime", "")

            if prev_regime and prev_regime != curr_regime:
                alert_shift_key = f"{now_d}_{prev_regime}_to_{curr_regime}"
                if st.get("last_rvol_shift_key") != alert_shift_key:
                    st["last_rvol_shift_key"] = alert_shift_key

                    if prev_regime in ("DEAD_ZONE", "TRAP_ZONE", "CHOP_ZONE") and curr_regime in ("TREND_ZONE", "NORMAL", "TREND_DAY"):
                        msg = (
                            f"🟢 *RVOL REGIME SHIFT: {prev_regime} → TREND ZONE*\n\n"
                            f"📊 Live RVOL: *{val_now:.2f}×* (crossed above 1.30 threshold)\n"
                            f"✅ *Action:* Signals active! Breakout and directional entries unlocked.\n"
                            f"⏰ Time: {now.strftime('%H:%M:%S')}"
                        )
                        st["last_rvol_alert_msg"] = msg
                        st["last_rvol_alert_time"] = now.strftime("%H:%M:%S")
                        log(f"rvol_alert: {msg}")
                        send_telegram_alert(msg)
                    elif curr_regime == "EXTREME_ZONE" and prev_regime != "EXTREME_ZONE":
                        msg = (
                            f"⚡ *RVOL REGIME SHIFT: {prev_regime} → EXTREME ZONE*\n\n"
                            f"📊 Live RVOL: *{val_now:.2f}×* (crossed above 2.50 extreme benchmark)\n"
                            f"⚠️ *Action:* Extreme volume expansion. Trend strong but watch for exhaustion reversals.\n"
                            f"🎯 *Playbook:* Trail stops aggressively; do not chase extended tops/bottoms."
                        )
                        st["last_rvol_alert_msg"] = msg
                        st["last_rvol_alert_time"] = now.strftime("%H:%M:%S")
                        log(f"rvol_alert: {msg}")
                        send_telegram_alert(msg)

            st["last_rvol_regime"] = curr_regime
    except Exception as exc:  # noqa: BLE001
        log(f"check_rvol_alerts warning: {exc}")


def log_floor_ceiling_observation(
    now: datetime,
    spot: float,
    tag: str,
    summary_line: str,
    action_text: str = "WATCH LEVEL",
    obs_dir: Optional[Path] = None,
) -> None:
    """
    Logs FLOOR_TEST or CEILING_TEST alert inline to data/observations/YYYY-MM-DD.md.
    If the 3-minute entry for now (## HH:MM) already exists, inserts the alert line
    inline into that entry before '> Note:' or '---'.
    If the entry does not exist yet, appends a new observation chunk.
    """
    try:
        rules_path = Path(__file__).parent / "strategy1_rules.json"
        rules_data = {}
        if rules_path.exists():
            try:
                rules_data = json.loads(rules_path.read_text())
            except Exception as e:  # noqa: BLE001
                log(f"log_observation: could not load rules: {e}")

        if obs_dir is None:
            obs_dir = Path(rules_data.get("observations", {}).get("dir", "data/observations"))
            if not obs_dir.is_absolute():
                obs_dir = Path(__file__).parent / obs_dir
        obs_dir.mkdir(parents=True, exist_ok=True)

        date_str = now.strftime("%Y-%m-%d")
        file_path = obs_dir / f"{date_str}.md"
        if not file_path.exists():
            file_path.write_text(f"# NIFTY Daily Observation Log — {date_str}\n\n", encoding="utf-8")

        content = file_path.read_text(encoding="utf-8")
        time_str = now.strftime("%H:%M")
        alert_entry_line = f"- Alert: [{tag}] {summary_line}"

        # Prevent duplicate alert logging for same minute & tag
        if f"## {time_str} " in content:
            parts = content.split(f"## {time_str} ", 1)
            after_header = parts[1]
            separator = "\n---"
            if separator in after_header:
                entry_body, rest = after_header.split(separator, 1)
                after_sep = separator + rest
            else:
                entry_body = after_header
                after_sep = ""

            if tag in entry_body:
                return  # already logged in this entry

            if "> Note:" in entry_body:
                updated_body = entry_body.replace("> Note:", f"{alert_entry_line}\n> Note:")
            else:
                updated_body = entry_body.rstrip() + f"\n{alert_entry_line}\n"

            new_content = parts[0] + f"## {time_str} " + updated_body + after_sep
            file_path.write_text(new_content, encoding="utf-8")
            log(f"obs_log: Added {tag} inline to existing {time_str} observation entry")
        else:
            spot_str = f"{int(round(spot)):,}"
            new_chunk = (
                f"## {time_str} | Spot {spot_str} | {tag}\n"
                f"{alert_entry_line}\n"
                f"- Action: {action_text}\n"
                f"> Note: [chart observation / what price did]\n\n"
                f"---\n\n"
            )
            with open(file_path, "a", encoding="utf-8") as f:
                f.write(new_chunk)
            log(f"obs_log: Appended {tag} observation entry at {time_str}")
    except Exception as exc:  # noqa: BLE001
        log(f"log_floor_ceiling_observation error: {exc}")


def check_cascade_alert(
    floor_weakening_streak: int,
    df: Any,
    spot: float,
    now: datetime,
    st: dict,
) -> None:
    """
    Fires when the floor weakening counter reaches cascade_alerts.min_consecutive.
    Warns that the floor is collapsing and shows next support.
    """
    try:
        rules_path = Path(__file__).parent / "strategy1_rules.json"
        rules_data = json.loads(rules_path.read_text()) if rules_path.exists() else {}
        cfg = rules_data.get("cascade_alerts", {})
        if not cfg.get("enabled", True):
            return

        min_consec = int(cfg.get("min_consecutive", 5))
        cooldown_sec = int(cfg.get("cooldown_minutes", 20)) * 60
        win_start_t = datetime.strptime(cfg.get("window_start", "09:20"), "%H:%M").time()
        win_end_t = datetime.strptime(cfg.get("window_end", "14:45"), "%H:%M").time()
        now_t = now.time()

        if not (win_start_t <= now_t <= win_end_t):
            return
        if _is_session_data_gap(now, rules_data):
            return
        if floor_weakening_streak < min_consec:
            return

        cd_key = "cascade_alert_last"
        if (now.timestamp() - st.get(cd_key, 0)) < cooldown_sec:
            return
        st[cd_key] = now.timestamp()

        # Find current floor and next support
        lot_size = float(rules_data.get("display_lot_size", 65))
        mult = lot_size / 100000.0
        min_wall = float(rules_data.get("floor_ceiling_alerts", {}).get("min_wall_oi_lakhs", 100.0))

        current_floor_str = "?"
        next_floor_str = "?"
        next_floor_oi = 0.0

        if df is not None and not getattr(df, "empty", True) and "pe_oi" in df.columns:
            pe_below = df[df["strike"] <= spot].copy()
            if not pe_below.empty:
                pe_below["oi_l"] = pe_below["pe_oi"] * mult
                major = pe_below[pe_below["oi_l"] >= min_wall].sort_values("strike", ascending=False)
                if not major.empty:
                    current_floor_str = str(int(round(float(major.iloc[0]["strike"]))))
                    if len(major) >= 2:
                        nxt = major.iloc[1]
                        next_floor_str = str(int(round(float(nxt["strike"]))))
                        next_floor_oi = float(nxt["oi_l"])

        msg = (
            f"🚨 FLOOR CASCADE: {current_floor_str} weakening\n"
            f"   {floor_weakening_streak}th consecutive briefing — floor losing defense\n"
            f"   Next support: {next_floor_str} PE [{int(round(next_floor_oi))}L]\n"
            f"   → PE momentum trade if RVOL > 0.85"
        )
        log(f"cascade_alert: {msg}")
        send_telegram_alert(msg)
        log_floor_ceiling_observation(
            now, spot, "FLOOR_CASCADE",
            f"{current_floor_str} weakening {floor_weakening_streak}x → next {next_floor_str}",
            action_text="NO CE BUY — FLOOR COLLAPSING",
        )

    except Exception as exc:
        log(f"check_cascade_alert error: {exc}")


def check_put_writer_defense_alert(
    df: Any,
    prev_df: Any,
    spot: float,
    now: datetime,
    st: dict,
) -> None:
    """
    Fires when put writers aggressively ADD OI near a major PE floor.
    Indicates the floor is being defended — potential bounce setup.
    """
    try:
        rules_path = Path(__file__).parent / "strategy1_rules.json"
        rules_data = json.loads(rules_path.read_text()) if rules_path.exists() else {}
        cfg = rules_data.get("put_writer_defense", {})
        if not cfg.get("enabled", True):
            return

        min_add = int(cfg.get("min_pe_add_contracts", 25000))
        max_dist = float(cfg.get("max_spot_distance_pts", 20.0))
        min_wall = float(cfg.get("min_wall_oi_lakhs", 150.0))
        cooldown_sec = int(cfg.get("cooldown_minutes", 15)) * 60
        win_start_t = datetime.strptime(cfg.get("window_start", "09:20"), "%H:%M").time()
        win_end_t = datetime.strptime(cfg.get("window_end", "14:45"), "%H:%M").time()
        freeze_start_t = datetime.strptime(cfg.get("freeze_start", "11:30"), "%H:%M").time()
        freeze_end_t = datetime.strptime(cfg.get("freeze_end", "13:15"), "%H:%M").time()
        now_t = now.time()

        if not (win_start_t <= now_t <= win_end_t):
            return
        if freeze_start_t <= now_t <= freeze_end_t:
            return
        if _is_session_data_gap(now, rules_data):
            return
        if df is None or getattr(df, "empty", True) or prev_df is None or getattr(prev_df, "empty", True):
            return
        if "pe_oi" not in df.columns or "strike" not in df.columns:
            return

        lot_size = float(rules_data.get("display_lot_size", 65))
        mult = lot_size / 100000.0

        # Check PE walls within max_dist below spot
        pe_near = df[(df["strike"] >= spot - max_dist) & (df["strike"] <= spot)]
        for _, row in pe_near.iterrows():
            stk = float(row["strike"])
            oi_now = float(row.get("pe_oi", 0) or 0)
            oi_l = oi_now * mult

            if oi_l < min_wall:
                continue

            # Find previous OI for this strike
            prev_row = prev_df[prev_df["strike"] == stk]
            if prev_row.empty:
                continue
            oi_prev = float(prev_row.iloc[0].get("pe_oi", 0) or 0)
            # Raw contract change over the lookback window (same unit as the
            # briefing's "Puts adding +40k" line — NOT multiplied by lot size)
            delta = oi_now - oi_prev

            if delta < min_add:
                continue

            level_int = int(round(stk))
            cd_key = f"put_writer_defense_{level_int}"
            if (now.timestamp() - st.get(cd_key, 0)) < cooldown_sec:
                continue
            st[cd_key] = now.timestamp()

            ce_target = int(round(stk + 50))
            msg = (
                f"💪 PUT WRITERS DEFENDING {level_int:,}\n"
                f"   PE adding +{int(delta):,} contracts — wall holding\n"
                f"   {level_int:,} PE [{int(round(oi_l))}L] absorbing sell pressure\n"
                f"   → Watch for bounce candle on 15m chart\n"
                f"   → CE buy if 8 EMA turns up | target {ce_target:,}"
            )
            log(f"put_writer_defense alert: {msg}")
            send_telegram_alert(msg)
            log_floor_ceiling_observation(
                now, spot, "PUT_DEFENSE",
                f"PE +{int(delta/1000)}k at {level_int:,} PE [{int(round(oi_l))}L]",
                action_text="WATCH BOUNCE + 8 EMA UP",
            )
            break  # one alert per poll

    except Exception as exc:
        log(f"check_put_writer_defense_alert error: {exc}")


def check_major_floor_test_alert(
    df: Any,
    spot: float,
    now: datetime,
    st: dict,
) -> None:
    """
    Fires when spot is within 15pts of a MAJOR PE wall (OI >= 200L).
    Uses shorter cooldown (10 min) than normal floor test.
    """
    try:
        rules_path = Path(__file__).parent / "strategy1_rules.json"
        rules_data = json.loads(rules_path.read_text()) if rules_path.exists() else {}
        fc_cfg = rules_data.get("floor_ceiling_alerts", {})
        if not fc_cfg.get("enabled", True):
            return

        major_oi = float(fc_cfg.get("major_wall_oi_lakhs", 200.0))
        prox = float(fc_cfg.get("major_floor_proximity_pts", 15.0))
        cooldown_sec = int(fc_cfg.get("major_floor_cooldown_minutes", 10)) * 60
        win_start_t = datetime.strptime(fc_cfg.get("window_start", "09:20"), "%H:%M").time()
        win_end_t = datetime.strptime(fc_cfg.get("window_end", "14:45"), "%H:%M").time()
        now_t = now.time()

        if not (win_start_t <= now_t <= win_end_t):
            return
        if df is None or getattr(df, "empty", True) or "pe_oi" not in df.columns:
            return

        lot_size = float(rules_data.get("display_lot_size", 65))
        mult = lot_size / 100000.0

        rearm_pts = float(fc_cfg.get("major_floor_rearm_pts", 25.0))          # OBSFIX-3 once per test
        rearm_sec = int(fc_cfg.get("major_floor_rearm_minutes", 60)) * 60
        for _k in [k for k in list(st.keys()) if str(k).startswith("major_floor_test_armed_")]:
            try:
                if abs(spot - float(str(_k).rsplit("_", 1)[1])) > prox + rearm_pts:
                    st[_k] = True                                         # price left the level → next touch is a new test
            except Exception:
                pass
        pe_near = df[(df["strike"] >= spot - prox) & (df["strike"] <= spot)]
        for _, row in pe_near.iterrows():
            stk = float(row["strike"])
            oi_l = float(row.get("pe_oi", 0) or 0) * mult
            if oi_l < major_oi:
                continue

            dist = spot - stk
            level_int = int(round(stk))
            cd_key = f"major_floor_test_{level_int}"
            arm_key = f"major_floor_test_armed_{level_int}"
            if (now.timestamp() - st.get(cd_key, 0)) < cooldown_sec:
                continue
            if not st.get(arm_key, True) and (now.timestamp() - st.get(cd_key, 0)) < rearm_sec:
                continue                                                  # same test still running — stay quiet
            st[cd_key] = now.timestamp()
            st[arm_key] = False

            msg = (
                f"🔴 MAJOR FLOOR TEST: Spot {int(round(spot)):,} → {level_int:,} PE [{int(round(oi_l))}L]\n"
                f"   Only {int(round(dist))}pts above MAJOR support\n"
                f"   Put writers must defend here\n"
                f"   → Watch for rejection (CE buy) or breakdown (PE buy)"
            )
            log(f"major_floor_test alert: {msg}")
            send_telegram_alert(msg)
            log_floor_ceiling_observation(
                now, spot, "MAJOR_FLOOR_TEST",
                f"Spot {int(round(spot)):,} → {level_int:,} PE [{int(round(oi_l))}L] ({int(round(dist))}pts)",
                action_text="WATCH DEFENSE OR BREAKDOWN",
            )
            break

    except Exception as exc:
        log(f"check_major_floor_test_alert error: {exc}")


def check_wall_test_counter(
    df: Any,
    spot: float,
    now: datetime,
    st: dict,
) -> None:
    """
    Counts tests within proximity_pts (default 15 pts) of a major wall (>=100L).
    Signal rule:
      - 1st test: normal defense expected
      - 2nd test: weakening defense
      - 3rd test:
          * If OI growing: '3rd test — OI expanding (+X L) — HOLDING STRONG'
          * If OI declining: '3rd test — OI unwinding (-X L) — PRE-CASCADE — breakdown risk high'
    Shadow mode only: logs message, zero orders/alerts placed.
    """
    try:
        rules_path = Path(__file__).parent / "strategy1_rules.json"
        rules_data = json.loads(rules_path.read_text()) if rules_path.exists() else {}
        cfg = rules_data.get("wall_test_counter", {})
        prox = float(cfg.get("proximity_pts", 15.0))
        min_wall = float(cfg.get("min_wall_oi_lakhs", 100.0))

        if df is None or getattr(df, "empty", True) or spot <= 0:
            return

        lot_size = float(rules_data.get("display_lot_size", 65))
        mult = lot_size / 100000.0

        history = st.setdefault("wall_test_history", {})

        for side, col in [("floor", "pe_oi"), ("ceiling", "ce_oi")]:
            if col not in df.columns:
                continue
            if side == "floor":
                cand = df[(df["strike"] <= spot) & (df["strike"] >= spot - prox * 2.0)].copy()
            else:
                cand = df[(df["strike"] >= spot) & (df["strike"] <= spot + prox * 2.0)].copy()

            if cand.empty:
                continue

            cand["oi_l"] = cand[col].fillna(0) * mult
            majors = cand[cand["oi_l"] >= min_wall]
            for _, row in majors.iterrows():
                stk = float(row["strike"])
                oi_l = float(row["oi_l"])
                dist = abs(spot - stk)

                wall_rec = history.setdefault(stk, {"side": side, "tests": [], "in_zone": False})
                if dist <= prox:
                    if not wall_rec["in_zone"]:
                        wall_rec["in_zone"] = True
                        wall_rec["tests"].append({"time": now.strftime("%H:%M:%S"), "spot": spot, "oi_l": oi_l})
                        test_num = len(wall_rec["tests"])
                        stk_int = int(round(stk))
                        wall_name = "FLOOR" if side == "floor" else "CEILING"

                        if test_num == 3:
                            initial_oi = wall_rec["tests"][0]["oi_l"]
                            delta_l = oi_l - initial_oi
                            sign_str = f"+{delta_l:.1f}" if delta_l >= 0 else f"{delta_l:.1f}"
                            if delta_l >= 0:
                                status_txt = "HOLDING STRONG"
                                msg = f"🛡️ [SHADOW] WALL TEST COUNTER: {wall_name} {stk_int:,} — 3rd test — OI expanding ({sign_str}L) — {status_txt}"
                            else:
                                status_txt = "PRE-CASCADE — breakdown risk high"
                                msg = f"⚠️ [SHADOW] WALL TEST COUNTER: {wall_name} {stk_int:,} — 3rd test — OI unwinding ({sign_str}L) — {status_txt}"
                            log(msg)
                        else:
                            log(f"[SHADOW] WALL TEST COUNTER: {wall_name} {stk_int:,} — {test_num}th test at spot {spot:,.1f} (OI {oi_l:.1f}L)")
                else:
                    if dist > prox + 5.0:
                        wall_rec["in_zone"] = False
    except Exception as exc:  # noqa: BLE001
        log(f"check_wall_test_counter error: {exc}")


def _snapshot_minutes_ago(now: datetime, minutes: float) -> Any:
    """Return the option-chain df from the session snapshot closest to `minutes` ago."""
    try:
        if len(_SESSION_SNAPSHOTS) < 2:
            return None
        target = now.timestamp() - minutes * 60.0
        best = None
        best_gap = None
        for ts, _sp, sdf in _SESSION_SNAPSHOTS[:-1]:
            gap = abs(ts.timestamp() - target)
            if best_gap is None or gap < best_gap:
                best, best_gap = sdf, gap
        # Reject if nearest snapshot is way off (data gap)
        if best_gap is not None and best_gap > max(120.0, minutes * 60.0):
            return None
        return best
    except Exception:
        return None


def _update_wall_streak(side: str, df: Any, spot: float, now: datetime, st: dict) -> int:
    """
    Tracks how many consecutive intervals (default 3 min = one briefing) the
    nearest major wall has LOST open interest.
      side="floor"   → nearest major PE wall at/below spot (puts leaving)
      side="ceiling" → nearest major CE wall at/above spot (calls leaving)
    Only updates once per `streak_interval_minutes`, so "5 consecutive" means
    ~15 minutes of sustained unwinding — same meaning as the briefing's
    "Floor weakening: Nth consecutive" line.
    """
    key = f"{side}_weakening_streak"
    streak = int(st.get(key, 0))
    try:
        rules_path = Path(__file__).parent / "strategy1_rules.json"
        rules_data = json.loads(rules_path.read_text()) if rules_path.exists() else {}
        cfg = rules_data.get("cascade_alerts", {})
        interval_min = float(cfg.get("streak_interval_minutes", 3))
        min_wall = float(cfg.get("min_wall_oi_lakhs", 100.0))
        lot_size = float(rules_data.get("display_lot_size", 65))
        mult = lot_size / 100000.0

        last_upd = float(st.get(f"{key}_updated", 0))
        if (now.timestamp() - last_upd) < interval_min * 60.0:
            return streak

        if _is_session_data_gap(now, rules_data):
            st[key] = 0
            st[f"{key}_updated"] = now.timestamp()
            return 0

        prev_df = _snapshot_minutes_ago(now, interval_min)
        col = "pe_oi" if side == "floor" else "ce_oi"
        if df is None or getattr(df, "empty", True) or prev_df is None or getattr(prev_df, "empty", True):
            st[key] = 0
            st[f"{key}_updated"] = now.timestamp()
            return 0
        if col not in df.columns or col not in prev_df.columns:
            st[key] = 0
            st[f"{key}_updated"] = now.timestamp()
            return 0

        if side == "floor":
            cand = df[df["strike"] <= spot].copy()
        else:
            cand = df[df["strike"] >= spot].copy()
        if cand.empty:
            st[key], st[f"{key}_updated"] = 0, now.timestamp()
            return 0
        cand["oi_l"] = cand[col].fillna(0) * mult
        major = cand[cand["oi_l"] >= min_wall]
        if major.empty:
            st[key], st[f"{key}_updated"] = 0, now.timestamp()
            return 0
        major = major.sort_values("strike", ascending=(side == "ceiling"))
        wall_stk = float(major.iloc[0]["strike"])
        cur_oi = float(major.iloc[0][col] or 0)
        prev_row = prev_df[prev_df["strike"] == wall_stk]
        if prev_row.empty:
            streak = 0
        else:
            prev_oi = float(prev_row.iloc[0][col] or 0)
            streak = streak + 1 if cur_oi < prev_oi else 0

        st[key] = streak
        st[f"{key}_updated"] = now.timestamp()
        st[f"{side}_wall_strike"] = wall_stk
    except Exception as exc:  # noqa: BLE001
        log(f"_update_wall_streak({side}) error: {exc}")
    return streak


def check_ceiling_breakout_alert(
    ceiling_weakening_streak: int,
    df: Any,
    spot: float,
    now: datetime,
    st: dict,
) -> None:
    """
    Mirror of the floor CASCADE alert for the ceiling.
    Fires when call writers at the nearest major CE wall have been unwinding
    for `ceiling_min_consecutive` consecutive intervals AND spot is rising
    toward (or already pressing) that wall. Resistance crumbling = breakout risk.
    """
    try:
        rules_path = Path(__file__).parent / "strategy1_rules.json"
        rules_data = json.loads(rules_path.read_text()) if rules_path.exists() else {}
        cfg = rules_data.get("cascade_alerts", {})
        if not cfg.get("enabled", True) or not cfg.get("ceiling_enabled", True):
            return

        min_consec = int(cfg.get("ceiling_min_consecutive", 5))
        cooldown_sec = int(cfg.get("ceiling_cooldown_minutes", cfg.get("cooldown_minutes", 20))) * 60
        max_dist = float(cfg.get("ceiling_max_distance_pts", 60.0))
        min_wall = float(cfg.get("min_wall_oi_lakhs", 100.0))
        win_start_t = datetime.strptime(cfg.get("window_start", "09:20"), "%H:%M").time()
        win_end_t = datetime.strptime(cfg.get("window_end", "14:45"), "%H:%M").time()
        now_t = now.time()

        if not (win_start_t <= now_t <= win_end_t):
            return
        if _is_session_data_gap(now, rules_data):
            return
        if ceiling_weakening_streak < min_consec:
            return
        if df is None or getattr(df, "empty", True) or "ce_oi" not in df.columns:
            return

        # Spot must be rising (vs ~3 min ago) — breakout pressure, not a fade
        prev_spot = None
        target = now.timestamp() - 180
        for ts, sp, _d in reversed(_SESSION_SNAPSHOTS[:-1]):
            if ts.timestamp() <= target:
                prev_spot = sp
                break
        if prev_spot is not None and spot <= prev_spot:
            return

        cd_key = "ceiling_breakout_last"
        if (now.timestamp() - st.get(cd_key, 0)) < cooldown_sec:
            return

        lot_size = float(rules_data.get("display_lot_size", 65))
        mult = lot_size / 100000.0
        ce_above = df[df["strike"] >= spot].copy()
        if ce_above.empty:
            return
        ce_above["oi_l"] = ce_above["ce_oi"].fillna(0) * mult
        major = ce_above[ce_above["oi_l"] >= min_wall].sort_values("strike")
        if major.empty:
            return
        cur = major.iloc[0]
        cur_stk = float(cur["strike"])
        if (cur_stk - spot) > max_dist:
            return
        cur_oi_l = float(cur["oi_l"])

        # How much the wall has shrunk over the streak window
        interval_min = float(cfg.get("streak_interval_minutes", 3))
        old_df = _snapshot_minutes_ago(now, interval_min * ceiling_weakening_streak)
        left_k = None
        if old_df is not None and not getattr(old_df, "empty", True) and "ce_oi" in old_df.columns:
            old_row = old_df[old_df["strike"] == cur_stk]
            if not old_row.empty:
                left_k = (float(old_row.iloc[0]["ce_oi"] or 0) - float(cur["ce_oi"] or 0)) / 1000.0

        next_str, next_oi = "?", 0.0
        if len(major) >= 2:
            nxt = major.iloc[1]
            next_str, next_oi = f"{int(round(float(nxt['strike']))):,}", float(nxt["oi_l"])

        st[cd_key] = now.timestamp()
        lvl = int(round(cur_stk))
        left_txt = f"{left_k:.0f}k leaving" if left_k is not None and left_k > 0 else "unwinding"
        msg = (
            f"⚡ CEILING BREAKING: {lvl:,} CE wall weakening\n"
            f"   {ceiling_weakening_streak}th consecutive briefing — resistance crumbling\n"
            f"   Call writers retreating: {left_txt} | {lvl:,} CE [{int(round(cur_oi_l))}L] left\n"
            f"   Next resistance: {next_str} CE [{int(round(next_oi))}L]\n"
            f"   → CE momentum trade if RVOL > 0.85\n"
            f"   → Enter only on 15m close above {lvl:,} + 8 EMA up"
        )
        log(f"ceiling_breakout alert: {msg}")
        send_telegram_alert(msg)
        log_floor_ceiling_observation(
            now, spot, "CEILING_BREAKOUT",
            f"{lvl:,} CE weakening {ceiling_weakening_streak}x → next {next_str}",
            action_text="WATCH CLOSE ABOVE + 8 EMA UP",
        )
    except Exception as exc:  # noqa: BLE001
        log(f"check_ceiling_breakout_alert error: {exc}")


def check_floor_ceiling_approach_alert(
    df: Any,
    spot: float,
    now: datetime,
    st: dict,
) -> None:
    """
    Monitors NIFTY spot approaching major PE floor or CE wall.
    Alerts:
      1. FLOOR TEST:
         - Spot within proximity_pts (default 25 pts) of major PE wall
         - PE wall OI >= min_wall_oi_lakhs (default 100L)
         - Spot approaching FROM ABOVE (falling)
      2. CEILING TEST:
         - Spot within proximity_pts (default 25 pts) of major CE wall
         - CE wall OI >= min_wall_oi_lakhs (default 100L)
         - Spot approaching FROM BELOW (rising)
    Rules:
      - Cooldown: cooldown_minutes (default 30 min) per level
      - Window: window_start (09:20) to window_end (14:45)
      - Suppressed during freeze window: freeze_start (11:30) to freeze_end (13:15)
      - All thresholds loaded from strategy1_rules.json under 'floor_ceiling_alerts'
      - Log to data/observations/YYYY-MM-DD.md with tag FLOOR_TEST or CEILING_TEST
    """
    global _SESSION_SNAPSHOTS
    try:
        if df is None or getattr(df, "empty", True) or spot <= 0:
            return

        rules_path = Path(__file__).parent / "strategy1_rules.json"
        rules_data = {}
        if rules_path.exists():
            try:
                rules_data = json.loads(rules_path.read_text())
            except Exception as e:  # noqa: BLE001
                log(f"floor_ceiling_alert: could not read rules JSON: {e}")

        cfg = rules_data.get("floor_ceiling_alerts", {})
        if not cfg.get("enabled", True):
            return

        win_start_str = cfg.get("window_start", "09:20")
        win_end_str = cfg.get("window_end", "14:45")
        freeze_start_str = cfg.get("freeze_start", "11:30")
        freeze_end_str = cfg.get("freeze_end", "13:15")

        win_start_t = datetime.strptime(win_start_str, "%H:%M").time()
        win_end_t = datetime.strptime(win_end_str, "%H:%M").time()
        freeze_start_t = datetime.strptime(freeze_start_str, "%H:%M").time()
        freeze_end_t = datetime.strptime(freeze_end_str, "%H:%M").time()
        now_t = now.time()

        # Check window 09:20 - 14:45
        if not (win_start_t <= now_t <= win_end_t):
            return

        # Check freeze window 11:30 - 13:15
        if freeze_start_t <= now_t <= freeze_end_t:
            return

        proximity_pts = float(cfg.get("proximity_pts", 25.0))
        min_wall_lakhs = float(cfg.get("min_wall_oi_lakhs", cfg.get("min_wall_lakhs", 100.0)))
        cooldown_sec = int(cfg.get("cooldown_minutes", cfg.get("cooldown", 30))) * 60
        lot_size = float(cfg.get("lot_size", rules_data.get("display_lot_size", 65)))
        mult = lot_size / 100000.0
        target_offset_pts = float(cfg.get("target_offset_pts", 50.0))

        # Check previous spot to determine direction (falling vs rising)
        prev_spot = None
        if len(_SESSION_SNAPSHOTS) >= 2:
            prev_spot = _SESSION_SNAPSHOTS[-2][1]

        # ── 1. FLOOR TEST (spot approaching PE wall from above) ───────────
        if "pe_oi" in df.columns and "strike" in df.columns:
            pe_candidates = df[df["strike"] <= spot]
            if not pe_candidates.empty:
                qualifying_floors = []
                for _, row in pe_candidates.iterrows():
                    stk = float(row["strike"])
                    dist = spot - stk
                    if 0.0 <= dist <= proximity_pts:
                        oi_lakhs = float(row.get("pe_oi", 0) or 0) * mult
                        if oi_lakhs >= min_wall_lakhs:
                            # Approaching from above (falling)
                            if prev_spot is None or (spot <= prev_spot and prev_spot >= stk):
                                qualifying_floors.append((dist, stk, oi_lakhs))

                if qualifying_floors:
                    # Select the closest floor to current spot
                    qualifying_floors.sort(key=lambda x: x[0])
                    _, floor_strike, floor_oi_l = qualifying_floors[0]
                    level_int = int(round(floor_strike))

                    # Cooldown check
                    cd_key = f"floor_test_time_{level_int}"
                    last_time = st.get(cd_key, 0)
                    if (now.timestamp() - last_time) >= cooldown_sec:
                        st[cd_key] = now.timestamp()
                        st.setdefault("alert_cooldowns", {})[f"floor_test_{level_int}"] = now.timestamp()

                        # Find next floor below floor_strike
                        strikes_below = df[df["strike"] < floor_strike]
                        next_floor_strike = floor_strike - target_offset_pts
                        next_floor_oi_l = 0.0
                        if not strikes_below.empty:
                            major_below = strikes_below[strikes_below["pe_oi"] * mult >= min_wall_lakhs]
                            if not major_below.empty:
                                next_row = major_below.sort_values("strike", ascending=False).iloc[0]
                                next_floor_strike = float(next_row["strike"])
                                next_floor_oi_l = float(next_row.get("pe_oi", 0) or 0) * mult
                            else:
                                next_row = strikes_below.loc[strikes_below["pe_oi"].idxmax()]
                                next_floor_strike = float(next_row["strike"])
                                next_floor_oi_l = float(next_row.get("pe_oi", 0) or 0) * mult

                        ce_target = int(round(floor_strike + target_offset_pts))
                        floor_msg = (
                            f"🎯 FLOOR TEST: Spot {int(round(spot)):,} → {level_int:,} PE floor [{int(round(floor_oi_l))}L]\n"
                            f"   Watch for 8 EMA bounce on 15m chart\n"
                            f"   If holds → CE buy, target +50pts ({ce_target:,})\n"
                            f"   If breaks → next floor {int(round(next_floor_strike)):,} PE [{int(round(next_floor_oi_l))}L]"
                        )

                        st["last_floor_test_msg"] = floor_msg
                        st["last_floor_test_time"] = now.strftime("%H:%M:%S")
                        log(f"floor_test alert:\n{floor_msg}")
                        send_telegram_alert(floor_msg)

                        summary = f"Spot {int(round(spot)):,} → {level_int:,} PE floor [{int(round(floor_oi_l))}L]"
                        log_floor_ceiling_observation(now, spot, "FLOOR_TEST", summary, action_text="WATCH 8 EMA BOUNCE")

        # ── 2. CEILING TEST (spot approaching CE wall from below) ─────────
        if "ce_oi" in df.columns and "strike" in df.columns:
            ce_candidates = df[df["strike"] >= spot]
            if not ce_candidates.empty:
                qualifying_ceils = []
                for _, row in ce_candidates.iterrows():
                    stk = float(row["strike"])
                    dist = stk - spot
                    if 0.0 <= dist <= proximity_pts:
                        oi_lakhs = float(row.get("ce_oi", 0) or 0) * mult
                        if oi_lakhs >= min_wall_lakhs:
                            # Approaching from below (rising)
                            if prev_spot is None or (spot >= prev_spot and prev_spot <= stk):
                                qualifying_ceils.append((dist, stk, oi_lakhs))

                if qualifying_ceils:
                    # Select the closest ceiling to current spot
                    qualifying_ceils.sort(key=lambda x: x[0])
                    _, ceil_strike, ceil_oi_l = qualifying_ceils[0]
                    level_int = int(round(ceil_strike))

                    # Cooldown check
                    cd_key = f"ceiling_test_time_{level_int}"
                    last_time = st.get(cd_key, 0)
                    if (now.timestamp() - last_time) >= cooldown_sec:
                        st[cd_key] = now.timestamp()
                        st.setdefault("alert_cooldowns", {})[f"ceiling_test_{level_int}"] = now.timestamp()

                        # Find next ceiling above ceil_strike
                        strikes_above = df[df["strike"] > ceil_strike]
                        next_ceil_strike = ceil_strike + target_offset_pts
                        next_ceil_oi_l = 0.0
                        if not strikes_above.empty:
                            major_above = strikes_above[strikes_above["ce_oi"] * mult >= min_wall_lakhs]
                            if not major_above.empty:
                                next_row = major_above.sort_values("strike", ascending=True).iloc[0]
                                next_ceil_strike = float(next_row["strike"])
                                next_ceil_oi_l = float(next_row.get("ce_oi", 0) or 0) * mult
                            else:
                                next_row = strikes_above.loc[strikes_above["ce_oi"].idxmax()]
                                next_ceil_strike = float(next_row["strike"])
                                next_ceil_oi_l = float(next_row.get("ce_oi", 0) or 0) * mult

                        pe_target = int(round(ceil_strike - target_offset_pts))
                        ceil_msg = (
                            f"🚧 CEILING TEST: Spot {int(round(spot)):,} → {level_int:,} CE wall [{int(round(ceil_oi_l))}L]\n"
                            f"   Watch for rejection or breakout on 15m chart\n"
                            f"   If rejects → PE buy, target -50pts ({pe_target:,})\n"
                            f"   If breaks → CE buy, target {int(round(next_ceil_strike)):,} CE [{int(round(next_ceil_oi_l))}L]"
                        )

                        st["last_ceiling_test_msg"] = ceil_msg
                        st["last_ceiling_test_time"] = now.strftime("%H:%M:%S")
                        log(f"ceiling_test alert:\n{ceil_msg}")
                        send_telegram_alert(ceil_msg)

                        summary = f"Spot {int(round(spot)):,} → {level_int:,} CE wall [{int(round(ceil_oi_l))}L]"
                        log_floor_ceiling_observation(now, spot, "CEILING_TEST", summary, action_text="WATCH REJECTION/BREAKOUT")

    except Exception as exc:  # noqa: BLE001
        log(f"floor_ceiling_alert warning: {exc}")


def check_prior_day_level_alert(spot: float, now: datetime, st: dict) -> None:
    """
    Alerts when spot approaches a prior-session key level (PDH, PDL, PDC, PDO, P2DC).
    All thresholds read from strategy1_rules.json under 'prior_day_levels'.
    Only fires 09:15-14:45, skip freeze 11:30-13:15.
    Cooldown per level (default 20 min).
    Logs to observation file with tag PRIOR_DAY_LEVEL.
    """
    try:
        if spot <= 0:
            return

        rules_path = Path(__file__).resolve().parent / "strategy1_rules.json"
        if not rules_path.exists():
            return
        rules = json.loads(rules_path.read_text(encoding="utf-8"))
        cfg = rules.get("prior_day_levels", {})
        if not cfg.get("enabled", True):
            return

        win_start_str = cfg.get("window_start", "09:15")
        win_end_str = cfg.get("window_end", "14:45")
        freeze_start_str = cfg.get("freeze_start", "11:30")
        freeze_end_str = cfg.get("freeze_end", "13:15")

        now_t = now.time()
        win_start_t = datetime.strptime(win_start_str, "%H:%M").time()
        win_end_t = datetime.strptime(win_end_str, "%H:%M").time()
        freeze_start_t = datetime.strptime(freeze_start_str, "%H:%M").time()
        freeze_end_t = datetime.strptime(freeze_end_str, "%H:%M").time()

        if not (win_start_t <= now_t <= win_end_t):
            return
        if freeze_start_t <= now_t <= freeze_end_t:
            return

        import session_levels
        ohlc_csv_path = cfg.get("ohlc_csv_path", "data/ohlc/OHLC.csv")
        pd_levels = session_levels.load_prior_day_levels(now.date(), ohlc_path=ohlc_csv_path)
        if not pd_levels:
            return

        proximity_pts = float(cfg.get("proximity_pts", 15.0))
        cooldown_sec = int(cfg.get("cooldown_minutes", 20)) * 60

        descriptions = {
            "PDH": "Prior-session high",
            "PDL": "Prior-session low",
            "PDC": "Prior-session close",
            "PDO": "Prior-session open",
            "P2DC": "2-day prior close",
        }

        qualifying = []
        for lvl_name, lvl_val in pd_levels.items():
            if lvl_name in ("date", "P2_date") or not isinstance(lvl_val, (int, float)):
                continue
            if lvl_val <= 0:
                continue
            dist = abs(spot - lvl_val)
            if dist <= proximity_pts:
                qualifying.append((dist, lvl_name, lvl_val))

        if not qualifying:
            return

        qualifying.sort(key=lambda x: x[0])
        dist, lvl_name, lvl_val = qualifying[0]
        desc = descriptions.get(lvl_name, f"Prior-day {lvl_name}")

        cd_key = f"prior_day_level_{lvl_name}"
        last_alert_time = st.setdefault("alert_cooldowns", {}).get(cd_key, 0)
        if (now.timestamp() - last_alert_time) < cooldown_sec:
            return

        st["alert_cooldowns"][cd_key] = now.timestamp()

        if lvl_name == "PDH":
            action_setup = "Rejection = PE setup | Clean break = CE setup"
        elif lvl_name == "PDL":
            action_setup = "Bounce = CE setup | Clean break = PE setup"
        elif spot <= lvl_val:
            action_setup = "Rejection = PE setup | Clean break = CE setup"
        else:
            action_setup = "Bounce = CE setup | Clean breakdown = PE setup"

        dist_int = int(round(dist))
        lvl_int = int(round(lvl_val))
        spot_int = int(round(spot))

        msg = (
            f"📍 PRIOR-DAY LEVEL: Spot {spot_int:,} → {lvl_name} {lvl_int:,} ({dist_int}pts)\n"
            f"   {desc} — watch for rejection or break\n"
            f"   → {action_setup}"
        )

        st["last_prior_day_alert_msg"] = msg
        st["last_prior_day_alert_time"] = now.strftime("%H:%M:%S")
        log(f"prior_day_level alert:\n{msg}")
        send_telegram_alert(msg)

        summary = f"Spot {spot_int:,} → {lvl_name} {lvl_int:,} ({dist_int}pts)"
        log_floor_ceiling_observation(
            now,
            spot,
            "PRIOR_DAY_LEVEL",
            summary,
            action_text=f"WATCH {lvl_name}",
        )
    except Exception as exc:  # noqa: BLE001
        log(f"check_prior_day_level_alert error: {exc}")


def is_in_fast_poll_window(now: datetime, rules: Optional[dict] = None) -> bool:
    """Returns True if current time is within the fast poll window (default 09:15-09:30)."""
    if rules is None:
        try:
            rules_path = Path(__file__).resolve().parent / "strategy1_rules.json"
            if rules_path.exists():
                rules = json.loads(rules_path.read_text(encoding="utf-8"))
            else:
                rules = {}
        except Exception:
            rules = {}
    cfg = rules.get("fast_poll_window", {})
    if cfg.get("enabled", False):
        try:
            start_t = datetime.strptime(cfg.get("start", "09:15"), "%H:%M").time()
            end_t = datetime.strptime(cfg.get("end", "09:30"), "%H:%M").time()
            return start_t <= now.time() < end_t
        except Exception:
            return False
    return False


def get_chain_poll_interval(now: datetime, rules: Optional[dict] = None) -> int:
    """Returns the polling interval in seconds for the chain job (default 60s, or 20s during fast_poll_window)."""
    if rules is None:
        try:
            rules_path = Path(__file__).resolve().parent / "strategy1_rules.json"
            if rules_path.exists():
                rules = json.loads(rules_path.read_text(encoding="utf-8"))
            else:
                rules = {}
        except Exception:
            rules = {}
    cfg = rules.get("fast_poll_window", {})
    if cfg.get("enabled", False):
        try:
            start_t = datetime.strptime(cfg.get("start", "09:15"), "%H:%M").time()
            end_t = datetime.strptime(cfg.get("end", "09:30"), "%H:%M").time()
            if start_t <= now.time() < end_t:
                return int(cfg.get("chain_interval_seconds", cfg.get("interval_seconds", 20)))
        except Exception:
            pass
    return getattr(config, "POLL_SECONDS", 60)


def job_chain(st: dict) -> None:
    """Fetch the chain, score it, append a row to the signal log."""
    global _CONSECUTIVE_CHAIN_FAILURES, _LAST_PAYLOAD_HASH
    try:
        import hashlib
        import chain_parser as cp
        import analytics as an

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

        # Wire up RVOL gate session counters whenever a CE/PE signal occurs (time-normalized to 1 min)
        if sig.action in ("BUY_CE", "BUY_PE"):
            cur_min = now.strftime("%Y-%m-%d %H:%M")
            if st.get("last_rvol_signal_counted_minute") != cur_min:
                st["last_rvol_signal_counted_minute"] = cur_min
                import rvol_gate
                gate_dec = rvol_gate.get_gate_decision(now.date(), now.time())
                if gate_dec.get("status") == "SUPPRESSED":
                    st["rvol_signals_suppressed"] = st.get("rvol_signals_suppressed", 0) + 1
                elif gate_dec.get("status") == "ALLOWED":
                    st["rvol_signals_allowed"] = st.get("rvol_signals_allowed", 0) + 1

        # Single-writer guarantee: Ensure Greeks are attached so ALL snapshots carry Greeks
        if ("ce_gamma" not in df_nearest.columns or df_nearest["ce_gamma"].isna().all()):
            exp_dt_calc = exp_dt if "exp_dt" in locals() and exp_dt is not None else None
            if exp_dt_calc is None and expiry:
                for fmt in ("%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d"):
                    try:
                        exp_dt_calc = datetime.strptime(expiry, fmt).replace(hour=15, minute=30, second=0)
                        break
                    except ValueError:
                        pass
            if exp_dt_calc is not None:
                try:
                    import greeks
                    df_nearest = greeks.add_greeks(df_nearest, spot, expiry=exp_dt_calc, now=now)
                except Exception as g_exc:
                    log(f"scheduler: failed to attach greeks: {g_exc}")

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
        if is_in_fast_poll_window(now):
            st["fast_window_chain_polls"] = st.get("fast_window_chain_polls", 0) + 1
        st["last_chain"] = now.strftime("%H:%M:%S")
        _CONSECUTIVE_CHAIN_FAILURES = 0
        outcome = "SUCCESS_STALE" if is_stale else "SUCCESS"
        record_heartbeat(outcome, spot=spot, drift_minutes=drift_minutes)

        # Autonomous 5-minute candle synchronization (refreshes live candles with true futures volume)
        if "last_candle_sync_min" not in st or (now.minute % 5 == 0 and st.get("last_candle_sync_min") != now.minute):
            st["last_candle_sync_min"] = now.minute
            try:
                import dhan_client
                dhan_client.backfill_5m_candles(days_back=2)
            except Exception as c_exc:
                log(f"scheduler: autonomous candle sync error: {c_exc}")

        # Periodic Observation Log Append & Briefing Telegram Dispatch (every 3 minutes)
        if "last_obs_poll_min" not in st or (now.minute % 3 == 0 and st.get("last_obs_poll_min") != now.minute):
            st["last_obs_poll_min"] = now.minute
            try:
                import agt_briefing
                rules_b = agt_briefing.load_briefing_rules()
                brief_out = agt_briefing.build_briefing(now.date(), now.time(), append_obs=True)
                latest_ts_b = getattr(agt_briefing.build_briefing, "last_snapshot_ts", now)
                tg_b = getattr(agt_briefing.build_briefing, "telegram_briefing", None)
                agt_briefing.dispatch_telegram_briefing(
                    brief_out,
                    rules_b,
                    now.date(),
                    latest_ts_b,
                    tg_briefing=tg_b,
                    freeze_end_alert=getattr(agt_briefing.build_briefing, "freeze_end_alert", None),
                    approach_alert=getattr(agt_briefing.build_briefing, "approach_alert", None),
                    trap_alert=getattr(agt_briefing.build_briefing, "trap_alert", None),
                    eod_summary=getattr(agt_briefing.build_briefing, "eod_summary", None),
                )
            except Exception as b_exc:
                log(f"scheduler: observation / briefing error: {b_exc}")

        # Periodic 5-MIN & 15-MIN Snapshots (clock-aligned, 10s after boundary)
        try:
            import agt_briefing
            snap_tf = agt_briefing.get_snapshot_tf_for_time(now.time())
            if snap_tf and now.second >= 10 and st.get("last_periodic_snap_min") != now.minute:
                st["last_periodic_snap_min"] = now.minute
                rules_snap = agt_briefing.load_briefing_rules()
                snap_res = agt_briefing.build_periodic_snapshot(snap_tf, now.date(), now.time(), append_obs=True)
                if snap_res and snap_res.get("telegram_text"):
                    agt_briefing.dispatch_telegram_snapshot(snap_res, rules_snap, now.date(), snap_res.get("timestamp"))
                    try:
                        sep_emoji = "🟦 5-MIN" if snap_tf == "5m" else "🟪 15-MIN"
                        time_label = now.strftime("%H:%M")
                        sep_header = f"──── {sep_emoji} {time_label} ────"
                        watch_log_file = config.DATA_DIR / "agt_briefing_watch.log"
                        with open(watch_log_file, "a", encoding="utf-8") as wf:
                            wf.write(f"\n{sep_header}\n{snap_res['telegram_text']}\n\n")
                    except Exception as log_exc:
                        log(f"scheduler: error writing snapshot to agt_briefing_watch.log: {log_exc}")
        except Exception as snap_exc:
            log(f"scheduler: periodic snapshot error: {snap_exc}")

        # OI Dominance Alert (Telegram)
        check_oi_dominance_alert(df_nearest, spot, now, st)

        # Institutional Barrier Alert (Solid Support / Resistance Approaching)
        check_institutional_barrier_alert(df_nearest, spot, now, st)

        # Liquidity Trap Alert (Wick Sweep & Reclaim)
        check_liquidity_trap_alert(df_nearest, spot, now, st)

        # Expiry Day Rapid PCR Squeeze Alert (Telegram)
        check_expiry_pcr_squeeze_alert(df_nearest, spot, now, st, expiry=expiry)

        # Relative Volume (RVOL) Gate & Regime Shift Alert (Telegram)
        check_rvol_alerts(now, st)

        # Floor / Ceiling Approach Alert (Telegram & Observations)
        check_floor_ceiling_approach_alert(df_nearest, spot, now, st)

        # Major Floor Test Alert (MAJOR wall >= 200L, shorter cooldown)
        check_major_floor_test_alert(df_nearest, spot, now, st)

        # Wall Test Counter (3rd test OI expansion vs unwinding shadow tracking)
        check_wall_test_counter(df_nearest, spot, now, st)

        # Prior-Day Level Alert (PDH, PDL, PDC, PDO proximity check)
        check_prior_day_level_alert(spot, now, st)

        # Floor Cascade Warning (nearest major PE wall losing OI N+ consecutive intervals)
        _floor_streak = _update_wall_streak("floor", df_nearest, spot, now, st)
        check_cascade_alert(_floor_streak, df_nearest, spot, now, st)

        # Ceiling Breakout Warning (nearest major CE wall losing OI N+ consecutive intervals)
        _ceil_streak = _update_wall_streak("ceiling", df_nearest, spot, now, st)
        check_ceiling_breakout_alert(_ceil_streak, df_nearest, spot, now, st)

        # Put Writer Defense Alert (PE writers adding near floor)
        try:
            _pwd_lookback = float(json.loads((Path(__file__).parent / "strategy1_rules.json").read_text())
                                  .get("put_writer_defense", {}).get("lookback_minutes", 15))
        except Exception:
            _pwd_lookback = 15.0
        _prev_df_for_pwd = _snapshot_minutes_ago(now, _pwd_lookback)
        check_put_writer_defense_alert(df_nearest, _prev_df_for_pwd, spot, now, st)

        log(f"chain  spot {spot:,.2f}  {read.regime}  "
            f"score {read.bias_score:+.1f}  S1 {read.levels.s1} "
            f"R1 {read.levels.r1}  (poll {st['chain_polls']})")
    except Exception as exc:                                # noqa: BLE001
        _CONSECUTIVE_CHAIN_FAILURES += 1
        record_heartbeat("FAILURE", exc=exc)
        exc_class = exc.__class__.__name__
        exc_msg = str(exc)
        is_rate_limit_or_reset = (
            "429" in exc_msg
            or isinstance(exc, ConnectionResetError)
            or "ConnectionResetError" in exc_msg
            or "connection reset" in exc_msg.lower()
        )
        if is_rate_limit_or_reset:
            log(f"chain WARNING: rate limit / connection reset detected ({exc_class}: {exc_msg}). Backing off 10s.")
            _time.sleep(10)
        if _CONSECUTIVE_CHAIN_FAILURES >= 3:
            log(f"chain CRITICAL [{_CONSECUTIVE_CHAIN_FAILURES} consecutive failures] ({exc_class}): {exc_msg}")
        else:
            log(f"chain FAILED (attempt {_CONSECUTIVE_CHAIN_FAILURES}, {exc_class}): {exc_msg}")


def job_ohlc(st: dict, force: bool = False) -> None:
    """
    Append today's completed bar, then rebuild the master levels.

    Refuses before 15:30 on NSE's own clock, so a partial bar cannot get in.
    """
    if st.get("ohlc_done"):
        return

    now = datetime.now()
    cutoff = getattr(config, "MARKET_CLOSE", dtime(15, 30))
    if not force and now.weekday() < 5 and now.time() < cutoff:
        st["ohlc_message"] = f"refused before {cutoff:%H:%M} — active trading session"
        log(f"ohlc   refused before {cutoff:%H:%M}: market active")
        return

    try:
        import market_data as md

        st["ohlc_attempts"] += 1
        ok, msg = False, ""

        # 1. Primary: Pull from DhanHQ (avoids NSE 503 failures)
        try:
            ok, msg = md.backfill_ohlc_dhan(days_back=config.OHLC_BACKFILL_DAYS)
        except Exception as d_exc:
            log(f"scheduler ohlc: dhan backfill error: {d_exc}")

        # 2. Fallback: NSE client
        if not ok:
            try:
                client = get_nse_client()
                ok, msg = md.backfill_ohlc(client, config.OHLC_BACKFILL_DAYS)
            except Exception as nse_exc:
                log(f"scheduler ohlc: nse backfill error: {nse_exc}")

        # 3. Fallback: Local completed candles
        if not ok:
            try:
                import levels
                ldf = levels.load_ohlc()
                if not ldf.empty:
                    ok, msg = True, f"synced from local candles ({len(ldf)} sessions to {ldf['Date'].max():%d-%b-%Y})"
            except Exception as l_exc:
                log(f"scheduler ohlc: candle sync error: {l_exc}")

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
            import rvol_gate
            rvol_gate.log_session_gate_decision(
                date.today(),
                signals_suppressed=st.get("rvol_signals_suppressed", 0),
                signals_allowed=st.get("rvol_signals_allowed", 0),
            )
            log(f"rvol   session gate decision logged to data/rvol_gate_log.jsonl (suppressed: {st.get('rvol_signals_suppressed', 0)}, allowed: {st.get('rvol_signals_allowed', 0)})")
        except Exception as r_exc:
            log(f"rvol gate eod logging failed: {r_exc}")
            raise
        try:
            rules_path = Path(__file__).resolve().parent / "strategy1_rules.json"
            retention = 90
            if rules_path.exists():
                cfg = json.loads(rules_path.read_text(encoding="utf-8"))
                retention = cfg.get("greeks", {}).get("multi_expiry_retention_days", 90)
            pruned = store.prune_multi_snapshots(retention)
            if len(pruned) > 0:
                log(f"retention: pruned {len(pruned)} old multi-expiry snapshot folder(s) (> {retention} days): {pruned}")
        except Exception as p_exc:                          # noqa: BLE001
            log(f"ERROR: retention check failed: {p_exc}")
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
    if st.get("last_oi_dominance"):
        print(f"  oi dominance     {st.get('last_oi_dominance')}"
              + (f"   (alert: {st['last_oi_alert_msg']})" if st.get("last_oi_alert_msg") else ""))
    if st.get("last_barrier_alert_msg"):
        print(f"  barrier alert    {st['last_barrier_alert_msg']}")
    if st.get("last_trap_alert_msg"):
        print(f"  trap alert       {st['last_trap_alert_msg']}")
    if st.get("fast_window_chain_polls"):
        print(f"  fast window polls {st.get('fast_window_chain_polls', 0)} (09:15-09:30)")
    print(f"  rvol signals     suppressed {st.get('rvol_signals_suppressed', 0)} · allowed {st.get('rvol_signals_allowed', 0)}")
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


def reconcile_observation_log(target_date: date) -> dict:
    """
    Self-healing: replays missing 3-minute observation bins for target_date from stored snapshots.
    Reports which bins were recovered and which bins CANNOT be recovered because
    no snapshot exists on disk.

    LIMITATION (Permanent):
    Dhan serves only the CURRENT option chain. There is no historical intraday OI endpoint.
    If the poller was not running, that window's OI, GEX, walls, PCR and dominance are
    permanently lost, and must be reported as gaps — never interpolated, never carried
    forward from a stale snapshot.
    """
    import re
    import store
    import agt_briefing

    obs_file = config.DATA_DIR / "observations" / f"{target_date:%Y-%m-%d}.md"
    existing_bins = set()
    if obs_file.exists():
        text = obs_file.read_text(encoding="utf-8")
        for m in re.finditer(r"^##\s+(\d{2}:\d{2})", text, re.MULTILINE):
            existing_bins.add(m.group(1))

    all_snaps = store.load_day(target_date)
    if not all_snaps:
        return {"recovered": 0, "unrecoverable_gaps": 0, "status": "no snapshots"}

    recovered = 0
    gaps = []

    now_t = datetime.now().time() if target_date == date.today() else dtime(15, 30)
    cur = datetime.combine(target_date, dtime(9, 15))
    end_t = datetime.combine(target_date, min(dtime(15, 30), now_t))

    while cur <= end_t:
        hm = cur.strftime("%H:%M")
        if hm not in existing_bins:
            matching = [s for s in all_snaps if abs((s[0] - cur).total_seconds()) <= 60]
            if matching:
                try:
                    agt_briefing.build_briefing(target_date, cur.time(), append_obs=True)
                    recovered += 1
                    existing_bins.add(hm)
                except Exception as exc:
                    log(f"reconcile_observation_log error at {hm}: {exc}")
            else:
                gaps.append(hm)
        cur += timedelta(minutes=3)

    if gaps:
        log(f"reconcile_observation_log for {target_date}: {len(gaps)} UNRECOVERABLE gaps (no snapshot on disk): {gaps[:10]}")
    if recovered > 0:
        log(f"reconcile_observation_log for {target_date}: recovered {recovered} missing observation bins from snapshots")

    return {"recovered": recovered, "unrecoverable_gaps": len(gaps), "gap_times": gaps}


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

    rules_path = Path(__file__).resolve().parent / "strategy1_rules.json"
    fast_cfg = {}
    try:
        if rules_path.exists():
            fast_cfg = json.loads(rules_path.read_text(encoding="utf-8")).get("fast_poll_window", {})
    except Exception:
        pass
    fast_int = fast_cfg.get("chain_interval_seconds", fast_cfg.get("interval_seconds", 20))
    fast_start = fast_cfg.get("start", "09:15")
    fast_end = fast_cfg.get("end", "09:30")
    fast_en = fast_cfg.get("enabled", True)

    log("scheduler starting")

    # Option C: Enable writes exclusively for scheduler process
    store.enable_writes("scheduler")
    import price_action
    price_action.enable_writes("scheduler")

    # Startup Self-Healing (daemon mode only):
    if not once:
        # 1. 5m Candles: refetch missing bars for today and previous 15 sessions from Dhan historical API
        try:
            import dhan_client
            c_paths = dhan_client.backfill_5m_candles(days_back=15)
            log(f"startup self-healing: synced {len(c_paths)} candle sessions from Dhan")
        except Exception as c_exc:
            log(f"WARNING: startup candle self-healing error: {c_exc}")

        # 2. Observation log replay: replay missing 3-minute bins from stored snapshots for today
        try:
            rec_res = reconcile_observation_log(date.today())
            if rec_res.get("recovered", 0) > 0:
                log(f"startup self-healing: recovered {rec_res['recovered']} observation bins for today")
        except Exception as r_exc:
            log(f"WARNING: observation log self-healing error: {r_exc}")
    cadence_str = f"  every {config.POLL_SECONDS}s"
    if fast_en:
        cadence_str += f" (fast window {fast_start}-{fast_end}: every {fast_int}s)"
    log(f"  chain  {config.CHAIN_WINDOW[0]:%H:%M}-{config.CHAIN_WINDOW[1]:%H:%M}{cadence_str}")
    log(f"  view   {config.VIEW_WINDOW[0]:%H:%M}-{config.VIEW_WINDOW[1]:%H:%M}"
        f"  every {config.VIEW_REFRESH_MINUTES}m")
    log(f"  ohlc   {config.OHLC_RETRY_FROM:%H:%M}-"
        f"{config.OHLC_RETRY_UNTIL:%H:%M}  retry every "
        f"{config.OHLC_RETRY_MINUTES}m until it lands")

    try:
        retention = 90
        if rules_path.exists():
            cfg = json.loads(rules_path.read_text(encoding="utf-8"))
            retention = cfg.get("greeks", {}).get("multi_expiry_retention_days", 90)
        pruned = store.prune_multi_snapshots(retention, dry_run=True)
        if len(pruned) > 0:
            log(f"startup retention (dry-run): would prune {len(pruned)} old multi-expiry snapshot folder(s) (> {retention} days): {pruned}")
        else:
            log(f"startup retention (dry-run): 0 folders eligible for pruning (> {retention} days, keeping >= 15 recent)")
    except Exception as p_exc:                              # noqa: BLE001
        log(f"WARNING: startup retention check failed: {p_exc}")
        # never raise — housekeeping must not stop data capture

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
                    poll_interval = get_chain_poll_interval(now)
                    next_chain = now + timedelta(seconds=poll_interval)
            else:
                if (now - last_skip_hb).total_seconds() >= 60:
                    reason = "Weekend (market closed)" if not is_mkt_day else f"Outside window {config.CHAIN_WINDOW[0]:%H:%M}-{config.CHAIN_WINDOW[1]:%H:%M}"
                    record_heartbeat("SKIPPED", skip_reason=reason)
                    last_skip_hb = now

            # Fast window completion tracking
            in_fast_win = is_in_fast_poll_window(now)
            was_fast_active = st.get("fast_window_active", False)
            if was_fast_active and not in_fast_win:
                count = st.get("fast_window_chain_polls", 0)
                log(f"fast_poll_window complete: {count} requests made between {fast_start} and {fast_end} (Dhan daily quota check)")
                st["fast_window_active"] = False
            elif in_fast_win and not was_fast_active:
                st["fast_window_active"] = True

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
            _time.sleep(1)
    except KeyboardInterrupt:
        log("stopped")
        return 0


if __name__ == "__main__":
    sys.exit(main())
