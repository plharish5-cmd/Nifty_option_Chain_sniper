"""
nifty_scanner.py — NiftyScanner: LOGS every ICT event together with what order flow and the option chain said at that moment.

It does NOT issue trade signals. The three-way "confluence" (ICT event + order-flow confirmation + option-chain bias)
cannot be validated yet: order flow only exists from the first recorded session, and the option chain only from the first
stored snapshot. So the scanner writes each event with its context now, and `--report` later asks the only question that
matters: did events with a higher confluence score move further in the signalled direction than events with a low one,
more than shuffling the scores would give? Until n >= 30 events with a score, the answer is PENDING.

Events logged (new ones only; dedupe on kind + candle time + side + level):
  TURTLE_SOUP  wick 2-10 pts past a liquidity pool, 5m candle closed back inside       (turtle_soup.py)
  ICT_SETUP    sweep → displacement/MSS → FVG, with CE entry / SL / premium-discount   (ict_model.py)
Context per event:
  order flow   nifty_orderflow.confirm() on ticks BEFORE the event candle closed → CONFIRMED / AGAINST / NEUTRAL / NO DATA
  option chain latest snapshot at or before the event: call wall, put wall, net bias over ATM±3 strikes
               (mean CE vote − mean PE vote, long-buildup/short-covering = +1, short-buildup/long-unwinding = −1),
               and a squeeze flag (the wall strike's OI falling while its premium rises)
Score (UNVALIDATED): 1 for the ICT event, +1 if order flow CONFIRMED, +1 if option-chain bias agrees (|net| ≥ 0.25).

  .venv/bin/python nifty_scanner.py --once              # scan now, log new events, print them
  .venv/bin/python nifty_scanner.py --watch 60          # repeat every 60 s while the market is open
  .venv/bin/python nifty_scanner.py --day 2026-10-05    # replay a stored day
  .venv/bin/python nifty_scanner.py --backfill 30       # replay the last 30 stored sessions (price + whatever chain/flow exists)
  .venv/bin/python nifty_scanner.py --report            # score buckets, permutation test → data/backtests/confluence.json
Logs: data/scanner/events_YYYY-MM-DD.csv.   Information only — not financial advice.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
import backtest_five_rule as fr          # noqa: E402
import ict_model as im                   # noqa: E402
import turtle_soup as ts                 # noqa: E402

LOG_DIR = ROOT / "data" / "scanner"
OUT_DIR = ROOT / "data" / "backtests"
NIFTY_TOKEN = "13"                       # Dhan security id of the NIFTY 50 index
COLS = ["logged_at", "day", "event_t", "token", "kind", "side", "level_kind", "level", "depth", "close_inside", "smt",
        "entry_ce", "sl", "flow", "flow_note", "chain", "call_wall", "put_wall", "net_bias", "squeeze", "ref_strike",
        "score", "unvalidated"]
BIAS_MIN = 0.25
ATM_N = 3


def _atm(spot: float) -> int:
    return int(round(spot / 50.0) * 50)


# ═══════════════════════════════════════════════════════════════════════════
# Option-chain context
# ═══════════════════════════════════════════════════════════════════════════
def chain_state(snaps: list, t: pd.Timestamp, sgn: int) -> dict:
    """Option-chain read at the latest snapshot at or before t. Empty dict-values when there is none."""
    empty = dict(chain="NO DATA", call_wall=np.nan, put_wall=np.nan, net_bias=np.nan, squeeze="", ref_strike=_atm(0))
    if not snaps:
        return empty
    ok = [s for s in snaps if pd.Timestamp(s[0]) <= t]
    if not ok:
        return empty
    ts_, spot, df = ok[-1]
    if (t - pd.Timestamp(ts_)) > pd.Timedelta(minutes=20):                  # stale snapshot — do not pretend
        e = dict(empty)
        e["ref_strike"] = _atm(spot)
        return e
    import metrics
    need = {"strike", "ce_oi", "pe_oi", "ce_chg_oi", "pe_chg_oi", "ce_chg", "pe_chg"}
    if not need.issubset(df.columns):
        return empty
    df = df.sort_values("strike").reset_index(drop=True)
    up, dn = df[df["strike"] >= spot], df[df["strike"] <= spot]
    cw = up.loc[up["ce_oi"].idxmax()] if len(up) and up["ce_oi"].max() > 0 else None
    pw = dn.loc[dn["pe_oi"].idxmax()] if len(dn) and dn["pe_oi"].max() > 0 else None
    near = df.iloc[(df["strike"] - spot).abs().argsort()[:2 * ATM_N + 1]]
    cv = [metrics._contract_vote(float(r.ce_chg_oi), float(r.ce_chg), "combined") for r in near.itertuples()]
    pv = [metrics._contract_vote(float(r.pe_chg_oi), float(r.pe_chg), "combined") for r in near.itertuples()]
    net = float(np.mean(cv) - np.mean(pv))
    squeeze = ""
    if cw is not None and cw["ce_chg_oi"] < 0 and cw["ce_chg"] > 0:
        squeeze = f"CALL short-covering at wall {int(cw['strike'])}"
    elif pw is not None and pw["pe_chg_oi"] < 0 and pw["pe_chg"] > 0:
        squeeze = f"PUT short-covering at wall {int(pw['strike'])}"
    return dict(chain="OK", call_wall=float(cw["strike"]) if cw is not None else np.nan,
                put_wall=float(pw["strike"]) if pw is not None else np.nan, net_bias=round(net, 2),
                squeeze=squeeze, ref_strike=_atm(spot))


# ═══════════════════════════════════════════════════════════════════════════
# Order-flow context
# ═══════════════════════════════════════════════════════════════════════════
def flow_state(data: dict, t: pd.Timestamp, sgn: int) -> tuple:
    """confirm() using only ticks that were recorded BEFORE the event candle closed."""
    import nifty_orderflow as nf
    if not data or data.get("ticks") is None or data["ticks"].empty:
        return "NO DATA", "no recorded ticks for this day"
    cut = {k: (v[v["t"] < t].copy() if len(v) and "t" in v else v) for k, v in data.items()}
    f = nf.compute(cut, 5, day="")
    return nf.confirm(f, sgn)


# ═══════════════════════════════════════════════════════════════════════════
# The scanner
# ═══════════════════════════════════════════════════════════════════════════
class NiftyScanner:
    def __init__(self, P: dict | None = None):
        self.P = P or im.rules()
        self.min_events = 30

    # -- data ----------------------------------------------------------
    def _price(self):
        m1 = fr.load_1m()
        return m1

    def _bn5(self):
        try:
            import bn_history as bn
            b = bn.load()
            return im.resample(b, int(self.P["tf_minutes"])) if len(b) else None
        except Exception:
            return None

    def events(self, day: str, now: pd.Timestamp | None = None, m1: pd.DataFrame | None = None) -> list:
        """ICT events for one session that were already knowable at `now` (None = the whole session)."""
        m1 = self._price() if m1 is None else m1
        if m1.empty:
            return []
        days = sorted(m1["timestamp"].dt.date.unique())
        d = pd.Timestamp(day).date()
        if d not in days:
            return []
        k = days.index(d)
        keep = set(days[max(0, k - 6):k + 1])                              # a week of context for previous-day levels
        sub = m1[m1["timestamp"].dt.date.isin(keep)]
        tf = int(self.P["tf_minutes"])
        d5 = im.resample(sub, tf)
        if now is not None:                                                  # only fully closed candles
            d5 = d5[d5["timestamp"] + pd.Timedelta(minutes=tf) <= now]
        r5 = self._bn5()
        out = []
        for e in ts.events(d5, self.P, r5):
            if e["day"] != day:
                continue
            out.append(dict(kind="TURTLE_SOUP", event_t=e["t"], side=e["side"], level_kind=e["kind"], level=e["level"],
                            depth=e["depth"], close_inside=e["close_inside"], smt=e["smt"], entry_ce=np.nan, sl=np.nan))
        for s in im.detect_all(d5, self.P, r5):
            if s["day"] != day:
                continue
            if now is not None and s["sig_t"] > now:
                continue
            out.append(dict(kind="ICT_SETUP", event_t=s["sig_t"], side=s["side"], level_kind=s["level_kind"], level=s["level"],
                            depth=np.nan, close_inside=True, smt=s.get("smt"), entry_ce=s["entry_ce"], sl=s["sl"]))
        return sorted(out, key=lambda x: x["event_t"])

    # -- one session ---------------------------------------------------
    def scan(self, day: str | None = None, now: pd.Timestamp | None = None, m1: pd.DataFrame | None = None,
             quiet: bool = False) -> pd.DataFrame:
        import nifty_orderflow as nf
        import store
        day = day or datetime.now().strftime("%Y-%m-%d")
        live = now is not None
        evs = self.events(day, now, m1)
        path = LOG_DIR / f"events_{day}.csv"
        old = pd.read_csv(path) if path.exists() else pd.DataFrame(columns=COLS)
        seen = set(zip(old["kind"], old["event_t"].astype(str), old["side"], old["level"].round(1))) if len(old) else set()
        new = [e for e in evs if (e["kind"], str(e["event_t"]), e["side"], round(float(e["level"]), 1)) not in seen]
        if not new:
            return pd.DataFrame(columns=COLS)
        try:
            data = nf.load_day(day) if day in nf.recorded_days() else {}
        except Exception:
            data = {}
        try:
            snaps = store.load_day(pd.Timestamp(day).date())
        except Exception:
            snaps = []
        rows = []
        for e in new:
            sgn = 1 if e["side"] == "BULLISH" else -1
            fl, note = flow_state(data, e["event_t"], sgn)
            ch = chain_state(snaps, e["event_t"], sgn)
            if not ch["ref_strike"]:
                ch["ref_strike"] = _atm(float(e["level"]))
            score = 1 + int(fl == "CONFIRMED") + int(not np.isnan(ch["net_bias"]) and abs(ch["net_bias"]) >= BIAS_MIN
                                                       and np.sign(ch["net_bias"]) == sgn)
            rows.append(dict(logged_at=datetime.now().isoformat(timespec="seconds"), day=day, event_t=str(e["event_t"]),
                             token=NIFTY_TOKEN, kind=e["kind"], side=e["side"], level_kind=e["level_kind"],
                             level=round(float(e["level"]), 1), depth=e["depth"], close_inside=bool(e["close_inside"]),
                             smt=e["smt"], entry_ce=e["entry_ce"], sl=e["sl"], flow=fl, flow_note=note, score=score,
                             unvalidated=True, **ch))
        df = pd.DataFrame(rows)[COLS]
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        pd.concat([old, df], ignore_index=True)[COLS].to_csv(path, index=False)
        if not quiet:
            for r in df.itertuples():
                self._print(r)
        return df

    @staticmethod
    def _print(r):
        arrow = "▲ up-idea" if r.side == "BULLISH" else "▼ down-idea"
        opt = f"{int(r.ref_strike)} {'CE' if r.side == 'BULLISH' else 'PE'}"
        print(f"{str(r.event_t)[11:16]}  {r.kind:<11} {arrow:<12} {r.level_kind:<6}@{r.level:,.1f}  "
              f"flow:{r.flow:<9} chain:{r.chain:<7} bias:{'—' if pd.isna(r.net_bias) else f'{r.net_bias:+.2f}'}"
              f"{('  ' + r.squeeze) if r.squeeze else ''}  score {r.score}/3  [reference strike {opt}]  UNVALIDATED")

    # -- history -------------------------------------------------------
    def backfill(self, n_days: int):
        m1 = self._price()
        days = [str(d) for d in sorted(m1["timestamp"].dt.date.unique())][-n_days:]
        tot = 0
        for d in days:
            tot += len(self.scan(d, None, m1, quiet=True))
        print(f"Backfill: {len(days)} sessions replayed, {tot} new events logged to {LOG_DIR}")

    # -- report --------------------------------------------------------
    def report(self, horizon: int = 30, reps: int = 2000, seed=3) -> dict:
        files = sorted(LOG_DIR.glob("events_*.csv")) if LOG_DIR.exists() else []
        ev = pd.concat([pd.read_csv(f) for f in files], ignore_index=True) if files else pd.DataFrame(columns=COLS)
        out = dict(generated=datetime.now().isoformat(timespec="seconds"), horizon_min=horizon, status="PENDING",
                   sessions=int(ev["day"].nunique()) if len(ev) else 0, events=int(len(ev)),
                   flow_sessions=int(ev.loc[ev["flow"] != "NO DATA", "day"].nunique()) if len(ev) else 0,
                   chain_sessions=int(ev.loc[ev["chain"] != "NO DATA", "day"].nunique()) if len(ev) else 0,
                   by_score={}, test={}, min_events=self.min_events)
        if ev.empty:
            print("No events logged yet. Run --backfill 30 or --once during market hours.")
            return self._save(out)
        m1 = self._price()
        from backtest_turtle_soup import Prices, forward
        px = Prices(m1)
        ev["event_t"] = pd.to_datetime(ev["event_t"])
        ev["sgn"] = np.where(ev["side"] == "BULLISH", 1, -1)
        for h in (15, 30, 60):
            ev[f"m{h}"] = [forward(px, d, t, h) for d, t in zip(ev["day"], ev["event_t"])]
        ev["mv"] = ev["sgn"] * ev[f"m{horizon}"]
        ev = ev.dropna(subset=["mv"])
        print(f"{len(ev)} events on {ev['day'].nunique()} sessions · order-flow sessions: {out['flow_sessions']} · "
              f"chain sessions: {out['chain_sessions']}\n")
        print(f"  {'score':<28}{'n':>5}{'avg pts @' + str(horizon) + 'm':>14}{'hit':>8}   (signed in the event's direction)")
        for sc, g in ev.groupby("score"):
            lab = {1: "1 · ICT event only", 2: "2 · + flow OR chain agrees", 3: "3 · all three agree"}.get(int(sc), str(sc))
            out["by_score"][str(int(sc))] = dict(n=int(len(g)), avg_pts=round(float(g["mv"].mean()), 2),
                                                 hit=round(float((g["mv"] > 0).mean()), 3),
                                                 **{f"avg_{h}": round(float((g['sgn'] * g[f'm{h}']).mean()), 2) for h in (15, 30, 60)})
            print(f"  {lab:<28}{len(g):>5}{g['mv'].mean():>+14.2f}{100 * (g['mv'] > 0).mean():>7.0f}%")
        hi, lo = ev[ev["score"] >= 3], ev[ev["score"] <= 1]
        if len(hi) >= self.min_events and len(lo) >= self.min_events:
            obs = float(hi["mv"].mean() - lo["mv"].mean())
            sub = ev[(ev["score"] >= 3) | (ev["score"] <= 1)]
            is_hi = (sub["score"] >= 3).to_numpy()
            mv = sub["mv"].to_numpy(float)
            rng = np.random.default_rng(seed)
            null = np.empty(reps)
            for r in range(reps):
                lab = rng.permutation(is_hi)
                null[r] = mv[lab].mean() - mv[~lab].mean()
            p = float(((null >= obs).sum() + 1) / (reps + 1))
            out["test"] = dict(n_high=int(len(hi)), n_low=int(len(lo)), diff_pts=round(obs, 2), p=round(p, 4))
            out["status"] = "EDGE" if (obs > 0 and p < 0.05 and hi["mv"].mean() > 0) else "NO EDGE"
            print(f"\n  score-3 minus score≤1: {obs:+.2f} pts, permutation p = {p:.3f} → {out['status']}")
        else:
            print(f"\n  PENDING — needs ≥ {self.min_events} events at score 3 and at score ≤ 1 "
                  f"(have {len(hi)} / {len(lo)}). Order flow records from the first live session; keep the logger running.")
        return self._save(out)

    @staticmethod
    def _save(out: dict) -> dict:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        (OUT_DIR / "confluence.json").write_text(json.dumps(out, indent=2, default=str))
        print(f"Saved {OUT_DIR / 'confluence.json'}. Information only — not financial advice.")
        return out


def _market_open(now: datetime) -> bool:
    return now.weekday() < 5 and (9, 15) <= (now.hour, now.minute) <= (15, 30)


def main():
    import runlog
    runlog.start("nifty_scanner")
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--once", action="store_true")
    g.add_argument("--watch", type=int, metavar="SECONDS")
    g.add_argument("--day", metavar="YYYY-MM-DD")
    g.add_argument("--backfill", type=int, metavar="DAYS")
    g.add_argument("--report", action="store_true")
    a = ap.parse_args()
    sc = NiftyScanner()
    if a.report:
        sc.report()
    elif a.backfill:
        sc.backfill(a.backfill)
    elif a.day:
        out = sc.scan(a.day)
        print(f"{len(out)} new event(s) logged for {a.day}")
    elif a.watch:
        print(f"Watching every {a.watch}s (Ctrl-C to stop). Events are logged, not traded.")
        while True:
            now = datetime.now()
            if _market_open(now):
                sc.scan(now.strftime("%Y-%m-%d"), pd.Timestamp(now))
            time.sleep(a.watch)
    else:
        now = datetime.now()
        out = sc.scan(now.strftime("%Y-%m-%d"), pd.Timestamp(now))
        print(f"{len(out)} new event(s)")


if __name__ == "__main__":
    main()
