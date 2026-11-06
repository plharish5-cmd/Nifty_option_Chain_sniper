# Port 8510 v1.9.3 — recording and observations

## Verdict

**BUILT:** permanent recorder, coverage monitor, EMA persistence/export, radar tracker/export. **ADVISORY:** stop-width experiment. **NOT BUILT:** any new execution rule, trailing rule, or live stop-width change. The recorder starts forward; no historical option ticks or radar signals were manufactured.

Runtime verification: new 8510 coverage endpoint responds; see runtime_coverage.json

**The 30-point stop has the largest estimated mean on the matched priced subset (₹186.78 vs ₹99.96 at 20), but that is 50 candidates over 18 of 40 sessions, using assumed sign-specific delta. It does not establish an option edge or a winner on all 40 sessions. No live setting was changed.** CHOP remains negative at every width and gets worse as the stop widens. Worst-day scenario loss increases from ₹2,867.06 at 20 to ₹5,729.31 at 50. The EXPIRY subset is concentrated: at stop 20, one day contributes 60% of its positive gross profit; at 30, 42.86%; at 40/50, 42.01%. Treat these subsets as unproven.

## 1. Permanent observations

- Database: `data/terminal8510/recording.sqlite3`, separate from the execution ledger. Its tables are `chain_snap`, `chain_coverage`, `contract_tick`, `ema_proximity_history`, `radar_tier_signals`, plus tracking/state metadata. This does not replace or silently merge the existing market database.
- Every successful Dhan chain refresh is enqueued from the existing OI poll; there is no extra chain poller. Raw receipts are retained. Coverage counts occupied three-minute slots (maximum 125), not repeated polls as independent market updates.
- One read-only quote batch each minute for tracked contracts. First signal observations also request the named contract promptly. Held contracts, live signal contracts, radar contracts and ATM±100 CE/PE resolve through the existing instrument master. Unresolvable held identities are exposed in coverage.
- `contract_tick.ts` is the receipt-minute bucket. **`observed_at` is the actual quote receipt timestamp**, and `exchange_ts` carries last trade time if supplied. This is an observed quote, not an execution price or proof of a trade at a minute boundary.
- A bounded queue and a dedicated worker isolate network, database and CSV work. Overflow/write errors log and fail open. HTTP quote timeout/backoff affects only this worker. Missing values never become zero or delta-derived premiums.
- Signal tracking continues to 15:20; held contracts continue through the session and closed contracts through exit+30 minutes when later. No endless overnight polling for an unclosed old row. Radar outcomes freeze at 15:20, never overnight.
- Retention guard preserves spot/FUT/FUT2 candles and original chain snapshots. It takes effect when the existing collector process next reloads its Python module; this task did not stop/restart port 8600. New 8510 observations are in their own unpruned database regardless.
- `scripts/archive_volume.py --report --date 2026-11-06` reads unified source/forward coverage without writing. Archives now include `data/archive/recording/legacy_chain`, `forward_chain`, `contract_tick`, one atomic `.csv.gz` per date. Current-day option backups refresh; past files are idempotent unless `--force`.

## 2. Coverage before and after installation — 06 Nov 2026

| Series | Before | After | Full-session coverage | Largest gap |
| --- | --- | --- | --- | --- |
| Spot | 375/375 | 375/375 | 100.00% | 0.0 min at 15:30 |
| FUT | 375/375 | 375/375 | 100.00% | 0.0 min at 15:30 |
| FUT2 | 375/375 | 375/375 | 100.00% | 0.0 min at 15:30 |
| Existing chain: occupied 3-minute slots | 100/125 | 100/125 | 80.00% | 6.0 min at 13:18 |

The original chain has **141 raw refresh timestamps**, occupying 100 of 125 three-minute slots; 141/125 is not a valid coverage percentage. Both raw count and occupied-slot definitions are explicit.

Forward recorder today: **0 chain slots; 0 contract ticks; 0 tracked identities.** Installation was after market close. Zero new history is expected and is not relabelled as a full day's capture. Live UI percentages use elapsed expected slots; after close they use the full-session denominator. Amber below 95%, red below 80%. The old source and new source are displayed separately.

## 3. EMA matrix persistence and exports

