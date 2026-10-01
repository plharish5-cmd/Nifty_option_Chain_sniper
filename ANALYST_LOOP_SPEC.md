# nifty-scanner — AI Analyst Loop: Build Spec

## 0. How to use this file
1. Standing rules are codified in `AGENTS.md`.
2. Build one phase at a time and verify each phase before starting the next.
3. Start with Phase 1 offline replay before any LLM API or Telegram notifications are involved.

---

## 1. Agent Rules
See `AGENTS.md` at repo root.

---

## 2. Goal
Every closed 5-min candle (09:20–15:00 IST), build a compact JSON snapshot from what the scanner has ALREADY computed. Send it to an LLM for a short written read, then post that read to Telegram. Log everything for later validation.

The LLM is a narrator and cross-checker, NOT a decision maker. The `ACTION` field is derived from `strategy1` state in code. The LLM may downgrade an action (flag a conflict), but it can never upgrade WAIT into an entry.

---

## 3. New Files (all under `analyst/`)
| File | Purpose |
| :--- | :--- |
| `snapshot.py` | Collect current state from existing modules into one dict (schema in section 4). Uses CLOSED candles only (forming-candle rule). |
| `news.py` | Pull headlines from 2–3 Indian market RSS feeds every 15 min, keyword-filter (Nifty, RBI, FII, crude, Fed, results of top-10 weights), dedupe, keep latest 5. |
| `events.py` | Event flags: expiry day, RBI/Fed/budget dates (from JSON calendar), NSE holiday check. |
| `llm.py` | Pluggable provider: gemini / claude / ollama, selected in config. Same prompt, same output schema. Timeout 30 s, one retry, then skip cycle (never block loop). |
| `prompt.py` | System prompt + output schema (section 5). |
| `notify.py` | Telegram send (reuse existing bot token from `.env`). Message ≤ 700 chars. |
| `loop.py` | Scheduler: market-hours gate, change detection, daily call cap, logging. |
| `replay.py` | Offline dry run: feed stored `data/candles/*.csv` + chain snapshots through the pipeline and print messages without calling Telegram. |
| `analyst_config.json` | Provider, model, cadence, caps, feeds, thresholds. |

---

## 4. Snapshot Schema (≤ ~4k tokens)
```json
{
  "ts": "2026-09-10T11:35:00+05:30",
  "spot": 0, "session": {"open":0,"high":0,"low":0,"prev_close":0,"gap_pts":0},
  "candles_5m": "last 12 CLOSED bars: o,h,l,c + bb_upper,bb_mid,bb_lower,ema4,ema8",
  "candles_15m": "last 8 CLOSED bars: same fields",
  "bands": {"bb_width_5m":0,"slope_5m":"UP|DOWN|FLAT","trend_15m":"UP|DOWN|FLAT"},
  "vwap": {"nifty_fut":0,"atm_ce":0,"atm_pe":0},
  "levels": {"nearest_3_above":[{"px":0,"type":"OI wall|PDH|CPR|swing|gap|round","dist":0}],
             "nearest_3_below":[], "pdc":0,"pdh":0,"pdl":0,"cpr":{"tc":0,"bc":0,"width_pct":0}},
  "chain": {"pcr":0,"pcr_change":0,"max_ce_oi_strike":0,"max_pe_oi_strike":0,
            "top_oi_adds":[],"atm_iv":0,"straddle":0,"implied_move_pts":0,"conditions":"his 5 named conditions"},
  "bias": {"score":0,"components":{},"regime":"TREND_UP|TREND_DOWN|RANGE|CONSOLIDATION"},
  "strategy1": {"state":"IDLE|ARM|TRIGGER|OPEN|CLOSED","setup":"","side":"CE|PE|null","stop":0,"targets":[]},
  "skip_checks": {"after_3pm":false,"bb_width_lt_60":false,"trend_conflict_15m":false,"rs_within_40":false,"second_opportunity":false},
  "context": {"vix":0,"vix_change":0,"fii_dii":"cached daily","basis":0,"breadth_adv_dec":"optional","banknifty_trend":"optional","gift_nifty_preopen":"optional"},
  "events": [], "news": ["max 5 headlines"]
}
```

---

## 5. Prompt and Output Contract

**System Prompt (in `prompt.py`):**
> "You are a price-action analyst reviewing a NIFTY 50 intraday system. The rules engine (strategy1) is authoritative. Explain its state, check the 5m decision against the 15m trend and the option chain, and flag conflicts. Never invent an entry the engine has not armed or triggered. Use only numbers present in the snapshot."

**Required Output (JSON):**
```json
{
  "bias": "BULLISH|BEARISH|RANGE",
  "engine_action": "(copied from strategy1, not chosen)",
  "agree": true,
  "conflicts": ["..."],
  "key_levels": {"resistance": [0], "support": [0]},
  "invalidation": 0,
  "read": "≤ 3 sentences",
  "news_flag": "none|caution|high-impact"
}
```

---

## 6. Loop Logic (`loop.py`)
- Run window 09:20–15:00 IST, weekdays, skip NSE holidays.
- Wake at each 5-min candle close + 20 s buffer for data to settle.
- Change detection: hash of (strategy1 state, regime, bias bucket, nearest-level set, skip checks). Call LLM only if hash changed OR 15 min elapsed since last call. Always call on strategy1 transitions.
- Daily call cap: default 60 calls/day.
- Priority push: ARM/TRIGGER, price within level proximity (default 15 pts), news_flag = high-impact.
- Run on T40 as a systemd service.

---

## 7. Logging (for `validate.py`)
`data/analyst/YYYY-MM-DD.jsonl`: One line per cycle recording ts, snapshot hash, snapshot, provider, model, tokens in/out, latency, raw response, parsed output, mismatch flag, telegram sent.

---

## 8. Phases
1. `snapshot.py` + `replay.py`. Print snapshots from stored data. Verify every number against the dashboard.
2. `llm.py` + `prompt.py` with provider=ollama (free). Replay one full stored session and read all messages.
3. `notify.py` + `loop.py` live, alert-only.
4. `news.py` + `events.py` + optional context fields.
5. Switch provider to gemini or claude for comparison against ollama.
6. Optional: End-of-day review task at 15:45.
