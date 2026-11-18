# Cursor Prompt — live "OI Filter Decision Board" (both sides debate, per strategy)

> Owner can't tell what the OI-filter radio buttons are doing or why no trade fired — it feels like
> a black box. Build a LIVE decision board, per strategy (Prototype AND Reversal), that shows both
> trade sides (BUY/CE vs SELL/PE), each filter's live vote, the armed state, and the current
> allow/block verdict — updating as OI changes. Branch gemini/v2.0.38-oi-decision-board off
> gemini/v2.0.36-clean-and-scope. Tags pre-/post-. Push. Do NOT merge. No protected modules.
> verify_locks 11/11. DISPLAY ONLY — no gating-logic change (the gate already exists; this
> visualizes it).

```
GOAL: make the OI-filter decision transparent and live, so the owner always sees WHAT is armed,
WHAT each filter says, and WHY a side is allowed or blocked — for both the Prototype and the
8 EMA Reversal, independently.

PANEL (one per strategy, on that strategy's cockpit card; Prototype reads scanner.ema8_prototype
.oi_filters, Reversal reads scanner.reversal.oi_filters — independent)
Top line — ARMED STATE + MODE:
  "8 EMA Prototype — armed: PE (waiting for touch)   ·   Mode: Log-only"
  armed state comes from the lane status (PE armed / CE armed / none / in-trade); if waiting, say
  what for (waiting for away / waiting for touch / no setup). Mode = log_only or block.
Two-column vote table — BUY (CE) vs SELL (PE):
  One row per ENABLED filter (writer_flow, price_oi, total_oi, fresh_oi, volume_oi, and the
  momentum S / price-confirm if on). Each row shows:
    - BUY(CE) cell: ✓ agrees / ✗ opposes / — neutral|stale  (a read that says CE agrees with BUY
      and opposes SELL; PE the reverse; NEUTRAL/stale = — for both)
    - SELL(PE) cell: the mirror.
    - live reading: the human value driving it, e.g. "Call 18.6Cr > Put 15.9Cr → bearish",
      "Call +0.3 / Put −0.2 Cr/min → bearish", "S −0.21 → PE", or "— stale / no data".
Verdict row — per side, under the current Rule (all_agree / any_agrees / majority):
    BUY(CE): 🟢 ALLOWED  or  🔴 BLOCKED by <filters that opposed>
    SELL(PE): same.
  In log_only, label it "(would-block — not actually blocking)"; in block, it's the real gate.
Plain-language line under the table:
    "Right now: SELL allowed · BUY blocked by Total OI, Fresh OI."

BEHAVIOUR
- Updates every eval tick (the 3s banner cadence). Values move as OI changes.
- Reads the per-read telemetry the backend already computes (direction, S, would-block, age). If the
  both-sides verdict isn't already exposed, enrich the lane status telemetry to include, per enabled
  filter: {direction, live_detail, vote_buy, vote_sell} and the combined {buy_verdict, buy_blockers,
  sell_verdict, sell_blockers}. DO NOT change the gating logic itself — derive the display from the
  same oi_switch_block inputs so the board and the actual gate can never disagree.
- Independent per strategy; the Prototype board reflects the Prototype's filters/armed state, the
  Reversal board its own.
- Stale/None reads show "— stale" and count as neither vote (fail-open), matching the gate.

GUARDRAILS
- DISPLAY ONLY: no change to oi_switch_block, entry, or gating. Keep filters' defaults log_only.
  No protected modules. verify_locks 11/11. Config via patch only if a new key is needed (backup first);
  preserve M terminal8510_config.json.
- TESTS: the board's buy/sell verdict MATCHES oi_switch_block's actual decision for the same inputs
  (board can never disagree with the real gate); armed-state line reflects lane status; stale read =
  — for both sides; present-key guard on the panel. verify_locks 11/11; full pytest diffed against
  the ~889/35/42 baseline — no new failures. Browser-check both cards: panel renders, updates live,
  0 console errors.

DELIVERABLE
Commit on gemini/v2.0.38-oi-decision-board, tags, push, AGENT_STATUS block. File list + run commands.
Browser-verify the board on both Prototype and Reversal cards. Do not merge.

CONFIRM before coding: live per-strategy board showing armed state + mode + per-filter BUY-vs-SELL
votes + per-side allow/block verdict with blockers named; derived from the SAME inputs as
oi_switch_block (board never disagrees with the gate); display only, no gating change; independent
Prototype vs Reversal; stale = — / fail-open; defaults stay log_only.
```

---

## For you
- After this, you're **never guessing again.** The board shows, live: *what the strategy is armed to*,
  *what each filter says for BUY and for SELL*, and *the verdict with the blocking filter named* —
  and it changes as the OI data moves, exactly like you asked ("it should keep changing as per OI
  data").
- It also answers today's "why no trade": the board would have shown *"PE allowed — armed, waiting
  for touch"* or *"PE blocked by Total OI"* at each setup.
- **It's display-only and can never disagree with the real gate** (built from the same inputs) — so
  what you see is exactly what the engine decides.
- Keep the filters in **log-only** while you watch the board for a few sessions — that way they show
  their opinion without blocking a single trade, and you learn what they do before trusting them.
  The backtest Cursor is running will tell you if/when to flip any to Block.
```
