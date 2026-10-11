# NIFTY ICT / SMC desk — build guide

How the app is built, in the order to build it, what every rule actually is, a worked example on real NIFTY
candles (Thu 8 Oct 2026), and the ten mistakes that sink most home-built SMC tools.

---

## 0 · The idea in one picture

```
 Dhan WebSocket (binary ticks) ──┐                      ┌── REST /charts/intraday (backfill, gap repair, 15:31 truth)
                                 ▼                      ▼
                       ┌──────────────────────────────────────┐
                       │  Candles: forming 1m in memory        │   ticks are never stored
                       │  closed 1m → SQLite → 5m/15m/1h (09:15│
                       │  aligned)                             │
                       └───────────────┬──────────────────────┘
                                       │ only CLOSED candles go further
                 ┌─────────────────────▼─────────────────────┐
                 │ ENGINE  swings → structure (BOS/MSS) → FVG │
                 │ → order blocks → liquidity pools → sweeps  │
                 │ → dealing range (EQ / premium / OTE)       │
                 └─────────────────────┬─────────────────────┘
                                       ▼
                 PLAYBOOK A raid→MSS→FVG · B BOS confirm · C OTE      ──▶ alerts (dedupe, cooldown, cap) ──▶ Telegram
                                       │                                               │
                                       ▼                                               ▼
                                 paper trades (1m-resolved)               React UI over WebSocket (/ws)
 Dhan /optionchain (2 expiries, 1 req / 3 s) ──▶ chain table, walls, PCR, max pain, OI builds ──────────▲
```

Three rules hold everything together:
1. **Closed candles only.** Nothing the engine says is computed from the candle that is still forming.
2. **A swing is only known `n` bars after it formed.** Every rule checks `confirm_i ≤ bar`.
3. **REST is the source of truth for history; the WebSocket is only for "now".**

---

## 1 · Build order (each phase ends with something you can test)

| Phase | Build | Files | Done when |
|---|---|---|---|
| 1 Data | config + `.env` reader, IST time helpers, SQLite | `config.py`, `timeutil.py`, `db.py` | `pytest` passes the resample test |
| 1 Data | Dhan REST client with rate-limit buckets and error classes | `dhan/rest.py` | backfill writes 30 days of 1m candles |
| 1 Data | WebSocket feed: binary decoder, reconnect, time-base detect | `dhan/feed.py` | ticks arrive; kill Wi-Fi 30 s → it reconnects and repairs the gap |
| 1 Data | candle builder + resampler; option chain poller | `market/candles.py`, `market/chain.py` | 5m candles equal Dhan's 5m; chain table fills both expiries |
| 2 Engine | pivots, ATR, zig-zag → structure → FVG → OB → liquidity → range | `engine/*.py` | 8-Oct test reproduces the events in §4 |
| 3 Signals | playbook A/B/C with filters → alerts → paper | `signals/*.py` | textbook short test fires; cancelled-raid test does not |
| 4 API | hub (live pipeline) + FastAPI routes + `/ws` | `api/hub.py`, `api/main.py` | `/api/dashboard` returns everything |
| 5 UI | chart with zones primitive, chain table, panels | `frontend/src/*` | replay with `tools/fake_dhan.py` looks right at 12× speed |
| 6 Run | systemd service, Tailscale access, Telegram | README §7 | phone shows the desk at `100.113.93.21:8600` |

Build the phases in this order. Most people start at phase 5 (a pretty chart) and end up debugging the engine
through the UI.

---

## 2 · Dhan API v2 — what this app uses

| What | Endpoint | Notes |
|---|---|---|
| Auth headers (data) | `access-token: <JWT>` + `client-id: <id>` | token lasts **24 h** (SEBI); generate at web.dhan.co → My Profile → Access DhanHQ APIs |
| Renew token | `GET /v2/RenewToken` (`access-token`, `dhanClientId` headers) | only a **still-valid web** token; `AUTO_RENEW_TOKEN=1` does it at 08:35 |
| Intraday candles | `POST /v2/charts/intraday` `{securityId:"13", exchangeSegment:"IDX_I", instrument:"INDEX", interval:"1", fromDate, toDate}` | intervals 1/5/15/25/60; ≤ 90 days per call; epoch-second timestamps |
| Daily candles | built from 60m intraday | `/charts/historical` answers DH-905 for the NIFTY index (your OC app found this) |
| Option chain | `POST /v2/optionchain` `{UnderlyingScrip:13, UnderlyingSeg:"IDX_I", Expiry:"YYYY-MM-DD"}` | **1 unique request / 3 s**; OI, previous_oi, IV, greeks, bid/ask per strike |
| Expiries | `POST /v2/optionchain/expirylist` | always use this — never compute expiry dates |
| LTP fallback | `POST /v2/marketfeed/ltp` `{"IDX_I":[13,21]}` | quote APIs 1 / s; used only when the feed goes quiet > 10 s |
| Live feed | `wss://api-feed.dhan.co?version=2&token=…&clientId=…&authType=2`, subscribe `{"RequestCode":15,…}` | 5 sockets per user, 100 instruments per subscribe message, server pings every 10 s |
| **Trading** | `/v2/orders`, `/super/orders`, `/forever/orders` | need a whitelisted static IP — **not used anywhere in this app** |

Rate limits: data 5 / s (100 000 / day), quote 1 / s, non-trading 20 / s, orders 10 / s. Errors that matter:
DH-901 / 807 / 808 / 809 = token; DH-902 / 806 = Data plan; DH-904 / 805 = too many requests or sockets;
DH-905 = bad body.

Binary feed header (little-endian, 8 bytes): `code u8 · length u16 · segment u8 · security id u32`; ticker body
`LTP f32 · LTT i32`; prev-close body `close f32 · OI i32`; disconnect body `reason i16`.

---

## 3 · The concepts — your ICT ↔ SMC table, as code

