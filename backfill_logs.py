"""
backfill_logs.py — replay past sessions through both strategies.

Run this once and the logs stop being empty: every session since the start
date is walked through Strategy 1 and Strategy 2, and the trades they would
have produced are written down.

WHY EVERY ROW IS TAGGED "backfill"
──────────────────────────────────
Because this is a backtest wearing the clothes of a record, and the two are
not the same evidence.

A live row means: the app said this at the time, you saw it, and price did
what it did afterwards. A backfilled row means: given the candles, the rules
would have produced this. The second is weaker in three specific ways —

  The parameters were chosen while looking at recent charts. The 55-point
  big-candle threshold, the 40-point stop, the Doji body ratio: all of them
  were set with August and September in view. Testing them on August and
  September is partly marking your own homework, and it will flatter the
  result by an amount nobody can measure.

  VWAP was not available then and is not reconstructable now. Strategy 2's
  "whichever comes first" rule degenerates to 8-EMA-only across the whole
  backfill, so those rows are a different strategy from the one the tab
  runs today.

  There was no execution. Nothing here accounts for a fill you did not get,
  a spread you paid, or the trade you talked yourself out of at 09:47.

So `source` is written on every row and `scorecard()` reports the two
populations separately. Blending them would let thirty backtested trades
satisfy the "enough data" gate that exists precisely to stop you trusting a
small live sample — which would turn a safeguard into its opposite.
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta

import pandas as pd

import config


def fetch_15m(since: date) -> pd.DataFrame:
    """15-minute bars from local candles_15m store (with Dhan fallback), market hours only."""
    from pathlib import Path
    candle_dir = config.DATA_DIR / "candles_15m"
    dfs = []
    if candle_dir.exists():
        for f in sorted(candle_dir.glob("*.csv")):
            try:
                d = date.fromisoformat(f.stem)
                if d >= since:
                    sub_df = pd.read_csv(f)
                    if "timestamp" in sub_df.columns:
                        sub_df["timestamp"] = pd.to_datetime(sub_df["timestamp"])
                        dfs.append(sub_df)
            except ValueError:
                continue
    if dfs:
        bars = pd.concat(dfs, ignore_index=True)
        ts = pd.to_datetime(bars["timestamp"])
        keep = (ts.dt.time >= config.MARKET_OPEN) & (ts.dt.time <= config.MARKET_CLOSE)
        out = bars[keep].copy()
        out["session"] = pd.to_datetime(out["timestamp"]).dt.date
        return out.sort_values("timestamp").drop_duplicates(subset=["timestamp"]).reset_index(drop=True)

    import dhan_client as dc
    days = (date.today() - since).days + 2
    if days > 90:
        raise SystemExit(f"{since:%d-%b} is {days} days back; Dhan's intraday "
                         f"window is about 90 days.")
    bars = dc.intraday(config.DHAN_INDEX_SECURITY_ID, "IDX_I", "INDEX",
                       days_back=days, interval="15")
    if bars is None or bars.empty:
        raise SystemExit("Dhan returned no 15-minute bars")
    ts = pd.to_datetime(bars["timestamp"])
    keep = (ts.dt.time >= config.MARKET_OPEN) & (ts.dt.time <= config.MARKET_CLOSE)
    out = bars[keep].copy()
    out["session"] = pd.to_datetime(out["timestamp"]).dt.date
    return out.sort_values("timestamp").reset_index(drop=True)


def daily_closes(bars: pd.DataFrame | None = None) -> dict:
    """Previous-session close per date, for Strategy 1's gap gate."""
    out = {}
    try:
        import build_master as bm
        h = bm.load_all(config.OHLC_DIR)
        h["Date"] = pd.to_datetime(h["Date"]).dt.date
        h = h.sort_values("Date")
        out = {d: c for d, c in zip(h["Date"].shift(-1), h["Close"])}
    except Exception:                                       # noqa: BLE001
        pass
    if bars is not None and "session" in bars and "close" in bars:
        day_closes = bars.groupby("session")["close"].last()
        prev_series = day_closes.shift(1)
        for d, c in prev_series.dropna().items():
            if d not in out or pd.isna(out[d]):
                out[d] = float(c)
    return out


