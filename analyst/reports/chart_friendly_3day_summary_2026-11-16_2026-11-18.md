# Chart-friendly 3-day backtest (2026-11-16 .. 2026-11-18)

Generated: 2026-11-18 20:14:30 IST

## Assumptions (matches desk chart-friendly patch)

- `retouch_requires_away` = **OFF** (Prototype)
- Prototype + Reversal OI filters = **log_only** (not replayed in synth backtest)
- Re-entry same side + after SL = **ON**
- Dhan OI synergy gates, radar, five-factor confluence = **OFF**
- Simulation: **1m NIFTY spot** walk; **spot points**; **₹2 cost/trade** deducted for net
- **8600 board OI filters are not replayed** in this engine path

Market DB: `/home/harish/Documents/Swathi K N/1 Trading/option chain/OC/ict_smc_app/backend/data/ict_smc.sqlite3`

## Totals

- Trades: **29**
- Gross spot pts: **-237.1**
- Net after 2.0 pts/trade: **-295.1**
- Win rate: **34.5%**
- Wins / losses: **10** / **19**

## By day

- **2026-11-16**: 9 trades, net **-78.2** pts (W/L 4/5)
- **2026-11-17**: 9 trades, net **-175.5** pts (W/L 3/6)
- **2026-11-18**: 11 trades, net **-41.3** pts (W/L 3/8)

## By lane

- **8 EMA Prototype**: 16 trades, net **-108.8** pts
- **Stage 2 8-EMA (Long)**: 5 trades, net **-176.8** pts
- **Stage 3 8-EMA (Short)**: 3 trades, net **+0.5** pts
- **8 EMA Reversal**: 4 trades, net **-48.0** pts
- **1 HR Flagship**: 1 trades, net **+38.0** pts

## Trade log

Full CSV: `/home/harish/Documents/Swathi K N/1 Trading/option chain/OC/analyst/reports/chart_friendly_3day_trades_2026-11-16_2026-11-18.csv`

