# The 8 EMA Prototype ("ATMA") — Complete Design

**Branch:** `claude/v2.0.23-proto-glitches` · **13 Nov 2026** · every mechanic below is read from the code, not paraphrased.

"ATMA" = the number **8** = the **8 EMA**. Everywhere in this app the 8 EMA is the **dynamic / running** 8 EMA on the 15-minute NIFTY-spot chart — a line that keeps moving, like an LTP, not a value frozen at the last candle's close.

Files: `analyst/ema8_prototype_8510.py` (the rules), `analyst/nse_spot_candles_8510.py` (the EMA maths and 15m bars), `analyst/terminal8510_backend.py` (the live worker + order path), `analyst/backtest_lab_8510.py` + `run_date_range_backtest` (history).

---

## PART A — What the 8 EMA actually is

### A.1 The EMA formula (identical in live, paper and backtest)

The 4 EMA and 8 EMA are **exponential moving averages of the 15-minute candle CLOSE**, computed the TradingView way with `adjust = False`:

```
EMA_today = EMA_yesterday + α · (close − EMA_yesterday)
α (alpha) = 2 / (N + 1)
```

- **8 EMA:** N = 8 → α = 2 / 9 ≈ **0.2222**
- **4 EMA:** N = 4 → α = 2 / 5 = **0.40**

So each new 15-minute close moves the 8 EMA about 22% of the way from its old value toward the new close. This is the recursive form (`ema_tv`, `ema4_ema8` in `nse_spot_candles_8510.py`). It is **not** a simple average of the last 8 candles — older candles fade but never fully drop out.

### A.2 The 15-minute candles

- Built from 1-minute NIFTY-spot candles, **anchored at 09:15 IST**, in 900-second (15-minute) buckets: 09:15, 09:30, 09:45 … 15:15 (`resample_nse_15m`).
- A 1-minute candle is stamped with the minute it **opens**.
- The session is sliced to 09:15–15:30.

### A.3 The "running" (dynamic) 8 EMA — the heart of it

A normal 8 EMA only has a new value when a 15-minute candle **closes** (every 15 minutes). That is too slow to catch a touch. So the Prototype uses a **forming-bar** 8 EMA that updates continuously:

1. Take the **completed** 15-minute candles up to now. Their 8 EMA is fixed.
2. Take the **current, still-forming** 15-minute candle (the one in progress).
3. **Replace that forming candle's close with the live NIFTY spot price**, and stretch its high/low to include the live spot (`apply_forming_bar_to_b15`, `compute_dynamic_emas_on_bar`).
4. Recompute the 8 EMA across the whole frame.

The value you get is:

```
running_8EMA_now = last_closed_8EMA + 0.2222 · (live_spot − last_closed_8EMA)
```

Because `live_spot` changes every few seconds, the running 8 EMA **moves every few seconds** — exactly the "fluctuates like an LTP" behaviour. When the 15-minute candle finally closes, `last_closed_8EMA` updates to that final value and the next forming bar starts from there. This is the same line shown in the top banner ("8 EMA Prototype line · live") and the same line the algo compares spot against.

### A.4 How often it is calculated — **live 3 s, backtest 30 s**

| Mode | Refresh | Config key | Why |
|---|---|---|---|
| **Live (real + paper)** | **every 3 seconds** | `e8_refresh_seconds = 3.0` | Owner, 12 Nov: "catch the touch as soon as it happens." Changed from 30 s. |
| **Backtest / Replay** | **every 30 seconds** inside each 15m bar | `backtest_tick_seconds = 30.0` | History is 1-minute candles. Pretending to read them every 3 s would invent precision the data does not have, making the backtest look better than reality. |

> **Answer to "every 3 seconds, will it calculate on the 15-minute candle?"** — Yes. Every 3 seconds (live) the engine rebuilds the current 15-minute candle using the latest spot, recomputes the 8 EMA on the 15-minute series, and checks the touch. The 15-minute structure never changes; only the forming bar's close (= live spot) and therefore the running 8 EMA move.

**Nothing between ticks is lost.** Between two 3-second checks the live worker still records the highest and lowest spot it saw (`_take_tick_spot_range` / `_sleep_tracking_spot`), and feeds that high/low into the touch test. So even a 1-second spike that touches the line and leaves is caught on the next 3-second check.

