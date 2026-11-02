"""
tests/test_dhan_algo_executor.py — Comprehensive Unit Tests for Custom Dhan API Algo Execution Engine.

Verifies:
1. Paper mode simulates order entry, -15pt spot SL exit, and +20/+40pt target exits (with trailing SL)
   without calling external Dhan HTTP endpoints.
2. Macro Trend-Day Anti-Fade Gate blocks counter-trend PE orders on +2.0Cr+ bullish days,
   and blocks counter-trend CE orders on -2.0Cr- bearish days.
3. Daily Kill-Switch blocks new entries once max_trades_per_day or max_daily_loss_inr is reached.
4. Strategy A (1H_8EMA_SUPPORT_BOUNCE), Strategy B (4H_1H_VIRGIN_WICK_REJECTION), and Strategy C (SNIPER_DIP_BUY_SYNERGY)
   trigger properly under verified conditions.
5. Emergency Panic button 'flatten_all_and_lock' squares off positions and locks executor.
"""
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
import asyncio
from unittest.mock import AsyncMock, patch

from app.engine.dhan_algo_executor import DhanAlgoExecutor, Position


@pytest.fixture
def clean_executor():
    """Provides a fresh DhanAlgoExecutor instance for isolated test runs."""
    eng = DhanAlgoExecutor()
    eng.execution_mode = "PAPER"
    eng.max_trades_per_day = 3
    eng.lots_per_trade = 1
    eng.lot_size = 75
    eng.max_daily_loss_inr = -3000.0
    eng.daily_trades_count = 0
    eng.daily_realized_pnl_inr = 0.0
    eng.is_locked = False
    eng.lock_reason = ""
    eng.active_position = None
    eng.pending_signal = None
    eng.closed_positions = []
    eng.execution_logs = []
    return eng


# ─────────────────────────────────────────────────────────────────────────────
# 1. PAPER MODE & SPOT-TRACKED AUTO-EXIT (SL, TP, TRAILING SL)
# ─────────────────────────────────────────────────────────────────────────────

def test_paper_mode_order_entry_and_sl_exit(clean_executor):
    """Verifies paper mode creates simulated order and exits on -15pt SL without HTTP calls."""
    async def _run():
        eng = clean_executor
        assert eng.execution_mode == "PAPER"

        trade_params = {
            "strategy": "1H_8EMA_SUPPORT_BOUNCE",
            "option_type": "CE",
            "strike": 24200.0,
            "security_id": "SIM_24200_CE",
            "symbol": "NIFTY 24200 CE",
            "spot": 24200.0,
            "entry_option_price": 150.0,
            "spot_sl": 24185.0,   # -15 pts
            "spot_tp1": 24220.0,  # +20 pts
            "spot_tp2": 24240.0,  # +40 pts
            "reason": "1H 8-EMA Bounce Test",
        }

        # 1. Entry
        res = await eng.trigger_order_entry(trade_params)
        assert res["ok"] is True
        assert eng.active_position is not None
        assert eng.active_position.entry_spot == 24200.0
        assert eng.active_position.spot_sl == 24185.0
        assert eng.active_position.quantity == 75

        # 2. Price fluctuates above SL: spot = 24195 (only -5 pts) -> Should NOT exit
        exit_res = await eng.update_spot_and_check_exit(24195.0)
        assert exit_res is None
        assert eng.active_position is not None
        assert eng.active_position.status == "OPEN"

        # 3. Price drops to SL: spot = 24184.5 (-15.5 pts) -> Must exit with CLOSED_SL
        exit_res = await eng.update_spot_and_check_exit(24184.5)
        assert exit_res is not None
        assert exit_res["ok"] is True
        assert exit_res["closed_position"]["status"] == "CLOSED_SL"
        assert eng.active_position is None
        assert eng.daily_trades_count == 1
        assert eng.daily_realized_pnl_inr < 0  # Loss recorded

    asyncio.run(_run())


