"""Is the 8600 app connected, and is what it sends live? One plain answer for the cockpit.

Display only. Reads the signal bridge's last result and the option-chain
context that already exist; decides nothing about any trade. Never raises.
"""
from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

BUILD_LABEL = "v2.0.11"
WHATS_NEW = [
    ["8 EMA Prototype card", "Algo Terminal tab → strategy cards → gold-bordered card. Sentence + 5-step tracker at the top."],
    ["8600 Board (15 option-chain reads, ON/OFF buttons)", "Inside the 8 EMA Prototype card, below the filter checkboxes. Scroll down inside the card."],
    ["How the Board acts on the Prototype", "Board → 'Applies to' row: the '8 EMA Prototype' button is lit. A read filters trades only when its own button is ON."],
    ["8600 Ticket Lane card", "Algo Terminal tab → the teal-bordered card right after the 8 EMA Prototype card."],
    ["Active Positions summary + stop→target bar", "Positions & Orders tab → first card."],
    ["Orders Book summary, Today / All days, Completed trades", "Positions & Orders tab → second card."],
    ["Points analysis with max drawdown", "Replay tab → appears under the result numbers after you run a replay or a backtest."],
]


def build_id(root: Path) -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=str(root), capture_output=True,
                             text=True, timeout=3)
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def _age(stamp: Any, now: datetime) -> Optional[int]:
    try:
        return int(max(0.0, (now - datetime.fromisoformat(str(stamp))).total_seconds()))
    except (TypeError, ValueError):
        return None


def link_status(bridge: Any, oi_ctx: Optional[Dict[str, Any]], now: datetime, *, market_open: Optional[bool] = None,
                live_after_sec: int = 420) -> Dict[str, Any]:
    """{"desk8600": {state, headline, detail, fix, ...}, "chain": {state, headline, detail, fix}}

    desk8600.state: LIVE | STALE | NOT_ANSWERING | NO_SESSION | STARTING | OFF
    chain.state:    LIVE | STALE | NO_DATA
    """
    d: Dict[str, Any] = {"state": "STARTING", "headline": "8600 link is starting", "detail": "", "fix": "",
                         "base_url": "", "polled_age_sec": None, "bar_time": None, "bar_age_sec": None}
    try:
        cfg = getattr(bridge, "cfg", None) or {}
        d["base_url"] = str(cfg.get("base_url") or "")
        last = getattr(bridge, "last", None) or {}
        if bridge is None:
            d.update(state="OFF", headline="8600 link is not running in this 8510 engine",
                     fix="Restart the 8510 app so it loads the current code.")
        elif cfg.get("enabled", True) is False:
            d.update(state="OFF", headline="8600 link is switched off in the config",
                     fix="Set ict_smc_desk_signals.enabled to true and restart the 8510 app.")
        elif not last:
            d.update(headline="Waiting for the first answer from the 8600 app",
                     detail=f"asking {d['base_url']} every {cfg.get('cache_seconds', 20)} seconds")
        else:
            d["polled_age_sec"] = _age(last.get("evaluated_at"), now)
            rows = {str(r.get("signal_group")): r for r in (last.get("rows") or []) if isinstance(r, dict)}
            act = rows.get("Actionable Setups") or {}
            reason = str(act.get("reason") or "")
            if str(act.get("verdict") or "UNAVAILABLE") == "UNAVAILABLE":
                low = reason.lower()
                if any(k in low for k in ("refused", "urlerror", "timed out", "timeout", "connection", "unreachable", "errno")):
                    d.update(state="NOT_ANSWERING", headline="8600 app is NOT answering",
                             detail=reason[:160],
                             fix=f"Start the 8600 app and check it opens at {d['base_url']} in the browser. This card turns LIVE within 20 seconds.")
                elif "not current" in low or "no closed source bar" in low or "no source rows" in low:
                    d.update(state="NO_SESSION", headline="8600 app is connected — no bars for today's session yet",
                             detail=reason[:160],
                             fix="Normal before 09:18 and on a closed market. Tickets start after the first closed 3-minute bar.")
                elif "stale" in low:
                    d.update(state="STALE", headline="8600 app is connected but its data is OLD",
                             detail=reason[:160],
                             fix="The 8600 app has stopped receiving market data. Check its own feed / token and restart it.")
                else:
                    d.update(state="NOT_ANSWERING", headline="8600 app answered, but the reading could not be used",
                             detail=reason[:160] or "no reason given", fix="Open the 8600 app and check its OC Lab bar-flow table is filling.")
            else:
                age = _age(act.get("source_ts"), now)
                d["bar_age_sec"] = age
                d["bar_time"] = str(act.get("source_ts") or "")[11:16]
                if age is not None and age <= live_after_sec:
                    d.update(state="LIVE", headline="8600 app is connected — LIVE data",
                             detail=f"last closed 3-minute bar ended {d['bar_time']} ({age} s ago) · tape: {act.get('verdict')}")
                else:
                    d.update(state="STALE", headline="8600 app is connected but its data is OLD",
                             detail=f"last closed bar ended {d['bar_time']}",
                             fix="The 8600 app has stopped receiving market data. Check its own feed / token and restart it.")
    except Exception as exc:
        d.update(state="NOT_ANSWERING", headline="8600 link check failed", detail=type(exc).__name__)

    c: Dict[str, Any] = {"state": "NO_DATA", "headline": "No option-chain snapshots for today", "detail": "", "fix": ""}
    try:
        ctx = oi_ctx or {}
        if ctx.get("status") == "OK":
            c.update(state="LIVE", headline="Option-chain snapshots — LIVE",
                     detail=f"newest snapshot {ctx.get('snapshot_time')} ({ctx.get('snapshot_age_sec')} s ago) · expiry {ctx.get('expiry')}")
        else:
            why = str(ctx.get("reason") or "no snapshots")
            stale = "min old" in why
            c.update(state="STALE" if stale else "NO_DATA",
                     headline="Option-chain snapshots are OLD" if stale else "No option-chain snapshots for today yet",
                     detail=why,
                     fix="Snapshots are written by the 8600 app and the 8510 recorder during market hours. "
                         "If the market is open, check both are running.")
    except Exception as exc:
        c.update(detail=type(exc).__name__)
    if market_open is False:
        for x in (d, c):
            if x["state"] != "LIVE":
                x["fix"] = "Market is closed now — this is expected. It turns LIVE after 09:15 when the 8600 app is running."
    return {"desk8600": d, "chain": c, "market_open": market_open, "checked_at": now.strftime("%H:%M:%S")}
