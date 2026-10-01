"""
snapshots_tab.py — "📸 SNAPSHOTS" Dashboard Tab for NIFTY.

Displays 5-MIN and 15-MIN periodic snapshots parsed from data/observations/YYYY-MM-DD.md:
- Timeline of snapshots (most recent at top)
- Toggle: [ All | 5-min only | 15-min only ]
- 5-min cards: blue left border (#2563EB)
- 15-min cards: purple left border (#7C3AED)
- Dark-navy design language:
  #0B1B3A cards, #D4AF37 gold accents, #22C55E green, #EF4444 red
- Auto-refreshes when a new snapshot lands
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import streamlit as st

import config

logger = logging.getLogger("snapshots_tab")


def load_observation_snapshots(target_date: date) -> List[Dict[str, Any]]:
    """
    Reads data/observations/YYYY-MM-DD.md and parses all entries into structured dicts.
    """
    obs_dir = config.DATA_DIR / "observations"
    file_path = obs_dir / f"{target_date.strftime('%Y-%m-%d')}.md"
    if not file_path.exists():
        # Fallback to most recent observation log
        avail_files = sorted(obs_dir.glob("*.md"))
        if avail_files:
            file_path = avail_files[-1]
        else:
            return []

    try:
        content = file_path.read_text(encoding="utf-8")
    except Exception as exc:
        logger.warning(f"Error reading {file_path}: {exc}")
        return []

    entries: List[Dict[str, Any]] = []
    # Split by separator
    raw_blocks = content.split("---")
    header_pattern = re.compile(r"^##\s+(\d{2}:\d{2})\s+\|\s+Spot\s+([0-9,]+(?:\s+\([^)]+\))?)\s+\|\s+(.+)$", re.MULTILINE)

    for block in raw_blocks:
        block = block.strip()
        if not block or "## " not in block:
            continue

        match = header_pattern.search(block)
        if not match:
            continue

        time_str = match.group(1).strip()
        spot_str = match.group(2).strip()
        bias_str = match.group(3).strip()

        # Detect timeframe tag
        tf = "3m"  # default
        if "<!-- tf: 5m" in block:
            tf = "5m"
        elif "<!-- tf: 15m" in block:
            tf = "15m"
        elif "<!-- tf: 3m" in block:
            tf = "3m"

        # Parse key-value bullet points
        fields: Dict[str, str] = {}
        alerts: List[str] = []
        for line in block.splitlines():
            line = line.strip()
            if line.startswith("- Alert:"):
                alerts.append(line.replace("- Alert:", "").strip())
            elif line.startswith("- ") and ":" in line:
                parts = line[2:].split(":", 1)
                k = parts[0].strip()
                v = parts[1].strip()
                fields[k] = v

        entries.append({
            "time": time_str,
            "spot": spot_str,
            "bias": bias_str,
            "tf": tf,
            "fields": fields,
            "alerts": alerts,
            "raw": block,
        })

    # Sort most recent first
    entries.sort(key=lambda x: x["time"], reverse=True)
    return entries


def render_snapshots_tab(target_date: Optional[date] = None) -> None:
    """
    Renders the Snapshots tab in Streamlit.
    """
    if target_date is None:
        target_date = date.today()

    st.markdown(
        """
        <style>
        .snap-card {
            background-color: #0B1B3A;
            border-radius: 8px;
            padding: 16px 20px;
            margin-bottom: 16px;
            color: #F3F4F6;
            box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.4);
        }
        .snap-card-5m {
            border-left: 6px solid #2563EB;
        }
        .snap-card-15m {
            border-left: 6px solid #7C3AED;
        }
        .snap-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid #1E293B;
            padding-bottom: 8px;
            margin-bottom: 12px;
        }
        .badge-5m {
            background-color: #2563EB;
            color: #FFFFFF;
            padding: 3px 8px;
            border-radius: 4px;
            font-weight: 700;
            font-size: 0.8rem;
        }
        .badge-15m {
            background-color: #7C3AED;
            color: #FFFFFF;
            padding: 3px 8px;
            border-radius: 4px;
            font-weight: 700;
            font-size: 0.8rem;
        }
        .badge-3m {
            background-color: #4B5563;
            color: #FFFFFF;
            padding: 3px 8px;
            border-radius: 4px;
            font-weight: 700;
            font-size: 0.8rem;
        }
        .gold-txt {
            color: #D4AF37;
            font-weight: 700;
        }
        .bias-bull {
            background-color: rgba(34, 197, 94, 0.2);
            color: #22C55E;
            padding: 3px 8px;
            border-radius: 4px;
            font-weight: 700;
            font-size: 0.85rem;
            border: 1px solid #22C55E;
        }
        .bias-bear {
            background-color: rgba(239, 68, 68, 0.2);
            color: #EF4444;
            padding: 3px 8px;
            border-radius: 4px;
            font-weight: 700;
            font-size: 0.85rem;
            border: 1px solid #EF4444;
        }
        .bias-neutral {
            background-color: rgba(156, 163, 175, 0.2);
            color: #9CA3AF;
            padding: 3px 8px;
            border-radius: 4px;
            font-weight: 700;
            font-size: 0.85rem;
            border: 1px solid #9CA3AF;
        }
        .field-row {
            display: flex;
            justify-content: space-between;
            padding: 4px 0;
            font-size: 0.9rem;
            border-bottom: 1px solid rgba(255, 255, 255, 0.05);
        }
        .field-label {
            color: #9CA3AF;
            font-weight: 600;
            width: 130px;
        }
        .field-value {
            color: #F9FAFB;
            flex: 1;
            text-align: right;
            font-weight: 500;
        }
        .action-banner {
            margin-top: 10px;
            padding: 8px 12px;
            border-radius: 6px;
            background-color: #1E293B;
            font-weight: 700;
            display: flex;
            align-items: center;
            justify-content: space-between;
        }
        .alert-banner {
            margin-top: 8px;
            padding: 6px 12px;
            border-radius: 4px;
            background-color: rgba(239, 68, 68, 0.15);
            border-left: 4px solid #EF4444;
            color: #FCA5A5;
            font-size: 0.85rem;
            font-weight: 600;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    col1, col2 = st.columns([2, 1])
    with col1:
        st.subheader("📸 Periodic Snapshots (5-MIN & 15-MIN)")
    with col2:
        filter_opt = st.radio(
            "Filter",
            options=["All", "5-min only", "15-min only"],
            index=0,
            horizontal=True,
            label_visibility="collapsed",
        )

    snapshots = load_observation_snapshots(target_date)

    if not snapshots:
        st.info("No periodic snapshots recorded for today yet. Snapshots fire at closed candle boundaries.")
        return

    # Filter according to selection
    if filter_opt == "5-min only":
        filtered = [s for s in snapshots if s["tf"] == "5m"]
    elif filter_opt == "15-min only":
        filtered = [s for s in snapshots if s["tf"] == "15m"]
    else:
        filtered = [s for s in snapshots if s["tf"] in ("5m", "15m")]

    if not filtered:
        st.info(f"No {filter_opt} snapshots found in today's observation log.")
        return

    st.caption(f"Showing {len(filtered)} snapshot cards (most recent first) • Auto-refreshes on incoming data")

    for s in filtered:
        tf = s["tf"]
        card_class = "snap-card-5m" if tf == "5m" else ("snap-card-15m" if tf == "15m" else "snap-card")
        badge_class = f"badge-{tf}"
        badge_txt = "5-MIN" if tf == "5m" else ("15-MIN" if tf == "15m" else "3-MIN")

        bias = s["bias"]
        if "BULLISH" in bias:
            bias_class = "bias-bull"
        elif "BEARISH" in bias:
            bias_class = "bias-bear"
        else:
            bias_class = "bias-neutral"

        fields = s["fields"]
        alerts = s["alerts"]

        # Card container
        st.markdown(
            f"""
            <div class="snap-card {card_class}">
                <div class="snap-header">
                    <div>
                        <span class="{badge_class}">{badge_txt}</span>
                        &nbsp;&nbsp;<span style="font-size: 1.1rem; font-weight: 700; color: #FFFFFF;">{s['time']}</span>
                        &nbsp;&nbsp;<span class="gold-txt" style="font-size: 1.1rem;">Spot {s['spot']}</span>
                    </div>
                    <div>
                        <span class="{bias_class}">{bias}</span>
                    </div>
                </div>
            """,
            unsafe_allow_html=True,
        )

        # Content rows
        display_keys = [
            ("Day range", fields.get("Day range")),
            ("GEX", fields.get("GEX")),
            ("RVOL", fields.get("RVOL")),
            ("IV", fields.get("IV")),
            ("Floor / Ceiling", fields.get("Floor", "") + (" | " + fields.get("Ceiling", "") if "Ceiling" in fields else "")),
            ("Flow", fields.get("Flow")),
            ("Dominance", fields.get("Dominance")),
            ("Structure", fields.get("Structure")),
            ("Floor weakening", fields.get("Floor weakening")),
            ("Writer lines", fields.get("Writer lines")),
            ("Checklist", fields.get("Checklist")),
        ]

        row_htmls = []
        for k, val in display_keys:
            if val:
                row_htmls.append(
                    f"""
                    <div class="field-row">
                        <span class="field-label">{k}</span>
                        <span class="field-value">{val}</span>
                    </div>
                    """
                )

        if row_htmls:
            st.markdown("".join(row_htmls), unsafe_allow_html=True)

        # Action line callout
        act_val = fields.get("Action", "STAND ASIDE")
        act_color = "#22C55E" if "LONG" in act_val else ("#EF4444" if "SHORT" in act_val else ("#D4AF37" if "PULLBACK" in act_val else "#9CA3AF"))
        st.markdown(
            f"""
            <div class="action-banner">
                <span style="color: #9CA3AF; font-size: 0.85rem;">ACTION</span>
                <span style="color: {act_color};">{act_val}</span>
            </div>
            """,
            unsafe_allow_html=True,
        )

        # Alerts if any
        if alerts:
            for al in alerts:
                st.markdown(
                    f"""
                    <div class="alert-banner">
                        🚨 Alert: {al}
                    </div>
                    """,
                    unsafe_allow_html=True,
                )

        st.markdown("</div>", unsafe_allow_html=True)


if __name__ == "__main__":
    st.set_page_config(page_title="NIFTY Snapshots", layout="wide")
    render_snapshots_tab()
