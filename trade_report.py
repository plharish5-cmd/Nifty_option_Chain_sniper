"""
trade_report.py — daily trading report from Dhan's "Global Transaction Report" CSV (+ your Paper Desk trades).

What it shows (one self-contained HTML file, dark navy / gold, opens on phone or laptop):
  • Latest day: orders, gross P&L, every charge (brokerage, STT, NSE txn incl. IPFT, GST, SEBI, stamp), net, per strike,
    and a RULES SCORECARD (orders, daily loss limit, size, expiry day, overnight)
  • Whole period: where the money went, day-wise net + cumulative, expiry vs other days, orders/day vs P&L,
    size per month, win/loss sizes, overnight holds (FIFO), CE vs PE, behaviour after a red day
  • Paper Desk readiness checklist — the conditions to meet before trading real money again
  • Dhan's charge formula, re-derived from YOUR contract notes

  .venv/bin/python trade_report.py --dhan "Dhan_GlobalTransction_Report_01-04-2026_07-10-2026.csv"
  .venv/bin/python trade_report.py            # newest Dhan CSV in data/dhan_reports/ (or this folder) + paper trades
  .venv/bin/python trade_report.py --paper-only

Daily habit: after 15:30 download Dhan → Reports → Global Transaction (from your first date to today) into
data/dhan_reports/, run this, open data/reports/trade_report_<date>.html.
Your name/UCC/mobile are NOT copied into the report. Information only — not financial advice.
"""
from __future__ import annotations

import argparse
import html
import io
import math
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent
DATA = ROOT / "data"
LOT = 65

# ── your rules (edit these) ─────────────────────────────────────────────────
RULES = dict(max_orders=6,          # executed orders per day (3 round trips)
             daily_loss=1500.0,     # stop trading for the day at this net loss (₹)
             max_lots=1,            # lots per order until the paper record qualifies
             no_expiry_day=True,    # no trading on expiry Tuesday
             no_overnight=True)     # no option buys carried overnight
READY = dict(sessions=20, profit_factor=1.3, max_dd_pct=5.0, orders_ok_pct=90.0)

# Dhan options tariff — verified against the user's contract notes (Jul–Oct 2026)
TARIFF = dict(brokerage=20.0, stt_sell=0.0015, txn=0.0003553, sebi=0.000001, stamp_buy=0.00003, gst=0.18)

NAVY, PANEL, GOLD, WHITE, MUTED, GREEN, RED, GRID = ("#0b1a33", "#11264a", "#d4af37", "#f1f5f9", "#94a3b8",
                                                    "#22c55e", "#ef4444", "#1e3a5f")


# ═══════════════════════════════════════════════════════════════════════════
# 1 · load
# ═══════════════════════════════════════════════════════════════════════════
def load_dhan(path: Path) -> pd.DataFrame:
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    i0 = next(i for i, l in enumerate(lines) if l.startswith("Date,Scrip"))
    i1 = next((i for i, l in enumerate(lines) if i > i0 and not l.strip()), len(lines))
    df = pd.read_csv(io.StringIO("\n".join(lines[i0:i1])))
    df["Date"] = pd.to_datetime(df["Date"], format="%d %b %Y %H:%M:%S", errors="coerce")
    for c in df.columns:
        if c not in ("Date", "Scrip Name", "Exchange", "Bill No."):
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
    m = df["Scrip Name"].str.extract(r"OPT NIFTY (\d{2} \w{3} \d{4}) (\d+) (CE|PE)")
    df["expiry"] = pd.to_datetime(m[0], format="%d %b %Y", errors="coerce")
    df["strike"] = pd.to_numeric(m[1], errors="coerce")
    df["side"] = m[2]
    df["charges"] = df[["Brokerage", "GST", "STT", "SEBI Fees", "Stamp Duty", "Txn. Charges", "Oth. Charges"]].sum(axis=1)
    df["gross"] = df["Sell Value"] - df["Buy Value"]
    df["net"] = df["gross"] - df["charges"]
    df["orders"] = (df["Brokerage"] / TARIFF["brokerage"]).round()
    df["lots_per_order"] = (df["Buy Qty."] + df["Sell Qty."]) / LOT / df["orders"].replace(0, np.nan)
    df["expday"] = df["Date"].dt.normalize() == df["expiry"]
    return df.dropna(subset=["Date"])


