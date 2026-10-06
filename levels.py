"""
Price-structure levels from daily OHLC history.

This is the second axis of analysis. The option chain tells you where money is
POSITIONED. This module tells you where price has actually REACTED. A level
that shows up in both is worth far more than one that shows up in either.

Level types, ordered by how much weight they deserve:

  unfilled_gap   Strongest. Price left a hole and has not returned to it.
                 Unfilled gaps act as magnets and as reaction zones, and
                 there is a mechanical reason: orders that wanted to fill in
                 that range never got the chance.
  swing          Prior highs and lows where price visibly turned. Real
                 memory, real reactions.
  pivot          Classic floor-trader pivots and the Central Pivot Range,
                 from the previous session. A formula, not a reaction point —
                 but an enormous number of intraday traders and algos plot
                 exactly these, and that reflexivity is worth something a
                 level nobody watches does not have. The CPR WIDTH is the
                 genuinely useful part: narrow tends to precede a trending
                 day, wide a sideways one.
  round          Psychological round numbers (24000, 24500). Weak alone but
                 they attract option strikes and stop clusters.
  gann           Included because you asked for it, but be clear-eyed: the
                 Square-of-9 formula (sqrt(P) + deg/180)^2 produces levels at
                 a near-constant PERCENTAGE from spot — roughly 0.32%, 0.65%,
                 0.97%, 1.29% for 45/90/135/180 degrees, at any price. It is
                 a percentage grid, not a discovered level. Weighted lowest
                 for that reason, and easy to switch off in config.

Time cycles are computed and displayed but deliberately never scored. With
one major high and one major low in a year of data, "day 144 from the top"
fires a handful of times and there is no way to tell signal from coincidence.
Look at them; do not trade them.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

import config

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# Types
# ═══════════════════════════════════════════════════════════════════════════

#: how much each source counts toward a confluence score
WEIGHTS = {
    "unfilled_gap": 3.0,
    "ai_quantile":  2.5,
    "swing":        2.0,
    "pivot":        1.5,
    "round":        1.0,
    "gann":         0.5,
}


@dataclass(frozen=True)
class PriceLevel:
    price: float
    kind: str            # support | resistance
    source: str          # unfilled_gap | swing | round | gann
    label: str           # human readable, e.g. "Swing high 12-Aug"

    @property
    def weight(self) -> float:
        return WEIGHTS.get(self.source, 0.5)


# ═══════════════════════════════════════════════════════════════════════════
# Loading
# ═══════════════════════════════════════════════════════════════════════════

def _read_one(path: Path) -> pd.DataFrame:
    """Read a single OHLC CSV, tolerating NSE's export quirks."""
    df = pd.read_csv(path)
    df.columns = [c.strip() for c in df.columns]

    rename = {}
    for want in ("Open", "High", "Low", "Close", "Date"):
        for have in df.columns:
            if have.lower().replace(" ", "") == want.lower():
                rename[have] = want
    df = df.rename(columns=rename)

    missing = {"Date", "Open", "High", "Low", "Close"} - set(df.columns)
    if missing:
        raise ValueError(f"{path.name} is missing columns: {sorted(missing)}")

    # Smart date parsing: if sample starts with 4 digits (YYYY-MM-DD),
    # dayfirst MUST be False, otherwise pandas inverts month and day for dates <= 12!
    sample = str(df["Date"].dropna().iloc[0]) if len(df["Date"].dropna()) else ""
    if "-" in sample and len(sample.split("-")[0]) == 4:
        df["Date"] = pd.to_datetime(df["Date"], format="%Y-%m-%d", errors="coerce")
    else:
        df["Date"] = pd.to_datetime(df["Date"], dayfirst=True, errors="coerce")

    for c in ("Open", "High", "Low", "Close"):
        # Always coerce. Do not gate this on dtype == object: pandas 3 reads
        # text columns as a str dtype, so that check silently skips the
        # cleaning and you end up with '24,106.35' as a string.
        df[c] = pd.to_numeric(
            df[c].astype(str).str.replace(",", "", regex=False).str.strip(),
            errors="coerce")
    return df[["Date", "Open", "High", "Low", "Close"]]