def test_paper_mode_tp1_trail_sl_and_tp2_exit(clean_executor):
    """Verifies TP1 (+20pts) trails SL to cost, and TP2 (+40pts) closes position."""
    async def _run():
        eng = clean_executor

        trade_params = {
            "strategy": "1H_8EMA_SUPPORT_BOUNCE",
            "option_type": "CE",
            "strike": 24200.0,
            "security_id": "SIM_24200_CE",
            "symbol": "NIFTY 24200 CE",
            "spot": 24200.0,
            "entry_option_price": 150.0,
            "spot_sl": 24185.0,   # -15 pts
            "spot_tp1": 24220.0,  # +20 pts
            "spot_tp2": 24240.0,  # +40 pts
            "reason": "1H 8-EMA Bounce Trailing Test",
        }

        # Entry
        await eng.trigger_order_entry(trade_params)

        # 1. Price reaches TP1: spot = 24221.0 (+21 pts)
        await eng.update_spot_and_check_exit(24221.0)
        assert eng.active_position is not None
        assert eng.active_position.tp1_hit is True
        # SL must now be trailed to cost (24200.0)
        assert eng.active_position.spot_sl == 24200.0

        # 2. Price reaches TP2: spot = 24241.0 (+41 pts) -> Full target exit!
        exit_res = await eng.update_spot_and_check_exit(24241.0)
        assert exit_res is not None
        assert exit_res["closed_position"]["status"] == "CLOSED_TP"
        assert eng.active_position is None
        assert eng.daily_realized_pnl_inr > 0  # Profit recorded

    asyncio.run(_run())





# ─────────────────────────────────────────────────────────────────────────────
# 2. MACRO TREND-DAY ANTI-FADE GATE
# ─────────────────────────────────────────────────────────────────────────────

def test_macro_trend_anti_fade_gate_blocks_pe_on_bullish_trend_day(clean_executor):
    """
    NEVER fire a PE (short) order when cumulative Net PE−CE >= +2.0Cr
    and price is above 1H 8-EMA or PDH.
    """
    eng = clean_executor
    spot = 24350.0
    ema8_1h = 24300.0  # spot is above 8-EMA
    pdh = 24280.0      # spot is above PDH
    net_pe_ce_bullish = 25000000.0  # +2.5Cr

    # 1. Attempt PE order -> Must be REJECTED
    passed, reason = eng.check_macro_trend_anti_fade_gate(
        action_type="PE",
        spot=spot,
        net_pe_ce=net_pe_ce_bullish,
        ema8_1h=ema8_1h,
        pdh=pdh,
    )
    assert passed is False
    assert "ANTI-FADE BLOCKED" in reason
    assert "PE" in reason

    # 2. Attempt CE order on same bullish day -> Allowed
    passed_ce, _ = eng.check_macro_trend_anti_fade_gate(
        action_type="CE",
        spot=spot,
        net_pe_ce=net_pe_ce_bullish,
        ema8_1h=ema8_1h,
        pdh=pdh,
    )
    assert passed_ce is True


def test_macro_trend_anti_fade_gate_blocks_ce_on_bearish_trend_day(clean_executor):
    """
    NEVER fire a CE (long) order when cumulative Net PE−CE <= -2.0Cr
    and price is below 1H 8-EMA or PDL.
    """
    eng = clean_executor
    spot = 23900.0
    ema8_1h = 24000.0  # spot is below 8-EMA
    pdl = 23950.0      # spot is below PDL
    net_pe_ce_bearish = -22000000.0  # -2.2Cr

    # 1. Attempt CE order -> Must be REJECTED
    passed, reason = eng.check_macro_trend_anti_fade_gate(
        action_type="CE",
        spot=spot,
        net_pe_ce=net_pe_ce_bearish,
        ema8_1h=ema8_1h,
        pdl=pdl,
    )
    assert passed is False
    assert "ANTI-FADE BLOCKED" in reason
    assert "CE" in reason

    # 2. Attempt PE order on same bearish day -> Allowed
    passed_pe, _ = eng.check_macro_trend_anti_fade_gate(
        action_type="PE",
        spot=spot,
        net_pe_ce=net_pe_ce_bearish,
        ema8_1h=ema8_1h,
        pdl=pdl,
    )
    assert passed_pe is True


# ─────────────────────────────────────────────────────────────────────────────
# 3. DAILY KILL-SWITCH GATES (MAX TRADES & MAX DAILY LOSS)
# ─────────────────────────────────────────────────────────────────────────────

def test_daily_kill_switch_max_trades(clean_executor):
    """Blocks new entries once max_trades_per_day (3) is reached."""
    eng = clean_executor
    eng.daily_trades_count = 3
    eng.max_trades_per_day = 3

    can_trade, reason = eng.check_kill_switch()
    assert can_trade is False
    assert "Max daily trades limit reached" in reason