| ICT | SMC | Exact rule in this app | File |
|---|---|---|---|
| Liquidity zones | Liquidity sweep / stop losses | Pools = confirmed swing highs/lows, equal highs/lows within max(4 pts, 0.15×ATR), PDH/PDL, opening range (09:15–09:30), initial balance (09:15–10:15). **Sweep** = wick ≥ 0.5 pt beyond, close back inside. **Run** = close beyond. | `engine/liquidity.py` |
| Trading range | Price range / Fibonacci | Dealing range = the leg that made the latest structure break: origin (protected swing) → extreme since. Invalid after a close beyond the origin. | `engine/ranges.py` |
| Equilibrium 50 % | Fair price | (high + low) / 2 of the dealing range | `engine/ranges.py` |
| Discount / premium | Buying / selling zone | below EQ = discount (longs only), above = premium (shorts only) | `engine/ranges.py` |
| Order block | Supply & demand zone | last opposite-close candle at the leg's extreme, **only when the break had displacement**. States: fresh → mitigated → respected, or broken → breaker | `engine/orderblocks.py` |
| Fair value gap | Imbalance / market efficiency | 3-candle gap ≥ max(3 pts, 0.1×ATR); tracked: touched → 50 % CE → filled, or invalid on a close through | `engine/fvg.py` |
| Break of structure | BOS | close beyond the latest confirmed swing **with** the trend | `engine/structure.py` |
| Change of character | Market structure shift | close beyond the latest confirmed swing **against** the trend; "displacement" = body ≥ 1×ATR with ≥ 60 % body, or the leg left an FVG | `engine/structure.py` |
| Trading sessions | Session high & low | IST: open 09:15, OR to 09:30, IB to 10:15, trade windows 09:30–11:30 and 13:30–14:45, last entry 14:45, square-off 15:15 | `rules.json`, `engine/ranges.py` |
| Highs & lows connection | False breakout | zig-zag swing line on the chart; **false breakout** = a run followed within 2 bars by a close back inside | `engine/primitives.py`, `engine/liquidity.py` |
| Best entry point (OTE) | Entry at 61.8 % | 61.8 / 70.5 / 79 % retracement of the dealing range | `engine/ranges.py`, playbook C |

All numbers live in `backend/app/rules.json`.

### The playbook

**A · Raid → MSS → FVG** (the ICT model)
1. Sweep or false breakout of a pool (BSL for shorts, SSL for longs). If two pools sit at the same price, the
   stronger one counts (PDH/PDL > EQH/EQL > OR/IB > swing).
2. Cancelled if any close goes back beyond the raided level before the entry.
3. Opposite structure break **with displacement** within 12 bars.
4. FVG formed by that leg (after the raid, at most one bar after the MSS).
5. Within 10 bars of the MSS, a candle trades into the FVG and **closes** inside it → entry at the close.
6. Filters: entry in premium (short) / discount (long) of the raid range; 15m trend not against the trade; inside
   a trade window; risk 8–45 pts; R:R ≥ 1.5 to the nearest opposing pool known at the entry (else 2R, labelled).
   Stop = the raid extreme + 2 pts.

**B · BOS / MSS with displacement** — information only.

**C · OTE** — the first closed candle inside the 61.8–79 % band after a break; stop beyond the origin, target the
range extreme, same filters.

Every evaluation is shown as TRADE or SKIP with its reasons. The skips teach you more than the trades do.

---

## 4 · Worked example — Thursday 8 Oct 2026 (real candles)

Source: your dashboard's 5m chart (13:10–15:25) and the broker's 15m chart, read to about ±1 pt
(`backend/tests/oct8_data.py`). The day: open 22,603.05, low 22,179.90 at 15:05, close 22,231.80. The 15m
trend was down from the open.

### What the engine sees on 5m (closed candles, ATR ≈ 22 pts)

| Time | Event | Engine output |
|---|---|---|
| 13:30 | swing low 22,227.8 | SSL pool (confirmed 13:40) |
| 14:00 | close 22,211.9 below 22,227.8, body 29 pts | **BOS bearish, displacement** · supply OB 22,264.7–22,291.0 (the 13:35 candle) |
| 14:10 | close 22,236.3 back above 22,227.8 | **false breakout** of SSL 22,227.8 (bullish) |
| 14:15 | swing high 22,244.7 | BSL pool |
| 14:20 | low 22,190.0 | swing low → SSL pool |
| 14:30 | wick 22,245.7, close 22,235.4 | **sweep** of BSL 22,244.7 — *1 point* |
| 14:35 | close 22,248.6 above 22,244.7 | **MSS bullish** (bull FVG 22,215.7–22,233.3, 17.6 pts) → **the 14:30 sweep is cancelled** |
| 14:45 | high 22,233.0 vs 14:35 low 22,233.3 | a 0.3-pt gap → **ignored** (minimum 3 pts / 0.1×ATR) |
| 15:05 | close 22,186.1 below 22,190.0, bear FVG 22,195.8–22,201.0 (5.2 pts) | **MSS bearish, displacement** · supply OB 22,233.3–22,255.9 (14:35 candle, fresh) |
| 15:10 | close 22,215.8 back above 22,190.0 | **false breakout** of SSL 22,190.0 (bullish) |
| 15:25 | close 22,231.8 | dealing range 22,179.9–22,255.9 · EQ 22,217.9 · **in premium (68 %)** · OTE 22,226.9 / 22,233.5 / 22,239.9 → price is **inside the OTE band** |

### What the playbook says

* **Setup A short (14:30 raid of 22,244.7):** cancelled at 14:35 by the close above the swept high.
* **Setup A long (14:10 false breakout of 22,227.8):** cancelled at 14:20 by the close back below 22,227.8.
* **Setup C short at 15:25:** entry 22,231.8, stop 22,257.9 (range high + 2), target 22,179.9 (the range low = the
  sell-side liquidity), risk 26.1, R:R 2.0. All filters pass **except time**: 15:30 is after the 14:45 last entry.
  → **SKIP.** Correct: a 2R short with 15 minutes left is a lottery ticket, not a trade.
* **Result: no trade on 8 Oct.** The old Fakeout tab painted "SELL HERE" at 14:50 on the 0.3-pt gap, after a
  cancelled sweep and before the MSS. It worked to 1R by luck of the 15:05 drop. Under these rules it does not
  exist.

### What the 15m chart adds

The 15:00 15m candle (O 22,203.2 · H 22,219.1 · L 22,179.9 · C 22,215.8) is a clean **sweep** of the 14:15 15m low
(22,190). On 5m the same move printed as a close below and a close back above, i.e. a false breakout. The higher
timeframe often shows a raid as one wick where the lower timeframe needs two candles. That is why the app runs
every timeframe and shows the bias grid.

### A data lesson from the same day

The 15:15 and 15:20 5m candles on your dashboard were flat at 22,215.9: the feed stalled. A flat candle next to a
moving one creates a fake bullish FVG (22,195.8–22,215.9). That is why the app re-downloads the day's candles
after every reconnect and again at 15:31. Never analyse candles you built during a feed gap.

---

## 5 · Top 10 mistakes to avoid

1. **Polling instead of streaming.** Calling `/marketfeed/ltp` every second for price burns the 1 / s quote
   limit and still lags. Stream ticks over the WebSocket; use REST only for history, gap repair, and as a fallback
   when ticks stop for 10 s.
2. **Ignoring rate limits until you are blocked.** The option chain allows one unique request per 3 s. Two
   expiries means alternate them (each refreshes every ~6 s); do not fire both together. Back off exponentially on
   429 / DH-904 / 805; do not retry in a tight loop.