- Exact requested 18-column schema, keyed by date/time/timeframe. A close adds 1m/15m/1H context observations. No forming candle is persisted; incomplete or unavailable source bars are STALE. Disabled paper/kill-switch context is PAUSED when data is otherwise fresh. Higher-timeframe values are latest closed source bars, sampled each minute, not interpolated candles.
- `gap_points = ma4 − ema8`; `gap_vs_atr = (ma4 − ema8) / timeframe ATR`. `price_vs_ema8_points` and `price_vs_ma4_points` use that closed bar's price. Live spot is separately labelled.
- INSERT OR IGNORE prevents overwrite/restart duplicates. SQLite is authoritative; append-only daily CSVs reconcile an interrupted append from existing rows. Startup loads yesterday+today; the UI has date/timeframe selection and older/newer pages.
- Local CSV path: `analyst/proximity_data/YYYY-MM-DD.csv`. It is created on the first forward close, not with fabricated rows after hours.
- CSV streaming uses `fetchmany`; Excel uses write-only sheets and a disk spool, then chunked HTTP download. Both include all statuses and reject ranges over 90 calendar days. Excel sheets are 1m, 15m, 1H.
- Endpoints: `/api/ema_proximity/export?from=YYYY-MM-DD&to=YYYY-MM-DD&format=csv|xlsx`, `/api/recording/history`, `/api/recording/coverage`.

Actual forward EMA rows today: **0**. There are no three live rows to present tonight. The following is a **TEST-ONLY export-format sample**, produced from the isolated snapshot with an explicitly paused simulated collector. It was never installed as live history:

```csv
date,time_ist,timeframe,nifty_spot_price,close_price,ma4,ema8,gap_points,gap_vs_atr,price_vs_ema8_points,price_vs_ma4_points,proximity_status,crossover_status,support_resistance,flip_count_12bar,trend,signal,status
2026-11-06,11:01:00,15m,24575.95,24589.45,24615.02,24603.87,11.15,0.2014,-14.42,-25.57,FAR,NO_CROSS,NEUTRAL,3,BULLISH,None,PAUSED
2026-11-06,11:01:00,1H,24575.95,24589.45,24581.53,24570.36,11.17,0.1129,19.09,7.92,FAR,NO_CROSS,NEUTRAL,2,BULLISH,None,PAUSED
2026-11-06,11:01:00,1m,24575.95,24575.95,24585.78,24593.23,-7.45,-0.503,-17.28,-9.83,TOUCHING,NO_CROSS,SUPPORT,1,BEARISH,None,PAUSED
```

## 4. Radar tracker — honest outcomes

- One condition observation per tier/direction/minute avoids LTP-driven duplicate multiplication; the requested full unique key and monotonic sequence remain stored. Several tiers can each have a row. Restart checks the persisted minute before emitting again.
- Fire time preserves signal price, independently observed spot, contract and available premium. **Risk is measured from observed spot to the protective stop**, so mixing signal price and spot cannot inflate max/risk. The supplied illustrative 24,293 vs 25,840 example is deliberately not reproduced.
- Numeric micro-stop metadata reuses the existing structural-stop formula, including on crossover-warning rows; the original displayed warning, strategy conditions and execution stop policy remain unchanged.
- Walk only complete minutes that begin after the fire observation. Never use the earlier portion of a partially elapsed signal minute. Same-minute target/SL is AMBIGUOUS; missing spot intervals also make unresolved sequencing ambiguous. A stop-hit bar cannot supply a new favourable maximum with unknown ordering.
- A later stop preserves prior MFE and its time. Complete-horizon unresolved targets at 15:20 are NO; missing final data leaves final move unknown and unresolved target ordering ambiguous. Expired NO rows are not counted as stop-first. Pending and expired counts remain visible beside percentages.
- Premium columns use exact contract minute receipts. Any gap blanks the cumulative premium columns; no spot-to-premium conversion is used here.
- CSV/Excel export: `/api/radar_tier/export?from=YYYY-MM-DD&to=YYYY-MM-DD&format=csv|xlsx`.

Actual new tier observations today: **0** (after-hours installation; no retrospective signal insertion).

