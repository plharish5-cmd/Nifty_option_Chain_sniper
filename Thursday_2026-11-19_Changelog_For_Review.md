# Thursday 19 Nov 2026 — changelog for independent review (8 EMA desk, NOT ATM)

The signal is the **8 EMA line** (not ATM). Everything below is what was asked of Cursor today and what
got implemented, with git versions, so ChatGPT can review independently.

## Implemented today (verified present on the remote)
| # | Change asked | Branch | Tip | Tag | Deliverable files | Status |
|---|---|---|---|---|---|---|
| 1 | Persist arm state across restarts (Prototype + Reversal) — end the 5-hour arm holes | `gemini/v2.0.39-arm-persistence` | `4a14e2a` 18:32 | `post-gemini-v2.0.39-arm-persistence` | `analyst/ema_arm_persistence_8510.py`, `analyst/ema_lane_lock_8510.py`, `analyst/test_arm_persistence_8510.py` | ✅ |
| 2 | Finish-line: 19-Nov per-trade replay proof; log-only "BLOCKED" wording; engine exit-cause log | `gemini/v2.0.40-finish-line` | `9983e53` 19:05 | `post-gemini-v2.0.40-finish-line` | `analyst/engine_exit_log_8510.py`, `analyst/replay_marked_opportunities_8510.py`, `analyst/reports/MARKED_OPP_REPLAY_2026-11-19.md`, tests | ✅ (replay 4/9 would-take) |
| 3 | Rebuild arm from Dhan candles on every app open (Prototype + Reversal) | `gemini/v2.0.41-arm-reconstruct` | `0fcda9a` 19:10 | `post-gemini-v2.0.41-arm-reconstruct` | `analyst/arm_reconstruct_8510.py`, `analyst/test_arm_reconstruct_8510.py` | ✅ |

## ChatGPT's own fixes today (already folded into the chain above)
- `404dd47` 14:38 — Fix Prototype opposite-arm loss + recorded OI baseline; distinguish local timeouts
- `bdf3d1a` 15:01 — Keep EMA quote refresh running without an open browser
- `ed87b1a` 15:32 — Prefer continuous market history; offload blocking 8600 flow wrapper
- `176c836` 15:37 — v242 host verification, regression baseline, privileged restart handoff

## Lineage
`v2.0.41 (0fcda9a) → v2.0.40 (9983e53) → v2.0.39 (4a14e2a) → v242 (176c836)` — linear.
**Run `gemini/v2.0.41-arm-reconstruct`: it contains everything implemented today.**

## Asked but NOT built yet (prompt only)
| Change | Intended branch | Status |
|---|---|---|
| Lane auto-release (orphaned lock), intraday stuck-open exit recovery, live Missed-Trade Watchdog, optional 1H Flagship reconstruct (S2′) | `gemini/v2.0.42-selfaudit` | ❌ prompt only — `claude/Cursor_Prompt_Lane_Autorelease_And_Miss_Watchdog_2026-11-19.md` |

## Owner-only (no code fixes these)
- **DH-905 Invalid IP** — add machine IP to Dhan API allowlist (unblocks real fills).
- **8600 service** — `sudo systemctl restart paperdesk-ict.service`.
- **Stability** — avoid mid-session redeploys; let one build run a full day.

## Honest caveats on the replay proof
- 4/9 "would take"; the 5 "no" are claimed legitimate (3 mid-hold of an open CE, 2 no-touch in window) —
  ChatGPT should re-derive, not trust.
- Replay is spot-only, log_only OI, assumes continuous run — it proves ENTRY logic, not
  persistence/reconstruct (those are unit-tested separately).
