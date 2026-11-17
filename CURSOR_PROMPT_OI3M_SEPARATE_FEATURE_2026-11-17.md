# Cursor Prompt — oi_3m gate + 8600 context panel as a SEPARATE feature

> Paste the box into Cursor (Gemini). This SUPERSEDES
> `CURSOR_PROMPT_8600_OI3M_GATE_AND_VIEWS_DISPLAY_2026-11-17.md` — build it as a **separate
> feature** (own files, own config block, own branch), NOT bolted into the existing oi_filters.
> Read `OI_Momentum_Spec_v2_Normalized_2026-11-17.md` first; this reuses its maths by import.

---

```
ROLE
Edit the 8510 Algo Terminal on a NEW branch gemini/v2.0.26-oi3m-separate-feature, cut from
gemini/v2.0.24-proto-oi-filters (so it carries OI Momentum v2). Tags pre-/post-. Co-Authored-By.
Do not merge. Do not touch PROTECTED MODULES. verify_locks.py must stay PASS 11/11.

GOAL — a SELF-CONTAINED feature, default OFF, that adds:
(1) oi_3m — a 3-minute OI-momentum GATE (independent of the 5 oi_filters switches).
(2) an 8600 CONTEXT PANEL — display-only reads that can NEVER gate a trade.
Isolation is the point: new files, a new config block, a new branch. If it misbehaves, one
`enabled: false` disables it and OI Momentum v2 is completely untouched. Deleting the 2 new
files + the 1 config block must fully remove it with zero changes to v2.

HARD ISOLATION RULES
- New code lives in NEW files: analyst/oi_3m_gate_8510.py, analyst/oi_context_panel_8510.py,
  analyst/test_oi_3m_separate_8510.py.
- Do NOT edit chain_reads_8510.py except, if strictly needed, to EXPORT the existing momentum
  helper so the new file can import it. Do NOT copy-paste its maths.
- REUSE the real v2 function by import — use its ACTUAL name, signature and return shape as it
  exists in chain_reads_8510.py today (open the file and check; do NOT assume it is
  `momentum_direction_from_significance(S, sig_min, sig_extreme)`). If the helper is not cleanly
  importable (e.g. it is inlined), extract the smallest pure function and import that, leaving v2
  behaviour byte-for-byte identical.
- New config block is scanner.oi_3m_feature — NOT inside scanner.ema8_prototype.oi_filters.

CONFIG (new block; via a new patch script scripts/patch_add_oi_3m_feature_8510.py, backup first):
  "oi_3m_feature": {
    "enabled": false,
    "gate":   { "enabled": false, "window_min": 3, "sig_min": 0.15, "sig_extreme": 0.30,
                "mode": "log_only" },
    "context_panel": { "enabled": false,
      "show": ["pcr_zone","walls","day_oi","pcr_futures","ticket","high_conviction",
               "pcr_trinity","smart_oi","pe_ce_matrix","cvd"] }
  }
  - enabled:false = whole feature off. gate.enabled:false = gate off even if feature on.
  - mode:"log_only" = compute + record "would block", NEVER blocks. "block" = actually skips
    the entry (same setup-release path as the other gates). Default log_only.

oi_3m GATE — maths (reuse v2, 3-min window)
- Same normalized-S maths as OI Momentum v2 (Option A: S = dD/T over a fixed window, NO division
  by window_min; all OI in LAKHS via the existing _to_lakhs; window found BY TIMESTAMP with a
  ±2 min tolerance band; fail-OPEN → direction None when no snapshot in band, T=0, or <2 points),
  but with window_min = 3. Thresholds 0.15 / 0.30. B = D/T computed for DISPLAY ONLY — never
  gates. Reuse the real v2 direction helper by import.
- Gate decision (pure, no I/O, never raises): block only when feature.enabled AND gate.enabled
  AND mode=="block" AND the oi_3m direction OPPOSES the touch side (CE touch blocked by PE,
  PE touch blocked by CE). direction None or NEUTRAL → NEVER block. B → NEVER block.
- Honest behaviour note (put in the module docstring): the 3-min window measures only the last
  ~3 minutes, so it STAYS NEUTRAL on slow drift and only fires on a sharp recent burst. It is a
  stricter, noise-resistant CONFIRM, and it typically fires a minute AFTER the 10-min S, not
  before. Do not describe it as an early-warning. (Validated on 17 Nov: 10-min S STRONG at 13:08;
  3-min NEUTRAL at 13:08 (S −0.08), STRONG only at 13:09 (S −0.16).)

8600 CONTEXT PANEL — display-only (analyst/oi_context_panel_8510.py)
- Build a read-only list from the 8600 board / protected-module verdicts ALREADY computed:
  pcr_zone, walls, day_oi, pcr_futures, ticket, high_conviction, and the richer views'
  verdicts pcr_trinity, smart_oi, pe_ce_matrix, cvd. Each → {label, value, direction} for the
  cockpit. Stale/missing → "—".
- CRITICAL: no function in this file may return a block/allow decision. These reads must NEVER
  enter oi_switch_block, the oi_3m gate, or any entry decision. PROTECTED MODULES are read-only:
  consume their public verdicts, never edit them. If a verdict isn't surfaced, add a read-only
  accessor to its public output; never touch the locked module. verify_locks 11/11.

WIRING
- Live worker: when oi_3m_feature.enabled, compute the oi_3m result each eval from the recorded
  chain snapshot series (same source v2 uses) and pass it to the gate alongside the touch side.
  In log_only, record the would-block verdict to telemetry/log; still take the trade. In block,
  skip the entry via the existing setup-release path. Independent of the oi_filters gate — the
  overall decision is: existing gates OR oi_3m gate (when both enabled).
- **Missing-config safety:** if the scanner.oi_3m_feature block is absent entirely, treat it as
  enabled:false and do nothing — never raise a KeyError. Read every key with a default so a
  terminal that never ran the patch script behaves exactly as before this feature.
- Backtest: apply the same oi_3m gate in the Prototype backtest when snapshots exist; a spec flag
  turns the feature/gate on so its effect on held-out points can be measured. Fail-OPEN on no data.
- Cockpit: a new panel "OI 3m + 8600 Context" placed BELOW and visually separate from the 8600
  OI Filters block. Show the oi_3m line (`3m S +0.21 · bias +0.42 · CumD +6L → direction`) and
  the context rows. Toggles for feature/gate/mode persist to scanner.oi_3m_feature. Present-key guard.

TESTS (analyst/test_oi_3m_separate_8510.py)
- 3-min CE/PE/NEUTRAL at thresholds 0.15/0.30; insufficient 3-min history / no snapshot in ±2min
  band / T=0 → direction None (fail OPEN); feature OFF → should_block False always; gate OFF →
  False; mode log_only → False but logs direction; mode block + opposing → True; + agreeing →
  False; B not gating (B=+0.42, S=−0.20 → PE allowed); context panel present → oi_3m/gate output
  identical with vs without it (display can't leak into gating).
- Run verify_locks.py (11/11) and PYTHONPATH=. pytest analyst -q --continue-on-collection-errors;
  diff against the handover baseline (~826–831). Browser: panel shows, toggles persist, gate
  unaffected by context, 0 console errors.

DELIVERABLE
Commit on gemini/v2.0.26-oi3m-separate-feature, tags, push, AGENT_STATUS.md block. File list +
run commands. State what couldn't be verified (no live 8600 feed / broker in sandbox). Do not merge.

CONFIRM before coding: new files + new scanner.oi_3m_feature block + new branch (NOT inside
oi_filters, NOT editing chain_reads maths beyond an export); reuse the REAL v2 helper by its
actual signature; oi_3m default OFF / log_only, fail-OPEN, B never gates, 3-min is a stricter
confirm (fires ~1 min after the 10-min, not before); context panel display-only, protected
modules read-only (verify_locks 11/11).
```

---

## Notes for you
- **Why separate is worth it:** OI Momentum v2 is verified and working. This new feature is
  experimental (does a 3-min burst filter actually add points?). Keeping it in its own files +
  config + branch means a bug here can never break v2, and you can delete it cleanly if the
  backtest says it doesn't earn its place.
- **Set expectations on oi_3m:** it is a *stricter* confirm, not a faster one. It only vetoes on a
  sharp recent OI flush and stays NEUTRAL otherwise — so in `all`/combined terms it rarely blocks,
  and when it does, there was a real burst. Judge it the same way: log-only week, points-saved-vs-
  cost on held-out days.
- **The context panel is the real win for "all the 8600 views":** you get PCR Trinity, Smart OI,
  PE-CE Matrix 2.0, CVD, walls, PCR-futures all on the cockpit to read — none of them able to veto
  a trade. That's how you see everything without the "all agree = no trades" trap.
```
