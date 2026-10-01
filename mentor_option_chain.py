"""
mentor_option_chain.py — the trainer's option-chain method (Lecture 3), read from the
snapshots your scanner already stores (store.load_day). Nothing in the scanner changes.

  .venv/bin/python mentor_option_chain.py                 # today's latest reading
  .venv/bin/python mentor_option_chain.py --backtest      # every stored day

What it computes (rules + thresholds in mentor_option_chain_rules.json):
  totals        all strikes of the expiry: CE/PE OI, change in OI, volume; PCR OI, PCR vol (PE/CE),
                PCR chg OI  (his top rows)
  writer side   net = PE chg OI - CE chg OI (his N9-M9): + put writers aggressive (green, support),
                - call writers aggressive (red, sideways/down)
  volume side   net = CE vol - PE vol (his D3-F3): market tends to the side with more volume (unproven)
  ladder        100-pt strikes, ATM +/- 300, top-2 max OI each side (his red marks), ITM shading
  S/R zones     resistance = max CE OI strike + CE premium; support = max PE OI strike - PE premium
  ITM traps     writers adding / closing 100-200 pts in the money -> short covering / long unwinding
  buildup       long / short buildup, short covering, long unwinding per strike (day and last interval)
  timeline      his Ctrl+Q log: one row per snapshot through the day
  candles       valid-candle / inside-candle structure on 15-min candles
"""
from __future__ import annotations

import argparse
import json
import logging
from datetime import date, datetime, time, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

import config
import store

log = logging.getLogger("mentor_option_chain")
HERE = Path(__file__).resolve().parent
RULES_PATH = HERE / "mentor_option_chain_rules.json"


def rules() -> dict:
    return json.loads(RULES_PATH.read_text())


def _f(x) -> float:
    try:
        v = float(x)
        return v if np.isfinite(v) else 0.0
    except (TypeError, ValueError):
        return 0.0


# ─────────────────────────────── one snapshot ───────────────────────────────
def totals(df: pd.DataFrame) -> dict:
    s = {c: _f(pd.to_numeric(df.get(c), errors="coerce").sum()) for c in
         ("ce_oi", "pe_oi", "ce_chg_oi", "pe_chg_oi", "ce_vol", "pe_vol")}
    s["pcr_oi"] = s["pe_oi"] / s["ce_oi"] if s["ce_oi"] else np.nan
    s["pcr_vol"] = s["pe_vol"] / s["ce_vol"] if s["ce_vol"] else np.nan
    s["pcr_chg_oi"] = s["pe_chg_oi"] / s["ce_chg_oi"] if s["ce_chg_oi"] else np.nan
    s["net_chg_oi"] = s["pe_chg_oi"] - s["ce_chg_oi"]          # his N9-M9
    s["net_vol"] = s["ce_vol"] - s["pe_vol"]                    # his D3-F3
    s["writer_side"] = "PUT WRITERS AGGRESSIVE" if s["net_chg_oi"] > 0 else \
        "CALL WRITERS AGGRESSIVE" if s["net_chg_oi"] < 0 else "BALANCED"
    s["volume_side"] = "CALL VOLUME HIGHER" if s["net_vol"] > 0 else \
        "PUT VOLUME HIGHER" if s["net_vol"] < 0 else "BALANCED"
    w = np.sign(s["net_chg_oi"])
    v = np.sign(s["net_vol"])
    s["agreement"] = ("BOTH GREEN — up" if w > 0 and v > 0 else "BOTH RED — down" if w < 0 and v < 0
                      else "MIXED — confusion")
    return s


