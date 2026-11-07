# v1.9.7 staging review

Base4580d79. Separate clone/branch chatgpt/v1.9.7-live-collector-and-staging. No production deployment, live arming or broker calls. This feature is observational.

## Collector

Local exports ONLY, as Harish selected. It does not automate the StockMojo website or use session tokens. Supported files: PE_CE_NIFTY*.csv/.json/.tsv in Downloads and the dedicated drop folder. .xls is not parsed. Filename must declare a session date unless timestamps include one. Filename dates remain declared/unverified; they do not establish live provenance.

Scans every135–170 seconds, weekdays09:15–15:30 IST; this is not an exchange holiday calendar. It sorts top-prepended rows into chronology, removes Prev Day, and persists per-minute observations plus derived telemetry. Duplicates are ignored; conflicting completed observations are quarantined, never silently overwritten. Atomic replacement protects the session snapshot. Current/future minutes and suspiciously suppressed newest rows are held. Completion remains heuristic. A final15:30 candidate may require a later explicit --scan-now once completed; the routine scanner does not extend market hours. No historical file is silently promoted to live evidence.

Deterministic prefix computation runs outside SQLite writer transactions; inserts use short transactions. Restart can recover saved telemetry without duplicating it. Single-file parsing and bulk historical replay are not guaranteed sub25ms; cache reads and execution do not wait for Ollama.

## Progressive windows

Below4 completed observations: WARMING_UP, bias0, confidence0. At4/6/16/31/61 rows,3/5/15/30/60-minute features respectively become available. N minutes still requires N+1 observations. Missing history is not padded. Early scoring weights normalize available3m/5m windows. Once15m exists,3m remains diagnostic; the registered5/15/30/60 weights are retained and normalized over available windows to preserve the full v1.9.6 comparator. This is explicit in data_quality.scoring_weights. MIN_HISTORY61 remains a compatibility constant, not a lockout gate.

Session classification requires a contiguous valid observed09:15 anchor, price displacement/persistence and matching OI-gap change. Missing anchors/gaps produce SESSION_NOT_AVAILABLE. Thresholds are documented heuristics in JSON, not trained trading edges. current_regime describes recent windows; day_regime remains its compatibility alias. confidence_kind is HEURISTIC_NOT_CALIBRATED.

Sample evaluated15:39 after suspected15:40 discard: SESSION_STRONG_BULLISH; currentNEUTRAL_MIXED; bias+7/100; confidence0/100. Session futures displacement+581.9; OI-gap change+47,724,560 raw units. Neither score is a probability. Source session date is not independently verified.

## Qwen

One daemon worker with a bounded latest-pending queue; deterministic evidence is published/persisted first. Generation guards stop old responses replacing newer observations. Local-only qwen2.5:3b, timeout15s, keep_alive30m, num_predict220, num_ctx1536. Model returns brief summary/warnings only. Application owns all structured mathematics. First/subsequent request timing labels do not prove model residency.

Actual tool-environment attempts returned ConnectionError, with deterministic fallback. They do not validate cold/warm inference success. Run the supplied probe locally before approving a model narrative. Simulated delayed success/failure, slim narrative schema and cache isolation have targeted coverage.

## Validation boundaries

The baseline and current suite use identical copied market/master fixtures, recorded hashes, and a frozen11:30 IST Matrix clock. The Matrix cutoff itself is unchanged; a boundary test verifies15:05 allowed and15:06 blocked. Two obsolete suites are explicitly module-skipped before import. Ordinary marks cannot prevent import errors. Existing unrelated failures are not hidden or weakened.

The tool environment loses thread-to-asyncio socket wakeups. The external, uncommitted v197_environment_plugin.py uses bounded10ms event-loop polling to let both baseline and candidate tests complete, and applies the same Matrix clock to the baseline. This is a test-runner workaround, not production code. Host verification should repeat ordinary pytest without that environment workaround.

Browser rendering remains BLOCKED: loopback listener denied EPERM, and browser policy disallows file URLs. No alternate browser or policy bypass attempted. Actual component JavaScript syntax and mock-DOM state checks pass; they are not a browser rendering check.

Disposable migration/rollback rehearsal passed:1 existing telemetry row preserved,21 unique new rows added, repeat import deduplicated, rollback restored1 row and source contents; runtime config unchanged. Live checkout never touched.

Results and remaining failures are in AGENT_STATUS.md and the external execution report. No100%-green or deployment-ready claim is made.
