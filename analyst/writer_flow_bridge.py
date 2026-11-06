"""
writer_flow_bridge.py — call/put writer flow for the 8510 desk, sourced from 8600.

WHY
---
Stock Mojo is retired: its feed returns 401 Unauthorized, the JWT expires every
few days and has to be recaptured by hand, and the whole dependency sat on a
third party's session cookie. The 8600 ICT-SMC desk already computes the same
numbers from OUR OWN Dhan option chain, and its /api/pe_ce_matrix endpoint was
built as a "StockMojo 12-Column Mathematical Parity" endpoint precisely so it
could stand in for the scraper.

So this does not add a new signal. It repoints an existing panel at a source
that works, that we own, and that cannot expire.

CONTRACT
--------
get_snapshot() returns EXACTLY the keys StockMojoScraper.get_snapshot()
returned, so the 8510 telemetry and the 128 UI references keep working with no
changes:

    pe_minus_ce_day, pe_minus_ce_interval, call_oi_change_day,
    put_oi_change_day, total_call_oi, total_put_oi, writer_bias,
    cw_vs_pw_ratio, pcr, tag, badge, signal_hint, writer_signal,
    last_sync_time, futures_volume_surge, is_surge

FAILS CLOSED, ALWAYS
--------------------
When 8600 is not running, the request times out, or the payload is malformed,
this returns available=False with zeros and writer_bias "NO DATA". It NEVER
invents a bias. That matters: the trend watchlist in this same codebase shipped
with three indicators hardcoded to BULLISH that nothing ever overwrote, so the
desk could not report a bearish regime at all. A bridge that guessed on failure
would reintroduce exactly that class of bug.

Direction convention
--------------------
PE - CE OI change > 0 means put writers are adding faster than call writers.
Put writing is support, so that reads BULLISH. The reverse reads BEARISH. This
is the same convention pe_ce_matrix.py uses ("Put writers aggressively adding
... Squeeze ahead!").

Python 3.9 compatible: no PEP 604 unions.
"""
from __future__ import annotations

import logging
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

log = logging.getLogger("desk.writer_flow")

DEFAULT_BASE_URL = "http://127.0.0.1:8600"
DEFAULT_PATH = "/api/pe_ce_matrix"
DEFAULT_TIMEOUT = 4.0
DEFAULT_CACHE_SECONDS = 20

# The 12 parity columns, as 8600 labels them.
COL_TIME = "Time"
COL_PUT_DAY = "Put OI Change (Day)"
COL_PUT_STEP = "Put OI Change"
COL_CALL_DAY = "Call OI Change (Day)"
COL_CALL_STEP = "Call OI Change"
COL_PECE_DAY = "PE-CE OI Change (Day)"
COL_PECE_STEP = "PE-CE OI Change"
COL_PCR = "PCR Total"


def _num(val: Any, default: float = 0.0) -> float:
    try:
        if val is None:
            return default
        return float(val)
    except (TypeError, ValueError):
        return default


def empty_snapshot(reason: str) -> Dict[str, Any]:
    """The honest no-data shape. Same keys, no invented bias."""
    return {
        "available": False,
        "source": "8600_ICT_SMC_DESK",
        "reason": reason,
        "pe_minus_ce_day": 0.0,
        "pe_minus_ce_interval": 0.0,
        "call_oi_change_day": 0.0,
        "put_oi_change_day": 0.0,
        "total_call_oi": 0.0,
        "total_put_oi": 0.0,
        "writer_bias": "NO DATA",
        "writer_signal": "NO DATA",
        "cw_vs_pw_ratio": 0.0,
        "pcr": 0.0,
        "tag": "NO DATA",
        "badge": "side",
        "signal_hint": "8600 writer flow unavailable: {}".format(reason),
        "last_sync_time": "",
        "futures_volume_surge": 0.0,
        "is_surge": False,
    }


def classify_writer_bias(pe_minus_ce_day: float,
                         threshold: float = 0.0) -> Dict[str, str]:
    """PE - CE day change -> writer dominance.

    Threshold is caller-supplied so it lives in config, never baked in here.
    """
    if pe_minus_ce_day > threshold:
        return {"writer_bias": "BULLISH", "writer_signal": "PUT WRITERS DOMINANT",
                "tag": "BULLISH", "badge": "bull"}
    if pe_minus_ce_day < -threshold:
        return {"writer_bias": "BEARISH", "writer_signal": "CALL WRITERS DOMINANT",
                "tag": "BEARISH", "badge": "bear"}
    return {"writer_bias": "NEUTRAL", "writer_signal": "WRITERS BALANCED",
            "tag": "NEUTRAL", "badge": "side"}


