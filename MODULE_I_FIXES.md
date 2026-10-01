# Module I — Fixes Before Paper-Trade Clock Starts
# Source: Kimi analysis, Sep 2026

## FIX 1: Absorption definition too loose
- Current: any 15k one-sided volume + ≤2 tick pause, anywhere
- Fix: must occur within 3 ticks of a persisted wall
  AND ≥2 pulses at same level within absorb_window
- Expected: 137 events → 15-25 events

## FIX 2: Replay was direction-blind
- Re-run split by: long/short direction AND signal type
  (wall-hold / absorption / break)
- Report win rate per cell, +5m and +15m
- Gate = ≥60% in dominant-direction cell on ≥3 of 5 days

## FIX 3: Need 5 days minimum, 3 regimes
- One trend-up day
- One trend-down day  
- One chop day
- Report per-direction, per-regime, per-signal-type

## FIX 4: Remove GRASIM from acceptance
- Stock DOM thresholds incompatible with NIFTY options
- NIFTY option recordings only

## DO NOT start 2-week paper-trade clock until all 4 fixes done
## and multi-day replay passes the revised gate