def newest_dhan() -> Path | None:
    cands = sorted([*(DATA / "dhan_reports").glob("*Dhan*Global*.csv"), *ROOT.glob("*Dhan*Global*.csv")],
                   key=lambda p: p.stat().st_mtime)
    return cands[-1] if cands else None


def load_paper() -> pd.DataFrame:
    f = DATA / "paper_trading" / "trades.csv"
    if not f.exists():
        return pd.DataFrame()
    t = pd.read_csv(f)
    for c in ("quantity", "entry_price", "exit_price", "pnl_rupees", "charges_rupees", "net_pnl_rupees"):
        t[c] = pd.to_numeric(t[c], errors="coerce").fillna(0)
    t["day"] = pd.to_datetime(t["exit_time"].astype(str).str[:10], errors="coerce")
    return t.dropna(subset=["day"])


# ═══════════════════════════════════════════════════════════════════════════
# 2 · analysis
# ═══════════════════════════════════════════════════════════════════════════
def days(df: pd.DataFrame) -> pd.DataFrame:
    d = df.groupby(df["Date"].dt.normalize()).agg(
        orders=("orders", "sum"), gross=("gross", "sum"), charges=("charges", "sum"), net=("net", "sum"),
        brokerage=("Brokerage", "sum"), stt=("STT", "sum"), txn=("Txn. Charges", "sum"), gst=("GST", "sum"),
        lots=("Buy Qty.", lambda v: v.sum() / LOT), expday=("expday", "max"),
        maxlots=("lots_per_order", "max")).sort_index()
    d["cum"] = d["net"].cumsum()
    return d


def overnight(df: pd.DataFrame) -> pd.DataFrame:
    """FIFO: quantity bought one day and sold on a later day (same contract)."""
    out = []
    for s, g in df.sort_values("Date").groupby("Scrip Name"):
        inv = []
        for r in g.itertuples(index=False):
            bq, sq = float(r[4]), float(r[6])
            bp = float(r[5]) / bq if bq else 0.0
            sp = float(r[7]) / sq if sq else 0.0
            m = min(bq, sq)
            rest_b, rest_s = bq - m, sq - m
            while rest_s > 0 and inv:
                q, p, dd = inv[0]
                t = min(q, rest_s)
                out.append(dict(contract=s, bought=dd, sold=r.Date, qty=t, buy=p, sell=sp, gross=t * (sp - p),
                                to_expiry=bool(r.Date.normalize() == r.expiry)))
                rest_s -= t
                if q > t:
                    inv[0] = (q - t, p, dd)
                else:
                    inv.pop(0)
            if rest_b > 0:
                inv.append((rest_b, bp, r.Date))
    return pd.DataFrame(out)


def scorecard(day_row: pd.Series, held_over: bool) -> list[tuple[str, bool, str]]:
    r = RULES
    return [
        (f"≤ {r['max_orders']} orders", day_row["orders"] <= r["max_orders"], f"{int(day_row['orders'])} orders"),
        (f"daily loss ≤ ₹{r['daily_loss']:,.0f}", day_row["net"] >= -r["daily_loss"], f"net {money(day_row['net'])}"),
        (f"≤ {r['max_lots']} lot per order", (day_row["maxlots"] or 0) <= r["max_lots"] + 1e-9,
         f"up to {day_row['maxlots']:.1f} lots/order" if pd.notna(day_row["maxlots"]) else "—"),
        ("no expiry-day trading", not (r["no_expiry_day"] and bool(day_row["expday"])),
         "expiry day" if day_row["expday"] else "normal day"),
        ("nothing carried overnight", not (r["no_overnight"] and held_over), "carried" if held_over else "flat at close"),
    ]


