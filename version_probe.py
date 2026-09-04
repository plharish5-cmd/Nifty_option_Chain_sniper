"""
version_probe.py — is any file older than the code that depends on it?

check.py verifies each file is individually valid. That is not the same as
verifying they are the SAME GENERATION. Copy a two-week-old config.py over a
current one and every file still parses, every import still resolves, and
the app starts — then a panel silently reads a default instead of your
setting, or a function that should exist does not.

This looks for marker strings that only newer versions contain.
"""
import pathlib

MARKERS = {
    "config.py": [
        ("PDC_CANDLE", "PDC uses the 15:00 candle"),
        ("TAB18_VWAP_MODE", "VWAP/EMA tab source selector"),
        ("GEX_DEALER_SIGN", "GEX sign convention"),
        ("_FALLBACKS", "missing-key safety net"),
        ("CHAIN_COLOUR_MODE", "chain colour = premium change"),
        ("ALIGN_POLL_TO_CANDLES", "candle-aligned refresh"),
    ],
    "dhan_client.py": [
        ("daily_from_intraday", "daily bars from hourly candles"),
        ("futures_sanity", "wrong-instrument guard"),
        ("resolve_futures_by_price", "price-based futures resolver"),
        ("invalid_authentication", "token vs subscription diagnosis"),
    ],
    "market_data.py": [
        ("backfill_ohlc_dhan", "Dhan OHLC backfill"),
        ("_expected_session", "incomplete-response fallback"),
    ],
    "analytics.py": [
        ("pcr_change_state", "change-PCR sign handling"),
    ],
    "price_action.py": [
        ("coverage_report", "resampled-candle coverage"),
        ("candle_route", "candle provenance"),
    ],
    "build_master.py": [
        ("quiet", "quiet load_all"),
        ("STALE", "stale-levels warning"),
    ],
    "setups.py": [
        ("CHAIN_COLOUR_MODE", "premium-based chain colours"),
    ],
    "dashboard.py": [
        ("views.chart_levels", "migrated view"),
        ("CTX = _ctxmod.build", "shared context"),
        ("institutional", "heavyweights / GEX / decay"),
        ("import basis as _bs", "futures basis"),
        ("dhan_frames", "tab 18 from Dhan"),
        ("Daily OHLC not updated", "EOD failure now surfaced"),
    ],
    "strategy1.py": [
        ("trend_strength", "hold-through check"),
        ("BLOCK IS CORRECT", "gap rule reverted to block"),
    ],
    "strategy1_tab.py": [
        ("session_levels", "day high/low breakout"),
        ("wick_support", "gap-down wick zones"),
        ("exit time", "exit time column"),
    ],
    "flow.py": [
        ("heat_style", "matplotlib-free heatmap"),
        ("ChurnFlag", "churn detector"),
    ],
}

here = pathlib.Path(".")
stale, missing, ok = [], [], 0
print()
for fname, marks in MARKERS.items():
    p = here / fname
    if not p.exists():
        missing.append(fname)
        print(f"  MISSING  {fname}")
        continue
    src = p.read_text(errors="ignore").lower()
    gone = [(m, why) for m, why in marks if m.lower() not in src]
    if gone:
        stale.append(fname)
        print(f"  STALE    {fname}")
        for m, why in gone:
            print(f"             missing '{m}' — {why}")
    else:
        ok += 1
        print(f"  current  {fname}")

print()
if not stale and not missing:
    print("  every checked file is current.\n")
else:
    print(f"  {len(stale)} stale, {len(missing)} missing, {ok} current.")
    print("  Replace the stale ones from the latest zip.\n")
