"""Recover only missing repeat-entry arm from completed current-session candles.

No historical entry is submitted. Existing consumed/stop/position state wins.
"""
from analyst.ema8_prototype_8510 import (
    _close_crossed_above, _close_crossed_below, _ribbon_blocks,
    _arm_new, bar_open_timestamp, reentry_enabled,
)
from analyst.nse_spot_candles_8510 import ema4_ema8


def recover_missing_arm(b15, *, state, cfg, session_day):
    if (state.get('arm') or state.get('open') or state.get('consumed_cross_ts')
            or state.get('sl_blocked_side') or not reentry_enabled(cfg)
            or not cfg.get('enabled', True) or len(b15) < 5):
        return False
    e4,e8=ema4_ema8(b15['close'])
    latest=None
    # Caller supplies a forming last candle. Never infer a cross from it.
    for i in range(1,len(b15)-1):
        if b15.index[i].strftime('%Y-%m-%d') != session_day:
            continue
        prev=float(b15['close'].iloc[i-1]);close=float(b15['close'].iloc[i])
        old=float(e8.iloc[i-1]);line=float(e8.iloc[i]);op=float(b15['open'].iloc[i])
        up=_close_crossed_above(prev,old,close,line,op)
        down=_close_crossed_below(prev,old,close,line,op)
        if up or down:
            latest=(i,'LONG' if up else 'SHORT',line)
    if latest is None:
        return False
    i,side,line=latest
    if _ribbon_blocks(float(e4.iloc[i]),line,cfg):
        return False
    # Reconstruct away_seen from COMPLETED bars after the cross (never the forming
    # last bar). Had the engine stayed up, the live evaluator would have flipped
    # away_seen=True the moment a bar moved >= away_min_pts beyond the dynamic 8EMA.
    # Without this, a rebuilt arm starts away_seen=False and the FIRST live touch
    # after a mid-session restart is consumed just priming away_seen instead of
    # trading (the 14:45 miss on 2026-11-20). Requiring a demonstrated move-away on
    # a completed bar means this can never fabricate an entry at the cross itself.
    away_min = float(cfg.get('away_min_pts', 0.0) or 0.0)
    away_seen = False
    for j in range(i + 1, len(b15) - 1):
        if b15.index[j].strftime('%Y-%m-%d') != session_day:
            continue
        ej = float(e8.iloc[j])
        if side == 'LONG' and float(b15['high'].iloc[j]) > ej + away_min:
            away_seen = True
            break
        if side == 'SHORT' and float(b15['low'].iloc[j]) < ej - away_min:
            away_seen = True
            break
    _arm_new(state,direction=side,cross_bar_open_ts=bar_open_timestamp(b15,i),e8=line,away_seen=away_seen)
    state['day']=session_day
    return True
