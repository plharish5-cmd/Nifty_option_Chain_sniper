# AI Assistant Instructions & Architecture Rules

## MULTI-AGENT WORK — READ FIRST
Harish runs Claude, ChatGPT/Codex and Gemini in parallel on this repo.
**Before your first commit, read `MULTI_AGENT_GIT_RULES.md` and `AGENT_STATUS.md`.**
In short: own branch `<agent>/<version>-<topic>`, never commit to `integrate/v1.9`
or `main`, tag `pre-`/`post-<agent>-<version>`, never commit `data/`,
`analyst/replay_data/`, `*.sqlite3` or `.env`, change config only via a narrow
patch script, merge only when Harish says so, and **never add a rule, gate or
limit he did not ask for.**

## IMMUTABLE FEATURE LOCK (STRICT RULE)
NEVER modify any file or function listed in `PROTECTED_MODULES.md` when building new features unless the user prompt explicitly contains `"UNLOCK [Module Name]"`.

The following core modules are protected under a 3-layer immutable lock and baseline manifest (`ict_smc_app/LOCKED_MANIFEST.json`):
1. **3-Chart Institutional PCR & OI Trinity Suite** (`pcr_trinity.py`, `PcrTrinitySuite.tsx`)
2. **Classic Writer Flow** (`OcLab.tsx` -> `WritersFlow`, `chain.py`)
3. **Smart OI Intelligence Suite** (`smart_oi.py`, `SmartOISuite.tsx`)
4. **PE − CE Institutional Matrix 2.0 & Classic PE-CE Table** (`pe_ce_matrix.py`, `PeceMatrix.tsx`, `OcLab.tsx`)
5. **Order Flow & CVD Layers** (`cvd_divergence.py`, `CvdAlertWidget.tsx`, `Chart.tsx` overlays: CVD, Stacked Nodes, OI Walls, Big Trades, Delta Detail)

Any change to these modules will fail `scripts/verify_locks.py` and `ict_smc_app/backend/tests/test_immutable_feature_lock.py`.

See `PROTECTED_MODULES.md` and `AGENTS.md` for complete rules.
