"""
Everything that turns a chain snapshot into a read.

Design rule: every number this module produces must be traceable. The bias
score is not a black box — it returns its own components, so you can see
exactly which input pushed it bullish and by how much. If a component turns
out not to earn its weight when you backtest the logged snapshots, delete it
from COMPONENT_WEIGHTS. That auditability is the whole point of building it
yourself.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, date, time as dtime

import numpy as np
import pandas as pd
from scipy.stats import norm

import config


# ═══════════════════════════════════════════════════════════════════════════
# Data holders
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class Levels:
    atm: int
    spot: float
    s1: int
    s2: int
    r1: int
    r2: int
    s1_oi: float
    r1_oi: float
    max_pain: int
    range_pct: float          # (r1-s1)/spot * 100
    position: float           # 0.0 at S1, 1.0 at R1
    r1_dominance: float       # r1_oi / r2_oi — a lone tall wall means more
    s1_dominance: float


@dataclass
class Read:
    levels: Levels
    regime: str
    bias_score: float
    components: dict[str, float]
    pcr_oi: float
    pcr_chg: float
    atm_iv_ce: float
    atm_iv_pe: float
    iv_skew: float            # pe_iv - ce_iv; rising skew = fear bid
    total_ce_oi: float
    total_pe_oi: float
    notes: list[str] = field(default_factory=list)


@dataclass
class Signal:
    action: str               # BUY_CE | BUY_PE | NO_TRADE
    strike: int | None
    premium: float | None
    cost_per_lot: float | None
    delta: float | None
    move_per_50: float | None   # expected premium move for a 50-pt NIFTY move
    reason: str
    blocks: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def actionable(self) -> bool:
        return self.action != "NO_TRADE" and not self.blocks


# ═══════════════════════════════════════════════════════════════════════════
# Levels
# ═══════════════════════════════════════════════════════════════════════════

def atm_strike(spot: float) -> int:
    return int(round(spot / config.STRIKE_STEP) * config.STRIKE_STEP)


def window(df: pd.DataFrame, spot: float, n: int | None = None) -> pd.DataFrame:
    n = n if n is not None else config.WINDOW_STRIKES
    atm = atm_strike(spot)
    lo = atm - n * config.STRIKE_STEP
    hi = atm + n * config.STRIKE_STEP
    return df[(df.strike >= lo) & (df.strike <= hi)].copy()


def max_pain(df: pd.DataFrame) -> int:
    """Strike at which writers pay out the least in aggregate."""
    strikes = df.strike.values
    ce = np.nan_to_num(df.ce_oi.values)
    pe = np.nan_to_num(df.pe_oi.values)
    pain = [
        (np.maximum(k - strikes, 0) * ce).sum() + (np.maximum(strikes - k, 0) * pe).sum()
        for k in strikes
    ]
    return int(strikes[int(np.argmin(pain))])


def deduplicate_strikes_live(df: pd.DataFrame, context: str = "") -> pd.DataFrame:
    """
    Live consumer guard: logs an error, drops duplicates keeping nearest expiry (or first),
    tags df.attrs['duplicate_strikes_dropped'] = True, and CONTINUES. Never raises.
    """
    if df is None or df.empty or "strike" not in df.columns or not df["strike"].duplicated().any():
        return df
    import logging
    logger = logging.getLogger("scanner")
    logger.error("[%s] Duplicate strikes detected (%d duplicate rows); dropping duplicates keeping nearest/first",
                 context, df["strike"].duplicated().sum())
    d = df.copy()
    if "expiry" in d.columns:
        def _parse_exp_dt(exp_val):
            if not exp_val or exp_val != exp_val:
                return datetime.max
            s = str(exp_val).strip()
            for fmt in ("%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d"):
                try:
                    return datetime.strptime(s, fmt)
                except ValueError:
                    pass
            return datetime.max
        d["_exp_dt"] = d["expiry"].apply(_parse_exp_dt)
        d = d.sort_values(["strike", "_exp_dt"]).drop_duplicates(subset=["strike"], keep="first").drop(columns=["_exp_dt"])
    else:
        d = d.drop_duplicates(subset=["strike"], keep="first")
    d.attrs["duplicate_strikes_dropped"] = True
    return d


def compute_levels(df: pd.DataFrame, spot: float) -> Levels:
    """
    Walls come from the analysis window, not the full chain. Far strikes carry
    large legacy OI from positional traders and will drag your levels 500
    points away from anything that matters intraday.
    """
    df = deduplicate_strikes_live(df, "compute_levels")
    w = window(df, spot, config.WINDOW_STRIKES + 3)

    # Resistance must sit ABOVE spot and support BELOW it.
    #
    # Without this both sides can select the same strike. It happened live:
    # a huge wall at 24,000 carried the largest call OI and the largest put
    # OI at once, so S1 and R1 both read 24,000 with spot at 23,972 — and
    # every consumer of that span broke with it. The headline "expected
    # range 24000-24000 (0 pts)" was this, not a straddle fault.
    #
    # The strike price is ON is excluded from both sides rather than
    # assigned to one. A wall price is sitting exactly on is neither
    # support nor resistance until it resolves, and picking a side would be
    # inventing information.
    _ce_pool = w[w.strike > spot].dropna(subset=["ce_oi"])
    _pe_pool = w[w.strike < spot].dropna(subset=["pe_oi"])

    # Fall back to the unfiltered side only if one is empty — better a
    # degraded level than none, but say nothing false about which side.
    if _ce_pool.empty:
        _ce_pool = w.dropna(subset=["ce_oi"])
    if _pe_pool.empty:
        _pe_pool = w.dropna(subset=["pe_oi"])

    ce = _ce_pool.nlargest(2, "ce_oi")
    pe = _pe_pool.nlargest(2, "pe_oi")

    r1 = int(ce.iloc[0].strike); r1_oi = float(ce.iloc[0].ce_oi)
    r2 = int(ce.iloc[1].strike) if len(ce) > 1 else r1
    r2_oi = float(ce.iloc[1].ce_oi) if len(ce) > 1 else r1_oi

    s1 = int(pe.iloc[0].strike); s1_oi = float(pe.iloc[0].pe_oi)
    s2 = int(pe.iloc[1].strike) if len(pe) > 1 else s1
    s2_oi = float(pe.iloc[1].pe_oi) if len(pe) > 1 else s1_oi

    span = max(r1 - s1, 1)
    return Levels(
        atm=atm_strike(spot), spot=spot,
        s1=s1, s2=s2, r1=r1, r2=r2, s1_oi=s1_oi, r1_oi=r1_oi,
        max_pain=max_pain(df),
        range_pct=(r1 - s1) / spot * 100,
        position=float(np.clip((spot - s1) / span, -0.5, 1.5)),
        r1_dominance=r1_oi / max(r2_oi, 1),
        s1_dominance=s1_oi / max(s2_oi, 1),
    )


# ═══════════════════════════════════════════════════════════════════════════
# Quadrants
# ═══════════════════════════════════════════════════════════════════════════

PRICE_EPS = 0.05      # rupees; below this the LTP has not meaningfully moved
OI_EPS = 1.0          # contracts


def quadrant_strict(price_chg, oi_chg, ltp=None) -> tuple[str, str]:
    """
    Four-pillar classification with the evidence requirement ENFORCED.

    HARD RULE: a pillar may never be inferred from OI change alone. Price
    direction and OI direction are both required, because OI rising says
    positions were created and says nothing about which side created them.
    Without the price leg, "OI up" is equally consistent with fresh buyers
    and fresh sellers — opposite readings from the same number.

    Returns (pillar, confidence). When the price leg is missing, stale or
    flat the pillar is UNKNOWN and the app says so rather than guessing. An
    honest gap is worth more than a confident coin-flip.
    """
    bad_price = (price_chg is None or price_chg != price_chg)
    bad_oi = (oi_chg is None or oi_chg != oi_chg)
    if bad_price or bad_oi:
        return "unknown", "NONE"
    if ltp is not None and ltp == ltp and ltp <= 0:
        return "unknown", "NONE"

    if abs(price_chg) < PRICE_EPS and abs(oi_chg) < OI_EPS:
        return "flat", "HIGH"
    if abs(price_chg) < PRICE_EPS:
        return "unknown", "NONE"          # OI moved, price did not — who?
    if abs(oi_chg) < OI_EPS:
        return "flat", "MODERATE"

    pillar = quadrant(price_chg, oi_chg)
    price_strong = abs(price_chg) >= 1.0
    oi_strong = abs(oi_chg) >= 2000
    conf = ("HIGH" if price_strong and oi_strong else
            "MODERATE" if price_strong or oi_strong else "WEAK")
    return pillar, conf


def quadrant(d_premium: float, d_oi: float) -> str:
    if np.isnan(d_premium) or np.isnan(d_oi):
        return "no_data"
    if d_oi > 0:
        return "long_buildup" if d_premium > 0 else "short_buildup"
    if d_oi < 0:
        return "short_covering" if d_premium > 0 else "long_unwinding"
    return "flat"


# What each quadrant implies for the INDEX (not for the option).
# Calls and puts invert — this is the step people get wrong.
_CE_BIAS = {"long_buildup": +1, "short_buildup": -1,
            "short_covering": +1, "long_unwinding": -1, "flat": 0, "no_data": 0}
_PE_BIAS = {"long_buildup": -1, "short_buildup": +1,
            "short_covering": -1, "long_unwinding": +1, "flat": 0, "no_data": 0}


def _oi_flow_score(w: pd.DataFrame, side: str) -> float:
    """
    DEPRECATED — kept only so old logs remain readable.

    This computed a premium-direction quadrant per side. It was replaced
    because the call and put versions were mathematically identical: CE and
    PE premiums are driven by the same spot and so move opposite, while the
    put bias table is the mirror of the call one. Mirror applied to mirror is
    identity, so both sides returned the same number on every single poll —
    confirmed on live data at 100% agreement, correlation 1.0.

    What it actually measured was "did spot rise since the last poll", which
    is a price echo, not positioning. Two of six components were spending
    four score points on it.
    """
    col_chg, col_oi = f"{side}_chg", f"{side}_chg_oi"
    table = _CE_BIAS if side == "ce" else _PE_BIAS

    num = den = 0.0
    for _, r in w.iterrows():
        q = quadrant(r[col_chg], r[col_oi])
        weight = abs(r[col_oi]) if not np.isnan(r[col_oi]) else 0.0
        num += table[q] * weight
        den += weight
    return 0.0 if den == 0 else float(np.clip(num / den, -1, 1) * 2)


def _oi_positioning_score(w: pd.DataFrame, spot: float) -> float:
    """
    Where OI is BUILDING relative to spot. Scaled to -2..+2.

    This is the replacement, and it is deliberately blind to premium
    direction — that was the whole problem before. It asks only where new
    open interest is appearing:

        calls building ABOVE spot  -> a ceiling forming   -> bearish
        puts  building BELOW spot  -> a floor forming     -> bullish

    Strike LOCATION carries the information, so a rise in spot cannot by
    itself move this score. Normalised by total activity in the window so a
    quiet day and a busy one are comparable.
    """
    call_above = put_below = 0.0
    for _, r in w.iterrows():
        k = r["strike"]
        c, p_ = r.get("ce_chg_oi"), r.get("pe_chg_oi")
        if k > spot and c == c and c > 0:
            call_above += float(c)
        if k < spot and p_ == p_ and p_ > 0:
            put_below += float(p_)

    total = call_above + put_below
    if total <= 0:
        return 0.0
    return float(np.clip((put_below - call_above) / total, -1, 1) * 2)


def _oi_unwinding_score(w: pd.DataFrame, spot: float) -> float:
    """
    Where OI is being REMOVED. Scaled to -2..+2.

    Genuinely independent of the building score: writers leaving a level is a
    different event from writers arriving, and the two often happen at
    different strikes on the same poll.

        calls unwinding ABOVE spot -> ceiling dissolving -> bullish
        puts  unwinding BELOW spot -> floor dissolving   -> bearish
    """
    call_above = put_below = 0.0
    for _, r in w.iterrows():
        k = r["strike"]
        c, p_ = r.get("ce_chg_oi"), r.get("pe_chg_oi")
        if k > spot and c == c and c < 0:
            call_above += abs(float(c))
        if k < spot and p_ == p_ and p_ < 0:
            put_below += abs(float(p_))

    total = call_above + put_below
    if total <= 0:
        return 0.0
    return float(np.clip((call_above - put_below) / total, -1, 1) * 2)


# ═══════════════════════════════════════════════════════════════════════════
# Bias score
# ═══════════════════════════════════════════════════════════════════════════

def _pcr_score(pcr: float) -> float:
    """PCR is directional in the middle and contrarian at the extremes."""
    if np.isnan(pcr):
        return 0.0
    if pcr >= config.PCR_EXTREME_HI:
        return -1.0                      # everyone already long; crowded
    if pcr <= config.PCR_EXTREME_LO:
        return +1.0                      # capitulation; crowded short
    if pcr >= config.PCR_BULL:
        return +2.0
    if pcr <= config.PCR_BEAR:
        return -2.0
    return 0.0


def _migration_score(levels: Levels, baseline: Levels | None) -> float:
    """
    Movement of the walls since the session baseline. This is a stronger
    signal than the static level — a support that keeps climbing is a market
    where writers grow more confident by the hour.
    """
    if baseline is None:
        return 0.0
    s = 0.0
    if levels.s1 > baseline.s1: s += 1
    if levels.s1 < baseline.s1: s -= 1
    if levels.r1 > baseline.r1: s += 1
    if levels.r1 < baseline.r1: s -= 1
    return s


def _break_score(levels: Levels, peaks: dict, spot_prev: float | None) -> tuple[float, list[str]]:
    """
    Detect a wall failing. Writers covering at a wall while price presses into
    it is the fastest move on the board — and the opposite of a wall holding.
    """
    notes: list[str] = []
    score = 0.0

    r1_peak = peaks.get(f"ce_{levels.r1}", levels.r1_oi)
    s1_peak = peaks.get(f"pe_{levels.s1}", levels.s1_oi)
    rising = spot_prev is not None and levels.spot > spot_prev
    falling = spot_prev is not None and levels.spot < spot_prev

    if r1_peak > 0 and levels.r1_oi < r1_peak * 0.85 and rising:
        score += 3
        notes.append(f"Call writers covering at {levels.r1} — resistance breaking")
    if s1_peak > 0 and levels.s1_oi < s1_peak * 0.85 and falling:
        score -= 3
        notes.append(f"Put writers covering at {levels.s1} — support breaking")
    return score, notes


def _position_score(levels: Levels) -> float:
    if levels.position <= 0.25: return +1.0     # sitting on support
    if levels.position >= 0.75: return -1.0     # pressed into resistance
    return 0.0


def analyse(df: pd.DataFrame, spot: float,
            baseline: Levels | None = None,
            peaks: dict | None = None,
            spot_prev: float | None = None) -> Read:
    df = deduplicate_strikes_live(df, "analyse")

    peaks = peaks or {}
    levels = compute_levels(df, spot)
    w = window(df, spot)

    total_ce = float(np.nansum(df.ce_oi)); total_pe = float(np.nansum(df.pe_oi))
    d_ce = float(np.nansum(df.ce_chg_oi)); d_pe = float(np.nansum(df.pe_chg_oi))
    pcr_oi = total_pe / total_ce if total_ce else np.nan
    # Only a ratio when both sides are building — see pcr_change_state.
    _pc, _pcs, _pcn = pcr_change_state(d_ce, d_pe)
    pcr_chg = _pc if _pc is not None else np.nan

    brk, notes = _break_score(levels, peaks, spot_prev)

    components = {
        "pcr":            _pcr_score(pcr_oi),
        "oi_positioning": _oi_positioning_score(w, spot),
        "oi_unwinding":   _oi_unwinding_score(w, spot),
        "wall_migration": _migration_score(levels, baseline),
        "position":       _position_score(levels),
        "wall_break":     brk,
    }
    score = float(sum(components.values()))

    # ── regime ──────────────────────────────────────────────────────────────
    if spot > levels.r1 + config.STRIKE_STEP / 2:
        regime = "BREAKOUT_UP"
        notes.append(f"Trading above the call wall at {levels.r1}")
    elif spot < levels.s1 - config.STRIKE_STEP / 2:
        regime = "BREAKDOWN"
        notes.append(f"Trading below the put wall at {levels.s1}")
    elif levels.range_pct < config.CONSOLIDATION_PCT and abs(score) < config.BULL_SCORE:
        regime = "CONSOLIDATION"
        notes.append(f"Walls only {levels.r1 - levels.s1} pts apart — compression, expect a squeeze")
    elif score >= config.BULL_SCORE:
        regime = "BULLISH"
    elif score <= config.BEAR_SCORE:
        regime = "BEARISH"
    else:
        regime = "RANGEBOUND"

    atm = levels.atm
    row = df[df.strike == atm]
    ce_iv = float(row.ce_iv.iloc[0]) if len(row) else np.nan
    pe_iv = float(row.pe_iv.iloc[0]) if len(row) else np.nan
    # NSE stops publishing IV once an option is worthless, so at expiry one or
    # both sides come back empty. Fall back to the nearest strike that has it.
    if np.isnan(ce_iv) or ce_iv <= 0:
        alt = df[(df.ce_iv > 0)].assign(d=lambda x: (x.strike - atm).abs()).nsmallest(1, "d")
        ce_iv = float(alt.ce_iv.iloc[0]) if len(alt) else np.nan
    if np.isnan(pe_iv) or pe_iv <= 0:
        alt = df[(df.pe_iv > 0)].assign(d=lambda x: (x.strike - atm).abs()).nsmallest(1, "d")
        pe_iv = float(alt.pe_iv.iloc[0]) if len(alt) else np.nan
    skew = pe_iv - ce_iv if not (np.isnan(ce_iv) or np.isnan(pe_iv)) else np.nan

    return Read(
        levels=levels, regime=regime, bias_score=score, components=components,
        pcr_oi=pcr_oi, pcr_chg=pcr_chg,
        atm_iv_ce=ce_iv, atm_iv_pe=pe_iv, iv_skew=skew,
        total_ce_oi=total_ce, total_pe_oi=total_pe, notes=notes,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Greeks
# ═══════════════════════════════════════════════════════════════════════════

def bs_delta(spot: float, strike: float, iv_pct: float | None,
             days: float | None, is_call: bool, r: float = 0.065) -> float:
    """Black-Scholes delta. Used only to size expectations, not to price."""
    if days is None or iv_pct is None:
        return np.nan
    if any(np.isnan(x) for x in (iv_pct, days)) or iv_pct <= 0 or days <= 0:
        return np.nan
    t = days / 365.0
    sig = iv_pct / 100.0
    d1 = (np.log(spot / strike) + (r + sig ** 2 / 2) * t) / (sig * np.sqrt(t))
    return float(norm.cdf(d1)) if is_call else float(norm.cdf(d1) - 1)


# ═══════════════════════════════════════════════════════════════════════════
# Signal — BUY ONLY
# ═══════════════════════════════════════════════════════════════════════════

def _pick_strike(df: pd.DataFrame, read: Read, side: str) -> tuple[int, float, float, float] | None:
    """
    Prefer ATM, then one strike ITM. Both track the index closely enough for a
    chart-based stop to mean something. Far-OTM strikes are deliberately not
    considered: delta near 0.15 means your stop and target stop being real.
    Rejects anything with a wide bid-ask — an illiquid strike bleeds you on
    entry and again on exit.
    """
    atm = read.levels.atm
    step = config.STRIKE_STEP
    candidates = [atm, atm - step, atm - 2 * step] if side == "ce" else \
                 [atm, atm + step, atm + 2 * step]

    for k in candidates:
        row = df[df.strike == k]
        if row.empty:
            continue
        r = row.iloc[0]
        ltp = r[f"{side}_ltp"]; bid = r[f"{side}_bid"]; ask = r[f"{side}_ask"]
        oi = r[f"{side}_oi"]; iv = r[f"{side}_iv"]
        if np.isnan(ltp) or ltp <= 0.5 or np.isnan(oi) or oi < 1000:
            continue
        if not np.isnan(bid) and not np.isnan(ask) and ltp > 0:
            if (ask - bid) / ltp * 100 > config.MAX_SPREAD_PCT:
                continue
        return int(k), float(ltp), float(iv), float(oi)
    return None


def make_signal(df: pd.DataFrame, read: Read, now: datetime,
                days_to_expiry: float | None, confirmed: bool = True,
                rangebound_trades: int = 0,
                expiry_today: bool = False,
                stops_today: int = 0) -> Signal:
    lv = read.levels
    blocks: list[str] = []
    warnings: list[str] = []

    # ── guard rails, from your own course rules ─────────────────────────────
    t = now.time()
    open_plus = dtime(config.MARKET_OPEN.hour,
                      config.MARKET_OPEN.minute + config.SUPPRESS_FIRST_MINUTES)
    if t < config.MARKET_OPEN:
        blocks.append("Market not open yet — chain is yesterday's close")
    elif t > config.MARKET_CLOSE:
        blocks.append("Market closed — chain is today's final snapshot")
    elif t < open_plus:
        blocks.append(f"Opening {config.SUPPRESS_FIRST_MINUTES} min — OI still settling")
    if t >= config.NO_ENTRY_AFTER:
        blocks.append("After 3:00 PM — no fresh entries (course rule)")
    if (getattr(config, "ENFORCE_BUYER_FREEZE", False)
            and config.BUYER_FREEZE_FROM <= t <= config.BUYER_FREEZE_UNTIL):
        blocks.append(
            f"Midday freeze {config.BUYER_FREEZE_FROM:%H:%M}-"
            f"{config.BUYER_FREEZE_UNTIL:%H:%M} — theta accelerates while "
            f"volume drops. Fresh buys before 10:30 or after 13:30.")
    if not confirmed:
        blocks.append(f"Bias not yet held for {config.CONFIRM_SNAPSHOTS} polls")
    if stops_today >= getattr(config, "MAX_STOPS_PER_DAY", 2):
        blocks.append(
            f"{stops_today} stop-losses already today — CIRCUIT BREAKER. "
            f"Conditions are misaligned with the model. Close the terminal.")
    if expiry_today:
        warnings.append("EXPIRY DAY — theta is brutal and OI levels stop holding")
    if days_to_expiry is None:
        warnings.append("Expiry date unknown — delta and DTE-dependent terms skipped")
    elif days_to_expiry <= 0.5:
        warnings.append("Under half a day to expiry — premium decays fast")

    # ── direction ───────────────────────────────────────────────────────────
    regime = read.regime
    if regime == "BREAKOUT_UP":
        action, reason = "BUY_CE", f"Above the {lv.r1} call wall — writers forced to cover"
    elif regime == "BREAKDOWN":
        action, reason = "BUY_PE", f"Below the {lv.s1} put wall — writers forced to cover"
    elif regime == "BULLISH":
        action, reason = "BUY_CE", f"OI flow bullish (score {read.bias_score:+.1f}), support holding at {lv.s1}"
    elif regime == "BEARISH":
        action, reason = "BUY_PE", f"OI flow bearish (score {read.bias_score:+.1f}), resistance capping at {lv.r1}"
    elif regime == "CONSOLIDATION":
        action, reason = "NO_TRADE", "Compression — walls too close together, wait for the break"
    else:  # RANGEBOUND
        if rangebound_trades >= config.MAX_TRADES_RANGEBOUND:
            action, reason = "NO_TRADE", f"Already {rangebound_trades} attempts in a sideways market — stop"
        elif lv.position <= 0.25:
            action, reason = "BUY_CE", f"At support {lv.s1} in a range — buy the floor, target {lv.r1}"
        elif lv.position >= 0.75:
            action, reason = "BUY_PE", f"At resistance {lv.r1} in a range — sell the ceiling, target {lv.s1}"
        else:
            action, reason = "NO_TRADE", "Mid-range with no edge — wait for a boundary"

    if action == "NO_TRADE":
        return Signal(action, None, None, None, None, None, reason, blocks, warnings)

    # ── strike selection ────────────────────────────────────────────────────
    side = "ce" if action == "BUY_CE" else "pe"
    pick = _pick_strike(df, read, side)
    if pick is None:
        blocks.append("No strike passed the liquidity and spread checks")
        return Signal("NO_TRADE", None, None, None, None, None,
                      reason + " — but no tradeable strike", blocks, warnings)

    strike, ltp, iv, _oi = pick
    cost = ltp * config.LOT_SIZE
    if days_to_expiry is not None:
        delta = bs_delta(lv.spot, strike, iv, days_to_expiry, side == "ce")
        move50 = abs(delta) * 50 * config.LOT_SIZE if not np.isnan(delta) else np.nan
    else:
        delta = np.nan
        move50 = np.nan

    return Signal(action, strike, ltp, cost, delta, move50, reason, blocks, warnings)


def signal_to_row(read: Read, sig: Signal, ts: datetime) -> dict:
    """Flat record for the CSV log — this is your backtest fuel."""
    return {
        "timestamp": ts.isoformat(timespec="seconds"),
        "spot": read.levels.spot, "atm": read.levels.atm,
        "s1": read.levels.s1, "s2": read.levels.s2,
        "r1": read.levels.r1, "r2": read.levels.r2,
        "max_pain": read.levels.max_pain,
        "range_pct": round(read.levels.range_pct, 3),
        "position": round(read.levels.position, 3),
        "regime": read.regime, "bias_score": read.bias_score,
        **{f"c_{k}": v for k, v in read.components.items()},
        "pcr_oi": read.pcr_oi, "pcr_chg": read.pcr_chg,
        "iv_skew": read.iv_skew,
        "action": sig.action, "strike": sig.strike, "premium": sig.premium,
        "blocked": "|".join(sig.blocks),
        # fill these in yourself later — this is what makes the log a backtest
        "spot_plus_30min": "", "outcome": "",
    }


# ═══════════════════════════════════════════════════════════════════════════
# Pre-open anchored centre strike, and the ATM OI weight
# ═══════════════════════════════════════════════════════════════════════════

def centre_strike(spot: float, preopen: float | None = None) -> tuple[int, str]:
    """
    Which strike sits at the centre of the analysis window.

    Two defensible methods, and they are NOT the same thing:

      PRE-OPEN ANCHOR (MJ's)  lock the centre to the 9:08 pre-open level and
        hold it all session. The argument is that the day's positioning was
        laid down against that level, so the strikes that matter are fixed
        from the open.

      LIVE ATM (the default here)  re-derive the centre from spot on every
        poll, so the window follows price.

      They diverge exactly when it matters most. On a 200-point trend day the
      pre-open anchor leaves you watching strikes price left behind an hour
      ago; the live ATM keeps the window centred but loses sight of where the
      day's positioning was actually built. Neither is obviously right, which
      is why this is a toggle rather than a decision I made for you — set
      CENTRE_STRIKE_MODE in config and let the signal log settle it.
    """
    mode = getattr(config, "CENTRE_STRIKE_MODE", "live")
    if mode == "preopen" and preopen:
        return int(round(preopen / config.STRIKE_STEP) * config.STRIKE_STEP), \
               f"pre-open {preopen:,.2f}"
    return atm_strike(spot), f"live spot {spot:,.2f}"


def window_width(days_to_expiry: float) -> int:
    """
    How many strikes either side of centre.

    Widens with time left, as the session describes: far from expiry the
    market can travel further, so a five-strike window clips the strikes that
    end up mattering. Near expiry it narrows because the range collapses.
    """
    if not getattr(config, "WINDOW_SCALES_WITH_EXPIRY", True):
        return config.WINDOW_STRIKES
    if days_to_expiry <= 1:
        return max(3, config.WINDOW_STRIKES - 2)
    if days_to_expiry <= 3:
        return config.WINDOW_STRIKES
    if days_to_expiry <= 7:
        return config.WINDOW_STRIKES + 2
    return config.WINDOW_STRIKES + 4


@dataclass
class AtmWeight:
    strike: int
    ce_oi: float
    pe_oi: float
    ce_chg: float
    pe_chg: float
    ce_pchg: float | None = None
    pe_pchg: float | None = None

    @property
    def ratio(self) -> float:
        """Call OI divided by put OI at the centre strike."""
        return self.ce_oi / self.pe_oi if self.pe_oi else float("inf")

    @property
    def heavier(self) -> str:
        if self.ratio >= config.ATM_WEIGHT_RATIO:
            return "CALL"
        if self.ratio <= 1 / config.ATM_WEIGHT_RATIO:
            return "PUT"
        return "BALANCED"

    @property
    def conventional(self) -> str:
        """
        The reading this app uses elsewhere: OI marks where writers defend.
        """
        if self.heavier == "CALL":
            return "heavy call OI at the centre — writers capping it, BEARISH"
        if self.heavier == "PUT":
            return "heavy put OI at the centre — writers defending it, BULLISH"
        return "balanced at the centre — no lean"

    @property
    def contrarian(self) -> str:
        """
        MJ's reading: trade AGAINST the weight, because a large share of the
        crowd sitting on the heavy side is positioned wrongly.

        Kept SEPARATE from the score on purpose. On the heavy-call case the
        two readings agree — both point down — but they agree by different
        logic, and on some configurations they will diverge. Folding a
        contrarian rule into a score built on the conventional one would hide
        which of them is doing the work.
        """
        if self.heavier == "CALL":
            return "trade against the call weight — expect DOWN"
        if self.heavier == "PUT":
            return "trade against the put weight — expect UP"
        return "no weight to trade against"

    @property
    def agree(self) -> bool:
        c, k = self.conventional, self.contrarian
        return ("BEARISH" in c and "DOWN" in k) or ("BULLISH" in c and "UP" in k)


def atm_weight(df: pd.DataFrame, centre: int) -> AtmWeight | None:
    """Call versus put OI at the centre strike itself, not the aggregate."""
    row = df[df.strike == centre]
    if row.empty:
        return None
    r = row.iloc[0]

    def f(name):
        v = r.get(name)
        return float(v) if v == v and v is not None else 0.0

    def opt(name):
        v = r.get(name)
        return float(v) if v == v and v is not None else None

    return AtmWeight(strike=centre,
                     ce_oi=f("ce_oi"), pe_oi=f("pe_oi"),
                     ce_chg=f("ce_chg_oi"), pe_chg=f("pe_chg_oi"),
                     ce_pchg=opt("ce_pchg_oi"), pe_pchg=opt("pe_pchg_oi"))


# ═══════════════════════════════════════════════════════════════════════════
# Institutional reads — straddle range, intraday PCR, skew, conviction
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class StraddleRange:
    strike: int
    ce_ltp: float
    pe_ltp: float
    spot: float

    @property
    def premium(self) -> float:
        return self.ce_ltp + self.pe_ltp

    @property
    def upper(self) -> float:
        return self.strike + self.premium

    @property
    def lower(self) -> float:
        return self.strike - self.premium

    @property
    def width_pct(self) -> float:
        return self.premium * 2 / self.spot * 100

    @property
    def position(self) -> str:
        if self.spot > self.upper:
            return "ABOVE the expected range — sellers already beaten"
        if self.spot < self.lower:
            return "BELOW the expected range — sellers already beaten"
        room_up = self.upper - self.spot
        room_dn = self.spot - self.lower
        if room_up < self.premium * 0.25:
            return f"near the upper edge, {room_up:,.0f} pts of room left"
        if room_dn < self.premium * 0.25:
            return f"near the lower edge, {room_dn:,.0f} pts of room left"
        return f"mid-range · {room_dn:,.0f} pts down, {room_up:,.0f} pts up"


def straddle_range(df: pd.DataFrame, spot: float) -> StraddleRange | None:
    """
    The market's own expected move, priced by the ATM straddle.

    Spot +/- (ATM call + ATM put). This is what option SELLERS are pricing the
    day's range at, and they have capital committed to defending it — which
    makes it a completely independent estimate from the OI walls. When the two
    agree you have a strong boundary; when they disagree the straddle is
    usually the more honest number, because it is a live price rather than a
    position that may be a hedge.
    """
    atm = atm_strike(spot)
    row = df[df.strike == atm]
    if row.empty:
        return None
    r = row.iloc[0]
    ce, pe = r.get("ce_ltp"), r.get("pe_ltp")
    if not (ce == ce and pe == pe) or ce <= 0 or pe <= 0:
        return None
    return StraddleRange(atm, float(ce), float(pe), spot)


def intraday_pcr(df: pd.DataFrame, spot: float, strikes: int = 6) -> dict:
    """
    PCR built from CHANGE in OI over the strikes near ATM, not total OI.

    Total PCR is dominated by deep-OTM positions that will never be tested and
    lags badly. This measures today's fresh writing where it actually matters.

    Thresholds from the notes: above 1.3 put writers are aggressively
    defending; below 0.7 call writers are capping rallies.
    """
    w = window(df, spot, strikes)
    ce = float(np.nansum(w["ce_chg_oi"]))
    pe = float(np.nansum(w["pe_chg_oi"]))
    if ce == 0:
        return {}
    ratio = pe / ce
    if ce < 0 and pe < 0:
        read = "both sides unwinding — positions being closed, not built"
    elif ratio >= 1.3:
        read = "put writers aggressively defending — intraday bullish"
    elif ratio <= 0.7:
        read = "call writers capping the rally — intraday bearish"
    else:
        read = "balanced fresh writing — no intraday lean"
    return {"ratio": ratio, "ce_chg": ce, "pe_chg": pe,
            "strikes": strikes, "read": read}


def vol_skew(df: pd.DataFrame, spot: float, distance: int = 200) -> dict:
    """
    OTM put IV against the equidistant OTM call IV.

    A standard put skew (put IV higher) is normal — institutions pay up to
    hedge downside. A flattening or inverted skew means call demand is
    unusually strong, which reads as upside speculation rather than
    protection.

    Different from the ATM skew already reported: that compares two options at
    the SAME strike, so it measures pricing asymmetry at the money. This
    compares equal distances out, which is where hedging demand actually
    shows.
    """
    step = config.STRIKE_STEP
    put_k = int(round((spot - distance) / step) * step)
    call_k = int(round((spot + distance) / step) * step)

    pr = df[df.strike == put_k]
    cr = df[df.strike == call_k]
    if pr.empty or cr.empty:
        return {}
    piv, civ = pr.iloc[0].get("pe_iv"), cr.iloc[0].get("ce_iv")
    if not (piv == piv and civ == civ) or piv <= 0 or civ <= 0:
        return {}

    diff = float(piv) - float(civ)
    if diff > 2:
        read = "normal put skew — downside being hedged as usual"
    elif diff < -2:
        read = ("INVERTED skew — call IV above put IV, upside speculation or "
                "aggressive call buying")
    else:
        read = "skew flat — unusual, hedging demand has thinned"
    return {"put_strike": put_k, "call_strike": call_k,
            "put_iv": float(piv), "call_iv": float(civ),
            "diff": diff, "read": read}


def conviction(df: pd.DataFrame, spot: float, strikes: int = 6) -> dict:
    """
    Volume against change in OI — activity versus commitment.

    High volume with little OI change is intraday churn: contracts changing
    hands without new positions being created. High OI change on moderate
    volume is positional build-up, which is the one that carries overnight.

    This is the distinction the notes call activity vs conviction, and it is
    the reason a busy-looking chain can mean nothing.
    """
    w = window(df, spot, strikes)
    vol = float(np.nansum(w["ce_vol"]) + np.nansum(w["pe_vol"]))
    oi = abs(float(np.nansum(w["ce_chg_oi"]))) + abs(float(np.nansum(w["pe_chg_oi"])))
    if vol <= 0:
        return {}
    ratio = oi / vol
    if ratio >= 0.35:
        read = "POSITIONAL — fresh OI relative to volume, money staying on"
    elif ratio <= 0.12:
        read = "CHURN — heavy volume creating little new OI, scalping not conviction"
    else:
        read = "mixed — some build-up, some churn"
    return {"volume": vol, "oi_change": oi, "ratio": ratio, "read": read}


def trapped_writers(df: pd.DataFrame, spot: float, levels: Levels,
                    approach_pts: float = 60) -> dict:
    """
    The gamma squeeze setup: writers cutting losses at a wall price is testing.

    Three conditions together, not one:
      1. spot is APPROACHING a heavy-OI strike
      2. OI at that strike is FALLING — writers exiting
      3. the option's LTP is RISING — they are paying up to get out

    Any one alone is noise. All three is the non-linear move the notes
    describe, because market makers must buy futures to hedge the short gamma
    they are left holding.
    """
    out = {}
    for side, wall, direction in (("ce", levels.r1, "up"),
                                  ("pe", levels.s1, "down")):
        row = df[df.strike == wall]
        if row.empty:
            continue
        r = row.iloc[0]
        chg_oi = r.get(f"{side}_chg_oi")
        chg_ltp = r.get(f"{side}_chg")
        if not (chg_oi == chg_oi and chg_ltp == chg_ltp):
            continue
        near = abs(spot - wall) <= approach_pts
        exiting = float(chg_oi) < 0
        paying_up = float(chg_ltp) > 0
        if near and exiting and paying_up:
            out[side] = {
                "strike": int(wall), "distance": float(wall - spot),
                "oi_change": float(chg_oi), "ltp_change": float(chg_ltp),
                "read": (f"{'CALL' if side == 'ce' else 'PUT'} WRITERS TRAPPED "
                         f"at {int(wall)} — OI falling {abs(float(chg_oi)):,.0f} "
                         f"while premium rises {float(chg_ltp):+.1f}. Squeeze "
                         f"{direction} likely as hedging forces futures buying."),
            }
    return out


# ═══════════════════════════════════════════════════════════════════════════
# The opening — 9:15 to 9:45
# ═══════════════════════════════════════════════════════════════════════════

CHAIN_RELIABLE_FROM = dtime(9, 20)


def chain_is_stale(now: datetime, nse_stamp: str = "") -> tuple[bool, str]:
    """
    Between 9:15 and 9:20 the option chain is showing YESTERDAY'S OI.

    NSE refreshes the chain on a two-to-three minute cycle, so at the bell the
    "change in OI" column has not been recomputed for today at all. Reading it
    then is reading yesterday's close and calling it this morning's flow —
    which is worse than having no data, because it looks current.

    The first genuinely fresh snapshot lands around 9:20. This flags the
    window explicitly rather than letting the app present stale numbers with
    a confident face.
    """
    t = now.time()
    if not (config.MARKET_OPEN <= t < CHAIN_RELIABLE_FROM):
        return False, ""
    mins = (CHAIN_RELIABLE_FROM.hour * 60 + CHAIN_RELIABLE_FROM.minute) - \
           (t.hour * 60 + t.minute)
    return True, (f"Chain OI is still YESTERDAY'S close — NSE has not "
                  f"recomputed today's change yet. First reliable snapshot in "
                  f"about {mins} min. Use price action only until 9:20.")


@dataclass
class OpeningRead:
    scenario: str
    gap: float
    detail: str
    playbook: str
    confidence: str
    inputs: dict = field(default_factory=dict)


def opening_scenario(df: pd.DataFrame, spot: float, prev_close: float,
                     levels: Levels, straddle: StraddleRange | None,
                     vix: float | None = None, vix_chg: float | None = None,
                     ipcr: dict | None = None,
                     fii_long_pct: float | None = None) -> OpeningRead:
    """
    Classify the open into one of the three scenarios from the session.

    Deliberately run at 9:20 or later, once the chain carries real numbers.
    Every input here already exists in the app — this is the synthesis, not
    new data:

      SCENARIO 1  Trapped writer squeeze. A 60-100 pt gap THROUGH the heaviest
                  OI wall, with those writers unwinding while premium rises.
                  The strongest of the three because the flow is forced.

      SCENARIO 2  Gap fade. A moderate gap that lands INSIDE the straddle
                  breakeven and stalls at a wall the writers are defending.
                  Mean reversion toward VWAP or the previous close.

      SCENARIO 3  Trend expansion. Gap in the direction of FII futures
                  positioning with VIX expanding and one-sided fresh writing.

    Where nothing fits, it says so. A forced classification would be worse
    than none — most opens are simply ordinary.
    """
    gap = spot - prev_close
    absgap = abs(gap)
    up = gap > 0
    inputs = {"gap": gap, "vix": vix, "vix_chg": vix_chg,
              "fii_long_pct": fii_long_pct}

    wall = levels.r1 if up else levels.s1
    through_wall = (spot > levels.r1) if up else (spot < levels.s1)
    inputs["wall"] = wall
    inputs["through_wall"] = through_wall

    # is the wall's own OI unwinding while its premium rises?
    side = "ce" if up else "pe"
    row = df[df.strike == wall]
    unwinding = paying_up = False
    if not row.empty:
        r = row.iloc[0]
        c_oi, c_ltp = r.get(f"{side}_chg_oi"), r.get(f"{side}_chg")
        unwinding = bool(c_oi == c_oi and float(c_oi) < 0)
        paying_up = bool(c_ltp == c_ltp and float(c_ltp) > 0)
    inputs["wall_unwinding"] = unwinding
    inputs["wall_premium_up"] = paying_up

    inside_straddle = bool(straddle and straddle.lower <= spot <= straddle.upper)
    inputs["inside_straddle"] = inside_straddle

    pcr = (ipcr or {}).get("ratio")
    inputs["intraday_pcr"] = pcr

    # ── Scenario 1 ──────────────────────────────────────────────────────────
    if 55 <= absgap <= 130 and through_wall and unwinding and paying_up:
        return OpeningRead(
            "1 · TRAPPED WRITER SQUEEZE", gap,
            f"Opened {absgap:.0f} pts {'above' if up else 'below'} and cleared "
            f"the {wall} wall. Those writers are unwinding while premium "
            f"rises — they are paying to get out.",
            f"Buy ATM {'CE' if up else 'PE'} on a retest of the first 3-min "
            f"candle {'high' if up else 'low'} or VWAP. The squeeze typically "
            f"runs 70-120 points. Anchor the stop to that candle, not to "
            f"premium.",
            "HIGH — forced flow, not sentiment", inputs)

    # ── Scenario 3 ──────────────────────────────────────────────────────────
    vix_expanding = vix_chg is not None and vix_chg > 3
    fii_aligned = (fii_long_pct is not None and
                   ((up and fii_long_pct > 65) or (not up and fii_long_pct < 30)))
    pcr_trend = pcr is not None and ((up and pcr > 1.4) or (not up and pcr < 0.6))
    if absgap >= 40 and vix_expanding and (fii_aligned or pcr_trend):
        why = []
        if fii_aligned:
            why.append(f"FII {fii_long_pct:.0f}% long")
        if pcr_trend:
            why.append(f"intraday PCR {pcr:.2f}")
        return OpeningRead(
            "3 · TREND EXPANSION", gap,
            f"Gap of {absgap:.0f} pts with VIX expanding {vix_chg:+.1f}% and "
            + " and ".join(why) + " — one-sided institutional drive.",
            "Ride it with a 5-minute EMA trailing stop. Do not fade. Size "
            "smaller than usual: expanding VIX means wider swings and a "
            "stop that needs more room.",
            "MEDIUM-HIGH — directional, but VIX expansion cuts both ways",
            inputs)

    # ── Scenario 2 ──────────────────────────────────────────────────────────
    defending = False
    if not row.empty:
        c_oi = row.iloc[0].get(f"{side}_chg_oi")
        defending = bool(c_oi == c_oi and float(c_oi) > 0)
    inputs["wall_defending"] = defending
    vix_crushing = vix_chg is not None and vix_chg < -3
    if 25 <= absgap <= 70 and inside_straddle and (defending or vix_crushing):
        return OpeningRead(
            "2 · GAP FADE", gap,
            f"Gap of {absgap:.0f} pts landed inside the straddle breakeven "
            f"({straddle.lower:,.0f}-{straddle.upper:,.0f}) and "
            + ("writers are adding size at the wall"
               if defending else f"VIX is crushing {vix_chg:+.1f}%") + ".",
            f"Fade it toward VWAP or the previous close ({prev_close:,.0f}). "
            f"Buy ATM {'PE' if up else 'CE'} on rejection at the wall. Target "
            f"the gap fill, not a trend.",
            "MEDIUM — mean reversion, so respect the wall as your stop", inputs)

    # ── nothing fits ────────────────────────────────────────────────────────
    return OpeningRead(
        "NO CLEAR SCENARIO", gap,
        f"Gap of {gap:+.0f} pts does not match any of the three patterns. "
        f"{'Inside' if inside_straddle else 'Outside'} the straddle range, "
        f"wall at {wall} {'breached' if through_wall else 'intact'}.",
        "Wait for the 9:30 confirmation window: a 5-minute range break WITH "
        "change-in-OI confirming writer surrender. Most opens are ordinary.",
        "LOW — no edge identified", inputs)


# ═══════════════════════════════════════════════════════════════════════════
# Regime: trend day or range-bound day
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class DayRegime:
    verdict: str                 # TREND | RANGE | UNCLEAR
    score: int                   # -4..+4, positive = trend
    checks: list[tuple[str, str, str]] = field(default_factory=list)
    playbook: str = ""
    instruments: str = ""

    @property
    def agreed(self) -> int:
        return sum(1 for _, v, _ in self.checks if v in ("TREND", "RANGE"))


def day_regime(df: pd.DataFrame, spot: float, levels: Levels,
               baseline: Levels | None = None,
               vix_chg: float | None = None,
               vwap_crosses: int | None = None,
               ipcr: dict | None = None) -> DayRegime:
    """
    The four-point check from the session, run once the day has shape (9:45).

    Treating every morning the same is the earliest mistake, and these four
    inputs already exist in the app — this is the synthesis:

      1. VIX trend        expanding +3 to +8% = trend; crushing -2 to -6% = range
      2. VWAP relationship price staying one side = trend; oscillating = range
      3. Intraday OI      one-sided writing = trend; straddle build on both = range
      4. Strike migration writers rolling to chase = trend; holding = range

    A single check means nothing. Three or four agreeing is the signal, which
    is why the count of agreeing checks is reported alongside the verdict.
    """
    checks: list[tuple[str, str, str]] = []
    score = 0

    # 1 — VIX
    if vix_chg is None:
        checks.append(("India VIX", "?", "no reading"))
    elif vix_chg >= 3:
        checks.append(("India VIX", "TREND", f"expanding {vix_chg:+.1f}%"))
        score += 1
    elif vix_chg <= -2:
        checks.append(("India VIX", "RANGE", f"crushing {vix_chg:+.1f}% — "
                                             f"premium sellers dominate"))
        score -= 1
    else:
        checks.append(("India VIX", "flat", f"{vix_chg:+.1f}%"))

    # 2 — VWAP
    if vwap_crosses is None:
        checks.append(("VWAP", "?", "not available"))
    elif vwap_crosses <= 1:
        checks.append(("VWAP", "TREND",
                       f"{vwap_crosses} cross today — price holding one side"))
        score += 1
    elif vwap_crosses >= 4:
        checks.append(("VWAP", "RANGE",
                       f"{vwap_crosses} crosses — oscillating around it"))
        score -= 1
    else:
        checks.append(("VWAP", "mixed", f"{vwap_crosses} crosses"))

    # 3 — one-sided writing, or a straddle build on both
    w = window(df, spot, 6)
    ce = float(np.nansum(w["ce_chg_oi"]))
    pe = float(np.nansum(w["pe_chg_oi"]))
    if ce == 0 and pe == 0:
        checks.append(("Intraday OI", "?", "no change yet"))
    else:
        one_sided = (ce > 0 > pe) or (pe > 0 > ce)
        both_heavy = ce > 0 and pe > 0 and min(ce, pe) > abs(ce - pe) * 0.6
        if one_sided:
            side = "puts written, calls unwinding" if pe > 0 else \
                   "calls written, puts unwinding"
            checks.append(("Intraday OI", "TREND", f"one-sided — {side}"))
            score += 1
        elif both_heavy:
            checks.append(("Intraday OI", "RANGE",
                           "straddle build — heavy writing BOTH sides"))
            score -= 1
        else:
            checks.append(("Intraday OI", "mixed",
                           f"CE {ce:+,.0f} · PE {pe:+,.0f}"))

    # 4 — strike migration
    if baseline is None:
        checks.append(("Strike migration", "?", "no session baseline yet"))
    else:
        moved = (levels.r1 != baseline.r1) or (levels.s1 != baseline.s1)
        if moved:
            checks.append(("Strike migration", "TREND",
                           f"walls rolled — R1 {baseline.r1}->{levels.r1}, "
                           f"S1 {baseline.s1}->{levels.s1}"))
            score += 1
        else:
            checks.append(("Strike migration", "RANGE",
                           f"writers holding {levels.s1}-{levels.r1}"))
            score -= 1

    if score >= 2:
        v = "TREND"
        play = ("Breakout of the 15-min opening balance plus a 5-min close "
                "across VWAP. No fixed target — trail the 9 EMA on the 5-min, "
                "or exit when a 5-min candle closes back across VWAP. Stop at "
                "the prior 5-min swing, 15-20 index points.")
        inst = "ATM or slightly ITM options, delta 0.50-0.65"
    elif score <= -2:
        v = "RANGE"
        play = ("Fade the extremes. Rejection wick at the day's high/low or a "
                "key OI barrier, failing to sustain beyond the first standard "
                "deviation of VWAP. Stop on a 5-min close outside the wick. "
                "Target VWAP or the opposite boundary, 1:2 minimum.")
        inst = "Defined-risk spreads, or fades back to VWAP"
    else:
        v = "UNCLEAR"
        play = ("Fewer than two checks agree. Wait — an unclassified day is "
                "the one that chops both playbooks.")
        inst = "none — stay flat until the regime declares itself"

    return DayRegime(v, score, checks, play, inst)


# ═══════════════════════════════════════════════════════════════════════════
# VWAP Trap & Retest — the highest-expectancy setup in the notes
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class VwapTrap:
    direction: str               # SHORT | LONG
    level: float                 # the level that was faked through
    level_kind: str
    fake_extreme: float          # the swing high/low of the fakeout
    vwap: float
    stop: float
    target: float
    rr: float
    detail: str

    @property
    def valid_rr(self) -> bool:
        return self.rr >= 2.0


def vwap_trap(candles: pd.DataFrame, df: pd.DataFrame, spot: float,
              levels: Levels, pdh: float | None = None,
              pdl: float | None = None,
              prev_close: float | None = None,
              poke_lookback: int = 24) -> VwapTrap | None:
    """
    Price spikes through a level, gets rejected, and loses VWAP.

    Four conditions, and all four are required:
      1. price traded ABOVE the level (PDH or the heavy call strike)
      2. it closed back BELOW it — the rejection
      3. writers at that strike ADDED OI — they are defending, not fleeing
      4. price then closed below VWAP

    Condition 3 is what separates this from an ordinary failed breakout, and
    it is the exact inverse of the trapped-writer squeeze: there the writers
    run, here they stand and defend. Same chart shape, opposite outcome —
    which is why the OI check cannot be skipped.
    """
    closed = candles[candles["closed"]] if "closed" in candles else candles
    if len(closed) < 4 or "vwap" not in closed or closed["vwap"].isna().all():
        return None

    last = closed.iloc[-1]
    vwap = float(last["vwap"])
    close = float(last["close"])

    # The poke and the VWAP loss can be an hour apart — the notes place this
    # setup between 9:30 and 11:00. A six-candle window missed the spike
    # entirely on 5-minute bars. The REJECTION and the VWAP position are
    # judged on the latest candle; only the poke is allowed to be older.
    recent = closed.tail(poke_lookback)

    for level, kind in ((pdh, "PDH"), (float(levels.r1), "heavy call OI")):
        if level is None:
            continue
        poked = bool((recent["high"] > level).any())
        rejected = close < level
        below_vwap = close < vwap
        if not (poked and rejected and below_vwap):
            continue

        row = df[df.strike == levels.r1]
        defending = False
        if not row.empty:
            c = row.iloc[0].get("ce_chg_oi")
            defending = bool(c == c and float(c) > 0)
        if kind == "heavy call OI" and not defending:
            continue

        fake_high = float(recent["high"].max())
        stop = fake_high
        target = prev_close if prev_close else float(recent["low"].min())
        risk = stop - close
        reward = close - target
        rr = reward / risk if risk > 0 else 0.0
        return VwapTrap(
            "SHORT", float(level), kind, fake_high, vwap, stop, float(target),
            rr,
            f"Price poked {level:,.0f} ({kind}), rejected, and closed "
            f"{vwap - close:,.0f} pts below VWAP"
            + (" while call writers ADDED OI — defending, not fleeing"
               if defending else ""))

    for level, kind in ((pdl, "PDL"), (float(levels.s1), "heavy put OI")):
        if level is None:
            continue
        poked = bool((recent["low"] < level).any())
        rejected = close > level
        above_vwap = close > vwap
        if not (poked and rejected and above_vwap):
            continue

        row = df[df.strike == levels.s1]
        defending = False
        if not row.empty:
            c = row.iloc[0].get("pe_chg_oi")
            defending = bool(c == c and float(c) > 0)
        if kind == "heavy put OI" and not defending:
            continue

        fake_low = float(recent["low"].min())
        stop = fake_low
        target = prev_close if prev_close else float(recent["high"].max())
        risk = close - stop
        reward = target - close
        rr = reward / risk if risk > 0 else 0.0
        return VwapTrap(
            "LONG", float(level), kind, fake_low, vwap, stop, float(target),
            rr,
            f"Price poked {level:,.0f} ({kind}), rejected, and closed "
            f"{close - vwap:,.0f} pts above VWAP"
            + (" while put writers ADDED OI — defending" if defending else ""))

    return None


def position_size(risk_rupees: float, stop_points: float,
                  lot_size: int | None = None) -> dict:
    """
    Lots = risk budget / (stop in points x lot size).

    Sized from the STOP, never from account balance. The stop distance is the
    only thing that determines what a loss actually costs, so it is the only
    sensible denominator.
    """
    lot = lot_size or config.LOT_SIZE
    per_lot = stop_points * lot
    if per_lot <= 0:
        return {}
    lots = risk_rupees / per_lot
    return {"lots": lots, "whole_lots": int(lots),
            "risk_per_lot": per_lot, "lot_size": lot,
            "actual_risk": int(lots) * per_lot,
            "affordable": int(lots) >= 1}


# ═══════════════════════════════════════════════════════════════════════════
# Option-chain R1-R4 / S1-S4, with level state
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class ChainLevel:
    rank: int                    # 1 = nearest to spot
    kind: str                    # resistance | support
    strike: int
    oi: float
    chg_oi: float
    pchg_oi: float | None
    ltp: float | None
    distance: float              # signed, from spot

    ltp_chg: float | None = None
    pillar: str = "unknown"
    confidence: str = "NONE"
    neighbour_building: bool = False
    contested: bool = False
    # the OTHER side at this same strike — without it, no level can be
    # called TRUE or CONFIRMED, only a candidate
    opp_oi: float = 0.0
    opp_chg: float = 0.0
    opp_pct: float | None = None

    @property
    def oi_pct(self) -> float:
        if self.pchg_oi is not None:
            return self.pchg_oi
        base = self.oi - self.chg_oi
        return self.chg_oi / base * 100 if base > 0 else 0.0

    @property
    def opp_pct_value(self) -> float:
        if self.opp_pct is not None:
            return self.opp_pct
        base = self.opp_oi - self.opp_chg
        return self.opp_chg / base * 100 if base > 0 else 0.0

    @property
    def dominates(self) -> bool:
        """
        Does THIS side clearly out-build the other side at this same strike?

        The condition that separates a real level from a candidate. OI rising
        on one side says nothing until you know what the other side did: a
        put strike gaining 30% while the call side gains 28% is a fight, not
        a floor, and the put column alone cannot tell you which.
        """
        return self.oi_pct - self.opp_pct_value >= config.LEVEL_DOMINANCE_PCT

    @property
    def state(self) -> str:
        """
        CONFIRMED / BUILDING / POTENTIAL / HOLDING / WEAKENING / UNWINDING /
        MIGRATING / CONTESTED / UNKNOWN.

        Two hard rules, both learned the hard way:

        1. High OI alone never decides this. OI is positioning CONCENTRATION,
           not proof of writing and not proof the level holds.

        2. Neither does one side's OI change. A level is only CONFIRMED when
           this side's OI% clearly exceeds the OTHER side's at the SAME
           strike. Without that comparison, "BUILDING" claims more than the
           data supports — a put strike building 30% while calls build 28%
           is contested, and the put column alone looks identical to a
           genuine floor.

        POTENTIAL is the honest label for "OI is arriving here, but the
        opposite side is matching it, so this is a candidate rather than a
        level".
        """
        if self.contested:
            return "CONTESTED"
        pct = self.oi_pct
        if self.pillar == "unknown":
            return "UNKNOWN"
        if pct <= -5 and self.neighbour_building:
            return "MIGRATING"
        if pct <= -20:
            return "UNWINDING"
        if pct <= -5:
            return "WEAKENING"
        if pct >= 15:
            return "CONFIRMED" if self.dominates else "POTENTIAL"
        if pct >= 4:
            return "BUILDING" if self.dominates else "POTENTIAL"
        return "HOLDING"

    @property
    def pillar_name(self) -> str:
        return PILLAR_MEANING.get(self.pillar, ("UNKNOWN", ""))[0]

    @property
    def writing(self) -> bool:
        """
        Is this side being WRITTEN rather than bought?

        Short buildup — premium falling while OI rises — is writing on
        whichever side it appears. On calls that caps the upside; on puts it
        defends the downside. The pillar name is identical, the consequence
        for the index is opposite, and that is why the side has to be read
        alongside it rather than the pillar alone.
        """
        return self.pillar == "short_buildup"

    @property
    def reliability(self) -> str:
        st = self.state
        kind = "resistance" if self.kind == "resistance" else "support"
        opp = self.opp_pct_value

        if st == "UNKNOWN":
            return "cannot classify — price leg missing, treat as unverified"
        if st == "CONTESTED":
            return (f"both sides building here ({self.oi_pct:+.0f}% vs "
                    f"{opp:+.0f}%) — a battle, not a wall")
        if st == "MIGRATING":
            return "positioning is moving to the next strike — expect this to give"
        if st == "CONFIRMED":
            return (f"confirmed {kind} — this side {self.oi_pct:+.0f}% clearly "
                    f"out-builds the other at {opp:+.0f}%"
                    + (", and it is being written" if self.writing else
                       ", though as buying rather than writing"))
        if st == "BUILDING":
            return (f"{kind} building — {self.oi_pct:+.0f}% against "
                    f"{opp:+.0f}% on the other side")
        if st == "POTENTIAL":
            return (f"possible {kind}, NOT confirmed — OI is arriving "
                    f"({self.oi_pct:+.0f}%) but the other side is matching it "
                    f"({opp:+.0f}%). Needs same-strike dominance before you "
                    f"can rely on it")
        if st == "HOLDING":
            return "concentration present, no fresh conviction either way"
        if st == "WEAKENING":
            return "fading — softer than the raw OI suggests"
        return "unwinding fast — do not rely on it"

    @property
    def label(self) -> str:
        return f"{'R' if self.kind == 'resistance' else 'S'}{self.rank}"


def chain_levels(df: pd.DataFrame, spot: float, n: int = 4,
                 span: int = 500) -> dict[str, list[ChainLevel]]:
    """
    R1-R4 and S1-S4 derived from OPTION-CHAIN positioning.

    Completely separate from the price-swing levels in build_master, and
    deliberately so — they answer different questions. Swing levels say where
    price has previously turned. These say where option writers currently have
    capital at risk. When the two agree you have a genuinely strong boundary;
    when they disagree, that disagreement is itself information.

    Resistances come from CALL OI above spot, supports from PUT OI below.
    Ranked by PROXIMITY to spot rather than by size, because R1 should mean
    "the first wall price meets", which is what you act on. Size and state
    travel with each level so a large distant wall is still visible as such.
    """
    out: dict[str, list[ChainLevel]] = {"resistance": [], "support": []}
    w = df[(df.strike >= spot - span) & (df.strike <= spot + span)]
    if w.empty:
        return out

    def build(side: str, kind: str, mask) -> list[ChainLevel]:
        sub = w[mask].copy()
        oi_col = f"{side}_oi"
        if sub.empty or oi_col not in sub:
            return []
        # only strikes carrying meaningful OI qualify as a level at all
        cutoff = sub[oi_col].median() if len(sub) > 3 else 0
        sub = sub[sub[oi_col] > max(cutoff, 1)]
        if sub.empty:
            return []
        sub = sub.reindex((sub["strike"] - spot).abs().sort_values().index)

        rows = []
        for i, (_, r) in enumerate(sub.head(n).iterrows(), 1):
            def val(col):
                v = r.get(col)
                return float(v) if v == v and v is not None else None

            strike = int(r["strike"])
            ltp = val(f"{side}_ltp")
            ltp_chg = val(f"{side}_chg")
            oi_chg = val(f"{side}_chg_oi") or 0.0
            pillar, conf = quadrant_strict(ltp_chg, oi_chg, ltp)

            # MIGRATING: is the next strike OUT from spot building while this
            # one bleeds? A level does not just weaken, it hands over.
            step = config.STRIKE_STEP
            nxt = strike + step if kind == "resistance" else strike - step
            nrow = w[w.strike == nxt]
            neighbour_building = False
            if not nrow.empty:
                nchg = nrow.iloc[0].get(f"{side}_chg_oi")
                noi = nrow.iloc[0].get(f"{side}_oi")
                if nchg == nchg and noi == noi and noi - nchg > 0:
                    neighbour_building = (nchg / (noi - nchg) * 100) >= 10

            # CONTESTED: both CE and PE adding size at this same strike
            other = "pe" if side == "ce" else "ce"
            o_chg = r.get(f"{other}_chg_oi")
            contested = bool(oi_chg > 0 and o_chg == o_chg and o_chg is not None
                             and float(o_chg) > 0
                             and min(oi_chg, float(o_chg))
                             > abs(oi_chg - float(o_chg)) * 0.6)

            rows.append(ChainLevel(
                rank=i, kind=kind, strike=strike,
                oi=float(r[oi_col]), chg_oi=oi_chg,
                pchg_oi=val(f"{side}_pchg_oi"), ltp=ltp,
                distance=float(r["strike"]) - spot,
                ltp_chg=ltp_chg, pillar=pillar, confidence=conf,
                neighbour_building=neighbour_building, contested=contested,
                opp_oi=val(f"{other}_oi") or 0.0,
                opp_chg=float(o_chg) if o_chg == o_chg and o_chg is not None else 0.0,
                opp_pct=val(f"{other}_pchg_oi")))
        return rows

    out["resistance"] = build("ce", "resistance", w.strike > spot)
    out["support"] = build("pe", "support", w.strike < spot)
    return out


# ═══════════════════════════════════════════════════════════════════════════
# Four pillars — per strike and aggregated
# ═══════════════════════════════════════════════════════════════════════════

PILLAR_MEANING = {
    "long_buildup":    ("LONG BUILD-UP", "price up, OI up — fresh buyers"),
    "short_buildup":   ("SHORT BUILD-UP", "price down, OI up — fresh sellers"),
    "short_covering":  ("SHORT COVERING", "price up, OI down — shorts closing"),
    "long_unwinding":  ("LONG UNWINDING", "price down, OI down — longs exiting"),
    "flat":            ("FLAT", "no meaningful change"),
    "unknown":         ("UNKNOWN", "price leg missing — not classified"),
}

#: which pillars represent NEW money versus positions being closed
FRESH = {"long_buildup", "short_buildup"}
EXIT = {"short_covering", "long_unwinding"}


@dataclass
class SideRow:
    """One side of one strike, with every input the classification used."""
    ltp: float | None
    ltp_chg: float | None
    ltp_pct: float | None
    oi: float
    oi_chg: float
    oi_pct: float | None
    pillar: str
    confidence: str

    @property
    def state(self) -> str:
        return PILLAR_MEANING[self.pillar][0]

    @property
    def why(self) -> str:
        if self.pillar == "unknown":
            return "price leg missing or flat — cannot classify"
        p = "up" if (self.ltp_chg or 0) > 0 else "down"
        o = "up" if self.oi_chg > 0 else "down"
        return f"price {p}, OI {o}"


@dataclass
class PillarRow:
    strike: int
    distance: float
    ce: SideRow
    pe: SideRow
    is_atm: bool = False

    # kept so existing callers keep working
    @property
    def ce_pillar(self) -> str: return self.ce.pillar
    @property
    def pe_pillar(self) -> str: return self.pe.pillar
    @property
    def ce_oi(self) -> float: return self.ce.oi
    @property
    def pe_oi(self) -> float: return self.pe.oi
    @property
    def ce_chg(self) -> float: return self.ce.oi_chg
    @property
    def pe_chg(self) -> float: return self.pe.oi_chg
    @property
    def ce_ltp_chg(self) -> float: return self.ce.ltp_chg or 0.0
    @property
    def pe_ltp_chg(self) -> float: return self.pe.ltp_chg or 0.0


def four_pillars(df: pd.DataFrame, spot: float, strikes: int = 6) -> dict:
    """
    Classify every strike in the window on both sides, then aggregate.

    The classification itself is the standard price/OI quadrant. What matters
    here is doing it across the WHOLE window rather than one strike, because
    a single strike agreeing with your bias is noise and six strikes agreeing
    is a read (§4).

    The aggregate also answers the question §21 asks and most tools do not:
    is this move FRESH PARTICIPATION or is it EXIT-DRIVEN? Price rising on
    short covering and price rising on long buildup look identical on a
    chart and are not the same event.
    """
    atm = atm_strike(spot)
    w = window(df, spot, strikes)
    rows: list[PillarRow] = []

    for _, r in w.iterrows():
        def v(col, default=0.0):
            x = r.get(col)
            return float(x) if x == x and x is not None else default

        def opt(col):
            x = r.get(col)
            return float(x) if x == x and x is not None else None

        def side(pfx) -> SideRow:
            ltp = opt(f"{pfx}_ltp")
            chg = opt(f"{pfx}_chg")
            oi = v(f"{pfx}_oi")
            oi_chg = v(f"{pfx}_chg_oi")
            oi_pct = opt(f"{pfx}_pchg_oi")
            if oi_pct is None and oi - oi_chg > 0:
                oi_pct = oi_chg / (oi - oi_chg) * 100
            pct = opt(f"{pfx}_pchg")
            if pct is None and ltp and chg is not None and ltp - chg != 0:
                pct = chg / (ltp - chg) * 100
            pillar, conf = quadrant_strict(chg, oi_chg, ltp)
            return SideRow(ltp, chg, pct, oi, oi_chg, oi_pct, pillar, conf)

        rows.append(PillarRow(
            strike=int(r["strike"]), distance=float(r["strike"]) - spot,
            ce=side("ce"), pe=side("pe"),
            is_atm=int(r["strike"]) == atm))

    from collections import Counter
    ce_counts = Counter(r.ce_pillar for r in rows)
    pe_counts = Counter(r.pe_pillar for r in rows)

    # fresh vs exit, weighted by how much OI actually moved
    fresh_w = sum(abs(r.ce_chg) for r in rows if r.ce_pillar in FRESH)
    fresh_w += sum(abs(r.pe_chg) for r in rows if r.pe_pillar in FRESH)
    exit_w = sum(abs(r.ce_chg) for r in rows if r.ce_pillar in EXIT)
    exit_w += sum(abs(r.pe_chg) for r in rows if r.pe_pillar in EXIT)
    total = fresh_w + exit_w

    if total <= 0:
        drive = "NO FLOW"
        drive_note = "nothing meaningful has changed in this window"
    elif fresh_w / total >= 0.65:
        drive = "FRESH PARTICIPATION"
        drive_note = (f"{fresh_w / total * 100:.0f}% of OI movement is new "
                      f"positions — the move has capital behind it")
    elif exit_w / total >= 0.65:
        drive = "EXIT-DRIVEN"
        drive_note = (f"{exit_w / total * 100:.0f}% of OI movement is "
                      f"positions CLOSING — this move is people leaving, not "
                      f"arriving, and tends to run out")
    else:
        drive = "MIXED"
        drive_note = (f"{fresh_w / total * 100:.0f}% fresh, "
                      f"{exit_w / total * 100:.0f}% exit — no clean read")

    atm_row = next((r for r in rows if r.is_atm), None)
    return {
        "rows": rows, "atm": atm, "atm_row": atm_row,
        "ce_counts": dict(ce_counts), "pe_counts": dict(pe_counts),
        "drive": drive, "drive_note": drive_note,
        "fresh_weight": fresh_w, "exit_weight": exit_w,
        "ce_dominant": ce_counts.most_common(1)[0][0] if ce_counts else "flat",
        "pe_dominant": pe_counts.most_common(1)[0][0] if pe_counts else "flat",
    }


def pcr_change_state(d_ce: float, d_pe: float) -> tuple[float | None, str, str]:
    """
    A change-based PCR is only a ratio when both sides are building.

    `d_pe / d_ce` is written everywhere as though it were a put-call ratio,
    and for the ordinary case — both sides adding OI — it is. The other
    three sign combinations are the problem, and one of them is genuinely
    dangerous:

      Calls building, puts unwinding. Ratio comes out NEGATIVE. Harmless in
      the sense that nobody can mistake -0.10 for a PCR, though every
      threshold it touches silently evaluates False.

      Calls unwinding, puts building. Also negative. Same.

      BOTH UNWINDING. -100,000 / -80,000 = 1.25. A perfectly ordinary,
      mildly bullish-looking PCR, produced by a market where positions are
      being closed on both sides — which is not bullish, is not bearish, and
      is not what the number appears to say. This one passes every sanity
      check because it looks completely normal.

    So the ratio is returned only for the both-building case. Otherwise the
    value is None and the state names what is actually happening, which is
    more information than a ratio could have carried anyway.
    """
    if d_ce is None or d_pe is None:
        return None, "NO DATA", "change in OI unavailable"
    try:
        d_ce, d_pe = float(d_ce), float(d_pe)
    except (TypeError, ValueError):
        return None, "NO DATA", "change in OI unreadable"
    if np.isnan(d_ce) or np.isnan(d_pe):
        return None, "NO DATA", "change in OI unavailable"

    if abs(d_ce) < 1 and abs(d_pe) < 1:
        return None, "FLAT", "almost no OI change on either side yet"

    if d_ce > 0 and d_pe > 0:
        r = d_pe / d_ce
        # A ratio far outside the readable band is a state, not a number.
        #
        # The bands — above 1.15 supportive, below 0.85 capping — assume a
        # figure near 1. A reading of 32.73 is off the end of that ruler:
        # arithmetically correct, and meaningless on the only scale anyone
        # interprets it against.
        #
        # It is NOT noise. It happens when one side builds hard while the
        # other barely moves, and that lopsidedness is real information —
        # it was seen live with puts stacking across every strike while
        # calls sat still. So the state is reported and the ratio withheld,
        # rather than printing a number that invites reading on a scale it
        # does not belong to.
        if r > 5.0 or r < 0.2:
            heavy = "PUTS" if r > 1 else "CALLS"
            light = "calls" if r > 1 else "puts"
            return None, f"{heavy} BUILDING, {light.upper()} FLAT", (
                f"{heavy.lower()} added {max(d_pe, d_ce):,.0f} against only "
                f"{min(d_pe, d_ce):,.0f} on the {light} side. The ratio "
                f"({r:.1f}) is off the readable scale — the 0.7 to 1.3 bands "
                f"assume a figure near 1 — so the lopsidedness is reported "
                f"instead. One side is being built and the other is not, "
                f"which is the part that matters.")
        lean = ("put writers more active — supportive" if r > 1.15 else
                "call writers more active — capping" if r < 0.85 else
                "both sides building at a similar pace")
        return r, "BOTH BUILDING", f"{r:.2f} — {lean}"

    if d_ce < 0 and d_pe < 0:
        return None, "BOTH UNWINDING", (
            f"calls {d_ce:+,.0f} and puts {d_pe:+,.0f} — positions being "
            f"closed on BOTH sides. A ratio here would come out positive and "
            f"look like an ordinary PCR; it would mean nothing. Nobody is "
            f"taking a side, which usually precedes a move rather than "
            f"describing one")

    if d_ce > 0 > d_pe:
        return None, "CALLS BUILDING, PUTS UNWINDING", (
            f"calls {d_ce:+,.0f}, puts {d_pe:+,.0f} — call writers stepping "
            f"in while put support is withdrawn. Leans bearish, and no ratio "
            f"can express it")

    return None, "PUTS BUILDING, CALLS UNWINDING", (
        f"puts {d_pe:+,.0f}, calls {d_ce:+,.0f} — put writers stepping in "
        f"while call resistance is withdrawn. Leans bullish, and no ratio "
        f"can express it")
