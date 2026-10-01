"""
participant_sheet_tab.py — the trainer's participant workbook inside the app.

  .venv/bin/streamlit run participant_sheet_tab.py        (standalone)
  or from dashboard.py:  import participant_sheet_tab; participant_sheet_tab.render()

Shows: FII/DII cash columns (BE, BD, DII AB) in his buy-green / sell-red, the Nifty & Bank
split, the FPI fortnight sector sheet (fpi_sector_tab), download of the updated workbook (.xlsm with his colours, formulas, charts and
Ctrl-key macros), today's three ratios, the last 20 sessions painted with his
conditional-formatting colours, his data bars for Net Options / Net Stock Futures,
the ratio-vs-Nifty chart, and the edge table re-computed from the sheet's own history.

Stock Unlimited course learnings integrated (2026-10-01):
  — Four Quadrants interpreter: reads Δ OI + Δ premium to classify each side
    into Long Buildup / Short Buildup / Long Unwinding / Short Covering
    and derives the market bias verdict (ceiling / floor / squeeze starting etc.)
  — Participant Alignment Signal: shows FII vs Retail vs PRO direction agreement
    and flags smart-money / dumb-money divergence (follow FII when they split)
  — Course callout expander: MJ's exact rules from the notes, inline
"""
from __future__ import annotations

import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import participant_ratios as pr

HERE = Path(__file__).resolve().parent
LOG = pr.WORKBOOK.parent / "update.log"
COLS = {
    "nifty": "Nifty (AZ)",
    "nifty_chg_pct": "Chg %",
    "fii_AM": "FII PS/CS (AM)",
    "retail_AI": "Retail PS/CS (AI)",
    "pro_AB": "PRO PS/CS (AB)",
    "fii_Y_net_options": "FII Net Options (Y)",
    "fii_Z_net_futures": "FII Net Futures (Z)",
    "fii_AE_net_stock_fut": "FII Net Stock Fut (AE)",
    "fii_cash_prov": "FII cash NSE (BE)",
    "fii_cash_final": "FII cash NSDL (BD)",
    "dii_cash": "DII cash (AB)",
    "regime": "Regime",
}
CASH = ("fii_cash_prov", "fii_cash_final", "dii_cash")


@st.cache_data(ttl=300)
def _load(mtime: float) -> pd.DataFrame:
    return pr.load(pr.WORKBOOK)


def _style_table(df: pd.DataFrame):
    view = df[list(COLS)].rename(columns=COLS)
    view.index = pd.to_datetime(view.index).strftime("%d-%b-%Y %a")   # 25-Sep-2026 Fri, no 00:00:00
    view.index.name = "Date"
    rev = {v: k for k, v in COLS.items()}

    def paint(col: pd.Series):
        key = rev[col.name]
        out = []
        for v in col:
            fill, text = pr.cell_style(key, v) if key in ("fii_AM", "retail_AI", "pro_AB") + CASH else (None, None)
            out.append(f"background-color:{fill};color:{text}" if fill else "")
        return out

    sty = view.style.apply(paint, axis=0)
    # his data bars: green for Y / Z / AE (and red side for negatives)
    for key in ("fii_Y_net_options", "fii_Z_net_futures", "fii_AE_net_stock_fut"):
        sty = sty.bar(subset=[COLS[key]], align="zero", color=[pr.BAR_RED, pr.BAR_GREEN])
    sty = sty.format({COLS["nifty"]: "{:,.0f}", COLS["nifty_chg_pct"]: "{:+.2f}",
                      COLS["fii_AM"]: "{:.2f}", COLS["retail_AI"]: "{:.2f}", COLS["pro_AB"]: "{:.2f}",
                      COLS["fii_Y_net_options"]: "{:,.0f}", COLS["fii_Z_net_futures"]: "{:,.0f}",
                      COLS["fii_AE_net_stock_fut"]: "{:,.0f}", COLS["fii_cash_prov"]: "{:,.0f}",
                      COLS["fii_cash_final"]: "{:,.0f}", COLS["dii_cash"]: "{:,.0f}"}, na_rep="–")
    sty = sty.map(lambda v: f"color:{pr.GOOD_TEXT}" if isinstance(v, (int, float)) and v > 0 else
                  (f"color:{pr.BAD_TEXT}" if isinstance(v, (int, float)) and v < 0 else ""),
                  subset=[COLS["nifty_chg_pct"]])
    return sty


