"""
test_btst.py — Comprehensive Unit & Integration Test for BTST Suite.
Tests both:
1. Stock Momentum BTST Basket (diversification, sector balance, scoring, targets)
2. Index Long Straddle BTST Analyzer (breakeven gap, theta drag, scenario payoff)
"""
import pandas as pd
import numpy as np

import btst_scanner as bs


def test_stock_btst():
    print("Testing Stock BTST Screening & Diversification...")

    # Mock sample constituent data from 3 sectors + 1 weak stock + 1 high wick stock
    sample_rows = [
        # Banking (Strong, close near high)
        {
            "symbol": "HDFCBANK",
            "companyName": "HDFC Bank Ltd",
            "lastPrice": 1650.0,
            "open": 1610.0,
            "dayHigh": 1655.0,
            "dayLow": 1605.0,
            "pChange": 2.5,
            "totalTradedVolume": 8000000,
            "totalTradedValue": 13200000000, # 1320 Cr
        },
        # Banking 2 (Also strong, but should yield to diversification if other sectors exist)
        {
            "symbol": "ICICIBANK",
            "companyName": "ICICI Bank Ltd",
            "lastPrice": 1280.0,
            "open": 1250.0,
            "dayHigh": 1285.0,
            "dayLow": 1245.0,
            "pChange": 2.2,
            "totalTradedVolume": 6000000,
            "totalTradedValue": 7680000000,
        },
        # IT (Strong, close near high)
        {
            "symbol": "TCS",
            "companyName": "Tata Consultancy Services",
            "lastPrice": 4200.0,
            "open": 4120.0,
            "dayHigh": 4210.0,
            "dayLow": 4100.0,
            "pChange": 1.9,
            "totalTradedVolume": 1500000,
            "totalTradedValue": 6300000000,
        },
        # Automobile (Strong, close near high)
        {
            "symbol": "TATAMOTORS",
            "companyName": "Tata Motors Ltd",
            "lastPrice": 980.0,
            "open": 950.0,
            "dayHigh": 982.0,
            "dayLow": 945.0,
            "pChange": 3.1,
            "totalTradedVolume": 12000000,
            "totalTradedValue": 11760000000,
        },
        # Metals (Strong)
        {
            "symbol": "TATASTEEL",
            "companyName": "Tata Steel Ltd",
            "lastPrice": 155.0,
            "open": 150.0,
            "dayHigh": 156.0,
            "dayLow": 149.0,
            "pChange": 3.3,
            "totalTradedVolume": 30000000,
            "totalTradedValue": 4650000000,
        },
        # Pharma (Strong)
        {
            "symbol": "SUNPHARMA",
            "companyName": "Sun Pharmaceutical Ind",
            "lastPrice": 1800.0,
            "open": 1770.0,
            "dayHigh": 1805.0,
            "dayLow": 1765.0,
            "pChange": 1.7,
            "totalTradedVolume": 2000000,
            "totalTradedValue": 3600000000,
        },
        # Negative / Bearish stock (Should be rejected)
        {
            "symbol": "INFY",
            "companyName": "Infosys Ltd",
            "lastPrice": 1800.0,
            "open": 1850.0,
            "dayHigh": 1860.0,
            "dayLow": 1795.0,
            "pChange": -2.1,
            "totalTradedVolume": 5000000,
            "totalTradedValue": 9000000000,
        },
        # Large upper wick / dumped near close (Should be rejected)
        {
            "symbol": "RELIANCE",
            "companyName": "Reliance Industries",
            "lastPrice": 2900.0,
            "open": 2880.0,
            "dayHigh": 2980.0, # High was 2980, closed at 2900 -> huge upper wick
            "dayLow": 2870.0,
            "pChange": 0.7,
            "totalTradedVolume": 4000000,
            "totalTradedValue": 11600000000,
        },
    ]

    res = bs.scan_stock_btst(sample_rows, basket_size=5)
    print(f"Screened: {res.all_screened_count}, Qualified: {res.qualified_count}, Basket: {len(res.basket)}")
    print(f"Sectors in basket: {list(res.sector_counts.keys())}")
    assert len(res.basket) == 5, f"Expected 5 stocks, got {len(res.basket)}"
    assert len(res.sector_counts) >= 4, f"Expected high sector diversity, got {len(res.sector_counts)}"
    
    # Check that INFY and RELIANCE were rejected
    symbols = [c.symbol for c in res.basket]
    assert "INFY" not in symbols, "INFY should have been rejected (negative pChange)"
    assert "RELIANCE" not in symbols, "RELIANCE should have been rejected (large upper wick)"
    
    for c in res.basket:
        print(f"  ✓ {c.symbol:<12} [{c.sector:<18}] LTP: {c.ltp:<7.1f} (+{c.pchange:.1f}%) "
              f"Target: {c.target_price:<7.1f} SL: {c.stop_loss:<7.1f} Score: {c.score:.1f}")
    print("Stock BTST Test Passed!\n")


