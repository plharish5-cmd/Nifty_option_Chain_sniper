"""
strike_select_tab.py — Strike Selection & Premium Behaviour Tab for Streamlit.

WHAT THIS TAB DOES:
───────────────────
- Renders the Strike Selection & Premium Behaviour Tab for NIFTY options.
- Visualizes the 24× outcome difference across candidate strikes for an identical spot move.
- Classifies ATM IV into market momentum regimes (<10, 10–20, >20) with buyer/seller verdicts.
- Displays candidate strikes ranked by Return on Capital (ROC %), highlighting the ₹200–₹300 band
  and flagging strikes exceeding 30% of account capital.
- Translates spot target/stop points into account rupees and delta-adjusted Risk:Reward.
- Visualizes calendar-day theta decay (including intraday, overnight, and multi-day weekend decay).
- Restates Open Interest walls into four-participant intent (Call/Put buyers and sellers).

WHAT THIS TAB CANNOT DO:
────────────────────────
- It CANNOT guarantee option outcomes. Delta projections are strictly FIRST-ORDER linear
  approximations. As spot moves, Gamma changes Delta continuously: first-order math
  understates gains on large favourable moves and overstates losses on adverse moves.
- It CANNOT treat IV regime bands (<10, 10–20, >20) or the ₹200–₹300 premium band as proven laws.
  These are one experienced trader's heuristic thresholds; they have NOT been statistically
  backtested or validated on your historical dataset.
- It CANNOT predict volatility shocks or skew shifts. A correct directional call with falling IV
  (IV crush) can lose money even when the index hits target.
- It CANNOT execute or place trades. Alert-only. Never recommends a strike; displays trade-offs
  so the trader can make an informed choice.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import config
from ui_utils import render_html, _render_html
from strike_select import (
    load_rules,
    run_strike_selection,
    StrikeCandidate,
    StrikeSelectionResult,
    ThetaDecayProjection,
    WallIntentAnalysis,
)

# ─────────────────────────────────────────────────────────────────────────────
# 1. Styling Constants
# ─────────────────────────────────────────────────────────────────────────────
GREEN = "#10B981"
RED = "#EF4444"
AMBER = "#F59E0B"
BLUE = "#38BDF8"
PURPLE = "#A855F7"
SLATE = "#94A3B8"
DARK_CARD = "#1E293B"
DARK_BG = "#0F172A"
BORDER = "#334155"


def _provenance_badge(text: str) -> None:
    """Renders a standard provenance indicator line."""
    st.caption(f"📍 *Provenance:* {text}")


# ─────────────────────────────────────────────────────────────────────────────
# 2. Hero Component: The 24x Outcome Reality Check
# ─────────────────────────────────────────────────────────────────────────────
def _render_reality_check_hero(ref_strikes: List[Dict[str, Any]], target_pts: float = 40.0) -> None:
    """
    Displays the dramatic demonstration from the source transcript:
    Three traders with identical directional calls and identical 40-pt targets,
    experiencing outcomes that differ by 24×.
    """
    if not ref_strikes or len(ref_strikes) < 3:
        return

    s1, s2, s3 = ref_strikes[0], ref_strikes[1], ref_strikes[2]

    hero_html = f"""
    <div style="background:{DARK_CARD}; border:1px solid {BORDER}; border-radius:8px; padding:16px; margin-bottom:20px;">
      <div style="display:flex; justify-content:space-between; align-items:center; border-bottom:1px solid {BORDER}; padding-bottom:10px; margin-bottom:12px;">
        <div>
          <span style="font-size:15px; font-weight:700; color:#F8FAFC; text-transform:uppercase; letter-spacing:0.5px;">
            ⚡ The Problem This Tab Solves: The 24× Outcome Gap
          </span>
          <div style="font-size:12px; color:{SLATE}; margin-top:2px;">
            Identical directional read, identical {target_pts:.0f}-point spot target, one lot each — completely divergent account outcomes.
          </div>
        </div>
        <span style="background:rgba(56, 189, 248, 0.15); color:{BLUE}; border:1px solid {BLUE}; padding:3px 8px; border-radius:4px; font-size:11px; font-weight:700;">
          SOURCE LIVE CASE
        </span>
      </div>

      <div style="display:grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap:12px;">
        <!-- Strike 1: Deep ITM -->
        <div style="background:{DARK_BG}; border:1px solid rgba(239, 68, 68, 0.4); border-radius:6px; padding:12px;">
          <div style="display:flex; justify-content:space-between; align-items:center;">
            <span style="font-weight:700; color:#F8FAFC; font-size:13px;">{s1.get('strike'):,} CE</span>
            <span style="color:{RED}; font-size:11px; font-weight:700; background:rgba(239, 68, 68, 0.15); padding:2px 6px; border-radius:3px;">
              CAPITAL STRAIN ({s1.get('capital_pct'):.0f}%)
            </span>
          </div>
          <div style="font-size:11px; color:{SLATE}; margin-top:2px;">{s1.get('label')}</div>
          <div style="margin-top:8px; font-size:12px; color:#CBD5E1; line-height:1.6;">
            • Premium: <b>₹{s1.get('premium'):.0f}</b> | Delta: <b>{s1.get('delta'):.2f}</b><br>
            • On {target_pts:.0f}-pt move: <b>+{s1.get('gain_40pt'):.0f} pts → ₹{s1.get('rupees_40pt'):,.0f}</b><br>
            • Cost of 1 lot: <b>₹{s1.get('lot_cost'):,.0f}</b><br>
            • Return on Capital: <b style="color:{AMBER};">{s1.get('roc_pct'):.1f}%</b>
          </div>
          <div style="margin-top:6px; font-size:10.5px; color:#FCA5A5;">
            ⚠️ Ties up ₹37k capital for intrinsic value you do not need. Exceeds standard capital limit.
          </div>
        </div>

        <!-- Strike 2: Recommended Band -->
        <div style="background:{DARK_BG}; border:2px solid {GREEN}; border-radius:6px; padding:12px;">
          <div style="display:flex; justify-content:space-between; align-items:center;">
            <span style="font-weight:700; color:#F8FAFC; font-size:13px;">{s2.get('strike'):,} CE</span>
            <span style="color:{GREEN}; font-size:11px; font-weight:700; background:rgba(16, 185, 129, 0.15); padding:2px 6px; border-radius:3px;">
              RECOMMENDED BAND (₹200–300)
            </span>
          </div>
          <div style="font-size:11px; color:{SLATE}; margin-top:2px;">{s2.get('label')}</div>
          <div style="margin-top:8px; font-size:12px; color:#CBD5E1; line-height:1.6;">
            • Premium: <b>₹{s2.get('premium'):.0f}</b> | Delta: <b>{s2.get('delta'):.2f}</b><br>
            • On {target_pts:.0f}-pt move: <b>+{s2.get('gain_40pt'):.0f} pts → ₹{s2.get('rupees_40pt'):,.0f}</b><br>
            • Cost of 1 lot: <b>₹{s2.get('lot_cost'):,.0f}</b><br>
            • Return on Capital: <b style="color:{GREEN};">{s2.get('roc_pct'):.1f}%</b>
          </div>
          <div style="margin-top:6px; font-size:10.5px; color:#6EE7B7;">
            ✅ Optimal sweet-spot: sufficient delta, tolerable theta, capital-efficient conversion.
          </div>
        </div>

        <!-- Strike 3: Deep OTM -->
        <div style="background:{DARK_BG}; border:1px solid rgba(245, 158, 11, 0.4); border-radius:6px; padding:12px;">
          <div style="display:flex; justify-content:space-between; align-items:center;">
            <span style="font-weight:700; color:#F8FAFC; font-size:13px;">{s3.get('strike'):,} CE</span>
            <span style="color:{AMBER}; font-size:11px; font-weight:700; background:rgba(245, 158, 11, 0.15); padding:2px 6px; border-radius:3px;">
              DELTA DRAG (24× LESS)
            </span>
          </div>
          <div style="font-size:11px; color:{SLATE}; margin-top:2px;">{s3.get('label')}</div>
          <div style="margin-top:8px; font-size:12px; color:#CBD5E1; line-height:1.6;">
            • Premium: <b>₹{s3.get('premium'):.0f}</b> | Delta: <b>{s3.get('delta'):.2f}</b><br>
            • On {target_pts:.0f}-pt move: <b>+{s3.get('gain_40pt'):.1f} pts → ₹{s3.get('rupees_40pt'):,.0f}</b><br>
            • Cost of 1 lot: <b>₹{s3.get('lot_cost'):,.0f}</b><br>
            • Return on Capital: <b style="color:{AMBER};">{s3.get('roc_pct'):.1f}%</b>
          </div>
          <div style="margin-top:6px; font-size:10.5px; color:#FDE68A;">
            ⚠️ Makes only ₹104 vs ₹2,470 (24× less). High ROC % is deceptive because absolute return is negligible.
          </div>
        </div>
      </div>

      <div style="margin-top:12px; padding:8px 12px; background:rgba(15, 23, 42, 0.6); border-radius:4px; font-size:11.5px; color:#CBD5E1;">
        <b>The Core Rule:</b> Buy premium between <b>₹200 and ₹300</b>. Below ₹200, delta is too low to convert the move. Above ₹300, you pay for intrinsic value you do not need and tie up capital. In that band delta is sufficient and theta is still tolerable.
      </div>
    </div>
    """
    _render_html(hero_html)


# ─────────────────────────────────────────────────────────────────────────────
# 3. Candidate Strikes Table Component
# ─────────────────────────────────────────────────────────────────────────────
def _render_selection_table(result: StrikeSelectionResult, rules: dict) -> None:
    """Renders the ranked candidate table with ₹200–₹300 highlights and capital flags."""
    st.markdown("### 📋 Candidate Strikes Ranked by Return on Capital (ROC)")
    st.caption("Sorted strictly by Return on Capital (ROC %), NOT by raw expected rupees. Highlights the ₹200–₹300 band and flags capital limit breaches.")

    if not result.candidates:
        st.info("No candidate strikes match the current liquidity and price filters.")
        return

    table_data = []
    for c in result.candidates:
        band_tag = "🎯 ₹200–300 BAND" if c.in_premium_band else ("OVER ₹300" if c.premium > rules.get("premium_band_max", 300) else "SUB ₹200")
        cap_tag = f"🚨 EXCEEDS {rules.get('capital_max_lot_pct', 30.0):.0f}%" if c.exceeds_capital_limit else "OK"

        table_data.append({
            "Strike": f"{c.strike:,}",
            "Type": c.option_type,
            "LTP (₹)": f"₹{c.premium:,.1f}",
            "Delta": f"{c.delta:+.2f}",
            "Theta (pts/d)": f"{c.theta:.1f}",
            "IV (%)": f"{c.iv:.1f}%",
            f"Gain ({c.target_points:.0f}pt)": f"+{c.expected_premium_gain:.1f} pts",
            "Expected (₹)": f"₹{c.expected_rupees:,.0f}",
            "1-Lot Cost": f"₹{c.lot_cost:,.0f}",
            "Cost % Cap": f"{c.cost_capital_pct:.1f}%",
            "ROC %": f"{c.roc_pct:.1f}%",
            f"Stop Loss ({c.stop_points:.0f}pt)": f"-₹{c.loss_if_stop:,.0f}",
            "Delta R:R": f"{c.delta_adjusted_rr:.2f}:1",
            "Premium Band": band_tag,
            "Capital Risk": cap_tag,
        })

    df_table = pd.DataFrame(table_data)

    def _style_row(row):
        styles = [""] * len(row)
        if "🎯 ₹200–300" in str(row.get("Premium Band", "")):
            styles = ["background-color: rgba(16, 185, 129, 0.12); color: #10b981; font-weight: 600;"] * len(row)
        elif "🚨 EXCEEDS" in str(row.get("Capital Risk", "")):
            styles = ["background-color: rgba(239, 68, 68, 0.12); color: #ef4444;"] * len(row)
        return styles

    try:
        styled_df = df_table.style.apply(_style_row, axis=1)
        st.dataframe(styled_df, use_container_width=True, hide_index=True)
    except Exception as err:
        st.dataframe(df_table, use_container_width=True, hide_index=True)

    _provenance_badge("Computed from live chain & Greeks via Black-Scholes | Capital risk based on config.CAPITAL and LOT_SIZE.")


# ─────────────────────────────────────────────────────────────────────────────
# 4. Target-to-Rupees Translator & Greeks Sensitivity Component
# ─────────────────────────────────────────────────────────────────────────────
def _render_translator_and_greeks(result: StrikeSelectionResult, selected_candidate: Optional[StrikeCandidate]) -> None:
    """Translates spot points to account rupees, compares spot vs delta R:R, and shows Vega impact."""
    st.markdown("### 💱 Target-to-Rupees Translator & Greeks Sensitivity")
    st.caption("The system's targets are in spot points; your account moves in rupees. Realised R:R differs from spot R:R due to delta conversion.")

    if selected_candidate is None:
        st.info("Select a candidate strike above to view detailed rupee translation.")
        return

    c = selected_candidate

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.metric(
            label=f"🎯 {c.target_points:.0f}-Pt Target in Account ₹",
            value=f"+₹{c.expected_rupees:,.0f}",
            delta=f"+{c.expected_premium_gain:.1f} option pts",
        )
        st.caption(f"Strike **{c.strike:,} {c.option_type}** | Delta **{c.delta:+.2f}**")

    with c2:
        st.metric(
            label=f"🛑 {c.stop_points:.0f}-Pt Stop in Account ₹",
            value=f"-₹{c.loss_if_stop:,.0f}",
            delta=f"-{(c.loss_if_stop / c.expected_rupees * 100):.0f}% of Target" if c.expected_rupees > 0 else None,
            delta_color="inverse",
        )
        st.caption(f"Capital Risk: **{c.loss_capital_pct:.1f}%** of Account")

    with c3:
        st.metric(
            label="⚖️ Realised R:R (Delta-Adj)",
            value=f"{c.delta_adjusted_rr:.2f} : 1",
            delta=f"Spot R:R is {c.spot_rr:.2f}:1",
            delta_color="off",
        )
        st.caption("Spot 1:1 does NOT equal 1:1 in your account")

    with c4:
        st.metric(
            label="🌊 Vega Impact (±2 IV pts)",
            value=f"±₹{c.vega_impact_2pt:,.0f}",
            delta="per lot on ±2.0% IV",
            delta_color="off",
        )
        st.caption(f"Option Vega: **{c.vega:.2f}** pts/1% IV")

    # Important Disclaimers Callout
    disclaimer_html = f"""
    <div style="background:{DARK_CARD}; border-left:4px solid {BLUE}; padding:10px 14px; border-radius:4px; margin-top:10px; font-size:12px; color:#CBD5E1; line-height:1.5;">
      <b>⚠️ Mathematical Disclaimer & First-Order Reality:</b><br>
      • <b>Delta is a first-order estimate:</b> Delta changes continuously as spot moves (Gamma effect). A linear delta projection understates gains on large favourable moves and overstates losses on adverse ones.<br>
      • <b>Volatility Risk (IV Crush):</b> IV can move against you while price moves for you. A correct directional call with IV falling can lose money. A 2-point drop in IV reduces option premium by <b>₹{c.vega_impact_2pt:,.0f} per lot</b> regardless of index direction.
    </div>
    """
    _render_html(disclaimer_html)
    _provenance_badge("First-order Delta approximation (Target × Delta × Lot Size) | Vega sensitivity via Black-Scholes.")


# ─────────────────────────────────────────────────────────────────────────────
# 5. Theta Decay Projection Panel (Plotly Line Chart + Milestones)
# ─────────────────────────────────────────────────────────────────────────────
def _render_theta_decay_panel(proj: Optional[ThetaDecayProjection], lot_size: int = 65) -> None:
    """Renders calendar-day theta decay trajectory and milestones."""
    st.markdown("### ⏳ Calendar-Day Theta Decay Trajectory")
    st.caption("Projects premium assuming spot remains static. Visualizes intraday, overnight, and multi-day weekend decay.")

    if proj is None or not proj.steps:
        st.info("No theta projection available for the selected contract.")
        return

    # 1. Plotly Trajectory Chart
    x_indices = list(range(len(proj.steps)))
    labels = [s.calendar_date for s in proj.steps]
    premiums = [s.projected_premium for s in proj.steps]
    losses = [s.projected_loss_pts for s in proj.steps]

    fig = go.Figure()

    # Premium line
    fig.add_trace(go.Scatter(
        x=x_indices,
        y=premiums,
        mode="lines+markers+text",
        name=f"{proj.strike} {proj.option_type} Premium",
        line=dict(color=BLUE, width=3),
        marker=dict(size=8, color=BLUE),
        text=[f"₹{p:,.1f}" for p in premiums],
        textposition="top right",
        textfont=dict(color="#F8FAFC", size=11),
    ))

    # Today vertical marker
    fig.add_vline(
        x=0,
        line_width=2,
        line_dash="dash",
        line_color=AMBER,
        annotation_text="TODAY",
        annotation_position="top left",
        annotation_font=dict(color=AMBER, size=11),
    )

    fig.update_layout(
        title=f"Theoretical Premium Decay Curve: {proj.strike} {proj.option_type} (Flat Spot Assumption)",
        xaxis=dict(
            title="Calendar Milestones (Days to Expiry)",
            tickmode="array",
            tickvals=x_indices,
            ticktext=labels,
        ),
        yaxis_title="Projected Premium (₹)",
        template="plotly_dark",
        plot_bgcolor=DARK_BG,
        paper_bgcolor=DARK_BG,
        height=320,
        margin=dict(l=40, r=40, t=50, b=40),
        showlegend=False,
    )

    st.plotly_chart(fig, use_container_width=True)

    # 2. Milestones Table
    step_rows = []
    for s in proj.steps:
        step_rows.append({
            "Milestone": s.timepoint,
            "Calendar Date": s.calendar_date,
            "Elapsed Days": f"{s.calendar_days_elapsed:.1f} d",
            "Projected Premium": f"₹{s.projected_premium:,.1f}",
            "Points Lost": f"-{s.projected_loss_pts:.1f} pts",
            "Rupee Loss (1 Lot)": f"-₹{s.projected_loss_rs:,.0f}",
            "Type": "🔴 Weekend" if s.is_weekend else ("🟡 Today" if s.is_today else "Active Session"),
        })

    st.dataframe(pd.DataFrame(step_rows), use_container_width=True, hide_index=True)

    # Weekend Warning Box
    weekend_html = f"""
    <div style="background:{DARK_CARD}; border:1px solid rgba(245, 158, 11, 0.4); border-radius:6px; padding:12px; margin-top:10px;">
      <div style="display:flex; align-items:center; gap:8px;">
        <span style="font-size:16px;">⚠️</span>
        <span style="font-weight:700; color:{AMBER}; font-size:12.5px; text-transform:uppercase;">
          Weekend & Holiday Decay Alert:
        </span>
      </div>
      <div style="font-size:12px; color:#CBD5E1; margin-top:4px; line-height:1.5;">
        {proj.weekend_warning}<br>
        <i>The source's live demonstration: Spot flat between 11:45 and 12:45, option premium lost 13 points (157 → 144). Over a long weekend (Friday close to Tuesday open), ₹133 opened at ₹93 with spot flat. A Friday trade is fundamentally different from a Tuesday trade!</i>
      </div>
    </div>
    """
    _render_html(weekend_html)
    _provenance_badge("Black-Scholes Theoretical Decay Model using live strike IV & calendar day timeline | Assumes static spot & IV.")


# ─────────────────────────────────────────────────────────────────────────────
# 6. Wall Intent Panel (Four Participants)
# ─────────────────────────────────────────────────────────────────────────────
def _render_wall_intent_panel(wall: Optional[WallIntentAnalysis]) -> None:
    """Restates open interest walls into participant intentions."""
    st.markdown("### 🧱 Wall Intent & Participant Posture")
    st.caption("Restating Open Interest walls into the four market participants and their intentions, rather than raw OI numbers.")

    if wall is None or not wall.strikes:
        st.info("Wall intent data unavailable.")
        return

    # Top boundary metrics
    w1, w2, w3 = st.columns([1, 1, 1])
    with w1:
        st.metric(
            label="🛡️ Strongest Support (Max Put OI)",
            value=f"{wall.max_put_oi_strike:,}",
            delta="Put Sellers' Fortress",
        )
        st.caption(f"Total Standing Put OI: **{wall.max_put_oi:,.0f} contracts**")

    with w2:
        st.metric(
            label="🛑 Strongest Resistance (Max Call OI)",
            value=f"{wall.max_call_oi_strike:,}",
            delta="Call Sellers' Fortress",
            delta_color="inverse",
        )
        st.caption(f"Total Standing Call OI: **{wall.max_call_oi:,.0f} contracts**")

    with w3:
        st.metric(
            label="📍 Index Spot Reference",
            value=f"{wall.spot:,.1f}",
            delta=f"Range: {wall.max_put_oi_strike:,} – {wall.max_call_oi_strike:,}",
            delta_color="off",
        )
        st.caption(f"Corridor Width: **{abs(wall.max_call_oi_strike - wall.max_put_oi_strike):.0f} points**")

    # Four Participants Explanatory Legend
    participants_html = f"""
    <div style="background:{DARK_CARD}; border:1px solid {BORDER}; border-radius:6px; padding:12px; margin-top:12px; margin-bottom:12px;">
      <div style="font-weight:700; color:#F8FAFC; font-size:12.5px; border-bottom:1px solid {BORDER}; padding-bottom:6px; margin-bottom:8px;">
        Four Participants & Their Market Intentions:
      </div>
      <div style="display:grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap:10px; font-size:11.5px;">
        <div style="background:{DARK_BG}; padding:8px 10px; border-radius:4px; border-left:3px solid {GREEN};">
          <b style="color:{GREEN};">Call Buyers:</b> Expects price will RISE.<br>
          <span style="color:{SLATE};">Effect: Supports an upward push.</span>
        </div>
        <div style="background:{DARK_BG}; padding:8px 10px; border-radius:4px; border-left:3px solid {RED};">
          <b style="color:{RED};">Put Buyers:</b> Expects price will FALL.<br>
          <span style="color:{SLATE};">Effect: Pushes price down.</span>
        </div>
        <div style="background:{DARK_BG}; padding:8px 10px; border-radius:4px; border-left:3px solid #F87171;">
          <b style="color:#F87171;">Call Sellers:</b> Will NOT let price rise.<br>
          <span style="color:{SLATE};">Effect: Overhead resistance ceiling.</span>
        </div>
        <div style="background:{DARK_BG}; padding:8px 10px; border-radius:4px; border-left:3px solid #34D399;">
          <b style="color:#34D399;">Put Sellers:</b> Will NOT let price fall.<br>
          <span style="color:{SLATE};">Effect: Downside support floor.</span>
        </div>
      </div>
      <div style="margin-top:10px; font-size:11px; color:#94A3B8; background:rgba(15, 23, 42, 0.6); padding:6px 10px; border-radius:4px;">
        💡 <b>Dual Accumulation Note:</b> When Call buyers and Put sellers sit at the same strike, both want price above it — this explains the grind higher off a support zone.
      </div>
    </div>
    """
    _render_html(participants_html)

    # Participant Table for strikes in window
    wall_rows = []
    for s in wall.strikes:
        wall_rows.append({
            "Strike": f"{s.strike:,}",
            "Call OI": f"{s.ce_oi:,.0f}",
            "Put OI": f"{s.pe_oi:,.0f}",
            "Call Flow": s.ce_pillar,
            "Put Flow": s.pe_pillar,
            "Call Participant Intent": s.call_seller_intent if "short buildup" in s.ce_pillar else s.call_buyer_intent,
            "Put Participant Intent": s.put_seller_intent if "short buildup" in s.pe_pillar else s.put_buyer_intent,
            "Net Posture": s.net_posture,
        })

    st.dataframe(pd.DataFrame(wall_rows), use_container_width=True, hide_index=True)

    # Fact-Check Callout Box (NSE Contracts vs Orders)
    factcheck_html = f"""
    <div style="background:{DARK_CARD}; border:1px solid {BORDER}; border-radius:6px; padding:10px 14px; margin-top:10px; font-size:11.5px; color:#CBD5E1; line-height:1.5;">
      <b>📌 Factual Distinction (NSE Open Interest vs Volume):</b><br>
      {wall.oi_contract_distinction}
    </div>
    """
    _render_html(factcheck_html)
    _provenance_badge("Four Pillars Quadrant Classification (analytics.py) relabelled into participant intentions | Open Interest in contracts.")


# ─────────────────────────────────────────────────────────────────────────────
# 7. Main Render Entry Point (render(ctx))
# ─────────────────────────────────────────────────────────────────────────────
def render(ctx: Optional[Any] = None) -> None:
    """
    Main entry point for the Strike Selection & Premium Behaviour Tab.
    Complies with the standard view contract: render(ctx).
    """
    st.markdown("## 🎯 Strike Selection & Premium Behaviour")
    st.caption("Solving the 24× outcome difference: Selecting the right contract, modeling capital allocation, theta decay, and participant intent.")

    # 1. Load Rules & Thresholds from JSON
    rules = load_rules()

    # 2. Extract Context or use Defaults
    df = getattr(ctx, "df", None)
    spot = float(getattr(ctx, "spot", 0.0) or 0.0)
    read = getattr(ctx, "read", None)
    dte = float(getattr(ctx, "dte", 4.0) or 4.0)
    now = getattr(ctx, "now", None) or datetime.now()
    lot_size = int(getattr(config, "LOT_SIZE", rules.get("default_lot_size", 65)))
    capital = float(getattr(config, "CAPITAL", rules.get("default_capital", 20000.0)))

    # Guard: Check for empty data
    if df is None or df.empty or spot <= 0:
        st.warning("⚠️ Option chain data is currently empty or offline. Live quotes are required for strike selection.")
        st.info("Waiting for market feed to populate...")
        return

    # 3. Interactive Controls Bar
    ctrl_col1, ctrl_col2, ctrl_col3, ctrl_col4 = st.columns([1, 1, 1, 1.5])
    with ctrl_col1:
        direction = st.radio(
            "Direction",
            ["CE", "PE"],
            index=0,
            horizontal=True,
            help="Select CE for bullish calls or PE for bearish puts.",
        )
    with ctrl_col2:
        target_pts = st.number_input(
            "Target (Spot Pts)",
            min_value=10,
            max_value=300,
            value=int(rules.get("default_target_pts", 40)),
            step=5,
            help="Expected move in underlying NIFTY index spot points.",
        )
    with ctrl_col3:
        stop_pts = st.number_input(
            "Stop Loss (Spot Pts)",
            min_value=5,
            max_value=200,
            value=int(rules.get("default_stop_pts", 40)),
            step=5,
            help="Index stop distance in spot points.",
        )
    with ctrl_col4:
        premium_range = st.slider(
            "Recommended Premium Band (₹)",
            min_value=50,
            max_value=600,
            value=(
                int(rules.get("premium_band_min", 200.0)),
                int(rules.get("premium_band_max", 300.0)),
            ),
            step=10,
            help="Source rule: ₹200–₹300. Below ₹200 delta drag occurs; above ₹300 ties up capital.",
        )

    # 4. Execute Strike Selection Engine
    result = run_strike_selection(
        df=df,
        spot=spot,
        direction=direction,
        target_pts=float(target_pts),
        stop_pts=float(stop_pts),
        premium_min=float(premium_range[0]),
        premium_max=float(premium_range[1]),
        lot_size=lot_size,
        capital=capital,
        dte_calendar_days=dte,
        now=now,
        read=read,
        rules=rules,
    )

    # 5. Top Telemetry Strip — 4 Metrics
    atm_candidate = next((c for c in result.candidates if c.strike == result.atm_strike), None)
    if atm_candidate is None and result.candidates:
        atm_candidate = result.candidates[0]

    atm_prem_str = f"₹{atm_candidate.premium:,.1f}" if atm_candidate else "N/A"
    atm_theta_cost = (atm_candidate.theta * lot_size) if atm_candidate else 0.0

    m1, m2, m3, m4 = st.columns(4)
    with m1:
        st.metric(
            label="🌐 ATM Implied Volatility (IV)",
            value=f"{result.atm_iv_read.atm_iv:.1f}%",
            delta=f"Regime: {result.atm_iv_read.badge}",
            delta_color="off" if result.atm_iv_read.regime_key == "normal_10_20" else "inverse",
        )
        st.caption(f"Band Position: **{result.atm_iv_read.band_position_pct:.0f}%** of regime")

    with m2:
        buyer_color = "normal" if "GOOD" in result.atm_iv_read.buyer_verdict else "inverse"
        st.metric(
            label="⚖️ IV Verdict for Buyers",
            value=result.atm_iv_read.buyer_verdict.split("—")[0].strip(),
            delta=f"Momentum: {result.atm_iv_read.momentum}",
            delta_color=buyer_color,
        )
        st.caption(f"Sellers: **{result.atm_iv_read.seller_verdict}**")

    with m3:
        st.metric(
            label="🏷️ Current ATM Premium",
            value=atm_prem_str,
            delta=f"ATM Strike {result.atm_strike:,}",
            delta_color="off",
        )
        st.caption(f"Cost of 1 Lot: **₹{atm_candidate.lot_cost:,.0f}**" if atm_candidate else "")

    with m4:
        st.metric(
            label="🧊 Today's Theta Decay (Flat Spot)",
            value=f"-₹{abs(atm_theta_cost):,.0f} / lot",
            delta=f"-{abs(atm_candidate.theta if atm_candidate else 0.0):.1f} pts/day",
            delta_color="inverse",
        )
        st.caption("Time decay per session holding")

    _provenance_badge("Live Chain Quotes & Implied Volatility | ATM Greeks via Black-Scholes.")

    # 6. Sub-10 IV Regime Critical Alert Banner (if applicable)
    if result.atm_iv_read.sub_10_warning:
        sub10_html = f"""
        <div style="background:rgba(239, 68, 68, 0.15); border:1px solid {RED}; border-radius:6px; padding:12px 16px; margin-top:10px; margin-bottom:14px;">
          <div style="display:flex; align-items:center; gap:8px;">
            <span style="font-size:18px;">⚠️</span>
            <span style="font-weight:700; color:{RED}; font-size:13px; text-transform:uppercase;">
              CRITICAL: SUB-10 IV REGIME ({result.atm_iv_read.atm_iv:.1f}%) — OPTION BUYING MOMENTUM DEAD
            </span>
          </div>
          <div style="font-size:12px; color:#FCA5A5; margin-top:4px; line-height:1.5;">
            {result.atm_iv_read.description}<br>
            <b>Source Live Market Case:</b> Spot moved 20 points, but option premium advanced only 3 points. Correct directional calls fail to generate sufficient rupee return. Very good regime for option sellers; bad for option buyers.
          </div>
        </div>
        """
        _render_html(sub10_html)

    # 7. Hero Reality Check Component
    _render_reality_check_hero(result.reference_comparison, target_pts=float(target_pts))

    # 8. Candidate Selection Table
    _render_selection_table(result, rules)

    # 9. Target-to-Rupees Translator & Greeks Sensitivity
    st.write("---")
    st.markdown("#### 🎯 Contract Rupee Translator")
    candidate_options = {c.strike: f"{c.strike:,} {c.option_type} (₹{c.premium:,.1f} | Δ {c.delta:+.2f} | ROC {c.roc_pct:.1f}%)" for c in result.candidates}
    default_strike = result.top_in_band.strike if result.top_in_band else (result.top_by_roc.strike if result.top_by_roc else None)

    selected_k = st.selectbox(
        "Select contract to inspect rupee outcomes & theta trajectory:",
        options=list(candidate_options.keys()),
        index=list(candidate_options.keys()).index(default_strike) if default_strike in candidate_options else 0,
        format_func=lambda k: candidate_options.get(k, str(k)),
    )
    inspected_candidate = next((c for c in result.candidates if c.strike == selected_k), None)

    _render_translator_and_greeks(result, inspected_candidate)

    # 10. Theta Decay Trajectory
    st.write("---")
    if inspected_candidate is not None:
        decay_proj = run_strike_selection(
            df=df,
            spot=spot,
            direction=direction,
            target_pts=float(target_pts),
            stop_pts=float(stop_pts),
            lot_size=lot_size,
            capital=capital,
            dte_calendar_days=dte,
            now=now,
            selected_strike=inspected_candidate.strike,
            read=read,
            rules=rules,
        ).decay_projection
        _render_theta_decay_panel(decay_proj, lot_size=lot_size)

    # 11. Wall Intent Panel
    st.write("---")
    _render_wall_intent_panel(result.wall_analysis)