def test_daily_kill_switch_max_daily_loss(clean_executor):
    """Engages kill switch and locks engine once max_daily_loss_inr (-3000) is breached."""
    eng = clean_executor
    eng.daily_realized_pnl_inr = -3200.0  # <= -3000

    can_trade, reason = eng.check_kill_switch()
    assert can_trade is False
    assert eng.is_locked is True
    assert "Max daily loss" in eng.lock_reason


# ─────────────────────────────────────────────────────────────────────────────
# 4. STRATEGY TRIGGER VERIFICATIONS
# ─────────────────────────────────────────────────────────────────────────────

def test_strategy_a_1h_8ema_support_bounce(clean_executor):
    """Strategy A triggers when spot touches rising 1H 8-EMA with Net PE-CE >= +50L."""
    eng = clean_executor
    spot = 24201.0
    ema8 = 24200.0  # dist = 1.0 pt (<= 2.0 pts)
    slope = "RISING"
    net_pe_ce = 6000000.0  # +60L >= +50L

    sig = eng.evaluate_strategies(
        spot=spot,
        net_pe_ce=net_pe_ce,
        ema8_val=ema8,
        ema8_slope=slope,
        approach_state="TESTING_FROM_ABOVE_SUPPORT",
    )
    assert sig is not None
    assert sig["strategy"] == "1H_8EMA_SUPPORT_BOUNCE"
    assert sig["option_type"] == "CE"
    assert sig["strike"] == 24200.0
    assert sig["spot_sl"] == spot - 15.0
    assert sig["spot_tp1"] == spot + 20.0
    assert sig["spot_tp2"] == spot + 40.0


def test_strategy_b_virgin_wick_rejection(clean_executor):
    """Strategy B triggers when touching a virgin wick level aligned with S1."""
    eng = clean_executor
    spot = 24151.0

    active_levels = [
        {
            "id": "lvl-1h-sup",
            "tf": "1H",
            "side": "SUPPORT",
            "price": 24150.0,
            "touch_count": 0,  # Virgin
        }
    ]
    pivots_data = {
        "supports": [{"name": "S1", "price": 24152.0}],  # Aligned within 2 pts
    }

    sig = eng.evaluate_strategies(
        spot=spot,
        net_pe_ce=1000000.0,
        active_levels=active_levels,
        pivots_data=pivots_data,
        writer_flow_net=500000.0,
    )
    assert sig is not None
    assert sig["strategy"] == "4H_1H_VIRGIN_WICK_REJECTION"
    assert sig["option_type"] == "CE"
    assert sig["strike"] == 24150.0


def test_strategy_c_sniper_dip_buy_synergy(clean_executor):
    """Strategy C triggers on high-conviction ⚡ Synergy DIP BUY CE."""
    eng = clean_executor
    spot = 24250.0

    synergy_alert = {
        "setup": "DIP BUY CE",
        "synergy": True,
        "conviction_pct": 95,
        "conviction_band": "A+",
        "ts": 1000,
    }

    sig = eng.evaluate_strategies(
        spot=spot,
        net_pe_ce=3000000.0,  # Aligned bullish regime
        synergy_alert=synergy_alert,
    )
    assert sig is not None
    assert sig["strategy"] == "SNIPER_DIP_BUY_SYNERGY"
    assert sig["option_type"] == "CE"
    assert sig["strike"] == 24250.0


# ─────────────────────────────────────────────────────────────────────────────
# 5. EMERGENCY PANIC BUTTON (FLATTEN ALL & LOCK)
# ─────────────────────────────────────────────────────────────────────────────

def test_emergency_flatten_all_and_lock(clean_executor):
    """Panic button squares off active positions and permanently locks executor."""
    async def _run():
        eng = clean_executor

        # Open position
        await eng.trigger_order_entry({
            "strategy": "1H_8EMA_SUPPORT_BOUNCE",
            "option_type": "CE",
            "strike": 24200.0,
            "security_id": "SIM_24200_CE",
            "symbol": "NIFTY 24200 CE",
            "spot": 24200.0,
            "entry_option_price": 150.0,
            "spot_sl": 24185.0,
            "spot_tp1": 24220.0,
            "spot_tp2": 24240.0,
            "reason": "Test",
        })
        assert eng.active_position is not None

        # Trigger Emergency Panic Button
        res = await eng.flatten_all_and_lock()
        assert res["ok"] is True
        assert res["locked"] is True
        assert eng.is_locked is True
        assert eng.active_position is None

        # Subsequent trades must be blocked
        can_trade, reason = eng.check_kill_switch()
        assert can_trade is False
        assert "LOCKED" in reason

    asyncio.run(_run())


