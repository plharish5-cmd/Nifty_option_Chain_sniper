# Standing Agent Rules for Nifty Scanner & AI Analyst

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

8. **Alert-only.** Nothing in this build places, modifies or cancels orders.

9. **IMMUTABLE FEATURE LOCK**: Core features verified working with StockMojo parity are documented and protected in `PROTECTED_MODULES.md` with baseline manifest `ict_smc_app/LOCKED_MANIFEST.json`. NEVER modify any file or function listed in `PROTECTED_MODULES.md` (Trinity 3-tabs, Classic Writer Flow, Smart OI, PE-CE Matrix 2.0, CVD Divergence, Stacked Nodes, OI Walls, Big Trades) unless the user prompt explicitly contains `UNLOCK [Module Name]`.

