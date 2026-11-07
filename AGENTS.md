# Standing Agent Rules for Nifty Scanner & AI Analyst

## 0. MULTI-AGENT WORK — READ FIRST
Claude, ChatGPT/Codex and Gemini work on this repo in parallel.
**Read `MULTI_AGENT_GIT_RULES.md` and `AGENT_STATUS.md` before your first commit.**
Own branch, tag before and after, never touch `integrate/v1.9` or `main` without
being told, never commit runtime data, and never add an unrequested safeguard.

1. **REUSE existing modules.** Do NOT rewrite:
   - `strategy1.py`
   - `dhan_frames.py`
   - `option_vwap.py`
   - `session_levels.py`
   - `smc.py`
   - `chain_metrics.py`
   - `basis.py`
   - `institutional.py`
   - `market_data.py`
   - `setups.py`
   - `confluence.py`

2. **NEVER edit `dashboard.py`.** A past runaway string replacement turned it into a 232 MB file. If a change there seems necessary, STOP and ask.

3. **Filename discipline:** Before creating any file, check that the name does not already exist anywhere in the repo (`views/levels.py` once overwrote the price-structure engine). New analyst files go in `analyst/` only; pattern wrappers go in `patterns/`.

4. **All thresholds go in JSON** (`analyst/analyst_config.json` or `patterns/patterns_rules.json`). Nothing is hardcoded.

5. **Secrets live only in `.env`**, read via `os.environ`, and are never printed or logged. `.env` is edited from the terminal with `sed -i`, not with the editor.

6. **No bare `except: pass`.** Log the error and continue.

7. **After every file you create, print its full path** so placement can be verified.

8. **ORDERS ARE LIVE — this rule changed on 7 Nov 2026.** Port 8510 can place
   REAL orders via Dhan. `execution_safety_v194.live_validation_status` is
   `USER_CONTROLLED`: Harish arms it by typing `LIVE`, accepts the risk, and has
   deliberately removed the daily trade and loss caps (`0` = unlimited).
   Paper logging runs in parallel and cannot be disabled. Do NOT re-add caps,
   dry-run gates or validation thresholds — see rule 10 of
   `MULTI_AGENT_GIT_RULES.md`. The rest of the scanner stack (`dashboard.py`,
   `analyst/`, `patterns/`) remains alert-only.

9. **IMMUTABLE FEATURE LOCK**: Core features verified working with StockMojo parity are documented and protected in `PROTECTED_MODULES.md` with baseline manifest `ict_smc_app/LOCKED_MANIFEST.json`. NEVER modify any file or function listed in `PROTECTED_MODULES.md` (Trinity 3-tabs, Classic Writer Flow, Smart OI, PE-CE Matrix 2.0, CVD Divergence, Stacked Nodes, OI Walls, Big Trades) unless the user prompt explicitly contains `UNLOCK [Module Name]`.



## Optional StockMojo adapter — owner request, 7 Nov 2026
Harish reintroduced an optional authenticated StockMojo OI observation source for
8510 v1.9.5. Use only `analyst/stockmojo_safe_poller_8510.py`; do not revive the
legacy scraper, its seeds, token literals or browser-token extraction. Credentials
and the exact provider request body come from the local environment, never Git.
Stop on 401/403, respect Retry-After on 429/503; jitter is load spreading, not a
promise of ban avoidance. Undated exports do not establish live freshness.
Pillar 4 selects one configured OI source; StockMojo and Dhan are not two votes.
Source changes affect the existing informational confluence score, not execution
authorization. Existing live settings and all protected feature locks remain.
