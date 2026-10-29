# AI Assistant Instructions & Architecture Rules

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
