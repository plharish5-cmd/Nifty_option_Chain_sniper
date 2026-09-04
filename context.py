"""
context.py — everything a view needs, in one object.

THE PROBLEM THIS SOLVES
───────────────────────
Today all eighteen tab bodies execute on every rerun. You look at one and
the other seventeen still run: Four Pillars rebuilds its table, the flow
detectors recompute, the Timeline scorecard reloads thirty CSV files. Click
a checkbox and it all happens again.

A sidebar router fixes that — but only one view running is exactly what
breaks the current code, because tabs quietly feed each other. Tab 18 reads
`chain_levels_now` that tab 12 wrote. Under a router, tab 12 may never have
run.

So this object is the contract. Everything shared is computed ONCE in the
orchestrator, before routing, and handed to whichever view is showing. A
view reads from `ctx` and never from a module-level global.

That single rule is what makes the split safe. It is also the part that
takes the time — moving files is trivial, finding every cross-tab
dependency is not.

MIGRATION ORDER THAT KEEPS THE APP WORKING
──────────────────────────────────────────
  1. Add this file and populate it in dashboard.py. Change nothing else.
     The app behaves identically; you have only added an unused object.
  2. Move ONE view to views/ and have it take ctx. Everything else stays.
  3. Repeat per view, confirming after each.
  4. Add the router LAST, once every view already takes ctx.

Adding the router first is tempting and wrong: you would have eighteen
half-migrated views and no way to see any of them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pandas as pd


@dataclass
class Ctx:
    """
    Everything computed once per rerun, before any view renders.

    Fields are typed loosely on purpose. This is a transport object, and
    tightening the types would mean importing half the app into this module
    just to name them — which would reintroduce exactly the coupling the
    split is meant to remove.
    """

    # ── the chain and the moment ────────────────────────────────────────
    df: pd.DataFrame = None            # option chain, greeks attached
    spot: float = 0.0
    now: datetime = None
    expiry: str = ""
    dte: float = 0.0
    demo: bool = False

    # ── derived reads ───────────────────────────────────────────────────
    read: Any = None                   # analytics.Read
    decision: Any = None               # the four-layer decision bar
    alignment: Any = None              # multi-timeframe alignment
    s1_result: Any = None              # Strategy 1, computed once globally

    # ── price action ────────────────────────────────────────────────────
    pa_frames: dict = field(default_factory=dict)
    index_candles: pd.DataFrame = None
    futures_candles: pd.DataFrame = None
    prev_close: float | None = None

    # ── levels, in the three families ───────────────────────────────────
    #
    # Named rather than merged because a chain wall built this morning and a
    # swing high from three weeks ago behave nothing alike when price
    # arrives. Merging them loses the part that tells you whether to trust
    # the level.
    chain_levels: dict = field(default_factory=dict)      # S1_ops / R1_ops
    master_levels: dict = field(default_factory=dict)     # S1..S4 / R1..R4
    pdc_levels: dict = field(default_factory=dict)        # PDC_S* / PDC_R*
    all_levels: dict = field(default_factory=dict)        # the three merged
    master_frame: pd.DataFrame = None                     # master_levels.csv
    ohlc: pd.DataFrame = None                             # daily history

    # ── view building sources ───────────────────────────────────────────
    vb: dict = field(default_factory=dict)

    # ── centre strike, used by half the chain panels ─────────────────────
    centre: int = 0
    centre_mode: str = ""

    # ── anything not yet migrated ───────────────────────────────────────
    #
    # A deliberate escape hatch. Without it the first migration stalls the
    # moment a view needs something not yet on the contract, and the honest
    # options at that point are to add a field or to give up. This lets you
    # move a view today and tidy the field in later.
    #
    # Things left here are not finished. Grep for `ctx.extra[` before
    # calling the refactor done.
    extra: dict = field(default_factory=dict)

    # ── helpers views actually use ───────────────────────────────────────

    def level(self, name: str) -> float | None:
        return self.all_levels.get(name)

    @property
    def has_vwap(self) -> bool:
        f = self.pa_frames.get("15m")
        return (f is not None and "vwap" in getattr(f, "columns", [])
                and f["vwap"].notna().any())

    def missing(self) -> list[str]:
        """
        What is absent, so a view can degrade instead of raising.

        A view that checks this can say "levels unavailable" in its own
        panel. A view that does not will raise, and the guard in the
        orchestrator will catch it — which is a worse experience but not a
        dead app.
        """
        out = []
        if self.df is None or (hasattr(self.df, "empty") and self.df.empty):
            out.append("option chain")
        if not self.spot:
            out.append("spot")
        if not self.pa_frames:
            out.append("price action frames")
        if self.ohlc is None or (hasattr(self.ohlc, "empty") and self.ohlc.empty):
            out.append("daily OHLC")
        if not self.all_levels:
            out.append("levels")
        return out


def build(**kw) -> Ctx:
    """
    Construct a Ctx, ignoring anything it does not have a field for.

    Unknown keys go to `extra` rather than raising. During a migration the
    orchestrator and the contract drift apart constantly, and a hard failure
    on an unexpected keyword would mean the app dies at import time on a
    trading morning because someone added a variable.
    """
    known = {f for f in Ctx.__dataclass_fields__}
    fields = {k: v for k, v in kw.items() if k in known}
    extra = {k: v for k, v in kw.items() if k not in known}
    c = Ctx(**fields)
    c.extra.update(extra)
    return c