def load_ohlc(folder: Path | str | None = None) -> pd.DataFrame:
    """
    Build the daily series from the OHLC folder, and keep a growing archive.

    The workflow this supports: drop a fresh `OHLC.csv` in whenever you like,
    overwriting yesterday's. Every load merges what it finds with
    `_archive_master.csv` and writes the union back, so history ACCUMULATES
    even though the input file gets replaced.

    That matters more than it sounds. The unfilled-gap tracker walks the
    entire series from the beginning — if your download window rolls forward
    and drops the older months, gaps opened before the window silently vanish
    from the analysis. No error, just quietly worse levels. The archive stops
    that happening.

    On a date collision the freshest source wins, so a corrected re-download
    supersedes what is already archived.
    """
    folder = Path(folder or config.OHLC_DIR)
    if not folder.exists():
        folder.mkdir(parents=True, exist_ok=True)
        raise FileNotFoundError(
            f"OHLC folder was missing, so it has been created: {folder}\n"
            f"Drop your NIFTY daily CSV in as '{config.OHLC_FILE}' and re-run."
        )

    archive = folder / config.OHLC_ARCHIVE_FILE
    incoming = sorted(
        (p for p in folder.glob("*.csv")
         if p.name != archive.name
         and not p.name.endswith("_dhan.csv")
         and not any(x in p.name.lower() for x in ("1m", "5m", "15m", "minute", "candle"))),
        key=lambda p: p.stat().st_mtime
    )

    if not incoming and not archive.exists():
        raise FileNotFoundError(
            f"No CSV files in {folder}\n"
            f"Drop your NIFTY daily OHLC export in as '{config.OHLC_FILE}'."
        )

    # Archive first so that fresher files override it on duplicate dates.
    frames: list[pd.DataFrame] = []
    if archive.exists():
        try:
            frames.append(_read_one(archive))
        except Exception as _ae:
            logging.warning("levels.load_ohlc: archive read error: %s", _ae)
    for f in incoming:
        try:
            frames.append(_read_one(f))
        except Exception as _fe:
            logging.warning("levels.load_ohlc: skipping non-daily or malformed file %s: %s", f.name, _fe)

    out = pd.concat(frames, ignore_index=True).dropna(subset=["Date", "Close"])
    if "partial" in out.columns:
        out = out[~out["partial"].astype(str).str.lower().isin(["true", "1"])]
    out = (out.sort_values("Date")
              .drop_duplicates(subset=["Date"], keep="last")
              .reset_index(drop=True))

    # Sync from local Dhan candles (data/candles/*.csv)
    # If there are completed session files from Dhan, synthesize/reconcile the daily bar
    # and merge it so levels are never stale or corrupted by mid-session partial bars.
    candle_dir = getattr(config, "CANDLE_DIR", None)
    if candle_dir and candle_dir.exists():
        try:
            candle_files = sorted(candle_dir.glob("20*.csv"))
            now_dt = datetime.now()
            today = now_dt.date()
            is_market_open = (now_dt.weekday() < 5 and now_dt.time() < config.MARKET_CLOSE)
            existing_dates = set(out["Date"].dt.date)
            new_bars = []
            updated = False

            for cf in candle_files:
                try:
                    f_date = datetime.strptime(cf.stem[:10], "%Y-%m-%d").date()
                    # Rule: NEVER process today's candle file during active market hours
                    if f_date == today and is_market_open:
                        continue
                    cdf = pd.read_csv(cf)
                    if cdf.empty or "open" not in cdf or "close" not in cdf:
                        continue
                    # A completed trading session has at least 200 bars (375 1m bars for full day)
                    # Never sync a partial or incomplete session (< 200 bars)
                    if len(cdf) < 200:
                        continue

                    c_open = float(cdf["open"].iloc[0])
                    c_high = float(cdf["high"].max())
                    c_low = float(cdf["low"].min())
                    c_close = float(cdf["close"].iloc[-1])

                    if f_date not in existing_dates:
                        new_bars.append({
                            "Date": pd.Timestamp(f_date),
                            "Open": c_open,
                            "High": c_high,
                            "Low": c_low,
                            "Close": c_close,
                        })
                        existing_dates.add(f_date)
                    else:
                        # Reconcile if an existing row was partial or inaccurate
                        mask = out["Date"].dt.date == f_date
                        if mask.any():
                            cur = out.loc[mask].iloc[0]
                            if (abs(cur["Low"] - c_low) > 0.05 or
                                abs(cur["High"] - c_high) > 0.05 or
                                abs(cur["Close"] - c_close) > 0.05):
                                out.loc[mask, "Open"] = c_open
                                out.loc[mask, "High"] = c_high
                                out.loc[mask, "Low"] = c_low
                                out.loc[mask, "Close"] = c_close
                                updated = True
                except Exception as cf_err:
                    log.warning(f"levels.py: could not parse candle file {cf.name}: {cf_err}")
                    continue

            if new_bars:
                out = pd.concat([out, pd.DataFrame(new_bars)], ignore_index=True)
                out = (out.sort_values("Date")
                          .drop_duplicates(subset=["Date"], keep="last")
                          .reset_index(drop=True))
                updated = True

            # Sync any new or corrected rows to OHLC.csv
            if updated:
                ohlc_file = folder / config.OHLC_FILE
                if ohlc_file.exists():
                    try:
                        raw_ohlc = pd.read_csv(ohlc_file)
                        raw_ohlc["Date"] = pd.to_datetime(raw_ohlc["Date"], errors="coerce")
                        combined = pd.concat([raw_ohlc, out], ignore_index=True)
                        combined = combined.sort_values("Date").drop_duplicates(subset=["Date"], keep="last")
                        combined["Date"] = combined["Date"].dt.strftime("%Y-%m-%d")
                        for col in ("Open", "High", "Low", "Close"):
                            combined[col] = combined[col].map(lambda x: f"{float(x):.2f}" if pd.notna(x) else "")
                        combined.to_csv(ohlc_file, index=False)
                    except Exception as o_err:
                        log.warning(f"levels.py: failed to sync corrected bar to {ohlc_file}: {o_err}")
        except Exception as e:
            log.warning(f"levels.py: candle sync failed: {e}")

    # Safety: strictly drop any future dates
    out = out[out["Date"] <= pd.Timestamp.now().normalize()].reset_index(drop=True)

    if getattr(config, "OHLC_KEEP_ARCHIVE", True):
        try:
            arc = out.copy()
            arc["Date"] = arc["Date"].dt.strftime("%d-%b-%Y")
            arc.to_csv(archive, index=False)
        except Exception:                                   # noqa: BLE001
            pass                                            # never let archiving break a run

    return out


