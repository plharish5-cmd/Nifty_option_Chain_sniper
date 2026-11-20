# Independent execution review — consolidated v2.6.0

Reviewed source: `d63e3d0`, compared with `4917bb6`. Review date: 20 November 2026 IST.
All work is in an isolated checkout. No real orders, arming, cancels, production config writes or production restarts were performed. Broker calls in probes are fakes; the repository test harness blocks external HTTP and redirects ledgers/configs to temporary paths.

## Decision

**Do not call this release execution-ready on the strength of its new buttons or replay trail.** The fresh paper bid/ask path is materially better and its controlled mechanics work, but the new Prototype trailing exit is not wired into runtime position management. The preflight and test-order button have reproducible integration defects. Real Dhan order acceptance remains unverified.

## Ranked findings

### P1 — +40 then trail is replay-only; runtime still exits at +40

`analyst/terminal8510_backend.py:8606` in `get_active_positions` still closes a long Prototype at `spot_idx >= spot_tgt`, assigning `SPOT_TARGET_HIT`. Its mirror closes shorts. The only candle-trail branch in this section is explicitly restricted to CROSSOVER strategies. There is no `trail_after_target` reference in this method. In contrast, `simulate_prototype_spot_exit` and backtest delegation use the new trail.

Probe: `test_observed_live_monitor_has_no_prototype_trail_wiring`. This affects paper positions managed live as well as real positions; it is not merely an unverified broker stop modification. The claim that the live setup now holds beyond +40 is **REFUTED**.

### P1 — preflight reads the wrong funds schema

`terminal8510_backend.py:4161` expects top-level `availabelBalance` or `availableBalance`. The actual `DhanBroker.get_fundlimit` normalizes these to `margin_avail` (retaining the provider response inside `raw`). A valid normalized response with margin_avail=25047.70 is reported as funds_available=0 / NOT_READY.

Probe: `test_observed_preflight_ignores_actual_normalized_funds_schema`. The pure aggregation tests bypass this backend adapter and cannot establish the wiring.

### P1 — the real-test button cannot get past the permanent algo-only gate

`place_real_test_order` calls `execute_order_with_chaser` with `is_algo_trade=False` at line4216. That method unconditionally rejects non-algo calls before placing an order. The actual-method probe returns `algo_only_blocked=True`, with no fake broker BUY. Confirmation protection does work before contract resolution.

The result then sets `routed_real` from desk mode alone (line4235), so a locally rejected call can report routed_real=True. Do not bypass the algo-only policy to make the button green; choose an explicitly authorized testing policy and accurately describe the outcome. No gate bypass was demonstrated; the opposite problem is demonstrated: the advertised button is blocked by design.

### P1 — replay close-through trail books a non-executable stop price on a gap

In the trailing branch at line1030 the simulator detects a candle close through the trail, but books `_trail_stop`, rather than the observed close/next executable observation. Probe: trail120, following candle opens110, high115, low100, closes105. Simulator records exit120 even though the entire exit candle traded below120. That overstates the long result by15 index points in this example. The short mirror requires equivalent correction. A close-confirmed rule cannot assume a resting stop fill at a better price.

### P2 — readiness is not real execution eligibility or broker acceptance

The preflight uses `get_masked_config`, not v245's `public_ip_status` nor authenticated Dhan Get IP. The old masked-config path has a300s cache and even substitutes current IP as saved IP when saved whitelist is missing (`terminal8510_backend.py:1822`). It therefore cannot prove broker registration.

`real_order_preflight` hardcodes lane_live=True and derives desk mode rather than the real order's complete decision path. A probe with kill_switch_active=True, otherwise favorable supplied metadata and the balance key the adapter expects returns READY_REAL. Runtime execution would refuse the order. Cost is minimum configured premium×caller lot quantity, with fallback250; it is not a freshly selected contract's executable cost. Funds itself can be cached10s. A zero balance can be genuine, not necessarily a feed failure.

The UI phrases “would be ACCEPTED” and “will reject” exceed the evidence. Rename the output to preliminary local checks; include unknowns, actual lane/gate decisions, verified broker identity/IP and contract-specific price/quantity if it is to be used operationally. Never promise broker acceptance without an actual broker response.

### P2 — the documented reversible trail toggle is not runtime configuration

`_proto_trail_cfg` at line823 imports module `DEFAULT_CFG`; it does not read this backend's scanner configuration. The runtime exit path does not consume it at all. Editing a per-lane setting cannot switch that live trail on/off. Global DEFAULT changes also change historical backtest defaults for callers with opposite-close enabled. That is a strategy-version change, not financial validation.

### P2 — rejection origin helper was intentionally omitted, but the new button still imports it

Line4226 imports `classify_rejection` from `ema_arm_persistence_8510`, which is absent on the consolidated source. The broad exception swallows the ImportError. Thus the new test button does not supply its claimed LOCAL/BROKER classification. The v244 HTML still displays exact reasons/categories; this omission is specific to the new method's claimed origin fields.

## Charter verdicts

