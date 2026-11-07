# Ledger separation — prepared 7 November 2026

## Installation status: installed in source after user GO AHEAD; daemon restart required

Another editor repeatedly rewrote the backend and staged dashboard/config
changes during implementation. One overwrite removed all new provenance hooks
and restored the broad-delete code. This change was therefore completed in the
isolated `chatgpt/ledger-separation` checkout. The partial UI/API edits made by
this task in the active checkout were removed by restoring the other writer's
staged UI/API contents; that staged state and its maintenance script are retained.
No active SQLite records were deleted, modified, or cleaned up by this task.
The user subsequently authorized installation with GO AHEAD. The reviewed patch applied cleanly over the newer live-window edits, which were preserved. No config file was installed or changed. The running daemon still needs a user-controlled restart to load the code.

## Behaviour

- Institutional order book defaults to **real orders only**, requiring an actual
  broker order ID. Market paper and off-market tests have separate filters.
- Off-market tests have a dedicated **Clear off-market tests** action and
  **Restore hidden tests**. Cleanup is reversible hiding, not physical erasure.
- Cleanup preserves all open positions/pending orders, live/mixed modes,
  market-session records and unclassifiable records. Explicit parent linkage
  keeps late exits in their original entry-session category.
- The protected weekday entry window is 09:15–15:45 inclusive, per user request.
  This is a retention boundary, not a change to exchange/trading session hours.
- Clearly identifiable legacy paper-only off-session closed positions/BUY tests
  can be labelled during cleanup. Untagged late SELLs without a provable parent,
  mixed records and broker-associated records are not disposable.
- SQLite triggers prevent physical deletion of orders/positions, replacement of
  filled orders/existing positions, editing frozen entry fields, or changing
  settled execution fields. Open MTM/trailing management remains permitted.
- Broad reset requests asking to wipe order/closed-position history now fail
  with a message directing the caller to narrow off-market cleanup.
- Cleanup changes visibility only: it does not settle positions, alter wallet
  balances, place/cancel broker orders or rewrite realized PnL.

## Price provenance

New paper entry prices are observed option LTP, without a synthetic slippage
increment. They are labelled paper quote observations, not broker executions.
The same entry is used in the order and position. A fresh-quote requirement
prevents absent option quotes from manufacturing an automatic paper exit.
NIFTY spot observations and observed option premiums are captured separately.
Existing tariff calculations and strategy entry/exit criteria are unchanged.

Confirmed broker average fills are kept distinct from internal legacy limit
fallbacks: the latter are not displayed as confirmed entry prices. Missing
historical option/spot observations show **Not recorded**, not reconstructed
numbers. Order and position exports carry the same observed-price and provenance
fields. An active position naturally has no final exit price yet.

This patch does not implement new broker-fill reconciliation. In particular,
an unconfirmed real exit remains unknown; it is not filled from a paper mark.
Old raw values remain available in the database for audit, without being
promoted to verified prices in the main display.

## Validation

- 10 new tests pass: session boundaries; live/mixed protection; reversible
  cleanup; delete/update/replace guards; market entry with late exit; exact
  242→276 observed premiums and separate index observations; unknown broker
  fills; legacy-test labelling; actual backend migration; no-quote non-exit.
- Combined existing paper-wallet/fill tests plus new tests: **45 passed,
  2 failed**. The two unchanged legacy failures explicitly expect blanket order
  and closed-position deletion, intentionally superseded by this requirement.
- Python compilation, inline JavaScript syntax and all 11 protected locks pass.
- No production daemon restart, real-money arming or broker orders performed.

Source files: `analyst/ledger_provenance_8510.py`, its JSON rules,
`analyst/terminal8510_backend.py`, `engine_8510_live.py`, and the 8510 HTML.
The feature's metadata is stored in the existing ledger's new
`execution_provenance` table. Migration runs when the updated backend starts.


## Authorized installation verification

After the user's GO AHEAD, the source patch was installed cleanly over the
current backend. The separately staged live-window/config changes and unrelated
maintenance script were preserved. No runtime settings were edited.

On a private SQLite backup of the current ledger, migration and test cleanup
hid 262 order rows and 158 positions, then restored all 420. Hashes of all raw
orders and positions matched before and after. No real ledger cleanup ran.
The installed-source focused suite passes 45 tests with the two superseded
blanket-delete tests explicitly deselected; the earlier full result (45 pass,
2 fail) remains documented above. The daemon still requires restart.


## Algo-only presentation refinement — 7 November 2026

Positions and Orders now offer only Paper Algo and Real Algo. Eligibility is
computed from explicit original source/placed-by fields (before display fallbacks),
entry-session time and test/replay/manual markers. New executions also retain
the explicit is_algo_trade boolean in provenance, inherited by exit legs. Late
exits use their original position entry time. Unknown origin is not promoted
into an algo merely by a default label. All old dates use the same filter.

The Source column and manual/test/all-source controls were removed from this
view. Test cleanup remains outside it in Funds; historical replay is unchanged.
Execution rows are retained for audit, not physically purged. Order/position
exports and bulk position actions use the same selected algo view. Emergency
kill and risk-management queries still retain access to all underlying records.

Paired columns: NIFTY entry/exit, entry/exit time, option entry/exit. Contract,
quantity, setup, fresh LTP, PnL and status remain. Open-trade exits/missing recorded
prices display a dash. Real order MTM uses its broker-confirmed fill rather than
a paper-parent entry. No new broker reconciliation or historical fills are
invented. Existing controls for square-off and trailing remain present.

Validation: 15 ledger tests pass, including runtime origin, legacy late exits,
shared UI/export/action filtering, retained action functions, and broker-fill
MTM independent of a different paper parent price. The prior combined focused
run had 49 passes with 2 superseded blanket-delete tests deselected, followed by
one additional passing MTM regression. Python/JS syntax and all 11 locks pass.
No live setting was modified, no broker order was submitted and no ledger row
was deleted by this change. Restart 8510 and hard-refresh to load both backend
eligibility flags and the new table layout.
