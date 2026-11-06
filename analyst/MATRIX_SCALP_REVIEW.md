# MATRIX archived replay correction — 7 November 2026

The user requested relocation into the existing Previous-Day / Recorded Date
Replay Engine, not a separate forward paper strategy. The prior MATRIX wallet
panel, standalone API routes, background startup task and forward service class
have been removed. No historical files or ledgers have been deleted.

Select a date in tab 4 and click Run Recorded Replay. The existing recorded
ledger results remain and MATRIX Tier 3 reconstructed trades appear in the same
table, with setup MATRIX, index entry/exit, direction, duration, outcome, index
points and stop progression. The existing Excel export includes these columns.
Existing rupee PnL remains recorded-ledger only; MATRIX index points are separate.
No option premiums, charges or profits are fabricated.

The user's ladder remains -15 initially, +15 locks +10, +20 locks +15,
+25 locks +20, and five-point steps thereafter. Entry uses the next minute open
following an existing Matrix Tier 3 episode. One position; no averaging; fresh
Tier 3 episode required for re-entry. Previous session candles warm the Matrix
engine; first available session lacks warmup and is skipped. Missing selected
dates stay empty. This is reconstruction from observed candles, not proof of
alerts emitted live. Conflicting OHLC orders remain ambiguous.

6 November 2026 test: 39 MATRIX observations, 21 resolved, 18 ambiguous,
resolved-only total -115 NIFTY points. Do not use the resolved subset as an
unbiased strategy-performance estimate. No profitability claim.

Validation: 9 offline regression tests, Python compilation, inline-JavaScript
syntax check, 11/11 protected-module verification. Wrapper test confirms that
recorded rupee totals and existing rows are preserved, and selected date is
passed into MATRIX replay. No broker calls or runtime daemon restarts performed.

Restart the existing 8510 process from its own terminal and hard-refresh.
Open tab 4, select 2026-11-06, Run Recorded Replay. Other strategies and live
execution remain unchanged. The old MATRIX output files are retained only as
historical research artifacts; no startup task reads or records them.