3. **Mixing time zones.** Store epoch seconds, display IST, align candles to **09:15** (an "hourly" NIFTY candle
   is 09:15–10:15, not 09:00–10:00). Lightweight Charts draws UTC, so shift by +5:30 only at the edge. Detect the
   feed's LTT time base instead of assuming it.
4. **Storing ticks forever.** At NIFTY volumes a day of ticks is useless weight. Keep 1m candles (≈ 375 a day),
   rebuild every other timeframe from them, prune option-chain snapshots after a few days.
5. **Hard-coding expiries.** NIFTY weeklies moved to Tuesday and holidays move them again. Take dates from
   `/optionchain/expirylist`, and drop expiries that have passed.
6. **Signalling on the forming candle.** A sweep, an FVG or "SELL HERE" computed from an open candle can vanish
   by the close; the chart then repaints and looks like genius. Every signal here waits for the candle to close.
7. **Look-ahead in swings.** A fractal high is only known `n` bars later. Using it at the bar it formed is the
   most common reason SMC backtests show 70 % win rates that never appear live.
8. **Treating every wick as a sweep, and every gap as an FVG.** Without minimum sizes (3 pts / 0.1×ATR), without
   cancelling a sweep when price closes back beyond it, and without requiring displacement on the MSS, the chart
   fills with signals and none of them mean anything.
9. **Forgetting the plumbing limits.** Five WebSockets per Dhan user (your Streamlit dashboard and
   orderflow_recorder count), a 24-hour token, and index volume that is always zero (any "index VWAP" is
   futures VWAP underneath). Show token and feed health in the UI so you notice before 09:15.
10. **Over-alerting.** Twenty pings a day train you to ignore the one that matters. Alert only on closed-candle
    setups that pass every filter; dedupe by key, cool down 15 min per setup, cap at 10 a day; keep BOS/OTE info
    in the app. Paper-trade at least 30 setups before you believe the win rate, and never wire real orders until
    then.

---

## 6 · Replaying a day after hours

`tools/fake_dhan.py` speaks Dhan's REST and binary WebSocket formats and replays 8 Oct on a simulated clock
(README §4). Use it whenever you change the engine: run the replay, watch the playbook panel, then
`pytest -q`. The fake previous day (7 Oct) and the option chain are synthetic. Only the 8 Oct candles are real.

## 7 · What to build next

* Futures candles (NSE_FNO) for real volume and VWAP, the way your OC app already does.
* An intraday OI-change chart from `chain_snap` (stored every minute already).
* A per-setup expectancy report once the paper log has 30+ closed trades. Then decide, with numbers, which
  setup earns a place in your real trading.

---

## 8 · Zone registry + Delta / Footprint (added 9 Oct 2026)

Nothing here changes a trigger. Playbook A/B/C fire exactly as in §3. Delta only adds **overlays**, **events**
and a **confidence** number (with reasons) on Setup A / C cards.

### 8.1 Ground-truth rules (unchanged, now explicit)

| Concept | Rule | Config (`rules.json`) |
|---|---|---|
| FVG = Imbalance | 3-candle gap, size ≥ max(3 pts, 0.1×ATR). State: fresh → touched → ce (50 %) → filled; **invalid** on a close through the far edge | `engine.fvg_min_pts`, `engine.fvg_min_atr` |
| Displacement | break candle body ≥ 1×ATR **and** ≥ 60 % of its range, **or** the leg left an FVG | `engine.displacement_body_atr`, `engine.displacement_body_ratio` |
| Order block | last opposite-close candle at the leg extreme, **only** for a displacement break. fresh → mitigated → respected, or broken → breaker | `engine.ob_max_age_bars` |
| Liquidity | swings, EQH/EQL within max(4 pts, 0.15×ATR), PDH/PDL, OR, IB. Sweep = wick ≥ 0.5 pt beyond + close back inside; Run = close beyond | `engine.eq_tolerance_*`, `engine.sweep_min_pen_pts` |
| "Imbalance" | always means the FVG above. The footprint version is always called **Footprint imbalance** (or **Proxy imbalance**) | `delta.footprint.*`, `delta.proxy.*` |

### 8.2 Zone registry — data model (`zones` table, `engine/registry.py`)

| Field | Meaning |
|---|---|
| `id` | `TYPE-tf-created_at-hash(type,tf,side,created_at,low,high)` — built from **candle timestamps**, never bar indices, so a re-download cannot duplicate it |
| `type` | `LIQ_POOL` · `ORDER_BLOCK` · `FVG` |
| `timeframe` | 1m · 5m · 15m · 1h |
| `side` | `BSL`/`SSL` (pools) · `SUPPLY`/`DEMAND` (OB, FVG) |
| `price_low`, `price_high` | zone edges (a pool: low = high) |
| `created_at` | start of the closed candle that created it (OB: the displacement break candle; FVG: the 3rd candle; pool: the confirming candle) |
| `mitigated_at` | first touch (FVG touched / OB mitigated / pool swept) |
| `invalidated_at` | FVG close through · OB close through (broken) · pool run |
| `state` | pools: fresh, swept, run · OB: fresh, mitigated, respected, broken, expired, breaker, breaker_mitigated, breaker_respected, breaker_broken · FVG: fresh, touched, ce, filled, invalid · any: **void** |
| `source_event_id` | `STRUCT|tf|dir|ts` for the break that made an OB/FVG, `LIQ|tf|kind|side|ts|level` for the event that consumed a pool |
| `extra` | anchor candle, CE price, `ce_at`, `filled_at`, `respected_at`, pool source, `void_reason` |

**Rules:** set-once timestamps, the state rank only goes up, and nothing is ever deleted (only pruned after `zones.keep_days`).
A zone becomes **void** only after a REST repair (start-up, reconnect, 15:31, `POST /api/repair`), when the
corrected candles no longer produce it and it was created inside the recomputed window, past the ATR/pivot
warm-up. That is how the 8-Oct "flat feed-stall candle" fake FVG disappears without rewriting history.

```
reconcile(tf, fresh_zones, window, after_repair):
    stored ← SELECT zones WHERE tf
    for z in fresh_zones:
        s ← stored[z.id]
        if not s: INSERT z
        else:     s.mitigated_at ← s.mitigated_at ?? z.mitigated_at          # set once
                  s.invalidated_at ← s.invalidated_at ?? z.invalidated_at
                  if rank(z.state) > rank(s.state) and s.state ≠ void: s.state ← z.state
    if after_repair:
        for s in stored − fresh_zones with window.start + warmup ≤ s.created_at ≤ window.end:
            s.state ← void
```

### 8.3 FVG and OB pseudocode (what `fvg.py` / `orderblocks.py` do)

