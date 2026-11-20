"""Is the 8600 app connected, and is what it sends live? One plain answer for the cockpit.

Display only. Reads the signal bridge's last result and the option-chain
context that already exist; decides nothing about any trade. Never raises.
"""
from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

BUILD_LABEL = "v2.0.23"
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
                if "timed out" in low or "timeout" in low:
                    d.update(state="TIMEOUT", headline="8600 local request timed out — app may be busy",
                             detail=reason[:160],
                             fix="A timeout does not prove the app is stopped or the internet is slow. Check the 8600 page and server load; source signals remain unavailable until a fresh response arrives.")
                elif any(k in low for k in ("refused", "urlerror", "connection", "unreachable", "errno")):
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


# --- v2.0.45: one loud, actionable stale banner + staleness transition log -------------------
# Display + observability only. The OI filters remain LOG mode / fail-open: a stale read is
# ignored and never blocks, skips, or exits a trade. These helpers only decide what to SHOW the
# owner and what to LOG — they take no trade decision.

STALE_FIX_CMD = "sudo systemctl restart paperdesk-ict.service"
# States that mean "8600 data cannot be trusted right now" (for the headline banner only).
_BANNER_STATES = {"STALE", "NOT_ANSWERING", "TIMEOUT", "NO_SESSION"}


def _minutes_from_age(age_sec: Optional[int], detail: str) -> Optional[int]:
    """Best-effort age in whole minutes: prefer a numeric age, else parse 'N min old' text."""
    if isinstance(age_sec, (int, float)) and age_sec >= 0:
        return int(age_sec) // 60
    low = str(detail or "").lower()
    if "min old" in low:
        for tok in low.replace("(", " ").replace(")", " ").split():
            if tok.isdigit():
                return int(tok)
    return None


def stale_banner(status: Dict[str, Any], *, staleness_alert_min: int = 3) -> Optional[Dict[str, Any]]:
    """One headline banner when the 8600 feed cannot be trusted, else None.

    Replaces five quiet '— stale' rows as the only signal. Returns:
      {level, state, age_min, text, fix_cmd}
    level: 'warn' (stale/old data) or 'error' (not answering / down).
    Never raises; display only; does not change any trade decision.
    """
    try:
        desk = (status or {}).get("desk8600") or {}
        chain = (status or {}).get("chain") or {}
        dstate = str(desk.get("state") or "")
        cstate = str(chain.get("state") or "")
        if dstate == "LIVE" and cstate == "LIVE":
            return None
        if dstate not in _BANNER_STATES and cstate != "STALE":
            return None

        age_min = _minutes_from_age(desk.get("bar_age_sec"), desk.get("detail") or "")
        chain_min = _minutes_from_age(None, chain.get("detail") or "")
        if chain_min is not None and (age_min is None or chain_min > age_min):
            age_min = chain_min

        # Threshold applies when we actually know the age; a down link (NOT_ANSWERING) always shows.
        down = dstate in {"NOT_ANSWERING", "TIMEOUT"}
        if not down and age_min is not None and age_min < staleness_alert_min:
            return None

        if down:
            level, state = "error", dstate
            head = "⚠ 8600 app is NOT answering"
        else:
            level, state = "warn", "STALE"
            age_txt = f"last snapshot {age_min} min old" if age_min is not None else "data is OLD"
            head = f"⚠ 8600 data is STALE ({age_txt})"
        text = (f"{head}. OI filters are being IGNORED (fail-open) — trades still run. "
                f"To restore: restart the 8600 app ({STALE_FIX_CMD}) and check its Dhan token.")
        return {"level": level, "state": state, "age_min": age_min, "text": text, "fix_cmd": STALE_FIX_CMD}
    except Exception:
        return None


def log_staleness_transition(prev_state: Optional[str], status: Dict[str, Any], now: datetime,
                             log_path: Any) -> Optional[str]:
    """Append a STALE START / STALE END line on a transition; return the line written, else None.

    prev_state is the caller-held previous desk8600 state ('' / None on first call). Pure I/O:
    the caller passes the log path (so tests can inject a temp file). Never raises.
    """
    try:
        desk = (status or {}).get("desk8600") or {}
        cur = str(desk.get("state") or "")
        prev = str(prev_state or "")
        was_bad = prev in _BANNER_STATES
        is_bad = cur in _BANNER_STATES
        if was_bad == is_bad:
            return None  # no transition
        ts = now.strftime("%H:%M:%S")
        if is_bad:
            bar = desk.get("bar_time") or "?"
            line = f"STALE START {ts} (state {cur}, last bar {bar}, age {desk.get('bar_age_sec')}s)"
        else:
            line = f"STALE END {ts} (recovered, state {cur})"
        try:
            p = Path(log_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:
            pass  # logging must never break the caller
        return line
    except Exception:
        return None