---

## PART B — The state machine (the rules)

One Prototype trade at a time. The lane walks these states:

```
WAITING ──(15m candle CLOSES across the 8 EMA)──▶ ARMED ──(price moves ≥2 pts away)──▶ AWAY ──(price returns and touches 8 EMA ±3)──▶ ENTRY ──▶ IN TRADE ──(exit)──▶ back to ARMED or flipped
```

### B.1 ARM — a 15-minute **close** crosses the 8 EMA

- Checked on each **closed** 15-minute candle. If the previous close was on one side of the 8 EMA and this close is on the other side, that side **arms**:
  - close crosses **above** 8 EMA → arm **LONG** (will buy **CE**)
  - close crosses **below** 8 EMA → arm **SHORT** (will buy **PE**)
- The candle that does the crossing is recorded (`cross_bar_open_ts`). **No entry is taken on the cross candle itself.**
- Your 13 Nov session: 09:15 & 09:30 candles below the 8 EMA → PE armed; 10:30 candle closed above → CE armed; 11:15–11:45 below → PE armed again; 14:30 above → CE armed.

### B.2 AWAY — price must leave the line first (`away_min_pts = 2.0`)

Once armed, price must move **at least 2 points beyond** the line (plus tolerance) before any touch counts:
- LONG: a tick's high must exceed `8EMA + tol`
- SHORT: a tick's low must drop below `8EMA − tol`

This prevents arming and entering on the same spot. The exact tick that first sees "away" is **never** also counted as the touch — within one tick the order of its high and low is unknown, so the return has to be seen on a **later** tick (`check_retouch`, `away_seen` flag).

### B.3 ENTRY — the touch of the running 8 EMA (`touch_tol_pts = 3.0`)

After "away" is seen, the entry fires on the **re-touch**:
- LONG (CE): a tick's **low ≤ 8EMA + 3** → enter CE
- SHORT (PE): a tick's **high ≥ 8EMA − 3** → enter PE

Tolerance is **±3 points** by default. The cockpit "Slow market — exact 8 EMA line" button sets it to **0** (`touch_tol_slow_pts`), i.e. price must touch the line exactly.

Entry window: **no entry before 09:30** (`no_entry_before_hm`) and **no new entry from 15:20** (`no_entry_from_hm`).

### B.4 Contract chosen at entry (premium ≥ ₹200)

- Current-week CE (for LONG) or PE (for SHORT), premium **≥ ₹200** (`min_option_premium_inr`).
- It first tries the 4 "matrix" strikes (ATM and ATM±100). If all four are under ₹200 (common in the afternoon as options decay), it walks **deeper in-the-money in 50-point steps up to 500 pts** and takes the nearest strike whose live quote is ≥ ₹200 (v2.0.18). If none qualifies, the signal is skipped with a reason, e.g. *"no PE with premium ≥ ₹200 up to 500 pts ITM (best 25450 @ 188.00)."*

### B.5 EXITS — whichever comes first

| Exit | Reason tag |
|---|---|
| Spot moves **+40** in favour | `SPOT_TARGET_HIT` |
| Option premium moves **+40** in favour | `OPTION_TARGET_HIT` |
| Spot moves **−40** against | `SPOT_SL_HIT` |
| Option premium moves **−40** against | `OPTION_SL_HIT` |
| Broker protective stop fills | `BROKER_SL_FILLED` |
| A 15-minute candle **closes on the opposite side** of the 8 EMA | `OPPOSITE_8EMA_CLOSE` |
| **15:20 square-off** | `EOD_SQUARE_OFF_15:20` |

"+40 spot **or** +40 option, whichever first" means the trade can hit its target on the option premium before the spot has moved 40, and vice-versa.

### B.6 RE-ENTRY and FLIP (`note_prototype_exit`, `_maybe_flip_arm`, `_after_entry`)

This is the owner's 11-Nov rule, and the part the cockpit radios control:

- **Profit exit (+40 spot or option):** nothing is cancelled. The arm **stays**; `away_seen` is reset to False, so the **same side** re-enters on the **next** touch of the line. (`reentry_same_side = True`, the "After profit: same side again ON" radio.)
- **Stop-loss exit (−40, or the broker stop filled):** that side is **cancelled** — its arm is removed and `sl_blocked_side` is set. It **cannot arm again** until a 15-minute close arms the **opposite** side. (`reentry_after_stop_loss = False` by default; the "After stop-loss: same side again ON" radio flips this.)
- **Opposite 15-minute close** (while in a trade): exits the trade **and** flips the arm to the other side.
- **One trade at a time:** while a position is open, a second entry is blocked.

> **No cap on trades per day.** As many as the touches allow. On 13 Nov the chart gives 5: 09:56 PE (+40), 10:57 CE (−40 stop → flips to PE), 12:00 PE (+40), 13:38 PE (+40), 15:00 CE (+35 to 15:15). Net ≈ **+115 pts** — all reachable on the current defaults, because the −40 stop on the 10:57 CE naturally flips to PE. You do **not** need the stop-loss-same-side radio for this sequence.

### B.7 Optional filters (cockpit, all default OFF except "away")

| Filter | Effect | Config |
|---|---|---|
| Ribbon | Block entry when the 4 EMA and 8 EMA are within 3 pts of each other (chop) | `ribbon_filter_enabled` |
| EMA separation | LONG only when 4>8, SHORT only when 4<8 | `ema_separation_enabled` |
| SuperTrend | Require SuperTrend(5, 1) agreement | `supertrend_enabled` |
| Retouch requires away | The "must move 2 pts away first" rule (ON by default) | `retouch_requires_away` |
| Slow market exact line | Touch tolerance 0 instead of 3 | `slow_market_exact_line` |
| OI direction filter | **Placeholder** — when a later feed writes `oi_bias` = CE/PE, trade only that side | `oi_direction_filter_enabled` |

---

## PART C — The three environments

The **rules above are identical** in all three. What differs is where the price comes from and what happens on entry/exit.

### C.1 PAPER trade

- The live scanner runs the exact state machine on live spot every 3 seconds.
- On entry, **no real order is sent.** A paper fill is recorded from the live option quote, and a row is written to the ledger `orders` and `positions` tables with `mode = PAPER_ONLY`.
- Exits are simulated the same way and written to the ledger. Paper P&L lives in the paper wallet.
- Paper runs **in parallel** and must never block or alter a real trade.
- The Prototype card shows **"Paper only — this lane never sends a real order"** when its route is `PAPER_ONLY`.

### C.2 REAL money

- Same state machine, same 3-second cadence, same live spot.
- Route is set by the **`execution_mode`** toggle:
  - **`LIVE_ALLOWED`** — sends a **real** order **when the desk is armed live**; otherwise it behaves as paper.
  - **`PAPER_ONLY`** — never sends a real order (a one-way downgrade in `execute_order_with_chaser`).
- On a real entry the order goes through the **aggressive limit chaser** (steps the limit price toward the touch, bounded retries) and a **companion protective stop** is placed. `live_filled = 1` is set only when the broker confirms the fill; the desk **sells only when `live_filled = 1`**, and the companion stop is confirmed out of the market **before** a SELL is sent.
- Exits are **resumable**: `exit_order_id` / `exit_reason` are tracked, a chaser works the exit, partial fills are weighted-averaged, and an `EXIT_PENDING` position is recovered after a restart (`_spawn_close`).
- **15:20 square-off** closes any still-open position (`check_eod_square_off`, wired into the loop in v2.0.23 — before this it was never called, which is why positions stayed open).
- Real orders are written to the same ledger with `mode` reflecting LIVE/BOTH, alongside the Dhan order id.

> Real and paper share one code path for the signal and the exits; only the final "send to broker vs simulate" step differs. That is deliberate — it means a paper result is a faithful preview of the real behaviour, minus slippage.

### C.3 BACKTEST / Replay (history)

