"""
btst_scanner.py — Buy Today, Sell Tomorrow (BTST) Strategy Engine.

Implements the two BTST trading methodologies outlined by Neeraj Joshi:

1. Stock Momentum BTST Basket:
   - Evaluates momentum stocks closing near day's high (upper 15-20% of range).
   - Filters for volume shockers and institutional turnover.
   - Enforces Sector Breadth / Diversification (5-6 stocks from distinct sectors
     to mitigate overnight single-sector or company-specific risks).
   - Generates entry price (3:15 PM), morning target (+1.5% to +2.5%), and stop-loss.

2. Index Long Straddle BTST Analyzer:
   - Evaluates simultaneous ATM Call + ATM Put purchase before close (3:15-3:25 PM).
   - Quantifies overnight Theta decay drag and calculates the exact morning gap %
     needed to break even.
   - Warns against expiry day traps (fatal theta drop).
   - Models morning gap payoff scenarios (-1.5% to +1.5%).

3. BTST Market Timing & Window Rules:
   - 15:10 - 15:25 IST: Prime Entry Window.
   - 09:15 - 09:30 IST: Morning Exit / Gap Profit Booking Window.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Any

import numpy as np
import pandas as pd

import config

# ═══════════════════════════════════════════════════════════════════════════
# Sector Mapping (Liquid Indian Equities)
# ═══════════════════════════════════════════════════════════════════════════

SECTOR_MAP: dict[str, str] = {
    # Banking & Financials
    "HDFCBANK": "Banking & Finance",
    "ICICIBANK": "Banking & Finance",
    "SBIN": "Banking & Finance",
    "AXISBANK": "Banking & Finance",
    "KOTAKBANK": "Banking & Finance",
    "BAJFINANCE": "Banking & Finance",
    "BAJAJFINSV": "Banking & Finance",
    "CHOLAFIN": "Banking & Finance",
    "SHRIRAMFIN": "Banking & Finance",
    "INDUSINDBK": "Banking & Finance",
    "BANKBARODA": "Banking & Finance",
    "MUTHOOTFIN": "Banking & Finance",
    "PFC": "Banking & Finance",
    "RECLTD": "Banking & Finance",
    "SBICARD": "Banking & Finance",
    "SBILIFE": "Banking & Finance",
    "HDFCLIFE": "Banking & Finance",
    "CANBK": "Banking & Finance",
    "PNB": "Banking & Finance",
    "AUBANK": "Banking & Finance",
    "IDFCFIRSTB": "Banking & Finance",
    "JIOFIN": "Banking & Finance",
    "FEDERALBNK": "Banking & Finance",
    "LICHSGFIN": "Banking & Finance",
    "MANAPPURAM": "Banking & Finance",

    # IT & Software Services
    "TCS": "IT & Software",
    "INFY": "IT & Software",
    "HCLTECH": "IT & Software",
    "WIPRO": "IT & Software",
    "TECHM": "IT & Software",
    "LTIM": "IT & Software",
    "COFORGE": "IT & Software",
    "PERSISTENT": "IT & Software",
    "MPHASIS": "IT & Software",
    "LTTS": "IT & Software",
    "NAUKRI": "IT & Software",
    "KPITTECH": "IT & Software",
    "OFSS": "IT & Software",
    "TATAELXSI": "IT & Software",

    # Automobile & Ancillaries
    "TATAMOTORS": "Automobile",
    "MARUTI": "Automobile",
    "M&M": "Automobile",
    "BAJAJ-AUTO": "Automobile",
    "HEROMOTOCO": "Automobile",
    "TVSMOTOR": "Automobile",
    "BHARATFORG": "Automobile",
    "BOSCHLTD": "Automobile",
    "EICHERMOT": "Automobile",
    "MOTHERSON": "Automobile",
    "MRF": "Automobile",
    "APOLLOTYRE": "Automobile",
    "ASHOKLEY": "Automobile",
    "BALKRISIND": "Automobile",

    # Metals, Mining & Commodities
    "TATASTEEL": "Metals & Mining",
    "JSWSTEEL": "Metals & Mining",
    "HINDALCO": "Metals & Mining",
    "VEDL": "Metals & Mining",
    "COALINDIA": "Metals & Mining",
    "NMDC": "Metals & Mining",
    "JINDALSTEL": "Metals & Mining",
    "NATIONALUM": "Metals & Mining",
    "HINDZINC": "Metals & Mining",
    "SAIL": "Metals & Mining",

    # Pharma & Healthcare
    "SUNPHARMA": "Pharma & Healthcare",
    "CIPLA": "Pharma & Healthcare",
    "DRREDDY": "Pharma & Healthcare",
    "DIVISLAB": "Pharma & Healthcare",
    "APOLLOHOSP": "Pharma & Healthcare",
    "LUPIN": "Pharma & Healthcare",
    "TORNTPHARM": "Pharma & Healthcare",
    "ZYDUSLIFE": "Pharma & Healthcare",
    "MAXHEALTH": "Pharma & Healthcare",
    "MANKIND": "Pharma & Healthcare",
    "BIOCON": "Pharma & Healthcare",
    "AUROPHARMA": "Pharma & Healthcare",
    "ALKEM": "Pharma & Healthcare",
    "GLENMARK": "Pharma & Healthcare",
    "IPCALAB": "Pharma & Healthcare",

    # Energy, Power & Utilities
    "RELIANCE": "Energy & Oil/Gas",
    "ONGC": "Energy & Oil/Gas",
    "NTPC": "Power & Utilities",
    "POWERGRID": "Power & Utilities",
    "BPCL": "Energy & Oil/Gas",
    "IOC": "Energy & Oil/Gas",
    "GAIL": "Energy & Oil/Gas",
    "TATAPOWER": "Power & Utilities",
    "ADANIGREEN": "Power & Utilities",
    "ADANIPOWER": "Power & Utilities",
    "NHPC": "Power & Utilities",
    "IGL": "Energy & Oil/Gas",
    "MGL": "Energy & Oil/Gas",
    "PETRONET": "Energy & Oil/Gas",
    "OIL": "Energy & Oil/Gas",

    # FMCG & Consumer Products
    "ITC": "FMCG & Consumer",
    "HINDUNILVR": "FMCG & Consumer",
    "NESTLEIND": "FMCG & Consumer",
    "BRITANNIA": "FMCG & Consumer",
    "TATACONSUM": "FMCG & Consumer",
    "DABUR": "FMCG & Consumer",
    "GODREJCP": "FMCG & Consumer",
    "MARICO": "FMCG & Consumer",
    "VBL": "FMCG & Consumer",
    "COLPAL": "FMCG & Consumer",
    "PGHH": "FMCG & Consumer",
    "DMART": "Retail & Consumer",
    "TITAN": "Consumer Discretionary",
    "TRENT": "Retail & Consumer",
    "ZOMATO": "Tech & Delivery",

    # Infrastructure, Capital Goods & Defense
    "LT": "Infra & Capital Goods",
    "ADANIENT": "Infra & Conglomerate",
    "ADANIPORTS": "Infra & Logistics",
    "SIEMENS": "Infra & Capital Goods",
    "ABB": "Infra & Capital Goods",
    "BEL": "Defense & Electronics",
    "HAL": "Defense & Aerospace",
    "BHEL": "Infra & Capital Goods",
    "CUMMINSIND": "Infra & Capital Goods",
    "HAVELLS": "Consumer Electricals",
    "POLYCAB": "Consumer Electricals",
    "GRASIM": "Materials & Conglomerate",
    "VOLTAS": "Consumer Electricals",

    # Real Estate & Realty
    "DLF": "Real Estate",
    "GODREJPROP": "Real Estate",
    "OBEROIRLTY": "Real Estate",
    "PRESTIGE": "Real Estate",
    "LODHA": "Real Estate",
    "PHOENIXLTD": "Real Estate",

    # Chemicals & Fertilizers
    "PIDILITIND": "Chemicals",
    "SRF": "Chemicals",
    "UPL": "Chemicals",
    "TATACHEM": "Chemicals",
    "DEEPAKNTR": "Chemicals",
    "AARTIIND": "Chemicals",
    "PIIND": "Chemicals",
}


def get_sector(symbol: str) -> str:
    """Returns the sector for a given stock symbol or a heuristic fallback."""
    clean = symbol.strip().upper()
    if clean in SECTOR_MAP:
        return SECTOR_MAP[clean]
    if "BANK" in clean or "FIN" in clean:
        return "Banking & Finance"
    if "PHARMA" in clean or "LAB" in clean:
        return "Pharma & Healthcare"
    if "TECH" in clean or "SOFT" in clean:
        return "IT & Software"
    if "AUTO" in clean or "MOT" in clean:
        return "Automobile"
    if "STEEL" in clean or "MET" in clean:
        return "Metals & Mining"
    if "POWER" in clean or "ENERGY" in clean or "GAS" in clean:
        return "Energy & Power"
    return "Diversified"


# ═══════════════════════════════════════════════════════════════════════════
# Market Timing Check
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class TimingStatus:
    status: str          # "ACTIVE_WINDOW", "MONITORING", "EXIT_WINDOW", "CLOSED"
    message: str
    color: str           # Hex color for UI badges
    is_entry_time: bool
    is_exit_time: bool


def get_timing_status(now: datetime | None = None) -> TimingStatus:
    """
    Checks if the current market time is in the optimal BTST entry or exit window.
    Neeraj Joshi rule:
      Entry: 3:10 PM - 3:25 PM
      Exit:  9:15 AM - 9:30 AM next morning
    """
    now = now or datetime.now()
    t = now.time()

    if time(15, 10) <= t <= time(15, 26):
        return TimingStatus(
            status="🔥 PRIME ENTRY WINDOW",
            message="Optimal BTST execution window (3:10 PM - 3:25 PM). Screen stocks & place MOC/limit orders.",
            color="#2FBF71",  # Green
            is_entry_time=True,
            is_exit_time=False,
        )
    elif time(9, 15) <= t <= time(9, 30):
        return TimingStatus(
            status="🚀 MORNING EXIT WINDOW",
            message="Opening momentum window (9:15 AM - 9:30 AM). Book gap-up profits and protect gains.",
            color="#4A9BD1",  # Blue
            is_entry_time=False,
            is_exit_time=True,
        )
    elif time(14, 30) <= t < time(15, 10):
        return TimingStatus(
            status="👀 PRE-CLOSING RADAR",
            message="Afternoon build-up phase. Watch for volume shockers and day-high consolidation.",
            color="#E0A030",  # Amber
            is_entry_time=False,
            is_exit_time=False,
        )
    elif time(9, 30) < t < time(14, 30):
        return TimingStatus(
            status="⏳ INTRADAY SESSION",
            message="Intraday session active. BTST screening begins after 2:30 PM.",
            color="#8FA6B8",  # Slate
            is_entry_time=False,
            is_exit_time=False,
        )
    else:
        return TimingStatus(
            status="🌙 POST MARKET / STANDBY",
            message="Market closed. Showing latest recorded session data for prep and backtest.",
            color="#8FA6B8",
            is_entry_time=False,
            is_exit_time=False,
        )


# ═══════════════════════════════════════════════════════════════════════════
# Strategy 1: Stock Momentum BTST Basket
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class StockCandidate:
    symbol: str
    company_name: str
    sector: str
    ltp: float
    pchange: float
    day_high: float
    day_low: float
    open_price: float
    volume: int
    turnover_cr: float
    close_to_high_pct: float     # (LTP / DayHigh) * 100
    upper_wick_pct: float        # (High - Close) / (High - Low)
    target_price: float          # Recommended target (+1.8%)
    stop_loss: float             # Recommended SL (-1.2%)
    risk_reward: float
    score: float
    reasons: list[str] = field(default_factory=list)


@dataclass
class StockBasketResult:
    basket: list[StockCandidate]
    all_screened_count: int
    qualified_count: int
    sector_counts: dict[str, int]
    avg_pchange: float
    summary: str


def scan_stock_btst(
    data_rows: list[dict[str, Any]],
    min_pchange: float = 1.0,
    max_pchange: float = 7.0,
    max_upper_wick_ratio: float = 0.22,
    min_turnover_cr: float = 20.0,
    basket_size: int = 5,
) -> StockBasketResult:
    """
    Screens constituent stock data according to Neeraj Joshi's Stock BTST Rules:
    1. Strong Positive Momentum (+1.0% to +7.0% pChange, avoids crazy circuit traps).
    2. Closing near Day's High: Price in the top 20% of range; LTP >= 98.5% of high.
    3. Institutional Liquidity / Volume: Turnover > min_turnover_cr.
    4. Multi-Sector Breadth: Selects up to 1 stock per distinct sector first to create
       a 5-6 stock diversified basket, eliminating single-sector crash risk.
    """
    candidates: list[StockCandidate] = []

    for row in data_rows:
        sym = str(row.get("symbol") or "").strip().upper()
        if not sym or sym in ("NIFTY 50", "NIFTY 100", "NIFTY 500", "NIFTY NEXT 50"):
            continue

        ltp = float(row.get("lastPrice") or 0.0)
        high = float(row.get("dayHigh") or 0.0)
        low = float(row.get("dayLow") or 0.0)
        open_p = float(row.get("open") or 0.0)
        pchange = float(row.get("pChange") or 0.0)
        vol = int(row.get("totalTradedVolume") or 0)
        
        # totalTradedValue is typically in INR on NSE JSON
        raw_val = float(row.get("totalTradedValue") or 0.0)
        turnover_cr = round(raw_val / 1e7, 2)

        # Basic validity checks
        if ltp <= 0 or high <= 0 or low <= 0 or open_p <= 0:
            continue

        # 1. Bullish candle check: Close must be higher than Open
        if ltp < open_p:
            continue

        # 2. Percentage change filter
        if pchange < min_pchange or pchange > max_pchange:
            continue

        # 3. Upper wick and day high proximity
        rng = high - low
        if rng <= 0:
            continue

        wick_ratio = (high - ltp) / rng
        if wick_ratio > max_upper_wick_ratio:
            continue

        close_to_high = (ltp / high) * 100.0
        if close_to_high < 98.0:
            continue

        # 4. Liquidity / turnover filter
        if turnover_cr > 0 and turnover_cr < min_turnover_cr:
            continue

        sector = get_sector(sym)
        company = str(row.get("companyName") or sym)

        # 5. Calculate Score
        # Rewards higher % change, tighter close to high, and high turnover
        score = (
            (pchange * 2.5)
            + ((1.0 - wick_ratio) * 15.0)
            + (min(turnover_cr, 500.0) / 40.0)
        )

        reasons = [
            f"Strong momentum: +{pchange:.2f}%",
            f"Close in top {round((1 - wick_ratio)*100)}% of range (LTP: ₹{ltp:,.2f})",
            f"Turnover: ₹{turnover_cr:,.1f} Cr",
        ]

        target = round(ltp * 1.018, 2)   # +1.8% morning target
        sl = round(ltp * 0.988, 2)       # -1.2% stop loss
        rr = round(1.8 / 1.2, 2)

        candidates.append(
            StockCandidate(
                symbol=sym,
                company_name=company,
                sector=sector,
                ltp=ltp,
                pchange=pchange,
                day_high=high,
                day_low=low,
                open_price=open_p,
                volume=vol,
                turnover_cr=turnover_cr,
                close_to_high_pct=round(close_to_high, 2),
                upper_wick_pct=round(wick_ratio, 3),
                target_price=target,
                stop_loss=sl,
                risk_reward=rr,
                score=round(score, 2),
                reasons=reasons,
            )
        )

    # Sort all qualified candidates by score descending
    candidates.sort(key=lambda c: c.score, reverse=True)

    # Multi-Sector Diversification selection:
    # First pass: Pick the highest-scoring stock from each distinct sector
    selected_basket: list[StockCandidate] = []
    seen_sectors: set[str] = set()

    for cand in candidates:
        if cand.sector not in seen_sectors:
            selected_basket.append(cand)
            seen_sectors.add(cand.sector)
            if len(selected_basket) >= basket_size:
                break

    # Second pass: If fewer than basket_size distinct sectors are found,
    # fill remaining slots with highest remaining candidates
    if len(selected_basket) < basket_size:
        selected_symbols = {c.symbol for c in selected_basket}
        for cand in candidates:
            if cand.symbol not in selected_symbols:
                selected_basket.append(cand)
                selected_symbols.add(cand.symbol)
                if len(selected_basket) >= basket_size:
                    break

    # Sector distribution count
    sector_dist: dict[str, int] = {}
    for c in selected_basket:
        sector_dist[c.sector] = sector_dist.get(c.sector, 0) + 1

    avg_pc = (
        round(sum(c.pchange for c in selected_basket) / len(selected_basket), 2)
        if selected_basket
        else 0.0
    )

    summary = (
        f"Selected {len(selected_basket)} stocks across {len(sector_dist)} sectors "
        f"with avg momentum of +{avg_pc}%."
        if selected_basket
        else "No stocks met strict BTST criteria (positive close near high with turnover)."
    )

    return StockBasketResult(
        basket=selected_basket,
        all_screened_count=len(data_rows),
        qualified_count=len(candidates),
        sector_counts=sector_dist,
        avg_pchange=avg_pc,
        summary=summary,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Strategy 2: Index Long Straddle BTST Analyzer
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class StraddleScenario:
    gap_pct: float           # e.g. -1.0%, +0.5%
    sim_spot: float
    gap_pts: float
    est_ce_val: float
    est_pe_val: float
    est_straddle_val: float
    pnl_per_lot_rs: float
    pnl_pct: float
    outcome: str             # "PROFIT", "LOSS", "BREAKEVEN"


@dataclass
class StraddleAnalysis:
    spot: float
    atm_strike: float
    ce_ltp: float
    pe_ltp: float
    ce_iv: float
    pe_iv: float
    straddle_pts: float              # CE LTP + PE LTP
    straddle_cost_rs: float          # Straddle * Lot Size
    lot_size: int
    expiry: str
    dte: float
    vix: float | None
    est_overnight_theta_pts: float   # Expected decay if flat open
    est_overnight_theta_rs: float
    decay_pct: float
    breakeven_gap_pts: float         # Points move needed to break even at 9:18 AM
    breakeven_gap_pct: float         # % gap needed to break even
    upper_breakeven: float
    lower_breakeven: float
    verdict: str                     # "FAVORABLE", "CAUTION", "AVOID"
    verdict_badge: str
    verdict_reason: str
    is_expiry_tomorrow: bool
    scenarios: list[StraddleScenario]


def evaluate_options_straddle(
    chain_df: pd.DataFrame,
    spot: float,
    expiry: str = "",
    dte: float = 4.0,
    vix: float | None = None,
    lot_size: int = 65,
) -> StraddleAnalysis | None:
    """
    Analyzes an Overnight Long Straddle (ATM CE + ATM PE) based on Neeraj Joshi's framework:
    - Calculates exact straddle cost and overnight theta exposure.
    - Estimates the required morning opening gap % to beat time decay.
    - Warns against high theta degradation (e.g. holding straddle into expiry morning).
    - Generates morning opening P&L scenario payoff.
    """
    if chain_df.empty or spot <= 0:
        return None

    # Identify ATM strike (closest to spot)
    chain_sorted = chain_df.copy()
    chain_sorted["dist"] = (chain_sorted["strike"] - spot).abs()
    atm_row = chain_sorted.sort_values("dist").iloc[0]

    atm_strike = float(atm_row["strike"])
    ce_ltp = float(atm_row.get("ce_ltp") or 0.0)
    pe_ltp = float(atm_row.get("pe_ltp") or 0.0)
    ce_iv = float(atm_row.get("ce_iv") or 0.0)
    pe_iv = float(atm_row.get("pe_iv") or 0.0)

    if ce_ltp <= 0 or pe_ltp <= 0:
        return None

    straddle_pts = round(ce_ltp + pe_ltp, 2)
    straddle_cost_rs = round(straddle_pts * lot_size, 2)

    # Check if tomorrow is expiry (DTE <= 1.0)
    is_expiry_tomorrow = dte <= 1.0

    # Overnight Theta Drag estimation:
    # On regular days (DTE >= 3), overnight theta loss is ~12-16% of extrinsic.
    # On DTE = 2, overnight loss is ~18-22%.
    # On DTE = 1 (expiry tomorrow morning), overnight theta loss is massive (~30-45%).
    if dte <= 0.8:
        decay_rate = 0.40  # 40% decay
    elif dte <= 1.5:
        decay_rate = 0.28  # 28% decay
    elif dte <= 3.0:
        decay_rate = 0.18  # 18% decay
    else:
        decay_rate = 0.14  # 14% decay

    est_overnight_theta_pts = round(straddle_pts * decay_rate, 2)
    est_overnight_theta_rs = round(est_overnight_theta_pts * lot_size, 2)

    # Breakeven Gap needed:
    # The winning leg must expand enough to cover the theta loss + delta drag of losing leg
    breakeven_gap_pts = round(est_overnight_theta_pts * 1.35, 1)
    breakeven_gap_pct = round((breakeven_gap_pts / spot) * 100.0, 2)

    upper_be = round(spot + breakeven_gap_pts, 1)
    lower_be = round(spot - breakeven_gap_pts, 1)

    # Determine Verdict
    if is_expiry_tomorrow:
        verdict = "AVOID"
        verdict_badge = "🔴 AVOID STRADDLE"
        verdict_reason = (
            f"Tomorrow is Weekly Expiry (DTE {dte:.1f}). Overnight theta decay will destroy "
            f"~{int(decay_rate*100)}% of the straddle (₹{est_overnight_theta_rs:,.0f}/lot) if open is flat. "
            f"Do not buy overnight straddles on pre-expiry days."
        )
    elif vix is not None and vix > 20.0:
        verdict = "CAUTION"
        verdict_badge = "🟠 CAUTION (HIGH IV)"
        verdict_reason = (
            f"India VIX is elevated ({vix:.1f}). Options are expensive; morning IV crush "
            f"can hurt both legs even on a moderate gap."
        )
    elif vix is not None and vix < 11.5:
        verdict = "FAVORABLE"
        verdict_badge = "🟢 HIGH CONVICTION (CHEAP STRADDLE)"
        verdict_reason = (
            f"India VIX is compressed ({vix:.1f}). Combined premium is low; any overnight "
            f"global shock/gap produces explosive gamma expansion with minimal initial outlay."
        )
    else:
        verdict = "SELECTIVE"
        verdict_badge = "🟡 EVENT-DRIVEN ONLY"
        verdict_reason = (
            f"Standard market regime. Only take overnight straddles before high-impact overnight "
            f"catalysts (US Fed, RBI Policy, Major Election, or Global Index breakouts). "
            f"Requires at least ±{breakeven_gap_pct}% opening gap (±{breakeven_gap_pts} pts) to profit."
        )

    # Scenario Simulation (-1.5% to +1.5%)
    scenarios: list[StraddleScenario] = []
    gaps = [-1.5, -1.0, -0.6, -0.3, 0.0, 0.3, 0.6, 1.0, 1.5]

    for g in gaps:
        sim_spot = spot * (1.0 + g / 100.0)
        gap_pts = round(sim_spot - spot, 1)

        # Post-open estimated value: Intrinsic value + remaining extrinsic value after decay
        # For CE:
        ce_intrinsic = max(0.0, sim_spot - atm_strike)
        ce_extrinsic = max(0.0, ce_ltp - max(0.0, spot - atm_strike))
        est_ce = round(ce_intrinsic + ce_extrinsic * (1.0 - decay_rate), 2)

        # For PE:
        pe_intrinsic = max(0.0, atm_strike - sim_spot)
        pe_extrinsic = max(0.0, pe_ltp - max(0.0, atm_strike - spot))
        est_pe = round(pe_intrinsic + pe_extrinsic * (1.0 - decay_rate), 2)

        est_tot = round(est_ce + est_pe, 2)
        diff_pts = est_tot - straddle_pts
        pnl_rs = round(diff_pts * lot_size, 2)
        pnl_pct = round((diff_pts / straddle_pts) * 100.0, 1)

        if pnl_rs > 200:
            outcome = "PROFIT"
        elif pnl_rs < -200:
            outcome = "LOSS"
        else:
            outcome = "BREAKEVEN"

        scenarios.append(
            StraddleScenario(
                gap_pct=g,
                sim_spot=round(sim_spot, 1),
                gap_pts=gap_pts,
                est_ce_val=est_ce,
                est_pe_val=est_pe,
                est_straddle_val=est_tot,
                pnl_per_lot_rs=pnl_rs,
                pnl_pct=pnl_pct,
                outcome=outcome,
            )
        )

    return StraddleAnalysis(
        spot=spot,
        atm_strike=atm_strike,
        ce_ltp=ce_ltp,
        pe_ltp=pe_ltp,
        ce_iv=ce_iv,
        pe_iv=pe_iv,
        straddle_pts=straddle_pts,
        straddle_cost_rs=straddle_cost_rs,
        lot_size=lot_size,
        expiry=expiry,
        dte=dte,
        vix=vix,
        est_overnight_theta_pts=est_overnight_theta_pts,
        est_overnight_theta_rs=est_overnight_theta_rs,
        decay_pct=round(decay_rate * 100, 1),
        breakeven_gap_pts=breakeven_gap_pts,
        breakeven_gap_pct=breakeven_gap_pct,
        upper_breakeven=upper_be,
        lower_breakeven=lower_be,
        verdict=verdict,
        verdict_badge=verdict_badge,
        verdict_reason=verdict_reason,
        is_expiry_tomorrow=is_expiry_tomorrow,
        scenarios=scenarios,
    )
