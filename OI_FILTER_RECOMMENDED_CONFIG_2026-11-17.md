# 8 EMA Prototype — 8600 OI Filters: recommended config & how to read it

**17 Nov 2026 · branch `gemini/v2.0.24-proto-oi-filters` · for AGY / Gemini-Argon / future-Harish**

This note sits next to the Cursor spec (`CURSOR_PROMPT_8600_OI_FILTERS_ON_PROTOTYPE_2026-11-17.md`)
and the design doc (`EMA8_PROTOTYPE_DESIGN_2026-11-13.md`). It says what to turn on, why, and
how to judge whether a filter is helping.

---

## 1. What the filter does, in one line

Each 8600 option-chain read carries a **direction**: `CE` = bullish, `PE` = bearish
(call-writing scores bearish → `PE`; put-writing scores bullish → `CE`). A switch that is
**ON** blocks a Prototype entry whose side the read **opposes**:

- **CE (buy) trade is blocked when the read says `PE`** → i.e. **call writers are dominant.**
- **PE (sell) trade is blocked when the read says `CE`** → i.e. **put writers are dominant.**

That is exactly the rule we want: don't buy into heavy call writing, don't sell into heavy
put writing.

## 2. Three settings that change behaviour

- **Mode = Log only** → filter runs, cockpit shows *"would block"*, **trade still taken.** Learning mode.
- **Mode = Block** → same verdict now **skips the entry, on BOTH paper and real** (one entry
  path serves both). The arm is kept, so a later touch can still fire if OI has flipped.
- **Rule = All agree** → block if **any** ON read opposes the side. **Use this** — it matches
  "block when call writers are more on fresh OI *or* total OI."
- Rule = Any agrees / Majority → more permissive; not what we want for a writer-veto.
- **Neutral blocks = OFF** → a read that is NEUTRAL (balanced / churn) does **not** block.
  Keep OFF, or churny days will veto everything.

## 3. Fail-OPEN — the safety line (important)

The reads are live **only when the 8600 app is running and feeding.** When a read is missing,
`None`, or older than `max_age_sec` (420s), it **fails OPEN — it does not block.** So:

> **If 8600 is not providing signals, the Prototype trades on its own 8 EMA signal exactly as
> it does today.** You never lose a trade because the feed died — you only lose the *filter*
> until 8600 is back. This is by design. For the filters to actually bite, keep the 8600 app
> running during live/paper hours.

## 4. Recommended starting config

```
Mode:            Log only          # run this for ~1 week first, then switch to Block
Rule:            All agree
Neutral blocks:  OFF
Switches ON:     fresh_oi, total_oi        # the two you described
Switches OFF:    writer_flow, price_oi, volume_oi   # add later, one at a time
```

Why these two first:
- **`fresh_oi` (Fresh Change in OI)** — intraday writer pressure *right now*. This is the
  "OI Change (Call vs Put)" chart.
- **`total_oi` (Cumulative Total OI)** — the standing writer wall. This is the "Total OI
  (Call vs Put)" chart.
- **`writer_flow`** — put-change minus call-change; a third writer read, usually agrees with
  fresh_oi. Add it once the first two look clean.
- **`price_oi`** — distinguishes *writing* from *short-covering/unwinding*; good confirm, but
  needs 15 min of snapshots before it speaks.
- **`volume_oi`** — a *quality* read (is the OI real or churn?), goes NEUTRAL on churn. It is
  not a direction filter; leave it off unless you want to sit out churny days.

Then: after a week of Log-only, if fresh_oi + total_oi blocked more losers than winners
(measured in points, see §6), switch **Mode = Block** and optionally add `writer_flow`.

## 5. Worked example — today, 17 Nov (from your charts)

On the **OI Change (Call vs Put)** chart, the **Put OI Change (red) line sat above the Call
OI Change (green) line all through the morning** — more puts being written than calls →
**put writers dominant → bullish bias → read direction `CE`.** Spot rose 25,300 → ~25,500.

What the filter would have done with `fresh_oi` ON, Rule = All agree:
- A **CE (buy)** touch in that window → read says `CE` (agrees) → **allowed.** Good, that was
  the right side.
- A **PE (sell)** touch in that same window → read says `CE` (opposes PE) → **blocked.** It
  would have kept you from shorting into a put-writer-supported rising market.

Late in the day both OI lines roll over and collapse (unwinding into the close) → the read
goes NEUTRAL / flips → the filter stops favouring CE. That is the read doing its job: it only
vetoes while the writer imbalance is real.

## 6. How to read the Log-only results (the only honest test)

Every blocked-or-would-block event is logged with: time, side, which reads opposed, and their
directions. To decide if a filter earns its place, do **not** judge by feel. For each ON
switch, over the held-out backtest and the log-only live days, compute:

- **Points saved** = sum of P&L of the trades it blocked that were **losers** (you avoided them).
- **Points cost**  = sum of P&L of the trades it blocked that were **winners** (you missed them).
- Keep the filter **only if points saved > points cost**, by a clear margin.

Remember the base rate: the Prototype's good days come from **few trades** (13 Nov = 5 trades,
+115 pts). A filter can only *remove* trades, so one that vetoes a single big winner can wipe a
week of small saves. Measure in points, on held-out days, before trusting it live.

## 7. Instructions for the implementer (Cursor / AGY / Gemini)

- **Block mode must act on both paper and real** (it already does — the gate is at the single
  entry path). Do not make Block log-only-only.
- **Default saved Rule for the Prototype = `all_agree`** (the UI must save All-agree as the
  default, not "Any agrees").
- **Fail-OPEN on stale/None/aged-out** is mandatory — a dead 8600 feed must never block a trade.
- Commit on `gemini/v2.0.24-proto-oi-filters` with the usual `pre-`/`post-` tags, push, append a
  dated block to `AGENT_STATUS.md`. Run `scripts/verify_locks.py` (PASS 11/11) and the full
  `pytest analyst` diffed against §6 of `HANDOVER_8510_2026-11-13.md` (~661 baseline). Browser-
  check the Prototype card: toggles persist, live direction/age shows, 0 console errors.
- Do not touch PROTECTED MODULES (Writer Flow is read-only here). No added gates beyond these.
