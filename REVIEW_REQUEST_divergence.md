# Independent review request — RSI / pivot / VIX divergence engines

Copy everything below the line into AGY (or any other reviewer). It is written to be adversarial on
purpose: the author of this code is asking to be checked, not agreed with.

---

You are reviewing signal-generation code in a live NIFTY options trading desk. Real money is traded
from its output. Another AI wrote this tonight. **Your job is to find what is wrong with it, not to
confirm it.** Assume the author was over-confident. If you cannot find problems, say so explicitly
and show what you checked — but look hard first.

Do **not** trust the comments or commit messages in these files. They are the author's own account of
their work and may be wrong, self-flattering, or describing intent rather than behaviour. Verify
against the code and the data.

## What to review

Commits `8788e68`, `e3df853`, `e52cf80`, `28d5b7b`, `42aca59` on `main`. Files:

- `ict_smc_app/backend/app/engine/rsi_divergence.py`
- `ict_smc_app/backend/app/engine/vix_divergence.py`
- `ict_smc_app/backend/tests/test_rsi_divergence.py`
- `ict_smc_app/backend/tests/test_vix_divergence.py`
- `scripts/backtest_divergence.py`
- `ict_smc_app/frontend/src/components/divergence.ts`
- the `rsi_divergence` and `vix_divergence` blocks in `ict_smc_app/backend/app/rules.json`
- `GET /api/desk/divergence` in `ict_smc_app/backend/app/api/main.py`

## What it claims to do

1. Compute Wilder's RSI(14) on closed bars.
2. Find swing pivots (extreme of ±`pivot_window` bars) and compare each pivot against up to
   `max_lookback_pivots` earlier pivots to classify four divergence types: regular bearish/bullish
   (reversal) and hidden bearish/bullish (continuation).
3. Treat every divergence as a **watch**, not a trade: it ARMS with a `structure_trigger` (the
   opposing extreme between the two pivots) and only becomes `STRUCTURE_CONFIRMED` when a bar
   **closes** through that trigger. Otherwise it ends `INVALIDATED` (price ran past the pivot) or
   `EXPIRED`.
4. Veto reversal watches that point against a decided day regime.
5. Report price/VIX relationship as context only, never as an entry, and report `UNAVAILABLE` rather
   than interpolating a missing VIX series.

## Specific things to check

**Causality / look-ahead bias — the most important.** This is a backtested signal engine, so a single
leak invalidates every number it produces.
- `find_pivots` inspects `bars[i+1 : i+window+1]`, which is future relative to bar `i`. Is the
  engine's `armed_i = b + window` sufficient to make that honest? Can any event be acted on before
  the data that defined it existed?
- In the lifecycle loop, does any event use a bar at or before its own `armed_i`?
- In `scripts/backtest_divergence.py`, does `score()` ever read a bar at or before the confirmation
  bar? Does `aggregate_1m` ever emit a partially-formed bucket?

**RSI correctness.** Verify `wilder_rsi` against an independent implementation (pandas-ta, TA-Lib, or
by hand) on the same input. Wilder smoothing is `alpha = 1/period`, not a simple moving average of
gains. Check the seeding of the first value and the `avg_loss == 0` branch.

**Pivot logic.** `find_pivots` is strict on the left and inclusive on the right. Does that produce
exactly one pivot on a plateau of equal highs? Does it miss pivots at the very start or end of the
series in a way that matters? Is `pivot_window: 5` defensible on 3-minute NIFTY bars, or arbitrary?

**Divergence classification.** Check `classify()` against the standard definitions. Specifically:
regular bearish is price higher-high with RSI lower-high; hidden bullish is price higher-low with RSI
lower-low. Are any of the four inverted?

**Trigger and invalidation placement.** For a bearish watch the trigger is the lowest low between the
two pivot highs and the invalidation is the later pivot high plus a buffer. Is that the right way
round? Can `invalidation` ever sit on the wrong side of `structure_trigger`, making the event
unreachable or instantly invalid?

**Thresholds.** `pivot_window: 5`, `max_pivot_gap_bars: 90`, `max_lookback_pivots: 6`,
`min_price_diff_pts: 8`, `min_rsi_diff: 3`, `expiry_bars: 10`, `sl_buffer_pts: 12`. The author chose
these by judgement, not measurement, after a single live session. Are any of them obviously wrong?
Would a sweep over the 35 stored sessions change the conclusions below?

**The backtest's honesty.** `scripts/backtest_divergence.py` scores each confirmed signal by **MFE**
(maximum favourable excursion) divided by risk. MFE is the best the trade ever looked, not an
achievable exit, so reported R is an **upper bound** and real results with a fixed target would be
worse. Confirm that. Also check whether the walk-forward "was the stop hit before the target"
ordering is actually correct, and whether `--regime-gate`'s use of first-half price direction as a
stand-in for cumulative Net PE−CE is a fair proxy or a biased one.

**VIX engine.** `_series_at` takes the most recent observation within `match_tolerance_s` and never
interpolates. Can a stale series still sneak a value through at one window edge but not the other,
and does the code handle that? Is `UNAVAILABLE` genuinely unreachable-by-accident?

**Statistical validity.** The headline results below come from 35 confirmed signals across 35
sessions. Is that enough to conclude anything? What confidence interval does 3/35 carry? Is the
regular-vs-hidden split (8 vs 27 signals) large enough to support the claim that they behave
differently, or is that noise?

## Results the author reported — reproduce or refute them

Run: `python3 scripts/backtest_divergence.py`

```
confirmed signals : 35
reached 2R        : 3  (9%)
stopped first     : 9  (26%)
median R (MFE)    : 0.51
reversal (regular)    :  8 signals, 0 reached 2R (0%), median R 0.19
continuation (hidden) : 27 signals, 3 reached 2R (11%), median R 0.74
```

The author concluded from this that the engine is **not tradeable as a standalone entry signal** and
should be used only as context, and separately that reversal divergence specifically is worthless on
this instrument and timeframe.

Do you agree? Check whether the numbers reproduce, whether the measurement is sound, and whether the
conclusion follows from it. If the engine is worse than reported, say so. If the author's framing is
flattering the result anywhere, point it out.

## Also worth checking if you have time

- `ict_smc_app/frontend/src/components/deskState.ts` and `regime.ts` — a permission/regime/action
  state machine and a day-regime veto, both written tonight by the same author.
- Whether the frontend can ever present an `ARMED` watch in a way that reads as an instruction. The
  explicit design intent is that only `STRUCTURE_CONFIRMED` may interrupt the trader, because this
  desk's previous divergence indicator produced five bearish warnings on a session that rose 217
  points and one of them became a losing trade.

## Output I want

1. Any correctness bug, with the file, line and a failing case that demonstrates it.
2. Any look-ahead bias or measurement error in the backtest, with evidence.
3. Your own verdict on whether these signals are tradeable, and what the data actually supports.
4. Anything the author claimed that you could not verify.
