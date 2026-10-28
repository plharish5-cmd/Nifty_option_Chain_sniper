# Kronos on NIFTY — Step 1 audit (independent, code-level)

**Date:** 2026-10-26
**Auditor scope:** I read the Kronos model and reference implementation myself and checked the
desk's assumptions against the actual source. **Important honesty caveat up front:** this session
could **not** reach your GitHub repo, and your *desk* integration files were not attached, so I have
**not** yet read your own `kronos_forecast.py`, `kronos_api.py`, `KronosPanel.tsx`,
`test_kronos_forecast.py` or `futures.py`. What I *did* read is the **Tradehull reference bundle you
uploaded** (`4__ai_trained_on_stock_market.zip`) — the original implementation your previous session
built the desk tab from. That bundle contains the real model (`model/kronos.py`, `model/module.py`),
the path sampler (`kronos_paths.py`), the sidecar service (`service.py`), `backtest_hourly.py` and
`compare_random.py`.

So this audit is authoritative about **the model contract your desk calls into** and **the shared
reference logic**, and it lists every desk-specific item as "verify in your file" with the exact line
to look at. It is not a rubber stamp — several items below are real and at least one is a latent crash.

---

## A. What the previous session got RIGHT (verified against source, not assumed)

1. **Feature column order `[open, high, low, close, volume, amount]` — correct.**
   `KronosPredictor.price_cols = ['open','high','low','close']`, `vol_col='volume'`, `amt_vol='amount'`
   (`model/kronos.py` L489–492), and the feature matrix is `df[price_cols + [vol_col, amt_vol]]`
   (L540, and `kronos_paths.COLS` L18). The brief's assumed order is exactly right. `amount` is
   auto-filled as `volume * mean(OHLC)` when absent (L532 / `kronos_paths.py` L174).

