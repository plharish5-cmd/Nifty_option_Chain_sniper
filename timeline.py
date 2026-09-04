"""
timeline.py — a one-day record of every state change the app announced.

The app says a great many things over a session. It says them one poll at a
time, each reading replacing the last, and by 15:30 there is no record of
what it claimed at 10:15 or when it changed its mind. That makes the app
unfalsifiable: it can be wrong all day and still look reasonable at close,
because only the final reading survives.

This module keeps the receipts. Every time a tracked field changes value it
writes one row: the time, what it changed from, what it changed to, and the
spot price at that moment. After the close you open the chart, find those
timestamps, and see for yourself whether the call was any good.

Two design choices worth stating:

  Only changes are written, never every poll. A field that holds BULLISH for
  four hours is one row, not eighty.

  Flips are recorded, not smoothed. If a field oscillates between two values
  on alternate polls, that is not noise to be hidden — it is the app telling
  you it has no real read, and it belongs in the record. The flip count at
  the top of the tab is there to make that visible.

The file is keyed by date, so each session starts a fresh table with no
carry-over. Yesterday's file stays on disk for later comparison but is never
shown.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import pandas as pd

import config

DIR = config.DATA_DIR / "timeline"

COLUMNS = ["time", "field", "from_state", "to_state", "spot", "held_mins",
           "note"]

# The fields worth a row of their own. Order here is the order shown.
TRACKED = [
    ("Regime",      "day regime — the headline read"),
    ("Bias",        "direction the evidence leans"),
    ("Structure",   "swing structure: uptrend, downtrend or neither"),
    ("Breakout",    "breakout quality at the nearest level"),
    ("Range",       "range-bound / range trap state"),
    ("Pinning",     "whether price is being pinned to a strike"),
    ("Agreement",   "spot direction against chain positioning"),
    ("Actionable",  "the app's actionability verdict"),
]

_LABELS = {f for f, _ in TRACKED}


def _path(day: date | None = None) -> Path:
    day = day or datetime.now().date()
    return DIR / f"{day.isoformat()}.csv"


def load_today(day: date | None = None) -> pd.DataFrame:
    """Today's transitions, oldest first. Empty frame before the first one."""
    p = _path(day)
    if not p.exists():
        return pd.DataFrame(columns=COLUMNS)
    try:
        df = pd.read_csv(p)
    except Exception:                                       # noqa: BLE001
        return pd.DataFrame(columns=COLUMNS)
    for c in COLUMNS:
        if c not in df.columns:
            df[c] = None
    return df[COLUMNS]


def _last_state(df: pd.DataFrame, field: str) -> tuple[str | None, datetime | None]:
    """Most recent value of a field today, and when it was set."""
    if df.empty:
        return None, None
    rows = df[df.field == field]
    if rows.empty:
        return None, None
    r = rows.iloc[-1]
    try:
        when = datetime.fromisoformat(str(r["time"]))
    except Exception:                                       # noqa: BLE001
        when = None
    return str(r["to_state"]), when


@dataclass
class Transition:
    time: datetime
    field: str
    from_state: str
    to_state: str
    spot: float
    held_mins: float
    note: str = ""

    @property
    def is_open(self) -> bool:
        """First sighting of a field, as opposed to a genuine change."""
        return self.from_state in ("", "—", "OPEN")


def record(states: dict[str, str], now: datetime, spot: float,
           day: date | None = None) -> list[Transition]:
    """
    Compare the current reading against the last one and write the changes.

    `states` maps a field name from TRACKED to its current value. Fields that
    are absent or None are skipped entirely rather than logged as UNKNOWN —
    a detector that could not run is not the same as one that ran and found
    nothing, and collapsing the two would put noise in a record whose whole
    purpose is to be checked.
    """
    DIR.mkdir(parents=True, exist_ok=True)
    prune(config.TIMELINE_KEEP_DAYS)     # roll the window as each day starts
    existing = load_today(day)
    out: list[Transition] = []
    rows = []

    for field, _desc in TRACKED:
        new = states.get(field)
        if new is None:
            continue
        new = str(new).strip()
        if not new or new.upper() in ("NONE", "NAN"):
            continue

        prev, since = _last_state(existing, field)
        if prev == new:
            continue

        held = 0.0
        if since is not None:
            held = max((now - since).total_seconds() / 60.0, 0.0)

        t = Transition(now, field, prev or "OPEN", new, float(spot),
                       round(held, 1),
                       "first reading" if prev is None else "")
        out.append(t)
        rows.append({"time": now.isoformat(timespec="seconds"),
                     "field": field, "from_state": t.from_state,
                     "to_state": new, "spot": round(float(spot), 2),
                     "held_mins": t.held_mins, "note": t.note})

    if rows:
        p = _path(day)
        pd.DataFrame(rows).to_csv(p, mode="a", header=not p.exists(),
                                  index=False)
    return out


