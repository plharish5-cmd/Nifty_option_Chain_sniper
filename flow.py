"""
Flow detectors — what changed since the last poll, and does it mean anything.

Every detector here needs TWO snapshots. That is the whole point: a strike
holding 50 lakh OI tells you nothing about whether it arrived this morning or
last Tuesday, and the difference is where the information is.

WHAT THESE DELIBERATELY DO NOT DO

They do not produce BUY or SELL. Your own earlier specification was explicit
that the app must not generate blind directional signals, and that was the
right call — these are observations with evidence attached, and the decision
stays yours. Anything promising a win rate from three confluences is
promising something no backtest here has established.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time as dtime

import numpy as np
import pandas as pd

import config


def _num(row, col, default=0.0) -> float:
    v = row.get(col)
    return float(v) if v == v and v is not None else default


# ═══════════════════════════════════════════════════════════════════════════
# OI iceberg — a single strike taking size in one poll
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class Iceberg:
    strike: int
    side: str                  # CE | PE
    added: float
    pct: float
    distance: float
    price_chg: float | None
    pillar: str

    @property
    def lakhs(self) -> float:
        return self.added / 100_000

    @property
    def read(self) -> str:
        who = "call" if self.side == "CE" else "put"
        if self.pillar == "short_buildup":
            act = f"{who} WRITING — premium fell as OI rose"
            effect = ("caps the upside here" if self.side == "CE"
                      else "defends the downside here")
        elif self.pillar == "long_buildup":
            act = f"{who} BUYING — premium rose with OI"
            effect = ("directional bet on upside" if self.side == "CE"
                      else "hedging or a bet on downside")
        else:
            act = f"{who} OI added, price leg unclear"
            effect = "cannot say which side did it"
        return f"{act} — {effect}"


def icebergs(df: pd.DataFrame, spot: float, min_lakhs: float = 5.0,
             span: int = 600) -> list[Iceberg]:
    """
    Single-poll OI additions above a size threshold.

    A retail trader does not add five lakh contracts to one strike in three
    minutes. Size arriving that fast at one price is an institution taking a
    position, and it is visible in the change column while being invisible in
    the OI column.

    Threshold is in lakhs of contracts and should be raised if it fires
    constantly — a detector that alerts every poll is noise with a siren.
    """
    from analytics import quadrant_strict

    w = df[(df.strike >= spot - span) & (df.strike <= spot + span)]
    out: list[Iceberg] = []
    floor = min_lakhs * 100_000

    for _, r in w.iterrows():
        for side, pfx in (("CE", "ce"), ("PE", "pe")):
            chg = _num(r, f"{pfx}_chg_oi")
            if chg < floor:
                continue
            oi = _num(r, f"{pfx}_oi")
            base = oi - chg
            pct = chg / base * 100 if base > 0 else 0.0
            ltp_chg = r.get(f"{pfx}_chg")
            pillar, _ = quadrant_strict(
                float(ltp_chg) if ltp_chg == ltp_chg and ltp_chg is not None
                else None, chg, _num(r, f"{pfx}_ltp"))
            out.append(Iceberg(int(r["strike"]), side, chg, pct,
                               float(r["strike"]) - spot,
                               float(ltp_chg) if ltp_chg == ltp_chg else None,
                               pillar))
    return sorted(out, key=lambda x: -x.added)


# ═══════════════════════════════════════════════════════════════════════════
# Straddle expansion
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class StraddleMove:
    now: float
    then: float
    minutes: float

    @property
    def change(self) -> float:
        return self.now - self.then

    @property
    def pct(self) -> float:
        return self.change / self.then * 100 if self.then else 0.0

    @property
    def state(self) -> str:
        """
        Expanding straddle = the market repricing the expected move UP.

        Sellers widen the straddle when they think the range is about to
        break. It often moves before the chart does, because the option
        writers are the ones taking the risk and they reprice first.

        Contracting = the opposite, and it is the quieter killer: premium
        bleeding out of both legs while price goes nowhere.
        """
        if self.minutes <= 0:
            return "NO HISTORY"
        rate = self.change / self.minutes
        if rate >= 4:
            return "EXPANDING FAST"
        if rate >= 1.5:
            return "EXPANDING"
        if rate <= -4:
            return "CRUSHING"
        if rate <= -1.5:
            return "CONTRACTING"
        return "STEADY"

    @property
    def note(self) -> str:
        st = self.state
        if st == "EXPANDING FAST":
            return (f"{self.change:+.0f} pts in {self.minutes:.0f} min — "
                    f"sellers are repricing the expected range upward, which "
                    f"usually precedes the move rather than following it")
        if st == "EXPANDING":
            return f"{self.change:+.0f} pts in {self.minutes:.0f} min — widening"
        if st == "CRUSHING":
            return (f"{self.change:+.0f} pts in {self.minutes:.0f} min — IV "
                    f"collapsing; option buyers lose even if direction is right")
        if st == "CONTRACTING":
            return f"{self.change:+.0f} pts — premium bleeding from both legs"
        return "straddle steady — no repricing"


def straddle_move(log: pd.DataFrame, now_premium: float,
                  now: datetime, minutes: int = 10) -> StraddleMove | None:
    """Compare the current ATM straddle against N minutes ago, from the log."""
    if log is None or log.empty or "straddle" not in log:
        return None
    d = log.copy()
    d["timestamp"] = pd.to_datetime(d["timestamp"], errors="coerce")
    d = d.dropna(subset=["timestamp"]).sort_values("timestamp")
    past = d[d["timestamp"] <= now - pd.Timedelta(minutes=minutes)]
    if past.empty:
        return None
    then = past.iloc[-1]
    prev = then.get("straddle")
    if prev is None or prev != prev or float(prev) <= 0:
        return None
    gap = (now - then["timestamp"].to_pydatetime()).total_seconds() / 60
    return StraddleMove(now_premium, float(prev), gap)


# ═══════════════════════════════════════════════════════════════════════════
# Bid-ask spread — who is absorbing whom
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class SpreadRead:
    strike: int
    side: str
    bid: float
    ask: float
    oi_chg: float

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    @property
    def spread_pct(self) -> float:
        mid = (self.ask + self.bid) / 2
        return self.spread / mid * 100 if mid > 0 else 0.0

    @property
    def read(self) -> str:
        """
        Wide spread with OI rising is worth noticing, but the common
        interpretation is stronger than the evidence supports.

        The claim is that a wide spread plus rising OI means retail buying
        into an institution. That may be true. It is equally consistent with
        a thinly traded strike where nobody is quoting tightly. This reports
        the spread and lets you weigh it — the app has no way to see WHO is
        on either side of a trade.
        """
        if self.spread_pct > 8 and self.oi_chg > 0:
            return ("wide spread while OI builds — thin liquidity here, so "
                    "your fill will be poor. Often read as retail absorbing "
                    "institutional supply, though a thin strike looks the same")
        if self.spread_pct <= 2 and self.oi_chg > 0:
            return "tight spread with OI building — liquid, clean to trade"
        if self.spread_pct > 8:
            return "wide spread — expensive to enter and exit"
        return "normal spread"


def spreads(df: pd.DataFrame, spot: float, strikes: int = 4) -> list[SpreadRead]:
    from analytics import atm_strike
    atm = atm_strike(spot)
    step = config.STRIKE_STEP
    out = []
    for i in range(-strikes, strikes + 1):
        k = atm + i * step
        row = df[df.strike == k]
        if row.empty:
            continue
        r = row.iloc[0]
        for side, pfx in (("CE", "ce"), ("PE", "pe")):
            bid, ask = _num(r, f"{pfx}_bid"), _num(r, f"{pfx}_ask")
            if bid <= 0 or ask <= 0:
                continue
            out.append(SpreadRead(int(k), side, bid, ask,
                                  _num(r, f"{pfx}_chg_oi")))
    return out


# ═══════════════════════════════════════════════════════════════════════════
# Max pain as a magnet
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class MaxPainRead:
    level: int
    spot: float
    now: datetime
    expiry_today: bool = False

    @property
    def distance(self) -> float:
        return self.level - self.spot

    @property
    def pull(self) -> str:
        """
        The magnet idea is real on expiry day and weak otherwise.

        Writers hedging toward expiry genuinely drag price toward the level
        where most options expire worthless. Away from expiry that force is
        small and easily overwhelmed — so this reports the pull as weak
        rather than pretending every afternoon has a magnet.
        """
        t = self.now.time()
        if not self.expiry_today:
            return "WEAK — the magnet effect is an expiry-day phenomenon"
        if t < dtime(13, 30):
            return "BUILDING — pull strengthens through the afternoon"
        if t < dtime(14, 30):
            return "ACTIVE — writers hedging toward the level"
        return "STRONG — final hours, pin risk highest"

    @property
    def note(self) -> str:
        d = self.distance
        direction = "above" if d > 0 else "below"
        return (f"max pain {self.level:,} sits {abs(d):,.0f} pts {direction} "
                f"spot — {self.pull}")


# ═══════════════════════════════════════════════════════════════════════════
# Level shift alerts
# ═══════════════════════════════════════════════════════════════════════════

def shift_alerts(prev_levels: dict | None, now_levels: dict) -> list[str]:
    """
    Say plainly when a wall moves, rather than leaving you to spot it.

    A level that moved is a bigger event than a level that is merely large,
    and it is invisible unless something remembers where it was.

    FEED THIS SIZE-RANKED LEVELS, not proximity-ranked ones. The question is
    "where has the biggest concentration gone", and proximity ranking answers
    a different question — R1 stays put as long as the nearest strike is
    unchanged, even when the actual wall has relocated 100 points away.
    Use rulebook.levels_by_size for this.
    """
    if not prev_levels:
        return []
    out = []
    for kind, word in (("resistance", "Resistance"), ("support", "Support")):
        old = prev_levels.get(kind) or []
        new = now_levels.get(kind) or []
        if not old or not new:
            continue
        o, n = old[0].strike, new[0].strike
        if o == n:
            continue
        up = n > o
        if kind == "resistance":
            meaning = ("ceiling moved UP — bullish shift" if up else
                       "ceiling moved DOWN — sellers pressing closer")
        else:
            meaning = ("floor moved UP — buyers stepping up" if up else
                       "floor moved DOWN — bearish shift")
        out.append(f"{word} shifted {o} → {n}: {meaning}")
    return out


# ═══════════════════════════════════════════════════════════════════════════
# Three PCRs
# ═══════════════════════════════════════════════════════════════════════════

def three_pcrs(df: pd.DataFrame, spot: float) -> dict:
    """
    Total, ATM-only, and change-in-OI — they answer different questions.

    Total PCR is dominated by deep-OTM positions that will never be tested,
    which is why it lags and why it is the one quoted on television. The
    ATM-only and change-based readings are the intraday ones.
    """
    from analytics import atm_strike
    step = config.STRIKE_STEP
    atm = atm_strike(spot)

    total_ce, total_pe = float(np.nansum(df["ce_oi"])), float(np.nansum(df["pe_oi"]))
    near = df[(df.strike >= atm - 3 * step) & (df.strike <= atm + 3 * step)]
    n_ce, n_pe = float(np.nansum(near["ce_oi"])), float(np.nansum(near["pe_oi"]))
    c_ce, c_pe = (float(np.nansum(near["ce_chg_oi"])),
                  float(np.nansum(near["pe_chg_oi"])))

    def ratio(pe, ce):
        # Both-building only. A ratio of opposite-signed sums is not a
        # PCR, and a ratio of two negatives looks like one but is not.
        if ce is None or pe is None:
            return None
        return pe / ce if (ce > 0 and pe > 0) else None

    return {
        "total": {"value": ratio(total_pe, total_ce),
                  "note": "all strikes — lags, dominated by untested far OTM"},
        "atm": {"value": ratio(n_pe, n_ce),
                "note": f"ATM +/- 3 strikes — the real intraday reading"},
        "change": {"value": ratio(c_pe, c_ce) if c_ce else None,
                   "note": "change in OI at near strikes — today's flow only",
                   "ce_chg": c_ce, "pe_chg": c_pe},
    }


# ═══════════════════════════════════════════════════════════════════════════
# Weighted level strength — fresh addition counts double
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class WeightedLevel:
    strike: int
    kind: str
    oi: float
    oi_chg: float
    distance: float

    @property
    def strength(self) -> float:
        """
        OI + (OI change x 2).

        A sensible weighting: what arrived in the last few minutes matters
        more than what has been sitting there since Tuesday. The doubling is
        arbitrary — it is a judgement about how much more, not a measurement
        — but it is a better default than treating a stale position and a
        fresh one as equal, which is what raw OI does.

        Note the change term can be NEGATIVE, so a level bleeding OI scores
        below its raw size. That is the intended behaviour.
        """
        return self.oi + self.oi_chg * config.FRESH_OI_WEIGHT

    @property
    def vs_raw(self) -> float:
        return self.strength - self.oi


def weighted_levels(df: pd.DataFrame, spot: float, n: int = 3,
                    span: int = 600,
                    spot_change: float | None = None
                    ) -> dict[str, list[WeightedLevel]]:
    """Top N supports and resistances by weighted strength, not raw OI."""
    w = df[(df.strike >= spot - span) & (df.strike <= spot + span)]
    out: dict[str, list[WeightedLevel]] = {"resistance": [], "support": []}
    if w.empty:
        return out

    for kind, side, mask in (("resistance", "ce", w.strike > spot),
                             ("support", "pe", w.strike < spot)):
        sub = w[mask]
        rows = [WeightedLevel(int(r["strike"]), kind, _num(r, f"{side}_oi"),
                              _num(r, f"{side}_chg_oi"),
                              float(r["strike"]) - spot)
                for _, r in sub.iterrows()]
        # Direction of travel re-ranks the walls. A level being built while
        # price walks into it is about to be tested; the same level while
        # price walks away is decoration.
        #
        # The multipliers NUDGE rather than override, and that is deliberate.
        # x1.5 against x1.2 will not flip a wall that is 38% larger, but it
        # will flip two of comparable size — which is the case where
        # direction actually decides which one matters. A weighting strong
        # enough to always put the nearest wall first would just be sorting
        # by distance with extra steps.
        #
        # Applied silently: the user sees a better ordering, not another
        # number to interpret.
        if spot_change is not None and abs(spot_change) >= 5:
            def _adj(lv):
                w = approach_weight(float(lv.strike), spot, spot_change, kind)
                return -(lv.strength * w["multiplier"])
            out[kind] = sorted(rows, key=_adj)[:n]
        else:
            out[kind] = sorted(rows, key=lambda x: -x.strength)[:n]
    return out


# ═══════════════════════════════════════════════════════════════════════════
# Consolidation vs trending meter
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class RegimeMeter:
    consolidation: int          # 0-100
    checks: list[tuple[str, str, bool]] = field(default_factory=list)

    @property
    def trending(self) -> int:
        return 100 - self.consolidation

    @property
    def label(self) -> str:
        if self.consolidation >= 70:
            return "CONSOLIDATION"
        if self.consolidation <= 30:
            return "TRENDING"
        return "MIXED"

    @property
    def note(self) -> str:
        n = sum(1 for _, _, hit in self.checks if hit)
        return (f"{n} of {len(self.checks)} conditions point that way. This is "
                f"a COUNT of conditions met, not a probability — 80% here "
                f"means four of five checks agree, nothing more.")


def regime_meter(df: pd.DataFrame, spot: float, pcrs: dict,
                 straddle_state: str | None = None,
                 max_pain_distance: float | None = None,
                 pcr_minutes_in_band: float | None = None,
                 require_minutes: float = 30.0) -> RegimeMeter:
    """
    A 0-100 reading built from five named conditions.

    The number is a tally, not a probability. Presenting "80% consolidation"
    as though it were calibrated would be the false precision this app has
    avoided everywhere else — so the label states plainly that it counts
    conditions.
    """
    checks: list[tuple[str, str, bool]] = []
    atm = int(round(spot / config.STRIKE_STEP) * config.STRIKE_STEP)
    # The source is explicit that PCR must hold the band for 30 minutes, not
    # merely be inside it on this poll. A single reading of 1.0 says nothing;
    # half an hour without leaving the band is a market that has stopped
    # choosing a side. Duration IS the claim.
    band_held = (pcr_minutes_in_band is None
                 or pcr_minutes_in_band >= require_minutes)

    pcr_atm = (pcrs.get("atm") or {}).get("value")
    if pcr_atm is not None:
        inside = 0.85 <= pcr_atm <= 1.15
        flat = inside and band_held
        if inside and not band_held:
            detail = (f"{pcr_atm:.2f} inside 0.85-1.15 but only "
                      f"{pcr_minutes_in_band:.0f}/{require_minutes:.0f} min — "
                      f"not yet persistent")
        else:
            detail = (f"{pcr_atm:.2f} "
                      f"{'inside' if inside else 'outside'} 0.85-1.15"
                      + (f" for {pcr_minutes_in_band:.0f} min"
                         if inside and pcr_minutes_in_band is not None else ""))
        checks.append(("ATM PCR", detail, flat))

    row = df[df.strike == atm]
    if not row.empty:
        r = row.iloc[0]
        ce_c, pe_c = _num(r, "ce_chg_oi"), _num(r, "pe_chg_oi")
        both = ce_c > 0 and pe_c > 0
        checks.append(("ATM build", f"CE {ce_c:+,.0f}, PE {pe_c:+,.0f}"
                       + (" — both sides" if both else " — one-sided"), both))

    if straddle_state:
        decaying = straddle_state in ("CRUSHING", "CONTRACTING", "STEADY")
        checks.append(("Straddle", straddle_state.lower(), decaying))

    if max_pain_distance is not None:
        close = abs(max_pain_distance) < 60
        checks.append(("Max pain", f"{abs(max_pain_distance):,.0f} pts from spot"
                       + (" — close" if close else " — far"), close))

    pcr_chg = (pcrs.get("change") or {}).get("value")
    if pcr_chg is not None:
        balanced = 0.8 <= pcr_chg <= 1.25
        checks.append(("OI-change PCR", f"{pcr_chg:.2f}"
                       + (" — balanced flow" if balanced else " — one-sided flow"),
                       balanced))

    if not checks:
        return RegimeMeter(50, [])
    hits = sum(1 for _, _, ok in checks if ok)
    return RegimeMeter(round(hits / len(checks) * 100), checks)


# ═══════════════════════════════════════════════════════════════════════════
# PCR by volume — the third variant
# ═══════════════════════════════════════════════════════════════════════════

def pcr_by_volume(df: pd.DataFrame, spot: float, strikes: int = 3) -> dict:
    """
    Put volume over call volume at the near strikes.

    Different question again from the OI variants: volume counts every
    trade, so this measures today's ACTIVITY balance rather than positions
    held. Useful as a cross-check — activity leaning one way while OI leans
    the other usually means churn rather than commitment.
    """
    from analytics import atm_strike
    step = config.STRIKE_STEP
    atm = atm_strike(spot)
    near = df[(df.strike >= atm - strikes * step)
              & (df.strike <= atm + strikes * step)]
    if near.empty or "ce_vol" not in near:
        return {}
    ce = float(np.nansum(near["ce_vol"]))
    pe = float(np.nansum(near["pe_vol"]))
    if ce <= 0:
        return {}
    return {"value": pe / ce, "ce_vol": ce, "pe_vol": pe,
            "note": "put volume / call volume at near strikes — activity, "
                    "not positions held"}


# ═══════════════════════════════════════════════════════════════════════════
# OI heatmap over time
# ═══════════════════════════════════════════════════════════════════════════

def oi_heatmap(snapshots: list[tuple[datetime, pd.DataFrame]], spot: float,
               side: str = "ce", strikes: int = 6) -> pd.DataFrame:
    """
    Strikes down, time across, OI change in each cell.

    Shows where money moved and WHEN, which no single snapshot can. Reading
    a row left to right shows one level being built or dismantled through
    the session; reading a column shows which strikes moved together.

    Fills in as the scanner runs, since it needs stored snapshots.
    """
    from analytics import atm_strike
    step = config.STRIKE_STEP
    atm = atm_strike(spot)
    wanted = [atm + i * step for i in range(-strikes, strikes + 1)]

    cols = {}
    for when, snap in snapshots:
        vals = {}
        for k in wanted:
            row = snap[snap.strike == k]
            vals[k] = _num(row.iloc[0], f"{side}_chg_oi") if not row.empty else np.nan
        cols[when.strftime("%H:%M")] = vals
    if not cols:
        return pd.DataFrame()
    out = pd.DataFrame(cols)
    out.index.name = "strike"
    return out.sort_index(ascending=False)


# ═══════════════════════════════════════════════════════════════════════════
# Custom alerts
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class AlertRule:
    """A user-defined condition, checked on every poll."""
    kind: str                    # oi_add | pcr | spot | straddle
    strike: int | None = None
    side: str | None = None
    threshold: float = 0.0
    direction: str = "above"     # above | below

    def describe(self) -> str:
        if self.kind == "oi_add":
            return (f"{self.strike} {self.side} OI adds "
                    f"{self.threshold:,.0f} in one poll")
        if self.kind == "pcr":
            return f"PCR goes {self.direction} {self.threshold:.2f}"
        if self.kind == "spot":
            return f"spot goes {self.direction} {self.threshold:,.0f}"
        if self.kind == "straddle":
            return f"ATM straddle goes {self.direction} {self.threshold:,.0f}"
        return "unknown rule"

    def check(self, df: pd.DataFrame, spot: float, pcr: float | None,
              straddle: float | None) -> str | None:
        """Return a message when the rule fires, otherwise None."""
        if self.kind == "oi_add" and self.strike:
            row = df[df.strike == self.strike]
            if row.empty:
                return None
            chg = _num(row.iloc[0], f"{(self.side or 'ce').lower()}_chg_oi")
            if chg >= self.threshold:
                return (f"{self.strike} {self.side} added {chg:,.0f} "
                        f"(rule: {self.threshold:,.0f})")
        elif self.kind == "pcr" and pcr is not None:
            if (self.direction == "above" and pcr > self.threshold) or \
               (self.direction == "below" and pcr < self.threshold):
                return f"PCR {pcr:.2f} is {self.direction} {self.threshold:.2f}"
        elif self.kind == "spot":
            if (self.direction == "above" and spot > self.threshold) or \
               (self.direction == "below" and spot < self.threshold):
                return f"spot {spot:,.0f} is {self.direction} {self.threshold:,.0f}"
        elif self.kind == "straddle" and straddle is not None:
            if (self.direction == "above" and straddle > self.threshold) or \
               (self.direction == "below" and straddle < self.threshold):
                return (f"straddle {straddle:,.0f} is {self.direction} "
                        f"{self.threshold:,.0f}")
        return None


# ═══════════════════════════════════════════════════════════════════════════
# Chain VWAP — where the average position sits
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class ChainVwap:
    call_vwap: float | None
    put_vwap: float | None
    combined: float | None
    spot: float

    @property
    def call_gap(self) -> float | None:
        return None if self.call_vwap is None else self.call_vwap - self.spot

    @property
    def put_gap(self) -> float | None:
        return None if self.put_vwap is None else self.put_vwap - self.spot

    @property
    def note(self) -> str:
        """
        Sum(strike x OI) / sum(OI) — the OI-weighted average strike.

        Read it as where the average outstanding position sits. When the call
        VWAP is far above spot, call positioning is concentrated well out of
        the money; when it drops toward spot, that positioning has moved
        close and the strikes that matter have changed.

        A blunt measure — it says nothing about who is long or short, only
        where the contracts are. Useful as a drift indicator across the
        session rather than a signal on its own.
        """
        if self.call_vwap is None or self.put_vwap is None:
            return "insufficient OI to compute"
        c, p = self.call_gap, self.put_gap
        if self.combined and self.combined > self.spot + 25:
            lean = "positioning sits ABOVE spot"
        elif self.combined and self.combined < self.spot - 25:
            lean = "positioning sits BELOW spot"
        else:
            lean = "positioning is centred on spot"
        return (f"calls average {self.call_vwap:,.0f} ({c:+,.0f}), puts "
                f"average {self.put_vwap:,.0f} ({p:+,.0f}) — {lean}")


def chain_vwap(df: pd.DataFrame, spot: float, span: int = 800) -> ChainVwap:
    w = df[(df.strike >= spot - span) & (df.strike <= spot + span)]
    if w.empty:
        return ChainVwap(None, None, None, spot)

    def wavg(col):
        oi = w[col].fillna(0).to_numpy(dtype=float)
        k = w["strike"].to_numpy(dtype=float)
        tot = oi.sum()
        return float((k * oi).sum() / tot) if tot > 0 else None

    ce, pe = wavg("ce_oi"), wavg("pe_oi")
    both = None
    if ce is not None and pe is not None:
        tc = float(w["ce_oi"].fillna(0).sum())
        tp = float(w["pe_oi"].fillna(0).sum())
        both = (ce * tc + pe * tp) / (tc + tp) if tc + tp > 0 else None
    return ChainVwap(ce, pe, both, spot)


# ═══════════════════════════════════════════════════════════════════════════
# How long has a state persisted?
# ═══════════════════════════════════════════════════════════════════════════

def state_duration(snapshots: list[tuple[datetime, pd.DataFrame]],
                   strike: int, side: str, now: datetime) -> dict:
    """
    How many consecutive polls has this strike held its current pillar?

    "24100 CE short buildup" is one observation. "24100 CE short buildup for
    nine minutes" is a different claim entirely — the first could be a single
    noisy poll, the second is a position being worked. Duration is what
    separates them and no single snapshot can show it.
    """
    from analytics import quadrant_strict
    if not snapshots:
        return {}

    seq = []
    for when, snap in sorted(snapshots, key=lambda x: x[0]):
        row = snap[snap.strike == strike]
        if row.empty:
            continue
        r = row.iloc[0]
        ltp_chg = r.get(f"{side}_chg")
        pillar, _ = quadrant_strict(
            float(ltp_chg) if ltp_chg == ltp_chg and ltp_chg is not None else None,
            _num(r, f"{side}_chg_oi"), _num(r, f"{side}_ltp"))
        seq.append((when, pillar))

    if not seq:
        return {}
    current = seq[-1][1]
    held = 0
    since = seq[-1][0]
    for when, p in reversed(seq):
        if p != current:
            break
        held += 1
        since = when
    minutes = (now - since).total_seconds() / 60
    return {"strike": strike, "side": side.upper(), "pillar": current,
            "polls": held, "minutes": minutes, "since": since,
            "note": (f"{strike} {side.upper()} has been {current.replace('_', ' ')} "
                     f"for {minutes:.0f} min ({held} polls)")}


def pcr_persistence(log: pd.DataFrame, now: datetime,
                    lo: float = 0.85, hi: float = 1.15) -> dict:
    """
    How long has PCR stayed inside the consolidation band?

    The spec asks for "PCR stuck between 0.85 and 1.15 FOR 30 MINUTES", and
    the duration is the whole point — a PCR that touches 1.0 on one poll
    says nothing, while one that has not left the band for half an hour is
    a market that has genuinely stopped choosing a side.
    """
    if log is None or log.empty or "pcr" not in log:
        return {}
    d = log.copy()
    d["timestamp"] = pd.to_datetime(d["timestamp"], errors="coerce")
    d = d.dropna(subset=["timestamp", "pcr"]).sort_values("timestamp")
    if d.empty:
        return {}

    inside_now = lo <= float(d["pcr"].iloc[-1]) <= hi
    held = 0
    since = d["timestamp"].iloc[-1]
    for _, r in d.iloc[::-1].iterrows():
        if (lo <= float(r["pcr"]) <= hi) != inside_now:
            break
        held += 1
        since = r["timestamp"]
    minutes = (now - since.to_pydatetime()).total_seconds() / 60
    return {"inside": inside_now, "polls": held, "minutes": minutes,
            "band": f"{lo}-{hi}", "current": float(d["pcr"].iloc[-1]),
            "note": (f"PCR has been {'INSIDE' if inside_now else 'OUTSIDE'} "
                     f"{lo}-{hi} for {minutes:.0f} min ({held} polls)")}


# ═══════════════════════════════════════════════════════════════════════════
# Does the chain agree with what spot is doing?
# ═══════════════════════════════════════════════════════════════════════════

def spot_chain_agreement(df: pd.DataFrame, spot: float,
                         spot_change: float, strikes: int = 3) -> dict:
    """
    Spot direction against which side is being written.

    Spot up with put OI building means put writers are comfortable selling
    downside protection into strength — they are being paid to take a risk
    they believe is small, which corroborates the move. Spot up with CALL
    writing means the opposite: writers are selling into the rally.

    When the two disagree, the move is not corroborated by positioning, and
    that is worth knowing before acting on it.
    """
    from analytics import atm_strike
    step = config.STRIKE_STEP
    atm = atm_strike(spot)
    near = df[(df.strike >= atm - strikes * step) &
              (df.strike <= atm + strikes * step)]
    if near.empty:
        return {}

    ce = float(np.nansum(near["ce_chg_oi"]))
    pe = float(np.nansum(near["pe_chg_oi"]))
    up = spot_change > 0

    if abs(spot_change) < 10:
        return {"state": "SPOT FLAT", "agrees": None,
                "note": f"spot barely moved ({spot_change:+.0f}) — nothing to "
                        f"corroborate"}

    if up and pe > ce and pe > 0:
        return {"state": "PUT WRITERS CONFIDENT", "agrees": True,
                "ce": ce, "pe": pe,
                "note": (f"spot {spot_change:+.0f} with put OI building "
                         f"{pe:+,.0f} against calls {ce:+,.0f} — writers are "
                         f"selling downside into strength, which corroborates "
                         f"the move")}
    if (not up) and ce > pe and ce > 0:
        return {"state": "CALL WRITERS CONFIDENT", "agrees": True,
                "ce": ce, "pe": pe,
                "note": (f"spot {spot_change:+.0f} with call OI building "
                         f"{ce:+,.0f} against puts {pe:+,.0f} — writers are "
                         f"selling upside into weakness, corroborating the "
                         f"move")}
    return {"state": "SPOT AND CHAIN DISAGREE", "agrees": False,
            "ce": ce, "pe": pe,
            "note": (f"spot {spot_change:+.0f} but OI is building on the "
                     f"other side (CE {ce:+,.0f}, PE {pe:+,.0f}) — the move "
                     f"is not corroborated by positioning")}


# ═══════════════════════════════════════════════════════════════════════════
# Approach-weighted level strength
# ═══════════════════════════════════════════════════════════════════════════

def approach_weight(level: float, spot: float, spot_change: float,
                    kind: str) -> dict:
    """
    Is price TRAVELLING toward this level, or away from it?

    A wall being built while price walks into it is a very different
    proposition from the same wall while price walks away. The first is
    about to be tested; the second is decoration. Level rankings ignore
    direction of travel entirely, which is why a distant wall can outrank
    the one price is about to hit.
    """
    gap = level - spot
    approaching = (gap > 0 and spot_change > 0) or (gap < 0 and spot_change < 0)
    if abs(spot_change) < 5:
        return {"approaching": None, "multiplier": 1.0,
                "note": "spot is not moving — direction of travel undefined"}
    near = abs(gap) <= 100
    if approaching and near:
        return {"approaching": True, "multiplier": 1.5,
                "note": (f"price is moving toward {level:,.0f} and is "
                         f"{abs(gap):,.0f} pts away — this level is about to "
                         f"be tested")}
    if approaching:
        return {"approaching": True, "multiplier": 1.2,
                "note": f"moving toward {level:,.0f}, {abs(gap):,.0f} pts away"}
    return {"approaching": False, "multiplier": 0.8,
            "note": (f"price is moving AWAY from {level:,.0f} — however "
                     f"large, it is not in play right now")}


# ═══════════════════════════════════════════════════════════════════════════
# Usage gates — the thresholds that turn a metric into a filter
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class ChurnFlag:
    strike: int
    side: str
    volume: float
    oi_chg: float

    @property
    def ratio(self) -> float:
        """Volume per unit of NEW open interest."""
        return self.volume / abs(self.oi_chg) if self.oi_chg else float("inf")

    @property
    def is_churn(self) -> bool:
        return self.ratio > config.CHURN_RATIO

    @property
    def note(self) -> str:
        if self.is_churn:
            return (f"{self.ratio:.1f}x volume per unit of new OI — contracts "
                    f"changing hands without positions being created. Day "
                    f"traders fighting, not conviction; treat this wall as "
                    f"soft")
        if self.ratio < 1.0:
            return (f"{self.ratio:.1f}x — big OI added on modest volume. "
                    f"Someone built quietly. This wall is more likely to hold")
        return f"{self.ratio:.1f}x — normal mix of positioning and churn"


def churn_check(df: pd.DataFrame, spot: float, strikes: int = 6
                ) -> list[ChurnFlag]:
    """
    Did the volume at this strike actually CREATE open interest?

    Everyone reads the OI change column. Far fewer check whether the volume
    behind it opened positions or just passed contracts between day traders.
    Heavy volume with little new OI is churn — that wall has no committed
    money behind it and tends to break. Low volume with a large OI build is
    the opposite: someone accumulated quietly and intends to stay.
    """
    from analytics import atm_strike
    step = config.STRIKE_STEP
    atm = atm_strike(spot)
    out = []
    for i in range(-strikes, strikes + 1):
        k = atm + i * step
        row = df[df.strike == k]
        if row.empty:
            continue
        r = row.iloc[0]
        for side, pfx in (("CE", "ce"), ("PE", "pe")):
            vol = _num(r, f"{pfx}_vol")
            chg = _num(r, f"{pfx}_chg_oi")
            if vol <= 0 or abs(chg) < 500:
                continue
            out.append(ChurnFlag(int(k), side, vol, chg))
    return out


def pcr_divergence(pcrs: dict) -> dict:
    """
    Total PCR against change-based PCR — when they disagree, say so.

    Total PCR is built from every open contract, most of them placed days
    ago and never to be tested. Change PCR is today only. Total can read 0.9
    while change reads 1.4, and anyone watching the first is looking at last
    week while the second describes this morning.

    The app already shows both. This states plainly when they point opposite
    ways, which is the moment the distinction matters.
    """
    tot = (pcrs.get("total") or {}).get("value")
    chg = (pcrs.get("change") or {}).get("value")
    if tot is None or chg is None:
        return {}

    def lean(v):
        return "bullish" if v > 1.15 else "bearish" if v < 0.85 else "neutral"

    # An off-scale change PCR gets a state, not a lean.
    #
    # This banner printed "TODAY'S flow (32.73) reads bullish". The
    # direction was right — puts were being built across every strike while
    # calls sat still — but 32.73 cannot be read on a scale whose bands are
    # 0.85 and 1.15, and quoting it that way lends a precision the number
    # does not have. The lopsidedness is the finding; the ratio is not.
    if chg > 5.0 or chg < 0.2:
        heavy = "puts" if chg > 1 else "calls"
        light = "calls" if chg > 1 else "puts"
        return {
            "diverging": True, "total": tot, "change": chg,
            "total_lean": lean(tot), "change_lean": "off scale",
            "note": (f"Total PCR {tot:.2f} reads {lean(tot)}. Today, {heavy} "
                     f"are being built while {light} sit still — so heavily "
                     f"that the ratio ({chg:.1f}) falls outside the readable "
                     f"band and is not quoted. Check the Change in OI chart: "
                     f"one-sided building is the finding, and it leans "
                     f"{'bullish' if heavy == 'puts' else 'bearish'}."),
        }

    lt, lc = lean(tot), lean(chg)
    if lt == lc:
        return {"diverging": False, "total": tot, "change": chg,
                "note": f"both PCRs read {lt} — total {tot:.2f}, "
                        f"today {chg:.2f}"}
    return {"diverging": True, "total": tot, "change": chg,
            "total_lean": lt, "change_lean": lc,
            "note": (f"Total PCR {tot:.2f} reads {lt} but TODAY'S flow "
                     f"({chg:.2f}) reads {lc}. The total is dominated by "
                     f"positions placed days ago — today's number is the "
                     f"intraday one")}


@dataclass
class VwapTrust:
    bars: int
    expected: int
    timeframe: str

    @property
    def coverage(self) -> float:
        return self.bars / self.expected * 100 if self.expected else 0.0

    @property
    def usable(self) -> bool:
        return self.coverage >= config.VWAP_MIN_COVERAGE

    @property
    def note(self) -> str:
        if self.usable:
            return (f"{self.bars} of ~{self.expected} bars "
                    f"({self.coverage:.0f}%) — anchored properly")
        return (f"ONLY {self.bars} of ~{self.expected} bars "
                f"({self.coverage:.0f}%). This VWAP is anchored from when the "
                f"app started, not from 09:15 — it is not the line other "
                f"traders are watching. Do not trade against it")


def vwap_trust(bars_used: int, now: datetime, timeframe_mins: int = 1
               ) -> VwapTrust:
    """
    Is this VWAP anchored from the open, or from whenever you launched?

    A session VWAP means nothing unless it accumulated from 09:15. Start the
    app at 10:30 and the line is a 10:30 anchor wearing a VWAP label — it
    will sit in a different place from every other screen in the market, and
    trading against it is trading against a number nobody else can see.
    """
    open_mins = config.MARKET_OPEN.hour * 60 + config.MARKET_OPEN.minute
    now_mins = min(now.hour * 60 + now.minute,
                   config.MARKET_CLOSE.hour * 60 + config.MARKET_CLOSE.minute)
    elapsed = max(now_mins - open_mins, 0)
    expected = max(elapsed // timeframe_mins, 1)
    tf = f"{timeframe_mins}m"
    return VwapTrust(bars_used, expected, tf)


def skew_inversion(skew: dict) -> dict:
    """
    Flag the inversion rather than leaving it in a metric nobody reads.

    A normal chain has OTM put IV above the equidistant call IV: people pay
    up for downside protection. When that flips, demand has moved to upside
    calls — speculation rather than hedging. It is uncommon, and it sits 200
    points out where almost nobody looks.

    Uncommon is not the same as predictive. Treat it as a condition worth
    noticing, not a signal, until your own log says otherwise.
    """
    if not skew or skew.get("diff") is None:
        return {}
    diff = skew["diff"]
    if diff < -2:
        return {"inverted": True, "diff": diff,
                "note": (f"SKEW INVERTED — {skew['call_strike']} call IV "
                         f"{skew['call_iv']:.1f} above {skew['put_strike']} "
                         f"put IV {skew['put_iv']:.1f}. Demand has shifted to "
                         f"upside calls: speculation, not hedging. Uncommon, "
                         f"and worth noticing rather than acting on")}
    if abs(diff) <= 2:
        return {"inverted": False, "flat": True, "diff": diff,
                "note": (f"skew flat ({diff:+.1f}) — hedging demand has "
                         f"thinned on both sides")}
    return {"inverted": False, "flat": False, "diff": diff,
            "note": f"normal put skew ({diff:+.1f}) — downside being hedged"}


def duration_intensity(minutes: float) -> float:
    """
    Opacity for a state label, from how long the state has held.

    A name alone treats one poll and forty minutes as the same observation.
    Shading by duration makes a worked position visually heavier than a
    single reading, which is the distinction that matters and the one a
    label cannot carry.
    """
    if minutes <= 0:
        return 0.25
    return min(0.25 + minutes / 30.0 * 0.75, 1.0)


def heat_style(df: pd.DataFrame, invert: bool = False):
    """
    Colour a numeric grid red-to-green without pulling in matplotlib.

    pandas' own background_gradient needs matplotlib, which is a plotting
    library being imported to tint about sixty table cells. The ramp below
    does the same job in a dozen lines and removes the dependency, so the
    heatmap cannot break again on a machine where matplotlib is missing.

    Scaling is symmetric around zero rather than min-to-max, so +50,000 and
    -50,000 get equal intensity in their own colour. A min-to-max ramp would
    stretch whatever range happened to be present across the full palette,
    making a grid of small additions and one tiny reduction look like a
    pitched battle between buyers and sellers.

    What it does NOT do is make a quiet session look quiet. The ramp is
    relative to each grid's own peak, so a day whose largest move was 8,000
    contracts is coloured just as strongly as one whose largest was 800,000.
    Read the numbers for size; the colour only shows shape and where within
    THIS grid the weight sits.
    """
    num = df.select_dtypes("number")
    if num.empty:
        return df.style
    peak = float(np.nanmax(np.abs(num.to_numpy()))) if num.size else 0.0
    if not peak or not np.isfinite(peak):
        return df.style

    def tint(v):
        if v is None or (isinstance(v, float) and not np.isfinite(v)):
            return ""
        try:
            f = float(v) / peak
        except (TypeError, ValueError):
            return ""
        f = max(-1.0, min(1.0, f))
        if invert:
            f = -f
        # green for positive, red for negative, alpha by magnitude
        r, g, b = (15, 157, 88) if f >= 0 else (232, 80, 58)
        a = min(abs(f), 1.0) * 0.75
        text = "#111" if abs(f) > 0.55 else "inherit"
        return f"background-color:rgba({r},{g},{b},{a:.2f});color:{text}"

    return df.style.map(tint, subset=num.columns)