def ladder(df: pd.DataFrame, spot: float, R: dict) -> pd.DataFrame:
    L = R["ladder"]
    step, win = L["strike_step"], L["window_pts"]
    d = df.copy()
    d["strike"] = pd.to_numeric(d["strike"], errors="coerce")
    d = d[(d["strike"] % step == 0) & (d["strike"].between(spot - win - step, spot + win + step))]
    d = d.sort_values("strike").reset_index(drop=True)
    for side in ("ce", "pe"):
        top = d[f"{side}_oi"].nlargest(L["top_n_max_oi"]).index
        d[f"{side}_max_oi"] = d.index.isin(top)
    d["ce_itm"] = d["strike"] < spot
    d["pe_itm"] = d["strike"] > spot
    atm = d.iloc[(d["strike"] - spot).abs().argsort()[:1]]["strike"]
    d["atm"] = d["strike"].isin(atm)
    return d


def sr_zones(df: pd.DataFrame, spot: float, R: dict) -> dict:
    """Nearest highlighted max-OI strike each side, plus its premium (his buffer zone)."""
    lad = ladder(df, spot, R)
    out = {}
    ce = lad[lad["ce_max_oi"] & (lad["strike"] >= spot - R["ladder"]["strike_step"])]
    if ce.empty:
        ce = lad[lad["ce_max_oi"]]
    if len(ce):
        r = ce.iloc[(ce["strike"] - spot).abs().argsort()].iloc[0]
        prem = _f(r.get("ce_ltp"))
        out["resistance_strike"], out["resistance_premium"] = float(r["strike"]), prem
        out["resistance_zone"] = (float(r["strike"]) + prem * (1 - R["sr_zones"]["zone_buffer_fraction"]),
                                  float(r["strike"]) + prem)
    pe = lad[lad["pe_max_oi"] & (lad["strike"] <= spot + R["ladder"]["strike_step"])]
    if pe.empty:
        pe = lad[lad["pe_max_oi"]]
    if len(pe):
        r = pe.iloc[(pe["strike"] - spot).abs().argsort()].iloc[0]
        prem = _f(r.get("pe_ltp"))
        out["support_strike"], out["support_premium"] = float(r["strike"]), prem
        out["support_zone"] = (float(r["strike"]) - prem,
                               float(r["strike"]) - prem * (1 - R["sr_zones"]["zone_buffer_fraction"]))
    rz, sz = out.get("resistance_zone"), out.get("support_zone")
    if rz and spot > rz[1]:
        out["zone_state"] = "ABOVE RESISTANCE ZONE — call writers exiting, short covering likely"
    elif sz and spot < sz[0]:
        out["zone_state"] = "BELOW SUPPORT ZONE — put writers exiting, long unwinding likely"
    elif rz and spot >= rz[0]:
        out["zone_state"] = "INSIDE RESISTANCE ZONE — watch for rejection or breakout"
    elif sz and spot <= sz[1]:
        out["zone_state"] = "INSIDE SUPPORT ZONE — watch for bounce or breakdown"
    else:
        out["zone_state"] = "BETWEEN ZONES"
    return out


def itm_traps(df: pd.DataFrame, spot: float, R: dict, prev: pd.DataFrame | None = None) -> list[dict]:
    """ITM writers 100-200 pts in the money: adding (confident) or closing (move against them started)."""
    T = R["itm_trap"]
    step = R["ladder"]["strike_step"]
    d = df.copy()
    d["strike"] = pd.to_numeric(d["strike"], errors="coerce")
    d = d[d["strike"] % step == 0].set_index("strike")
    p = None
    if prev is not None:
        p = prev.copy()
        p["strike"] = pd.to_numeric(p["strike"], errors="coerce")
        p = p[p["strike"] % step == 0].set_index("strike")
    out = []
    lo, hi = T["min_itm_pts"] - step / 2, T["max_itm_pts"] + step / 2      # e.g. 50 < ITM distance <= 250
    for side in ("ce", "pe"):
        for k in d.index:
            dist = (spot - k) if side == "ce" else (k - spot)              # CE ITM below spot, PE ITM above
            if not (lo < dist <= hi):
                continue
            k = float(k)
            chg = _f(d.at[k, f"{side}_chg_oi"])
            delta = None if p is None or k not in p.index else _f(d.at[k, f"{side}_oi"]) - _f(p.at[k, f"{side}_oi"])
            if chg <= T["min_add_contracts"] and (delta is None or delta == 0):
                continue
            who = "call" if side == "ce" else "put"
            if delta is not None and delta < 0:
                state = "CLOSING"
                meaning = ("ITM call writers covering -> short covering, market pushes UP" if side == "ce" else
                           "ITM put writers exiting -> long unwinding, market pushes DOWN")
            else:
                state = "ADDING" if (delta or 0) > 0 or chg > 0 else "HOLDING"
                meaning = ("ITM call writers confident (bearish) — trapped if price rises: short-covering risk"
                           if side == "ce" else
                           "ITM put writers confident (bullish) — trapped if price falls: long-unwinding risk")
            out.append({"side": who, "strike": int(k), "pts_itm": int(abs(k - spot)),
                        "chg_oi_day": chg, "chg_oi_last": delta, "state": state, "meaning": meaning,
                        "bias": ("DOWN" if (side == "ce" and state != "CLOSING") or (side == "pe" and state == "CLOSING")
                                 else "UP")})
    return out


