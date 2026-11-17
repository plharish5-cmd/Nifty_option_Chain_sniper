# Cursor Prompt — add `oi_3m` as one gate + 8600 views as display-only context

> Paste the box into Cursor (Gemini). Addendum to the OI-filter + momentum work on
> `gemini/v2.0.24-proto-oi-filters`. Read `OI_Momentum_Spec_v2_Normalized_2026-11-17.md` first.
> Validated on real 1-min data (17 Nov 12:40–13:19): S fired PE STRONG at 13:08, EXTREME 13:10,
> NEUTRAL exit 13:19; B stayed positive throughout — proof level reads must NOT gate.

---

```
ROLE
Edit the 8510 Algo Terminal on branch gemini/v2.0.24-proto-oi-filters (or a child branch
gemini/v2.0.26-oi3m-and-views off it). Tags pre-/post- as usual. Co-Authored-By footer.
Do not merge. Read OI_Momentum_Spec_v2_Normalized_2026-11-17.md — this reuses its maths.

GOAL — two separate things, both default-OFF / non-blocking
(1) ADD ONE optional GATE: oi_3m — a 3-minute version of the validated normalized-S momentum,
    as a sixth Prototype OI-filter switch.
(2) ADD the other 8600 views as DISPLAY-ONLY context on the cockpit — shown, never gating.

PART 1 — oi_3m gate (directional, safe)
- Same maths as OI Momentum v2 (Option A: S = dD/T over a fixed time window, lakhs via
  _to_lakhs, window found by timestamp with ±2 min tolerance, fail-OPEN on miss) — but with
  window_min = 3 instead of 10. Reuse momentum_direction_from_significance; do NOT write new
  maths. Direction CE/PE/NEUTRAL from the SAME thresholds.
- Add to config under scanner.ema8_prototype.oi_filters (via the patch script, back up first):
    "switches": { ... existing ..., "oi_3m": false }     # default OFF
    "momentum_3m": { "window_min": 3, "sig_min": 0.15, "sig_extreme": 0.30 }
  (Separate 3-min sig thresholds so they can be tuned without touching the 10-min ones. A 3-min
   window moves less, so it fires only on sharp flushes — that is the intended "EXTREME confirm"
   behaviour. Keep 0.15/0.30 as the start; the log-only week decides if they need raising.)
- Wire oi_3m into oi_switch_block EXACTLY like the other switches: when ON and Rule=all_agree,
  a CE touch is blocked if oi_3m opposes (says PE), and vice-versa. NEUTRAL/stale never blocks.
- Cockpit: add the "3-min OI impulse" toggle to the 8600 OI Filters block with its own
  S · bias · CumD → direction line (3-min window). Persist to config. Present-key guard.
- B (level) still NEVER gates. price_oi/volume_oi still untouched. Everything else unchanged.

PART 2 — 8600 views as DISPLAY-ONLY context (never gate)
- Add a separate read-only "8600 Context" panel on the cockpit (NOT in the OI Filters block, so
  it is visually clear these do not gate). Show, each at the Prototype banner cadence, whatever
  the 8600 board / protected modules already expose — READ-ONLY, pulled from existing outputs:
    pcr_zone, walls (room to target), day_oi (PE−CE since 09:15), pcr_futures,
    ticket, high_conviction, and the richer views' verdicts: PCR Trinity, Smart OI,
    PE-CE Matrix 2.0, CVD — show each as a labelled context value (direction/verdict + number).
- These are CONTEXT ONLY. They must NOT enter oi_switch_block, must NOT appear as gate switches,
  and must NOT be able to block a trade. No toggle that routes them into the gate.
- PROTECTED MODULES (pcr_trinity.py, smart_oi.py, pe_ce_matrix.py, cvd_divergence.py, chain.py):
  READ their already-computed verdicts only. Do NOT modify them. If a verdict is not already
  surfaced on the board, add a read-only accessor that consumes their public output — never edit
  the locked module. verify_locks.py must print PASS 11/11.
- If a context read is stale/missing → show "—" / "stale". It never affects trading either way.

GUARDRAILS
- oi_3m default OFF; all context display-only. Do NOT add level reads (pcr_zone, walls,
  pcr_futures) as GATES — they stay one-sided all day and would block the 13:08-type trade
  (the whole reason B is display-only). Only oi_3m (directional, leading) is a new gate.
- Fail-OPEN everywhere; if any ambiguity, prefer None/NEUTRAL over blocking.
- Config via patch script only; preserve M terminal8510_config.json. No remote model calls.
- TESTS (analyst/test_oi_3m_gate_8510.py): oi_3m CE/PE/NEUTRAL on a 3-min window; fail-open on
  insufficient 3-min history; oi_3m OFF = no effect; all-agree with oi_3m opposing blocks, with
  oi_3m agreeing allows. A test that the context reads never enter oi_switch_block (gate output
  identical with context present vs absent).
- Run verify_locks.py (11/11) and PYTHONPATH=. pytest analyst -q --continue-on-collection-errors;
  diff against the handover baseline (~826–831). Browser-check: oi_3m toggle persists, context
  panel shows, gate unaffected by context, 0 console errors.

DELIVERABLE
Commit, tags, push, append AGENT_STATUS.md block. File list + run commands. State what you could
not verify (no live 8600 feed / broker in sandbox). Do not merge.

CONFIRM before coding: oi_3m is the ONLY new gate (default OFF), uses the v2 S-maths with a 3-min
window; pcr_zone/walls/pcr_futures and the Trinity/Smart OI/PE-CE/CVD views are DISPLAY-ONLY and
can never block; protected modules read-only (verify_locks 11/11).
```

---

## Why this split (for you, not the prompt)
- **One new gate, not five.** `oi_3m` is the only addition that's *directional and leading* — it
  catches the same flush as your S, just faster, so it's a clean second vote. The validated 13:08
  signal is exactly the behaviour it confirms.
- **Everything else is display.** You get to *see* PCR, walls, PE-CE Matrix, Smart OI, CVD, the
  Trinity — all the 8600 views — on the cockpit as context, but none of them can veto a trade. So
  you get the full picture without the "all agree = no trades" trap.
- **The level reads are the trap.** pcr_zone / walls / pcr_futures stay one-sided all day (like B
  did at +0.30 while the right trade was PE). Gating on them would block the 13:08 entry. That's
  why they're display-only, permanently.
- **Still judged on points.** Even oi_3m goes log-only first and only earns Block if the held-out
  points say so.
```