| Entry (IST) | Exit (IST) | Lane | Dir | Entry spot | Exit spot | Gross | Net | Reason |
|---|---|---|---|---:|---:|---:|---:|---|
| 2026-11-16 10:33 | 2026-11-16 10:46 | Stage 2 8-EMA (Long) | LONG | 25244.25 | 25204.25 | -40.0 | -42.0 | SPOT_SL_HIT |
| 2026-11-16 10:46 | 2026-11-16 12:00 | 8 EMA Prototype | SHORT | 25218.95 | 25258.95 | -40.0 | -42.0 | SPOT_SL_HIT |
| 2026-11-16 12:08 | 2026-11-16 13:30 | 8 EMA Prototype | LONG | 25262.15 | 25249.00 | -13.2 | -15.2 | OPPOSITE_8EMA_CLOSE |
| 2026-11-16 12:31 | 2026-11-16 13:33 | 8 EMA Reversal | LONG | 25253.80 | 25233.80 | -20.0 | -22.0 | SPOT_SL_HIT |
| 2026-11-16 12:31 | 2026-11-16 13:53 | Stage 2 8-EMA (Long) | LONG | 25253.80 | 25213.80 | -40.0 | -42.0 | SPOT_SL_HIT |
| 2026-11-16 13:30 | 2026-11-16 13:53 | 8 EMA Prototype | SHORT | 25249.00 | 25209.00 | +40.0 | +38.0 | SPOT_TARGET_HIT |
| 2026-11-16 13:45 | 2026-11-16 13:45 | 1 HR Flagship | SHORT | 25245.95 | 25205.95 | +40.0 | +38.0 | TARGET_HIT |
| 2026-11-16 14:31 | 2026-11-16 15:20 | 8 EMA Prototype | SHORT | 25226.50 | 25220.05 | +6.5 | +4.5 | EOD_SQUARE_OFF_15:20 |
| 2026-11-16 14:31 | 2026-11-16 15:20 | Stage 3 8-EMA (Short) | SHORT | 25226.50 | 25220.05 | +6.5 | +4.5 | EOD_SQUARE_OFF_15:20 |
| 2026-11-17 12:40 | 2026-11-17 13:04 | 8 EMA Prototype | LONG | 25466.45 | 25426.45 | -40.0 | -42.0 | SPOT_SL_HIT |
| 2026-11-17 12:44 | 2026-11-17 13:05 | Stage 2 8-EMA (Long) | LONG | 25457.95 | 25417.95 | -40.0 | -42.0 | SPOT_SL_HIT |
| 2026-11-17 13:40 | 2026-11-17 13:50 | 8 EMA Reversal | SHORT | 25408.75 | 25428.75 | -20.0 | -22.0 | SPOT_SL_HIT |
| 2026-11-17 13:40 | 2026-11-17 13:54 | 8 EMA Prototype | SHORT | 25408.75 | 25448.75 | -40.0 | -42.0 | SPOT_SL_HIT |
| 2026-11-17 13:40 | 2026-11-17 13:54 | Stage 3 8-EMA (Short) | SHORT | 25408.75 | 25448.75 | -40.0 | -42.0 | SPOT_SL_HIT |
| 2026-11-17 14:18 | 2026-11-17 14:23 | 8 EMA Reversal | LONG | 25439.90 | 25459.90 | +20.0 | +18.0 | SPOT_TARGET_HIT |
| 2026-11-17 14:18 | 2026-11-17 14:45 | 8 EMA Prototype | LONG | 25439.90 | 25442.40 | +2.5 | +0.5 | OPPOSITE_8EMA_CLOSE |
| 2026-11-17 14:35 | 2026-11-17 15:01 | Stage 2 8-EMA (Long) | LONG | 25446.05 | 25406.05 | -40.0 | -42.0 | SPOT_SL_HIT |
| 2026-11-17 14:45 | 2026-11-17 15:01 | 8 EMA Prototype | SHORT | 25442.40 | 25402.40 | +40.0 | +38.0 | SPOT_TARGET_HIT |
| 2026-11-18 09:30 | 2026-11-18 09:32 | 8 EMA Prototype | SHORT | 25293.95 | 25333.95 | -40.0 | -42.0 | SPOT_SL_HIT |
| 2026-11-18 11:20 | 2026-11-18 11:30 | 8 EMA Prototype | SHORT | 25267.30 | 25278.85 | -11.6 | -13.6 | OPPOSITE_8EMA_CLOSE |
| 2026-11-18 11:20 | 2026-11-18 11:55 | Stage 3 8-EMA (Short) | SHORT | 25267.30 | 25227.30 | +40.0 | +38.0 | SPOT_TARGET_HIT |
| 2026-11-18 11:31 | 2026-11-18 11:45 | 8 EMA Prototype | LONG | 25264.35 | 25258.50 | -5.8 | -7.8 | OPPOSITE_8EMA_CLOSE |
| 2026-11-18 11:46 | 2026-11-18 12:30 | 8 EMA Prototype | SHORT | 25240.45 | 25200.45 | +40.0 | +38.0 | SPOT_TARGET_HIT |
| 2026-11-18 13:52 | 2026-11-18 13:57 | 8 EMA Prototype | SHORT | 25198.05 | 25238.05 | -40.0 | -42.0 | SPOT_SL_HIT |
| 2026-11-18 14:10 | 2026-11-18 14:19 | 8 EMA Prototype | LONG | 25221.65 | 25261.65 | +40.0 | +38.0 | SPOT_TARGET_HIT |
| 2026-11-18 14:39 | 2026-11-18 15:04 | 8 EMA Reversal | LONG | 25227.20 | 25207.20 | -20.0 | -22.0 | SPOT_SL_HIT |
| 2026-11-18 14:39 | 2026-11-18 15:00 | 8 EMA Prototype | LONG | 25227.20 | 25216.55 | -10.7 | -12.7 | OPPOSITE_8EMA_CLOSE |
| 2026-11-18 14:39 | 2026-11-18 15:20 | Stage 2 8-EMA (Long) | LONG | 25227.20 | 25220.45 | -6.8 | -8.8 | EOD_SQUARE_OFF_15:20 |
| 2026-11-18 15:01 | 2026-11-18 15:20 | 8 EMA Prototype | SHORT | 25215.90 | 25220.45 | -4.5 | -6.5 | EOD_SQUARE_OFF_15:20 |
