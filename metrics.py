"""
metrics.py — everything I'd actually look at in a chain, plus MJ's #15.

Two layers on purpose:

  1. MJ_LAYER   — the 5 conditions and the -2..+2 score for your view sheet.
  2. EDGE_LAYER — what the chain is really telling you: where writers are
                  parked, which wall is being defended, whether premium is
                  flowing in or bleeding out, and how all of that has moved
                  since the last poll. Snapshot-to-snapshot delta is the part
                  a static screenshot can never give you.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import config


# ======================================================================
# helpers
# ======================================================================
def atm_strike(spot, step):
    return int(round(spot / step) * step)


def _band(df, spot, width):
    return df[(df["strike"] >= spot - width) & (df["strike"] <= spot + width)]


def zone_labels(df, spot, step):
    """
    Reproduce exactly how the NSE page shades its rows.

    NSE shades a row yellow when that leg is IN the money:
        calls -> strike < spot          puts -> strike > spot
    The ATM strike (nearest to spot on the strike grid) is called out
    separately because it is the pivot the whole chain hinges on, even though
    MJ's sheet has no ATM quadrant.

    Returns the df with two new columns: ce_zone, pe_zone.
    """
    atm = atm_strike(spot, step)
    out = df.copy()
    out["is_atm"] = out["strike"] == atm
    out["ce_zone"] = np.where(out["is_atm"], "ATM",
                              np.where(out["strike"] < spot, "ITM", "OTM"))
    out["pe_zone"] = np.where(out["is_atm"], "ATM",
                              np.where(out["strike"] > spot, "ITM", "OTM"))
    return out


def _quadrants(df, spot):
    """
    Calls ITM below spot, OTM above. Puts the mirror image.

    Note the ATM strike sits in whichever side of spot it falls on — it is not
    excluded, because MJ's sheet only defines four quadrants and dropping the
    single most-traded strike would distort every score.
    """
    return {
        "call_itm": df[df["strike"] < spot],
        "call_otm": df[df["strike"] >= spot],
        "put_itm": df[df["strike"] > spot],
        "put_otm": df[df["strike"] <= spot],
    }


def _leg(quad_name):
    return "ce" if quad_name.startswith("call") else "pe"


# ======================================================================
# 1. MJ LAYER — quadrant colour -> condition -> score
# ======================================================================
def _contract_vote(chg_oi, price_chg, mode):
    """Return -1 (selling), 0 (flat) or +1 (buying) for one contract."""
    if mode == "price":
        return int(np.sign(price_chg))
    if mode == "oi":
        return int(np.sign(chg_oi))
    # combined: OI direction x price direction
    if chg_oi > 0 and price_chg > 0:
        return 1          # long buildup — fresh buying
    if chg_oi > 0 and price_chg < 0:
        return -1         # short buildup — writing
    if chg_oi < 0 and price_chg > 0:
        return 1          # short covering
    if chg_oi < 0 and price_chg < 0:
        return -1         # long unwinding
    return 0


def quadrant_colours(df, spot, mode=None):
    """
    Score each of the 4 quadrants from -1.0 (heavy selling) to +1.0 (heavy
    buying), then colour it GREEN / RED / FLAT.
    """
    mode = mode or config.QUADRANT_MODE
    out = {}
    for name, sub in _quadrants(df, spot).items():
        leg = _leg(name)
        if sub.empty:
            out[name] = {"score": 0.0, "colour": "FLAT", "n": 0}
            continue

        chg_oi = sub[f"{leg}_chg_oi"].to_numpy(dtype=float)
        price_chg = sub[f"{leg}_chg"].to_numpy(dtype=float)
        votes = np.array([_contract_vote(o, p, mode)
                          for o, p in zip(chg_oi, price_chg)], dtype=float)

        if config.QUADRANT_WEIGHT == "volume":
            w = sub[f"{leg}_volume"].to_numpy(dtype=float)
        elif config.QUADRANT_WEIGHT == "oi":
            w = np.abs(chg_oi)
        else:
            w = np.ones(len(sub))
        if w.sum() <= 0:
            w = np.ones(len(sub))

        score = float((votes * w).sum() / w.sum())
        dz = config.QUADRANT_DEADZONE
        colour = "GREEN" if score >= dz else "RED" if score <= -dz else "FLAT"
        out[name] = {"score": round(score, 3), "colour": colour, "n": len(sub)}
    return out


def mj_condition(colours):
    """
    Map the 4 quadrant colours onto MJ's 5 conditions.
    FLAT is treated as RED for matching (his sheet has no flat state), but the
    raw colours are always reported so you can see when a match was forced.
    """
    def g(k):
        return colours[k]["colour"] == "GREEN"

    ci, co, pi, po = g("call_itm"), g("call_otm"), g("put_itm"), g("put_otm")

    if ci and co and not pi and not po:
        return 1, 2, "SUPER BULLISH", "Trail winners to 100-400 pts"
    if not ci and not co and pi and po:
        return 2, -2, "SUPER BEARISH", "Trail winners to 100-400 pts"
    if ci and not co and not pi and not po:
        return 3, 1, "BULLISH CONSOLIDATION", "Exit at +40, do not trail"
    if pi and not ci and not co and not po:
        return 4, -1, "BEARISH CONSOLIDATION", "Exit at +40, do not trail"
    if not any([ci, co, pi, po]):
        return 5, 0, "CONSOLIDATION", "Range day — avoid extension setups"
    greens = [k for k in ("call_itm", "call_otm", "put_itm", "put_otm") if g(k)]
    return 0, 0, "UNMAPPED (" + "+".join(greens) + ")", \
        "Does not match MJ's 5 conditions — treat as 0 and wait for 9:45"


# ======================================================================
# 2. EDGE LAYER
# ======================================================================
def pcr(df):
    ce, pe = df["ce_oi"].sum(), df["pe_oi"].sum()
    return float(pe / ce) if ce else float("nan")


def pcr_label(v):
    if np.isnan(v):
        return "n/a"
    if v > config.PCR_OVERBOUGHT:
        return f"{v:.2f} OVERBOUGHT (heavy puts)"
    if v < config.PCR_OVERSOLD:
        return f"{v:.2f} OVERSOLD (heavy calls)"
    return f"{v:.2f} neutral"


def max_pain(df):
    """Strike where option writers lose the least — price magnet into expiry."""
    strikes = df["strike"].to_numpy(dtype=float)
    ce_oi = df["ce_oi"].to_numpy(dtype=float)
    pe_oi = df["pe_oi"].to_numpy(dtype=float)
    pain = []
    for k in strikes:
        ce_loss = np.maximum(0.0, k - strikes) * ce_oi
        pe_loss = np.maximum(0.0, strikes - k) * pe_oi
        pain.append(ce_loss.sum() + pe_loss.sum())
    return float(strikes[int(np.argmin(pain))])


def oi_walls(df, n=3):
    calls = df.nlargest(n, "ce_oi")[["strike", "ce_oi"]]
    puts = df.nlargest(n, "pe_oi")[["strike", "pe_oi"]]
    return {
        "resistance_walls": [(int(r.strike), int(r.ce_oi)) for r in calls.itertuples()],
        "support_walls": [(int(r.strike), int(r.pe_oi)) for r in puts.itertuples()],
    }


def oi_builds(df, n=3):
    """Where TODAY's positions are being added — the intraday walls forming."""
    ce = df.nlargest(n, "ce_chg_oi")[["strike", "ce_chg_oi"]]
    pe = df.nlargest(n, "pe_chg_oi")[["strike", "pe_chg_oi"]]
    return {
        "call_oi_added": [(int(r.strike), int(r.ce_chg_oi)) for r in ce.itertuples()],
        "put_oi_added": [(int(r.strike), int(r.pe_chg_oi)) for r in pe.itertuples()],
    }


