# RIBBON SCALP — Feature lock (5th strategy)

**Locked:** 2026-11-09 by desk instruction: `YES LOCK RIBBON`

## Scope
- **Ribbon Scalp** is the product name for `matrix_scalp` in `terminal8510_config.json`.
- **Observational → paper only** until 30+ sessions of graded paper are reviewed.
- **Does not** change Stage 2 / 3 / Crossover / FLAGSHIP 15m logic.
- **Uses** `analyst/ema_proximity_engine.py` 1m matrix; entries ride the same ledger as other algo paper.

## Signal grades (computed at emit time)
| Grade | Requirements |
|-------|----------------|
| **A+** | Tier 3 + matching STAGE_2 bounce / STAGE_3 reject (or 1H S/R) + MTF stack agree + fresh 4/8 reversal cross (this bar or prior 2×1m) |
| **A** | Tier 3 + matching S/R label + MTF agree (15m + 1m stack; 1H not opposite) |
| **B** | Tier 3 + direction from S/R + no whipsaw (weaker / log & optional quarter size) |

Config `min_grade` gates emission: `B` < `A` < `A+` (recommended paper: **A**, promotion study: **A+** only).

## Execution (paper)
- Spot-defined target/stop in config; premium via measured deltas (0.59 / 0.67).
- Optional `use_wick_micro_sl` tightens stop from signal bar wick ±5 pts (bounded).
- `strategy_id`: `ribbon_scalp_long` | `ribbon_scalp_short` (query also accepts legacy `matrix_scalp_%`).

## Unlock
Modify graded rules or real-money routing only when the user prompt contains **`UNLOCK RIBBON SCALP`**.
