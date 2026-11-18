# Cursor Prompt — add the Reversal's own OI-filter panel to the cockpit (independent)

> Cockpit-only gap fix. The Reversal's OI filters already run in the backend
> (scanner.reversal.oi_filters, log_only), but the Reversal card has no UI for them — only the
> Prototype card shows an "8600 OI Filters" panel. Add an INDEPENDENT panel on the Reversal card.
> Branch gemini/v2.0.35-reversal-oi-panel off gemini/v2.0.34-finish-and-enable. Tags pre-/post-.
> Push. Do NOT merge. No protected modules. verify_locks 11/11.

```
GOAL: give the 8 EMA Reversal its own OI-filter panel on its cockpit card, reading/writing
scanner.reversal.oi_filters — fully INDEPENDENT of the Prototype's scanner.ema8_prototype.oi_filters.
This is DISPLAY + CONTROL only. The backend already evaluates the Reversal OI gate; do NOT change
backend gating logic.

UI (Algorithmic_Trading_Workspace_8510.html, inside the 8 EMA Reversal card — the card that already
has revPreArm / revSep / revTrail / TF / Route)
- Add an "8600 OI Filters (optional)" panel mirroring the Prototype's, with its OWN element IDs
  (prefix rev..., e.g. revOiModeLog / revOiModeBlock, revRuleSel, revNeutralBlocks, revWriterFlow,
  revPriceOi, revTotalOi, revFreshOi, revVolumeOi). Rows:
    Mode: Log only / Block      Rule: All agree / Any agrees / Majority      Neutral blocks
    Classic Writer Flow · Price vs OI (15m ATM) · Cumulative Total OI · Fresh Change in OI ·
    Volume-backed OI
  Plus the momentum / price-confirm state line for the Reversal lane (S · bias · CumD · price
  agrees/diverges · would-block), same as the Prototype shows.
- Bind every control to scanner.reversal.oi_filters (NOT the Prototype's). Reading and saving must
  target the Reversal config path only; changing a Reversal toggle must NOT touch
  scanner.ema8_prototype.oi_filters, and vice-versa.
- Use the v2.0.23 present-key guard: set each control only when its key is present in the Reversal
  telemetry (`if (!(key in revCfg)) return;`), so partial telemetry never clobbers a toggle.
- Read the live Reversal OI telemetry (the would-block verdict + per-read direction/age) from the
  Reversal lane's status, not the Prototype's. Show "— stale" when a read is stale.
- Save via the existing config-save path, writing only the scanner.reversal.oi_filters subtree.

GUARDRAILS
- Independence: a test/manual check that toggling a Reversal OI filter changes ONLY
  scanner.reversal.oi_filters and leaves scanner.ema8_prototype.oi_filters untouched (and vice-versa).
- No backend gating change (the Reversal OI gate already exists from v2.0.34). No protected modules.
  verify_locks 11/11. Config via patch script if any new keys (back up first); preserve
  M terminal8510_config.json.
- Defaults stay as patched: Reversal filters log_only (not blocking). This panel just lets the owner
  see and change them from the cockpit.
- TESTS / checks: Reversal panel renders; toggles persist to scanner.reversal.oi_filters and reload
  correctly; present-key guard holds under partial telemetry; Prototype panel unaffected; 0 console
  errors. verify_locks 11/11; full pytest diffed against the ~889/35/42 baseline — no new failures.

DELIVERABLE
Commit on gemini/v2.0.35-reversal-oi-panel, tags, push, AGENT_STATUS block. File list + run commands.
Browser-check the Reversal card (panel shows, toggles persist, independent from Prototype, 0 console
errors). Do not merge.

CONFIRM before coding: Reversal gets its OWN OI-filter panel bound to scanner.reversal.oi_filters,
independent from the Prototype's; display + control only (no backend gating change); present-key
guard; defaults stay log_only; Prototype panel untouched.
```

---

## For you
- This is the **last cockpit gap** — after it, you control the Reversal's OI filters from its own
  card, completely separate from the Prototype's. You can run, say, Fresh+Total OI on the Prototype
  and Writer Flow only on the Reversal, independently.
- It's **UI only** — the filters already run in the backend; this just surfaces and controls them.
  Nothing about trading changes until you flip a Reversal filter to "block" yourself (after backtest).
- Keep the Reversal filters in **log-only** until the sweep covers the Reversal on held-out days —
  same discipline as the Prototype.
```
