# -*- coding: utf-8 -*-
"""RVOL — relative volume, computed straight from the Dhan API.

The number: today's cumulative volume UP TO A CLOCK TIME, divided by the average
cumulative volume up to that SAME clock time over the last N completed sessions.
Same stock, same time of day.

Why the time-of-day baseline: volume through the day is U-shaped - heavy at the
open, quiet through lunch, heavy into the close. Compared against a whole-day
average, every stock looks asleep at 10am and frantic at 3pm; you would be
measuring the clock, not the stock.

Why the floor is in RUPEES and not shares: MARUTI traded 29,442 shares by 09:45
on 2026-08-25 - under any sane share floor - yet that is Rs 39.8 crore of real
turnover. A Rs-50 stock doing 1 lakh shares is Rs 50 lakh and genuinely thin.
Filtering on share count quietly deletes every expensive stock.
"""
import datetime, json, os, threading, time, urllib.request

from . import config as C
from .universe import NIFTY50_SIDS

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
OPEN_SECS = 9 * 3600 + 15 * 60
CLOSE_SECS = 15 * 3600 + 30 * 60
CACHE_SECS = 900
MIN_GAP = 0.30                 # Dhan answers 429 if calls come back to back

_LOCK = threading.Lock()
_LAST = [0.0]


def universe(cfg):
    s = cfg.get("scan") or {}
    if s.get("universe") == "custom" and s.get("custom"):
        want = set(x.strip().upper() for x in s["custom"] if x.strip())
        return {k: v for k, v in NIFTY50_SIDS.items() if k in want}
    return dict(NIFTY50_SIDS)


def _fetch(cfg, sid):
    d = cfg["dhan"]
    frm = (datetime.datetime.now(IST) - datetime.timedelta(days=36)).strftime("%Y-%m-%d")
    to = datetime.datetime.now(IST).strftime("%Y-%m-%d")
    body = json.dumps({"securityId": str(sid), "exchangeSegment": "NSE_EQ",
                       "instrument": "EQUITY", "interval": "5",
                       "fromDate": frm, "toDate": to}).encode()
    req = urllib.request.Request(
        "https://api.dhan.co/v2/charts/intraday", data=body,
        headers={"access-token": d["access_token"],
                 "client-id": str(d.get("client_id") or ""),
                 "Content-Type": "application/json", "Accept": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=40).read())


def candles(cfg, sym, sid, force=False):
    """Cached + paced + retried. Firing 50 requests with no gap earns HTTP 429
    and the scan silently returns half the universe - which looks like a full
    table on screen, so it must not be allowed to happen."""
    p = os.path.join(C.cache_dir(), "%s.json" % sym)
    if not force and os.path.exists(p) and time.time() - os.path.getmtime(p) < CACHE_SECS:
        try:
            with open(p) as f:
                return json.load(f)
        except Exception:
            pass
    err = None
    for attempt in range(4):
        with _LOCK:
            gap = time.time() - _LAST[0]
            if gap < MIN_GAP:
                time.sleep(MIN_GAP - gap)
            _LAST[0] = time.time()
        try:
            res = _fetch(cfg, sid)
            with open(p, "w") as f:
                json.dump(res, f)
            return res
        except Exception as e:
            err = e
            if getattr(e, "code", None) == 429:
                time.sleep(0.8 * (attempt + 1))
                continue
            break
    if os.path.exists(p):                     # stale beats a hole in the table
        try:
            with open(p) as f:
                return json.load(f)
        except Exception:
            pass
    raise err


def by_session(res):
    out = {}
    for t, v, c in zip(res.get("timestamp") or [], res.get("volume") or [],
                       res.get("close") or []):
        dt = datetime.datetime.fromtimestamp(float(t), IST)
        out.setdefault(dt.strftime("%Y-%m-%d"), []).append(
            (dt.hour * 3600 + dt.minute * 60, float(v), float(c)))
    for d in out:
        out[d].sort()
    return out