```
FVG: for i ≥ 2 (closed):  need ← max(fvg_min_pts, fvg_min_atr × ATR[i])
       bull if low[i] − high[i−2] ≥ need  → gap (high[i−2], low[i])
       bear if low[i−2] − high[i] ≥ need  → gap (high[i], low[i−2])
     for j > i:  touched (first trade into the gap) → ce (50 %) → filled (far edge traded)
                 close beyond the far edge → invalid, stop
OB:  for each structure break e with e.displacement:
       k ← last opposite-close candle at e.origin (or ≤ 3 bars before it)
       zone (low[k], high[k]); for j > e.i (closed):
         close through far edge → broken (+ a BREAKER record of the other side) · stop
         first trade into the zone → mitigated;  then a close back out in its direction → respected
         age > ob_max_age_bars → expired
```

### 8.4 Delta / Footprint

**Reading:** delta = aggressive buys − aggressive sells. **Positive = buyers attacking, negative = sellers attacking.**

**Data honesty:** the NIFTY index has no volume, and Dhan's v2 feed does not tag the aggressor, so the app
**cannot** compute real delta from what it already receives. Choose a source in the *Delta / Footprint* tab
(or `delta.adapter`):

| Adapter | What it is | Status shown |
|---|---|---|
| `footprint_feed` (default) | Real-feed aggregator (1m × price levels), complete and tested. Only the source is a TODO: call `ingest_trade(ts, price, qty, 'buy'/'sell')` from an aggressor-tagged NIFTY futures feed | "Delta unavailable" until trades arrive |
| `csv` | Upload from your footprint tool. Bars: `timestamp,delta` or `timestamp,buy_vol,sell_vol`. Levels: `timestamp,price,buy_vol,sell_vol` | rows + time range |
| `mock` | Deterministic fake numbers for UI and tests | amber **MOCK** banner everywhere; mock events are never stored in the log |

**CSV time policy:** naive times are **IST**. ISO with an offset or `Z` is honoured. Epoch s/ms are accepted.
`ts_is=end` subtracts one bar. Rows must start inside 09:15–15:30. Off-grid rows are floored (or rejected with
`misaligned=reject`) to the 09:15-anchored grid. A 5m/15m/1h candle gets a delta only if the rows cover
≥ `min_coverage` of it. Re-uploading the same candle replaces it.

```
compute(an, zones, adapter, now):                       # CLOSED candles only
    for i in bars while start[i] + tf ≤ now:
        d[i] ← adapter.get_delta(bar i)        levels[i] ← adapter.get_footprint_levels(bar i)
        z[i] ← (d[i] − mean(d[i−W..i−1])) / std(...)      # baseline excludes bar i; needs ≥ min bars
        skip bar i for rules if range < min_candle_range_atr × ATR      # a stalled flat candle proves nothing
        extreme ← |z| ≥ aggression_z  and |d| ≥ aggression_min_abs
        hit ← registry zones active at bar i that its range overlaps (± touch tolerance)
        extreme                                  → DELTA_AGGRESSION (dir = sign d)
        extreme ∧ hit ∧ low progress             → DELTA_ABSORPTION @ i  (rule 1, dir = −sign d)
             low progress = body/range ≤ rule1_max_body_ratio  or  close ≥ rule1_min_close_off_extreme of range from the attacked extreme
        bar i−1 ENTERED a zone with extreme delta ∧ close[i] fails to extend close[i−1] by max(pts, k×ATR)
                                                 → DELTA_ABSORPTION @ i  (rule 2, stamped on the NEXT candle)
        sign flip, both |z| ≥ flip.min_z, at a zone or ≤ flip.max_bars after a sweep → DELTA_FLIP
        levels: ≥ stacked_min consecutive ratio imbalances (diagonal) touching a zone edge → FOOTPRINT_IMBALANCE
        no levels: |z| ≥ proxy.min_z + wick ≥ proxy.min_wick_ratio of range at a zone → FOOTPRINT_IMBALANCE (proxy=true)
```

**Event shape** (`events` table, unique id, so there are no duplicates): `id, event_type, ts, time, tf, zone_id, direction,
confidence, explanation, rule, price, delta, z, proxy, mock, source`. `direction` = what it implies for price
(absorbed buyers → `bear`). `GET /api/events` merges these with BOS/MSS/sweeps into one timeline. Each row
carries its full zone record, for the inspector.

### 8.5 Confluence (Setup A / C confidence only)

Base `confluence.base` = 0.50. Only events known at the entry candle's close, from the last `lookback_bars`:

| Pattern | Adj. | Example text on the card |
|---|---|---|
| Short + absorption at a SUPPLY OB (long: DEMAND) | +0.15 | "Buyers attacked inside supply OB … but price didn't move up → absorption → short bias stronger." |
| Absorption inside a FVG of the trade side | +0.10 | "aggression absorbed inside bearish FVG … → the gap is being defended." |
| Setup A: sweep, then flip/absorption in the trade direction ≤ `flip.max_bars` | +0.15 | "14:30 sweep, then 14:35 delta flipped to sellers at the sweep → reversal confidence up." |
| Footprint/proxy imbalance in the trade direction at a zone | +0.05 | |
| Aggression **against** the trade that was not absorbed | −0.15 | "Buyers attacked … and were NOT absorbed → short confidence lower." |

With no delta source, every A/C card says "Delta unavailable — confidence from structure only" at 0.50.

### 8.6 New `rules.json` keys

`zones.*`: persist, forward_only, void_missing_after_repair, price_round, keep_days, touch_tolerance_pts/atr ·
`delta.*`: adapter, timeframes, min_coverage, baseline_window, baseline_min_bars, baseline_method (zscore |
percentile), aggression_z, aggression_percentile, aggression_min_abs, min_candle_range_atr,
absorption.rule1_max_body_ratio, absorption.rule1_min_close_off_extreme, absorption.rule2_min_follow_pts,
absorption.rule2_min_follow_atr, flip.min_z, flip.max_bars, footprint.imbalance_ratio, footprint.imbalance_mode,
footprint.min_level_volume, footprint.stacked_min, footprint.boundary_tolerance_pts, proxy.enabled, proxy.min_z,
proxy.min_wick_ratio, keep_days · `confluence.*`: base, lookback_bars and the bonus/penalty weights.

The weights are starting guesses, not measured edges. Keep them until 30+ paper trades exist with a real
delta source, then compare win rate by confidence bucket.

### 8.7 UI

* Chart layer **Delta**: ABS (gold dot) and FLIP (white square). **Δ detail** adds Δ+/Δ− aggression and IMB/pIMB markers.
* Bottom tab **Delta / Footprint**: source switch, CSV upload with an import report, delta bars per closed candle
  (bright = |z| ≥ 2, gold dot = absorption), and the **event inspector** (tap an event to see its zone record and reason).
* Playbook cards: a confidence bar plus Δ lines (green = raised, red = lowered).

---