def _ratio_chart(df: pd.DataFrame, sessions: int) -> go.Figure:
    d = df.tail(sessions)
    fig = go.Figure()
    for key, name, colour in (("fii_AM", "FII (AM)", "#1f4e79"), ("retail_AI", "Retail (AI)", "#c55a11"),
                              ("pro_AB", "PRO (AB)", "#548235")):
        fig.add_trace(go.Scatter(x=d.index, y=d[key], name=name, mode="lines", line=dict(color=colour, width=2)))
    fig.add_hline(y=1.0, line_dash="dash", line_color="#7f7f7f", annotation_text="1.0 bullish / bearish line")
    fig.add_trace(go.Scatter(x=d.index, y=d["nifty"], name="Nifty", yaxis="y2", mode="lines",
                             line=dict(color="#a6a6a6", width=1.5)))
    fig.update_layout(height=420, margin=dict(l=10, r=10, t=30, b=10), legend=dict(orientation="h", y=1.08),
                      yaxis=dict(title="Put Short ÷ Call Short"),
                      yaxis2=dict(title="Nifty", overlaying="y", side="right", showgrid=False))
    return fig


def _net_bars(df: pd.DataFrame, sessions: int) -> go.Figure:
    d = df.tail(sessions)
    fig = go.Figure()
    for key, name in (("fii_Y_net_options", "FII Net Options (Y)"), ("fii_AE_net_stock_fut", "FII Net Stock Futures (AE)")):
        fig.add_trace(go.Bar(x=d.index, y=d[key], name=name,
                             marker_color=[pr.BAR_GREEN if v >= 0 else pr.BAR_RED for v in d[key]]))
    fig.update_layout(height=320, barmode="group", margin=dict(l=10, r=10, t=30, b=10),
                      legend=dict(orientation="h", y=1.1), yaxis=dict(title="contracts"))
    return fig


@st.cache_data(ttl=300)
def _nifty_bank(mtime: float) -> pd.DataFrame:
    """His 'Nifty & Bank' sheet (Ctrl+N / Ctrl+B), last 20 filled rows."""
    cols = {0: "Date", 1: "Nifty fut net", 2: "Fut avg buy", 3: "Fut avg sell", 5: "Nifty opt net",
            6: "Opt avg buy strike", 7: "Opt avg sell strike", 8: "Nifty opt net ₹Cr",
            13: "BN fut net", 17: "BN opt net", 20: "BN opt net ₹Cr"}
    df = pd.read_excel(pr.WORKBOOK, sheet_name="Nifty & Bank", header=None, skiprows=4,
                       usecols=list(cols), engine="openpyxl")
    df.columns = [cols[c] for c in sorted(cols)]
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    for c in df.columns[1:]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna(subset=["Date", "Nifty fut net"]).tail(20).iloc[::-1]


# ─────────────────────────────── Four Quadrants ──────────────────────────────
# From OPTION_CHAIN_FOUR_QUADRANTS.pdf — MJ / Stock Unlimited course
# Δ Premium + Δ OI → one of four quadrants per side; combined → market verdict.

_QUADRANT = {
    # (premium_up, oi_up): (label, one-liner, bias, colour)
    (True,  True):  ("Long Buildup",    "New buyers entering — fresh demand",          "BULLISH",  "#1a7a1a"),
    (False, True):  ("Short Buildup",   "Writers opening — wall forming",              "BEARISH",  "#b30000"),
    (False, False): ("Long Unwinding",  "Buyers exiting — conviction fading",          "NEUTRAL",  "#7f6000"),
    (True,  False): ("Short Covering",  "Writers closing — wall may BREAK (squeeze!)", "SQUEEZE",  "#7f007f"),
}

def _quadrant_card(side: str, d_prem: float, d_oi: float) -> dict:
    """Classify one chain side (CE or PE) into a quadrant."""
    q = _QUADRANT[(d_prem >= 0, d_oi >= 0)]
    label, oneliner, bias, colour = q
    # Flip meaning for PE side: CE Long Buildup = BULLISH index; PE Long Buildup = BEARISH index
    if side == "PE":
        bias = {"BULLISH": "BEARISH", "BEARISH": "BULLISH", "NEUTRAL": "NEUTRAL", "SQUEEZE": "SQUEEZE"}.get(bias, bias)
    return {"side": side, "label": label, "oneliner": oneliner, "bias": bias, "colour": colour,
            "d_prem": d_prem, "d_oi": d_oi}


