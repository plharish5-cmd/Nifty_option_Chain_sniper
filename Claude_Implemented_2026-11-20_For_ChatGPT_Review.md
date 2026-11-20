# What Claude implemented — 20 Nov 2026 — for independent ChatGPT review

> ChatGPT: you are the INDEPENDENT reviewer. Claude wrote the items below. Your job is NOT to confirm
> them — it is to reproduce each from the code, try to break it, and find any hidden reason it fails
> (wrong wiring, overlooked parameter, side effect, duplicate logic, lost work). Assume nothing until
> you've verified it yourself. Separate verdicts: CONFIRMED / REFUTED / UNVERIFIABLE (needs live market
> or live Dhan). Read-only + PAPER_ONLY; do not place real orders. Rank findings by impact on whether
> the desk executes a genuine 8 EMA trade with a correct price.

Branch under review: **`reconcile/v2.6.0-consolidated`** (tip `d63e3d0`).
Base it was built on: **`chatgpt/v251-paper-executable-quotes`** (`4917bb6`) — your own line.
Common ancestor of both lines: `0fcda9a` (v2.0.41 rebuild-arm-from-Dhan).

## A. The reconciliation decision (verify nothing was lost or duplicated)
Claude took YOUR v251 line as the base and added only its 2 unique features on top, because Claude's
other work (reject reasons, IP check, OI freshness, pricing) DUPLICATED yours. Verify:
1. `git log 4917bb6..reconcile/v2.6.0-consolidated` shows ONLY: trail, preflight, test-button, paper-quote-finish — nothing else.
2. All of your v244–v251 commits are present in the base (reject reasons f9347ad, IP check f629427, OI
   freshness 9dc3999, OI recording 24ce19e, panel cleanups 69e0183/8b1c607, v2.0.42 self-audit 48a28b2).
3. Claude did NOT bring its own v2.0.43/v2.0.45/v2.0.46 (they live on separate `gemini/*` branches, unused
   here). Confirm none of those duplicate-implementations leaked into this branch.
4. CHALLENGE: is there anything in Claude's `gemini/v2.0.43/45/46` branches that is BETTER than your
   equivalent and should have been kept instead? Name it if so.

## B. Claude's feature 1 — Prototype "+40 then trail" exit  (commit 7915ea6)
Files: `analyst/terminal8510_backend.py` (simulate_prototype_spot_exit + simulate_backtest_spot_exit),
`analyst/ema8_prototype_8510.py` (DEFAULT_CFG), `analyst/test_proto_trail_8510.py` (6 tests).
Claim: +40 is now the TRIGGER, not the exit. On reaching entry±40 the stop moves to breakeven, then
trails the prior CLOSED 1m candle low/high (ratchet-only); exit on a candle that CLOSES through the
trail (SPOT_TRAIL_EXIT). Pre-trigger −40 stop, opposite-15m-close, and 15:20 still override. Toggle
`trail_after_target_enabled` default True (reversible). Only the Prototype (exit_on_opposite_close) path.
VERIFY / TRY TO BREAK:
- Does the trail ever move the stop the WRONG way (loosen)? Does it use the FORMING bar (repaint risk)?
- On a gap that jumps the trail intrabar, does CLOSE-through still exit correctly, or can it skip?
- Does Reversal/Stage behaviour change at all? (It must not — they pass trail_after_target=False.)
- Is the trail wired into the LIVE exit path, or only the replay/backtest simulate functions? (Claude
  flagged the live real-order broker-stop trail as NOT verified off-box — confirm what actually runs live.)
