"""
participant_correlation.py — the trainer's Lecture 2 "Data Correlation" rules, computed from
his participant workbook every evening, with a history check for each rule.

  .venv/bin/python participant_correlation.py            # today's readings + history table

Rules (thresholds in participant_correlation_rules.json):
  R1 Portfolio   FII!AE net stock futures: build > 2.2L (1/5 tranches), add per +1L,
                 book part < 1.25L, exit < 1L, re-enter > 2.2L; rising AE in a falling market
                 = they accumulate, add. (His numbers + a version scaled to today's AE levels.)
  R2 Y           FII!Y net options below -80k = bearish, above = bullish, 2-3 days in a row.
  R3 AM          FII!AM put short / call short >= 0.97 = buy on dip; < 0.95 two days = bearish.
  R4 All three   FII AM, PRO AB, Retail AI all > 1 two days running -> near a bottom.
  R5 Bias        R2 + R3 together -> "buy on dip" / "sell on rise" for the next session.
  R6 PRO Monday  PRO net CE < -2.6L on cycle day 2 -> short covering above cycle-day-1 high by expiry.
  R7 FII pre-expiry  +call long, -call short, put long added > put short -> expiry gap-up then fall.
  R8 Vol expiry  FII net CE & PE volume clearly negative on expiry day -> next-day momentum (straddle).
  R9 Vol Tue-Wed both negative and more negative on the 2 days before expiry -> expiry-day momentum.
  R10 Monthly expiry  after it, Y / AM jump because contracts roll off — don't read the jump as a signal.

Rules R6-R9 need Nifty open/high/low: from Dhan (your Data API; daily endpoint, else hourly
intraday bars aggregated per day), cached in data/ohlc/nifty_daily_dhan.csv and topped up each
run; yfinance only fills the months before Dhan's ~5-year intraday history.
"""
from __future__ import annotations

import json
import logging
import warnings
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", category=UserWarning, module="openpyxl")

import config

log = logging.getLogger("participant_correlation")
HERE = Path(__file__).resolve().parent
RULES_PATH = HERE / "participant_correlation_rules.json"
WORKBOOK = config.DATA_DIR / "participant_sheet" / "Partcipant_Data_Sheet_NEW.xlsm"


def rules() -> dict:
    return json.loads(RULES_PATH.read_text())


