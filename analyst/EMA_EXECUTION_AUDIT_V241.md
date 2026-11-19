# EMA execution audit completion — 19 November 2026

## Current desk configuration retained from Cursor

- Prototype and Reversal enabled; both lane routes LIVE_ALLOWED; both lane live opt-ins checked.
- Prototype cadence 3 seconds; normal ±3-point touch tolerance; ribbon/separation/SuperTrend OFF; retouch-away OFF; profit and stop same-side re-entry ON.
- Both OI panels Log only. Total OI/Fresh OI selected. Separate 8600 board has no FILTER sources. Order cooldown zero.
- Reversal 15m, candle trail enabled at +20 NIFTY points, EMA separation OFF.
- Contract selector prefers premium ≥₹200; Cursor's fallback ≥₹150 retained.
- Desk-wide real money was disarmed at the audit check. No arming, caps, filters, targets or stops were changed by this follow-up.

## Already fixed by Cursor and preserved

Reversal LIVE_ALLOWED duplicate-argument crash; nested OI configuration; zero cooldown honored; serial candidate retry instead of dropping the second candidate; pending same-lane duplicate protection; Reversal BB width obeys BB OFF; three-second Prototype cadence.

## Remaining repairs in this follow-up

1. **Reversal OFF now works in BOTH paper and live evaluator paths.** Cursor put the OFF check after the legacy return, leaving ordinary paper mode active despite enabled=false.
2. **Last attempted entry now reports its stage, reason and timestamp** in scanner telemetry and beside the OI board. Includes direction restriction, reservation/open position, allowlist, retry/cooldown, OI veto, separate 8600 veto, missing contract, duplicate contract, accepted/rejected response and exceptions. OI ALLOWED is still not an order or fill.
3. **Pre-order errors do not discard the remaining candidate batch.** Reservations/setup flags are cleaned up and the next eligible candidate can be evaluated. A failure after an order submission has begun still aborts the batch and surfaces ERROR: the broker outcome may be unknown, so the repair does not immediately submit another lane on that assumption.
4. **Reversal trailing ignores candles closed before entry.** It retains the existing stop until an eligible post-entry completed candle exists. Cursor's all-candle-extremes trailing rule remains unchanged.

5. **A newer opposite crossover supersedes the older Reversal setup.** Old directions no longer fire after a newer opposite cross; the waiting status reports the current armed side.
6. **Prototype cadence description follows its actual configured value**, rather than always claiming 30 seconds.

7. **Route telemetry is normalized from the current desk and lane settings on every status response.** A live-eligible lane correctly reports paper execution while the desk is disarmed, rather than carrying a stale evaluator capability flag. This is display-only; execution routing is unchanged.

## Important setup differences

| Feature | Prototype | Reversal |
|---|---|---|
| Arming | Closed 15m price/8 EMA cross | Closed 4/8 EMA crossover |
| Entry | Later retouch of dynamic 8 EMA | Wait two bars, then first touch within the next two-bar window |
| Re-entry | Same-side repeats enabled; still one open position and subject to other checks | One successful entry per crossover, not unlimited retouches |
| Default exits | ±40 spot OR ±40 option premium points, whichever triggers first; opposite closed 15m side also exits | ±20 spot OR ±20 option premium points unless spot trail has armed |
| Candle trail | General Super Trail does not govern this fixed-exit lane | At +20 favorable spot points, spot target is replaced and completed candle extremes tighten stop; OPTION target/stop remains active |
| Trading window | No new entry before 09:30 or from 15:20 | No entry before 09:30; desk entry window/EOD applies |

LONG/SHORT here mean bullish CE buy / bearish PE buy. They are not option short-selling. The common reservation/open-position rule can make one lane stand down while another position is open. Enabling both lanes does not authorize simultaneous independent positions.

With Retouch Away OFF, current code can qualify using an earlier overlap in the forming candle; it does not insist price is precisely on the line at submission. This existing preference was not altered. Neither screenshots nor one-minute bars establish every three-second path or executable historical option fill.

## Controls and what they do

