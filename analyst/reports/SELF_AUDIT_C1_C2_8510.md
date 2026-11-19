# v2.0.42 verify-and-report — C1 tick gap & C2 IST bar boundaries

## C1 — Live tick gap (T1)

**Mechanism:** `start_ema8_prototype_intrabar_worker` sleeps via `_sleep_tracking_spot`, which samples
`cached_telemetry.spot` about **once per second** and stores min/max in `_tick_spot_range`. Each eval
cycle merges that range into `tick_high` / `tick_low` (`_take_tick_spot_range` + forming 1m highs).

**Fixture (conceptual):** spot jumps across the ±3 EMA band and returns within a single 1s sample
without the telemetry cache updating mid-second.

**Finding:** **PASS with residual risk.** A touch that occurs only between 1s spot samples and never
appears in the cache can still be missed. This is narrower than “between 3s polls” because the worker
tracks range across the refresh interval. No threshold/rule change applied; if the feed stalls entirely
for >refresh seconds, misses remain possible (covered by stale-spot pause + watchdog stale cause).

## C2 — IST bar boundaries (D3)

| Consumer | Anchor source | IST |
|----------|---------------|-----|
| Prototype 3s worker | `current_nse_15m_open_ts`, `resample_nse_15m`, `ensure_forming_b15_bar` | **PASS** (`Asia/Kolkata` via `nse_spot_candles_8510`) |
| MJ dynamic intrabar | Same 15m resample path in worker | **PASS** |
| OI baseline / desk8600 | Session day from `datetime.now(IST)`; board reads keyed to session | **PASS** (baseline fixes v242 unchanged) |
| arm reconstruct replay | `resample_nse_15m` on DB 1m | **PASS** |

**Finding:** **PASS** — all named consumers derive 15m/1H buckets from `nse_spot_candles_8510` (09:15
session anchor). No drift found in alternate `pandas` default resample on these hot paths.

## Part D — Flagship reconstruct

**Not included in v2.0.42.** `arm_reconstruct_8510.py` still rebuilds Prototype + Reversal only.
**S2′ remains a known gap.**