# Cursor Work Order — v2.0.43: make the app CONFESS why a real order rejected (it's app-side, not Dhan)

> PROVEN 20 Nov: the broker is healthy — Dhan token Active (23h), Static IP set (49.205.39.195), funds
> ₹25,047.70, and MANUAL orders FILL (25250 PE buy+sell both Success). Yet AUTO ALGO real orders REJECT
> — and they do NOT appear in the Dhan order book at all, which means the app is rejecting them LOCALLY
> before they ever reach Dhan. The exact reason is already captured in code but never surfaced to the
> owner (he sees a bare "REJECTED"). This job: surface the real reason on the order row + log the full
> auto-submit context + fix the stale/zero funds guard. Branch gemini/v2.0.43-reject-reason off
> gemini/v2.0.41-arm-reconstruct (or latest consolidated tip). Tags pre-/post-. Push. Do NOT merge. No
> protected modules. verify_locks 11/11. NO strategy-rule / EMA / exit-threshold change. This is
> diagnosis + a funds-snapshot fix, NOT a change to WHAT trades. PAPER_ONLY for testing; do not flip
> real-money.

```
EVIDENCE / ANCHORS (read before changing anything)
- Local cash guard: terminal8500_execution.py:90–91
    balance=self.funds.get('availabelBalance', self.funds.get('availableBalance',0))
    if quote*quantity>balance: raise ValueError('Available cash below sampled option premium requirement')
  self.funds is a CACHED /fundlimit snapshot set in sync() (:18–22). If sync is stale/failed or the key
  is absent, balance reads 0 → every real order rejects locally though the account has ₹25k.
- Tick-alignment guard: terminal8500_execution.py:95–97 — missing contract tickSize → raise
  'Option bracket is not aligned to instrument tick size' (local reject).
- Auto path uses SUPER/bracket orders: /super/orders (:106), productType INTRADAY, orderType MARKET,
  with targetPrice/stopLossPrice/trailingJump. Owner's MANUAL fills were NORMAL product orders — a
  different endpoint. The rejected auto orders are ABSENT from Dhan's book → rejected before Dhan.
- Reason IS captured elsewhere: terminal8510_backend.py:2376 / 2394
    err = data.get("remarks") or data.get("errorMessage") or res.text[:200]
  but it is not written onto the order-book row the owner sees.

PART A — SURFACE THE REAL REJECTION REASON (highest priority)
A1. Every REJECTED real order row in the ledger/orders book must carry a human reason string, not a bare
    "REJECTED". Populate a reject_reason column (one exists at terminal8510_backend.py:3283
    rejection_reason TEXT — reuse it) from, in order: the local ValueError message (cash/tick/confirm/
    daily-loss), else the Dhan remarks/errorMessage (the 2376/2394 capture), else the raw response.
A2. Show it in the cockpit orders table and include it in the CSV/download. Format:
    "REJECTED — Available cash below sampled option premium requirement (balance read ₹0.00)",
    "REJECTED — DH-<code>: <remarks>", etc.
A3. Distinguish LOCAL reject (never sent to Dhan) from BROKER reject (Dhan said no) with a one-word tag
    [LOCAL] / [BROKER], so the owner instantly knows whether the app or Dhan stopped it.

PART B — FIX THE STALE/ZERO FUNDS GUARD (the prime suspect)
B1. Before the cash guard runs, ensure self.funds is FRESH: re-sync /fundlimit (or reuse a snapshot no
    older than N seconds) immediately before submit. If the funds fetch fails or returns no balance key,
    do NOT silently treat balance as 0 → instead reject with an explicit, different reason
    "funds snapshot unavailable — cannot validate margin" and LOG the raw /fundlimit response + which key
    was read. A real ₹25k balance must never read as ₹0 because of a stale/missing snapshot.
B2. Log at submit time: the balance value, the exact key used (availabelBalance vs availableBalance —
    Dhan's real key is the misspelled 'availabelBalance'), quote, quantity, quote*quantity, and the
    pass/fail. One line per real submit attempt → data/terminal8510/real_submit_audit.log.
B3. Do NOT change the RULE that an order needs sufficient margin — only ensure the check uses a TRUE,
    fresh balance and fails loudly (not silently as ₹0) when it can't read one.

PART C — VERIFY THE SUPER-ORDER PATH END-TO-END (report, fix only if clearly broken)
C1. Confirm the /super/orders payload the auto path builds is valid for this account: tickSize present on
    the contract, stop/target on the 0.05 tick, product/segment correct. If tickSize is missing from
    contract metadata, that is a local reject cause — fetch/default it correctly (document the source),
    don't let a missing field nuke every order.
C2. Report whether Super (bracket) orders are the blocker vs plain orders. If Super orders are rejected
    by Dhan while Normal fill, SAY SO and surface Dhan's remarks — but do NOT silently switch the
    strategy to Normal orders (that drops the SL/target bracket); flag it for the owner to decide.

GUARDRAILS
- Diagnosis + funds-snapshot correctness only. NO change to entry/exit RULES, EMA maths, OI filters, or
  the bracket SL/target policy. No protected modules. verify_locks 11/11. Config via patch if a key is
  needed. PAPER_ONLY for tests; do not flip real-money. Do not weaken the margin check — make it honest.
- Owner-side items remain owner's: none needed here — broker is confirmed healthy (token Active, IP set,
  funds present, manual fills work). This is an APP bug.

TESTS (analyst/test_real_reject_reason_8510.py)
- A rejected real order persists and displays its reason string + [LOCAL]/[BROKER] tag.
- Cash guard with a STALE/empty funds snapshot (balance key absent) → reason
  "funds snapshot unavailable", NOT a false "cash below requirement", and the raw response is logged.
- Cash guard with a FRESH ₹25,047 balance and a ₹16,335 order → PASSES (does not reject).
- Missing tickSize → explicit tick reason, not a bare REJECTED.
- Local reject is tagged [LOCAL] and never appears in the Dhan intents table as sent.
- verify_locks 11/11; full pytest diffed vs baseline — no NEW failures.

DELIVERABLE
Commit on gemini/v2.0.43-reject-reason, tags, push, AGENT_STATUS. Paste back: (1) the reason now shown
for the 20-Nov rejects if reproducible from recorded state, (2) the real_submit_audit.log format, (3) the
C1/C2 super-order finding. State plainly whether the root cause was the stale/zero funds snapshot, the
missing tickSize, or a Dhan super-order rejection. Do not merge.

CONFIRM before coding: surface the captured reject reason (remarks/errorMessage/ValueError) onto the
order row + CSV with a [LOCAL]/[BROKER] tag; re-sync funds before the cash guard and fail loudly (never
silent ₹0) when the balance can't be read; log balance+key+quote+qty per real submit; verify the
/super/orders payload (tickSize/tick alignment) and report Super-vs-Normal; no rule/EMA/exit/bracket
changes; PAPER_ONLY for tests.
```

---

## For you, Harish — plainly
- **Your account is not the problem — the app is.** Token active, IP set, ₹25k funds, manual orders
  filling: Dhan is working. The auto orders are being thrown out **by our own code before they reach
  Dhan** (that's why they're not even in your Dhan order book).
- **Most likely cause:** the app checked your balance against a **stale/zero cached snapshot** and
  decided you were broke — while your real account had ₹25k. A ₹16k order got rejected against a phantom
  ₹0. Second possibility: a missing tick-size field on the contract. Both are local, both findable.
- **After v2.0.43** your rejected rows will say the actual reason — "[LOCAL] cash below requirement
  (balance read ₹0.00)" or "[BROKER] DH-xxx: ..." — so we never guess again, and the funds check will
  use your true live balance instead of a cached zero.
- **Right now, to confirm it yourself:** the auto rejects absent from Dhan's book = local rejection. If
  you want the exact string before v2.0.43 lands, check the app's log for "Available cash below" around
  09:32 — if it's there with a low/zero balance, that's the smoking gun.