def snapshot_from_rows(rows: List[Dict[str, Any]],
                       threshold: float = 0.0) -> Dict[str, Any]:
    """Map 8600's 12-column rows onto the snapshot contract.

    Pure: no network, no clock beyond the row's own label, so it is testable
    with a literal list of rows.
    """
    if not rows:
        return empty_snapshot("8600 returned no rows")

    # 8600 orders newest-first.
    latest = rows[0] or {}
    put_day = _num(latest.get(COL_PUT_DAY))
    call_day = _num(latest.get(COL_CALL_DAY))
    pece_day = _num(latest.get(COL_PECE_DAY), put_day - call_day)
    pece_step = _num(latest.get(COL_PECE_STEP),
                     _num(latest.get(COL_PUT_STEP)) - _num(latest.get(COL_CALL_STEP)))
    pcr = _num(latest.get(COL_PCR))

    out = {
        "available": True,
        "source": "8600_ICT_SMC_DESK",
        "reason": "",
        "pe_minus_ce_day": round(pece_day, 2),
        "pe_minus_ce_interval": round(pece_step, 2),
        "call_oi_change_day": round(call_day, 2),
        "put_oi_change_day": round(put_day, 2),
        # 8600's parity rows carry OI CHANGE, not absolute OI. Reporting change
        # as total would be a quiet lie, so these stay 0 with a note until a
        # total-OI source is wired.
        "total_call_oi": 0.0,
        "total_put_oi": 0.0,
        "cw_vs_pw_ratio": round(call_day / put_day, 3) if put_day else 0.0,
        "pcr": round(pcr, 2),
        "last_sync_time": str(latest.get(COL_TIME, "") or ""),
        "n_rows": len(rows),
        "_note": "total_call_oi / total_put_oi are 0: 8600's parity endpoint "
                 "returns OI CHANGE, not absolute OI.",
        # Volume surge is a separate engine; this bridge does not speak for it.
        "futures_volume_surge": 0.0,
        "is_surge": False,
    }
    out.update(classify_writer_bias(pece_day, threshold))
    out["signal_hint"] = "{} | PE-CE day {:+,.0f} | PCR {:.2f} | from 8600".format(
        out["writer_signal"], pece_day, pcr)
    return out


class WriterFlowBridge:
    """Fetches writer flow from the 8600 desk, with a short cache.

    Cached because 8510's telemetry loop runs every few seconds while the option
    chain itself only refreshes about every 3 minutes: polling 8600 harder than
    the data changes would just add latency to every telemetry call.
    """

    def __init__(self, cfg: Optional[Dict[str, Any]] = None) -> None:
        cfg = cfg or {}
        self.base_url = str(cfg.get("base_url", DEFAULT_BASE_URL)).rstrip("/")
        self.path = str(cfg.get("pe_ce_matrix", DEFAULT_PATH))
        self.timeout = float(cfg.get("timeout_seconds", DEFAULT_TIMEOUT))
        self.cache_seconds = float(cfg.get("cache_seconds", DEFAULT_CACHE_SECONDS))
        self.bias_threshold = float(cfg.get("writer_bias_threshold", 0.0))
        self.display_only = bool(cfg.get("display_only", True))
        self._cache: Optional[Dict[str, Any]] = None
        self._cache_ts = 0.0

    @property
    def url(self) -> str:
        return "{}{}".format(self.base_url, self.path)

    def _fetch_rows(self) -> List[Dict[str, Any]]:
        import json
        req = urllib.request.Request(self.url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        if isinstance(payload, dict):
            for key in ("rows", "stockmojo_rows", "data", "table"):
                val = payload.get(key)
                if isinstance(val, list):
                    return val
            return []
        if isinstance(payload, list):
            return payload
        return []

    def get_snapshot(self, force: bool = False) -> Dict[str, Any]:
        """Writer flow in the StockMojoScraper.get_snapshot() shape."""
        now = time.time()
        if not force and self._cache is not None and (now - self._cache_ts) < self.cache_seconds:
            return self._cache

        try:
            rows = self._fetch_rows()
            snap = snapshot_from_rows(rows, self.bias_threshold)
        except urllib.error.HTTPError as exc:
            snap = empty_snapshot("8600 returned HTTP {}".format(exc.code))
        except urllib.error.URLError as exc:
            snap = empty_snapshot("8600 not reachable ({})".format(exc.reason))
        except (ValueError, KeyError, TypeError) as exc:
            snap = empty_snapshot("8600 payload unreadable ({})".format(exc))
        except OSError as exc:
            snap = empty_snapshot("8600 request failed ({})".format(exc))

        snap["url"] = self.url
        snap["display_only"] = self.display_only
        self._cache, self._cache_ts = snap, now
        if not snap.get("available"):
            log.debug("writer flow unavailable: %s", snap.get("reason"))
        return snap

    # ── Compatibility shims ──────────────────────────────────────────────────
    # The 8510 engine calls these on the old scraper object. They exist so the
    # swap does not require touching every call site, and so a stale route
    # cannot silently reach the retired feed.

    def get_pe_ce_table(self, limit: int = 60) -> Dict[str, Any]:
        try:
            rows = self._fetch_rows()
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as exc:
            return {"ok": False, "rows": [], "error": "8600 unavailable ({})".format(exc)}
        return {"ok": True, "rows": rows[: max(1, int(limit))], "source": "8600_ICT_SMC_DESK"}

    def start_background_polling(self) -> None:
        """No daemon. Fetches are on demand and cached."""
        log.info("Writer flow source: 8600 desk at %s (on-demand, %ds cache).",
                 self.url, int(self.cache_seconds))


def get_writer_flow_bridge(cfg: Optional[Dict[str, Any]] = None) -> WriterFlowBridge:
    return WriterFlowBridge(cfg)
