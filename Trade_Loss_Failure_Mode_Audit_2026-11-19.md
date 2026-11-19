# Trade-Loss Failure Mode Audit (FMEA) — 8510 desk, all setups

Purpose: enumerate EVERY point in a trade's life where a genuine setup can die silently, so misses are
designed out as a class — not patched one at a time after the owner catches them. Setups covered:
8 EMA Prototype (15m), 8 EMA Reversal (5m/15m), 1H Flagship.

Legend: ✅ fixed · ⚠️ needs verify · 🔴 open/high-severity · 👤 owner (external, no code)

## Data / EMA layer
| ID | Failure point | Effect | Status |
|----|---------------|--------|--------|
| D1 | Stale/gapped spot feed pauses touch eval | touches not seen during gap | ✅ v2.0.39 — pause ≠ drop arm |
| D2 | Backfilled EMA drifts from live EMA after reconstruct | arms on a wrong line | ✅ v2.0.41 — epsilon assert vs live brain |
| D3 | Clock/TZ skew on 15m/1H bar close & 09:15 OI baseline | bar closes wrong minute / baseline off | ⚠️ baseline fixed v242; **bar boundary IST to verify (C2)** |
| D4 | Forming-bar / 1-min candle ambiguity | partial bar treated as closed | ✅ `_drop_forming` present |

## State / Arm layer
| ID | Failure point | Effect | Status |
|----|---------------|--------|--------|
| S1 | Arm lost on engine restart | 5-hour arm holes, misses | ✅ v2.0.39 persistence |
| S2 | Arm not rebuilt on fresh mid-session open / crash-no-save | opens un-armed, misses genuine setup | ✅ v2.0.41 reconstruct-from-Dhan |
| S3 | Opposite 15m close deletes newly-armed opposite side | new side never arms | ✅ v242 |
| S4 | **Orphaned lane-lock reservation after crash mid-entry** | **desk takes ZERO trades all day, looks alive** | 🔴 open → v2.0.42 Part A |
| S5 | SL-blocked side never clears | one side dead until opposite close | ⚠️ by design — verify it clears on opposite close |
| S6 | Previous-day arm bleeding into new session day | wrong-day arm | ✅ day reset present (reconstruct must respect it) |
| S7 | Older crossover stays eligible after a newer opposite | wrong direction/reason | ✅ v242 |

## Trigger / Entry layer
| ID | Failure point | Effect | Status |
|----|---------------|--------|--------|
| T1 | Spot leaps across ±3 band BETWEEN 3s ticks | fast touch skipped live | ⚠️ within-bar low/high OK; **tick-to-tick to verify (C1)** |
| T2 | No option quote at touch minute → entry abandoned | qualified touch produces no order | ⚠️ v2.0.40 shows reason; **retry-while-touch-holds to confirm (B5)** |
| T3 | Strike/contract selection fails (expiry roll, illiquid) | no order | ⚠️ verify |
| T4 | Broker rejection DH-905 Invalid IP | real orders rejected | 👤 Dhan allowlist |
| T5 | Cooldown / market-open guard wrongly blocking | valid entries blocked | ✅ cooldown 0, guards corrected |

## Exit / Re-entry layer
| ID | Failure point | Effect | Status |
|----|---------------|--------|--------|
| X1 | **Exit missed (feed gap at exit) → position stuck "open"** | **lane locked → everything blocked rest of day** | 🔴 open → v2.0.42 Part A |
| X2 | Re-entry after profit doesn't re-arm same side | misses continuation | ⚠️ verify |
| X3 | 15:20 square-off doesn't fire | stuck open overnight | ✅ rule present; recovery added in Part A |
| X4 | Stop-loss not firing on option leg | runaway loss | ⚠️ `test_live_exit_fixes` exists — verify coverage |

## Process / Infra / Observability layer
| ID | Failure point | Effect | Status |
|----|---------------|--------|--------|
| P1 | Engine stops silently | desk dead, no trades | ✅ exit-cause logging v2.0.40 (no auto-restart) |
| P2 | No watchdog / auto-restart | dead desk stays dead | ⚠️ exit logged; auto-restart is owner/systemd choice |
| P3 | **No live reconciliation — app never reports a qualified-but-untaken setup** | **owner is the manual miss-detector every day** | 🔴 open → v2.0.42 Part B (Missed-Trade Watchdog) |
| P4 | No alert when armed-but-idle for N minutes | can't tell idle-alive from dead | 🔴 open → v2.0.42 Part B heartbeat |

## Owner-only (no code reaches these)
- **DH-905 Invalid IP** — add machine IP to Dhan API allowlist (unblocks real fills).
- **8600 service** — `sudo systemctl restart paperdesk-ict.service`.
- **Stability** — avoid redeploying mid-session; let one build run a full day.

## Fix stack (branch order)
1. v2.0.39 — arm persistence (resume saved arm) ✅
2. v2.0.40 — 19-Nov per-trade replay proof + wording/exit-cause logging (in progress)
3. v2.0.41 — rebuild arm from Dhan candles on every open (reconstruct) — definitive arm fix
4. v2.0.42 — lane auto-release + exit recovery + Missed-Trade Watchdog (self-audit) — ends the daily manual catching
