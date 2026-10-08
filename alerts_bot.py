"""
alerts_bot.py — 📲 Telegram alerts for your NIFTY day, so you don't have to stare at screens.

  08:55  pre-market brief: GIFT NIFTY / global-cue gap, similar gaps in your history, your gap rule, today's events
  09:15–15:30 (every 30 s)
         • 15-min 8 EMA setup: candle CLOSES across the 8 EMA (never the first 15-min candle) → "watch"; the next
           candle BREAKS the signal candle's high / low → "TRIGGER" with ITM strike (MJ rule), entry and SL
         • price near / through the previous day's HIGH or LOW
         • call wall / put wall shifted · max pain shifted (writers moving their line)
         • ATM straddle expansion burst (+4 % in 5 min) = breakout moment
         • paper desk discipline: 6th order of the day, day loss beyond ₹1,500 (every profile)
  20:30  tomorrow's events (holiday / expiry / RBI / Fed / CPI / big results)

Setup (once)
  1. Telegram → @BotFather → /newbot → copy the TOKEN.   2. Open your new bot and send it "hi".
  3. Add to the .env in the OC folder:   TELEGRAM_BOT_TOKEN=123456:ABC...
  4. .venv/bin/python alerts_bot.py --chatid      → prints your chat id → add   TELEGRAM_CHAT_ID=12345678
     (your wife: she sends "hi" to the same bot, run --chatid again, then TELEGRAM_CHAT_ID=12345678,87654321)
  5. .venv/bin/python alerts_bot.py --test        → you get a test message
  6. ./install_alerts.sh                          → runs by itself every trading day (survives reboots)
  Other:  --once (one check now, printed)  ·  --dry (print instead of sending)  ·  --brief (send the pre-market brief now)
Information only — not financial advice. Alerts are prompts to LOOK, not orders.
"""
from __future__ import annotations

import json
import os
import sys
import time as _time
import traceback
from datetime import date, datetime, time, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
DRY = "--dry" in sys.argv


def _env() -> None:
    try:
        import dhan_client as dc
        dc._load_env(force=True)
    except Exception:                                         # noqa: BLE001
        pass
    f = ROOT / ".env"
    if f.exists():
        for ln in f.read_text().splitlines():
            if "=" in ln and not ln.strip().startswith("#"):
                k, v = ln.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def send(text: str) -> bool:
    if DRY:
        print("──", datetime.now().strftime("%H:%M:%S"), "\n" + text)
        return True
    import requests
    _env()
    tok, chats = os.environ.get("TELEGRAM_BOT_TOKEN", ""), os.environ.get("TELEGRAM_CHAT_ID", "")
    if not tok or not chats:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing in .env — message not sent:\n", text)
        return False
    ok = True
    for c in [x.strip() for x in chats.split(",") if x.strip()]:
        try:
            r = requests.post(f"https://api.telegram.org/bot{tok}/sendMessage", timeout=15,
                              json=dict(chat_id=c, text=text[:4000], parse_mode="HTML", disable_web_page_preview=True))
            ok &= r.ok
            if not r.ok:
                print("telegram error", r.status_code, r.text[:200])
        except Exception as exc:                              # noqa: BLE001
            print("telegram send failed:", exc)
            ok = False
    return ok


# ── state (what was already sent today) ─────────────────────────────────────
def _state_file() -> Path:
    import config
    return Path(config.DATA_DIR) / "alerts_state.json"


def load_state() -> dict:
    try:
        s = json.loads(_state_file().read_text())
    except Exception:                                         # noqa: BLE001
        s = {}
    if s.get("day") != date.today().isoformat():
        s = {"day": date.today().isoformat(), "sent": {}, "walls": {}, "mp": None, "bursts": []}
    return s


def save_state(s: dict) -> None:
    _state_file().write_text(json.dumps(s, default=str))


def once(s: dict, key: str, text: str) -> None:
    if key in s["sent"]:
        return
    if send(text):
        s["sent"][key] = datetime.now().isoformat(timespec="seconds")
        save_state(s)


# ═══════════════════════════════════════════════════════════════════════════
# checks
# ═══════════════════════════════════════════════════════════════════════════
_C: dict = {"n": -1, "snaps": []}


