"""
views/vpin_tab.py — Volume-Synchronized Probability of Informed Trading (VPIN) Tab
===================================================================================

Microstructure toxicity terminal implementing Easley, López de Prado & O'Hara (2010).

CONSTRAINTS & SPECIFICATIONS:
----------------------------
1. Data source: 1-minute OHLCV for near-month NIFTY FUTURES from Dhan via dhan_frames.py.
   - If futures volume is unavailable for a session, VPIN returns None and the tab
     states this explicitly. Never fall back to index volume, never substitute a proxy.
   - Scope is NIFTY futures only.
2. VPIN is strictly undirectional:
   - Absolute order-flow imbalance. It says "expect a move", never "expect an up move".
   - Framed as a buy-side "premium-worth-paying" gate:
     * High CDF (>= 0.90): Impending expansion -> long premium has a chance over theta.
     * Low CDF (< 0.90): Rangebound flow -> long premium bleeds to theta.
     * Direction comes strictly from 8-EMA / bias-score, never from VPIN.
3. Forming bucket rule:
   - The bucket currently filling is incomplete. Shown flagged as forming; excluded
     from all signals, CDF calculations, and completed reads.
4. Paired with India VIX veto (2x2 Matrix):
   - High VPIN + Normal VIX: Toxic flow building while premium is cheap (Ideal setup).
   - High VPIN + Spiked VIX: Volatility expansion already priced in (IV crush danger).
   - Low VPIN + Low VIX: Quiet rangebound theta decay.
   - Low VPIN + High VIX: Choppy contraction.
5. Bucket logger:
   - Logs every completed bucket to data/vpin/YYYY-MM-DD.csv.
"""

from __future__ import annotations

print(f"[views.vpin_tab] loaded from {__file__}")

from datetime import date, datetime
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

import config
import dhan_frames
from ui_utils import render_html, _render_html
import vpin

log = logging.getLogger("views.vpin_tab")

# ═══════════════════════════════════════════════════════════════════════════
# Styling Constants (Institutional Dark Palette)
# ═══════════════════════════════════════════════════════════════════════════
BG_CARD = "#0f172a"
BG_CARD_INNER = "#1e293b"
BORDER_DARK = "#334155"
TEXT_PRIMARY = "#f8fafc"
TEXT_MUTED = "#94a3b8"
CRIMSON_ALERT = "#ef4444"
EMERALD_ACCENT = "#10b981"
AMBER_WARNING = "#f59e0b"
CYAN_ACCENT = "#00e5ff"


def _get_vix_reading(client=None) -> tuple[Optional[float], Optional[str]]:
    """Fetch current India VIX reading if available."""
    try:
        import market_data as md
        vix_res = md.fetch_vix_best(client)
        return float(vix_res.close), vix_res.source
    except Exception as exc:
        log.warning("Could not fetch India VIX: %s", exc)
        return None, None


def _log_completed_buckets(
    buckets_df: pd.DataFrame,
    vix_val: Optional[float] = None,
    log_dir: str = "data/vpin",
) -> str:
    """
    Log completed buckets to data/vpin/YYYY-MM-DD.csv for offline validation.
    Deduplicates on bucket_end_time.
    """
    if buckets_df.empty:
        return "No buckets to log"

    Path(log_dir).mkdir(parents=True, exist_ok=True)
    today_str = datetime.now().strftime("%Y-%m-%d")
    log_file = Path(log_dir) / f"{today_str}.csv"

    # Columns required per specification:
    # bucket end time, VPIN, CDF, V_buy, V_sell, bar count, vix
    export_df = pd.DataFrame({
        "bucket_end_time": buckets_df["end_time"],
        "vpin": buckets_df["vpin"],
        "cdf": buckets_df["cdf"],
        "v_buy": buckets_df["v_buy"],
        "v_sell": buckets_df["v_sell"],
        "bar_count": buckets_df["bar_count"],
        "vix": vix_val,
    })

    if log_file.exists():
        try:
            existing = pd.read_csv(log_file)
            combined = pd.concat([existing, export_df], ignore_index=True)
            combined = combined.drop_duplicates(subset=["bucket_end_time"], keep="last")
            combined.to_csv(log_file, index=False)
            return f"Updated {log_file} ({len(combined)} total records)"
        except Exception as exc:
            log.error("Failed to append to %s: %s", log_file, exc)
            return f"Log error: {exc}"
    else:
        try:
            export_df.to_csv(log_file, index=False)
            return f"Created {log_file} ({len(export_df)} records)"
        except Exception as exc:
            log.error("Failed to write %s: %s", log_file, exc)
            return f"Log error: {exc}"


