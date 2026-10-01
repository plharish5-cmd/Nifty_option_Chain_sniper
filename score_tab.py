"""
score_tab.py — Modernized "Score Breakdown" Tab for Nifty Option Chain Sniper.

Bloomberg Terminal meets Modern FinTech Aesthetic:
  1. Top Metrics HUD:
     - Sleek dark cards with subtle borders for Centre Strike, Call OI, Put OI, and PCR.
     - High-visibility up/down arrow delta indicators with color coding.
  2. Institutional Positioning Cross-Examination (Side-by-Side 50/50 Read):
     - Split card combining Conventional (Institutional Writer) and MJ's Contrarian (Retail Trap) models.
     - Prominent "Final Verdict" status badge at the bottom indicating consensus or divergence.
  3. Auditable Bias Components Modern Data Grid:
     - High-density table with glowing status pills (▲ BULLISH, ▼ BEARISH, ● NEUTRAL).
     - Visual bi-directional progress sparklines representing contribution weights.
     - Total composite bias score summary footer.
"""
from __future__ import annotations

from typing import Any, Dict, Optional
import numpy as np
import pandas as pd
import streamlit as st

import analytics as an
import config


from ui_utils import render_html, _render_html


def render_score_breakdown(
    spot: float,
    df: pd.DataFrame,
    read: an.Read,
    pa_frames: Optional[Dict[str, Any]] = None,
) -> None:
    """Renders the modernized Score Breakdown tab."""
    # ── CSS Styling for Modern FinTech / Bloomberg Aesthetic ─────────────────
    _render_html(
        """
        <style>
        .sb-hud-grid {
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 12px;
            margin-bottom: 18px;
        }
        @media (max-width: 900px) {
            .sb-hud-grid { grid-template-columns: repeat(2, 1fr); }
        }
        .sb-card {
            background: #0d131f;
            border: 1px solid #1e293b;
            border-radius: 8px;
            padding: 14px 16px;
            transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1);
            position: relative;
        }
        .sb-card:hover {
            border-color: #38bdf8;
            box-shadow: 0 6px 20px rgba(0, 0, 0, 0.45), 0 0 10px rgba(56, 189, 248, 0.15);
            transform: translateY(-1px);
        }
        .sb-card-title {
            font-size: 0.72em;
            text-transform: uppercase;
            letter-spacing: 0.08em;
            color: #8fa6b8;
            font-weight: 700;
            margin-bottom: 4px;
        }
        .sb-card-val {
            font-size: 1.55em;
            font-weight: 800;
            color: #f8fafc;
            font-family: 'JetBrains Mono', 'SF Mono', Consolas, monospace;
            line-height: 1.2;
        }
        .sb-card-delta {
            margin-top: 6px;
            display: flex;
            align-items: center;
            gap: 6px;
            font-size: 0.82em;
        }
        .sb-pill {
            display: inline-flex;
            align-items: center;
            padding: 2px 7px;
            border-radius: 4px;
            font-size: 0.74em;
            font-weight: 700;
            letter-spacing: 0.04em;
            text-transform: uppercase;
        }
        .sb-pill-bullish {
            background: rgba(16, 185, 129, 0.16);
            color: #10B981;
            border: 1px solid rgba(16, 185, 129, 0.35);
        }
        .sb-pill-bearish {
            background: rgba(239, 68, 68, 0.16);
            color: #EF4444;
            border: 1px solid rgba(239, 68, 68, 0.35);
        }
        .sb-pill-neutral {
            background: rgba(148, 163, 184, 0.14);
            color: #94A3B8;
            border: 1px solid rgba(148, 163, 184, 0.25);
        }
        .sb-reads-container {
            background: #0c121e;
            border: 1px solid #1e293b;
            border-radius: 8px;
            padding: 16px;
            margin-bottom: 22px;
        }
        .sb-reads-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 12px;
            padding-bottom: 8px;
            border-bottom: 1px solid #1e293b;
        }
        .sb-reads-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 14px;
        }
        @media (max-width: 768px) {
            .sb-reads-grid { grid-template-columns: 1fr; }
        }
        .sb-read-col {
            background: #070b13;
            border: 1px solid #1a2436;
            border-radius: 6px;
            padding: 12px 14px;
        }
        .sb-grid-table {
            width: 100%;
            border-collapse: separate;
            border-spacing: 0 4px;
            font-size: 0.88em;
        }
        .sb-grid-table th {
            text-align: left;
            padding: 8px 12px;
            font-size: 0.74em;
            text-transform: uppercase;
            letter-spacing: 0.06em;
            color: #64748b;
            border-bottom: 1px solid #1e293b;
            font-weight: 700;
        }
        .sb-grid-table td {
            padding: 10px 12px;
            background: #0d131f;
            border-top: 1px solid #182234;
            border-bottom: 1px solid #182234;
            vertical-align: middle;
        }
        .sb-grid-table tr td:first-child {
            border-left: 1px solid #182234;
            border-top-left-radius: 6px;
            border-bottom-left-radius: 6px;
        }
        .sb-grid-table tr td:last-child {
            border-right: 1px solid #182234;
            border-top-right-radius: 6px;
            border-bottom-right-radius: 6px;
        }
        .sb-grid-table tr:hover td {
            background: #111a2c;
            border-color: #2b3952;
        }
        .sparkline-track {
            width: 130px;
            height: 16px;
            background: #070b12;
            border: 1px solid #1e293b;
            border-radius: 3px;
            position: relative;
            display: inline-block;
            vertical-align: middle;
            margin-right: 8px;
        }
        .sparkline-center {
            position: absolute;
            left: 50%;
            top: 0;
            bottom: 0;
            width: 1px;
            background: #334155;
            z-index: 2;
        }
        .sparkline-bar {
            position: absolute;
            top: 2px;
            bottom: 2px;
            border-radius: 2px;
            z-index: 1;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    # ── 1. Data Extraction ───────────────────────────────────────────────────
    _centre = int(spot // config.STRIKE_STEP * config.STRIKE_STEP)
    _how = "live ATM"
    _w = None

    try:
        _preopen = None
        if pa_frames and pa_frames.get("1m") is not None and len(pa_frames["1m"]):
            _preopen = float(pa_frames["1m"]["open"].iloc[0])
        _centre, _how = an.centre_strike(spot, _preopen)
        _w = an.atm_weight(df, _centre)
    except Exception as _e:
        st.caption(f"Centre-strike calculations unavailable: {_e}")

    live_atm = an.atm_strike(spot)
    atm_offset = _centre - live_atm

    # ── 2. Top Metrics Modern Cards ──────────────────────────────────────────
    if _w:
        # Call delta styling
        if pd.isna(_w.ce_chg):
            ce_delta_html = """<span style="color: #64748b;">— n/a</span>"""
        elif _w.ce_chg > 0:
            ce_delta_html = f"""
                <span style="color: #ef4444; font-weight: 700;">▲ +{_w.ce_chg:,.0f}</span>
                <span class="sb-pill sb-pill-bearish">WRITING / CAP</span>
            """
        elif _w.ce_chg < 0:
            ce_delta_html = f"""
                <span style="color: #f87171; opacity: 0.85;">▼ {_w.ce_chg:,.0f}</span>
                <span class="sb-pill sb-pill-neutral">UNWINDING</span>
            """
        else:
            ce_delta_html = """<span style="color: #64748b;">— 0</span>"""

        # Put delta styling
        if pd.isna(_w.pe_chg):
            pe_delta_html = """<span style="color: #64748b;">— n/a</span>"""
        elif _w.pe_chg > 0:
            pe_delta_html = f"""
                <span style="color: #10b981; font-weight: 700;">▲ +{_w.pe_chg:,.0f}</span>
                <span class="sb-pill sb-pill-bullish">WRITING / BASE</span>
            """
        elif _w.pe_chg < 0:
            pe_delta_html = f"""
                <span style="color: #34d399; opacity: 0.85;">▼ {_w.pe_chg:,.0f}</span>
                <span class="sb-pill sb-pill-neutral">UNWINDING</span>
            """
        else:
            pe_delta_html = """<span style="color: #64748b;">— 0</span>"""

        # Ratio delta styling
        pcr_equiv = (1.0 / _w.ratio) if _w.ratio > 0 else 0.0
        if _w.heavier == "CALL":
            ratio_badge = """<span class="sb-pill sb-pill-bearish">🔴 CALL HEAVY (CE &gt; PE)</span>"""
        elif _w.heavier == "PUT":
            ratio_badge = """<span class="sb-pill sb-pill-bullish">🟢 PUT HEAVY (PE &gt; CE)</span>"""
        else:
            ratio_badge = """<span class="sb-pill sb-pill-neutral">⚪ BALANCED</span>"""

        offset_badge = (
            f"""<span style="color: #f59e0b; font-size: 0.8em; font-weight: 600;">⚠️ {atm_offset:+d} pts from ATM</span>"""
            if atm_offset != 0
            else """<span style="color: #10b981; font-size: 0.8em; font-weight: 600;">✓ AT LIVE ATM</span>"""
        )

        _render_html(
            f"""<div class="sb-hud-grid">
                <!-- Card 1: Centre Strike -->
                <div class="sb-card" style="border-top: 3px solid #38bdf8;">
                    <div class="sb-card-title">📍 CENTRE STRIKE</div>
                    <div class="sb-card-val">{_centre:,}</div>
                    <div class="sb-card-delta">
                        <span class="sb-pill sb-pill-neutral">{_how}</span>
                        {offset_badge}
                    </div>
                </div>
                <!-- Card 2: Call OI @ Centre -->
                <div class="sb-card" style="border-top: 3px solid #ef4444;">
                    <div class="sb-card-title">🔴 CALL OI @ CENTRE</div>
                    <div class="sb-card-val">{_w.ce_oi:,.0f}</div>
                    <div class="sb-card-delta">
                        {ce_delta_html}
                    </div>
                </div>
                <!-- Card 3: Put OI @ Centre -->
                <div class="sb-card" style="border-top: 3px solid #10b981;">
                    <div class="sb-card-title">🟢 PUT OI @ CENTRE</div>
                    <div class="sb-card-val">{_w.pe_oi:,.0f}</div>
                    <div class="sb-card-delta">
                        {pe_delta_html}
                    </div>
                </div>
                <!-- Card 4: Call/Put Ratio & PCR -->
                <div class="sb-card" style="border-top: 3px solid #f59e0b;">
                    <div class="sb-card-title">⚖️ C/P RATIO (PCR {pcr_equiv:.2f})</div>
                    <div class="sb-card-val">{_w.ratio:.2f}</div>
                    <div class="sb-card-delta">
                        {ratio_badge}
                    </div>
                </div>
            </div>"""
        )

        # ── 3. Combined Side-by-Side 50/50 Card with Verdict Badge ───────────
        if _w.agree:
            verdict_badge = """<div style="margin-top: 12px; background: rgba(16, 185, 129, 0.12); border: 1.5px solid #10b981; border-radius: 6px; padding: 10px 16px; display: flex; align-items: center; gap: 12px;"><span style="font-size: 1.25em; color: #10b981;">✓</span><div><span style="color: #34d399; font-weight: 800; font-size: 0.92em; letter-spacing: 0.5px;">CONSENSUS ALIGNMENT: </span><span style="color: #e2e8f0; font-size: 0.88em;">Both Institutional Writer Defense and MJ's Retail Trap models point in the same direction. High directional conviction.</span></div></div>"""
        else:
            verdict_badge = """<div style="margin-top: 12px; background: rgba(245, 158, 11, 0.12); border: 1.5px solid #f59e0b; border-radius: 6px; padding: 10px 16px; display: flex; align-items: center; gap: 12px;"><span style="font-size: 1.25em; color: #f59e0b;">⚡</span><div><span style="color: #fbbf24; font-weight: 800; font-size: 0.92em; letter-spacing: 0.5px;">MODEL DIVERGENCE: </span><span style="color: #e2e8f0; font-size: 0.88em;">Institutional and Contrarian models conflict. System score uses Conventional Writer defense; MJ's contrarian signal is isolated for cross-examination.</span></div></div>"""

        _render_html(
            f"""<div class="sb-reads-container">
                <div class="sb-reads-header">
                    <span style="font-size: 0.82em; text-transform: uppercase; letter-spacing: 1px; color: #8fa6b8; font-weight: 700;">
                        ⚡ Centre Strike Positioning Cross-Examination
                    </span>
                    <span style="font-size: 0.78em; color: #64748b; font-family: monospace;">
                        CENTRE: {_centre} · SPOT: {spot:,.1f}
                    </span>
                </div>
                <div class="sb-reads-grid">
                    <!-- Column 1: Conventional Read -->
                    <div class="sb-read-col" style="border-left: 3px solid #3b82f6;">
                        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px;">
                            <span class="sb-pill" style="background: rgba(59, 130, 246, 0.16); color: #60a5fa; border: 1px solid rgba(59, 130, 246, 0.35);">
                                🏛️ CONVENTIONAL (WRITER DEFENSE)
                            </span>
                        </div>
                        <div style="color: #f1f5f9; font-weight: 600; font-size: 0.95em; line-height: 1.4;">
                            {_w.conventional}
                        </div>
                        <div style="font-size: 0.78em; color: #8fa6b8; margin-top: 6px; line-height: 1.35;">
                            Treats heavy open interest as well-capitalized institutional sellers anchoring and defending strike {_centre}.
                        </div>
                    </div>
                    <!-- Column 2: MJ's Contrarian Read -->
                    <div class="sb-read-col" style="border-left: 3px solid #a855f7;">
                        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px;">
                            <span class="sb-pill" style="background: rgba(168, 85, 247, 0.16); color: #c084fc; border: 1px solid rgba(168, 85, 247, 0.35);">
                                🎯 MJ'S CONTRARIAN (RETAIL TRAP)
                            </span>
                        </div>
                        <div style="color: #f1f5f9; font-weight: 600; font-size: 0.95em; line-height: 1.4;">
                            {_w.contrarian}
                        </div>
                        <div style="font-size: 0.78em; color: #8fa6b8; margin-top: 6px; line-height: 1.35;">
                            Assumes retail crowd concentration represents wrong-footed liquidity ripe for hunting and fast squeezes.
                        </div>
                    </div>
                </div>
                {verdict_badge}
            </div>"""
        )

    # ── 4. Auditable Bias Components Modern Data Grid ────────────────────────
    _render_html(
        """<div style="display: flex; justify-content: space-between; align-items: flex-end; margin-bottom: 8px;">
            <div>
                <span style="font-size: 1.05em; font-weight: 700; color: #f8fafc; letter-spacing: 0.5px;">
                    📊 Auditable Bias Components Data Grid
                </span>
                <div style="font-size: 0.8em; color: #8fa6b8; margin-top: 2px;">
                    Deconstructs live directional bias into 6 mathematical sub-models with weight sparklines.
                </div>
            </div>
        </div>"""
    )

    component_metadata = {
        "pcr": {
            "name": "Put-Call Ratio (Near-ATM)",
            "icon": "⚖️",
            "desc": "Near-ATM Put vs Call ratio relative to institutional equilibrium threshold.",
        },
        "oi_positioning": {
            "name": "OI Strike Positioning",
            "icon": "🎯",
            "desc": "Detects where open interest is clustering relative to current spot price trajectory.",
        },
        "oi_unwinding": {
            "name": "OI Unwinding / Covering",
            "icon": "⚡",
            "desc": "Tracks where open interest is being surrendered or aggressively shed by writers.",
        },
        "wall_migration": {
            "name": "Wall Migration Speed",
            "icon": "🌊",
            "desc": "Measures intraday drift and migration direction of primary S1 / R1 strike barriers.",
        },
        "position": {
            "name": "Spot vs Boundary Walls",
            "icon": "📏",
            "desc": "Measures spot price headroom and compression relative to nearest support and resistance.",
        },
        "wall_break": {
            "name": "Wall Penetration Momentum",
            "icon": "💥",
            "desc": "Evaluates direct break tests and persistent holds beyond major strike boundaries.",
        },
    }

    comp_items = read.components.items() if hasattr(read, "components") and read.components else []
    max_weight = max([abs(v) for _, v in comp_items] or [1.0])
    if max_weight <= 0:
        max_weight = 1.0

    table_rows_html = []
    for k, v in comp_items:
        meta = component_metadata.get(
            k,
            {"name": k.replace("_", " ").title(), "icon": "🔹", "desc": "Auditable quantitative component."},
        )

        # Direction status pill
        if v > 0:
            dir_html = """<span class="sb-pill sb-pill-bullish">▲ BULLISH</span>"""
            val_col = "#10b981"
            pct = min(50.0, round((abs(v) / max_weight) * 50.0, 1))
            bar_style = f"left: 50%; width: {pct}%; background: #10b981; box-shadow: 0 0 8px rgba(16,185,129,0.5);"
        elif v < 0:
            dir_html = """<span class="sb-pill sb-pill-bearish">▼ BEARISH</span>"""
            val_col = "#ef4444"
            pct = min(50.0, round((abs(v) / max_weight) * 50.0, 1))
            bar_style = f"left: {50.0 - pct}%; width: {pct}%; background: #ef4444; box-shadow: 0 0 8px rgba(239,68,68,0.5);"
        else:
            dir_html = """<span class="sb-pill sb-pill-neutral">● NEUTRAL</span>"""
            val_col = "#94a3b8"
            bar_style = "display: none;"

        sparkline_html = (
            f'<div style="display: inline-flex; align-items: center;">'
            f'<div class="sparkline-track"><div class="sparkline-center"></div><div class="sparkline-bar" style="{bar_style}"></div></div>'
            f'<span style="font-family: \'JetBrains Mono\', monospace; font-weight: 700; color: {val_col}; font-size: 0.95em; min-width: 48px;">{v:+0.2f}</span>'
            f'</div>'
        )

        row_html = (
            f'<tr>'
            f'<td style="font-weight: 600; color: #f1f5f9;"><div style="display: flex; align-items: center; gap: 8px;"><span>{meta["icon"]}</span><span>{meta["name"]}</span></div></td>'
            f'<td>{dir_html}</td>'
            f'<td>{sparkline_html}</td>'
            f'<td style="color: #94a3b8; font-size: 0.85em;">{meta["desc"]}</td>'
            f'</tr>'
        )
        table_rows_html.append(row_html)

    # Summary Row
    bias_score = getattr(read, "bias_score", 0.0)
    regime = getattr(read, "regime", "RANGEBOUND")
    if bias_score > 0.5:
        sum_pill = """<span class="sb-pill sb-pill-bullish">BULLISH BIAS</span>"""
        sum_col = "#10b981"
    elif bias_score < -0.5:
        sum_pill = """<span class="sb-pill sb-pill-bearish">BEARISH BIAS</span>"""
        sum_col = "#ef4444"
    else:
        sum_pill = """<span class="sb-pill sb-pill-neutral">BALANCED / RANGE</span>"""
        sum_col = "#f59e0b"

    summary_row_html = (
        f'<tr style="background: #090e17;">'
        f'<td style="font-weight: 800; color: #f8fafc; font-size: 0.95em; border-top: 2px solid #334155;">🎯 TOTAL COMPOSITE BIAS</td>'
        f'<td style="border-top: 2px solid #334155;">{sum_pill}</td>'
        f'<td style="border-top: 2px solid #334155;"><span style="font-family: \'JetBrains Mono\', monospace; font-size: 1.25em; font-weight: 900; color: {sum_col};">{bias_score:+0.2f}</span></td>'
        f'<td style="border-top: 2px solid #334155; color: #cbd5e1; font-weight: 600;">Active Regime: <span style="color: {sum_col};">{regime}</span></td>'
        f'</tr>'
    )

    full_grid_html = (
        f'<table class="sb-grid-table">'
        f'<thead><tr>'
        f'<th style="width: 25%;">Component</th>'
        f'<th style="width: 15%;">Direction</th>'
        f'<th style="width: 25%;">Weight Sparkline</th>'
        f'<th style="width: 35%;">Auditable Role in Model</th>'
        f'</tr></thead>'
        f'<tbody>'
        f'{"".join(table_rows_html)}'
        f'{summary_row_html}'
        f'</tbody></table>'
    )
    _render_html(full_grid_html)

    st.caption(
        "Audit Principle: Every component is exposed so the trader can verify the score without a black box. "
        "oi_positioning tracks WHERE open interest is building relative to spot; oi_unwinding tracks where it is "
        "being surrendered. Premium directional movement is intentionally separated to eliminate price-echo bias."
    )
