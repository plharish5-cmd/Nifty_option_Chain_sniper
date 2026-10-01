"""
mentor_traps.py — two option-chain / level features with their own history checks.

1. LIQUIDITY TRAP (the scheduler.py alert that caught 29-Sep's +128 pts: "S1 22,600 swept, low 22,596, reclaim
   22,619, CE BUY"). Rebuilt on 3-min candles made from Dhan 1-min history (up to 5 years), same numbers as
   strategy1_rules.json -> liquidity_trap_alerts:
     bullish  a support level is swept by 2-25 pts within 6 min and price is back >= 15 pts above it, and the
              three previous 3-min closes rise (the live 'higher lows' filter) -> CE
     bearish  a resistance is swept by 2-25 pts within 45 min and price is back >= 1.5 pts below it -> PE
   Levels: previous day / week / month high and low. The live alert also uses the max-OI CE/PE walls; there is
   no 5-year option-chain history, so walls are tested separately on your stored snapshots (part 3).
   Result: SL beyond the sweep wick (2 pts), targets 1R and 2R, everything closed by 15:15.

2. CLIFF BEHIND A WALL (Trading with Siddhant, option-chain video 14:50): the max-OI put wall below price (call
   wall above). cliff ratio = average OI of the next 2 strikes beyond the wall / OI at the wall. Low = thin
   behind the wall: if the wall breaks, little stops the move.

3. History check on your stored option-chain snapshots (data/snapshots/): after price breaks a wall, how far
   does it run in 15 / 30 min when the cliff is thin vs thick, vs any random snapshot time. Also: the trap
   alert at the OI walls.

  .venv/bin/python mentor_traps.py                 # walls + cliff ratios now
  .venv/bin/python mentor_traps.py --backtest      # trap (5 years, 3-min) + cliff (your snapshot days)
"""
from __future__ import annotations

import argparse
import copy
import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

import config
import mentor_structure as ms
import mentor_sweeps as msw

log = logging.getLogger("mentor_traps")
HERE = Path(__file__).resolve().parent
RULES_PATH = HERE / "mentor_traps_rules.json"
VERDICT_PATH = HERE / "data" / "mentor_traps_verdict.json"


def rules() -> dict:
    return json.loads(RULES_PATH.read_text())


def live_trap_cfg() -> dict:
    """The live alert's numbers, read from your strategy1_rules.json so the test matches what fires."""
    try:
        return json.loads((HERE / "strategy1_rules.json").read_text()).get("liquidity_trap_alerts", {})
    except (OSError, ValueError):
        return {}


# ─────────────────────────────── data ───────────────────────────────────────
def minute_history(refresh: bool = False) -> pd.DataFrame | None:
    """Dhan 1-min index candles, cached in data/ohlc/nifty_1m_dhan.csv (first run fetches the years once)."""
    R = rules()["history"]
    S = copy.deepcopy(ms.rules())
    S["history"].update({"dhan_interval": "1", "days": R["minute_days"], "cache": R["minute_cache"]})
    return ms.load_history(S, refresh=refresh)


def bars(m: pd.DataFrame, minutes: int = 3) -> pd.DataFrame:
    x = m.copy()
    x["timestamp"] = pd.to_datetime(x["timestamp"])
    x = x.set_index("timestamp").between_time("09:15", "15:29")
    out = [g.resample(f"{minutes}min", origin="start_day", offset="9h15min").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}).dropna() for _, g in x.groupby(x.index.date)]
    return pd.concat(out).reset_index() if out else pd.DataFrame()


def ref_levels(c: pd.DataFrame) -> dict:
    """date -> {name: (price, side)} with previous day / week / month highs and lows (known before the day)."""
    ts = pd.to_datetime(c["timestamp"])
    d = c.groupby(ts.dt.date).agg(high=("high", "max"), low=("low", "min"))
    d.index = pd.to_datetime(d.index)
    wk = d.resample("W-FRI").agg({"high": "max", "low": "min"}).dropna()
    mo = d.resample("MS").agg({"high": "max", "low": "min"}).dropna()
    out = {}
    days = list(d.index)
    for k in range(1, len(days)):
        day = days[k]
        lv = {"PDH": (float(d["high"].iloc[k - 1]), "res"), "PDL": (float(d["low"].iloc[k - 1]), "sup")}
        w = wk[wk.index < day - pd.Timedelta(days=day.weekday())]      # weeks that ended before this week
        if len(w):
            lv["PWH"], lv["PWL"] = (float(w["high"].iloc[-1]), "res"), (float(w["low"].iloc[-1]), "sup")
        mm = mo[mo.index < day.replace(day=1)]
        if len(mm):
            lv["PMH"], lv["PML"] = (float(mm["high"].iloc[-1]), "res"), (float(mm["low"].iloc[-1]), "sup")
        out[day.date()] = lv
    return out


