"""
ema8_signals.py — YOUR 8 EMA method, from homework batches 1 + 2 (04-Aug .. 30-Sep-2026), on INDEX candles.

  THE TRADE
    signal     a candle CLOSES above the 8 EMA (previous close was not above it)  -> CE
               a candle CLOSES below the 8 EMA (previous close was not below it)  -> PE
               (a doji is not a signal candle - body must be >= body_min_share of the range)
    trigger    FOLLOW-THROUGH: a later candle (within trigger_bars) breaks the signal candle's high (CE) / low (PE)
               -> entry at that break. No break = no trade.
    SL         the signal candle's LOW for CE / HIGH for PE (the red box on your charts), +-1 point
    exits      trail  - SL follows each closed candle's low (CE) / high (PE)   ("TSL")
               doji   - a doji forms -> out on the break of the doji           ("exit at doji")
               bb     - price touches the Bollinger band on the trade side     ("upper / lower BB")
               ema    - a close back through the 8 EMA
               mix    - whichever of trail / doji / bb comes first (closest to how you actually exit)
    tags       1st 15-min candle colour (bias), gap up / down (you avoid CE on gap-up days), entry number of the
               day for that side (you avoid the late 3rd entry), 4 / 8 EMA separation

  .venv/bin/python ema8_signals.py                    # today so far, 15m / 5m / 3m
  .venv/bin/python ema8_signals.py --day 2026-09-02   # any day (replay, no look-ahead)
  .venv/bin/python ema8_signals.py --backtest         # 5 years vs RANDOM candles with the same break / SL / exits
  .venv/bin/python ema8_signals.py --backtest --tf 15m

Numbers in ema8_signals_rules.json. Tags are SHOWN, not used as filters, until the backtest says which matter.
Information only - not financial advice.
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

import pa_signals as pa

log = logging.getLogger("ema8_signals")
HERE = Path(__file__).resolve().parent
RULES_PATH = HERE / "ema8_signals_rules.json"
VERDICT_PATH = HERE / "data" / "ema8_signals_verdict.json"
TF_MIN = pa.TF_MIN
TF_TAG = {"15m": "[8EMA 15m SUPER]", "5m": "[8EMA 5m]", "3m": "[8EMA 3m]"}
TF_HEAD = {"15m": "🟪🟪 15-MIN SUPER 8 EMA", "5m": "🟦 5-MIN 8 EMA", "3m": "🟨 3-MIN 8 EMA (noisy)"}
MODES = ("trail", "doji", "bb", "ema", "mix", "cap")   # cap = your beginner plan: TSL each candle, max +120 pts


def rules() -> dict:
    return json.loads(RULES_PATH.read_text())


def rules_for(tf: str, R: dict | None = None) -> dict:
    R = copy.deepcopy(R or rules())
    for sec, vals in R.get("tf_overrides", {}).get(tf, {}).items():
        if isinstance(vals, dict) and isinstance(R.get(sec), dict):
            R[sec].update(vals)
    return R


# ─────────────────────────────── indicators ─────────────────────────────────
def _arrays(c: pd.DataFrame, R: dict) -> dict:
    S, X = R["signal"], R["exit"]
    c = c.reset_index(drop=True)
    ts = pd.to_datetime(c["timestamp"])
    cl = c["close"].astype(float)
    mid = cl.rolling(X["bb_period"], min_periods=5).mean()
    sd = cl.rolling(X["bb_period"], min_periods=5).std(ddof=0)
    return {"O": c["open"].values.astype(float), "H": c["high"].values.astype(float),
            "L": c["low"].values.astype(float), "C": cl.values,
            "ema": cl.ewm(span=S["ema_span"], adjust=False).mean().values,
            "ema4": cl.ewm(span=S["fast_ema_span"], adjust=False).mean().values,
            "bbu": (mid + X["bb_std"] * sd).values, "bbl": (mid - X["bb_std"] * sd).values,
            "days": ts.dt.date.values, "tod": ts.dt.strftime("%H:%M").values, "ts": ts.values, "n": len(c)}


def _is_doji(a, k, share) -> bool:
    rng = a["H"][k] - a["L"][k]
    return rng > 0 and abs(a["C"][k] - a["O"][k]) <= share * rng


# ─────────────────────────────── exits ──────────────────────────────────────
def simulate(a: dict, side: str, j: int, entry: float, sl: float, R: dict) -> dict:
    """Walk forward from the entry candle j. Returns r_<mode> and exit_<mode> for every exit mode."""
    X = R["exit"]
    s = 1 if side == "CE" else -1
    O, H, L, C, days, tod, n = a["O"], a["H"], a["L"], a["C"], a["days"], a["tod"], a["n"]
    risk = abs(entry - sl)
    slip = R["risk"].get("stop_slip_points", 1.0)
    end = j
    while end + 1 < n and days[end + 1] == days[j] and tod[end + 1] <= X["exit_by"]:
        end += 1
    out = {}
    for mode in MODES:
        stop, dlev, r, how, px, kx = sl, None, None, "day end", None, end
        cap = X.get("max_points", 120)
        mfe = 0.0
        for k in range(j, end + 1):
            lo, hi = L[k], H[k]
            kx = k
            # 1. stop (original / trailed) and doji level: gap through -> fill at the open
            lv = stop if dlev is None else (max(stop, dlev) if s > 0 else min(stop, dlev))
            if (lo <= lv) if s > 0 else (hi >= lv):
                px = (min(lv, O[k]) if s > 0 else max(lv, O[k])) if k > j else lv
                px -= s * slip                      # a stop fills a little worse than its level
                r, how = s * (px - entry) / risk, ("SL" if lv == sl else "doji" if lv == dlev else "TSL")
                break
            mfe = max(mfe, (hi - entry) if s > 0 else (entry - lo))
            # 1b. beginner plan: book the whole thing at +max_points
            if mode == "cap" and mfe >= cap:
                px = entry + s * cap
                r, how = cap / risk, f"+{cap:.0f} max target"
                break
            # 2. Bollinger band touch on the trade side
            if mode in ("bb", "mix"):
                band = a["bbu"][k - 1] if s > 0 else a["bbl"][k - 1]     # band known before the candle
                if k > j and not np.isnan(band) and ((hi >= band) if s > 0 else (lo <= band)) and s * (band - entry) > 0:
                    px = max(band, O[k]) if s > 0 else min(band, O[k])
                    r, how = s * (px - entry) / risk, "BB"
                    break
            # 3. close back through the 8 EMA
            if mode == "ema" and ((C[k] < a["ema"][k]) if s > 0 else (C[k] > a["ema"][k])):
                r, how = s * (C[k] - entry) / risk, "8 EMA"
                break
            # 4. after the candle closes: trail / doji level for the next candle
            if mode in ("trail", "mix", "cap") and s * (C[k] - entry) >= X["trail_after_r"] * risk:
                stop = max(stop, lo - 1) if s > 0 else min(stop, hi + 1)
            if mode in ("doji", "mix") and _is_doji(a, k, X["doji_body_share"]):
                dlev = lo - 1 if s > 0 else hi + 1
        if r is None:
            r, px = s * (C[end] - entry) / risk, C[end]
        out[f"r_{mode}"], out[f"exit_{mode}"] = float(r), how
        if mode == "cap":
            out.update(px_cap=float(px if px is not None else entry + s * r * risk), t_cap=pd.Timestamp(a["ts"][kx]),
                       mfe_cap=float(mfe))
    return out


# ─────────────────────────────── scan ───────────────────────────────────────
def _trigger(a: dict, i: int, side: str, R: dict):
    """Follow-through break of candle i. Returns (j, entry, sl, risk, skip)."""
    T, K = R["trigger"], R["risk"]
    s = 1 if side == "CE" else -1
    H, L, O, days, n = a["H"], a["L"], a["O"], a["days"], a["n"]
    brk = H[i] + T["buffer_points"] if s > 0 else L[i] - T["buffer_points"]
    sl = L[i] - K["sl_buffer_points"] if s > 0 else H[i] + K["sl_buffer_points"]
    risk0 = abs(brk - sl)
    if risk0 < K["min_sl_points"]:
        return None, brk, sl, risk0, f"SL only {risk0:.0f} pts (< {K['min_sl_points']})"
    if risk0 > K["max_sl_points"]:
        return None, brk, sl, risk0, f"signal candle too long: SL {risk0:.0f} pts (> {K['max_sl_points']})"
    for q in range(i + 1, min(n, i + 1 + T["trigger_bars"])):
        if days[q] != days[i]:
            break
        if (H[q] >= brk) if s > 0 else (L[q] <= brk):
            entry = (max(brk, O[q]) if s > 0 else min(brk, O[q])) + s * K.get("stop_slip_points", 1.0)  # a stop-entry fills a bit late
            return q, entry, sl, abs(entry - sl), None
    return None, brk, sl, risk0, None


def scan(c: pd.DataFrame, R: dict | None = None, only_day: date | None = None) -> pd.DataFrame:
    R = R or rules()
    S, G = R["signal"], R["tags"]
    a = _arrays(c, R)
    O, H, L, C, ema, days, tod, n = a["O"], a["H"], a["L"], a["C"], a["ema"], a["days"], a["tod"], a["n"]
    first_idx, prev_close = {}, {}
    last_close = None
    for i in range(n):
        if days[i] not in first_idx:
            first_idx[days[i]] = i
            prev_close[days[i]] = last_close
        last_close = C[i]
    rows, count = [], {}
    for i in range(1, n):
        d = days[i]
        if only_day is not None and d != only_day:
            continue
        if not (S["start"] <= tod[i] <= S["end"]):
            continue
        up = C[i] > ema[i] and C[i - 1] <= ema[i - 1]
        dn = C[i] < ema[i] and C[i - 1] >= ema[i - 1]
        kind = "cross"
        if not (up or dn) and S.get("stage3", True):
            # STAGE 3: already on this side of the 8 EMA, the candle pulled back INTO the EMA and closed away from it
            # again (your "stage 3 8 EMA" entries in a trend)
            tol = S.get("stage3_touch_points", 3)
            up = C[i] > ema[i] and C[i - 1] > ema[i - 1] and L[i] <= ema[i] + tol and C[i] > O[i]
            dn = C[i] < ema[i] and C[i - 1] < ema[i - 1] and H[i] >= ema[i] - tol and C[i] < O[i]
            kind = "stage3"
        if not (up or dn):
            continue
        rng = H[i] - L[i]
        if rng <= 0 or abs(C[i] - O[i]) < S["body_min_share"] * rng:
            continue
        side = "CE" if up else "PE"
        f = first_idx[d]
        pc = prev_close[d]
        gap = (O[f] - pc) if pc is not None else 0.0
        gap_w = "gap-up" if gap >= G["gap_points"] else "gap-down" if gap <= -G["gap_points"] else "flat open"
        # first 15-min candle of the day (all 15-min bars of 09:15 on lower timeframes)
        fe = f
        while fe + 1 < n and days[fe + 1] == d and tod[fe + 1] < "09:30":
            fe += 1
        fc_green = C[fe] >= O[f]
        sep = abs(a["ema4"][i] - ema[i])
        j, entry, sl, risk, skip = _trigger(a, i, side, R)
        x = {"idx": i, "day": d, "time": pd.Timestamp(a["ts"][i]), "side": side, "kind": kind, "sig_high": H[i], "sig_low": L[i],
             "close": C[i], "ema8": ema[i], "entry": entry, "sl": sl, "risk": risk, "skip": skip,
             "triggered": j is not None, "trigger_time": pd.Timestamp(a["ts"][j]) if j is not None else pd.NaT,
             "first_candle": "green" if fc_green else "red", "bias_ok": fc_green == (side == "CE"),
             "gap": gap_w, "gap_pts": gap,
             "gap_against": (side == "CE" and gap_w == "gap-up") or (side == "PE" and gap_w == "gap-down"),
             "sep": sep, "sep_ok": sep >= G["sep_min_points"]}
        if j is not None:
            count[(d, side)] = count.get((d, side), 0) + 1
            x["entry_no"] = count[(d, side)]
            x["late_entry"] = x["entry_no"] > G["max_entry_of_day"]
            x.update(simulate(a, side, j, entry, sl, R))
        rows.append(x)
    return pd.DataFrame(rows)


# ─────────────────────────────── backtest ───────────────────────────────────
def _history(tf: str) -> pd.DataFrame | None:
    if tf == "15m":
        import mentor_structure as ms
        return ms.load_history(ms.rules())
    try:
        import mentor_traps as mt
        m1 = mt.minute_history()
        if m1 is not None and len(m1):
            return pa._resample(m1, TF_MIN[tf])
    except Exception as exc:
        log.info("1-min history unavailable (%s)", exc)
    m = pa._minute_data(date.today(), 400)
    return pa._resample(m, TF_MIN[tf]) if m is not None else None


def random_control(c: pd.DataFrame, t: pd.DataFrame, R: dict, k: int = 5, seed: int = 7) -> pd.DataFrame:
    """For every real trade, k RANDOM candles of the same day (same side, same time window) are treated as the
    'signal candle': the same follow-through break of its high / low, the SAME SL distance as the real trade (so
    slippage costs the same in R), the same exits. Only the 8 EMA close differs, so this measures whether the EMA close itself adds anything."""
    a = _arrays(c, R)
    S = R["signal"]
    by_day = pd.Series(np.arange(a["n"])).groupby(a["days"]).apply(list).to_dict()
    rng = np.random.default_rng(seed)
    Rc = copy.deepcopy(R)
    Rc["risk"].update(min_sl_points=0, max_sl_points=1e9)
    rows = []
    for _, x in t.iterrows():
        pool = [i for i in by_day[x["day"]] if S["start"] <= a["tod"][i] <= S["end"]]
        for i in rng.choice(pool, size=min(k, len(pool)), replace=False):
            j, entry, _, _, _ = _trigger(a, int(i), x["side"], Rc)
            if j is None:
                continue
            sl = entry - (1 if x["side"] == "CE" else -1) * x["risk"]      # same SL distance as the real trade
            rows.append({"side": x["side"], **simulate(a, x["side"], j, entry, sl, R)})
    return pd.DataFrame(rows)


def backtest(R: dict | None = None, tf: str = "15m") -> dict:
    R = rules_for(tf, R)
    h = _history(tf)
    if h is None or len(h) < 1000:
        return {"error": f"no {tf} history"}
    c = h.reset_index(drop=True)
    s = scan(c, R)
    t = s[s["triggered"]] if len(s) else s
    if not len(t):
        return {"error": "no triggered signals"}
    rnd = random_control(c, t, R)
    Hh = R["history"]

    def row(x, lab):
        if len(x) < Hh["min_sample_size"]:
            return {"Setup": lab, "Trades": len(x), "Avg R (mix)": "INSUFFICIENT SAMPLE"}
        d = {"Setup": lab, "Trades": len(x)}
        for m in MODES:
            d[f"Avg R ({m})"] = round(x[f"r_{m}"].mean(), 2)
        d["Win % (mix)"] = round(100 * (x["r_mix"] > 0).mean(), 1)
        d["Avg SL pts"] = round(x["risk"].mean(), 1) if "risk" in x else ""
        return d
    rows = [row(t, "ALL triggered 8 EMA trades"), row(rnd, "CONTROL: random candles, same break / candle SL / exits")]
    for side in ("CE", "PE"):
        rows.append(row(t[t["side"] == side], f"  {side}"))
    rows.append(row(t[t["bias_ok"]], "  with the 1st 15-min candle colour"))
    rows.append(row(t[~t["bias_ok"]], "  against the 1st 15-min candle colour"))
    rows.append(row(t[t["gap_against"]], "  CE on gap-up / PE on gap-down (you avoid these)"))
    rows.append(row(t[~t["gap_against"]], "  not against the gap"))
    for nth in (1, 2):
        rows.append(row(t[t["entry_no"] == nth], f"  entry #{nth} of the day for that side"))
    rows.append(row(t[t["entry_no"] >= 3], "  entry #3+ (late entry)"))
    tgt = R["exit"].get("beginner_points", [15, 20])
    for p_ in tgt:
        print(f"  {tf}: trades that went +{p_} pts in your favour before the exit: "
              f"{100 * (t['mfe_cap'] >= p_).mean():.1f}%")
    rows.append(row(t[t["kind"] == "cross"], "  fresh close across the 8 EMA"))
    rows.append(row(t[t["kind"] == "stage3"], "  STAGE 3: pullback into the 8 EMA, close away again"))
    rows.append(row(t[t["risk"] <= 60], "  SL <= 60 pts"))
    rows.append(row(t[t["risk"] > 60], "  SL > 60 pts (long signal candle)"))
    rows.append(row(t[t["sep_ok"]], "  4 / 8 EMA separated"))
    rows.append(row(t[~t["sep_ok"]], "  4 / 8 EMA tangled"))
    tm = pd.to_datetime(t["trigger_time"]).dt.strftime("%H:%M")
    rows.append(row(t[tm < "12:00"], "  morning (before 12:00)"))
    rows.append(row(t[tm >= "12:00"], "  afternoon"))
    good = t[t["bias_ok"] & ~t["gap_against"] & (t["entry_no"] <= 2)]
    rows.append(row(good, "  YOUR CONTEXT: with bias, not against gap, entry 1-2"))
    verd = {m: pa._v(t[f"r_{m}"], rnd[f"r_{m}"], R, m) for m in MODES}
    verd["context_mix"] = pa._v(good["r_mix"], rnd["r_mix"], R, "mix") if len(good) else {"verdict": "NOT ENOUGH DATA", "detail": "0"}
    verd["all"] = verd["mix"]
    return {"table": pd.DataFrame(rows), "verdicts": verd, "signals": len(s), "triggered": len(t),
            "span": (c["timestamp"].min(), c["timestamp"].max())}


def verdict_text(tf: str = "15m") -> str:
    try:
        v = json.loads(VERDICT_PATH.read_text()).get(tf, {}).get("all", {})
        return f"{v.get('verdict')} ({v.get('detail')})" if v else "not tested yet"
    except (OSError, ValueError, AttributeError):
        return f"not tested yet - run ema8_signals.py --backtest --tf {tf}"


# ─────────────────────────────── candle context ─────────────────────────────
def candle_context(c: pd.DataFrame, R: dict | None = None, only_day: date | None = None) -> list[dict]:
    """For every closed 15m candle (or only_day), describe WHERE it sits — body type, EMA side,
    BB position, signal status. Returns a list of dicts, one per candle."""
    R = R or rules_for("15m")
    a = _arrays(c, R)
    S = R["signal"]
    rows = []
    n = a["n"]
    for i in range(1, n):
        d = a["days"][i]
        if only_day is not None and d != only_day:
            continue
        tod = a["tod"][i]
        O, H, L, C = a["O"][i], a["H"][i], a["L"][i], a["C"][i]
        ema = a["ema"][i]
        bbu = a["bbu"][i - 1] if i > 0 else float("nan")
        bbl = a["bbl"][i - 1] if i > 0 else float("nan")
        rng = H - L
        body = abs(C - O)
        body_share = body / rng if rng > 0 else 0

        # ── candle body type ──────────────────────────────────────────────
        if body_share <= R["exit"]["doji_body_share"]:
            ctype = "DOJI"
        elif body_share >= 0.85 and (H - max(O, C)) < 0.07 * rng and (min(O, C) - L) < 0.07 * rng:
            ctype = "MARUBOZU"
        elif (H - max(O, C)) > 0.55 * rng and body_share < 0.35:
            ctype = "SHOOTING STAR" if C < O else "INVERTED HAMMER"
        elif (min(O, C) - L) > 0.55 * rng and body_share < 0.35:
            ctype = "HAMMER" if C > O else "HANGING MAN"
        elif body_share >= 0.5:
            ctype = "STRONG"
        else:
            ctype = "NORMAL"

        # ── EMA side ──────────────────────────────────────────────────────
        if C > ema:
            ema_side = "ABOVE"
        elif C < ema:
            ema_side = "BELOW"
        else:
            ema_side = "ON"
        ema_dist = round(abs(C - ema), 1)

        # ── fresh cross? stage3? ──────────────────────────────────────────
        prev_C = a["C"][i - 1]
        prev_ema = a["ema"][i - 1]
        up_cross = C > ema and prev_C <= prev_ema
        dn_cross = C < ema and prev_C >= prev_ema
        tol = S.get("stage3_touch_points", 3)
        stage3_ce = (C > ema and prev_C > prev_ema and L <= ema + tol and C > O)
        stage3_pe = (C < ema and prev_C < prev_ema and H >= ema - tol and C < O)
        signal_kind = None
        signal_side = None
        if up_cross and body_share >= S["body_min_share"]:
            signal_kind, signal_side = "cross", "CE"
        elif dn_cross and body_share >= S["body_min_share"]:
            signal_kind, signal_side = "cross", "PE"
        elif stage3_ce and body_share >= S["body_min_share"]:
            signal_kind, signal_side = "stage3", "CE"
        elif stage3_pe and body_share >= S["body_min_share"]:
            signal_kind, signal_side = "stage3", "PE"

        # ── BB position ───────────────────────────────────────────────────
        bb_pos = "inside"
        if not (np.isnan(bbu) or np.isnan(bbl)):
            if H >= bbu:
                bb_pos = "touching/above UPPER BB"
            elif L <= bbl:
                bb_pos = "touching/below LOWER BB"
            elif C > (bbu + bbl) / 2:
                bb_pos = "upper half"
            else:
                bb_pos = "lower half"

        rows.append({
            "day": d, "time": tod, "open": O, "high": H, "low": L, "close": C,
            "ema8": round(ema, 1), "bbu": round(bbu, 1) if not np.isnan(bbu) else None,
            "bbl": round(bbl, 1) if not np.isnan(bbl) else None,
            "body_type": ctype, "candle_dir": "GREEN" if C >= O else "RED",
            "ema_side": ema_side, "ema_dist": ema_dist,
            "bb_pos": bb_pos, "signal_kind": signal_kind, "signal_side": signal_side,
        })
    return rows


def fmt_candle_context(rows: list[dict]) -> str:
    """Pretty-print candle context rows for CLI / log."""
    lines = []
    for r in rows:
        sig = ""
        if r["signal_side"]:
            sig = f"  ⚡ {r['signal_side']} SIGNAL ({r['signal_kind']})"
        bb_str = ""
        if r["bbu"]:
            bb_str = f"  BB {r['bb_pos']} (U {r['bbu']:,.0f} / L {r['bbl']:,.0f})"
        lines.append(
            f"{r['time']}  {r['candle_dir']:5} {r['body_type']:16} "
            f"O {r['open']:,.0f}  H {r['high']:,.0f}  L {r['low']:,.0f}  C {r['close']:,.0f}  "
            f"EMA8 {r['ema8']:,.1f} [{r['ema_side']} {r['ema_dist']:.0f}pts]{bb_str}{sig}"
        )
    return "\n".join(lines)


def candle_context_live(now: datetime | None = None) -> list[dict]:
    """Candle context for all closed 15m candles today (live use)."""
    now = now or datetime.now()
    R = rules_for("15m")
    try:
        c = pa.tf_candles("15m", now.date(), 5)
    except Exception as exc:
        log.info("15m candles unavailable: %s", exc)
        return []
    if c is None or not len(c):
        return []
    c = c[pd.to_datetime(c["timestamp"]) + timedelta(minutes=15) <= now].reset_index(drop=True)
    if not len(c):
        return []
    return candle_context(c, R, only_day=now.date())


# ─────────────────────────────── live ───────────────────────────────────────
def live(now: datetime | None = None, tfs: list[str] | None = None) -> dict:
    base = rules()
    now = now or datetime.now()
    out = {"ctx": pa.briefing_context()}
    for tf in tfs or base.get("timeframes", ["15m", "5m", "3m"]):
        R = rules_for(tf, base)
        try:
            c = pa.tf_candles(tf, now.date(), 5)
        except Exception as exc:
            log.info("%s candles unavailable: %s", tf, exc)
            c = None
        if c is None or not len(c):
            continue
        c = c[pd.to_datetime(c["timestamp"]) + timedelta(minutes=TF_MIN[tf]) <= now].reset_index(drop=True)
        if not len(c) or pd.to_datetime(c["timestamp"]).dt.date.iloc[-1] != now.date():
            continue
        out[tf] = {"signals": scan(c, R, only_day=now.date()), "last_idx": len(c) - 1, "candles": c, "R": R}
    return out


def _tags(x: dict, R: dict) -> str:
    t = [("STAGE 3 pullback to the 8 EMA" if x.get("kind") == "stage3" else "fresh 8 EMA close"),
         f"1st 15m candle {x['first_candle']} ({'with' if x['bias_ok'] else 'AGAINST'} bias)",
         f"{x['gap']}" + (" - you avoid this side on this gap" if x["gap_against"] else ""),
         f"4/8 EMA {'separated' if x['sep_ok'] else 'tangled'} ({x['sep']:.0f} pts)"]
    if pd.notna(x.get("entry_no")) and x.get("entry_no"):
        t.append(f"entry #{int(x['entry_no'])} of the day" + (" - LATE, you usually skip" if x.get("late_entry") else ""))
    return " · ".join(t)


def fmt_signal(x: dict, R: dict, verdict: str, tf: str) -> str:
    buy = x["side"] == "CE"
    return (f"{TF_TAG[tf]} {TF_HEAD[tf]}\n{'🟢' if buy else '🔴'} 8 EMA SIGNAL · {x['side']} · {pd.Timestamp(x['time']):%H:%M} candle "
            f"closed {'above' if buy else 'below'} the 8 EMA ({x['close']:,.0f} vs {x['ema8']:,.0f})\n"
            f"  entry on break of {'high' if buy else 'low'} {x['entry']:,.0f} · SL candle {'low' if buy else 'high'} {x['sl']:,.0f} "
            f"({abs(x['entry'] - x['sl']):.0f} pts)" + (f"\n  ⛔ {x['skip']}" if isinstance(x.get("skip"), str) else "") +
            f"\n  {_tags(x, R)}\n  backtest: {verdict}")


def fmt_trigger(x: dict, tf: str) -> str:
    buy = x["side"] == "CE"
    return (f"{TF_TAG[tf]} ✅ 8 EMA TRIGGERED · BUY {x['side']} · entry {x['entry']:,.0f} · SL {x['sl']:,.0f} "
            f"({x['risk']:.0f} pts, signal candle {'low' if buy else 'high'}) · exits: TSL each candle "
            f"{'low' if buy else 'high'} · first aim +15 to +20 · trail up to +120 max")


def exit_hints(tf: str, key: str, side: str, c: pd.DataFrame, R: dict) -> list[tuple[str, str]]:
    """(id, text) hints on the LAST closed candle for an open trade."""
    a = _arrays(c, R)
    k = a["n"] - 1
    if k < 1:
        return []
    s = 1 if side == "CE" else -1
    t = pd.Timestamp(a["ts"][k])
    out = []
    if (a["C"][k] < a["L"][k - 1]) if s > 0 else (a["C"][k] > a["H"][k - 1]):
        out.append(("tsl", f"TSL: {t:%H:%M} candle closed {'below the previous candle low' if s > 0 else 'above the previous candle high'}"))
    if _is_doji(a, k, R["exit"]["doji_body_share"]):
        out.append(("doji", f"doji on the {t:%H:%M} candle - exit on the break of its {'low' if s > 0 else 'high'} "
                            f"{(a['L'][k] if s > 0 else a['H'][k]):,.0f}"))
    band = a["bbu"][k] if s > 0 else a["bbl"][k]
    if not np.isnan(band) and ((a["H"][k] >= band) if s > 0 else (a["L"][k] <= band)):
        out.append(("bb", f"touched the {'upper' if s > 0 else 'lower'} Bollinger band {band:,.0f}"))
    if (a["C"][k] < a["ema"][k]) if s > 0 else (a["C"][k] > a["ema"][k]):
        out.append(("ema", f"{t:%H:%M} candle closed back {'below' if s > 0 else 'above'} the 8 EMA"))
    return [(f"{key}|{i}{t:%H%M}", f"{TF_TAG[tf]} ⚠️ EXIT HINT for the {key.split('|')[1]}: {m}") for i, m in out]


# ─────────────────────────────── 5-year journal ─────────────────────────────
def _reason(x: dict) -> str:
    buy = x["side"] == "CE"
    k = "Stage 3 pullback to 8 EMA" if x.get("kind") == "stage3" else "close " + ("above" if buy else "below") + " 8 EMA"
    return (f"{x['time']:%H:%M} candle {k}, break of its {'high' if buy else 'low'}; SL candle "
            f"{'low' if buy else 'high'} {x['sl']:,.0f}; {x['first_candle']} 1st candle, {x['gap']}")


def journal(t: pd.DataFrame, R: dict) -> pd.DataFrame:
    """Your homework format, one row per trade. Exit = your beginner plan (TSL each candle, max +120)."""
    lo, hi = R["exit"].get("beginner_points", [15, 20])
    rows = []
    for d, g in t.sort_values("trigger_time").groupby("day"):
        tot = 0.0
        for n_, (_, x) in enumerate(g.iterrows(), 1):
            s = 1 if x["side"] == "CE" else -1
            pts = round(s * (x["px_cap"] - x["entry"]), 1)
            tot += pts
            ex = x["exit_cap"]
            ex = {"TSL": "trailing SL (candle " + ("low" if s > 0 else "high") + ")", "SL": "stop loss hit",
                  "day end": "15:15 day end"}.get(ex, ex)
            rows.append({"Date": f"{d:%d-%m-%Y}", "Trade #": n_, "Side": x["side"],
                         "Entry time": f"{x['trigger_time']:%H:%M}", "Entry price": round(x["entry"], 1),
                         "Entry reason": _reason(x), "SL": round(x["sl"], 1), "Risk pts": round(x["risk"], 1),
                         "Exit time": f"{x['t_cap']:%H:%M}", "Exit price": round(x["px_cap"], 1), "Exit reason": ex,
                         "Profit": pts if pts > 0 else 0.0, "Loss": pts if pts < 0 else 0.0,
                         "Max move pts": round(x["mfe_cap"], 1),
                         f"{lo} pts worked": "YES" if x["mfe_cap"] >= lo else "no",
                         f"{hi} pts worked": "YES" if x["mfe_cap"] >= hi else "no",
                         "Day total": None})
        rows[-1]["Day total"] = round(tot, 1)
    return pd.DataFrame(rows)


def write_journal(tf: str = "15m") -> Path | None:
    R = rules_for(tf)
    h = _history(tf)
    if h is None or not len(h):
        print(f"no {tf} history")
        return None
    s = scan(h.reset_index(drop=True), R)
    t = s[s["triggered"]]
    j = journal(t, R)
    out = HERE / "data" / "homework" / f"ema8_journal_{tf}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    j.to_csv(out, index=False)
    lo, hi = R["exit"].get("beginner_points", [15, 20])
    j["Year"] = j["Date"].str[-4:]
    j["pts"] = j["Profit"] + j["Loss"]
    y = j.groupby("Year").agg(Trades=("pts", "size"), Points=("pts", "sum"), Win_pct=("pts", lambda v: round(100 * (v > 0).mean(), 1)),
                              **{f"{lo}_worked_pct": (f"{lo} pts worked", lambda v: round(100 * (v == "YES").mean(), 1)),
                                 f"{hi}_worked_pct": (f"{hi} pts worked", lambda v: round(100 * (v == "YES").mean(), 1))})
    days = j.dropna(subset=["Day total"])
    print(y.to_string())
    print(f"\n{len(j):,} trades on {len(days):,} days · total {j['pts'].sum():+,.0f} pts · "
          f"green days {100 * (days['Day total'] > 0).mean():.0f}% · avg day {days['Day total'].mean():+.1f} pts")
    print(f"saved {out}")
    return out


def day_journal_text(day: date | None = None) -> str | None:
    """Today's 15-min journal (for the watch log after the close)."""
    day = day or date.today()
    R = rules_for("15m")
    c = pa.tf_candles("15m", day, 5)
    if c is None or not len(c):
        return None
    s = scan(c, R, only_day=day)
    t = s[s["triggered"]] if len(s) else s
    if not len(t):
        return f"{TF_TAG['15m']} 📒 8 EMA JOURNAL {day:%d-%m-%Y}: no trades"
    j = journal(t, R)
    lines = [f"{TF_TAG['15m']} 📒 8 EMA JOURNAL {day:%d-%m-%Y} (TSL each candle, max +120)"]
    lo, hi = R["exit"].get("beginner_points", [15, 20])
    for _, r in j.iterrows():
        pts = r["Profit"] + r["Loss"]
        lines.append(f"  #{r['Trade #']} {r['Side']} in {r['Entry time']} @ {r['Entry price']:,.0f} -> out {r['Exit time']} "
                     f"@ {r['Exit price']:,.0f} ({r['Exit reason']}) {pts:+.0f} pts · max move {r['Max move pts']:.0f} · "
                     f"{lo} pts {r[f'{lo} pts worked']} · {hi} pts {r[f'{hi} pts worked']}")
    lines.append(f"  DAY TOTAL {j['Day total'].dropna().iloc[-1]:+.0f} pts")
    return "\n".join(lines)