def test_straddle_btst():
    print("Testing Options Straddle BTST Evaluator...")

    # Mock option chain around spot = 24,500
    spot = 24510.0
    strikes = [24400, 24450, 24500, 24550, 24600]
    data = []
    for s in strikes:
        dist = s - 24500
        data.append({
            "strike": s,
            "ce_ltp": max(10.0, 110.0 - dist * 0.5),
            "pe_ltp": max(10.0, 95.0 + dist * 0.5),
            "ce_iv": 14.5,
            "pe_iv": 15.0,
            "ce_oi": 1500000,
            "pe_oi": 1400000,
        })
    chain_df = pd.DataFrame(data)

    analysis = bs.evaluate_options_straddle(
        chain_df=chain_df,
        spot=spot,
        expiry="10-OCT-2026",
        dte=3.0,
        vix=13.5,
        lot_size=65,
    )

    assert analysis is not None, "Straddle analysis should not be None"
    print(f"ATM Strike: {analysis.atm_strike}")
    print(f"Straddle Points: {analysis.straddle_pts} (₹{analysis.straddle_cost_rs:,.0f} per lot)")
    print(f"Est Overnight Theta: -{analysis.est_overnight_theta_pts} pts (-₹{analysis.est_overnight_theta_rs:,.0f})")
    print(f"Breakeven Gap Needed: ±{analysis.breakeven_gap_pts} pts (±{analysis.breakeven_gap_pct}%)")
    print(f"Verdict: {analysis.verdict_badge}")
    print(f"Scenarios simulated: {len(analysis.scenarios)}")

    # Check flat open scenario
    flat_sc = next(s for s in analysis.scenarios if s.gap_pct == 0.0)
    print(f"Flat Open P&L: ₹{flat_sc.pnl_per_lot_rs} ({flat_sc.pnl_pct}%)")
    assert flat_sc.pnl_per_lot_rs < 0, "Flat open must show theta loss"

    # Check +1.0% gap scenario
    gap_sc = next(s for s in analysis.scenarios if s.gap_pct == 1.0)
    print(f"+1.0% Gap Open P&L: ₹{gap_sc.pnl_per_lot_rs} ({gap_sc.pnl_pct}%)")
    assert gap_sc.pnl_per_lot_rs > 0, "+1.0% gap must show profit"

    # Check Expiry Day Warning
    analysis_exp = bs.evaluate_options_straddle(
        chain_df=chain_df,
        spot=spot,
        expiry="10-OCT-2026",
        dte=0.5,
        vix=13.5,
        lot_size=65,
    )
    assert analysis_exp.verdict == "AVOID", "Expiry day straddle must return AVOID"
    print(f"Expiry day check: {analysis_exp.verdict_badge} — Successfully caught!")
    print("Options Straddle BTST Test Passed!\n")


if __name__ == "__main__":
    test_stock_btst()
    test_straddle_btst()
    print("ALL BTST TESTS PASSED SUCCESSFULLY!")