| Tier | n | Avg max | Avg risk | Max/risk | T15 before SL | SL first | Ambiguous |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 0 | — | — | — | — | — | — |
| 2 | 0 | — | — | — | — | — | — |
| 3 | 0 | — | — | — | — | — | — |


> Max move is the maximum by construction and cannot support a trading rule on its own. "T15 before SL" is the tradeable number. Ambiguous rows are minutes where target and stop both sat inside one bar's range and the order is unknowable. These are observations, not an edge: n is small and no cost model is applied.

## 5. Stop-width experiment — all registered widths, no tuning

Frozen 321 original Stage 2/3 candidates; 40 sessions (2026-09-09 to 2026-11-06). The cohort is committed in `analyst/stop_width_cohort_8510.json`. All four widths use the same candidates, a fixed existing 40-spot-point Stage 2/3 target, closed 15m 8EMA violation and 15:20 cutoff. The prior descriptive MFE horizon did not exit at a fixed target; this trade-outcome experiment explicitly adds that fixed target before any run. Partial exits, new trailing rules and live execution are not simulated. Missing minutes abstain; opening gaps use the observed open for adverse stop scenarios. A bar spanning target and stop is ambiguous unless its open already establishes an adverse stop gap.

**The n column has two denominators.** Spot statistics use all resolved candidates; cost results use the identical 50 exact-entry-premium candidates across widths. Those are **estimated**, not observed exit premiums: requested delta 0.59 for winning spot moves / 0.67 for losing ones, multiplied by the dynamically read current-master lot (65). The existing statutory tariff is called for each premium scenario. Historical lot-size changes, bid/ask, fill quality and slippage are not reconstructed. No ₹400 flat cost or 0.60/0.50 delta is introduced. This is not an executed P&L ledger.

| Stop | Segment | n spot / cost | Spot WR % | Avg win pts | Avg loss pts | Spot payoff | Spot BE WR % | Est. net ₹/trade | Worst scenario day ₹ |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 20 | ALL | 321 / 50 | 41.43 | 36.05 | 18.95 | 1.90 | 34.46 | 99.96 | -2,867.06 |
| 20 | TREND | 140 / 29 | 50.71 | 37.86 | 19.13 | 1.98 | 33.56 | 414.39 | -1,918.02 |
| 20 | CHOP | 181 / 21 | 34.25 | 33.96 | 18.85 | 1.80 | 35.70 | -334.24 | -2,867.06 |
| 20 | EXPIRY | 38 / 12 | 42.11 | 38.43 | 18.69 | 2.06 | 32.72 | 54.61 | -2,328.95 |
| 30 | ALL | 321 / 50 | 49.84 | 36.08 | 24.82 | 1.45 | 40.75 | 186.78 | -4,171.04 |
| 30 | TREND | 140 / 29 | 64.29 | 37.59 | 24.79 | 1.52 | 39.74 | 677.20 | 47.70 |
| 30 | CHOP | 181 / 21 | 38.67 | 34.15 | 24.83 | 1.38 | 42.10 | -490.46 | -4,171.04 |
| 30 | EXPIRY | 38 / 12 | 55.26 | 38.80 | 23.71 | 1.64 | 37.93 | 273.56 | -4,067.58 |
| 40 | ALL | 321 / 50 | 53.27 | 35.77 | 27.60 | 1.30 | 43.56 | 184.57 | -5,475.04 |
| 40 | TREND | 140 / 29 | 69.29 | 37.13 | 26.28 | 1.41 | 41.45 | 767.63 | -756.09 |
| 40 | CHOP | 181 / 21 | 40.88 | 34.00 | 28.14 | 1.21 | 45.28 | -620.60 | -5,475.04 |
| 40 | EXPIRY | 38 / 12 | 60.53 | 37.41 | 25.00 | 1.50 | 40.06 | 255.36 | -3,851.32 |
| 50 | ALL | 321 / 50 | 53.58 | 35.80 | 29.84 | 1.20 | 45.46 | 163.40 | -5,729.31 |
| 50 | TREND | 140 / 29 | 70.00 | 37.16 | 24.49 | 1.52 | 39.72 | 834.93 | -1,368.96 |
| 50 | CHOP | 181 / 21 | 40.88 | 34.00 | 31.93 | 1.06 | 48.43 | -763.94 | -5,729.31 |
| 50 | EXPIRY | 38 / 12 | 60.53 | 37.41 | 25.15 | 1.49 | 40.20 | 249.75 | -5,155.33 |