def buildup_label(d_price: float, d_oi: float) -> str:
    if d_oi > 0:
        return "LONG BUILDUP" if d_price > 0 else "SHORT BUILDUP" if d_price < 0 else "OI ADDED"
    if d_oi < 0:
        return "SHORT COVERING" if d_price > 0 else "LONG UNWINDING" if d_price < 0 else "OI CUT"
    return "—"


MEANING = {  # writer's-side market meaning
    ("ce", "SHORT BUILDUP"): "call writing — bearish", ("ce", "SHORT COVERING"): "call writers exiting — bullish",
    ("ce", "LONG BUILDUP"): "call buying — bullish", ("ce", "LONG UNWINDING"): "call buyers exiting — bearish",
    ("pe", "SHORT BUILDUP"): "put writing — bullish", ("pe", "SHORT COVERING"): "put writers exiting — bearish",
    ("pe", "LONG BUILDUP"): "put buying — bearish", ("pe", "LONG UNWINDING"): "put buyers exiting — bullish",
}


def buildups(df: pd.DataFrame, spot: float, R: dict, prev: pd.DataFrame | None = None) -> pd.DataFrame:
    """Per strike and side: the day's buildup (chg OI vs premium change) and the last interval's."""
    lad = ladder(df, spot, R)
    pv = None
    if prev is not None:
        pv = prev.copy()
        pv["strike"] = pd.to_numeric(pv["strike"], errors="coerce")
        pv = pv.set_index("strike")
    rows = []
    for _, r in lad.iterrows():
        k = r["strike"]
        for side in ("ce", "pe"):
            day = buildup_label(_f(r.get(f"{side}_chg")), _f(r.get(f"{side}_chg_oi")))
            last = "—"
            if pv is not None and k in pv.index:
                last = buildup_label(_f(r.get(f"{side}_ltp")) - _f(pv.at[k, f"{side}_ltp"]),
                                     _f(r.get(f"{side}_oi")) - _f(pv.at[k, f"{side}_oi"]))
            rows.append({"strike": int(k), "side": side.upper(), "day": day,
                         "day_meaning": MEANING.get((side, day), ""), "last_interval": last,
                         "last_meaning": MEANING.get((side, last), "")})
    return pd.DataFrame(rows)


def buildup_verdict(b: pd.DataFrame) -> str:
    bull = b["day_meaning"].str.contains("bullish").sum()
    bear = b["day_meaning"].str.contains("bearish").sum()
    if bull > bear * 1.5:
        return f"BULLISH ({bull} bullish vs {bear} bearish strike reads)"
    if bear > bull * 1.5:
        return f"BEARISH ({bear} bearish vs {bull} bullish strike reads)"
    return f"MIXED ({bull} bullish / {bear} bearish)"


