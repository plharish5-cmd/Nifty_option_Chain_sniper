"""
mentor_fib.py — rules from the "Market Structure + Fibo + Doubts" class (SBI Life / Reliance / Nifty 15-min).

  .venv/bin/python mentor_fib.py               # today's Fib targets and the latest imbalance
  .venv/bin/python mentor_fib.py --backtest    # history check vs shuffled bars

His rules (numbers in mentor_fib_rules.json):
  Fib targets      after a CLOSE above the high (BOS/CHoCH up) draw Fib from that leg's high to its low:
                   targets 1.2 and 1.4 above the high. After a CHoCH down, draw low -> high: 1.2 / 1.4 below.
                   Book most at the previous high, the rest at the Fib level.
  imbalance hold   latest 3-candle imbalance (1-2-3). A candle goes into it, may even poke below it, but CLOSES
                   inside/above it -> buy, SL = that candle's low, target = Fib 1.2 (or the recent high).
                   If it closes below -> no trade. Mirror for sells. Only the LATEST imbalance counts.
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd

import mentor_structure as ms
import mentor_sweeps as msw

log = logging.getLogger("mentor_fib")
HERE = Path(__file__).resolve().parent
RULES_PATH = HERE / "mentor_fib_rules.json"


def rules() -> dict:
    return json.loads(RULES_PATH.read_text())


def _breaks(c: pd.DataFrame) -> list[dict]:
    res = ms.analyse(c.reset_index(drop=True))
    return sorted((e for e in res.events if e["kind"] in ("BOS_UP", "BOS_DN", "CHOCH_UP", "CHOCH_DN")),
                  key=lambda e: e["idx"])


def fib_targets(c: pd.DataFrame, R: dict | None = None, tf: str = "15m") -> pd.DataFrame:
    """Every structure break with its leg, 1.2/1.4 levels and whether/when price reached them."""
    R = R or rules()
    F = R["fib"]
    O, H, L, C = msw._arr(c)
    n = len(c)
    look = F["leg_lookback_bars"].get(tf, 20)
    horizon = F["max_bars"].get(tf, 26)
    rows = []
    ev = _breaks(c)
    for k, e in enumerate(ev):
        i = e["idx"]
        start = max(ev[k - 1]["idx"] if k else 0, i - look)
        if i - start < 2:
            continue
        up = e["kind"].endswith("UP")
        lvl = e["price"]
        if up:
            base = float(L[start:i + 1].min())
            leg = lvl - base
        else:
            base = float(H[start:i + 1].max())
            leg = base - lvl
        if leg <= 0:
            continue
        row = {"tf": tf, "idx": i, "kind": e["kind"], "side": "bull" if up else "bear", "level": lvl, "base": base,
               "leg": leg, "close": float(C[i])}
        end = min(n - 1, i + horizon)
        for r in F["levels"]:
            t = lvl + (r - 1) * leg if up else lvl - (r - 1) * leg
            row[f"t{r}"] = t
            hit = next((j for j in range(i + 1, end + 1) if (H[j] >= t if up else L[j] <= t)), None)
            row[f"hit{r}"] = hit is not None
            row[f"hit{r}_idx"] = hit
        inv = next((j for j in range(i + 1, end + 1) if (L[j] <= base if up else H[j] >= base)), None)
        h1 = row[f"hit{F['levels'][0]}_idx"]
        row["base_first"] = inv is not None and (h1 is None or inv < h1)
        rows.append(row)
    return pd.DataFrame(rows)


def imbalance_hold(c: pd.DataFrame, R: dict | None = None, tf: str = "15m") -> pd.DataFrame:
    """Latest-imbalance trades: first candle back into it that closes back inside/beyond it."""
    R = R or rules()
    I = R["imbalance_hold"]
    buf = R["risk"]["sl_buffer_points"]
    O, H, L, C = msw._arr(c)
    n = len(c)
    gaps = [g for g in ms.imbalances(c) if g["top"] - g["bottom"] >= I["min_points"].get(tf, 1)]
    ts = c["timestamp"] if "timestamp" in c else None
    rows = []
    for gi, g in enumerate(gaps):
        f = g["idx"] + 2
        k = g["filled_idx"]
        if k is None or k - f > I["max_wait_bars"].get(tf, 26):
            continue
        # only the LATEST imbalance: a newer one formed before the touch replaces it
        if I["latest_only"] and any(f < gg["idx"] + 2 < k for gg in gaps[gi + 1:gi + 40]):
            continue
        bull = g["side"] == "bull"
        held = C[k] >= g["bottom"] if bull else C[k] <= g["top"]
        row = {"tf": tf, "side": g["side"], "formed_idx": f, "touch_idx": k, "bottom": g["bottom"], "top": g["top"],
               "held": bool(held), "entry": np.nan, "sl": np.nan, "target": np.nan, "result": "NO TRADE",
               "r": np.nan, "passes_rr": False}
        if held:
            entry = float(C[k])
            sl = float(L[k]) - buf if bull else float(H[k]) + buf
            ext = float(H[f:k + 1].max()) if bull else float(L[f:k + 1].min())
            target = ext
            if I["target"] == "fib1.2":
                base = float(L[max(0, f - 20):f + 1].min()) if bull else float(H[max(0, f - 20):f + 1].max())
                target = ext + 0.2 * (ext - base) if bull else ext - 0.2 * (base - ext)
            risk = abs(entry - sl)
            if risk > 0 and (target - entry if bull else entry - target) > 0:
                rr = abs(target - entry) / risk
                row.update(entry=entry, sl=sl, target=target, passes_rr=rr >= R["risk"]["min_rr"] and risk >= msw._min_sl(R["risk"], tf))
                mb = I["max_bars"].get(tf, 26)
                if tf == "15m" and I.get("intraday_only", True):
                    mb = min(mb, msw._session_end(ts, k) - k)
                res, _, r = msw._replay(H, L, C, "bull" if bull else "bear", k, entry, sl, target, mb)
                row.update(result=res, r=r)
        rows.append(row)
    return pd.DataFrame(rows)


def _fib_row(df: pd.DataFrame, lab: str, tf: str, R: dict) -> dict:
    lv = R["fib"]["levels"]
    n = len(df)
    ok = n >= R["history"]["min_sample_size"]
    return {"Fib targets": lab, "TF": tf, "Breaks": n,
            **{f"Reached {r} %": round(100 * df[f"hit{r}"].mean(), 1) if ok else "INSUFFICIENT SAMPLE" for r in lv},
            "Back to leg start first %": round(100 * df["base_first"].mean(), 1) if ok else ""}


def backtest(R: dict | None = None) -> dict:
    R = R or rules()
    minimum = R["history"]["min_sample_size"]
    frames = {}
    h15 = ms.load_history(ms.rules())
    if h15 is not None and len(h15) > 500:
        frames["15m"] = h15.reset_index(drop=True)
    try:
        import participant_correlation as pc
        dd = pc.load_ohlc()
        if dd is not None:
            frames["1d"] = msw.daily_frame(dd).reset_index(drop=True)
    except Exception as exc:
        log.warning("daily unavailable: %s", exc)
    if not frames:
        return {"error": "no history"}
    fib_rows, imb_rows, verd = [], [], {}
    for tf, c in frames.items():
        tr = msw.trend_series(c) if tf == "15m" else None
        for lab, cc in (("", c), ("CONTROL: ", msw._shuffle(c))):
            ft = fib_targets(cc, R, tf)
            if len(ft):
                for sub, x in (("all breaks", ft), ("  BOS (with trend)", ft[ft["kind"].str.startswith("BOS")]),
                               ("  CHoCH (reversal)", ft[ft["kind"].str.startswith("CHOCH")])):
                    fib_rows.append(_fib_row(x, lab + sub, tf, R))
            ih = imbalance_hold(cc, R, tf)
            if len(ih):
                t = ih[ih["passes_rr"] & ih["result"].ne("NO TRADE")]
                imb_rows.append(msw._stats(t, minimum, lab + "Imbalance hold -> trade", tf))
                imb_rows[-1]["Held (closed back) %"] = round(100 * ih["held"].mean(), 1)
                if not lab and tr is not None and len(t):
                    s = np.where(t["side"] == "bull", 1, -1)
                    w = t[tr[t["touch_idx"].astype(int).values] == s]
                    a = t[tr[t["touch_idx"].astype(int).values] == -s]
                    imb_rows.append(msw._stats(w, minimum, "  with the 15-min trend", tf))
                    imb_rows.append(msw._stats(a, minimum, "  against the 15-min trend", tf))
                if len(t) >= minimum:
                    verd.setdefault(tf, {})["CONTROL" if lab else "his"] = float(t["r"].mean())
                    if not lab:
                        verd[tf]["n"] = int(len(t))
    for tf, v in verd.items():
        if "his" in v and "CONTROL" in v:
            d = v["his"] - v["CONTROL"]
            v["verdict"] = "BEATS RANDOM" if d >= R["history"]["beats_by_r"] else (
                "WORSE THAN RANDOM" if d <= -R["history"]["beats_by_r"] else "NO EDGE")
            n_his = v.get("n", 0)
            if v["verdict"] == "BEATS RANDOM" and n_his < R["history"].get("min_trades_for_star", 200):
                v["verdict"] = "PROMISING, SMALL SAMPLE"
    return {"fib": pd.DataFrame(fib_rows), "imbalance": pd.DataFrame(imb_rows), "verdicts": verd,
            "span": {tf: (c["timestamp"].min(), c["timestamp"].max(), len(c)) for tf, c in frames.items()}}


def save_verdict(b: dict) -> None:
    p = HERE / "data" / "mentor_fib_verdict.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(b.get("verdicts", {}), indent=1))


def load_verdict(tf: str = "15m") -> str:
    p = HERE / "data" / "mentor_fib_verdict.json"
    try:
        return json.loads(p.read_text()).get(tf, {}).get("verdict", "not tested")
    except (OSError, ValueError):
        return "not tested"


def live(c: pd.DataFrame, R: dict | None = None, tf: str = "15m") -> dict:
    """Latest break's Fib targets + the latest imbalance and whether it is waiting / held."""
    R = R or rules()
    c = c.reset_index(drop=True)
    out = {}
    ft = fib_targets(c, R, tf)
    if len(ft):
        out["fib"] = ft.iloc[-1].to_dict()
    ih = imbalance_hold(c, R, tf)
    gaps = ms.imbalances(c)
    if gaps:
        g = gaps[-1]
        out["latest_imbalance"] = {**g, "status": "WAITING" if g["filled_idx"] is None else "TOUCHED"}
    if len(ih):
        out["last_hold"] = ih.iloc[-1].to_dict()
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backtest", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    pd.set_option("display.width", 220, "display.max_columns", 20, "display.max_colwidth", 50)
    if a.backtest:
        b = backtest()
        if "error" in b:
            print(b["error"])
            return 1
        for tf, (f, t, n) in b["span"].items():
            print(f"{tf}: {n:,} bars {f:%d-%b-%Y} .. {t:%d-%b-%Y}")
        print("\n== Fib 1.2 / 1.4 after a structure break ==")
        print(b["fib"].to_string(index=False))
        print("\n== Latest-imbalance hold ==")
        print(b["imbalance"].to_string(index=False))
        print("\nVerdicts:", json.dumps(b["verdicts"], indent=1))
        save_verdict(b)
        return 0
    c, src = ms.recent_candles(10)
    if c is None:
        print("no candles")
        return 1
    L = live(c)
    if "fib" in L:
        f = L["fib"]
        print(f"Last break {f['kind']} at {f['level']:,.0f} (leg from {f['base']:,.0f}): "
              + ", ".join(f"{r} -> {f[f't{r}']:,.0f}{' (hit)' if f[f'hit{r}'] else ''}" for r in rules()["fib"]["levels"]))
    if "latest_imbalance" in L:
        g = L["latest_imbalance"]
        print(f"Latest imbalance {g['side']} {g['bottom']:,.0f}-{g['top']:,.0f}: {g['status']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
