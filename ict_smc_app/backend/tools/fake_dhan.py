"""
tools/fake_dhan.py — a local stand-in for DhanHQ v2 so the whole app can be tested after market hours.

It replays Thursday 8 Oct 2026 on a simulated clock:
  REST   POST /v2/charts/intraday      1m / 5m / 15m / 25m / 60m candles up to the simulated "now"
         POST /v2/optionchain/expirylist, POST /v2/optionchain (synthetic chain around spot, BS greeks)
         POST /v2/marketfeed/ltp, GET /v2/profile, GET /v2/RenewToken
  WS     /  (binary little-endian ticker packets, code 2, + prev-close packet code 6) — same wire format as Dhan
Price path: the real 8-Oct 5m (13:10–15:25) and 15m (09:15–13:00) candles, split into 1-minute paths that hit
each candle's exact open/high/low/close. 13:00–13:10 and the previous day (7 Oct) are SYNTHETIC — 7 Oct only
exists so the app has a previous close (22,216.2, which makes 8 Oct's close of 22,231.8 the +0.07 % on your chart).
The option chain is synthetic too (call wall 22,500, put wall 22,200, like your screenshots).

Run (two terminals):
  export ICT_SIM_START=$(python -c "import datetime as d;print(int(d.datetime(2026,10,8,14,25,tzinfo=d.timezone(d.timedelta(hours=5,minutes=30))).timestamp()))")
  export ICT_SIM_REAL0=$(date +%s) ICT_SIM_SPEED=10
  .venv/bin/python tools/fake_dhan.py                         # port 8701
  ENV_FILE=tools/fake.env .venv/bin/python -m uvicorn app.api.main:app --port 8600
Options: FAKE_DROP_AFTER_S=40 closes the socket every 40 real seconds (tests auto-reconnect + gap repair);
         FAKE_LTT_MODE=ist stamps LTT as IST wall-clock seconds (tests the time-base auto-detect).
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import random
import struct
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))
from oct8_data import FIFTEEN_MIN, FIVE_MIN, rows  # noqa: E402

import uvicorn  # noqa: E402
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
SIM_START = float(os.environ.get("ICT_SIM_START", "0") or 0)
SIM_REAL0 = float(os.environ.get("ICT_SIM_REAL0", "0") or time.time())
SPEED = float(os.environ.get("ICT_SIM_SPEED", "1") or 1)
DROP_AFTER = float(os.environ.get("FAKE_DROP_AFTER_S", "0") or 0)
LTT_MODE = os.environ.get("FAKE_LTT_MODE", "epoch")
PREV_CLOSE = 22216.2
VIX0 = 11.8


def sim_now() -> float:
    if not SIM_START:
        return time.time()
    return SIM_START + (time.time() - SIM_REAL0) * SPEED


# ── build the 1-minute path ──────────────────────────────────────────────────
def split(c, minutes, rng):
    """One candle → `minutes` 1m candles whose aggregate is EXACTLY c. Path: open → first extreme → second
    extreme → close (green candles visit the low first), with noise so 1m swings look like a real tape."""
    o, h, l, cl = c["o"], c["h"], c["l"], c["c"]
    if minutes == 1:
        return [dict(c)]
    if h - l < 0.3:                                       # flat (feed-gap) candle stays flat
        return [{"ts": c["ts"] + 60 * k, "o": o, "h": h, "l": l, "c": cl, "v": 0.0} for k in range(minutes)]
    first, second = (l, h) if cl >= o else (h, l)
    a = rng.randint(0, max(0, minutes // 2 - 1))
    b = rng.randint(a + 1, minutes - 1)
    way = [(0, o), (a, first), (b, second), (minutes - 1, cl)]
    rngp = max(h - l, 0.5)
    closes = []
    for k in range(minutes):
        seg = max(i for i in range(len(way)) if way[i][0] <= k)
        k0, p0 = way[seg]
        k1, p1 = way[min(seg + 1, len(way) - 1)]
        f = 0.0 if k1 == k0 else (k - k0) / (k1 - k0)
        x = p0 + (p1 - p0) * f + rng.gauss(0, 0.15 * rngp)
        closes.append(min(h - 0.1, max(l + 0.1, x)))
    closes[-1] = cl
    out, prev = [], o
    for k in range(minutes):
        ck = closes[k]
        hi_ = min(h - 0.05, max(prev, ck) + abs(rng.gauss(0, 0.06 * rngp)))
        lo_ = max(l + 0.05, min(prev, ck) - abs(rng.gauss(0, 0.06 * rngp)))
        out.append({"ts": c["ts"] + 60 * k, "o": prev, "h": max(hi_, prev, ck), "l": min(lo_, prev, ck), "c": ck,
                    "v": 0.0})
        prev = ck
    for k, ext in ((a, first), (b, second)):
        if ext == h:
            out[k]["h"] = h
        else:
            out[k]["l"] = l
    return out


def build_path():
    rng = random.Random(8)
    m15 = [r for r in rows(FIFTEEN_MIN) if datetime.fromtimestamp(r["ts"], IST).strftime("%H:%M") < "13:00"]
    five = rows(FIVE_MIN)
    day = []
    for c in m15:
        day += split(c, 15, rng)
    t13 = int(datetime(2026, 10, 8, 13, 0, tzinfo=IST).timestamp())
    bridge = [{"ts": t13, "o": 22267.0, "h": 22293.4, "l": 22266.5, "c": 22281.0},
              {"ts": t13 + 300, "o": 22281.0, "h": 22288.0, "l": 22276.0, "c": 22283.0}]
    for c in bridge + five:
        day += split(c, 5, rng)
    # synthetic previous day ending at PREV_CLOSE
    prev, p = [], 22300.0
    t7 = int(datetime(2026, 10, 7, 9, 15, tzinfo=IST).timestamp())
    for m in range(375):
        drift = (PREV_CLOSE - p) / max(1, 375 - m)
        np_ = p + drift + rng.gauss(0, 3.2)
        if m == 374:
            np_ = PREV_CLOSE
        prev.append({"ts": t7 + 60 * m, "o": p, "h": max(p, np_) + abs(rng.gauss(0, 1.5)),
                     "l": min(p, np_) - abs(rng.gauss(0, 1.5)), "c": np_, "v": 0.0})
        p = np_
    return prev + day


PATH = build_path()
BY_TS = {c["ts"]: c for c in PATH}

# ── synthetic NIFTY futures: Oct (front, carries the volume) and Nov (thin) ─────────────────────────────────
FUTS = {35001: ("NIFTY-Oct2026-FUT", "2026-10-27", 82.0, 1.0), 35002: ("NIFTY-Nov2026-FUT", "2026-11-24", 205.0, 0.08)}
SCRIP = ("SEM_EXM_EXCH_ID,SEM_SEGMENT,SEM_SMST_SECURITY_ID,SEM_INSTRUMENT_NAME,SEM_TRADING_SYMBOL,SEM_EXPIRY_DATE\n"
         "NSE,D,35003,FUTIDX,BANKNIFTY-Oct2026-FUT,2026-10-27 14:30:00\n"
         + "".join(f"NSE,D,{k},FUTIDX,{v[0]},{v[1]} 14:30:00\n" for k, v in FUTS.items())
         + "NSE,D,40100,OPTIDX,NIFTY-Oct2026-22000-CE,2026-10-13 14:30:00\n")


def basis_at(sid: int, t: float) -> float:
    return FUTS[sid][2] - 0.0004 * ((t - 1791344700) / 60) + 1.5 * math.sin(t / 700.0)


def fut_vol(sid: int, c) -> float:
    """U-shaped day (busy open and close) × how much the minute moved, deterministic."""
    d0 = datetime.fromtimestamp(c["ts"], IST).replace(hour=9, minute=15, second=0).timestamp()
    m = (c["ts"] - d0) / 60
    shape = 1 + 1.6 * math.exp(-m / 25) + 0.9 * math.exp(-(374 - m) / 20)
    move = abs(c["c"] - c["o"]) / max(1.0, c["h"] - c["l"])
    seed = random.Random(int(c["ts"]) // 60)
    return float(int(9000 * shape * (0.6 + 2.5 * move) * (0.7 + 0.6 * seed.random()) * FUTS[sid][3]))


def fut_cum(sid: int, t: float) -> int:
    """Cumulative day volume up to t (the current minute pro-rata)."""
    d0 = datetime.fromtimestamp(t, IST).replace(hour=9, minute=15, second=0).timestamp()
    tot = 0.0
    for c in PATH:
        if d0 <= c["ts"] < t:
            v = fut_vol(sid, c)
            tot += v if c["ts"] + 60 <= t else v * (t - c["ts"]) / 60
    return int(tot)




def price_at(t: float) -> float:
    m = int(t // 60 * 60)
    c = BY_TS.get(m)
    if c is None:
        past = [x for x in PATH if x["ts"] <= t]
        return past[-1]["c"] if past else PATH[0]["o"]
    f = (t - m) / 60.0
    # walk o → (h or l) → (l or h) → c inside the minute
    seq = [c["o"], c["l"], c["h"], c["c"]] if c["c"] >= c["o"] else [c["o"], c["h"], c["l"], c["c"]]
    x = f * 3
    k = min(2, int(x))
    return seq[k] + (seq[k + 1] - seq[k]) * (x - k)


# ── REST ─────────────────────────────────────────────────────────────────────
app = FastAPI()
calls = {"chain_last": 0.0}


def _auth(req: Request):
    if not req.headers.get("access-token") or not req.headers.get("client-id"):
        return JSONResponse({"errorType": "Invalid_Authentication", "errorCode": "DH-901",
                             "errorMessage": "Client ID or user generated access token is invalid or expired."}, 401)
    if req.headers.get("access-token") == "expired":
        return JSONResponse({"errorType": "Invalid_Authentication", "errorCode": "DH-901",
                             "errorMessage": "access token expired"}, 401)
    return None


@app.post("/v2/charts/intraday")
async def intraday(req: Request):
    bad = _auth(req)
    if bad:
        return bad
    body = await req.json()
    iv = int(body.get("interval", 1))
    now = sim_now()
    frm = datetime.strptime(body["fromDate"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST).timestamp()
    to = datetime.strptime(body["toDate"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST).timestamp()
    src = [c for c in PATH if frm <= c["ts"] <= to and c["ts"] + 60 <= now]
    sid = int(float(body.get("securityId", 13)))
    if body.get("exchangeSegment") == "NSE_FNO" and sid in FUTS:
        src = [{"ts": c["ts"], "o": c["o"] + basis_at(sid, c["ts"]), "h": c["h"] + basis_at(sid, c["ts"]),
                "l": c["l"] + basis_at(sid, c["ts"]), "c": c["c"] + basis_at(sid, c["ts"]), "v": fut_vol(sid, c)}
               for c in src]
    if body.get("exchangeSegment") == "NSE_FNO" and sid >= 40000:   # synthetic option candles (Black-Scholes on the path)
        call = sid < 70000                                  # CE ids = 40000 + K (~6xxxx), PE ids = 50000 + K (~7xxxx)
        K = sid - (40000 if call else 50000)
        ed = datetime(2026, 10, 13, 15, 30, tzinfo=IST).timestamp()
        skew = lambda S: 0.125 + 0.00004 * max(0, 22300 - K) + 0.00001 * abs(K - S) / 10 + (0 if call else 0.01)  # noqa: E731
        prem = lambda S, t: _bs(S, K, max(1e-4, (ed - t) / (365 * 86400)), skew(S), call)[0]                     # noqa: E731
        opt = []
        for c in src:
            ps = [prem(c[k], c["ts"] + 30) for k in ("o", "h", "l", "c")]
            seed = (c["ts"] // 60 + K) % 97
            opt.append({"ts": c["ts"], "o": ps[0], "h": max(ps), "l": min(ps), "c": ps[3],
                        "v": int(65 * (40 + seed + 900 * abs(c["c"] - c["o"]) / max(1.0, c["h"] - c["l"] + 1)))})
        src = opt
    out = {"open": [], "high": [], "low": [], "close": [], "volume": [], "timestamp": []}
    buckets = {}
    for c in src:
        d0 = datetime.fromtimestamp(c["ts"], IST).replace(hour=9, minute=15, second=0).timestamp()
        b = int(d0 + ((c["ts"] - d0) // (iv * 60)) * iv * 60)
        x = buckets.get(b)
        if x is None:
            buckets[b] = dict(c, ts=b)
        else:
            x["h"], x["l"], x["c"] = max(x["h"], c["h"]), min(x["l"], c["l"]), c["c"]
            x["v"] = x.get("v", 0) + c.get("v", 0)
    for b in sorted(buckets):
        x = buckets[b]
        for k, kk in (("open", "o"), ("high", "h"), ("low", "l"), ("close", "c")):
            out[k].append(round(x[kk], 2))
        out["volume"].append(int(x.get("v", 0)))
        out["timestamp"].append(b)
    return out


@app.post("/v2/optionchain/expirylist")
async def expirylist(req: Request):
    bad = _auth(req)
    return bad or {"data": ["2026-10-13", "2026-10-20", "2026-10-27"], "status": "success"}


def _ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _bs(S, K, T, iv, call):
    if T <= 0:
        return max(0.0, S - K) if call else max(0.0, K - S), (1.0 if (call and S > K) else 0.0), 0, 0, 0
    d1 = (math.log(S / K) + 0.5 * iv * iv * T) / (iv * math.sqrt(T))
    d2 = d1 - iv * math.sqrt(T)
    pdf = math.exp(-d1 * d1 / 2) / math.sqrt(2 * math.pi)
    if call:
        price, delta = S * _ncdf(d1) - K * _ncdf(d2), _ncdf(d1)
    else:
        price, delta = K * _ncdf(-d2) - S * _ncdf(-d1), _ncdf(d1) - 1
    gamma = pdf / (S * iv * math.sqrt(T))
    vega = S * pdf * math.sqrt(T) / 100
    theta = -(S * pdf * iv) / (2 * math.sqrt(T)) / 365
    return price, delta, gamma, theta, vega


@app.post("/v2/optionchain")
async def optionchain(req: Request):
    bad = _auth(req)
    if bad:
        return bad
    body = await req.json()
    if time.time() - calls["chain_last"] < 3.0 / max(1.0, SPEED) * 0.9:
        pass                                                # (the real limit is 1 unique request / 3 s)
    calls["chain_last"] = time.time()
    exp = body.get("Expiry", "2026-10-13")
    now = sim_now()
    S = price_at(now)
    ed = datetime.strptime(exp, "%Y-%m-%d").replace(hour=15, minute=30, tzinfo=IST).timestamp()
    T = max(1e-4, (ed - now) / (365 * 86400))
    progress = min(1.0, max(0.0, (now - datetime(2026, 10, 8, 9, 15, tzinfo=IST).timestamp()) / 22500))
    oc = {}
    for K in range(21600, 22901, 50):
        skew = 0.125 + 0.00004 * max(0, 22300 - K) + 0.00001 * abs(K - S) / 10
        ce_p, ce_d, g, ce_t, vg = _bs(S, K, T, skew, True)
        pe_p, pe_d, _, pe_t, _ = _bs(S, K, T, skew, False)
        base = 1_800_000 * math.exp(-((K - S) / 260) ** 2)
        ce_oi = base * (1.0 + (2.2 if K == 22500 else 0) + (0.9 if K == 22300 else 0)) * (1 + 0.6 * progress * (K >= 22250))
        pe_oi = base * (1.0 + (2.0 if K == 22200 else 0) + (0.7 if K == 22000 else 0)) * (1 - 0.25 * progress * (K >= 22250))
        oc[f"{K:.6f}"] = {
            "ce": {"last_price": round(ce_p, 2), "previous_close_price": round(ce_p * (1.6 if K >= 22250 else 1.25), 2),
                   "oi": int(ce_oi), "previous_oi": int(ce_oi / (1 + 0.6 * progress * (K >= 22250) + 0.15)),
                   "volume": int(ce_oi * 3), "implied_volatility": round(100 * skew, 2),
                   "greeks": {"delta": round(ce_d, 4), "gamma": round(g, 6), "theta": round(ce_t, 2), "vega": round(vg, 2)},
                   "top_bid_price": round(ce_p - 0.3, 2), "top_ask_price": round(ce_p + 0.3, 2), "security_id": 40000 + K},
            "pe": {"last_price": round(pe_p, 2), "previous_close_price": round(pe_p * 0.7, 2),
                   "oi": int(pe_oi), "previous_oi": int(pe_oi * 1.1), "volume": int(pe_oi * 3),
                   "implied_volatility": round(100 * (skew + 0.01), 2),
                   "greeks": {"delta": round(pe_d, 4), "gamma": round(g, 6), "theta": round(pe_t, 2), "vega": round(vg, 2)},
                   "top_bid_price": round(pe_p - 0.3, 2), "top_ask_price": round(pe_p + 0.3, 2), "security_id": 50000 + K},
        }
    return {"data": {"last_price": round(S, 2), "oc": oc}, "status": "success"}


@app.get("/scrip.csv")
async def scrip():
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(SCRIP)


@app.post("/v2/marketfeed/ltp")
async def ltp(req: Request):
    bad = _auth(req)
    if bad:
        return bad
    now = sim_now()
    return {"data": {"IDX_I": {"13": {"last_price": round(price_at(now), 2)}, "21": {"last_price": vix_at(now)}}},
            "status": "success"}


@app.get("/v2/profile")
async def profile(req: Request):
    bad = _auth(req)
    return bad or {"dhanClientId": "TEST", "tokenValidity": "09/10/2026 08:30", "dataPlan": "Active"}


@app.get("/v2/RenewToken")
async def renew(req: Request):
    bad = _auth(req)
    return bad or {"accessToken": "renewed-test-token"}


def vix_at(t: float) -> float:
    return round(VIX0 + 0.4 * math.sin(t / 900.0), 2)


# ── WebSocket feed ───────────────────────────────────────────────────────────
def pkt_ticker(sid: int, ltp: float, ltt: int) -> bytes:
    return struct.pack("<BHBI", 2, 16, 0, sid) + struct.pack("<fi", ltp, ltt)


def pkt_full(sid: int, ltp: float, ltt: int, vol: int, bid: float, ask: float) -> bytes:
    body = struct.pack("<fhifiiiiiiffff", ltp, 75, ltt, ltp, vol, 0, 0, 0, 0, 0, ltp, ltp, ltp, ltp)
    depth = b"".join(struct.pack("<iihhff", 750, 900, 5, 6, bid - 0.05 * k, ask + 0.05 * k) for k in range(5))
    return struct.pack("<BHBI", 8, 162, 2, sid) + body + depth


def pkt_prev(sid: int, pc: float) -> bytes:
    return struct.pack("<BHBI", 6, 16, 0, sid) + struct.pack("<fi", pc, 0)


@app.websocket("/")
async def feed(ws: WebSocket):
    q = ws.query_params
    if q.get("version") != "2" or not q.get("token") or not q.get("clientId") or q.get("authType") != "2":
        await ws.close(code=1008)
        return
    await ws.accept()
    if q.get("token") == "expired":
        await ws.send_bytes(struct.pack("<BHBI", 50, 10, 0, 0) + struct.pack("<h", 807))
        await ws.close()
        return
    t_open = time.time()
    subs = {"tick": [], "full": set()}

    async def reader():
        while True:
            sub = json.loads(await ws.receive_text())
            ids = [int(x["SecurityId"]) for x in sub.get("InstrumentList", [])]
            rc = sub.get("RequestCode")
            if rc == 15:
                subs["tick"] += [i for i in ids if i not in subs["tick"]]
                for sid in ids:
                    await ws.send_bytes(pkt_prev(sid, PREV_CLOSE if sid == 13 else 11.5))
            elif rc == 21:
                subs["full"] |= set(ids)
            elif rc == 22:
                subs["full"] -= set(ids)

    rd = asyncio.create_task(reader())
    last = {}
    try:
        while True:
            if rd.done():
                return
            now = sim_now()
            ltt = int(now) + (19800 if LTT_MODE == "ist" else 0)
            frames = b""
            for sid in subs["tick"]:
                px = price_at(now) if sid == 13 else vix_at(now)
                frames += pkt_ticker(sid, px, ltt)
            for sid in subs["full"]:
                if sid not in FUTS:
                    continue
                mid = price_at(now) + basis_at(sid, now)
                up = mid >= last.get(sid, mid)
                last[sid] = mid
                bid = math.floor(mid * 20) / 20 - 0.05
                ask = bid + 0.15
                frames += pkt_full(sid, ask if up else bid, ltt, fut_cum(sid, now), bid, ask)
            if frames:
                await ws.send_bytes(frames)                  # several packets in one message, like Dhan
            await asyncio.sleep(0.25)
            if DROP_AFTER and time.time() - t_open > DROP_AFTER:
                rd.cancel()
                await ws.close()
                return
    except (WebSocketDisconnect, RuntimeError):
        return


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("FAKE_PORT", "8701")), log_level="warning")