| Control | Effect |
|---|---|
| Master scanner ACTIVE / PAUSED | Enables/disables new scanner entries; not the same as lane enabled |
| Prototype ACTIVE / PAUSED | Stops Prototype evaluation when paused, even if its route/live checkbox permits live |
| Lane route PAPER_ONLY / LIVE_ALLOWED | PAPER_ONLY forces paper. LIVE_ALLOWED permits real money only when the desk is armed and lane opt-in is checked |
| Real money for this lane | Separate lane opt-in; does not arm the desk, enable a paused lane, create a setup or bypass filters |
| Desk typed LIVE confirmation | Global live permission; this audit does not activate it |
| Directional bias BOTH / LONG_ONLY / SHORT_ONLY | LONG_ONLY rejects PE/short-spot candidates; SHORT_ONLY rejects CE/long-spot candidates |
| Prototype Ribbon ON | Rejects when 4/8 EMA gap is within ribbon threshold; may prevent arming at a cross |
| Prototype EMA separation ON | Requires bullish 4>8 for CE or bearish 4<8 for PE |
| Prototype SuperTrend ON | Requires selected SuperTrend direction agreement |
| Retouch requires away ON | First observe movement away, then a later observation returning to the EMA. It prevents ambiguous same-observation away-and-touch |
| Retouch requires away OFF | Uses candle range overlap; an earlier touch in the same forming candle can qualify later. This is less temporally precise, not a promise of better fills |
| Normal tolerance | Touch band ±3 NIFTY points under current config |
| Slow market exact line | Narrows to zero-point tolerance; a near miss no longer qualifies |
| After profit same side ON | Prototype can reuse the armed side on a later eligible retouch; cooldown and open-position checks still apply |
| After stop same side OFF | Cancels the stopped side until an opposite close arms the other direction |
| After stop same side ON | Allows same-side Prototype re-entry after a stop; other entry conditions remain |
| Reversal TF 15m / 5m / both | Selects reversal timeframe evaluator. Current intended/tested operational focus here is 15m |
| Pre-arm 15m from 5m | Adds early context from 5m; not permission to skip 15m setup requirements |
| Reversal EMA separation | Requires configured minimum gap at the CROSS candle, not an arbitrary later gap |
| Reversal candle trail | After configured favorable NIFTY move, replaces fixed SPOT target with a distant sentinel and tightens SPOT stop from favorable closed candle lows/highs. The OPTION target/stop remains active; this is not an unlimited runner |
| General Super Trail | Does not govern the fixed spot-or-option exit lanes here. Do not assume a global trailing checkbox changes Prototype/Reversal |
| Bollinger switch | Reversal BB width/slope gates apply only when enabled after this repair |
| Party Switch | When ON, active bullish/bearish party can reject the opposite direction |
| OI checkbox | Includes that source in THAT panel's opinion/gate; does not create a technical trigger |
| OI Log only | Records/displays hypothetical opposition; does NOT veto, and does NOT guarantee an entry |
| OI Block | Enables a veto when configured fresh sources oppose under the selected rule |
| OI All agree | Any fresh opposing selected vote can veto (neutral/stale default abstain); name does not mean every missing source must agree |
| OI Any agrees | Blocks if there is opposition and zero agreement |
| OI Majority | Blocks if opposing votes exceed agreeing votes; tie allows in this lane panel |
| Neutral blocks | Counts neutral as opposition; stale/missing still abstains |
| Classic Writer Flow | Source's inferred CE/PE flow opinion, not proof of seller identity |
| Price vs OI | Recorded 15m ATM price/OI buildup classification |
| Cumulative Total OI | Positioning-derived reading; can differ from recent change |
| Fresh Change in OI | Session-change/derived momentum reading; does not double as independent PCR evidence |
| Volume-backed OI | Source's volume-related OI classification |
| Separate 8600 board FILTER | Independent gate from the lane OI panel. MAJORITY blocks when opposing votes are at least as numerous as agreeing votes and opposition exists (ties differ from the lane OI panel) |
| OI 3m feature / gate | Another separately enabled gate; context-only displays do not automatically veto |
| Contract premium minimum | Prototype and Reversal share Prototype's contract selector, currently ₹200 preferred / ₹150 fallback; missing valid executable quotes must not be filled with invented prices |
| Kill switch | Prevents live entry; does not imply that an existing broker position/protection was cancelled |


## How to use the actual app

Open http://127.0.0.1:8510/ and hard-refresh. Port 8880 is a read-only layout preview; it cannot save switches or execute orders. Per-lane LIVE_ALLOWED + lane opt-in + desk LIVE arming are all required, alongside a valid setup, fresh market data, resolved contract and broker acceptance. This audit does not arm the desk or send a real order.

The last-attempt message is historical and timestamped. ERROR or ORDER_ACCEPTED does not certify the current broker position; inspect the order/position ledger and broker fill status. After a restart it will be empty until a new genuine attempt, rather than displaying a fabricated example.

## Practical examples

| Situation | Expected result |
|---|---|
| Valid EMA trigger, route/live lane enabled, desk still PAPER_ONLY | Paper leg only; no broker buy |
| Prototype paused even though its live checkbox is checked | No Prototype entry |
| CE trigger with fresh opposing OI in Log only | OI opinion recorded; OI panel does not veto |
| Same CE trigger in Block mode | OI veto; no order from that attempt |
| Valid touch but no qualifying option quote/contract | No invented fill; last attempt identifies contract failure |
| Another position or pending EMA reservation exists | Stand down; no second EMA order |
| First candidate's contract lookup raises before any order call | ERROR shown, reservation released, next eligible candidate may be evaluated |
| Order call raises with unknown broker outcome | ERROR shown and batch stops; this is not interpreted as a confirmed rejection or fill |
| Reversal touch occurs after its configured crossover window | NO_TRADE; a later touch does not restart that crossover |
| Reversal reaches its option premium target while spot trailing is enabled | Existing option target can close it; enabling spot candle trail does not disable that option target |
