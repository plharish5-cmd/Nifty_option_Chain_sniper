# 8 EMA Prototype + 8 EMA Reversal — execution audit (v2.40)

For Harish — **8510 live desk**, not the read-only preview on 8880.

## Applied for **tomorrow** (18 Nov 2026 night)

Patch: `scripts/patch_tomorrow_desk_ready_8510.py`  
Backup: `data/terminal8510/config_backups/pre_tomorrow_desk_20261118_225400.json`  
Verify: `python3 scripts/verify_tomorrow_desk_ready_8510.py` → **all PASS**

| Setting | Now |
|---------|-----|
| Prototype `enabled` | **true** |
| Prototype / Reversal route | **LIVE_ALLOWED** |
| `live_trade_enabled` | **true** (both lanes) |
| 8600 board FILTER sources | **cleared** (record only) |
| Lane OI | **log_only** |
| Touch cadence | **3s** |
| Cooldown / skip retry | **0** |
| Desk `execution_mode` | **PAPER_ONLY** until you arm LIVE |

Engine restart was run; live API showed Prototype **enabled**, state **SCANNING**.

---

## What blocked you **today** (config on disk, 18 Nov 2026)

| Check | Your value | Effect |
|--------|------------|--------|
| **`scanner.ema8_prototype.enabled`** | **`false`** | **Prototype cannot fire at all** (`desk_scope_8510` reads this flag). |
| **`execution_mode` (desk)** | `PAPER_ONLY` | No broker orders until master LIVE armed. |
| **Reversal `execution_mode`** | `PAPER_ONLY` | Reversal **never sends real money** even if “Real money” is checked. |
| **Prototype `live_trade_enabled`** | `true` | Meaningless until desk LIVE + route LIVE + **prototype enabled**. |
| **`desk8600_board` sources** | `tape_3m`, `writer_flow`, `high_conviction` = **FILTER** on **both** lanes | **Separate from** lane OI panel; can **skip** trades when sources disagree. |
| **Lane OI `mode`** | `log_only` | OI panel **does not block**; only logs. |
| **`bollinger_enabled`** | `false` | BB width gate should be off (code fix applied for Reversal). |
| **`e8_refresh_seconds`** | was **30** → patched to **3** | Touch scan cadence (restart engine). |

## Real money — **all three layers** (Prototype & Reversal)

1. **Desk**: Master **LIVE** (`execution_mode` BOTH + type **LIVE** + not kill-switch).
2. **Lane route**: `execution_mode` = **LIVE_ALLOWED** (not PAPER_ONLY).
3. **Lane opt-in**: **“Real money for this lane”** checked.

Prototype and Reversal are **independent lanes** (separate locks). Only **one open EMA lane position** at a time is allowed when live.

## Setup design (short)

### 8 EMA Prototype

- **When**: 15m 4/8 EMA regime; **dynamic 8 EMA** retouch on spot (default **3s** poll after patch).
- **Entry**: BUY CE (long bias) or BUY PE (short bias) at retouch within tolerance.
- **Exits (spot bookkeeping)**: Fixed **40/40** spot SL/target on 1m walk; optional **opposite 8 EMA close**; EOD **15:20**.
- **Re-entry**: Can re-touch same side / after SL if enabled (you have both **ON**).
- **Retouch requires away**: **OFF** (chart-friendly) — earlier touch in same bar can qualify.
- **Not the same as** Stage 2/3 or Reversal crossover logic.

### 8 EMA Reversal (4×8 crossover)

- **When**: After 4/8 **crossover**, price retouches **dynamic 8 EMA** within window (MJ Sir rules).
- **Exits**: Typically **20/20** spot SL/target (config); optional candle trail if enabled.
- **One trade per crossover** per touch window (not repeatable retouches like Prototype).
- **Bollinger**: When desk **Bollinger OFF**, BB width gate is now **not** applied to Reversal (fix).

## Filter map — what each control does

### Lane OI panel (Prototype / Reversal)

| Control | Meaning |
|---------|---------|
| **Mode: log_only** | OI never blocks orders; telemetry only. |
| **Mode: block** | Selected switches can **skip** the order when reads disagree. |
| **Rule: all_agree / any_agrees / majority** | How many ON switches must agree with trade direction. |
| **Per-switch (Writer, Price OI, Total OI, Fresh OI, Volume)** | Each ON switch is a vote from 8600 board reads. |
| **Neutral blocks** | NEUTRAL counts as opposing (stricter). |
| **Reversal “log only” override** | Forces observational even if mode says block. |

### **8600 Board** (separate card — easy to miss)

| Source mode | Meaning |
|-------------|---------|
| **FILTER** | Can **block** entries for lanes in `apply_to` (you have Prototype + Reversal). |
| **LOG** | Record only. |
| **OFF** | Ignored. |

**Chart-friendly trading** often sets these to **LOG** or disables board filtering — your JSON still has **three FILTER sources**.

### Optional Prototype filters (checkboxes)

| Filter | When ON |
|--------|---------|
| Ribbon | Blocks when 4 & 8 EMA too close (choppy). |
| EMA separation | Requires minimum EMA spread. |
| SuperTrend | Extra trend filter. |
| Slow market exact line | Stricter touch geometry. |

### Desk scope

Turns entire lanes on/off via `ema8_prototype.enabled` and `reversal.enabled`.

## Code fixes applied (this session)

1. **Reversal OI** — execution uses nested `{"oi_filters": ...}` so switches actually apply (display matches).
2. **Batch gate** — no longer drops 2nd candidate before retry / failed-order cleanup.
3. **Cooldown 0** — `0` means no 5m wait (was treated as 5).
4. **Reversal LIVE revamp path** — removed duplicate `bar_ci` (could crash before signals).
5. **BB width** — respects `bollinger_enabled` for Reversal.
6. **Lane lock** — pending reservation blocks same-lane double fill.
7. **Cadence** — `e8_refresh_seconds` → **3** (backup under `data/terminal8510/config_backups/`).

Tests: `analyst/test_ema_execution_audit_v240_8510.py` + OI board tests.

## Tomorrow checklist (before 9:15)

1. **8510 engine restart** after code + config changes.
2. Cockpit: **Enable 8 EMA Prototype** (saves `enabled: true`) — **critical**.
3. For real money: desk **LIVE** + Reversal route **LIVE_ALLOWED** + checkboxes per lane.
4. If you do **not** want 8600 board to veto trades: set board sources to **LOG** or narrow `apply_to`.
5. Watch cockpit **last skip reason** / `signal_observations` if a touch does not fire.

## What we still cannot prove from 18 Nov

Ledger showed **no Prototype/Reversal observations** that day while prototype was **disabled** — so chart-vs-engine attribution for “missed” touches is partial. After enabling + fixes, use the scanner log and signal_observations for same-day proof.