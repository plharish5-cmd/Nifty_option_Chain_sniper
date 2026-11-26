# Independent review — 99e22ca — 26 November 2026

**Verdict: FAIL. No approval to run real money Friday with this candidate's writer-bias BLOCK settings.** This is a software-readiness verdict, not a prediction of trading profitability. Review only: no production source/config changes, merge, restart, enablement or broker orders.

Candidate: `claude/v266-spot-feed-reliability` at `99e22ca87b`; comparison base `a54c3c2` (`origin/reconcile/v2.6.0-consolidated`). Review artifacts live on `chatgpt/v266-independent-review`. The inspected active OC checkout was a54c3c2; that alone does not establish the running daemon's loaded build.

## Numeric verification

| Check | Result |
|---|---|
| Supplied spot reliability + gate-toggle + decision audit + writer-bias suites | 27 passed, 0 failed |
| Independent acceptance probes | **26 passed, 12 failed / 38** |
| Explicit STALE observations, all 3 lanes × all 4 signals × 2 age states | 24 passed / 24 (included above) |
| Real loop WS reconnect and unchanged 208-second stale entry guard | 2 passed / 2 (included above) |
| Existing stale/no-flood audit probes | 2 passed, 78 deselected |
| Protected-module locks | 11/11 PASS |
| TypeScript/Python parity | 9,059 rows × 17 fields, 100%, zero mismatches |
| Focused baseline diff: board, decision audit, feed freshness | base 26 passed / 1 failed; candidate 26 passed / 1 failed; **0 new regressions on these 27 tests** |

The shared failure is `test_dual_tables_expose_positional_and_intraday_columns`, with missing StockMojo SQLite table in the isolated fixture. This is inherited, not a newly introduced regression. The supplied tests passing does not override the independent failing acceptance probes. Do not compare these fixtures to older agent totals as though they were identical suites.

Full baseline comparison status is appended at the end; no blanket full-suite zero-regression claim is made. Independent probes are named `independent_v266_review_probes.py`, run explicitly, and are separate from ordinary branch regression totals. Their failures intentionally expose unmet acceptance requirements, not modified production logic. They are not skipped or converted into expected passes.

## Part 1 — What the 26-Nov CSV actually proves

Source: `/home/harish/Downloads/trade_decision_audit (6).csv`, 487 rows. SHA256: `5ef116def6b23cbe943a2094036b51da8424703fee6b3b4482185c5cb84b430e`.

For **Prototype only**, 09:15 ≤ time < 11:30: **105 rows**: 67 STATE, 32 WAITING, 2 CANDIDATE, 2 EVALUATING, 2 ORDER_REJECTED; no ORDER_ACCEPTED row.

- 57 STATE rows say waiting for dynamic-EMA re-touch. All 57 have `armed=True`, `retouch_requires_away=False`, directional bias BOTH, `arm_mode=cross_only`. Arm directions: 45 LONG, 12 SHORT. These records do not support unarmed/away-required as the reason for those specific waits.
- Every one of these 57 settings snapshots has **`e8_refresh_seconds=30`**, not 3. The worker uses this live setting (`terminal8510_backend.py:5596`). A fully fresh feed can still miss a brief ±3-point touch between widely spaced checks; the touch routine also requires current spot within tolerance (`ema8_prototype_8510.py:464`). This is an additional plausible hindrance, not proof that any particular marked trade was executable.
- 11:26:48 candidate → **11:26:52 rejected: Spot feed STALE (6.8s), REST fallback failed**.
- 11:27:42 candidate → **11:27:48 rejected: DH-905 Invalid IP**.
- 32 WAITING rows explicitly report stale/missing spot, with recorded ages including 5–48 seconds and n/a.

`terminal8510_backend.py:5600–5627` reads cached spot, checks freshness, saves arm state and continues without evaluating the touch when stale. This directly prevents candidate generation, so paper and real entry paths are both bypassed during those evaluations. It is stronger/more precise than merely saying the frozen numerical price misses the tolerance.

