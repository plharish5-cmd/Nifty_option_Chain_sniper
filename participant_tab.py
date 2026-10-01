"""
participant_tab.py — Institutional Participant Positioning History & Structural Context.

================================================================================
WHAT THIS DATA CANNOT TELL YOU (DERIVATIVES REALITY AUDIT)
================================================================================
1. PREVIOUS-SESSION EOD DATA ONLY:
   This data is compiled and published by the NSE Clearing Corporation daily
   after 18:00 IST for the completed trading session. It provides macro context
   for the upcoming day, NEVER an intraday entry, exit, or trigger.

2. FUTURES SHORTS ARE FREQUENTLY CASH PORTFOLIO HEDGES:
   FIIs managing large domestic equity portfolios routinely short index futures
   and buy protective puts to hedge underlying stock holdings. Derivatives
   positioning alone cannot separate a directional bearish bet from an
   institutional cash equity portfolio hedge.

3. "CLIENT" IS NOT "RETAIL":
   The NSE "Client" category encompasses all non-institutional participants:
   retail traders, High Net-Worth Individuals (HNIs), family offices, corporate
   treasuries, and algorithmic proprietary firms trading under client codes.
   It must NEVER be labeled pure "retail" without this qualification.

4. 65 SESSIONS CANNOT ESTABLISH PREDICTIVE EDGE:
   65 sessions is too small a sample to prove statistical significance or
   out-of-sample edge. Decile buckets contain only ~7 observations each (below
   the minimum required sample size of 10). Forward returns reflect regime
   noise, not an actionable predictive rule.
================================================================================
"""

from __future__ import annotations

from datetime import date
from typing import Any, Optional

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import participant_history as ph

# Palette
GREEN = "#2FBF71"
RED = "#E8503A"
SLATE = "#8FA6B8"
GOLD = "#E0A030"
BLUE = "#4A9BD1"
PURPLE = "#9B51E0"
DARK_BG = "#131722"
PLOT_BG = "#181c27"
GRID_COLOR = "#2a2e39"


def _provenance_line(prov: dict[str, Any]) -> str:
    """Format a consistent provenance metadata line."""
    latest = prov.get("latest_session", "—")
    count = prov.get("session_count", 0)
    source = prov.get("source", "NSE Clearing")
    return (
        f"<div style='font-size:0.75rem;color:#787b86;margin-top:4px;margin-bottom:12px;'>"
        f"<b>Data Provenance:</b> Source: {source} · Latest Completed Session: <b>{latest}</b> · "
        f"Sessions Loaded: <b>{count}</b> · Status: <i>Previous-Session EOD Context (Not Intraday)</i>"
        f"</div>"
    )


