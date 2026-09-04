"""
institutional.py — heavyweight attribution, net GEX, straddle decay.

Three additions that were proposed together. They are unrelated to each
other and share only this file, so read the one you need.

Each carries a specific reason to distrust it, stated in its own section
rather than collected at the bottom where nobody reads them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time

import numpy as np
import pandas as pd

import config


# ═══════════════════════════════════════════════════════════════════════════
# 1 · Heavyweight point attribution
# ═══════════════════════════════════════════════════════════════════════════
#
# NIFTY is market-cap weighted, so a handful of stocks move most of it. If
# the index is pressing R1 while the two largest constituents are at day
# lows, the index is being carried by names that cannot carry it far.
#
# THE WEAKNESS, AND IT IS REAL: these weights are hardcoded. NSE rebalances
# semi-annually and publishes weights in a factsheet, not an API. A weight
# that has drifted produces an attribution that looks precise and is wrong,
# and nothing in the app can detect that — the number will not misbehave, it
# will just quietly stop being true.
#
# So the date they were entered travels with them, and the app says how old
# they are. Re-check against NSE's factsheet each quarter.

WEIGHTS_AS_OF = "2026-06-30"
HEAVYWEIGHTS = {
    "HDFCBANK":  {"weight": 13.3, "name": "HDFC Bank"},
    "RELIANCE":  {"weight":  9.1, "name": "Reliance"},
    "ICICIBANK": {"weight":  8.7, "name": "ICICI Bank"},
    "INFY":      {"weight":  5.2, "name": "Infosys"},
    "TCS":       {"weight":  3.9, "name": "TCS"},
}


@dataclass
class Contribution:
    symbol: str
    name: str
    weight: float
    pct_change: float
    points: float

    @property
    def helping(self) -> bool:
        return self.points > 0


def attribution(quotes: dict, spot: float) -> dict:
    """
    Index points contributed by each heavyweight.

        points = weight% / 100 x stock% change x index level

    `quotes` maps symbol to percentage change. Missing symbols are dropped
    and reported rather than treated as zero — a stock with no quote and a
    stock that is flat contribute the same number and mean opposite things.
    """
    rows, missing = [], []
    for sym, meta in HEAVYWEIGHTS.items():
        pct = quotes.get(sym)
        if pct is None:
            missing.append(meta["name"])
            continue
        pts = meta["weight"] / 100.0 * float(pct) / 100.0 * spot
        rows.append(Contribution(sym, meta["name"], meta["weight"],
                                 float(pct), pts))
    rows.sort(key=lambda r: r.points)

    total = sum(r.points for r in rows)
    up = [r for r in rows if r.helping]
    dn = [r for r in rows if not r.helping]
    covered = sum(HEAVYWEIGHTS[r.symbol]["weight"] for r in rows)

    return {
        "rows": rows, "total_points": total, "missing": missing,
        "weight_covered": covered, "as_of": WEIGHTS_AS_OF,
        "split": f"{len(up)} lifting / {len(dn)} dragging",
        "note": _attr_note(rows, up, dn, total),
    }


def _attr_note(rows, up, dn, total) -> str:
    if not rows:
        return "no heavyweight quotes available"
    if len(up) >= 4:
        return (f"Four or five of the top five are lifting, worth "
                f"{total:+.0f} index points between them. The move has its "
                f"leaders behind it.")
    if len(dn) >= 4:
        return (f"Four or five are dragging, {total:+.0f} points. Any rally "
                f"here is being carried by the other forty-five names.")
    biggest = max(rows, key=lambda r: abs(r.points))
    return (f"Split {len(up)}/{len(dn)}, net {total:+.0f} points. "
            f"{biggest.name} alone is {biggest.points:+.0f}. A breakout with "
            f"the two largest names disagreeing is the case worth being "
            f"careful about.")


# ═══════════════════════════════════════════════════════════════════════════
# 2 · Net gamma exposure
# ═══════════════════════════════════════════════════════════════════════════
#
# READ THIS BEFORE USING THE NUMBER.
#
# GEX rests on an assumption about WHO HOLDS WHAT, and that assumption is
# imported from US equity markets. There, dealers are reliably short calls
# and long puts, so their delta hedging suppresses volatility above the flip
# level and amplifies it below.
#
# In NIFTY weeklies the positioning may be the other way round. Retail buys
# options heavily here; institutions write them. If dealers are net LONG
# gamma where the US model says SHORT, every reading inverts: the app would
# tell you to fade a breakout precisely when it is about to run.
#
# I have no evidence either way, so the sign is a setting rather than a
# decision. `GEX_DEALER_SIGN = -1` is the US convention. Flip it to +1 if
# you establish that Indian positioning runs the other way. Until you have
# evidence, treat the regime label as a hypothesis you are testing in the
# Timeline log, not as a fact about the market.

def net_gex(df: pd.DataFrame, spot: float, sign: int | None = None,
            band: float | None = None) -> dict:
    """
    Cumulative gamma exposure and the level where it flips.

    Requires ce_gamma and pe_gamma, which greeks.add_greeks attaches. Strikes
    whose gamma could not be computed (unusable IV) are skipped rather than
    treated as zero gamma — zero is a real value meaning "no convexity" and
    would drag the total toward a flip point that does not exist.
    """
    sign = sign if sign is not None else getattr(config, "GEX_DEALER_SIGN", -1)
    need = {"ce_gamma", "pe_gamma", "ce_oi", "pe_oi"}
    if df is None or df.empty or not need.issubset(df.columns):
        return {"known": False,
                "note": "gamma not attached — run greeks.add_greeks first"}

    # Only strikes near the money.
    #
    # Deep OTM strikes trade at five paise with a spread wider than the
    # price, so the IV solver fails on them and they were being reported as
    # "46 strikes skipped" every poll. They also contribute almost nothing:
    # gamma is a bell centred on spot and is negligible ten strikes out, so
    # the total barely moves while the warning implies something is wrong.
    #
    # Narrowing does shift the flip level slightly, because excluded strikes
    # carried real if tiny weight. That is a trade of a little accuracy for
    # a lot less noise, and it is the right trade only because the excluded
    # contributions are small — widen `band` if you ever want to check.
    band = band if band is not None else getattr(config, "GEX_BAND_PTS", 500)
    near = df[(df["strike"] >= spot - band) & (df["strike"] <= spot + band)]
    d = near.dropna(subset=["ce_gamma", "pe_gamma"]).copy()
    excluded = len(df) - len(near)
    if d.empty:
        return {"known": False,
                "note": "no strike had a usable IV, so no gamma was computed"}

    unit = spot * spot * 0.01
    d["ce_gex"] = d["ce_gamma"] * d["ce_oi"] * unit
    d["pe_gex"] = d["pe_gamma"] * d["pe_oi"] * unit
    d["net"] = sign * (d["ce_gex"] - d["pe_gex"])
    total = float(d["net"].sum())

    # the flip is where cumulative net gamma crosses zero across strikes
    d = d.sort_values("strike")
    cum = d["net"].cumsum().to_numpy()
    ks = d["strike"].to_numpy(float)
    flip = None
    for i in range(1, len(cum)):
        if cum[i - 1] == 0 or np.sign(cum[i]) != np.sign(cum[i - 1]):
            a, b = cum[i - 1], cum[i]
            if b != a:
                flip = float(ks[i - 1] + (ks[i] - ks[i - 1]) * (-a) / (b - a))
            else:
                flip = float(ks[i])
            break

    positive = total > 0
    return {
        "known": True, "total": total, "flip": flip,
        "regime": "POSITIVE — mean reverting" if positive
                  else "NEGATIVE — trending",
        "skipped": int(len(near) - len(d)),
        "band": band, "outside_band": int(excluded),
        "sign": sign,
        "note": (
            ("Dealers net long gamma on this convention: they sell into "
             "rallies and buy dips, which suppresses range and favours fade "
             "setups."
             if positive else
             "Dealers net short gamma on this convention: hedging amplifies "
             "moves, so breakouts run further and reversals are violent.")
            + (f" Flip level {flip:,.0f}, {flip - spot:+,.0f} from spot."
               if flip else " No flip level inside the strikes available.")
            + " UNVERIFIED: the dealer-positioning assumption behind this is "
              "American. If Indian positioning is the reverse, so is this "
              "reading."),
    }


# ═══════════════════════════════════════════════════════════════════════════
# 3 · Straddle decay schedule
# ═══════════════════════════════════════════════════════════════════════════
#
# A straddle should shed premium through the day at a rate set by theta. If
# it has not, sellers are refusing to discount, which usually means they
# expect something. If it has shed far more, they are dumping risk.
#
# TWO WEAKNESSES, THE SECOND ONE SERIOUS.
#
# First: the square-root-of-time curve is a convention, not a law. Intraday
# theta is neither linear nor exactly root-time, so the gap is soft. Read
# large divergences and ignore small ones.
#
# Second, and this changes how you read the output: THE CURVE IS EXTREMELY
# SENSITIVE TO DAYS-TO-EXPIRY, and getting it wrong biases every reading the
# same way. Measured on identical prices:
#
#     dte=1   10:30 ON SCHEDULE  13:30 UNDER  15:00 UNDER
#     dte=3   10:30 ON SCHEDULE  13:30 ON SCH 15:00 OVER
#     dte=5   10:30 ON SCHEDULE  13:30 OVER   15:00 OVER
#
# Same straddle, same prices, opposite conclusions. Pass too FEW days and
# everything reads UNDER-DECAYING; too many and everything reads OVER. The
# afternoon readings are where it bites, because that is where the curves
# separate.
#
# So `expiry_days` must be the real figure, and on expiry day itself the
# root-time model understates how sharply premium collapses into the close.
# Treat the final hour on expiry day as uninformative rather than as a
# strong UNDER-DECAYING signal.

@dataclass
class Decay:
    at: datetime
    actual: float
    theoretical: float
    open_premium: float
    minutes_elapsed: float
    minutes_total: float

    @property
    def gap(self) -> float:
        return self.actual - self.theoretical

    @property
    def gap_pct(self) -> float:
        return (self.gap / self.theoretical * 100
                if self.theoretical else 0.0)

    dte: float = 1.0

    @property
    def band(self) -> float:
        b = 8.0
        if self.dte <= 1.0:
            b = 14.0
        return b

    @property
    def unreliable(self) -> bool:
        """
        The last fifth of expiry day, where this model stops working.

        Root-time sends theoretical value toward zero as time runs out,
        while a real straddle holds intrinsic value and does not. At 15:00
        on expiry the curve predicted 71 against an actual 190 — a +169%
        gap that says nothing about sellers and everything about the model.
        Widening the band was the first fix and it was wrong: a threshold
        cannot repair a curve that has gone vertical. So the reading is
        withheld instead.
        """
        if self.dte > 1.0 or not self.minutes_total:
            return False
        return self.minutes_elapsed / self.minutes_total > 0.75

    @property
    def state(self) -> str:
        if self.unreliable:
            return "MODEL UNRELIABLE"
        if abs(self.gap_pct) < self.band:
            return "ON SCHEDULE"
        return "UNDER-DECAYING" if self.gap > 0 else "OVER-DECAYING"

    @property
    def burn_actual(self) -> float | None:
        """Points shed per hour so far, measured."""
        hrs = self.minutes_elapsed / 60.0
        if hrs < 0.25:
            return None
        return (self.actual - self.open_premium) / hrs

    @property
    def burn_expected(self) -> float | None:
        """Points per hour the curve says it should be shedding."""
        hrs = self.minutes_elapsed / 60.0
        if hrs < 0.25:
            return None
        return (self.theoretical - self.open_premium) / hrs

    @property
    def burn_line(self) -> str:
        a, e = self.burn_actual, self.burn_expected
        if a is None or e is None:
            return "too early to measure a burn rate"
        return (f"{a:+.1f} pts/hr actual against {e:+.1f} expected"
                + ("" if abs(a - e) < 0.8 else
                   " — premium holding up" if a > e else
                   " — melting faster than theta explains"))

    @property
    def note(self) -> str:
        if self.state == "MODEL UNRELIABLE":
            return ("Late on expiry day the root-time curve collapses toward "
                    "zero while the straddle keeps its intrinsic value, so "
                    "the comparison stops meaning anything. No reading given "
                    "— this is a limit of the model, not a market condition.")
        if self.state == "ON SCHEDULE":
            return (f"Straddle {self.actual:.0f} against {self.theoretical:.0f} "
                    f"expected — decaying about as it should.")
        if self.state == "UNDER-DECAYING":
            return (f"Straddle {self.actual:.0f} is {self.gap_pct:+.0f}% above "
                    f"the {self.theoretical:.0f} expected. Sellers are not "
                    f"discounting premium — they are pricing something they "
                    f"expect to happen. Often precedes an expansion in range.")
        return (f"Straddle {self.actual:.0f} is {self.gap_pct:+.0f}% below the "
                f"{self.theoretical:.0f} expected. Premium melting faster "
                f"than theta explains — writers confident the range holds.")


def decay_curve(open_premium: float, actual: float, now: datetime,
                expiry_days: float = 1.0) -> Decay | None:
    """
    Where the straddle should be by now, against where it is.

    Theoretical value follows sqrt(time remaining) — the standard shorthand
    for option time value, and shorthand is what it is. On expiry day the
    real curve steepens sharply into the close in a way root-time
    understates, so treat the final hour's readings with more suspicion than
    the morning's.
    """
    if not open_premium or open_premium <= 0:
        return None
    o = datetime.combine(now.date(), config.MARKET_OPEN)
    c = datetime.combine(now.date(), config.MARKET_CLOSE)
    total = (c - o).total_seconds() / 60.0
    elapsed = max(min((now - o).total_seconds() / 60.0, total), 0.0)

    day_frac = elapsed / total if total else 0.0
    left = max(expiry_days - day_frac, 0.0001)
    theo = open_premium * np.sqrt(left / expiry_days)

    return Decay(now, float(actual), float(theo), float(open_premium),
                 elapsed, total, float(expiry_days))


# ═══════════════════════════════════════════════════════════════════════════
# Data fetchers
# ═══════════════════════════════════════════════════════════════════════════

_hw_cache: dict = {}


def quotes_from_nse(ad: dict) -> tuple[dict, list]:
    """
    Heavyweight percentage changes from NSE's constituent payload.

    Preferred over the Dhan route below, for three reasons that all point
    the same way:

      ONE request instead of five, and the app already makes it for
      advance/decline. The marginal cost is zero.

      All fifty names arrive, not five. The five here are hardcoded because
      the WEIGHTS are hardcoded, but the data no longer limits that — add a
      sixth name to HEAVYWEIGHTS and it works with no extra fetching.

      Same source as the index level itself, so the percentages and the
      spot they are applied to come from one snapshot. The Dhan route mixed
      NSE's index with Dhan's equity feed, which can differ by a tick at the
      moment you read them.

    Returns empty when the payload has no constituent block, so the caller
    can fall back rather than showing zeros.
    """
    stocks = (ad or {}).get("stocks") or {}
    if not stocks:
        return {}, ["NSE payload carried no constituent list"]
    out, missing = {}, []
    for sym in HEAVYWEIGHTS:
        if sym in stocks:
            out[sym] = stocks[sym]
        else:
            missing.append(sym)
    return out, ([f"not in payload: {', '.join(missing)}"] if missing else [])


def heavyweight_quotes(max_age_s: int = 300) -> tuple[dict, list]:
    """
    Percentage change today for the five heavyweights, from Dhan.

    One intraday call per stock, cached for five minutes. Five calls every
    three-minute poll would be 800 a session for a number that moves slowly;
    five minutes is frequent enough to catch a heavyweight rolling over and
    infrequent enough to be free.

    Percentage change is computed against the PREVIOUS SESSION'S close, not
    against today's open. A stock that gapped down and then rallied is still
    dragging the index — measuring from the open would hide exactly the case
    this feature exists to catch.
    """
    now = datetime.now()
    hit = _hw_cache.get("at")
    if hit and (now - hit).total_seconds() < max_age_s:
        return _hw_cache.get("quotes", {}), _hw_cache.get("errors", [])

    quotes, errors = {}, []
    try:
        import dhan_client as dc
    except Exception as exc:                                # noqa: BLE001
        return {}, [f"dhan_client unavailable: {exc}"]

    ids = _hw_cache.get("ids")
    if ids is None:
        try:
            ids = _resolve_equities(dc)
            _hw_cache["ids"] = ids
        except Exception as exc:                            # noqa: BLE001
            return {}, [f"could not resolve equity ids: {exc}"]

    for sym, sid in ids.items():
        try:
            d = dc.intraday(sid, "NSE_EQ", "EQUITY", days_back=3,
                            interval="25")
        except Exception as exc:                            # noqa: BLE001
            errors.append(f"{sym}: {str(exc)[:50]}")
            continue
        if d is None or d.empty:
            errors.append(f"{sym}: no candles")
            continue
        ts = pd.to_datetime(d["timestamp"])
        d = d[(ts.dt.time >= config.MARKET_OPEN)
              & (ts.dt.time <= config.MARKET_CLOSE)].copy()
        if d.empty:
            errors.append(f"{sym}: no in-hours candles")
            continue
        d["day"] = pd.to_datetime(d["timestamp"]).dt.date
        days = sorted(set(d["day"]))
        if len(days) < 2:
            errors.append(f"{sym}: only one session, no previous close")
            continue
        prev = float(d[d["day"] == days[-2]]["close"].iloc[-1])
        last = float(d[d["day"] == days[-1]]["close"].iloc[-1])
        if prev > 0:
            quotes[sym] = (last - prev) / prev * 100.0

    _hw_cache.update(at=now, quotes=quotes, errors=errors)
    return quotes, errors


def _resolve_equities(dc) -> dict:
    """Security ids for the five heavyweights, from the scrip master."""
    df = pd.read_csv(dc.SCRIP_MASTER, low_memory=False)
    cols = {c.upper().replace(" ", "_"): c for c in df.columns}

    def col(*names):
        for n in names:
            if n in cols:
                return cols[n]
        return None

    seg = col("SEM_EXM_EXCH_ID", "EXCH_ID")
    sym = col("SEM_TRADING_SYMBOL", "TRADING_SYMBOL")
    inst = col("SEM_INSTRUMENT_NAME", "INSTRUMENT_NAME")
    sid = col("SEM_SMST_SECURITY_ID", "SECURITY_ID")
    seri = col("SEM_SERIES", "SERIES")

    m = df[df[seg].astype(str).str.upper() == "NSE"]
    if inst:
        m = m[m[inst].astype(str).str.upper().str.contains("EQUITY", na=False)]
    if seri:
        m = m[m[seri].astype(str).str.upper().str.strip() == "EQ"]

    out = {}
    for s in HEAVYWEIGHTS:
        hit = m[m[sym].astype(str).str.upper().str.strip() == s]
        if len(hit):
            out[s] = str(hit.iloc[0][sid]).split(".")[0]
    return out


# ── opening straddle, stored so the decay curve has an anchor ──────────────

def open_straddle(current: float, now: datetime | None = None) -> float | None:
    """
    The ATM straddle premium as at the first reading of the session.

    Stored because the decay curve needs a starting point and the app is not
    always running at 09:15. If the first reading of the day happens at
    11:00, that becomes the anchor — and the curve is then measuring decay
    from 11:00, not from the open, which understates it. The stored
    timestamp is returned alongside so the caller can say so.
    """
    now = now or datetime.now()

    # Refuse to anchor outside market hours.
    #
    # This stored the first reading of the calendar day whenever it came,
    # so opening the app at 03:23 anchored the curve on an overnight quote.
    # The decay model then measures from 03:23 against a session that has
    # not started, which is roughly six hours of dead time counted as
    # elapsed — every reading would print UNDER-DECAYING for reasons that
    # have nothing to do with sellers.
    #
    # Before 09:15 the value is returned for display but never stored, so
    # the real anchor is still available when the session opens.
    in_session = config.MARKET_OPEN <= now.time() <= config.MARKET_CLOSE

    path = config.DATA_DIR / "open_straddle.json"
    import json
    state = {}
    if path.exists():
        try:
            state = json.loads(path.read_text())
        except Exception:                                   # noqa: BLE001
            state = {}
    key = now.date().isoformat()
    if key not in state and current and current > 0 and in_session:
        state[key] = {"premium": round(float(current), 2),
                      "at": now.isoformat(timespec="seconds")}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state))
    rec = state.get(key)
    return rec["premium"] if rec else None


def open_straddle_at(now: datetime | None = None) -> str | None:
    import json
    path = config.DATA_DIR / "open_straddle.json"
    if not path.exists():
        return None
    try:
        rec = json.loads(path.read_text()).get(
            (now or datetime.now()).date().isoformat())
        return rec["at"] if rec else None
    except Exception:                                       # noqa: BLE001
        return None


# ═══════════════════════════════════════════════════════════════════════════
# Participant divergence — FII against retail
# ═══════════════════════════════════════════════════════════════════════════
#
# The data was already being fetched: ParticipantOI carries FII, DII and
# Client index-futures long and short. What was missing is the comparison —
# whether institutions and retail are positioned on opposite sides.
#
# TWO THINGS THAT LIMIT IT, BOTH WORTH KNOWING
#
# It is PREVIOUS SESSION data. NSE publishes participant-wise OI after the
# close, so this describes yesterday's book, not today's. It is context for
# the day, never a trigger inside it.
#
# "Client" is not retail. The category is everyone who is not FII, DII or
# Pro — which includes HNIs, family offices and prop desks trading under
# client codes. Reading it as "the crowd" overstates what it is.

@dataclass
class Participants:
    fii_long: float
    fii_short: float
    client_long: float
    client_short: float
    dii_long: float = 0.0
    dii_short: float = 0.0
    as_of: str = ""

    @staticmethod
    def _ratio(lo: float, sh: float) -> float | None:
        return (lo / sh) if sh else None

    @property
    def fii_ratio(self) -> float | None:
        return self._ratio(self.fii_long, self.fii_short)

    @property
    def client_ratio(self) -> float | None:
        return self._ratio(self.client_long, self.client_short)

    @property
    def fii_net(self) -> float:
        return self.fii_long - self.fii_short

    @property
    def client_net(self) -> float:
        return self.client_long - self.client_short

    @property
    def diverging(self) -> bool:
        return (self.fii_net > 0) != (self.client_net > 0)

    @property
    def note(self) -> str:
        fr = self.fii_ratio
        cr = self.client_ratio
        if fr is None or cr is None:
            return "one side has no short position — ratio undefined"
        fside = "long" if self.fii_net > 0 else "short"
        cside = "long" if self.client_net > 0 else "short"
        base = (f"FII net {fside} ({fr:.2f} long/short), Client net {cside} "
                f"({cr:.2f}).")
        if not self.diverging:
            return (base + " Both on the same side — no divergence to read, "
                           "which is the ordinary state.")
        return (base + f" OPPOSITE SIDES. The historical framing is that FIIs "
                       f"are more often right at turning points, but that is "
                       f"a claim about averages over years and says nothing "
                       f"about this week. And 'Client' is not retail — it "
                       f"includes HNIs and prop desks under client codes.")


def participants(p) -> Participants | None:
    """Wrap a ParticipantOI record for comparison. Returns None if empty."""
    if p is None:
        return None
    try:
        return Participants(
            float(getattr(p, "fii_idx_fut_long", 0) or 0),
            float(getattr(p, "fii_idx_fut_short", 0) or 0),
            float(getattr(p, "client_idx_fut_long", 0) or 0),
            float(getattr(p, "client_idx_fut_short", 0) or 0),
            float(getattr(p, "dii_idx_fut_long", 0) or 0),
            float(getattr(p, "dii_idx_fut_short", 0) or 0),
            str(getattr(p, "date", "") or ""))
    except Exception:                                       # noqa: BLE001
        return None