# ─────────────────────────────── CLI ────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backtest", action="store_true")
    ap.add_argument("--day")
    ap.add_argument("--tf", default="all")
    ap.add_argument("--journal", action="store_true", help="5-year trade journal in your homework format")
    ap.add_argument("--context", action="store_true", help="show candle-by-candle location table for the day")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    pd.set_option("display.width", 260, "display.max_columns", 20, "display.max_colwidth", 60)
    R = rules()
    tfs = R.get("timeframes", ["15m", "5m", "3m"]) if a.tf == "all" else [a.tf]
    if a.journal:
        for tf in tfs:
            write_journal(tf)
        return 0
    if a.context:
        day = pd.Timestamp(a.day).date() if a.day else date.today()
        R = rules_for("15m")
        c = pa.tf_candles("15m", day, 5)
        if c is None or day not in set(pd.to_datetime(c["timestamp"]).dt.date):
            print(f"no 15m candles for {day}")
            return 1
        rows = candle_context(c, R, only_day=day)
        print(f"\n===== 15m CANDLE CONTEXT — {day:%a %d-%b-%Y} ({len(rows)} candles) =====")
        print(fmt_candle_context(rows))
        return 0
    if a.backtest:
        try:
            allv = json.loads(VERDICT_PATH.read_text())
        except (OSError, ValueError):
            allv = {}
        for tf in tfs:
            b = backtest(R, tf)
            if "error" in b:
                print(f"{tf}: {b['error']}")
                continue
            print(f"\n===== {tf}: {b['span'][0]:%d-%b-%Y} .. {b['span'][1]:%d-%b-%Y}: {b['signals']:,} 8 EMA closes, "
                  f"{b['triggered']:,} with follow-through =====")
            print(b["table"].to_string(index=False))
            for k, v in b["verdicts"].items():
                if isinstance(v, dict):
                    print(f"{k:12} {v['verdict']:40} {v['detail']}")
            allv[tf] = b["verdicts"]
        allv["run"] = str(date.today())
        VERDICT_PATH.parent.mkdir(parents=True, exist_ok=True)
        VERDICT_PATH.write_text(json.dumps(allv, indent=1, default=str))
        print(f"saved {VERDICT_PATH}")
        return 0
    day = pd.Timestamp(a.day).date() if a.day else date.today()
    for tf in tfs:
        Rt = rules_for(tf, R)
        c = pa.tf_candles(tf, day, 5)
        print(f"\n===== {TF_HEAD[tf]} - {day:%a %d-%b-%Y} =====")
        if c is None or day not in set(pd.to_datetime(c["timestamp"]).dt.date):
            print(f"no {tf} candles for {day}")
            continue
        s = scan(c, Rt, only_day=day)
        if not len(s):
            print("no 8 EMA closes")
            continue
        for _, x in s.iterrows():
            line = f"{x['time']:%H:%M} {x['side']} close {'above' if x['side'] == 'CE' else 'below'} 8 EMA | {_tags(x, Rt)}"
            if x["triggered"]:
                line += (f"\n      -> TRIGGERED {x['trigger_time']:%H:%M} entry {x['entry']:,.0f} SL {x['sl']:,.0f} ({x['risk']:.0f}) | "
                         + " | ".join(f"{m} {x['exit_' + m]} {x['r_' + m] * x['risk']:+.0f}" for m in MODES))
            else:
                line += f"\n      -> {x['skip'] if isinstance(x['skip'], str) else 'no follow-through'}"
            print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
