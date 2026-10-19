"""
tests/test_intra_sweep.py — Unit tests for real-time intra-candle wick breach detection.
"""
import pytest
from app.api.hub import Hub
from app.engine.liquidity import Pool


@pytest.mark.anyio
async def test_intra_candle_pool_sweep_alert():
    hub = Hub()
    hub.primary = "5m"

    # Mock an analysis with armed pools
    class MockAnalysis:
        def __init__(self):
            self.pools = [
                Pool(side="BSL", src="PDH", price=24250.0, known_i=10, ts=1700000000),
                Pool(side="SSL", src="PDL", price=24050.0, known_i=10, ts=1700000000),
            ]

    hub.analyses["5m"] = MockAnalysis()

    alerts_sent = []

    async def mock_broadcast(msg):
        if msg.get("type") == "alert":
            alerts_sent.append(msg["alert"])

    hub.broadcast = mock_broadcast

    # 1. Price below PDH: no alert
    await hub._check_intra_sweep(24240.0, 1700000060.0)
    assert len(alerts_sent) == 0

    # Reset throttle timer to simulate 1 second elapsed
    hub._last_intra_sweep_check = 0.0

    # 2. Price breaches PDH (BSL): in-wick sweep alert fires immediately
    await hub._check_intra_sweep(24252.0, 1700000065.0)
    assert len(alerts_sent) == 1
    assert alerts_sent[0]["kind"] == "IN_WICK_SWEEP"
    assert "PDH" in alerts_sent[0]["title"]
    assert alerts_sent[0]["price"] == 24252.0

    # Reset throttle timer but within cooldown: should not duplicate
    hub._last_intra_sweep_check = 0.0
    await hub._check_intra_sweep(24255.0, 1700000070.0)
    assert len(alerts_sent) == 1

    # Reset throttle timer and breach SSL pool (PDL)
    hub._last_intra_sweep_check = 0.0
    await hub._check_intra_sweep(24045.0, 1700000075.0)
    assert len(alerts_sent) == 2
    assert alerts_sent[1]["kind"] == "IN_WICK_SWEEP"
    assert "PDL" in alerts_sent[1]["title"]
    assert alerts_sent[1]["side"] == "bull"