def paper_readiness(t: pd.DataFrame, capital: float = 100000.0) -> dict | None:
    if t.empty:
        return None
    d = t.groupby("day").agg(net=("net_pnl_rupees", "sum"), trades=("net_pnl_rupees", "size"),
                             charges=("charges_rupees", "sum")).sort_index()
    d["orders"] = d["trades"] * 2                                   # one buy + one sell per closed trade
    last = d.tail(READY["sessions"])
    wins, losses = t[t.day.isin(last.index)]["net_pnl_rupees"], None
    gp = float(wins[wins > 0].sum())
    gl = float(-wins[wins < 0].sum())
    pf = gp / gl if gl else (math.inf if gp else 0.0)
    eq = capital + last["net"].cumsum()
    dd = float(((eq.cummax() - eq) / eq.cummax()).max() * 100) if len(eq) else 0.0
    ok_orders = 100 * float((last["orders"] <= RULES["max_orders"]).mean()) if len(last) else 0.0
    checks = [
        (f"≥ {READY['sessions']} paper sessions", len(d) >= READY["sessions"], f"{len(d)} so far"),
        ("net profit AFTER charges (last 20)", float(last["net"].sum()) > 0, money(float(last["net"].sum()))),
        (f"profit factor ≥ {READY['profit_factor']}", pf >= READY["profit_factor"], f"{pf:.2f}" if np.isfinite(pf) else "∞"),
        (f"≤ {RULES['max_orders']} orders on ≥ {READY['orders_ok_pct']:.0f}% of days", ok_orders >= READY["orders_ok_pct"],
         f"{ok_orders:.0f}% of days"),
        (f"no day worse than −₹{RULES['daily_loss']:,.0f}", bool((last["net"] >= -RULES["daily_loss"]).all()),
         f"worst {money(float(last['net'].min()))}"),
        (f"max drawdown < {READY['max_dd_pct']}%", dd < READY["max_dd_pct"], f"{dd:.1f}%"),
    ]
    return dict(days=d, checks=checks, ready=all(c[1] for c in checks))


# ═══════════════════════════════════════════════════════════════════════════
# 3 · html helpers
# ═══════════════════════════════════════════════════════════════════════════
def money(v: float, dec: int = 0) -> str:
    s = f"{abs(v):,.{dec}f}"
    return f"+₹{s}" if v > 0 else (f"−₹{s}" if v < 0 else "₹0")


def cls(v: float) -> str:
    return "g" if v > 0 else "r" if v < 0 else "m"


def esc(s) -> str:
    return html.escape(str(s))


def bars_svg(d: pd.DataFrame, col: str, title: str, height: int = 200) -> str:
    """Single-axis bar chart (profit above zero, loss below), native hover titles."""
    v = d[col].to_numpy(float)
    n = len(v)
    if not n:
        return ""
    W, H, pad = 920, height, 34
    hi, lo = max(0.0, v.max()), min(0.0, v.min())
    span = (hi - lo) or 1.0
    y0 = pad / 2 + (hi / span) * (H - pad)
    bw = max(2.0, (W - 96) / n - 2)
    out = [f"<svg viewBox='0 0 {W} {H + 18}' role='img' aria-label='{esc(title)}' style='width:100%;height:auto'>"]
    for k in (hi, 0.0, lo):
        y = pad / 2 + ((hi - k) / span) * (H - pad)
        out.append(f"<line x1='86' x2='{W}' y1='{y:.1f}' y2='{y:.1f}' stroke='{GRID}' stroke-width='1'/>"
                   f"<text x='82' y='{y + 4:.1f}' text-anchor='end' font-size='11' fill='{MUTED}'>{money(k)}</text>")
    for i, (idx, val) in enumerate(zip(d.index, v)):
        x = 90 + i * ((W - 96) / n)
        y = pad / 2 + ((hi - max(val, 0)) / span) * (H - pad)
        h = abs(val) / span * (H - pad)
        c = GREEN if val >= 0 else RED
        out.append(f"<rect x='{x:.1f}' y='{y:.1f}' width='{bw:.1f}' height='{max(h, 1):.1f}' rx='2' fill='{c}'>"
                   f"<title>{idx:%a %d %b}: {money(val)}</title></rect>")
    step = max(1, n // 8)
    for i in range(0, n, step):
        x = 90 + i * ((W - 96) / n) + bw / 2
        out.append(f"<text x='{x:.1f}' y='{H + 14}' text-anchor='middle' font-size='11' fill='{MUTED}'>{d.index[i]:%d %b}</text>")
    out.append("</svg>")
    return "".join(out)


def line_svg(d: pd.DataFrame, col: str, title: str, height: int = 180) -> str:
    v = d[col].to_numpy(float)
    n = len(v)
    if n < 2:
        return ""
    W, H, pad = 920, height, 30
    hi, lo = max(0.0, v.max()), min(0.0, v.min())
    span = (hi - lo) or 1.0
    xs = [90 + i * (W - 96) / (n - 1) for i in range(n)]
    ys = [pad / 2 + ((hi - x) / span) * (H - pad) for x in v]
    pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))
    z = pad / 2 + (hi / span) * (H - pad)
    out = [f"<svg viewBox='0 0 {W} {H + 18}' role='img' aria-label='{esc(title)}' style='width:100%;height:auto'>",
           f"<line x1='86' x2='{W}' y1='{z:.1f}' y2='{z:.1f}' stroke='{GRID}'/>",
           f"<text x='82' y='{z + 4:.1f}' text-anchor='end' font-size='11' fill='{MUTED}'>₹0</text>",
           f"<text x='82' y='{ys[int(np.argmin(v))] + 4:.1f}' text-anchor='end' font-size='11' fill='{MUTED}'>{money(v.min())}</text>",
           f"<polyline points='{pts}' fill='none' stroke='{GOLD}' stroke-width='2'/>"]
    for x, y, idx, val in zip(xs, ys, d.index, v):
        out.append(f"<circle cx='{x:.1f}' cy='{y:.1f}' r='6' fill='transparent'><title>{idx:%a %d %b}: {money(val)}</title></circle>")
    out.append(f"<text x='{xs[-1]:.1f}' y='{ys[-1] - 8:.1f}' text-anchor='end' font-size='12' fill='{WHITE}'>{money(v[-1])}</text></svg>")
    return "".join(out)