def _cum(bars, cut):
    return sum(v for s, v, _ in bars if s <= cut)


def _tno(bars, cut):
    return sum(v * c for s, v, c in bars if s <= cut)


def stats(cfg, sym, sid, cut, today, floor):
    try:
        sess = by_session(candles(cfg, sym, sid))
    except Exception:
        return None
    if today not in sess:
        return None
    tv, tn = _cum(sess[today], cut), _tno(sess[today], cut)
    nd = int((cfg.get("scan") or {}).get("base_days") or 10)
    prior = [d for d in sorted(sess) if d < today][-nd:]
    vals = [x for x in (_cum(sess[d], cut) for d in prior) if x > 0]
    if len(vals) < 3:
        return None
    base = sum(vals) / len(vals)
    if base <= 0:
        return None
    return {"symbol": sym, "rvol": round(tv / base, 2), "today": int(tv),
            "normal": int(base), "turnover": int(tn), "sessions": len(vals),
            "ltp": round(sess[today][-1][2], 2), "thin": tn < floor}


def scan(cfg, cut_hm=None, today=None):
    s = cfg.get("scan") or {}
    cut_hm = cut_hm or s.get("default_cutoff") or "09:45"
    hh, mm = [int(x) for x in cut_hm.split(":")]
    cut = hh * 3600 + mm * 60
    today = today or datetime.datetime.now(IST).strftime("%Y-%m-%d")
    floor = float(s.get("min_turnover_cr") or 5) * 1e7
    uni = universe(cfg)
    rows = []
    for sym, sid in uni.items():
        r = stats(cfg, sym, sid, cut, today, floor)
        if r:
            rows.append(r)
    return {"cutoff": cut_hm, "date": today, "base_days": int(s.get("base_days") or 10),
            "min_turnover": floor, "n": len(rows), "universe": len(uni),
            "rows": sorted([r for r in rows if not r["thin"]], key=lambda r: -r["rvol"]),
            "thin": sorted([r for r in rows if r["thin"]], key=lambda r: -r["rvol"])}


def curve(cfg, sym, today=None):
    """Today's cumulative volume vs the last N sessions.

    Today stops at the last candle that exists; the average runs the FULL
    session, so you can see how far ahead of its own pace a stock is.
    """
    today = today or datetime.datetime.now(IST).strftime("%Y-%m-%d")
    sid = universe(cfg).get(sym) or NIFTY50_SIDS.get(sym)
    if not sid:
        return {"error": "unknown symbol %s" % sym}
    sess = by_session(candles(cfg, sym, sid))
    if today not in sess:
        return {"error": "no data for %s on %s" % (sym, today)}
    nd = int((cfg.get("scan") or {}).get("base_days") or 10)
    grid = list(range(OPEN_SECS, CLOSE_SECS + 1, 300))
    prior = [d for d in sorted(sess) if d < today][-nd:]

    def series(bars):
        out, run, i = [], 0.0, 0
        for g in grid:
            while i < len(bars) and bars[i][0] <= g:
                run += bars[i][1]
                i += 1
            out.append(run)
        return out

    tod = series(sess[today])
    last = max((s for s, _v, _c in sess[today]), default=OPEN_SECS)
    stack = [series(sess[d]) for d in prior]
    n = len(grid)
    return {"symbol": sym, "date": today, "sessions": len(prior),
            "times": ["%02d:%02d" % (g // 3600, (g % 3600) // 60) for g in grid],
            "today": [round(v) if g <= last else None for g, v in zip(grid, tod)],
            "avg": [round(sum(s[i] for s in stack) / len(stack)) if stack else 0
                    for i in range(n)],
            "hi": [round(max(s[i] for s in stack)) if stack else 0 for i in range(n)],
            "lo": [round(min(s[i] for s in stack)) if stack else 0 for i in range(n)],
            "last_time": "%02d:%02d" % (last // 3600, (last % 3600) // 60)}
