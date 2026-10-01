"""
volume_profile_tab.py — Volume Profile & Nifty 50 Auction Market Analysis Tab.

Institutional Auction Market Theory (AMT) engine with dual-pane Plotly charting
and high-conviction decision cockpit for NIFTY 50 options traders.

Features:
  1. Price-Based Volume Profile: POC (Fair Price Magnet), 70% Value Area (VAH/VAL),
     High Volume Nodes (HVN Mini S/R), and Low Volume Nodes (LVN Breakout Highways).
  2. Market Profile Shape Scanner: D-Shape (Balanced), P-Shape (Fast Downward Break),
     and B-Shape (Bullish Structure) classification.
  3. Nifty 50 Algorithmic Rules Engine:
     - 09:40 AM Opening Time Filter (Pre-09:40 is observation only).
     - <=0.65% Shallow Pullback Buy vs. >0.65% Deep Drop (Higher Low required).
     - Anti-Trap Rule: Theta Decay Shield disabling buy/sell signals at POC.
     - LVN Breakout Entry with massive exhaustion candle invalidation.
     - Structure-Based Stop Loss placed tightly outside HVN/LVN shelves.
  4. Trader Decision Cockpit: Market State HUD (Balanced vs Imbalanced), Distance
     to POC Magnet, Shape Bias, and Actionable Trade Signals table.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st
from ui_utils import render_html, _render_html


# =============================================================================
# UI Color Palette & Design Constants
# =============================================================================
NAVY = "#071626"
NAVY2 = "#0B2035"
SLATE = "#1E293B"
TEXT_MUTED = "#94A3B8"
TEXT_LIGHT = "#F8FAFC"
GOLD = "#FFB703"          # POC Fair Price Magnet
CYAN = "#00D4FF"          # Value Area (70%)
EMERALD = "#10B981"       # VAL Support / Buy
CRIMSON = "#EF4444"       # VAH Resistance / Sell
PURPLE = "#A855F7"        # HVN Mini S/R
AMBER = "#F59E0B"         # LVN Breakout Highway / Warnings


# =============================================================================
# Data Structures
# =============================================================================

@dataclass
class VolumeNode:
    price: float
    volume: float
    node_type: str        # 'HVN' or 'LVN'
    label: str
    price_low: float
    price_high: float


@dataclass
class ProfileShape:
    shape: str            # 'D-Shape', 'P-Shape', 'B-Shape'
    status_tag: str
    description: str
    poc_relative: float   # 0.0 (bottom) to 1.0 (top)


@dataclass
class AlgorithmicSignal:
    timestamp: Any
    signal_type: str      # 'BUY', 'SELL', 'BLOCKED', 'INVALIDATED'
    trigger_rule: str
    entry_price: float
    stop_loss: float
    target_price: float
    risk_points: float
    reward_points: float
    is_valid: bool
    status_reason: str


@dataclass
class VolumeProfileResult:
    bins: np.ndarray
    profile: np.ndarray
    poc_price: float
    poc_idx: int
    vah_price: float
    val_price: float
    value_area_volume: float
    total_volume: float
    inside_va_mask: np.ndarray
    nodes: List[VolumeNode]
    shape: ProfileShape
    signals: List[AlgorithmicSignal]


# =============================================================================
# Quantitative Engine
# =============================================================================

class VolumeProfileEngine:
    """
    Auction Market Theory (AMT) calculation engine for NIFTY 50.
    """

    def __init__(
        self,
        bin_size: float = 5.0,
        value_area_pct: float = 0.70,
        time_filter_start: time = time(9, 40),
        max_drop_pct: float = 0.65,
        poc_zone_pts: float = 12.0,
        sl_buffer_pts: float = 6.0,
    ):
        self.bin_size = bin_size
        self.value_area_pct = value_area_pct
        self.time_filter_start = time_filter_start
        self.max_drop_pct = max_drop_pct
        self.poc_zone_pts = poc_zone_pts
        self.sl_buffer_pts = sl_buffer_pts

    def compute_profile(self, df: pd.DataFrame) -> VolumeProfileResult:
        """Computes volume profile, Steidlmayer 70% value area, nodes, and algorithmic rules."""
        if df.empty:
            raise ValueError("Input DataFrame is empty.")

        col_map = {c.lower(): c for c in df.columns}
        low_s = df[col_map["low"]].astype(float)
        high_s = df[col_map["high"]].astype(float)
        close_s = df[col_map["close"]].astype(float)
        open_s = df[col_map["open"]].astype(float)
        vol_s = df[col_map["volume"]].astype(float) if "volume" in col_map else pd.Series(100000.0, index=df.index)

        # 1. Distribute volume across price bins
        p_min = np.floor(low_s.min() / self.bin_size) * self.bin_size
        p_max = np.ceil(high_s.max() / self.bin_size) * self.bin_size
        bins = np.arange(p_min, p_max + self.bin_size, self.bin_size)
        profile = np.zeros(len(bins), dtype=float)

        for l, h, v in zip(low_s, high_s, vol_s):
            if h == l:
                idx = int((l - p_min) / self.bin_size)
                if 0 <= idx < len(profile):
                    profile[idx] += v
            else:
                i_start = max(0, int((l - p_min) / self.bin_size))
                i_end = min(len(profile) - 1, int((h - p_min) / self.bin_size))
                num_spanned = max(1, i_end - i_start + 1)
                profile[i_start : i_end + 1] += v / num_spanned

        total_volume = float(np.sum(profile))

        # 2. Point of Control (POC)
        poc_idx = int(np.argmax(profile))
        poc_price = float(bins[poc_idx])

        # 3. 70% Value Area (Steidlmayer Expansion)
        target_vol = self.value_area_pct * total_volume
        accum_vol = profile[poc_idx]
        u_idx = poc_idx + 1
        l_idx = poc_idx - 1

        while accum_vol < target_vol and (u_idx < len(profile) or l_idx >= 0):
            vol_up = profile[u_idx] if u_idx < len(profile) else -1.0
            vol_down = profile[l_idx] if l_idx >= 0 else -1.0

            if vol_up >= vol_down:
                accum_vol += max(0.0, vol_up)
                u_idx += 1
            else:
                accum_vol += max(0.0, vol_down)
                l_idx -= 1

        val_idx = max(0, l_idx + 1)
        vah_idx = min(len(profile) - 1, u_idx - 1)
        val_price = float(bins[val_idx])
        vah_price = float(bins[vah_idx])
        inside_va_mask = (bins >= val_price) & (bins <= vah_price)

        # 4. Node Scanner (HVN & LVN)
        nodes = self._identify_nodes(bins, profile, poc_idx)

        # 5. Profile Shape Classification
        shape = self._classify_shape(bins, profile, poc_price, p_min, p_max)

        # 6. Algorithmic Signals
        signals = self._generate_algorithmic_signals(
            df, open_s, high_s, low_s, close_s, poc_price, vah_price, val_price, nodes
        )

        return VolumeProfileResult(
            bins=bins,
            profile=profile,
            poc_price=poc_price,
            poc_idx=poc_idx,
            vah_price=vah_price,
            val_price=val_price,
            value_area_volume=accum_vol,
            total_volume=total_volume,
            inside_va_mask=inside_va_mask,
            nodes=nodes,
            shape=shape,
            signals=signals,
        )

    def _identify_nodes(
        self, bins: np.ndarray, profile: np.ndarray, poc_idx: int
    ) -> List[VolumeNode]:
        """Identifies High Volume Nodes (HVNs) and Low Volume Nodes (LVNs)."""
        nodes: List[VolumeNode] = []
        if len(profile) < 5:
            return nodes

        kernel = np.array([0.2, 0.6, 0.2])
        smoothed = np.convolve(profile, kernel, mode="same")
        poc_vol = smoothed[poc_idx]
        mean_vol = np.mean(smoothed)

        hvn_indices, lvn_indices = [], []

        for i in range(1, len(smoothed) - 1):
            if i == poc_idx:
                continue

            # Local Peak (HVN)
            if smoothed[i] > smoothed[i - 1] and smoothed[i] > smoothed[i + 1]:
                if smoothed[i] >= 0.45 * poc_vol and smoothed[i] >= 0.85 * mean_vol:
                    hvn_indices.append(i)

            # Local Valley (LVN)
            if smoothed[i] < smoothed[i - 1] and smoothed[i] < smoothed[i + 1]:
                if smoothed[i] <= 0.65 * mean_vol:
                    lvn_indices.append(i)

        for idx in hvn_indices:
            nodes.append(
                VolumeNode(
                    price=float(bins[idx]),
                    volume=float(profile[idx]),
                    node_type="HVN",
                    label="Mini Support/Resistance (Consolidation Zone)",
                    price_low=float(bins[idx] - self.bin_size),
                    price_high=float(bins[idx] + self.bin_size),
                )
            )

        for idx in lvn_indices:
            nodes.append(
                VolumeNode(
                    price=float(bins[idx]),
                    volume=float(profile[idx]),
                    node_type="LVN",
                    label="Breakout Highway (Liquidity Void)",
                    price_low=float(bins[idx] - self.bin_size),
                    price_high=float(bins[idx] + self.bin_size),
                )
            )

        return nodes

    def _classify_shape(
        self,
        bins: np.ndarray,
        profile: np.ndarray,
        poc_price: float,
        p_min: float,
        p_max: float,
    ) -> ProfileShape:
        """Classifies Profile into D-Shape, P-Shape, or B-Shape."""
        price_range = max(1.0, p_max - p_min)
        poc_rel = (poc_price - p_min) / price_range

        mid_price = p_min + 0.5 * price_range
        upper_vol = np.sum(profile[bins >= mid_price])
        lower_vol = np.sum(profile[bins < mid_price])
        total_vol = max(1.0, upper_vol + lower_vol)
        upper_ratio = upper_vol / total_vol

        if poc_rel >= 0.60 or (poc_rel > 0.55 and upper_ratio >= 0.58):
            return ProfileShape(
                shape="P-Shape Profile",
                status_tag="Fast Downward Move Expected on Break",
                description="POC clustered near highs with a thin lower stem. Typical of short-covering exhaustion or top distribution. Breaking below the P-head triggers fast downward liquidation through thin volume.",
                poc_relative=poc_rel,
            )
        elif poc_rel <= 0.40 or (poc_rel < 0.45 and upper_ratio <= 0.42):
            return ProfileShape(
                shape="B-Shape Profile",
                status_tag="Bullish Structure",
                description="POC clustered near lows with a thin upper stem. Typical of long-liquidation absorption or accumulation at support. Once congestion clears, structure favors upward expansion.",
                poc_relative=poc_rel,
            )
        else:
            return ProfileShape(
                shape="D-Shape Profile",
                status_tag="Sideways/Balanced Market",
                description="Symmetrical bell-shaped profile with POC centered. Balanced two-sided auction where buyers and sellers agree on fair value. Mean-reversion rules apply between VAH and VAL.",
                poc_relative=poc_rel,
            )

    def _generate_algorithmic_signals(
        self,
        df: pd.DataFrame,
        open_s: pd.Series,
        high_s: pd.Series,
        low_s: pd.Series,
        close_s: pd.Series,
        poc: float,
        vah: float,
        val: float,
        nodes: List[VolumeNode],
    ) -> List[AlgorithmicSignal]:
        """Evaluates Nifty 50 algorithmic rules."""
        signals: List[AlgorithmicSignal] = []
        n = len(df)
        if n < 3:
            return signals

        prev_close = close_s.shift(1).fillna(open_s)
        tr = np.maximum(
            high_s - low_s,
            np.maximum((high_s - prev_close).abs(), (low_s - prev_close).abs()),
        )
        atr_14 = tr.rolling(window=14, min_periods=1).mean().values

        swing_high = high_s.iloc[0]
        recent_low = low_s.iloc[0]
        higher_low_confirmed = False

        lvn_zones = [node for node in nodes if node.node_type == "LVN"]
        hvn_zones = [node for node in nodes if node.node_type == "HVN"]

        for i in range(n):
            ts = df.index[i]
            c_time = ts.time() if hasattr(ts, "time") else None
            c_open = open_s.iloc[i]
            c_high = high_s.iloc[i]
            c_low = low_s.iloc[i]
            c_close = close_s.iloc[i]
            c_atr = atr_14[i]
            c_range = max(c_high - c_low, 1e-4)

            if c_high > swing_high:
                swing_high = c_high
                higher_low_confirmed = False

            if c_low < recent_low:
                recent_low = c_low
                higher_low_confirmed = False
            elif c_close > c_open and c_low > recent_low + 6.0:
                higher_low_confirmed = True

            # 1. Time Filter (09:40 AM)
            if c_time and c_time < self.time_filter_start:
                continue

            # 2. Anti-Trap Rule (Theta Decay at POC)
            is_at_poc = abs(c_close - poc) <= self.poc_zone_pts

            # 3. Percentage Move Logic
            pct_drop = ((swing_high - c_close) / swing_high) * 100.0
            test_val = abs(c_low - val) <= 15.0 or (c_low <= val and c_close >= val - 5.0)
            test_hvn = any(abs(c_low - hvn.price) <= 12.0 for hvn in hvn_zones if hvn.price < poc)

            if (test_val or test_hvn) and c_close > c_open:
                if is_at_poc:
                    signals.append(
                        AlgorithmicSignal(
                            timestamp=ts,
                            signal_type="BLOCKED",
                            trigger_rule="Anti-Trap Rule (Theta Decay)",
                            entry_price=c_close,
                            stop_loss=val - self.sl_buffer_pts,
                            target_price=vah,
                            risk_points=round(c_close - (val - self.sl_buffer_pts), 1),
                            reward_points=round(vah - c_close, 1),
                            is_valid=False,
                            status_reason="BLOCKED: Price hovering at POC Fair Price. Sideways zone destroys option buyers.",
                        )
                    )
                elif pct_drop <= self.max_drop_pct:
                    sl = (val if test_val else min(hvn.price for hvn in hvn_zones)) - self.sl_buffer_pts
                    signals.append(
                        AlgorithmicSignal(
                            timestamp=ts,
                            signal_type="BUY",
                            trigger_rule="<=0.65% Shallow Pullback Buy",
                            entry_price=c_close,
                            stop_loss=round(sl, 1),
                            target_price=round(poc if c_close < poc else vah, 1),
                            risk_points=round(c_close - sl, 1),
                            reward_points=round((poc if c_close < poc else vah) - c_close, 1),
                            is_valid=True,
                            status_reason=f"VALID: Shallow drop of {pct_drop:.2f}% (<=0.65%). Direct entry at first support.",
                        )
                    )
                else:
                    if higher_low_confirmed:
                        sl = recent_low - self.sl_buffer_pts
                        signals.append(
                            AlgorithmicSignal(
                                timestamp=ts,
                                signal_type="BUY",
                                trigger_rule=">0.65% Deep Drop with Higher Low",
                                entry_price=c_close,
                                stop_loss=round(sl, 1),
                                target_price=round(poc if c_close < poc else vah, 1),
                                risk_points=round(c_close - sl, 1),
                                reward_points=round((poc if c_close < poc else vah) - c_close, 1),
                                is_valid=True,
                                status_reason=f"VALID: Deep drop ({pct_drop:.2f}%) stabilized. Higher Low confirmed.",
                            )
                        )
                    else:
                        signals.append(
                            AlgorithmicSignal(
                                timestamp=ts,
                                signal_type="BLOCKED",
                                trigger_rule=">0.65% Deep Drop (Unconfirmed)",
                                entry_price=c_close,
                                stop_loss=val - self.sl_buffer_pts,
                                target_price=vah,
                                risk_points=round(c_close - (val - self.sl_buffer_pts), 1),
                                reward_points=round(vah - c_close, 1),
                                is_valid=False,
                                status_reason=f"BLOCKED: Drop is {pct_drop:.2f}% (>0.65%). Immediate buy blocked until Higher Low forms.",
                            )
                        )

            # 4. LVN Breakout Entry & Invalidation
            for lvn in lvn_zones:
                entering_lvn_bullish = c_low <= lvn.price_low and c_close > lvn.price_low and c_close < lvn.price_high
                entering_lvn_bearish = c_high >= lvn.price_high and c_close < lvn.price_high and c_close > lvn.price_low

                if entering_lvn_bullish or entering_lvn_bearish:
                    is_massive_candle = (c_range > 2.2 * c_atr) or (c_close >= lvn.price_high - 1.0)
                    if is_massive_candle:
                        signals.append(
                            AlgorithmicSignal(
                                timestamp=ts,
                                signal_type="INVALIDATED",
                                trigger_rule="LVN Breakout (Exhaustion Invalidation)",
                                entry_price=c_close,
                                stop_loss=lvn.price_low - self.sl_buffer_pts if entering_lvn_bullish else lvn.price_high + self.sl_buffer_pts,
                                target_price=vah if entering_lvn_bullish else val,
                                risk_points=round(abs(c_close - lvn.price_low), 1),
                                reward_points=round(abs(vah - c_close), 1),
                                is_valid=False,
                                status_reason="INVALIDATED: Massive candle formed past breakout point. Chasing exhaustion risk.",
                            )
                        )
                    elif not is_at_poc:
                        sl = (lvn.price_low - self.sl_buffer_pts) if entering_lvn_bullish else (lvn.price_high + self.sl_buffer_pts)
                        tgt = vah if entering_lvn_bullish else val
                        signals.append(
                            AlgorithmicSignal(
                                timestamp=ts,
                                signal_type="BUY" if entering_lvn_bullish else "SELL",
                                trigger_rule="LVN Highway Breakout",
                                entry_price=c_close,
                                stop_loss=round(sl, 1),
                                target_price=round(tgt, 1),
                                risk_points=round(abs(c_close - sl), 1),
                                reward_points=round(abs(tgt - c_close), 1),
                                is_valid=True,
                                status_reason=f"VALID: Price entered Low Volume Node @ {lvn.price:.1f}. High velocity highway expected.",
                            )
                        )

        return signals


# =============================================================================
# Plotly Chart Builder
# =============================================================================

class VolumeProfileChartBuilder:
    @staticmethod
    def build_figure(
        df: pd.DataFrame,
        res: VolumeProfileResult,
        show_poc: bool = True,
        show_va: bool = True,
        show_nodes: bool = True,
        show_signals: bool = True,
    ) -> go.Figure:
        fig = make_subplots(
            rows=1,
            cols=2,
            column_widths=[0.78, 0.22],
            shared_yaxes=True,
            horizontal_spacing=0.012,
            subplot_titles=("NIFTY 50 Auction Levels & Candlesticks", "Volume Profile (Price)"),
        )

        col_map = {c.lower(): c for c in df.columns}
        dates = df.index

        fig.add_trace(
            go.Candlestick(
                x=dates,
                open=df[col_map["open"]],
                high=df[col_map["high"]],
                low=df[col_map["low"]],
                close=df[col_map["close"]],
                name="NIFTY 50",
                increasing_line_color=EMERALD,
                decreasing_line_color=CRIMSON,
                increasing_fillcolor=EMERALD,
                decreasing_fillcolor=CRIMSON,
            ),
            row=1,
            col=1,
        )

        if show_va:
            fig.add_hrect(
                y0=res.val_price,
                y1=res.vah_price,
                fillcolor="rgba(0, 212, 255, 0.05)",
                line_width=0,
                row=1,
                col=1,
            )

        if show_poc:
            fig.add_hline(
                y=res.poc_price,
                line=dict(color=GOLD, width=2.5, dash="solid"),
                annotation_text=f"POC Fair Price (Magnet): {res.poc_price:.1f}",
                annotation_position="top left",
                annotation_font=dict(color=GOLD, size=11, family="Inter, sans-serif"),
                row=1,
                col=1,
            )

        if show_va:
            fig.add_hline(
                y=res.vah_price,
                line=dict(color=CRIMSON, width=1.5, dash="dash"),
                annotation_text=f"VAH (Resistance): {res.vah_price:.1f}",
                annotation_position="top left",
                annotation_font=dict(color=CRIMSON, size=10, family="Inter, sans-serif"),
                row=1,
                col=1,
            )
            fig.add_hline(
                y=res.val_price,
                line=dict(color=EMERALD, width=1.5, dash="dash"),
                annotation_text=f"VAL (Support): {res.val_price:.1f}",
                annotation_position="bottom left",
                annotation_font=dict(color=EMERALD, size=10, family="Inter, sans-serif"),
                row=1,
                col=1,
            )

        if show_nodes:
            for node in res.nodes:
                if node.node_type == "HVN":
                    fig.add_hrect(
                        y0=node.price_low,
                        y1=node.price_high,
                        fillcolor="rgba(168, 85, 247, 0.12)",
                        line=dict(color="rgba(168, 85, 247, 0.4)", width=1, dash="dot"),
                        annotation_text=f"HVN (Mini S/R): {node.price:.1f}",
                        annotation_position="right",
                        annotation_font=dict(color="#C084FC", size=9),
                        row=1,
                        col=1,
                    )
                elif node.node_type == "LVN":
                    fig.add_hrect(
                        y0=node.price_low,
                        y1=node.price_high,
                        fillcolor="rgba(245, 158, 11, 0.12)",
                        line=dict(color="rgba(245, 158, 11, 0.5)", width=1, dash="dot"),
                        annotation_text=f"LVN Highway: {node.price:.1f}",
                        annotation_position="right",
                        annotation_font=dict(color=AMBER, size=9),
                        row=1,
                        col=1,
                    )

        if show_signals:
            valid_buys = [s for s in res.signals if s.signal_type == "BUY" and s.is_valid]
            invalid_signals = [s for s in res.signals if not s.is_valid]

            if valid_buys:
                fig.add_trace(
                    go.Scatter(
                        x=[s.timestamp for s in valid_buys],
                        y=[s.entry_price - 6.0 for s in valid_buys],
                        mode="markers+text",
                        marker=dict(symbol="triangle-up", size=14, color="#00E676", line=dict(width=1, color="#FFFFFF")),
                        text=["BUY" for _ in valid_buys],
                        textposition="bottom center",
                        textfont=dict(color="#00E676", size=10),
                        name="Valid Buys",
                        hovertext=[f"{s.trigger_rule}<br>Entry: {s.entry_price:.1f}<br>SL: {s.stop_loss:.1f}<br>Tgt: {s.target_price:.1f}" for s in valid_buys],
                    ),
                    row=1,
                    col=1,
                )

            if invalid_signals:
                fig.add_trace(
                    go.Scatter(
                        x=[s.timestamp for s in invalid_signals],
                        y=[s.entry_price for s in invalid_signals],
                        mode="markers",
                        marker=dict(symbol="x", size=10, color=AMBER),
                        name="Blocked/Invalidated",
                        hovertext=[f"{s.trigger_rule}<br>{s.status_reason}" for s in invalid_signals],
                    ),
                    row=1,
                    col=1,
                )

        bar_colors = []
        hover_lines = []
        for i, p in enumerate(res.bins):
            v = res.profile[i]
            if i == res.poc_idx:
                bar_colors.append(GOLD)
                hover_lines.append(f"POC Fair Price Magnet: {p:.1f}<br>Volume: {v:,.0f}")
            elif res.val_price <= p <= res.vah_price:
                bar_colors.append(CYAN)
                hover_lines.append(f"Value Area (70%): {p:.1f}<br>Volume: {v:,.0f}")
            else:
                bar_colors.append("#334155")
                hover_lines.append(f"Outside Value Area: {p:.1f}<br>Volume: {v:,.0f}")

        fig.add_trace(
            go.Bar(
                x=res.profile,
                y=res.bins,
                orientation="h",
                marker=dict(color=bar_colors),
                name="Volume Profile",
                hovertext=hover_lines,
                hoverinfo="text",
            ),
            row=1,
            col=2,
        )

        if show_poc:
            fig.add_hline(y=res.poc_price, line=dict(color=GOLD, width=2), row=1, col=2)
        if show_va:
            fig.add_hline(y=res.vah_price, line=dict(color=CRIMSON, width=1.5, dash="dash"), row=1, col=2)
            fig.add_hline(y=res.val_price, line=dict(color=EMERALD, width=1.5, dash="dash"), row=1, col=2)

        fig.update_layout(
            template="plotly_dark",
            paper_bgcolor="#071626",
            plot_bgcolor="#071626",
            font=dict(family="Inter, -apple-system, BlinkMacSystemFont, sans-serif", color=TEXT_MUTED),
            height=760,
            margin=dict(l=30, r=30, t=50, b=40),
            showlegend=False,
            xaxis=dict(rangeslider=dict(visible=False), gridcolor="#1E293B"),
            yaxis=dict(gridcolor="#1E293B"),
            xaxis2=dict(title="Volume", gridcolor="#1E293B", showticklabels=False),
            yaxis2=dict(gridcolor="#1E293B"),
        )
        return fig


# =============================================================================
# Helper: Synthetic Data Generator (Fallback outside market hours)
# =============================================================================

def _generate_synthetic_nifty(timeframe: str = "5m", spot: float = 24550.0) -> pd.DataFrame:
    dates = pd.date_range("2026-09-04 09:15", "2026-09-04 15:25", freq=timeframe)
    n = len(dates)
    np.random.seed(42)

    p = spot
    prices = [p]
    for i in range(1, n):
        p += np.random.randn() * 9.5
        prices.append(p)
    prices = np.array(prices)

    return pd.DataFrame({
        "open": prices + np.random.uniform(-4, 4, n),
        "high": prices + np.random.uniform(4, 18, n),
        "low": prices - np.random.uniform(4, 18, n),
        "close": prices + np.random.uniform(-4, 4, n),
        "volume": np.random.randint(65000, 240000, n).astype(float),
    }, index=dates)


# =============================================================================
# Main Tab Render Function
# =============================================================================

def render(
    pa_frames: Optional[Dict[str, pd.DataFrame]] = None,
    spot: Optional[float] = None,
    read: Any = None,
    df: Optional[pd.DataFrame] = None,
) -> None:
    _render_html(
        """
        <style>
        .amt-hud-card {
            background: #0B2035;
            border: 1px solid #1E293B;
            border-radius: 10px;
            padding: 14px 18px;
            color: #F8FAFC;
            margin-bottom: 12px;
        }
        .amt-badge {
            font-size: 11px;
            font-weight: 700;
            padding: 3px 8px;
            border-radius: 6px;
            letter-spacing: 0.5px;
            text-transform: uppercase;
        }
        .amt-badge.balanced {
            background: rgba(0, 212, 255, 0.15);
            color: #00D4FF;
            border: 1px solid rgba(0, 212, 255, 0.35);
        }
        .amt-badge.imbalanced {
            background: rgba(239, 68, 68, 0.15);
            color: #EF4444;
            border: 1px solid rgba(239, 68, 68, 0.35);
        }
        .amt-badge.bullish {
            background: rgba(16, 185, 129, 0.15);
            color: #10B981;
            border: 1px solid rgba(16, 185, 129, 0.35);
        }
        .stat-label {
            font-size: 11px;
            font-weight: 600;
            color: #94A3B8;
            text-transform: uppercase;
        }
        .stat-value {
            font-size: 20px;
            font-weight: 700;
            color: #F8FAFC;
            font-family: 'JetBrains Mono', monospace;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    st.markdown("### 📊 Volume Profile & Nifty 50 Auction Market Analysis")

    col_tf, col_tog1, col_tog2, col_tog3, col_tog4 = st.columns([1.2, 1, 1, 1, 1])

    with col_tf:
        tf_choice = st.segmented_control(
            "Timeframe",
            options=["5m", "15m"],
            default="5m",
            label_visibility="collapsed",
            key="vp_tf_select",
        ) or "5m"

    with col_tog1:
        show_poc = st.toggle("POC Magnet", value=True, key="vp_show_poc")
    with col_tog2:
        show_va = st.toggle("70% Value Area", value=True, key="vp_show_va")
    with col_tog3:
        show_nodes = st.toggle("HVN & LVN Nodes", value=True, key="vp_show_nodes")
    with col_tog4:
        show_signals = st.toggle("Algo Signals & SL", value=True, key="vp_show_signals")

    chart_df: Optional[pd.DataFrame] = None
    if pa_frames and tf_choice in pa_frames and not pa_frames[tf_choice].empty:
        chart_df = pa_frames[tf_choice].copy()
    else:
        ref_spot = float(spot) if spot and not np.isnan(spot) else 24550.0
        chart_df = _generate_synthetic_nifty(timeframe=tf_choice, spot=ref_spot)

    engine = VolumeProfileEngine(bin_size=5.0, value_area_pct=0.70)
    res = engine.compute_profile(chart_df)

    curr_spot = float(spot) if spot and not np.isnan(spot) else float(chart_df["close"].iloc[-1])
    is_inside_va = res.val_price <= curr_spot <= res.vah_price
    dist_to_poc = curr_spot - res.poc_price
    is_at_poc = abs(curr_spot - res.poc_price) <= engine.poc_zone_pts

    # 1. Market State HUD
    col_hud1, col_hud2, col_hud3, col_hud4 = st.columns([1.5, 1, 1, 1])

    with col_hud1:
        if is_inside_va:
            _render_html(
                """
                <div class="amt-hud-card">
                    <span class="amt-badge balanced">BALANCED MARKET (CHOP / ROTATION)</span>
                    <div style="font-size: 13px; font-weight: 600; margin-top: 6px; color: #38BDF8;">
                        ⚖️ Expect Sideways Movement inside Value Area
                    </div>
                    <div style="font-size: 11px; color: #94A3B8; margin-top: 2px;">
                        Mean reversion rules apply. Avoid buying directional breakouts inside value.
                    </div>
                </div>
                """
            )
        elif curr_spot > res.vah_price:
            _render_html(
                """
                <div class="amt-hud-card">
                    <span class="amt-badge bullish">IMBALANCED MARKET (BULLISH EXPANSION)</span>
                    <div style="font-size: 13px; font-weight: 600; margin-top: 6px; color: #10B981;">
                        🚀 Fast Upward Move Expected Above VAH
                    </div>
                    <div style="font-size: 11px; color: #94A3B8; margin-top: 2px;">
                        Buyers rejected fair price upward. Momentum expansion into upper discovery.
                    </div>
                </div>
                """
            )
        else:
            _render_html(
                """
                <div class="amt-hud-card">
                    <span class="amt-badge imbalanced">IMBALANCED MARKET (BEARISH BREAKDOWN)</span>
                    <div style="font-size: 13px; font-weight: 600; margin-top: 6px; color: #EF4444;">
                        🩸 Fast Downward Move Expected Below VAL
                    </div>
                    <div style="font-size: 11px; color: #94A3B8; margin-top: 2px;">
                        Sellers rejected fair price downward. Rapid liquidation through thin volume.
                    </div>
                </div>
                """
            )

    with col_hud2:
        _render_html(
            f"""
            <div class="amt-hud-card">
                <div class="stat-label">POC Fair Price (Magnet)</div>
                <div class="stat-value" style="color: {GOLD};">{res.poc_price:.1f}</div>
                <div style="font-size: 11px; color: {'#10B981' if dist_to_poc > 0 else '#EF4444'};">
                    {dist_to_poc:+.1f} pts from Spot
                </div>
            </div>
            """
        )

    with col_hud3:
        _render_html(
            f"""
            <div class="amt-hud-card">
                <div class="stat-label">Value Area (70%) Range</div>
                <div class="stat-value" style="color: {CYAN};">{res.val_price:.0f} – {res.vah_price:.0f}</div>
                <div style="font-size: 11px; color: #94A3B8;">
                    VA Width: {res.vah_price - res.val_price:.0f} pts
                </div>
            </div>
            """
        )

    with col_hud4:
        _render_html(
            f"""
            <div class="amt-hud-card">
                <div class="stat-label">Profile Shape Bias</div>
                <div style="font-size: 14px; font-weight: 700; color: #F8FAFC; margin-top: 4px;">{res.shape.shape}</div>
                <div style="font-size: 11px; color: #38BDF8; font-weight: 600;">
                    {res.shape.status_tag}
                </div>
            </div>
            """
        )

    # 2. Anti-Trap Shield Banner
    if is_at_poc:
        st.error(
            f"🛡️ **ANTI-TRAP ACTIVE (THETA DECAY WARNING):** Nifty Spot ({curr_spot:.1f}) is currently trading "
            f"directly inside the POC Equilibrium Zone ({res.poc_price - engine.poc_zone_pts:.1f} – {res.poc_price + engine.poc_zone_pts:.1f}). "
            "Both buyers and sellers agree on value here. Directional option buying is strictly disabled — theta decay destroys option premiums in this zone!"
        )

    # 3. Dual-Pane Chart
    fig = VolumeProfileChartBuilder.build_figure(
        df=chart_df,
        res=res,
        show_poc=show_poc,
        show_va=show_va,
        show_nodes=show_nodes,
        show_signals=show_signals,
    )
    st.plotly_chart(fig, use_container_width=True)

    # 4. Actionable Signals & Execution Cockpit
    col_sig, col_ref = st.columns([1.6, 1])

    with col_sig:
        st.markdown("#### ⚡ Algorithmic Trade Execution Matrix")
        if res.signals:
            rows = []
            for s in res.signals:
                rows.append({
                    "Timestamp": str(s.timestamp)[-8:] if hasattr(s.timestamp, "strftime") else str(s.timestamp),
                    "Action": "🟢 BUY" if s.signal_type == "BUY" else ("🔴 SELL" if s.signal_type == "SELL" else f"⚠️ {s.signal_type}"),
                    "Rule": s.trigger_rule,
                    "Entry": f"{s.entry_price:.1f}",
                    "Structure SL": f"{s.stop_loss:.1f}",
                    "Target": f"{s.target_price:.1f}",
                    "R:R": f"1 : {s.reward_points / max(1.0, s.risk_points):.1f}" if s.risk_points > 0 else "—",
                    "Audit / Status": s.status_reason,
                })
            st.dataframe(pd.DataFrame(rows), use_container_width=True, height=220)
        else:
            st.info("No algorithmic triggers active in the current window.")

    with col_ref:
        st.markdown("#### 📖 Auction Market Theory Playbook")
        with st.expander("Auction Rules & Decision Framework", expanded=True):
            st.markdown(
                """
                * **POC (Fair Price Magnet):** Highest volume node. Price tends to rotate back to POC like a magnet.
                * **70% Value Area:** Institutional standard range where 70% of business transpired.
                  - **Above VAH:** Buyers in control. Ride momentum breakouts.
                  - **Below VAL:** Sellers in control. Ride breakdown flushes.
                  - **Inside VA:** Mean-reversion scalping between VAL and VAH only.
                * **HVN (High Volume Node):** Thick volume bulge. Acts as mini support/resistance; slows down price.
                * **LVN (Low Volume Node):** Thin volume gap. Acts as a *Breakout Highway*; price shoots through rapidly.
                * **09:40 AM Rule:** First 25 minutes are for institutional price discovery. No signals allowed before 9:40 AM.
                """
            )