## 9 · Strike mode: selected CE / PE for flow, structure stays on NIFTY (added 9 Oct 2026)

### 9.1 Repo Fit Report

**ICT desk (`ict_smc_app/`)**

| Area | Files | Change |
|---|---|---|
| Engine | `engine/primitives.py · structure.py · liquidity.py · fvg.py · orderblocks.py · ranges.py · analyze.py` | **none** |
| Zone registry | `engine/registry.py` (§8) | none, stays anchored to NIFTY index candles |
| Playbook A/B/C | `signals/playbook.py` | new fields on `Signal` only (`confidence_parts`); triggers untouched |
| Data | `dhan/rest.py · dhan/feed.py · market/candles.py · market/chain.py` | none. `rest.intraday()` already takes `segment / instrument` |
| **New** instrument model | `market/instruments.py` | INDEX / OPTION identity, key, resolver from the chain's `security_id` |
| **New** option candles | `market/options.py` | REST NSE_FNO / OPTIDX 1m, stored as `candles_1m` sec_id `O:<sid>` |
| **New** strike selector | `strikes/priyanka.py · strikes/selector.py · strikes/context.py` | OC rule port, plugin `select_strikes()`, 3m switching, focused panel, verdict |
| Delta | `delta/adapters.py · delta/engine.py · delta/confluence.py` | adapters take `instrument`; strike flow at index zones; confidence breakdown |
| **New** proxy | `delta/proxy.py` | Options Flow Proxy, `PROXY_FLOW_*` only |
| Paper | `signals/paper.py` | strike snapshot + premium P&L columns |
| DB | `db.py` | `delta_bars / footprint_levels` keyed by `inst` (auto-migrated), `strike_picks`, paper columns |
| API / hub | `api/hub.py · api/main.py` | selection on chain refresh, option candles after each 1m close, new routes |
| UI | `InstrumentPanel.tsx` (new) · `App.tsx · Chart.tsx · Panels.tsx · DeltaPanel.tsx · api.ts` | panel, option chart mode, breakdown bars, sources, proxy toggle |
| Tests | `test_baseline_lock.py · test_strikes.py` (new) | 16 new tests; the 27 existing tests are unchanged |

**OC dashboard (`OC/`, Streamlit), read but not changed**

| File | What it holds | Used how |
|---|---|---|
| `apply_strike_advisor_tab.py` | patcher that adds the "🧭 Strike Advisor" tab | not needed |
| `options_lab_tab.py` | Options Lab; `_one_expiry`, `gamma_flip`, `gex_by_strike`; rules in `strategy1_rules.json → options_lab` | `gamma_flip` and the `gex_by_strike` core ported verbatim |
| `strike_advisor_pro.py` | **the decision**: `chain_now`, `scenarios` (Greeks Taylor), `pick` (MJ course pick + fast pick), `path_levels` | ported verbatim into `strikes/priyanka.py`; only the constants became parameters (same defaults); parity test against a verbatim copy |
| `olab_pro.py` (not provided) | `move_map` σ-day / rest-of-day move, the CE/PE side grade | **not ported**. The ICT playbook decides the side; the target is `fixed 40` (OC "Fixed pts") or `first_level` with the OC 60-pt fallback reach |

### 9.2 Data contracts

```
Instrument      {type: INDEX|OPTION, underlying: "NIFTY", expiry: "YYYY-MM-DD", strike: int, right: CE|PE,
                 security_id, segment: IDX_I|NSE_FNO, instrument: INDEX|OPTIDX, key, label}
                 key = "NIFTY"  |  "NIFTY|2026-10-13|22400|CE"      label = "NIFTY 13 Oct 22400 CE"
Pick            {instrument, role: best|fast|manual, ltp, delta, gamma, theta, vega, iv, oi, itm, kind, rr, fit,
                 ret_pct, spread, prem_tgt, prem_sl, course, why}
select_strikes  → {expiry, spot, calls: [Pick], puts: [Pick], reasons: [str], picks: {CE|PE: {move, target}},
                   scores: {CE|PE: {instrument key: fit}}}
strike_picks    (ts PK, expiry, ce_key, pe_key, payload)   one row each time the ACTIVE strike changes
delta event     §8 shape + instrument (key)                 direction is ALWAYS the implied NIFTY direction
proxy event     {id, event_type: PROXY_FLOW_SURGE|PROXY_FLOW_FADE, ts, tf, zone_id, direction, confidence,
                 explanation "PROXY (not real delta) · …", net_flow, z, proxy: true, label}
delta_bars      (inst, ts) PK, secs, buy, sell, delta, src     footprint_levels (inst, ts, price) PK, buy, sell
paper_trades    + instrument_mode, strikes_snapshot (json), opt_key, opt_sid, opt_entry, opt_exit, opt_pnl_pts,
                  opt_pnl_rs, confidence, confidence_parts (json)
Signal          + confidence_parts {structure, options_context | null, flow, flow_source: delta|proxy|none}
```

### 9.3 Pseudocode

```
select_strikes(snapshot)                                 # strikes/selector.py, wraps the OC rule
    ch     ← chain_frame(snapshot.chain)                 # ICT chain → OC column names (ce_ltp, ce_delta, …)
    ch     ← optional filters (|Δ| range, min OI / ΔOI / volume), all OFF by default
    levels ← put wall, call wall, max pain, gamma flip, gamma pin (max |GEX|), prev close, open
    for side in CE, PE:
        move ← target_pts (OC "Fixed pts", 40)  or  |path_levels(levels, spot, side, reach) − spot|
        sc   ← scenarios(ch, spot, side, move, stop 40, hold 30 min, ΔIV 0 / −0.5)     # OC, unchanged
        best, fast ← pick(sc)                                                          # OC, unchanged
        scores[side] ← course-pick fit of every qualifying strike
    StrikeBook: recompute on every chain refresh; switch the ACTIVE strike only on a 3m close,
                only if the new best ≥ 15 % better (or the old one stops qualifying); manual = pinned

strike flow at index zones                                # delta/engine.compute(…, flow={instrument, candles})
    for each CLOSED NIFTY bar t:
        opt ← selected strike's candle at t (REST, closed only)   d ← adapter.get_delta(opt, tf, instrument)
        z   ← (d − mean(previous W)) / std(previous W)
        hit ← registry zones that the NIFTY candle at t overlaps (zones stay on the index)
        dir ← implied NIFTY direction (buyers attacking a PE = bear)
        rule 1: |z| extreme ∧ hit ∧ option candle shows low progress          → DELTA_ABSORPTION @ t  (−dir)
        rule 2: bar t−1 entered a zone with extreme d ∧ option close at t does not extend → DELTA_ABSORPTION @ t
        flip / footprint imbalance as §8;  zero-range or zero-price candles never produce an event
    no source → "Delta unavailable", confidence = structure only

Options Flow Proxy (opt-in, PROXY (not real delta))        # delta/proxy.py
    net[t] ← (CEc − CEo)·CEvol·|Δce| − (PEc − PEo)·PEvol·|Δpe|
    z[t]   ← (net[t] − mean(previous W)) / std(previous W)
    |z| ≥ 2                                          → PROXY_FLOW_SURGE (dir = sign net)
    t−1 surged into an index zone ∧ NIFTY close at t doesn't extend → PROXY_FLOW_FADE (opposite dir)
    never emits DELTA_*; every text starts "PROXY (not real delta) ·"
```

