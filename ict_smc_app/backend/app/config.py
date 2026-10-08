"""
config.py — settings from .env + every threshold from rules.json.

.env lookup order (first that exists wins):
  1. the file named by ENV_FILE  (point it at your OC folder's .env to share the daily Dhan token)
  2. backend/.env

Keys read:  DHAN_CLIENT_ID, DHAN_ACCESS_TOKEN, [DHAN_PIN, DHAN_TOTP_SECRET],
            [TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID], [DHAN_BASE_URL, DHAN_FEED_URL — only for the fake server]

The .env is re-read whenever its modification time changes, so `sed -i` on the token at 08:30 is picked up
without a restart.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

BACKEND_DIR = Path(__file__).resolve().parent.parent
APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("ICT_DATA_DIR", BACKEND_DIR / "data"))
DB_PATH = DATA_DIR / "ict_smc.sqlite3"
RULES_PATH = APP_DIR / "rules.json"
FRONTEND_DIST = BACKEND_DIR.parent / "frontend" / "dist"

DATA_DIR.mkdir(parents=True, exist_ok=True)

_env_mtime: float = -1.0
_env_path: Optional[Path] = None


def env_file() -> Path:
    p = os.environ.get("ENV_FILE", "").strip()
    if p:
        return Path(os.path.expanduser(p))
    return BACKEND_DIR / ".env"


def load_env(force: bool = False) -> None:
    """Minimal .env reader (no python-dotenv). Values already set in the real environment are kept
    unless the .env changed since the last read — then the file wins (the token was refreshed)."""
    global _env_mtime, _env_path
    p = env_file()
    if not p.exists():
        return
    m = p.stat().st_mtime
    if not force and _env_path == p and m == _env_mtime:
        return
    first_read = _env_mtime < 0
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if first_read and k in os.environ and os.environ[k]:
            continue                                   # an explicit env var beats the file on first read
        os.environ[k] = v
    _env_mtime, _env_path = m, p


def env(key: str, default: str = "") -> str:
    load_env()
    return os.environ.get(key, default).strip()


def base_url() -> str:
    return env("DHAN_BASE_URL", "https://api.dhan.co/v2").rstrip("/")


def feed_url() -> str:
    return env("DHAN_FEED_URL", "wss://api-feed.dhan.co")


_rules: Optional[Dict[str, Any]] = None


def rules() -> Dict[str, Any]:
    global _rules
    if _rules is None:
        _rules = json.loads(RULES_PATH.read_text(encoding="utf-8"))
    return _rules


def rule(section: str, key: str, default: Any = None) -> Any:
    return rules().get(section, {}).get(key, default)
