"""Owner Rule D (27 Nov 2026): after +40, trail ONLY on confirming-colour candles —
green-candle low for CE/long, red-candle high for PE/short. Counter-colour candles
must NOT ratchet the stop. Covers the Prototype and Reversal live trails."""
from analyst.ema8_prototype_8510 import prototype_trail_tick
from analyst.reversal_spot_exit_8510 import apply_reversal_candle_trail_to_stop

GREEN = {"open": 25510.0, "high": 25560.0, "low": 25520.0, "close": 25555.0}  # close>open
RED = {"open": 25555.0, "high": 25560.0, "low": 25520.0, "close": 25510.0}    # close<open
# Short fixtures: candle high must be BELOW the stop (25500) to be able to tighten it down.
RED_LOW = {"open": 25490.0, "high": 25480.0, "low": 25460.0, "close": 25470.0}    # close<open
GREEN_LOW = {"open": 25470.0, "high": 25480.0, "low": 25460.0, "close": 25490.0}  # close>open


# ---------- Prototype ----------
def test_proto_long_ratchets_on_green_only():
    # armed long, stop at breakeven 25500, price well above stop
    ns, _, hit, _ = prototype_trail_tick(
        is_long=True, spot_entry=25500, spot_idx=25600,
        trigger_level=25540, stop_level=25500.0, armed=True,
        bar=GREEN, confirming_color_only=True,
    )
    assert not hit and ns == 25520.0  # ratcheted to green candle low


def test_proto_long_skips_red_candle():
    ns, _, hit, _ = prototype_trail_tick(
        is_long=True, spot_entry=25500, spot_idx=25600,
        trigger_level=25540, stop_level=25500.0, armed=True,
        bar=RED, confirming_color_only=True,
    )
    assert not hit and ns == 25500.0  # red candle must NOT lift a long stop


def test_proto_short_ratchets_on_red_only():
    # armed short, stop at breakeven 25500, price well below stop
    ns, _, hit, _ = prototype_trail_tick(
        is_long=False, spot_entry=25500, spot_idx=25400,
        trigger_level=25460, stop_level=25500.0, armed=True,
        bar=RED_LOW, confirming_color_only=True,
    )
    assert not hit and ns == 25480.0  # ratcheted to red candle high (below stop)


def test_proto_short_skips_green_candle():
    ns, _, hit, _ = prototype_trail_tick(
        is_long=False, spot_entry=25500, spot_idx=25400,
        trigger_level=25460, stop_level=25500.0, armed=True,
        bar=GREEN_LOW, confirming_color_only=True,
    )
    assert not hit and ns == 25500.0  # green candle must NOT lower a short stop


def test_proto_legacy_off_trails_every_candle():
    # confirming_color_only=False preserves old behaviour: red lifts a long stop
    ns, _, _, _ = prototype_trail_tick(
        is_long=True, spot_entry=25500, spot_idx=25600,
        trigger_level=25540, stop_level=25500.0, armed=True,
        bar=RED, confirming_color_only=False,
    )
    assert ns == 25520.0


# ---------- Reversal ----------
def test_reversal_long_green_only():
    assert apply_reversal_candle_trail_to_stop(
        is_long=True, spot_stop=25500.0, candle_o=GREEN["open"], candle_h=GREEN["high"],
        candle_l=GREEN["low"], candle_c=GREEN["close"], confirming_color_only=True,
    ) == 25520.0
    assert apply_reversal_candle_trail_to_stop(
        is_long=True, spot_stop=25500.0, candle_o=RED["open"], candle_h=RED["high"],
        candle_l=RED["low"], candle_c=RED["close"], confirming_color_only=True,
    ) == 25500.0  # red skipped


def test_reversal_short_red_only():
    assert apply_reversal_candle_trail_to_stop(
        is_long=False, spot_stop=25500.0, candle_o=RED_LOW["open"], candle_h=RED_LOW["high"],
        candle_l=RED_LOW["low"], candle_c=RED_LOW["close"], confirming_color_only=True,
    ) == 25480.0
    assert apply_reversal_candle_trail_to_stop(
        is_long=False, spot_stop=25500.0, candle_o=GREEN_LOW["open"], candle_h=GREEN_LOW["high"],
        candle_l=GREEN_LOW["low"], candle_c=GREEN_LOW["close"], confirming_color_only=True,
    ) == 25500.0  # green skipped