def table(day: date | None = None) -> pd.DataFrame:
    """Today's log formatted for display — newest first, tidy columns."""
    df = load_today(day)
    if df.empty:
        return df
    df = df.copy()
    df["Time"] = pd.to_datetime(df["time"], errors="coerce").dt.strftime("%H:%M")
    df["Field"] = df["field"]
    df["Changed"] = df.apply(
        lambda r: f"{r['from_state']} → {r['to_state']}"
        if str(r["from_state"]) != "OPEN" else f"opened at {r['to_state']}",
        axis=1)
    df["Spot"] = pd.to_numeric(df["spot"], errors="coerce").round(1)
    df["Previous held"] = pd.to_numeric(df["held_mins"], errors="coerce").apply(
        lambda m: "—" if pd.isna(m) or m <= 0 else f"{m:.0f}m")
    return df[["Time", "Field", "Changed", "Spot", "Previous held"]][::-1]


def summary(day: date | None = None) -> dict:
    """
    How busy the day was, per field.

    Flip count is the number the app is being judged on as much as any
    verdict. A field that changed twice described a market that turned twice.
    A field that changed twenty times described nothing and was reacting to
    poll-to-poll noise; the honest reading of a high count is that the field
    was not usable today, and that is worth knowing before the next session
    rather than after it.
    """
    df = load_today(day)
    if df.empty:
        return {"total": 0, "fields": {}, "first": None, "last": None}

    times = pd.to_datetime(df["time"], errors="coerce")
    fields = {}
    for field, _ in TRACKED:
        sub = df[df.field == field]
        if sub.empty:
            continue
        changes = len(sub[sub.from_state != "OPEN"])
        held = pd.to_numeric(sub["held_mins"], errors="coerce")
        fields[field] = {
            "changes": changes,
            "current": str(sub.iloc[-1]["to_state"]),
            "longest_hold": float(held.max()) if held.notna().any() else 0.0,
            "restless": changes >= 8,
        }
    return {
        "total": int((df.from_state != "OPEN").sum()),
        "fields": fields,
        "first": times.min().strftime("%H:%M") if times.notna().any() else None,
        "last": times.max().strftime("%H:%M") if times.notna().any() else None,
    }


def prune(keep: int = 30) -> int:
    """
    Keep the newest `keep` SESSIONS and delete the rest.

    Sessions, deliberately, not calendar days. Thirty calendar days spans
    roughly twenty-one trading days once weekends and holidays are removed,
    and more still if you skip a session — so pruning by age would quietly
    hand you two-thirds of the history you asked for. Counting files means
    thirty means thirty, however long they took to accumulate.
    """
    if not DIR.exists():
        return 0
    dated = []
    for f in DIR.glob("*.csv"):
        try:
            dated.append((date.fromisoformat(f.stem), f))
        except ValueError:
            continue
    dated.sort(reverse=True)
    gone = 0
    for _, f in dated[keep:]:
        f.unlink()
        gone += 1
    return gone


# ═══════════════════════════════════════════════════════════════════════════
# The rolling window — one day is not enough to judge anything
# ═══════════════════════════════════════════════════════════════════════════
#
# A single session tells you whether the app was right once. That is close to
# worthless: any read is right some days. What matters is whether a field is
# right often enough to act on, and that question needs weeks, not hours.
#
# So the log is kept as a rolling thirty sessions. The newest day is added,
# the thirty-first oldest drops off, and nothing needs remembering on your
# part. Miss a review for a week and the week is still there.

def available_days(limit: int = 30) -> list[date]:
    """Dates with a timeline on disk, newest first."""
    if not DIR.exists():
        return []
    days = []
    for f in DIR.glob("*.csv"):
        try:
            days.append(date.fromisoformat(f.stem))
        except ValueError:
            continue
    return sorted(days, reverse=True)[:limit]


