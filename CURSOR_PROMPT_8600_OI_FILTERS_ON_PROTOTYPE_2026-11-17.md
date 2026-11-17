# Cursor Prompt — 8600 Option-Chain Filters as ON/OFF switches on the 8 EMA Prototype

> Paste the box below into Cursor (Gemini). It is self-contained and names the real
> files, functions, config keys and signal IDs in this repo. AGY / Gemini-Argon will
> review after. **13–17 Nov design docs to read first:** `HANDOVER_8510_2026-11-13.md`,
> `EMA8_PROTOTYPE_DESIGN_2026-11-13.md`.

---

```
ROLE
You are editing the 8510 Algo Terminal in this repo. Work on a NEW branch off the
current tip claude/v2.0.23-proto-glitches, named gemini/v2.0.24-proto-oi-filters.
Do not commit to integrate/v1.9 or main. Read HANDOVER_8510_2026-11-13.md and
EMA8_PROTOTYPE_DESIGN_2026-11-13.md before writing any code.

GOAL (exactly this, nothing more)
Add FIVE independent ON/OFF switches to the 8 EMA Prototype. Each switch, when ON,
makes an 8600 option-chain read act as a FILTER on the Prototype entry: a touch that
would otherwise enter is BLOCKED when the chosen read disagrees with the trade side.
When a switch is OFF it has zero effect. All five default OFF. This must not change
any Prototype entry/exit math — it only adds optional gates at the moment of entry.

THE FIVE READS (use these exact IDs — they already exist on the 8600 Board)
  writer_flow  — Classic Writer Flow
  price_oi     — Price vs OI buildup (15 min, at the money)
  total_oi     — Cumulative Total OI
  fresh_oi     — Fresh Change in OI
  volume_oi    — Volume-backed OI (15 min), churn = NEUTRAL
Each read already resolves to a direction of "CE", "PE", "NEUTRAL", or None, produced
by analyst/desk8600_board_8510.py (build_board -> sources[].direction) and
analyst/chain_reads_8510.py. DO NOT recompute these reads. Consume the direction that
the board/chain_reads already produce. DO NOT modify any PROTECTED MODULE (writer_flow
originates in the 8600 app's chain.py; you only READ its board output here).

HOW A SWITCH GATES A TRADE
- Prototype LONG entry buys CE. A read "agrees" with LONG when its direction == "CE".
- Prototype SHORT entry buys PE. A read "agrees" with SHORT when direction == "PE".
- For each switch that is ON:
    direction opposes the side (CE-side trade, read says "PE", or vice-versa) -> BLOCK.
    direction == NEUTRAL -> block ONLY if neutral_blocks is true (default false).
    direction == None / read missing / snapshot older than max_age -> NEVER block
      (fail OPEN). The 8600 app may not be live; a stale read must not freeze trading.
- Combine rule across the ON switches (config `oi_filters.rule`, default "all_agree"):
    "all_agree" : block if ANY ON switch opposes (strictest; every ON read must not oppose).
    "any_agrees": allow if AT LEAST ONE ON read agrees; block only if none agree and
                  at least one opposes.
    "majority"  : block if more ON reads oppose than agree.
  Implement all three; default "all_agree".

CONFIG (add under scanner.ema8_prototype in analyst/terminal8510_config.json)
Change config ONLY via a new narrow patch script scripts/patch_add_oi_filters_8510.py
(back up the file first, touch only these keys, print a diff). Add:
  "oi_filters": {
     "mode": "log_only",          # "log_only" (record would-block, still trades) or "block"
     "rule": "all_agree",         # all_agree | any_agrees | majority
     "neutral_blocks": false,
     "max_age_sec": 420,          # older reads fail OPEN
     "switches": {
        "writer_flow": false,
        "price_oi":    false,
        "total_oi":    false,
        "fresh_oi":    false,
        "volume_oi":   false
     }
  }
Keep every existing Prototype key and default UNCHANGED (touch_tol_pts 3, away_min_pts 2,
target_pts/sl_pts 40, e8_refresh_seconds 3, backtest_tick_seconds 30, no_entry_before_hm
09:30, no_entry_from_hm 15:20, reentry_same_side true, reentry_after_stop_loss false).
Leave the old single placeholder oi_direction_filter_enabled in place and unused, OR
have the new block supersede it — but do not break configs that still set it.

WHERE TO WIRE IT (backend)
- analyst/ema8_prototype_8510.py: there is already _oi_filter_blocks(bullish, cfg,
  proto_state) called from _build_signal at the touch (around the "elif touched and
  _oi_filter_blocks(...)" line). Add a new function, e.g.
     oi_switch_block(bullish, cfg, board_reads) -> (blocked: bool, detail: dict)
  that reads cfg["oi_filters"], looks up each ON switch's direction from board_reads
  (a plain dict {read_id: {"direction":..., "age_sec":...}}), applies the agree/oppose/
  neutral/stale logic and the combine rule, and returns whether to block plus a detail
  dict naming each ON read's direction and the verdict. Pure function, fully unit-testable,
  no I/O, never raises.
- The live worker that evaluates the Prototype (terminal8510_backend.py, the intrabar
  worker that calls evaluate_ema8_prototype_intrabar and records _ema8_proto_last_check)
  already builds / has access to the 8600 board. Pass the current board reads into the
  Prototype evaluation so oi_switch_block can see them. Build the {read_id: {direction,
  age_sec}} map from build_board(...)["sources"] filtered to the five IDs.
- mode == "log_only": compute the verdict, write it to the Prototype status/telemetry
  and to the signal log, but STILL TAKE THE TRADE. mode == "block": skip the entry
  (release the setup the same way a skipped signal does today — see _release_unfilled_setup
  / the v2.0.21 "skipped signal no longer uses up its setup" fix) and log the reason.
- Record on each check, for the banner/telemetry: for every one of the five reads, its
  current direction + age_sec, whether its switch is ON, and the combined would-block
  verdict for the currently-armed side. This must appear even when nothing is blocked.

UI (Algorithmic_Trading_Workspace_8510.html, the Prototype cockpit card)
- Add a small "8600 OI Filters" group on the Prototype card with 5 labelled toggles
  (use the human labels above), a mode selector (Log-only / Block), and a rule selector
  (All agree / Any agrees / Majority). Persist each to scanner.ema8_prototype.oi_filters
  via the existing config-save path. Follow the v2.0.23 radio pattern: only set a control
  when its key is present in the telemetry (the setRadio/`if (!(key in cfg)) return;`
  helper) so partial telemetry never clobbers a toggle.
- Next to each toggle show the read's live direction (CE green / PE red / NEUTRAL grey /
  "— stale" when None or too old) and its age. Show one line: "Right now this armed PE
  setup WOULD be blocked by: writer_flow, total_oi" (or "not blocked"). Update it on the
  same cadence as the rest of the Prototype banner.
- Do not add any gate, confirmation, or cap that is not listed here.

BACKTEST / REPLAY
- analyst/backtest_lab_8510.py and the Prototype backtest: the recorded option-chain
  snapshots exist, so compute the five reads per bar from the recorded chain (reuse
  chain_reads_8510.compute_reads + the board judging — read-only) and apply the SAME
  oi_switch_block at the backtest entry. Add a backtest spec flag to turn the oi_filters
  on/off and to pick which switches, so I can measure each filter's effect on the
  held-out PASS/WEAK/FAIL verdict. If a bar has no fresh snapshot, the read is None ->
  fail OPEN (same as live). Keep reporting in spot points.
- If recorded chain history is too short for a read on a given day, say so in the report
  rather than silently dropping days.

HARD RULES (from CLAUDE.md — do not violate)
- Never modify a PROTECTED MODULE without an UNLOCK instruction. scripts/verify_locks.py
  must print PASS 11/11 after your change. You are only reading 8600 board output.
- No remote model/provider calls. Config only via the patch script. Never commit data/,
  analyst/replay_data/, *.sqlite3, .env. Preserve M analyst/terminal8510_config.json.
- Do not add any rule/gate/limit/dry-run/confirmation the owner did not ask for.

TESTS + VERIFICATION
- New unit tests for oi_switch_block: agree/oppose/neutral/stale/None per read; each
  combine rule (all_agree, any_agrees, majority); mode log_only vs block; all-off =
  never blocks (baseline unchanged). Put them in analyst/test_proto_oi_filters_8510.py.
- A test that with all five switches OFF, the Prototype produces byte-for-byte the same
  signals as before this change (no behaviour drift when disabled).
- Run: python3 scripts/verify_locks.py  (must be PASS 11/11)
       python3 -m pytest analyst -q --continue-on-collection-errors
  Diff your failure list against the §6 baseline in HANDOVER_8510_2026-11-13.md
  (~661 passed; the phase5_eod/phase7 wall-clock tests fail after 15:20 IST — not yours).
- Browser-check the cockpit: load the HTML, click every tab, 0 page errors, toggle each
  switch and confirm it persists and the live direction/age shows.

DELIVERABLE
- Branch gemini/v2.0.24-proto-oi-filters off v2.0.23. Append a dated block to
  AGENT_STATUS.md. Give me the file list + the run commands (checkout, patch script,
  restart, hard-refresh). State plainly what you could not verify (no live broker/market
  data in the sandbox, 8600 app not running). Do not merge — Harish merges.

CONFIRM the switch IDs, the fail-OPEN-on-stale rule, and "all five default OFF, log_only
default" before you start coding.
```

---

## Quick notes for you (not part of the prompt)
- **Default is safe:** every switch OFF and `mode: log_only` means nothing changes until
  you flip a toggle — you can watch what each read *would* have blocked before you let it
  block anything real.
- **Fail-OPEN is the key safety line:** if the 8600 app isn't live, the reads go stale and
  the filters simply stop filtering — they never freeze the Prototype. That's deliberate,
  and it's why you must have the 8600 app running for the filters to actually bite.
- **The five reads map 1:1** to the board IDs `writer_flow`, `price_oi`, `total_oi`,
  `fresh_oi`, `volume_oi`. No new signal maths — Cursor just consumes directions the
  8600 Board already computes.
- Writer Flow lives in a protected 8600 module; this only *reads* its verdict, so
  `verify_locks.py` stays green.