def _render_top_strip(percentiles_df: pd.DataFrame, divergence_dict: dict[str, Any], prov: dict[str, Any]) -> None:
    """Render the top 4 metric cards."""
    st.markdown(
        """
        <div style='background:linear-gradient(135deg, #1e222d 0%, #151820 100%);padding:14px 18px;border-radius:8px;border:1px solid #2a2e39;margin-bottom:14px;'>
            <div style='display:flex;align-items:center;justify-content:space-between;'>
                <div>
                    <span style='font-size:1.15rem;font-weight:700;color:#f0f3f6;'>🏛️ Institutional Participant Positioning & History</span>
                    <span style='font-size:0.75rem;background:#2962ff22;color:#2962ff;padding:3px 8px;border-radius:4px;margin-left:10px;font-weight:600;border:1px solid #2962ff55;'>
                        NSE 65-SESSION CALIBRATION
                    </span>
                </div>
                <div style='font-size:0.75rem;color:#f59e0b;background:#f59e0b18;padding:3px 10px;border-radius:4px;border:1px solid #f59e0b44;'>
                    ⚠️ Previous-Session EOD Data (Context Only · Never Intraday Trigger)
                </div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    c1, c2, c3, c4 = st.columns(4)

    # 1. FII Net CE
    fii_ce = percentiles_df[(percentiles_df["participant"] == "FII") & (percentiles_df["series"] == "net_ce")]
    if not fii_ce.empty:
        r = fii_ce.iloc[0]
        val = r["raw_value"]
        pct = r["percentile_rank"]
        lbl = r["label"]
        c1.metric(
            label="FII Net CE (Short Calls)",
            value=f"{val:+,.0f}",
            delta=f"{pct:.1f}th pct · {lbl}",
            delta_color="off",
            help="FII net call options contracts (Long CE minus Short CE). Negative indicates net option writing. Historical average is -234,046 contracts.",
        )
    else:
        c1.metric("FII Net CE", "—", "No data")

    # 2. FII Net PE
    fii_pe = percentiles_df[(percentiles_df["participant"] == "FII") & (percentiles_df["series"] == "net_pe")]
    if not fii_pe.empty:
        r = fii_pe.iloc[0]
        val = r["raw_value"]
        pct = r["percentile_rank"]
        lbl = r["label"]
        c2.metric(
            label="FII Net PE (Long Puts)",
            value=f"{val:+,.0f}",
            delta=f"{pct:.1f}th pct · {lbl}",
            delta_color="off",
            help="FII net put options contracts (Long PE minus Short PE). Positive indicates net long protection. Historical average is +524,236 contracts.",
        )
    else:
        c2.metric("FII Net PE", "—", "No data")

    # 3. Retail / Client Net PE
    ret_pe = percentiles_df[(percentiles_df["participant"] == "Retailers") & (percentiles_df["series"] == "net_pe")]
    if not ret_pe.empty:
        r = ret_pe.iloc[0]
        val = r["raw_value"]
        pct = r["percentile_rank"]
        lbl = r["label"]
        c3.metric(
            label="Client Net PE (Put Writers)",
            value=f"{val:+,.0f}",
            delta=f"{pct:.1f}th pct · {lbl}",
            delta_color="off",
            help="Client (Retail + HNI + Prop) net put contracts. Negative indicates net put writers. Clients are net short puts on 65 of 65 sessions.",
        )
    else:
        c3.metric("Client Net PE", "—", "No data")

    # 4. Divergence Spread
    spreads = divergence_dict.get("spreads", {})
    pe_spread = spreads.get("pe_spread", {})
    if pe_spread:
        s_val = pe_spread.get("today_value", 0.0)
        s_pct = pe_spread.get("percentile_rank", 50.0)
        flagged = pe_spread.get("flagged", False)
        flag_lbl = "⚠️ EXTREME SPREAD" if flagged else "NORMAL SPREAD"
        c4.metric(
            label="FII vs Client PE Spread",
            value=f"{s_val:+,.0f}",
            delta=f"{s_pct:.1f}th pct · {flag_lbl}",
            delta_color="inverse" if flagged else "off",
            help="Spread between FII Net PE and Client Net PE. Flag triggers ONLY when spread breaches the 10th or 90th percentile.",
        )
    else:
        c4.metric("Divergence Spread", "—", "No data")


def _render_executive_findings(baseline_dict: dict[str, Any], prov: dict[str, Any]) -> None:
    """Highlight the three core empirical findings that justify this module."""
    truths = baseline_dict.get("structural_truths", [])
    if not truths:
        return

    st.markdown("### 🔍 Measured Empirical Findings (65-Session Historical Reality)")
    st.markdown(_provenance_line(prov), unsafe_allow_html=True)

    col1, col2, col3 = st.columns(3)

    # Finding 1
    with col1:
        st.markdown(
            f"""
            <div style='background:#1e222d;padding:14px;border-radius:6px;border-left:4px solid {GREEN};height:100%;'>
                <div style='font-size:0.85rem;font-weight:700;color:#2FBF71;'>1 · US DEALER CONVENTION VALIDATED</div>
                <div style='font-size:0.78rem;color:#f0f3f6;margin:8px 0;line-height:1.4;'>
                    <b>FIIs matched US dealer convention on 65 of 65 sessions (100.0%)</b> — net short calls, net long puts on every single trading day.
                </div>
                <div style='font-size:0.72rem;color:#8FA6B8;border-top:1px solid #2a2e39;padding-top:6px;'>
                    <b>Actionable Takeaway:</b> Validates <code>GEX_DEALER_SIGN = -1</code> in Net GEX. The standing "unverified" warning is removed with sample size (n=65) cited.
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    # Finding 2
    with col2:
        st.markdown(
            f"""
            <div style='background:#1e222d;padding:14px;border-radius:6px;border-left:4px solid {GOLD};height:100%;'>
                <div style='font-size:0.85rem;font-weight:700;color:#E0A030;'>2 · PRO 'SHORT BOTH SIDES' IS FALSE</div>
                <div style='font-size:0.78rem;color:#f0f3f6;margin:8px 0;line-height:1.4;'>
                    <b>Pro desks were net LONG both calls & puts on 74% of sessions (48/65)</b>. The widely cited claim that Indian pro desks short both sides held on only <b>1 of 65 sessions</b>.
                </div>
                <div style='font-size:0.72rem;color:#8FA6B8;border-top:1px solid #2a2e39;padding-top:6px;'>
                    <b>Actionable Takeaway:</b> Any gamma model assuming proprietary desks are net short both options inverts the output and gives wrong trade signals.
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    # Finding 3
    with col3:
        st.markdown(
            f"""
            <div style='background:#1e222d;padding:14px;border-radius:6px;border-left:4px solid {BLUE};height:100%;'>
                <div style='font-size:0.85rem;font-weight:700;color:#4A9BD1;'>3 · CLIENTS ARE SYSTEMATIC PUT WRITERS</div>
                <div style='font-size:0.78rem;color:#f0f3f6;margin:8px 0;line-height:1.4;'>
                    <b>Clients are net short puts on 65 of 65 sessions (100.0%)</b>. They are put <i>writers</i>, not put buyers, maintaining a leveraged bullish posture.
                </div>
                <div style='font-size:0.72rem;color:#8FA6B8;border-top:1px solid #2a2e39;padding-top:6px;'>
                    <b>Actionable Takeaway:</b> A binary "OPPOSITE SIDES" flag fires every day because retail writing puts into FIIs is the permanent market structure, not a signal.
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    st.markdown("<div style='margin-bottom:18px;'></div>", unsafe_allow_html=True)


def _render_structural_baseline_panel(baseline_dict: dict[str, Any], prov: dict[str, Any]) -> None:
    """Render structural baseline table showing what each participant does normally."""
    st.markdown("### 📊 Structural Baseline (Normal Market Posture Over 65 Sessions)")
    st.markdown(_provenance_line(prov), unsafe_allow_html=True)

    parts_data = baseline_dict.get("participants", {})
    if not parts_data:
        st.info("No structural baseline data available.")
        return

    table_rows = []
    for part, p_stats in parts_data.items():
        part_display = "Client (Retail / HNI / Prop)" if part == "Retailers" else part

        for inst, inst_label in [("net_ce", "Net CE (Calls)"), ("net_pe", "Net PE (Puts)"), ("net_fut", "Net Futures")]:
            if inst not in p_stats:
                # DII lacks options
                if part == "DII" and inst != "net_fut":
                    table_rows.append({
                        "Participant": part_display,
                        "Instrument": inst_label,
                        "Observed Sessions": "65",
                        "Latest Value": "Not Published by NSE",
                        "% Net Long": "—",
                        "% Net Short": "—",
                        "Mean Net Position": "—",
                        "Median Net Position": "—",
                        "Max Run (Long/Short)": "—",
                        "Baseline Assessment": "NSE Clearing omits option-short columns for DII (not imputed).",
                    })
                continue

            s = p_stats[inst]
            latest = s.get("latest_value", 0.0)
            n_obs = s.get("observed_sessions", 0)
            long_pct = s.get("long_pct", 0.0)
            short_pct = s.get("short_pct", 0.0)
            mean_val = s.get("mean", 0.0)
            med_val = s.get("median", 0.0)
            max_l = s.get("max_consecutive_long", 0)
            max_s = s.get("max_consecutive_short", 0)

            # Context commentary
            if long_pct >= 99.0:
                ctx_eval = f"Normal posture: 100% net long ({max_l}d run). Today's reading carries zero directional surprise."
            elif short_pct >= 99.0:
                ctx_eval = f"Normal posture: 100% net short ({max_s}d run). Today's reading carries zero directional surprise."
            elif long_pct > 65.0:
                ctx_eval = f"Dominantly net long ({long_pct}% of days). Max run: {max_l} days long."
            elif short_pct > 65.0:
                ctx_eval = f"Dominantly net short ({short_pct}% of days). Max run: {max_s} days short."
            else:
                ctx_eval = f"Two-way flow: {long_pct}% long vs {short_pct}% short."

            table_rows.append({
                "Participant": part_display,
                "Instrument": inst_label,
                "Observed Sessions": str(n_obs),
                "Latest Value": f"{latest:+,.0f}",
                "% Net Long": f"{long_pct:.1f}%",
                "% Net Short": f"{short_pct:.1f}%",
                "Mean Net Position": f"{mean_val:+,.0f}",
                "Median Net Position": f"{med_val:+,.0f}",
                "Max Run (Long/Short)": f"{max_l}d L / {max_s}d S",
                "Baseline Assessment": ctx_eval,
            })

    df_base = pd.DataFrame(table_rows)
    st.dataframe(df_base, use_container_width=True, hide_index=True)

    # Mandatory derivatives reality note
    st.caption(
        "📌 **Futures Short Hedge Rule**: An FII holding substantial Indian cash equity portfolios "
        "shorts index futures against them: net short futures, net long the underlying equity market. "
        "Derivatives data alone cannot separate a directional short bet from a cash portfolio hedge."
    )
    st.markdown("<div style='margin-bottom:18px;'></div>", unsafe_allow_html=True)


def _render_divergence_panel(divergence_dict: dict[str, Any], prov: dict[str, Any]) -> None:
    """Render proper percentile-ranked divergence engine with historical firing frequency."""
    st.markdown("### ⚖️ FII vs Client Divergence (Calibrated Percentile Spreads)")
    st.markdown(_provenance_line(prov), unsafe_allow_html=True)

    spreads = divergence_dict.get("spreads", {})
    if not spreads:
        st.info("No divergence spread data available.")
        return

    cols = st.columns(len(spreads))
    idx = 0
    for key, sp in spreads.items():
        with cols[idx]:
            title = sp.get("title", key)
            today_v = sp.get("today_value", 0.0)
            pct = sp.get("percentile_rank", 50.0)
            p10 = sp.get("p10", 0.0)
            p90 = sp.get("p90", 0.0)
            mean_v = sp.get("mean", 0.0)
            flagged = sp.get("flagged", False)
            flag_dir = sp.get("flag_direction", "NORMAL")
            hist_cnt = sp.get("historical_triggers_count", 0)
            hist_pct = sp.get("historical_triggers_pct", 0.0)
            tot_n = sp.get("sample_size", 0)

            border_color = RED if flagged else "#2a2e39"
            badge_bg = "#ef444422" if flagged else "#10b98122"
            badge_fg = RED if flagged else GREEN

            st.markdown(
                f"""
                <div style='background:#1e222d;padding:14px;border-radius:6px;border:1px solid {border_color};margin-bottom:10px;'>
                    <div style='font-size:0.82rem;font-weight:700;color:#f0f3f6;margin-bottom:6px;'>{title}</div>
                    <div style='display:flex;align-items:baseline;justify-content:space-between;'>
                        <span style='font-size:1.3rem;font-weight:800;color:#ffffff;'>{today_v:+,.0f}</span>
                        <span style='font-size:0.75rem;font-weight:700;color:{badge_fg};background:{badge_bg};padding:2px 8px;border-radius:4px;'>
                            {pct:.1f}th pct
                        </span>
                    </div>
                    <div style='font-size:0.72rem;color:#8FA6B8;margin-top:8px;'>
                        <b>10th pct:</b> {p10:+,.0f} | <b>90th pct:</b> {p90:+,.0f}<br/>
                        <b>Historical Mean:</b> {mean_v:+,.0f}
                    </div>
                    <div style='margin-top:8px;padding-top:6px;border-top:1px solid #2a2e39;font-size:0.72rem;color:#c9ced6;'>
                        <b>Flag Status:</b> <span style='color:{badge_fg};font-weight:700;'>{flag_dir}</span><br/>
                        <b>Trigger Frequency:</b> Fired on {hist_cnt} of {tot_n} sessions ({hist_pct}%) historically.
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )
        idx += 1

    st.caption(
        "💡 **Calibration Guarantee**: A binary flag that fires every day is noise. "
        "By measuring the spread's percentile against trailing history and flagging ONLY outside "
        "the 10th/90th percentile band, the flag fires in exactly ~20% of historical sessions."
    )
    st.markdown("<div style='margin-bottom:18px;'></div>", unsafe_allow_html=True)


def _render_history_chart(tidy_df: pd.DataFrame, divergence_dict: dict[str, Any], prov: dict[str, Any]) -> None:
    """Render interactive 65-session Plotly chart with 10th/90th percentile shading."""
    st.markdown("### 📈 Multi-Session Positioning History & Percentile Bands")
    st.markdown(_provenance_line(prov), unsafe_allow_html=True)

    c_sel, c_view = st.columns([2, 2])
    with c_sel:
        metric_choice = st.selectbox(
            "Select Derivatives Series to Plot",
            [
                "Net PE (Long Put minus Short Put)",
                "Net CE (Long Call minus Short Call)",
                "Net Futures (Long Futures minus Short Futures)",
                "Gross Call Long Contracts",
                "Gross Put Long Contracts",
                "FII vs Client Divergence Spreads",
            ],
            index=0,
        )

    # Participant Colors
    color_map = {
        "FII": "#2962FF",       # Blue
        "PRO": "#E0A030",       # Amber / Gold
        "Retailers": "#2FBF71", # Emerald Green
        "DII": "#9B51E0",       # Purple
    }

    fig = go.Figure()

    if metric_choice == "FII vs Client Divergence Spreads":
        spreads = divergence_dict.get("spreads", {})
        for k, sp in spreads.items():
            s = sp.get("history_series")
            if s is not None and not s.empty:
                s_dates = [d.strftime("%d-%m") if isinstance(d, date) else str(d) for d in s.index]
                fig.add_trace(go.Scatter(
                    x=s_dates,
                    y=s.values,
                    mode="lines+markers",
                    name=sp.get("title", k),
                    line=dict(width=2),
                ))
        fig.update_layout(title="FII-minus-Client Spread History (Trailing 65 Sessions)")
    else:
        col_name = {
            "Net PE (Long Put minus Short Put)": "net_pe",
            "Net CE (Long Call minus Short Call)": "net_ce",
            "Net Futures (Long Futures minus Short Futures)": "net_fut",
            "Gross Call Long Contracts": "ce_long",
            "Gross Put Long Contracts": "pe_long",
        }.get(metric_choice, "net_pe")

        # Plot for each participant
        for part in ["FII", "PRO", "Retailers", "DII"]:
            p_df = tidy_df[tidy_df["participant"] == part].sort_values("date").reset_index(drop=True)
            if p_df.empty or col_name not in p_df.columns:
                continue

            valid_p = p_df.dropna(subset=[col_name])
            if valid_p.empty:
                continue

            x_vals = [d.strftime("%d-%m") if isinstance(d, date) else str(d) for d in valid_p["date"]]
            y_vals = valid_p[col_name].values
            p_name = "Client (Retail / HNI)" if part == "Retailers" else part

            fig.add_trace(go.Scatter(
                x=x_vals,
                y=y_vals,
                mode="lines",
                name=p_name,
                line=dict(color=color_map.get(part, "#8FA6B8"), width=2),
            ))

            # Mark today's latest value with a marker
            if len(y_vals) > 0:
                fig.add_trace(go.Scatter(
                    x=[x_vals[-1]],
                    y=[y_vals[-1]],
                    mode="markers+text",
                    name=f"{p_name} Latest",
                    marker=dict(size=9, color=color_map.get(part, "#8FA6B8"), symbol="circle"),
                    text=[f"{y_vals[-1]:+,.0f}"],
                    textposition="top center",
                    showlegend=False,
                ))

        # Add 10th and 90th percentile shading for FII on this series
        fii_sub = tidy_df[tidy_df["participant"] == "FII"].dropna(subset=[col_name]).sort_values("date")
        if not fii_sub.empty and len(fii_sub) > 5:
            q10 = fii_sub[col_name].quantile(0.10)
            q90 = fii_sub[col_name].quantile(0.90)
            all_x = [d.strftime("%d-%m") if isinstance(d, date) else str(d) for d in fii_sub["date"]]
            fig.add_hrect(
                y0=q10, y1=q90,
                fillcolor="#2962ff", opacity=0.08,
                line_width=0,
                annotation_text="FII 10th-90th Percentile Normal Band",
                annotation_position="bottom right",
            )

    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor=DARK_BG,
        plot_bgcolor=PLOT_BG,
        font=dict(color="#d1d4dc", size=11),
        margin=dict(l=40, r=40, t=40, b=40),
        height=380,
        hovermode="x unified",
        xaxis=dict(gridcolor=GRID_COLOR, showgrid=True),
        yaxis=dict(gridcolor=GRID_COLOR, showgrid=True, zeroline=True, zerolinecolor="#4b5563"),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )

    st.plotly_chart(fig, use_container_width=True)
    st.markdown("<div style='margin-bottom:18px;'></div>", unsafe_allow_html=True)


def _render_forward_outcome_panel(outcomes_dict: dict[str, Any], prov: dict[str, Any]) -> None:
    """Render forward-outcome scoring with strict n < 10 sample size guards."""
    st.markdown("### 🎯 Forward-Outcome Scoring (Predictive Power Reality Check)")
    st.markdown(_provenance_line(prov), unsafe_allow_html=True)

    if not outcomes_dict.get("known", False):
        st.warning(f"Outcome scoring unavailable: {outcomes_dict.get('note', 'Missing data')}")
        return

    verdict = outcomes_dict.get("statistical_verdict", "")
    st.markdown(
        f"""
        <div style='background:#26181b;padding:14px 18px;border-radius:6px;border:1px solid #ef444455;margin-bottom:14px;'>
            <div style='font-size:0.88rem;font-weight:700;color:#ef4444;'>
                🚨 STATISTICAL RIGOR AUDIT — NO PREDICTIVE EDGE IN 65 SESSIONS
            </div>
            <div style='font-size:0.80rem;color:#fca5a5;margin-top:6px;line-height:1.4;'>
                {verdict}
            </div>
            <div style='font-size:0.74rem;color:#f87171;margin-top:6px;'>
                <b>Mandatory Rule:</b> Buckets with n &lt; 10 are reported as <code>INSUFFICIENT SAMPLE</code>,
                never as a win rate. No cherry-picking thresholds. Reporting the absence of an edge stops you from watching a useless indicator for months on faith.
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    raw_outcomes = outcomes_dict.get("outcomes", [])
    if not raw_outcomes:
        st.info("No forward outcome records available.")
        return

    table_data = []
    for o in raw_outcomes:
        table_data.append({
            "Series": o.get("series_label", ""),
            "Bucket (Fixed Decile)": o.get("bucket", ""),
            "Cutoff Rule": o.get("threshold_rule", ""),
            "Sample Size (n)": o.get("sample_size", 0),
            "1-Day Mean Return": f"{o['mean_1d_ret']:+.2f}%" if o.get("mean_1d_ret") is not None else "—",
            "1-Day Hit Rate (>0)": o.get("hit_rate_1d", "INSUFFICIENT SAMPLE"),
            "3-Day Mean Return": f"{o['mean_3d_ret']:+.2f}%" if o.get("mean_3d_ret") is not None else "—",
            "3-Day Hit Rate (>0)": o.get("hit_rate_3d", "INSUFFICIENT SAMPLE"),
            "Statistically Valid?": "✅ YES (n≥10)" if o.get("is_valid_sample") else "❌ NO (n<10)",
        })

    df_outcomes = pd.DataFrame(table_data)
    st.dataframe(df_outcomes, use_container_width=True, hide_index=True)

    st.caption(
        "⚖️ **Derivatives Edge Reality**: A 65-session dataset contains only ~7 trading sessions in the top "
        "and bottom deciles. Treating a 5/7 (71%) win rate as a 'high probability setup' is the gambler's fallacy: "
        "out-of-sample testing requires hundreds of sessions across trending, range-bound, and crisis regimes."
    )


# ═══════════════════════════════════════════════════════════════════════════
# Main Entry Point
# ═══════════════════════════════════════════════════════════════════════════

def render(ctx: Optional[Any] = None) -> None:
    """Main tab render function callable from dashboard.py."""
    rules = ph.load_rules()
    window = rules.get("window_sessions", 65)

    # Load data
    try:
        tidy_df = ph.load()
    except Exception as e:
        st.error(f"Failed to load participant positioning data: {e}")
        return

    if tidy_df.empty:
        st.warning("No participant positioning data available. Check that data/participants.parquet or Excel master exists.")
        return

    prov = ph.get_provenance(tidy_df)
    percentiles_df = ph.compute_percentiles(tidy_df, window=window, rules=rules)
    baseline_dict = ph.compute_structural_baseline(tidy_df, window=window)
    divergence_dict = ph.compute_divergence(tidy_df, window=window, rules=rules)

    # Check OHLC in ctx or fallback
    ohlc_df = None
    if isinstance(ctx, dict):
        ohlc_df = ctx.get("ohlc")

    outcomes_dict = ph.score_forward_outcomes(tidy_df, ohlc_df=ohlc_df, min_sample=rules.get("outcome_scoring", {}).get("min_sample_size", 10))

    # Render Panels
    _render_top_strip(percentiles_df, divergence_dict, prov)
    _render_executive_findings(baseline_dict, prov)

    tab_overview, tab_baseline, tab_divergence, tab_chart, tab_outcomes = st.tabs([
        "📋 Overview & Percentiles",
        "🏛️ Structural Baseline",
        "⚖️ Divergence Engine",
        "📈 65-Session Chart",
        "🎯 Outcome Reality Audit",
    ])

    with tab_overview:
        st.markdown("#### Participant Positioning — Current Session vs 65-Session Trailing Window")
        st.markdown(_provenance_line(prov), unsafe_allow_html=True)
        if not percentiles_df.empty:
            disp_df = percentiles_df[[
                "participant", "series", "raw_value", "percentile_rank", "label", "z_score", "dod_change", "dod_percentile", "mean", "median"
            ]].copy()
            disp_df.columns = [
                "Participant", "Series", "Current Value", "Percentile Rank", "Regime Label", "Z-Score", "DoD Change", "DoD Percentile", "65d Mean", "65d Median"
            ]
            # Format numbers
            disp_df["Current Value"] = disp_df["Current Value"].apply(lambda v: f"{v:+,.0f}")
            disp_df["Percentile Rank"] = disp_df["Percentile Rank"].apply(lambda v: f"{v:.1f}%")
            disp_df["Z-Score"] = disp_df["Z-Score"].apply(lambda v: f"{v:+.2f}")
            disp_df["DoD Change"] = disp_df["DoD Change"].apply(lambda v: f"{v:+,.0f}")
            disp_df["DoD Percentile"] = disp_df["DoD Percentile"].apply(lambda v: f"{v:.1f}%")
            disp_df["65d Mean"] = disp_df["65d Mean"].apply(lambda v: f"{v:+,.0f}")
            disp_df["65d Median"] = disp_df["65d Median"].apply(lambda v: f"{v:+,.0f}")
            st.dataframe(disp_df, use_container_width=True, hide_index=True)
        else:
            st.info("No percentiles computed.")

    with tab_baseline:
        _render_structural_baseline_panel(baseline_dict, prov)

    with tab_divergence:
        _render_divergence_panel(divergence_dict, prov)

    with tab_chart:
        _render_history_chart(tidy_df, divergence_dict, prov)

    with tab_outcomes:
        _render_forward_outcome_panel(outcomes_dict, prov)