# ─────────────────────────────── data ────────────────────────────────────────
def _col(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n - 1


def _read(path: Path, sheet: str, cols: dict[str, str]) -> pd.DataFrame:
    df = pd.read_excel(path, sheet_name=sheet, header=None, skiprows=1,
                       usecols=[_col(c) for c in cols], engine="openpyxl")
    df.columns = [cols[c] for c in sorted(cols, key=_col)]
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    for c in df.columns:
        if c != "date":
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna(subset=["date"]).drop_duplicates("date").set_index("date").sort_index()


RAW = {"A": "date", "D": "fl", "F": "fs", "H": "sl", "J": "ss", "L": "cl", "N": "pl", "P": "cs", "R": "ps"}


def load_dataset(path: Path | str = WORKBOOK) -> pd.DataFrame:
    """One row per trading day, the columns his rules read."""
    path = Path(path)
    fii = _read(path, "FII", {**RAW, "AZ": "nifty"}).dropna(subset=["cs"])
    pro = _read(path, "PRO", RAW).dropna(subset=["cs"])
    ret = _read(path, "Retailers", RAW).dropna(subset=["cs"])
    vol = _read(path, "FII Vol", {"A": "date", "G": "vcl", "I": "vpl", "K": "vcs", "M": "vps"}).dropna(subset=["vcs"])
    d = pd.DataFrame(index=fii.index)
    d["nifty"] = fii["nifty"]
    d["AE"] = fii.sl - fii.ss
    d["Y"] = (fii.cl - fii.cs) - (fii.pl - fii.ps)
    d["AM"] = fii.ps / fii.cs
    d["pro_AB"] = (pro.ps / pro.cs).reindex(d.index)
    d["ret_AI"] = (ret.ps / ret.cs).reindex(d.index)
    d["pro_T"] = (pro.cl - pro.cs).reindex(d.index)
    for c in ("cl", "pl", "cs", "ps"):
        d[f"fii_d_{c}"] = fii[c].diff()
    d["vol_ce"] = (vol.vcl - vol.vcs).reindex(d.index)
    d["vol_pe"] = (vol.vpl - vol.vps).reindex(d.index)
    d = d[d["nifty"].notna()].copy()
    d["ret"] = d["nifty"].pct_change()
    d["r1"] = d["nifty"].shift(-1) / d["nifty"] - 1
    d["r5"] = d["nifty"].shift(-5) / d["nifty"] - 1
    return d


def _dhan_daily(start: date, end: date) -> pd.DataFrame:
    """Nifty daily OHLC from Dhan (your Data API subscription).
    1) dhan_client.daily() — /charts/historical;
    2) if Dhan rejects that for the index (DH-905, as your dhan_client notes), hourly bars from
       /charts/intraday in 88-day windows, filtered to 09:15-15:30 and aggregated per day
       (same rules as dhan_client.daily_from_intraday: stray after-hours bars dropped, days
       with < 4 hourly bars dropped, today dropped until the close)."""
    import time as _time
    import dhan_client as dc
    sid = str(getattr(config, "DHAN_INDEX_SECURITY_ID", "13"))
    try:
        df = dc.daily(sid, days_back=(date.today() - start).days + 3)
        df = df.rename(columns=str.lower).set_index("date")[["open", "high", "low", "close"]]
        df.index = pd.to_datetime(df.index).normalize()
        log.info("Dhan /charts/historical: %d days", len(df))
        return df[df.index >= pd.Timestamp(start)]
    except Exception as exc:
        log.info("Dhan daily endpoint not usable (%s) — building days from hourly intraday bars", str(exc)[:120])
    frames, failed, cur = [], 0, start
    while cur <= end:
        to = min(cur + timedelta(days=88), end)
        try:
            payload = dc._post("/charts/intraday", {
                "securityId": sid, "exchangeSegment": "IDX_I", "instrument": "INDEX",
                "interval": "60", "oi": False,
                "fromDate": f"{cur:%Y-%m-%d} 09:00:00", "toDate": f"{to:%Y-%m-%d} 16:00:00"})
            frames.append(dc._to_frame(payload))
        except Exception as exc:
            failed += 1
            if failed <= 2:
                log.info("Dhan intraday %s..%s not available: %s", cur, to, str(exc)[:100])
        cur = to + timedelta(days=1)
        _time.sleep(0.35)                                  # stay well inside Dhan's rate limit
    if not frames:
        return pd.DataFrame(columns=["open", "high", "low", "close"])
    df = pd.concat(frames, ignore_index=True).drop_duplicates("timestamp")
    ts = pd.to_datetime(df["timestamp"])
    df = df[(ts.dt.time >= config.MARKET_OPEN) & (ts.dt.time <= config.MARKET_CLOSE)].copy()
    df["day"] = pd.to_datetime(df["timestamp"]).dt.normalize()
    out = df.sort_values("timestamp").groupby("day").agg(
        open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last"),
        bars=("close", "count"))
    out = out[out["bars"] >= 4].drop(columns="bars")
    if datetime.now().time() < config.MARKET_CLOSE:
        out = out[out.index < pd.Timestamp(date.today())]
    log.info("Dhan hourly -> %d daily bars (%s to %s), %d windows unavailable", len(out),
             out.index.min().date() if len(out) else "-", out.index.max().date() if len(out) else "-", failed)
    return out


def _yf_daily(symbol: str) -> pd.DataFrame | None:
    try:
        import yfinance as yf
        df = yf.download(symbol, start="2019-11-01", interval="1d", auto_adjust=False,
                         progress=False, threads=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df.rename(columns=str.lower)[["open", "high", "low", "close"]].dropna()
        df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
        return df if len(df) else None
    except Exception as exc:
        log.warning("yfinance Nifty daily failed: %s", exc)
        return None


def load_ohlc(rules_: dict | None = None, refresh: bool = False) -> pd.DataFrame | None:
    """Daily Nifty OHLC (open, high, low, close) since Nov-2019, or None.
    Order: data/ohlc/OHLC.csv if it already covers the history -> Dhan (cached in
    data/ohlc/nifty_daily_dhan.csv, topped up incrementally) -> yfinance only for days Dhan
    does not have (Dhan keeps about 5 years of intraday history)."""
    r = (rules_ or rules())["paths"]
    p = HERE / r["ohlc_csv"]
    if p.exists():
        try:
            raw = pd.read_csv(p)
            cols = {c.lower().strip(): c for c in raw.columns}
            tcol = next((cols[k] for k in ("date", "datetime", "timestamp", "time") if k in cols), None)
            if tcol and all(k in cols for k in ("open", "high", "low", "close")):
                raw["_t"] = pd.to_datetime(raw[tcol], errors="coerce")
                raw = raw.dropna(subset=["_t"]).sort_values("_t")
                g = raw.groupby(raw["_t"].dt.normalize())
                daily = pd.DataFrame({"open": g[cols["open"]].first(), "high": g[cols["high"]].max(),
                                      "low": g[cols["low"]].min(), "close": g[cols["close"]].last()})
                if len(daily) > 250 and daily.index.min() < pd.Timestamp("2020-01-01"):
                    return daily
        except Exception as exc:
            log.warning("OHLC csv %s not usable: %s", p, exc)

    cache = HERE / r.get("ohlc_dhan_cache", "data/ohlc/nifty_daily_dhan.csv")
    have = pd.read_csv(cache, index_col=0, parse_dates=True) if cache.exists() else None
    fresh = cache.exists() and (datetime.now() - datetime.fromtimestamp(cache.stat().st_mtime)) < timedelta(hours=6)
    if have is not None and fresh and not refresh:
        return have
    start = date(2019, 11, 1) if have is None or have.empty else (have.index.max() - timedelta(days=5)).date()
    try:
        got = _dhan_daily(start, date.today())
    except Exception as exc:
        log.warning("Dhan OHLC fetch failed: %s", exc)
        got = pd.DataFrame(columns=["open", "high", "low", "close"])
    parts = [x for x in (have, got) if x is not None and len(x)]
    merged = pd.concat(parts) if parts else pd.DataFrame(columns=["open", "high", "low", "close"])
    merged = merged[~merged.index.duplicated(keep="last")].sort_index()
    if merged.empty or merged.index.min() > pd.Timestamp("2019-12-05"):
        yf_df = _yf_daily(r["yf_symbol"])                    # only the gap before Dhan's coverage
        if yf_df is not None:
            first = merged.index.min() if len(merged) else pd.Timestamp.max
            merged = pd.concat([yf_df[yf_df.index < first], merged]).sort_index()
    if len(merged) > 250:
        cache.parent.mkdir(parents=True, exist_ok=True)
        merged.to_csv(cache)
        return merged
    return have if have is not None and len(have) else (merged if len(merged) else None)


# ────────────────────────────── calendar ────────────────────────────────────
def _schedule_value(schedule: list, t: pd.Timestamp):
    val = schedule[0][1]
    for start, v in schedule:
        if t >= pd.Timestamp(start):
            val = v
    return val


def expiry_calendar(dates: pd.DatetimeIndex, rules_: dict) -> pd.DataFrame:
    """is_expiry, is_monthly_expiry, cycle_day (1 = first session after an expiry),
    days_to_expiry (0 on expiry) for each trading date in `dates`."""
    sched = rules_["expiry_calendar"]["weekday_schedule"]
    dates = pd.DatetimeIndex(sorted(set(dates)))
    exp = set()
    wk = pd.Series(dates, index=dates)
    for (_, _), grp in wk.groupby([dates.isocalendar().year, dates.isocalendar().week]):
        first = grp.iloc[0]
        target = first - timedelta(days=first.weekday()) + timedelta(days=_schedule_value(sched, first))
        cand = grp[grp <= target]
        if len(cand):
            exp.add(cand.iloc[-1])
    cal = pd.DataFrame(index=dates)
    cal["is_expiry"] = [t in exp for t in dates]
    ym = dates.to_period("M")
    last_in_month = {}
    for t in sorted(exp):
        last_in_month[t.to_period("M")] = t
    cal["is_monthly_expiry"] = [t == last_in_month.get(p) for t, p in zip(dates, ym)]
    cyc, k = [], 0
    for e in cal["is_expiry"]:
        k += 1
        cyc.append(k)
        if e:
            k = 0
    cal["cycle_day"] = cyc
    dte, n = [], None
    for e in cal["is_expiry"][::-1]:
        n = 0 if e else (None if n is None else n + 1)
        dte.append(n)
    cal["days_to_expiry"] = dte[::-1]
    return cal


def _streak(m: pd.Series, k: int) -> pd.Series:
    return m.astype(float).rolling(k).sum() == k


def _lot(t: pd.Timestamp, schedule: list) -> int:
    return int(_schedule_value(schedule, t))


# ─────────────────────────────── signals ────────────────────────────────────
def add_signals(d: pd.DataFrame, R: dict, ohlc: pd.DataFrame | None = None) -> pd.DataFrame:
    d = d.copy()
    future = pd.bdate_range(d.index[-1] + timedelta(days=1), periods=10)   # so the last days know their expiry
    cal = expiry_calendar(d.index.append(future), R).reindex(d.index)
    d = d.join(cal)

    # R1 portfolio
    P = R["portfolio_stock_futures"]
    d["AE_scale"] = (d["AE"].rolling(P["scaled_window_sessions"], min_periods=120).median()
                     / P["calibration_median_2021_22"])
    for tag, scale in (("", pd.Series(1.0, index=d.index)), ("_scaled", d["AE_scale"])):
        expo, state = _portfolio(d["AE"], scale, P)
        d["pf_expo" + tag] = expo
        d["pf_state" + tag] = state

    # R2 Y
    Yr = R["net_options_Y"]
    d["Y_line"] = float(Yr["line"])
    d["Y_line_lot"] = [Yr["line"] * Yr["line_lot_basis"] / _lot(t, Yr["nifty_lot_schedule"]) for t in d.index]
    d["Y_line_scaled"] = d["Y"].rolling(Yr["scaled_window_sessions"], min_periods=120).median()
    k = Yr["confirm_days"]
    for tag, line in (("", d["Y_line"]), ("_scaled", d["Y_line_scaled"])):
        d["Y_bull" + tag] = _streak(d["Y"] >= line, k)
        d["Y_bear" + tag] = _streak(d["Y"] < line, k)

    # R3 AM
    A = R["put_call_short_AM"]
    d["AM_bull"] = _streak(d["AM"] >= A["bull_line"], A["confirm_days"])
    d["AM_bear"] = _streak(d["AM"] < A["bear_line"], A["confirm_days"])

    # R4 all three
    T = R["all_three_above_1"]
    a3 = (d["AM"] > T["line"]) & (d["pro_AB"] > T["line"]) & (d["ret_AI"] > T["line"])
    d["all3"] = a3
    d["all3_confirmed"] = _streak(a3, T["confirm_days"])

    # R5 bias
    d["bias"] = np.select([d["AM_bull"] & d["Y_bull_scaled"], d["AM_bear"] & d["Y_bear_scaled"]],
                          ["BUY ON DIP", "SELL ON RISE"], "MIXED")

    # R6 PRO Monday
    M = R["pro_monday_short_covering"]
    d["pro_monday"] = (d["cycle_day"] == M["signal_cycle_day"]) & (d["pro_T"] < M["net_ce_line"]) \
        & d["days_to_expiry"].notna() & (d["days_to_expiry"] > 0)

    # R7 FII pre-expiry
    F = R["fii_pre_expiry_reversal"]
    d["fii_pre_expiry"] = (d["days_to_expiry"] == 1) & (d["fii_d_cl"] > F["min_call_long_add"]) \
        & (d["fii_d_cs"] < F["max_call_short_change"]) & (d["fii_d_pl"] > d["fii_d_ps"])

    # R8 / R9 volume
    V = R["fii_vol_momentum"]
    s = d["vol_ce"] + d["vol_pe"]
    d["vol_sum"] = s
    d["vol_sum_pctl_line"] = s.rolling(V["percentile_window_sessions"], min_periods=60).quantile(V["strong_percentile"] / 100)
    both_neg = (d["vol_ce"] < 0) & (d["vol_pe"] < 0)
    d["vol_expiry_signal"] = d["is_expiry"] & both_neg & (s <= d["vol_sum_pctl_line"])
    prev_neg = both_neg.shift(1, fill_value=False)
    d["vol_pre_expiry_signal"] = (d["days_to_expiry"] == 1) & both_neg & prev_neg & (s < s.shift(1))

    # outcomes needing OHLC
    if ohlc is not None and len(ohlc):
        o = ohlc.reindex(d.index)
        d["o_open"], d["o_high"], d["o_low"], d["o_close"] = o["open"], o["high"], o["low"], o["close"]
    return d


def _portfolio(ae: pd.Series, scale: pd.Series, P: dict) -> tuple[pd.Series, pd.Series]:
    expo, state = [], []
    e, last_add = 0.0, None
    for a, s in zip(ae.values, scale.values):
        if np.isnan(a) or np.isnan(s):
            expo.append(e)
            state.append("NO DATA" if e == 0 else state[-1] if state else "NO DATA")
            continue
        st = None
        if e == 0 and a > P["entry"] * s:
            e, last_add, st = P["tranche_fraction"], a, "BUILD (1st tranche)"
        elif e > 0:
            if a < P["exit"] * s:
                e, st = 0.0, "EXIT ALL"
            elif a < P["partial_book"] * s and e > P["tranche_fraction"]:
                e, st = max(P["tranche_fraction"], e / 2), "BOOK PART"
            elif a >= last_add + P["add_step"] * s and e < 1.0:
                e, last_add, st = min(1.0, e + P["tranche_fraction"]), a, "ADD TRANCHE"
        if st is None:
            st = "HOLD" if e > 0 else "STAY OUT"
        expo.append(e)
        state.append(st)
    return pd.Series(expo, index=ae.index), pd.Series(state, index=ae.index)


# ───────────────────────────── history check ────────────────────────────────
def _row(name: str, m: pd.Series, outcome: pd.Series, base: pd.Series, what: str, R: dict,
         also5: pd.Series | None = None) -> dict:
    ok = m.fillna(False).astype(bool) & outcome.notna()
    n = int(ok.sum())
    minimum = R["outcome_scoring"]["min_sample_size"]
    row = {"Rule": name, "Days": n, "Outcome measured": what}
    if n < minimum:
        row.update({"Hit %": "INSUFFICIENT SAMPLE", "All days %": "", "Edge (pts of %)": ""})
        return row
    hit = 100 * outcome[ok].astype(float).mean()
    b = 100 * base.dropna().astype(float).mean()
    row.update({"Hit %": round(hit, 1), "All days %": round(b, 1), "Edge (pts of %)": round(hit - b, 1)})
    if also5 is not None:
        row["Next 5 days up %"] = round(100 * (also5[ok] > 0).mean(), 1)
    return row


def history_table(d: pd.DataFrame, R: dict) -> pd.DataFrame:
    up1 = (d["r1"] > 0).where(d["r1"].notna())
    rows = [
        _row("R2 Y >= -80k (2 days) — bullish", d["Y_bull"], up1, up1, "Nifty up next day", R, d["r5"]),
        _row("R2 Y < -80k (2 days) — bearish", d["Y_bear"], up1, up1, "Nifty up next day", R, d["r5"]),
        _row("R2 Y above scaled line (2 days)", d["Y_bull_scaled"], up1, up1, "Nifty up next day", R, d["r5"]),
        _row("R2 Y below scaled line (2 days)", d["Y_bear_scaled"], up1, up1, "Nifty up next day", R, d["r5"]),
        _row("R3 AM >= 0.97 (2 days) — buy on dip", d["AM_bull"], up1, up1, "Nifty up next day", R, d["r5"]),
        _row("R3 AM < 0.95 (2 days) — bearish", d["AM_bear"], up1, up1, "Nifty up next day", R, d["r5"]),
        _row("R4 All three > 1, 2 days running", d["all3_confirmed"], up1, up1, "Nifty up next day", R, d["r5"]),
        _row("R5 Bias BUY ON DIP", d["bias"] == "BUY ON DIP", up1, up1, "Nifty up next day", R, d["r5"]),
        _row("R5 Bias SELL ON RISE", d["bias"] == "SELL ON RISE", up1, up1, "Nifty up next day", R, d["r5"]),
    ]
    inv = d["pf_expo"].shift(1) > 0
    r = d["ret"]
    for tag, label in (("", "his numbers"), ("_scaled", "scaled")):
        m = d["pf_expo" + tag].shift(1) > 0
        rows.append({"Rule": f"R1 Portfolio in-market days ({label})", "Days": int(m.sum()),
                     "Outcome measured": "avg Nifty day % in vs out",
                     "Hit %": round(100 * r[m].mean(), 3), "All days %": round(100 * r[~m & r.notna()].mean(), 3),
                     "Edge (pts of %)": round(100 * (r[m].mean() - r[~m & r.notna()].mean()), 3)})

    if "o_open" in d and d["o_open"].notna().sum() > 250:
        rows += _ohlc_rows(d, R)
    else:
        rows.append({"Rule": "R6-R9 (need Nifty open/high/low)", "Days": 0,
                     "Outcome measured": "OHLC not available yet", "Hit %": "Dhan OHLC not loaded — check the Dhan token"})
    out = pd.DataFrame(rows)
    for c in ("Hit %", "All days %", "Edge (pts of %)", "Next 5 days up %"):
        if c in out:
            out[c] = out[c].map(lambda v: "" if v is None or (isinstance(v, float) and np.isnan(v))
                                else v if isinstance(v, str) else f"{v:g}")
    return out


def _ohlc_rows(d: pd.DataFrame, R: dict) -> list[dict]:
    rows = []
    idx = list(d.index)
    pos = {t: i for i, t in enumerate(idx)}
    # R6: after the signal day, does the market trade above the cycle-day-1 high by expiry?
    def crosses_ref_high(t):
        i = pos[t]
        j0 = i - 1                                          # cycle day 1 (Friday then)
        if j0 < 0 or d["cycle_day"].iloc[j0] != 1:
            return np.nan
        ref = d["o_high"].iloc[j0]
        k = i + int(d["days_to_expiry"].iloc[i])
        if np.isnan(ref) or k >= len(idx):
            return np.nan
        return float(d["o_high"].iloc[i + 1:k + 1].max() > ref)
    cyc2 = d.index[(d["cycle_day"] == 2) & (d["days_to_expiry"] > 0)]
    base6 = pd.Series({t: crosses_ref_high(t) for t in cyc2})
    sig6 = pd.Series(True, index=d.index[d["pro_monday"]])
    rows.append(_row("R6 PRO net CE < -2.6L on cycle day 2", sig6.reindex(base6.index, fill_value=False),
                     base6, base6, "crosses cycle-day-1 high by expiry", R))
    # R7: expiry day opens above the previous close and trades below the previous day's low
    def gap_then_break(t):
        i = pos[t]
        if i + 1 >= len(idx) or not d["is_expiry"].iloc[i + 1]:
            return np.nan
        o, lo = d["o_open"].iloc[i + 1], d["o_low"].iloc[i + 1]
        pc, plo = d["o_close"].iloc[i], d["o_low"].iloc[i]
        if np.isnan(o) or np.isnan(plo):
            return np.nan
        return float(o > pc and lo < plo)
    pre = d.index[d["days_to_expiry"] == 1]
    base7 = pd.Series({t: gap_then_break(t) for t in pre})
    rows.append(_row("R7 FII pre-expiry pattern", d["fii_pre_expiry"].reindex(base7.index, fill_value=False),
                     base7, base7, "expiry: gap-up then below prev low", R))
    red = pd.Series({t: (float(d["o_close"].iloc[pos[t] + 1] < d["o_open"].iloc[pos[t] + 1])
                         if pos[t] + 1 < len(idx) else np.nan) for t in pre})
    rows.append(_row("R7 FII pre-expiry pattern", d["fii_pre_expiry"].reindex(red.index, fill_value=False),
                     red, red, "expiry day closes below its open", R))
    # R8 / R9: momentum = max move from open >= X% of open
    V = R["fii_vol_momentum"]
    mv = np.maximum(d["o_high"] - d["o_open"], d["o_open"] - d["o_low"]) / d["o_open"] * 100
    mom = (mv >= V["momentum_move_pct"]).where(mv.notna())
    nxt = mom.shift(-1)
    exp_days = d.index[d["is_expiry"]]
    rows.append(_row("R8 FII vol both negative on expiry day", d["vol_expiry_signal"].reindex(exp_days, fill_value=False),
                     nxt.reindex(exp_days), nxt.reindex(exp_days), f"next day moves >= {V['momentum_move_pct']}% from open", R))
    rows.append(_row("R9 FII vol negative & falling, 2 days before expiry", d["vol_pre_expiry_signal"].reindex(pre, fill_value=False),
                     nxt.reindex(pre), nxt.reindex(pre), f"expiry day moves >= {V['momentum_move_pct']}% from open", R))
    return rows


def portfolio_trades(d: pd.DataFrame, tag: str = "") -> pd.DataFrame:
    e = d["pf_expo" + tag]
    rows, start = [], None
    for t, prev, cur in zip(d.index, e.shift(1).fillna(0), e):
        if prev == 0 and cur > 0:
            start = t
        elif prev > 0 and cur == 0 and start is not None:
            rows.append({"Entry": f"{start:%d-%b-%Y}", "Exit": f"{t:%d-%b-%Y}", "Nifty in": d.at[start, "nifty"],
                         "Nifty out": d.at[t, "nifty"],
                         "Move %": round(100 * (d.at[t, "nifty"] / d.at[start, "nifty"] - 1), 1)})
            start = None
    if start is not None:
        rows.append({"Entry": f"{start:%d-%b-%Y}", "Exit": "still held", "Nifty in": d.at[start, "nifty"],
                     "Nifty out": d["nifty"].iloc[-1],
                     "Move %": round(100 * (d["nifty"].iloc[-1] / d.at[start, "nifty"] - 1), 1)})
    return pd.DataFrame(rows)


# ─────────────────────────────── today ──────────────────────────────────────
def today_cards(d: pd.DataFrame, R: dict) -> list[dict]:
    """Latest session's reading for each rule: rule, reading, state (BULL/BEAR/NEUTRAL/INFO), action."""
    x = d.iloc[-1]
    t = d.index[-1]
    P, Yr, A = R["portfolio_stock_futures"], R["net_options_Y"], R["put_call_short_AM"]
    s = x["AE_scale"] if not np.isnan(x["AE_scale"]) else 1.0
    cards = []

    def card(rule, reading, state, action):
        cards.append({"rule": rule, "reading": reading, "state": state, "action": action})

    ae5 = d["AE"].diff(5).iloc[-1]
    n5 = d["nifty"].pct_change(5).iloc[-1] * 100
    card("R1 Portfolio (FII stock futures, AE)",
         f"AE {x['AE']:,.0f} ({'+' if ae5 >= 0 else ''}{ae5:,.0f} in 5 days, Nifty {n5:+.1f}%). "
         f"His lines 2.2L / 1.25L / 1L; scaled to today {P['entry'] * s:,.0f} / {P['partial_book'] * s:,.0f} / {P['exit'] * s:,.0f}",
         "BULL" if x["pf_expo"] > 0 else "NEUTRAL",
         f"His numbers: {x['pf_state']} ({x['pf_expo'] * 5:.0f}/5 invested). Scaled: {x['pf_state_scaled']} "
         f"({x['pf_expo_scaled'] * 5:.0f}/5). "
         + ("AE rising while Nifty falls = they accumulate: add, don't fear. " if ae5 > 0 and n5 < -1 else "")
         + ("AE falling while Nifty rises = profit booking into the rally; top comes when AE is back near the exit line."
            if ae5 < 0 and n5 > 1 else ""))
    yb = "BEAR" if x["Y_bear_scaled"] else "BULL" if x["Y_bull_scaled"] else "NEUTRAL"
    card("R2 Net options (FII Y)",
         f"Y {x['Y']:,.0f}; his line -80,000 ({'below' if x['Y'] < Yr['line'] else 'above'}); "
         f"scaled line {x['Y_line_scaled']:,.0f} ({'below' if x['Y'] < x['Y_line_scaled'] else 'above'}); "
         f"lot-adjusted {x['Y_line_lot']:,.0f}",
         yb, "Believe it after 2-3 days on the same side. Below = bearish / sell on rise; above = bullish.")
    ams = "BULL" if x["AM_bull"] else "BEAR" if x["AM_bear"] else "NEUTRAL"
    last3 = ", ".join(f"{v:.2f}" for v in d["AM"].tail(3))
    card("R3 FII put short / call short (AM)", f"last 3 days {last3}; lines 0.97 bull / 0.95 bear", ams,
         "Leads Y. >= 0.97 two days = buy-on-dip market; < 0.95 two days = bearish. One day across doesn't count.")
    card("R4 All three > 1 (FII AM, PRO AB, Retail AI)",
         f"FII {x['AM']:.2f} | PRO {x['pro_AB']:.2f} | Retail {x['ret_AI']:.2f}",
         "BULL" if x["all3_confirmed"] else "NEUTRAL",
         "All three > 1 for 2 days = near a bottom, expect up. " + ("CONFIRMED today." if x["all3_confirmed"] else
                                                                    "Not met." if not x["all3"] else "Day 1 of 2."))
    card("R5 Next-session bias (AM + Y)", x["bias"],
         "BULL" if x["bias"] == "BUY ON DIP" else "BEAR" if x["bias"] == "SELL ON RISE" else "NEUTRAL",
         "Intraday: buy dips / sell rises at your levels; work level to level (price action decides the entry).")
    dte = x["days_to_expiry"]
    exp_note = f"next expiry in {int(dte)} session(s)" if pd.notna(dte) else "expiry unknown"
    card("R6 PRO Monday short covering", f"cycle day {int(x['cycle_day'])}, {exp_note}; PRO net CE {x['pro_T']:,.0f} (line -2.6L)",
         "BULL" if x["pro_monday"] else "INFO",
         "Signal active: expect a dip first, then a move above the cycle-day-1 high by expiry; target = that high."
         if x["pro_monday"] else "Checked on cycle day 2 (Monday in his Thursday-expiry weeks).")
    card("R7 FII pre-expiry reversal",
         f"Δcall long {x['fii_d_cl']:+,.0f}, Δcall short {x['fii_d_cs']:+,.0f}, Δput long {x['fii_d_pl']:+,.0f}, Δput short {x['fii_d_ps']:+,.0f}",
         "BEAR" if x["fii_pre_expiry"] else "INFO",
         "Signal active: expiry day may gap up then reverse; buy puts after the gap-up, target below today's low."
         if x["fii_pre_expiry"] else "Checked on the session before expiry.")
    card("R8/R9 FII volume momentum",
         f"net CE vol {x['vol_ce']:,.0f}, net PE vol {x['vol_pe']:,.0f} (strong line {x['vol_sum_pctl_line']:,.0f})",
         "INFO" if not (x["vol_expiry_signal"] or x["vol_pre_expiry_signal"]) else "BULL",
         ("Expiry-day writing: expect momentum tomorrow — long straddle after the first 15-min candle. "
          if x["vol_expiry_signal"] else "")
         + ("Writing built on both days before expiry: expect expiry-day momentum — long straddle. "
            if x["vol_pre_expiry_signal"] else "")
         + ("" if (x["vol_expiry_signal"] or x["vol_pre_expiry_signal"]) else "No volume setup today."))
    if x["is_monthly_expiry"] or (len(d) > 1 and d["is_monthly_expiry"].iloc[-2]):
        card("R10 Monthly expiry", f"{t:%d-%b} is/after monthly expiry", "INFO",
             "Futures and monthly options roll off: Y and AM can jump (e.g. -1.26L to -23k). Read the next 2 days, not the jump.")
    return cards


def summary_line(path: Path | str = WORKBOOK) -> str:
    R = rules()
    d = add_signals(load_dataset(path), R)
    x = d.iloc[-1]
    return (f"Correlation {d.index[-1]:%d-%b}: bias {x['bias']} | Y {x['Y']:,.0f} "
            f"({'below' if x['Y'] < x['Y_line_scaled'] else 'above'} scaled line) | AM {x['AM']:.2f} | "
            f"portfolio {x['pf_state']} {x['pf_expo'] * 5:.0f}/5 (scaled view: {x['pf_state_scaled']})"
            + (" | ALL THREE >1 CONFIRMED" if x["all3_confirmed"] else "")
            + (" | PRO short-covering setup" if x["pro_monday"] else "")
            + (" | FII pre-expiry reversal setup" if x["fii_pre_expiry"] else "")
            + (" | straddle setup (FII vol)" if (x["vol_expiry_signal"] or x["vol_pre_expiry_signal"]) else ""))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    R = rules()
    d = add_signals(load_dataset(), R, load_ohlc(R))
    print(summary_line())
    for c in today_cards(d, R):
        print(f"- [{c['state']}] {c['rule']}: {c['reading']}\n    -> {c['action']}")
    print(history_table(d, R).to_string(index=False))
    print(portfolio_trades(d, "_scaled").to_string(index=False))