All 321 spot outcomes resolved per width: **0 ambiguous, 0 missing** in this particular dataset; ambiguity handling is tested, not omitted. The EXPIRY row contains only dates verified by stored chain/master expiries; absence from that subset is not proof of a non-expiry day. TREND/CHOP are hindsight session-body labels used only for reporting, not fire-time decisions.

Cost-adjusted break-even on the matched priced subset (distinct from spot break-even above):

| Stop | Cost n | Est. net WR % | Avg net win ₹ | Avg net loss ₹ | Net payoff | Net BE WR % | Clears BE | Best-day positive gross % |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 20 | 50 | 44.00 | 1,396.58 | 918.81 | 1.52 | 39.68 | YES | 14.10 |
| 30 | 50 | 54.00 | 1,406.60 | 1,245.17 | 1.13 | 46.96 | YES | 11.42 |
| 40 | 50 | 60.00 | 1,323.83 | 1,524.32 | 0.87 | 53.52 | YES | 10.89 |
| 50 | 50 | 60.00 | 1,323.83 | 1,577.24 | 0.84 | 54.37 | YES | 10.89 |


All four achieved spot WRs exceed their own empirical spot payoff break-even in this sample. All four matched **estimated net** WRs exceed their own estimated fee-adjusted break-even. Higher stops beat 20's estimated mean on the matched ALL subset, but cannot establish the requested financial win across all 40 sessions with 22 sessions unpriced. **No width is promoted.**

Best-day concentration is the largest day's sum of positive gross candidate contributions divided by all positive gross contributions. Worst-day is the sum of candidate scenario nets, including overlaps; it is not an executable one-position portfolio drawdown. These are four requested variants on already-used history, with no fresh holdout or claim of statistical validation.

Files: `stop_width/stop_width_summary.csv`, `stop_width/stop_width_trades.csv`, `stop_width/stop_width.json` next to this report.

## 6. Verification and guardrails

- Full isolated suite, completed in three non-overlapping batches (294 + 10 + 18): **322 passed / 12 skipped / 0 failed**, four existing FastAPI lifespan deprecation warnings. Test source/data/config copies were isolated; outbound networking was blocked. Earlier regressions caught an SQL quoting typo and a portable-archiver import, both corrected before signoff. A NumPy JSON scalar issue in the research summary was also corrected and the full CLI completed successfully. The new launcher harness initially raced its asynchronous browser helper; a bounded wait fixed it (4/4 pass). A long monolithic rerun was interrupted with exit 143 and is not counted as a pass; the final three batches cover the entire suite without omission.
- Focused recorder suite: **42 passed**, including all 20 requested assertions plus queue bounds, partial fire minutes, missing final data, restart price changes, master identity/exit tails, last-write accuracy, source-slot coverage, current-spot risk and no overnight poll.
- ASGI smoke: coverage/history, CSV, Excel, tier CSV return 200; over-90-day request returns 400. Startup/scanner deliberately not invoked. Quote fetching was disabled in this isolated smoke; next-session real quote receipts are still to be observed.
- EMA parity against pre-tag: original fields/summary identical on 376 1m, 26 15m and 8 1H source rows. Partial/forming rows are not persisted as fresh recording data.
- Launcher regression cases use fake HTTP/browser helpers: correct Algo identity opens, Streamlit identity refuses, Harish/Wife after-hours launchers use their non-conflicting ports. The harness explicitly waits for the asynchronous browser opener.
- HTML JS syntax and new DOM IDs verified. UI preview used a read-only mock server, not the broker. Existing seven tabs, sound toggle and order controls remain.
- `verify_locks.py`: **[PASS] All 11 locked core modules verified intact against LOCKED_MANIFEST.json.**
- New code has zero retired-provider integrations and no order API operations. Literal legacy provider names still exist in old config keys, deprecated routes and skipped tests: the repository-wide literal count is **not zero**, and those historical references were not deleted to manufacture that claim.
- Required config is PAPER_ONLY and algo-only ON after the one-time activation. Continuation remains OFF / ADVISORY_ONLY. Existing runtime partial exits are preserved for Harish's decision, not silently reverted. The commit retains their original false baseline; the runtime preference remains an unstaged difference.
- Dynamic lot in the study fails closed on missing/ambiguous master data; no lot constant was added. Existing execution code was not refactored.
- **No live orders placed, modified or cancelled by this work.** Production market data was read-only for testing; source gaps stay gaps.

