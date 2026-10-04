"""
decision_desk.py — 🎯 Decision Desk: ONE screen to decide, at the top of the Decision Cockpit tab.

How a discretionary NIFTY options trader reads a screen, top to bottom:
  1. THE DECISION   BUY CE / BUY PE / BUY ON PULLBACK / WAIT / NO TRADE — with the reason
                    and what would flip it.
  2. CONFLUENCE     every engine built in this app votes, weighted:
                      Structure (15m external) · SMC Trade Desk (5m) · 5-Rule model ·
                      Smart OI (PE−CE per candle) · Writers trend (PE−CE) · OI build-up ·
                      PCR trend · Volume PCR · IV / Vega · Gamma regime (context) ·
                      FII/DII cash (context) · Daily draw (validated target)
  3. LEVELS LADDER  every key level above/below spot with distance.
  4. STRIKES        best CE and best PE side by side: premium, delta, ₹ at target & SL, R:R.
                    The active side is highlighted; the other is "only if the picture flips".

Reuses the engines — no duplicated logic. Weights/thresholds in strategy1_rules.json →
"decision_desk". Closed candles only. Information only — not financial advice.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from pathlib import Path

import numpy as np
import pandas as pd

import config

ROOT = Path(__file__).parent
RULES_PATH = ROOT / "strategy1_rules.json"
_DEF = {
    "weights": {"structure": 2.0, "smc_desk": 1.5, "five_rule": 2.0, "smart_oi": 1.0, "writers": 1.0,
                "oi_buildup": 1.0, "pcr": 0.75, "vol_pcr": 0.5, "iv_vega": 0.5, "fii_dii": 0.5},
    "lean_threshold": 0.20, "act_threshold": 0.35, "no_new_trades_after": "15:00",
    "fallback_sl_pts": 30.0, "hold_hours": 1.0, "sub10_iv_warning": 10.0, "min_target_r": 1.5,
    "five_rule_can_trigger": "auto",   # auto = only if backtest_five_rule.py passed · true · false
}
NAVY, PANEL, GRID, GOLD, WHITE, MUTED = "#0b1a33", "#10223f", "#1e3358", "#d4af37", "#f1f5f9", "#94a3b8"
GREEN, RED, AMBER, BLUE, PURPLE = "#22c55e", "#ef4444", "#f59e0b", "#38bdf8", "#a78bfa"


def _rules() -> dict:
    try:
        r = json.loads(RULES_PATH.read_text()).get("decision_desk", {})
    except Exception:
        r = {}
    out = {**_DEF, **{k: v for k, v in r.items() if not k.startswith("_")}}
    out["weights"] = {**_DEF["weights"], **r.get("weights", {})}
    return out


def _band() -> tuple[float, float]:
    try:
        r = json.loads((ROOT / "strike_select_rules.json").read_text())
        return float(r.get("premium_band_min", 200)), float(r.get("premium_band_max", 300))
    except Exception:
        return 200.0, 300.0


def _clean(df: pd.DataFrame | None) -> pd.DataFrame | None:
    if df is None or len(df) == 0:
        return None
    d = df.copy()
    if "timestamp" not in d and isinstance(d.index, pd.DatetimeIndex):
        d = d.reset_index().rename(columns={d.index.name or "index": "timestamp"})
    if "timestamp" not in d:
        return None
    d["timestamp"] = pd.to_datetime(d["timestamp"])
    if "closed" in d:
        d = d[d["closed"].fillna(True).astype(bool)]
    return d.sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)


@dataclass
class Vote:
    key: str
    name: str
    vote: int            # +1 bull, -1 bear, 0 neutral
    strength: float      # 0..1
    reading: str
    available: bool = True
    context_only: bool = False


@dataclass
class Desk:
    decision: str = "WAIT"
    side: str = "NONE"
    score: float = 0.0
    headline: str = ""
    flip: str = ""
    warnings: list = field(default_factory=list)
    votes: list = field(default_factory=list)
    levels: list = field(default_factory=list)       # (price, name, kind)
    plan: dict = field(default_factory=dict)
    strikes: dict = field(default_factory=dict)       # "CE"/"PE" -> dict
    spot: float = 0.0


# ═══════════════════════════════════════════════════════════════════════════
# Votes
# ═══════════════════════════════════════════════════════════════════════════
_V = {"BULLISH": 1, "BEARISH": -1}


def _safe(fn, key, name, ctx=False):
    try:
        return fn()
    except Exception as e:                                   # noqa: BLE001
        return Vote(key, name, 0, 0, f"unavailable ({type(e).__name__})", available=False, context_only=ctx)


def collect(spot: float, chain_df: pd.DataFrame | None, pa_frames: dict | None, now: datetime,
            snaps: list | None, dte: float, rr: dict) -> tuple[Desk, dict]:
    import smc_trade_desk as std
    import smc_five_rule as sfr
    d = Desk(spot=spot)
    f = {k: _clean((pa_frames or {}).get(k)) for k in ("1m", "5m", "15m")}
    df5, df15, df1 = f["5m"], f["15m"], f["1m"]
    ctx = {"desk": None, "five": None, "series": None, "gex": None, "oip": None}

    # 1-3 · price structure engines
    if df5 is not None and len(df5) >= 20:
        today5 = std._today(df5)
        try:
            ctx["desk"] = std.compute(today5, df15)
        except Exception:
            pass
        try:
            ctx["five"] = sfr.evaluate(df1, df5, df15, now,
                                       sfr.best_daily(df1 if df1 is not None and len(df1) > len(df5) * 3 else df5))
        except Exception:
            pass
    five, desk = ctx["five"], ctx["desk"]

    def v_struct():
        e = five.external
        v = _V.get(e.trend, 0)
        return Vote("structure", "Structure · 15m external", v, 1.0 if v else 0,
                    f"{e.trend.lower()} · swing {e.swing_low:,.0f}–{e.swing_high:,.0f} (flips only on a CLOSE beyond)")

    def v_desk():
        a = desk.action
        v = 1 if a == "BUY" else -1 if a == "SELL" else _V.get(desk.trend_tf, 0)
        s = 1.0 if a in ("BUY", "SELL") else 0.4 if a == "WAIT" else 0.2
        return Vote("smc_desk", "SMC Trade Desk · 5m", v, s, f"{a}: {desk.headline}")

    def v_five():
        v = _V.get(five.side, 0)
        ev = sfr.edge_validation()
        tag = "" if ev and ev["passed"] else (" · ⚠ unproven (backtest not run)" if ev is None else
                                               f" · ⚠ unproven (backtest avg {ev['avg_R']:+.2f}R, no edge vs random)")
        return Vote("five_rule", "5-Rule SMC model", v, five.score / 5, f"{five.score}/5 · {five.verdict}"
                    + (f" · next: {five.next_needed}" if five.next_needed and 'TAKE' not in five.verdict else "") + tag)

    for fn, k, n in ((v_struct, "structure", "Structure · 15m external"), (v_desk, "smc_desk", "SMC Trade Desk · 5m"),
                     (v_five, "five_rule", "5-Rule SMC model")):
        d.votes.append(_safe(fn, k, n) if (five or desk) else Vote(k, n, 0, 0, "needs ≥ 20 closed 5m candles", False))

    # 4-9 · option-chain engines (stored snapshots)
    import options_lab_tab as ol
    r_ol = ol._rules()
    if snaps:
        try:
            ctx["series"] = ol.build_series(snaps, r_ol)
        except Exception:
            ctx["series"] = None
    s = ctx["series"]
    mapv = lambda x: {"BULLISH": 1, "BEARISH": -1}.get(x, 0)

    def from_read(key, name, fn, strength=1.0):
        def run():
            v, txt = fn()
            return Vote(key, name, mapv(v), strength if mapv(v) else 0, txt.replace("**", ""))
        return _safe(run, key, name)

    if s is not None and len(s) >= 3:
        c = ol.candles(s, 3)
        d.votes.append(from_read("smart_oi", "Smart OI · PE−CE per candle", lambda: ol.read_flow(c, r_ol)))
        d.votes.append(from_read("writers", "Writers trend · PE−CE", lambda: ol.read_pe_ce(s, r_ol)))
        d.votes.append(from_read("pcr", "PCR trend", lambda: ol.read_pcr(s, r_ol), 0.8))
        d.votes.append(from_read("vol_pcr", "Volume PCR", lambda: ol.read_volpcr(s, r_ol), 0.6))
        d.votes.append(from_read("iv_vega", "IV / Vega", lambda: ol.read_vega(s, r_ol), 0.6))
    else:
        for k, n in (("smart_oi", "Smart OI · PE−CE per candle"), ("writers", "Writers trend · PE−CE"), ("pcr", "PCR trend"),
                     ("vol_pcr", "Volume PCR"), ("iv_vega", "IV / Vega")):
            d.votes.append(Vote(k, n, 0, 0, "no option-chain snapshots today", False))

    def v_oip():
        import oi_profile_panel as op
        r = op._rules()
        o = op.profile(snaps, len(snaps) - 1, 30, None, None, None, r["oi_multiplier"])
        ctx["oip"] = o
        sc, lab = op.sentiment(o, r)
        return Vote("oi_buildup", "OI build-up · last 30 min", 1 if sc > 0.2 else -1 if sc < -0.2 else 0, min(abs(sc), 1),
                    f"{lab} ({sc:+.2f}) · calls {ol.fmt(o['dce'])}, puts {ol.fmt(o['dpe'])}")
    d.votes.append(_safe(v_oip, "oi_buildup", "OI build-up · last 30 min") if snaps and len(snaps) > 3
                   else Vote("oi_buildup", "OI build-up · last 30 min", 0, 0, "no snapshots", False))

    def v_gex():
        g = ol.gex_by_strike(snaps[-1][2], snaps[-1][1], r_ol)
        ctx["gex"] = g
        flip = ol.gamma_flip(g)
        reg = ("above flip → slow / pinning: fade extremes, smaller targets" if flip and spot > flip else
               "below flip → fast moves: breakouts follow through" if flip else "flip not found")
        return Vote("gamma", "Gamma regime (context)", 0, 0, f"flip {flip:,.0f} · {reg}" if flip else reg, context_only=True)
    d.votes.append(_safe(v_gex, "gamma", "Gamma regime (context)", True) if snaps
                   else Vote("gamma", "Gamma regime (context)", 0, 0, "no snapshots", False, True))

    def v_fii():
        import fii_dii_panel as fd
        v, txt = fd.read_cash(fd.load_cash())
        return Vote("fii_dii", "FII / DII cash (context)", mapv(v), 0.6 if mapv(v) else 0, txt.replace("**", "").split(". Last 5")[0] + ".",
                    context_only=False)
    d.votes.append(_safe(v_fii, "fii_dii", "FII / DII cash (context)"))

    def v_draw():
        lv = five.levels.get("daily_draw") if five else None
        if lv:
            return Vote("daily_draw", "Daily draw (validated target)", 0, 0,
                        f"{lv[1]} {lv[0]:,.0f} likely to be taken today ({lv[2] * 100:.0f}% vs {lv[3] * 100:.0f}% chance)",
                        context_only=True)
        return Vote("daily_draw", "Daily draw (validated target)", 0, 0,
                    "no validated draw today (only BEAR→PDL passed your NIFTY backtest)", context_only=True)
    d.votes.append(_safe(v_draw, "daily_draw", "Daily draw (validated target)", True))

    def v_of():
        import nifty_orderflow as nf
        f = nf.today_flow(5)
        if f is None:
            return Vote("orderflow", "NIFTY 50 order flow", 0, 0, "no recording today — run orderflow_recorder.py",
                        available=False, context_only=True)
        try:
            w = float(json.loads(nf.RULES_PATH.read_text()).get("decision_desk_weight", 0.0))
        except Exception:
            w = 0.0
        code = f.state.get("code", 0)
        v = 1 if code > 0 else -1 if code < 0 else 0
        valid = any(x.get("passed") for x in nf.validation().get("signals", {}).values())
        rr["weights"]["orderflow"] = w
        return Vote("orderflow", "NIFTY 50 order flow" + ("" if (w > 0 and valid) else " (context · untested)"),
                    v, min(1.0, abs(code) / 2), f"{f.state['label']} · day CVD ₹{f.totals['cvd']:+,.0f} Cr · "
                    f"last candle breadth {int(f.candles['breadth_up'].iloc[-1])}↑/{int(f.candles['breadth_dn'].iloc[-1])}↓",
                    context_only=not (w > 0 and valid))
    d.votes.append(_safe(v_of, "orderflow", "NIFTY 50 order flow", True))

    def v_garch():
        import garch_range as gr
        b = gr.box_for(now.date())
        if b is None:
            return Vote("garch", "GARCH expected move (context)", 0, 0, "needs ≥300 daily closes", available=False,
                        context_only=True)
        z = (spot - b["ref_close"]) / (b["pts1"] or 1)
        where = "outside 2σ — extreme" if abs(z) >= 2 else "outside 1σ — stretched" if abs(z) >= 1 else "inside 1σ"
        if abs(z) >= 1:
            d.warnings.append(f"NIFTY is {z:+.1f}σ from yesterday's close (GARCH box ±{b['pts1']:,.0f} pts) — the day's normal "
                              f"move is largely used up; chasing further {'up' if z > 0 else 'down'} has less room.")
        return Vote("garch", "GARCH expected move (context)", 0, 0,
                    f"1σ {b['lo1']:,.0f}–{b['hi1']:,.0f} (±{b['pts1']:,.0f}) · 2σ ±{b['pts2']:,.0f} · now {z:+.1f}σ ({where})",
                    context_only=True)
    d.votes.append(_safe(v_garch, "garch", "GARCH expected move (context)", True))
    return d, ctx


# ═══════════════════════════════════════════════════════════════════════════
# Decision
# ═══════════════════════════════════════════════════════════════════════════
def decide(d: Desk, ctx: dict, now: datetime, chain_df: pd.DataFrame | None, rr: dict) -> None:
    W = rr["weights"]
    num = den = 0.0
    for v in d.votes:
        if not v.available or v.context_only or v.key not in W:
            continue
        num += W[v.key] * v.vote * v.strength
        den += W[v.key]
    d.score = num / den if den else 0.0
    desk, five = ctx.get("desk"), ctx.get("five")
    struct = next((v for v in d.votes if v.key == "structure"), None)
    late = now.time() >= dtime(*map(int, rr["no_new_trades_after"].split(":")))
    side = "BULL" if d.score >= rr["lean_threshold"] else "BEAR" if d.score <= -rr["lean_threshold"] else "NONE"
    sgn = 1 if side == "BULL" else -1 if side == "BEAR" else 0
    d.side = side

    # IV warning for option buyers
    try:
        if chain_df is not None and len(chain_df):
            atm = chain_df.iloc[(pd.to_numeric(chain_df["strike"]) - d.spot).abs().values.argmin()]
            iv = float(np.nanmean([atm.get("ce_iv", np.nan), atm.get("pe_iv", np.nan)]))
            if iv and iv < rr["sub10_iv_warning"]:
                d.warnings.append(f"ATM IV {iv:.1f}% — sub-10 regime: option buyers get little premium per point; size down or skip.")
    except Exception:
        pass
    if struct and struct.available and sgn and struct.vote == -sgn:
        d.warnings.append("Score leans one way but 15m EXTERNAL structure points the other — counter-trend, treat as WAIT.")
    if desk and desk.phase == "CONSOLIDATION":
        d.warnings.append("5m is consolidating — trade only a candle CLOSE out of the box.")

    import smc_five_rule as sfr
    trig = rr.get("five_rule_can_trigger", "auto")
    ev = sfr.edge_validation()
    five_ok = (trig is True or str(trig).lower() == "true" or
               (str(trig).lower() == "auto" and ev is not None and ev["passed"]))
    five_take = five_ok and five is not None and "TAKE" in five.verdict and _V.get(five.side, 0) == sgn
    desk_go = desk is not None and ((desk.action == "BUY" and sgn > 0) or (desk.action == "SELL" and sgn < 0))
    opt = "CE" if sgn > 0 else "PE"
    if late:
        d.decision, d.headline = "NO TRADE", f"After {rr['no_new_trades_after']} — manage open positions only."
    elif side == "NONE" or (struct and struct.available and struct.vote == -sgn):
        d.decision = "WAIT"
        d.headline = "Engines disagree — no edge right now." if side == "NONE" else \
            "Short-term flow fights the 15m structure — wait for them to align."
    elif abs(d.score) >= rr["act_threshold"] and (five_take or desk_go):
        d.decision = f"BUY {opt}"
        d.headline = f"{'Bullish' if sgn > 0 else 'Bearish'} confluence {abs(d.score) * 100:.0f}% and the execution model is green."
    else:
        d.decision = f"BUY {opt} ON PULLBACK"
        why = desk.headline if desk else "wait for the entry zone"
        d.headline = f"{'Bullish' if sgn > 0 else 'Bearish'} lean {abs(d.score) * 100:.0f}% — but not at a good price yet: {why}"

    # plan (spot levels) — prefer 5-Rule plan, then Trade Desk plan
    if five and five.entry and five.sl and _V.get(five.side, 0) == sgn:
        tp1 = five.levels.get("TP1") or five.tp
        d.plan = dict(entry=five.entry, sl=five.sl, tp1=tp1, tp2=five.tp,
                      src="5-Rule model" if five_ok else "5-Rule model (unproven — levels only)",
                      tp1_name=five.levels.get("TP1_name", "nearest swing"))
    elif desk and desk.entry and desk.sl:
        d.plan = dict(entry=desk.entry, sl=desk.sl, tp1=desk.tp1, tp2=desk.tp2, src="SMC Trade Desk",
                      tp1_name=desk.tp1_name)
    if five and five.external and five.external.trend in ("BULLISH", "BEARISH"):
        e = five.external
        d.flip = (f"Idea is wrong on a 15m CLOSE below {e.swing_low:,.0f} (protected low)" if sgn > 0 else
                  f"Idea is wrong on a 15m CLOSE above {e.swing_high:,.0f} (protected high)" if sgn < 0 else
                  f"Bullish only above {e.swing_high:,.0f}, bearish only below {e.swing_low:,.0f} (15m closes)")


# ═══════════════════════════════════════════════════════════════════════════
# Levels ladder
# ═══════════════════════════════════════════════════════════════════════════
def ladder(d: Desk, ctx: dict, chain_df: pd.DataFrame | None) -> None:
    L = []
    five, desk, g, oip = ctx.get("five"), ctx.get("desk"), ctx.get("gex"), ctx.get("oip")
    if five:
        e = five.external
        if e.swing_high:
            L.append((e.swing_high, "15m external swing high", "res"))
        if e.swing_low:
            L.append((e.swing_low, "15m external swing low", "sup"))
        for k, v in five.levels.items():
            if isinstance(v, (int, float)) and k in ("PDH", "OR high", "IB high"):
                L.append((v, k, "res"))
            if isinstance(v, (int, float)) and k in ("PDL", "OR low", "IB low"):
                L.append((v, k, "sup"))
    if desk:
        for lo, hi, _, lab in desk.supply[:2]:
            L.append(((lo + hi) / 2, f"Supply ({lab}) {lo:,.0f}–{hi:,.0f}", "res"))
        for lo, hi, _, lab in desk.demand[:2]:
            L.append(((lo + hi) / 2, f"Demand ({lab}) {lo:,.0f}–{hi:,.0f}", "sup"))
    if chain_df is not None and len(chain_df):
        try:
            import options_lab_tab as ol
            c = ol._one_expiry(chain_df)
            above, below = c[c.strike > d.spot], c[c.strike < d.spot]
            if len(above):
                k = above.loc[pd.to_numeric(above.ce_oi).idxmax()]
                L.append((float(k.strike), f"Call wall (max CE OI {ol.fmt(float(k.ce_oi))})", "res"))
            if len(below):
                k = below.loc[pd.to_numeric(below.pe_oi).idxmax()]
                L.append((float(k.strike), f"Put wall (max PE OI {ol.fmt(float(k.pe_oi))})", "sup"))
        except Exception:
            pass
    if oip and oip.get("max_pain"):
        L.append((oip["max_pain"], "Max pain", "mag"))
    if g is not None:
        try:
            import options_lab_tab as ol
            fl = ol.gamma_flip(g)
            if fl:
                L.append((fl, "Gamma flip", "mag"))
        except Exception:
            pass
    L = [x for x in L if x[0] and abs(x[0] - d.spot) <= 600]
    L.sort(key=lambda x: -x[0])
    merged = []
    for price, name, kind in L:                               # merge levels within 4 pts
        if merged and abs(merged[-1][0] - price) <= 4:
            p0, n0, k0 = merged[-1]
            merged[-1] = (p0, f"{n0} · {name}", k0 if k0 == kind else "mag")
        else:
            merged.append((price, name, kind))
    d.levels = merged


# ═══════════════════════════════════════════════════════════════════════════
# Strike cards (CE and PE)
# ═══════════════════════════════════════════════════════════════════════════
def strikes(d: Desk, chain_df: pd.DataFrame | None, dte: float, rr: dict) -> None:
    if chain_df is None or not len(chain_df):
        return
    import greeks_scenario as gs
    import options_lab_tab as ol
    c = ol._one_expiry(chain_df).copy()
    c["strike"] = pd.to_numeric(c["strike"], errors="coerce")
    lo_b, hi_b = _band()
    mid_b = (lo_b + hi_b) / 2
    t = max(dte, 0.05) / 365
    lot = int(getattr(config, "LOT_SIZE", 65))
    cap = float(getattr(config, "CAPITAL", 20000))
    res_above = sorted([x[0] for x in d.levels if x[0] > d.spot + 10])
    sup_below = sorted([x[0] for x in d.levels if x[0] < d.spot - 10], reverse=True)
    for opt in ("CE", "PE"):
        side = opt.lower()
        cc = c.dropna(subset=[f"{side}_ltp"])
        cc = cc[pd.to_numeric(cc[f"{side}_ltp"], errors="coerce") > 1]
        if cc.empty:
            continue
        cc = cc.assign(prem=pd.to_numeric(cc[f"{side}_ltp"]))
        inb = cc[(cc.prem >= lo_b) & (cc.prem <= hi_b)]
        pick = (inb if len(inb) else cc).iloc[((inb if len(inb) else cc).prem - mid_b).abs().values.argmin()]
        k, prem = float(pick.strike), float(pick.prem)
        iv = pd.to_numeric(pick.get(f"{side}_iv"), errors="coerce")
        if pd.isna(iv) or iv <= 0:
            iv = pd.to_numeric(pick.get("iv_used"), errors="coerce")
        iv = float(iv) if pd.notna(iv) and iv > 0 else 12.0
        call = opt == "CE"
        g = gs.greeks_at(d.spot, k, iv, t, call)
        active = (d.side == "BULL" and call) or (d.side == "BEAR" and not call)
        sgn = 1 if call else -1
        if active and d.plan:
            entry = sum(d.plan["entry"]) / 2
            sl = d.plan.get("sl")
            cands = [x for x in (d.plan.get("tp1"), d.plan.get("tp2")) if x]
            src = d.plan.get("src", "")
        else:
            entry, sl = d.spot, None
            cands = res_above if call else sup_below
            sl = (sup_below[0] if call and sup_below else res_above[0] if not call and res_above else None)
            src = "nearest levels"
        if sl is None or (sl - entry) * sgn > -10:
            sl = entry - sgn * rr["fallback_sl_pts"]
        risk_pts = abs(entry - sl)
        lvls = sorted(set(list(cands) + (res_above if call else sup_below)), key=lambda x: sgn * x)
        tgt = next((x for x in lvls if (x - entry) * sgn >= rr["min_target_r"] * risk_pts), None)   # first level ≥ min R
        if tgt is None:
            tgt = entry + sgn * max(2 * risk_pts, 40)
        ent = gs.scenario(d.spot, k, iv, t, call, prem, entry - d.spot, 0.0, 0.0)
        p_entry = ent["end"]
        up = gs.scenario(d.spot, k, iv, t, call, prem, tgt - d.spot, rr["hold_hours"], 0.0)
        dn = gs.scenario(d.spot, k, iv, t, call, prem, sl - d.spot, rr["hold_hours"] / 2, 0.0)
        gain, loss = up["end"] - p_entry, p_entry - dn["end"]
        cap_lim = 30.0
        try:
            cap_lim = float(json.loads((ROOT / "strike_select_rules.json").read_text()).get("capital_max_lot_pct", 30))
        except Exception:
            pass
        d.strikes[opt] = dict(
            entry=entry, p_entry=p_entry, cap_lim=cap_lim,
            strike=k, premium=prem, in_band=bool(len(inb)), delta=abs(g["delta"]), theta=g["theta"], iv=iv,
            lot_cost=prem * lot, cap_pct=prem * lot / cap * 100 if cap else np.nan,
            tgt=tgt, sl=sl, prem_tgt=up["end"], prem_sl=dn["end"], gain_lot=gain * lot, loss_lot=loss * lot,
            rr=gain / loss if loss > 0 else None, active=active, src=src, theta_hour=g["theta"] / 24)


# ═══════════════════════════════════════════════════════════════════════════
# Render
# ═══════════════════════════════════════════════════════════════════════════
def render(spot: float, chain_df: pd.DataFrame | None, pa_frames: dict | None, now: datetime | None = None,
           dte: float = 1.0, prev_close: float | None = None) -> None:
    import streamlit as st
    import store
    rr = _rules()
    now = now or datetime.now()

    @st.cache_data(ttl=60, show_spinner=False)
    def _snaps(day_str: str, n_files: int):
        return store.load_day(pd.Timestamp(day_str).date())

    day_dir = Path(config.SNAPSHOT_DIR) / now.strftime("%Y-%m-%d")
    n_files = len(list(day_dir.glob("*.meta.json"))) if day_dir.exists() else 0
    snaps = _snaps(now.strftime("%Y-%m-%d"), n_files) if n_files else []
    if chain_df is None and snaps:
        chain_df = snaps[-1][2]
    if (not spot) and snaps:
        spot = float(snaps[-1][1])

    with st.spinner("Building confluence…"):
        d, ctx = collect(spot, chain_df, pa_frames, now, snaps, dte, rr)
        decide(d, ctx, now, chain_df, rr)
        ladder(d, ctx, chain_df)
        strikes(d, chain_df, dte, rr)

    # ── 1 · THE DECISION ───────────────────────────────────────────────────
    col = GREEN if d.decision.startswith("BUY CE") else RED if d.decision.startswith("BUY PE") else \
        AMBER if d.decision == "WAIT" else "#64748b"
    chg = f" ({(spot - prev_close):+,.0f} / {(spot / prev_close - 1) * 100:+.2f}%)" if prev_close else ""
    bull_n = sum(1 for v in d.votes if v.available and not v.context_only and v.vote > 0)
    bear_n = sum(1 for v in d.votes if v.available and not v.context_only and v.vote < 0)
    neut_n = sum(1 for v in d.votes if v.available and not v.context_only and v.vote == 0)
    gauge = int((d.score + 1) / 2 * 100)
    plan_html = ""
    if d.plan:
        p = d.plan
        plan_html = (f"<div style='margin-top:10px;display:flex;gap:18px;flex-wrap:wrap;color:{WHITE}'>"
                     f"<span>Entry <b style='color:{BLUE}'>{p['entry'][0]:,.0f}–{p['entry'][1]:,.0f}</b></span>"
                     f"<span>SL <b style='color:{RED}'>{p['sl']:,.0f}</b></span>"
                     + (f"<span>TP1 <b style='color:{GREEN}'>{p['tp1']:,.0f}</b> <small style='color:{MUTED}'>{p.get('tp1_name') or ''}</small></span>" if p.get("tp1") else "")
                     + (f"<span>TP2 <b style='color:{GREEN}'>{p['tp2']:,.0f}</b></span>" if p.get("tp2") else "")
                     + f"<span style='color:{MUTED}'>plan from {p['src']}</span></div>")
    warn = "".join(f"<div style='color:#fcd34d;margin-top:4px'>⚠ {w}</div>" for w in d.warnings)
    st.markdown(f"""
    <div style="background:{NAVY};border:2px solid {col};border-radius:12px;padding:16px 20px;margin-bottom:12px">
      <div style="display:flex;justify-content:space-between;flex-wrap:wrap;gap:10px;align-items:center">
        <div><div style="color:{GOLD};font-weight:800;letter-spacing:0.6px">🎯 DECISION DESK · NIFTY {spot:,.1f}{chg} · {now:%H:%M}</div>
          <div style="color:{MUTED};font-size:0.85rem;margin-top:2px">{bull_n} bullish · {bear_n} bearish · {neut_n} neutral engines · DTE {dte:.1f}</div></div>
        <div style="background:{col};color:#000;font-weight:900;font-size:1.6rem;padding:6px 22px;border-radius:8px">{d.decision}</div>
      </div>
      <div style="margin-top:12px;height:12px;border-radius:6px;background:linear-gradient(90deg,{RED},#334155 50%,{GREEN});position:relative">
        <div style="position:absolute;left:calc({gauge}% - 7px);top:-4px;width:14px;height:20px;border-radius:4px;background:{WHITE};border:2px solid {NAVY}"></div></div>
      <div style="display:flex;justify-content:space-between;color:{MUTED};font-size:0.75rem"><span>BEARISH</span>
        <span>confluence score {d.score:+.2f}</span><span>BULLISH</span></div>
      <div style="color:{WHITE};font-size:1.08rem;margin-top:10px"><b>{d.headline}</b></div>
      {plan_html}
      {f"<div style='color:{PURPLE};margin-top:8px'>🔄 {d.flip}</div>" if d.flip else ""}
      {warn}
    </div>""", unsafe_allow_html=True)

    # ── 2 · STRIKES (CE | PE) ──────────────────────────────────────────────
    cA, cB = st.columns(2)
    for colm, opt in ((cA, "CE"), (cB, "PE")):
        s = d.strikes.get(opt)
        with colm:
            if not s:
                st.info(f"No {opt} quotes available.")
                continue
            c2 = (GREEN if opt == "CE" else RED) if s["active"] else "#475569"
            tag = ("ACTIVE SIDE" if s["active"] else "ONLY IF THE PICTURE FLIPS")
            rrtxt = f"{s['rr']:.1f} : 1" if s["rr"] else "—"
            st.markdown(f"""
            <div style="background:{NAVY};border:2px solid {c2};border-radius:10px;padding:14px 16px;opacity:{1 if s['active'] else 0.78}">
              <div style="display:flex;justify-content:space-between;align-items:center">
                <span style="color:{WHITE};font-size:1.25rem;font-weight:800">{s['strike']:,.0f} {opt}</span>
                <span style="background:{c2};color:#000;font-weight:800;padding:2px 10px;border-radius:5px;font-size:0.8rem">{tag}</span></div>
              <div style="color:{MUTED};font-size:0.8rem;margin-bottom:8px">{'in ₹%.0f–%.0f band' % _band() if s['in_band'] else 'closest to ₹%.0f–%.0f band' % _band()} · plan from {s['src']}</div>
              <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px;color:{WHITE}">
                <div><div style="color:{MUTED};font-size:0.75rem">PREMIUM NOW → AT ENTRY {s['entry']:,.0f}</div><b>₹{s['premium']:,.1f} → ₹{s['p_entry']:,.1f}</b></div>
                <div><div style="color:{MUTED};font-size:0.75rem">DELTA</div><b>{s['delta']:.2f}</b></div>
                <div><div style="color:{MUTED};font-size:0.75rem">1 LOT</div><b>₹{s['lot_cost']:,.0f}</b> <small style="color:{RED if s['cap_pct'] > s['cap_lim'] else MUTED}">{s['cap_pct']:.0f}% of capital{' ⚠ above ' + format(s['cap_lim'], '.0f') + '% limit' if s['cap_pct'] > s['cap_lim'] else ''}</small></div>
                <div><div style="color:{MUTED};font-size:0.75rem">AT TARGET {s['tgt']:,.0f}</div><b style="color:{GREEN}">₹{s['prem_tgt']:,.1f} (+₹{s['gain_lot']:,.0f}/lot)</b></div>
                <div><div style="color:{MUTED};font-size:0.75rem">AT SL {s['sl']:,.0f}</div><b style="color:{RED}">₹{s['prem_sl']:,.1f} (−₹{s['loss_lot']:,.0f}/lot)</b></div>
                <div><div style="color:{MUTED};font-size:0.75rem">R : R · THETA/HR</div><b>{rrtxt}</b> · <small>{s['theta_hour']:+.1f}</small></div>
              </div></div>""", unsafe_allow_html=True)
    st.caption(f"₹ per lot measured FROM THE ENTRY ZONE (not from spot). Target = first level at least {rr['min_target_r']:g}R away. "
               "Premiums = Black-Scholes repricing from the live LTP (move + ~1h of decay, IV unchanged). "
               "Deeper what-ifs (late move, IV crush): 🎯 Strike Selection → Greeks Scenario Lab.")

    # ── 3 · CONFLUENCE MATRIX + LADDER ─────────────────────────────────────
    left, right = st.columns([1.55, 1])
    with left:
        W = rr["weights"]
        rows = ""
        for v in d.votes:
            if not v.available:
                badge, bc = "N/A", "#334155"
            elif v.context_only:
                badge, bc = "CONTEXT", "#475569"
            else:
                badge = "BULL" if v.vote > 0 else "BEAR" if v.vote < 0 else "NEUTRAL"
                bc = GREEN if v.vote > 0 else RED if v.vote < 0 else "#64748b"
            w = W.get(v.key)
            rows += (f"<tr><td style='padding:6px 8px;color:{GOLD};white-space:nowrap'>{v.name}</td>"
                     f"<td style='padding:6px 8px'><span style='background:{bc};color:#000;font-weight:800;padding:1px 8px;border-radius:4px;font-size:0.78rem'>{badge}</span></td>"
                     f"<td style='padding:6px 8px;color:{MUTED};text-align:center'>{'' if w is None or v.context_only else f'×{w:g}'}</td>"
                     f"<td style='padding:6px 8px;color:#cbd5e1;font-size:0.85rem'>{v.reading}</td></tr>")
        st.markdown(f"""<div style="background:{NAVY};border:1px solid {GRID};border-radius:10px;padding:10px 12px">
          <div style="color:{GOLD};font-weight:800;margin-bottom:6px">📊 CONFLUENCE MATRIX</div>
          <table style="width:100%;border-collapse:collapse">{rows}</table></div>""", unsafe_allow_html=True)
    with right:
        rows = ""
        placed = False
        for price, name, kind in d.levels:
            if not placed and price < d.spot:
                rows += (f"<tr style='background:rgba(212,175,55,0.18)'><td style='padding:5px 8px;color:{GOLD};font-weight:800'>{d.spot:,.0f}</td>"
                         f"<td style='padding:5px 8px;color:{GOLD};font-weight:800'>◀ SPOT</td><td></td></tr>")
                placed = True
            cl = RED if kind == "res" else GREEN if kind == "sup" else PURPLE
            rows += (f"<tr><td style='padding:5px 8px;color:{cl};font-weight:700'>{price:,.0f}</td>"
                     f"<td style='padding:5px 8px;color:#cbd5e1;font-size:0.85rem'>{name}</td>"
                     f"<td style='padding:5px 8px;color:{MUTED};font-size:0.8rem;text-align:right'>{price - d.spot:+,.0f}</td></tr>")
        if not placed:
            rows += (f"<tr style='background:rgba(212,175,55,0.18)'><td style='padding:5px 8px;color:{GOLD};font-weight:800'>{d.spot:,.0f}</td>"
                     f"<td style='padding:5px 8px;color:{GOLD};font-weight:800'>◀ SPOT</td><td></td></tr>")
        st.markdown(f"""<div style="background:{NAVY};border:1px solid {GRID};border-radius:10px;padding:10px 12px">
          <div style="color:{GOLD};font-weight:800;margin-bottom:6px">🪜 LEVELS LADDER <small style="color:{MUTED}">(red = resistance · green = support · purple = magnet)</small></div>
          <table style="width:100%;border-collapse:collapse">{rows or '<tr><td style="color:#94a3b8">No levels yet</td></tr>'}</table></div>""",
                    unsafe_allow_html=True)

    # ── 4 · HOW TO READ ────────────────────────────────────────────────────
    st.markdown(f"""<div style="background:{NAVY};border-left:4px solid {GOLD};border-radius:6px;padding:12px 16px;
      margin:12px 0 6px 0;color:{WHITE};font-size:0.9rem;line-height:1.55">
      <div style="color:{GOLD};font-weight:700">HOW THE DECISION IS MADE</div>
      <ul style="margin:4px 0 0 18px;color:#cbd5e1">
        <li>Each engine votes bull / bear / neutral with a strength; the weighted average is the <b>confluence score</b> (−1 … +1).</li>
        <li><b>BUY CE / BUY PE</b> = score ≥ {rr['act_threshold']:.2f} in that direction <b>and</b> the execution model is green (Trade Desk BUY/SELL, or a 5-Rule TAKE TRADE — the 5-Rule counts only once backtest_five_rule.py shows an edge).</li>
        <li><b>ON PULLBACK</b> = direction agreed (score ≥ {rr['lean_threshold']:.2f}) but price is not at the entry zone — place a limit / alert at the zone, don't chase.</li>
        <li><b>WAIT</b> = engines disagree, or short-term flow fights the 15m external structure. <b>NO TRADE</b> = after {rr['no_new_trades_after']}.</li>
        <li>Context rows (gamma, daily draw) don't vote — they change <i>how</i> you trade (target size, pinning, likely level to be taken).</li>
        <li>Strike = premium closest to the middle of your ₹{_band()[0]:.0f}–{_band()[1]:.0f} band. Weights are in <code>strategy1_rules.json → decision_desk</code>.</li>
        <li>Information only — not financial advice. A vote count is not a backtested signal.</li></ul></div>""",
                unsafe_allow_html=True)