### 9.4 New `rules.json` keys

`strikes.*`: enabled, expiry_index, stop_pts 40, hold_min 30, target_mode fixed|first_level, target_pts 40,
reach_fallback_pts 60, iv_change_win 0, iv_change_loss −0.5, priyanka.{window_pts 300, min_ltp 1, max_spread_pct 3,
course_strike_multiple 100, course_itm_min 50, course_itm_max 250, course_ltp_min 120, course_ltp_max 320,
course_premium 200, course_premium_span 400, fit_floor 0.2, fast_itm_min −100, fast_itm_max 25, kind_band_pts 25},
filters.{delta_range_ce, delta_range_pe, min_oi, min_oi_chg, min_volume} (all off), auto_refresh, switch_tf 3m,
switch_min_improve 0.15, candles_days 2, candle_fetch_delay_s 4 ·
`options_flow_proxy.*`: enabled false, weight_by_delta, baseline_window 30, baseline_min_bars 10, surge_z 2,
fade_min_follow_pts 2, fade_min_follow_atr 0.1, min_candle_range_atr 0.15 ·
`confluence.options_context.{supportive +0.05, opposing −0.05}`, `confluence.proxy_surge_bonus`,
`confluence.proxy_against_penalty` · `delta.adapter` default **none** · `paper.premium_pnl` true.

### 9.5 Validation checklist (8 Oct replay)

1. `pytest -q tests` → **43 passed**. `test_baseline_lock.py` pins the 8 Oct playbook: B INFO 14:00 / 14:35 / 15:05,
   C short SKIP 22,231.8 / 22,257.9 / 22,179.9 "15:30 is outside the trade windows", no TRADE. It is re-checked
   with no source, mock delta and the real-feed stub.
2. Replay (README §4) → `/api/strikes` shows CE and PE course picks, e.g. *22100 CE best course pick: Δ +0.69 · ITM 169 ·
   premium 239.5 · … R:R 0.85 · fit 0.76* and *22400 PE … ITM 131 · premium 207.2*.
3. `/api/candles?tf=5m&inst=NIFTY|2026-10-13|22100|CE` returns real premium candles (O 240.5 H 255.0 L 236.6 C 248.1).
4. `/api/signals` before and after switching on mock delta and the proxy is **byte-identical**: triggers are untouched.
5. `/api/flow` lists strike events in NIFTY direction, e.g. *"Buyers in NIFTY 13 Oct 22100 CE hit SSL EQL 22,278.6 at
   11:10 … next candle no follow-through … → bearish for NIFTY."*
6. `/api/proxy` lists only `PROXY_FLOW_SURGE / PROXY_FLOW_FADE`, each starting "PROXY (not real delta) ·".
7. UI: Instrument card (chips, expiry, Select strikes, auto, manual pin, focused panels, verdict, "why" lines),
   Option chart on the 22100 CE with the amber EXPERIMENTAL line, Delta tab sources Unavailable / CSV / Real-feed /
   Mock, the proxy toggle, and three breakdown bars on A/C cards.

### 9.6 Honest limits

* Real delta is still **unavailable**. Dhan sends no per-trade buy/sell aggressor tag for options either. The
  Real-feed adapter takes `ingest_trade(…, instrument=…)` once you have such a source.
* Strike flow for the entry candle arrives about 4 s after the close (REST). The card's confidence updates then; a
  paper trade keeps the confidence it had at entry.
* Premium P&L uses the strike's 1m **close** at the entry and exit minutes. That is an approximation of a fill,
  not a quote.
* The options context is scored only for the candle that just closed (the chain is "now"). Older signals show
  "not recorded".
* The Priyanka/MJ weights and the 0.05 / 0.15 confluence weights are not backtested. Judge them only after 30+
  paper trades.

---

## 10 · Setup D — CRT + TBS (added 9 Oct 2026)

AR's Candle Range Theory + Turtle Body Soup, built from the 14 course PDFs. A **separate engine**
(`backend/app/engine/crt_tbs.py`). It never changes Setup A / B / C: the previous release and this build give
byte-identical A/B/C signals on the same 8 Oct replay candles, and `tests/test_baseline_lock.py` still passes.

### 10.1 Rules as implemented

| Step | TF | Rule (course) | Implementation |
|---|---|---|---|
| CRT candle | HTF | body thicker than the wick; always choose a thick candle | closed candle, body ≥ 50 % of range, range ≥ 0.8× ATR (≥ 1.2× = A+) |
| Keylevel | same HTF | OHP · OLP · OB · FVG, found after the CRT candle | OHP/OLP = CRT candle took an untaken, already-confirmed swing; OB / FVG = CRT candle traded into one that was valid before it. Sets the bias |
| Manipulation | next HTF candle | "as soon as the 2nd candle got manipulated, switch to LTF" | only LTF candles inside that HTF candle are searched |
| TBS | LTF | first candle that manipulates the old high/low with a **body close** | first close beyond CRTH (sell) / CRTL (buy) by ≥ 0.5 pt; a wick poke is not a TBS |
| A+ TBS | LTF | 2–6 candles inside (validation page) | consecutive closes inside the level before the TBS; reported, not required |
| Model #1 | LTF | sell: the thick buy candle after TBS (the order block); buy: the thick sell candle | body ≥ 50 % of range, opposite colour; the latest one before the break |
| Entry | LTF | next candle closes beyond Model #1 | entry at that close, within 20 LTF candles of the TBS |
| Stop | LTF | extreme high / low; no tiny stop | extreme since TBS ± 2 pts |
| TP-1 | — | 50 % of the CRT: close half, then breakeven — not before TP-1 | paper books ½ at 50 %, stop → entry |
| TP-2 | — | the CRT low (sell) / high (buy) | rest of the position |

Pairs (course "timeframe alignment"): **1h → 1m** scalping · **4h → 5m** short-term · **1D → 1h** day trading.
NSE 4h candles are 09:15–13:15 and 13:15–15:30.

### 10.2 Desk filters (not AR's — shown on the card when they apply)

Trade windows (09:30–11:30, 13:30–14:45) · R:R ≥ 1.0 to TP-1 · stop size per pair (1h→1m 5–40, 4h→5m 8–70,
1D→1h 15–150 pts) · entry not already past the 50 % · **optional** `require_bias_alignment` (off by default).

