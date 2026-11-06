# MATRIX scalp paper implementation — 7 November 2026

User-authorized NIFTY-point stops: −15 initially; +15 favourable locks +10;
+20 locks +15; +25 locks +20; thereafter advance every five points.
Direction reverses for bearish entries. Stops never loosen.

## Scope

Uses existing 1-minute EMA Matrix Tier 3 confirmation episodes, rather than
turning Tier 1 approach/Tier 2 touch warnings into entries. Next minute open
is the simulated entry. One index position at a time; no averaging. A fresh
Tier 3 episode is needed for re-entry. These are explicit execution choices,
not a claim that Tier 3 has a demonstrated edge.

Adds an isolated durable index-paper ledger, `data/terminal8510/matrix_paper.sqlite3`,
shown in tab **3. Funds & Paper Wallet → MATRIX · Algo paper scalps**.
This is NIFTY-point paper simulation: it does not select/fill an option, debit
the existing option wallet, calculate rupee PnL or place a broker order.
Existing option-paper strategies and live execution are unchanged.

The paper service runs automatically while the scanner is enabled. It accepts
only completed, fresh local minute candles. Eligible signals start at service
activation; historical replay is never inserted into forward paper history.
Inputs and session rule/radar settings are frozen on first observation, giving
restart/repeated-poll parity. Config edits in `matrix_scalp_rules.json` apply
to the next session; they do not repaint an existing session's paper ledger.
Pausing/killing after paper entries halts this paper session and marks any
open observation PAPER_HALTED with unknown exit PnL; it never closes a broker
position. Existing EOD control is unchanged; MATRIX has its own 15:20 cutoff.

## Data and replay

Local NIFTY candles cover 251 session dates, 2025-11-03 through 2026-11-06.
No permanent EMA/radar recorder rows existed at inspection time. Historical
signals are therefore RECONSTRUCTED from the existing Matrix engine, not
claimed to have been emitted live. Two prior available sessions warm the
engine; the first available session is explicitly skipped without prior data.
Full-history and prefix Matrix entry fields matched on sampled Nov 6 prefixes.

Every minute is replayed through O-H-L-C and O-L-H-C paths. If the resulting
exit differs, or one exits and the other remains open, the result is AMBIGUOUS.
These path candidates are not observed ticks. No realized PnL is invented.
Old-stop opening gaps fill at the observed open. Missing minutes terminate
as DATA_GAP. Partial sessions remain unresolved. No synthetic candles.

Results: 6,549 observations; 4,060 resolved; 2,489 ambiguous; no trade-horizon
gaps in this run. Resolved outcomes total −36,196.45 NIFTY points. Excluding
ambiguous cases is selective: resolved-only win rate/point totals do not
estimate full strategy performance. No option prices, fees or slippage are
modelled. No tuning, random baseline or profitability claim.

## Verification

- 7 offline regression tests: ladder, non-loosening, adverse gaps, same-minute
  ambiguity, bearish symmetry, missing minute and next-minute/tier entry gates.
- Private temporary-ledger experiment: repeat/restart parity and paper halt.
- Sampled Matrix prefix parity: existing entry fields match.
- Python compilation and complete inline JavaScript syntax check pass.
- Protected module verification: 11/11 intact.
- Static browser preview: panel renders in the Funds & Paper Wallet tab.
  Static preview API 404 is expected; it has no execution backend.

## Run / view

Restart only the existing 8510 daemon from its own terminal (Ctrl+C, then the
existing `.venv/bin/python engine_8510_live.py` command). Hard refresh 8510.
No process was killed, no new production daemon started, and no real order
was submitted during this work.

Offline report regeneration (from repository root):

    .venv/bin/python -m analyst.matrix_scalp_8510 --market ict_smc_app/backend/data/ict_smc.sqlite3 --config analyst/terminal8510_config.json --output data/terminal8510

Produces `matrix_backtest.json`, `matrix_trades.csv`, `matrix_daily.csv`.
The dashboard's explicit historical-load button reads the saved JSON; no
costly replay runs in a UI request.