def history_health(df: pd.DataFrame) -> list[str]:
    """
    Warnings about the depth and continuity of the loaded history.

    Short or gappy history does not raise an error, it just produces worse
    levels — which is exactly the kind of silent degradation worth surfacing.
    """
    notes: list[str] = []
    if df.empty:
        return ["No usable rows in the OHLC history."]

    days = len(df)
    span = (df.Date.max() - df.Date.min()).days
    if days < 60:
        notes.append(f"Only {days} trading days loaded. Swing levels need ~60+ "
                     f"and the gap tracker improves with a year or more.")
    elif days < 120:
        notes.append(f"{days} trading days loaded — workable, but more history "
                     f"gives better unfilled-gap coverage.")

    # a missing stretch mid-series distorts both swings and gaps
    gaps = df.Date.diff().dt.days.fillna(0)
    big = int((gaps > 7).sum())
    if big:
        notes.append(f"{big} break(s) of more than a week inside the series — "
                     f"gaps and swings across those breaks will be unreliable.")

    stale = (pd.Timestamp.today().normalize() - df.Date.max()).days
    if stale > 5:
        notes.append(f"Most recent bar is {stale} days old ({df.Date.max():%d-%b-%Y}). "
                     f"Pivots and CPR are computed from it, so refresh the file.")
    return notes


# ═══════════════════════════════════════════════════════════════════════════
# Level extraction
# ═══════════════════════════════════════════════════════════════════════════

def swing_levels(df: pd.DataFrame, spot: float, n: int = 4,
                 lookback: int = 120) -> list[PriceLevel]:
    """
    Prior highs above spot and prior lows below it, nearest first.

    Uses a fractal test — a bar whose high exceeds its neighbours on both
    sides — rather than a running maximum, so what comes back is a level
    where price actually turned, not merely the highest print in the window.
    """
    d = df.tail(lookback).reset_index(drop=True)
    out: list[PriceLevel] = []

    highs, lows = [], []
    for i in range(2, len(d) - 2):
        h, lo = d.loc[i, "High"], d.loc[i, "Low"]
        if h > d.loc[i-1, "High"] and h > d.loc[i-2, "High"] \
           and h > d.loc[i+1, "High"] and h > d.loc[i+2, "High"]:
            highs.append((h, d.loc[i, "Date"]))
        if lo < d.loc[i-1, "Low"] and lo < d.loc[i-2, "Low"] \
           and lo < d.loc[i+1, "Low"] and lo < d.loc[i+2, "Low"]:
            lows.append((lo, d.loc[i, "Date"]))

    above = sorted([x for x in highs if x[0] > spot], key=lambda x: x[0])[:n]
    below = sorted([x for x in lows if x[0] < spot], key=lambda x: -x[0])[:n]

    for i, (p, dt) in enumerate(above, 1):
        out.append(PriceLevel(round(p, 2), "resistance", "swing",
                              f"R{i} swing high {dt:%d-%b}"))
    for i, (p, dt) in enumerate(below, 1):
        out.append(PriceLevel(round(p, 2), "support", "swing",
                              f"S{i} swing low {dt:%d-%b}"))
    return out