### 10.3 What the 8 Oct replay showed

The 1h → 1m pair fired three longs (11:35, 12:37, 13:21), each from a bullish 1h FVG keylevel. 8 Oct trended down
and all three would have been **stopped, −3R**. They were skipped only because they closed outside the trade
windows. Every one was against the 15m structure (bearish all day), so `require_bias_alignment: true` would have
skipped them for a real reason. One day proves nothing either way; collect 30+ D paper trades before deciding.

### 10.4 Where it shows

Chart layer **CRT + TBS**: gold box = CRT candle → end of the manipulation candle (edge red = bearish, green =
bullish), dashed line = 50 %, ■ TBS, ● M1, ▲/▼ CRT▶ entry. A pair shows on any chart as fine as its LTF (1m events
sit on their 5m candle). Playbook cards show TP-1 (½ + BE) and TP-2 with both R:Rs. `GET /api/crt?tf=5m` returns
every pair's ranges, events and signals. Alerts and Telegram use the existing alert engine (same cooldown and daily
cap). Paper: one open trade at a time, shared with A / C; new status **BE** = second half stopped at entry.


### 10.5 Second batch of course cards (added 10 Oct 2026)

| Concept | Card | Implementation |
|---|---|---|
| **RB — Rejection Block** keylevel | "five keylevels: OB, FVG, RB, OHP, OLP" | the wick of a confirmed swing candle when that wick is ≥ 50 % of its range (upper → bearish, lower → bullish), valid until a close through its extreme |
| **Model #2 — FVG** entry | CRT + TBS Model #2 | after the TBS, a 3-candle gap in the trade direction (≥ 1 pt) → entry at the close of its 3rd candle |
| **Model #3 — BOS** entry | CRT + TBS Model #3 | close beyond the last LTF swing low (sell) / high (buy) confirmed before the TBS |
| Which model | "entry Models (Model #1, #2 or #3)" | the first to confirm wins; `crt_tbs.models` can restrict it |
| **Inside-bar CRT** | Inside bar — CRT (bearish model) | if the 2nd HTF candle closed fully inside the CRT range, the 3rd candle is the manipulation candle |
| **3-candle model** | "pros enter on the 2nd candle, others wait for its close and enter on the 3rd" | the TBS must be in the manipulation candle; the entry may come until the next HTF candle closes |
| Timing / session | "not a matter if your analysis is correct" | no session filter added; the desk's own trade windows stay |
| "80–95 % win rate" | sketch-map card | a marketing claim, not a rule — nothing implemented, and the 8 Oct replay (3 losing longs) is a reason to measure your own |

### 10.6 Paper slots, scorecard, service (added 10 Oct 2026)

* **One open trade per strategy slot**: A, C, and one per CRT + TBS pair (D|1h>1m, D|4h>5m, D|1D>1h). A trade in one
  slot never blocks a signal in another, so every strategy gets every chance and the comparison is fair. A second
  signal in the SAME slot still waits until that slot's trade closes.
* **Scorecard** (Paper tab, `GET /api/paper/scorecard`): per setup, per D pair, per D entry model, and all setups —
  trades, wins, losses, flat, BE (TP-1 booked then breakeven), win %, avg R, total R, best / worst R, ₹ premium, and
  a ✓ once a row has 30 closed trades. Win = R > 0. Judge on avg R and total R, not win %.
* **Service**: `./install_ict_service.sh` (OC folder) installs `paperdesk-ict`, adds it to `desk_hours.sh`
  (08:55–16:00 Mon–Fri) and to the watchdog. Log: `ict_smc_app/backend/ict_desk.log`. `./install_ict_service.sh remove`
  undoes it.

## 11 · Auction read — "don't confuse activity with control" (added 10 Oct 2026)

From Viswas Yadav's 10 order-flow / auction cards. **Observation only**: it never opens a paper trade, never
changes Setups A / B / C / D (the 8 Oct baseline lock still passes), and it is not part of the scorecard.

### 11.1 The 5 questions (tab **Auction**, 5m / 15m / 1h)
| # | Question | How the desk answers it |
|---|----------|-------------------------|
| 1 | Location | above / inside / below yesterday's value area · inside today's developing value · at pVAH / pVAL / pPOC / PDH / PDL (within 0.35 ATR) · at the edge of a live balance box |
| 2 | Effort | real delta z of the last closed candle. NIFTY index has no volume → with no real source: **"Delta unavailable"** (never guessed) |
| 3 | Result | move of the last 3 closed candles in ATR. High effort (|z| ≥ 2) + result ≤ 0.35 ATR → "activity without control" |
| 4 | Response | **Accepted** = after a close beyond a reference, still beyond 2 closes later with ≥ 0.6 ATR follow-through (closes, not wicks) · **Rejected** = a close back on the old side, or a wick probe ≥ 3 pts beyond that closes back · **No follow-through** = still beyond after 6 closes without the 0.6 ATR |
| 5 | Decision | text that combines 1–4: accepted → only pullbacks that hold the level, with a playbook / CRT entry · rejected → failed auction, expect rotation into value · balance / testing → wait |

### 11.2 Profile and balance
* **TPO (time-at-price) profile** from closed 1-minute candles, 5-point buckets — each minute adds 1 to every bucket
  it traded through. Value area = 70 % of the time around the POC. Labelled TPO everywhere: it is not volume.
* References for a day = the previous day's VAH / VAL / POC + PDH / PDL (all known before the open → no look-ahead).
* **Balance box** = ≥ 8 closed candles inside 2 × ATR. A close beyond the box is a break test of its edge (Bal-H / Bal-L).

### 11.3 Chart layer "Auction (value · accept / reject)"
Blue dashed box = yesterday's value · solid blue = pPOC · pink dashed = today's developing POC · grey box = balance.
Markers: ▲▼ **ACC** accepted · ● **REJ** rejected · ● **NoFT** no follow-through; references resolved on the same
candle are joined (ACC pVAH·pPOC·PDH). The dashboard "Read of the market" gets an "Auction (5m)" line.

### 11.4 What the cards say that the desk already did
Cards 3, 4, 6 (aggression ≠ success, passive liquidity, positive delta + falling price) are the existing
Delta / Footprint absorption rules 1 and 2 — they work once a real delta source is connected.

Rules: `rules.json → auction`. API: `GET /api/auction?tf=5m`. Tests: `tests/test_auction.py`.

## 12 · Real volume — NIFTY future, VWAP, estimated delta, options activity (added 10 Oct 2026)

The NIFTY index (IDX_I 13) has volume 0. The desk now reads the **current-month NIFTY future** (NSE_FNO FUTIDX).
Display / context only: no trigger, zone, signal, paper trade or scorecard row changes (baseline lock passes).