2. **`calc_time_stamps` contract — correct, and naive IST is REQUIRED, not merely acceptable.**
   `calc_time_stamps` returns five columns in this order: `minute, hour, weekday, day, month`
   (`model/kronos.py` L472–479), read via the pandas `.dt` accessor, so it needs a **datetime64
   Series**. Those five integers are fed into `TemporalEmbedding` (`model/module.py` L536–562),
   which are **`nn.Embedding` lookups with hard-coded vocab sizes**: minute 60, hour 24, weekday 7,
   day 32, month 13. Consequences:
   - The fields must be wall-clock **local** values. `pandas.dt.weekday` is 0–6 (Mon=0) ✓, `day`
     1–31 ✓, `month` 1–12 ✓, `hour` 0–23 ✓.
   - If you pass **naive IST**, `hour` = 9..14 and `weekday` = the real Indian trading weekday —
     correct, and this is what Kronos was trained on (local exchange time).
   - If anyone converts to **UTC or a tz-aware stamp**, `hour` becomes 3..9 and the weekday can flip
     near midnight. It will **not crash** (still < vocab 24) — it will **silently feed the model the
     wrong time-of-day**, degrading every forecast with no error. This is the single most important
     correctness dependency in the whole tab.
   - **ACTION (your file):** open `kronos_api.py` and confirm the hourly timestamps handed to
     `sample_paths` are tz-naive IST. If they are `tz_localize`'d or `tz_convert`'d to UTC anywhere,
     that is a real bug even though all 239 tests pass (synthetic tests won't catch a tz mistake).

3. **The band and up-probability come from a loop that KEEPS every path — correct, and this is the
   subtle trap the previous session avoided.** The *stock* `KronosPredictor` averages the sampled
   paths into one smooth line: `auto_regressive_inference(...)` ends with `preds = np.mean(preds,
   axis=1)` (`model/kronos.py` L467). If the desk had called `KronosPredictor.predict(sample_count=30)`
   for the band, the 30 runs would have been **averaged into a single path and the band would
   collapse to zero width.** The reference instead uses a forked loop, `kronos_paths.sample_paths`,
   whose docstring explicitly says it is "the same inference loop, unchanged, minus that final
   average" (`kronos_paths.py` L1–4), and it returns `(n_paths, pred_len, 6)` with paths intact.
   - **ACTION (your file):** confirm `kronos_forecast.py` / the sidecar calls `sample_paths(...)`
     (paths kept) and **never** `KronosPredictor.predict(..., sample_count=30)` (paths averaged).

4. **`up_prob` is a COUNT, labelled as such — correct.** `up_prob = (end > last).mean()`
   (`service.py` L259). It is the fraction of the 30 sampled paths that end above the last close.
   That is a Monte-Carlo vote share, **not** a calibrated probability. As long as the UI presents it
   as "N of 30 runs closed up", that is honest. If the UI calls it a "probability of going up",
   that is overclaiming — see §D.

5. **Dropping the 15:15 quarter-hour stub — correct and deliberate.** `SLOTS =
   ["09:15","10:15","11:15","12:15","13:15","14:15"]` with the comment: since 3 Aug 2026 Dhan's
   intraday feed stops at 15:15, so the 15:15–15:30 stub is excluded and every retained candle is a
   true 60 minutes (`service.py` L6–7, L40). So **6 true-hour candles/day**, and 360 context candles
   ≈ 60 sessions. This answers the brief's item #3: the 09:15 start and the 15:15 exclusion are
   deliberate, documented choices, not accidents — *provided your desk copied the 6-slot list*.
   - **ACTION (your file):** the *older* research script `backtest_hourly.py` (L15) uses a **7-slot**
     list that still includes 15:15. If your desk's slot list was copied from `backtest_hourly.py`
     rather than `service.py`, your forecast timestamps and "next day" indexing are off by one slot
     per day. Confirm the desk uses the **6-slot** list.

6. **The fast (KV-cached) loop is a legitimate optimization of the reference loop.** `_infer_fast`
   (`kronos_paths.py` L109–162) caches self-attention K/V and feeds one new token per step instead
   of re-running the whole window. Its correctness rests on two facts I confirmed in `module.py`:
   (a) the dependency layer's cross-attention always has a **length-1 query**, so RoPE on the query
   is position-0 = identity and the keys receive no rotation in *either* loop (`module.py` L371–379
   with q_len=1; `kronos.py` `decode_s2` is always called with a 1-wide `s1_ids`); (b) self-attention
   RoPE uses absolute positions, which match between the two loops **only while the window never
   slides** — which is exactly what the `assert L0 + pred_len <= max_context` guarantees
   (`kronos_paths.py` L114). I have written a runnable equivalence test (`test_fast_equiv.py`) that
   proves fast == slow candle-for-candle on a random-init model; result is in §C.

---

## B. Real problems and fragilities (most severe first)

### B1. (LATENT CRASH) `hist_candles` has a hard ceiling of 488; raising it past that in `rules.json` crashes the default path.
`_infer_fast` asserts `L0 + pred_len <= max_context` (`kronos_paths.py` L114), i.e.
`hist_candles + 24 <= 512`, so **`hist_candles` must be ≤ 488**. `sample_paths` defaults to
`fast=True` (L165), so this is the path the desk actually runs. At the shipped 360 it is fine
(360+24=384). But the brief says thresholds live in `rules.json` and asks whether `hist_candles` can
be raised — **if anyone sets it to 489+ the tab dies with `AssertionError: history + forecast must
fit in the context window`**, and because it is the *default* path there is no graceful fallback.
- **Fix:** either clamp `hist_candles` to 488 where `rules.json` is loaded (and surface a clear
  error), or document the ceiling at the rule definition. The slow loop (`fast=False`) would survive
  via its rolling buffer (`kronos.py` L447–454), but silently switching to it changes the numbers, so
  clamping + an explicit message is better than a silent fallback.
- Confirmed by the boundary test in §C.

### B2. (OPERATIONAL — can starve your live desk) Dhan fetches in the reference bypass the budget counter entirely.
Your brief (item #4) worries the cold Kronos cache can starve the live desk's 7,000/day Dhan budget
(`app/dhan/rest.py`, `desk_data_budget`). **The reference bundle has no budget concept at all** — it
calls `https://api.dhan.co/v2/charts/intraday` directly through raw `urllib` (`service.py` L142–156,
`fetch_hourly.py` L37). A cold fetch pages 180 days in 80-day windows ≈ 3 calls/symbol
(`service.py` L159–167), and `candles()` re-fetches whenever the CSV is older than 600 s
(`FRESH_SECONDS`, L44, L189) — so during market hours a Kronos page refresh can fire uncounted Dhan
calls every 10 minutes.
- **ACTION (your file):** open `kronos_api.py` and confirm its candle fetch goes **through
  `app/dhan/rest.py`** (so it decrements `desk_data_budget`) and not through a copied raw-`urllib`
  fetch. If it copied the reference pattern, it is invisible to the budget and the brief's fear is
  real. **Fix:** route all Kronos Dhan calls through the same budgeted client the desk uses, and make
  Kronos yield when the remaining budget is low so it can never starve live trading.

### B3. (CORRECTNESS, needs a real call) Index↔futures hourly timestamp alignment is unverified and fails *silently* to 0% volume.
The desk attaches futures hourly volume to index hourly price bars by **exact epoch merge**, and
drops the volume column if coverage < 95% (brief, and this guard is desk-only — the reference feeds
volume unconditionally, `service.py` L249). The failure mode is nasty: if Dhan stamps the 60-minute
**FUTIDX** candle even one minute differently from the 60-minute **IDX_I** candle, the exact-epoch
merge yields **0% coverage, volume is dropped with no error, and the forecast quietly runs
price-only.** This cannot be confirmed without a real Dhan call (Step 2).
- **ACTION (Step 2, your machine):** print both timestamp series and the actual coverage %. If they
  disagree, the fix is to merge on the **candle's IST calendar hour** (date + hour), not the raw
  epoch.

### B4. (DATA QUALITY) Futures volume depth over 360 bars is a claim, not a measurement.
The desk assumes the front NIFTY future has traded long enough to cover 360 hourly bars (~60
sessions). Expired contracts leave Dhan's scrip master, so older ones aren't stitched. If the current
front contract is younger than ~60 sessions, early context bars have **no** volume and (per B3) the
whole volume column may be dropped.
- **ACTION (Step 2):** print the real volume coverage % of the 360-bar context. Report it honestly;
  if it's routinely < 95%, the "volume" forecast (Q3 below) is meaningless on NIFTY and should be
  labelled as such or hidden.

### B5. (LABELS ONLY — low severity) NSE holidays are not modelled.
`_next_slots` skips only weekends (`service.py` L213–214), not NSE holidays. Over a 24-candle
(~4-session) horizon that spans a holiday, the forecast timestamps are mislabelled by one session.
**How much does it matter?** The forecast *values* are barely affected: the temporal embedding is an
**additive** fixed sinusoid of (minute,hour,weekday,day,month) (`module.py` L562), applied only to the
24 *future* steps; the 360-bar history is always correctly stamped from real closed candles. A
one-day label slip is a small perturbation to the conditioning of the generated candles, not a
structural break. So: **the x-axis dates and any "which session" labelling are wrong around holidays;
the forecast itself is only marginally affected.** Fix is cosmetic — feed an NSE holiday list into
`_next_slots` — and optional.

### B6. (ROBUSTNESS) Volume-channel normalization when volume is thin.
`sample_paths` z-scores each column independently: `xn = (x - mean)/(std + 1e-5)` (`kronos_paths.py`
L176–177). If volume is present but mostly zeros, its mean/std are dominated by zeros and the volume
channel becomes noise fed to the model; if volume is dropped entirely, `sample_paths` fills it with
0.0 (L171–174) → constant column → normalizes to 0 (no NaN, safe) but the model sees a dead volume
signal and its volume forecast is meaningless. This is the mechanism behind "no edge on volume" if
coverage is poor; it's not a crash, but it means **Q3 (volume) should be judged separately from Q1
(direction).**

---

## C. The one thing I could actually RUN here: fast-vs-slow equivalence

I cannot run a *real* forecast in this session (no 407 MB weights — the HuggingFace download is
blocked by this sandbox's proxy — and no Dhan token, which is correct; it must never leave your
machine). But fast-vs-slow equivalence is a property of the two **inference implementations** given
identical weights, so a small random-init model with the real architecture exercises the exact code.

`test_fast_equiv.py` builds a scaled-down but architecturally real Kronos, runs `sample_paths(...,
fast=False)` and `sample_paths(..., fast=True)` under an identical torch seed, and compares the
returned candles. It also probes the B1 assert boundary.

> **RESULT — ran in this session (torch 2.14.1 CPU, random-init model, no weights):**
> ```
> reference (slow)     shape (8, 24, 6)
> desk default (fast)  shape (8, 24, 6)
> max abs diff over all paths/candles/channels : 0.0
> max abs diff on CLOSE channel (price points) : 0.0
> VERDICT: PASS - fast loop == reference loop
>
> --- context-window assert boundary (fast path) ---
>   hist=488 + pred=24 = 512  -> OK
>   hist=489 + pred=24 = 513  -> AssertionError: history + forecast must fit in the context window
> ```
> **Conclusion:** the desk's default `fast=True` loop is **bit-identical** to the reference
> auto-regressive loop (difference exactly 0.0, not just "close"), so the KV-cache optimization is
> trustworthy. And the `hist_candles` ceiling is **exactly 488** — 489 crashes — confirming B1.

---

## D. Honesty about what the tab can and cannot claim (prep for Step 3)

The published Kronos backtests on Indian equities found **direction ≈ a coin flip** (47.6% vs 50.5%
for random paths) and the real close **outside** the band about **2 times in 3**. Nothing in this
code changes that — it is a faithful port of the same method, so there is **no a-priori reason to
expect edge on NIFTY** until the walk-forward test (Step 3) says otherwise on your own history.

`compare_random.py` scores four questions and I will run the same for NIFTY:
- **Q1 direction** (up/down next session) vs resampled-random paths and vs a coin flip.
- **Q2 volatility amplification** (Brier score vs random).
- **Q3 volume** (correlation vs random draws) — expect this to be the weakest, per B4/B6.
- **Q4 band coverage** — does the real close land in the 90% band 90% of the time? (Published: no.)

I have written the missing `backtest_hourly_data.load_hourly` loader that `compare_random.py` imports;
it returns the 6-slot hourly frame tagged by `day`, from a CSV you export on your machine. **For
NIFTY specifically, that CSV must be the desk's basis-adjusted future-volume hourly series**
(`market/futures.py mapped()`/`combined_1m()`), not raw index candles, or Q3 is meaningless by
construction.

**Bottom line I will commit to after Step 3:** if NIFTY direction hit-rate sits inside the random
luck band and the band coverage is well under nominal, I will tell you plainly that **this tab has no
edge on NIFTY** and should be treated as a visualization of model uncertainty, not a signal — which,
per your scope note, must never be wired into any intraday alert or score regardless.
