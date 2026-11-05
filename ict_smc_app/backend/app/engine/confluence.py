"""
confluence.py — one normalised verdict row per desk engine, plus a consensus gate.

WHY THIS EXISTS
---------------
The desk runs ~40 engines across 91 endpoints, each emitting its own JSON shape and
its own human-readable verdict string. There has never been a single place that says
"here is what every engine currently thinks, and how old each opinion is". This module
is that place. It is also the feed for the 8500/8510 algo gate.

DESIGN RULES (deliberate, please keep)
-------------------------------------
1. PURE. This module imports no engine and touches no network. The caller fetches the
   payloads and hands them in. That makes the whole thing unit-testable with no DB,
   no Dhan token and no market session.
2. NEVER GUESS. An engine whose payload is missing, malformed or whose verdict text
   does not match any known phrase returns UNKNOWN / UNAVAILABLE. It is excluded from
   consensus. A fabricated verdict is worse than no verdict.
3. AGE IS PART OF THE VERDICT. Every row carries the age of its own data and a
   FRESH / STALE / UNAVAILABLE status. Stale rows are displayed but never counted.
   (The desk has twice shipped a green badge that was not checking anything.)
4. EVIDENCE IS PART OF THE VERDICT. Engines measured and refuted are shown with a
   REFUTED tag and are structurally barred from consensus, whatever they say.
5. NO HARDCODED THRESHOLDS. Everything tunable lives in confluence_config.json.

Python 3.9 compatible: no PEP 604 unions anywhere (the desk runs 3.9.23).
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("desk.confluence")

CONFIG_PATH = Path(__file__).resolve().parent / "confluence_config.json"

# Verdicts that carry a directional opinion, in the order a human reads them.
DIRECTIONAL = ("BULLISH", "BEARISH", "REVERSAL_UP", "REVERSAL_DOWN",
               "BREAKOUT_UP", "BREAKOUT_DOWN", "SUPPORT", "RESISTANCE")

# Keys an engine might use for its own data timestamp, most explicit first.
TS_KEYS = ("ts", "asof", "as_of", "timestamp", "updated_ts", "fetched_at", "updated_at")

# Reserved key the gatherer stamps on each payload recording when WE called the
# engine. Used only as a fallback when the engine publishes no timestamp of its
# own. It is a weaker guarantee — it says "we asked 2s ago", not "the underlying
# data is 2s old" — so rows resolved this way are tagged age_basis="fetch" and
# counted in board["health"]["fetch_aged"] so the weakness stays visible.
FETCH_TS_KEY = "_confluence_fetched_ts"

# AUTHORITATIVE verdict fields, most trustworthy first. Classification uses the
# FIRST of these that is present — never a concatenation. That matters: desk_bias
# returns bias_theme="bullish" alongside a narrative mentioning "Stage 3 Bearish
# Acceleration", and blending the two would let prose outvote the engine's own
# stated verdict.
VERDICT_KEYS = ("bias_theme", "alignment_state", "current_verdict", "verdict",
                "verdict_badge", "confluence_summary", "dominant_bias", "dominant_tag",
                "gamma_regime", "pin_state", "bias_title", "bias", "bias_trend",
                "day_bias", "h1_bias", "pcr_band", "regime_state", "latest_state",
                "stage", "model", "phase", "signal", "state", "action", "headline",
                "direction", "side", "gauge_label", "pin_label", "label", "mode",
                "status")

# Shown to the operator, never used for classification — prose may contain words
# that contradict the engine's own verdict.
DETAIL_KEYS = ("verdict_summary", "pcr_band_label", "narrative", "confluence_summary",
               "text", "summary", "risk_note", "target_wall", "note")

_CFG_CACHE: Dict[str, Any] = {}
_CFG_MTIME: float = 0.0


def load_config(path: Optional[Path] = None) -> Dict[str, Any]:
    """Reads confluence_config.json, re-reading only when the file changes on disk."""
    global _CFG_CACHE, _CFG_MTIME
    p = path or CONFIG_PATH
    try:
        mtime = p.stat().st_mtime
    except OSError as exc:
        log.error("confluence_config.json unreadable at %s: %s", p, exc)
        return _CFG_CACHE or {}
    if _CFG_CACHE and mtime == _CFG_MTIME:
        return _CFG_CACHE
    try:
        _CFG_CACHE = json.loads(p.read_text(encoding="utf-8"))
        _CFG_MTIME = mtime
    except (OSError, ValueError) as exc:
        log.error("confluence_config.json invalid: %s", exc)
        return _CFG_CACHE or {}
    return _CFG_CACHE


# ─────────────────────────────────────────────────────────────────────────────
# TEXT -> VERDICT
# ─────────────────────────────────────────────────────────────────────────────

def classify_text(text: str, cfg: Optional[Dict[str, Any]] = None) -> str:
    """Maps an engine's human-readable verdict string to a normalised verdict.

    Longest phrase wins, so "bullish reversal" beats "bullish" and "strong bearish"
    beats "bearish". Returns UNKNOWN when nothing matches - never a guess.
    """
    if not text:
        return "UNKNOWN"
    cfg = cfg or load_config()
    keywords = cfg.get("keywords", {})
    # Engines emit SNAKE_CASE enums (BULLISH_PUT_CONTROL, POSITIVE_GAMMA) as well
    # as prose. Normalising underscores to spaces lets one space-separated phrase
    # in the config match both forms, instead of maintaining two spellings each.
    low = " ".join(str(text).lower().replace("_", " ").split())

    best_verdict = "UNKNOWN"
    best_len = 0
    for verdict, phrases in keywords.items():
        if verdict.startswith("_") or not isinstance(phrases, list):
            continue
        for phrase in phrases:
            p = str(phrase).lower()
            if p and p in low and len(p) > best_len:
                best_verdict, best_len = verdict, len(p)
    return best_verdict


def verdict_bias(verdict: str, cfg: Optional[Dict[str, Any]] = None) -> int:
    cfg = cfg or load_config()
    table = cfg.get("verdict_bias", {})
    val = table.get(verdict, 0)
    try:
        return int(val)
    except (TypeError, ValueError):
        return 0


# ─────────────────────────────────────────────────────────────────────────────
# PAYLOAD -> TIMESTAMP / TEXT / LEVELS
# ─────────────────────────────────────────────────────────────────────────────

def extract_ts(payload: Any, now_ts: float) -> Tuple[Optional[float], str]:
    """Finds the payload's own data timestamp. Returns (epoch_seconds, source_key).

    Accepts epoch ints/floats and ISO-ish strings. A value that is clearly not a
    timestamp (e.g. "15:28:03 IST") is rejected rather than fabricated into 'now'.

    If the engine publishes no timestamp at all, falls back to FETCH_TS_KEY - when
    we called it - and returns that key so the caller can tag the row age_basis
    "fetch". That is deliberately a second-class answer, not a substitute.
    """
    if not isinstance(payload, dict):
        return None, ""
    for key in TS_KEYS:
        if key not in payload:
            continue
        raw = payload[key]
        if isinstance(raw, (int, float)) and raw > 0:
            val = float(raw)
            if val > 1e11:          # milliseconds
                val /= 1000.0
            # Sanity: within ~10 years of now, else it is not an epoch.
            if abs(now_ts - val) < 10 * 365 * 24 * 3600:
                return val, key
        elif isinstance(raw, str) and raw.strip():
            txt = raw.strip()
            if txt.isdigit():
                val = float(txt)
                if val > 1e11:
                    val /= 1000.0
                if abs(now_ts - val) < 10 * 365 * 24 * 3600:
                    return val, key
    fetched = payload.get(FETCH_TS_KEY)
    if isinstance(fetched, (int, float)) and fetched > 0:
        return float(fetched), FETCH_TS_KEY
    return None, ""


def extract_verdict_text(payload: Any) -> Tuple[str, str]:
    """The single most authoritative verdict string, as (text, source_key).

    Returns the FIRST VERDICT_KEYS hit rather than a concatenation, so an engine's
    own stated verdict cannot be outvoted by contradictory words in its prose.

    Many engines nest their verdict one level down and each uses a different
    container name (auction under `today`, gamma under `gamma_review`,
    pe_ce_matrix under `regime`). Rather than maintain a list of container names
    that silently rots whenever an engine is refactored, the fallback scans every
    top-level dict value one level deep, in VERDICT_KEYS priority order.
    """
    if not isinstance(payload, dict):
        return "", ""
    for key in VERDICT_KEYS:
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip(), key
    # Priority is by verdict key, not by container, so a `regime` nested anywhere
    # outranks a `status` nested anywhere.
    for key in VERDICT_KEYS:
        for sub_key, sub in payload.items():
            if not isinstance(sub, dict):
                continue
            val = sub.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip(), "{}.{}".format(sub_key, key)
    return "", ""


def extract_detail(payload: Any) -> str:
    """Human-readable context for the operator. Never used for classification."""
    if not isinstance(payload, dict):
        return ""
    parts: List[str] = []
    for key in DETAIL_KEYS:
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            parts.append(val.strip())
        if len(parts) >= 2:
            break
    return " · ".join(parts)


def extract_levels(payload: Any) -> Dict[str, Optional[float]]:
    """Pulls support/resistance style levels where an engine publishes them."""
    out: Dict[str, Optional[float]] = {"support": None, "resistance": None}
    if not isinstance(payload, dict):
        return out
    sup_keys = ("key_support_level", "pe_wall", "support", "sup", "put_wall",
                "struct_support", "demand")
    res_keys = ("key_resistance_level", "ce_wall", "resistance", "res", "call_wall",
                "struct_resistance", "supply")
    for k in sup_keys:
        v = payload.get(k)
        if isinstance(v, (int, float)) and v > 0:
            out["support"] = float(v)
            break
    for k in res_keys:
        v = payload.get(k)
        if isinstance(v, (int, float)) and v > 0:
            out["resistance"] = float(v)
            break
    return out


# ─────────────────────────────────────────────────────────────────────────────
# ONE ENGINE -> ONE ROW
# ─────────────────────────────────────────────────────────────────────────────

def build_row(engine_cfg: Dict[str, Any], payload: Any, now_ts: float,
              cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Normalises a single engine payload into one board row. Never raises."""
    cfg = cfg or load_config()
    fresh_cfg = cfg.get("freshness", {})
    fresh_max = float(fresh_cfg.get("fresh_max_age_s", 300))
    stale_max = float(fresh_cfg.get("stale_max_age_s", 900))

    evidence = str(engine_cfg.get("evidence", "UNTESTED")).upper()
    row: Dict[str, Any] = {
        "key": engine_cfg.get("key"),
        "label": engine_cfg.get("label", engine_cfg.get("key")),
        "category": engine_cfg.get("category", "OTHER"),
        "endpoint": engine_cfg.get("endpoint"),
        "evidence": evidence,
        "evidence_note": engine_cfg.get("evidence_note", ""),
        "eligible": bool(engine_cfg.get("in_consensus", False)) and evidence != "REFUTED",
        "verdict": "UNKNOWN",
        "bias": 0,
        "detail": "",
        "levels": {"support": None, "resistance": None},
        "age_s": None,
        "ts": None,
        "age_basis": None,
        "status": "UNAVAILABLE",
        "counted": False,
        "reason": "",
    }

    if payload is None:
        row["reason"] = "no payload"
        return row
    if isinstance(payload, dict) and payload.get("ok") is False:
        row["reason"] = str(payload.get("error") or payload.get("message") or "engine reported not ok")
        return row

    try:
        verdict_text, verdict_key = extract_verdict_text(payload)
        row["verdict_text"] = verdict_text[:160]
        row["verdict_source"] = verdict_key
        row["detail"] = (extract_detail(payload) or verdict_text)[:220]
        row["verdict"] = classify_text(verdict_text, cfg)
        row["bias"] = verdict_bias(row["verdict"], cfg)
        row["levels"] = extract_levels(payload)

        ts, ts_key = extract_ts(payload, now_ts)
        if ts is None:
            row["status"] = "UNKNOWN_AGE"
            row["reason"] = "payload carries no usable timestamp"
        else:
            age = max(0.0, now_ts - ts)
            row["ts"] = int(ts)
            row["age_s"] = round(age, 1)
            row["ts_source"] = ts_key
            row["age_basis"] = "fetch" if ts_key == FETCH_TS_KEY else "data"
            if age <= fresh_max:
                row["status"] = "FRESH"
            elif age <= stale_max:
                row["status"] = "STALE"
            else:
                row["status"] = "EXPIRED"
    except Exception as exc:  # defensive: one bad engine must not kill the board
        log.warning("confluence: engine %s failed to normalise: %s", row["key"], exc)
        row["reason"] = "normalise error: {}".format(exc)
        return row

    row["counted"] = bool(
        row["eligible"]
        and row["status"] == "FRESH"
        and row["verdict"] in DIRECTIONAL
    )
    if not row["counted"] and not row["reason"]:
        if not row["eligible"]:
            row["reason"] = "refuted" if evidence == "REFUTED" else "not in consensus set"
        elif row["status"] != "FRESH":
            row["reason"] = "not fresh ({})".format(row["status"])
        else:
            row["reason"] = "no directional verdict"
    return row


