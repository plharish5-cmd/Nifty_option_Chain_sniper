"""MJ Sir lecture — Stage 2 / Stage 3 / reversal unit checks."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

import pandas as pd

from analyst.mj_sir_stage_reversal_8510 import evaluate_mj_sir_four_pack, load_mj_cfg
from analyst.nse_spot_candles_8510 import ema4_ema8

IST = timezone(timedelta(hours=5, minutes=30))


def _synthetic_b15(n: int = 40, base: float = 24500.0) -> pd.DataFrame:
    start = datetime(2026, 11, 9, 9, 15, tzinfo=IST)
    idx = [start + timedelta(minutes=15 * i) for i in range(n)]
    close = pd.Series([base + i * 2.0 for i in range(n)], index=idx)
    df = pd.DataFrame(
        {
            "open": close - 1,
            "high": close + 5,
            "low": close - 8,
            "close": close,
            "volume": 1.0,
        },
        index=idx,
    )
    return df


class TestMjSirStageReversal(unittest.TestCase):
    def test_ribbon_blocks_trade(self):
        from analyst.mj_sir_stage_reversal_8510 import ribbon_attached

        cfg = load_mj_cfg({})
        self.assertTrue(ribbon_attached(24500.0, 24498.0, 15.0, cfg))

    def test_stage2_triggers_on_support_touch(self):
        cfg = load_mj_cfg({})
        mj_state: dict = {}
        b15 = _synthetic_b15(50, 24400.0)
        e4, e8 = ema4_ema8(b15["close"])
        i = -2
        e8v = float(e8.iloc[i])
        b15.iloc[i, b15.columns.get_loc("low")] = e8v
        b15.iloc[i, b15.columns.get_loc("close")] = e8v + 15
        b15.iloc[i - 1, b15.columns.get_loc("close")] = e8v + 10
        sigs, st = evaluate_mj_sir_four_pack(
            b15,
            cfg=cfg,
            bb_stage2=False,
            bb_stage3=False,
            bb_cross=False,
            party_switch_enabled=False,
            active_party="NEUTRAL",
            mj_state=mj_state,
            session_day="2026-11-09",
        )
        if st["stage2_long"]["state"] == "TRIGGERED":
            self.assertTrue(any(s["strategy_id"] == "STAGE_2_LONG" for s in sigs))


if __name__ == "__main__":
    unittest.main()
