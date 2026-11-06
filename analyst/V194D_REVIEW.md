# Port 8510 v1.9.4d — manual-install review

Prepared from claude/v1.9.4b-live-switch @ 615fa68, on chatgpt/v1.9.4d.
The running desk and its user-owned runtime data were NOT replaced.

## Approved changes
- Harish chose recorded data with explicit empty states, and LONG_CE_ITM disabled.
- Removed the embedded allTrades / sandbox30Trades arrays, calibrated date dictionary, date-specific trades, synthesized missing candles, fabricated futures offsets, bullish trend-label defaults, fabricated reconciliation matches, and canned copy/speech P&L.
- Historical views now query actual closed PAPER_ONLY ledger rows and actual market candles. This is LEDGER REVIEW, not a counterfactual strategy backtest. Legacy rows may include earlier testing; they are not certified broker fills or genuine market executions.
- Missing entry-time EMA/ADX/R²/MTF/volume/PCR measurements stay UNKNOWN. They cannot support claimed filter savings or the 128-combination study. Those outputs remain unavailable until proper source measurements exist.
- No old replay JSON, SQLite raw records or archive files are erased. Derived daily views use a separate recorded_daily_views table.
- Legacy BOTH positions are excluded because the old schema mixes simulated and real legs; this view does not certify independent executions.
- LONG_CE_ITM is false in the shipped source. The supplied apply_v194d_config.py patches only that approved selection and missing existing guard defaults. It preserves broker IP, partial-exit preferences, other strike selections and live_validation_status. It migrates the earlier standard confirmation string to LIVE; it does not arm trading.

## Safety and testing
Live order/chaser, charges, configure_real_money, stop_live_trading, emergency kill and 15:20 EOD functions were compared with 615fa68 and are byte-identical in AST source. No live order was placed. NO_GO / dry-run safeguards stay unchanged. Nothing was reverted to a 100-point strike grid.

Tests now construct temporary configs and ledgers before API app collection. Dhan credentials are fake/blank and external HTTP is blocked. The EMA-radar singleton is redirected too. A test guard refuses writes to the shipped config rather than silently restoring them. No existing test assertion was loosened.

Baseline comparison used the SAME private-state fixture, local instrument master and database snapshots on both revisions. This differs from the handoff's container-only baseline (21 failed / 342 passed / 12 skipped).

Current baseline run: 7 failed, 388 passed, 13 skipped, 4 warnings in 181.53s (0:03:01)

Full-suite checkpoint: 11 failed, 397 passed, 12 skipped, 4 warnings in 193.33s (0:03:13)

Final focused run: 19 passed, 4 warnings in 18.68s

The full-suite checkpoint preceded three additional focused report/isolation tests. Those final tests passed in the focused run; the full suite was not represented as green. This is NOT a clean full-suite release.

### Four newly failing legacy expectations
- test_3_tab7_pull_todays_data_api
- test_5_multifactor_trend_lab_includes_05nov
- test_7_automated_post_market_eod_summary_and_whatsapp_digest
- test_nov4_3strategies_replay_sequence

These expect a canned four-trade sequence, synthetic completion markers, a bullish dated trend, or an invented one-trade EOD summary. They now receive recorded rows or UNKNOWN / empty states. The assertions were retained; genuine fixture-based replacements remain maintenance work.

### All full-suite failures
- test_part2_directional_bias_replay_and_backtest
- test_date_range_backtest_simulation
- test_nov4_3strategies_replay_sequence
- test_nov3_clean_bullish_trades_replay
- test_backtest_timestamp_logging_and_homework_reconciliation
- test_1_replay_dropdown_dynamic
- test_2_auto_ingest_after_1600_ist
- test_3_tab7_pull_todays_data_api
- test_4_backtester_includes_05nov_and_reconciles
- test_5_multifactor_trend_lab_includes_05nov
- test_7_automated_post_market_eod_summary_and_whatsapp_digest