def snaps_today() -> list:
    import config
    import store
    d = Path(config.SNAPSHOT_DIR) / date.today().strftime("%Y-%m-%d")
    n = len(list(d.glob("*.meta.json"))) if d.exists() else 0
    if n != _C["n"]:
        _C.update(n=n, snaps=store.load_day(date.today()) if n else [])
    return _C["snaps"]


def prev_snaps() -> list:
    """Previous stored session (to seed the 8 EMA with real candles), loaded once a day."""
    if _C.get("prev_day") == date.today():
        return _C.get("prev", [])
    import config
    import store
    root = Path(config.SNAPSHOT_DIR)
    days = sorted(p.name for p in root.iterdir() if p.is_dir() and p.name < date.today().isoformat()) if root.exists() else []
    _C["prev"] = store.load_day(date.fromisoformat(days[-1])) if days else []
    _C["prev_day"] = date.today()
    return _C["prev"]


def candles15(snaps: list) -> pd.DataFrame:
    if not snaps:
        return pd.DataFrame()
    idx = pd.to_datetime([pd.Timestamp(x[0]).tz_localize(None) if pd.Timestamp(x[0]).tzinfo else pd.Timestamp(x[0]) for x in snaps])
    sp = pd.Series([float(x[1]) for x in snaps], index=idx)
    parts = [g.resample("15min", origin="start_day", offset="15min").ohlc().dropna() for _, g in sp.groupby(sp.index.date)]
    return pd.concat(parts) if parts else pd.DataFrame()


def prev_day() -> pd.Series | None:
    try:
        import levels
        d = levels.load_ohlc()
        d = d[pd.to_datetime(d["Date"]).dt.date < date.today()]
        return d.iloc[-1] if len(d) else None
    except Exception:                                         # noqa: BLE001
        return None


def itm_strike(spot: float, side: str) -> int:
    """MJ: CE = spot − 100…200, PE = spot + 100…200, multiples of 100."""
    return int(np.floor((spot - 100) / 100) * 100) if side == "CE" else int(np.ceil((spot + 100) / 100) * 100)


def check_ema(s: dict, snaps: list, pdr) -> None:
    if len(snaps) < 20:
        return
    sp = pd.Series([float(x[1]) for x in snaps])
    now = datetime.now()
    allc = pd.concat([candles15(prev_snaps()), candles15(snaps)])
    allc = allc[allc.index + pd.Timedelta(minutes=15) <= now]   # closed candles only
    e8_all = allc.close.ewm(span=8, adjust=False).mean()          # seeded with the previous session's candles
    c = allc[allc.index.date == now.date()]
    e8 = e8_all.reindex(c.index)
    if len(c) < 2:
        return
    pc = float(pdr["Close"]) if pdr is not None else float(c.open.iloc[0])
    gap = float(c.open.iloc[0]) - pc
    block = "CE" if gap >= 50 else "PE" if gap <= -50 else None
    for i in range(1, len(c)):                                  # i = signal candle (never the 09:15 candle)
        t = c.index[i]
        if t + pd.Timedelta(minutes=15) < now - pd.Timedelta(minutes=35):
            continue                                            # old signals (e.g. bot restarted mid-day) — don't spam
        up = c.close.iloc[i] > e8.iloc[i] and c.close.iloc[i - 1] <= e8.iloc[i - 1]
        dn = c.close.iloc[i] < e8.iloc[i] and c.close.iloc[i - 1] >= e8.iloc[i - 1]
        if not (up or dn):
            continue
        side = "CE" if up else "PE"
        hi, lo = float(c.high.iloc[i]), float(c.low.iloc[i])
        rule = f"\n⛔ Your gap rule blocks {side} today (gap {gap:+.0f})" if block == side else ""
        once(s, f"ema_watch_{t:%H%M}",
             f"👀 <b>15-min 8 EMA {'CROSS UP' if up else 'CROSS DOWN'}</b> {t:%H:%M} candle closed {c.close.iloc[i]:,.1f} "
             f"(8 EMA {e8.iloc[i]:,.1f}).\nBuy {side} ONLY if the next candle breaks "
             f"{'above ' + format(hi, ',.1f') if up else 'below ' + format(lo, ',.1f')}. SL {'below ' + format(lo, ',.1f') if up else 'above ' + format(hi, ',.1f')}."
             + rule)
        live_px = float(sp.iloc[-1])
        for j in list(range(i + 1, min(i + 3, len(c)))) + ([None] if i >= len(c) - 2 else []):
            # closed follow-through candles, then the candle forming NOW (alert the moment price breaks the level)
            hit = ((live_px > hi) if up else (live_px < lo)) if j is None else \
                ((c.high.iloc[j] > hi) if up else (c.low.iloc[j] < lo))
            if hit:
                when = "the candle forming now" if j is None else f"the {c.index[j]:%H:%M} candle"
                spot = float(sp.iloc[-1])
                k = itm_strike(spot, side)
                once(s, f"ema_trig_{t:%H%M}",
                     f"🚀 <b>{side} TRIGGER</b> — follow-through: {when} broke the {t:%H:%M} signal "
                     f"{'high ' + format(hi, ',.1f') if up else 'low ' + format(lo, ',.1f')}.\nNIFTY {spot:,.1f} · ITM strike "
                     f"<b>{k} {side}</b> · SL on NIFTY {'below ' + format(lo, ',.1f') if up else 'above ' + format(hi, ',.1f')} · "
                     f"one trade per rally, 1 lot." + rule)
                break