**Sufficiency verdict: not proved.** The 57 re-touch rows do not contain contemporaneous spot in evaluator_state, and there is no pointwise tick/dynamic-EMA trace in this CSV proving every claimed chart touch. The records establish a real stale-price pause and two actual rejection causes; they cannot establish that stale spot alone explains every missed opportunity. They also do not justify excluding cadence or other gates outside those specific rows. Other lanes have ORDER_ACCEPTED audit records before 11:30; those labels alone are not proof of real broker fills.

The wording `feed stale — not blocking` is misleading for spot here: the function actually pauses touch evaluation. Stale **OI context** must not veto a trade; stale **execution price** still correctly prevents an entry. These are different contracts.

## Part 2 — Reconnect works; the full cure and rate-limit safety do not pass

### Verified passes

An actual mocked `Feed.run()` loop reconnects after three consecutive receive timeouts, rather than merely checking source text. The finally block closes the old socket and the outer loop waits the configured retry interval. The independent 208-second stale-spot test confirms entry recovery still returns failure when REST fails. The guard has not been removed.

The new background task starts from engine startup and calls the existing REST helper when market-open and spot age exceeds three seconds. This improves the architecture but is not proof of continuous live quote delivery or capturing every brief touch.

### Blocking findings

1. **Concurrent request race — reproduced two outbound requests at once.** `dhan_spot_rest_fallback_8510.py:80–86` releases the lock before awaiting HTTP; the reservation timestamp is only updated after success (`:116–119`). Two simultaneous calls both pass. The concurrency acceptance probe fails.
2. **429 failures do not consume throttle/backoff — reproduced two immediate failed requests.** Non-200 returns occur before `_last_http_mono` is updated. The keepalive can therefore retry into rate limiting without a reserved attempt interval.
3. **An unrelated spot response renews the global option cache — reproduced.** An option received at monotonic 100, then spot at 110, then option recovery at 110.1 returns the old option price without a new quote. `_cached_prices.update` retains old symbols while `_cached_at_mono` is global. This is an execution-data defect, not merely a display issue.
4. **No account-wide request arbiter.** `DhanBroker.get_marketfeed_ltp` has a separate cache/backoff and directly sends requests (`terminal8510_backend.py:2488–2491`), bypassing the fallback helper's throttle. Paper quotes use another `/marketfeed/quote` path. Therefore this candidate cannot demonstrate non-starvation or an account-wide rate budget. Option-chain traffic is a different endpoint; do not assume it is the identical LTP quota without provider evidence.

Items 1–3 already exist in the base helper; the new continuous consumer amplifies their exposure. They are **not falsely labelled as newly introduced helper regressions**. Nevertheless they block accepting the new worker as a proven safe cure. The three-second refresh threshold is not a guarantee: scheduling and REST latency/timeouts can still take the quote beyond the five-second entry limit.

Receive-timeout reconnect detects a fully silent socket; incoming other-instrument frames can reset the timeout counter while NIFTY itself is stale. A live-market end-to-end check remains necessary.

## Part 3 — BLOCK correctness fails

### Critical new writer-bias computation/integration defects

