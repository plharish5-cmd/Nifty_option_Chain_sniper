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
- **`agreement_local` Arbitrary Linear Scaling**: Threshold is `dom_min * (15/375) = 1,000 contracts` — a linear time-scaling of a full-day threshold, which assumes OI accrues evenly through the session. It does not. The threshold is arbitrary and the column is acknowledged as noisy in the first 30 minutes. Logged only; no weight anywhere.

## Known Open Bugs
- **LIMITATION (Spread vs Single-Leg Flow)**: `spread = dPE - dCE` cannot distinguish put-side BUILDING from call-side UNWINDING; both widen the spread identically. Evidence: 2026-09-01 ATM 24050, 11:00 -> 13:00, dCE fell +316,640 -> +68,389 (-248,251) while dPE fell +618,631 -> +601,844 (-16,787). Spread widened 301,991 -> 533,455 and PCR rose 2.08 -> 5.41 purely from call unwinding; puts did not build. State read `PE_DOMINANT` / `FLAT`. `flow=UNWINDING` requires BOTH legs falling, so a single-leg collapse reads `FLAT` and is invisible in the panel. Not fixed. Candidate fix (do NOT implement now): emit per-leg direction (`ce_direction`, `pe_direction` over the lookback) alongside dominance, so the panel can say "PE dominant via CE unwind" vs "PE dominant via PE build". Flow is a 9-minute local read, not a session read — the panel should state its lookback window so `FLAT` is not misread as "nothing happened today".
- **Repricing-vs-LTP Diagnostic Inconsistency**: Near-ATM reported 46.17 pts mean error (39.4% of LTP) where parity holds within a few points (contradicts 7.3 pt forward gap at 4 DTE). Deep ITM reported 768 pts at 5.71% LTP, implying an impossible 13,450 LTP. Caused by percentage averaging across heterogeneous denominators or comparing model price against the wrong leg's LTP. *Does not affect OI timeline computation.*
- **`strike_select.py` Theoretical ROC Sort Flaw**: ROC formula $\frac{\Delta}{\text{Premium}}$ prioritizes far-OTM strikes due to near-zero denominators. (Live scanner is unaffected as `analytics._pick_strike` strictly selects from `[ATM, ATM-1 ITM, ATM-2 ITM]`).
- **Duplicate `POLL_SECONDS` in `config.py`**: Line 21 defines `POLL_SECONDS = 60`; line 402 defines `POLL_SECONDS = 180`. Line 21 takes precedence during imports, but redundancy requires cleanup.
- **24500 PE Sub-Intrinsic Print Hypothesis**: Stale-quote explanation is UNVERIFIED because NSE API does not publish per-strike trade timestamps.
- **VPIN Module Residuals**: Incomplete status; four original calculation/boundary bugs remain unverified since focus shifted to option chain pipeline.

## Closed as Unanswerable
- **14:14 Multi-Strike Discontinuity (2026-09-11)**: Occurred during the uncaptured 14:00–14:30 interval; cannot be validated from existing single-expiry data.
- **Multi-Expiry External Reconciliation (OptionTick 2026-09-11)**: Historical snapshots captured only nearest expiry; magnitude divergence cannot be reconciled without live multi-expiry captures.

## Quadrant Agreement Columns Definition
- **`agreement_cum` (Naive OI vs Cumulative Buyer Quadrant, `oi_timeline.py:680-689`)**: Compares full-day net OI direction against cumulative LTP quadrant. When day spread $\ge +25,000$ contracts (naive Put Writing), it checks if `pe_quad_cum == 'long_buildup'` (Put Buying). If yes $\rightarrow$ `DISAGREE`; else `AGREE`. Tests against full-day threshold (`dom_min = 25,000`).
- **`agreement_local` (Local Naive Flow vs Local Buyer Quadrant, `oi_timeline.py:691-698`)**: Compares recent 15m delta-OI direction against local 15m LTP quadrant. Tests against time-scaled threshold (`local_dom_thresh = 25,000 * 15/375 = 1,000`). If 15m `local_spread >= 1,000` and `pe_quad_local == 'long_buildup'` $\rightarrow$ `DISAGREE`; else `AGREE`.
- **`cum_vs_local_agreement` (Cumulative Flow vs Local Flow Alignment, `oi_timeline.py:700-706`)**: Directly tests if current 15m quadrant matches cumulative quadrant (`pe_quad_cum == pe_quad_local` for PE-dominant; both match for balanced).
- **Row 2 of `2026-08-21.csv` Explanation**: At 09:21, spread is $+2,539$. In `agreement_cum`, $2,539 < 25,000$ so the cumulative threshold is not met $\rightarrow$ `AGREE`. In `agreement_local`, $2,539 \ge 1,000$ (scaled threshold) and PE LTP rose ($+4.0$), flagging local Put Buying $\rightarrow$ `DISAGREE`. In `cum_vs_local_agreement`, `pe_quad_cum` and `pe_quad_local` are both `long_buildup`, so they match $\rightarrow$ `AGREE`.
- **Assessment**: The columns compare different pairs and different thresholds: `agreement_cum` and `agreement_local` are intent disambiguators (Buyer vs Writer at macro vs micro scale), while `cum_vs_local_agreement` is a momentum-continuation flag. None is undefined, but `agreement_local` is the most sensitive to early morning noise due to the scaled 1,000-contract threshold.

## Next Priorities (In Strict Order)
1. **Live Session Capture**: Let the persistent poller run undisturbed to accumulate clean, multi-expiry market-hour sessions.
2. **Permutation Control**: At $\approx 20$ clean sessions, run the shuffled-baseline permutation control on `crossovers.csv` (identical to the cross-disagreement benchmark).
3. **Scoring Integration**: Wire crossover or flow metrics into `bias_score` ONLY if they pass permutation testing.