- Did turning the default ON silently change any other backtest/report? (Claude pinned the phase6 check
  to classic mode via an explicit trail_after_target=False override — confirm that's legitimate, not a hack.)

## C. Claude's feature 2 — real-order READINESS preflight  (commit 25a1803)
Files: `analyst/real_order_preflight_8510.py` (pure build_order_preflight), `analyst/test_real_order_preflight_8510.py`
(7 tests), `terminal8510_backend.py` (real_order_preflight method), `engine_8510_live.py`
(GET /api/real-order/preflight), cockpit button.
Claim: reports whether a real order WOULD be accepted — Dhan connected, token valid, IP allowlisted
(DH-905 hint), funds vs ~1-lot cost — plus whether the desk is LIVE-armed (else paper). Places NOTHING.
VERIFY / TRY TO BREAK:
- Does it read the SAME ip_matched / token fields your v245 public-IP check populates, or a stale copy?
- Funds: does it use a FRESH /fundlimit, and is the ₹0 "feed problem" path correct (not a false NOT READY)?
- Is the est-cost (premium×65) realistic, or could a wrong premium make funds falsely pass/fail?
- Can the readiness verdict ever disagree with what the real order path would actually do?

## D. Claude's feature 3 — real 1-lot TEST ORDER button  (commit 7e4be08)
Files: `terminal8510_backend.py` (place_real_test_order), `engine_8510_live.py` (POST /api/real-order/test),
cockpit button + typed-PLACE confirm.
Claim: places ONE 1-lot BUY via the proven execute_order_with_chaser, requires typed "PLACE", resolves
contract via _select_ema8_prototype_contract_any_strike, respects EVERY live gate (real only if desk
LIVE-armed, else paper), tags reject [LOCAL]/[BROKER]. No gate bypass.
VERIFY / TRY TO BREAK (this spends real money if armed — scrutinise hardest):
- Can it EVER place a real order when the desk is NOT properly LIVE-armed? (kill switch, APPROVED verdict,
  per-lane toggle) — find any bypass.
- Does "confirm != PLACE" reliably block it? Any code path that places before the check?
- Could it place more than 1 lot, wrong side, or wrong contract? Does it leave an unmanaged position?
- Does it correctly route to PAPER when not armed, and never silently to REAL?

## E. Claude finishing YOUR work — paper-executable-quotes  (commit d63e3d0)
Files: `terminal8510_backend.py` (new `execute_position_exit` alias over _close_position_with_broker),
`analyst/test_live_exit_orders_8510.py` (FakeBroker.get_marketfeed_quote).
Context: you wrote `test_paper_quotes_v251.py` (TDD) but ran out of tokens before the method existed.
Claim: `execute_position_exit(pos, reason, spot_exit)` books only a FRESH executable bid; if none, the
position stays EXIT_PENDING (never a stale LTP) and a later call fills at the fresh bid. Fixes both
paper_quotes tests + test_6; no new failures.
VERIFY / TRY TO BREAK:
- Is the alias semantically identical to what your test intended, or a shortcut that hides a gap?
- Is the EXIT_PENDING→fresh-bid retry actually exercised in the LIVE engine loop, or only in tests?
- Does FakeBroker.get_marketfeed_quote (bid==ask==quote) misrepresent a real crossed/empty book?

## F. KNOWN OPEN — Claude did NOT fix these (verify they are pre-existing, not Claude-caused)
6 tests remain RED, all failing on YOUR clean base `4917bb6` before Claude touched anything:
- `test_live_exit_fixes_8510.py`: 5 live-buy tests (entry now needs a fresh executable quote the fixtures
  don't supply).
- `test_phase2_option_points_8510.py::test_prototype_buy_option_points_40`.
Claude's claim: these are TEST-FIXTURE staleness from YOUR entry/exit rewiring, NOT app bugs — the engine
imports, locks 11/11, and the pricing works in real code. VERIFY: confirm they are pre-existing on
`4917bb6`, confirm they are fixture gaps (missing get_marketfeed_quote + entry quote setup) and not a real
regression in the entry path, and say whether the running entry path is actually correct.

## G. Global checks
- `python3 scripts/verify_locks.py` → 11/11 (confirm).
- Engine imports: `python3 -c "import engine_8510_live"` → OK (confirm; this was broken before the
  stockmojo module was committed).
- Full pytest vs the pre-session baseline — confirm Claude introduced NO new failures beyond the 6 above
  (which pre-date Claude).
- Protected modules (LOCKED_MANIFEST) untouched by all Claude commits (confirm).

Report CONFIRMED / REFUTED / UNVERIFIABLE per item, with file:line or a repro. Push back freely — the goal
is a desk that executes a correct 8 EMA trade, not agreement.