# ─────────────────────────────────────────────────────────────────────────────
# WHOLE BOARD + CONSENSUS
# ─────────────────────────────────────────────────────────────────────────────

def build_board(payloads: Dict[str, Any], now_ts: Optional[float] = None,
                cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """payloads: {engine_key: raw_payload_or_None} -> full confluence board.

    The caller is responsible for fetching. Missing keys are reported as
    UNAVAILABLE rather than silently dropped, so the board always lists every
    registered engine and you can see what is not reporting.
    """
    cfg = cfg or load_config()
    now_ts = float(now_ts if now_ts is not None else time.time())

    rows = [build_row(e, payloads.get(e.get("key")), now_ts, cfg)
            for e in cfg.get("engines", [])]
    for r, e in zip(rows, cfg.get("engines", [])):
        r["watchlist"] = bool(e.get("watchlist", False))
        r["synthesis"] = bool(e.get("synthesis", False))

    # The headline: a synthesis engine (desk_bias) summarises the others, so it
    # reads as the top line rather than as another vote.
    headline = next((r for r in rows if r["synthesis"] and r["status"] == "FRESH"), None)

    counted = [r for r in rows if r["counted"]]
    net = sum(r["bias"] for r in counted)
    bulls = sum(1 for r in counted if r["bias"] > 0)
    bears = sum(1 for r in counted if r["bias"] < 0)

    c_cfg = cfg.get("consensus", {})
    min_engines = int(c_cfg.get("min_fresh_engines", 3))
    min_net = int(c_cfg.get("min_net_bias", 2))

    if len(counted) < min_engines:
        consensus, gate = "NO_CONSENSUS", "ALLOW_ALL"
        reason = "only {} eligible fresh engine(s); need {}".format(len(counted), min_engines)
    elif net >= min_net:
        consensus, gate = "BULLISH", "CE_ONLY"
        reason = "net +{} across {} engines".format(net, len(counted))
    elif net <= -min_net:
        consensus, gate = "BEARISH", "PE_ONLY"
        reason = "net {} across {} engines".format(net, len(counted))
    else:
        consensus, gate = "NO_CONSENSUS", "ALLOW_ALL"
        reason = "net {} below threshold {}".format(net, min_net)

    by_cat: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        by_cat.setdefault(r["category"], []).append(r)

    return {
        "ok": True,
        "generated_ts": int(now_ts),
        "rows": rows,
        "watchlist": [r for r in rows if r["watchlist"]],
        "headline": headline,
        "by_category": by_cat,
        "consensus": {
            "state": consensus,
            "gate": gate,
            "net_bias": net,
            "bulls": bulls,
            "bears": bears,
            "counted": len(counted),
            "total": len(rows),
            "reason": reason,
            "_fail_open": gate == "ALLOW_ALL",
        },
        "health": {
            "fresh": sum(1 for r in rows if r["status"] == "FRESH"),
            "stale": sum(1 for r in rows if r["status"] in ("STALE", "EXPIRED")),
            "unavailable": sum(1 for r in rows if r["status"] in ("UNAVAILABLE", "UNKNOWN_AGE")),
            "refuted_shown": sum(1 for r in rows if r["evidence"] == "REFUTED"),
            "fetch_aged": sum(1 for r in rows if r.get("age_basis") == "fetch"),
            "_fetch_aged_note": "engines that publish no timestamp of their own; "
                                "age shown is when we called them, not how old their data is",
        },
    }


def gate_allows(board: Dict[str, Any], side: str) -> Tuple[bool, str]:
    """Would the gate permit a CE or PE entry right now?

    FAILS OPEN by design: if the board cannot form a consensus, trading proceeds.
    A local service hiccup must never silently halt the desk - but the UI must
    always show why the gate is open.
    """
    gate = str(((board or {}).get("consensus") or {}).get("gate", "ALLOW_ALL"))
    side = (side or "").upper()
    if gate == "CE_ONLY" and side == "PE":
        return False, "blocked: confluence is bullish (CE only)"
    if gate == "PE_ONLY" and side == "CE":
        return False, "blocked: confluence is bearish (PE only)"
    return True, "allowed ({})".format(gate)


# ─────────────────────────────────────────────────────────────────────────────
# SCOREBOARD — the thing that lets us score 40 engines in a few months
# ─────────────────────────────────────────────────────────────────────────────

SIGNAL_LOG_DDL = """
CREATE TABLE IF NOT EXISTS signal_log (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  ts        INTEGER NOT NULL,
  engine    TEXT    NOT NULL,
  category  TEXT,
  verdict   TEXT,
  bias      INTEGER,
  status    TEXT,
  age_s     REAL,
  evidence  TEXT,
  counted   INTEGER,
  spot      REAL,
  detail    TEXT,
  UNIQUE(ts, engine) ON CONFLICT REPLACE);
CREATE INDEX IF NOT EXISTS ix_signal_log_ts ON signal_log(ts);
CREATE INDEX IF NOT EXISTS ix_signal_log_engine ON signal_log(engine, ts);

CREATE TABLE IF NOT EXISTS consensus_log (
  ts        INTEGER PRIMARY KEY,
  state     TEXT,
  gate      TEXT,
  net_bias  INTEGER,
  counted   INTEGER,
  spot      REAL);
"""


def ensure_tables(db_path: str) -> None:
    """Creates signal_log / consensus_log if absent. Safe to call repeatedly."""
    with sqlite3.connect(db_path, timeout=10) as conn:
        conn.executescript(SIGNAL_LOG_DDL)


def log_board(db_path: str, board: Dict[str, Any], spot: Optional[float] = None) -> int:
    """Appends one row per engine plus one consensus row. Returns rows written.

    Best-effort: a logging failure must never break the desk, so it is caught and
    reported rather than raised.
    """
    rows = (board or {}).get("rows") or []
    if not rows:
        return 0
    ts = int(board.get("generated_ts") or time.time())
    try:
        ensure_tables(db_path)
        with sqlite3.connect(db_path, timeout=10) as conn:
            conn.executemany(
                "INSERT INTO signal_log (ts, engine, category, verdict, bias, status, "
                "age_s, evidence, counted, spot, detail) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                [(ts, r.get("key"), r.get("category"), r.get("verdict"), r.get("bias"),
                  r.get("status"), r.get("age_s"), r.get("evidence"),
                  1 if r.get("counted") else 0, spot, (r.get("detail") or "")[:220])
                 for r in rows])
            c = board.get("consensus") or {}
            conn.execute(
                "INSERT OR REPLACE INTO consensus_log (ts, state, gate, net_bias, counted, spot) "
                "VALUES (?,?,?,?,?,?)",
                (ts, c.get("state"), c.get("gate"), c.get("net_bias"), c.get("counted"), spot))
        return len(rows)
    except sqlite3.Error as exc:
        log.warning("confluence: signal_log write failed: %s", exc)
        return 0


