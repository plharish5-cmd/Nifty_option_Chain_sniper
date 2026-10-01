"""
vpin_scanner.py — High-Frequency Order Flow Toxicity & VPIN Scanner
====================================================================

CLI tool to compute and display the Volume-Synchronized Probability of
Informed Trading (VPIN) indicator and option strategy signals using either:
  1. Offline historical candle data (data/candles/YYYY-MM-DD.csv)
  2. Live market data via Dhan HQ APIs (dhan_frames.py / dhan_client.py)

METHODOLOGY:
  Easley, Lopez de Prado, & O'Hara (2010, 2012):
  - Standard OHLCV bars (no tick rule)
  - Bulk Volume Classification (BVC) via standard normal CDF
  - Strict uniform volume bucketing with proportional row splitting
  - Rolling VPIN calculation over n volume buckets
  - Directional order flow imbalance & empirical CDF toxicity percentile

USAGE:
  python vpin_scanner.py --date 2026-09-11
  python vpin_scanner.py --dhan --interval 15 --days 5
  python vpin_scanner.py --date 2026-09-11 --bucket-volume 2000000 --window 20

RULEBOOK ADHERENCE:
  - Rule 1: Reuses dhan_frames.py and dhan_client.py without rewriting
  - Rule 2: Does not touch dashboard.py
  - Rule 3: Filename checked before creation
  - Rule 4: Thresholds loaded from vpin_rules.json
  - Rule 5: Secrets read only via .env, never printed
  - Rule 6: No bare except: pass
  - Rule 7: Full path printed on placement
  - Rule 8: Alert-only. No trade execution.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from vpin_engine import VPINEngine, load_vpin_rules

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
log = logging.getLogger("vpin_scanner")


def parse_args() -> argparse.Namespace:
    rules = load_vpin_rules()
    parser = argparse.ArgumentParser(
        description="VPIN (Volume-Synchronized Probability of Informed Trading) Scanner"
    )
    parser.add_argument(
        "--date",
        type=str,
        default=None,
        help="Date string YYYY-MM-DD for offline candle CSV in data/candles/ (e.g. 2026-09-11)",
    )
    parser.add_argument(
        "--dhan",
        action="store_true",
        help="Fetch live market data via Dhan HQ API",
    )
    parser.add_argument(
        "--interval",
        type=str,
        default="15",
        help="Candle interval for Dhan (1, 5, 15). Default: 15",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=5,
        help="Days of historical data to fetch via Dhan. Default: 5",
    )
    parser.add_argument(
        "--bucket-volume",
        type=float,
        default=None,
        help=f"Bucket volume V. If not passed, auto-estimated or loaded from config (default: {rules.get('bucket_volume')})",
    )
    parser.add_argument(
        "--window",
        type=int,
        default=None,
        help=f"Rolling bucket window length n (default: {rules.get('window_length')})",
    )
    parser.add_argument(
        "--tail",
        type=int,
        default=12,
        help="Number of latest volume buckets to display. Default: 12",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    rules = load_vpin_rules()

    print("\n" + "=" * 78)
    print(" VPIN SCANNER — VOLUME-SYNCHRONIZED PROBABILITY OF INFORMED TRADING")
    print(" Easley, Lopez de Prado, & O'Hara (2010, 2012) Microstructure Model")
    print("=" * 78)

    df: pd.DataFrame | None = None
    data_source_desc = ""

    # 1. Fetch live from Dhan if requested
    if args.dhan:
        print("\n[Data Fetch] Connecting to Dhan HQ APIs...")
        try:
            import dhan_client as dc
            ok, msg = dc.verify_token()
            if not ok:
                print(f"[Dhan Auth Error] {msg}")
                print("Falling back to local offline candles...")
            else:
                print(f"[Dhan Auth OK] Connected: {msg}")
                res, report = VPINEngine.from_dhan_frames(
                    interval=args.interval,
                    days_back=args.days,
                    mode="futures",
                    bucket_volume=args.bucket_volume,
                    window_length=args.window,
                )
                if not res.buckets.empty:
                    df = res.bars
                    data_source_desc = f"Dhan HQ Live Futures ({args.interval}m candles, {report.get('source', 'api')})"
                    display_vpin_results(res, data_source_desc, args.tail)
                    return 0
        except Exception as exc:
            log.error("Failed to fetch from Dhan HQ: %s", exc)
            print(f"[Warning] Dhan API call failed: {exc}. Switching to offline candle data.")

    # 2. Offline candle loading
    candle_dir = Path(__file__).parent / "data" / "candles"
    target_csv: Path | None = None

    if args.date:
        candidate = candle_dir / f"{args.date}.csv"
        if candidate.exists():
            target_csv = candidate
        else:
            print(f"[Error] Candle file for {args.date} not found at {candidate}")
            return 1
    else:
        # Pick latest available CSV in data/candles/
        csv_files = sorted(candle_dir.glob("2026-*.csv"))
        if csv_files:
            target_csv = csv_files[-1]
        else:
            print(f"[Error] No candle files found in {candle_dir}")
            return 1

    print(f"\n[Data Fetch] Loading historical candles from: {target_csv.name}")
    try:
        df = pd.read_csv(target_csv)
    except Exception as exc:
        log.error("Failed to read CSV %s: %s", target_csv, exc)
        return 1

    data_source_desc = f"Offline OHLCV ({target_csv.name}, {len(df)} bars)"

    # Auto-adjust bucket volume for index cash volume vs futures volume
    tot_vol = df["volume"].sum()
    b_vol = args.bucket_volume
    if b_vol is None:
        if tot_vol > 50_000_000:
            # Index volume aggregate (~270M/day) -> ~50 buckets/day = ~5.4M per bucket
            # Use 2.5M for more granularity across the session
            b_vol = 2_500_000.0
        else:
            b_vol = float(rules.get("bucket_volume", 50000.0))

    w_len = args.window or int(rules.get("window_length", 20))

    engine = VPINEngine(
        bucket_volume=b_vol,
        window_length=w_len,
    )

    result = engine.calculate(df)
    display_vpin_results(result, data_source_desc, args.tail)
    return 0


def display_vpin_results(result, data_source_desc: str, tail: int = 12) -> None:
    b_df = result.buckets
    summary = result.summary

    print(f"\nSource:           {data_source_desc}")
    print(f"Total Bars:       {summary.get('total_bars')}")
    print(f"Bucket Volume V:  {summary.get('bucket_volume'):,.0f}")
    print(f"Window Length n:  {summary.get('window_length')} buckets")
    print(f"Buckets Created:  {summary.get('total_buckets')}")

    if b_df.empty:
        print("\n[Notice] Not enough volume to fill a single volume bucket.")
        return

    latest_vpin = summary.get("latest_vpin")
    latest_dir = summary.get("latest_direction")
    latest_cdf = summary.get("latest_cdf")
    latest_regime = summary.get("latest_regime")
    alert_active = summary.get("alert_active")

    print("\n" + "-" * 78)
    print(" LATEST ORDER FLOW TOXICITY READING")
    print("-" * 78)

    if latest_vpin is not None:
        vpin_bar = "█" * int(latest_vpin * 40) + "░" * (40 - int(latest_vpin * 40))
        print(f" VPIN Score:         {latest_vpin:.4f}  [{vpin_bar}]  (Range: 0.0 - 1.0)")
        print(f" Directional Bias:   {latest_dir:+.4f}  ({'BULLISH' if latest_dir > 0 else 'BEARISH' if latest_dir < 0 else 'NEUTRAL'})")
        if latest_cdf is not None:
            print(f" Empirical CDF:      {latest_cdf:.1%}  (Historical toxicity percentile)")
        print(f" Strategy Regime:    {latest_regime}")
        print(f" Flash Crash Alert:  {'CRITICAL DANGER (CDF > 99%)' if alert_active else 'Normal'}")

        print("\n" + "-" * 78)
        print(" ACTIONABLE DERIVATIVES SIGNAL (Rule 8: Alert Only)")
        print("-" * 78)
        if latest_regime == "EXPANSION_OPTION_BUYING_CE":
            print(" >> SIGNAL: STRONG BULLISH MOMENTUM BREAKOUT")
            print(" >> RECOMMENDED: OPTION BUYING — ATM/Slight OTM CALL (CE)")
            print(" >> RATIONALE: High order flow toxicity backed by dominant aggressive buy volume.")
        elif latest_regime == "EXPANSION_OPTION_BUYING_PE":
            print(" >> SIGNAL: STRONG BEARISH FLUSH / BREAKDOWN")
            print(" >> RECOMMENDED: OPTION BUYING — ATM/Slight OTM PUT (PE)")
            print(" >> RATIONALE: High order flow toxicity backed by dominant aggressive sell volume.")
        elif latest_regime == "EXPANSION_VOLATILITY_BREAKOUT":
            print(" >> SIGNAL: DIRECTIONALLY UNRESOLVED VOLATILITY EXPANSION")
            print(" >> RECOMMENDED: OPTION BUYING — LONG STRADDLE / STRANGLE")
            print(" >> RATIONALE: Extreme order flow toxicity; market makers widening spreads.")
        elif latest_regime == "RANGEBOUND_OPTION_SELLING":
            print(" >> SIGNAL: LOW TOXICITY CONSOLIDATION")
            print(" >> RECOMMENDED: OPTION SELLING — SHORT STRADDLE / STRANGLE / IRON CONDOR")
            print(" >> RATIONALE: Order flow is balanced, adverse selection risk is low, capture theta.")
        else:
            print(" >> SIGNAL: BALANCED / NEUTRAL")
            print(" >> RECOMMENDED: WAIT FOR REGIME EXPANSION")
    else:
        print(" [Warming up volume buckets — window length not yet reached]")

    print("\n" + "-" * 78)
    print(f" RECENT VOLUME BUCKETS (Last {tail} buckets)")
    print("-" * 78)

    disp_cols = [
        "bucket_idx", "timestamp", "price", "buy_volume", "sell_volume",
        "abs_imbalance", "vpin", "vpin_direction", "strategy_regime"
    ]
    sub_df = b_df[disp_cols].tail(tail).copy()
    sub_df["buy_vol_k"] = (sub_df["buy_volume"] / 1000).map("{:,.1f}k".format)
    sub_df["sell_vol_k"] = (sub_df["sell_volume"] / 1000).map("{:,.1f}k".format)
    sub_df["price"] = sub_df["price"].map("{:,.2f}".format)
    sub_df["vpin"] = sub_df["vpin"].map(lambda x: f"{x:.3f}" if pd.notna(x) else "---")
    sub_df["vpin_direction"] = sub_df["vpin_direction"].map(lambda x: f"{x:+.3f}" if pd.notna(x) else "---")

    final_disp = sub_df[[
        "bucket_idx", "timestamp", "price", "buy_vol_k", "sell_vol_k",
        "vpin", "vpin_direction", "strategy_regime"
    ]]
    print(final_disp.to_string(index=False))
    print("-" * 78)


if __name__ == "__main__":
    sys.exit(main())