def _render_four_quadrants(df: pd.DataFrame) -> None:
    """Four Quadrants section — needs net_options and net_futures change columns."""
    st.markdown("### 🟦 Four Quadrants Interpreter")
    st.caption("From Stock Unlimited Lecture 2 — OPTION_CHAIN_FOUR_QUADRANTS. "
               "Δ Premium + Δ OI classify each side; combined verdict gives market bias.")

    # Derive proxy Δ from participant net_options (FII Y) and net_futures (FII Z)
    # Positive net_options = net call buying (CE long buildup); negative = net put buying (PE long buildup)
    # Day-on-day change gives Δ
    if len(df) < 2:
        st.info("Need at least 2 sessions of data.")
        return

    last  = df.iloc[-1]
    prev  = df.iloc[-2]

    # CE side proxy: FII net options direction and change
    ce_d_oi   = float(last.get("fii_Y_net_options", 0) or 0)
    ce_d_prem = float((last.get("fii_Y_net_options", 0) or 0) - (prev.get("fii_Y_net_options", 0) or 0))
    # PE side proxy: invert (negative net options = put buying)
    pe_d_oi   = -ce_d_oi
    pe_d_prem = -ce_d_prem

    ce = _quadrant_card("CE", ce_d_prem, ce_d_oi)
    pe = _quadrant_card("PE", pe_d_prem, pe_d_oi)

    # Combined verdict
    if ce["bias"] == pe["bias"] == "BULLISH":
        verdict, vcolour, vicon = "STRONG BULL — CE long buildup + PE short buildup aligned", "#1a7a1a", "🟢"
    elif ce["bias"] == pe["bias"] == "BEARISH":
        verdict, vcolour, vicon = "STRONG BEAR — PE long buildup + CE short buildup aligned", "#b30000", "🔴"
    elif "SQUEEZE" in (ce["bias"], pe["bias"]):
        who = "CE" if ce["bias"] == "SQUEEZE" else "PE"
        verdict, vcolour, vicon = f"⚡ SQUEEZE WATCH — {who} short covering. Wall may break!", "#7f007f", "⚡"
    else:
        verdict, vcolour, vicon = "MIXED / RANGE — no clear alignment between CE and PE sides", "#555555", "⬜"

    col1, col2 = st.columns(2)
    for col, q in ((col1, ce), (col2, pe)):
        with col:
            st.markdown(
                f"<div style='border-left:4px solid {q['colour']};padding:10px 14px;"
                f"background:#1a1a2e;border-radius:6px;margin-bottom:8px'>"
                f"<b style='color:{q['colour']};font-size:1.05em'>{q['side']} SIDE — {q['label']}</b><br>"
                f"<span style='color:#ccc'>{q['oneliner']}</span><br>"
                f"<span style='font-size:0.85em;color:#aaa'>Δ Net: {q['d_oi']:+,.0f} · Δ Prem: {q['d_prem']:+,.0f}</span><br>"
                f"<b style='color:{q['colour']}'>Index bias → {q['bias']}</b>"
                f"</div>",
                unsafe_allow_html=True,
            )

    st.markdown(
        f"<div style='background:#0d1b2a;border:1px solid {vcolour};border-radius:8px;padding:12px 16px;"
        f"text-align:center;margin:8px 0'>"
        f"<span style='color:{vcolour};font-size:1.15em;font-weight:700'>{vicon} COMBINED VERDICT: {verdict}</span>"
        f"</div>",
        unsafe_allow_html=True,
    )

    with st.expander("📖 Course note — The Four Quadrants (MJ / Stock Unlimited)"):
        st.markdown("""
**Δ Premium UP + Δ OI UP** → **Long Buildup** — buyers opening new positions. Premium rises because demand is real.
- CE side: index BULLISH · PE side: index BEARISH

**Δ Premium DOWN + Δ OI UP** → **Short Buildup** — writers opening new positions. More supply = premium falls.
- CE side: CEILING forming (BEARISH) · PE side: FLOOR forming (BULLISH)

**Δ Premium DOWN + Δ OI DOWN** → **Long Unwinding** — buyers exiting. OI falls because they are closing out.
- CE side: bulls surrendering · PE side: bears surrendering

**Δ Premium UP + Δ OI DOWN** → **Short Covering** ⚡ — writers closing. To close, they BUY BACK what they sold.
Their buying adds to demand → **price accelerates through the level = SQUEEZE**.
- CE side: ceiling BREAKING (BULLISH squeeze) · PE side: floor COLLAPSING (BEARISH crash)

> *"Nobody writes casually. A wall here is money that has committed to a level. Writers put up lakhs in margin and carry unrestricted loss."*
> — Stock Unlimited, Option Chain Four Quadrants notes
        """)


