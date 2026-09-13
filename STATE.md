# NIFTY Scanner & AI Analyst — Project State

## Working and Verified
- **Poller Architecture**: Persistent `NSEClient`, systemd user unit enabled with lingering, `systemd-inhibit` (lid-close/sleep immunity), zero live `raise` on data conditions, and deterministic audit trail in `data/scheduler_heartbeat.jsonl`.
- **OI Timeline Engine**: Pure-Python computation in `oi_timeline.py` with 12 unit tests passing; resamples 1m/cadence snapshots into 3m bins, tracks per-strike cumulative flows (dCE, dPE, spread, strike PCR), and computes deterministic missed opening OI.
- **Single-IV Parity Greeks**: OTM-sourced implied volatility with put-call parity derivation ($\Delta_{\text{put}} = \Delta_{\text{call}} - e^{-qT}$, $\Gamma_{\text{put}} = \Gamma_{\text{call}}$); eliminates independent IV skew divergence; 12 unit tests passing.
- **Historical Quality Gate**: Successfully validates 4 gate-passing sessions (`2026-08-21`, `2026-08-31`, `2026-09-01`, `2026-09-02`) with $\ge 86\%$ coverage, lateness $\le 3.0\text{m}$, and continuous intraday flow.
- **Multi-Expiry Retention Pruner**: Prunes `snapshots_multi/` older than 90 days on startup and post-market OHLC; hardcoded path check refuses execution if directed at primary `snapshots/`.

## Built but Unvalidated (No Forward-Return Proof)
- **Dominance & Flow States**: Labels (`PE_DOMINANT`, `CE_DOMINANT`, `UNWINDING`, `EOD_UNWIND`, `FLAT`) categorize flow but have zero forward-return statistical validation; **none feeds the bias score**.
- **Crossover Detection**: Event tagging on spread zero-crossings is purely descriptive and uncalibrated against forward returns; **does not feed the bias score**.
- **Quadrant Disambiguation (Cumulative vs Local 15m)**: Dual-window classification of writing vs buying; currently displays high disagreement rate; unverified predictive power.
- **VPIN & Order Flow Metrics**: Toxicity and volume-synchronized probability of toxicity are unvalidated on live NIFTY tick feeds and inactive in live scoring.

## Known Open Bugs
- **Repricing-vs-LTP Diagnostic Inconsistency**: Near-ATM reported 46.17 pts mean error (39.4% of LTP) where parity holds within a few points (contradicts 7.3 pt forward gap at 4 DTE). Deep ITM reported 768 pts at 5.71% LTP, implying an impossible 13,450 LTP. Caused by percentage averaging across heterogeneous denominators or comparing model price against the wrong leg's LTP. *Does not affect OI timeline computation.*
- **`strike_select.py` Theoretical ROC Sort Flaw**: ROC formula $\frac{\Delta}{\text{Premium}}$ prioritizes far-OTM strikes due to near-zero denominators. (Live scanner is unaffected as `analytics._pick_strike` strictly selects from `[ATM, ATM-1 ITM, ATM-2 ITM]`).
- **Duplicate `POLL_SECONDS` in `config.py`**: Line 21 defines `POLL_SECONDS = 60`; line 402 defines `POLL_SECONDS = 180`. Line 21 takes precedence during imports, but redundancy requires cleanup.
- **24500 PE Sub-Intrinsic Print Hypothesis**: Stale-quote explanation is UNVERIFIED because NSE API does not publish per-strike trade timestamps.
- **VPIN Module Residuals**: Incomplete status; four original calculation/boundary bugs remain unverified since focus shifted to option chain pipeline.

## Closed as Unanswerable
- **14:14 Multi-Strike Discontinuity (2026-09-11)**: Occurred during the uncaptured 14:00–14:30 interval; cannot be validated from existing single-expiry data.
- **Multi-Expiry External Reconciliation (OptionTick 2026-09-11)**: Historical snapshots captured only nearest expiry; magnitude divergence cannot be reconciled without live multi-expiry captures.

## Next Priorities (In Strict Order)
1. **Live Session Capture**: Let the persistent poller run undisturbed to accumulate clean, multi-expiry market-hour sessions.
2. **Permutation Control**: At $\approx 20$ clean sessions, run the shuffled-baseline permutation control on `crossovers.csv` (identical to the cross-disagreement benchmark).
3. **Scoring Integration**: Wire crossover or flow metrics into `bias_score` ONLY if they pass permutation testing.
