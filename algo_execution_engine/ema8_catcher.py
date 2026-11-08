"""Intrabar 8-EMA Catching State Machine (Persistent State across ticks)."""
from __future__ import annotations
import logging
from dataclasses import asdict, dataclass
from typing import Any, Dict, Tuple

log = logging.getLogger("algo.ema8_catcher")

@dataclass
class Ema8CatchingState:
    timeframe: str
    closed_bar_ts: int = 0
    is_armed: bool = False
    armed_timestamp: int = 0
    frozen_ema8: float = 0.0
    arm_threshold_pts: float = 10.0
    min_bounce_pts: float = 8.0
    max_chase_pts: float = 12.0
    intrabar_touch_detected: bool = False
    touch_price: float = 0.0
    touch_timestamp: int = 0
    confirmed_bounce: bool = False
    action_taken_for_bar: bool = False

    def reset_for_new_bar(self, new_bar_ts: int, new_frozen_ema8: float) -> None:
        self.closed_bar_ts = new_bar_ts
        self.is_armed = False
        self.armed_timestamp = 0
        self.frozen_ema8 = round(new_frozen_ema8, 2)
        self.intrabar_touch_detected = False
        self.touch_price = 0.0
        self.touch_timestamp = 0
        self.confirmed_bounce = False
        self.action_taken_for_bar = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

def update_or_arm_state(
    state: Ema8CatchingState,
    closed_bar_ts: int,
    frozen_ema8: float,
    current_spot: float,
    current_ts: int,
) -> Ema8CatchingState:
    if state.closed_bar_ts != closed_bar_ts:
        state.reset_for_new_bar(closed_bar_ts, frozen_ema8)
    if state.action_taken_for_bar or frozen_ema8 <= 0.0:
        return state
    dist = abs(current_spot - state.frozen_ema8)
    if not state.is_armed and dist <= state.arm_threshold_pts:
        state.is_armed = True
        state.armed_timestamp = current_ts
        log.info("🎯 [8-EMA CATCHER ARMED] TF: %s | Spot: %.2f | Frozen 8 EMA: %.2f", state.timeframe, current_spot, state.frozen_ema8)
    return state

def check_intrabar_touch(
    state: Ema8CatchingState,
    bar_1m_high: float,
    bar_1m_low: float,
    current_spot: float,
    current_ts: int,
    side: str = "BUY",
) -> Tuple[bool, bool, str]:
    if not state.is_armed or state.action_taken_for_bar:
        return False, False, "STATE_NOT_ARMED_OR_COMPLETED"
    ema8 = state.frozen_ema8
    touch = (bar_1m_low <= ema8 <= bar_1m_high) or (abs(current_spot - ema8) <= 2.5)
    if touch and not state.intrabar_touch_detected:
        state.intrabar_touch_detected = True
        state.touch_price = ema8
        state.touch_timestamp = current_ts
        log.info("⚡ [INTRABAR 8-EMA TOUCH DETECTED] TF: %s | Frozen 8 EMA: %.2f", state.timeframe, ema8)
    if not state.intrabar_touch_detected:
        return False, False, "NO_TOUCH"
    if side == "BUY":
        bounce_pts = current_spot - min(bar_1m_low, ema8)
        if (current_spot >= ema8) and (bounce_pts >= state.min_bounce_pts):
            state.confirmed_bounce = True
            return True, True, f"BOUNCE_CONFIRMED (+{bounce_pts:.1f} pts)"
        return True, False, f"WAITING_BOUNCE (+{bounce_pts:.1f} pts)"
    else:
        rejection_pts = max(bar_1m_high, ema8) - current_spot
        if (current_spot <= ema8) and (rejection_pts >= state.min_bounce_pts):
            state.confirmed_bounce = True
            return True, True, f"REJECTION_CONFIRMED (-{rejection_pts:.1f} pts)"
        return True, False, f"WAITING_REJECTION (-{rejection_pts:.1f} pts)"

def should_enter_or_skip(
    state: Ema8CatchingState,
    current_spot: float,
    current_ts: int,
) -> Tuple[bool, str, Dict[str, Any]]:
    if state.action_taken_for_bar:return False, "BAR_ALREADY_HANDLED", {}
    if not (state.is_armed and state.intrabar_touch_detected and state.confirmed_bounce):
        return False, "PRECONDITIONS_NOT_MET", {}
    dist_from_ema8 = abs(current_spot - state.frozen_ema8)
    payload = {
        "timeframe": state.timeframe,
        "entry_spot": current_spot,
        "frozen_ema8": state.frozen_ema8,
        "distance_pts": round(dist_from_ema8, 2),
        "max_chase_allowed": state.max_chase_pts,
        "touch_ts": state.touch_timestamp,
        "execution_ts": current_ts,
    }
    if dist_from_ema8 <= state.max_chase_pts:
        return True, "ENTER_WITHIN_DISCIPLINE_WINDOW", payload
    state.action_taken_for_bar = True
    log.warning("🛑 [CHASE PREVENTED — MISSED RAN AWAY] TF: %s | Dist: %.2f pts > %.1f pts", state.timeframe, dist_from_ema8, state.max_chase_pts)
    return False, "MISSED_RAN_AWAY_CHASE_ABORTED", payload
