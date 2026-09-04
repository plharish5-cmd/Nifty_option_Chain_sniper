"""
outcomes.py — what price actually did after the app said something.

The timeline records claims. This scores them. For every logged transition
it looks forward a fixed window and asks one narrow question: did spot move
twenty points up, twenty points down, or neither — and which came first.

That last part matters more than it looks. Price that runs +25 and then
reverses to -40 is not "both directions" and it is certainly not a win; the
order decides whether you were right or merely early. So the window is
walked forward in time and the first threshold touch is the answer.

WHAT THIS IS NOT
────────────────
This is not a win rate, and the numbers it produces must not be read as one.

It ignores option premium entirely. Spot rising twenty points does not mean
a call made money — theta, spread and IV crush can all take it away, and on
expiry afternoon usually do. It ignores stops: a trade that eventually went
your way after a forty-point adverse excursion was a loss for anyone with a
real stop-loss. It ignores costs, which on 65 lots is ₹400 a round trip.

It also uses an arbitrary threshold over an arbitrary horizon. Twenty points
in thirty minutes was chosen because it is a plausible intraday move, not
because anything says it is the right question.

What it does tell you is narrower and still worth having: when this app said
BULLISH, did spot tend to go up next? A field that fails that test cannot
possibly be useful. A field that passes it has cleared the lowest available
bar and nothing more.

RESOLUTION LIMIT
────────────────
Spot comes from the stored 3-minute snapshots, so a spike that touched +25
and reverted before the next poll is invisible here. Excursions are
therefore understated, never overstated. Readings this produces are a floor
on what happened, not a measurement of it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

import pandas as pd

import config
import store
import timeline as tl

HORIZON_MINS = getattr(config, "OUTCOME_HORIZON_MINS", 30)
THRESHOLD_PTS = getattr(config, "OUTCOME_THRESHOLD_PTS", 20.0)
MIN_SAMPLE = getattr(config, "OUTCOME_MIN_SAMPLE", 8)

# What each state implies about direction. +1 up, -1 down, 0 flat, None none.
#
# Absent states are scored as having no directional claim rather than as a
# failure — "NO CLEAR STRUCTURE" is the app declining to call a direction,
# and marking that wrong would punish it for honesty.
#
# The Breakout row assumes an upside break, because the dashboard always
# evaluates breakout against the nearest RESISTANCE. If that ever changes to
# evaluate support too, this mapping becomes wrong and must change with it.
EXPECT: dict[str, dict[str, int | None]] = {
    "Regime": {"BULLISH": 1, "BREAKOUT": 1, "BEARISH": -1, "BREAKDOWN": -1,
               "RANGEBOUND": 0, "CONSOLIDATION": 0},
    "Bias": {"BULLISH": 1, "BEARISH": -1, "NEUTRAL": None},
    "Structure": {"UPTREND": 1, "DOWNTREND": -1, "NO CLEAR STRUCTURE": None},
    "Breakout": {"REAL": 1, "POSSIBLE": 1, "FAILED": -1, "FAKE": -1,
                 "UNCONFIRMED": None},
    "Range": {"BROKEN OUT": None, "RANGE TRAP": 0, "INSIDE RANGE": 0},
    "Pinning": {"PINNED": 0, "NOT PINNED": None},
    "Agreement": {"PUT WRITERS CONFIDENT": 1, "CALL WRITERS CONFIDENT": -1,
                  "SPOT AND CHAIN DISAGREE": None, "SPOT FLAT": None},
    "Actionable": {},          # no directional content by design
}

_LABEL = {1: "UP", -1: "DOWN", 0: "FLAT"}


def spot_series(day: date) -> pd.DataFrame:
    """Timestamp and spot for every snapshot of a session, oldest first."""
    rows = [{"t": ts, "spot": sp} for ts, sp in store.spot_only(day)]
    if not rows:
        return pd.DataFrame(columns=["t", "spot"])
    return pd.DataFrame(rows).sort_values("t").reset_index(drop=True)


@dataclass
class Outcome:
    moved: str            # UP / DOWN / FLAT / NO DATA
    first_hit_mins: float | None
    max_up: float
    max_down: float
    close_move: float
    points: int           # snapshots seen in the window

    @property
    def known(self) -> bool:
        return self.moved != "NO DATA"

    @property
    def label(self) -> str:
        if not self.known:
            return "no data"
        if self.moved == "FLAT":
            return f"flat (±{max(self.max_up, -self.max_down):.0f} max)"
        arrow = "+" if self.moved == "UP" else ""
        return (f"{self.moved} {arrow}{self.close_move:.0f} "
                f"(hit in {self.first_hit_mins:.0f}m)")


def forward(series: pd.DataFrame, at: datetime, spot_at: float,
            horizon: int = HORIZON_MINS,
            threshold: float = THRESHOLD_PTS) -> Outcome:
    """
    Walk forward from a transition and report the first threshold touched.

    Both thresholds crossing inside one snapshot gap cannot be separated at
    this resolution; the snapshot's own direction decides, and the ambiguity
    is left in rather than resolved by a rule that would only look precise.
    """
    if series.empty:
        return Outcome("NO DATA", None, 0.0, 0.0, 0.0, 0)

    end = at + timedelta(minutes=horizon)
    win = series[(series.t > at) & (series.t <= end)]
    if win.empty:
        return Outcome("NO DATA", None, 0.0, 0.0, 0.0, 0)

    moves = win["spot"] - spot_at
    max_up, max_down = float(moves.max()), float(moves.min())
    close_move = float(moves.iloc[-1])

    first, hit_at = "FLAT", None
    for t, m in zip(win["t"], moves):
        if m >= threshold:
            first, hit_at = "UP", t
            break
        if m <= -threshold:
            first, hit_at = "DOWN", t
            break

    mins = (hit_at - at).total_seconds() / 60.0 if hit_at is not None else None
    return Outcome(first, mins, max_up, max_down, close_move, len(win))


def score(field: str, to_state: str, out: Outcome) -> str:
    """
    Did what happened agree with what was claimed?

    Returns MATCHED, OPPOSED, MISSED (a directional call that went nowhere),
    NOISY (a flat call that ran), or "—" where the state made no directional
    claim at all and there is nothing to grade.
    """
    if not out.known:
        return "—"
    exp = EXPECT.get(field, {}).get(str(to_state).strip().upper())
    if exp is None:
        return "—"
    if exp == 0:
        return "MATCHED" if out.moved == "FLAT" else "NOISY"
    if out.moved == "FLAT":
        return "MISSED"
    return "MATCHED" if _LABEL[exp] == out.moved else "OPPOSED"


def annotate(days: int = 30, field: str | None = None,
             horizon: int = HORIZON_MINS,
             threshold: float = THRESHOLD_PTS) -> pd.DataFrame:
    """Every logged transition with what price did next attached."""
    log = tl.load_range(days)
    if log.empty:
        return pd.DataFrame()
    if field and field != "All fields":
        log = log[log.field == field]
    if log.empty:
        return pd.DataFrame()

    cache: dict[str, pd.DataFrame] = {}
    rows = []
    for _, r in log.iterrows():
        d = str(r["day"])
        if d not in cache:
            cache[d] = spot_series(date.fromisoformat(d))
        try:
            at = datetime.fromisoformat(str(r["time"]))
        except ValueError:
            continue
        out = forward(cache[d], at, float(r["spot"]), horizon, threshold)
        rows.append({
            "Date": at.strftime("%d %b"), "Time": at.strftime("%H:%M"),
            "Field": r["field"],
            "Changed": (f"{r['from_state']} → {r['to_state']}"
                        if str(r["from_state"]) != "OPEN"
                        else f"opened at {r['to_state']}"),
            "Spot": round(float(r["spot"]), 1),
            f"Next {horizon}m": out.label,
            "Max up": round(out.max_up, 1),
            "Max down": round(out.max_down, 1),
            "Verdict": score(str(r["field"]), str(r["to_state"]), out),
        })
    return pd.DataFrame(rows)[::-1].reset_index(drop=True)


def scorecard(days: int = 30, horizon: int = HORIZON_MINS,
              threshold: float = THRESHOLD_PTS) -> pd.DataFrame:
    """
    Agreement rate per field, with the sample size beside it.

    Percentages below MIN_SAMPLE are withheld and shown as a raw count.
    Three out of three is not a hundred percent — it is three observations,
    and a figure with a percent sign next to it invites belief that a bare
    count does not. Small samples are exactly where a validation tool does
    its damage, so the number is simply not printed until it means anything.
    """
    df = annotate(days, None, horizon, threshold)
    if df.empty:
        return pd.DataFrame()
    rows = []
    for f, _ in tl.TRACKED:
        sub = df[(df.Field == f) & (df.Verdict != "—")]
        if sub.empty:
            continue
        n = len(sub)
        matched = int((sub.Verdict == "MATCHED").sum())
        opposed = int((sub.Verdict == "OPPOSED").sum())
        rows.append({
            "Field": f, "Graded": n, "Matched": matched, "Opposed": opposed,
            "Agreement": (f"{matched / n * 100:.0f}%" if n >= MIN_SAMPLE
                          else f"{matched}/{n} — too few to rate"),
            "Enough data": "yes" if n >= MIN_SAMPLE else "NO",
        })
    return pd.DataFrame(rows)