# ─────────────────────────────────────────────────────────────────────────────
# 6. FASTAPI ENDPOINTS VERIFICATION
# ─────────────────────────────────────────────────────────────────────────────

def test_algo_api_endpoints():
    """Verifies all /api/algo/* endpoints respond with proper schema."""
    from starlette.testclient import TestClient
    from app.api.main import app

    client = TestClient(app)

    # 1. GET /api/algo/status
    res = client.get("/api/algo/status")
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    assert data["execution_mode"] in ("PAPER", "SEMI_AUTO", "FULL_AUTO")
    assert "strategies_enabled" in data
    assert "daily_trades_count" in data

    # 2. POST /api/algo/config
    res_cfg = client.post("/api/algo/config", json={
        "execution_mode": "PAPER",
        "strategies": {"1H_8EMA_SUPPORT_BOUNCE": True, "4H_1H_VIRGIN_WICK_REJECTION": True, "SNIPER_DIP_BUY_SYNERGY": True},
        "lots_per_trade": 1,
        "max_trades_per_day": 3,
        "max_daily_loss_inr": -3000.0,
    })
    assert res_cfg.status_code == 200
    assert res_cfg.json()["execution_mode"] == "PAPER"

    # 3. POST /api/algo/unlock
    res_unlock = client.post("/api/algo/unlock")
    assert res_unlock.status_code == 200
    assert res_unlock.json()["is_locked"] is False

    # 4. POST /api/algo/dismiss-pending when none
    res_dismiss = client.post("/api/algo/dismiss-pending")
    assert res_dismiss.status_code == 200

    # 5. GET /api/algo/premarket-check
    res_chk = client.get("/api/algo/premarket-check")
    assert res_chk.status_code == 200
    chk_data = res_chk.json()
    assert chk_data["ok"] is True
    assert "items" in chk_data
    assert len(chk_data["items"]) == 6
    item_ids = [it["id"] for it in chk_data["items"]]
    assert "token_margin" in item_ids
    assert "expiry_contracts" in item_ids
    assert "candle_sync" in item_ids
    assert "gann_matrix" in item_ids
    assert "broker_sl" in item_ids
    assert "risk_guardrails" in item_ids
    assert "dual_sl_settings" in chk_data

    # 6. POST /api/algo/arm-sl
    res_arm = client.post("/api/algo/arm-sl", json={
        "spot_sl_pts": 15.0,
        "broker_sl_mode": "SL-M",
        "broker_sl_pts": 10.0,
        "broker_sl_type": "POINTS",
        "broker_sl_limit_buffer_pts": 1.5,
        "exchange_sl_armed": True,
    })
    assert res_arm.status_code == 200
    arm_data = res_arm.json()
    assert arm_data["ok"] is True
    assert arm_data["broker_sl_mode"] == "SL-M"
    assert arm_data["broker_sl_pts"] == 10.0

    # 7. POST /api/algo/target-mode
    res_tm = client.post("/api/algo/target-mode", json={"mode": "DYNAMIC"})
    assert res_tm.status_code == 200
    assert res_tm.json()["target_mode"] == "DYNAMIC"

    # 8. GET /api/algo/smart-target-preview
    res_st = client.get("/api/algo/smart-target-preview?option_type=CE")
    assert res_st.status_code == 200
    st_data = res_st.json()
    assert "target_1_pts" in st_data
    assert "target_2_pts" in st_data
    assert "reason" in st_data

    # 9. GET /api/algo/strategy-slots
    res_slots = client.get("/api/algo/strategy-slots")
    assert res_slots.status_code == 200
    slots = res_slots.json()
    assert len(slots) == 6
    slot_ids = [s["id"] for s in slots]
    assert "1H_8EMA_SUPPORT_BOUNCE" in slot_ids
    assert "WRITER_TRAP_BREAKOUT" in slot_ids
    assert "CUSTOM_RULE_BUILDER" in slot_ids

    # 10. POST /api/algo/strategy-slot/toggle
    res_tog = client.post("/api/algo/strategy-slot/toggle", json={"slot_id": "WRITER_TRAP_BREAKOUT", "enabled": True})
    assert res_tog.status_code == 200
    assert res_tog.json()["slot"]["enabled"] is True