# ─────────────────────────────── one day ────────────────────────────────────
def day_timeline(day: date | None = None) -> pd.DataFrame:
    """His Ctrl+Q log: one row per stored snapshot."""
    rows = []
    for ts, spot, df in store.load_day(day):
        t = totals(df)
        rows.append({"time": ts, "spot": spot, **{k: t[k] for k in (
            "ce_oi", "pe_oi", "ce_chg_oi", "pe_chg_oi", "ce_vol", "pe_vol", "pcr_oi", "pcr_vol",
            "pcr_chg_oi", "net_chg_oi", "net_vol")}})
    return pd.DataFrame(rows)


def latest_reading(day: date | None = None, R: dict | None = None) -> dict | None:
    R = R or rules()
    snaps = store.load_day(day)
    if not snaps:
        return None
    ts, spot, df = snaps[-1]
    prev = snaps[-2][2] if len(snaps) > 1 else None
    b = buildups(df, spot, R, prev)
    return {"time": ts, "spot": spot, "expiry": df.attrs.get("expiry"), "totals": totals(df),
            "ladder": ladder(df, spot, R), "zones": sr_zones(df, spot, R),
            "traps": itm_traps(df, spot, R, prev), "buildups": b, "buildup_verdict": buildup_verdict(b),
            "snapshots": len(snaps)}


# ─────────────────────────────── candles ────────────────────────────────────
def valid_candles(c: pd.DataFrame) -> pd.DataFrame:
    """His rule: a candle counts only if it breaks (or equals) the previous VALID candle's high or low.
    Adds: valid (bool), inside (bool), dir (+1 broke high, -1 broke low, sign(close-open) if both),
    swing ('H' / 'L' at turning points of the valid sequence)."""
    c = c.reset_index(drop=True).copy()
    c["valid"], c["inside"], c["dir"], c["swing"] = False, False, 0, ""
    if c.empty:
        return c
    c.loc[0, "valid"] = True
    ref = 0
    for i in range(1, len(c)):
        up = c.at[i, "high"] >= c.at[ref, "high"]
        dn = c.at[i, "low"] <= c.at[ref, "low"]
        if up or dn:
            c.at[i, "valid"] = True
            c.at[i, "dir"] = 1 if up and not dn else -1 if dn and not up else int(np.sign(c.at[i, "close"] - c.at[i, "open"]) or 1)
            ref = i
        else:
            c.at[i, "inside"] = True
    v = c[c["valid"]]
    idx, dirs = list(v.index), list(v["dir"])
    for j in range(1, len(idx) - 1):
        if dirs[j] >= 0 and dirs[j + 1] < 0:
            c.at[idx[j], "swing"] = "H"
        elif dirs[j] <= 0 and dirs[j + 1] > 0:
            c.at[idx[j], "swing"] = "L"
    return c


def load_candles(day: date | None = None, minutes: int = 15) -> tuple[pd.DataFrame | None, str]:
    """15-min candles for a day: futures if your candle source has them (he reads futures), else index."""
    day = day or date.today()
    src = "index"
    df = None
    try:
        import candle_source
        idx, fut = candle_source.fetch(3)
        base = fut if fut is not None and len(fut) else idx
        src = "futures" if base is fut and fut is not None else "index"
        df = base
    except Exception as exc:
        log.info("candle_source not usable (%s) — reading the stored 5-min file", str(exc)[:80])
        p = getattr(config, "CANDLE_5M_DIR", config.DATA_DIR / "candles_5m") / f"{day:%Y-%m-%d}.csv"
        if p.exists():
            df = pd.read_csv(p)
    if df is None or df.empty:
        return None, src
    df = df.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df[df["timestamp"].dt.date == day]
    if df.empty:
        return None, src
    r = df.set_index("timestamp")[["open", "high", "low", "close"]].resample(
        f"{minutes}min", origin="start_day", offset="15min").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}).dropna().reset_index()
    return r, src


