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
