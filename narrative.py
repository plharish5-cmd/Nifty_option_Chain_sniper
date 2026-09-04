"""
narrative.py — Master 5-Minute Executive Decision Cockpit & Quantitative Confluence Engine.
Synthesizes all 100+ parameters across Options, Price Action, Microstructure, SMC, 
Macro, and Execution Liquidity into:
  1. A definitive Institutional Score (-100 to +100) with weighted multi-factor voting.
  2. A "Zero-Dilemma" Order Placement Ticket (Exact Strike, Entry Trigger, Stop Loss, Targets, Risk/Reward in ₹).
  3. A 14-Factor Institutional Traffic Light Grid.
  4. A 5-Minute Master Market Story in plain English.
  5. Rulebook & Risk Guardrails.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import numpy as np
import pandas as pd
import config


@dataclass
class ConfluenceFactor:
    name: str
    category: str        # "Options", "Price Action", "Order Flow", "SMC & Structure", "Macro"
    value_str: str
    weight: int          # Max weight contribution (e.g. 10, 8, 7)
    score: int           # Actual score contribution (-weight to +weight)
    sentiment: str       # "BULLISH" | "BEARISH" | "NEUTRAL"
    plain_meaning: str   # Plain English explanation for the trader


@dataclass
class OrderTicket:
    action: str              # "BUY CALL (CE)" | "BUY PUT (PE)" | "NO TRADE / WAIT" | "HARVEST THETA (SELL)"
    strike: int
    option_type: str         # "CE" | "PE" | "NONE"
    entry_trigger: str       # e.g. "Buy at 24,165 on 5m close above VWAP"
    stop_loss: float         # Index SL level
    stop_loss_pts: float     # Points risk
    target_1: float          # Index T1 level
    target_1_pts: float      # Points reward T1
    target_2: float          # Index T2 level
    target_2_pts: float      # Points reward T2
    risk_per_lot_inr: float  # SL pts * lot_size
    reward_per_lot_inr: float# T1 pts * lot_size
    risk_reward_ratio: str   # e.g. "1 : 1.8"


@dataclass
class MasterDecisionCockpit:
    # 1. Headline Verdict
    action: str
    action_color: str
    confidence: str
    institutional_score: int # -100 to +100
    regime_title: str
    headline_summary: str
    what_to_do_now: str

    # 2. Order Placement Ticket
    ticket: OrderTicket

    # 3. 14-Parameter Confluence Matrix
    factors: list[ConfluenceFactor]
    bull_score: int
    bear_score: int
    net_score: int
    consensus_summary: str

    # 4. Master Market Story
    story_paragraphs: list[str]

    # 5. Playbooks
    ce_playbook: str
    pe_playbook: str
    seller_playbook: str


def build_master_cockpit(
    read, sig, spot: float,
    vix=None, flows=None, gex: dict | None = None,
    cpr_meta: dict | None = None,
    pa_frames: dict | None = None,
    order_flow_res: dict | None = None,
    smc_res: dict | None = None,
    liq_res: dict | None = None,
    xcheck: tuple | None = None,
    trapped_writers: dict | None = None,
    icebergs: list | None = None,
    lot_size: int = 65,
) -> MasterDecisionCockpit:
    """
    Evaluates all parameters, calculates weighted institutional score,
    and formats the complete Decision Cockpit.
    """
    lv = read.levels
    s1, r1 = lv.s1, lv.r1
    s2, r2 = lv.s2, lv.r2
    atm = lv.atm
    dist_s1 = spot - s1
    dist_r1 = r1 - spot
    range_width = r1 - s1

    factors: list[ConfluenceFactor] = []

    # ──────────────────────────────────────────────────────────────────────────
    # ENGINE 1: Option Chain Mechanics (Weight: 28%)
    # ──────────────────────────────────────────────────────────────────────────

    # 1. Total PCR (OI) (Weight: 8)
    pcr = float(read.pcr_oi)
    if pcr >= 1.25:
        pcr_s, pcr_pts = "BULLISH", 8
        pcr_m = f"PCR at {pcr:.2f} — Heavy Put writing indicates strong institutional support."
    elif pcr <= 0.75:
        pcr_s, pcr_pts = "BEARISH", -8
        pcr_m = f"PCR at {pcr:.2f} — Heavy Call writing indicates institutional resistance."
    else:
        pcr_s, pcr_pts = "NEUTRAL", 0
        pcr_m = f"PCR at {pcr:.2f} — Neutral balance between Call and Put writers."
    factors.append(ConfluenceFactor("Total PCR (OI)", "Options", f"{pcr:.2f}", 8, pcr_pts, pcr_s, pcr_m))

    # 2. Intraday Change in PCR (Delta OI) (Weight: 6)
    pcr_day = getattr(read, "pcr_day", None)
    if pcr_day is not None and not np.isnan(pcr_day):
        if pcr_day >= 1.2:
            pcrd_s, pcrd_pts = "BULLISH", 6
            pcrd_m = f"Intraday PCR {pcr_day:.2f} — Fresh Put writing arriving aggressively today."
        elif pcr_day <= 0.8:
            pcrd_s, pcrd_pts = "BEARISH", -6
            pcrd_m = f"Intraday PCR {pcr_day:.2f} — Fresh Call writing arriving aggressively today."
        else:
            pcrd_s, pcrd_pts = "NEUTRAL", 0
            pcrd_m = f"Intraday PCR {pcr_day:.2f} — Balanced day change in open interest."
        val_str = f"{pcr_day:.2f}"
    else:
        pcrd_s, pcrd_pts, val_str = "NEUTRAL", 0, "—"
        pcrd_m = "Intraday PCR not available for this snapshot."
    factors.append(ConfluenceFactor("Intraday PCR (ΔOI)", "Options", val_str, 6, pcrd_pts, pcrd_s, pcrd_m))

    # 3. Option Chain Bias Score (Weight: 8)
    bias = float(read.bias_score)
    if bias >= 1.5:
        b_s, b_pts = "BULLISH", min(8, int(bias * 2))
        b_m = f"Chain Bias {bias:+.1f} — Option metrics strongly aligned to the upside."
    elif bias <= -1.5:
        b_s, b_pts = "BEARISH", max(-8, int(bias * 2))
        b_m = f"Chain Bias {bias:+.1f} — Option metrics strongly aligned to the downside."
    else:
        b_s, b_pts = "NEUTRAL", 0
        b_m = f"Chain Bias {bias:+.1f} — Conflicting option chain sub-components."
    factors.append(ConfluenceFactor("Option Chain Bias", "Options", f"{bias:+.1f}", 8, b_pts, b_s, b_m))

    # 4. Max Pain Gravitational Bias (Weight: 6)
    mp = lv.max_pain
    mp_diff = mp - spot
    if mp_diff >= 35:
        mp_s, mp_pts = "BULLISH", 6
        mp_m = f"Max Pain {mp:,.0f} ({mp_diff:+.0f} pts above) acts as upward gravitational magnet."
    elif mp_diff <= -35:
        mp_s, mp_pts = "BEARISH", -6
        mp_m = f"Max Pain {mp:,.0f} ({mp_diff:+.0f} pts below) acts as downward gravitational magnet."
    else:
        mp_s, mp_pts = "NEUTRAL", 0
        mp_m = f"Spot is near Max Pain ({mp:,.0f}) — Expiry gravitational equilibrium."
    factors.append(ConfluenceFactor("Max Pain Magnet", "Options", f"{mp:,.0f} ({mp_diff:+.0f}p)", 6, mp_pts, mp_s, mp_m))

    # ──────────────────────────────────────────────────────────────────────────
    # ENGINE 2: Macro, Volatility & Institutional Flows (Weight: 18%)
    # ──────────────────────────────────────────────────────────────────────────

    # 5. India VIX & Volatility Environment (Weight: 8)
    if vix:
        v_val = float(vix.value)
        v_chg = float(vix.pct_change)
        if v_chg <= -2.5 and v_val <= 14:
            v_s, v_pts = "BULLISH", 6
            v_m = f"VIX {v_val:.2f} ({v_chg:+.1f}%) — Falling volatility favors steady bullish trends."
        elif v_chg >= 3.0:
            v_s, v_pts = "BEARISH", -8
            v_m = f"VIX {v_val:.2f} ({v_chg:+.1f}%) — Spiking volatility signals fear / downside hedging."
        elif v_val <= 11.5:
            v_s, v_pts = "NEUTRAL", 0
            v_m = f"VIX {v_val:.2f} — Ultra-low volatility: options decay fast, rangebound grind."
        else:
            v_s, v_pts = "NEUTRAL", 0
            v_m = f"VIX {v_val:.2f} ({v_chg:+.1f}%) — Normal volatility environment."
        v_str = f"{v_val:.2f} ({v_chg:+.1f}%)"
    else:
        v_s, v_pts, v_str = "NEUTRAL", 0, "—"
        v_m = "India VIX feed offline."
    factors.append(ConfluenceFactor("India VIX Trend", "Macro", v_str, 8, v_pts, v_s, v_m))

    # 6. Institutional Flows (FII/DII) (Weight: 6)
    if flows:
        f_note = str(flows.note)
        if "bullish" in f_note.lower() or "bought" in f_note.lower():
            fl_s, fl_pts = "BULLISH", 6
            fl_m = f"Institutional flows: {f_note}"
        elif "bearish" in f_note.lower() or "sold" in f_note.lower():
            fl_s, fl_pts = "BEARISH", -6
            fl_m = f"Institutional flows: {f_note}"
        else:
            fl_s, fl_pts = "NEUTRAL", 0
            fl_m = f"Institutional flows: {f_note}"
        fl_str = flows.note[:25] + "..." if len(flows.note) > 25 else flows.note
    else:
        fl_s, fl_pts, fl_str = "NEUTRAL", 0, "—"
        fl_m = "Daily institutional flow summary unavailable."
    factors.append(ConfluenceFactor("FII / DII Flows", "Macro", fl_str, 6, fl_pts, fl_s, fl_m))

    # 7. Gamma Exposure (GEX) Regime (Weight: 6)
    if gex and gex.get("known"):
        g_reg = str(gex.get("regime", ""))
        g_tot = float(gex.get("total", 0.0))
        g_flip = gex.get("flip")
        if "POSITIVE" in g_reg:
            g_s, g_pts = "NEUTRAL", 2
            g_m = "Positive Gamma — Dealers dampening moves; pullbacks to mean favored."
        else:
            g_s, g_pts = "BEARISH" if spot < (g_flip or spot) else "BULLISH", -6 if spot < (g_flip or spot) else 6
            g_m = "Negative Gamma — Acceleration risk! Fast moves upon breakout."
        g_str = f"{g_reg.split(' —')[0]} ({g_tot/1e9:+.1f}B)"
    else:
        g_s, g_pts, g_str = "NEUTRAL", 0, "—"
        g_m = "Net Gamma Exposure not active."
    factors.append(ConfluenceFactor("Net Gamma (GEX)", "Options", g_str, 6, g_pts, g_s, g_m))

    # ──────────────────────────────────────────────────────────────────────────
    # ENGINE 3: Price Action & Trend (Weight: 20%)
    # ──────────────────────────────────────────────────────────────────────────

    # 8. 5-Minute Price vs VWAP (Weight: 10)
    pa_sent = "NEUTRAL"
    pa_pts = 0
    pa_read = "At VWAP"
    pa_m = "Price trading within ±5 pts of session VWAP benchmark."
    if pa_frames and "5m" in pa_frames:
        f5 = pa_frames["5m"]
        if not f5.empty and "vwap" in f5 and pd.notna(f5["vwap"].iloc[-1]):
            last_c = float(f5["close"].iloc[-1])
            vw = float(f5["vwap"].iloc[-1])
            v_diff = last_c - vw
            if v_diff >= 12:
                pa_sent, pa_pts = "BULLISH", 10
                pa_read = f"+{v_diff:.0f} pts above VWAP"
                pa_m = f"Price is {v_diff:+.0f} pts above VWAP ({vw:,.0f}) — Buyers in intraday control."
            elif v_diff <= -12:
                pa_sent, pa_pts = "BEARISH", -10
                pa_read = f"{v_diff:.0f} pts below VWAP"
                pa_m = f"Price is {v_diff:.0f} pts below VWAP ({vw:,.0f}) — Sellers pressing below benchmark."
            else:
                pa_read = f"At VWAP ({v_diff:+.0f}p)"
    factors.append(ConfluenceFactor("5m vs VWAP", "Price Action", pa_read, 10, pa_pts, pa_sent, pa_m))

    # 9. Chain vs Price Alignment (Weight: 6)
    if xcheck and xcheck[0]:
        xc_verdict, xc_why = xcheck
        if "ALIGNED BULLISH" in xc_verdict:
            xc_s, xc_pts = "BULLISH", 6
            xc_m = f"Full Alignment: Price action and Option Chain agree bullishly. {xc_why}"
        elif "ALIGNED BEARISH" in xc_verdict:
            xc_s, xc_pts = "BEARISH", -6
            xc_m = f"Full Alignment: Price action and Option Chain agree bearishly. {xc_why}"
        elif "CONFLICT" in xc_verdict:
            xc_s, xc_pts = "NEUTRAL", 0
            xc_m = f"Divergence Warning: Price and Option chain disagree! {xc_why}"
        else:
            xc_s, xc_pts = "NEUTRAL", 0
            xc_m = xc_why
        xc_str = xc_verdict
    else:
        xc_s, xc_pts, xc_str = "NEUTRAL", 0, "—"
        xc_m = "Cross-check between chain and candles active."
    factors.append(ConfluenceFactor("Chain vs Price", "Price Action", xc_str, 6, xc_pts, xc_s, xc_m))

    # ──────────────────────────────────────────────────────────────────────────
    # ENGINE 4: Smart Money Concepts & Structure (Weight: 18%)
    # ──────────────────────────────────────────────────────────────────────────

    # 10. SMC Market Structure Trend (Weight: 10)
    smc_s, smc_pts = "NEUTRAL", 0
    smc_str = "Transition"
    smc_m = "Market structure in range/equilibrium."
    if smc_res and smc_res.get("structure"):
        stx = smc_res["structure"]
        last_ev = stx.last
        ev_label = f" ({last_ev.label})" if last_ev else ""
        if stx.trend == "BULLISH":
            smc_s, smc_pts = "BULLISH", 10
            smc_str = "Bullish (HH/HL)"
            smc_m = f"Bullish structure sequence — making Higher Highs and Higher Lows.{ev_label}"
        elif stx.trend == "BEARISH":
            smc_s, smc_pts = "BEARISH", -10
            smc_str = "Bearish (LH/LL)"
            smc_m = f"Bearish structure sequence — making Lower Highs and Lower Lows.{ev_label}"
    factors.append(ConfluenceFactor("SMC Structure (15m)", "SMC & Structure", smc_str, 10, smc_pts, smc_s, smc_m))

    # 11. CPR Range & Position (Weight: 6)
    cpr_s, cpr_pts = "NEUTRAL", 0
    cpr_str = "Inside CPR"
    cpr_m = "Price oscillating inside CPR."
    if cpr_meta:
        c_top = cpr_meta.get("top", spot)
        c_bot = cpr_meta.get("bottom", spot)
        c_wid = cpr_meta.get("width", 50)
        c_vrd = cpr_meta.get("verdict", "NORMAL")
        if spot > c_top:
            cpr_s, cpr_pts = "BULLISH", 6
            cpr_str = f"Above CPR ({c_vrd})"
            cpr_m = f"Price above CPR Top ({c_top:,.0f}). Trend day potential: {cpr_meta.get('hint')}."
        elif spot < c_bot:
            cpr_s, cpr_pts = "BEARISH", -6
            cpr_str = f"Below CPR ({c_vrd})"
            cpr_m = f"Price below CPR Bottom ({c_bot:,.0f}). Trend day potential: {cpr_meta.get('hint')}."
        else:
            cpr_str = f"Inside CPR ({c_wid:.0f}p)"
            cpr_m = f"Price stuck inside Central Pivot Range ({c_bot:,.0f}–{c_top:,.0f}) — Choppy zone."
    factors.append(ConfluenceFactor("CPR Pivot Bias", "SMC & Structure", cpr_str, 6, cpr_pts, cpr_s, cpr_m))

    # ──────────────────────────────────────────────────────────────────────────
    # ENGINE 5: Microstructure & Execution Safety (Weight: 16%)
    # ──────────────────────────────────────────────────────────────────────────

    # 12. Level 2 (5-Depth) Order Book Imbalance (Weight: 8)
    dp_s, dp_pts = "NEUTRAL", 0
    dp_str = "Balanced Depth"
    dp_m = "Resting limit buy and sell queues are evenly matched."
    if order_flow_res and order_flow_res.get("depth"):
        dp = order_flow_res["depth"]
        if dp.imbalance_5 >= 0.20:
            dp_s, dp_pts = "BULLISH", 8
            dp_str = f"+{dp.imbalance_5:.1%} Bids"
            dp_m = f"Institutional buyer defense: +{dp.imbalance_5:.1%} bid dominance ({dp.bid_qty_5:,} bids vs {dp.ask_qty_5:,} asks)."
        elif dp.imbalance_5 <= -0.20:
            dp_s, dp_pts = "BEARISH", -8
            dp_str = f"{dp.imbalance_5:.1%} Asks"
            dp_m = f"Institutional seller pressure: {dp.imbalance_5:.1%} ask dominance ({dp.ask_qty_5:,} asks vs {dp.bid_qty_5:,} bids)."
        else:
            dp_str = f"{dp.imbalance_5:+.1%} Depth"
    factors.append(ConfluenceFactor("Level 2 Order Book", "Order Flow", dp_str, 8, dp_pts, dp_s, dp_m))

    # 13. Tape Speed (Volume Velocity) (Weight: 4)
    sp_s, sp_pts = "NEUTRAL", 0
    sp_str = "Steady"
    sp_m = "Tape running at standard 15-minute pace."
    if order_flow_res and order_flow_res.get("speed"):
        sp = order_flow_res["speed"]
        sp_str = f"{sp.state} ({sp.ratio:.2f}x)"
        if sp.state == "ACCELERATED":
            sp_s = pa_sent if pa_sent != "NEUTRAL" else "BULLISH"
            sp_pts = 4 if sp_s == "BULLISH" else -4
            sp_m = f"Tape accelerated ({sp.ratio:.1f}x) — Aggressive market orders driving the move."
        elif sp.state == "STALLED":
            sp_s, sp_pts = "NEUTRAL", 0
            sp_m = f"Tape stalled ({sp.ratio:.2f}x) — Low volume drift, lack of institutional follow-through."
    factors.append(ConfluenceFactor("Tape Speed Velocity", "Order Flow", sp_str, 4, sp_pts, sp_s, sp_m))

    # 14. Contract Liquidity Thumb Rule (Weight: 4)
    liq_s, liq_pts = "NEUTRAL", 0
    liq_str = "Liquid"
    liq_m = "Options spreads are tight; minimal impact cost."
    if liq_res:
        is_liq = liq_res.get("is_liquid", True)
        impact = liq_res.get("max_impact_inr", 0.0)
        if is_liq:
            liq_s, liq_pts = "BULLISH", 4
            liq_str = f"Liquid (₹{impact:.0f}/lot)"
            liq_m = f"Tight spreads across ATM strikes (₹{impact:.0f} impact < ₹800 limit) — Safe to execute."
        else:
            liq_s, liq_pts = "BEARISH", -4
            liq_str = f"Illiquid ⚠️ (₹{impact:.0f})"
            liq_m = f"High slippage warning! Spread impact ₹{impact:.0f} > ₹800 threshold. Avoid market orders."
    factors.append(ConfluenceFactor("Option Liquidity", "Order Flow", liq_str, 4, liq_pts, liq_s, liq_m))

    # ──────────────────────────────────────────────────────────────────────────
    # TOTAL QUANTITATIVE SCORE & VERDICT
    # ──────────────────────────────────────────────────────────────────────────
    bull_score = sum(f.score for f in factors if f.score > 0)
    bear_score = abs(sum(f.score for f in factors if f.score < 0))
    net_score = sum(f.score for f in factors)  # -100 to +100
    net_score = max(-100, min(100, net_score))

    # Determine Action & Guidance
    if sig.blocks:
        action = "NO TRADE"
        action_color = "#8A8F98"
        confidence = "HIGH"
        headline = f"Execution Blocked — {sig.blocks[0]}"
        what_now = f"**STAND ASIDE.** {sig.blocks[0]}. Do not initiate fresh positions until market restrictions lift."
    elif range_width <= 55 and read.regime == "CONSOLIDATION":
        action = "WAIT FOR BREAKOUT"
        action_color = "#E65100"
        confidence = "HIGH"
        headline = f"Range Compression ({s1:,.0f}–{r1:,.0f}) — Squeeze Building"
        what_now = (
            f"**DO NOT BUY OPTIONS INSIDE THIS NARROW 50-PT CHANNEL.** Theta will destroy premium. "
            f"Wait for a decisive breakout above {r1:,.0f} or breakdown below {s1:,.0f} before entering."
        )
    elif net_score >= 40 and dist_s1 <= 30:
        action = "BUY CALL (CE)"
        action_color = "#0F9D58"
        confidence = "HIGH" if net_score >= 55 else "MODERATE"
        headline = f"Strong Institutional Confluence (+{net_score}/100) — Support Defense at {s1:,.0f}"
        what_now = (
            f"**HIGH-PROBABILITY CALL (CE) SETUP.** Institutional score is +{net_score}/100. "
            f"Buy on confirmation above support {s1:,.0f} aiming for target {r1:,.0f}."
        )
    elif net_score <= -40 and dist_r1 <= 30:
        action = "BUY PUT (PE)"
        action_color = "#E8503A"
        confidence = "HIGH" if net_score <= -55 else "MODERATE"
        headline = f"Strong Institutional Confluence ({net_score}/100) — Resistance Rejection at {r1:,.0f}"
        what_now = (
            f"**HIGH-PROBABILITY PUT (PE) SETUP.** Institutional score is {net_score}/100. "
            f"Buy on rejection from resistance {r1:,.0f} aiming for target {s1:,.0f}."
        )
    elif net_score > 15:
        action = "LEAN CALL ON DIPS"
        action_color = "#0F9D58"
        confidence = "MODERATE"
        headline = f"Mild Bullish Lean (+{net_score}/100) — Wait for Pullback to S1"
        what_now = (
            f"**DO NOT CHASE.** Market has a mild bullish tilt (+{net_score}/100), but risk-reward at current spot "
            f"({spot:,.1f}) is mediocre. Look to enter CE only on a clean pullback near {s1:,.0f}."
        )
    elif net_score < -15:
        action = "LEAN PUT ON RALLIES"
        action_color = "#E8503A"
        confidence = "MODERATE"
        headline = f"Mild Bearish Lean ({net_score}/100) — Wait for Pullback to R1"
        what_now = (
            f"**DO NOT CHASE.** Market has a mild bearish tilt ({net_score}/100). "
            f"Look to enter PE only on a test of overhead resistance near {r1:,.0f}."
        )
    else:
        action = "NO TRADE / CHOP"
        action_color = "#8A8F98"
        confidence = "HIGH"
        headline = f"Neutral Confluence ({net_score:+} pts) — No Directional Edge"
        what_now = (
            f"**CAPITAL PRESERVATION MODE.** Bullish and bearish indicators are deadlocked ({bull_score} vs {bear_score}). "
            f"There is no edge for retail buyers. Stand aside and let the market show its hand."
        )

    # ──────────────────────────────────────────────────────────────────────────
    # ORDER PLACEMENT TICKET (Zero Dilemma)
    # ──────────────────────────────────────────────────────────────────────────
    if "CALL" in action:
        t_action = "BUY CALL (CE)"
        t_strike = atm if dist_s1 > 15 else s1
        t_type = "CE"
        t_entry = f"Buy on 5m candle close above {spot + 10:,.0f} with tape velocity > 1.2x"
        t_sl = float(s1 - 15)
        t_sl_pts = round(spot - t_sl)
        t_t1 = float(r1)
        t_t1_pts = round(t_t1 - spot)
        t_t2 = float(r2)
        t_t2_pts = round(t_t2 - spot)
    elif "PUT" in action:
        t_action = "BUY PUT (PE)"
        t_strike = atm if dist_r1 > 15 else r1
        t_type = "PE"
        t_entry = f"Buy on breakdown below {spot - 10:,.0f} with tape velocity > 1.2x"
        t_sl = float(r1 + 15)
        t_sl_pts = round(t_sl - spot)
        t_t1 = float(s1)
        t_t1_pts = round(spot - t_t1)
        t_t2 = float(s2)
        t_t2_pts = round(spot - t_t2)
    else:
        t_action = "STAND ASIDE (NO TRADE)"
        t_strike = atm
        t_type = "NONE"
        t_entry = f"Wait for clean breakout above {r1:,.0f} or breakdown below {s1:,.0f}"
        t_sl = float(s1)
        t_sl_pts = 25.0
        t_t1 = float(r1)
        t_t1_pts = 50.0
        t_t2 = float(r2)
        t_t2_pts = 100.0

    t_sl_pts = max(15.0, t_sl_pts)
    t_t1_pts = max(20.0, t_t1_pts)
    risk_inr = t_sl_pts * lot_size
    reward_inr = t_t1_pts * lot_size
    rrr = f"1 : {reward_inr / risk_inr:.1f}" if risk_inr > 0 else "1 : 1.5"

    ticket = OrderTicket(
        action=t_action,
        strike=int(t_strike),
        option_type=t_type,
        entry_trigger=t_entry,
        stop_loss=t_sl,
        stop_loss_pts=t_sl_pts,
        target_1=t_t1,
        target_1_pts=t_t1_pts,
        target_2=t_t2,
        target_2_pts=t_t2_pts,
        risk_per_lot_inr=risk_inr,
        reward_per_lot_inr=reward_inr,
        risk_reward_ratio=rrr,
    )

    # Concrete Playbooks
    ce_play = (
        f"Buy **{atm} CE** ONLY if a 5-minute candle closes firmly above **{spot + 10:,.0f}** "
        f"with tape speed accelerating (> 1.3x). Target: **{r1:,.0f}** (+{r1 - spot:.0f} pts). "
        f"Invalidation Stop: **{s1 - 10:,.0f}** (Risk: ₹{t_sl_pts * lot_size:,.0f}/lot)."
    )
    pe_play = (
        f"Buy **{atm} PE** ONLY if Major Support **{s1:,.0f}** breaks and holds on 5m "
        f"with Call writer surge. Target: **{s2:,.0f}** (+{spot - s2:.0f} pts). "
        f"Invalidation Stop: **{r1 + 10:,.0f}** (Risk: ₹{t_sl_pts * lot_size:,.0f}/lot)."
    )
    seller_play = (
        f"Sell **{r1 + 50:,.0f} CE** and **{s1 - 50:,.0f} PE** Strangle to harvest theta decay "
        f"as long as spot remains within the {s1:,.0f}–{r1:,.0f} channel."
    )

    # Plain English Market Story
    p1 = (
        f"**1. Price & Option Walls**: NIFTY is trading at **{spot:,.1f}**, sandwiched between "
        f"Major Support S1 at **{s1:,.0f}** ({dist_s1:+.0f} pts away) and Major Resistance R1 at **{r1:,.0f}** "
        f"({dist_r1:+.0f} pts away). Total range is **{range_width:.0f} points**. PCR sits at **{pcr:.2f}** "
        f"with institutional bias scored at **{bias:+.1f}**."
    )
    p2 = (
        f"**2. Institutional Flow & Microstructure**: Level 2 Order Book shows **{dp_str}** with "
        f"tape speed clocking at **{sp_str}**. Net GEX is **{g_str}**, indicating "
        f"{'dealers dampening volatility' if 'POSITIVE' in g_str else 'rapid price acceleration if walls break'}."
    )
    p3 = (
        f"**3. SMC & Structural Trend**: 15-minute SMC structure is in **{smc_str}** mode. "
        f"Central Pivot Range is **{cpr_str}**. Net multi-factor institutional score is **{net_score:+} / 100** "
        f"({bull_score} bullish points vs {bear_score} bearish points)."
    )

    return MasterDecisionCockpit(
        action=action,
        action_color=action_color,
        confidence=confidence,
        institutional_score=net_score,
        regime_title=read.regime.replace("_", " "),
        headline_summary=headline,
        what_to_do_now=what_now,
        ticket=ticket,
        factors=factors,
        bull_score=bull_score,
        bear_score=bear_score,
        net_score=net_score,
        consensus_summary=f"+{bull_score} Bullish · -{bear_score} Bearish · Net {net_score:+}",
        story_paragraphs=[p1, p2, p3],
        ce_playbook=ce_play,
        pe_playbook=pe_play,
        seller_playbook=seller_play,
    )


def render_cockpit(cockpit: MasterDecisionCockpit, spot: float, lv, sig, dte: float, nse_ts: str):
    """
    Renders the rich, zero-dilemma Decision Cockpit in Streamlit.
    """
    import streamlit as st

    # ═══════════════════════════════════════════════════════════════════════════
    # 1. Executive Verdict & Institutional Gauge
    # ═══════════════════════════════════════════════════════════════════════════
    score_pct = int((cockpit.institutional_score + 100) / 2)  # 0 to 100%
    gauge_col = cockpit.action_color

    st.markdown(
        f"""
        <div style="background: #191D26; border: 2px solid {gauge_col}; border-radius: 12px; padding: 20px 24px; margin-bottom: 20px;">
            <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 15px;">
                <div>
                    <span style="color: #8A8F98; font-size: 13px; font-weight: 700; text-transform: uppercase; letter-spacing: 1px;">
                        🎯 5-MINUTE INSTITUTIONAL DECISION VERDICT
                    </span>
                    <div style="color: {gauge_col}; font-size: 34px; font-weight: 900; line-height: 1.15; margin: 4px 0;">
                        {cockpit.action}
                    </div>
                    <div style="color: #D1D4DC; font-size: 14.5px; font-weight: 500;">
                        State: <b>{cockpit.regime_title}</b> · Conviction: <b style="color:{gauge_col};">{cockpit.confidence}</b> · Channel: <b>{lv.s1:,.0f} – {lv.r1:,.0f} ({lv.r1-lv.s1:.0f} pts)</b>
                    </div>
                </div>
                <div style="background: rgba(255,255,255,0.04); padding: 12px 20px; border-radius: 10px; border: 1px solid rgba(255,255,255,0.08); text-align: right; min-width: 250px;">
                    <div style="color: #8A8F98; font-size: 12px; font-weight: 600;">INSTITUTIONAL CONFLUENCE SCORE</div>
                    <div style="color: {gauge_col}; font-size: 26px; font-weight: 900; margin: 2px 0;">
                        {cockpit.institutional_score:+} <span style="font-size: 14px; color: #8A8F98;">/ 100</span>
                    </div>
                    <div style="color: #A0A5B0; font-size: 12.5px;">
                        {cockpit.consensus_summary}
                    </div>
                </div>
            </div>
        </div>
        """,
        unsafe_allow_html=True
    )

    # ═══════════════════════════════════════════════════════════════════════════
    # 2. What To Do Right Now
    # ═══════════════════════════════════════════════════════════════════════════
    if "BUY CALL" in cockpit.action or "BUY PUT" in cockpit.action:
        st.success(f"### 📋 Action Directive: {cockpit.what_to_do_now}")
    elif "WAIT" in cockpit.action or "COMPRESSION" in cockpit.headline_summary:
        st.warning(f"### 📋 Action Directive: {cockpit.what_to_do_now}")
    else:
        st.info(f"### 📋 Action Directive: {cockpit.what_to_do_now}")

    # ═══════════════════════════════════════════════════════════════════════════
    # 3. Trader's Order Placement Ticket (Zero Dilemma)
    # ═══════════════════════════════════════════════════════════════════════════
    t = cockpit.ticket
    st.markdown("### 🎫 Trader's Order Placement Ticket")
    st.caption("Everything you need before clicking 'Buy' or 'Sell' — No mental math needed.")

    t_col1, t_col2, t_col3, t_col4 = st.columns(4)
    t_col1.metric("Recommended Contract", f"{t.strike} {t.option_type}" if t.option_type != "NONE" else "NO TRADE")
    t_col2.metric("Stop Loss (Index)", f"{t.stop_loss:,.0f}", f"-{t.stop_loss_pts:.0f} pts risk", delta_color="inverse")
    t_col3.metric("Target 1 (Wall)", f"{t.target_1:,.0f}", f"+{t.target_1_pts:.0f} pts reward", delta_color="normal")
    t_col4.metric("Risk : Reward (RRR)", t.risk_reward_ratio, f"Risk ₹{t.risk_per_lot_inr:,.0f} / lot", delta_color="off")

    st.markdown(
        f"""
        <div style="background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.1); border-radius: 8px; padding: 12px 18px; margin-bottom: 20px;">
            <b>🎯 Execution Trigger:</b> <span style="color:#FFF;">{t.entry_trigger}</span><br>
            <b>💰 Capital & P&L Math:</b> Risk per lot: <b>₹{t.risk_per_lot_inr:,.0f}</b> · Target 1 Reward per lot: <b>₹{t.reward_per_lot_inr:,.0f}</b> · Target 2: <b>{t.target_2:,.0f} (+{t.target_2_pts:.0f} pts)</b>
        </div>
        """,
        unsafe_allow_html=True
    )

    # ═══════════════════════════════════════════════════════════════════════════
    # 4. Multi-Indicator Confluence Matrix (Table of all 14 parameters)
    # ═══════════════════════════════════════════════════════════════════════════
    st.markdown("### 📊 14-Parameter Institutional Confluence Matrix")
    st.caption("Complete breakdown of every mathematical engine evaluated.")

    cat_cols = st.columns(3)
    categories = ["Options", "Price Action", "Order Flow", "SMC & Structure", "Macro"]

    table_rows = []
    for f in cockpit.factors:
        sent_badge = {
            "BULLISH": "🟢 Bullish",
            "BEARISH": "🔴 Bearish",
            "NEUTRAL": "🟡 Neutral"
        }.get(f.sentiment, "⚪")
        table_rows.append({
            "Engine": f.category,
            "Indicator": f.name,
            "Reading": f.value_str,
            "Sentiment": sent_badge,
            "Impact Score": f"{f.score:+} / {f.weight}",
            "Meaning for Your Trade": f.plain_meaning
        })

    df_mat = pd.DataFrame(table_rows)
    st.dataframe(
        df_mat,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Engine": st.column_config.TextColumn("Category", width="medium"),
            "Indicator": st.column_config.TextColumn("Parameter", width="medium"),
            "Reading": st.column_config.TextColumn("Value", width="small"),
            "Sentiment": st.column_config.TextColumn("Signal", width="small"),
            "Impact Score": st.column_config.TextColumn("Weight", width="small"),
            "Meaning for Your Trade": st.column_config.TextColumn("Plain English Translation", width="large"),
        }
    )

    st.markdown("<div style='margin-top: 15px;'></div>", unsafe_allow_html=True)

    # ═══════════════════════════════════════════════════════════════════════════
    # 5. Playbook & The 5-Minute Story
    # ═══════════════════════════════════════════════════════════════════════════
    c_p1, c_p2 = st.columns([1.2, 1])
    with c_p1:
        st.markdown("### ⚡ 5-Minute Action Playbooks")
        st.markdown(
            f"""
            <div style="background: rgba(15, 157, 88, 0.08); border-left: 4px solid #0F9D58; padding: 12px; border-radius: 4px; margin-bottom: 10px;">
                <b style="color: #0F9D58;">📈 Call Buyer Playbook (CE)</b><br>
                <span style="font-size: 13px; color: #E0E0E0;">{cockpit.ce_playbook}</span>
            </div>
            <div style="background: rgba(232, 80, 58, 0.08); border-left: 4px solid #E8503A; padding: 12px; border-radius: 4px; margin-bottom: 10px;">
                <b style="color: #E8503A;">📉 Put Buyer Playbook (PE)</b><br>
                <span style="font-size: 13px; color: #E0E0E0;">{cockpit.pe_playbook}</span>
            </div>
            <div style="background: rgba(230, 81, 0, 0.08); border-left: 4px solid #E65100; padding: 12px; border-radius: 4px;">
                <b style="color: #E65100;">🛡️ Option Seller Playbook (Theta)</b><br>
                <span style="font-size: 13px; color: #E0E0E0;">{cockpit.seller_playbook}</span>
            </div>
            """,
            unsafe_allow_html=True
        )
    with c_p2:
        st.markdown("### 📖 The 5-Minute Story")
        with st.container():
            for p in cockpit.story_paragraphs:
                st.markdown(p)

    st.markdown("<div style='margin-top: 20px;'></div>", unsafe_allow_html=True)

    # ═══════════════════════════════════════════════════════════════════════════
    # 6. Best Instruments to Trade (Copy-Paste Watchlist) & Greeks Hierarchy
    # ═══════════════════════════════════════════════════════════════════════════
    render_best_instruments(spot=spot, expiry_str=str(dte), lot_size=getattr(config, "LOT_SIZE", 65))


def render_best_instruments(spot: float, expiry_str: str = "", lot_size: int = 65):
    """
    Renders the Top 3 CE and Top 3 PE instruments with live Greeks,
    gain per 50-pt move, daily theta loss, and copy-paste watchlist symbols.
    """
    import streamlit as st
    from pathlib import Path

    st.markdown("### 🎯 Best Instruments to Trade (Copy-Paste Watchlist)")
    st.caption("Categorized by trader risk profile with exact Delta, Theta, Gamma, and profit math.")

    # Try loading Dhan full chain if available for official broker greeks
    dhan_chain_p = config.DATA_DIR / "dhan_option_chains" / "latest_chain.csv"
    chain_df = None
    if dhan_chain_p.exists():
        try:
            chain_df = pd.read_csv(dhan_chain_p)
        except Exception:
            chain_df = None

    atm = int(round(spot / 50.0) * 50)
    ce_strikes = [
        (atm - 50, "🛡️ ITM 1 (Safe / High Delta)", "Moves fast with spot, lowest time decay risk."),
        (atm,      "⚖️ ATM (Balanced Sweet Spot)", "Best balance of capital cost, delta, and liquidity."),
        (atm + 50, "⚡ OTM 1 (High Gamma / Momentum)", "High leverage on fast breakouts. DO NOT hold during chop!")
    ]
    pe_strikes = [
        (atm + 50, "🛡️ ITM 1 (Safe / High Delta)", "Moves fast with spot, lowest time decay risk."),
        (atm,      "⚖️ ATM (Balanced Sweet Spot)", "Best balance of capital cost, delta, and liquidity."),
        (atm - 50, "⚡ OTM 1 (High Gamma / Momentum)", "High leverage on fast breakdowns. DO NOT hold during chop!")
    ]

    def build_table(strikes_info, side="ce"):
        rows = []
        for k, role, rationale in strikes_info:
            ltp, delta, theta, gamma, vega = 120.0, 0.50, -10.0, 0.0015, 10.0
            if chain_df is not None and not chain_df.empty:
                m = chain_df[chain_df["strike"] == k]
                if not m.empty:
                    r = m.iloc[0]
                    ltp = float(r.get(f"{side}_ltp", 120.0) or 120.0)
                    delta = float(r.get(f"{side}_delta", 0.50) or 0.50)
                    theta = float(r.get(f"{side}_theta", -10.0) or -10.0)
                    gamma = float(r.get(f"{side}_gamma", 0.0015) or 0.0015)
                    vega = float(r.get(f"{side}_vega", 10.0) or 10.0)
            else:
                # Approximate typical NIFTY greeks if chain_df is offline
                diff = (spot - k) if side == "ce" else (k - spot)
                delta = round(0.50 + (diff / 100.0) * 0.15, 2)
                delta = min(0.85, max(0.20, delta)) if side == "ce" else -min(0.85, max(0.20, delta))
                ltp = max(5.0, round(100.0 + diff * 0.6, 1))

            cost_lot = ltp * lot_size
            gain_50 = abs(delta) * 50 * lot_size
            decay_day = abs(theta) * lot_size
            watch_sym = f"NIFTY {k} {side.upper()}"

            rows.append({
                "Trader Persona / Role": role,
                "Watchlist Symbol (Search in Kite/Dhan)": watch_sym,
                "LTP": f"₹{ltp:,.2f}",
                "Cost / Lot": f"₹{cost_lot:,.0f}",
                "Delta (Δ)": f"{delta:.2f}",
                "Daily Theta Loss": f"-₹{decay_day:,.0f} / day",
                "Gamma (Γ)": f"{gamma:.4f}",
                "+50p NIFTY Move Gain": f"+₹{gain_50:,.0f}",
                "Rationale": rationale
            })
        return pd.DataFrame(rows)

    tab_ce, tab_pe, tab_greeks = st.tabs([
        "🟢 Top 3 Call Options (CE) to Trade",
        "🔴 Top 3 Put Options (PE) to Trade",
        "📚 The Greeks Order of Importance & Rules"
    ])

    with tab_ce:
        st.markdown("**Top Recommended Call Contracts (Choose One Based on Your Style)**")
        df_ce = build_table(ce_strikes, "ce")
        st.dataframe(
            df_ce,
            use_container_width=True,
            hide_index=True,
            column_config={
                "Trader Persona / Role": st.column_config.TextColumn("Profile", width="medium"),
                "Watchlist Symbol (Search in Kite/Dhan)": st.column_config.TextColumn("Watchlist Symbol", width="medium"),
                "LTP": st.column_config.TextColumn("Premium", width="small"),
                "Cost / Lot": st.column_config.TextColumn("Capital / Lot", width="small"),
                "Delta (Δ)": st.column_config.TextColumn("Delta (Speed)", width="small"),
                "Daily Theta Loss": st.column_config.TextColumn("Holding Cost", width="small"),
                "+50p NIFTY Move Gain": st.column_config.TextColumn("+50p Move Profit", width="small"),
                "Rationale": st.column_config.TextColumn("Why This Strike?", width="large"),
            }
        )
        st.info("💡 **Quick Copy for Broker Search**: `NIFTY " + f"{atm-50} CE` · `NIFTY {atm} CE` · `NIFTY {atm+50} CE`")

    with tab_pe:
        st.markdown("**Top Recommended Put Contracts (Choose One Based on Your Style)**")
        df_pe = build_table(pe_strikes, "pe")
        st.dataframe(
            df_pe,
            use_container_width=True,
            hide_index=True,
            column_config={
                "Trader Persona / Role": st.column_config.TextColumn("Profile", width="medium"),
                "Watchlist Symbol (Search in Kite/Dhan)": st.column_config.TextColumn("Watchlist Symbol", width="medium"),
                "LTP": st.column_config.TextColumn("Premium", width="small"),
                "Cost / Lot": st.column_config.TextColumn("Capital / Lot", width="small"),
                "Delta (Δ)": st.column_config.TextColumn("Delta (Speed)", width="small"),
                "Daily Theta Loss": st.column_config.TextColumn("Holding Cost", width="small"),
                "+50p NIFTY Move Gain": st.column_config.TextColumn("+50p Move Profit", width="small"),
                "Rationale": st.column_config.TextColumn("Why This Strike?", width="large"),
            }
        )
        st.info("💡 **Quick Copy for Broker Search**: `NIFTY " + f"{atm+50} PE` · `NIFTY {atm} PE` · `NIFTY {atm-50} PE`")

    with tab_greeks:
        st.markdown(
            """
            #### 🏆 The Quantitative Order of Importance for Option Buyers
            
            | Rank | Greek / Metric | Importance | What It Does | Pro-Trader Golden Rule |
            | :---: | :--- | :---: | :--- | :--- |
            | **#1** | **DELTA ($\Delta$)** | **40%** | **The Profit Engine**: Points your premium gains per 1 point NIFTY move. | **Never buy options with Delta $< 0.40$ for day trading.** Deep OTM options barely move even when your direction is right. |
            | **#2** | **THETA ($\Theta$)** | **30%** | **The Holding Tax**: Money you bleed per hour/day just by holding the contract. | **Always check $\Delta / \|\Theta\|$ ratio.** ITM options lose a smaller percentage of their value to time decay than cheap OTM lotteries. |
            | **#3** | **GAMMA ($\Gamma$)** | **20%** | **The Turbocharger**: How fast Delta expands when price moves in your favor. | **Highest at ATM on Expiry Days.** If trading an explosive breakout with high volume, ATM Gamma gives the highest percentage return. |
            | **#4** | **VEGA ($\nu$) & VIX** | **10%** | **The Volatility Wind**: Premium change per 1% change in India VIX. | **When VIX is crashing, avoid buying options.** IV crush will offset your directional gains. Buy when VIX is steady or turning up. |
            """
        )

