"""Recover only missing repeat-entry arm from completed current-session candles.

No historical entry is submitted. Existing consumed/stop/position state wins.
"""
from analyst.ema8_prototype_8510 import (
    _close_crossed_above, _close_crossed_below, _ribbon_blocks,
    _arm_new, bar_open_timestamp, reentry_enabled, DEFAULT_CFG,
)
from analyst.nse_spot_candles_8510 import ema4_ema8


def recover_missing_arm(b15, *, state, cfg, session_day):
    if (state.get('arm') or state.get('open') or state.get('consumed_cross_ts')
            or not reentry_enabled(cfg) or not cfg.get('enabled', True) or len(b15) < 5):
        return False
    e4, e8 = ema4_ema8(b15['close'])
    away_min = float(cfg.get('away_min_pts', DEFAULT_CFG.get('away_min_pts', 0.0)) or 0.0)
    touch_tol = float(cfg.get('touch_tol_pts', DEFAULT_CFG.get('touch_tol_pts', 3.0)) or 3.0)
    sl_pts = float(cfg.get('stop_loss_pts', 40.0) or 40.0)
    tgt_pts = float(cfg.get('target_pts', 40.0) or 40.0)
    has_hl = ('high' in b15.columns) and ('low' in b15.columns)

    sl_blocked_side = str(state.get('sl_blocked_side') or '').upper() or None
    initial_sl_block = sl_blocked_side
    active_side = None
    cross_idx = None
    cross_line = None
    away_seen = False
    in_pos = False
    pos_sl = 0.0
    pos_tgt = 0.0

    # Caller supplies a forming last candle (len - 1). Never infer a cross or exit from it.
    for i in range(1, len(b15) - 1):
        if b15.index[i].strftime('%Y-%m-%d') != session_day:
            continue
        prev = float(b15['close'].iloc[i - 1])
        close = float(b15['close'].iloc[i])
        old = float(e8.iloc[i - 1])
        line = float(e8.iloc[i])
        op = float(b15['open'].iloc[i])
        up = _close_crossed_above(prev, old, close, line, op)
        down = _close_crossed_below(prev, old, close, line, op)

        if up:
            if sl_blocked_side == 'SHORT':
                sl_blocked_side = None  # Opposite 15m close clears SL block
            if sl_blocked_side != 'LONG' and not _ribbon_blocks(float(e4.iloc[i]), line, cfg):
                active_side = 'LONG'
                cross_idx = i
                cross_line = line
                away_seen = False
                in_pos = False
        elif down:
            if sl_blocked_side == 'LONG':
                sl_blocked_side = None  # Opposite 15m close clears SL block
            if sl_blocked_side != 'SHORT' and not _ribbon_blocks(float(e4.iloc[i]), line, cfg):
                active_side = 'SHORT'
                cross_idx = i
                cross_line = line
                away_seen = False
                in_pos = False

        # If armed and not in a simulated trade, check for move away and touch
        if active_side is not None and not in_pos:
            hi = float(b15['high'].iloc[i]) if has_hl else close
            lo = float(b15['low'].iloc[i]) if has_hl else close
            # Away seen on bars strictly after cross
            if cross_idx is not None and i > cross_idx:
                if active_side == 'LONG' and hi > line + away_min:
                    away_seen = True
                elif active_side == 'SHORT' and lo < line - away_min:
                    away_seen = True

            # If away_seen primed, check if touch occurs on this bar
            if away_seen:
                if active_side == 'LONG' and lo <= line + touch_tol:
                    in_pos = True
                    entry = line
                    pos_sl = entry - sl_pts
                    pos_tgt = entry + tgt_pts
                elif active_side == 'SHORT' and hi >= line - touch_tol:
                    in_pos = True
                    entry = line
                    pos_sl = entry + sl_pts
                    pos_tgt = entry - tgt_pts

        # If in simulated trade, check exit outcomes
        if in_pos:
            hi = float(b15['high'].iloc[i]) if has_hl else close
            lo = float(b15['low'].iloc[i]) if has_hl else close
            if active_side == 'LONG':
                if lo <= pos_sl:
                    # Stopped out: side becomes SL-blocked until opposite close
                    sl_blocked_side = 'LONG'
                    active_side = None
                    cross_idx = None
                    away_seen = False
                    in_pos = False
                elif hi >= pos_tgt:
                    # Profit target hit: arm retained for re-entry
                    in_pos = False
            elif active_side == 'SHORT':
                if hi >= pos_sl:
                    # Stopped out: side becomes SL-blocked until opposite close
                    sl_blocked_side = 'SHORT'
                    active_side = None
                    cross_idx = None
                    away_seen = False
                    in_pos = False
                elif lo <= pos_tgt:
                    # Profit target hit: arm retained for re-entry
                    in_pos = False

    if initial_sl_block and active_side == initial_sl_block:
        state['sl_blocked_side'] = initial_sl_block
        state.pop('arm', None)
        state['day'] = session_day
        return False

    if sl_blocked_side:
        state['sl_blocked_side'] = sl_blocked_side
        state.pop('arm', None)
        state['day'] = session_day
        return False

    if active_side is not None and cross_idx is not None:
        state.pop('sl_blocked_side', None)
        _arm_new(
            state,
            direction=active_side,
            cross_bar_open_ts=bar_open_timestamp(b15, cross_idx),
            e8=cross_line,
            away_seen=away_seen,
        )
        state['day'] = session_day
        return True

    return False
