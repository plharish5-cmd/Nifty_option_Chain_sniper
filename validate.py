"""
Validation harness — does any of this actually predict anything?

    python validate.py

Reads data/signal_log.csv, looks up what spot ACTUALLY did 15, 30 and 60
minutes after each row, and measures every bias component against that
outcome. Then tells you which ones earned their weight and which are noise.

This is the most important file in the project. Everything else produces
opinions; this produces evidence. The weights in analytics.py — PCR at +/-2,
unfilled gap at 3.0, Gann at 0.5 — are reasoned guesses, nothing more. Until
they are measured they are exactly as unfounded as a subscription service's
secret formula, with the single advantage that you can inspect them.

HOW TO READ THE OUTPUT

  IC (information coefficient) is the rank correlation between a component's
  value and what price subsequently did. Interpretation for intraday signals:

      |IC| < 0.02   noise, indistinguishable from nothing
      0.02 - 0.05   weak but potentially real
      0.05 - 0.10   good for this domain
      > 0.10        suspiciously good — check for lookahead before believing it

  Hit rate is the fraction of times the component pointed the right way. 50%
  is a coin. Anything above ~55% on a decent sample is worth keeping.

THE SAMPLE-SIZE TRAP, WHICH MATTERS MORE THAN THE NUMBERS

  Consecutive 3-minute snapshots are NOT independent observations. The OI at
  11:03 is almost identical to the OI at 11:00, and their 30-minute forward
  windows overlap by 90%. So 2,000 logged rows are nowhere near 2,000
  independent tests — closer to a few dozen. This script reports an effective
  sample size that accounts for the overlap, and you should trust that number
  rather than the raw row count.

  The practical consequence: with one month of data you can detect a large
  effect, not a small one. If a component shows IC 0.03 on 15 effective
  observations, that is not evidence. Resist the urge to prune on it.
"""
from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd

import config
import store

HORIZONS = (15, 30, 60)          # minutes
COMPONENTS = ["c_pcr", "c_ce_oi_flow", "c_pe_oi_flow",
              "c_wall_migration", "c_position", "c_wall_break"]


# ═══════════════════════════════════════════════════════════════════════════
# Building the outcome column
# ═══════════════════════════════════════════════════════════════════════════

def attach_forward_returns(log: pd.DataFrame) -> pd.DataFrame:
    """
    For each row, find the spot price N minutes later WITHIN THE SAME DAY.

    Same-day only, deliberately: an overnight gap is not something an
    intraday signal could have predicted, and letting one leak in would
    flatter every component at once.
    """
    df = log.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    df = df.dropna(subset=["timestamp", "spot"]).sort_values("timestamp")
    df["day"] = df["timestamp"].dt.date

    for h in HORIZONS:
        col = f"fwd_{h}m"
        vals: list[float] = []
        for day, grp in df.groupby("day"):
            times = grp["timestamp"].values
            spots = grp["spot"].values
            for i, t in enumerate(times):
                target = t + np.timedelta64(h, "m")
                j = np.searchsorted(times, target)
                # require the match to land within one poll interval
                if j < len(times) and (times[j] - target) <= np.timedelta64(
                        config.POLL_SECONDS, "s"):
                    vals.append(float(spots[j] - spots[i]))
                else:
                    vals.append(np.nan)
        df[col] = vals
    return df


