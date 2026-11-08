"""Human-readable StockMojo PE-CE ingest status (no secrets)."""
from __future__ import annotations

import os
import time
from typing import Any, Dict, Optional


def env_credential_flags() -> Dict[str, bool]:
    j = bool((os.environ.get("STOCKMOJO_JWT_J") or "").strip())
    t = bool((os.environ.get("STOCKMOJO_TOKEN_T") or "").strip())
    req = bool((os.environ.get("STOCKMOJO_REQUEST_JSON") or "").strip())
    return {
        "credentials_entered": j and t,
        "jwt_present": j,
        "session_token_present": t,
        "request_body_configured": req,
    }


def summarize_stockmojo_status(
    *,
    master_enabled: bool,
    writer_gate_enabled: bool,
    snap: Optional[Dict[str, Any]] = None,
    poller_present: bool,
) -> Dict[str, Any]:
    snap = snap or {}
    env = env_credential_flags()
    poll_status = str(snap.get("poll_status") or "NOT_STARTED")
    cfg_status = str(snap.get("configuration_status") or "")
    halted = bool(snap.get("halted"))
    available = bool(snap.get("available"))
    age = snap.get("age_seconds")

    credentials_entered = env["credentials_entered"]
    request_ready = credentials_entered and (
        env["request_body_configured"] or cfg_status == "OK" or poller_present
    )

    if not master_enabled:
        auth_state = "MASTER_OFF"
        download_state = "STOPPED"
        telemetry_state = "OFF"
        summary = "OFF"
        detail = "StockMojo download and PE-CE telemetry are turned OFF (master switch)."
    elif not poller_present:
        auth_state = "NOT_INITIALIZED"
        download_state = "STOPPED"
        telemetry_state = "OFF"
        summary = "NOT_READY"
        detail = "Poller not initialized — restart 8510 engine after config change."
    elif not credentials_entered:
        auth_state = "MISSING_CREDENTIALS"
        download_state = "STOPPED"
        telemetry_state = "OFF"
        summary = "SETUP_REQUIRED"
        detail = "J/T credentials missing in .env — run stockmojo_downloader_8510 --configure"
    elif cfg_status == "REQUEST_BODY_REQUIRED":
        auth_state = "MISSING_REQUEST"
        download_state = "STOPPED"
        telemetry_state = "OFF"
        summary = "SETUP_REQUIRED"
        detail = "Request body (symbol/expiry/range) not configured — run --configure-request"
    elif halted or poll_status.startswith("ACCESS_DENIED"):
        auth_state = "BLOCKED"
        download_state = "BLOCKED"
        telemetry_state = "OFF"
        summary = "AUTH_DENIED"
        detail = "StockMojo rejected credentials (401/403). Refresh tokens from browser."
    elif poll_status == "MARKET_CLOSED":
        auth_state = "OK_IDLE"
        download_state = "SESSION_CLOSED"
        telemetry_state = "IDLE"
        summary = "MARKET_CLOSED"
        detail = "Credentials look configured; market hours required for downloads."
    elif poll_status == "OK" and available:
        auth_state = "AUTHENTICATED"
        download_state = "ACTIVE"
        telemetry_state = "ACTIVE" if writer_gate_enabled else "DOWNLOAD_ONLY"
        summary = "LIVE_OK"
        detail = "Downloading PE-CE JSON and telemetry is fresh."
    elif poll_status == "OK" and not available:
        auth_state = "AUTHENTICATED"
        download_state = "STALE_DATA"
        telemetry_state = "STALE"
        summary = "STALE"
        detail = f"Last response stale (age {age}s). Waiting for next poll."
    else:
        auth_state = "PENDING"
        download_state = "WAITING"
        telemetry_state = "WAITING"
        summary = "WAITING"
        detail = f"Last poll: {poll_status or 'pending first download'}."

    return {
        "summary": summary,
        "detail": detail,
        "master_enabled": master_enabled,
        "credentials_entered": credentials_entered,
        "request_ready": request_ready,
        "authenticated": auth_state == "AUTHENTICATED",
        "auth_state": auth_state,
        "download_state": download_state,
        "downloading_active": download_state == "ACTIVE",
        "telemetry_active": telemetry_state == "ACTIVE",
        "telemetry_state": telemetry_state,
        "writer_gate_enabled": writer_gate_enabled,
        "poll_status": poll_status,
        "configuration_status": cfg_status,
        "last_archive": snap.get("last_archive"),
        "last_sync_time_ist": snap.get("time_ist"),
        "age_seconds": age,
    }