# ─────────────────────────────── 1. liquidity trap ──────────────────────────
def traps(c: pd.DataFrame, R: dict | None = None, session_levels: bool = False) -> pd.DataFrame:
    R = R or rules()
    T = {**R["trap"], **{k: v for k, v in live_trap_cfg().items() if k in R["trap"]}}
    c = c.reset_index(drop=True)
    O, H, L, C = (c[k].values.astype(float) for k in ("open", "high", "low", "close"))
    ts = pd.to_datetime(c["timestamp"])
    tod, days = ts.dt.strftime("%H:%M").values, ts.dt.date.values
    lvmap = ref_levels(c)
    n = len(c)
    rows = []
    look = int(T["lookback_snapshots"])
    cool = int(T["cooldown_minutes"]) // 3
    day_start, last_fire = 0, {}
    for i in range(n):
        if i == 0 or days[i] != days[i - 1]:
            day_start, last_fire = i, {}
        if not (T["warmup_until"] <= tod[i] < T["eod_from"]) or i - day_start < 4:
            continue
        lv = dict(lvmap.get(days[i], {}))
        if session_levels and i - day_start > look:
            lv["Session high"] = (float(H[day_start:i - look].max()), "res")
            lv["Session low"] = (float(L[day_start:i - 1].min()), "sup")
        fired = False
        for name, (p, side) in lv.items():
            key = (side, round(p))
            if key in last_fire and i - last_fire[key] < cool:
                continue
            if side == "sup":
                lo = min(L[i - 1], L[i])
                sw = p - lo
                ok = T["sweep_min_pts"] <= sw <= T["sweep_max_pts"] and C[i] >= p + T["min_false_breakdown_reclaim_pts"]
                trend = C[i - 3] < C[i - 2] < C[i - 1]
                if not ok:
                    continue
                entry, sl, sgn = C[i], lo - R["risk"]["sl_buffer_points"], 1
            else:
                hi = H[max(day_start, i - look + 1):i + 1].max()
                sw = hi - p
                ok = T["sweep_min_pts"] <= sw <= T["sweep_max_pts"] and C[i] <= p - T["reclaim_pts"]
                trend = True
                if not ok:
                    continue
                entry, sl, sgn = C[i], hi + R["risk"]["sl_buffer_points"], -1
            last_fire[key] = i
            risk = abs(entry - sl)
            end = i
            while end + 1 < n and days[end + 1] == days[i] and tod[end + 1] <= R["risk"]["exit_by"]:
                end += 1
            row = {"idx": i, "time": ts.iloc[i], "side": "CE" if sgn > 0 else "PE", "level": name, "price": p,
                   "sweep": sw, "entry": entry, "sl": sl, "risk": risk, "trend_ok": trend}
            for tr in R["risk"]["targets_r"]:
                target = entry + sgn * tr * risk
                res, _, r = msw._replay(H, L, C, "bull" if sgn > 0 else "bear", i, entry, sl, target, end - i)
                row[f"r_{tr}R"], row[f"res_{tr}R"] = r, res
            for mins in (30, 60):
                j = min(end, i + mins // 3)
                row[f"best_{mins}m"] = (H[i + 1:j + 1].max() - entry) if sgn > 0 and j > i else \
                    (entry - L[i + 1:j + 1].min()) if j > i else 0.0
            rows.append(row)
            fired = True
            break                                        # the live code sends at most one trap per poll
        if fired:
            continue
    return pd.DataFrame(rows)


def random_entries(c: pd.DataFrame, t: pd.DataFrame, R: dict | None = None, k: int = 5, seed: int = 11) -> pd.DataFrame:
    """Baseline with the SAME trade shape: for every trap, k entries at random times of the same session, same
    side, same SL distance and targets. Tests whether the trap's TIMING adds anything."""
    R = R or rules()
    T = {**R["trap"], **{kk: v for kk, v in live_trap_cfg().items() if kk in R["trap"]}}
    c = c.reset_index(drop=True)
    H, L, C = (c[x].values.astype(float) for x in ("high", "low", "close"))
    ts = pd.to_datetime(c["timestamp"])
    tod, days = ts.dt.strftime("%H:%M").values, ts.dt.date.values
    rng = np.random.default_rng(seed)
    by_day = pd.Series(np.arange(len(c))).groupby(days).apply(list).to_dict()
    rows = []
    for _, x in t.iterrows():
        pool = [i for i in by_day[days[int(x["idx"])]] if T["warmup_until"] <= tod[i] < T["eod_from"]]
        for i in rng.choice(pool, size=min(k, len(pool)), replace=False):
            sgn = 1 if x["side"] == "CE" else -1
            entry, risk = C[i], x["risk"]
            sl = entry - sgn * risk
            end = i
            while end + 1 < len(c) and days[end + 1] == days[i] and tod[end + 1] <= R["risk"]["exit_by"]:
                end += 1
            row = {"side": x["side"], "risk": risk}
            for tr in R["risk"]["targets_r"]:
                res, _, r = msw._replay(H, L, C, "bull" if sgn > 0 else "bear", i, entry, sl, entry + sgn * tr * risk, end - i)
                row[f"r_{tr}R"], row[f"res_{tr}R"] = r, res
            row["best_30m"] = 0.0
            rows.append(row)
    return pd.DataFrame(rows)


def _trow(df: pd.DataFrame, lab: str, R: dict) -> dict:
    n, m = len(df), R["history"]["min_sample_size"]
    base = {"Setup": lab, "Trades": n}
    if n < m:
        return {**base, "Avg R (1R target)": "INSUFFICIENT SAMPLE"}
    return {**base, "Avg R (1R target)": round(df["r_1R"].mean(), 2), "1R hit %": round(100 * (df["res_1R"] == "TARGET").mean(), 1),
            "Avg R (2R target)": round(df["r_2R"].mean(), 2), "2R hit %": round(100 * (df["res_2R"] == "TARGET").mean(), 1),
            "Median best move 30 min": "" if df["best_30m"].isna().all() else round(df["best_30m"].median()), "Median risk (pts)": round(df["risk"].median())}


# ─────────────────────────────── 2. cliff ratio ─────────────────────────────
def walls(df: pd.DataFrame, spot: float, R: dict | None = None) -> dict:
    """Max-OI put wall below spot and call wall above, with the cliff behind each."""
    R = R or rules()
    W = R["cliff"]
    step = W["strike_step"]
    d = df.copy()
    d["strike"] = pd.to_numeric(d["strike"], errors="coerce")
    d = d[(d["strike"] % step == 0) & d["strike"].between(spot - W["window_pts"], spot + W["window_pts"])]
    d = d.set_index("strike").sort_index()
    out = {}
    for side, col, below in (("PE", "pe_oi", True), ("CE", "ce_oi", False)):
        cand = d[d.index <= spot] if below else d[d.index >= spot]
        if cand.empty or cand[col].max() <= 0:
            continue
        k = float(cand[col].idxmax())
        wall = float(cand[col].max())
        beyond = [k - step * j for j in (1, 2)] if below else [k + step * j for j in (1, 2)]
        vals = [float(d[col].get(b, np.nan)) for b in beyond]
        vals = [v for v in vals if not np.isnan(v)]
        ratio = (np.mean(vals) / wall) if vals and wall > 0 else np.nan
        rest = d[d.index < k - step] if below else d[d.index > k + step]
        nxt = float(rest[col].idxmax()) if len(rest) and rest[col].max() > 0 else np.nan
        out[side] = {"strike": k, "oi": wall, "cliff_ratio": ratio, "thin": bool(ratio < W["thin_below"]) if vals else False,
                     "next_wall": nxt}
    return out


def scan_breaks(snaps: list, R: dict | None = None) -> list[dict]:
    """Walk one day's snapshots. The 'active' wall on each side is the one computed while price was still on its
    near side; a break = price later beyond it by break_buffer_pts. Then the next wall becomes active."""
    R = R or rules()
    b = R["cliff"]["break_buffer_pts"]
    active, out = {}, []
    for k, (ts, spot, df) in enumerate(snaps):
        try:
            w = walls(df, spot, R)
        except Exception as exc:
            log.info("walls failed %s: %s", ts, exc)
            continue
        for side in ("PE", "CE"):
            a = active.get(side)
            near = a is not None and ((spot >= a["strike"]) if side == "PE" else (spot <= a["strike"]))
            if a is None or near:
                if side in w:
                    active[side] = w[side]
                continue
            beyond = (spot < a["strike"] - b) if side == "PE" else (spot > a["strike"] + b)
            if beyond:
                out.append({"k": k, "time": ts, "spot": spot, "side": side, **a})
                if side in w:
                    active[side] = w[side]
    return out


def snapshot_days() -> list[date]:
    root = Path(getattr(config, "SNAPSHOT_DIR", HERE / "data" / "snapshots"))
    return sorted(pd.Timestamp(p.name).date() for p in root.glob("20*-*-*") if p.is_dir()) if root.exists() else []


def cliff_breaks(R: dict | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Wall breaks on every stored snapshot day + the move that followed; and the baseline moves at all times."""
    import store
    R = R or rules()
    W = R["cliff"]
    rows, base = [], []
    for day in snapshot_days():
        try:
            snaps = store.load_day(day)
        except Exception as exc:
            log.info("skip %s: %s", day, exc)
            continue
        if len(snaps) < 20:
            continue
        t = [x[0] for x in snaps]
        sp = np.array([x[1] for x in snaps], dtype=float)
        for k, ts in enumerate(t):
            j30 = [j for j in range(k + 1, len(snaps)) if t[j] <= ts + timedelta(minutes=30)]
            j15 = [j for j in j30 if t[j] <= ts + timedelta(minutes=15)]
            if j30 and "09:30" <= ts.strftime("%H:%M") <= "15:00":
                base.append({"down15": sp[k] - sp[j15].min() if j15 else 0, "up15": sp[j15].max() - sp[k] if j15 else 0,
                             "down30": sp[k] - sp[j30].min(), "up30": sp[j30].max() - sp[k]})
        for x in scan_breaks(snaps, R):
            k, ts, spot = x["k"], x["time"], x["spot"]
            if not ("09:30" <= ts.strftime("%H:%M") <= "15:00"):
                continue
            j30 = [j for j in range(k + 1, len(snaps)) if t[j] <= ts + timedelta(minutes=30)]
            if not j30:
                continue
            j15 = [j for j in j30 if t[j] <= ts + timedelta(minutes=15)] or j30[:1]
            sgn = -1 if x["side"] == "PE" else 1
            f15, f30 = sp[j15], sp[j30]
            rows.append({"day": day, "time": ts, "side": x["side"], "wall": x["strike"], "cliff_ratio": x["cliff_ratio"],
                         "thin": x["thin"], "next_wall": x["next_wall"], "spot": spot,
                         "run15": (spot - f15.min()) if sgn < 0 else (f15.max() - spot),
                         "run30": (spot - f30.min()) if sgn < 0 else (f30.max() - spot),
                         "against30": (f30.max() - spot) if sgn < 0 else (spot - f30.min()),
                         "reached_next": bool(not np.isnan(x["next_wall"]) and
                                              ((f30.min() <= x["next_wall"]) if sgn < 0 else (f30.max() >= x["next_wall"])))})
    return pd.DataFrame(rows), pd.DataFrame(base)


# ─────────────────────────────── backtest ───────────────────────────────────
def backtest(R: dict | None = None) -> dict:
    R = R or rules()
    out = {"verdicts": {}}
    m1 = minute_history()
    if m1 is not None and len(m1) > 5000:
        c3 = bars(m1, 3)
        out["span"] = (c3["timestamp"].min(), c3["timestamp"].max(), len(c3))
        t = traps(c3, R)
        ts_ = traps(c3, R, session_levels=True)
        ctl = traps(msw._shuffle(c3), R)
        rows = []
        if len(t):
            f = t[t["side"].eq("PE") | t["trend_ok"]]          # the live CE alert needs the rising-closes filter
            rows += [_trow(f, "Trap alerts as live (PDH/PDL/PWH/PWL/PMH/PML)", R),
                     _trow(f[f["side"] == "CE"], "  CE (support swept, reclaimed +15)", R),
                     _trow(f[f["side"] == "PE"], "  PE (resistance swept, back below)", R),
                     _trow(t[t["side"] == "CE"], "  CE without the rising-closes filter", R),
                     _trow(f[f["risk"] >= R["risk"]["min_sl_points"]], f"  only SL >= {R['risk']['min_sl_points']} pts", R)]
            for lv in ("PDH", "PDL", "PWH", "PWL", "PMH", "PML"):
                rows.append(_trow(f[f["level"] == lv], f"    at {lv}", R))
        if len(ts_):
            g = ts_[ts_["side"].eq("PE") | ts_["trend_ok"]]
            rows.append(_trow(g[g["level"].str.startswith("Session")], "  (extra) session high/low as levels", R))
        cf = ctl[ctl["side"].eq("PE") | ctl["trend_ok"]] if len(ctl) else ctl
        rows.append(_trow(cf, f"CONTROL 2: same rule on shuffled bars ({rules_shuffles()} pooled)", R))
        f = t[t["side"].eq("PE") | t["trend_ok"]] if len(t) else t
        rnd = random_entries(c3, f, R) if len(f) else pd.DataFrame()
        if len(rnd):
            rows.append(_trow(rnd.assign(best_30m=np.nan), "CONTROL 1: random times, same side / SL / targets (5 each)", R))
        out["trap"] = pd.DataFrame(rows)
        if len(f) and len(rnd):
            # primary yardstick = random timing with the same trade shape (the shuffle control reads this rule
            # ~0.08 R low even on pure random data, so it is shown but not used for the verdict)
            for key, sd in (("trap_all", None), ("trap_ce", "CE"), ("trap_pe", "PE")):
                a = f if sd is None else f[f["side"] == sd]
                b = rnd if sd is None else rnd[rnd["side"] == sd]
                out["verdicts"][key] = _verdict(a["r_1R"], b["r_1R"], R, key)
    else:
        out["trap_error"] = "no 1-min history (Dhan token / Data API?)"
    br, base = cliff_breaks(R)
    out["cliff_days"] = len(snapshot_days())
    if len(br):
        rows = []
        for lab, x in (("All wall breaks", br), ("  THIN cliff behind the wall", br[br["thin"]]),
                       ("  thick cliff", br[~br["thin"]]), ("  PE wall broken (down)", br[br["side"] == "PE"]),
                       ("  CE wall broken (up)", br[br["side"] == "CE"])):
            rows.append({"Wall breaks": lab, "Cases": len(x),
                         "Median run 15 min": round(x["run15"].median()) if len(x) else "",
                         "Median run 30 min": round(x["run30"].median()) if len(x) else "",
                         "Median against 30 min": round(x["against30"].median()) if len(x) else "",
                         "Reached next wall in 30 min %": round(100 * x["reached_next"].mean()) if len(x) else ""})
        if len(base):
            rows.append({"Wall breaks": "BASELINE: any snapshot time (same direction)", "Cases": len(base),
                         "Median run 15 min": round(np.median(np.r_[base["down15"], base["up15"]])),
                         "Median run 30 min": round(np.median(np.r_[base["down30"], base["up30"]])),
                         "Median against 30 min": "", "Reached next wall in 30 min %": ""})
        out["cliff"] = pd.DataFrame(rows)
        thin, thick = br[br["thin"]], br[~br["thin"]]
        n = len(thin)
        if n >= R["history"]["min_sample_size"] and len(thick) >= R["history"]["min_sample_size"]:
            ratio = thin["run30"].median() / max(1e-9, thick["run30"].median())
            lvl = 1 if ratio >= R["cliff"]["thin_runs_x"] else 0
            if lvl and n < R["history"]["min_trades_for_star"]:
                v = "PROMISING, SMALL SAMPLE"
                lvl = 0
            else:
                v = "THIN CLIFFS RUN FURTHER" if lvl else "NO DIFFERENCE"
            out["verdicts"]["cliff"] = {"rule": "Thin cliff behind a broken wall", "verdict": v, "level": lvl, "n": n,
                                        "detail": f"median 30-min run {thin['run30'].median():.0f} pts thin vs "
                                                  f"{thick['run30'].median():.0f} thick, {n} thin cases"}
        else:
            out["verdicts"]["cliff"] = {"rule": "Thin cliff behind a broken wall", "verdict": "NOT ENOUGH DATA",
                                        "level": 0, "n": n, "detail": f"{len(br)} wall breaks on {out['cliff_days']} days"}
    else:
        out["cliff_error"] = f"no wall breaks found on {out['cliff_days']} snapshot days"
    return out


def rules_shuffles() -> int:
    return int(msw.rules().get("history", {}).get("control_shuffles", 5))


def _verdict(a: pd.Series, b: pd.Series, R: dict, key: str) -> dict:
    H = R["history"]
    if len(a) < H["min_sample_size"] or len(b) < H["min_sample_size"]:
        return {"rule": key, "verdict": "NOT ENOUGH DATA", "level": 0, "n": int(len(a)), "detail": f"{len(a)} trades"}
    d = a.mean() - b.mean()
    se = float(np.sqrt(a.var(ddof=1) / len(a) + b.var(ddof=1) / len(b)))
    sure = abs(d) >= H.get("min_z", 2.0) * se            # a 0.1 R gap on ~200 trades is often just luck
    lvl = (1 if d >= H["beats_by_r"] else -1 if d <= -H["beats_by_r"] else 0) if sure else 0
    v = {1: "BEATS RANDOM", 0: "NO EDGE", -1: "WORSE THAN RANDOM"}[lvl]
    if lvl > 0 and len(a) < H["min_trades_for_star"]:
        v, lvl = "PROMISING, SMALL SAMPLE", 0
    return {"rule": key, "verdict": v, "level": lvl, "n": int(len(a)),
            "detail": f"{a.mean():+.2f} R vs {b.mean():+.2f} R at random times, {len(a)} trades, "
                      f"gap = {d / se if se > 0 else 0:.1f} x its noise (needs {H.get('min_z', 2.0):.0f})"}


def save_verdicts(v: dict) -> None:
    VERDICT_PATH.parent.mkdir(parents=True, exist_ok=True)
    VERDICT_PATH.write_text(json.dumps({"run": str(date.today()), **v}, indent=1, default=str))


def load_verdicts() -> dict:
    try:
        return json.loads(VERDICT_PATH.read_text())
    except (OSError, ValueError):
        return {}


# ─────────────────────────────── live ───────────────────────────────────────
def live_cliff(day: date | None = None) -> dict | None:
    """Walls on the latest snapshot + a break vs the previous snapshot."""
    import store
    R = rules()
    snaps = store.load_day(day or date.today())
    if len(snaps) < 2:
        return None
    t1, s1, d1 = snaps[-1]
    w1 = walls(d1, s1, R)
    br = scan_breaks(snaps, R)
    brk = br[-1] if br and br[-1]["k"] == len(snaps) - 1 else None
    return {"time": t1, "spot": s1, "walls": w1, "break": brk}


def cliff_text(x: dict) -> str:
    b = x["break"]
    down = b["side"] == "PE"
    thin = "THIN OI behind it - fast move likely" if b["thin"] else "OI stacked behind it - move likely absorbed"
    nxt = f" towards the next wall {b['next_wall']:,.0f}" if b["thin"] and not np.isnan(b["next_wall"]) else ""
    return (f"{b['strike']:,.0f} {'PE' if down else 'CE'} wall BROKEN ({'down' if down else 'up'}, spot {x['spot']:,.0f}): "
            f"{thin}{nxt} (cliff ratio {b['cliff_ratio']:.2f})")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backtest", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    pd.set_option("display.width", 230, "display.max_columns", 20, "display.max_colwidth", 60)
    if a.backtest:
        b = backtest()
        if "span" in b:
            f, t, n = b["span"]
            print(f"3-min: {n:,} bars {f:%d-%b-%Y} .. {t:%d-%b-%Y} (from Dhan 1-min)")
            print("\n== Liquidity trap (price levels) ==")
            print(b["trap"].to_string(index=False))
        else:
            print(b.get("trap_error"))
        print(f"\n== Cliff behind a broken wall ({b.get('cliff_days', 0)} snapshot days) ==")
        print(b["cliff"].to_string(index=False) if "cliff" in b else b.get("cliff_error"))
        print("\n== verdicts ==")
        for k, v in b["verdicts"].items():
            print(f"{v['verdict']:28} {k:10} {v['detail']}")
        save_verdicts(b["verdicts"])
        print(f"saved {VERDICT_PATH}")
        return 0
    x = live_cliff()
    if not x:
        print("no option-chain snapshots today yet")
        return 0
    for side, w in x["walls"].items():
        print(f"{side} wall {w['strike']:,.0f} ({w['oi']:,.0f} OI): cliff ratio {w['cliff_ratio']:.2f} "
              f"-> {'THIN behind it' if w['thin'] else 'stacked behind it'}; next wall {w['next_wall']:,.0f}")
    if x["break"]:
        print(cliff_text(x))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
