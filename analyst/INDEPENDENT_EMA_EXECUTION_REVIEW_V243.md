# Independent 8510 execution review — 19 November 2026

## Verdict

**The desk can execute both 8 EMA lanes in an isolated paper fixture. That does not establish readiness for unattended real money after fixing the IP.** I reproduced additional state/quote/reconciliation defects. No trading rule, production file, broker credential, running app, route or arming setting was changed for this review.

Primary snapshot: `gemini/v2.0.41-arm-reconstruct`, **0fcda9a**. Supplemental snapshot: `gemini/v2.0.42-selfaudit`, **f33276e**. Both were inspected in separate checkouts. The claimed linear ancestry `176c836 → 4a14e2a → 9983e53 → 0fcda9a` is CONFIRMED by Git. The active OC checkout had already advanced to f33276e; the charter's “v2.0.42 not built” statement is now out of date. Nothing was merged/deployed by this audit.

## Claims 1–4

| Claim | Verdict | Independent evidence and limits |
|---|---|---|
| 1. Arm/runtime serialization survives restart | **CONFIRMED**, bounded | Actual SQLite save/load preserves Prototype arm and away state; abrupt child-process kill after commit preserves the same arm. Reversal runtime JSON also round-trips. CE/PE fixtures for both lanes reach actual paper ledger fills after object reconstruction. This does not prove every full HTTP daemon startup behaves correctly; see findings below. |
| 2. Replay/report and log-only wording implemented | **CONFIRMED**, bounded | Report exists; independently rerunning the module with a private snapshot of real 19-Nov market candles reproduces 4/9. Log-only verdict has “WOULD BLOCK (trading still allowed)” rather than bare BLOCKED. Exit logging hooks exist and their exception-writing test passes. These do not prove all exit causes are captured; SIGKILL/OOM cannot reliably be logged by the dying process. |
| 3. Every startup rebuilds both lanes so genuine trades cannot be missed | **REFUTED** as an unconditional guarantee | Reversal reconstruction is inside the Prototype worker after the Prototype-enabled and fresh-spot guards. Prototype OFF prevents it. Reconstruction can overwrite real stop state, and can infer an execution lock from a simulated position. Its backfill/replay is synchronous on the async worker. |
| 4. Auto-release/sweep/watchdog not built, and these are the only remaining bugs | **REFUTED** as a current statement | f33276e contains these features. The orphan reservation does bite in 0fcda9a. The blanket stuck-OPEN claim was not reproduced with a usable quote: the existing monitor closed it. Supplemental v2.0.42 probes identify fail-open reconciliation and a watchdog deadline reset, so that build is not independently cleared either. |

## Ranked findings

### P1 — Old cached spot can be called LIVE

`analyst/terminal8510_backend.py:2339–2384` returns cached quotes on backoff/errors. The producer at **5536** treats a positive cached spot as fresh when exchange LTT is absent; it does not check the cache receipt timestamp there. The payload at **5787** then stamps it with the current clock.

Independent test `test_spot_classification_trusts_old_cached_quote_when_ltt_absent` executes the actual classification AST branch with a one-hour-old receipt timestamp and missing LTT: it returns LIVE/is_stale=false. This is a narrow producer-branch proof, not a full network outage replay. A downstream age guard cannot detect an old quote whose timestamp was renewed by its producer.

Required correction before relying on live eligibility: use actual quote receipt/exchange freshness, preserve age across cache fallback, and test timeout/429/non-200 paths. Do not merely increase the stale threshold.

### P1 — Synchronous reconstruction can freeze the event loop

`analyst/terminal8510_backend.py:5190` calls `reconstruct_arms_on_startup` directly from the async Prototype worker. That function performs synchronous Dhan HTTP/backoff (`dhan_session_backfill_8510.py:29`) plus synchronous SQL and replay. During it, quote/exit/other worker coroutines on the same event loop cannot run normally.

`test_sync_startup_reconstruction_blocks_event_loop` replaces the external read with a controlled 80ms delay; an independent 5ms heartbeat cannot run until the delay finishes. Real retries can take tens of seconds, plus replay. This remains in the supplemental source. Move initialization work off the event loop with safe coordination; do not race replay state replacement against live orders.

### P1 — Reconstruction can overwrite actual risk/consumption state

`arm_reconstruct_8510.py:315–340` replaces the live Prototype and Reversal dictionaries with simulated states. It does not reconcile real execution/stop history first. `test_reconstruction_erases_real_stop_block_without_trade_reconciliation` demonstrates a real LONG stop-block being removed and replaced with a LONG arm, despite no reconciliation evidence. Candle history cannot establish whether the actual option stop filled.

Required correction: reconstruct market setup evidence separately from execution-owned consumed/stop/open-position state. Reconcile actual ledger/broker facts before replacing the latter. A wider lock sweep does not solve this distinction.

### P1 — Flat desk can inherit a lock from a hypothetical trade