### 12.1 Which contract (market/futures.py)
* Dhan scrip master → NSE FUTIDX rows whose symbol starts with `NIFTY-` (BANKNIFTY / NIFTYNXT50 excluded), nearest
  unexpired first; cached daily in `data/scrip_master_futidx.csv`.
* **Price check**: accepted only within `futures.max_basis_pct` (2.5 %) of the index — a name match alone once
  produced VWAP from an instrument at 1,580 (your dhan_client.py lesson).
* **Rollover**: within `rollover_days` (4) of expiry, if next month traded more today, next month is used.
* Pin: `rules.json → futures.security_id` or env `NIFTY_FUT_ID`. Status: dashboard / `GET /api/volume`.
* Candles: REST 1-minute with volume (7 days on start, then each minute + 4 s), sec_id `FUT`.
* **Basis-adjusted**: every minute, future OHLC − (future close − spot close) → levels sit on the NIFTY chart.

### 12.2 Volume engine (engine/volume.py)
| Item | Rule |
|------|------|
| Volume profile | each minute's volume spread evenly over the 5-pt buckets it traded; POC; value area 70 %; HVN = local peak ≥ 1.3× mean; LVN = local trough ≤ 0.5× mean between two HVNs |
| Naked POC | an earlier day's POC no later minute has traded through (last 5 days) |
| VWAP | Σ(typical × vol)/Σ vol from 09:15; σ = volume-weighted std; bands ±1σ, ±2σ; plotted at each candle's close |
| Anchored VWAP | from the start of the leg of the last MSS; from the newest active CRT candle |
| RVOL | candle volume ÷ average of the same clock slot over the previous ≤ 10 days (≥ 2 needed), else previous 20 candles; only earlier data; ≥ 80 % minute coverage |
| Annotations | OB = displacement candle RVOL · FVG = gap candle · BOS/MSS/sweep marker `V2.1` (≥ 1.5×) or `lowV` (≤ 0.7×) · CRT candle RVOL |

### 12.3 Estimated delta (delta/recorder.py, adapter `fut_quote_rule`, now the default)
Dhan v2 Full packet (RequestCode 21) of the future: LTP, cumulative volume, best bid/ask. Δ volume between packets is
classed: price ≥ previous ask → buy; ≤ previous bid → sell; else tick rule; else unclassified (never guessed).
Levels mapped to index prices with a smoothed live basis, `delta.fut_level_step` (5) points per row. Stored in
delta_bars / footprint_levels (inst `FUTQR`) — survives restarts. **Limits**: snapshots, not every trade; no history
before the first recorded session; index only (strike flow stays on its own sources). Labelled "ESTIMATED" everywhere.
It also writes a provisional FUT 1-minute candle the instant a minute closes; REST overwrites it with Dhan's numbers.

### 12.4 Options activity (market/activity.py)
Nearest expiry, ±10 strikes of ATM: new volume since the previous chain poll × |delta| → per minute (table
`opt_activity`). z-score vs the previous 30 candles. Participation only, direction unknown. Live only.

### 12.5 Auction now uses volume
Value area / POC references come from the volume profile (TPO only when no futures data). Effort (question 2) =
estimated delta z + FUT RVOL + options activity z; "high effort, low result" fires on any of them.

Rules: `futures`, `volume`, `activity`, `delta.fut_level_step`. API: `GET /api/volume?tf=5m`. Tests: `tests/test_volume.py`.

### 12.6 Futures OI build-up (engine/oi.py) — added 10 Oct 2026
REST candles of the future are fetched with `oi=True` (table `fut_oi`, backfilled 7 days); the live Full packet's OI
fills the newest minute until REST replaces it. Per candle: ΔOI = OI at its last minute − OI at the previous candle's
last minute (same session only). Counted if |ΔOI| ≥ `oi.oi_min_pct` (0.05 %) of OI and |Δprice| ≥ 0.1 ATR:
price ↑ OI ↑ long build-up · price ↓ OI ↑ short build-up · price ↑ OI ↓ short covering · price ↓ OI ↓ long unwinding.
Summary over the last 12 candles (dominant build-up, net ΔOI). Shown in the Auction tab, the effort line, the footer
and the narrative. Context only.

### 12.7 IV and delta over time (market/ivtrack.py)
Once a minute from the chain (nearest expiry): ATM IV = mean of ATM CE / PE IV; the selected strikes' IV, delta, LTP
(table `iv_series`). Live only — Dhan keeps no IV history. Trend over 60 min (±0.5 IV pts = falling / rising);
strike |Δ| over the day (±0.08). ATM IV is the orange line above the volume strip.
Not added on purpose: "delta falling + moving-average sell" — both read the same price move, not two confirmations.

### 12.8 Dhan API budget (added 10 Oct 2026)
Dhan's limits are per ACCOUNT — the 8600 desk, the paper desks, the OC scanner, recorders and the Dhan MCP all share
them: Data APIs 7,000 calls/day · option chain 1 per 3 s · quotes 1/s.
* Header pill **Dhan API: n / 7,000** = this desk's data calls today (hover: chain calls, rate-limited answers).
  Amber at 70 %, red once over `data.desk_data_budget` (5,000): strike and futures candle refreshes then run only
  every 5th minute. The feed, estimated delta recorder (provisional futures volume) and repairs keep running.
* Option chain: one call every `data.chain_poll_every_s` (6 s, was ~3 s); nearest expiry on 3 of 4 polls
  (`chain_far_every`). Normal use ≈ 1,100–1,500 data calls a day.

### 12.9 Futures OI · EMA 8, OI surge, BANKNIFTY confirmation (added 11 Oct 2026)
* Chart layer **Futures OI · EMA 8**: pane 1 = NIFTY futures OI + its 8-EMA (`oi.ema_len`); pane 2 = ΔOI per candle,
  coloured by build-up (green long build-up, light green short covering, red short build-up, pink long unwinding,
  amber = surge).
* **OI surge** (`OI⚡n×` marker): ΔOI ≥ `oi.surge_k` (2.5) × the average |ΔOI| of the previous `oi.surge_window` (20)
  candles of the same session, and OI above its 8-EMA. Big move building — no direction implied.
* **BANKNIFTY future** (`futures.banknifty`): resolved like NIFTY's (scrip master, price-checked against the
  BANKNIFTY index IDX_I 25), REST candles with OI every minute (1 extra data call/min), stored as sec_id `BNFUT` and
  table `fut_oi_sym`. Build-up on the same timeframe; **aligned** (same bias) = broad move, **diverging** = narrow
  move, more likely to fail. Auction tab, effort line, narrative.
* Same rules in 🧪 Options Lab → "📈 Futures OI · EMA 8" (`futures_oi_panel.py`, OC folder).