def run(since: date, sessions_back: int | None = None, overwrite: bool = True) -> dict:
    import setups as su
    import strategy1 as s1
    import strategy1_log as s1log
    import strategy2 as s2

    warmup_since = since - timedelta(days=45)
    bars = fetch_15m(warmup_since)
    all_days = sorted(set(bars["session"]))
    days = [d for d in all_days if d >= since]
    if sessions_back:
        days = days[-sessions_back:]
    prev_close = daily_closes(bars)

    s1_rows, s2_rows, per_day = [], [], []

    for d in days:
        # Every bar UP TO AND INCLUDING this session. Strategy 1 needs the
        # history — Bollinger(20) on 15 minutes wants twenty candles and a
        # session is twenty-five — but must not see the future, so the frame
        # is truncated at the session's end rather than handed everything.
        upto = bars[bars["session"] <= d].copy()
        frame = su.prepare(upto.set_index("timestamp"))
        today = frame[pd.to_datetime(frame.index).date == d]
        if len(today) < 4:
            continue

        n1 = n2 = 0
        try:
            res = s1.evaluate(frame, prev_close=prev_close.get(d),
                              now=datetime.combine(d, config.MARKET_CLOSE))
            for s in res.signals:
                if getattr(s, "exit_i", None) is None:
                    continue
                if s.armed_ts.date() != d:
                    continue
                s1_rows.append({
                    "day": d.isoformat(), "direction": s.direction,
                    "armed_ts": str(s.armed_ts),
                    "entry_ts": str(getattr(s, "entry_ts", "")),
                    "entry": getattr(s, "trigger", None),
                    "trigger": getattr(s, "trigger", None),
                    "stop0": getattr(s, "stop0", None),
                    "exit_ts": str(getattr(s, "exit_ts", "")),
                    "exit_price": getattr(s, "exit_price", None),
                    "exit_reason": getattr(s, "exit_reason", ""),
                    "points": getattr(s, "points", None),
                    "ambiguous": bool(getattr(s, "ambiguous", False)),
                    "held_candles": (s.exit_i - s.entry_i
                                     if getattr(s, "entry_i", None) is not None
                                     else None),
                    "source": "backfill",
                })
                n1 += 1
        except Exception as exc:                            # noqa: BLE001
            print(f"  {d}: strategy 1 failed — {exc}")

        try:
            for t in s2.run(today, mode="mentor"):
                s2_rows.append({
                    "day": d.isoformat(), "direction": t.direction,
                    "entry_ts": str(t.entry_ts), "entry": round(t.entry, 2),
                    "trigger": t.trigger, "stop0": round(t.stop0, 2),
                    "stop_final": round(t.stop, 2),
                    "exit_ts": str(t.exit_ts) if t.exit_ts else "",
                    "exit": None if t.exit is None else round(t.exit, 2),
                    "reason": t.reason,
                    "points": None if t.points is None else round(t.points, 2),
                    "trails": len(t.trail_log), "source": "backfill",
                })
                n2 += 1
        except Exception as exc:                            # noqa: BLE001
            print(f"  {d}: strategy 2 failed — {exc}")

        per_day.append((d, n1, n2))

    # write backfilled logs cleanly
    if s1_rows:
        s1log.DIR.mkdir(parents=True, exist_ok=True)
        for d, grp in pd.DataFrame(s1_rows).groupby("day"):
            p = s1log.DIR / f"{d}.csv"
            if overwrite or not p.exists():
                grp.to_csv(p, index=False)
            else:
                existing = pd.read_csv(p) if p.exists() else pd.DataFrame()
                if len(existing) and "source" in existing:
                    existing = existing[existing["source"] != "backfill"]
                pd.concat([existing, grp], ignore_index=True).to_csv(p, index=False)

    s2dir = config.DATA_DIR / "strategy2_log"
    if s2_rows:
        s2dir.mkdir(parents=True, exist_ok=True)
        for d, grp in pd.DataFrame(s2_rows).groupby("day"):
            p = s2dir / f"{d}.csv"
            if overwrite or not p.exists():
                grp.to_csv(p, index=False)
            else:
                existing = pd.read_csv(p) if p.exists() else pd.DataFrame()
                if len(existing) and "source" in existing:
                    existing = existing[existing["source"] != "backfill"]
                pd.concat([existing, grp], ignore_index=True).to_csv(p, index=False)

    return {"sessions": len(per_day), "s1": len(s1_rows), "s2": len(s2_rows),
            "per_day": per_day,
            "first": days[0] if days else None,
            "last": days[-1] if days else None}


def main() -> int:
    since = date(2026, 8, 3)
    if len(sys.argv) > 1:
        try:
            since = date.fromisoformat(sys.argv[1])
        except ValueError:
            print("usage: python backfill_logs.py [YYYY-MM-DD]")
            return 1

    print(f"replaying sessions from {since:%d-%b-%Y}\n")
    r = run(since)
    print(f"{'session':<12}{'S1':>5}{'S2':>5}")
    for d, a, b in r["per_day"]:
        print(f"{d.isoformat():<12}{a:>5}{b:>5}")
    print(f"\n{r['sessions']} sessions · Strategy 1: {r['s1']} trades · "
          f"Strategy 2: {r['s2']} trades")
    print("\nEvery row is tagged source=backfill. The tabs report backtested "
          "and live trades separately;\nthe 'enough data' gate counts only "
          "live ones, because that gate exists to stop a small\nsample being "
          "mistaken for evidence and a backtest is not the missing evidence.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
