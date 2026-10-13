"""
ts_chain_pro.py — "⏱ Time-series Chain" sub-tab of 🧪 Options Lab (StockMojo's "Timewise OI & Volume" grid, ditto,
plus a pro read underneath).

  Grid     strikes down the middle · CALL columns on the left (oldest → newest, newest next to the strike) ·
           PUT columns on the right (newest next to the strike → oldest) — same layout as StockMojo.
           Each column = one time bucket (1m / 3m / 5m / 15m / 1h, buckets start 09:15). The LAST column is the bucket
           still in progress (it keeps changing until the bucket closes).
  Values   OI Change = OI added (+) / cut (−) during that bucket · OI = open interest at the end of the bucket ·
           Price = premium at the end of the bucket · Volume = contracts traded during that bucket.
           Shares, Indian K / L format (like StockMojo). Light green / red = up / down; DARK green / red = the biggest
           build / cut in that column.
  Settings expiry · timeframe · how many columns (Timestamp) · Range (± ATM) or Custom strikes · end time (replay).
  Below    ★ PRO READ: who is writing where, rolls (writers moving their line), support / resistance shifting,
           acceleration in the newest buckets, breakout / breakdown watch, and a one-line read for EVERY column.
Information only — not financial advice. Reads are heuristics, not backtested in this app.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

import options_lab_tab as ol
from options_lab_tab import NAVY, GOLD, WHITE, MUTED, GREEN, RED

TF = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "1h": 60}
MODES = ["OI Change", "OI", "Price", "Volume"]


# ═══════════════════════════════════════════════════════════════════════════
# 1 · Data
# ═══════════════════════════════════════════════════════════════════════════
def expiries(snaps: list) -> list[str]:
    seen = {}
    for _, _, df in snaps[-3:] + snaps[:3]:
        if df is not None and "expiry" in df:
            for e in df["expiry"].dropna().unique():
                s = str(e)
                seen[s] = pd.to_datetime(s, errors="coerce", dayfirst=not s[:4].isdigit())
    return [k for k, v in sorted(seen.items(), key=lambda kv: (pd.isna(kv[1]), kv[1]))]


def long_frame(snaps: list, expiry: str) -> pd.DataFrame:
    rows = []
    for ts, spot, df in snaps:
        if df is None or df.empty:
            continue
        d = df[df["expiry"].astype(str) == str(expiry)] if "expiry" in df else df
        if d.empty:
            continue
        d = d.drop_duplicates("strike")
        g = lambda c: pd.to_numeric(d[c], errors="coerce") if c in d else np.nan
        rows.append(pd.DataFrame({"ts": pd.Timestamp(ts), "spot": float(spot),
                                  "strike": pd.to_numeric(d["strike"], errors="coerce"),
                                  "ce_oi": g("ce_oi"), "pe_oi": g("pe_oi"), "ce_ltp": g("ce_ltp"), "pe_ltp": g("pe_ltp"),
                                  "ce_vol": g("ce_vol"), "pe_vol": g("pe_vol"),
                                  "ce_dchg": g("ce_chg_oi"), "pe_dchg": g("pe_chg_oi"),     # OI change vs previous close
                                  "ce_pchg": g("ce_chg"), "pe_pchg": g("pe_chg")}))         # price change vs previous close
    if not rows:
        return pd.DataFrame()
    x = pd.concat(rows, ignore_index=True).dropna(subset=["strike"])
    x["strike"] = x["strike"].round().astype(int)
    return x


def lot_size() -> float:
    try:
        import config
        return float(getattr(config, "LOT_SIZE", 0) or getattr(config, "NIFTY_LOT_SIZE", 0) or 65)
    except Exception:                                           # noqa: BLE001
        return 65.0


def share_mult(x: pd.DataFrame) -> tuple[float, str]:
    r = ol._rules()
    m = float(r.get("oi_multiplier", 1) or 1)
    try:
        import config
        lot = float(getattr(config, "LOT_SIZE", 0) or getattr(config, "NIFTY_LOT_SIZE", 0) or 65)
    except Exception:                                           # noqa: BLE001
        lot = 65.0
    last = x[x.ts == x.ts.max()]
    tot = float(last.ce_oi.sum() + last.pe_oi.sum()) * m
    if m == 1 and 0 < tot < 5e6:
        return lot, f"OI in lots → ×{lot:.0f} = shares"
    return m, "OI in shares" if m == 1 else f"OI ×{m:g}"


def buckets(x: pd.DataFrame, minutes: int, end: pd.Timestamp | None = None) -> pd.DataFrame:
    """Last snapshot in every bucket. Buckets are (end − N min, end], ends on the 09:15 grid — the StockMojo way:
    the 11:48 column (3m) = what changed from the 11:45 snapshot to the 11:48 snapshot. Column 'b' = bucket END."""
    if end is not None:
        x = x[x.ts <= end]
    d0 = x.ts.min().normalize() + pd.Timedelta(hours=9, minutes=15)
    tm = x.ts.dt.floor("min")                                   # snapshots land at hh:mm:05 — use the minute
    step = pd.Timedelta(minutes=minutes)
    k = np.ceil(((tm - d0) / step).astype(float)).clip(lower=0)
    x = x.assign(b=d0 + k.astype(int) * step)
    last_ts = x.groupby("b")["ts"].max().rename("bts")
    y = x.merge(last_ts, left_on=["b", "ts"], right_on=["b", "bts"]).drop(columns="bts")
    return y


def grid(x: pd.DataFrame, tf: str, mode: str, n_cols: int, strikes: list[int], mult: float,
         end: pd.Timestamp | None) -> dict:
    """{'cols': [bucket starts], 'ce': DataFrame strike×bucket, 'pe': …, 'spot': Series by bucket, 'partial': bool}"""
    m = TF[tf]
    y = buckets(x, m, end)
    cols_all = sorted(y.b.unique())
    if mode in ("OI Change", "Volume") and len(cols_all) > 1 and y[y.b == cols_all[0]].ts.dt.floor("min").max() <= cols_all[0]:
        cols_all = cols_all[1:]                                 # the 09:15 opening snapshot is the baseline, not a bucket
    if not cols_all:
        return {}
    first = x[x.ts == x.ts.min()].set_index("strike")
    out = {}
    for side in ("ce", "pe"):
        oi = y.pivot_table(index="strike", columns="b", values=f"{side}_oi", aggfunc="last").sort_index(axis=1)
        if mode == "OI Change":
            base = first[f"{side}_oi"].reindex(oi.index)
            prev = pd.concat([base.rename("_base"), oi], axis=1).ffill(axis=1)
            v = (prev.diff(axis=1).iloc[:, 1:]) * mult
        elif mode == "OI":
            v = oi * mult
        elif mode == "Price":
            v = y.pivot_table(index="strike", columns="b", values=f"{side}_ltp", aggfunc="last").sort_index(axis=1)
        else:
            vol = y.pivot_table(index="strike", columns="b", values=f"{side}_vol", aggfunc="last").sort_index(axis=1)
            base = first[f"{side}_vol"].reindex(vol.index)
            v = pd.concat([base.rename("_base"), vol], axis=1).ffill(axis=1).diff(axis=1).iloc[:, 1:].clip(lower=0)
            v = v * (mult if mult > 1 else 1)
        out[side] = v.reindex(index=strikes).loc[:, cols_all[-n_cols:]]
        # "Day Change" / "Day Change %" — value at the end of each bucket vs the PREVIOUS SESSION's close
        piv = lambda c: y.pivot_table(index="strike", columns="b", values=c, aggfunc="last").sort_index(axis=1)
        if mode in ("OI", "OI Change"):
            lvl = oi * mult
            dc = piv(f"{side}_dchg") * mult
            if not np.isfinite(dc.values).any() or not np.nanmax(np.abs(dc.values)) > 0:
                dc = lvl.sub(first[f"{side}_oi"].reindex(lvl.index) * mult, axis=0)       # fallback: vs first snapshot
            pc = dc / (lvl - dc).where((lvl - dc) > 0) * 100
        elif mode == "Price":
            lvl = piv(f"{side}_ltp")
            dc = piv(f"{side}_pchg")
            if not np.isfinite(dc.values).any() or not np.nanmax(np.abs(dc.values)) > 0:
                dc = lvl.sub(first[f"{side}_ltp"].reindex(lvl.index), axis=0)
            pc = dc / (lvl - dc).where((lvl - dc) > 0) * 100
        else:
            dc = piv(f"{side}_vol").sub(first[f"{side}_vol"].reindex(oi.index).fillna(0), axis=0) * (mult if mult > 1 else 1)
            pc = dc * np.nan
        out[f"{side}_day"] = dc.reindex(index=strikes).loc[:, cols_all[-n_cols:]]
        out[f"{side}_pct"] = pc.reindex(index=strikes).loc[:, cols_all[-n_cols:]]
    sp = y.groupby("b")["spot"].last()
    out["spot"] = sp.reindex(cols_all[-n_cols:])
    out["cols"] = cols_all[-n_cols:]
    last_end = pd.Timestamp(out["cols"][-1])
    now_ts = y.ts.max()
    now_min = pd.Timestamp(now_ts).floor("min")
    out["partial"] = bool(now_min < last_end)
    out["elapsed"] = int(m - (last_end - now_min).total_seconds() // 60)
    out["now_ts"] = now_ts
    out["step_min"] = m
    # StockMojo labels a column with the time it ENDS (11:48 = change 11:45 → 11:48); the running one = latest snapshot
    out["labels"] = {c_: min(pd.Timestamp(c_), now_min).strftime("%H:%M") for c_ in out["cols"]}
    # full-window OI change (for the read) regardless of display mode
    if mode != "OI Change":
        out["chg"] = grid(x, tf, "OI Change", n_cols, strikes, mult, end)
    return out


# ═══════════════════════════════════════════════════════════════════════════
# 2 · Formatting (StockMojo look)
# ═══════════════════════════════════════════════════════════════════════════
def indian(v: float) -> str:
    """1,17,000 style grouping."""
    s = f"{abs(v):.0f}"
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        head = ",".join([head[max(0, i - 2):i] for i in range(len(head), 0, -2)][::-1])
        s = f"{head},{tail}"
    return ("-" if v < 0 else "") + s


def fmt_in(v: float, price: bool = False, full: bool = False) -> str:
    if v is None or not np.isfinite(v):
        return "—"
    if price:
        return f"{v:,.2f}"
    if full:
        return indian(v)
    a = abs(v)
    if a >= 1e7:
        return f"{v / 1e7:.2f} Cr"
    if a >= 1e5:
        return f"{v / 1e5:.2f} L"
    if a >= 1e3:
        return f"{v / 1e3:.2f} K"
    return f"{v:,.0f}"


LG, LR, DG, DR, ATM_BG = "#dcfce7", "#fee2e2", "#16a34a", "#dc2626", "#fef9c3"


def _cell_bg(v, col_max, col_min, mode, prev=None, hl: bool = True):
    if v is None or not np.isfinite(v):
        return "#ffffff", "#334155"
    if hl and mode == "OI Change":
        if v == col_min and v < 0:
            return DR, "#ffffff"
        if v == col_max and v > 0:
            return DG, "#ffffff"
    if hl and mode in ("OI", "Volume"):
        if v == col_max:
            return DG, "#ffffff"
        if v == col_min:
            return DR, "#ffffff"
    if mode == "OI Change":
        if v == 0:
            return "#ffffff", "#334155"
        return (LG, "#14532d") if v > 0 else (LR, "#7f1d1d")
    # OI / Price: colour by change vs previous column
    if prev is None or not np.isfinite(prev) or v == prev:
        return "#ffffff", "#334155"
    return (LG, "#14532d") if v > prev else (LR, "#7f1d1d")


def grid_html(g: dict, mode: str, atm: int, tf: str, opt: dict | None = None) -> str:
    opt = opt or {}
    full, hl = bool(opt.get("full")), opt.get("hl", True)
    show_day, show_pct = bool(opt.get("day")), bool(opt.get("pct"))
    fs = {"Small": 12, "Medium": 13.5, "Large": 15}.get(opt.get("font", "Small"), 12)
    price = mode == "Price"
    div = float(opt.get("lot_div", 1) or 1) if not price else 1.0
    cols = g["cols"]
    ce, pe = g["ce"] / div, g["pe"] / div
    day = {s: g[f"{s}_day"] / div for s in ("ce", "pe")}
    pct = {s: g[f"{s}_pct"] for s in ("ce", "pe")}
    tl = lambda c: pd.Timestamp(c).strftime("%H:%M")
    th = f"padding:6px 4px;text-align:center;font-weight:500;border:1px solid #e5e7eb;font-size:{fs}px;color:#334155;background:#f8fafc"
    td = f"padding:6px 4px;border:1px solid #eef2f7;text-align:center;font-size:{fs}px;white-space:nowrap"
    n = len(cols)
    head1 = (f"<tr><th colspan='{n}' style='{th};background:#ecfdf5;color:#047857'>Call</th>"
             f"<th rowspan='{4 if opt.get('sig') else 3}' style='{th};background:#fefce8;width:80px'>Strike</th>"
             f"<th colspan='{n}' style='{th};background:#fef2f2;color:#b91c1c'>Put</th></tr>")
    last_lab = lambda c: g["labels"].get(c, tl(c)) + (" ⏳" if (c == cols[-1] and g.get("partial")) else "")
    head2 = ("<tr>" + "".join(f"<th style='{th}'>{last_lab(c)}</th>" for c in cols) +
             "".join(f"<th style='{th}'>{last_lab(c)}</th>" for c in reversed(cols)) + "</tr>")
    sp = g["spot"]
    spc = [f"{v:,.0f}" if np.isfinite(v) else "—" for v in sp.values]
    head3 = ("<tr>" + "".join(f"<th style='{th};color:#64748b;font-size:11px'>NIFTY {s}</th>" for s in spc) +
             "".join(f"<th style='{th};color:#64748b;font-size:11px'>NIFTY {s}</th>" for s in reversed(spc)) + "</tr>")
    sig = opt.get("sig") or {}
    head4 = ""
    if sig:                                                      # 🎯 signal row: one badge per bucket, mirrored both sides
        import ts_signals
        b_ = lambda c: sig.get(pd.Timestamp(c), ("", "flat"))
        head4 = ("<tr>" + "".join(ts_signals.badge_cell(b_(c), th) for c in cols) +
                 "".join(ts_signals.badge_cell(b_(c), th) for c in reversed(cols)) + "</tr>")
    cmax = {s: d.max(axis=0) for s, d in (("ce", ce), ("pe", pe))}
    cmin = {s: d.min(axis=0) for s, d in (("ce", ce), ("pe", pe))}
    body = []
    for k in ce.index:
        cells = []
        for side, order in (("ce", cols), ("pe", list(reversed(cols)))):
            d = ce if side == "ce" else pe
            for c in order:
                v = d.at[k, c] if c in d.columns else np.nan
                j = cols.index(c)
                prev = d.at[k, cols[j - 1]] if j > 0 else None
                bg, fg = _cell_bg(v, cmax[side][c], cmin[side][c], mode, prev, hl)
                extra = []
                if show_day:
                    dv = day[side].at[k, c] if c in day[side].columns else np.nan
                    if np.isfinite(dv):
                        extra.append(("+" if dv > 0 else "") + fmt_in(dv, price, full))
                if show_pct:
                    pv = pct[side].at[k, c] if c in pct[side].columns else np.nan
                    if np.isfinite(pv):
                        extra.append(f"{pv:+.1f}%")
                sub = (f"<div style='font-size:{fs - 2}px;opacity:.75'>{' · '.join(extra)}</div>" if extra else "")
                cells.append(f"<td style='{td};background:{bg};color:{fg}'>{fmt_in(v, price, full)}{sub}</td>")
            if side == "ce":
                bgk = ATM_BG if k == atm else "#fffbeb"
                cells.append(f"<td style='{td};background:{bgk};color:#0f172a;font-weight:{700 if k == atm else 500}'>{k}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    return ("<style>.tsc-g tr:hover td{filter:brightness(.88)}</style>"
            f"<div style='overflow-x:auto;border:1px solid #e5e7eb;border-radius:6px;background:#fff'>"
            f"<table class='tsc-g' style='border-collapse:collapse;width:100%;font-family:Inter,system-ui,sans-serif'>"
            f"{head1}{head2}{head3}{head4}{''.join(body)}</table></div>")


# ═══════════════════════════════════════════════════════════════════════════
# 3 · Pro read
# ═══════════════════════════════════════════════════════════════════════════
def _top(s: pd.Series, n: int = 1, largest: bool = True):
    s = s.dropna()
    s = s[s > 0] if largest else s[s < 0]
    return (s.nlargest(n) if largest else s.nsmallest(n))


def column_read(ce: pd.Series, pe: pd.Series, atm: int, step: float) -> tuple[str, str]:
    """One line for one bucket of OI change."""
    near = lambda s: s[(s.index >= atm - 4 * step) & (s.index <= atm + 4 * step)]
    cw, pw = near(ce).clip(lower=0).sum(), near(pe).clip(lower=0).sum()
    cu, pu = -near(ce).clip(upper=0).sum(), -near(pe).clip(upper=0).sum()
    bull = pw + cu                                               # put writing + call covering
    bear = cw + pu                                               # call writing + put covering
    parts = []
    tc, tp = _top(ce), _top(pe)
    uc, up = _top(ce, largest=False), _top(pe, largest=False)
    if len(tc):
        parts.append(f"CE writing {tc.index[0]} (+{fmt_in(tc.iloc[0])})")
    if len(tp):
        parts.append(f"PE writing {tp.index[0]} (+{fmt_in(tp.iloc[0])})")
    if len(uc):
        parts.append(f"CE cut {uc.index[0]} ({fmt_in(uc.iloc[0])})")
    if len(up):
        parts.append(f"PE cut {up.index[0]} ({fmt_in(up.iloc[0])})")
    tot = bull + bear
    if tot <= 0:
        return "NEUTRAL", "quiet bucket"
    r = (bull - bear) / tot
    tag = "BULLISH" if r > 0.25 else "BEARISH" if r < -0.25 else "MIXED"
    return tag, " · ".join(parts)


def rolls(ce: pd.DataFrame, pe: pd.DataFrame, step: float, frac: float = 0.35) -> list[str]:
    """Writers moving their line inside the window: big cut at K, big build at K ± 1–3 steps on the same side."""
    out = []
    for side, d, lab in (("ce", ce, "Call"), ("pe", pe, "Put")):
        w = d.sum(axis=1)
        big = float(w.abs().max() or 0)
        if not big:
            continue
        for k, v in w[w < -frac * big].sort_values().items():
            for j in (-1, 1, -2, 2, -3, 3):
                kk = k + int(j * step)
                if kk in w.index and w[kk] > 0.5 * abs(v):
                    up_ = kk > k
                    meaning = ("ceiling LIFTING — bullish" if side == "ce" and up_ else
                               "ceiling PRESSING DOWN — bearish" if side == "ce" else
                               "floor RISING — bullish" if up_ else "floor GIVING WAY — bearish")
                    out.append(f"<b>{lab} writers rolled {'UP' if up_ else 'DOWN'} {k:,} → {kk:,}</b> "
                               f"({fmt_in(v)} / +{fmt_in(w[kk])}) — {meaning}")
                    break
    return out


def pro_read(chg: dict, atm: int, step: float, spot_now: float) -> dict:
    ce, pe, cols = chg["ce"], chg["pe"], chg["cols"]
    wce, wpe = ce.sum(axis=1), pe.sum(axis=1)                   # window totals per strike
    above, below = wce[wce.index >= atm - step], wpe[wpe.index <= atm + step]
    res = above.idxmax() if len(above.dropna()) and above.max() > 0 else None
    sup = below.idxmax() if len(below.dropna()) and below.max() > 0 else None
    res_cut = _top(wce[wce.index >= atm - 2 * step], largest=False)     # call writers leaving at / above spot
    sup_cut = _top(wpe[wpe.index <= atm + 2 * step], largest=False)     # put writers leaving at / below spot
    near = lambda s: s[(s.index >= atm - 4 * step) & (s.index <= atm + 4 * step)]
    bull = near(wpe).clip(lower=0).sum() + (-near(wce).clip(upper=0).sum())
    bear = near(wce).clip(lower=0).sum() + (-near(wpe).clip(upper=0).sum())
    tot = bull + bear
    score = (bull - bear) / tot if tot else 0.0
    bias = "BULLISH" if score > 0.25 else "BEARISH" if score < -0.25 else "MIXED / RANGE"
    bcol = GREEN if bias == "BULLISH" else RED if bias == "BEARISH" else GOLD

    # persistence: strikes written in most columns
    pers_ce = (ce > 0).sum(axis=1)
    pers_pe = (pe > 0).sum(axis=1)
    n = len(cols)
    L = []
    L.append(f"<b>Window bias ({n} buckets): <span style='color:{bcol}'>{bias}</span></b> — near ATM: put writing + call "
             f"covering {fmt_in(bull)} vs call writing + put covering {fmt_in(bear)}.")
    if res is not None:
        L.append(f"<b>Resistance being built: {res:,}</b> (+{fmt_in(wce[res])} calls written, {int(pers_ce[res])}/{n} "
                 f"buckets) — {'consistent: a real ceiling' if pers_ce[res] >= max(2, n - 2) else 'one-off build: watch if it holds'}.")
    if sup is not None:
        dist = spot_now - sup
        L.append(f"<b>Support being built: {sup:,}</b> (+{fmt_in(wpe[sup])} puts written, {int(pers_pe[sup])}/{n} buckets)"
                 f"{f' — {dist:,.0f} pts below spot: far away, the floor is not close' if dist > 4 * step else ''}.")
    if len(sup_cut):
        k = sup_cut.index[0]
        L.append(f"<b>Support weakening: {k:,} puts cut {fmt_in(sup_cut.iloc[0])}</b> — put writers leaving "
                 f"{'right at / just below spot → the floor here is going' if abs(k - spot_now) <= 3 * step else 'that level'}.")
    if len(res_cut):
        k = res_cut.index[0]
        L.append(f"<b>Resistance weakening: {k:,} calls cut {fmt_in(res_cut.iloc[0])}</b> — call writers leaving "
                 f"{'near spot → short covering can lift price' if abs(k - spot_now) <= 3 * step else 'a far strike (often a roll — see below)'}.")
    rl = rolls(ce, pe, step)
    if rl:
        L.append("<b>Rolls:</b> " + " · ".join(rl[:3]))
    # acceleration: last 2 buckets vs the earlier ones
    if n >= 4:
        def b_score(cs):
            c_, p_ = ce[cs].sum(axis=1), pe[cs].sum(axis=1)
            b1 = near(p_).clip(lower=0).sum() - near(c_).clip(upper=0).sum()
            b2 = near(c_).clip(lower=0).sum() - near(p_).clip(upper=0).sum()
            return (b1 - b2) / (b1 + b2) if (b1 + b2) else 0.0
        s_new, s_old = b_score(cols[-2:]), b_score(cols[:-2])
        if s_new - s_old < -0.3:
            L.append(f"<b>Acceleration:</b> the last 2 buckets turned MORE bearish ({s_old:+.2f} → {s_new:+.2f}) — "
                     "selling pressure is picking up now.")
        elif s_new - s_old > 0.3:
            L.append(f"<b>Acceleration:</b> the last 2 buckets turned MORE bullish ({s_old:+.2f} → {s_new:+.2f}) — "
                     "buyers / put writers stepping in now.")
        else:
            L.append(f"<b>Acceleration:</b> none — the newest buckets read like the earlier ones ({s_old:+.2f} → {s_new:+.2f}).")
    # breakout / breakdown watch at ATM
    ce_atm = ce.loc[[k for k in (atm, atm + int(step)) if k in ce.index]].iloc[:, -2:].sum().sum()
    pe_atm = pe.loc[[k for k in (atm, atm - int(step)) if k in pe.index]].iloc[:, -2:].sum().sum()
    if ce_atm < 0 and pe_atm > 0:
        watch = (f"<b style='color:{GREEN}'>BREAKOUT WATCH ↑</b> — ATM calls being cut and ATM puts written in the last "
                 "2 buckets: writers are giving up the ceiling. Confirm with a candle close above the nearest call wall.")
    elif ce_atm > 0 and pe_atm < 0:
        watch = (f"<b style='color:{RED}'>BREAKDOWN WATCH ↓</b> — ATM calls written and ATM puts cut in the last 2 buckets: "
                 "writers are pressing price down. Confirm with a candle close below the nearest put support.")
    else:
        watch = "<b>No breakout / breakdown pattern at ATM</b> in the last 2 buckets."
    L.append(watch)
    do = []
    if bias == "BEARISH":
        do.append(f"Sell-on-rise bias: PE on pull-backs toward {res:,} resistance" if res else "Sell-on-rise bias")
        do.append("Don't buy CE because it 'looks cheap' while ATM puts are being cut.")
    elif bias == "BULLISH":
        do.append(f"Buy-on-dip bias: CE on dips toward {sup:,} support" if sup else "Buy-on-dip bias")
        do.append("Don't short into fresh put writing at ATM.")
    else:
        do.append("Range: trade only at the walls with a reversal candle, or wait.")
    do.append("A single coloured cell is noise; the same strike in the same colour for 3+ buckets is a decision.")
    return dict(lines=L, bias=bias, bcol=bcol, score=score, res=res, sup=sup, do=do)


# ═══════════════════════════════════════════════════════════════════════════
# 4 · Streamlit
# ═══════════════════════════════════════════════════════════════════════════
HOW = [
    "<b>Columns are time buckets</b> of the timeframe you pick, labelled with the time they END (3m: 11:48 = what "
    "changed from 11:45 to 11:48). Buckets start at 09:15. "
    "Calls read left → right (oldest → newest), puts read right → left — the NEWEST bucket is always next to the strike.",
    "<b>The newest column (⏳) is still running</b> — its numbers keep changing until the bucket closes. Judge on closed buckets.",
    "<b>OI Change</b> cell = OI added (+) or cut (−) in that bucket only. CE + = call writing (resistance). CE − = call "
    "writers leaving (short covering, bullish if near spot). PE + = put writing (support). PE − = put writers leaving "
    "(support breaking, bearish if near spot).",
    "<b>Dark green / dark red</b> = the biggest build / cut in that column — where the money moved most in that bucket.",
    "<b>Read across a ROW</b> (one strike through time): the same colour for 3+ buckets = a real decision, not noise.",
    "<b>Read down a COLUMN</b> (one moment): calls written above + puts cut below = bearish push; puts written below + calls "
    "cut above = bullish push.",
    "<b>Rolls:</b> big cut at one strike + build 1–3 strikes away on the same side = writers moving their line. Call roll DOWN "
    "= ceiling pressing down (bearish); put roll UP = floor rising (bullish).",
    "<b>Timeframe:</b> 1m/3m = entries and exits, 5m/15m = the session story, 1h = the big picture. A signal that shows on "
    "both 3m and 15m is far stronger than one that only shows on 1m.",
    "<b>OI</b> view = open interest level (green / red vs the previous bucket). <b>Price</b> = premium. <b>Volume</b> = "
    "contracts traded in that bucket — high volume with little OI change = day-traders, not writers.",
    "Context, not a trigger — confirm with your price-action setup. 1 lot, SL at entry.",
]


def _card(title: str, body: str, border: str = GOLD) -> str:
    return (f"<div style='background:{NAVY};border:1px solid {border};border-radius:8px;padding:10px 14px;margin:8px 0'>"
            f"<div style='color:{GOLD};font-weight:700'>{title}</div>"
            f"<div style='color:{WHITE};margin-top:4px;line-height:1.6'>{body}</div></div>")


def _settings(st) -> dict:
    """StockMojo's ⚙ Settings: Full numbers · Day Change · Day Change % · Lot · Highlight High/Low · Font size."""
    box = st.popover("⚙ Settings") if hasattr(st, "popover") else st.expander("⚙ Settings")
    with box:
        full = st.toggle("Full numbers", value=False, key="tsc_full", help="1,17,000 instead of 1.17 L")
        day = st.toggle("Day Change", value=False, key="tsc_dayc",
                        help="Small line under each value: change vs the PREVIOUS session's close (OI / price), or the "
                             "day's total so far (volume)")
        pct = st.toggle("Day Change Percent", value=False, key="tsc_dayp", help="Same, in %")
        lot = st.toggle("Lot", value=False, key="tsc_lot", help="Show OI / volume in lots instead of shares")
        hl = st.toggle("Highlight High/Low", value=True, key="tsc_hl",
                       help="Dark green / dark red = the highest / lowest value in each column")
        font = st.radio("Font Size", ["Small", "Medium", "Large"], horizontal=True, key="tsc_font")
        auto = st.toggle("Auto-refresh (30 s)", value=True, key="tsc_auto", help="Live session only")
    return dict(full=full, day=day, pct=pct, lot=lot, hl=hl, font=font, auto=auto)