def unfilled_gaps(df: pd.DataFrame, spot: float,
                  min_points: float = 5.0, n: int = 3) -> list[PriceLevel]:
    """
    Walk the whole history tracking gaps that have never been filled.

    A gap up leaves an unfilled zone at the prior close BELOW the market — it
    becomes support. A gap down leaves one ABOVE — resistance. A gap is
    considered filled the first time price trades back through that close.
    """
    open_gaps: list[dict] = []
    for i in range(1, len(df)):
        o, h, lo = df.loc[i, "Open"], df.loc[i, "High"], df.loc[i, "Low"]
        prev_c = df.loc[i-1, "Close"]
        gap_pts = round(o - prev_c, 2)
        # GAPFIX — FIRST drop gaps today's range filled, THEN add today's gap
        open_gaps = [g for g in open_gaps
                     if not ((g["type"] == "up" and lo <= g["price"])
                             or (g["type"] == "down" and h >= g["price"]))]
        if gap_pts > min_points:
            open_gaps.append({"type": "up", "price": prev_c,
                              "date": df.loc[i, "Date"], "size": gap_pts})
        elif gap_pts < -min_points:
            open_gaps.append({"type": "down", "price": prev_c,
                              "date": df.loc[i, "Date"], "size": -gap_pts})

    out: list[PriceLevel] = []
    above = sorted([g for g in open_gaps if g["price"] > spot],
                   key=lambda g: g["price"])[:n]
    below = sorted([g for g in open_gaps if g["price"] < spot],
                   key=lambda g: -g["price"])[:n]

    for g in above:
        out.append(PriceLevel(round(g["price"], 2), "resistance", "unfilled_gap",
                              f"Unfilled gap {g['date']:%d-%b} ({g['size']:.0f}pt)"))
    for g in below:
        out.append(PriceLevel(round(g["price"], 2), "support", "unfilled_gap",
                              f"Unfilled gap {g['date']:%d-%b} ({g['size']:.0f}pt)"))
    return out


def gann_levels(spot: float) -> list[PriceLevel]:
    """
    Square-of-9 bands. See the module docstring: these sit at a near-constant
    percentage from spot, so treat them as a percentage grid rather than as
    discovered structure. Disabled by setting USE_GANN = False in config.
    """
    if not getattr(config, "USE_GANN", True):
        return []
    out: list[PriceLevel] = []
    root = np.sqrt(spot)
    for deg in (45, 90, 135, 180):
        up = round((root + deg / 180.0) ** 2, 2)
        dn = round((root - deg / 180.0) ** 2, 2)
        pct_u = (up - spot) / spot * 100
        pct_d = (dn - spot) / spot * 100
        out.append(PriceLevel(up, "resistance", "gann", f"Gann {deg}° (+{pct_u:.2f}%)"))
        out.append(PriceLevel(dn, "support", "gann", f"Gann -{deg}° ({pct_d:.2f}%)"))
    return out


