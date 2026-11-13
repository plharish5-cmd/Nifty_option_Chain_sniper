# Gemini / Cursor Prompt — 8 EMA Prototype Backtest (exact spec)

> Paste everything in the box below into Gemini in Cursor. It is self-contained.
> It replaces any older "9:15–9:30 touch of PDH/PDL" prompt — that logic is **not** this strategy.

---

```
You are backtesting ONE strategy on NIFTY 50 spot. Do not invent rules, gates,
caps, or filters I did not state. Report in NIFTY spot points only.

THE LINE
- Instrument: NIFTY 50 SPOT (index), not options, not futures.
- Candles: 15-minute, anchored at 09:15 IST (buckets 09:15, 09:30, 09:45 ... 15:15),
  built from 1-minute spot candles. A 1-minute candle is stamped at the minute it OPENS.
- 8 EMA: exponential MA of the 15-minute CLOSE, TradingView style, adjust=False:
      EMA_now = EMA_prev + alpha * (close - EMA_prev)
      alpha   = 2 / (8 + 1) = 0.2222222...
  Also compute the 4 EMA the same way (alpha = 0.40) for reference only; the 4 EMA
  does NOT gate entries unless I turn that filter on.

RUNNING (DYNAMIC) 8 EMA INSIDE A FORMING BAR
- Walk each 15-minute bar on a 30-SECOND grid (backtest cadence = 30s; live is 3s, but
  1-minute history cannot honestly be read finer than this).
- At each 30s tick, build the current forming 15-minute bar from the 1-minute candles
  that have ALREADY COMPLETED at that tick (a minute counts only once ts+60 <= tick_ts
  — never read a minute's high/low before that minute is over; no look-ahead).
- Set the forming bar's CLOSE = the latest spot at the tick, and stretch its high/low to
  include that spot. Then recompute the running 8 EMA:
      running_8EMA = last_closed_8EMA + 0.2222 * (spot - last_closed_8EMA)

STATE MACHINE (one trade at a time)
1. ARM — only on a CLOSED 15-minute candle that crosses the 8 EMA:
      close above 8 EMA  -> arm LONG  (would buy CE)
      close below 8 EMA  -> arm SHORT (would buy PE)
   NO entry on the cross candle itself. Record the cross bar.
2. AWAY — after arming, price must move at least 2.0 pts beyond the line before any
   touch counts:
      LONG : a tick high must exceed  running_8EMA + tol
      SHORT: a tick low  must drop below running_8EMA - tol
   The tick that first sees "away" can NEVER also be the touch; the return must be a LATER tick.
3. ENTRY — after away is seen, enter on the re-touch (tolerance tol = 3.0 pts):
      LONG : tick low  <= running_8EMA + 3   -> enter long  at that spot
      SHORT: tick high >= running_8EMA - 3   -> enter short at that spot
   Entry window: NO entry before 09:30, NO new entry from 15:20.
4. EXIT — first of these to occur:
      +40 spot in favour                         (target)
      -40 spot against                           (stop)
      a 15-minute candle CLOSES on the opposite side of the 8 EMA
      15:20 square-off (force close any open trade)
   NOTE: live/paper also exit on +/-40 OPTION premium, but the backtest does NOT
   simulate option premium — model the SPOT side only and say so in the report.
5. RE-ENTRY / FLIP:
      profit exit (+40 spot): arm STAYS, reset away -> SAME side re-enters on next touch.
      stop exit  (-40 spot): that side is CANCELLED and BLOCKED until a 15-minute close
                             arms the OPPOSITE side.
      opposite 15-minute close while in a trade: exit AND flip the arm to the other side.
   No cap on trades per day.

INTRABAR AMBIGUITY (1-minute data)
- Inside a minute you do not know whether the high or the low came first. If a single
  bar could hit both +40 and -40, book it as the STOP (-40), and separately report the
  "if targets filled first" number so I can see both.

OUTPUT
- Per trade: date, arm time, entry time, side (CE/PE), entry spot, exit time, exit spot,
  exit reason, points.
- Daily total points and trade count.
- Whole-period: total points, win rate, avg win, avg loss, max drawdown (in points),
  number of trades.
- Split the days: first 70% "train", last 30% "held-out". Report held-out separately
  and give a PASS/WEAK/FAIL read from the held-out days only (PASS if positive with a
  t-stat >= 2 on held-out trades).
- Subtract a cost of 2 spot points per trade (round-trip) unless I give another number.

WHAT IS NOT IN THIS STRATEGY
- PDH / PDL / PDC / PDO are NOT triggers here. The only trigger is the 8 EMA touch.
  If I later ask for them, they are a CONFLUENCE FILTER (skip touches with no PDH/PDL/PDC
  nearby), never the entry signal. Do not blend the two logics in one run.
- No 1-hour trigger, no "enter on the 09:15-09:30 touch", no OI/PCR gate, no volume gate,
  unless I explicitly turn one on.

Confirm you will follow THIS spec before running. If my data file's columns differ,
ask me which column is spot close / high / low / timestamp — do not guess.
```

---

## If you also want the optional filters wired (default OFF)
Add this block to the prompt only if you want to test them:

```
OPTIONAL FILTERS (each default OFF; turn on only the ones I name):
- ribbon:        block entry when |4EMA - 8EMA| <= 3 pts (chop guard)
- ema_separation: LONG only when 4EMA > 8EMA, SHORT only when 4EMA < 8EMA
- supertrend:    require SuperTrend(length=5, factor=1.0) to agree with the side
- slow_market_exact_line: set touch tolerance to 0 instead of 3 (exact line touch)
Report each run labelled with which filters were on.
```

## For AGY (if coding, not just backtesting)
Same spec, plus: match the existing module `analyst/ema8_prototype_8510.py` — do not
re-derive the rules, read `EMA8_PROTOTYPE_DESIGN_2026-11-13.md` on branch
`claude/v2.0.23-proto-glitches` first, keep config keys and defaults identical
(`touch_tol_pts 3`, `away_min_pts 2`, `target_pts/sl_pts 40`, `e8_refresh_seconds 3`,
`backtest_tick_seconds 30`, `no_entry_before_hm 09:30`, `no_entry_from_hm 15:20`,
`reentry_same_side true`, `reentry_after_stop_loss false`). Never touch the PROTECTED
MODULES without an `UNLOCK` instruction; run `scripts/verify_locks.py` (must pass 11/11).
```
