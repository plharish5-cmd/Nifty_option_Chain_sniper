"""Rebuild 1H Flagship pending entry from closed 15m confirmation bars."""
from __future__ import annotations

from typing import Any, Dict, Optional

import pandas as pd

from analyst.terminal8510_backend import detect_flagship_15m_confirmation


def recover_flagship_arm(
    b15: pd.DataFrame,
    *,
    mj_state: Dict[str, Any],
    session_day: str,
    no_entry_after: str = "15:15",
) -> bool:
    """Arm for the next live 15m evaluation; mark downtime triggers as consumed."""
    fr = mj_state.get("flagship_resume")
    if isinstance(fr, dict) and fr.get("armed"):
        return False
    if len(b15) < 4:
        return False

    last_closed = len(b15) - 2
    if last_closed < 1:
        return False
    try:
        hm = b15.index[last_closed].strftime("%H:%M")
    except Exception:
        hm = ""
    if hm >= no_entry_after:
        return False

    pa_long, pa_short = detect_flagship_15m_confirmation(b15, conf_idx=last_closed)
    if not pa_long and not pa_short:
        return False

    conf_i = last_closed
    conf_ts = int(b15.index[conf_i].timestamp())
    fired_long = f"{session_day}:FLAGSHIP_LONG:{conf_ts}"
    fired_short = f"{session_day}:FLAGSHIP_SHORT:{conf_ts}"

    # If confirmation bar itself would have triggered (close through entry ref), consume — no back-trade.
    entry_open = float(b15["open"].iloc[-1])
    ch = float(b15["high"].iloc[conf_i])
    cl = float(b15["close"].iloc[conf_i])

    if pa_long and mj_state.get("flagship_fired_key") != fired_long:
        if cl > ch * 0.999 and cl > entry_open:
            mj_state["flagship_fired_key"] = fired_long
            mj_state["day"] = session_day
            return False
        mj_state["flagship_resume"] = {
            "day": session_day,
            "armed": True,
            "direction": "LONG",
            "pattern": pa_long,
            "confirm_bar_ts": conf_ts,
            "entry_ref": entry_open,
        }
        mj_state["day"] = session_day
        return True

    if pa_short and mj_state.get("flagship_fired_key") != fired_short:
        if cl < ch * 1.001 and cl < entry_open:
            mj_state["flagship_fired_key"] = fired_short
            mj_state["day"] = session_day
            return False
        mj_state["flagship_resume"] = {
            "day": session_day,
            "armed": True,
            "direction": "SHORT",
            "pattern": pa_short,
            "confirm_bar_ts": conf_ts,
            "entry_ref": entry_open,
        }
        mj_state["day"] = session_day
        return True
    return False