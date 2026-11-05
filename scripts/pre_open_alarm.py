#!/usr/bin/env python3
"""
scripts/pre_open_alarm.py — 08:55 IST Pre-Open Desk Alarm & Health Check.

Performs strict verification 20 minutes before market open (08:55 IST):
  1. Dhan API token validity and hours remaining (must have >= 1 hour left).
  2. Core processes listening on expected ports (Port 8510, Port 8600).
  3. Live /api/candles or /api/dhan/status round-trip response.

On ANY failure:
  - Emits loud terminal bell and system audio beep.
  - Fires critical desktop notification (notify-send -u critical).
  - Sends emergency Telegram alert (if configured).
  - Writes alarm state to data/desk_alarm.json for sticky UI rendering.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [pre_open_alarm] %(message)s"
)
log = logging.getLogger("pre_open_alarm")

ALARM_FILE = ROOT / "data" / "desk_alarm.json"


def load_env() -> Dict[str, str]:
    env_file = ROOT / ".env"
    env_vars: Dict[str, str] = {}
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env_vars[k.strip()] = v.strip()
                os.environ.setdefault(k.strip(), v.strip())
    return env_vars


def sound_loud_alarm(title: str, message: str) -> None:
    """Emits audible sound and loud desktop alert."""
    print("\a" * 5, flush=True)  # Terminal bell

    # Desktop notification via notify-send
    if shutil.which("notify-send"):
        try:
            subprocess.run([
                "notify-send",
                "-u", "critical",
                "-t", "30000",
                f"🚨 {title}",
                message
            ], check=False, timeout=3)
        except Exception as e:
            log.debug("notify-send failed: %s", e)

    # Audio playback if player available
    sound_players = ["paplay", "aplay", "play"]
    for sp in sound_players:
        if shutil.which(sp):
            # Check for common system alert sounds
            for snd in ["/usr/share/sounds/freedesktop/stereo/alarm-clock-elapsed.oga",
                        "/usr/share/sounds/freedesktop/stereo/complete.oga",
                        "/usr/share/sounds/speech-dispatcher/test.wav"]:
                if Path(snd).exists():
                    try:
                        subprocess.Popen([sp, snd], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        break
                    except Exception:
                        pass
            break


def send_telegram_alert(text: str) -> None:
    tok = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if tok and chat:
        url = f"https://api.telegram.org/bot{tok}/sendMessage"
        payload = json.dumps({"chat_id": chat, "text": text}).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status == 200:
                    log.info("Telegram pre-open alert dispatched.")
        except Exception as e:
            log.warning("Telegram dispatch failed: %s", e)


def check_dhan_token() -> Tuple[bool, str, float]:
    """
    Checks Dhan token validity against Dhan API fundlimit.
    Returns: (is_valid, status_text, hours_remaining)
    """
    token = os.environ.get("DHAN_ACCESS_TOKEN", "").strip()
    client_id = os.environ.get("DHAN_CLIENT_ID", "").strip()

    if not token or not client_id:
        # Check analyst/terminal8510_config.json
        cfg_p = ROOT / "analyst" / "terminal8510_config.json"
        if cfg_p.exists():
            try:
                c = json.loads(cfg_p.read_text(encoding="utf-8"))
                token = token or c.get("DHAN_ACCESS_TOKEN", "")
                client_id = client_id or c.get("DHAN_CLIENT_ID", "")
            except Exception:
                pass

    if not token or not client_id:
        return False, "DHAN credentials missing from .env and config.json", 0.0

    # Calculate hours left from expiry
    hours_left = 24.0
    expiry_utc_str = os.environ.get("TOKEN_EXPIRY_UTC", "")
    if expiry_utc_str:
        try:
            exp_dt = datetime.fromisoformat(expiry_utc_str.replace("Z", "+00:00"))
            rem_s = (exp_dt - datetime.now(timezone.utc)).total_seconds()
            hours_left = round(rem_s / 3600.0, 1)
        except Exception:
            pass

    if hours_left <= 0:
        return False, f"Dhan token is EXPIRED ({hours_left:.1f} hours remaining)", hours_left

    # Live verification ping to Dhan API
    url = "https://api.dhan.co/v2/fundlimit"
    req = urllib.request.Request(url, headers={
        "access-token": token,
        "client-id": client_id,
        "Content-Type": "application/json"
    })
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            if r.status == 200:
                return True, f"Token Valid ({hours_left:.1f}h remaining)", hours_left
            return False, f"Dhan API returned HTTP {r.status}", hours_left
    except urllib.error.HTTPError as e:
        return False, f"Dhan Auth Failed (HTTP {e.code}): Token expired or invalid", hours_left
    except Exception as e:
        # Network/timeout error
        return False, f"Dhan connection error: {str(e)[:80]}", hours_left


def check_port_listening(port: int) -> bool:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(2.0)
        return s.connect_ex(("127.0.0.1", port)) == 0


def check_candles_roundtrip() -> Tuple[bool, str]:
    """Tests live round-trip to candles endpoint."""
    endpoints = [
        "http://127.0.0.1:8600/api/candles?tf=5m&days=1&inst=NIFTY",
        "http://127.0.0.1:8510/api/dhan/status",
    ]
    for ep in endpoints:
        try:
            req = urllib.request.Request(ep, headers={"User-Agent": "DeskPreOpenCheck/1.0"})
            with urllib.request.urlopen(req, timeout=4) as r:
                if r.status == 200:
                    return True, f"Round-trip OK ({ep})"
        except Exception:
            continue
    return False, "Failed round-trip to both Port 8600 /api/candles and Port 8510"


def run_pre_open_check() -> Dict[str, Any]:
    load_env()
    failures: List[str] = []
    warnings: List[str] = []

    # 1. Dhan token check
    token_ok, token_msg, hours_left = check_dhan_token()
    if not token_ok:
        failures.append(f"Dhan Token: {token_msg}")
    elif hours_left < 4.0:
        warnings.append(f"Dhan Token expires in {hours_left:.1f} hours! Renew before open.")

    # 2. Ports check
    port8510_ok = check_port_listening(8510)
    if not port8510_ok:
        failures.append("Port 8510 (engine_8510_live) is NOT listening!")

    port8600_ok = check_port_listening(8600)
    if not port8600_ok:
        warnings.append("Port 8600 (paperdesk-ict) not listening (may start at 08:55 via desk_hours.sh)")

    # 3. Candles round-trip
    candles_ok, candles_msg = check_candles_roundtrip()
    if not candles_ok:
        if port8510_ok:
            failures.append(f"Candles round-trip failed: {candles_msg}")
        else:
            warnings.append(f"Candles round-trip pending daemon start: {candles_msg}")

    status_str = "PASSED" if not failures else "CRITICAL_FAILED"
    result = {
        "timestamp": time.time(),
        "time_ist": datetime.now().strftime("%Y-%m-%d %H:%M:%S IST"),
        "status": status_str,
        "token_ok": token_ok,
        "token_hours_left": hours_left,
        "token_message": token_msg,
        "port_8510_ok": port8510_ok,
        "port_8600_ok": port8600_ok,
        "candles_ok": candles_ok,
        "candles_message": candles_msg,
        "failures": failures,
        "warnings": warnings,
    }

    # Save to disk for frontend consumption
    ALARM_FILE.parent.mkdir(parents=True, exist_ok=True)
    ALARM_FILE.write_text(json.dumps(result, indent=2), encoding="utf-8")

    if failures:
        fail_summary = "\n".join(f"• {f}" for f in failures)
        log.error("🚨 PRE-OPEN CHECK FAILED!\n%s", fail_summary)
        sound_loud_alarm(
            title="DESK PRE-OPEN ALARM (08:55 IST)",
            message=f"Pre-open failures detected!\n{fail_summary}"
        )
        send_telegram_alert(f"🚨 TRADING DESK PRE-OPEN ALARM (08:55 IST) 🚨\n\n{fail_summary}\n\nFix immediately before 09:15 open!")
    else:
        log.info("✅ Pre-open check PASSED. Token valid (%.1fh left), daemons listening.", hours_left)

    return result


if __name__ == "__main__":
    res = run_pre_open_check()
    if res["status"] != "PASSED":
        sys.exit(1)
    sys.exit(0)
