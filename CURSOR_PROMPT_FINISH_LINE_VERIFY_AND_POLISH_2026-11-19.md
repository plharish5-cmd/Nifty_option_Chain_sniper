# Cursor Work Order — finish line: prove the fixes on 19 Nov + polish the last gaps

> gemini/v2.0.39-arm-persistence already contains BOTH root-cause fixes (opposite-close arm
> deletion AND restart persistence) plus ChatGPT's v242 fixes (it was cut from v242). This job:
> (1) PROVE the fixes catch 19 Nov's missed trades via per-opportunity replay, and (2) close the
> last code gaps. Branch gemini/v2.0.40-finish-line off gemini/v2.0.39-arm-persistence. Tags
> pre-/post-. Push. Do NOT merge. No protected modules. verify_locks 11/11. NO rule changes,
> NO real-money toggling, PAPER_ONLY.

```
PART 1 — PROVE IT (the confidence check the owner needs)
Replay 19 Nov 2026 for the 8 EMA PROTOTYPE against that day's recorded 1-min candles and the
settings in effect, and produce a per-opportunity report for EACH owner-marked chart opportunity
(the CE buys at ~10:15, ~10:30, ~13:00, ~14:00, ~15:00 and the morning PE). For each, state ONE of:
  - "WOULD TAKE" — armed, away seen, touch ±3 met, no filter block → entry fires (give the ts/price), OR
  - a precise reason it does NOT: "armed but price never retouched 8 EMA", "arm was lost (gap)",
    "no executable option quote at that minute", "expired crossover window", "blocked by <filter>".
Do the SAME for the Reversal lane's marked setups. Output a short table: time | side | verdict |
reason. This replays history read-only; it must NOT fabricate a fill where no quote existed.
Goal: show how many of the owner's marked trades the FIXED engine now catches, and give the honest
reason for any it still can't — no hand-waving "all fixed".

PART 2 — "BLOCKED" wording in Log-only (stop the false alarm)
Everywhere the cockpit/telemetry says "BLOCKED" while mode == log_only, change it to
"would block — trading still allowed" (and keep a true "BLOCKED" only when mode == block AND it
actually skipped). This is the wording that made the owner think trades were being blocked when
they were not. Fix it in the decision board, the verdict lines, and any status string.

PART 3 — visible reason for every early wait / rejection
Ensure every pre-entry wait and every rejection shows a plain-language reason in the cockpit +
audit log: "waiting for away", "waiting for touch", "no quote", "broker rejected: <DH-xxx reason>",
"arm resumed", "stale spot — paused". No silent waits.

PART 4 — 8510 unexpected stop: capture the cause
The desk was seen stopping unexpectedly with no established cause. Add crash/exit logging: wrap the
main loop so any unhandled exception, signal, or exit writes a timestamped reason + traceback to a
log (data/terminal8510/engine_exits.log) before dying, and a lightweight heartbeat/watchdog line so
an abnormal stop is distinguishable from a clean shutdown. Do NOT auto-restart; just make the cause
visible so the next stop is diagnosable. Investigate and note any cause you can already identify
(port conflict, OOM, unhandled exception path).

GUARDRAILS
- NO entry-rule / EMA / filter / exit changes. NO real-money toggling (stay PAPER_ONLY). Protected
  modules untouched; verify_locks 11/11. Config via patch only; preserve M terminal8510_config.json.
- Dhan Invalid IP (DH-905) and the 8600 systemctl restart are the OWNER's external steps — do not
  attempt to work around them in code; just keep surfacing them clearly.
- TESTS: the 19-Nov replay reporter (deterministic on recorded data); log-only never prints a bare
  "BLOCKED"; every wait/rejection has a reason string; engine-exit logger writes on exception.
  verify_locks 11/11; full pytest diffed against the ~824/56/7 baseline — no NEW failures.

DELIVERABLE
Commit on gemini/v2.0.40-finish-line, tags, push, AGENT_STATUS block. PASTE BACK the Part-1
per-opportunity table for 19 Nov (how many of the owner's marked trades the fixed engine now takes,
and the honest reason for any it doesn't). State what couldn't be verified (live Dhan/market).
Do not merge.

CONFIRM before coding: per-opportunity 19-Nov replay with honest per-trade verdicts (no fabricated
fills); log-only "BLOCKED"->"would block"; every wait/rejection has a visible reason; engine-exit
cause logging; no rule/real-money changes; v2.0.39 already has both root-cause fixes so this is
proof + polish, not re-fixing.
```

---

## For you, Harish
- **Run `gemini/v2.0.39-arm-persistence`** — it has every fix (ChatGPT's + Cursor's), in one branch.
  v2.0.40 above just adds the *proof* and the wording/logging polish on top.
- **The two things only you can do** are the real-money unblock: **Dhan IP allowlist (DH-905)** and
  **`sudo systemctl restart paperdesk-ict.service`** for 8600. No code reaches those.
- **Part 1 is the one that will settle your gut** — it replays your exact 19-Nov marked trades and
  tells you, honestly, how many the fixed engine now catches. If it's 4 of 5 with a real reason for
  the 5th, you'll know it's fixed. If it's 1 of 5, we keep digging — but with proof, not feeling.
- Then: **restart ONCE on v2.0.39/40, fix the Dhan IP, restart 8600, and leave it running a full
  paper day.** Watch "ARM RESUMED" in the log. That's how you finally see it trade clean.
```