# ─────────────────────────────── backtest ───────────────────────────────────
def _stored_days() -> list[date]:
    root = config.SNAPSHOT_DIR
    if not root.exists():
        return []
    out = []
    for p in sorted(root.iterdir()):
        try:
            out.append(datetime.strptime(p.name, "%Y-%m-%d").date())
        except ValueError:
            continue
    return out


def backtest(R: dict | None = None, days: list[date] | None = None) -> tuple[pd.DataFrame, int]:
    """Every stored snapshot after 09:20: does the market go the way his readings point?"""
    R = R or rules()
    B = R["backtest"]
    recs = []
    days = days or _stored_days()
    for d in days:
        snaps = store.load_day(d)
        if len(snaps) < 10:
            continue
        times = [s[0] for s in snaps]
        spots = pd.Series([s[1] for s in snaps], index=pd.DatetimeIndex(times))
        close = spots.iloc[-1]
        exp = snaps[-1][2].attrs.get("expiry")
        is_exp = bool(exp) and str(exp)[:11] in (d.strftime("%d-%b-%Y"), d.strftime("%Y-%m-%d"))
        start = datetime.combine(d, time(9, 15)) + timedelta(minutes=B["skip_first_minutes"])
        prev_df = None
        for (ts, spot, df) in snaps:
            if ts < start:
                prev_df = df
                continue
            t = totals(df)
            z = sr_zones(df, spot, R)
            rec = {"day": d, "time": ts, "expiry_day": is_exp, "w": np.sign(t["net_chg_oi"]),
                   "v": np.sign(t["net_vol"]), "zone": z["zone_state"], "to_close": close / spot - 1}
            for m in B["forward_minutes"]:
                fut = spots[spots.index >= ts + timedelta(minutes=m)]
                rec[f"f{m}"] = (fut.iloc[0] / spot - 1) if len(fut) else np.nan
                win = spots[(spots.index > ts) & (spots.index <= ts + timedelta(minutes=m))]
                rec[f"up{m}"] = (win.max() / spot - 1) if len(win) else np.nan
                rec[f"dn{m}"] = (win.min() / spot - 1) if len(win) else np.nan
            tr = itm_traps(df, spot, R, prev_df)
            rec["trap_ce"] = any(x["side"] == "call" and x["state"] != "CLOSING" for x in tr)
            rec["trap_pe"] = any(x["side"] == "put" and x["state"] != "CLOSING" for x in tr)
            recs.append(rec)
            prev_df = df
    if not recs:
        return pd.DataFrame(), 0
    x = pd.DataFrame(recs)
    ndays = x["day"].nunique()
    minimum = B["min_sample_size"]
    rows = []

    def add(name, m, outcome, base, what):
        m = m & outcome.notna()
        n = int(m.sum())
        nd = int(x.loc[m, "day"].nunique())
        if nd < minimum:
            rows.append({"Reading": name, "Snapshots": n, "Days": nd, "Outcome": what,
                         "Hit %": "INSUFFICIENT SAMPLE (days)", "All snapshots %": ""})
            return
        rows.append({"Reading": name, "Snapshots": n, "Days": nd, "Outcome": what,
                     "Hit %": round(100 * outcome[m].astype(float).mean(), 1),
                     "All snapshots %": round(100 * base.dropna().astype(float).mean(), 1)})

    for m in B["forward_minutes"]:
        up, dn = (x[f"f{m}"] > 0).where(x[f"f{m}"].notna()), (x[f"f{m}"] < 0).where(x[f"f{m}"].notna())
        add(f"Put writers aggressive (net chg OI > 0)", x["w"] > 0, up, up, f"spot up after {m} min")
        add(f"Call writers aggressive (net chg OI < 0)", x["w"] < 0, dn, dn, f"spot down after {m} min")
        add(f"Call volume higher (net vol > 0)", x["v"] > 0, up, up, f"spot up after {m} min")
        add(f"Put volume higher (net vol < 0)", x["v"] < 0, dn, dn, f"spot down after {m} min")
        add(f"BOTH GREEN", (x["w"] > 0) & (x["v"] > 0), up, up, f"spot up after {m} min")
        add(f"BOTH RED", (x["w"] < 0) & (x["v"] < 0), dn, dn, f"spot down after {m} min")
    up_c, dn_c = (x["to_close"] > 0), (x["to_close"] < 0)
    add("Put writers aggressive, non-expiry days", (x["w"] > 0) & ~x["expiry_day"], up_c, up_c, "spot up by the close")
    add("Call writers aggressive, non-expiry days", (x["w"] < 0) & ~x["expiry_day"], dn_c, dn_c, "spot down by the close")
    add("Put writers aggressive, expiry days", (x["w"] > 0) & x["expiry_day"], up_c, up_c, "spot up by the close")
    add("Call writers aggressive, expiry days", (x["w"] < 0) & x["expiry_day"], dn_c, dn_c, "spot down by the close")
    ft = B["zone_follow_through_pct"] / 100
    above = x["zone"].str.startswith("ABOVE")
    below = x["zone"].str.startswith("BELOW")
    fu = (x["up30"] >= ft).where(x["up30"].notna())
    fd = (x["dn30"] <= -ft).where(x["dn30"].notna())
    add("Above resistance zone (short covering)", above, fu, fu, f"+{B['zone_follow_through_pct']}% more within 30 min")
    add("Below support zone (long unwinding)", below, fd, fd, f"-{B['zone_follow_through_pct']}% more within 30 min")
    tm = B["trap_move_pct"] / 100
    tu = (x["up60"] >= tm).where(x["up60"].notna())
    td = (x["dn60"] <= -tm).where(x["dn60"].notna())
    add("ITM call writers present (trap if price rises)", x["trap_ce"], tu, tu, f"rally >= {B['trap_move_pct']}% within 60 min")
    add("ITM put writers present (trap if price falls)", x["trap_pe"], td, td, f"fall >= {B['trap_move_pct']}% within 60 min")
    return pd.DataFrame(rows), ndays