def table(rows: list[list], head: list[str], right_from: int = 1) -> str:
    th = "".join(f"<th style='text-align:{'left' if i < right_from else 'right'}'>{esc(h)}</th>" for i, h in enumerate(head))
    tb = "".join("<tr>" + "".join(f"<td style='text-align:{'left' if i < right_from else 'right'}'>{c}</td>"
                                  for i, c in enumerate(r)) + "</tr>" for r in rows)
    return f"<div class='tw'><table><tr>{th}</tr>{tb}</table></div>"


CSS = f"""
:root{{color-scheme:dark}}
body{{margin:0;background:{NAVY};color:{WHITE};font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}}
.wrap{{max-width:980px;margin:0 auto;padding:16px}}
.hero{{background:linear-gradient(135deg,#0e2244,#0b1a33);border:1px solid {GOLD};border-left:5px solid {GOLD};
      border-radius:12px;padding:16px 18px;margin-bottom:14px}}
h1{{margin:0;color:{GOLD};font-size:1.45rem}} h2{{color:{GOLD};font-size:1.1rem;margin:22px 0 8px;
      border-bottom:2px solid {GOLD};padding-bottom:4px}} h3{{margin:12px 0 6px;font-size:1rem}}
.m{{color:{MUTED}}} .g{{color:{GREEN}}} .r{{color:{RED}}} .y{{color:{GOLD}}}
.tiles{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px;margin:10px 0}}
.tile{{background:{PANEL};border:1px solid {GRID};border-radius:10px;padding:10px 12px}}
.tile b{{display:block;font-size:1.35rem}} .tile span{{color:{MUTED};font-size:.8rem}}
.card{{background:{PANEL};border:1px solid {GRID};border-radius:10px;padding:12px 14px;margin:8px 0}}
.bad{{border-left:4px solid {RED}}} .good{{border-left:4px solid {GREEN}}} .warn{{border-left:4px solid {GOLD}}}
.tw{{overflow-x:auto}} table{{width:100%;border-collapse:collapse;font-size:.85rem}}
th{{color:{GOLD};font-weight:600;border-bottom:1px solid {GOLD};padding:5px 6px;white-space:nowrap}}
td{{border-bottom:1px solid {GRID};padding:5px 6px;white-space:nowrap}}
.chk{{display:flex;gap:8px;align-items:baseline;padding:4px 0;border-bottom:1px solid {GRID}}}
.pill{{font-weight:700;border-radius:6px;padding:1px 8px;font-size:.8rem}}
.ok{{background:#14532d;color:#bbf7d0}} .no{{background:#7f1d1d;color:#fecaca}}
li{{margin:4px 0}} .foot{{color:{MUTED};font-size:.8rem;margin-top:24px}}
"""


def chk_html(items) -> str:
    return "".join(f"<div class='chk'><span class='pill {'ok' if ok else 'no'}'>{'✔ PASS' if ok else '✘ FAIL'}</span>"
                   f"<span>{esc(lab)}</span><span class='m' style='margin-left:auto'>{esc(val)}</span></div>"
                   for lab, ok, val in items)


