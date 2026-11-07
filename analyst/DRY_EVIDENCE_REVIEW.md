# Four-setup NO_GO observation path — 7 November 2026

The old path could record depth only after real-money arming, while NO_GO
prevented arming. A separate observer now runs from the existing scanner,
without changing the arming decision, approval, strategy thresholds or filters.
The observer contains no order placement, modification or cancellation calls.

## Observations

All four families are sampled: Stage 2 long, Stage 3 short, 4/8 reversal,
1H 8-EMA confluence. A blocked/armed/scanning state is not forced into a trade.
Observations contain the available Dhan OI/synergy, institutional PCR/PE-CE,
EMA stack/slope, ADX/DI, R², confluence and volume context, with receipt time.
Unavailable components remain null. User-supplied example values are not defaults.

Each family's eligible signal can request a best bid/ask quote, using its
existing enabled ATM/ITM selection and instrument lot. Attempts are durably
deduplicated at the scanner's existing five-minute cadence. Quote requests
are spaced between families to avoid bursts and reuse existing broker backoff.
Only positive finite, non-crossed bid/ask with positive LTP and a valid contract
quantity can populate the existing dry_run_orders measurement table. Missing
depth/contract and request failures are recorded but do not count as samples.

Fresh telemetry, fresh source candles and an open weekday session are required
for depth measurement. Kill switch and scanner pause prevent measurement.
The observer logs states even when entries are unavailable. It never arms live
trading and never turns NO_GO into APPROVED. Reaching 30 samples is not approval.

The quote timestamp is the HTTP receipt time, not an exchange timestamp.
This measures quoted spread, not realized fill slippage, profitability or
independent confirmation. The existing scanner may use forming-bar indicators;
that limitation is stored in the observation context. Existing paper strategies
continue unchanged; historical reconstruction is not added to prospective counts.

## Where to view

Restart the existing 8510 daemon from its own terminal, then hard-refresh.
Tab 1: below the four setup cards, **Dry-run evidence · four setups**.
Counters show state observations, trigger attempts and valid spread samples.
Expand recent decisions for the indicator context. The status endpoint is
GET /api/dry-evidence (latest 100 details, all-time counters).
SQLite archive: data/terminal8510/dry_evidence.sqlite3.
Controls and sampling intervals: analyst/dry_evidence_8510.json.
Keep the scanner running and the dashboard connected so telemetry stays fresh.

## Verification

- 8 new offline tests passed: all families, NO_GO without arming, blocked setup,
  missing/crossed/nonfinite depth, stale data, weekend/kill gate, restart dedup,
  disabled contract selection. Fake broker order methods raise if called.
- Existing live-switch safety suite: 53 passed.
- MATRIX historical replay regressions: 9 passed.
- Python compilation, complete inline JS syntax check and 11 protected locks pass.
- Static browser preview renders four zero-count family rows from the real
  empty observer status. No fabricated quotes/trades were inserted.

All tests used temporary state and fake brokers / blocked external HTTP.
No real broker order, production daemon restart or live arming occurred.
Saturday 7 November has no ordinary live session; real spread samples must
be collected prospectively after restart during an open market session.
