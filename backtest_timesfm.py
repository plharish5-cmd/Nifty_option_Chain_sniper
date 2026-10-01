"""
backtest_timesfm.py — Rigorous Historical Walk-Forward Backtest of Google TimesFM 3.0
on all available Dhan 1-minute candle data (64 trading days, June 2026 to Sep 2026).

Evaluates:
1. Forecast Accuracy & Quantile Calibration (q10-q90 Coverage, Directional Hit Rate, MAE).
2. Systematic Trading Performance (Win Rate, Total Points, Profit Factor, Max Drawdown, CTC Rate).
"""
from __future__ import annotations

import argparse
import datetime
from pathlib import Path
import numpy as np
import pandas as pd

ROOT_DIR = Path(__file__).parent
CANDLE_DIR = ROOT_DIR / "data" / "candles"


def load_all_candles() -> pd.DataFrame:
    """Load and resample all available Dhan 1-minute candle files."""
    files = sorted(CANDLE_DIR.glob("20*.csv"))
    if not files:
        raise FileNotFoundError(f"No candle files found in {CANDLE_DIR}")

    frames = []
    for f in files:
        try:
            d = pd.read_csv(f)
            if not d.empty and "timestamp" in d and "close" in d:
                frames.append(d)
        except Exception:
            continue

    combined = pd.concat(frames, ignore_index=True)
    combined["timestamp"] = pd.to_datetime(combined["timestamp"])
    combined = (
        combined.dropna(subset=["timestamp", "close"])
        .sort_values("timestamp")
        .drop_duplicates("timestamp")
        .reset_index(drop=True)
    )
    return combined.set_index("timestamp")


def resample_bars(df_indexed: pd.DataFrame, timeframe: str = "15m") -> pd.DataFrame:
    """Resample to 5m or 15m candles aligned with NSE session (09:15)."""
    tf = timeframe.lower()
    rule = "15min" if "15m" in tf else "5min"
    res = df_indexed.resample(rule, offset="15min").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum"
    }).dropna()
    # Filter out hours outside market session (09:15 to 15:30)
    times = res.index.time
    mask = (times >= datetime.time(9, 15)) & (times <= datetime.time(15, 30))
    return res[mask]


