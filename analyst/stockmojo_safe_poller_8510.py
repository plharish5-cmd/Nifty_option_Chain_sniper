"""StockMojo compact-array poller — JSON archive + PE/CE analytics (no order routing)."""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import random
import re
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import requests

IST = ZoneInfo("Asia/Kolkata")
ROOT = Path(__file__).resolve().parent.parent
RULES = Path(__file__).with_name("stockmojo_download_rules_8510.json")
AUTH_J = "STOCKMOJO_JWT_J"
AUTH_T = "STOCKMOJO_TOKEN_T"
REQ_JSON = "STOCKMOJO_REQUEST_JSON"
ARRAYS = ("co", "po", "cv", "pv", "f", "fo", "cc", "pc")


def market_open(ts: float, rules: Dict[str, Any], holidays: Optional[List[str]] = None) -> bool:
    now = datetime.fromtimestamp(ts, IST)
    day = now.strftime("%Y-%m-%d")
    if holidays and day in holidays:
        return False
    if now.weekday() not in rules.get("weekdays", (0, 1, 2, 3, 4)):
        return False
    hm = now.strftime("%H:%M")
    return rules.get("session_start", "09:15") <= hm <= rules.get("session_end", "15:30")


def next_delay(
    rules: Dict[str, Any],
    failures: int = 0,
    retry_after: Optional[str] = None,
    now: Optional[float] = None,
    rng: Optional[random.Random] = None,
) -> float:
    rng = rng or random.SystemRandom()
    if failures:
        base = min(
            float(rules.get("backoff_max_seconds", 1800)),
            float(rules.get("backoff_initial_seconds", 180)) * (2 ** min(failures, 10)),
        )
    else:
        base = rng.uniform(
            float(rules.get("interval_min_seconds", 150)),
            float(rules.get("interval_max_seconds", 180)),
        )
    if retry_after:
        try:
            base = max(base, float(retry_after))
        except (TypeError, ValueError):
            pass
    return base


def _parse_ts(value: Any) -> datetime:
    d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return d.replace(tzinfo=IST) if d.tzinfo is None else d.astimezone(IST)


def normalize_payload(
    payload: Any,
    session_date: Optional[str] = None,
) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        if not session_date:
            raise ValueError("Named export rows require session_date")
        rows: List[Dict[str, Any]] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            po = float(item.get("Put OI Total") or item.get("put_oi_total") or 0)
            co = float(item.get("Call OI Total") or item.get("call_oi_total") or 0)
            rows.append(
                {
                    "time_ist": str(item.get("Time") or ""),
                    "put_oi": po,
                    "call_oi": co,
                    "pe_ce_oi_total": po - co,
                    "pcr_total": (po / co) if co > 0 else None,
                    "source_date_verified": 0,
                }
            )
        return rows

    if not isinstance(payload, dict) or not isinstance(payload.get("t"), list) or not payload["t"]:
        raise ValueError("UNSUPPORTED_RESPONSE_SCHEMA")
    n = len(payload["t"])
    for key in ARRAYS:
        arr = payload.get(key)
        if arr is not None and (not isinstance(arr, list) or len(arr) != n):
            raise KeyError("PARALLEL_ARRAY_LENGTH_MISMATCH")

    rows: List[Dict[str, Any]] = []
    for i in range(n):
        po = float(payload["po"][i])
        co = float(payload["co"][i])
        if not all(math.isfinite(v) for v in (po, co)) or po < 0 or co < 0:
            raise ValueError("INVALID_NUMERIC_ARRAY")
        pe_ce = po - co
        pcr = round(po / co, 4) if co > 0 else None
        ts = _parse_ts(payload["t"][i])
        rows.append(
            {
                "time_ist": ts.strftime("%H:%M:%S"),
                "timestamp_epoch": ts.timestamp(),
                "put_oi": po,
                "call_oi": co,
                "pe_ce_oi_total": pe_ce,
                "pcr_total": pcr,
                "future_ltp": float(payload["f"][i]) if payload.get("f") else None,
            }
        )

    for i in range(1, len(rows)):
        prev, cur = rows[i - 1], rows[i]
        d_pe = cur["put_oi"] - prev["put_oi"]
        d_ce = cur["call_oi"] - prev["call_oi"]
        cur["pe_ce_change_1m"] = (cur["pe_ce_oi_total"] - prev["pe_ce_oi_total"])
        cur["put_change_1m"] = d_pe
        cur["call_change_1m"] = d_ce

    for i, row in enumerate(rows):
        if i >= 3:
            row["pe_ce_change_3m"] = row["pe_ce_oi_total"] - rows[i - 3]["pe_ce_oi_total"]
            row["pe_ce_slope_3m"] = row["pe_ce_change_3m"] / 3.0
        else:
            row["pe_ce_change_3m"] = None
            row["pe_ce_slope_3m"] = None
        if i == 0:
            row["pe_ce_change_1m"] = row.get("pe_ce_change_1m")
        if row.get("pe_ce_change_1m") is None and i > 0:
            row["pe_ce_change_1m"] = row["pe_ce_oi_total"] - rows[0]["pe_ce_oi_total"]
        slope = row.get("pe_ce_slope_3m")
        if slope is not None and slope > 0:
            row["sentiment"] = "PE_CE_RISING"
        elif slope is not None and slope < 0:
            row["sentiment"] = "PE_CE_FALLING"
        else:
            row["sentiment"] = "MIXED"
    return rows