def premium_flow(df, lot, mode=None):
    """
    Directional premium flow in ₹ Cr.

    Note: chg_oi x LTP alone is NOT "money flowing in" — a written (sold) option
    also adds OI. So each strike's notional is signed by whether the position
    looks like buying (long buildup / short covering) or selling (writing /
    unwinding). Positive call flow = calls being bought.
    """
    mode = mode or config.QUADRANT_MODE
    out = {}
    for leg, key in (("ce", "call"), ("pe", "put")):
        chg = df[f"{leg}_chg_oi"].to_numpy(dtype=float)
        pchg = df[f"{leg}_chg"].to_numpy(dtype=float)
        ltp = df[f"{leg}_ltp"].to_numpy(dtype=float)
        votes = np.array([_contract_vote(o, p, mode) for o, p in zip(chg, pchg)],
                         dtype=float)
        out[f"{key}_flow_cr"] = float((votes * np.abs(chg) * ltp * lot).sum()) / 1e7
        out[f"{key}_notional_cr"] = float((chg * ltp * lot).sum()) / 1e7
    out["net_cr"] = out["call_flow_cr"] - out["put_flow_cr"]
    return out


def straddle(df, spot, step):
    k = atm_strike(spot, step)
    row = df[df["strike"] == k]
    if row.empty:
        return {}
    ce_ltp = float(row["ce_ltp"].iloc[0])
    pe_ltp = float(row["pe_ltp"].iloc[0])
    total = ce_ltp + pe_ltp
    return {
        "atm_strike": k,
        "ce_ltp": ce_ltp, "pe_ltp": pe_ltp,
        "straddle": round(total, 2),
        "expected_low": round(k - total, 1),
        "expected_high": round(k + total, 1),
        "ce_iv": float(row["ce_iv"].iloc[0]),
        "pe_iv": float(row["pe_iv"].iloc[0]),
        "iv_skew": round(float(row["pe_iv"].iloc[0] - row["ce_iv"].iloc[0]), 2),
    }