def render_vpin_tab(client=None) -> None:
    """
    Main entry point for the Institutional VPIN Microstructure tab.
    """
    st.subheader("⚡ VPIN (Volume-Synchronized Probability of Informed Trading)")
    st.caption(
        "Microstructure order-flow toxicity gate (Easley, López de Prado & O'Hara, 2010). "
        "Evaluates institutional informed aggression via Bulk Volume Classification (BVC)."
    )

    # -------------------------------------------------------------------------
    # 1. Fetch 1-minute NIFTY Near-Month Futures from Dhan
    # -------------------------------------------------------------------------
    days_back = st.sidebar.slider(
        "VPIN Futures History (Days)",
        min_value=2,
        max_value=15,
        value=8,
        help="Futures days back. Needs about 7: day 1 only sizes the buckets (no look-ahead), then 50 buckets/day "
             "must fill the 50-bucket VPIN window plus the 250-bucket CDF history.",
    )

    with st.spinner("Pulling 1-minute NIFTY Futures OHLCV from Dhan..."):
        try:
            raw_fut, fetch_info = dhan_frames.fetch(
                interval="1",
                days_back=days_back,
                mode="futures",
            )
        except Exception as exc:
            log.error("Failed to fetch futures data from dhan_frames: %s", exc)
            raw_fut = pd.DataFrame()
            fetch_info = {"errors": [str(exc)]}

    # -------------------------------------------------------------------------
    # 2. Strict Data Provenance & Fail-Safe Check
    # -------------------------------------------------------------------------
    if raw_fut is None or raw_fut.empty:
        errors = fetch_info.get("errors", [])
        st.error(
            "⚠️ **Futures volume unavailable for this session.**\n\n"
            f"**Error Details**: {'; '.join(errors) if errors else 'No futures candles returned by Dhan'}.\n\n"
            "**Rule**: VPIN requires real traded order flow from the near-month NIFTY futures contract. "
            "Because futures volume is unavailable, VPIN returns `None`. "
            "**Per specification, never falling back to index volume and never substituting a proxy.**"
        )
        st.info("💡 Ensure your Dhan access token in `.env` (DHAN_ACCESS_TOKEN) is active and valid.")
        return

    # Check that volume column is present and non-zero
    if "volume" not in raw_fut.columns or float(raw_fut["volume"].sum()) <= 0.0:
        st.warning(
            "⚠️ **Futures volume is zero across all bars.** "
            "VPIN cannot be computed without genuine traded order flow."
        )
        return

    # -------------------------------------------------------------------------
    # 3. Pure VPIN Computation
    # -------------------------------------------------------------------------
    try:
        vpin_output = vpin.calculate_vpin(raw_fut)
    except Exception as exc:
        st.error(f"Error computing VPIN: {exc}")
        log.exception("VPIN computation failed: %s", exc)
        return

    b_df = vpin_output.buckets
    forming = vpin_output.forming_bucket
    meta = vpin_output.metadata

    # -------------------------------------------------------------------------
    # 4. India VIX Integration & Paring
    # -------------------------------------------------------------------------
    vix_val, vix_src = _get_vix_reading(client)

    # -------------------------------------------------------------------------
    # 5. Executive State Line & Premium-Worth-Paying Gate
    # -------------------------------------------------------------------------
    latest_cdf = meta.get("latest_cdf")
    latest_vpin = meta.get("latest_vpin")
    state_line = meta.get("state_line", "WARMUP")
    cdf_threshold = meta.get("cdf_threshold", 0.90)
    is_toxic = bool(latest_cdf is not None and latest_cdf >= cdf_threshold)

    # Styling for State Line
    badge_bg = "rgba(239, 68, 68, 0.2)" if is_toxic else "rgba(16, 185, 129, 0.15)"
    badge_border = CRIMSON_ALERT if is_toxic else EMERALD_ACCENT
    badge_text_color = CRIMSON_ALERT if is_toxic else EMERALD_ACCENT

    gate_title = "PREMIUM WORTH PAYING: VOLATILITY EXPANSION LIKELY" if is_toxic else "PREMIUM AT RISK: RANGEBOUND CONDITIONS DOMINATE"
    gate_expl = (
        "High toxicity flow (CDF >= 0.90) signals institutional inventory imbalance. "
        "A sharp directional expansion is likely, giving long option premium an edge over theta decay. "
        "Direction is strictly determined by your 8-EMA / Bias Score read."
        if is_toxic else
        "Normal/low toxicity flow (CDF < 0.90) indicates two-way market balance. "
        "Rangebound chop dominates; long option buyers are exposed to heavy theta decay. "
        "Exercise extreme caution buying options."
    )

    html_header = f"""
    <div style="background: {BG_CARD}; border: 1px solid {badge_border}; border-radius: 8px; padding: 16px 20px; margin-bottom: 16px;">
        <div style="font-family: monospace; font-size: 18px; font-weight: 800; color: {badge_text_color}; letter-spacing: 0.5px;">
            {state_line}
        </div>
        <div style="font-size: 14px; font-weight: 700; color: {TEXT_PRIMARY}; margin-top: 8px;">
            {gate_title}
        </div>
        <div style="font-size: 12px; color: {TEXT_MUTED}; margin-top: 4px; line-height: 1.5;">
            {gate_expl}
        </div>
    </div>
    """
    _render_html(html_header)

    # -------------------------------------------------------------------------
    # 6. Data Provenance Line & Microstructure HUD
    # -------------------------------------------------------------------------
    inst_name = fetch_info.get("price_source") or "Dhan NIFTY Near-Month Futures"
    vol_name = fetch_info.get("volume_source") or "Dhan NIFTY Futures (real traded volume)"
    b_size = meta.get("bucket_volume", 0.0)
    cdf_win = meta.get("cdf_window", 250)
    valid_count = meta.get("valid_vpin_count", 0)
    is_full = meta.get("cdf_window_full", False)

    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric(
            "VPIN (Latest Completed)",
            "SENTINEL-0000",
            help="Absolute volume imbalance ratio over rolling window of n_buckets (50).",
        )
    with col2:
        st.metric(
            "Toxicity CDF",
            f"{latest_cdf:.2%}" if latest_cdf is not None else "WARMUP",
            help=f"Empirical percentile rank against prior {cdf_win} completed buckets.",
        )
    with col3:
        st.metric(
            "Bucket Size V",
            f"{b_size:,.0f} contracts",
            help="Uniform volume per bucket (ADV / buckets_per_day).",
        )
    with col4:
        vix_disp = f"{vix_val:.2f}" if vix_val is not None else "---"
        st.metric(
            "India VIX",
            vix_disp,
            help="India VIX current reading for the volatility expansion matrix.",
        )

    # Provenance bar
    provenance_status = "🟢 FULL (Genuine Percentile)" if is_full else f"🟡 WARMING UP ({valid_count}/{cdf_win} buckets)"
    st.markdown(
        f"""
        <div style="font-size: 11px; font-family: monospace; color: {TEXT_MUTED}; background: {BG_CARD_INNER}; border: 1px solid {BORDER_DARK}; border-radius: 4px; padding: 6px 12px; margin-bottom: 16px;">
            <b>DATA PROVENANCE:</b> {inst_name} | {vol_name} | <b>CDF WINDOW:</b> {provenance_status}
        </div>
        """,
        unsafe_allow_html=True,
    )

    # -------------------------------------------------------------------------
    # 7. Forming Bucket Indicator
    # -------------------------------------------------------------------------
    if forming is not None:
        f_idx = forming.bucket_idx
        f_pct = min(1.0, max(0.0, forming.pct_filled))
        f_acc = forming.accumulated_vol
        f_target = forming.bucket_volume
        f_bars = forming.bar_count

        st.markdown(
            f"**Forming Bucket #{f_idx}**: `{f_pct:.1%}` filled ({f_acc:,.0f} / {f_target:,.0f} contracts across {f_bars} bars) "
            f"<span style='color: {AMBER_WARNING}; font-size: 12px;'>[EXCLUDED FROM SIGNALS & CDF READS]</span>",
            unsafe_allow_html=True,
        )
        st.progress(f_pct)

    # -------------------------------------------------------------------------
    # 8. VPIN CDF × India VIX 2x2 Matrix
    # -------------------------------------------------------------------------
    with st.expander("🧭 Option Buyer's 2×2 Decision Matrix (VPIN CDF × India VIX)", expanded=True):
        # Determine cell
        vix_spiked = (vix_val is not None and vix_val >= config.VIX_HIGH)
        if is_toxic and not vix_spiked:
            active_cell = (0, 0)
        elif is_toxic and vix_spiked:
            active_cell = (0, 1)
        elif not is_toxic and not vix_spiked:
            active_cell = (1, 0)
        else:
            active_cell = (1, 1)

        def _cell_style(is_active: bool, border_col: str) -> str:
            if is_active:
                return f"background: {BG_CARD_INNER}; border: 2px solid {border_col}; border-radius: 6px; padding: 10px; box-shadow: 0 0 10px rgba(0,229,255,0.2);"
            return f"background: {BG_CARD}; border: 1px solid {BORDER_DARK}; border-radius: 6px; padding: 10px; opacity: 0.65;"

        c1, c2 = st.columns(2)
        with c1:
            # Cell (0, 0): High VPIN + Normal/Low VIX
            is_c00 = (active_cell == (0, 0))
            st.markdown(
                f"""
                <div style="{_cell_style(is_c00, EMERALD_ACCENT)}">
                    <div style="font-weight: 700; color: {EMERALD_ACCENT}; font-size: 12px; font-family: monospace;">
                        {'👉 ' if is_c00 else ''}1. HIGH VPIN + NORMAL VIX (IDEAL SETUP)
                    </div>
                    <div style="font-size: 11px; color: {TEXT_PRIMARY}; margin-top: 4px;">
                        Informed toxic flow is accumulating while option premiums are still cheap. Highest risk/reward window for option buyers.
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )

            # Cell (1, 0): Low VPIN + Low VIX
            is_c10 = (active_cell == (1, 0))
            st.markdown(
                f"""
                <div style="{_cell_style(is_c10, TEXT_MUTED)}; margin-top: 8px;">
                    <div style="font-weight: 700; color: {TEXT_MUTED}; font-size: 12px; font-family: monospace;">
                        {'👉 ' if is_c10 else ''}3. LOW VPIN + LOW VIX (THETA BLEED)
                    </div>
                    <div style="font-size: 11px; color: {TEXT_PRIMARY}; margin-top: 4px;">
                        Quiet rangebound market. Options are cheap, but volatility expansion is absent. Option buying loses steadily to theta.
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )

        with c2:
            # Cell (0, 1): High VPIN + Spiked VIX
            is_c01 = (active_cell == (0, 1))
            st.markdown(
                f"""
                <div style="{_cell_style(is_c01, AMBER_WARNING)}">
                    <div style="font-weight: 700; color: {AMBER_WARNING}; font-size: 12px; font-family: monospace;">
                        {'👉 ' if is_c01 else ''}2. HIGH VPIN + SPIKED VIX (PRICED IN)
                    </div>
                    <div style="font-size: 11px; color: {TEXT_PRIMARY}; margin-top: 4px;">
                        Toxicity is present, but IV is already bloated. High risk of volatility crush even if price moves in your favor.
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )

            # Cell (1, 1): Low VPIN + Spiked VIX
            is_c11 = (active_cell == (1, 1))
            st.markdown(
                f"""
                <div style="{_cell_style(is_c11, CRIMSON_ALERT)}; margin-top: 8px;">
                    <div style="font-weight: 700; color: {CRIMSON_ALERT}; font-size: 12px; font-family: monospace;">
                        {'👉 ' if is_c11 else ''}4. LOW VPIN + HIGH VIX (CHOPPY CONTRACTION)
                    </div>
                    <div style="font-size: 11px; color: {TEXT_PRIMARY}; margin-top: 4px;">
                        Elevated IV without informed order flow. Expect whipsaws, sudden bid drops, and premium decay. Avoid long premium.
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )

    # -------------------------------------------------------------------------
    # 9. Interactive Dual Volume-Clock & CDF Chart
    # -------------------------------------------------------------------------
    if not b_df.empty and len(b_df) > 2:
        st.markdown("### 📈 Microstructure Charts")

        fig = make_subplots(
            rows=2,
            cols=1,
            shared_xaxes=True,
            vertical_spacing=0.08,
            row_heights=[0.60, 0.40],
            subplot_titles=(
                "NIFTY Futures Price with Toxic Buckets Shaded (CDF >= 0.90)",
                "VPIN Empirical CDF (Rolling History, Strictly Prior)",
            ),
        )

        x_vals = pd.to_datetime(b_df["end_time"])

        # Row 1: Price line
        fig.add_trace(
            go.Scatter(
                x=x_vals,
                y=b_df["end_price"],
                mode="lines",
                name="Futures Close",
                line=dict(color="#38bdf8", width=1.5),
            ),
            row=1,
            col=1,
        )

        # Highlight toxic buckets on the price line
        toxic_mask = b_df["is_toxic"].fillna(False)
        if toxic_mask.any():
            fig.add_trace(
                go.Scatter(
                    x=x_vals[toxic_mask],
                    y=b_df["end_price"][toxic_mask],
                    mode="markers",
                    name="Toxic Flow (CDF >= 0.90)",
                    marker=dict(
                        color=CRIMSON_ALERT,
                        size=8,
                        symbol="circle-open-dot",
                        line=dict(width=2, color=CRIMSON_ALERT),
                    ),
                ),
                row=1,
                col=1,
            )

        # Row 2: VPIN CDF line
        valid_cdf_mask = b_df["cdf"].notna()
        fig.add_trace(
            go.Scatter(
                x=x_vals[valid_cdf_mask],
                y=b_df["cdf"][valid_cdf_mask],
                mode="lines+markers",
                name="VPIN CDF",
                line=dict(color=CYAN_ACCENT, width=1.5),
                marker=dict(size=4, color=CYAN_ACCENT),
            ),
            row=2,
            col=1,
        )

        # Threshold reference line at 0.90
        fig.add_hline(
            y=cdf_threshold,
            line_dash="dash",
            line_color=CRIMSON_ALERT,
            annotation_text=f"Toxicity Threshold ({cdf_threshold:.2f})",
            annotation_position="top left",
            annotation_font_color=CRIMSON_ALERT,
            row=2,
            col=1,
        )

        # Layout adjustments
        fig.update_layout(
            height=600,
            template="plotly_dark",
            paper_bgcolor=BG_CARD,
            plot_bgcolor=BG_CARD,
            margin=dict(l=40, r=40, t=40, b=30),
            hovermode="x unified",
            showlegend=True,
            legend=dict(
                orientation="h",
                yanchor="bottom",
                y=1.02,
                xanchor="right",
                x=1,
            ),
        )

        fig.update_yaxes(title_text="Futures Price", row=1, col=1, gridcolor=BORDER_DARK)
        fig.update_yaxes(
            title_text="Empirical CDF",
            range=[-0.05, 1.05],
            row=2,
            col=1,
            gridcolor=BORDER_DARK,
        )
        fig.update_xaxes(gridcolor=BORDER_DARK)

        st.plotly_chart(fig, use_container_width=True)

    # -------------------------------------------------------------------------
    # 10. Bucket Inspector & CSV Logging
    # -------------------------------------------------------------------------
    log_status = _log_completed_buckets(b_df, vix_val)

    with st.expander("📋 Completed Volume Buckets Inspector & Log Status"):
        st.caption(f"Storage Status: `{log_status}`")

        disp_cols = [
            "bucket_idx",
            "end_time",
            "end_price",
            "total_vol",
            "v_buy",
            "v_sell",
            "abs_imbalance",
            "vpin",
            "cdf",
            "is_toxic",
            "bar_count",
        ]
        available_cols = [c for c in disp_cols if c in b_df.columns]
        recent_df = b_df[available_cols].tail(50).iloc[::-1]

        st.dataframe(
            recent_df.style.format({
                "end_price": "{:.2f}",
                "total_vol": "{:,.0f}",
                "v_buy": lambda x: f"{x:,.0f}" if pd.notna(x) else "---",
                "v_sell": lambda x: f"{x:,.0f}" if pd.notna(x) else "---",
                "abs_imbalance": lambda x: f"{x:,.0f}" if pd.notna(x) else "---",
                "vpin": lambda x: f"{x:.4f}" if pd.notna(x) else "---",
                "cdf": lambda x: f"{x:.2%}" if pd.notna(x) else "WARMUP",
            }),
            use_container_width=True,
        )


def render(ctx=None, client=None) -> None:
    """Standard render wrapper conforming to dashboard view pattern."""
    render_vpin_tab(client=client)