def render(day: date | None = None) -> None:
    import streamlit as st
    import store
    import config

    days = ol._days_available()
    if not days:
        st.info("No option-chain snapshots stored yet.")
        return

    def _n(d):
        return len(list((Path(config.SNAPSHOT_DIR) / d.strftime("%Y-%m-%d")).glob("*.meta.json")))

    @st.cache_data(ttl=20, show_spinner=False)
    def _frame(d: date, n_files: int, e: str | None):
        snaps = store.load_day(d)
        exps = expiries(snaps) if snaps else []
        e = e if e in exps else (exps[0] if exps else None)
        return exps, e, (long_frame(snaps, e) if e else pd.DataFrame())

    # ── row 1: session · expiry · timeframe · timestamp · show ─────────────────────────────────────────
    c = st.columns([1.5, 1.6, 2.3, 1.0, 2.6])
    day = c[0].selectbox("Session", days, index=days.index(day) if day in days else 0,
                         format_func=lambda d: d.strftime("%a %d %b %Y"), key="tsc_day")
    exps, _, _ = _frame(day, _n(day), None)
    if not exps:
        st.warning("No snapshots with expiry information for this session yet.")
        return

    def _dte(e):
        x_ = pd.to_datetime(e, errors="coerce", dayfirst=not str(e)[:4].isdigit())
        return f"{(x_.date() - day).days}d" if pd.notna(x_) else "?"

    exp = c[1].selectbox("Expiry", exps, key="tsc_exp", format_func=lambda e: f"{e} ({_dte(e)})")
    tf = c[2].radio("Timeframe", list(TF), index=1, horizontal=True, key="tsc_tf")
    ncol = c[3].selectbox("Timestamp", [3, 4, 5, 6, 8, 10, 12], index=3, key="tsc_n")
    mode = c[4].radio("Show", MODES, index=0, horizontal=True, key="tsc_mode")
    # ── row 2: selection · ± ATM / custom · settings ───────────────────────────────────────────────────
    c2 = st.columns([1.6, 1.0, 4.4, 1.2])
    sel = c2[0].radio("Selection", ["Range", "Custom"], horizontal=True, key="tsc_sel")
    rng = c2[1].selectbox("± ATM", [3, 4, 5, 6, 8, 10, 12, 15], index=4, key="tsc_rng", disabled=sel == "Custom")
    with c2[3]:
        opt = _settings(st)
    custom = None
    _, _, x0 = _frame(day, _n(day), exp)
    if sel == "Custom" and not x0.empty:
        step0 = float(ol._rules().get("strike_step", 50) or 50)
        have0 = sorted(x0.strike.unique())
        atm0 = int(min(have0, key=lambda k: abs(k - float(x0[x0.ts == x0.ts.max()].spot.iloc[0]))))
        opts = sorted(sorted(have0, key=lambda k: abs(k - atm0))[:41])
        custom = sorted(int(k) for k in c2[2].multiselect("Strikes", opts, key="tsc_custom",
                                                          default=[k for k in opts if abs(k - atm0) <= 4 * step0]))
    live = day == max(days) and day == date.today()
    cc_ = st.columns([1, 1, 4])
    replay = cc_[0].toggle("Replay (pick an end time)", value=False, key="tsc_replay")
    show_sig = cc_[1].toggle("🎯 Signals", value=True, key="tsc_sig",
                             help="Support / resistance built · broken · reversal · fake breakout — a row in the grid + a card below it")

    def body():
        _, e2, x = _frame(day, _n(day), exp)
        if x.empty or x.ts.nunique() < 2:
            st.warning("Not enough snapshots for this expiry yet.")
            return
        mult, unit = share_mult(x)
        lot = lot_size()
        step = float(ol._rules().get("strike_step", 50) or 50)
        have = sorted(x.strike.unique())
        times = sorted(x.ts.unique())
        end = pd.Timestamp(times[-1])
        if replay:
            end = pd.Timestamp(st.select_slider("End at", options=times, value=times[-1],
                                                format_func=lambda t: pd.Timestamp(t).strftime("%H:%M"), key="tsc_end"))
        spot_now = float(x[x.ts == x[x.ts <= end].ts.max()].spot.iloc[0])
        atm = int(min(have, key=lambda k: abs(k - spot_now)))
        if sel == "Custom":
            strikes = [k for k in (custom or []) if k in have]
        else:
            strikes = [k for k in have if atm - rng * step <= k <= atm + rng * step]
        if not strikes:
            st.info("Pick at least one strike.")
            return
        g = grid(x, tf, mode, int(ncol), strikes, mult, end)
        if not g or not len(g["cols"]):
            st.warning("No buckets yet.")
            return
        try:
            import pcr_oi_pro as po
            f, _why = po.future_1m(day)
            fut_txt = (f"Future: <b>{float(f[pd.to_datetime(f.ts).dt.tz_localize(None) <= end].fut.iloc[-1]):,.1f}</b> · "
                       if f is not None and len(f) else "")
        except Exception:                                       # noqa: BLE001
            fut_txt = ""
        left = (f"⏳ newest {tf} bucket still running ({g['elapsed']} of "
                f"{g['step_min']} min)" if g["partial"] else "all buckets closed")
        units = "price ₹" if mode == "Price" else ("lots" if opt["lot"] else "shares")
        st.markdown(f"<div style='display:flex;justify-content:space-between;flex-wrap:wrap;color:{MUTED};font-size:0.82rem;"
                    f"margin:2px 0 6px'><span>{mode} · {tf} buckets · {units} ({unit}) · {left}</span>"
                    f"<span>{fut_txt}NIFTY <b>{spot_now:,.1f}</b> · {pd.Timestamp(g['now_ts']):%d %b %H:%M:%S}"
                    f"{' · live, refreshes every 30 s' if live and opt['auto'] and not replay else ''}</span></div>",
                    unsafe_allow_html=True)
        sg = None
        if show_sig:
            try:
                import ts_signals
                sg = ts_signals.compute(x, TF[tf], end)
            except Exception as e_:                             # noqa: BLE001
                st.caption(f"🎯 signals unavailable: {e_}")
        sig_map = {pd.Timestamp(r_.ts): r_.badge for r_ in sg.itertuples()} if sg is not None and len(sg) else {}
        st.markdown(grid_html(g, mode, atm, tf, dict(opt, lot_div=lot if opt["lot"] else 1, sig=sig_map)), unsafe_allow_html=True)
        if sg is not None and len(sg):
            import ts_signals
            st.markdown(_card(f"🎯 LEVEL SIGNALS — {tf} · buy / sell · built / broken / reversal / fake-out",
                              ts_signals.card_html(sg, g["cols"], g["labels"], WHITE, MUTED, GOLD, GREEN, RED)),
                        unsafe_allow_html=True)

        chg = g if mode == "OI Change" else g["chg"]
        rd = pro_read(chg, atm, step, spot_now)
        st.markdown(_card(f"★ PRO READ — {tf} · last {len(chg['cols'])} buckets",
                          "<br>".join(rd["lines"]), border=rd["bcol"]), unsafe_allow_html=True)
        rows = []
        for cc in chg["cols"]:
            tag, txt = column_read(chg["ce"][cc], chg["pe"][cc], atm, step)
            colr = GREEN if tag == "BULLISH" else RED if tag == "BEARISH" else GOLD
            sp = chg["spot"].get(cc, np.nan)
            part = " ⏳" if (cc == chg["cols"][-1] and chg.get("partial")) else ""
            rows.append(f"<tr><td style='padding:3px 8px;color:{GOLD};white-space:nowrap'><b>{chg['labels'].get(cc, '')}{part}</b></td>"
                        f"<td style='padding:3px 8px;white-space:nowrap'>{sp:,.0f}</td>"
                        f"<td style='padding:3px 8px;color:{colr};white-space:nowrap'><b>{tag}</b></td>"
                        f"<td style='padding:3px 8px'>{txt}</td></tr>")
        st.markdown(_card("🕐 BUCKET-BY-BUCKET NARRATIVE",
                          f"<div style='overflow-x:auto'><table style='color:{WHITE};font-size:0.86rem;width:100%'>"
                          f"<tr style='color:{MUTED};text-align:left'><th style='padding:3px 8px'>Bucket</th>"
                          f"<th style='padding:3px 8px'>NIFTY</th><th style='padding:3px 8px'>Read</th>"
                          f"<th style='padding:3px 8px'>Biggest moves</th></tr>{''.join(rows)}</table></div>"),
                    unsafe_allow_html=True)
        st.markdown(_card("WHAT TO DO WITH IT", "<ul style='margin:0 0 0 18px'>" +
                          "".join(f"<li>{x_}</li>" for x_ in rd["do"]) + "</ul>"), unsafe_allow_html=True)

    if live and opt["auto"] and not replay and hasattr(st, "fragment"):
        st.fragment(run_every=30)(body)()
    else:
        body()
    with st.expander("How to read the time-series grid"):
        st.markdown("<ul>" + "".join(f"<li style='margin:3px 0'>{h}</li>" for h in HOW) + "</ul>", unsafe_allow_html=True)
    st.caption("Information only — not financial advice. Reads are heuristics, not backtested in this app.")