- Runs **read-only**, in its own process, on recorded 1-minute spot candles. It **cannot place an order or touch the live desk** (the live state is swapped out for a throw-away state).
- Inside each 15-minute bar it walks a **30-second grid** (not 3 s — see A.4) and rebuilds the forming-bar 8 EMA from the 1-minute candles **that have already completed** at each tick (`forming_ohlc_from_1m(..., completed_only=True)`), so it never reads a minute's high/low before that minute is over (no look-ahead).
- The same arm → away → touch → exit → re-entry/flip logic runs (`backtest_ema8_prototype_at_bar`).
- **Reported in NIFTY spot points, per trade, after a cost you set.** Option premium is **not** simulated in the backtest — the "40 option points" exit is not modelled, only the spot side (±40 spot, opposite close, 15:20). This is a known, stated limitation.
- The Backtest Lab adds a **held-out verdict**: the last 30% of days are kept apart and the PASS/WEAK/FAIL grade is read from those unseen days only. See `claude/Backtest_Lab_Guide_2026-11-13.md`.

---

## PART D — One trade, start to finish (worked example, 13 Nov)

1. **09:15 / 09:30** 15m candles close **below** the running 8 EMA → **PE armed** (SHORT), cross candle recorded.
2. Price pushes **down ≥ 2 pts** below the line → `away_seen = True`.
3. **09:56** a 3-second tick sees spot rise back and its high reach within 3 pts of the running 8 EMA (≈ 25221) → **enter PE**, pick a current-week PE ≥ ₹200.
4. Spot falls. At **10:21** spot is 40 pts in favour (25221 → 25180) → `SPOT_TARGET_HIT`, **+40**. Profit exit → arm stays, `away_seen` reset → PE can re-enter on the next touch.
5. **10:30** a 15m candle closes **above** the 8 EMA → the arm **flips to CE** (LONG).
6. **10:57** touch of the line from above (≈ 25212) → **enter CE**. Spot falls instead; by **11:07** spot is 40 pts against → `SPOT_SL_HIT`, **−40**. Stop-loss → **CE cancelled** (`sl_blocked_side = LONG`) until an opposite close.
7. **11:15–11:45** closes below → **PE arms**. **12:00** touch → enter PE → +40 by 12:08. **13:38** touch → enter PE → +40 by 13:54.
8. **14:30** close above → CE arms. **15:00** touch → enter CE → +35 by 15:15. **15:20** would square off anything still open.

Net ≈ **+115 pts** across 5 trades.

---

## PART E — The exact config block

Under `scanner.ema8_prototype` in `analyst/terminal8510_config.json` (defaults in `ema8_prototype_8510.py`; change only via `scripts/patch_*`):

```
enabled: true                      execution_mode: LIVE_ALLOWED
touch_tol_pts: 3.0                 touch_tol_slow_pts: 0.0     slow_market_exact_line: false
target_pts: 40.0                   sl_pts: 40.0
e8_refresh_seconds: 3.0            backtest_tick_seconds: 30.0
away_min_pts: 2.0                  retouch_requires_away: true
min_option_premium_inr: 200.0
reentry_same_side: true            reentry_after_stop_loss: false
arm_until_opposite_close: true
no_entry_before_hm: "09:30"        no_entry_from_hm: "15:20"
ribbon_filter_enabled: false       ribbon_max_pts: 3.0
ema_separation_enabled: false
supertrend_enabled: false          supertrend_length: 5       supertrend_factor: 1.0
oi_direction_filter_enabled: false
```

---

## PART F — Known limits & the bugs that were fixed

**Fixed (now in this branch):** 15:20 square-off was never called (v2.0.23); the 5-minute signal wait used to start the moment a touch was *seen* and ate the next touches (v2.0.16); a skipped order used to burn the setup (v2.0.21); the contract picker only looked at 4 strikes (v2.0.18); the touch was checked every 30 s not 3 s (v2.0.19); the Orders Book "Today" filter and the stop-loss re-entry radio (v2.0.23).

**Still true / by design:** option premium is not simulated in the backtest (spot side only); the 8600 Board filters and the 8600 Ticket lane are not in the backtest; the OI direction filter is a placeholder; Stage/Reversal first-touch memory is in RAM and resets on restart; inside a 1-minute backtest candle the order of high and low is unknown (the Lab books the ambiguous case as a stop and shows the "if targets" swing).

---
*Companion docs in the project: `claude/EMA8_Prototype_Complete_Guide_2026-11-11.md` (owner-facing rules), `claude/Backtest_Lab_Guide_2026-11-13.md` (how to backtest with confidence), and `HANDOVER_8510_2026-11-13.md` (for other AIs).*
