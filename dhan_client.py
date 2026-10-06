"""
DhanHQ v2 client — intraday candles with volume.

CREDENTIALS NEVER LIVE IN CODE. The access token is read from the environment
or from a gitignored .env file beside this one:

    DHAN_ACCESS_TOKEN=eyJ0eXAi...
    DHAN_CLIENT_ID=1112377935

Dhan tokens default to 24-hour validity, so this will stop working every day
until you paste a fresh one into .env. That is a feature, not an annoyance —
a token that expires daily is a token that cannot be quietly abused. Also turn
on Static IP Setting on the Dhan API page so a leaked token is useless from
anywhere but your own connection.

TWO INSTRUMENTS, DELIBERATELY

  NIFTY 50 index  (IDX_I / 13)   candles for EMAs and structure. This is what
                                 your chart shows, so your levels match.
                                 Volume is always zero — an index is a
                                 calculated number, not a traded instrument.

  NIFTY futures   (NSE_FNO)      volume, and therefore VWAP. Real "NIFTY VWAP"
                                 is always futures VWAP; anyone quoting index
                                 VWAP is either using futures underneath or
                                 quoting something meaningless.

Note: I could not test this from my sandbox — it has no route to dhan.co. The
request shapes follow the v2 documentation, but run `python diagnose_dhan.py`
first and it will tell you exactly what came back.
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import requests

import config

log = logging.getLogger(__name__)

BASE = "https://api.dhan.co/v2"
SCRIP_MASTER = "https://images.dhan.co/api-data/api-scrip-master.csv"


class DhanError(RuntimeError):
    pass


_env_last_mtime: float = 0.0


def _load_env(force: bool = False) -> None:
    """Minimal .env reader — avoids adding python-dotenv as a dependency."""
    global _env_last_mtime
    env = Path(__file__).parent / ".env"
    if not env.exists():
        return
    try:
        mtime = env.stat().st_mtime
    except OSError:
        mtime = 0.0
    if not force and mtime == _env_last_mtime and os.environ.get("DHAN_ACCESS_TOKEN"):
        return
    _env_last_mtime = mtime
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ[k.strip()] = v.strip().strip('"').strip("'")


def token() -> str:
    _load_env()
    t = os.environ.get("DHAN_ACCESS_TOKEN", "").strip()
    if not t:
        raise DhanError(
            "No DHAN_ACCESS_TOKEN found.\n"
            "Create a file named .env next to dhan_client.py containing:\n"
            "    DHAN_ACCESS_TOKEN=your_token_here\n"
            "Generate the token at web.dhan.co -> Profile -> DhanHQ Trading "
            "APIs. Never paste it into a source file or a chat."
        )
    return t


def save_token(new_token: str, client_id: str | None = None) -> None:
    """
    Save Dhan access token (and optional client ID) to .env and os.environ.
    Sets file permissions to 0600.
    """
    new = new_token.strip()
    if not new:
        raise DhanError("Cannot save an empty token.")

    env = Path(__file__).parent / ".env"
    lines, seen_token, seen_cid = [], False, False
    if env.exists():
        for ln in env.read_text().splitlines():
            if ln.strip().startswith("DHAN_ACCESS_TOKEN="):
                lines.append(f"DHAN_ACCESS_TOKEN={new}")
                seen_token = True
            elif client_id is not None and client_id.strip() and ln.strip().startswith("DHAN_CLIENT_ID="):
                lines.append(f"DHAN_CLIENT_ID={client_id.strip()}")
                seen_cid = True
            else:
                lines.append(ln)
    if not seen_token:
        lines.append(f"DHAN_ACCESS_TOKEN={new}")
    if client_id is not None and client_id.strip() and not seen_cid:
        lines.append(f"DHAN_CLIENT_ID={client_id.strip()}")

    env.write_text("\n".join(lines) + "\n")
    try:
        env.chmod(0o600)
    except Exception:
        pass

    global _env_last_mtime
    try:
        _env_last_mtime = env.stat().st_mtime
    except OSError:
        pass

    os.environ["DHAN_ACCESS_TOKEN"] = new
    if client_id is not None and client_id.strip():
        os.environ["DHAN_CLIENT_ID"] = client_id.strip()
    log.info("Dhan access token updated successfully.")


def verify_token(test_token: str | None = None, client_id: str | None = None) -> tuple[bool, str]:
    """
    Live-test whether the given (or currently configured) Dhan token is active and working.
    Queries Dhan's intraday candle API for NIFTY index (IDX_I / 13).
    Returns (is_working: bool, message: str).
    """
    if test_token is not None:
        tok = test_token.strip()
        if not tok:
            return False, "Token cannot be empty."
    else:
        _load_env()
        tok = os.environ.get("DHAN_ACCESS_TOKEN", "").strip()
        if not tok:
            return False, "No Dhan access token configured in .env."

    cid = (client_id if client_id is not None else "").strip()
    if not cid:
        _load_env()
        cid = os.environ.get("DHAN_CLIENT_ID", "").strip()

    h = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "access-token": tok,
    }
    if cid:
        h["client-id"] = cid

    end = datetime.now()
    start = end - timedelta(days=1)
    body = {
        "securityId": str(getattr(config, "DHAN_INDEX_SECURITY_ID", "13")),
        "exchangeSegment": "IDX_I",
        "instrument": "INDEX",
        "interval": "1",
        "oi": False,
        "fromDate": start.strftime("%Y-%m-%d %H:%M:%S"),
        "toDate": end.strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        r = requests.post(
            f"{BASE}/charts/intraday",
            json=body,
            headers=h,
            timeout=getattr(config, "REQUEST_TIMEOUT", 10),
        )
        if r.status_code == 200:
            return True, "Access key is working! Dhan Market Data connected successfully."

        raw = r.text[:300]
        try:
            body_json = r.json()
            err_type = body_json.get("errorType", "")
            err_desc = body_json.get("errorMessage", "")
            err_code = body_json.get("errorCode", "")
        except Exception:
            err_type, err_desc, err_code = "", raw, ""

        combined = f"{err_type} {err_desc} {raw}".lower()
        if "invalid_authentication" in combined or "expired" in combined or err_code == "DH-901":
            return False, f"Token expired or invalid ({err_code or 'DH-901'}). Generate a fresh token from web.dhan.co -> Profile -> DhanHQ Trading APIs."
        elif "not subscribed" in combined or "entitle" in combined or "subscription" in combined:
            return False, "Token authenticated, but account is NOT subscribed to Data API (Rs499/mo required for live candles & VWAP)."
        else:
            return False, f"Dhan error ({r.status_code}): {err_desc or raw}"
    except requests.exceptions.Timeout:
        return False, "Connection timed out while reaching api.dhan.co."
    except requests.exceptions.ConnectionError:
        return False, "Network connection error — check internet connectivity."
    except Exception as exc:
        return False, f"Verification error: {exc}"


def token_preview() -> dict:
    """Safe masked info about the currently configured token."""
    _load_env()
    t = os.environ.get("DHAN_ACCESS_TOKEN", "").strip()
    cid = os.environ.get("DHAN_CLIENT_ID", "").strip()
    if not t:
        return {"configured": False, "length": 0, "preview": "None", "client_id": cid}
    masked = t[-6:] if len(t) >= 6 else t
    return {
        "configured": True,
        "length": len(t),
        "preview": f"...{masked}",
        "client_id": cid,
    }


def refresh_token() -> str:
    """
    Mint a fresh 24-hour access token without opening a browser.

    Dhan moved to 24-hour tokens to comply with SEBI's rules on retail algo
    trading, so a token generated by hand on Monday is dead on Tuesday. For
    an app you intend to open every morning at 08:30 that is not a small
    inconvenience — it is a manual step standing between you and the market
    open, on exactly the mornings you are in a hurry.

    READ THIS BEFORE ENABLING IT
    ───────────────────────────
    This needs your trading PIN and your TOTP secret in .env. Together those
    are your second factor and your PIN: anyone who reads that file can mint
    tokens against your account for as long as the secret is valid. A second
    factor kept in the same file as the thing it is meant to protect is not
    really a second factor.

    That is a real trade-off and it is yours to make, not mine. If you enable
    it, `chmod 600 .env`, keep it out of git — it already is — and understand
    that the convenience costs you the separation between the two factors.
    Leaving it off and pasting a fresh token each morning is the safer
    choice and perfectly workable.
    """
    _load_env()
    cid = os.environ.get("DHAN_CLIENT_ID", "").strip()
    pin = os.environ.get("DHAN_PIN", "").strip()
    secret = os.environ.get("DHAN_TOTP_SECRET", "").strip()
    if not (cid and pin and secret):
        raise DhanError(
            "Automatic refresh needs DHAN_CLIENT_ID, DHAN_PIN and "
            "DHAN_TOTP_SECRET in .env, and TOTP enabled on the account.\n"
            "Read the security note in dhan_client.refresh_token before "
            "adding them — it puts your PIN and second factor in one file.")
    try:
        import pyotp
    except ImportError:
        raise DhanError("pip install pyotp") from None

    code = pyotp.TOTP(secret).now()
    r = requests.post(
        "https://auth.dhan.co/app/generateAccessToken",
        params={"dhanClientId": cid, "pin": pin, "totp": code}, timeout=15)
    if r.status_code != 200:
        raise DhanError(
            f"token refresh returned HTTP {r.status_code}: {r.text[:200]!r}. "
            f"A 4xx here usually means TOTP is not enabled on the account, "
            f"or the PIN is wrong. Neither is worth retrying automatically.")
    body = r.json()
    new = body.get("accessToken")
    if not new:
        raise DhanError(f"no accessToken in response: {list(body)[:8]}")

    save_token(new)
    log.info("access token refreshed, expires %s", body.get("expiryTime"))
    return new


def _headers() -> dict[str, str]:
    _load_env()
    cid = os.environ.get("DHAN_CLIENT_ID", "").strip()
    h = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "access-token": token(),
    }
    if cid:
        h["client-id"] = cid
    return h


def _post(path: str, body: dict) -> dict:
    r = requests.post(f"{BASE}{path}", json=body, headers=_headers(),
                      timeout=config.REQUEST_TIMEOUT)
    if r.status_code in (401, 403):
        # Dhan puts the real reason in the body as an error code, and the
        # reasons are not interchangeable: an expired token and an
        # unsubscribed Data API both come back 401, and only one of them is
        # fixed by generating a new token. Discarding the body here meant
        # every failure was reported as expiry, which sent you round the
        # regenerate-and-retry loop for a problem that regenerating cannot
        # solve.
        body = r.text[:300]
        # Read the errorType, not the errorCode.
        #
        # DH-901 covers both "token expired" and "not entitled", so keying
        # the hint on the code sent you to the subscription page for a
        # problem a new token fixes in thirty seconds. The errorType field
        # is the one that separates them, and it is right there in the body.
        low = body.lower()
        if "invalid_authentication" in low or "expired" in low:
            hint = ("The TOKEN is invalid or expired. Dhan tokens last 24 "
                    "hours — this is a daily event, not a subscription "
                    "problem. Generate a new one at web.dhan.co -> Profile "
                    "-> DhanHQ Trading APIs and update DHAN_ACCESS_TOKEN in "
                    ".env. If these calls worked yesterday, this is all it "
                    "is.")
        elif ("not subscribed" in low or "entitle" in low
              or "subscription" in low):
            hint = ("The token is valid but NOT ENTITLED to data. Historical "
                    "and intraday candles need the Data API subscription "
                    "(Rs499/mo) — web.dhan.co -> Profile -> DhanHQ Trading "
                    "APIs -> Data APIs tab.")
        else:
            hint = ("Either the token has expired (they last 24 hours) or the "
                    "Data API subscription is inactive. The errorType above "
                    "says which — Invalid_Authentication means the token.")
        raise DhanError(
            f"Dhan returned {r.status_code} for {path}.\n"
            f"  response: {body!r}\n"
            f"  {hint}")
    if r.status_code != 200:
        raise DhanError(f"{path} returned HTTP {r.status_code}: {r.text[:200]!r}")
    try:
        return r.json()
    except ValueError:
        raise DhanError(f"{path} returned non-JSON: {r.text[:200]!r}") from None


# ═══════════════════════════════════════════════════════════════════════════
# Instrument lookup
# ═══════════════════════════════════════════════════════════════════════════

_futures_cache: dict = {}          # FUTFIX: {id, symbol, expiry (date), checked (date), fail_at}


def _ist_now() -> datetime:
    return datetime.utcnow() + timedelta(hours=5, minutes=30)


def nifty_futures_contract(force: bool = False) -> dict | None:
    """
    CURRENT NIFTY futures contract from Dhan's scrip master: {"id", "symbol", "expiry"}.

    Re-checked every trading day (IST) and rolled to the next month after 15:30 IST on expiry day, so a
    scheduler / dashboard left running over the monthly expiry never keeps reading the dead contract.
    Returns None on failure (VWAP then simply unavailable).
    """
    from datetime import time as _t
    pinned = getattr(config, "DHAN_FUTURES_SECURITY_ID", None)
    if pinned:
        log.warning("config.DHAN_FUTURES_SECURITY_ID is pinned (%s) — it will NOT roll at expiry", pinned)
        return {"id": str(pinned), "symbol": "pinned in config", "expiry": None}
    now = _ist_now()
    today, after_close = now.date(), now.time() >= _t(15, 30)
    c = _futures_cache
    if not force and c.get("id") and c.get("checked") == today and c.get("expiry") \
            and (c["expiry"] > today or (c["expiry"] == today and not after_close)):
        return {k: c[k] for k in ("id", "symbol", "expiry")}
    if not force and c.get("fail_at") and (now - c["fail_at"]).total_seconds() < 600:
        return None
    try:
        df = pd.read_csv(SCRIP_MASTER, low_memory=False)
        cols = {c_.upper().replace(" ", "_"): c_ for c_ in df.columns}

        def col(*names):
            for n in names:
                if n in cols:
                    return cols[n]
            return None

        seg = col("SEM_EXM_EXCH_ID", "EXCH_ID")
        inst = col("SEM_INSTRUMENT_NAME", "INSTRUMENT_NAME", "SEM_EXCH_INSTRUMENT_TYPE")
        under = col("SEM_TRADING_SYMBOL", "TRADING_SYMBOL")
        expiry = col("SEM_EXPIRY_DATE", "EXPIRY_DATE")
        sid = col("SEM_SMST_SECURITY_ID", "SECURITY_ID")
        if not all([seg, inst, under, expiry, sid]):
            log.warning("scrip master columns unexpected: %s", list(df.columns)[:10])
            c["fail_at"] = now
            return None

        m = df[(df[seg].astype(str).str.upper() == "NSE")
               & (df[under].astype(str).str.upper().str.startswith("NIFTY"))
               & (df[inst].astype(str).str.upper().str.contains("FUT", na=False))]
        m = m[~m[under].astype(str).str.upper().str.contains("BANK|FIN|MID|NXT|FPI|IT|PSE|CPSE|REIT", na=False)]
        m = m.assign(_exp=pd.to_datetime(m[expiry], errors="coerce")).dropna(subset=["_exp"])
        d = m["_exp"].dt.date
        m = m[(d > today) | ((d == today) & (not after_close))].sort_values("_exp")
        if m.empty:
            c["fail_at"] = now
            return None
        chosen = m.iloc[0]
        new_id = str(int(chosen[sid]))
        if c.get("id") and c["id"] != new_id:
            log.warning("NIFTY futures ROLLED: %s -> %s (%s expiry %s)", c["id"], new_id,
                        chosen[under], chosen._exp.date())
        c.update(id=new_id, symbol=str(chosen[under]), expiry=chosen._exp.date(), checked=today, fail_at=None)
        log.info("NIFTY futures: %s expiry %s -> id %s", chosen[under], chosen._exp.date(), new_id)
        return {k: c[k] for k in ("id", "symbol", "expiry")}
    except Exception as exc:                                # noqa: BLE001
        log.warning("futures lookup failed (%s) — VWAP will be unavailable", exc)
        c["fail_at"] = now
        return None


def nifty_futures_security_id(force: bool = False) -> str | None:
    """Security id of the CURRENT NIFTY future (see nifty_futures_contract)."""
    k = nifty_futures_contract(force)
    return k["id"] if k else None


# ═══════════════════════════════════════════════════════════════════════════
# Candles
# ═══════════════════════════════════════════════════════════════════════════

def _to_frame(payload: dict) -> pd.DataFrame:
    """Dhan returns parallel arrays keyed open/high/low/close/volume/timestamp."""
    keys = {k.lower(): k for k in payload}
    need = ["open", "high", "low", "close"]
    if not all(k in keys for k in need):
        raise DhanError(f"candle payload missing OHLC; keys={list(payload)[:8]}")

    ts_key = keys.get("timestamp") or keys.get("start_time") or keys.get("time")
    data = {k: payload[keys[k]] for k in need}
    data["volume"] = payload[keys["volume"]] if "volume" in keys else [0] * len(data["open"])

    df = pd.DataFrame(data)
    if ts_key:
        # v2 uses EPOCH seconds
        df["timestamp"] = pd.to_datetime(payload[ts_key], unit="s", errors="coerce")
        df["timestamp"] = (df["timestamp"].dt.tz_localize("UTC")
                           .dt.tz_convert("Asia/Kolkata").dt.tz_localize(None))
    else:
        raise DhanError(f"no timestamp field; keys={list(payload)[:8]}")
    return df.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)


def intraday(security_id: str, segment: str, instrument: str,
             days_back: int = 1, interval: str = "1") -> pd.DataFrame:
    """
    One-minute candles. We fetch ONLY 1-minute and resample locally.

    Reason: one call gives every timeframe, and resampling guarantees the 5m
    and 15m views agree with the 1m view. Polling three intervals separately
    invites them to disagree mid-candle, which is a miserable bug to chase.
    """
    end = datetime.now()
    start = end - timedelta(days=days_back)
    payload = _post("/charts/intraday", {
        "securityId": str(security_id),
        "exchangeSegment": segment,
        "instrument": instrument,
        "interval": interval,
        "oi": False,
        "fromDate": start.strftime("%Y-%m-%d %H:%M:%S"),
        "toDate": end.strftime("%Y-%m-%d %H:%M:%S"),
    })
    return _to_frame(payload)


def nifty_index_candles(days_back: int = 1) -> pd.DataFrame:
    """NIFTY 50 index. Volume is always zero here — that is correct, not a bug."""
    return intraday(config.DHAN_INDEX_SECURITY_ID, "IDX_I", "INDEX", days_back)


def banknifty_index_candles(days_back: int = 1) -> pd.DataFrame:
    """BANKNIFTY index from Dhan API (Security ID: 25, IDX_I segment)."""
    sec_id = getattr(config, "DHAN_BANKNIFTY_SECURITY_ID", "25")
    return intraday(sec_id, "IDX_I", "INDEX", days_back)


def futures_sanity(fut: pd.DataFrame | None, index_close: float | None
                   ) -> tuple[bool, str]:
    """
    Is this actually NIFTY futures, or some other instrument entirely?

    The security ID is resolved by pattern-matching Dhan's scrip master, and
    a pattern that matches the wrong row still returns a perfectly valid ID
    that fetches perfectly valid candles. Nothing downstream would notice:
    VWAP would be computed, the app would print "above VWAP", and the number
    would be meaningless because it belongs to a different instrument.

    Futures track the index within a percent or so — basis, not a different
    order of magnitude. So compare them. A future trading at 1,580 while the
    index is at 24,080 is not a basis, it is the wrong contract.
    """
    if fut is None or fut.empty:
        return False, "no futures candles"
    if not index_close:
        return True, "no index close to compare against — not checked"
    last = float(fut["close"].iloc[-1])
    drift = abs(last - index_close) / index_close * 100
    if drift > 5.0:
        return False, (
            f"futures last close {last:,.2f} against index {index_close:,.2f} "
            f"— {drift:.0f}% apart. Futures track the index within about a "
            f"percent, so this is the WRONG INSTRUMENT, not a basis. Any VWAP "
            f"built on it is meaningless. Run scrip_debug() to see what the "
            f"resolved security ID actually is.")
    return True, f"futures {last:,.2f} vs index {index_close:,.2f} ({drift:.2f}% basis)"


def scrip_debug(limit: int = 12) -> pd.DataFrame:
    """Every scrip-master row the futures matcher considers, so you can see
    which one it picked and why."""
    df = pd.read_csv(SCRIP_MASTER, low_memory=False)
    cols = {c.upper().replace(" ", "_"): c for c in df.columns}

    def col(*names):
        for n in names:
            if n in cols:
                return cols[n]
        return None

    seg = col("SEM_EXM_EXCH_ID", "EXCH_ID")
    inst = col("SEM_INSTRUMENT_NAME", "INSTRUMENT_NAME", "SEM_EXCH_INSTRUMENT_TYPE")
    under = col("SEM_TRADING_SYMBOL", "TRADING_SYMBOL")
    expiry = col("SEM_EXPIRY_DATE", "EXPIRY_DATE")
    sid = col("SEM_SMST_SECURITY_ID", "SECURITY_ID")
    m = df[(df[seg].astype(str).str.upper() == "NSE")
           & (df[under].astype(str).str.upper().str.startswith("NIFTY"))]
    if inst:
        m = m[m[inst].astype(str).str.upper().str.contains("FUT", na=False)]
    m = m[~m[under].astype(str).str.upper().str.contains("BANK|FIN|MID|NXT|FPI|IT|PSE|CPSE|REIT", na=False)]
    keep = [c for c in (under, inst, expiry, sid) if c]
    return m[keep].head(limit)


def nifty_futures_candles(days_back: int = 1) -> pd.DataFrame | None:
    """Current-month NIFTY futures — the only source of real volume."""
    sid = nifty_futures_security_id()
    if not sid:
        return None
    return intraday(sid, "NSE_FNO", "FUTIDX", days_back)


def daily(security_id: str = None, segment: str = "IDX_I",
          instrument: str = "INDEX", days_back: int = 400) -> pd.DataFrame:
    """
    Daily OHLC from Dhan's historical endpoint.

    The reason this exists: NSE's daily index history is a scraped report
    behind a cookie handshake, and when it fails it fails quietly. OHLC.csv
    stops growing, master levels keep rebuilding from whatever the newest
    row is, and the app prints "levels from the 26-08 close" with total
    confidence a week later. Nothing is wrong with the levels — they are
    correct for a close that is no longer today's.

    WHY IT TRIES SEVERAL REQUEST SHAPES
    ───────────────────────────────────
    v2 made `expiryCode` optional and it does not apply to an index at all,
    but the endpoint answers a wrong body with DH-905 "bad values for
    parameters" and does not say WHICH parameter. One wrong guess therefore
    looks identical to a broken subscription.

    So the variants are tried shortest-first and the one that works is
    reported. That is deliberately not silent: knowing the body Dhan accepts
    for an index is worth more than a frame, because the next endpoint added
    here will face the same undocumented ambiguity.
    """
    sid = str(security_id or getattr(config, "DHAN_INDEX_SECURITY_ID", "13"))
    end = datetime.now().date()
    start = end - timedelta(days=days_back)
    base = {
        "securityId": sid,
        "exchangeSegment": segment,
        "instrument": instrument,
        "fromDate": start.strftime("%Y-%m-%d"),
        "toDate": end.strftime("%Y-%m-%d"),
    }
    variants = [
        ("minimal", dict(base)),
        ("with oi", {**base, "oi": False}),
        ("with expiryCode", {**base, "expiryCode": 0}),
        ("with both", {**base, "expiryCode": 0, "oi": False}),
    ]

    errors = []
    for name, body in variants:
        try:
            payload = _post("/charts/historical", body)
        except DhanError as exc:
            errors.append(f"{name}: {str(exc)[:110]}")
            continue
        df = _to_frame(payload)
        log.info("daily(): Dhan accepted the %r body", name)
        df["Date"] = pd.to_datetime(df["timestamp"]).dt.normalize()
        out = df[["Date", "open", "high", "low", "close"]].copy()
        out.columns = ["Date", "Open", "High", "Low", "Close"]
        out = (out.drop_duplicates(subset=["Date"])
               .sort_values("Date").reset_index(drop=True))
        out.attrs["accepted_body"] = name
        return out

    raise DhanError(
        "every request shape was rejected by /charts/historical:\n  "
        + "\n  ".join(errors)
        + "\nIf all four say DH-905, the security ID or segment is wrong "
          "for a daily index query rather than the body being malformed.")


def daily_from_intraday(days_back: int = 60, security_id: str = None,
                        segment: str = "IDX_I",
                        instrument: str = "INDEX") -> pd.DataFrame:
    """
    Daily OHLC assembled from Dhan's INTRADAY endpoint.

    /charts/historical rejects every body shape for an index — minimal, with
    oi, with expiryCode, with both — all DH-905. /charts/intraday accepts the
    identical securityId and segment and returns candles happily. So the
    daily bars are built from hourly ones rather than argued about further.

    Two details that matter more than they look:

    Bars are filtered to 09:15-15:30 before aggregating. Dhan returns rows
    stamped outside market hours — the connectivity diagnostic showed one at
    19:28 — and a stray bar four hours after the close lands straight in the
    day's HIGH or LOW. Every level in the app is derived from those two
    numbers, so one junk row silently moves R1 through S4.

    Today's bar is dropped unless the session has closed. A partial day
    aggregates to a partial high and low, and a level built from half a
    session is worse than no level because nothing about it looks wrong.
    """
    sid = str(security_id or getattr(config, "DHAN_INDEX_SECURITY_ID", "13"))
    df = intraday(sid, segment, instrument,
                  days_back=min(days_back, 90), interval="60")
    if df is None or df.empty:
        raise DhanError("intraday returned nothing to aggregate")

    ts = pd.to_datetime(df["timestamp"])
    t = ts.dt.time
    open_t, close_t = config.MARKET_OPEN, config.MARKET_CLOSE
    keep = (t >= open_t) & (t <= close_t)
    dropped = int((~keep).sum())
    df = df[keep].copy()
    if df.empty:
        raise DhanError("every bar fell outside market hours — check timezone "
                        "handling in _to_frame")

    df["Date"] = pd.to_datetime(df["timestamp"]).dt.normalize()
    out = (df.groupby("Date")
             .agg(Open=("open", "first"), High=("high", "max"),
                  Low=("low", "min"), Close=("close", "last"),
                  bars=("close", "count"))
             .reset_index())

    today = pd.Timestamp(datetime.now().date())
    if datetime.now().time() < close_t:
        out = out[out["Date"] < today]

    # A full session is 7 hourly bars (09:15-15:30). Anything much shorter is
    # a truncated day whose high and low cannot be trusted.
    thin = out[out["bars"] < 4]
    out = out[out["bars"] >= 4].drop(columns=["bars"])
    out.attrs["dropped_outside_hours"] = dropped
    out.attrs["dropped_thin_days"] = len(thin)
    log.info("daily_from_intraday: %d sessions, %d bars outside hours "
             "ignored, %d thin days dropped", len(out), dropped, len(thin))
    return out.sort_values("Date").reset_index(drop=True)


def resolve_futures_by_price(index_close: float, max_candidates: int = 8,
                             tolerance_pct: float = 3.0) -> tuple[str | None, str]:
    """
    Pick the NIFTY futures contract by CHECKING ITS PRICE, not its name.

    The existing resolver matches trading symbols with a pattern and takes
    the nearest expiry. When that pattern catches the wrong row it still
    returns a valid security ID that fetches valid candles, and nothing
    downstream can tell. That is how VWAP ended up being computed from an
    instrument trading at 1,580 while NIFTY sat at 24,080 — every check
    passed, because every check was about whether data came back rather than
    whether it was the right data.

    A futures contract on an index trades within a percent or so of it. That
    is a fact the app can verify for itself, so it does: fetch a candle for
    each candidate, compare to the index, keep the one that matches. String
    matching proposes; the price disposes.

    Returns (security_id, explanation). The explanation names every
    candidate tried and why each was rejected, because a resolver that fails
    silently is what created this problem in the first place.
    """
    lines = []
    try:
        cands = scrip_debug(limit=max_candidates)
    except Exception as exc:                                # noqa: BLE001
        return None, f"could not read the scrip master: {exc}"
    if cands is None or cands.empty:
        return None, "no NIFTY futures rows matched in the scrip master"

    sid_col = [c for c in cands.columns if "SECURITY" in c.upper()]
    sym_col = [c for c in cands.columns if "SYMBOL" in c.upper()]
    if not sid_col:
        return None, f"no security-id column in {list(cands.columns)}"
    sid_col = sid_col[0]
    sym_col = sym_col[0] if sym_col else sid_col

    # Nearest unexpired contract first.
    #
    # Without this the loop tested rows in scrip-master order and returned
    # the first that priced near the index — which was the OCTOBER contract
    # at a 1.01% basis while the September one sat further down the table
    # untested. Both are "really NIFTY" and both pass a price check, so the
    # check alone cannot separate them. Front month is the one carrying the
    # volume, and volume is the entire reason this contract is being fetched
    # at all: VWAP built on a thin back-month contract is a worse number
    # than VWAP built on none.
    exp_col = [c for c in cands.columns if "EXPIRY" in c.upper()]
    if exp_col:
        try:
            cands = cands.copy()
            cands["_exp"] = pd.to_datetime(cands[exp_col[0]], errors="coerce")
            today = pd.Timestamp(datetime.now().date())
            cands = (cands[cands["_exp"] >= today]
                     .sort_values("_exp")
                     .drop(columns=["_exp"]))
        except Exception:                                   # noqa: BLE001
            pass

    for _, row in cands.iterrows():
        sid = str(row[sid_col]).split(".")[0]
        sym = str(row[sym_col])
        try:
            d = intraday(sid, "NSE_FNO", "FUTIDX", days_back=1, interval="60")
        except Exception as exc:                            # noqa: BLE001
            lines.append(f"  {sym} (id {sid}): fetch failed — {str(exc)[:60]}")
            continue
        if d is None or d.empty:
            lines.append(f"  {sym} (id {sid}): no candles")
            continue
        last = float(d["close"].iloc[-1])
        drift = abs(last - index_close) / index_close * 100
        if drift <= tolerance_pct:
            lines.insert(0, f"  MATCH {sym} (id {sid}): {last:,.2f} vs index "
                            f"{index_close:,.2f} — {drift:.2f}% basis")
            return sid, "\n".join(lines)
        lines.append(f"  {sym} (id {sid}): {last:,.2f} is {drift:.0f}% from "
                     f"the index — wrong instrument")

    return None, ("no candidate priced near the index:\n" + "\n".join(lines)
                  + "\nIf every row is far off, the segment or instrument "
                    "type in the scrip-master filter is wrong, not the "
                    "expiry choice.")


def futures_market_depth() -> dict | None:
    """
    Fetch Level 2 (5-depth) order book for the front-month NIFTY futures contract.
    Returns top 5 bids, top 5 asks, total quantities, and the 5-depth imbalance ratio.
    """
    try:
        sid = nifty_futures_security_id()
        if not sid:
            return None
        payload = {"NSE_FNO": [int(sid)]}
        r = requests.post(f"{BASE}/marketfeed/quote", json=payload,
                          headers=_headers(), timeout=config.REQUEST_TIMEOUT)
        if r.status_code != 200:
            return None
        res = r.json()
        data = res.get("data", {}).get("NSE_FNO", {}).get(str(sid), {})
        if not data:
            return None
        depth = data.get("depth", {})
        buys = depth.get("buy", [])
        sells = depth.get("sell", [])
        bid_qty_5 = sum(b.get("quantity", 0) for b in buys)
        ask_qty_5 = sum(s.get("quantity", 0) for s in sells)
        total_buy = data.get("buy_quantity", 0)
        total_sell = data.get("sell_quantity", 0)
        denom_5 = bid_qty_5 + ask_qty_5
        imbalance_5 = (bid_qty_5 - ask_qty_5) / denom_5 if denom_5 else 0.0
        return {
            "bid_qty_5": bid_qty_5,
            "ask_qty_5": ask_qty_5,
            "total_buy": total_buy,
            "total_sell": total_sell,
            "imbalance_5": imbalance_5,
            "buys": buys,
            "sells": sells,
            "ltp": data.get("last_price", 0.0),
        }
    except Exception as exc:                                # noqa: BLE001
        log.warning("futures_market_depth failed: %s", exc)
        return None


# EXPIRYROLL — one rule for the whole app: an expiry is LIVE until ROLL_AT IST on its own day.
ROLL_AT = "16:00"


def live_expiries(exps) -> list:
    """Drop expired dates (and today's after ROLL_AT IST), keep the original strings, sort nearest first."""
    import pandas as _pd
    from datetime import datetime as _dt, timedelta as _td
    now = _dt.utcnow() + _td(hours=5, minutes=30)
    roll = _dt.strptime(ROLL_AT, "%H:%M").time()
    keep = []
    for e in exps or []:
        d = _pd.to_datetime(str(e), errors="coerce", dayfirst=not str(e)[:4].isdigit())
        if _pd.isna(d):
            continue
        if d.date() > now.date() or (d.date() == now.date() and now.time() < roll):
            keep.append((d, e))
    keep.sort(key=lambda x: x[0])
    return [e for _, e in keep] or list(exps or [])


def fetch_dhan_expiries() -> list[str]:
    """Fetch list of valid expiry dates for NIFTY from Dhan."""
    try:
        r = requests.post(f"{BASE}/optionchain/expirylist",
                          json={"UnderlyingScrip": int(getattr(config, "DHAN_INDEX_SECURITY_ID", 13)),
                                "UnderlyingSeg": "IDX_I"},
                          headers=_headers(), timeout=config.REQUEST_TIMEOUT)
        if r.status_code == 200:
            return live_expiries(r.json().get("data", []))          # EXPIRYROLL
        return []
    except Exception as exc:                                # noqa: BLE001
        log.warning("fetch_dhan_expiries failed: %s", exc)
        return []


def fetch_dhan_option_chain(expiry: str | None = None) -> dict | None:
    """
    Fetch live full Option Chain from Dhan for NIFTY 50.
    Returns spot price and per-strike Call & Put data including official
    broker-calculated Greeks (Delta, Theta, Gamma, Vega), IV, OI, Volume, and Best Bid/Ask.
    """
    try:
        if not expiry:
            exps = fetch_dhan_expiries()
            if not exps:
                return None
            expiry = exps[0]
        r = requests.post(f"{BASE}/optionchain",
                          json={"UnderlyingScrip": int(getattr(config, "DHAN_INDEX_SECURITY_ID", 13)),
                                "UnderlyingSeg": "IDX_I",
                                "Expiry": expiry},
                          headers=_headers(), timeout=config.REQUEST_TIMEOUT)
        if r.status_code == 200:
            return r.json().get("data")
        return None
    except Exception as exc:                                # noqa: BLE001
        log.warning("fetch_dhan_option_chain failed: %s", exc)
        return None


def backfill_5m_candles(days_back: int = 15) -> list[Path]:
    """
    Download and store complete 5-minute candles into data/candles_5m/YYYY-MM-DD.csv
    with Index OHLC, Futures Volume, and true session VWAP.
    """
    out_dir = getattr(config, "CANDLE_5M_DIR", config.DATA_DIR / "candles_5m")
    out_dir.mkdir(parents=True, exist_ok=True)

    idx = intraday(config.DHAN_INDEX_SECURITY_ID, "IDX_I", "INDEX", days_back=days_back, interval="5")
    sid = nifty_futures_security_id()
    fut = intraday(sid, "NSE_FNO", "FUTIDX", days_back=days_back, interval="5") if sid else None

    if idx.empty:
        return []

    idx = idx.copy()
    idx["timestamp"] = pd.to_datetime(idx["timestamp"])
    idx["time"] = idx["timestamp"].dt.time
    idx = idx[(idx["time"] >= config.MARKET_OPEN) & (idx["time"] <= config.MARKET_CLOSE)].copy()
    idx["date"] = idx["timestamp"].dt.date

    if fut is not None and not fut.empty:
        fut = fut.copy()
        fut["timestamp"] = pd.to_datetime(fut["timestamp"])
        fut["time"] = fut["timestamp"].dt.time
        fut = fut[(fut["time"] >= config.MARKET_OPEN) & (fut["time"] <= config.MARKET_CLOSE)].copy()
        fut["date"] = fut["timestamp"].dt.date

    saved = []
    for d in sorted(idx["date"].unique()):
        idx_d = idx[idx["date"] == d].sort_values("timestamp").reset_index(drop=True)
        if fut is not None and not fut.empty:
            fut_d = fut[fut["date"] == d].sort_values("timestamp").reset_index(drop=True)
            m = pd.merge_asof(idx_d, fut_d[["timestamp", "volume"]].rename(columns={"volume": "fut_volume"}),
                              on="timestamp", direction="nearest")
            fvol = m["fut_volume"].fillna(0)
            tp = (m["high"] + m["low"] + m["close"]) / 3.0
            cum_pv = (tp * fvol).cumsum()
            cum_v = fvol.cumsum()
            m["vwap"] = (cum_pv / cum_v.replace(0, float("nan"))).round(2)
            m["volume"] = fvol
        else:
            m = idx_d
            m["vwap"] = float("nan")

        cols = ["timestamp", "open", "high", "low", "close", "volume", "vwap"]
        res = m[cols].copy()
        res["timestamp"] = res["timestamp"].dt.strftime("%Y-%m-%d %H:%M:%S")

        p = out_dir / f"{d:%Y-%m-%d}.csv"
        res.to_csv(p, index=False)
        saved.append(p)

    return saved


if __name__ == "__main__":
    import sys
    args = sys.argv[1:]
    if "--set" in args:
        idx = args.index("--set")
        if idx + 1 < len(args):
            t = args[idx + 1]
            print("Verifying token with Dhan API...")
            ok, msg = verify_token(t)
            if ok:
                save_token(t)
                print(f"[PASS] {msg}")
                sys.exit(0)
            else:
                print(f"[FAIL] {msg}")
                sys.exit(1)
        else:
            print("Usage: python dhan_client.py --set <access_token>")
            sys.exit(1)
    else:
        print("Testing currently configured Dhan token...")
        ok, msg = verify_token()
        info = token_preview()
        print(f"Configured: {info['configured']}, Length: {info['length']}, Preview: {info['preview']}")
        print(f"Result: {'PASS' if ok else 'FAIL'} - {msg}")
        sys.exit(0 if ok else 1)

