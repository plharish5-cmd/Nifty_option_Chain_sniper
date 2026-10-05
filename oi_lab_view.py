"""
oi_lab_view.py — the Options Lab readings (put/call writer ratio, PE−CE change, OI change call vs put, total OI, volume PCR, writer flow)
plus a STRIKE-BY-STRIKE call-OI / put-OI table, turned into plain BUY CE / BUY PE / WAIT / WAIT FOR REVERSAL lines for the briefing terminal.

It reuses options_lab_tab's own read_* functions (same numbers and same wording as the Options Lab tab) and reads stored snapshots only
(no web calls). Thresholds live in oi_lab_view_rules.json. Nothing here is a backtested edge: the action is a transparent points score of
the readings, shown with every check, and labelled UNVALIDATED.  Python 3.9 compatible.   Information only — not financial advice.

  compute(now) -> dict     # {"ok": True, "reads": [...], "levels": [...], "checks": [...], "total": int, "action": str, "why": str, ...}
  .venv/bin/python oi_lab_view.py          # print the three tables for the latest snapshot
"""
from __future__ import annotations

import json
import math
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
RULES_PATH = ROOT / "oi_lab_view_rules.json"
_DEF = {
    "lookback_minutes": 30,          # window for "what changed just now" (per-strike and writers ratio)
    "strikes_each_side": 3,          # strike table: ATM ± this many strikes
    "min_change_pct": 1.5,           # a strike's OI must move by this % of its OI ...
    "min_change_contracts": 3000,    # ... and at least this many contracts to count as writing / unwinding
    "writer_ratio_bull": 1.35,       # PE adds ÷ CE adds at or above this → put writers dominate (same cut-off as writers_ratio.py)
    "price_move_pts": 15,            # price must have moved this far against the writers' lean to call it "wait for reversal"
    "lean_score": 2,                 # |score| at or above → a lean
    "act_score": 4,                  # |score| at or above (and price not against it) → LEAN CE / LEAN PE side
    "buyer_freeze": ["11:30", "13:15"],
    "floor_ceiling_window_pts": 300, # look for the heaviest put OI below / call OI above spot within this distance
}


def rules() -> dict:
    try:
        r = json.loads(RULES_PATH.read_text())
    except Exception:
        r = {}
    return {**_DEF, **{k: v for k, v in r.items() if not k.startswith("_")}}


def fmt(x) -> str:
    """Indian units: Cr / L / K (same as options_lab_tab.fmt)."""
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "n/a"
    a, s = abs(x), "-" if x < 0 else "+"
    if a < 0.5:
        return "0"
    if a >= 1e7:
        return f"{s}{a / 1e7:.2f}Cr"
    if a >= 1e5:
        return f"{s}{a / 1e5:.2f}L"
    if a >= 1e3:
        return f"{s}{a / 1e3:.1f}K"
    return f"{s}{a:.0f}"


def _oi(x) -> str:
    return fmt(x).lstrip("+")


def _plain(t: str) -> str:
    return str(t).replace("**", "").strip()


_PTS = {"BULLISH": 1, "BEARISH": -1}
_SYM = {"BULLISH": "▲ BULLISH", "BEARISH": "▼ BEARISH", "NEUTRAL": "– NEUTRAL"}


def _one_expiry(df: pd.DataFrame) -> pd.DataFrame:
    import options_lab_tab as ol
    return ol._one_expiry(df)


def _move(delta: float, oi_now: float, R: dict) -> str:
    if delta is None or (isinstance(delta, float) and math.isnan(delta)):
        return "?"
    thr = max(float(R["min_change_contracts"]), float(R["min_change_pct"]) / 100.0 * abs(oi_now or 0))
    return "W" if delta >= thr else ("U" if delta <= -thr else "F")      # W = writing (OI added), U = unwinding (OI removed), F = flat


_WORD = {"W": "adding ▲", "U": "leaving ▼", "F": "flat", "?": "n/a"}