def writer_bias(df, spot):
    """
    The single most reliable structural read in the chain.
    OTM put writing > OTM call writing  = writers defending downside = bullish.
    Reverse = ceiling being built = bearish.
    """
    otm_c = df[df["strike"] > spot]["ce_chg_oi"].sum()
    otm_p = df[df["strike"] < spot]["pe_chg_oi"].sum()
    net = float(otm_p - otm_c)
    if otm_p <= 0 and otm_c <= 0:
        label = "both sides unwinding — no conviction"
    elif net > 0:
        label = "put writers dominant — downside defended (bullish)"
    else:
        label = "call writers dominant — ceiling being built (bearish)"
    return {"otm_call_oi_added": float(otm_c), "otm_put_oi_added": float(otm_p),
            "net": net, "label": label}


# ======================================================================
# 3. assemble one snapshot
# ======================================================================
def build_snapshot(df_full, meta, prev=None):
    sym = config.SYMBOL
    spot = meta["spot"]
    step = config.STRIKE_STEP.get(sym, 50)
    lot = config.LOT_SIZE.get(sym, 65)
    band = config.SCAN_BAND.get(sym, 500)

    df = _band(df_full, spot, band)

    colours = quadrant_colours(df, spot)
    cond_no, score, cond_name, cond_action = mj_condition(colours)

    snap = {
        "symbol": sym,
        "spot": spot,
        "nse_timestamp": meta["timestamp"],
        "expiry": meta["expiry"],
        "band": band,
        "strikes_in_band": len(df),
        # --- MJ layer
        "quadrants": colours,
        "quadrant_mode": config.QUADRANT_MODE,
        "condition_no": cond_no,
        "condition": cond_name,
        "param15_score": score,
        "action": cond_action,
        # cross-check: same chain read the other two ways
        "condition_price_mode": mj_condition(
            quadrant_colours(df, spot, "price"))[2],
        "condition_oi_mode": mj_condition(
            quadrant_colours(df, spot, "oi"))[2],
        # --- edge layer
        "pcr_band": pcr(df),
        "pcr_full": pcr(df_full),
        "pcr_label": pcr_label(pcr(df_full)),
        "max_pain": max_pain(df_full),
        "writer_bias": writer_bias(df, spot),
        "premium_flow": premium_flow(df, lot),
        "straddle": straddle(df, spot, step),
    }
    snap["max_pain_gap"] = round(spot - snap["max_pain"], 1)
    snap.update(oi_walls(df_full))
    snap.update(oi_builds(df))

    # --- delta vs previous poll: the real intraday signal
    if prev:
        snap["delta"] = {
            "spot": round(spot - prev.get("spot", spot), 2),
            "pcr_band": round(snap["pcr_band"] - prev.get("pcr_band", 0), 3),
            "max_pain": snap["max_pain"] - prev.get("max_pain", snap["max_pain"]),
            # prev is a FLAT row from to_row()/the summary CSV, so "straddle"
            # there is a number, not the nested dict.
            "straddle": round(
                float(snap["straddle"].get("straddle") or 0)
                - float(prev.get("straddle") or 0), 2),
            "condition_changed": prev.get("condition") != snap["condition"],
            "prev_condition": prev.get("condition"),
        }
        s = snap["delta"]["straddle"]
        snap["delta"]["straddle_read"] = (
            "premium expanding — trend day risk, writers under pressure"
            if s > 0 else
            "premium collapsing — range day, theta favours writers"
            if s < 0 else "premium flat")
    return snap


# ======================================================================
# 4. flat row for the CSV log
# ======================================================================
def to_row(snap):
    q = snap["quadrants"]
    st = snap["straddle"]
    return {
        "ts": snap["nse_timestamp"],
        "spot": snap["spot"],
        "expiry": snap["expiry"],
        "condition_no": snap["condition_no"],
        "condition": snap["condition"],
        "param15_score": snap["param15_score"],
        "call_itm": q["call_itm"]["colour"], "call_itm_s": q["call_itm"]["score"],
        "call_otm": q["call_otm"]["colour"], "call_otm_s": q["call_otm"]["score"],
        "put_itm": q["put_itm"]["colour"], "put_itm_s": q["put_itm"]["score"],
        "put_otm": q["put_otm"]["colour"], "put_otm_s": q["put_otm"]["score"],
        "pcr_band": round(snap["pcr_band"], 3),
        "pcr_full": round(snap["pcr_full"], 3),
        "max_pain": snap["max_pain"],
        "max_pain_gap": snap["max_pain_gap"],
        "atm": st.get("atm_strike"),
        "straddle": st.get("straddle"),
        "exp_low": st.get("expected_low"), "exp_high": st.get("expected_high"),
        "iv_skew": st.get("iv_skew"),
        "writer_net": snap["writer_bias"]["net"],
        "call_flow_cr": round(snap["premium_flow"]["call_flow_cr"], 2),
        "put_flow_cr": round(snap["premium_flow"]["put_flow_cr"], 2),
    }


