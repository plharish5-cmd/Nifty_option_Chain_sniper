# AGENT STATUS — WHO BUILT WHAT, AND WHERE

Single source of truth for parallel AI work. **Append a block, never rewrite
another agent's block.** Rules: `MULTI_AGENT_GIT_RULES.md`.

Trunk: **`integrate/v1.9`**

| branch | agent | state | merged into trunk? |
|---|---|---|---|
| `claude/v1.9.4-parity-verifier` | Claude | done, `3a12b09` | merged into `claude/v1.9.4b-live-switch` |
| `chatgpt/v1.9.4-ict-signals` | ChatGPT | done, `952bf32` | merged into `claude/v1.9.4b-live-switch` |
| `claude/v1.9.4b-live-switch` | Claude | **done, `d374e06`**, tag `post-claude-v1.9.4c` | **NO — awaiting Harish** |
| `chatgpt/v1.9.4d` | ChatGPT | done, tag `post-chatgpt-v1.9.4d-final` | **NO — awaiting Harish** |
| `claude/v1.9.4-safety-lock` | Claude | **BROKEN, `2d116b7` — DELETE** | never merge |

---

## 2026-11-07 · Claude · `claude/v1.9.4b-live-switch` @ `d374e06`

Tag: `post-claude-v1.9.4c`. Contains ChatGPT's `v1.9.4-ict-signals` already
merged in, so **branch from this, not from `integrate/v1.9`**, or ChatGPT's own
v1.9.4 work is lost.

**Real-money arming, at Harish's instruction**
- `live_validation_status: USER_CONTROLLED` (was `NO_GO`)
- `live_dry_run: false`, `dry_run_exit_min_orders: 0` — the forced dry-run gate is removed
- `max_live_trades_per_day: 0`, `max_live_loss_rupees_per_day: 0` — **0 means unlimited**
- `live_entry_window: 09:15–15:20`; blank either bound to disable the check
- Kept at his request: typed `LIVE` confirmation, parallel paper logging, STOP LIVE button, Dhan token check, 15:20 EOD square-off
- `LIVE_ARMABLE_STATUSES = ('APPROVED','USER_CONTROLLED')` — one tuple read by both `execution_mode` and `configure_real_money`, so they cannot drift apart

**Bugs fixed in the live code**
- `atm_strike` rounded to **100** points; NIFTY strikes are every **50**, so half the chain was unreachable and the desk bought strikes up to 25 points off ATM
- `ema_proximity_engine.load_candles_for_date` fabricated **375 flat candles at 24350.0** for any unrecorded date, manufacturing fake Tier 2/3 signals. Now returns `NO_DATA`
- `set_off_hours_paper_testing` **persisted** to config; a test run left it `true`, so the scanner traded at 02:49–04:15 IST. Now session-only and expires in 60 min
- `backtest_option_prices.py` computed charges on a flat **₹200** entry premium; now uses the measured Step-4 mean
- `(~₹200)` strike-matrix labels renamed to `(ATM-100)` / `(ATM+100)` — they read as a fixed entry price

**New**
- `analyst/verify_ict_smc_parity_8510.py` — ChatGPT's parity claim verified: **9,059 rows × 17 fields, zero mismatches**
- Matrix Scalp Tier 3 (`matrix_scalp` config, **ships disabled**) — prospective paper recording
- `scripts/clean_junk_paper_trades_8510.py` — removes off-hours/duplicate/zero-duration rows, dry-run by default
- `scripts/test_scalp_tier_spot_8510.py`, `scripts/parse_proximity_dump_8510.py`
- `/api/live/{status,arm,stop}`, `/api/matrix_scalp/config`

**Baseline:** `21 failed / 419 passed / 12 skipped`. Locks 11/11. Parity 100%.
The 21 are environmental — `images.dhan.co` is unreachable from a sandbox, so
every test needing the instrument master fails there. On the desk, expect far
fewer. ChatGPT's own fixture-isolated baseline was `7 failed / 388 passed`,
which is **not comparable** to this one.

**Ledger finding (6–7 Nov):** the paper ledger held 248 positions and
₹246,334 of which **₹240,504 was clone clusters** — one exit price recorded 31
times across nine overnight hours. 5 Nov entries were the fabricated ₹200
generator (a 23800 CE cannot cost ₹200 when spot is 24340; intrinsic alone is
~540). **Only 6 Nov survives: 10 trades, 7 wins, +₹10,177.93 paper.**

**Open, needs Harish:** `super_trail.partial_exit_enabled` is `false` here, was
`true` in his live tree. StockMojo JWT still in 6 commits of history, needs
rotation at source. `claude/v1.9.4-safety-lock` should be deleted.

---

## 2026-11-07 · ChatGPT · `chatgpt/v1.9.4d`

Tags: `post-chatgpt-v1.9.4d`, `post-chatgpt-v1.9.4d-final`. Branched from
`claude/v1.9.4b-live-switch` @ `615fa68`, so it does **not** contain Claude's
later commits `f53e59d` → `d374e06`.

- Removed the embedded `allTrades` / `sandbox30Trades` arrays, the calibrated
  date dictionary, synthesized candles, fabricated futures offsets, bullish
  trend defaults and canned copy/speech P&L
- Historical views now query real closed `PAPER_ONLY` ledger rows
- `LONG_CE_ITM` set `false`; narrow config patch via `analyst/apply_v194d_config.py`
- Test isolation: private config/ledger/market-DB fixtures, external HTTP blocked
- **Ran the pre-registered ICT/SMC study: largest |t| = 0.828 against a corrected
  threshold of 4.344 (19 session blocks, not the asymptotic 3.54). Zero results
  passed.** Writer Stack-up: NOT BUILT. Corridor/Radar: context only.
- 0 dry-run depth rows, so execution cost remains unmeasured
- Its baseline: `11 failed / 397 passed` full suite; it did **not** claim green

**Not in its ZIP — carry these over from Claude's branch:** the 50-point strike
grid fix, the fabricated-candle removal, and the off-hours persistence fix
(`fb91414`), which is what caused the 2 AM trades.

---

## TEMPLATE — copy this block

```
## YYYY-MM-DD · <agent> · `<branch>` @ `<sha>`
Tag: pre-<agent>-<ver> → post-<agent>-<ver>
Branched from: <branch>@<sha>   (so it does/does not contain: ...)

What changed:
- ...
Config keys touched (and the patch script used):
- ...
Baseline: N failed / N passed. Locks __/11. Parity __%.
Compared against: <previous agent's numbers>
Open / needs Harish:
- ...
```