def check_pd_levels(s: dict, snaps: list, pdr) -> None:
    if pdr is None or not snaps:
        return
    spot = float(snaps[-1][1])
    for lab, v in (("HIGH", float(pdr["High"])), ("LOW", float(pdr["Low"]))):
        if abs(spot - v) <= 10:
            once(s, f"pd_near_{lab}", f"📍 NIFTY {spot:,.1f} at the PREVIOUS DAY {lab} {v:,.1f} — your level: watch the candle "
                                      f"(rejection = trade back {'down' if lab == 'HIGH' else 'up'}, close through = breakout).")
        if (lab == "HIGH" and spot > v + 15) or (lab == "LOW" and spot < v - 15):
            once(s, f"pd_break_{lab}", f"💥 NIFTY {spot:,.1f} broke the PREVIOUS DAY {lab} {v:,.1f} by {abs(spot - v):.0f} pts — "
                                       "confirm on a 15-min close before acting.")


def check_walls(s: dict) -> None:
    try:
        import olab_pro as op
        _, a = op.load(date.today())
    except Exception:                                         # noqa: BLE001
        return
    if not a:
        return
    w = a.get("walls") or {}
    for key, lab in (("floor", "PUT WALL (support)"), ("ceiling", "CALL WALL (resistance)")):
        if not w.get(key):
            continue
        k = int(w[key]["strike"])
        old = s["walls"].get(key)
        if old is None:
            s["walls"][key] = k
            continue
        if k != old:
            if send(f"🧱 <b>{lab} moved {old:,} → {k:,}</b> ({'up' if k > old else 'down'}) — writers moved their line: "
                    + ("bullish" if (k > old) else "bearish") + f". NIFTY {a['spot']:,.1f}."):
                s["walls"][key] = k
    mp = a.get("mp_now")
    if mp:
        if s.get("mp") is None:
            s["mp"] = mp
        elif abs(mp - s["mp"]) >= 50:
            if send(f"🎯 <b>Max pain {s['mp']:,.0f} → {mp:,.0f}</b> — writers re-positioning "
                    f"{'higher (bullish tilt)' if mp > s['mp'] else 'lower (bearish tilt)'}. NIFTY {a['spot']:,.1f}."):
                s["mp"] = mp
    save_state(s)


def check_straddle(s: dict, snaps: list) -> None:
    try:
        import straddle_pro as sp_
        step = 50.0
        x = sp_.wide(snaps)
        z = sp_.add_future(sp_.series(x, None, step), None)
        b = sp_.bursts(z)
    except Exception:                                         # noqa: BLE001
        return
    for r in b.itertuples():
        key = f"burst_{pd.Timestamp(r.ts):%H%M}"
        if pd.Timestamp(r.ts) >= pd.Timestamp(datetime.now()) - pd.Timedelta(minutes=10):
            once(s, key, f"⚡ <b>Straddle burst</b> {pd.Timestamp(r.ts):%H:%M}: ATM straddle +{r.chg:.0f}% in 5 min, NIFTY "
                         f"{r.dfut:+.0f} pts — a range is breaking {'UP' if r.dfut > 0 else 'DOWN'}. Enter on the pull-back, "
                         "not the spike.")