`arm_reconstruct_8510.py:352–358` acquires a lane lock when reconstructed state has `bt_open`. That state describes a simulation, not a verified live/paper position.

Independent unit probe: applying a simulated bt_open with zero actual positions blocks a valid new signal. Recorded as-of probe at **10:31** using the full 19-Nov database produces a Prototype lock for the simulated 09:52–10:53 trade, with **zero real/paper OPEN positions**. Truncating the private database at 10:31 produces no lock. The historical/as-of path therefore depends on later rows; `_load_session_frames` has no now_ts cutoff and `_process_touch_signal` can apply future exit outcomes early.

Qualification: the later-row case is directly relevant to historical/as-of validation; a normal live database may not contain future rows. Do not claim this exact future-row scenario necessarily happened live. The unconditional simulated-position-to-lock conversion is independently demonstrated. v2.0.42's empty-ledger sweep can release this lock, but needs broker-aware reconciliation of uncertain submissions.

### P1 — Persisted orphan reservation blocks both lanes in v2.0.41

The actual reservation path persists a lock before order submission (`terminal8510_backend.py:7675`). `ema_lane_entry_gate_8510.py:29` rejects entry whenever holder is present, including the same lane. Abrupt termination after reservation but before fill leaves no OPEN position, yet both Prototype and Reversal remain blocked after reload.

`test_orphan_reservation_survives_restart_and_blocks_both_lanes` reproduces this using the actual ledger helpers. v2.0.42 adds release logic; see supplemental concerns below. Do not release a possibly submitted real order merely because a local position row is absent.

### P2 — Reversal-only startup depends on Prototype being enabled

The startup hook for **both lanes** is behind `if not proto_cfg.enabled: ... continue` at `terminal8510_backend.py:5144`. `test_disabled_prototype_prevents_reversal_startup_reconstruction` sets Prototype OFF and Reversal ON, drives the actual worker to its first sleep and verifies reconstruction was never called.

Existing persisted Reversal state may still load from SQLite, and a running scanner can derive future crosses; this finding specifically refutes independent fresh Reversal reconstruction on every startup.

### P2 — Paper exit can use a stale stored option LTP

`terminal8510_backend.py:8685–8693` tries cached telemetry, then stored current_ltp, requesting a new broker quote only if both are missing/nonpositive. There is no age validation on the stored LTP in this path.

`test_paper_exit_uses_old_stored_ltp_without_requesting_new_quote`: entry/stored price250; available mocked current quote260; close books250 and never asks for260. This confirms a quote-selection defect, not market performance. Paper exit/P&L cannot be used as evidence of executable pricing until freshness and appropriate bid/ask evidence are recorded.

### P2 — The 4/9 replay is not a joint execution proof

`replay_marked_opportunities_8510.py:227–230` runs the lanes separately. The resulting Prototype 09:52–10:53 hold overlaps Reversal10:31. The real desk's shared position/lock rules do not permit both entries as shown. No executable option quotes or dual spot/option exits are replayed.

Matching also uses ±6minutes for early Prototype marks, ±20minutes for late ones, and ±15minutes for Reversal. Thus15:00 gets credit for15:15. The generic matcher can reuse one entry for multiple marks (independently tested), though the reproduced four credited entries here are distinct.

The14:00 miss explanation is not reliable: the explanation helper runs intrabar checks from empty state without replaying closed-candle arming. It reported “arm missing,” while the report claimed “no touch.” Reversal NO rows use a generic “window/filter/touch” message, not a traced rejected condition.

## Required A–F results

### A. End-to-end paper execution — CONFIRMED in controlled fixtures

Both CE and PE for Prototype and Reversal use the real evaluator, real `_process_strategy_signals`, real `execute_order_with_chaser`, and actual private SQLite orders/positions. Contract resolution and market quotes are fixtures; no real broker order is sent. The global fixture route is PAPER_ONLY; the original freshness guard is restored instead of accepting the suite's automatic guard bypass. Prototype cases explicitly exercise cross → away → retouch. Reversal cases exercise a valid cross/window and live-shaped dynamic touch.

Result: each gets FILLED/PAPER_ONLY and OPEN in the private ledger. This proves the software can reach paper execution; it does not prove a production opportunity has all required data or will receive a broker fill.

### B. Restart continuity — CONFIRMED for serialization/controlled next touch; unconditional startup claim REFUTED

Prototype CE/PE next-touch order tests run with and without backend re-instantiation. Reversal CE/PE next-touch tests similarly reload runtime state before the trigger. Separate child-process test commits an arm, is killed, and the next process reloads identical arm values. Full FastAPI process startup with live Dhan was not executed; no production process was killed by this review. Reconstruction defects above remain important.

### C. Silent stalls — mixed verdict

- Orphan lock: CONFIRMED on0fcda9a.
- Satisfied target with usable quote inevitably stays OPEN all day: REFUTED as a blanket claim. `test_existing_monitor_recovers_satisfied_spot_exit` books CLOSED/SPOT_TARGET_HIT through the existing monitor and paper close path.
- Missing/stale quote, uncertain broker outcome or unavailable ledger: not equivalent to the usable-quote case. Supplemental reconciliation currently conflates some unknown states with flatness.

