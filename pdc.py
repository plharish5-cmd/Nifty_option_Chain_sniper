"""
pdc.py — the last 15-minute candle of each session, as tomorrow's level.

The idea: the 15:15 candle (15:15–15:30, the final one of the day) leaves a
high and a low that the next session often respects. Price sitting above
both treats them as support; sitting below, as resistance.

WHY THIS IS A SEPARATE FILE AND NOT A COLUMN IN OHLC.csv
────────────────────────────────────────────────────────
You suggested adding it to the OHLC sheet. I would not, and the reason is
specific rather than fussy.

OHLC.csv is read by build_master.load_all(), which globs every CSV in
data/ohlc and concatenates them on a fixed set of columns. Adding columns
to one file in that folder risks either being silently dropped or, worse,
shifting the frame that every R1–R4 and S1–S4 level is computed from. That
folder now holds seven years of history assembled from several sources and
it is the single most load-bearing thing in the project.

A separate file costs nothing and cannot break the level engine.

WHAT "PDC" MEANS HERE
─────────────────────
The candle STAMPED 15:15, covering 15:15 to 15:30. If your course means the
15:00 candle instead, change CLOSE_CANDLE below — it is one string, and the
two are different levels on most days.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

import pandas as pd

import config

PATH = config.DATA_DIR / "pdc_levels.csv"

# The candle STAMPED 15:00, covering 15:00–15:15, closing AT 15:15.
#
# Not the 15:15 candle. That one covers 15:15–15:30 and contains the closing
# call auction, so its close is an auction print rather than a normal traded
# price — which is exactly the number a level should not be built on. The
# 15:00 candle is the last clean continuous-session candle of the day.
#
# This was wrong until now: every PDC level recorded before this change came
# from the auction candle and must be rebuilt, not merely appended to.
CLOSE_CANDLE = getattr(config, "PDC_CANDLE", time(15, 0))
COLUMNS = ["session", "close_high", "close_low", "close_close", "source"]
COMPARE_CANDLE = time(15, 15)      # the auction candle, kept for comparison


def load() -> pd.DataFrame:
    if not PATH.exists():
        return pd.DataFrame(columns=COLUMNS)
    try:
        df = pd.read_csv(PATH)
        df["session"] = pd.to_datetime(df["session"]).dt.date
        return df.sort_values("session").reset_index(drop=True)
    except Exception:                                       # noqa: BLE001
        return pd.DataFrame(columns=COLUMNS)


def _extract(bars: pd.DataFrame) -> pd.DataFrame:
    """Pull the close candle out of a frame of 15-minute bars."""
    if bars is None or bars.empty:
        return pd.DataFrame(columns=COLUMNS)
    ts = pd.to_datetime(bars["timestamp"])
    d = bars.copy()
    d["session"] = ts.dt.date
    d["t"] = ts.dt.time
    hit = d[d["t"] == CLOSE_CANDLE]

    # Fall back to the last bar at or before the close candle. A session can
    # end early — muhurat trading, an exchange halt — and skipping those days
    # silently would leave gaps that look like missing data rather than
    # short sessions.
    missing = sorted(set(d["session"]) - set(hit["session"]))
    extra = []
    for s in missing:
        sub = d[(d["session"] == s) & (d["t"] <= CLOSE_CANDLE)]
        if len(sub):
            extra.append(sub.iloc[-1])
    if extra:
        hit = pd.concat([hit, pd.DataFrame(extra)], ignore_index=True)

    out = pd.DataFrame({
        "session": hit["session"],
        "close_high": hit["high"].astype(float).round(2),
        "close_low": hit["low"].astype(float).round(2),
        "close_close": hit["close"].astype(float).round(2),
        "source": "dhan-15m",
    })
    return out.drop_duplicates(subset=["session"]).sort_values("session")


def backfill(since: date | None = None) -> tuple[bool, str]:
    """
    Fetch 15-minute bars from Dhan and record each session's close candle.

    Dhan's intraday window is roughly 90 days, so anything older than that
    cannot be recovered from this route and the file simply starts where the
    data does. That is stated rather than padded: a fabricated level is far
    worse than a missing one, because a gap is visible and a guess is not.
    """
    since = since or date(2026, 8, 3)
    try:
        import dhan_client as dc
    except Exception as exc:                                # noqa: BLE001
        return False, f"dhan_client unavailable: {exc}"

    days = (date.today() - since).days + 2
    if days > 90:
        return False, (f"{since:%d-%b} is {days} days back; Dhan's intraday "
                       f"window is about 90 days. Ask for a later start date "
                       f"or supply the older sessions as a CSV.")
    try:
        bars = dc.intraday(config.DHAN_INDEX_SECURITY_ID, "IDX_I", "INDEX",
                           days_back=days, interval="15")
    except Exception as exc:                                # noqa: BLE001
        return False, f"fetch failed: {exc}"
    if bars is None or bars.empty:
        return False, "Dhan returned no 15-minute bars"

    fresh = _extract(bars)
    fresh = fresh[fresh["session"] >= since]
    if fresh.empty:
        return False, f"no close candles found on or after {since:%d-%b}"

    have = load()
    merged = (pd.concat([have, fresh], ignore_index=True)
              if len(have) else fresh)
    merged["session"] = pd.to_datetime(merged["session"]).dt.date
    merged = (merged.sort_values("session")
              .drop_duplicates(subset=["session"], keep="last")
              .reset_index(drop=True))
    PATH.parent.mkdir(parents=True, exist_ok=True)
    merged.to_csv(PATH, index=False)

    added = len(merged) - len(have)
    gaps = _gaps(merged)
    note = (f"{added} session(s) added, {len(merged)} total, "
            f"{merged['session'].min():%d-%b} to {merged['session'].max():%d-%b}")
    if gaps:
        note += (f" — {len(gaps)} weekday(s) missing: "
                 + ", ".join(f"{g:%d-%b}" for g in gaps[:6])
                 + (" ..." if len(gaps) > 6 else "")
                 + ". Holidays are expected here; anything else is a gap.")
    return True, note


def _gaps(df: pd.DataFrame) -> list:
    """Weekdays inside the covered range with no close candle recorded."""
    if df.empty:
        return []
    have = set(df["session"])
    lo, hi = min(have), max(have)
    out, d = [], lo
    while d <= hi:
        if d.weekday() < 5 and d not in have:
            out.append(d)
        d += timedelta(days=1)
    return out


def previous(for_day: date | None = None) -> dict | None:
    """The most recent close candle STRICTLY BEFORE the given session."""
    df = load()
    if df.empty:
        return None
    for_day = for_day or date.today()
    prior = df[df["session"] < for_day]
    if prior.empty:
        return None
    r = prior.iloc[-1]
    return {"session": r["session"], "high": float(r["close_high"]),
            "low": float(r["close_low"]), "close": float(r["close_close"])}


# ═══════════════════════════════════════════════════════════════════════════
# Proximity
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class Near:
    support: str = ""
    resistance: str = ""


def nearby(price: float, levels: dict, within: float = 25.0) -> Near:
    """
    Which levels sit within `within` points, split into below and above.

    Below price is support, above is resistance — decided by where price
    actually is rather than by what the level is called. A chain "support"
    that price has already broken through is resistance now, and labelling
    it by its origin would have you buying into a wall.

    `levels` maps a label to a price. Anything unusable is skipped rather
    than coerced, because a level at 0.0 would sit permanently below price
    and report as support forever.
    """
    below, above = [], []
    for label, lv in (levels or {}).items():
        try:
            v = float(lv)
        except (TypeError, ValueError):
            continue
        if v <= 0:
            continue
        d = price - v
        if 0 <= d <= within:
            below.append((d, f"{label} {v:,.0f} ({d:.0f}p below)"))
        elif -within <= d < 0:
            above.append((-d, f"{label} {v:,.0f} ({-d:.0f}p above)"))
    below.sort()
    above.sort()
    return Near(" · ".join(x[1] for x in below[:2]),
                " · ".join(x[1] for x in above[:2]))


def pdc_levels(price: float, sessions: int = 4,
               before: date | None = None) -> dict:
    """
    Recent PDC closes, labelled PDC_S* below price and PDC_R* above it.

    The candle is whatever CLOSE_CANDLE says — 15:00 by default, the last
    candle of the continuous session. NOT the 15:15 candle, which carries
    the closing call auction and can print seventy points away from where
    trading actually stopped.

    Four sessions, not six. Six produced a wall of five resistances and one
    support on the screen, which is not a level ladder — it is every recent
    close redrawn as a line, and at that density something is always
    "nearby". Four matches the Chart levels block.

    Only the close is used, per the brief — not the candle's high or low.
    One price per session, so a week of sessions gives a small ladder rather
    than a cloud.

    Numbering is by DISTANCE, not by age: PDC_S1 is the nearest close below
    price, whichever day it came from. That is the ordering that matters
    when you are watching price approach one, and it is the same convention
    S1..S4 already use. The session date travels with each level so you can
    still tell a fresh one from a fortnight-old one.
    """
    df = load()
    if df.empty:
        return {}
    before = before or date.today()
    prior = df[df["session"] < before].tail(sessions)
    if prior.empty:
        return {}

    below, above = [], []
    for _, r in prior.iterrows():
        c = float(r["close_close"])
        (below if c < price else above).append((abs(price - c), c, r["session"]))
    below.sort()
    above.sort()

    out = {}
    for i, (_, c, s) in enumerate(below, 1):
        out[f"PDC_S{i}"] = c
    for i, (_, c, s) in enumerate(above, 1):
        out[f"PDC_R{i}"] = c
    return out


def pdc_levels_dated(price: float, sessions: int = 4,
                     before: date | None = None) -> dict:
    """Same levels, each mapped to (price, session) so the UI can show where
    a line came from. A level with no date is a number you cannot check."""
    df = load()
    if df.empty:
        return {}
    prior = df if before is None else df[df["session"] < before]
    prior = prior.tail(sessions)
    below, above = [], []
    for _, r in prior.iterrows():
        c = float(r["close_close"])
        (below if c < price else above).append((abs(price - c), c, r["session"]))
    below.sort(); above.sort()
    out = {}
    for i, (_, c, s) in enumerate(below, 1):
        out[f"PDC_S{i}"] = (c, s)
    for i, (_, c, s) in enumerate(above, 1):
        out[f"PDC_R{i}"] = (c, s)
    return out


def all_levels(price: float, chain: dict | None = None,
               master: dict | None = None, sessions: int = 6) -> dict:
    """
    Every level, each carrying a label that says where it came from.

      S1_ops / R1_ops   option chain — biggest OI walls, move intraday
      S1..S4 / R1..R4   master levels from daily OHLC — fixed for the day
      PDC_S* / PDC_R*   previous sessions' PDC candle closes

    The naming is the point. A wall the chain built this morning and a swing
    high from three weeks ago behave nothing alike when price reaches them —
    the first can be withdrawn in an hour, the second cannot — so a table
    that shows "S1 24,000" without saying which kind it is has thrown away
    the part you needed to know.
    """
    out: dict[str, float] = {}
    for k, v in (chain or {}).items():
        out[k if k.endswith("_ops") else f"{k}_ops"] = v
    for k, v in (master or {}).items():
        out[k] = v
    out.update(pdc_levels(price, sessions))
    return out


def recent(n: int = 4, before: date | None = None) -> list[tuple]:
    """
    The last `n` sessions' PDC closes, NEWEST FIRST.

    Ordered by recency, not distance — the opposite of pdc_levels(). Both
    orderings are useful and they answer different questions. On a level
    sheet you are marking up a chart and want to know which day each line
    came from, so newest-first with the date attached is what reads
    correctly. In a proximity check you want the nearest line to price
    regardless of age.

    Rolling naturally: a new session appended to the file pushes the oldest
    out of this window without anything needing to be deleted.
    """
    df = load()
    if df.empty:
        return []
    # No date filter by default. An earlier version cut at today+1, which
    # silently dropped the newest session whenever the machine's clock sat
    # behind the data — and the symptom was the exact thing this function
    # exists to do: a new session failed to push the oldest out, while the
    # output still looked entirely reasonable.
    #
    # Today's own PDC close is a completed candle and belongs at PDC1 as
    # soon as it is recorded. Pass `before` explicitly if you need the
    # levels as they stood on some earlier morning.
    prior = df if before is None else df[df["session"] < before]
    prior = prior.tail(n)
    return [(r["session"], float(r["close_close"]))
            for _, r in prior.iloc[::-1].iterrows()]


def format_copy_paste(n: int = 4) -> str:
    """PDC block in the same layout as the resistance and support blocks."""
    rows = recent(n)
    if not rows:
        return ""
    out = [f"🟣 PDC — {CLOSE_CANDLE:%H:%M} candle close (Copy-Paste)"]
    out.append(" ".join(f"{s:%d-%m-%Y} C PDC{i} - {p:.2f}"
                        for i, (s, p) in enumerate(rows, 1)))
    return "\n".join(out)


def compare_candles(days_back: int = 12) -> pd.DataFrame:
    """
    Both candidate candles side by side, so the definition can be checked.

    Prints the 15:00 close (continuous session) against the 15:15 close
    (auction). If they differ by much, the auction moved the print — which
    is the whole reason for preferring the earlier one.

    Verify a couple of rows against your own chart before trusting the
    rebuild. I have been wrong about which candle this is once already.
    """
    import dhan_client as dc
    bars = dc.intraday(config.DHAN_INDEX_SECURITY_ID, "IDX_I", "INDEX",
                       days_back=min(days_back + 2, 90), interval="15")
    if bars is None or bars.empty:
        return pd.DataFrame()
    ts = pd.to_datetime(bars["timestamp"])
    d = bars.copy()
    d["session"] = ts.dt.date
    d["t"] = ts.dt.time

    rows = []
    for s in sorted(set(d["session"]))[-days_back:]:
        sub = d[d["session"] == s]
        a = sub[sub["t"] == time(15, 0)]
        b = sub[sub["t"] == time(15, 15)]
        rows.append({
            "session": s,
            "15:00 close": round(float(a["close"].iloc[0]), 2) if len(a) else None,
            "15:15 close": round(float(b["close"].iloc[0]), 2) if len(b) else None,
        })
    out = pd.DataFrame(rows)
    if len(out) and out["15:00 close"].notna().any():
        out["auction moved it"] = (out["15:15 close"] - out["15:00 close"]).round(2)
    return out


def rebuild(since: date | None = None) -> tuple[bool, str]:
    """
    Delete and refetch every PDC level.

    A plain backfill would only ADD sessions, leaving rows recorded under
    the old 15:15 definition sitting in the file next to correctly-built
    ones — a mixed file that looks fine and is wrong in places you cannot
    see. Changing the definition means starting again.
    """
    if PATH.exists():
        PATH.unlink()
    return backfill(since or date(2026, 8, 3))