def render_table(board: Dict[str, Any]) -> str:
    """Plain-text board, for the console and for pasting into a chat."""
    rows = (board or {}).get("rows") or []
    if not rows:
        return "(no engines registered)"
    out = ["{:<26} {:<12} {:<16} {:>8}  {:<11} {}".format(
        "ENGINE", "CATEGORY", "VERDICT", "AGE", "STATUS", "EVIDENCE")]
    out.append("-" * 96)
    for r in sorted(rows, key=lambda x: (x["category"], x["label"])):
        if r["age_s"] is None:
            age = "--"
        else:
            # "~" marks fetch-basis age: when we asked, not how old the data is.
            age = "{}{:.0f}s".format("~" if r.get("age_basis") == "fetch" else "", r["age_s"])
        mark = "*" if r["counted"] else " "
        out.append("{}{:<25} {:<12} {:<16} {:>8}  {:<11} {}".format(
            mark, str(r["label"])[:25], r["category"][:12], r["verdict"][:16],
            age, r["status"][:11], r["evidence"]))
    c = (board or {}).get("consensus") or {}
    out.append("-" * 96)
    out.append("CONSENSUS: {}  ->  GATE: {}   (net {:+d}, {} of {} counted)  {}".format(
        c.get("state"), c.get("gate"), c.get("net_bias", 0),
        c.get("counted", 0), c.get("total", 0), c.get("reason", "")))
    h = (board or {}).get("health") or {}
    out.append("* = counted in consensus (eligible, fresh, directional)   "
               "~age = engine publishes no timestamp; shows when we called it")
    out.append("health: {} fresh · {} stale · {} unavailable · {} fetch-aged · {} refuted".format(
        h.get("fresh", 0), h.get("stale", 0), h.get("unavailable", 0),
        h.get("fetch_aged", 0), h.get("refuted_shown", 0)))
    return "\n".join(out)