def _level_read(strike: float, spot: float, ce: str, pe: str) -> tuple:
    """(text, points)  points: +1 bullish for NIFTY, −1 bearish, 0 neutral — from what writers are doing AT this strike."""
    above, below = strike > spot + 25, strike < spot - 25
    if above:
        if ce == "W":
            return ("RESISTANCE building — call writers adding", -1)
        if ce == "U":
            return ("RESISTANCE weakening — call writers leaving, room up", 1)
        if pe == "W":
            return ("resistance unchanged · ITM put writers adding (confident, bullish)", 0)
        if pe == "U":
            return ("resistance unchanged · ITM put writers leaving (bearish)", 0)
        return ("resistance unchanged", 0)
    if below:
        if pe == "W":
            return ("SUPPORT building — put writers adding", 1)
        if pe == "U":
            return ("SUPPORT weakening — put writers leaving, risk down", -1)
        if ce == "W":
            return ("support unchanged · ITM call writers adding (confident, bearish)", 0)
        if ce == "U":
            return ("support unchanged · ITM call writers covering (bullish)", 0)
        return ("support unchanged", 0)
    if ce == "W" and pe == "W":
        return ("at spot: both adding → pinned range", 0)
    if ce == "U" and pe == "W":
        return ("at spot: calls leaving + puts adding → bullish lean", 1)
    if ce == "W" and pe == "U":
        return ("at spot: calls adding + puts leaving → bearish lean", -1)
    if ce == "U" and pe == "U":
        return ("at spot: both leaving → positions closing", 0)
    if ce == "W":
        return ("at spot: call writers adding → bearish lean", -1)
    if pe == "W":
        return ("at spot: put writers adding → bullish lean", 1)
    return ("at spot: no change", 0)


def _backtest_note() -> str:
    """Built from the SAVED NUMBERS of backtest_oi_lab.py (not its headline text, so an older saved file still reads honestly):
    OI alone · OI + break · break alone, plus the out-of-sample price-only run when it exists."""
    try:
        import config
        d = Path(config.DATA_DIR) / "backtests"
        j = json.loads((d / "oi_lab.json").read_text())
    except Exception:                                           # noqa: BLE001
        return "UNVALIDATED: this score is a transparent sum of the readings above, not a backtested edge (run backtest_oi_lab.py). A LEAN CE / LEAN PE side is where to LOOK, not a signal."
    try:
        V = j.get("variants", {})

        def pick(*need):
            for k, v in V.items():
                if all(x in k for x in need):
                    return v
            return {}

        def cell(v, h="30"):
            c = v.get(h, {}) if v else {}
            if not c or c.get("mean") is None:
                return "n/a"
            return f"{c['mean']:+.1f} pts ({c.get('verdict', '?')})"
        oi = pick("OI side", "· alone")
        oib = pick("OI side", "price-action break")
        brk = pick("price-action break ALONE")
        L = [f"BACKTEST ({j.get('sessions', '?')} sessions, {str(j.get('generated', ''))[:10]}), 30 min after the signal: "
             f"OI side alone {cell(oi)} · OI + price-action break {cell(oib)} · break alone {cell(brk)}"]
        try:
            po = json.loads((d / "oi_lab_price_only.json").read_text())
            pb = next((v for k, v in po.get("variants", {}).items() if "price-action break ALONE" in k), {})
            L.append(f"; out-of-sample price-only ({po.get('sessions', '?')} older sessions): break alone {cell(pb)}")
        except Exception:                                       # noqa: BLE001
            pass
        return "".join(L) + ". OI is not shown to add an edge — a LEAN CE / LEAN PE side is where to LOOK, not a signal."
    except Exception:                                           # noqa: BLE001
        return "BACKTEST: " + str(j.get("headline", "run backtest_oi_lab.py")) + " — a LEAN CE / LEAN PE side is where to LOOK, not a signal."


def compute(now: datetime | None = None) -> dict:
    now = now or datetime.now()
    try:
        import store
        snaps = [x for x in store.load_day(now.date()) if x[0] <= now + timedelta(minutes=1)]
    except Exception as e:                                      # noqa: BLE001
        return {"ok": False, "why": f"cannot read snapshots ({type(e).__name__}: {str(e)[:80]})"}
    return compute_from(snaps, now)