def effective_n(df: pd.DataFrame, horizon_min: int) -> int:
    """
    Independent-ish observation count.

    Overlapping forward windows make consecutive rows near-duplicates. A
    rough but honest correction: count how many non-overlapping windows fit
    inside the logged sessions.
    """
    if df.empty:
        return 0
    per_day = []
    for _, grp in df.groupby("day"):
        span = (grp["timestamp"].max() - grp["timestamp"].min()).total_seconds() / 60
        per_day.append(max(int(span // horizon_min), 1))
    return int(sum(per_day))


# ═══════════════════════════════════════════════════════════════════════════
# Measurement
# ═══════════════════════════════════════════════════════════════════════════

def spearman(a: np.ndarray, b: np.ndarray) -> float:
    """Rank correlation without a scipy dependency."""
    ok = ~(np.isnan(a) | np.isnan(b))
    if ok.sum() < 10:
        return np.nan
    ra = pd.Series(a[ok]).rank().values
    rb = pd.Series(b[ok]).rank().values
    if ra.std() == 0 or rb.std() == 0:
        return np.nan
    return float(np.corrcoef(ra, rb)[0, 1])


def component_report(df: pd.DataFrame, horizon: int) -> pd.DataFrame:
    fwd = df[f"fwd_{horizon}m"].values
    rows = []
    for c in COMPONENTS:
        if c not in df.columns:
            continue
        vals = pd.to_numeric(df[c], errors="coerce").values
        ic = spearman(vals, fwd)

        nz = ~np.isnan(vals) & ~np.isnan(fwd) & (vals != 0)
        if nz.sum() >= 10:
            agree = np.sign(vals[nz]) == np.sign(fwd[nz])
            hit = float(agree.mean() * 100)
            mean_when_pos = float(np.nanmean(fwd[nz & (vals > 0)])) if (nz & (vals > 0)).sum() else np.nan
            mean_when_neg = float(np.nanmean(fwd[nz & (vals < 0)])) if (nz & (vals < 0)).sum() else np.nan
            spread = mean_when_pos - mean_when_neg
        else:
            hit = spread = np.nan

        rows.append({
            "component": c.replace("c_", ""),
            "n_active": int(nz.sum()),
            "IC": round(ic, 4) if ic == ic else None,
            "hit%": round(hit, 1) if hit == hit else None,
            "pts when +": round(mean_when_pos, 1) if nz.sum() >= 10 and mean_when_pos == mean_when_pos else None,
            "pts when -": round(mean_when_neg, 1) if nz.sum() >= 10 and mean_when_neg == mean_when_neg else None,
            "spread": round(spread, 1) if spread == spread else None,
        })
    return pd.DataFrame(rows).sort_values("IC", key=lambda s: s.abs(), ascending=False)


def score_buckets(df: pd.DataFrame, horizon: int) -> pd.DataFrame:
    fwd = f"fwd_{horizon}m"
    d = df.dropna(subset=[fwd, "bias_score"]).copy()
    if d.empty:
        return pd.DataFrame()
    d["bucket"] = pd.cut(pd.to_numeric(d.bias_score, errors="coerce"),
                         [-99, -3, -1, 1, 3, 99],
                         labels=["<= -3", "-3..-1", "-1..1", "1..3", ">= 3"])
    g = d.groupby("bucket", observed=True)[fwd].agg(
        n="count", mean_pts="mean",
        up_rate=lambda s: (s > 0).mean() * 100)
    return g.round(2).reset_index()


def regime_report(df: pd.DataFrame, horizon: int) -> pd.DataFrame:
    fwd = f"fwd_{horizon}m"
    d = df.dropna(subset=[fwd, "regime"])
    if d.empty:
        return pd.DataFrame()
    g = d.groupby("regime")[fwd].agg(
        n="count", mean_pts="mean", sd="std",
        up_rate=lambda s: (s > 0).mean() * 100)
    return g.round(2).reset_index()


def action_report(df: pd.DataFrame, horizon: int) -> pd.DataFrame:
    """
    Did the signal make money after costs?

    Uses ~0.55 delta for an ATM option and the real round-trip friction, so
    this is closer to a P&L than the raw point move. A signal that predicts
    direction but not far enough to clear costs is not a signal you can trade.
    """
    fwd = f"fwd_{horizon}m"
    d = df.dropna(subset=[fwd, "action"]).copy()
    d = d[d.action.isin(["BUY_CE", "BUY_PE"])]
    if d.empty:
        return pd.DataFrame()

    direction = np.where(d.action == "BUY_CE", 1, -1)
    move = d[fwd].values * direction
    gross = move * config.ASSUMED_DELTA * config.LOT_SIZE
    net = gross - config.ROUND_TRIP_COST

    d = d.assign(gross=gross, net=net)
    g = d.groupby("action").agg(
        n=("net", "count"),
        win_rate=("net", lambda s: (s > 0).mean() * 100),
        mean_gross=("gross", "mean"),
        mean_net=("net", "mean"),
        total_net=("net", "sum"))
    return g.round(1).reset_index()


# ═══════════════════════════════════════════════════════════════════════════
# Report
# ═══════════════════════════════════════════════════════════════════════════

def verdict(n_eff: int) -> str:
    if n_eff < 30:
        return ("NOT ENOUGH DATA. Below ~30 independent windows nothing here "
                "is interpretable. Keep logging; change nothing yet.")
    if n_eff < 100:
        return ("THIN. Large effects would show; small ones will not. Treat "
                "everything below as provisional and do not prune components.")
    return ("USABLE SAMPLE. Components with |IC| under 0.02 across all three "
            "horizons are candidates for deletion.")


def main() -> int:
    log = store.load_log()
    if log.empty:
        print("No signal log yet at", config.SIGNAL_LOG)
        print("Run the dashboard through a live session first — every poll "
              "appends a row.")
        return 1

    df = attach_forward_returns(log)
    days = df["day"].nunique()

    print("=" * 72)
    print("VALIDATION REPORT")
    print("=" * 72)
    print(f"rows logged        {len(df):,}")
    print(f"sessions           {days}")
    print(f"date range         {df.timestamp.min():%d-%b-%Y %H:%M} to "
          f"{df.timestamp.max():%d-%b-%Y %H:%M}")
    for h in HORIZONS:
        usable = df[f"fwd_{h}m"].notna().sum()
        print(f"  {h:>2}m outcomes     {usable:,} rows with a forward price "
              f"| ~{effective_n(df, h)} independent windows")

    n_eff = effective_n(df, 30)
    print(f"\n>>> {verdict(n_eff)}\n")

    for h in HORIZONS:
        if df[f"fwd_{h}m"].notna().sum() < 20:
            continue
        print("=" * 72)
        print(f"HORIZON: {h} MINUTES   (~{effective_n(df, h)} independent windows)")
        print("=" * 72)

        print("\nComponent skill")
        print("-" * 72)
        cr = component_report(df, h)
        print(cr.to_string(index=False) if not cr.empty else "  no data")

        print("\nForward move by bias score bucket")
        print("-" * 72)
        sb = score_buckets(df, h)
        print(sb.to_string(index=False) if not sb.empty else "  no data")

        print("\nForward move by regime")
        print("-" * 72)
        rr = regime_report(df, h)
        print(rr.to_string(index=False) if not rr.empty else "  no data")

        print("\nSignal P&L after costs (1 lot, delta "
              f"{config.ASSUMED_DELTA}, Rs{config.ROUND_TRIP_COST:.0f} round trip)")
        print("-" * 72)
        ar = action_report(df, h)
        print(ar.to_string(index=False) if not ar.empty else
              "  no actionable signals logged yet")
        print()

    out = config.DATA_DIR / "validation_detail.csv"
    df.to_csv(out, index=False)
    print("=" * 72)
    print(f"Row-level detail written to {out}")
    print("""
WHAT TO DO WITH THIS

  Delete a component only when it shows |IC| below 0.02 across ALL THREE
  horizons on a usable sample. One bad horizon is noise.

  A component with a good IC but a negative net P&L is predicting direction
  without predicting enough MAGNITUDE to clear costs. That is not tradeable
  on its own, but it may still be worth keeping as a filter.

  If the score buckets do not step monotonically — if ">= 3" does not
  outperform "1..3" — the score is not ordering anything and the weights are
  wrong, even if individual components look fine.

  Resist re-tuning weights until you have several months. Fitting weights to
  one month of your own data is how you build something that backtests
  beautifully and loses money live.
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
