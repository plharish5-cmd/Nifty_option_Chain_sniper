# Cursor Prompt — clean the paper ledger + scope the desk to 3 strategies

> Owner's orders book is polluted with pytest test-fixture rows ("Stage 2 Test Fixture", Q_GATE,
> OD_V15) and old expired-contract paper strays (24300 CE 11 Nov, 25400 CE 17 Nov). The tiny
> "-₹60 / -₹87" P&L is charges on flat non-trades, not strategy results. Fix: stop tests polluting
> the live ledger, purge the junk, make the expired sweep work, and SCOPE the desk to only three
> strategies. Branch gemini/v2.0.36-clean-and-scope off gemini/v2.0.35-reversal-oi-panel.
> Tags pre-/post-. Push. Do NOT merge. No protected modules. verify_locks 11/11.

```
=== PART 1 — scope the desk to EXACTLY 3 strategies (owner request) ===
Keep ENABLED only: 8 EMA Prototype, 8 EMA Reversal (crossover), 1H Flagship (the
"1H 8-EMA Support + 15m PA (Homework)" lane).
DISABLE (config only, reversible — do NOT delete code or tests): Stage 2, Stage 3, the 8600 Ticket
lane, and any other scanner lane. Set their scanner enabled flags to false via a patch script
(backup config first). Their code and tests stay intact so the owner can re-enable later.
- The scanner must not generate signals or paper orders for the disabled lanes.
- Any "active strategies" / party-switch / Equity-Guys view shows only the 3 enabled lanes.

=== PART 2 — purge the junk from the paper ledger (one-time, safe) ===
Write scripts/clean_paper_ledger_8510.py that BACKS UP the ledger DB first, then DELETEs from the
paper orders/positions tables rows that are any of:
  (a) test fixtures — strategy_setup contains "Test Fixture", or order_id matches test markers
      (e.g. contains "Q_GATE", "OD_V15", or other obvious non-broker test IDs);
  (b) expired contracts — the option's expiry date < today;
  (c) rows from the now-disabled strategies (Stage 2, Stage 3, ticket) if the owner wants a clean
      slate — gate this behind a --drop-disabled flag (default on), keep a dry-run --preview mode.
ONLY paper rows. NEVER delete a real-money / live row (assert mode != LIVE before delete). Print a
summary of what was removed. Idempotent and re-runnable.

=== PART 3 — stop the test suite from EVER writing to the production ledger ===
Root cause: pytest fixtures (test_v15_fixes_8510 creates "Stage 2 Test Fixture" orders) reached the
real ledger. conftest.py sets a private ledger_db_path, but the isolation has a hole.
- Harden it: EVERY code path that writes an order must use the configured ledger_db_path, and under
  tests that path must be the temp one. Add a guard so an order write refuses (or is redirected)
  if, during a test run, the target ledger is the production DB. Add an assertion in conftest that
  the active ledger is the temp root, failing loudly if a test is about to touch production.
- Verify no test writes to analyst/.../ (the real data dir). A "Test Fixture" row must be
  impossible in the live orders book after this.

=== PART 4 — expired sweep + orders-book display filter ===
- Make the v2.0.31 cleanup (check_open_position_cleanup) actually close/remove EXPIRED-CONTRACT
  paper rows (expiry < today) on the scanner loop, not just off-hours ones — so strays like the
  24300 CE can't linger. Log each with reason EXPIRED_CONTRACT_CLEANUP.
- Orders book UI: as a safety net, hide rows whose strategy_setup looks like a test fixture, and
  rows from disabled strategies, so even if one slips in it never shows as a "trade".

GUARDRAILS
- Reversible: Stage 2/3/ticket are DISABLED in config, not deleted; their code + tests stay and
  pass. Protected modules untouched; verify_locks 11/11. Config via patch script (backup first);
  preserve M terminal8510_config.json. Never touch real-money rows.
- TESTS: a test proving pytest cannot write to the production ledger (isolation guard fires);
  clean_paper_ledger preview lists the junk and --drop removes only paper test/expired/disabled
  rows; expired-contract paper row is swept; disabled strategies produce no signals; the 3 enabled
  lanes still work. verify_locks 11/11; full pytest diffed against the ~889/35/42 baseline — the
  isolation fix may REDUCE failures (good); no NEW failures.

DELIVERABLE
Commit on gemini/v2.0.36-clean-and-scope, tags, push, AGENT_STATUS block. File list + the exact
commands: run clean_paper_ledger --preview, then --drop; run the scope patch; restart. State what
couldn't be verified (live). Do not merge.

CONFIRM before coding: only Prototype + Reversal + 1H Flagship enabled (Stage 2/3/ticket disabled
in config, code kept); ledger purge is paper-only, backed up, never touches live rows; tests can no
longer write to the production ledger; expired paper rows are swept; orders book hides test/disabled
rows.
```

---

## For you
- After this, your orders book shows **only real trades from your three strategies** — no test
  fixtures, no expired strays, no "-₹60 on a flat trade" noise. Then you can judge the strategy on
  real P&L.
- **Stage 2 / Stage 3 are switched off, not deleted** — flip them back on in config whenever you
  want. Nothing is lost.
- Run it in two steps on your machine: `clean_paper_ledger_8510.py --preview` first (see what it
  will remove), then `--drop` to actually clean. It backs up the ledger before touching anything.
- This is **data hygiene, not a strategy change** — your Prototype/Reversal/Flagship logic is
  untouched. The app wasn't making ₹1-2 trades; it was showing you test junk.
```