def compute_from(snaps: list, now: datetime, series=None, R: dict | None = None) -> dict:
    """Same read as compute() but from a list of (ts, spot, df) snapshots up to 'now' — used by the backtest too.
    series: optional full-day options_lab_tab.build_series(...) frame; it is cut to the snapshots given (build_series is causal)."""
    R = R or rules()
    try:
        import options_lab_tab as ol
    except Exception as e:                                      # noqa: BLE001
        return {"ok": False, "why": f"cannot import ({type(e).__name__}: {str(e)[:80]})"}
    if len(snaps) < 3:
        return {"ok": False, "why": f"only {len(snaps)} option-chain snapshot(s) today — need a few more"}
    LB = int(R["lookback_minutes"])
    rl = ol._rules()
    rl = {**rl, "lookback_minutes": LB}
    if series is not None:
        s = series[series["ts"] <= pd.Timestamp(snaps[-1][0])].reset_index(drop=True)
    else:
        s = ol.build_series(snaps, rl)
    if s is None or len(s) < 3:
        return {"ok": False, "why": "not enough usable snapshots"}
    out = {"ok": True, "time": f"{snaps[-1][0]:%H:%M}", "lookback": LB, "reads": [], "levels": [], "checks": [], "notes": []}

    # ── 1 · the Options Lab readings (same functions and wording as the tab) ──
    def safe(fn, *a):
        try:
            return fn(*a)
        except Exception as e:                                  # noqa: BLE001
            return ("NEUTRAL", f"n/a ({type(e).__name__})")
    try:
        c5 = ol.candles(s, 5)
    except Exception:                                           # noqa: BLE001
        c5 = None
    named = [("PCR (OI)", safe(ol.read_pcr, s, rl)),
             ("OI change since yesterday · Call vs Put", safe(ol.read_chg, s, rl)),
             ("Total OI · Call vs Put", safe(ol.read_total, s, rl)),
             ("PE − CE OI change · net writer lean", safe(ol.read_pe_ce, s, rl)),
             ("Volume PCR vs OI PCR", safe(ol.read_volpcr, s, rl)),
             ("Writer flow · last 5 candles", safe(ol.read_flow, c5, rl) if c5 is not None and len(c5) else ("NEUTRAL", "n/a (no candles yet)"))]
    # writers ratio over the lookback window (put adds ÷ call adds, whole chain)
    last, ago = s.iloc[-1], ol._ago(s, LB)
    dpe, dce = float(last.pe_oi - ago.pe_oi), float(last.ce_oi - ago.ce_oi)
    if dpe > 0 and dce > 0:
        ratio = dpe / dce
        if ratio >= float(R["writer_ratio_bull"]):
            wv, wt = "BULLISH", f"Put writers adding {ratio:.2f}× faster than call writers (PE {fmt(dpe)} vs CE {fmt(dce)}) → support is being built faster than resistance."
        elif ratio <= 1.0 / float(R["writer_ratio_bull"]):
            wv, wt = "BEARISH", f"Call writers adding {1 / ratio:.2f}× faster than put writers (CE {fmt(dce)} vs PE {fmt(dpe)}) → resistance is being built faster than support."
        else:
            wv, wt = "NEUTRAL", f"Both adding at a similar pace (PE {fmt(dpe)} vs CE {fmt(dce)}, ratio {ratio:.2f}) → range building."
    elif dce < 0 <= dpe:
        wv, wt = "BULLISH", f"Calls leaving ({fmt(dce)}) while puts add ({fmt(dpe)}) → short covering with support building."
    elif dpe < 0 <= dce:
        wv, wt = "BEARISH", f"Puts leaving ({fmt(dpe)}) while calls add ({fmt(dce)}) → long unwinding with resistance building."
    else:
        wv, wt = "NEUTRAL", f"Both sides leaving (CE {fmt(dce)}, PE {fmt(dpe)}) → positions closing."
    named.insert(1, (f"Writers ratio · PE adds ÷ CE adds, last {LB} min", (wv, wt)))
    for nm, (v, t) in named:
        out["reads"].append([nm, _plain(t), _SYM.get(v, v)])
        out["checks"].append([nm, _SYM.get(v, v), _PTS.get(v, 0)])

    # ── 2 · strike by strike ──
    ts_now, spot, df_now = snaps[-1]
    t_then = ts_now - timedelta(minutes=LB)
    prior = [x for x in snaps if x[0] <= t_then]
    ts_then, _, df_then = prior[-1] if prior else snaps[0]
    d = _one_expiry(df_now).copy()
    p = _one_expiry(df_then).set_index("strike")
    d["ce_d"] = d.apply(lambda r_: float(r_["ce_oi"]) - float(p["ce_oi"].get(r_["strike"], np.nan)) if r_["strike"] in p.index else np.nan, axis=1)
    d["pe_d"] = d.apply(lambda r_: float(r_["pe_oi"]) - float(p["pe_oi"].get(r_["strike"], np.nan)) if r_["strike"] in p.index else np.nan, axis=1)
    d = d.sort_values("strike").reset_index(drop=True)
    ai = int((d["strike"] - spot).abs().values.argmin())
    k = int(R["strikes_each_side"])
    out["window"] = f"{ts_then:%H:%M} → {ts_now:%H:%M}"
    for i in range(min(len(d) - 1, ai + k), max(0, ai - k) - 1, -1):          # highest strike first, like the ladder
        r_ = d.iloc[i]
        ce_m, pe_m = _move(r_["ce_d"], float(r_["ce_oi"]), R), _move(r_["pe_d"], float(r_["pe_oi"]), R)
        txt, _ = _level_read(float(r_["strike"]), float(spot), ce_m, pe_m)
        out["levels"].append([f"{r_['strike']:,.0f}" + (" ◀ ATM" if i == ai else ""), _oi(r_["ce_oi"]),
                              f"{fmt(r_['ce_d'])} {_WORD[ce_m]}", _oi(r_["pe_oi"]), f"{fmt(r_['pe_d'])} {_WORD[pe_m]}", txt])

    # ── 3 · the heaviest floor / ceiling and what the writers there are doing ──
    W = float(R["floor_ceiling_window_pts"])
    below = d[(d["strike"] <= spot) & (d["strike"] >= spot - W)]
    above = d[(d["strike"] >= spot) & (d["strike"] <= spot + W)]
    if len(below):
        f = below.loc[below["pe_oi"].astype(float).idxmax()]
        m = _move(f["pe_d"], float(f["pe_oi"]), R)
        pts = {"W": 1, "U": -1}.get(m, 0)
        word = {"W": "put writers ADDING → floor firming", "U": "put writers LEAVING → floor weakening", "F": "no change", "?": "n/a"}[m]
        out["checks"].append([f"Floor {f['strike']:,.0f} PE ({_oi(f['pe_oi'])})", f"{fmt(f['pe_d'])} in {LB} min · {word}", pts])
    if len(above):
        c = above.loc[above["ce_oi"].astype(float).idxmax()]
        m = _move(c["ce_d"], float(c["ce_oi"]), R)
        pts = {"W": -1, "U": 1}.get(m, 0)
        word = {"W": "call writers ADDING → ceiling firming", "U": "call writers LEAVING → ceiling weakening", "F": "no change", "?": "n/a"}[m]
        out["checks"].append([f"Ceiling {c['strike']:,.0f} CE ({_oi(c['ce_oi'])})", f"{fmt(c['ce_d'])} in {LB} min · {word}", pts])

    # ── 4 · the action ──
    total = int(sum(int(x[2]) for x in out["checks"]))
    n = len(out["checks"])
    px = float(last.spot - ago.spot)
    lean, act, pm = int(R["lean_score"]), int(R["act_score"]), float(R["price_move_pts"])
    if total >= lean and px <= -pm:
        action, why = "WAIT FOR REVERSAL ▲", f"writers lean bullish ({total:+d}) but NIFTY fell {abs(px):.0f} pts in {LB} min — wait for price to turn up (a candle closing above the last lower high) before buying CE."
    elif total <= -lean and px >= pm:
        action, why = "WAIT FOR REVERSAL ▼", f"writers lean bearish ({total:+d}) but NIFTY rose {abs(px):.0f} pts in {LB} min — wait for price to turn down (a candle closing below the last higher low) before buying PE."
    elif total >= act:
        action, why = "LEAN CE side", f"writers and OI lean bullish ({total:+d}) and price is not fighting it ({px:+.0f} pts in {LB} min) — CE ideas only, at a level, with a stop. UNVALIDATED: no backtest edge."
    elif total <= -act:
        action, why = "LEAN PE side", f"writers and OI lean bearish ({total:+d}) and price is not fighting it ({px:+.0f} pts in {LB} min) — PE ideas only, at a level, with a stop. UNVALIDATED: no backtest edge."
    elif total >= lean:
        action, why = "WAIT · lean ▲", f"mild bullish lean ({total:+d}) — not enough for a CE idea yet; wait for more checks to line up."
    elif total <= -lean:
        action, why = "WAIT · lean ▼", f"mild bearish lean ({total:+d}) — not enough for a PE idea yet; wait for more checks to line up."
    else:
        action, why = "WAIT", f"readings are mixed ({total:+d}) — no side has the writers behind it."
    out.update(total=total, n=n, action=action, why=why, price_move=px)
    try:
        a, b = R["buyer_freeze"]
        if a <= f"{now:%H:%M}" <= b:
            out["notes"].append(f"Your own buyer-freeze window {a}–{b} is ACTIVE — observe only, no new buys.")
    except Exception:                                           # noqa: BLE001
        pass
    out["notes"].append(_backtest_note())
    return out


