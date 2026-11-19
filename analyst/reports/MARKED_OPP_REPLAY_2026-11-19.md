# 19 Nov 2026 — owner-marked opportunities vs fixed-engine spot replay

Read-only replay: recorded NIFTY 1m (`market_db`), scanner config shape from disk (OI **log_only**), 3s prototype cadence. **No option premium fills** — spot rule only.

**Score: 4 / 9 WOULD TAKE**

| time | side | lane | verdict | reason |
|------|------|--------|---------|--------|
| 09:41 | PE | Prototype | **WOULD TAKE** | Replay entry **09:39** spot **25189.2** (aligns with ledger PE attempt; live DH-905 is broker-only) |
| 10:15 | CE | Prototype | NO | Already in CE from **09:52** — chart mark is **mid-hold**, not a new entry |
| 10:30 | CE | Prototype | NO | Same — still in CE opened 09:52 until ~10:53 target |
| 13:00 | CE | Prototype | **WOULD TAKE** | Replay entry **13:06** spot **25349.8** |
| 14:00 | CE | Prototype | NO | After 13:40 exit: **no touch ±3** at 14:00 in replay (nearest engine entry 13:06) |
| 15:00 | CE | Prototype | **WOULD TAKE** | Replay entry **15:15** spot **25503.5** (EOD square-off bar) |
| 09:45 | CE | Reversal | NO | No reversal entry in replay window (cross/touch window) |
| 10:30 | CE | Reversal | **WOULD TAKE** | Replay **10:31** spot **25254.7** (mentor e8_trig) |
| 13:15 | CE | Reversal | NO | No reversal entry in replay window |

## What replay does **not** prove

- Dhan **DH-905 Invalid IP** (live reject while paper filled) — external allowlist.
- Option chain quote / strike selection at the minute — spot-only synthesised rows.
- Mid-session **arm persistence across dev restarts** — continuous replay assumes no arm gap; v2.0.39 persistence is separate (unit-tested).

Regenerate:

```bash
PYTHONPATH=. .venv/bin/python -m analyst.replay_marked_opportunities_8510
```