### All skips
SKIPPED [1] analyst/test_stockmojo_synergy_8510.py:40: Parses the Stock Mojo rendered HTML table. That scraper is decommissioned; Dhan OI supplies these figures now (test_v19 test_06 covers the replacement).
SKIPPED [1] analyst/test_stockmojo_synergy_8510.py:65: Exercises the Stock Mojo dual-entity scrape buffers, which no longer exist.
SKIPPED [1] analyst/test_stockmojo_synergy_8510.py:120: Gates on Stock Mojo payloads. The Dhan OI synergy gate is covered by test_v19 test_03 and test_06.
SKIPPED [1] analyst/test_stockmojo_synergy_8510.py:256: Asserts 200 on Stock Mojo read endpoints that now answer 410 by design. The 410 contract is asserted in test_7 below.
SKIPPED [1] analyst/test_stockmojo_synergy_8510.py:398: Calls the Stock Mojo multi-strike endpoint directly. Decommissioned.
SKIPPED [1] analyst/test_super_trail_8510.py:688: Retired external-provider parsers; v1.9 Dhan schema tests replace them
SKIPPED [6] analyst/test_v192_trend_capture_8510.py:102: Part 1 evidence gate failed: hybrid trailing engine intentionally NOT BUILT; no passing implementation asserted

The baseline has one additional skip because its archive is not a Git worktree. test_time_series_bridge.py was excluded exactly as the handoff instructed; its pre-existing collection problem was not fixed in this task.

### Independent checks
[PASS] All 11 locked core modules verified intact against LOCKED_MANIFEST.json.

..........                                                               [100%]
10 passed in 1.83s

ICT/SMC PORT PARITY  (Python port vs the locked TypeScript)
==================================================================
fixtures     : ict_smc_parity_fixtures_8510.json.gz
timestamps   : 9059

field                         compared   agreement %
------------------------------------------------------------------
stage                             9059         100.0
stageBadge                        9059         100.0
actionTone                        9059         100.0
suppressed                        9059         100.0
convictionPct                     9059         100.0
isActionable                      9059         100.0
isHighConviction                  9059         100.0
isSynergy                         9059         100.0
isScissor                         9059         100.0
isPullbackToSupply                9059         100.0
isPullbackToDemand                9059         100.0
isShortCoveringReversal           9059         100.0
spotChange15m                     9059         100.0
radar                             9059         100.0
corridorPct                       9059         100.0
stateKey                          9059         100.0
stateChange                       9059         100.0

No mismatches on any compared field.

## ICT SMC pre-registered research
The earlier handoff said the study had not run. It has now been run on this desk's copied data. 1,286 raw prefixes; chain data in 19 of the latest 40 sessions. There were 1,589 unique directional group evaluations with at least one estimated priced horizon. These are not 1,589 actual fills. Entire missing sessions are not repaired by 100% pricing within the covered subset.

32 paired contrasts were evaluated inside the conservatively retained 128-hypothesis family. Bonferroni alpha 0.000390625. Largest |t| = 0.828. With 19 session blocks the critical |t| is 4.344, not the asymptotic 3.54 quoted in the handoff. Zero corrected results passed. No held-out confirmation or positive execution verdict is claimed. Constant-delta .59 and .67 exits are two sensitivity scenarios, not observed option exits or a calibrated per-trade delta model. Current master lot was read dynamically; historical lot changes remain unverified.

Research is separate from the recorded paper ledger. A read-only report link appears in Historical Date Replay. Raw scenario observations and CSV remain in the external outputs folder; they are not committed as executions. The evidence JSON contains summary results and source hashes only.

- State Change / Actionable / High Conviction / Burst: ADVISORY, corrected evidence did not pass.
- Active Corridor / Pullback Radar: context only; no standalone entry backtest invented.
- Writer Stack-up: NOT BUILT, exact source rule not identified.
- Matrix diverging bars: display port only; recorded provenance not certified, no fabricated outcomes.

No locked file changed. No new Stock Mojo integration. Its retired routes remain retired. The old JWT history still requires rotation by its owner; this task did not rewrite Git history or access that account.

## Spread evidence
The inherited slippage script reported 0 dry-run depth rows: no measured bid/ask-spread evidence yet. Its 'realised execution' header includes stored paper fills in this legacy ledger; it must not be read as verified live-broker slippage. Neither slippage assumption was changed.

## Delivery
The ZIP includes full code files needed to move the earlier 952bf32 state to this branch, including Claude's inherited continuation files. It excludes the wholesale runtime config, every SQLite database, replay_data, .env and dependencies. Use the supplied narrow config patch after stopping only the 8510 daemon. Existing runtime preferences are preserved. Do not overwrite them with a repository-default config.

No automated process stop, port-wide kill, real-money arm, order, deployment, or protected-module edit occurred. The legacy live-screen placeholder defaults outside the historical/EOD work were not redesigned in this task.