def load_range(days: int = 30, end: date | None = None) -> pd.DataFrame:
    """
    Every transition across the last `days` sessions, oldest first.

    Sessions are counted as files that exist, not as calendar days, so
    weekends and holidays do not eat into the window. Thirty here means
    thirty trading days you actually ran the app.
    """
    end = end or datetime.now().date()
    frames = []
    for d in [x for x in available_days(limit=10_000) if x <= end][:days]:
        df = load_today(d)
        if df.empty:
            continue
        df = df.copy()
        df["day"] = d.isoformat()
        frames.append(df)
    if not frames:
        return pd.DataFrame(columns=COLUMNS + ["day"])
    out = pd.concat(frames, ignore_index=True)
    return out.sort_values("time").reset_index(drop=True)


def table_range(days: int = 30, field: str | None = None,
                end: date | None = None) -> pd.DataFrame:
    """The rolling log formatted for display — newest first, with the date."""
    df = load_range(days, end)
    if df.empty:
        return pd.DataFrame(columns=["Date", "Time", "Field", "Changed",
                                     "Spot", "Previous held"])
    if field and field != "All fields":
        df = df[df.field == field]
    if df.empty:
        return pd.DataFrame(columns=["Date", "Time", "Field", "Changed",
                                     "Spot", "Previous held"])
    ts = pd.to_datetime(df["time"], errors="coerce")
    df = df.assign(
        Date=ts.dt.strftime("%d %b"),
        Time=ts.dt.strftime("%H:%M"),
        Field=df["field"],
        Changed=[f"{a} → {b}" if str(a) != "OPEN" else f"opened at {b}"
                 for a, b in zip(df["from_state"], df["to_state"])],
        Spot=pd.to_numeric(df["spot"], errors="coerce").round(1),
    )
    df["Previous held"] = pd.to_numeric(df["held_mins"],
                                        errors="coerce").apply(
        lambda m: "—" if pd.isna(m) or m <= 0 else f"{m:.0f}m")
    return df[["Date", "Time", "Field", "Changed", "Spot",
               "Previous held"]][::-1].reset_index(drop=True)


def by_day(days: int = 30, end: date | None = None) -> pd.DataFrame:
    """
    One row per session — the view for spotting a pattern across weeks.

    `Regimes` is the sequence the day actually took, which is the thing to
    read against a daily chart. `Changes` is the flip count: a day in the
    twenties was a day the app spent reacting to noise, whatever it happened
    to conclude by the close.
    """
    df = load_range(days, end)
    if df.empty:
        return pd.DataFrame(columns=["Date", "Changes", "Regimes",
                                     "Closed on", "Spot range"])
    rows = []
    for d, g in df.groupby("day"):
        regs = g[g.field == "Regime"]["to_state"].tolist()
        seq = []
        for r in regs:
            if not seq or seq[-1] != r:
                seq.append(str(r))
        spots = pd.to_numeric(g["spot"], errors="coerce").dropna()
        bias = g[g.field == "Bias"]
        rows.append({
            "Date": datetime.fromisoformat(d).strftime("%a %d %b"),
            "Changes": int((g.from_state != "OPEN").sum()),
            "Regimes": " → ".join(seq) if seq else "—",
            "Closed on": str(bias.iloc[-1]["to_state"]) if len(bias) else "—",
            "Spot range": (f"{spots.min():.0f}–{spots.max():.0f}"
                           if len(spots) else "—"),
        })
    return pd.DataFrame(rows)[::-1].reset_index(drop=True)


def field_stats(days: int = 30, end: date | None = None) -> pd.DataFrame:
    """
    Per-field behaviour across the window.

    This is the table that decides which fields survive. A field averaging
    two or three changes a session is describing a market. One averaging
    fifteen is describing its own instability, and no amount of chart
    comparison will rescue it — the honest response is to stop reading it.

    Note what this does NOT claim: nothing here says a field was correct.
    Steadiness is necessary for a read to be usable, not sufficient. A field
    that holds one value all month is perfectly steady and may be perfectly
    useless. Only your chart comparison settles that.
    """
    df = load_range(days, end)
    if df.empty:
        return pd.DataFrame(columns=["Field", "Sessions", "Changes",
                                     "Avg / day", "Steadiness"])
    rows = []
    for f, _ in TRACKED:
        sub = df[df.field == f]
        if sub.empty:
            continue
        sessions = sub["day"].nunique()
        changes = int((sub.from_state != "OPEN").sum())
        avg = changes / sessions if sessions else 0.0
        rows.append({
            "Field": f,
            "Sessions": sessions,
            "Changes": changes,
            "Avg / day": round(avg, 1),
            "Steadiness": ("steady" if avg <= 4 else
                           "busy" if avg <= 8 else "RESTLESS"),
        })
    return pd.DataFrame(rows)
