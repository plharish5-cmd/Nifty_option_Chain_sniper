"""
nifty_orderflow.py — NIFTY 50 order-flow engine (real data only).

Input: the files written by orderflow_recorder.py (data/orderflow/ticks|index|book/YYYY-MM-DD.csv).
Every number here comes from classified trades in the 50 NIFTY stocks, mapped onto the NIFTY 50 index
price at the moment they traded. There is NO synthetic or random fallback: no recording → no output.

What it computes (per 1/3/5/15-min candle of the NIFTY 50 index)
  buy / sell / delta (₹ crore), delta % of the candle's value, cumulative delta (CVD), breadth
  (how many of the 50 stocks were net buyers), footprint (buy × sell at each index price bucket),
  candle POC + value area, diagonal imbalances, stacked-imbalance zones, absorption, candle delta
  divergence, CVD divergence, unfinished auctions, and the 4-rule price/delta state.
Session: volume profile (POC / VAH / VAL in ₹), flow by stock, resting-book pressure.

Each signal type is labelled VALIDATED / FAILED / UNTESTED from data/backtests/orderflow.json
(written by backtest_orderflow.py). Information only — not financial advice.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent
OF_DIR = ROOT / "data" / "orderflow"
RULES_PATH = ROOT / "orderflow_rules.json"
VALIDATION_PATH = ROOT / "data" / "backtests" / "orderflow.json"
_DEF = {"bucket_pts": 5, "value_area_pct": 0.70, "imbalance_ratio": 3.0, "imbalance_min_share_pct": 4.0,
        "stack_min": 3, "divergence_min_delta_pct": 8.0, "absorption_min_delta_pct": 15.0,
        "absorption_max_range_mult": 0.6, "unfinished_min_side_pct": 10.0, "cvd_pivot_lookback": 6,
        "trend_lookback_candles": 6, "trend_min_delta_pct": 5.0, "big_delta_quantile": 0.85,
        "confirm_lookback_candles": 3}
OPEN, CLOSE = dtime(9, 15), dtime(15, 30)

SIGNALS = {   # key: (title, side-neutral meaning shown in the tab)
    "stacked_buy": ("Stacked BUY imbalances", "3+ price levels in a row where buyers lifted ≥3× what sellers hit one level lower — aggressive buying; the zone often acts as support."),
    "stacked_sell": ("Stacked SELL imbalances", "3+ levels in a row where sellers hit ≥3× what buyers lifted one level higher — aggressive selling; the zone often acts as resistance."),
    "absorb_bull": ("Bullish absorption", "Heavy aggressive SELLING but price did not fall — passive buyers soaked it up."),
    "absorb_bear": ("Bearish absorption", "Heavy aggressive BUYING but price did not rise — passive sellers soaked it up."),
    "div_bull": ("Bullish delta divergence", "Red candle but buyers were the aggressors (positive delta)."),
    "div_bear": ("Bearish delta divergence", "Green candle but sellers were the aggressors (negative delta)."),
    "cvd_div_bull": ("Bullish CVD divergence", "Price made a lower low but cumulative delta made a higher low — selling is drying up."),
    "cvd_div_bear": ("Bearish CVD divergence", "Price made a higher high but cumulative delta made a lower high — buying is drying up."),
}
BULLISH = {"stacked_buy", "absorb_bull", "div_bull", "cvd_div_bull"}
# Unfinished auctions are shown per candle in the footprint table only. With 50 stocks pooled onto one
# index price, both sides almost always trade at the extremes, so as a signal it fires on nearly every
# candle — noise, so it is deliberately NOT in the signal list.


def rules() -> dict:
    try:
        r = json.loads(RULES_PATH.read_text())
    except Exception:
        r = {}
    e = {k: v for k, v in (r.get("engine") or {}).items() if not k.startswith("_")}
    return {**_DEF, **e}


def validation() -> dict:
    try:
        return json.loads(VALIDATION_PATH.read_text())
    except Exception:
        return {}


def status_of(kind: str, val: dict | None = None) -> str:
    v = (val if val is not None else validation()).get("signals", {}).get(kind)
    if not v:
        return "UNTESTED"
    if v.get("passed"):
        return "VALIDATED"
    return f"TOO FEW (n={v.get('n', 0)})" if v.get("n", 0) < 30 else "FAILED"


# ═══════════════════════════════════════════════════════════════════════════
# Loading
# ═══════════════════════════════════════════════════════════════════════════
def recorded_days() -> list[str]:
    d = OF_DIR / "ticks"
    return sorted(f.stem for f in d.glob("20*.csv")) if d.exists() else []


def _to_local(ts: pd.Series) -> pd.Series:
    return pd.to_datetime(ts, unit="s", utc=True).dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)


def _session(df: pd.DataFrame, col: str = "t") -> pd.DataFrame:
    tt = df[col].dt.time
    return df[(tt >= OPEN) & (tt < CLOSE)]


def symbol_map() -> dict[str, str]:
    try:
        return {str(k): v for k, v in json.loads((OF_DIR / "constituents.json").read_text())["map"].items()}
    except Exception:
        return {}


def load_day(day: str) -> dict:
    """Returns {'ticks','index','book'} DataFrames (empty if missing)."""
    out = {}
    f = OF_DIR / "ticks" / f"{day}.csv"
    if f.exists():
        t = pd.read_csv(f, dtype={"sid": str})
        t = t[(t["qty"] > 0) & (t["idx"] > 0)]
        t["t"] = _to_local(t["ts"])
        t = _session(t)
        t["val"] = t["ltp"] * t["qty"] / 1e7                       # ₹ crore
        t["sym"] = t["sid"].map(symbol_map()).fillna(t["sid"])
        out["ticks"] = t.sort_values("ts").reset_index(drop=True)
    else:
        out["ticks"] = pd.DataFrame()
    f = OF_DIR / "index" / f"{day}.csv"
    if f.exists():
        x = pd.read_csv(f)
        x["t"] = _to_local(x["ts"])
        out["index"] = _session(x[x["ltp"] > 0]).sort_values("ts").reset_index(drop=True)
    else:
        out["index"] = pd.DataFrame()
    f = OF_DIR / "book" / f"{day}.csv"
    if f.exists():
        b = pd.read_csv(f, dtype={"sid": str})
        b["t"] = _to_local(b["ts"])
        out["book"] = b
    else:
        out["book"] = pd.DataFrame()
    return out


def heartbeat() -> dict:
    try:
        hb = json.loads((OF_DIR / "recorder_heartbeat.json").read_text())
        hb["age_s"] = (datetime.now() - datetime.fromisoformat(hb["updated"])).total_seconds()
        return hb
    except Exception:
        return {}


# ═══════════════════════════════════════════════════════════════════════════
# Core computation
# ═══════════════════════════════════════════════════════════════════════════
@dataclass
class Flow:
    day: str
    minutes: int
    candles: pd.DataFrame
    footprint: dict                      # candle index → DataFrame(level, buy, sell, delta, total, buy_imb, sell_imb)
    zones: list                          # dict(kind, lo, hi, t, active)
    events: pd.DataFrame                 # t, i, kind, price, detail
    profile: pd.DataFrame                # level, buy, sell, total
    poc: float | None
    vah: float | None
    val: float | None
    stocks: pd.DataFrame
    book: dict
    state: dict
    totals: dict
    notes: list = field(default_factory=list)


def _index_ohlc(data: dict, minutes: int) -> pd.DataFrame:
    x = data.get("index")
    src = x[["t", "ltp"]] if x is not None and len(x) else data["ticks"][["t", "idx"]].rename(columns={"idx": "ltp"})
    o = (src.set_index("t")["ltp"].resample(f"{minutes}min", origin="start_day", offset="15min")
         .ohlc().dropna().reset_index())
    return o.rename(columns={"open": "o", "high": "h", "low": "l", "close": "c"})


def _value_area(levels: np.ndarray, total: np.ndarray, pct: float) -> tuple:
    if len(levels) == 0 or total.sum() <= 0:
        return None, None, None
    k = int(np.argmax(total))
    lo = hi = k
    acc, goal = total[k], pct * total.sum()
    while acc < goal and (lo > 0 or hi < len(total) - 1):
        up = total[hi + 1] if hi < len(total) - 1 else -1
        dn = total[lo - 1] if lo > 0 else -1
        if up >= dn:
            hi += 1
            acc += up
        else:
            lo -= 1
            acc += dn
    return float(levels[k]), float(levels[hi]), float(levels[lo])


def _imbalances(fp: pd.DataFrame, ratio: float, min_val: float) -> pd.DataFrame:
    """Diagonal: BUY at level L vs SELL at the level below; SELL at L vs BUY at the level above."""
    fp = fp.sort_values("level").reset_index(drop=True)
    b, s = fp["buy"].to_numpy(), fp["sell"].to_numpy()
    s_below = np.r_[np.nan, s[:-1]]              # no level below the low / above the high → no comparison
    b_above = np.r_[b[1:], np.nan]
    with np.errstate(invalid="ignore"):
        fp["buy_imb"] = (b >= min_val) & (b >= ratio * s_below)
        fp["sell_imb"] = (s >= min_val) & (s >= ratio * b_above)
    return fp


def _stacks(fp: pd.DataFrame, col: str, n: int, bucket: float) -> list[tuple[float, float]]:
    out, run = [], []
    for lv, f in zip(fp["level"], fp[col]):
        if f and (not run or abs(lv - run[-1] - bucket) < 1e-6):
            run.append(lv)
        else:
            if len(run) >= n:
                out.append((run[0], run[-1] + bucket))
            run = [lv] if f else []
    if len(run) >= n:
        out.append((run[0], run[-1] + bucket))
    return out


def compute(data: dict, minutes: int = 5, R: dict | None = None, day: str = "") -> Flow | None:
    R = R or rules()
    t = data.get("ticks")
    if t is None or t.empty:
        return None
    B = float(R["bucket_pts"])
    t = t.copy()
    t["bin"] = t["t"].dt.floor("min") if minutes == 1 else \
        (t["t"] - pd.Timedelta(minutes=15)).dt.floor(f"{minutes}min") + pd.Timedelta(minutes=15)
    t["level"] = np.floor(t["idx"] / B) * B
    t["buy"] = np.where(t["side"] > 0, t["val"], 0.0)
    t["sell"] = np.where(t["side"] < 0, t["val"], 0.0)
    t["net"] = t["buy"] - t["sell"]

    ohlc = _index_ohlc(data, minutes)
    agg = t.groupby("bin").agg(buy=("buy", "sum"), sell=("sell", "sum"), vol=("val", "sum")).reset_index()
    c = ohlc.merge(agg, left_on="t", right_on="bin", how="inner").drop(columns="bin")
    if c.empty:
        return None
    c["delta"] = c["buy"] - c["sell"]
    c["delta_pct"] = np.where(c["vol"] > 0, 100 * c["delta"] / c["vol"], 0.0)
    c["cvd"] = c["delta"].cumsum()
    per_stock = t.groupby(["bin", "sym"])["net"].sum()
    br = per_stock.groupby(level=0).agg(lambda s: int((s > 0).sum())).rename("breadth_up")
    bn = per_stock.groupby(level=0).agg(lambda s: int((s < 0).sum())).rename("breadth_dn")
    c = c.merge(br, left_on="t", right_index=True, how="left").merge(bn, left_on="t", right_index=True, how="left")
    c[["breadth_up", "breadth_dn"]] = c[["breadth_up", "breadth_dn"]].fillna(0).astype(int)
    c["range"] = c["h"] - c["l"]

    # ── footprint per candle ────────────────────────────────────────────
    fps, zones, ev = {}, [], []
    g_all = t.groupby(["bin", "level"]).agg(buy=("buy", "sum"), sell=("sell", "sum")).reset_index()
    ratio = float(R["imbalance_ratio"])
    for i, row in c.iterrows():
        fp = g_all[g_all["bin"] == row["t"]][["level", "buy", "sell"]].copy()
        if fp.empty:
            continue
        fp["delta"] = fp["buy"] - fp["sell"]
        fp["total"] = fp["buy"] + fp["sell"]
        fp = _imbalances(fp, ratio, R["imbalance_min_share_pct"] / 100 * max(row["vol"], 1e-9))
        fps[i] = fp
        poc, vah, val = _value_area(fp["level"].to_numpy(), fp["total"].to_numpy(), R["value_area_pct"])
        c.loc[i, ["poc", "vah", "val"]] = poc, vah, val
        c.loc[i, "n_buy_imb"], c.loc[i, "n_sell_imb"] = int(fp["buy_imb"].sum()), int(fp["sell_imb"].sum())
        for kind, col in (("stacked_buy", "buy_imb"), ("stacked_sell", "sell_imb")):
            for lo, hi in _stacks(fp, col, int(R["stack_min"]), B):
                zones.append(dict(kind=kind, lo=lo, hi=hi, t=row["t"], i=i, active=True))
                ev.append(dict(t=row["t"], i=i, kind=kind, price=(lo + hi) / 2,
                               detail=f"{lo:,.0f}–{hi:,.0f} · {int(fp[col].sum())} imbalances in candle"))
        # auction at the extremes: finished = the weaker side < N% of the stronger (footprint info only)
        top, bot = fp.loc[fp["level"].idxmax()], fp.loc[fp["level"].idxmin()]
        mpct = R["unfinished_min_side_pct"] / 100
        c.loc[i, "high_auction"] = "unfinished" if min(top["buy"], top["sell"]) >= mpct * max(top["buy"], top["sell"], 1e-9) else "finished"
        c.loc[i, "low_auction"] = "unfinished" if min(bot["buy"], bot["sell"]) >= mpct * max(bot["buy"], bot["sell"], 1e-9) else "finished"

    # zone invalidation: a later candle CLOSES through the far side
    for z in zones:
        later = c[c.index > z["i"]]
        if z["kind"] == "stacked_buy":
            br_ = later[later["c"] < z["lo"]]
        else:
            br_ = later[later["c"] > z["hi"]]
        if len(br_):
            z["active"], z["broken_t"] = False, br_["t"].iloc[0]

    # ── candle-level signals ────────────────────────────────────────────
    med_rng = c["range"].expanding().median().shift(1).fillna(c["range"].median())
    for i, row in c.iterrows():
        dp = row["delta_pct"]
        small = row["range"] <= R["absorption_max_range_mult"] * med_rng.loc[i]
        if dp <= -R["absorption_min_delta_pct"] and small and row["c"] >= row["o"] - 0.25 * max(row["range"], 1e-9):
            ev.append(dict(t=row["t"], i=i, kind="absorb_bull", price=row["l"],
                           detail=f"delta {dp:+.0f}% (₹{row['delta']:+,.0f} Cr) yet price held"))
        elif dp >= R["absorption_min_delta_pct"] and small and row["c"] <= row["o"] + 0.25 * max(row["range"], 1e-9):
            ev.append(dict(t=row["t"], i=i, kind="absorb_bear", price=row["h"],
                           detail=f"delta {dp:+.0f}% (₹{row['delta']:+,.0f} Cr) yet price stalled"))
        elif row["c"] > row["o"] and dp <= -R["divergence_min_delta_pct"]:
            ev.append(dict(t=row["t"], i=i, kind="div_bear", price=row["h"], detail=f"green candle, delta {dp:+.0f}%"))
        elif row["c"] < row["o"] and dp >= R["divergence_min_delta_pct"]:
            ev.append(dict(t=row["t"], i=i, kind="div_bull", price=row["l"], detail=f"red candle, delta {dp:+.0f}%"))

    # CVD divergence vs the previous swing in the lookback window
    n = int(R["cvd_pivot_lookback"])
    H, L, CV = c["h"].to_numpy(), c["l"].to_numpy(), c["cvd"].to_numpy()
    eps = 0.1 * float(c["range"].median())
    gap = 0.5 * float(c["delta"].abs().median())          # CVD must disagree by a meaningful amount
    last_hi_sig = last_lo_sig = -10
    for i in range(n, len(c)):
        w = slice(i - n, i)
        j_hi = int(np.argmax(H[w])) + i - n
        j_lo = int(np.argmin(L[w])) + i - n
        if H[i] > H[j_hi] + eps and CV[i] < CV[j_hi] - gap and i - last_hi_sig > n // 2:
            ev.append(dict(t=c["t"].iloc[i], i=i, kind="cvd_div_bear", price=H[i],
                           detail=f"high {H[i]:,.0f} > {H[j_hi]:,.0f} but CVD ₹{CV[i]:+,.0f} < ₹{CV[j_hi]:+,.0f} Cr"))
            last_hi_sig = i
        if L[i] < L[j_lo] - eps and CV[i] > CV[j_lo] + gap and i - last_lo_sig > n // 2:
            ev.append(dict(t=c["t"].iloc[i], i=i, kind="cvd_div_bull", price=L[i],
                           detail=f"low {L[i]:,.0f} < {L[j_lo]:,.0f} but CVD ₹{CV[i]:+,.0f} > ₹{CV[j_lo]:+,.0f} Cr"))
            last_lo_sig = i
    events = pd.DataFrame(ev, columns=["t", "i", "kind", "price", "detail"]).sort_values(["t", "kind"]).reset_index(drop=True)

    # ── session profile ─────────────────────────────────────────────────
    prof = t.groupby("level").agg(buy=("buy", "sum"), sell=("sell", "sum")).reset_index().sort_values("level")
    prof["total"] = prof["buy"] + prof["sell"]
    poc, vah, val = _value_area(prof["level"].to_numpy(), prof["total"].to_numpy(), R["value_area_pct"])

    # ── stocks ──────────────────────────────────────────────────────────
    last15 = t[t["t"] >= t["t"].max() - pd.Timedelta(minutes=15)]
    sg = t.groupby("sym").agg(buy=("buy", "sum"), sell=("sell", "sum"), vol=("val", "sum"),
                              first=("ltp", "first"), last=("ltp", "last"))
    sg["delta"] = sg["buy"] - sg["sell"]
    sg["buy_pct"] = 100 * sg["buy"] / (sg["buy"] + sg["sell"]).replace(0, np.nan)
    sg["chg_pct"] = 100 * (sg["last"] / sg["first"] - 1)
    sg["delta_15m"] = last15.groupby("sym")["net"].sum()
    stocks = sg.fillna({"delta_15m": 0.0}).sort_values("delta", ascending=False).reset_index()

    # ── resting book (pending orders) ───────────────────────────────────
    book = {}
    b = data.get("book")
    if b is not None and len(b):
        lb = b[b["ts"] == b["ts"].max()]
        bq, sq = float((lb["tbq"] * lb["ltp"]).sum() / 1e7), float((lb["tsq"] * lb["ltp"]).sum() / 1e7)
        book = dict(bid_cr=bq, ask_cr=sq, ratio=bq / sq if sq > 0 else None, t=lb["t"].iloc[0])

    # ── 4-rule state on the last k candles ──────────────────────────────
    k = min(int(R["trend_lookback_candles"]), len(c) - 1)
    state = dict(label="NOT ENOUGH CANDLES", code=0, dp=0.0, dcvd=0.0)
    if k >= 2:
        w = c.iloc[-k - 1:]
        dp_ = float(w["c"].iloc[-1] - w["c"].iloc[0])
        dcv = float(w["cvd"].iloc[-1] - w["cvd"].iloc[0])
        strong = abs(dcv) >= R["trend_min_delta_pct"] / 100 * float(w["vol"].iloc[1:].sum())
        atr = float(w["range"].mean())
        moved = abs(dp_) >= 0.5 * atr
        if not strong or not moved:
            lab, code = "BALANCED — no clear aggressor", 0
        elif dp_ > 0 and dcv > 0:
            lab, code = "STRONG BULLISH — price ↑ with delta ↑", 2
        elif dp_ < 0 and dcv < 0:
            lab, code = "STRONG BEARISH — price ↓ with delta ↓", -2
        elif dp_ > 0 > dcv:
            lab, code = "BEARISH DIVERGENCE — price ↑ but delta ↓ (rally on passive buying / absorption)", -1
        else:
            lab, code = "BULLISH DIVERGENCE — price ↓ but delta ↑ (sellers being absorbed)", 1
        state = dict(label=lab, code=code, dp=dp_, dcvd=dcv, k=k)

    totals = dict(buy=float(c["buy"].sum()), sell=float(c["sell"].sum()), vol=float(c["vol"].sum()),
                  cvd=float(c["cvd"].iloc[-1]), stocks=int(t["sym"].nunique()),
                  last_t=t["t"].max(), first_t=t["t"].min(), trades=int(len(t)))
    return Flow(day=day, minutes=minutes, candles=c, footprint=fps, zones=zones, events=events, profile=prof,
                poc=poc, vah=vah, val=val, stocks=stocks, book=book, state=state, totals=totals)


# ═══════════════════════════════════════════════════════════════════════════
# Reads used by other tabs
# ═══════════════════════════════════════════════════════════════════════════
def confirm(f: Flow | None, direction: int, R: dict | None = None) -> tuple[str, str]:
    """direction +1 = long idea, −1 = short idea → ('CONFIRMED'|'AGAINST'|'NEUTRAL'|'NO DATA', reason)."""
    if f is None or f.candles.empty:
        return "NO DATA", "order-flow recorder has no data for today"
    R = R or rules()
    n = int(R["confirm_lookback_candles"])
    last = f.candles.tail(n)
    recent = f.events[f.events["i"] >= last.index.min()] if len(f.events) else f.events
    good = {"stacked_buy", "absorb_bull", "div_bull", "cvd_div_bull"} if direction > 0 else \
        {"stacked_sell", "absorb_bear", "div_bear", "cvd_div_bear"}
    bad = {"stacked_sell", "absorb_bear", "div_bear", "cvd_div_bear"} if direction > 0 else \
        {"stacked_buy", "absorb_bull", "div_bull", "cvd_div_bull"}
    d = float(last["delta"].sum())
    meaningful = abs(d) >= R["trend_min_delta_pct"] / 100 * max(float(last["vol"].sum()), 1e-9)
    g = recent[recent["kind"].isin(good)] if len(recent) else recent
    b = recent[recent["kind"].isin(bad)] if len(recent) else recent
    sd = (1 if d > 0 else -1) if meaningful else 0
    if len(g) and sd == direction and len(b) == 0:
        return "CONFIRMED", f"last {n} candles: delta ₹{d:+,.0f} Cr · " + ", ".join(SIGNALS[k][0] for k in g["kind"].unique())
    if (len(b) and sd != direction) or f.state.get("code", 0) * direction <= -2:
        why = ", ".join(SIGNALS[k][0] for k in b["kind"].unique()) if len(b) else f.state["label"]
        return "AGAINST", f"last {n} candles: delta ₹{d:+,.0f} Cr · {why}"
    return "NEUTRAL", f"last {n} candles: delta ₹{d:+,.0f} Cr · no decisive footprint"


def today_flow(minutes: int = 5) -> Flow | None:
    days = recorded_days()
    if not days:
        return None
    day = days[-1]
    if day != date.today().isoformat():
        return None
    return compute(load_day(day), minutes, day=day)
