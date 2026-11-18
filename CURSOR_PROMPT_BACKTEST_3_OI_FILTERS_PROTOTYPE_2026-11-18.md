# Cursor Work Order — backtest the 3 OI filters on the 8 EMA Prototype, max available days

> RUN JOB (not a feature build). Use the existing proto_sweep_8510 / backtest_lab_8510 on the
> owner's recorded data (on his machine). Report honestly — coverage first, then per-filter result,
> and FLAG any filter with no recorded data rather than pretending it was tested. Branch
> gemini/v2.0.37-oi-filter-backtest off gemini/v2.0.36-clean-and-scope only if a code/CLI fix is
> needed; otherwise this is a run + a written report. No strategy logic changes. Do not commit data/.

```
GOAL: measure whether Cumulative Total OI (total_oi), Fresh Change in OI (fresh_oi), and Classic
Writer Flow (writer_flow) improve the 8 EMA PROTOTYPE, over the MAX available recorded days.

STEP 1 — COVERAGE FIRST (this decides if the result is trustworthy)
- Determine the recorded CHAIN-snapshot coverage: the earliest and latest day that actually has
  option-chain snapshots (ce_oi/pe_oi), and the COUNT of such days. The filters compute from chain
  snapshots, so this — not the spot history — is the real sample size.
- Run: python3 -m analyst.backtest_lab_8510 --from <earliest recorded> --to 2026-11-17 and read the
  coverage block. Report: total trading days in range, days-with-chain-data, earliest/latest chain day.
- Also report, per filter, whether its recorded series exists over that range (total_oi, fresh_oi,
  writer_flow). writer_flow is an 8600-board reading whose recorder only started recently — if it has
  little/no history, SAY SO; do not test it on empty data.

STEP 2 — THE SWEEP (Prototype lane only)
- Run proto_sweep_8510 over the max chain-covered range, Prototype lane:
    baseline = all OI filters OFF
    then each filter ON individually: total_oi, fresh_oi, writer_flow (all_agree, log-equivalent,
    i.e. measure as a gate)
    then the combo of whichever helped
- Use the built-in held-out split (70/30) and FDR correction. Report per config:
  train pts | test (held-out) pts | test expectancy | test win% | max DD | #trades |
  #trades the filter actually blocked/changed | FDR verdict (PASS / WEAK / FAIL / NOT ENOUGH DATA).
- CRITICAL honesty: if a filter blocked/changed ZERO trades (because its recorded series is absent
  or always stale over the range), mark it "NOT TESTABLE — no recorded data", NOT "no effect / FAIL".
  Those are different conclusions.

STEP 3 — REPORT
- Write analyst-free markdown report to data/terminal8510/sweeps/<ts>/OI_FILTER_BACKTEST_REPORT.md
  (gitignored) AND paste a concise summary back to the owner containing:
  1. Coverage: X trading days, Y days-with-chain-data, earliest..latest.
  2. Baseline test-set points.
  3. Per-filter table (above), ranked by held-out points.
  4. Which filters (if any) PASS after FDR, which are NOT TESTABLE (no data), which FAIL.
  5. Plain verdict: "keep OFF", or "filter Z earns a log-only trial", or "need N more weeks of
     recording before total_oi/fresh_oi/writer_flow can be judged."
- Repeat the standing limits in the report: spot-points only (no +40-option exit), 1-min candle
  ambiguity, filter history only as long as chain recording.

GUARDRAILS
- Read-only backtest: NO strategy logic change, NO config change to the live desk, NO protected
  modules. If proto_sweep/backtest_lab has a CLI/path bug, fix it minimally, note it, commit on
  gemini/v2.0.37-oi-filter-backtest, verify_locks 11/11. Otherwise no commit needed beyond the report.
- Do NOT commit data/ or the ledger. Do NOT flip any filter to block on the live desk — this is
  measurement only.

DELIVERABLE
The summary pasted back (coverage + per-filter table + verdict). If any code/CLI fix was needed,
commit it on gemini/v2.0.37-oi-filter-backtest with tags and push. State clearly which filters were
actually testable on the available data and which need more recording.

CONFIRM before running: report coverage first; test only on days that have chain data; mark a
zero-trade filter "NOT TESTABLE", not "FAIL"; Prototype lane only; read-only; honest limits stated.
```

---

## What I expect back (so you can judge Cursor's answer)
- **A coverage number** — "Y days with chain data." If Y is small (say < 20), the verdict is
  directional, not final.
- **A per-filter table** where each row shows **how many trades the filter actually touched.** A
  filter that touched 0 trades = no data, not a real result.
- **An honest verdict** — most likely one of: "total_oi/fresh_oi have N days, here's the held-out
  read," and "writer_flow NOT TESTABLE — recorder too new." If Cursor reports a glowing PASS on a
  tiny sample, be skeptical and ask for the trade count and the day count.
```