| Item | Verdict | Evidence / qualification |
|---|---|---|
| A1 four new commits after4917bb6 | CONFIRMED | Exactly7915ea6,25a1803,7e4be08,d63e3d0. |
| A2 cited ChatGPT fixes retained | CONFIRMED | All listed hashes f9347ad,f629427,9dc3999,24ce19e,69e0183,8b1c607,48a28b2 are ancestors. |
| A3 duplicate branch implementations excluded | CONFIRMED | Duplicate commits ca3cfcd/94221e0/72b7a6f/c8037f0 are not ancestors; new runtime source diff inspected. |
| A4 better duplicate features | PARTIAL | LOCAL vs BROKER distinction is useful, but should use explicit provenance instead of substring guesses. The consolidated test method depends on the omitted helper. v45's single stale banner/transition log is useful observability to consider independently. Do not import v46's unvalidated cached-LTP fallback: v251's fresh-depth-only rule is stronger. |
| B replay trail ratchet | CONFIRMED bounded | max/min preserves monotonic stop; uses prior walked1m extreme. Existing long/short tests pass. Does not establish all callers pass only completed bars. |
| B gap / close-through accounting | REFUTED | Non-traded exit120 probe above. |
| B runtime trail | REFUTED | Active position monitor still fixed target. |
| B other strategies unchanged | CONFIRMED scoped | New default delegated only with opposite-close flag; runtime position logic unchanged. Not a claim that every historical report is unchanged. |
| B phase6 classic override | REASONABLE SCOPING | Explicit trail_after_target=False tests classic delegation; no financial-edge assertion. Add separate trail-mode expectations rather than reading this test as trail validation. |
| C usable readiness | REFUTED | Wrong normalized funds key, old IP path, incomplete gate checks. |
| D one-lot button works | REFUTED | Actual algo-only gate stops it before broker submission. |
| D missing confirmation prevents orders | CONFIRMED | No contract resolution or submission on NO. Real network forbidden. |
| D real arming bypass | NOT DEMONSTRATED | Shared execution kill/route gates remain. No real order submitted; one-lot/broker lifecycle beyond local rejection not established by this button. |
| E alias semantics | CONFIRMED | execute_position_exit directly awaits existing locked close method, with same args. |
| E pending retry | CONFIRMED controlled | Original reason retained; later valid bid closes; get_active_positions resumes EXIT_PENDING through existing task path. Tests cover mechanics, not network uptime. |
| E observed option pricing | CONFIRMED controlled | New paper ask251→bid279, qty65, gross1820. Actual fake broker fill250 stays authoritative when live path is explicitly opted in. Missing/crossed books rejected. Quote depth capacity/exchange freshness and live network remain unverified. |
| F all six are merely missing-depth fixtures | REFUTED as stated | Missing depth explains several failures, but supplying depth alone to the original option-points test still fails the expected live companion stop: Prototype default lane routing downgrades it to PAPER_ONLY. Fixture must explicitly choose the intended lane routing too. One listed “live-buy” failure is an exit-only test. |
| G production readiness | UNVERIFIABLE / NOT CLEARED | No live acceptance, no unattended proof; bugs above remain. |

## Test evidence

See the appended final verification block for exact baseline/candidate counts and any run limitations. Reviewer probes intentionally assert observed defects for diagnosis; they are named `independent_*`, are not auto-collected as acceptance tests, and must not become requirements to preserve bugs.

## Final verification — reproducible paired fixture set

| Check | Base4917bb6 | Candidated63e3d0 |
|---|---:|---:|
| Full analyst suite, excluding test_time_series_bridge.py as repository protocol requires | 947passed /71failed /42errors /14skipped | 962passed /69failed /42errors /14skipped |
| Exact new failing/error IDs | — | **0** |
| Previously failing tests resolved | — | paper exit test6; fresh-bid retry/exit alias probe |
| Focused inherited live_exit_fixes + phase2 option-points | 16passed /6failed | same6 failing IDs (candidate expanded focused selection:46passed /6failed) |
| Independent reviewer probes (diagnostic defects + working mechanics) | not applicable | **10passed**,8warnings |
| Locked modules | unchanged source | **11/11** |
| Immutable feature tests | unchanged source | **10/10** |
| Python/TypeScript parity | unchanged source | **9059rows ×17fields,100%,0mismatches** |

Full runs used the same clean committed configuration in separate worktrees, the repository's existing conftest temporary configs/ledgers/network blocking, and the same host Python3.9 environment. No production market DB/instrument master was imported into either worktree. The counts therefore must not be compared directly with older host/private-fixture baselines. Most setup errors explicitly report no current asyncio event loop after prior asyncio.run tests; these are inherited in this paired comparison, not cleared or hidden. Other inherited test failures remain open. An ordinary full run is **not green**.

Candidate has13 additional tests from trail/preflight modules, plus2 previously failing tests now pass; hence15 additional passes and2 fewer failures. Exact resolved IDs:
- analyst/test_live_exit_orders_8510.py::test_6_paper_position_zero_broker_calls_wallet_updated
- analyst/test_paper_quotes_v251.py::test_exit_ignores_old_positive_ltp_and_waits_then_uses_fresh_bid

The initial standalone parity attempt could not load TypeScript in the isolated checkout. The final parity run used TypeScript5.6.3 in a temporary dependency folder; the downloaded archive matched package-lock's sha512 integrity. A process-local Node resolver supplied this dependency to the unchanged oracle. No frontend/production dependency directory was modified.

Engine import was verified under the private, network-blocked PAPER_ONLY harness with a fresh event loop (Python3.9's module-level asyncio.Lock needs one). This is not an assertion that arbitrary import-after-asyncio.run works, nor a production startup/restart acceptance.

No production fixes were applied by this audit. Previous v243 findings are not implicitly cleared by this narrower consolidation review. Required repair priority: runtime trail wiring and causal gap fills; backend preflight schema/provenance/wording; explicit owner-compatible policy for the currently blocked test button; preserve the verified fresh paper depth path and pending retry.
