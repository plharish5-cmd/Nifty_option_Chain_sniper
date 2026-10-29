# PROTECTED_MODULES.md — Architectural Lock & Immutable Feature Specification

> [!IMPORTANT]
> **IMMUTABLE FEATURE LOCK RULE**:
> NEVER modify any file or function listed in `PROTECTED_MODULES.md` when building new features unless the user prompt explicitly contains `"UNLOCK [Module Name]"`.
> Any modification to these protected files without an explicit UNLOCK will fail regression lock tests and CI validation (`scripts/verify_locks.py`).

---

## 1. Locked & Encapsulated Modules

The following 5 core features are verified working with 100% precision and StockMojo parity. Their mathematical logic, API payloads, and UI visualizations are **FROZEN**:

### 1. 3-Chart Institutional PCR & OI Trinity Suite
- **Files**:
  - `ict_smc_app/backend/app/engine/pcr_trinity.py`
  - `ict_smc_app/frontend/src/components/PcrTrinitySuite.tsx`
- **API Endpoint**: `/api/chain/trinity`
- **Sub-Tabs**:
  1. `1. Put-Call Ratio (PCR-OI vs FUT)`: Dual-axis NIFTY Futures line + PCR-OI curve with regime bands.
  2. `2. Fresh Change in OI (PE Δ vs CE Δ — Today's Intraday Battle Since 09:15)`: Real-time net writing accumulation and delta gap.
  3. `3. Cumulative Total OI (PE Total vs CE Total — Positional + Intraday Walls)`: Absolute call and put walls with ceiling/floor indicators.
- **Visual & UI Contract**:
  - StockMojo colors: `Call = Green (#22c55e)`, `Put = Red (#ef4444)`, `PE-CE Gap = Purple (#a855f7)`.
  - Mouse-Over Trader Guidance banner with real-time interpretation.
  - Invariants: `total_oi_gap = put_oi - call_oi`, `net_oi_chg = put_oi_chg - call_oi_chg`, `pcr_oi = put_oi / call_oi`.

---

### 2. Classic Writer Flow
- **Files**:
  - `ict_smc_app/backend/app/market/chain.py` (Writers flow calculations)
  - `ict_smc_app/frontend/src/components/OcLab.tsx` (`WritersFlow` component)
- **Visual & Layout Contract**:
  - 3-pane synchronized layout: Candlestick price chart + Fresh Call/Put OI lines + 3m `flow Δput - Δcall` histogram.
  - 30-minute gold balance oscillator overlay.
  - Interactive hover coach banner providing instant trader guidance.

---

### 3. Smart OI Intelligence Suite
- **Files**:
  - `ict_smc_app/backend/app/engine/smart_oi.py`
  - `ict_smc_app/frontend/src/components/SmartOISuite.tsx`
- **API Endpoint**: `/api/chain/smart_oi` (and alias `/api/smart_oi`)
- **StockMojo 4-Panel Parity Layout**:
  - **Left 3 synced panes**:
    1. NIFTY Candlesticks with Cyan Smart OI toggle line.
    2. Smart OI Net Money Flow diverging histogram (`Green #34d399` / `Red #f87171`).
    3. Option Volume Activity (`Call Vol #22c55e vs Put Vol #ef4444`).
  - **Right 2 stacked charts**:
    1. Dual-axis `Put OI / Call OI / Purple PE-CE Chg Day`.
    2. `OI PCR vs Vol PCR` trend comparison.
- **Visual & UI Contract**:
  - Indian numbering formatters: `Lakhs (L)` and `Crores (Cr)` (`formatInrUnits`).
  - Interactive Mouse-Over Guidance (`getOptionVolumeGuidance`).

---

### 4. PE − CE Institutional Matrix 2.0 & Classic PE-CE Table
- **Files**:
  - `ict_smc_app/backend/app/engine/pe_ce_matrix.py`
  - `ict_smc_app/frontend/src/components/PeceMatrix.tsx`
  - `ict_smc_app/frontend/src/components/OcLab.tsx` (Matrix view container)
- **API Endpoint**: `/api/chain/pece-matrix`
- **Features & Contracts**:
  - 6-Stage Action Verdicts (`S0 WAIT`, `Stage 1 Bearish Breakdown (PE)`, `Stage 2 Pullback Sell (PE)`, `Stage 3 Bearish Acceleration (Add PE)`, `Stage 4 Hold PE`, `Stage 5 Short Covering / Book PE`, and Bullish counterparts).
  - Short-Covering Bounce & Slope-Flip detection.
  - 0-DTE Expiry Cockpit telemetry.
  - Center-zero diverging bar charts with equidistant strike ladders.

---

### 5. Order Flow & CVD Layers
- **Files**:
  - `ict_smc_app/backend/app/engine/cvd_divergence.py`
  - `ict_smc_app/frontend/src/components/CvdAlertWidget.tsx`
  - `ict_smc_app/frontend/src/components/Chart.tsx` (Overlay layers)
- **API Endpoint**: `/api/desk/cvd-divergence`
- **Layers & Engines**:
  - CVD & CVD vs VWAP Divergence Engine (`CVD_BEARISH_DIV`, `CVD_BULLISH_DIV`, `CVD_EXHAUSTION`).
  - Stacked Nodes Primitive (`StackedNodesPrimitive`).
  - Institutional OI Walls.
  - Big Trades Bubbles (`bubbles`).
  - All Delta Detail markers (`ABS BUY`, `ABS SELL`, `Δ SURGE`, `CVD EXH`) + Mouse-Over Delta Guidance tooltips.
- **Open Extensibility Note**:
  - Footprint cell rendering (`FP cells`) remains open for future refinement without modifying or impacting the locked layers above.

---

## 2. Integrity Verification

A baseline SHA-256 manifest is maintained in `ict_smc_app/LOCKED_MANIFEST.json`.
To verify that no locked files have been tampered with or modified:
```bash
python scripts/verify_locks.py
```

Automated contract and regression lock tests run under:
```bash
./ict_smc_app/backend/.venv/bin/pytest ict_smc_app/backend/tests/test_immutable_feature_lock.py
```

To unlock any module for intentional modification, the developer must explicitly state `"UNLOCK [Module Name]"` in their request, after which the baseline manifest in `ict_smc_app/LOCKED_MANIFEST.json` can be updated.