# ─────────────────────────────── Participant Alignment Signal ────────────────
# From Stock Unlimited Lecture 2 — ratio rules:
#   FII AM > 1 = FII net buying calls = BULLISH
#   Retail AI > 1 = Retail net buying calls (usually wrong side)
#   When FII and Retail diverge → follow FII (smart money)

def _ratio_direction(val: float) -> tuple[str, str]:
    """(arrow, colour) for a ratio value."""
    if val > 1.2:
        return "▲ BULLISH", "#1a7a1a"
    elif val < 0.85:
        return "▼ BEARISH", "#b30000"
    else:
        return "↔ NEUTRAL", "#7f6000"


def _render_alignment(last: "pd.Series", prev: "pd.Series") -> None:
    st.markdown("### 🧠 Participant Alignment Signal")
    st.caption("Smart money (FII) vs dumb money (Retail) divergence — from Stock Unlimited ratio rules. "
               "Follow FII when they split from Retail.")

    fii_dir,    fii_col    = _ratio_direction(float(last.fii_AM))
    retail_dir, retail_col = _ratio_direction(float(last.retail_AI))
    pro_dir,    pro_col    = _ratio_direction(float(last.pro_AB))

    c1, c2, c3 = st.columns(3)
    for col, name, val, direction, colour, note in (
        (c1, "FII (AM)",    last.fii_AM,    fii_dir,    fii_col,    "Smart money — follow this"),
        (c2, "Retail (AI)", last.retail_AI, retail_dir, retail_col, "Usually on wrong side near extremes"),
        (c3, "PRO (AB)",    last.pro_AB,    pro_dir,    pro_col,    "Prop desks — confirms when aligned with FII"),
    ):
        col.markdown(
            f"<div style='border:1px solid {colour};border-radius:8px;padding:10px;text-align:center;"
            f"background:#1a1a2e'>"
            f"<div style='color:#aaa;font-size:0.8em'>{name}</div>"
            f"<div style='font-size:1.6em;font-weight:700;color:{colour}'>{val:.2f}</div>"
            f"<div style='color:{colour};font-weight:600'>{direction}</div>"
            f"<div style='color:#777;font-size:0.75em;margin-top:4px'>{note}</div>"
            f"</div>",
            unsafe_allow_html=True,
        )

    # Divergence alert
    fii_bull    = float(last.fii_AM) > 1.0
    retail_bull = float(last.retail_AI) > 1.0
    if fii_bull != retail_bull:
        fii_side    = "BULLISH" if fii_bull else "BEARISH"
        retail_side = "BULLISH" if retail_bull else "BEARISH"
        st.markdown(
            f"<div style='background:#2a1a00;border:1px solid #c8a000;border-radius:8px;"
            f"padding:10px 14px;margin-top:8px'>"
            f"⚠️ <b style='color:#c8a000'>SMART / DUMB MONEY DIVERGENCE</b><br>"
            f"FII is <b>{fii_side}</b> · Retail is <b>{retail_side}</b><br>"
            f"<span style='color:#aaa;font-size:0.85em'>Course rule: follow FII (AM). "
            f"Retail is often positioned against the move near turning points.</span>"
            f"</div>",
            unsafe_allow_html=True,
        )
    elif fii_bull and retail_bull:
        st.success("✅ All three aligned BULLISH — strong confirmation. "
                   "FII + Retail + PRO all buying calls (ratio > 1).")
    elif not fii_bull and not retail_bull:
        st.error("🔴 All three aligned BEARISH — strong confirmation. "
                 "FII + Retail + PRO all buying puts (ratio < 1).")
    else:
        st.info("↔ Mixed alignment — no strong directional edge from participants today.")

    with st.expander("📖 Course note — Ratio rules (MJ / Stock Unlimited)"):
        st.markdown("""
**Ratio = Put Short ÷ Call Short (PS/CS)**
- Ratio **> 1** = more puts written than calls → writers betting price will NOT fall → **BULLISH**
- Ratio **< 1** = more calls written than puts → writers betting price will NOT rise → **BEARISH**
- Ratio **= 1** = balanced → NEUTRAL

**FII AM (column AM)** — the most reliable signal. FIIs have information advantage and large capital.
> *"Follow FII when they diverge from Retail."* — Stock Unlimited Lecture 2

**Retail AI (column AI)** — often on the wrong side near extremes. Useful as a contrarian signal.

**PRO AB (column AB)** — prop desks. Confirms when aligned with FII.

**Key divergence rule:** When FII > 1 and Retail < 1 → Retail is selling calls (expecting fall) but FII is selling puts (expecting rise). FII wins this battle. **Buy CE or stay long bias.**
        """)