def test_companion_exchange_sl_order_placement_and_cancellation(clean_executor):
    """Verifies companion Exchange-Side SL is dispatched on entry and cancelled on TP/SL exit."""
    async def _run():
        eng = clean_executor
        eng.execution_mode = "FULL_AUTO"
        eng.exchange_sl_armed = True
        eng.broker_sl_mode = "SL-M"
        eng.broker_sl_pts = 10.0

        trade_params = {
            "strategy": "1H_8EMA_SUPPORT_BOUNCE",
            "option_type": "CE",
            "strike": 23450.0,
            "security_id": "54050",
            "symbol": "NIFTY 23450 CE",
            "spot": 23440.0,
            "entry_option_price": 120.0,
            "spot_sl": 23425.0,
            "spot_tp1": 23460.0,
            "spot_tp2": 23480.0,
            "reason": "Exchange SL Test",
        }

        # Mock order placement methods
        with patch.object(eng, "place_dhan_order", new_callable=AsyncMock) as mock_place, \
             patch.object(eng, "place_dhan_sl_order", new_callable=AsyncMock) as mock_sl, \
             patch.object(eng, "cancel_dhan_order", new_callable=AsyncMock) as mock_cancel:

            mock_place.return_value = {"ok": True, "order_id": "DHAN_BUY_123"}
            mock_sl.return_value = {"ok": True, "order_id": "DHAN_SL_456"}
            mock_cancel.return_value = {"ok": True, "order_id": "DHAN_SL_456"}

            # 1. Trigger Entry -> places BUY order + companion SL order
            res = await eng.trigger_order_entry(trade_params)
            assert res["ok"] is True
            assert eng.active_position is not None
            assert eng.active_position.dhan_order_id == "DHAN_BUY_123"
            assert eng.active_position.broker_sl_order_id == "DHAN_SL_456"
            assert eng.active_position.broker_sl_trigger_price == 110.0  # 120 - 10.0 pts

            mock_place.assert_called_once()
            mock_sl.assert_called_once_with(
                security_id="54050",
                quantity=75,
                trigger_price=110.0,
                order_type="STOP_LOSS_MARKET",
                limit_price=0.0,
            )

            # 2. Price hits Target 2 -> Exits trade and cancels companion SL order
            exit_res = await eng.update_spot_and_check_exit(23485.0)
            assert exit_res is not None
            assert exit_res["ok"] is True
            assert eng.active_position is None

            mock_cancel.assert_called_once_with("DHAN_SL_456")

    asyncio.run(_run())


def test_compute_smart_target_front_running_and_modes(clean_executor):
    """Verifies compute_smart_target front-runs Call Writer Walls and respects regimes."""
    eng = clean_executor

    # 1. Strong Bullish Regime: Net PE-CE >= 1.5Cr -> Target 2 runner extended
    res_bull = eng.compute_smart_target(spot=23200.0, option_type="CE", net_pe_ce=18_000_000.0)
    assert res_bull["regime"] == "STRONG_BULLISH"
    assert res_bull["target_2_pts"] >= 50.0

    # 2. Call Writer Wall at 23,300 with Spot at 23,265 (Wall distance: 35 pts)
    # Target 2 (normally +40) should clamp to 23,300 - 5 = 23,295 (+30 pts from 23,265)
    res_wall = eng.compute_smart_target(
        spot=23265.0,
        option_type="CE",
        net_pe_ce=5_000_000.0,
        custom_call_wall=23300.0,
    )
    assert res_wall["target_2_price"] <= 23295.0
    assert "Call Wall" in res_wall["reason"]

    # 3. Fixed Mode: always +20 / +40 pts regardless of walls
    res_fixed = eng.compute_smart_target(
        spot=23265.0,
        option_type="CE",
        mode="FIXED",
        custom_call_wall=23300.0,
    )
    assert res_fixed["target_mode"] == "FIXED"
    assert res_fixed["target_1_pts"] == 20.0
    assert res_fixed["target_2_pts"] == 40.0
    assert res_fixed["target_1_price"] == 23285.0
    assert res_fixed["target_2_price"] == 23305.0