# ── drawing (uses terminal_tables) ────────────────────────────────────────────────────────────────────────────────────────
def panels(d: dict, w: int) -> dict:
    import terminal_tables as tt
    P = {}
    if not d or not d.get("ok"):
        why = (d or {}).get("why", "not available")
        P["OI ACTION"] = tt.panel("OPTIONS LAB · writers and OI", [f"N/A — {why}"], w)
        return P
    n = d["n"]
    P["OI ACTION"] = tt.grid(f"OPTIONS LAB · ACTION  (OI snapshot {d['time']} · window {d.get('window', '')})", ["Check", "Reading", "Pts"],
                             [[a, b, f"{int(c):+d}" if int(c) else "0"] for a, b, c in d["checks"]], w, aligns=["l", "l", "r"], flex=1,
                             sub=f"ACTION: {d['action']}   ·   score {d['total']:+d} of ±{n}   ·   {d['why']}" + ("\n" + "  ".join(d["notes"]) if d.get("notes") else ""))
    P["OI SCOREBOARD"] = tt.grid(f"OPTIONS LAB · writers, PCR and OI scoreboard (last {d['lookback']} min where it applies)", ["Signal", "What it says", "Vote"],
                                 d["reads"], w, flex=1)
    P["OI STRIKES"] = tt.grid(f"OPTIONS LAB · strike by strike · call OI vs put OI · change {d.get('window', '')}",
                              ["Strike", "CE OI", f"CE change", "PE OI", f"PE change", "What it means at this level"], d["levels"], w,
                              aligns=["r", "r", "l", "r", "l", "l"], flex=5, sub="adding ▲ = writers building a wall · leaving ▼ = writers unwinding · flat = under the change threshold in oi_lab_view_rules.json")
    return P


if __name__ == "__main__":
    import terminal_tables as tt
    dd = compute(datetime.now())
    ps = panels(dd, 112)
    for k in ("OI ACTION", "OI SCOREBOARD", "OI STRIKES"):
        print("\n".join(ps.get(k, [])))
    print("Information only — not financial advice.")
