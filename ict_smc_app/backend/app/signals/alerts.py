"""
signals/alerts.py — the alert engine. Its job is to say LESS (mistake #10: over-alerting trains you to ignore it).

  * Only signals whose trigger candle JUST closed can alert (no re-alerting history after a restart).
  * Each signal has a unique key → stored in SQLite → never sent twice, even across restarts.
  * Cooldown per (setup, side, timeframe), and a hard daily cap.
  * Grades: 'trade' (setup A/C passed every filter), 'skip' (shown in the app's log, never pushed),
    'info' (BOS/MSS confirmations — app only unless send_info_alerts).
  * Telegram is optional and reuses TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID from your OC .env.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional

import httpx

from .. import config, db
from ..timeutil import ist_date, now_epoch, to_ist
from .playbook import Signal

log = logging.getLogger("alerts")


class Alerts:
    def __init__(self, broadcast) -> None:
        self.broadcast = broadcast
        self._last: Dict[str, float] = {}

    def _today_count(self) -> int:
        d0 = int(now_epoch()) - int(now_epoch() + 19800) % 86400
        r = db.query("SELECT COUNT(*) AS n FROM alerts WHERE ts >= ? AND grade = 'trade'", (d0,))
        return int(r[0]["n"]) if r else 0

    async def handle(self, sigs: List[Signal]) -> List[Dict[str, Any]]:
        A = config.rules()["alerts"]
        sent = []
        for s in sigs:
            grade = "trade" if s.status == "TRADE" else ("info" if s.status == "INFO" else "skip")
            if db.query("SELECT 1 FROM alerts WHERE key = ?", (s.key,)):
                continue
            ck = f"{s.setup}|{s.side}|{s.tf}"
            if grade == "trade":
                if time.time() - self._last.get(ck, 0) < 60 * float(A["cooldown_min"]):
                    grade = "skip"
                    s.reasons.append(f"cooldown {A['cooldown_min']} min")
                elif self._today_count() >= int(A["max_per_day"]):
                    grade = "skip"
                    s.reasons.append(f"daily cap {A['max_per_day']} reached")
            row = {"ts": int(now_epoch()), "key": s.key, "tf": s.tf, "kind": s.setup, "side": s.side, "grade": grade,
                   "title": s.headline, "body": " → ".join(s.story), "price": s.entry, "payload": json.dumps(s.to_dict())}
            db.execute("INSERT OR IGNORE INTO alerts (ts,key,tf,kind,side,grade,title,body,price,payload) "
                       "VALUES (:ts,:key,:tf,:kind,:side,:grade,:title,:body,:price,:payload)", row)
            if grade == "trade":
                self._last[ck] = time.time()
            out = dict(row, payload=s.to_dict())
            sent.append(out)
            await self.broadcast({"type": "alert", "alert": out})
            if A.get("telegram") and (grade == "trade" or (grade == "info" and A.get("send_info_alerts"))):
                await telegram(f"{'🟢' if s.side == 'long' else '🔴'} NIFTY {s.headline}\n" + "\n".join(s.story))
        return sent


async def notify_event(alerts_obj, e: Dict[str, Any], max_age_s: int = 600) -> Optional[Dict[str, Any]]:
    """RVOL (and other context) events → the same alert log + live push + optional Telegram. Deduplicated by key,
    and only FRESH events alert (a restart never replays the morning)."""
    if now_epoch() - int(e["ts"]) > max_age_s or db.query("SELECT 1 FROM alerts WHERE key = ?", (e["key"],)):
        return None
    row = {"ts": int(now_epoch()), "key": e["key"], "tf": e.get("tf"), "kind": e["kind"], "side": e.get("dir"),
           "grade": "rvol", "title": e["title"], "body": e["body"], "price": e.get("price"), "payload": json.dumps(e)}
    db.execute("INSERT OR IGNORE INTO alerts (ts,key,tf,kind,side,grade,title,body,price,payload) "
               "VALUES (:ts,:key,:tf,:kind,:side,:grade,:title,:body,:price,:payload)", row)
    out = dict(row, payload=e)
    await alerts_obj.broadcast({"type": "alert", "alert": out})
    A = config.rules()["alerts"]
    if A.get("telegram") and config.rule("rvol", "telegram", True):
        await telegram(f"📊 {e['title']}\n{e['body']}")
    return out


async def telegram(text: str) -> bool:
    tok, chats = config.env("TELEGRAM_BOT_TOKEN"), config.env("TELEGRAM_CHAT_ID")
    if not tok or not chats:
        return False
    ok = True
    async with httpx.AsyncClient(timeout=10) as c:
        for chat in [x.strip() for x in chats.split(",") if x.strip()]:
            try:
                r = await c.post(f"https://api.telegram.org/bot{tok}/sendMessage", json={"chat_id": chat, "text": text})
                ok &= r.status_code == 200
            except httpx.HTTPError as e:
                log.warning("telegram failed: %s", e)
                ok = False
    return ok


def recent(limit: int = 60, grade: Optional[str] = None) -> List[Dict[str, Any]]:
    if grade:
        rows = db.query("SELECT * FROM alerts WHERE grade = ? ORDER BY id DESC LIMIT ?", (grade, limit))
    else:
        rows = db.query("SELECT * FROM alerts ORDER BY id DESC LIMIT ?", (limit,))
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["payload"] = json.loads(d["payload"] or "{}")
        except ValueError:
            d["payload"] = {}
        d["time"] = to_ist(d["ts"]).strftime("%d %b %H:%M")
        out.append(d)
    return out
