"""
replay_watch.py — replay a whole trading day as your phone would have seen it with EVERYTHING installed:
  - the 5-MIN / 15-MIN periodic snapshots (what scheduler.py writes to data/agt_briefing_watch.log), and
  - the mentor watcher (mentor_oc_watch.py every 3 minutes: option chain, structure, sweeps, body voids,
    zones, day-trend filter, expiry messages).
Each run only sees data up to that minute (option-chain snapshots and completed 15-min candles), so nothing
looks ahead. Nothing is sent to Telegram and the live state files are not touched.

  .venv/bin/python replay_watch.py 2026-09-28
  -> data/agt_briefing_watch_replay_2026-09-28.log   (same format as agt_briefing_watch.log + MENTOR blocks)
"""
from __future__ import annotations

import copy
import sys
import tempfile
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

import pandas as pd

import config
import mentor_option_chain as m
import mentor_oc_watch as w
import mentor_structure as ms
import mentor_sweeps as msw
import store


def main() -> int:
    day = pd.Timestamp(sys.argv[1]).date() if len(sys.argv) > 1 else date.today()
    snaps_all = store.load_day(day)
    if not snaps_all:
        print(f"no option-chain snapshots stored for {day} (data/snapshots/{day})")
    h = ms.load_history(ms.rules())
    if h is None:
        print("no 15-min history (Dhan token?)")
        return 1
    h = h[h["timestamp"].dt.date <= day].reset_index(drop=True)
    clock = {"t": datetime.combine(day, dtime(9, 15))}

    # ── time travel ──────────────────────────────────────────────────────────
    class FakeDate(date):
        @classmethod
        def today(cls):
            return day

    class FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["t"]

    w.date, w.datetime = FakeDate, FakeDT
    orig_load = store.load_day
    m.store.load_day = lambda d=None: [s for s in orig_load(d or day) if s[0] <= clock["t"]]

    def candles(days: int = 5, R=None):
        done = h[h["timestamp"] + timedelta(minutes=15) <= clock["t"]]
        keep = sorted(done["timestamp"].dt.date.unique())[-days:]
        return done[done["timestamp"].dt.date.isin(keep)].reset_index(drop=True), "index"

    ms.recent_candles = candles
    orig_fut = msw.futures_minutes

    def fut(days: int = 0):
        f = orig_fut(days)
        return None if f is None else f[f["timestamp"] <= clock["t"]]

    msw.futures_minutes = fut
    # day trend as of the previous close (the live code would see yesterday's last daily bar)
    try:
        import participant_correlation as pc
        dd = pc.load_ohlc()
        dd = dd[pd.to_datetime(dd.index).date < day]
        w._DAY["t"] = int(ms.analyse(msw.daily_frame(dd).reset_index(drop=True)).trend)
    except Exception as exc:
        print(f"(day trend unavailable: {exc}; treated as undecided)")
        w._DAY["t"] = 0
    # private state file + capture messages instead of sending
    R0 = m.rules()
    tmp_state = Path(tempfile.gettempdir()) / f"replay_watch_state_{day}.json"
    tmp_state.unlink(missing_ok=True)
    R0["watch"]["state_file"] = str(tmp_state)
    m.rules = lambda: copy.deepcopy(R0)
    out: list[tuple[datetime, str, str]] = []
    w._send = lambda text: out.append((clock["t"], "MENTOR", text))

    try:
        import agt_briefing as ab
    except Exception as exc:
        ab = None
        print(f"(agt_briefing unavailable: {exc}; only mentor alerts replayed)")

    t = datetime.combine(day, dtime(9, 15))
    end = datetime.combine(day, dtime(15, 31))
    while t <= end:
        # periodic 5-MIN / 15-MIN snapshot, 10 s after the boundary (as scheduler.py)
        if ab is not None:
            tf = ab.get_snapshot_tf_for_time(t.time())
            if tf:
                clock["t"] = t + timedelta(seconds=10)
                try:
                    r = ab.build_periodic_snapshot(tf, day, clock["t"].time(), append_obs=False)
                    if r and r.get("telegram_text"):
                        out.append((clock["t"], "5-MIN" if tf == "5m" else "15-MIN", r["telegram_text"]))
                except Exception as exc:
                    out.append((clock["t"], "ERROR", f"periodic snapshot failed: {exc}"))
        # mentor watcher: every 3 minutes at :40 s (systemd timer)
        if t.minute % 3 == 0:
            clock["t"] = t + timedelta(seconds=40)
            try:
                w.main()
            except Exception as exc:
                out.append((clock["t"], "ERROR", f"mentor watcher failed: {exc}"))
        t += timedelta(minutes=1)

    path = Path(getattr(config, "DATA_DIR", Path("data"))) / f"agt_briefing_watch_replay_{day}.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    head = {"5-MIN": "🟦 5-MIN", "15-MIN": "🟪 15-MIN", "MENTOR": "🟧 MENTOR", "ERROR": "⚠️ ERROR"}
    with open(path, "w", encoding="utf-8") as f:
        for ts, kind, text in out:
            f.write(f"\n──── {head.get(kind, kind)} {ts:%H:%M} ────\n{text}\n")
    n = {k: sum(1 for _, x, _ in out if x == k) for k in head}
    print(f"{day:%a %d-%b-%Y}: {n['5-MIN']} x 5-MIN, {n['15-MIN']} x 15-MIN snapshots, {n['MENTOR']} mentor messages, "
          f"{n['ERROR']} errors -> {path}")
    print(f"day trend used for the filter: { {1: 'BULLISH', -1: 'BEARISH', 0: 'undecided'}[w._DAY['t']] }")
    print("\nMentor messages:")
    for ts, kind, text in out:
        if kind in ("MENTOR", "ERROR"):
            print(f"\n[{ts:%H:%M}] {text}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