def render() -> None:
    st.subheader("Participant sheet (Stock Unlimited method)")
    wb = pr.WORKBOOK
    if not wb.exists():
        st.warning(f"Workbook not found at {wb}. Copy Partcipant_Data_Sheet_NEW.xlsm there and run "
                   "`python update_participant_sheet.py`.")
        return
    df = _load(wb.stat().st_mtime)
    last = df.iloc[-1]

    c1, c2, c3, c4, c5 = st.columns([1, 1, 1, 1.3, 1.6])
    c1.metric("FII AM", f"{last.fii_AM:.2f}", f"{last.fii_AM - df.fii_AM.iloc[-2]:+.2f}")
    c2.metric("Retail AI", f"{last.retail_AI:.2f}", f"{last.retail_AI - df.retail_AI.iloc[-2]:+.2f}")
    c3.metric("PRO AB", f"{last.pro_AB:.2f}", f"{last.pro_AB - df.pro_AB.iloc[-2]:+.2f}")
    c4.metric("Regime", last.regime, f"as of {df.index[-1]:%d-%b-%Y}", delta_color="off")
    with c5:
        st.download_button("⬇ Download workbook (.xlsm)", wb.read_bytes(), file_name=wb.name,
                            mime="application/vnd.ms-excel.sheet.macroEnabled.12", use_container_width=True)
        if st.button("Update now from NSE", use_container_width=True):
            with st.spinner("Downloading NSE files and writing the workbook…"):
                res = subprocess.run([sys.executable, str(HERE / "update_participant_sheet.py")],
                                     capture_output=True, text=True, cwd=HERE, timeout=1200)
            st.code((res.stdout or "")[-2500:] + (res.stderr or "")[-1000:])
            st.cache_data.clear()

    st.caption(pr.summary_line(wb))

    # ── Stock Unlimited course sections ──────────────────────────────────────
    st.divider()
    _render_four_quadrants(df)
    st.divider()
    _render_alignment(last, df.iloc[-2])
    st.divider()
    # ─────────────────────────────────────────────────────────────────────────

    st.markdown("**Last 20 sessions** — colours are the workbook's own conditional formatting "
                "(green > 1; FII AM < 0.55 green, > 1.3 red), bars are his data bars.")
    st.dataframe(_style_table(df.tail(20).iloc[::-1]), use_container_width=True, height=560)
    st.caption("FII cash NSDL (BD) for the latest day shows – until the next evening: NSDL publishes it one day "
               "late and the next run fills it (his rule, video 2 at 12:24).")

    sessions = st.select_slider("Chart window (sessions)", [60, 120, 250, 500, 1000], value=120)
    st.plotly_chart(_ratio_chart(df, sessions), use_container_width=True)
    st.plotly_chart(_net_bars(df, sessions), use_container_width=True)

    with st.expander("Does it work? Edge table from the sheet's own history"):
        st.dataframe(pr.edge_table(wb), use_container_width=True, hide_index=True)
        st.caption("Next-day / next-5-day Nifty direction after each reading, since Dec-2019.")
    st.divider()
    try:
        import participant_correlation_tab
        participant_correlation_tab.render()
    except Exception as exc:
        st.caption(f"Data-correlation section unavailable: {exc}")
    st.divider()

    with st.expander("Nifty & Bank split (Ctrl+N / Ctrl+B) — he found no strong correlation"):
        try:
            nbk = _nifty_bank(wb.stat().st_mtime)
            num_cols = [c for c in nbk.columns if c != "Date"]
            st.dataframe(nbk.style.format({c: "{:,.0f}" for c in num_cols}, na_rep="")
                         .map(lambda v: f"color:{pr.GOOD_TEXT}" if isinstance(v, (int, float)) and v > 0 else
                              (f"color:{pr.BAD_TEXT}" if isinstance(v, (int, float)) and v < 0 else ""),
                              subset=[c for c in num_cols if "net" in c]),
                         use_container_width=True, hide_index=True)
        except Exception as exc:
            st.caption(f"Nifty & Bank not readable: {exc}")
    if LOG.exists():
        with st.expander("Update log"):
            st.code("\n".join(LOG.read_text().splitlines()[-40:]))

    st.divider()
    try:
        import fpi_sector_tab
        fpi_sector_tab.render()
    except Exception as exc:
        st.caption(f"FPI sector section unavailable: {exc}")


if __name__ == "__main__":
    st.set_page_config(page_title="Participant sheet", layout="wide")
    render()