def summary_text(r: dict) -> str:
    t, z = r["totals"], r["zones"]
    lines = [f"OC (mentor) {r['time']:%H:%M} spot {r['spot']:,.1f}",
             f"Writers: {t['writer_side']} (net chg OI {t['net_chg_oi']:+,.0f}) | Volume: {t['volume_side']} "
             f"({t['net_vol']:+,.0f}) -> {t['agreement']}",
             f"PCR OI {t['pcr_oi']:.2f} | PCR vol {t['pcr_vol']:.2f} | PCR chg OI {t['pcr_chg_oi']:.2f}"]
    if "resistance_zone" in z:
        lines.append(f"R zone {z['resistance_zone'][0]:,.0f}-{z['resistance_zone'][1]:,.0f} "
                     f"({z['resistance_strike']:,.0f} CE max OI + {z['resistance_premium']:.0f})")
    if "support_zone" in z:
        lines.append(f"S zone {z['support_zone'][0]:,.0f}-{z['support_zone'][1]:,.0f} "
                     f"({z['support_strike']:,.0f} PE max OI - {z['support_premium']:.0f})")
    lines.append(z["zone_state"])
    for tr in r["traps"]:
        lines.append(f"ITM {tr['side']} writers {tr['state']} at {tr['strike']} ({tr['pts_itm']} pts ITM): {tr['meaning']}")
    lines.append(f"Buildups ATM±300: {r['buildup_verdict']}")
    return "\n".join(lines)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYY-MM-DD (default today)")
    ap.add_argument("--backtest", action="store_true")
    a = ap.parse_args()
    d = datetime.strptime(a.date, "%Y-%m-%d").date() if a.date else None
    if a.backtest:
        tbl, nd = backtest()
        print(f"{nd} stored days")
        print(tbl.to_string(index=False) if len(tbl) else "no stored snapshots")
    else:
        r = latest_reading(d)
        print(summary_text(r) if r else "no snapshots for that day")