### D. Touch fidelity and boundaries

- Fast110→100→110 round-trip between observed endpoints can be missed: CONFIRMED sampling limitation. Supplying the observed range containing100 detects it. A 1-second tracker cannot reconstruct an event its source never delivered; minute candles cannot prove 3-second execution fidelity.
- Tested central15m and1H resampling anchors: CONFIRMED09:15;15m09:15/09:30/09:45 and1H09:15/10:15/11:15. This is not proof of every consumer or every chart setting.
- A candle labelled09:30 closes09:45; that close cannot arm a trade retroactively at09:30.

### E. Real-money path

The recorded09:41 rejection is DH-905 Invalid IP. That establishes what Dhan returned, not that all remaining code is safe after updating the allowlist. Actual valid-IP broker placement is **UNVERIFIABLE in this read-only/PAPER_ONLY review**. No live toggle or real order was used. Quote freshness, initialization coordination, lock reconciliation and execution-owned state must be addressed independently of IP.

### F. Independent19-Nov replay

| Mark | Reproduced output | Interpretation |
|---|---|---|
| Prototype09:41PE | entry09:39 | fuzzy spot-model match; real entry09:41 was a separate broker attempt |
| Prototype10:15CE | NO; held09:52–10:53 | justified only within spot-only model; option exit could differ |
| Prototype10:30CE | NO; held09:52–10:53 | same qualification |
| Prototype13:00CE | entry13:06 | six-minute-late match, not exact-time fill |
| Prototype14:00CE | NO | explanation helper did not establish the actual blocker |
| Prototype15:00CE | entry15:15 | fifteen-minute-late match under widened window |
| Reversal09:45CE | NO | generic reason; precise rejection not independently established |
| Reversal10:30CE | entry10:31 | conflicts with independent Prototype hold if both lanes run together |
| Reversal13:15CE | NO | generic reason; precise rejection not independently established |

Numerical4/9 is reproducible. It is not four proven executable trades or a verified failure rate.

## Supplemental v2.0.42 review

The latest snapshot contains real implementation, not only a prompt. Its original9 tests plus3 independent defect probes pass in isolation. Passing defect probes means the defects were reproduced:

1. Reconciliation catches a failed position read and treats it as zero positions, releasing the lock.
2. With no local position and a mocked broker TRANSIT order, it releases without consulting broker pending orders. This probe establishes the missing check, not a real duplicate order.
3. Repeated unfilled signals every3seconds overwrite pending.t0 each time. A12-second watchdog never expires while retries continue; it alerts only after submissions pause. See `note_signals_submitted` aroundline105 in f33276e.

This is a partial supplemental review, not an all-code approval of v2.0.42.

## Verification and reproducibility

- Independent primary probes: **22 passed, 1 explicitly version-scoped skip**, `outputs/v243_independent_results.txt`. Some passing probes intentionally assert a reproduced defect. Do not present this count as a clean trading-system acceptance result.
- Supplemental snapshot: **12 passed** (9 existing +3 independent probes), `outputs/v243_latest_results.txt`.
- Existing primary arm/reconstruction/finish-line/v241/v242 bundle: **40passed/1failed**. The failure is `test_replay_19_nov_deterministic_row_count`, which resolves market_db to an absent private-checkout path. No assertion was changed. The separately configured private snapshot run reproduces4/9 successfully. These are different fixture sets, not contradictory results.
- Locks **11/11**; immutable feature tests **10/10**. No protected code changed. Full analyst regression and TypeScript/Python parity were not repeated for this read-only code audit/test-report addition. Prior v242 full817→824pass/56fail/7errors/10skip is a different source snapshot and is not this audit's baseline.
- Only new review probe scripts/report/status are committed. Probe filenames deliberately do not match test_* discovery: defect-confirmation assertions are diagnostic observations, not acceptance rules requiring broken behavior to remain. Private market snapshot, sanitized config, replay ledgers and raw results remain outside the source worktree; no runtime data or credentials committed.
- Repro commands: `python -m pytest analyst/independent_ema_probes_v243.py -q`; supplementary file is explicitly skipped on0fcda9a because its reviewed modules exist only in the later snapshot. Execute the supplement onf33276e to reproduce it. No existing test assertions or production source changed.

## Required before an unattended-live sign-off

Correct quote receipt freshness and paper executable-price evidence; separate reconstructed market setup from real trade/risk state; make startup reconstruction nonblocking and independently reachable for each lane; reconcile persisted reservations against actual positions and pending broker outcomes; repair watchdog timer semantics; replace the joint replay claim with a causal shared-account replay that includes option quotes and precise rejection reasons. Then repeat fault-injection and valid-feed paper acceptance. A broker IP change alone does not satisfy these checks.
