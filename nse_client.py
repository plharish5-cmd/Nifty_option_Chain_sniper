"""
NSE option chain fetcher.

Two things this handles that the naive version does not:

1. NSE moved the option chain to an `option-chain-v3` endpoint, which takes an
   explicit expiry and gets its expiry list from a separate contract-info call.
   The old `option-chain-indices` URL may or may not still answer. Rather than
   betting on one, this client tries each style in order and remembers the
   first that works, so it keeps running when NSE moves things again.

2. Errors report what actually went wrong. A bare "failed after 3 attempts"
   tells you nothing — status code, URL and body excerpt tell you everything.

Still true regardless of the above: NSE's WAF blocks datacenter IP ranges, so
this must run from a residential connection. Run `python diagnose.py` if
anything here fails.
"""
from __future__ import annotations

import logging
import time
from typing import Any

import requests

import config

log = logging.getLogger(__name__)


class NSEError(RuntimeError):
    """Carries the real cause, not a retry count."""


class NSEClient:
    #: tried in order; the first that answers is cached for the session
    STYLES = ("v3", "legacy")

    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": config.USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate",   # NOT br: needs the optional
                                                 # brotli package, absent in a
                                                 # clean venv -> undecodable body
            "Connection": "keep-alive",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        })
        self._booted = False
        self._last_request = 0.0
        self._style: str | None = getattr(config, "API_STYLE", None) or None

    # ── plumbing ─────────────────────────────────────────────────────────────
    def _throttle(self) -> None:
        gap = time.time() - self._last_request
        if gap < config.MIN_REQUEST_GAP:
            time.sleep(config.MIN_REQUEST_GAP - gap)
        self._last_request = time.time()

    def _bootstrap(self) -> None:
        """
        The cookies that matter (_abck, bm_sz, nsit) come from the
        option-chain page. The homepage is attempted first because it
        sometimes seeds additional Akamai cookies — but NSE's WAF returns 403
        to non-browser requests on `/` while still serving `/option-chain`
        normally, so a homepage failure must NOT abort the bootstrap.
        Only the option-chain page is required.
        """
        self.session.cookies.clear()

        self._throttle()
        try:
            r = self.session.get(config.BASE_URL, timeout=config.REQUEST_TIMEOUT)
            if r.status_code != 200:
                log.info("homepage returned %s — continuing, it is optional",
                         r.status_code)
        except Exception as exc:                               # noqa: BLE001
            log.info("homepage unreachable (%s) — continuing, it is optional", exc)

        self._throttle()
        r = self.session.get(config.CHAIN_PAGE, timeout=config.REQUEST_TIMEOUT)
        if r.status_code != 200:
            raise NSEError(f"option-chain page returned {r.status_code} — "
                           f"this one is required for cookies")
        if not len(self.session.cookies):
            raise NSEError("option-chain page set no cookies; the API will reject us")

        self.session.headers["Referer"] = config.CHAIN_PAGE
        self._booted = True
        log.info("bootstrapped: %d cookies", len(self.session.cookies))

    def _get(self, url: str, params: dict) -> requests.Response:
        self._throttle()
        return self.session.get(url, params=params, timeout=config.REQUEST_TIMEOUT)

    @staticmethod
    def _json(r: requests.Response, what: str) -> dict:
        """
        Parse JSON, and when it fails say WHY rather than surfacing a bare
        'Expecting value: line 1 column 1'. A 200 that will not parse is
        almost always one of: an HTML interstitial from the WAF, an empty
        body, or a content-encoding the client cannot decode.
        """
        try:
            return r.json()
        except ValueError:
            enc = r.headers.get("Content-Encoding", "none")
            ctype = r.headers.get("Content-Type", "?")
            body = r.text[:120] if r.text else "(empty body)"
            hint = ""
            if enc and enc != "none" and enc not in ("gzip", "deflate"):
                hint = (f" Body is '{enc}'-encoded, which this client cannot "
                        f"decode — install the matching package or drop it "
                        f"from Accept-Encoding in nse_client.py.")
            raise NSEError(
                f"{what} returned HTTP {r.status_code} but the body is not "
                f"JSON. content-type={ctype} content-encoding={enc} "
                f"len={len(r.content)}. Starts: {body!r}.{hint}"
            )

    # ── endpoint styles ──────────────────────────────────────────────────────
    def _fetch_v3(self, symbol: str) -> dict[str, Any]:
        info = self._get(f"{config.BASE_URL}/api/option-chain-contract-info",
                         {"symbol": symbol})
        if info.status_code != 200:
            raise NSEError(f"contract-info returned {info.status_code}: "
                           f"{info.text[:140]!r}")
        meta = self._json(info, "contract-info")
        expiries = (meta.get("expiryDates")
                    or meta.get("records", {}).get("expiryDates"))
        if not expiries:
            raise NSEError(f"contract-info gave no expiryDates; keys={list(meta)[:6]}")

        expiry = expiries[config.EXPIRY_INDEX]

        # v3 returns an empty body without an expiry, and NSE has used both
        # spellings for the parameter. Try each and keep the one with rows.
        attempts: list[str] = []
        for key in ("expiry", "expiryDate"):
            r = self._get(f"{config.BASE_URL}/api/option-chain-v3",
                          {"type": "Indices", "symbol": symbol, key: expiry})
            if r.status_code != 200:
                attempts.append(f"{key}= -> HTTP {r.status_code}")
                continue
            try:
                payload = self._json(r, f"option-chain-v3 ({key}=)")
            except NSEError as exc:
                attempts.append(str(exc))
                continue
            rows = (payload.get("records", {}).get("data")
                    or payload.get("filtered", {}).get("data") or [])
            if rows:
                payload.setdefault("records", {}).setdefault("expiryDates", expiries)
                return payload
            attempts.append(f"{key}= -> 200 but empty (keys={list(payload)[:4]})")

        raise NSEError(f"option-chain-v3 gave no rows for expiry {expiry}. "
                       + " | ".join(attempts))

    def _fetch_legacy(self, symbol: str) -> dict[str, Any]:
        r = self._get(f"{config.BASE_URL}/api/option-chain-indices", {"symbol": symbol})
        if r.status_code != 200:
            raise NSEError(f"option-chain-indices returned {r.status_code}: "
                           f"{r.text[:140]!r}")
        payload = self._json(r, "option-chain-indices")
        if "records" not in payload:
            raise NSEError(f"unexpected payload; keys={list(payload)[:6]}")
        return payload

    # ── public ───────────────────────────────────────────────────────────────
    def fetch_raw(self, symbol: str | None = None) -> dict[str, Any]:
        symbol = symbol or config.SYMBOL
        styles = [self._style] if self._style else list(self.STYLES)
        errors: list[str] = []

        for attempt in range(config.MAX_RETRIES):
            if not self._booted:
                try:
                    self._bootstrap()
                except Exception as exc:                       # noqa: BLE001
                    errors.append(f"bootstrap: {exc}")
                    if attempt < config.MAX_RETRIES - 1:
                        time.sleep(config.RETRY_BACKOFF * (2 ** attempt))
                    continue

            for style in styles:
                try:
                    fn = self._fetch_v3 if style == "v3" else self._fetch_legacy
                    payload = fn(symbol)
                    if self._style != style:
                        log.info("using '%s' endpoint style", style)
                        self._style = style
                    return payload
                except Exception as exc:                       # noqa: BLE001
                    errors.append(f"{style}: {exc}")
                    if "401" in str(exc) or "403" in str(exc):
                        self._booted = False                   # cookies stale

            if attempt < config.MAX_RETRIES - 1:
                time.sleep(config.RETRY_BACKOFF * (2 ** attempt))

        raise NSEError(
            "Could not fetch the option chain. What was tried:\n  - "
            + "\n  - ".join(errors[-6:])
            + "\n\nRun `python diagnose.py` for a full report."
        )

    # ── accessors (tolerant of both payload shapes) ─────────────────────────
    @staticmethod
    def expiries(payload: dict[str, Any]) -> list[str]:
        return list(payload.get("records", {}).get("expiryDates")
                    or payload.get("expiryDates") or [])

    @staticmethod
    def spot(payload: dict[str, Any]) -> float:
        rec = payload.get("records", {})
        for src in (rec.get("underlyingValue"),
                    payload.get("underlyingValue"),
                    payload.get("filtered", {}).get("underlyingValue")):
            if src:
                return float(src)
        raise NSEError("no underlyingValue in payload — cannot determine spot")

    @staticmethod
    def timestamp(payload: dict[str, Any]) -> str:
        return (payload.get("records", {}).get("timestamp")
                or payload.get("timestamp") or "")
