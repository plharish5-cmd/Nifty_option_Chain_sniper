"""
The strict rulebook — implemented literally, kept separate on purpose.

Every rule here is a direct transcription. Where the rulebook and the wider
app disagree, BOTH readings are produced and the disagreement is shown rather
than resolved, because the app has no basis for deciding which is right and
quietly picking one would hide a real question.

THE THREE THINGS THIS ADDS THAT THE APP DID NOT HAVE

  TRUE SUPPORT / RESISTANCE   compares put-side OI% against call-side OI% AT
      THE SAME STRIKE. The app judged each side on its own, which cannot see
      a level where both sides are aggressive — and that is precisely the
      CONTESTED case worth knowing about.

  ATM-ONLY BIAS   one comparison, Call OI against Put OI at the money.
      Deliberately narrow. The app's own bias reads five inputs, which is
      more informative but less falsifiable; this one can be checked by eye
      against the NSE screen in three seconds.

  OI DENSITY BETWEEN LEVELS   thin open interest between two supports means
      a break of the first drops fast to the second, because nothing is
      positioned in between to slow it. Level lists alone cannot show this.

THE ONE CONFLICT WORTH KNOWING ABOUT

The rulebook ranks R1/R2/R3 by OI SIZE — highest, second highest, third.
The app ranks by PROXIMITY, so R1 is the first wall price meets. On a
typical chain these name different strikes. Neither is wrong: size answers
"where is the most money", proximity answers "what do I hit first". Both
are returned, labelled.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

import config

#: OI% difference at a strike below which neither side "clearly dominates"
DOMINANCE_MARGIN = 8.0   # overridden from rules.json below
#: minimum OI% either side must show before a strike counts as CONTESTED.
#: Without this, two sides both drifting +1.7% read as a contest — which is
#: just a quiet strike, and labelling every quiet strike CONTESTED makes the
#: label meaningless where it matters.
CONTEST_MIN_PCT = 10.0   # overridden from rules.json below
#: ATM OI ratio bands for bias strength
BIAS_SLIGHT = 1.15       # overridden from rules.json below
BIAS_STRONG = 1.60


def _num(row, col, default=0.0) -> float:
    v = row.get(col)
    return float(v) if v == v and v is not None else default


def _opt(row, col):
    v = row.get(col)
    return float(v) if v == v and v is not None else None


def _pct(row, side: str) -> float | None:
    """OI % change for one side, computed if NSE did not supply it."""
    p = _opt(row, f"{side}_pchg_oi")
    if p is not None:
        return p
    oi, chg = _num(row, f"{side}_oi"), _num(row, f"{side}_chg_oi")
    base = oi - chg
    return chg / base * 100 if base > 0 else None


# ═══════════════════════════════════════════════════════════════════════════
# §4 — bias from ATM alone
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class AtmBias:
    atm: int
    call_oi: float
    put_oi: float

    @property
    def ratio(self) -> float:
        return self.call_oi / self.put_oi if self.put_oi else float("inf")

    @property
    def label(self) -> str:
        """
        BULLISH when put OI at ATM exceeds call OI. Nothing else considered.

        Narrow by design. This is the reading you can verify against the NSE
        screen in three seconds, which makes it falsifiable in a way the
        app's five-input bias is not.
        """
        r = self.ratio
        if r >= BIAS_STRONG:
            return "STRONG BEARISH"
        if r >= BIAS_SLIGHT:
            return "SLIGHT BEARISH"
        if r <= 1 / BIAS_STRONG:
            return "STRONG BULLISH"
        if r <= 1 / BIAS_SLIGHT:
            return "SLIGHT BULLISH"
        return "NEUTRAL"

    @property
    def strength(self) -> str:
        r = self.ratio
        gap = max(r, 1 / r) if r else 1.0
        return ("STRONG" if gap >= BIAS_STRONG else
                "MODERATE" if gap >= BIAS_SLIGHT else "WEAK")

    @property
    def why(self) -> str:
        return (f"ATM {self.atm}: call OI {self.call_oi:,.0f} vs put OI "
                f"{self.put_oi:,.0f} — ratio {self.ratio:.2f}")


def atm_bias(chain: pd.DataFrame, spot: float) -> AtmBias | None:
    atm = int(round(spot / config.STRIKE_STEP) * config.STRIKE_STEP)
    row = chain[chain.strike == atm]
    if row.empty:
        return None
    r = row.iloc[0]
    return AtmBias(atm, _num(r, "ce_oi"), _num(r, "pe_oi"))


# ═══════════════════════════════════════════════════════════════════════════
# §5 — true support / true resistance / contested, by same-strike OI%
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class StrikeVerdict:
    strike: int
    distance: float
    ce_pct: float | None
    pe_pct: float | None
    ce_oi: float
    pe_oi: float
    label: str
    why: str


def true_levels(chain: pd.DataFrame, spot: float,
                strikes: int = 6) -> list[StrikeVerdict]:
    """
    Classify each strike by comparing the TWO SIDES against each other.

    The rule the app was missing: a level is only true support when the
    put-side OI% clearly exceeds the call-side OI% at that same strike, and
    the call side is not matching the aggression. Judging one side alone
    cannot distinguish a defended floor from a strike where both sides are
    piling in — and those behave completely differently.
    """
    step = config.STRIKE_STEP
    atm = int(round(spot / step) * step)
    out = []
    for i in range(-strikes, strikes + 1):
        k = atm + i * step
        row = chain[chain.strike == k]
        if row.empty:
            continue
        r = row.iloc[0]
        cp, pp = _pct(r, "ce"), _pct(r, "pe")
        ce_oi, pe_oi = _num(r, "ce_oi"), _num(r, "pe_oi")

        if cp is None or pp is None:
            label, why = "UNKNOWN", "OI % change unavailable on one side"
        elif (cp >= CONTEST_MIN_PCT and pp >= CONTEST_MIN_PCT
              and abs(cp - pp) < DOMINANCE_MARGIN):
            label = "CONTESTED"
            why = (f"both sides building hard and neither dominates — "
                   f"CE {cp:+.1f}% vs PE {pp:+.1f}%")
        elif cp > 0 and pp > 0 and max(cp, pp) < CONTEST_MIN_PCT:
            label = "QUIET"
            why = (f"both sides drifting — CE {cp:+.1f}%, PE {pp:+.1f}% — "
                   f"neither is committing")
        elif pp - cp >= DOMINANCE_MARGIN and pp > 0:
            label = "TRUE SUPPORT"
            why = (f"put side {pp:+.1f}% clearly exceeds call side "
                   f"{cp:+.1f}% at this strike")
        elif cp - pp >= DOMINANCE_MARGIN and cp > 0:
            label = "TRUE RESISTANCE"
            why = (f"call side {cp:+.1f}% clearly exceeds put side "
                   f"{pp:+.1f}% at this strike")
        elif cp < 0 and pp < 0:
            label = "BOTH UNWINDING"
            why = f"both sides leaving — CE {cp:+.1f}%, PE {pp:+.1f}%"
        else:
            label = "NO CLEAR SIDE"
            why = f"CE {cp:+.1f}% vs PE {pp:+.1f}% — margin too small"

        out.append(StrikeVerdict(k, k - spot, cp, pp, ce_oi, pe_oi, label, why))
    return out


# ═══════════════════════════════════════════════════════════════════════════
# §3 — ranked-by-size levels, and the density between them
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class RankedLevel:
    rank: int
    strike: int
    oi: float
    distance: float


def levels_by_size(chain: pd.DataFrame, spot: float, n: int = 4,
                   span: int = 600) -> dict[str, list[RankedLevel]]:
    """R1..R4 and S1..S4 ranked by OI SIZE, as the rulebook specifies."""
    w = chain[(chain.strike >= spot - span) & (chain.strike <= spot + span)]
    out = {"resistance": [], "support": []}
    if w.empty:
        return out

    above = w[w.strike > spot].nlargest(n, "ce_oi") if "ce_oi" in w else w.head(0)
    below = w[w.strike < spot].nlargest(n, "pe_oi") if "pe_oi" in w else w.head(0)
    for i, (_, r) in enumerate(above.iterrows(), 1):
        out["resistance"].append(RankedLevel(i, int(r["strike"]),
                                             _num(r, "ce_oi"),
                                             float(r["strike"]) - spot))
    for i, (_, r) in enumerate(below.iterrows(), 1):
        out["support"].append(RankedLevel(i, int(r["strike"]),
                                          _num(r, "pe_oi"),
                                          float(r["strike"]) - spot))
    return out


@dataclass
class Density:
    from_strike: int
    to_strike: int
    side: str
    mean_oi: float
    reference_oi: float

    @property
    def ratio(self) -> float:
        return self.mean_oi / self.reference_oi if self.reference_oi else 0.0

    @property
    def label(self) -> str:
        r = self.ratio
        if r < 0.15:
            return "VERY THIN"
        if r < 0.35:
            return "THIN"
        if r < 0.7:
            return "MODERATE"
        return "DENSE"

    @property
    def note(self) -> str:
        gap = abs(self.to_strike - self.from_strike)
        if self.label in ("VERY THIN", "THIN"):
            return (f"{self.label.lower()} OI across {gap} pts — if "
                    f"{self.from_strike} breaks, little is positioned in "
                    f"between to slow the move to {self.to_strike}")
        return (f"{self.label.lower()} OI between them — a break of "
                f"{self.from_strike} would meet resistance on the way")


def density_between(chain: pd.DataFrame, a: int, b: int, side: str) -> Density | None:
    """
    How much OI sits BETWEEN two levels.

    The rulebook's point, and one a ranked list cannot express: two supports
    200 points apart with nothing in between is a very different risk from
    two supports with heavy OI stacked between them. The first gaps, the
    second grinds.
    """
    lo, hi = min(a, b), max(a, b)
    inner = chain[(chain.strike > lo) & (chain.strike < hi)]
    col = f"{side}_oi"
    if inner.empty or col not in chain:
        return None
    ends = chain[chain.strike.isin([lo, hi])]
    ref = float(ends[col].mean()) if not ends.empty else 0.0
    return Density(a, b, side, float(inner[col].mean()), ref)


# ═══════════════════════════════════════════════════════════════════════════
# §6 §8 — tightened build-up and pinning
# ═══════════════════════════════════════════════════════════════════════════

def build_state(row, side: str) -> str:
    """
    BUILD-UP requires OI change AND OI % both positive — the rulebook is
    explicit that it is both, not either. A large absolute change on a tiny
    base, or a percentage move on noise, is not a build-up.
    """
    chg = _num(row, f"{side}_chg_oi")
    pct = _pct(row, side)
    if pct is None:
        return "UNKNOWN"
    if chg > 0 and pct > 0:
        return "BUILD-UP"
    if chg < 0 and pct < 0:
        return "UNWINDING"
    return "FLAT"


@dataclass
class PinVerdict:
    atm: int
    label: str
    ce_state: str
    pe_state: str
    ce_pct: float | None
    pe_pct: float | None
    why: list[str] = field(default_factory=list)


def pinned_strict(chain: pd.DataFrame, spot: float,
                  price_range: float | None = None) -> PinVerdict | None:
    """
    PINNED requires both sides building AND neither unwinding AND price
    locked in a range — all three, per the rulebook.

    The app previously checked only that both were building, which would
    label a strike pinned while one side was quietly leaving.
    """
    atm = int(round(spot / config.STRIKE_STEP) * config.STRIKE_STEP)
    row = chain[chain.strike == atm]
    if row.empty:
        return None
    r = row.iloc[0]
    ce_s, pe_s = build_state(r, "ce"), build_state(r, "pe")
    cp, pp = _pct(r, "ce"), _pct(r, "pe")

    why = []
    both_building = ce_s == "BUILD-UP" and pe_s == "BUILD-UP"
    neither_unwinding = "UNWINDING" not in (ce_s, pe_s)

    if both_building:
        why.append(f"both sides building at {atm} — CE {cp:+.1f}%, PE {pp:+.1f}%")
    else:
        why.append(f"not both building — CE {ce_s}, PE {pe_s}")
    if not neither_unwinding:
        why.append("one side is UNWINDING — that disqualifies a pin")
    if price_range is not None:
        if price_range < 45:
            why.append(f"price locked in {price_range:,.0f} pts")
        else:
            why.append(f"price moving {price_range:,.0f} pts — not locked")

    locked = price_range is None or price_range < 45
    label = ("PINNED / MARKET-MAKER TRAP"
             if both_building and neither_unwinding and locked
             else "NOT PINNED")
    return PinVerdict(atm, label, ce_s, pe_s, cp, pp, why)


# ═══════════════════════════════════════════════════════════════════════════
# Thresholds as DATA — loaded from rules.json
# ═══════════════════════════════════════════════════════════════════════════

import json
from pathlib import Path

RULES_PATH = Path(__file__).parent / "rules.json"

#: fallbacks matching rules.json, used if the file is missing or unreadable
_DEFAULTS = {
    "bias_engine": {"neutral_bias_ratio": 1.15, "strong_bias_ratio": 1.60},
    "true_level_engine": {"true_level_gap_threshold": 8.0,
                          "contested_build_threshold": 10.0,
                          "contested_gap_threshold": 8.0},
    "oi_state_engine": {"min_oi_change": 1.0, "min_oi_pct": 0.0},
    "trap_engine": {"dual_buildup_threshold": 10.0,
                    "price_lock_points": 45.0, "major_unwind_pct": -5.0},
    "breakout_engine": {"accept_closes": 2, "next_level_min_pct": 10.0,
                        "old_level_max_pct": -5.0},
    "trend_strength_engine": {"dominant_side_strong_pct": 20.0},
    "actionability_engine": {"min_live_polls": 2},
    "density_engine": {"very_thin_ratio": 0.15, "thin_ratio": 0.35,
                       "moderate_ratio": 0.70},
    "session_reversal_matrix": {
        "gap_pts": 25.0, "round_number_step": 500, "round_proximity_pts": 25.0,
        "extremum_proximity_pts": 25.0, "chain_wall_proximity_pts": 25.0,
        "doji_body_pct": 0.12, "hammer_wick_ratio": 2.0, "hammer_opp_ratio": 0.6,
        "inverted_hammer_wick_ratio": 2.0, "inverted_hammer_opp_ratio": 0.6,
        "engulf_ratio": 1.1, "default_sl_buffer_pts": 1.0,
    },
    "chart_patterns": {
        "swing_lookback": 2, "double_extremum_tol_pct": 0.0035,
        "min_pattern_depth_pts": 30.0, "v_drop_min_pts": 35.0,
        "v_rebound_min_ratio": 0.50, "a_rise_min_pts": 35.0,
        "a_drop_min_ratio": 0.50,
    },
}


def load_rules(path: Path | None = None) -> dict:
    """
    Read the thresholds from rules.json.

    Kept as data rather than constants because every one of these numbers is
    a judgement, not a fact. A threshold buried in Python is a rule you
    cannot see, cannot argue with, and cannot change without an edit that
    risks breaking something else. In a file you can read them all in
    twenty seconds and change one without touching code.

    Falls back to the built-in defaults if the file is missing, and REPORTS
    that rather than failing silently — a scanner running on defaults while
    you believe it is using your tuned values is worse than one that stops.
    """
    p = path or RULES_PATH
    if not p.exists():
        return {**_DEFAULTS, "_source": "built-in defaults — rules.json not found"}
    try:
        raw = json.loads(p.read_text())
    except Exception as exc:                                # noqa: BLE001
        return {**_DEFAULTS, "_source": f"built-in defaults — rules.json "
                                        f"unreadable: {exc}"}
    out = {}
    for section, defaults in _DEFAULTS.items():
        block = raw.get(section, {})
        out[section] = {k: block.get(k, v) for k, v in defaults.items()}
    out["_source"] = str(p)
    out["_version"] = raw.get("version", "?")
    return out


RULES = load_rules()


def rule(section: str, key: str):
    return RULES.get(section, {}).get(key, _DEFAULTS[section][key])


# ═══════════════════════════════════════════════════════════════════════════
# Literal breakout and trend-strength engines
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class LiteralVerdict:
    label: str
    conditions: list[tuple[str, bool]] = field(default_factory=list)
    why: str = ""

    @property
    def met(self) -> int:
        return sum(1 for _, ok in self.conditions if ok)

    @property
    def total(self) -> int:
        return len(self.conditions)

    @property
    def confidence(self) -> str:
        if not self.total:
            return "WEAK"
        r = self.met / self.total
        return "STRONG" if r == 1.0 else "MODERATE" if r >= 0.6 else "WEAK"


def real_breakout(chain: pd.DataFrame, old_strike: int, next_strike: int,
                  side: str, price_accepts: bool,
                  price_crossed: bool = True) -> LiteralVerdict:
    """
    REAL_BREAKOUT exactly as specified — five conditions, all required.

    Every condition is listed with whether it held, so a FAKE verdict shows
    which leg failed rather than just asserting the conclusion. The
    distinction the spec draws is that FAKE is not "the opposite of real" —
    it is specifically price crossing WITH the old level unwinding but the
    next level failing to build. Positions left and did not re-form
    anywhere, which is an exit, not a migration.
    """
    def get(strike, pfx):
        row = chain[chain.strike == strike]
        if row.empty:
            return None, None
        r = row.iloc[0]
        return _num(r, f"{pfx}_chg_oi"), _pct(r, pfx)

    old_chg, old_pct = get(old_strike, side)
    nxt_chg, nxt_pct = get(next_strike, side)
    if old_chg is None or nxt_chg is None:
        return LiteralVerdict("NO DATA", [], "strike missing from the chain")

    old_max = rule("breakout_engine", "old_level_max_pct")
    nxt_min = rule("breakout_engine", "next_level_min_pct")

    conds = [
        (f"old level {old_strike} OI change < 0 ({old_chg:+,.0f})",
         old_chg < 0),
        (f"old level OI% below {old_max} ({old_pct:+.1f}%)"
         if old_pct is not None else "old level OI% unavailable",
         old_pct is not None and old_pct <= old_max),
        (f"next level {next_strike} OI change > 0 ({nxt_chg:+,.0f})",
         nxt_chg > 0),
        (f"next level OI% above {nxt_min} ({nxt_pct:+.1f}%)"
         if nxt_pct is not None else "next level OI% unavailable",
         nxt_pct is not None and nxt_pct >= nxt_min),
        ("price accepts the move", bool(price_accepts)),
    ]

    old_unwinding = old_chg < 0
    next_building = nxt_chg > 0 and (nxt_pct or 0) >= nxt_min

    if all(ok for _, ok in conds):
        label = "REAL_BREAKOUT"
        why = ("the barrier is unwinding while the next strike builds — "
               "positions migrated rather than merely vanished")
    elif price_crossed and old_unwinding and not next_building:
        label = "FAKE_BREAKOUT"
        why = ("price moved and the old level unwound, but NOTHING built at "
               "the next strike — positions left without re-forming, which "
               "is an exit, not a migration")
    elif not price_crossed:
        label = "NO_BREAK"
        why = "the level has not been tested meaningfully"
    else:
        label = "UNCONFIRMED"
        why = f"{sum(1 for _, ok in conds if ok)} of 5 conditions met"
    return LiteralVerdict(label, conds, why)


def trend_strength(chain: pd.DataFrame, old_strike: int, next_strike: int,
                   side: str, price_moves_with_shift: bool) -> LiteralVerdict:
    """
    STRONGER_TREND requires migration AND aggression AND price agreement.

    The spec's point, and it is a good one: price moving is the weakest of
    the three. A trend backed by positions actually relocating is a
    different animal from one where price drifted and nobody moved.
    """
    def get(strike, pfx):
        row = chain[chain.strike == strike]
        if row.empty:
            return None, None
        r = row.iloc[0]
        return _num(r, f"{pfx}_chg_oi"), _pct(r, pfx)

    old_chg, old_pct = get(old_strike, side)
    nxt_chg, nxt_pct = get(next_strike, side)
    if old_chg is None or nxt_chg is None:
        return LiteralVerdict("NO DATA", [], "strike missing from the chain")

    strong = rule("trend_strength_engine", "dominant_side_strong_pct")
    conds = [
        (f"old level {old_strike} unwinding ({old_chg:+,.0f})", old_chg < 0),
        (f"next level {next_strike} building ({nxt_chg:+,.0f})", nxt_chg > 0),
        (f"winning side OI% above {strong} ({(nxt_pct or 0):+.1f}%)",
         (nxt_pct or 0) >= strong),
        ("price moves with the shift", bool(price_moves_with_shift)),
    ]
    met = sum(1 for _, ok in conds if ok)
    if met == 4:
        label, why = "STRONGER_TREND", ("migration, aggression and price all "
                                        "agree")
    elif met <= 2:
        label, why = "WEAKER_TREND", ("the move lacks structural confirmation "
                                      "— price without position migration")
    else:
        label, why = "MIXED", f"{met} of 4 conditions met"
    return LiteralVerdict(label, conds, why)


def display_contract_check(sources: dict[str, str] | str) -> list[tuple[str, bool]]:
    """
    Does the app actually show everything the contract requires?

    A spec nobody checks drifts from the code within a week, so this greps
    for a marker per required element.

    Scans EVERY module, not just the dashboard. The first version checked
    dashboard.py alone and reported four elements missing that were all
    present — the layer names and trust states are constructed in
    market_state.py and only rendered by the dashboard. A checker that
    reports false failures gets ignored, which is worse than no checker.
    """
    if isinstance(sources, str):
        sources = {"dashboard.py": sources}
    blob = "\n".join(sources.values())
    required = {
        "bias": "Bias",
        "structure": "Structure",
        "breakout_quality": "Breakout quality",
        "actionability": "Actionability",
        "atm_battle": "ATM ",
        "support_resistance_ladder": "chain_levels",
        "four_pillars": "four_pillars",
        "trap_or_pinning_state": "pinning",
        "old_level_vs_next_level_shift": "neighbour_building",
        "what_changed_1m_3m_5m": "what_changed",
        "why_explanations": "Why each layer",
        "pre_market_context_only": "CONTEXT ONLY",
        "data_is_stale": "STALE CHAIN",
        "no_live_confirmation": "UNCONFIRMED",
    }
    return [(k, marker in blob) for k, marker in required.items()]


# Apply the loaded ruleset over the module constants, so both the literal
# engines and the earlier ones read the same numbers from the same file.
DOMINANCE_MARGIN = rule("true_level_engine", "true_level_gap_threshold")
CONTEST_MIN_PCT = rule("true_level_engine", "contested_build_threshold")
BIAS_SLIGHT = rule("bias_engine", "neutral_bias_ratio")
BIAS_STRONG = rule("bias_engine", "strong_bias_ratio")


# ═══════════════════════════════════════════════════════════════════════════
# Session Reversal Matrix (15m) Integration
# ═══════════════════════════════════════════════════════════════════════════

def evaluate_session_reversal_matrix(
    df_15m: pd.DataFrame | None,
    prev_close: float | None = None,
    spot: float | None = None,
    levels: Any = None,
    chain_df: pd.DataFrame | None = None,
    now: datetime | None = None,
):
    """
    Evaluates the 10 Mentor reversal conditions strictly on closed 15m candles.
    Delegates to dedicated session_reversal_matrix module.
    """
    import session_reversal_matrix as _srm
    return _srm.evaluate_session_reversals(
        df_15m=df_15m,
        prev_close=prev_close,
        spot=spot,
        levels=levels,
        chain_df=chain_df,
        now=now,
    )
