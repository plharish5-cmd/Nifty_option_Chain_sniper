# 19 November missed-trade investigation

Verified from the running app and recorded database:

- Prototype entry ORD_1795061483529 at 09:41:23 was rejected by Dhan: DH-905, Invalid IP. The paper shadow position subsequently closed at 09:46:14. A paper exit is not a real broker fill.
- Evaluator emits SQUARE_OFF SHORT then ARM LONG on an opposite close. The backend action handler erased the new LONG arm while routing the old exit. Reproduced failing test, repaired by leaving evaluator-owned arm intact.
- Empty repeat-entry arm can be recovered once per worker startup from completed current-session crosses. No historical entry is created. Existing arm, open position, consumed cross, stop-blocked side and re-entry OFF prevent recovery. Normal live quote/touch/entry gates still apply.
- Recording database first snapshot today: 09:16:15. Momentum reader incorrectly required an observation <=09:15:00. It now uses the first recorded session observation, explicitly publishing timestamp, delay and basis. Later starts are not called opening data. Signed fresh components with nonpositive normalization denominator remain unavailable; no fabricated direction is inserted.
- Port 8600 was running while the bridge reported a 4-second timeout. Later read-only requests succeeded: PE-CE 1.07s, flow 0.54s, chain 3.63s. This is intermittent latency, not proof of an app shutdown or internet failure. Link now reports TIMEOUT distinctly from connection refusal. Protected 8600 engines unchanged.
- Both EMA OI configurations were log_only. Disagreement/stale data in this mode records evidence; it does not veto entry. Separate FILTER/Block sources can veto when enabled.
- At inspection global real money was OFF/PAPER_ONLY. Prototype lane live opt-in was true; Reversal live opt-in false. These settings are preserved, not silently armed.
- A 09:30-labelled 15m candle completes at 09:45. Closed-candle arming cannot honestly promise an entry at its opening timestamp. Chart arrows alone cannot prove executable option fills or all configured entry conditions.

Broker follow-up: confirm the actual current outbound IP is registered in Dhan's broker settings. No broker allowlist, credentials, arming, targets, stops, routes, filters or caps were changed by this repair.

## Browser-independent telemetry
The original engine refreshed once at startup, then only through UI GET telemetry calls. A closed browser could therefore starve the scanner of current spot observations. A background task now calls the existing cache-guarded refresh path at its configured refresh cadence and is cancelled on shutdown. No new REST endpoint/source or relaxed age threshold was introduced. The broker's caching/backoff and slow requests can still cause a stale-data pause; a 3-second evaluation cadence is not a guarantee of new exchange ticks every three seconds.