# ======================================================================
# 5. Liquidity Assessment Tool (Bid-Ask Spread × Lot Size)
# ======================================================================
def assess_liquidity(df: pd.DataFrame, spot: float, lot_size: int | None = None,
                     threshold: float = 800.0, span: int = 5, step: int = 50) -> dict:
    """
    Evaluates option contract liquidity using the rule:
      Spread = Ask - Bid
      Impact Cost (₹) = Spread × Lot Size
      Threshold: ₹800 (below = Liquid, above = Illiquid / Warning for sellers)
    """
    lot = lot_size or getattr(config, "LOT_SIZE", 65)
    atm = atm_strike(spot, step)

    subset = df[(df["strike"] >= atm - span * step) & (df["strike"] <= atm + span * step)].copy()

    rows = []
    otm_ce_costs = []
    otm_pe_costs = []

    for _, r in subset.iterrows():
        k = int(r["strike"])
        is_atm = (k == atm)

        # CE
        cb, ca = float(r.get("ce_bid", np.nan)), float(r.get("ce_ask", np.nan))
        ce_spread = (ca - cb) if (pd.notna(ca) and pd.notna(cb) and ca >= cb and cb > 0) else None
        ce_impact = (ce_spread * lot) if ce_spread is not None else None
        ce_liquid = (ce_impact is not None and ce_impact <= threshold)

        # PE
        pb, pa = float(r.get("pe_bid", np.nan)), float(r.get("pe_ask", np.nan))
        pe_spread = (pa - pb) if (pd.notna(pa) and pd.notna(pb) and pa >= pb and pb > 0) else None
        pe_impact = (pe_spread * lot) if pe_spread is not None else None
        pe_liquid = (pe_impact is not None and pe_impact <= threshold)

        if k >= atm and ce_impact is not None:
            otm_ce_costs.append(ce_impact)
        if k <= atm and pe_impact is not None:
            otm_pe_costs.append(pe_impact)

        rows.append({
            "Strike": k,
            "ATM": "★" if is_atm else "",
            "CE Bid": cb if pd.notna(cb) and cb > 0 else np.nan,
            "CE Ask": ca if pd.notna(ca) and ca > 0 else np.nan,
            "CE Spread": ce_spread,
            "CE Impact (₹)": ce_impact,
            "CE Status": "LIQUID" if ce_liquid else "ILLIQUID ⚠️",
            "PE Bid": pb if pd.notna(pb) and pb > 0 else np.nan,
            "PE Ask": pa if pd.notna(pa) and pa > 0 else np.nan,
            "PE Spread": pe_spread,
            "PE Impact (₹)": pe_impact,
            "PE Status": "LIQUID" if pe_liquid else "ILLIQUID ⚠️",
        })

    avg_ce = float(np.mean(otm_ce_costs)) if otm_ce_costs else float("nan")
    avg_pe = float(np.mean(otm_pe_costs)) if otm_pe_costs else float("nan")

    near_ce = [r["CE Impact (₹)"] for r in rows if r["CE Impact (₹)"] is not None and abs(r["Strike"] - atm) <= 2 * step]
    near_pe = [r["PE Impact (₹)"] for r in rows if r["PE Impact (₹)"] is not None and abs(r["Strike"] - atm) <= 2 * step]
    max_near_impact = max((near_ce + near_pe) or [0.0])

    is_liquid = max_near_impact <= threshold
    verdict = "LIQUID" if is_liquid else "ILLIQUID"
    warning = (None if is_liquid else
               f"High impact cost (> ₹{threshold:.0f}/lot) detected near ATM strikes. Avoid naked option selling due to severe slippage risk.")

    return {
        "lot_size": lot,
        "threshold": threshold,
        "atm": atm,
        "spot": spot,
        "table": pd.DataFrame(rows),
        "avg_otm_ce_impact": avg_ce,
        "avg_otm_pe_impact": avg_pe,
        "max_near_impact": max_near_impact,
        "is_liquid": is_liquid,
        "verdict": verdict,
        "warning": warning,
    }
