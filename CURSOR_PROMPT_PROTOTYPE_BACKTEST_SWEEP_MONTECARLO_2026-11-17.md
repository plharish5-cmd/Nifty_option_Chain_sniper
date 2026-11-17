# Cursor Prompt — Prototype filter sweep + Monte Carlo backtest harness

> Paste into Cursor (Gemini). Builds a backtest HARNESS on top of the existing
> `analyst/backtest_lab_8510.py` and runs it on the owner's recorded data (on his machine).
> Purpose: measure each Prototype filter on/off, their interactions, and robustness via Monte
> Carlo — with held-out validation and multiple-testing correction, so a config that wins by
> luck is NOT trusted. Read `Backtest_Lab_Guide_2026-11-13.md` and
> `OI_Momentum_v2_And_OI3m_Handover_Pack_2026-11-17.md` first.

---

```
ROLE
Build a backtest harness analyst/proto_sweep_8510.py on branch
gemini/v2.0.29-backtest-sweep off the current tip. Reuse backtest_lab_8510.py (do not rewrite
the engine). Tags pre-/post-. Do not merge. No protected modules. verify_locks 11/11.

GOAL
Answer, per the recorded data: does any filter configuration beat the baseline (all filters OFF)
on UNSEEN days, after correcting for luck? Output a ranked table + a plain-English verdict.

DO NOT brute-force all 2^N combinations. That overfits. Use this staged design:

STAGE 0 — BASELINE
- Run the Prototype with every filter OFF (momentum, writer_flow, price_oi, total_oi, fresh_oi,
  volume_oi, oi_3m, require_volume_confirm, require_cumd_confirm, candle_trail). This is the
  reference. Report: trades, total spot points, win rate, avg win, avg loss, max drawdown (pts),
  expectancy/trade, t-stat.

STAGE 1 — SINGLE-FACTOR (marginal effect)
- Turn ON exactly one factor at a time vs baseline. Factors:
    momentum gate (log vs block), each OI switch individually, oi_3m, require_volume_confirm,
    require_cumd_confirm, candle_trail (tf 15 / 5 / 3), combine-rule (all_agree/any/majority),
    neutral_blocks.
- For each: delta vs baseline in points and in expectancy, # trades removed/changed, and whether
  the trades it removed were net losers or winners (points_saved vs points_cost).

STAGE 2 — PAIRWISE INTERACTION (only for factors that helped in Stage 1)
- Take the Stage-1 factors with a positive held-out delta and test them in pairs. Report the
  interaction term: does A+B beat A and B alone, or cancel? Skip pairs where either factor failed
  Stage 1.

STAGE 3 — CANDIDATE COMBOS
- Build at most ~10 full combos from the Stage-1/2 winners (e.g. fresh_oi+total_oi+oi_3m, +trail).
  Not thousands. Each gets the full report.

HELD-OUT VALIDATION (applies to every config above)
- Split days: first 70% train, last 30% test (reuse backtest_lab split_days / verdict). Rank and
  pick configs ONLY by test-set (unseen) performance, never train. Report train vs test side by
  side — a config that wins on train but not test is overfit; flag it.

MULTIPLE-TESTING CORRECTION (critical — this is the anti-luck step)
- You will test many configs, so some will look good by chance. Apply Benjamini-Hochberg FDR (or
  Bonferroni) across all configs' test-set p-values. A config is only "PASS" if it beats baseline
  AND survives the correction at q=0.10. Everything else is "not proven", not "good".

MONTE CARLO ROBUSTNESS (per surviving config + baseline)
- BLOCK BOOTSTRAP: resample whole TRADING DAYS with replacement (preserves intraday
  autocorrelation — do NOT iid-resample individual trades), 2000+ iterations. Report the 5th/50th/
  95th percentile of: total points, expectancy, max drawdown. If the 5th-percentile total points
  is <= 0, the edge is not robust — say so.
- PERMUTATION / NULL TEST: randomize the filter's block decisions (or shuffle entry sign) to build
  a null distribution of the delta-vs-baseline; the config's real delta must sit in the top 5% of
  the null to count. Report the empirical p-value.
- RANDOM-ENTRY BASELINE: a Prototype-like strategy entering at random touches, same exit rules, as
  a sanity floor — the real strategy must clearly beat it.

OUTPUT
- One ranked table (CSV + a readable MD): config | train pts | test pts | test expectancy | test
  win% | max DD | bootstrap 5th-pct pts | permutation p | FDR verdict (PASS/NOT PROVEN/FAIL).
- A plain-English summary: "Baseline test = X pts. Only configs that PASS after correction: <list
  or 'none'>. Recommended: <baseline, or the one surviving config>." If nothing beats baseline on
  held-out after correction, SAY SO — "keep filters OFF" is a valid, honest result.
- An explicit LIMITATIONS block in the report: spot-points only (no option premium); N recorded
  chain-days available for the OI filters (print the actual count and warn if < ~20); the OI
  filters cannot be trusted on thin history regardless of how good they look.

CLI
  python3 -m analyst.proto_sweep_8510 --from YYYY-MM-DD --to YYYY-MM-DD --stage all \
     --out data/terminal8510/sweeps/<ts>/
Writes the CSV + MD report there (gitignored). Runs as a background-capable job like backtest_lab.

GUARDRAILS
- Reuse backtest_lab_8510 engine + its no-look-ahead, completed-candle rules. Report spot points.
- Print the recorded-data coverage up front (trading days, chain-snapshot days) so the owner sees
  the sample size before reading any result.
- Tests analyst/test_proto_sweep_8510.py: staged selection logic, FDR correction math on a known
  input, block-bootstrap preserves day structure, verdict thresholds. verify_locks 11/11; full
  pytest diffed against baseline.

DELIVERABLE
Commit on gemini/v2.0.29-backtest-sweep, tags, push, AGENT_STATUS block. Give the owner the exact
run command and where the report lands. State plainly you could not run it in the sandbox (no
recorded data) — the owner runs it on his machine.

CONFIRM before coding: staged (not brute-force 2^N); rank by HELD-OUT only; FDR correction so
luck is filtered; block-bootstrap by day (not iid); random-entry floor; honest LIMITATIONS +
coverage print; "keep filters OFF" is an allowed verdict.
```

---

## How to read what comes back (for you)
- **Look only at the TEST (held-out) column and the FDR verdict.** Ignore anything that only wins
  on train — that's the overfit trap.
- **If the bootstrap 5th-percentile points is ≤ 0**, the edge isn't robust even if the average
  looks good — don't trust it.
- **"None PASS" is a real, good answer.** It means: keep the filters OFF, run the Prototype as
  validated. That's not a failure of the work — it's the backtest doing its job and stopping you
  from turning on noise.
- **The OI filters will likely be "not proven" simply from too few recorded chain-days.** That's
  expected this early — the harness prints the day count so you can see it. Give it a few weeks of
  recording before expecting a verdict on those.

## For tomorrow, regardless of the backtest
Everything default-OFF. Prototype runs as validated. OI filters at most **log-only** (watch, don't
block). That's the configuration least likely to fail — new, unbacktested gates stay off until the
held-out numbers above earn them.
```