def run_walk_forward_backtest(
    timeframe: str = "15m",
    horizon: int = 20,
    stride: int = 2,           # evaluate every 2 candles to simulate active trader check-ins
    use_neural: bool = True,
    max_trades_day: int = 2
) -> dict:
    print(f"\n=======================================================")
    print(f"🚀 INITIATING TIMESFM 3.0 WALK-FORWARD BACKTEST ({timeframe.upper()})")
    print(f"=======================================================")
    
    df_raw = load_all_candles()
    bars = resample_bars(df_raw, timeframe)
    print(f"Loaded {len(bars)} {timeframe} candles spanning {bars.index[0].date()} to {bars.index[-1].date()}.")
    
    # Initialize TimesFM 3.0 if requested
    forecaster = None
    if use_neural:
        try:
            from timesfm3 import TimesFM3Evaluator, ModelConfig
            print("Initializing TimesFM 3.0 model on CPU...")
            config = ModelConfig(
                checkpoint_path="google/timesfm-3.0-pytorch",
                per_core_batch_size=32,
                device="cpu"
            )
            forecaster = TimesFM3Evaluator(config)
            print("TimesFM 3.0 neural evaluator ready.")
        except Exception as e:
            print(f"[!] Neural model unavailable ({e}). Falling back to statistical engine.")
            forecaster = None

    # Identify test points (skip first 150 bars for context warm-up)
    warmup = 150
    indices = [i for i in range(warmup, len(bars) - horizon, stride)]
    print(f"Total evaluation checkpoints: {len(indices)}")

    # Batch compute forecasts
    contexts = []
    checkpoint_meta = []
    
    for idx in indices:
        ts = bars.index[idx]
        # Only evaluate during primary trading hours (09:30 to 14:15)
        if ts.time() < datetime.time(9, 30) or ts.time() > datetime.time(14, 15):
            continue
        ctx = bars["close"].values.astype(np.float32)[max(0, idx - 256):idx]
        contexts.append(ctx)
        checkpoint_meta.append(idx)

    print(f"Filtered to {len(checkpoint_meta)} active trading checkpoints. Generating predictions...")
    
    predictions = []
    batch_size = 32
    t0 = datetime.datetime.now()

    if forecaster is not None:
        for b_start in range(0, len(contexts), batch_size):
            b_end = min(b_start + batch_size, len(contexts))
            batch_ctx = contexts[b_start:b_end]
            outs = list(forecaster.predict_batch(
                batch_ctx, 
                horizon=horizon, 
                return_quantiles=True, 
                use_symmetric_averaging=False
            ))
            for out in outs:
                predictions.append({
                    "median": out.quantiles[:, 4],
                    "q10": out.quantiles[:, 0],
                    "q90": out.quantiles[:, 8],
                })
            pct = (b_end / len(contexts)) * 100
            print(f"  Processed {b_end}/{len(contexts)} forecasts ({pct:.1f}%)...")
    else:
        # Statistical Volatility Cone fallback
        for ctx in contexts:
            returns = np.diff(np.log(ctx[-100:]))
            mu, sigma = float(np.mean(returns)), float(np.std(returns))
            steps = np.arange(1, horizon + 1)
            drift = steps * mu
            diffusion = sigma * np.sqrt(steps)
            last_p = ctx[-1]
            med = last_p * np.exp(drift)
            q10 = last_p * np.exp(drift - 1.28 * diffusion)
            q90 = last_p * np.exp(drift + 1.28 * diffusion)
            predictions.append({"median": med, "q10": q10, "q90": q90})

    t_elapsed = (datetime.datetime.now() - t0).total_seconds()
    print(f"Predictions completed in {t_elapsed:.1f}s ({len(predictions) / max(1, t_elapsed):.1f} forecasts/sec).")

    # ─────────────────────────────────────────────────────────────────────────
    # 1. EVALUATE QUANTILE CALIBRATION & ACCURACY
    # ─────────────────────────────────────────────────────────────────────────
    total_horizon_steps = 0
    in_cone_steps = 0
    directional_correct_t1 = 0
    directional_total_t1 = 0
    directional_correct_end = 0
    directional_total_end = 0
    abs_errors_t1 = []
    abs_errors_end = []

    for idx, pred in zip(checkpoint_meta, predictions):
        current_price = bars["close"].iloc[idx]
        actual_future_closes = bars["close"].iloc[idx + 1 : idx + 1 + horizon].values
        
        # Quantile coverage
        actual_len = len(actual_future_closes)
        q10 = pred["q10"][:actual_len]
        q90 = pred["q90"][:actual_len]
        median = pred["median"][:actual_len]

        in_cone = (actual_future_closes >= q10) & (actual_future_closes <= q90)
        in_cone_steps += np.sum(in_cone)
        total_horizon_steps += actual_len

        # Directional accuracy at T+1
        if actual_len >= 1:
            pred_dir_t1 = np.sign(median[0] - current_price)
            actual_dir_t1 = np.sign(actual_future_closes[0] - current_price)
            if pred_dir_t1 != 0:
                directional_total_t1 += 1
                if pred_dir_t1 == actual_dir_t1:
                    directional_correct_t1 += 1
            abs_errors_t1.append(abs(actual_future_closes[0] - median[0]))

        # Directional accuracy at End of Horizon
        if actual_len >= horizon:
            pred_dir_end = np.sign(median[-1] - current_price)
            actual_dir_end = np.sign(actual_future_closes[-1] - current_price)
            if pred_dir_end != 0:
                directional_total_end += 1
                if pred_dir_end == actual_dir_end:
                    directional_correct_end += 1
            abs_errors_end.append(abs(actual_future_closes[-1] - median[-1]))

    cone_coverage_pct = (in_cone_steps / max(1, total_horizon_steps)) * 100
    dir_acc_t1 = (directional_correct_t1 / max(1, directional_total_t1)) * 100
    dir_acc_end = (directional_correct_end / max(1, directional_total_end)) * 100
    mae_t1 = np.mean(abs_errors_t1) if abs_errors_t1 else 0.0
    mae_end = np.mean(abs_errors_end) if abs_errors_end else 0.0

    # ─────────────────────────────────────────────────────────────────────────
    # 2. SIMULATE SYSTEMATIC TRADING SIGNALS
    # ─────────────────────────────────────────────────────────────────────────
    trades = []
    daily_trades_count = {}
    active_trade = None
    min_drift = 8.0 if "15m" in timeframe else 4.0

    for idx, pred in zip(checkpoint_meta, predictions):
        cur_ts = bars.index[idx]
        cur_date = cur_ts.date()
        cur_p = bars["close"].iloc[idx]
        
        # Check daily trade cap
        taken_today = daily_trades_count.get(cur_date, 0)
        if taken_today >= max_trades_day:
            continue

        # Skip if in an open trade on the same session
        if active_trade and active_trade["date"] == cur_date and not active_trade["closed"]:
            continue

        median_drift = pred["median"][-1] - cur_p
        
        # Signal Generation
        if median_drift > min_drift:
            direction = "LONG (CE)"
            sl_dist = 22.0 if "15m" in timeframe else 15.0
            sl = cur_p - sl_dist
            t1 = cur_p + abs(median_drift)
            t2 = pred["q90"][-1]
        elif median_drift < -min_drift:
            direction = "SHORT (PE)"
            sl_dist = 22.0 if "15m" in timeframe else 15.0
            sl = cur_p + sl_dist
            t1 = cur_p - abs(median_drift)
            t2 = pred["q10"][-1]
        else:
            continue  # Noise / Compression

        # Simulate execution across subsequent candles until exit
        daily_trades_count[cur_date] = taken_today + 1
        trade = {
            "date": cur_date,
            "entry_ts": cur_ts,
            "direction": direction,
            "entry": cur_p,
            "sl": sl,
            "t1": t1,
            "t2": t2,
            "exit_ts": None,
            "exit_p": None,
            "pnl": 0.0,
            "exit_reason": "OPEN",
            "closed": False,
            "ctc_triggered": False
        }

        # Track future bars within the same session
        future_idx = idx + 1
        while future_idx < len(bars):
            f_ts = bars.index[future_idx]
            if f_ts.date() != cur_date:
                # EOD square-off
                trade["exit_ts"] = bars.index[future_idx - 1]
                trade["exit_p"] = bars["close"].iloc[future_idx - 1]
                trade["exit_reason"] = "EOD Square-Off (15:25)"
                trade["closed"] = True
                break

            f_high = bars["high"].iloc[future_idx]
            f_low = bars["low"].iloc[future_idx]
            f_close = bars["close"].iloc[future_idx]

            # Long rules
            if direction == "LONG (CE)":
                # Check CTC trigger (+20 pts favorable excursion)
                if f_high >= trade["entry"] + 20.0 and not trade["ctc_triggered"]:
                    trade["ctc_triggered"] = True
                    trade["sl"] = trade["entry"]  # Cost-to-cost lock

                # Stop Loss hit
                if f_low <= trade["sl"]:
                    trade["exit_ts"] = f_ts
                    trade["exit_p"] = trade["sl"]
                    trade["exit_reason"] = "Cost-to-Cost (CTC)" if trade["ctc_triggered"] else "Stop Loss"
                    trade["closed"] = True
                    break
                # Target 1 hit
                if f_high >= trade["t1"]:
                    trade["exit_ts"] = f_ts
                    trade["exit_p"] = trade["t1"]
                    trade["exit_reason"] = "Target 1 (Median)"
                    trade["closed"] = True
                    break
                # Target 2 hit
                if f_high >= trade["t2"]:
                    trade["exit_ts"] = f_ts
                    trade["exit_p"] = trade["t2"]
                    trade["exit_reason"] = "Target 2 (Cone Cap)"
                    trade["closed"] = True
                    break

            # Short rules
            elif direction == "SHORT (PE)":
                # Check CTC trigger (+20 pts favorable excursion)
                if f_low <= trade["entry"] - 20.0 and not trade["ctc_triggered"]:
                    trade["ctc_triggered"] = True
                    trade["sl"] = trade["entry"]  # Cost-to-cost lock

                # Stop Loss hit
                if f_high >= trade["sl"]:
                    trade["exit_ts"] = f_ts
                    trade["exit_p"] = trade["sl"]
                    trade["exit_reason"] = "Cost-to-Cost (CTC)" if trade["ctc_triggered"] else "Stop Loss"
                    trade["closed"] = True
                    break
                # Target 1 hit
                if f_low <= trade["t1"]:
                    trade["exit_ts"] = f_ts
                    trade["exit_p"] = trade["t1"]
                    trade["exit_reason"] = "Target 1 (Median)"
                    trade["closed"] = True
                    break
                # Target 2 hit
                if f_low <= trade["t2"]:
                    trade["exit_ts"] = f_ts
                    trade["exit_p"] = trade["t2"]
                    trade["exit_reason"] = "Target 2 (Cone Floor)"
                    trade["closed"] = True
                    break

            # Mandatory 15:15 session cutoff
            if f_ts.time() >= datetime.time(15, 15):
                trade["exit_ts"] = f_ts
                trade["exit_p"] = f_close
                trade["exit_reason"] = "Session Cutoff (15:15)"
                trade["closed"] = True
                break

            future_idx += 1

        # Calculate PnL
        if trade["closed"] and trade["exit_p"] is not None:
            if direction == "LONG (CE)":
                trade["pnl"] = trade["exit_p"] - trade["entry"]
            else:
                trade["pnl"] = trade["entry"] - trade["exit_p"]
            trades.append(trade)

    df_trades = pd.DataFrame(trades)

    # ─────────────────────────────────────────────────────────────────────────
    # 3. PERFORMANCE STATISTICS
    # ─────────────────────────────────────────────────────────────────────────
    total_trades = len(df_trades)
    if total_trades > 0:
        wins = df_trades[df_trades["pnl"] > 1.0]
        losses = df_trades[df_trades["pnl"] < -1.0]
        breakevens = df_trades[(df_trades["pnl"] >= -1.0) & (df_trades["pnl"] <= 1.0)]

        win_rate = (len(wins) / total_trades) * 100
        loss_rate = (len(losses) / total_trades) * 100
        be_rate = (len(breakevens) / total_trades) * 100

        total_pts = df_trades["pnl"].sum()
        gross_profit = wins["pnl"].sum() if not wins.empty else 0.0
        gross_loss = abs(losses["pnl"].sum()) if not losses.empty else 1.0
        profit_factor = gross_profit / max(1.0, gross_loss)
        avg_win = wins["pnl"].mean() if not wins.empty else 0.0
        avg_loss = abs(losses["pnl"].mean()) if not losses.empty else 0.0
        expectancy = total_pts / total_trades

        # Drawdown calculation
        equity_curve = df_trades["pnl"].cumsum()
        peak = equity_curve.cummax()
        drawdown = peak - equity_curve
        max_dd = drawdown.max()
    else:
        win_rate = loss_rate = be_rate = total_pts = profit_factor = avg_win = avg_loss = expectancy = max_dd = 0.0

    report = {
        "timeframe": timeframe,
        "total_bars": len(bars),
        "total_sessions": len(sorted(set(bars.index.date))),
        "date_range": f"{bars.index[0].date()} to {bars.index[-1].date()}",
        "checkpoints_evaluated": len(checkpoint_meta),
        "engine": "TimesFM 3.0 (Google Research Foundation Model)" if forecaster else "Statistical Volatility Cone",
        # Calibration
        "cone_coverage_pct": cone_coverage_pct,
        "dir_acc_t1": dir_acc_t1,
        "dir_acc_end": dir_acc_end,
        "mae_t1": mae_t1,
        "mae_end": mae_end,
        # Trading
        "total_trades": total_trades,
        "wins": len(wins) if total_trades else 0,
        "losses": len(losses) if total_trades else 0,
        "breakevens": len(breakevens) if total_trades else 0,
        "win_rate": win_rate,
        "loss_rate": loss_rate,
        "be_rate": be_rate,
        "total_pts": total_pts,
        "profit_factor": profit_factor,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "expectancy": expectancy,
        "max_dd": max_dd,
        "df_trades": df_trades
    }

    _print_report(report)
    return report