Every skipped test and its reason:

```text
SKIPPED [1] analyst/test_stockmojo_synergy_8510.py:40: Parses the Stock Mojo rendered HTML table. That scraper is decommissioned; Dhan OI supplies these figures now (test_v19 test_06 covers the replacement).
SKIPPED [1] analyst/test_stockmojo_synergy_8510.py:65: Exercises the Stock Mojo dual-entity scrape buffers, which no longer exist.
SKIPPED [1] analyst/test_stockmojo_synergy_8510.py:120: Gates on Stock Mojo payloads. The Dhan OI synergy gate is covered by test_v19 test_03 and test_06.
SKIPPED [1] analyst/test_stockmojo_synergy_8510.py:256: Asserts 200 on Stock Mojo read endpoints that now answer 410 by design. The 410 contract is asserted in test_7 below.
SKIPPED [1] analyst/test_stockmojo_synergy_8510.py:398: Calls the Stock Mojo multi-strike endpoint directly. Decommissioned.
SKIPPED [1] analyst/test_super_trail_8510.py:687: Retired external-provider parsers; v1.9 Dhan schema tests replace them
SKIPPED [6] analyst/test_v192_trend_capture_8510.py:102: Part 1 evidence gate failed: hybrid trailing engine intentionally NOT BUILT; no passing implementation asserted
```

## 7. Opening and activation

Open `http://127.0.0.1:8510/` after restarting the 8510 terminal. Tab 1 has **Recording health**; **View Matrix Table** opens the existing EMA matrix containing saved-history controls, downloads, radar outcomes and tier summary.

The old daemon was observed overwriting its in-memory config after file installation. The user completed the one-time activation and started the correct Quant Terminal; its health and export routes were then verified. The one-time procedure was:

```bash
cd "/home/harish/Documents/Swathi K N/1 Trading/option chain/OC"
python3 "/home/harish/Documents/Codex/2026-10-27/yes-absolutely-introducing-cumulative-volume-delta/outputs/v193_recording/activate_v193.py"
.venv/bin/python engine_8510_live.py
```

The port conflict came from the legacy root `desk_launch.sh`, which used 8510/8511 for temporary after-hours Paper Desks. Only Harish’s conflicting temporary port moves to 8520; the wife’s temporary port remains 8511 and morning Paper Desks remain 8502/8503. `scripts/desk_launch.sh` now validates Quant Terminal JSON health before opening an occupied 8510 and refuses a wrong app without killing anything. A bound-but-not-ready health endpoint is retried without launching a duplicate scanner. The user stopped the specifically identified old listener (PID 188848) and started the Quant Terminal. No port-wide killing was used. This one-time activation preserves the partial-exit preference while adding recording config and restoring the requested algo-only guard. Live tape collection can only be verified in a subsequent open session; it cannot be proven by an after-hours test. The running app shows its existing Dhan token-expiry warning: renew credentials before the next session. No credentials were changed by this work.

## 8. Audit checkpoints

Branch: `chatgpt/v1.9.3-recording`. Base/pre tag: `pre-chatgpt-v1.9.3` at `0348576`. Initial post tag: `post-chatgpt-v1.9.3`. Final audit tag: `post-chatgpt-v1.9.3-final`, including a small follow-up keeping the unrelated wife launcher on its original port. Existing tags are preserved, never moved. Only named source/review paths are staged; no replay JSON, test database, .env, credentials, runtime ledger or format-sample data is included.

Run `git diff --stat pre-chatgpt-v1.9.3..post-chatgpt-v1.9.3-final` (the original `post-chatgpt-v1.9.3` checkpoint is also preserved). Final exact diff output is in `git_diff_stat.txt` beside this report.