def check_desk(s: dict) -> None:
    import config
    base = Path(config.DATA_DIR) / "paper_trading"
    dirs = [("you", base)] + [(p.name, p) for p in (base / "profiles").glob("*") if p.is_dir() and not p.name.startswith("replay")]
    today = date.today().isoformat()
    for who, d in dirs:
        f = d / "trades.csv"
        if not f.exists():
            continue
        try:
            t = pd.read_csv(f)
            t = t[t["exit_time"].astype(str).str[:10] == today]
        except Exception:                                     # noqa: BLE001
            continue
        n = len(t)
        net = float(pd.to_numeric(t.get("net_pnl_rupees"), errors="coerce").sum()) if n else 0.0
        if n >= 6:
            once(s, f"desk_n_{who}", f"✋ <b>{who}: {n} trades today</b> — your limit is 6. Stop for the day; charges are "
                                     "where your Dhan losses came from.")
        if net <= -1500:
            once(s, f"desk_loss_{who}", f"🛑 <b>{who}: day P&L ₹{net:,.0f}</b> — past your ₹1,500 daily stop. Close the desk.")


def premarket(s: dict) -> None:
    import events_cal as ec
    parts = []
    try:
        import global_cues as gc
        parts.append(gc.brief_text())
    except Exception as exc:                                  # noqa: BLE001
        parts.append(f"🌏 Global cues unavailable ({str(exc)[:80]})")
    parts.append(ec.flags_text(date.today()))
    once(s, "premarket", "\n\n".join(parts))


def evening(s: dict) -> None:
    import events_cal as ec
    nd = ec.next_trading_day(date.today())
    f = ec.calendar(date.today() + timedelta(days=1), nd)
    f = f[f.impact.isin(["CLOSED", "EXPIRY", "HIGH"])]
    if len(f):
        once(s, "evening", "🌙 <b>Before tomorrow</b>\n" + ec.flags_text(nd))


def cycle(s: dict) -> None:
    import events_cal as ec
    now = datetime.now()
    td = ec.trading_day(now.date())
    if td and time(8, 55) <= now.time() < time(9, 15):
        premarket(s)
    if time(20, 30) <= now.time() < time(21, 0):
        evening(s)
    if not td or not (time(9, 15) <= now.time() <= time(15, 31)):
        return
    snaps = snaps_today()
    pdr = prev_day()
    for fn in (lambda: check_ema(s, snaps, pdr), lambda: check_pd_levels(s, snaps, pdr), lambda: check_straddle(s, snaps),
               lambda: check_desk(s)):
        try:
            fn()
        except Exception:                                     # noqa: BLE001
            traceback.print_exc()
    if now.minute % 2 == 0:
        try:
            check_walls(s)
        except Exception:                                     # noqa: BLE001
            traceback.print_exc()


def chat_ids() -> None:
    import requests
    _env()
    tok = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not tok:
        print("Put TELEGRAM_BOT_TOKEN=... in .env first (from @BotFather)."); return
    r = requests.get(f"https://api.telegram.org/bot{tok}/getUpdates", timeout=15).json()
    seen = {}
    for u in r.get("result", []):
        ch = (u.get("message") or u.get("edited_message") or {}).get("chat") or {}
        if ch.get("id"):
            seen[ch["id"]] = f"{ch.get('first_name', '')} {ch.get('last_name', '')} @{ch.get('username', '')}".strip()
    if not seen:
        print("No messages yet — open your bot in Telegram, send 'hi', then run this again.")
    for k, v in seen.items():
        print(f"chat id {k}  ←  {v}")
    if seen:
        print("Add to .env:  TELEGRAM_CHAT_ID=" + ",".join(str(k) for k in seen))


def main():
    _env()
    if "--chatid" in sys.argv:
        chat_ids(); return
    if "--test" in sys.argv:
        print("sent" if send("✅ NIFTY alerts are connected. You'll get the pre-market brief at 08:55 on trading days.")
              else "not sent"); return
    s = load_state()
    if "--brief" in sys.argv:
        s["sent"].pop("premarket", None)
        premarket(s); return
    if "--once" in sys.argv:
        cycle(s); print("done"); return
    print("alerts running — Ctrl+C to stop")
    while True:
        try:
            s = load_state()
            cycle(s)
        except Exception:                                     # noqa: BLE001
            traceback.print_exc()
        _time.sleep(30)


if __name__ == "__main__":
    main()