def analyze_writer_momentum(rows: List[Dict[str, Any]], cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    cfg = cfg or {}
    if len(rows) < 2:
        return {
            "available": False,
            "reason": "Insufficient StockMojo history",
        }
    latest = rows[-1]
    prev = rows[-2]
    put_i = float(latest.get("put_change_1m") or 0) / 1e5
    call_i = float(latest.get("call_change_1m") or 0) / 1e5
    pe_ce_3m = float(latest.get("pe_ce_change_3m") or 0) / 1e5
    pe_ce_1m = float(latest.get("pe_ce_change_1m") or 0) / 1e5

    surge_put_l = float(cfg.get("surge_put_interval_lakhs", 20))
    surge_call_l = float(cfg.get("surge_call_interval_lakhs", 20))

    significant_put_surge_change = put_i >= surge_put_l and put_i > call_i
    significant_call_surge_change = call_i >= surge_call_l and call_i > put_i

    put_total_l = float(latest.get("put_oi") or 0) / 1e5
    call_total_l = float(latest.get("call_oi") or 0) / 1e5
    pe_ce_total_l = float(latest.get("pe_ce_oi_total") or 0) / 1e5
    total_thresh = float(cfg.get("surge_total_pe_ce_lakhs", 30))
    significant_put_surge_total = (
        put_total_l > call_total_l and pe_ce_total_l >= total_thresh
    )
    significant_call_surge_total = (
        call_total_l > put_total_l and pe_ce_total_l <= -total_thresh
    )

    significant_put_surge = significant_put_surge_change or significant_put_surge_total
    significant_call_surge = significant_call_surge_change or significant_call_surge_total

    prev_put_i = float(prev.get("put_change_1m") or 0) / 1e5
    prev_call_i = float(prev.get("call_change_1m") or 0) / 1e5
    writer_cross = (prev_put_i - prev_call_i) * (put_i - call_i) < 0
    divergence = writer_cross or (
        latest.get("sentiment") == "PE_CE_FALLING" and prev.get("sentiment") == "PE_CE_RISING"
    ) or (
        latest.get("sentiment") == "PE_CE_RISING" and prev.get("sentiment") == "PE_CE_FALLING"
    )

    pe_ce_total_l = float(latest.get("pe_ce_oi_total") or 0) / 1e5
    if significant_put_surge or pe_ce_total_l > 0:
        synergy = "BULLISH_SYNERGY"
        bias = "BULLISH"
    elif significant_call_surge or pe_ce_total_l < 0:
        synergy = "BEARISH_SYNERGY"
        bias = "BEARISH"
    else:
        synergy = "MIXED"
        bias = "MIXED"

    return {
        "available": True,
        "source": "STOCKMOJO_API",
        "bias": bias,
        "synergy_status": synergy,
        "put_interval_lakhs": round(put_i, 2),
        "call_interval_lakhs": round(call_i, 2),
        "pe_ce_change_1m_lakhs": round(pe_ce_1m, 2),
        "pe_ce_change_3m_lakhs": round(pe_ce_3m, 2),
        "significant_put_surge": significant_put_surge,
        "significant_call_surge": significant_call_surge,
        "significant_put_surge_change": significant_put_surge_change,
        "significant_call_surge_change": significant_call_surge_change,
        "significant_put_surge_total": significant_put_surge_total,
        "significant_call_surge_total": significant_call_surge_total,
        "put_oi_total_lakhs": round(put_total_l, 2),
        "call_oi_total_lakhs": round(call_total_l, 2),
        "writer_divergence": divergence,
        "atm_reversal_hint": (
            "UPSIDE_REVERSAL" if divergence and pe_ce_1m > 0
            else "DOWNSIDE_REVERSAL" if divergence and pe_ce_1m < 0
            else "NONE"
        ),
        "total_oi_bias": "BULLISH" if pe_ce_total_l > 0 else "BEARISH" if pe_ce_total_l < 0 else "MIXED",
        "change_oi_bias": bias,
        "timestamp_epoch": latest.get("timestamp_epoch"),
        "time_ist": latest.get("time_ist"),
        "pcr_total": latest.get("pcr_total"),
        "pe_ce_oi_total_lakhs": round(pe_ce_total_l, 2),
    }


def _redact(msg: str, secrets: Tuple[str, ...]) -> str:
    out = msg
    for s in secrets:
        if s and len(s) > 4:
            out = out.replace(s, "***")
    return out


class StockMojoPoller:
    def __init__(
        self,
        ledger_db: Path,
        rules_path: Path,
        error_log: Path,
        environ: Optional[Dict[str, str]] = None,
        transport: Optional[Callable] = None,
        clock: Optional[Callable[[], float]] = None,
    ):
        self.ledger_db = Path(ledger_db)
        self.ledger_db.parent.mkdir(parents=True, exist_ok=True)
        self.rules = json.loads(Path(rules_path).read_text())
        self.error_log = Path(error_log)
        self.environ = dict(os.environ)
        if environ:
            self.environ.update(environ)
        self.transport = transport
        self.clock = clock or time.time
        self.lock = threading.RLock()
        self.failures = 0
        self.halted = False
        self._memory_rows: List[Dict[str, Any]] = []
        self._snapshot: Dict[str, Any] = {"available": False, "source": "STOCKMOJO_API", "configured": False}
        self.logger = logging.getLogger("stockmojo_poller")
        if not self.logger.handlers:
            h = logging.FileHandler(self.error_log, encoding="utf-8")
            h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            self.logger.addHandler(h)
        self._init_db()

    def connect(self):
        return sqlite3.connect(self.ledger_db, timeout=10)

    def _init_db(self) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS stockmojo_pe_ce_live (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    time_ist TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    UNIQUE(time_ist) ON CONFLICT REPLACE
                )
                """
            )

    def request_parts(self) -> Tuple[str, str, Dict[str, Any]]:
        j = self.environ.get(AUTH_J, "").strip()
        t = self.environ.get(AUTH_T, "").strip()
        raw = self.environ.get(REQ_JSON, "").strip()
        if not j or not t:
            raise ValueError("MISSING_CREDENTIALS")
        if not raw:
            raise ValueError("REQUEST_BODY_REQUIRED")
        body = json.loads(raw)
        if not isinstance(body, dict):
            raise ValueError("REQUEST_BODY_INVALID")
        if set(body) != {"symbol", "expiry", "range"}:
            raise ValueError("REQUEST_SCOPE_INVALID")
        if any(k in body for k in ("j", "t", "J", "T")):
            raise ValueError("SECRETS_IN_BODY")
        for key in ("symbol", "expiry", "range"):
            val = body[key]
            if type(val) is not int or val <= 0:
                raise ValueError("REQUEST_SCOPE_INVALID")
        return j, t, body

    def _archive_json(self, payload: Dict[str, Any], received: datetime) -> Path:
        out_root = ROOT / self.rules.get("output_relative", "data/stockmojo_downloads")
        day = out_root / received.strftime("%Y-%m-%d")
        day.mkdir(parents=True, exist_ok=True)
        name = received.strftime("%Y%m%d_%H%M%S_%f") + "_" + uuid.uuid4().hex[:8] + ".json"
        path = day / name
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def _persist_rows(self, rows: List[Dict[str, Any]], now: float) -> None:
        with self.connect() as conn:
            for row in rows:
                conn.execute(
                    "INSERT OR REPLACE INTO stockmojo_pe_ce_live(time_ist,payload,created_at) VALUES(?,?,?)",
                    (row.get("time_ist", ""), json.dumps(row, allow_nan=False), now),
                )

    def poll_once(self, now: Optional[float] = None) -> float:
        now = self.clock() if now is None else float(now)
        with self.lock:
            if self.halted:
                return next_delay(self.rules, self.failures)
            if self.rules.get("market_hours_only", True) and not market_open(now, self.rules):
                self._snapshot.update(
                    available=False,
                    poll_status="MARKET_CLOSED",
                    configuration_status="OK",
                )
                return next_delay(self.rules, 0)

            try:
                j, t, body = self.request_parts()
            except ValueError as exc:
                code = str(exc)
                self._snapshot.update(
                    available=False,
                    configured=code != "MISSING_CREDENTIALS",
                    configuration_status=code,
                    poll_status=code,
                )
                return next_delay(self.rules, 0)

            endpoint = self.rules["endpoint"]
            headers = {
                "Origin": self.rules.get("origin", "https://stockmojo.in"),
                "Referer": self.rules.get("origin", "https://stockmojo.in") + "/",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "j": j,
                "t": t,
            }
            try:
                if self.transport:
                    response = self.transport(endpoint, headers=headers, json=body, allow_redirects=False)
                    status = getattr(response, "status_code", 0)
                    if status in (401, 403):
                        self.halted = True
                        self._snapshot.update(
                            available=False,
                            halted=True,
                            poll_status=f"ACCESS_DENIED_{status}",
                        )
                        return next_delay(self.rules, self.failures)
                    if status != 200:
                        self.failures += 1
                        ra = getattr(response, "headers", {}).get("Retry-After")
                        return next_delay(self.rules, self.failures, ra, now)
                    payload = response.json()
                else:
                    with requests.Session() as session:
                        session.trust_env = False
                        response = session.post(
                            endpoint,
                            headers=headers,
                            json=body,
                            timeout=float(self.rules.get("request_timeout_seconds", 20)),
                            allow_redirects=False,
                        )
                        if response.status_code in (401, 403):
                            self.halted = True
                            self._snapshot.update(
                                available=False,
                                halted=True,
                                poll_status=f"ACCESS_DENIED_{response.status_code}",
                            )
                            return next_delay(self.rules, self.failures)
                        if response.status_code != 200:
                            self.failures += 1
                            return next_delay(
                                self.rules,
                                self.failures,
                                response.headers.get("Retry-After"),
                                now,
                            )
                        payload = response.json()

                rows = normalize_payload(payload)
                received = datetime.fromtimestamp(now, IST)
                session_day = received.date().isoformat()
                valid_rows = [
                    r
                    for r in rows
                    if r.get("timestamp_epoch")
                    and datetime.fromtimestamp(r["timestamp_epoch"], IST).date().isoformat() == session_day
                ]
                if not valid_rows:
                    self._snapshot.update(available=False, poll_status="STALE_SESSION", configured=True)
                    return next_delay(self.rules, self.failures)

                self._persist_rows(valid_rows, now)
                self._memory_rows = valid_rows
                archive = self._archive_json(payload, received)
                latest = valid_rows[-1]
                momentum = analyze_writer_momentum(valid_rows)
                self.failures = 0
                self._snapshot = {
                    **momentum,
                    "configured": True,
                    "configuration_status": "OK",
                    "poll_status": "OK",
                    "halted": False,
                    "pe_ce_oi_total": latest.get("pe_ce_oi_total"),
                    "pcr_total": latest.get("pcr_total"),
                    "pe_ce_change_1m": latest.get("pe_ce_change_1m"),
                    "pe_ce_change_3m": latest.get("pe_ce_change_3m"),
                    "last_archive": str(archive),
                    "row_count": len(valid_rows),
                }
                return next_delay(self.rules, 0)
            except Exception as exc:
                self.failures += 1
                self.logger.error(_redact(str(exc), (self.environ.get(AUTH_J, ""), self.environ.get(AUTH_T, ""))))
                self._snapshot.update(available=False, poll_status=type(exc).__name__)
                return next_delay(self.rules, self.failures)

    def snapshot(self, now: Optional[float] = None) -> Dict[str, Any]:
        now = self.clock() if now is None else float(now)
        with self.lock:
            snap = dict(self._snapshot)
            ts = snap.get("timestamp_epoch")
            max_age = float(self.rules.get("freshness_max_seconds", 180))
            if ts is not None:
                snap["age_seconds"] = max(0.0, now - float(ts))
                if now - float(ts) > max_age:
                    snap["available"] = False
                    snap["reason"] = "StockMojo observation stale"
            return snap


def poller_from_config(cfg: Optional[Dict[str, Any]] = None) -> StockMojoPoller:
    cfg = cfg or {}
    ledger = ROOT / "data" / "terminal8510" / "ledger.sqlite3"
    err = ROOT / "data" / "stockmojo_downloads" / "poller_errors.log"
    err.parent.mkdir(parents=True, exist_ok=True)
    rules_path = ROOT / "data" / "stockmojo_downloads" / "poller_rules_runtime.json"
    rules = json.loads(RULES.read_text())
    rules["interval_min_seconds"] = float(cfg.get("poll_min_seconds", rules.get("interval_min_seconds", 120)))
    rules["interval_max_seconds"] = float(cfg.get("poll_max_seconds", rules.get("interval_max_seconds", 180)))
    rules_path.parent.mkdir(parents=True, exist_ok=True)
    rules_path.write_text(json.dumps(rules, indent=2), encoding="utf-8")
    environ: Dict[str, str] = {}
    if cfg.get("request_json"):
        environ[REQ_JSON] = json.dumps(cfg["request_json"])
    else:
        environ[REQ_JSON] = json.dumps(rules.get("payload", {}))
    return StockMojoPoller(ledger, rules_path, err, environ=environ)