- **Direction is reversed.** `writer_bias_live_reads_8510.py:50–54` maps BULLISH → PE and BEARISH → CE, while the gate interprets CE as agreeing with a bullish entry. Fresh OI uses another mapping which maps BULLISH → CE, so the sources can contradict each other due to code, not market evidence. Independent expected-CE test fails.
- **Put weights collapse to 0.01.** `:71` clamps the signed negative put delta into a positive `[0.01,0.99]` interval before the caller takes its absolute value. A near-ATM put probe returns **0.01**, rather than an absolute delta above 0.3. This mechanically underweights puts. Also, this path estimates Greeks from stored premiums; it does not consume the requested observed Dhan per-strike delta. Missing/invalid model inputs default to 0.5 instead of unavailable.
- **The requested poll baseline is computed but not used for headline/fresh regimes.** `:204–215` calculates `poll_agg` then selects `sess_agg['writing']`; fresh uses `sess_agg['fresh']`. In the independent three-snapshot example only PE adds OI with falling price in the last poll (bullish), yet headline reports **BEARISH** because earlier CE activity dominates the since-open comparison. Poll labels in the detail do not fix the regime's baseline.
- **Late first observation substitutes for 09:15.** `base_t` is simply the first recorded session snapshot. The test starting at 09:30 reports session baseline **09:30:00** and calculates the leg described as since 09:15. Missing opening history must be unavailable, not silently relabelled.
- **Slope and divergence do not enforce eligibility.** `evaluate_lane_gating` uses freshness and direction only; slope/divergence are informational fields. All three lanes block a fresh opposing direction with slope **None**. The requested sign-plus-slope warm-up and positional-plus-intraday/divergence rule are not implemented. Slope is computed on normalized scores, not the requested net values; its in-memory history is not keyed by expiry and is lost on restart.
- **Not all four signals are delta-weighted.** `desk8600_board_8510.py:322–326` correctly preserves Classic Writer Flow's original bridge output, but it is not transformed into the requested weighted writing-mass signal. Preserving the protected source is correct; calling all four sources weighted is inaccurate. No protected file was changed.
- Previous-day values come from a supplied prior stored chain, not a Dhan `previous_oi` field in this reader. That source distinction requires provenance/coverage verification. It is not automatically equivalent to an exchange closing baseline.

### Freshness verdict

**Explicit STALE: PASS 24/24. Universal missing-metadata safety: FAIL.** A source with `age_sec=1`, `feed_state=LIVE`, opposing direction and **no source_ts** blocks. `desk8600_lane_gating_8510.py:185–193` does not validate source timestamp. Do not summarize this as “STALE can never block” without stating the missing-metadata failure. Existing snapshots carry recorded-chain timestamps, not necessarily the raw Dhan receipt timestamps requested by the addendum.

### Control endpoint verdict

Ordinary helper file writes preserve valid unrelated JSON keys in the supplied tests. **Effective-mode round-trip fails:** `engine_8510_live.py:346` writes top-level runtime modes, but `load_desk8600_lane_gating` overlays nested `writer_bias.desk8600_lane_gating` afterward (`:65–68`). A LOG_ONLY toggle response can therefore coexist with effective BLOCK. Independently reproduced.

Writes are direct read/modify/write, without atomic replacement or coordination. Corrupt JSON is replaced with an empty document before writing modes, which can discard other configuration. Concurrent edits are not validated by the supplied tests. This is a control-plane blocker until effective modes and durable writes are verified together.

No persistent lane-lock mutation appears in the gate itself, and existing no-flood audit tests pass. However, reversed/opposing defaults can repeatedly veto entries; that is not cured by the absence of a literal permanent lock.

## Required before a real-money BLOCK acceptance

Correct direction and put delta; use matched poll changes for poll signals; keep genuine opening and previous-day baselines separate; enforce slope/divergence eligibility with explicit warm-up; reject missing source provenance; unify request scheduling and per-symbol cache ages, including failed attempts; make toggle response equal the effective durable runtime configuration. Then rerun these independent failing probes, the baseline diff and live-paper observation. Do not loosen the stale execution-price guard.

**Friday with BLOCK on: NO.** Unit tests use mocks; only live-market observation can validate provider delivery, quote ages, account-wide contention and actual execution behaviour. No claim of guaranteed fills or profitability is made.

## Full-suite limitation

The requested full `scripts/run_baseline_diff_tests.py` was attempted with a 240-second review bound. It did not finish the target suite within that interval (exit 124), and no complete full-suite JUnit result or baseline comparison was produced. The remaining reviewer-owned pytest child was stopped explicitly afterward. Consequently **full-suite branch-new regressions are NOT ESTABLISHED**; only the focused comparison above supports a zero-new-regression statement. The full run initially collected the independent review probes before their filename was moved out of ordinary discovery, another reason not to call that attempt a clean branch-only regression result. No inherited failures were silently hidden or assertions weakened.
