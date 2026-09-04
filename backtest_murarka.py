"""
backtest_murarka.py — Backtest Nitin Murarka's "Lighter Side" Dynamic-DTE Option Chain Strategy.

CORE METHODOLOGY:
─────────────────
1. Center Strike Anchor:
   - Anchored to the 9:08 pre-open spot (or 09:15 open spot), static for the day.
2. Dynamic DTE Strike Window (Trick 1 & 2):
   - 5 DTE: +/- 5 strikes (11 strikes)
   - 4 DTE: +/- 4 strikes (9 strikes)
   - 3 DTE: +/- 3 strikes (7 strikes)
   - 2 DTE: +/- 2 strikes (5 strikes)
   - 1 DTE: +/- 1 strike  (3 strikes)
3. Cumulative Change in OI ("The Lighter Side" - Trick 3):
   - Put Chg OI / Call Chg OI >= 1.30 -> BULLISH (Heavy put support, trade Call).
   - Put Chg OI / Call Chg OI <= 0.77 -> BEARISH (Heavy call resistance, trade Put).
   - Put unwinding + Call writing -> BEARISH.
   - Call unwinding + Put writing -> BULLISH.
4. Price & OI Correlation + Expiry Day Trap (Trick 4):
   - Bearish requires Put Price Chg > 0.
   - Bullish requires Call Price Chg > 0.
   - Trap: If BOTH Call Price Chg < 0 and Put Price Chg < 0 -> AVOID ALL TRADES!
5. VWAP Timing Confirmation (Trick 5):
   - Confirms that Spot is on the correct side of intraday VWAP.

Usage:
    python backtest_murarka.py                     # Runs standard dynamic backtest
    python backtest_murarka.py --mode 10:00        # Runs fixed 10:00 AM checkpoint
    python backtest_murarka.py --detailed          # Prints trade-by-trade breakdown
    python backtest_murarka.py --export            # Exports trades to data/murarka_backtest_trades.csv
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import numpy as np
import pandas as pd

import config
from murarka_strategy import (
    MurarkaBias,
    calculate_center_strike,
    evaluate_murarka_bias,
    get_dynamic_strike_window,
)

STRIKE_STEP = getattr(config, "STRIKE_STEP", 50)
LOT_SIZE = getattr(config, "NIFTY_LOT_SIZE", 75)


@dataclass
class MurarkaTrade:
    date: str
    entry_time: str
    direction: str             # BULLISH (BUY_CE) | BEARISH (BUY_PE)
    center_strike: float
    atm_strike: float
    dte: float
    window_n: int
    oi_ratio: float
    call_chg_oi: float
    put_chg_oi: float
    entry_spot: float
    entry_vwap: float
    entry_opt_ltp: float
    exit_time: str = ""
    exit_spot: float = 0.0
    exit_opt_ltp: float = 0.0
    exit_reason: str = ""      # TARGET_40 | SL_40 | OI_FLIP | EXPIRY_TRAP | EOD
    spot_pnl: float = 0.0
    opt_pnl: float = 0.0
    pnl_rupees: float = 0.0
    max_fav_spot: float = 0.0
    max_adv_spot: float = 0.0
    hit_target_40: bool = False
    hit_target_60: bool = False
    hit_target_80: bool = False


def load_market_snapshots(date_dir: Path) -> List[Tuple[time, Path, float]]:
    """
    Returns list of (time, parquet_path, spot_price) during market hours 09:15 - 15:30.
    """
    pfs = sorted(date_dir.glob("*.parquet"))
    market_snaps = []
    for pf in pfs:
        try:
            stem = pf.stem
            h = int(stem[0:2])
            m = int(stem[2:4])
            s = int(stem[4:6])
            snap_t = time(h, m, s)
            if time(9, 15) <= snap_t <= time(15, 30):
                mf = pf.with_suffix(".meta.json")
                spot = 0.0
                if mf.exists():
                    with open(mf) as f:
                        spot = float(json.load(f).get("spot", 0.0))
                market_snaps.append((snap_t, pf, spot))
        except Exception:
            continue
    return market_snaps


def simulate_session(
    date_dir: Path,
    mode: str = "dynamic",
    target_pts: float = 40.0,
    sl_pts: float = 40.0,
    use_vwap_filter: bool = True,
    use_fixed_window: bool = False,
) -> Optional[MurarkaTrade]:
    """
    Simulates Murarka strategy on a single day's snapshots.
    """
    market_snaps = load_market_snapshots(date_dir)
    if len(market_snaps) < 10:
        return None

    date_str = date_dir.name
    dt_obj = datetime.strptime(date_str, "%Y-%m-%d")
    dow = dt_obj.weekday()
    # DTE proxy: Mon=2, Tue=1 (Expiry), Wed=5, Thu=4, Fri=3
    dte = 5.0 - dow if dow <= 4 else 1.0

    # Load 5m candles if available
    csv_5m = Path(f"data/candles_5m/{date_str}.csv")
    df_5m = pd.read_csv(csv_5m) if csv_5m.exists() else None
    if df_5m is not None:
        df_5m["ts"] = pd.to_datetime(df_5m["timestamp"])

    # Center strike anchored to market open / pre-open
    spot_open = market_snaps[0][2]
    if spot_open <= 0 and df_5m is not None and not df_5m.empty:
        spot_open = float(df_5m.iloc[0]["open"])
    if spot_open <= 0:
        return None

    center_strike = calculate_center_strike(spot_open)
    window_n = 5 if use_fixed_window else get_dynamic_strike_window(dte)

    # Determine candidate entry snapshot
    entry_idx = -1
    entry_snap = None
    entry_bias: Optional[MurarkaBias] = None
    entry_vwap = 0.0

    if mode == "dynamic":
        # Scan from 09:30 onwards for the first valid confirmation
        for idx, (snap_t, pf, spot) in enumerate(market_snaps):
            if snap_t < time(9, 30):
                continue
            df_snap = pd.read_parquet(pf)
            bias_res = evaluate_murarka_bias(df_snap, center_strike, dte, timestamp=datetime.combine(dt_obj.date(), snap_t))
            if not bias_res or bias_res.bias not in ["BULLISH", "BEARISH"] or not bias_res.price_confirmed:
                continue

            # VWAP check
            cur_vwap = 0.0
            if df_5m is not None:
                sub_5m = df_5m[df_5m["ts"].dt.time <= snap_t]
                if not sub_5m.empty:
                    cur_vwap = float(sub_5m.iloc[-1]["vwap"])

            is_bull = (bias_res.bias == "BULLISH")
            vwap_ok = True
            if use_vwap_filter and cur_vwap > 0:
                vwap_ok = (spot > cur_vwap) if is_bull else (spot < cur_vwap)

            if vwap_ok:
                entry_idx = idx
                entry_snap = (snap_t, pf, spot)
                entry_bias = bias_res
                entry_vwap = cur_vwap
                break
    else:
        # Fixed time checkpoint (e.g. 10:00, 09:45, 10:30)
        target_time = datetime.strptime(mode, "%H:%M").time()
        for idx, (snap_t, pf, spot) in enumerate(market_snaps):
            if snap_t >= target_time:
                entry_idx = idx
                entry_snap = (snap_t, pf, spot)
                break

        if entry_snap:
            df_snap = pd.read_parquet(entry_snap[1])
            bias_res = evaluate_murarka_bias(df_snap, center_strike, dte, timestamp=datetime.combine(dt_obj.date(), entry_snap[0]))
            if bias_res and bias_res.bias in ["BULLISH", "BEARISH"] and bias_res.price_confirmed:
                cur_vwap = 0.0
                if df_5m is not None:
                    sub_5m = df_5m[df_5m["ts"].dt.time <= entry_snap[0]]
                    if not sub_5m.empty:
                        cur_vwap = float(sub_5m.iloc[-1]["vwap"])

                is_bull = (bias_res.bias == "BULLISH")
                vwap_ok = True
                if use_vwap_filter and cur_vwap > 0:
                    vwap_ok = (entry_snap[2] > cur_vwap) if is_bull else (entry_snap[2] < cur_vwap)

                if vwap_ok:
                    entry_bias = bias_res
                    entry_vwap = cur_vwap
                else:
                    entry_idx = -1
            else:
                entry_idx = -1

    if entry_idx < 0 or not entry_snap or not entry_bias:
        return None

    # Entry Trade Parameters
    entry_time_str = f"{entry_snap[0]:%H:%M:%S}"
    spot_entry = entry_snap[2]
    is_bull = (entry_bias.bias == "BULLISH")
    atm_strike = center_strike
    opt_col = "ce_ltp" if is_bull else "pe_ltp"

    # Option Contract Entry Price
    df_entry = pd.read_parquet(entry_snap[1])
    r_entry = df_entry[df_entry["strike"] == atm_strike]
    entry_opt_ltp = float(r_entry[opt_col].iloc[0]) if not r_entry.empty else 0.0

    # Simulate forward through remaining snapshots
    post_snaps = market_snaps[entry_idx + 1:]
    max_fav_spot = 0.0
    max_adv_spot = 0.0
    hit_40 = False
    hit_60 = False
    hit_80 = False

    exit_time_str = "15:30:00"
    exit_spot = spot_entry
    exit_opt_ltp = entry_opt_ltp
    exit_reason = "EOD"

    for cur_t, cur_pf, cur_spot in post_snaps:
        if cur_spot <= 0:
            continue

        fav = (cur_spot - spot_entry) if is_bull else (spot_entry - cur_spot)
        adv = (spot_entry - cur_spot) if is_bull else (cur_spot - spot_entry)
        if fav > max_fav_spot:
            max_fav_spot = fav
        if adv > max_adv_spot:
            max_adv_spot = adv

        if fav >= 40.0:
            hit_40 = True
        if fav >= 60.0:
            hit_60 = True
        if fav >= 80.0:
            hit_80 = True

        # Read current option contract LTP
        df_cur = pd.read_parquet(cur_pf)
        r_cur = df_cur[df_cur["strike"] == atm_strike]
        cur_opt_ltp = float(r_cur[opt_col].iloc[0]) if not r_cur.empty else entry_opt_ltp

        # Check Target Hit
        if fav >= target_pts and exit_reason == "EOD":
            exit_reason = f"TARGET_{int(target_pts)}"
            exit_time_str = f"{cur_t:%H:%M:%S}"
            exit_spot = cur_spot
            exit_opt_ltp = cur_opt_ltp
            break

        # Check Stop Loss Hit
        if adv >= sl_pts and exit_reason == "EOD":
            exit_reason = f"SL_{int(sl_pts)}"
            exit_time_str = f"{cur_t:%H:%M:%S}"
            exit_spot = cur_spot
            exit_opt_ltp = cur_opt_ltp
            break

        # Check Invalidation / Expiry Trap
        cur_bias = evaluate_murarka_bias(df_cur, center_strike, dte, timestamp=datetime.combine(dt_obj.date(), cur_t))
        if cur_bias:
            if cur_bias.is_expiry_trap and exit_reason == "EOD":
                exit_reason = "EXPIRY_TRAP_EXIT"
                exit_time_str = f"{cur_t:%H:%M:%S}"
                exit_spot = cur_spot
                exit_opt_ltp = cur_opt_ltp
                break
            elif is_bull and cur_bias.bias == "BEARISH" and exit_reason == "EOD":
                exit_reason = "OI_FLIP_EXIT"
                exit_time_str = f"{cur_t:%H:%M:%S}"
                exit_spot = cur_spot
                exit_opt_ltp = cur_opt_ltp
                break
            elif not is_bull and cur_bias.bias == "BULLISH" and exit_reason == "EOD":
                exit_reason = "OI_FLIP_EXIT"
                exit_time_str = f"{cur_t:%H:%M:%S}"
                exit_spot = cur_spot
                exit_opt_ltp = cur_opt_ltp
                break

    if exit_reason == "EOD" and post_snaps:
        last_t, last_pf, last_spot = post_snaps[-1]
        exit_time_str = f"{last_t:%H:%M:%S}"
        exit_spot = last_spot if last_spot > 0 else spot_entry
        df_last = pd.read_parquet(last_pf)
        r_last = df_last[df_last["strike"] == atm_strike]
        if not r_last.empty:
            exit_opt_ltp = float(r_last[opt_col].iloc[0])

    spot_pnl = (exit_spot - spot_entry) if is_bull else (spot_entry - exit_spot)
    opt_pnl = exit_opt_ltp - entry_opt_ltp
    pnl_rupees = opt_pnl * LOT_SIZE

    return MurarkaTrade(
        date=date_str,
        entry_time=entry_time_str,
        direction=entry_bias.bias,
        center_strike=center_strike,
        atm_strike=atm_strike,
        dte=dte,
        window_n=window_n,
        oi_ratio=entry_bias.oi_ratio,
        call_chg_oi=entry_bias.tot_call_chg_oi,
        put_chg_oi=entry_bias.tot_put_chg_oi,
        entry_spot=spot_entry,
        entry_vwap=entry_vwap,
        entry_opt_ltp=entry_opt_ltp,
        exit_time=exit_time_str,
        exit_spot=exit_spot,
        exit_opt_ltp=exit_opt_ltp,
        exit_reason=exit_reason,
        spot_pnl=round(spot_pnl, 2),
        opt_pnl=round(opt_pnl, 2),
        pnl_rupees=round(pnl_rupees, 2),
        max_fav_spot=round(max_fav_spot, 2),
        max_adv_spot=round(max_adv_spot, 2),
        hit_target_40=hit_40,
        hit_target_60=hit_60,
        hit_target_80=hit_80,
    )


def run_backtest(
    mode: str = "dynamic",
    target_pts: float = 40.0,
    sl_pts: float = 40.0,
    use_vwap_filter: bool = True,
    use_fixed_window: bool = False,
    detailed: bool = False,
    export_path: Optional[str] = None,
) -> pd.DataFrame:
    snap_dir = Path("data/snapshots")
    dates = sorted([d for d in snap_dir.iterdir() if d.is_dir()])

    trades: List[MurarkaTrade] = []
    skipped_days: List[str] = []

    for d in dates:
        t = simulate_session(
            date_dir=d,
            mode=mode,
            target_pts=target_pts,
            sl_pts=sl_pts,
            use_vwap_filter=use_vwap_filter,
            use_fixed_window=use_fixed_window,
        )
        if t:
            trades.append(t)
        else:
            skipped_days.append(d.name)

    df_trades = pd.DataFrame([tr.__dict__ for tr in trades])

    print("=" * 92)
    print(f"       NITIN MURARKA 'LIGHTER SIDE' DYNAMIC-DTE OPTION CHAIN BACKTEST")
    print("=" * 92)
    print(f"Mode: {mode.upper():10s} | Window: {'FIXED +/-5' if use_fixed_window else 'DYNAMIC DTE (+/-5 to +/-1)'}")
    print(f"Target: +{target_pts:.0f} pts | Stop Loss: -{sl_pts:.0f} pts | VWAP Filter: {use_vwap_filter}")
    print(f"Total Sessions Scanned: {len(dates)} | Tradeable Setups Triggered: {len(df_trades)}")

    if df_trades.empty:
        print("No trades triggered with current parameters.")
        return df_trades

    wins = df_trades[df_trades["exit_reason"].str.startswith("TARGET")]
    losses = df_trades[df_trades["exit_reason"].str.startswith("SL")]
    traps = df_trades[df_trades["exit_reason"] == "EXPIRY_TRAP_EXIT"]
    flips = df_trades[df_trades["exit_reason"] == "OI_FLIP_EXIT"]
    eods = df_trades[df_trades["exit_reason"] == "EOD"]

    win_rate = len(wins) / len(df_trades) * 100
    tot_spot_pnl = df_trades["spot_pnl"].sum()
    tot_opt_pnl = df_trades["opt_pnl"].sum()
    tot_rupees = df_trades["pnl_rupees"].sum()

    gross_profit = df_trades[df_trades["opt_pnl"] > 0]["opt_pnl"].sum()
    gross_loss = abs(df_trades[df_trades["opt_pnl"] < 0]["opt_pnl"].sum())
    profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else 99.0

    print("-" * 92)
    print(f"Win Rate:            {win_rate:5.1f}% ({len(wins)} Wins / {len(losses)} Losses / {len(traps)+len(flips)+len(eods)} Other Exits)")
    print(f"Profit Factor:       {profit_factor:5.2f}")
    print(f"Net Spot Points:     {tot_spot_pnl:+6.1f} pts")
    print(f"Net Option Premium:  {tot_opt_pnl:+6.1f} pts")
    print(f"Total PnL (₹/lot):   ₹{tot_rupees:+10,.2f}")
    print(f"Avg Max Favourable:  +{df_trades['max_fav_spot'].mean():5.1f} pts")
    print(f"Avg Max Adverse:     -{df_trades['max_adv_spot'].mean():5.1f} pts")
    print("-" * 92)

    if detailed or True:
        print("\n--- Individual Trade Execution Log ---")
        show_cols = [
            "date", "entry_time", "direction", "dte", "window_n", "oi_ratio",
            "entry_spot", "exit_spot", "exit_reason", "spot_pnl", "opt_pnl", "pnl_rupees"
        ]
        print(df_trades[show_cols].to_string(index=False))

    if export_path:
        out_p = Path(export_path)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        df_trades.to_csv(out_p, index=False)
        print(f"\n[OK] Trades successfully exported to: {out_p.resolve()}")

    return df_trades


def main():
    parser = argparse.ArgumentParser(description="Backtest Nitin Murarka Dynamic-DTE OI Strategy")
    parser.add_argument("--mode", type=str, default="dynamic", choices=["dynamic", "10:00", "09:45", "10:30", "all"], help="Entry timing checkpoint")
    parser.add_argument("--target", type=float, default=40.0, help="Target points on spot (default: 40)")
    parser.add_argument("--sl", type=float, default=40.0, help="Stop loss points on spot (default: 40)")
    parser.add_argument("--no-vwap", action="store_true", help="Disable VWAP alignment filter")
    parser.add_argument("--fixed-window", action="store_true", help="Use fixed +/-5 strike window instead of dynamic DTE")
    parser.add_argument("--detailed", action="store_true", help="Print detailed trade-by-trade log")
    parser.add_argument("--export", action="store_true", help="Export trades to data/murarka_backtest_trades.csv")

    args = parser.parse_args()
    export_file = "data/murarka_backtest_trades.csv" if args.export else None

    if args.mode == "all":
        print("\n=== RUNNING COMPARATIVE BACKTEST ACROSS ALL MODES ===")
        modes = ["dynamic", "10:00", "09:45", "10:30"]
        summary_rows = []
        for m in modes:
            df_m = run_backtest(
                mode=m,
                target_pts=args.target,
                sl_pts=args.sl,
                use_vwap_filter=not args.no_vwap,
                use_fixed_window=False,
                detailed=False,
            )
            if not df_m.empty:
                w = len(df_m[df_m["exit_reason"].str.startswith("TARGET")])
                gp = df_m[df_m["opt_pnl"] > 0]["opt_pnl"].sum()
                gl = abs(df_m[df_m["opt_pnl"] < 0]["opt_pnl"].sum())
                pf = round(gp / gl, 2) if gl > 0 else 99.0
                summary_rows.append({
                    "Mode": m,
                    "Window": "Dynamic DTE",
                    "Trades": len(df_m),
                    "Win Rate": f"{w / len(df_m) * 100:.1f}%",
                    "Profit Factor": pf,
                    "Net Spot": f"{df_m['spot_pnl'].sum():+.1f}",
                    "Net Opt Pts": f"{df_m['opt_pnl'].sum():+.1f}",
                    "PnL (₹)": f"₹{df_m['pnl_rupees'].sum():+,.0f}",
                })
        print("\n=== SUMMARY COMPARISON ===")
        print(pd.DataFrame(summary_rows).to_string(index=False))
    else:
        run_backtest(
            mode=args.mode,
            target_pts=args.target,
            sl_pts=args.sl,
            use_vwap_filter=not args.no_vwap,
            use_fixed_window=args.fixed_window,
            detailed=args.detailed,
            export_path=export_file,
        )


if __name__ == "__main__":
    main()
