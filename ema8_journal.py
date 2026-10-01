"""
ema8_journal.py — your 8 EMA homework, written by the app, one CSV per day, 15-min candles only.

  Folder: data/ema8_journal/
      days/2026-09-30.csv   one file per trading day (a day with no trade gets one "no trade" row)
      all.csv               every day joined together (open it in Excel / LibreOffice)
      summary_by_year.csv   trades, points, win %, how often +15 / +20 worked, green days

  .venv/bin/python ema8_journal.py                        # fill every missing day up to today (safe to run any time)
  .venv/bin/python ema8_journal.py --max                  # the whole 5-year history, all days (first run)
  .venv/bin/python ema8_journal.py --from 2026-09-01      # redo from a date (overwrites those days)
  .venv/bin/python ema8_journal.py --from 2026-09-01 --to 2026-09-10
  .venv/bin/python ema8_journal.py --today                # just today, from the live candles

Going forward the watcher (mentor_oc_watch.py, every 3 min) rewrites today's file each time a 15-min candle
closes, so the CSV fills in during the day. If the PC was off, just run the first command: it finds the gap and
fills it. Today's open trade shows exit reason "OPEN (running)" until it closes.

Columns: Date, Trade #, Side, Entry time, Entry price, Entry reason, SL, Risk pts, Exit time, Exit price,
Exit reason, Profit, Loss, Max move pts, 15 pts worked, 20 pts worked, Day total
Exit plan = your beginner plan: SL = signal candle low / high, trail under every 15-min candle, book at +120 max.
Information only - not financial advice.
"""
from __future__ import annotations

import argparse
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

import ema8_signals as e8

log = logging.getLogger("ema8_journal")
HERE = Path(__file__).resolve().parent
DIR = HERE / "data" / "ema8_journal"
DAYS = DIR / "days"


def _day_rows(t: pd.DataFrame, day: date, R: dict, live_last: pd.Timestamp | None = None) -> pd.DataFrame:
    if not len(t):
        return pd.DataFrame([{"Date": f"{day:%d-%m-%Y}", "Trade #": 0, "Exit reason": "no trade", "Day total": 0.0}])
    j = e8.journal(t, R)
    if live_last is not None:                 # today, market still open: the last trade may still be running
        tl = f"{live_last:%H:%M}"
        still = (j["Exit time"] == tl) & (j["Exit reason"] == "15:15 day end") & (tl < R["exit"]["exit_by"])
        j.loc[still, "Exit reason"] = "OPEN (running)"
    return j


def _write_day(day: date, j: pd.DataFrame) -> None:
    DAYS.mkdir(parents=True, exist_ok=True)
    j.to_csv(DAYS / f"{day:%Y-%m-%d}.csv", index=False)


def rebuild_all() -> None:
    files = sorted(DAYS.glob("20*.csv"))
    if not files:
        return
    a = pd.concat([pd.read_csv(f) for f in files], ignore_index=True)
    a.to_csv(DIR / "all.csv", index=False)
    x = a[a["Trade #"] > 0].copy()
    if not len(x):
        return
    x["Year"] = x["Date"].astype(str).str[-4:]
    x["pts"] = x["Profit"].fillna(0) + x["Loss"].fillna(0)
    d = a.dropna(subset=["Day total"]).copy()
    d["Year"] = d["Date"].astype(str).str[-4:]
    lo, hi = e8.rules_for("15m")["exit"].get("beginner_points", [15, 20])
    y = x.groupby("Year").agg(Trades=("pts", "size"), Points=("pts", "sum"),
                              Win_pct=("pts", lambda v: round(100 * (v > 0).mean(), 1)),
                              **{f"{lo}_pts_worked_pct": (f"{lo} pts worked", lambda v: round(100 * (v == "YES").mean(), 1)),
                                 f"{hi}_pts_worked_pct": (f"{hi} pts worked", lambda v: round(100 * (v == "YES").mean(), 1))})
    g = d.groupby("Year")["Day total"].agg(Days="size", Green_days_pct=lambda v: round(100 * (v > 0).mean(), 1),
                                           Avg_day_pts=lambda v: round(v.mean(), 1))
    s = y.join(g).reset_index()
    s["Points"] = s["Points"].round(0)
    s.to_csv(DIR / "summary_by_year.csv", index=False)
    print(s.to_string(index=False))


def fill(days: list[date] | None = None, start: date | None = None, end: date | None = None,
         only_missing: bool = True) -> int:
    """Scan the 15-min history once (EMA warmed up), write each wanted day's CSV."""
    R = e8.rules_for("15m")
    h = e8._history("15m")
    today = date.today()
    frames = [h] if h is not None and len(h) else []
    try:                                         # recent days (and today) from the live source, if newer
        c = e8.pa.tf_candles("15m", today, 10)
        if c is not None and len(c):
            frames.append(c[["timestamp", "open", "high", "low", "close"]])
    except Exception as exc:
        log.info("recent candles unavailable: %s", exc)
    if not frames:
        print("no 15-min candles")
        return 0
    c = pd.concat(frames, ignore_index=True)
    c["timestamp"] = pd.to_datetime(c["timestamp"])
    c = c.drop_duplicates("timestamp", keep="last").sort_values("timestamp").reset_index(drop=True)
    now = datetime.now()
    c = c[c["timestamp"] + timedelta(minutes=15) <= now].reset_index(drop=True)      # closed candles only
    have = set(pd.to_datetime(c["timestamp"]).dt.date)
    want = sorted(d for d in have if (start is None or d >= start) and (end is None or d <= end)
                  and (days is None or d in days))
    if only_missing:
        done = {pd.Timestamp(f.stem).date() for f in DAYS.glob("20*.csv")} if DAYS.exists() else set()
        want = [d for d in want if d not in done or d == today]
    if not want:
        print("journal already up to date")
        return 0
    s = e8.scan(c, R)
    t = s[s["triggered"]] if len(s) else s
    last = pd.Timestamp(c["timestamp"].iloc[-1])
    for d in want:
        td = t[t["day"] == d] if len(t) else t
        live_last = last if d == today and last.date() == today else None
        _write_day(d, _day_rows(td, d, R, live_last))
    print(f"wrote {len(want)} day file(s): {want[0]:%d-%m-%Y} .. {want[-1]:%d-%m-%Y} in {DAYS}")
    rebuild_all()
    return len(want)


def update_today(c: pd.DataFrame | None = None) -> bool:
    """Called by the watcher with the live 15-min candles it already has (closed candles only)."""
    try:
        R = e8.rules_for("15m")
        today = date.today()
        if c is None:
            c = e8.pa.tf_candles("15m", today, 5)
        if c is None or not len(c) or pd.to_datetime(c["timestamp"]).dt.date.iloc[-1] != today:
            return False
        s = e8.scan(c, R, only_day=today)
        t = s[s["triggered"]] if len(s) else s
        _write_day(today, _day_rows(t, today, R, pd.Timestamp(c["timestamp"].iloc[-1])))
        rebuild_all()
        return True
    except Exception as exc:
        log.warning("journal update failed: %s", exc)
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", action="store_true", help="every day in the history (overwrites)")
    ap.add_argument("--from", dest="start")
    ap.add_argument("--to", dest="end")
    ap.add_argument("--today", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    pd.set_option("display.width", 220)
    if a.today:
        print("updated" if update_today() else "no candles for today yet")
        return 0
    st = pd.Timestamp(a.start).date() if a.start else None
    en = pd.Timestamp(a.end).date() if a.end else None
    fill(start=st, end=en, only_missing=not (a.max or a.start))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