def _print_report(r: dict):
    print("\n" + "═" * 65)
    print(f"📊 TIMESFM 3.0 HISTORICAL BACKTEST AUDIT REPORT ({r['timeframe'].upper()})")
    print("═" * 65)
    print(f"Dataset Scope:       {r['date_range']} ({r['total_sessions']} Trading Sessions)")
    print(f"Total Candles:       {r['total_bars']:,} {r['timeframe']} candles")
    print(f"Forecast Engine:     {r['engine']}")
    print(f"Evaluations Run:     {r['checkpoints_evaluated']:,} checkpoints")
    print("─" * 65)
    print("🎯 1. FORECAST ACCURACY & QUANTILE CALIBRATION:")
    print(f"  • q10–q90 Cone Coverage:    {r['cone_coverage_pct']:.1f}% (Theory: 80.0%)")
    print(f"  • Directional Hit Rate T+1: {r['dir_acc_t1']:.1f}%")
    print(f"  • Directional Hit Rate End: {r['dir_acc_end']:.1f}%")
    print(f"  • Mean Absolute Error T+1:  {r['mae_t1']:.1f} pts")
    print(f"  • Mean Absolute Error End:  {r['mae_end']:.1f} pts")
    print("─" * 65)
    print("💰 2. SYSTEMATIC TRADING PERFORMANCE:")
    print(f"  • Total Trades Taken:       {r['total_trades']}")
    print(f"  • Win Rate (Excl. BE):      {r['win_rate']:.1f}% ({r['wins']} Wins)")
    print(f"  • Loss Rate:                {r['loss_rate']:.1f}% ({r['losses']} Losses)")
    print(f"  • Cost-to-Cost (CTC) Saves: {r['be_rate']:.1f}% ({r['breakevens']} Breakevens)")
    print(f"  • Total Net P&L:            {r['total_pts']:+,.1f} NIFTY Pts")
    print(f"  • Profit Factor:            {r['profit_factor']:.2f}")
    print(f"  • Average Win / Loss:       +{r['avg_win']:.1f} pts / -{r['avg_loss']:.1f} pts")
    print(f"  • Expectancy per Trade:     {r['expectancy']:+.1f} pts")
    print(f"  • Max Drawdown:             -{r['max_dd']:.1f} pts")
    print("═" * 65 + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backtest TimesFM 3.0 on Dhan Candles")
    parser.add_argument("--tf", default="15m", choices=["15m", "5m"], help="Timeframe (15m or 5m)")
    parser.add_argument("--horizon", "-H", type=int, default=20, help="Forecast horizon bars")
    parser.add_argument("--stride", "-s", type=int, default=2, help="Bar evaluation stride")
    parser.add_argument("--stat-only", action="store_true", help="Use statistical engine only")
    args = parser.parse_args()

    run_walk_forward_backtest(
        timeframe=args.tf,
        horizon=args.horizon,
        stride=args.stride,
        use_neural=not args.stat_only
    )