def round_levels(spot: float, step: int = 500, n: int = 2) -> list[PriceLevel]:
    """Psychological round numbers. Weak alone, but they cluster stops."""
    base = int(spot // step) * step
    out: list[PriceLevel] = []
    for i in range(1, n + 1):
        out.append(PriceLevel(float(base + i * step), "resistance", "round",
                              f"Round {base + i * step}"))
        out.append(PriceLevel(float(base - (i - 1) * step), "support", "round",
                              f"Round {base - (i - 1) * step}"))
    return [l for l in out if abs(l.price - spot) > 20]


def time_cycles(df: pd.DataFrame, today: date | None = None,
                tolerance: int = 1) -> list[str]:
    """
    Gann day-counts from the major high and major low in the loaded history.

    Displayed, never scored. With one high and one low in the dataset there is
    no way to distinguish a hit from a coincidence, and treating these as
    tradeable would be exactly the kind of unvalidated rule this project
    exists to avoid.
    """
    if df.empty:
        return []
    today = today or date.today()
    cycles = [30, 45, 60, 90, 120, 135, 144, 180, 225, 270, 315, 360]
    hi_date = df.loc[df["High"].idxmax(), "Date"].date()
    lo_date = df.loc[df["Low"].idxmin(), "Date"].date()

    out: list[str] = []
    for label, anchor in (("high", hi_date), ("low", lo_date)):
        days = (today - anchor).days
        if days <= 0:
            continue
        for c in cycles:
            if abs(days - c) <= tolerance:
                out.append(f"Day {days} from major {label} ({anchor:%d-%b}) "
                           f"— near {c}-day cycle")
    return out


def pivot_levels(df: pd.DataFrame, spot: float) -> list[PriceLevel]:
    """
    Classic floor-trader pivots from the PREVIOUS session's high/low/close.

        PP = (H + L + C) / 3
        R1 = 2*PP - L      S1 = 2*PP - H
        R2 = PP + (H - L)  S2 = PP - (H - L)
        R3 = H + 2*(PP-L)  S3 = L - 2*(H-PP)

    These are a formula applied to one day's range, not a place price turned —
    so on evidence they rank below a swing or an unfilled gap. What earns them
    their weight is reflexivity: a very large number of intraday traders and
    algos watch exactly these numbers, which gives them some self-fulfilling
    pull that a level nobody plots does not have. Hence 1.5, between round
    numbers and real swings.
    """
    if not getattr(config, "USE_PIVOTS", True) or len(df) < 2:
        return []

    prev = df.iloc[-1]
    h, l, c = float(prev.High), float(prev.Low), float(prev.Close)
    when = prev.Date.strftime("%d-%b") if hasattr(prev.Date, "strftime") else "prev"

    pp = (h + l + c) / 3.0
    rng = h - l
    pts = {
        "PP":  pp,
        "R1":  2 * pp - l,   "S1": 2 * pp - h,
        "R2":  pp + rng,     "S2": pp - rng,
        "R3":  h + 2 * (pp - l),
        "S3":  l - 2 * (h - pp),
    }

    out: list[PriceLevel] = []
    for name, price in pts.items():
        if not np.isfinite(price):
            continue
        kind = "resistance" if price > spot else "support"
        out.append(PriceLevel(round(price, 2), kind, "pivot",
                              f"Pivot {name} ({when})"))
    return out


def central_pivot_range(df: pd.DataFrame, spot: float
                        ) -> tuple[list[PriceLevel], dict]:
    """
    Central Pivot Range — pivot, plus a top and bottom central line.

        BC = (H + L) / 2
        TC = 2*Pivot - BC

    The levels matter less than the WIDTH. A narrow CPR means yesterday
    closed near the middle of a tight range, and the day that follows tends
    to trend. A wide CPR tends to precede a sideways, rangebound day.

    That is a genuinely useful cross-check on the OI-based regime call: when
    the chain says CONSOLIDATION and the CPR is wide, two independent inputs
    agree you should be sitting on your hands. When the chain says
    consolidation but the CPR is narrow, expect the compression to resolve.
    """
    if not getattr(config, "USE_PIVOTS", True) or len(df) < 2:
        return [], {}

    prev = df.iloc[-1]
    h, l, c = float(prev.High), float(prev.Low), float(prev.Close)
    when = prev.Date.strftime("%d-%b") if hasattr(prev.Date, "strftime") else "prev"

    pivot = (h + l + c) / 3.0
    bc = (h + l) / 2.0
    tc = 2 * pivot - bc
    lo_c, hi_c = min(tc, bc), max(tc, bc)

    width = hi_c - lo_c
    width_pct = width / spot * 100 if spot else 0.0
    if width_pct < config.CPR_NARROW_PCT:
        verdict, hint = "NARROW", "trending day more likely"
    elif width_pct > config.CPR_WIDE_PCT:
        verdict, hint = "WIDE", "rangebound / sideways day more likely"
    else:
        verdict, hint = "NORMAL", "no strong bias from CPR width"

    inside = lo_c <= spot <= hi_c
    levels = [
        PriceLevel(round(hi_c, 2), "resistance" if hi_c > spot else "support",
                   "pivot", f"CPR top ({when})"),
        PriceLevel(round(pivot, 2), "resistance" if pivot > spot else "support",
                   "pivot", f"CPR pivot ({when})"),
        PriceLevel(round(lo_c, 2), "resistance" if lo_c > spot else "support",
                   "pivot", f"CPR bottom ({when})"),
    ]
    meta = {
        "top": round(hi_c, 2), "pivot": round(pivot, 2), "bottom": round(lo_c, 2),
        "width": round(width, 1), "width_pct": round(width_pct, 3),
        "verdict": verdict, "hint": hint, "spot_inside": inside, "date": when,
    }
    return levels, meta


def all_levels(df: pd.DataFrame, spot: float) -> list[PriceLevel]:
    cpr_levels, _ = central_pivot_range(df, spot)
    return (unfilled_gaps(df, spot)
            + swing_levels(df, spot)
            + pivot_levels(df, spot)
            + cpr_levels
            + round_levels(spot)
            + gann_levels(spot))