# ═══════════════════════════════════════════════════════════════════════════
# 4 · report
# ═══════════════════════════════════════════════════════════════════════════
def build(df: pd.DataFrame | None, paper: pd.DataFrame) -> str:
    P = []
    if df is not None and len(df):
        d = days(df)
        o = overnight(df)
        tot = dict(gross=df.gross.sum(), ch=df.charges.sum(), net=df.net.sum(), orders=df.orders.sum(),
                   brok=df.Brokerage.sum(), stt=df.STT.sum(), txn=df["Txn. Charges"].sum(), gst=df.GST.sum(),
                   sebi=df["SEBI Fees"].sum(), stamp=df["Stamp Duty"].sum(),
                   turn=df["Buy Value"].sum() + df["Sell Value"].sum())
        last = d.index[-1]
        L = d.loc[last]
        held = bool(len(o) and (o["bought"].dt.normalize() == last).any()) or bool(
            df[df.Date.dt.normalize() == last].eval("`Buy Qty.` != `Sell Qty.`").any())
        P.append(f"<div class='hero'><h1>📊 Trading Report — {last:%a %d %b %Y}</h1>"
                 f"<div class='m'>Dhan F&amp;O · {d.index[0]:%d %b %Y} → {last:%d %b %Y} · {len(d)} trading days · "
                 f"{int(tot['orders']):,} orders</div></div>")
        # ── latest day ─────────────────────────────────────────────────────
        dl = df[df.Date.dt.normalize() == last]
        P.append(f"<h2>Latest day · {last:%d %b}</h2><div class='tiles'>"
                 f"<div class='tile'><span>Net P&amp;L</span><b class='{cls(L.net)}'>{money(L.net)}</b></div>"
                 f"<div class='tile'><span>Gross (before charges)</span><b class='{cls(L.gross)}'>{money(L.gross)}</b></div>"
                 f"<div class='tile'><span>Charges</span><b class='r'>−₹{L.charges:,.0f}</b></div>"
                 f"<div class='tile'><span>Orders</span><b>{int(L.orders)}</b></div></div>")
        P.append("<div class='card'><h3>Rules scorecard</h3>" + chk_html(scorecard(L, held)) + "</div>")
        rows = [[esc(r["Scrip Name"].replace("OPT NIFTY ", "")), int(r["Buy Qty."]), int(r["orders"]),
                 f"<span class='{cls(r.gross)}'>{money(r.gross)}</span>", f"₹{r.charges:,.0f}",
                 f"<b class='{cls(r.net)}'>{money(r.net)}</b>"] for _, r in dl.sort_values("net").iterrows()]
        P.append(table(rows, ["Contract", "Qty", "Orders", "Gross", "Charges", "Net"]))
        P.append(f"<div class='m' style='margin-top:6px'>Charges today: brokerage ₹{L.brokerage:,.0f} · STT ₹{L.stt:,.0f} · "
                 f"NSE txn ₹{L.txn:,.0f} · GST ₹{L.gst:,.0f}</div>")
        # ── period ─────────────────────────────────────────────────────────
        share = 100 * tot["ch"] / -tot["net"] if tot["net"] < 0 else 0
        P.append(f"<h2>Whole period · where the money went</h2><div class='tiles'>"
                 f"<div class='tile'><span>Net P&amp;L</span><b class='{cls(tot['net'])}'>{money(tot['net'])}</b></div>"
                 f"<div class='tile'><span>Gross (market)</span><b class='{cls(tot['gross'])}'>{money(tot['gross'])}</b></div>"
                 f"<div class='tile'><span>Charges</span><b class='r'>−₹{tot['ch']:,.0f}</b><span>{share:.0f}% of the loss</span></div>"
                 f"<div class='tile'><span>Green / red days</span><b>{int((d.net > 0).sum())} / {int((d.net < 0).sum())}</b></div></div>")
        crow = [("Brokerage (₹20 × orders)", tot["brok"]), ("STT", tot["stt"]), ("GST", tot["gst"]),
                ("NSE txn + IPFT", tot["txn"]), ("Stamp duty", tot["stamp"]), ("SEBI", tot["sebi"])]
        P.append(table([[esc(a), f"₹{b:,.0f}", f"{100 * b / tot['ch']:.1f}%"] for a, b in crow] +
                       [["<b>Total charges</b>", f"<b>₹{tot['ch']:,.0f}</b>",
                         f"{100 * tot['ch'] / tot['turn']:.3f}% of turnover"]], ["Charge", "Amount", "Share"]))
        P.append("<h3>Net P&amp;L per day</h3>" + bars_svg(d, "net", "Net P&L per day"))
        P.append("<h3>Cumulative net P&amp;L</h3>" + line_svg(d, "cum", "Cumulative net P&L"))
        # findings
        ex = d.groupby("expday")[["gross", "charges", "net"]].sum()
        exn = d.groupby("expday").size()
        b = pd.cut(d.orders, [0, 6, 25, 50, 100, 10_000], labels=["≤6", "7–25", "26–50", "51–100", ">100"])
        ob = d.groupby(b, observed=True).agg(days=("net", "size"), avg_net=("net", "mean"), total=("net", "sum"))
        mon = df.groupby(df.Date.dt.strftime("%b %Y"), sort=False).agg(lots=("Buy Qty.", lambda v: v.sum() / LOT),
                                                                      days=("Date", "nunique"), net=("net", "sum"))
        sd = df[df["Buy Qty."] == df["Sell Qty."]]
        aw, al = sd.net[sd.net > 0].mean(), sd.net[sd.net < 0].mean()
        d2 = d.assign(prev=d.net.shift())
        side = df.groupby("side")[["gross", "charges", "net"]].sum()
        P.append("<h2>What caused the losses</h2>")
        if True in ex.index and False in ex.index:
            P.append(f"<div class='card bad'><b>1 · Expiry Tuesdays</b> — {exn[True]} days: gross {money(ex.loc[True, 'gross'])}, "
                     f"net {money(ex.loc[True, 'net'])}. All other {exn[False]} days together: gross only "
                     f"{money(ex.loc[False, 'gross'])} — the market losses happened on expiry days.</div>")
        P.append(f"<div class='card bad'><b>2 · Over-trading → charges</b> — {int(tot['orders']):,} orders in {len(d)} days "
                 f"(≈{tot['orders'] / len(d):.0f}/day) cost ₹{tot['ch']:,.0f}: ≈ ₹{tot['ch'] / tot['orders']:.0f} per order. "
                 f"On non-expiry days you were near breakeven before charges; charges alone took ₹{ex.loc[False, 'charges']:,.0f} there."
                 + table([[esc(str(k)), int(r.days), money(r.avg_net), money(r.total)] for k, r in ob.iterrows()],
                         ["Orders/day", "Days", "Avg net/day", "Total net"]) + "</div>")
        P.append("<div class='card bad'><b>3 · Size grew while losing</b>"
                 + table([[esc(k), f"{r.lots / r.days:.1f}", int(r.days), money(r.net)] for k, r in mon.iterrows()],
                         ["Month", "Lots/day", "Days", "Net"]) + "</div>")
        P.append(f"<div class='card bad'><b>4 · Losers bigger than winners</b> — strike-days won {100 * (sd.net > 0).mean():.0f}% "
                 f"of the time; average win {money(aw)} vs average loss {money(al)} ({abs(al / aw):.1f}× bigger). "
                 f"Losses were held, profits were cut.</div>")
        if len(o):
            P.append(f"<div class='card warn'><b>5 · Overnight holds</b> — {len(o)} carried positions, gross {money(o.gross.sum())}"
                     + table([[esc(r.contract.replace('OPT NIFTY ', '')), f"{r.bought:%d %b}→{r.sold:%d %b}", int(r.qty),
                               f"{r.buy:.2f}→{r.sell:.2f}", f"<span class='{cls(r.gross)}'>{money(r.gross)}</span>",
                               "to expiry" if r.to_expiry else ""] for r in o.itertuples()],
                             ["Contract", "Held", "Qty", "Price", "Gross", ""]) + "</div>")
        if (d2.prev < 0).any() and (d2.prev > 0).any():
            P.append(f"<div class='card warn'><b>6 · After a red day</b> — next day you placed {d2[d2.prev < 0].orders.mean():.0f} "
                     f"orders on average (vs {d2[d2.prev > 0].orders.mean():.0f} after a green day): trying to win it back.</div>")
        P.append("<div class='card warn'><b>7 · CE vs PE</b>" + table(
            [[s, money(r.gross), f"₹{r.charges:,.0f}", money(r.net)] for s, r in side.iterrows()],
            ["Side", "Gross", "Charges", "Net"]) + "<span class='m'>Calls lost most in a falling market — "
            "buying against the trend.</span></div>")
        # charge formula
        P.append("<h2>How Dhan charges each order (verified from your contract notes)</h2>" + table([
            ["Brokerage", "₹20 per executed order (buy or sell)", "both"],
            ["STT", "0.15% of premium (since 1 Apr 2026; was 0.10%)", "sell only"],
            ["NSE txn + IPFT", "0.03553% of premium (0.03503% NSE + 0.0005% IPFT)", "both"],
            ["SEBI fee", "₹10 per crore (0.0001%)", "both"],
            ["Stamp duty", "0.003% of premium", "buy only"],
            ["GST", "18% of (brokerage + txn + SEBI)", "both"]], ["Charge", "Rate", "Leg"], right_from=9))
        ex1 = 150 * LOT
        b1 = TARIFF["brokerage"] + ex1 * TARIFF["txn"] + ex1 * TARIFF["sebi"]
        buy = b1 * (1 + TARIFF["gst"]) + ex1 * TARIFF["stamp_buy"]
        sell = b1 * (1 + TARIFF["gst"]) + ex1 * TARIFF["stt_sell"]
        P.append(f"<div class='m'>Example, 1 lot (65) at ₹150: buy ≈ ₹{buy:.2f}, sell ≈ ₹{sell:.2f} → round trip ≈ "
                 f"₹{buy + sell:.0f} = {(buy + sell) / LOT:.2f} pts of premium just to break even.</div>")
    # ── paper desk ─────────────────────────────────────────────────────────
    pr = paper_readiness(paper)
    P.append("<h2>Paper Desk · ready for real money?</h2>")
    if pr:
        pd_ = pr["days"].tail(10)
        P.append(f"<div class='card {'good' if pr['ready'] else 'bad'}'><b>{'✔ READY — start with 1 lot, same rules' if pr['ready'] else '✘ NOT YET — keep paper trading'}</b>"
                 + chk_html(pr["checks"]) + "</div>")
        P.append(table([[f"{k:%a %d %b}", int(r.trades), int(r.orders), f"₹{r.charges:,.0f}",
                         f"<b class='{cls(r.net)}'>{money(r.net)}</b>"] for k, r in pd_.iloc[::-1].iterrows()],
                       ["Day", "Trades", "≈Orders", "Charges", "Net"]))
    else:
        P.append("<div class='card'>No paper trades found in data/paper_trading/trades.csv yet.</div>")
    P.append("<h2>Rules for the next phase</h2><ol>"
             f"<li><b>Max {RULES['max_orders']} orders a day</b> (3 round trips). Charges fall from ≈ ₹1,700/day to ≈ ₹220.</li>"
             f"<li><b>Stop for the day at −₹{RULES['daily_loss']:,.0f} net.</b> Two red days in a row → next day paper only.</li>"
             f"<li><b>{RULES['max_lots']} lot per order</b> until the paper checklist passes; never add size after a loss.</li>"
             "<li><b>No trading on expiry Tuesday</b> — that is where the market losses came from.</li>"
             "<li><b>Flat at 15:15</b> — no option buys carried overnight, never into expiry.</li>"
             "<li><b>Stop-loss and target set at entry</b>; target ≥ 1.5× the stop. Exit losers at the stop — no averaging down.</li>"
             "<li><b>Trade with the trend</b> — no calls below a falling 20-EMA, no puts above a rising one.</li></ol>")
    P.append("<div class='foot'>Generated by trade_report.py · personal details from the Dhan file are not included · "
             "information only, not financial advice.</div>")
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,"
            f"initial-scale=1'><title>Trading Report</title><style>{CSS}</style></head><body><div class='wrap'>"
            + "".join(P) + "</div></body></html>")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dhan", help="Dhan Global Transaction CSV (default: newest in data/dhan_reports/ or here)")
    ap.add_argument("--paper-only", action="store_true")
    ap.add_argument("--out", help="output HTML path")
    a = ap.parse_args()
    df = None
    if not a.paper_only:
        f = Path(a.dhan) if a.dhan else newest_dhan()
        if f and f.exists():
            df = load_dhan(f)
            print(f"Dhan: {f.name} · {len(df)} rows")
        else:
            print("no Dhan CSV found — paper-only report")
    paper = load_paper()
    out = Path(a.out) if a.out else DATA / "reports" / f"trade_report_{date.today():%Y-%m-%d}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build(df, paper), encoding="utf-8")
    print(f"report → {out}")


if __name__ == "__main__":
    main()
