# Cursor Prompt — PE-CE volume columns (UNLOCKED) + volume conviction-confirm on momentum

> Owner authorized: **UNLOCK PE-CE ENTIRE MODULE** (protected module #4 only). This is the ONLY
> locked module you may change. The other 10 stay locked and must still pass verify_locks.
> Two separate parts: A (8600 PE-CE module, unlocked, additive) and B (8510 momentum confirm,
> no lock). Paste the box into Cursor.

---

```
ROLE
Owner has sent "UNLOCK PE-CE ENTIRE MODULE" — explicit authorization to modify protected module
#4 ONLY: ict_smc_app/backend/app/engine/pe_ce_matrix.py, ict_smc_app/frontend/src/components/
PeceMatrix.tsx, ict_smc_app/frontend/src/components/OcLab.tsx. Do NOT touch any other locked
module (pcr_trinity, smart_oi, chain, cvd and their .tsx). Work on branch
gemini/v2.0.27-pece-volume off gemini/v2.0.26-oi3m-separate-feature. Tags pre-/post-. Do not merge.

=== PART A — 8600 PE-CE module: add 3 volume columns (UNLOCKED, ADDITIVE ONLY) ===
GOAL: add Put Volume, Call Volume, and PE−CE Volume columns to the PE-CE table, to match the
owner's reference sheet.

CONSTRAINTS (critical — this is a protected module):
- ADDITIVE ONLY. The existing PE-CE OI logic, outputs and tests must stay byte-for-byte
  unchanged in behaviour. You are ADDING columns, not changing any existing calculation.
- pe_ce_matrix.py already SELECTs ce_vol, pe_vol (lines ~493, ~510). Reuse those; do not change
  the query shape beyond what's needed to carry volume through.
- Keep the diff minimal and reviewable.

VOLUME DEFINITION (to match the sheet):
- The sheet's volume is PER-INTERVAL (per-minute), not cumulative — values rise and fall each
  minute. Exchange/Dhan ce_vol/pe_vol is cumulative day volume. So compute interval volume as:
      put_vol_interval(t)  = sum_strikes(pe_vol at t) − sum_strikes(pe_vol at previous snapshot)
      call_vol_interval(t) = sum_strikes(ce_vol at t) − sum_strikes(ce_vol at previous snapshot)
      pece_vol(t)          = put_vol_interval − call_vol_interval
  Clamp negatives from a cumulative reset / new expiry to 0. Sum across the SAME strike range the
  table is showing (respect the ± range / strike filter, e.g. 24900–24900+… as selected).
- FIRST verify the definition empirically: print sum(ce_vol)/sum(pe_vol) for a couple of snapshots
  and compare magnitudes to the sheet. If the sheet turns out to be cumulative for this symbol,
  switch to cumulative; if per-interval, use the diff above. State which you picked and why.
- HONEST PARITY NOTE (put in code comment + AGENT_STATUS): the owner's sheet is a different data
  vendor than the app's Dhan feed. Match the METHOD; absolute figures may differ by feed timing.
  Do not claim exact parity you cannot guarantee.

OUTPUT / UI:
- pe_ce_matrix.py: add put_vol, call_vol, pece_vol to each row's output dict/record.
- PeceMatrix.tsx (+ OcLab.tsx as needed): add three columns "Put Vol", "Call Vol", "PE−CE Vol"
  in the table, formatted like the existing OI columns (lakhs/cr formatting consistent with the
  app). Colour pece_vol green/red like the other PE−CE columns if that matches the app style.

RE-LOCK (owner-sanctioned, mandatory after an authorized change):
- Update ict_smc_app/LOCKED_MANIFEST.json for ONLY the three PE-CE files: recompute their hashes
  into locked_files, and append a relock_history entry: {date: 2026-11-17, module: "PE-CE Matrix",
  reason: "owner UNLOCK: add Put/Call/PE-CE volume columns", author: "<model>"}. Do NOT change the
  hashes of the other 10 locked files.
- After re-lock, scripts/verify_locks.py MUST print PASS 11/11 again (the PE-CE entries now match
  the new baseline; the other 10 unchanged).
- Update ict_smc_app/backend/tests/test_pe_ce_matrix.py and the immutable-lock test as needed so
  they assert the NEW baseline, not the old one. Add tests for the 3 volume values.

=== PART B — 8510 momentum: volume as a CONVICTION CONFIRM (no lock, default OFF) ===
GOAL: let volume CONFIRM an OI-momentum (S) signal, so a divergence on thin/churny volume is
downgraded. Volume is NOT directional on its own — it only confirms or withholds.
- Reuse the existing volume-backing idea already in analyst/chain_reads_8510.py (the volume_oi
  "churn vs backed" ratio). Add an OPTIONAL sub-flag to the momentum config:
      scanner.ema8_prototype.oi_filters.momentum.require_volume_confirm (default false)
- When true: an S-based CE/PE direction is only kept if the window's volume backing is ABOVE the
  churn threshold (i.e. the OI change is volume-backed, not churn). If it's churn, downgrade the
  read to NEUTRAL (which, being fail-open, never blocks). When false: no change at all.
- Default false = behaviour identical to today. Config via the existing momentum patch script
  (back up first). Cockpit: show the volume-backing state next to the S line (e.g. "vol: backed"
  / "vol: churn") for the writer reads, display-only.
- Tests: require_volume_confirm off = identical to now; on + churn = S signal downgraded to
  NEUTRAL; on + backed = S signal kept.

GUARDRAILS (both parts)
- Part A touches ONLY the PE-CE module (authorized). verify_locks 11/11 after re-lock. No other
  locked module changed. Part B touches no locked module.
- Config via patch scripts only; preserve M terminal8510_config.json. No remote model calls.
- Run verify_locks.py and PYTHONPATH=. pytest (ict_smc_app backend tests for Part A;
  analyst for Part B) and diff against baselines. Browser-check: PE-CE table shows the 3 volume
  columns; 8510 cockpit shows the vol-backing note.

DELIVERABLE
Commit on gemini/v2.0.27-pece-volume, tags, push, AGENT_STATUS.md block noting the UNLOCK and
re-lock. File list + run commands. State what you could not verify (live Dhan feed, vendor parity
with the owner's sheet). Do not merge.

CONFIRM before coding: UNLOCK applies to the PE-CE module ONLY; Part A is additive (existing PE-CE
OI logic unchanged) and re-locks the 3 files in the manifest with a relock_history entry so
verify_locks returns 11/11; volume is per-interval summed across the shown strikes, method-matched
not vendor-exact; Part B is default-OFF and only downgrades, never adds a new directional gate.
```

---

## Notes for you
- **The UNLOCK is logged, not silent.** Part A re-locks the PE-CE files with a `relock_history`
  entry naming the reason ("owner UNLOCK: add volume columns") so the audit trail shows it was
  authorized, and `verify_locks` goes back to 11/11 against the new baseline. The other 10 modules
  are never touched.
- **Exact-match reality:** we match the *method* (per-interval volume, same strikes). Your sheet is
  a different vendor than the app's Dhan feed, so compare them live — small differences are the
  feeds, not a bug. If you want true exact parity, the only way is to feed the app from the same
  source as the sheet, which is a separate decision.
- **Part B is the one that actually helps your trading:** volume confirming S means a divergence on
  thin/churny volume won't fool the filter. It only ever *withholds* (downgrades to NEUTRAL), never
  adds a new block — so it's safe and default-OFF, judged on held-out points like everything else.
```
