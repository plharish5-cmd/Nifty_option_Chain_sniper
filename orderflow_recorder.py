"""
orderflow_recorder.py — records REAL order flow for the NIFTY 50 index from Dhan's live market feed.

Why the 50 stocks and not the index itself
  The NIFTY 50 index is a calculated number — nobody trades it, so it has no volume, no bid/ask and no
  buyers or sellers. The money that moves NIFTY is traded in its 50 constituent stocks. This recorder
  listens to all 50 in the cash market (NSE_EQ, Dhan "Full" packets with 5-level depth) plus the index
  price itself, and labels every traded quantity as an aggressive BUY or SELL:

      traded at/above the previous best ASK  → buyer lifted the offer  → BUY  (+1)
      traded at/below the previous best BID  → seller hit the bid      → SELL (−1)
      in between                             → compare with the previous price (up = BUY, down = SELL,
                                               unchanged = same side as last time)

  Each row also stores the NIFTY index price at that moment, so nifty_orderflow.py can build delta,
  cumulative delta, a footprint and a volume profile ON THE NIFTY 50 INDEX PRICE.

Honest limits
  • Dhan's feed sends snapshots, not every single exchange trade — volume between two packets is
    labelled as one lot. Good for delta/CVD/imbalances at 1–5 min; not a perfect tape.
  • NSE does not publish which side started a trade. Every Indian order-flow platform infers it the
    same way (quote rule + tick rule), including the commercial terminals.
  • History cannot be downloaded — order flow exists only for days this recorder was running.

Run (from the OC folder, needs DHAN_ACCESS_TOKEN + DHAN_CLIENT_ID in .env and the Dhan Data API plan):
  .venv/bin/python orderflow_recorder.py --diagnose 20   # connect, print 20 decoded packets, exit
  .venv/bin/python orderflow_recorder.py                 # record today 09:08–15:35, then exit
  .venv/bin/python orderflow_recorder.py --now           # record immediately (ignores the time window)
  Background every weekday: see nifty-orderflow.service (same style as nifty-scanner.service).

Output (data/orderflow/):
  ticks/YYYY-MM-DD.csv   ts,sid,ltp,qty,side,bid,ask,idx      (one row per traded-quantity change)
  book/YYYY-MM-DD.csv    ts,sid,ltp,tbq,tsq                     (resting buy/sell quantity, every 60 s)
  index/YYYY-MM-DD.csv   ts,ltp                                 (NIFTY 50 index price)
  constituents.json      security id → symbol map used today
  recorder_heartbeat.json  status read by the Order Flow tab
Information only — not financial advice.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import signal
import struct
import sys
import threading
import time
from datetime import datetime, timedelta, time as dtime
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
OF_DIR = ROOT / "data" / "orderflow"
RULES_PATH = ROOT / "orderflow_rules.json"

SEG_CODE = {0: "IDX_I", 1: "NSE_EQ", 2: "NSE_FNO", 3: "NSE_CURRENCY", 4: "BSE_EQ", 5: "MCX_COMM",
            7: "BSE_CURRENCY", 8: "BSE_FNO"}


def rules() -> dict:
    try:
        return json.loads(RULES_PATH.read_text())
    except Exception:
        return {}


def _hm(s: str) -> dtime:
    h, m = map(int, s.split(":"))
    return dtime(h, m)


# ═══════════════════════════════════════════════════════════════════════════
# Binary packet decoding (Dhan Live Market Feed v2, little-endian)
#   header: <B code><H length><B segment><I security id>   = 8 bytes
# ═══════════════════════════════════════════════════════════════════════════
HDR = struct.Struct("<BHBI")
TICKER = struct.Struct("<fI")                       # ltp, ltt
QUOTE = struct.Struct("<fhIfIIIffff")               # ltp, ltq, ltt, atp, vol, tsq, tbq, o, c, h, l
FULL = struct.Struct("<fhIfIIIIIIffff")             # … + oi, oi_hi, oi_lo before o,c,h,l
DEPTH = struct.Struct("<IIhhff")                    # bid qty, ask qty, bid orders, ask orders, bid px, ask px


def decode(buf: bytes) -> list[dict]:
    """A websocket message may carry several packets back to back."""
    out, i, n = [], 0, len(buf)
    while i + HDR.size <= n:
        code, length, seg, sid = HDR.unpack_from(buf, i)
        if length <= 0 or i + length > n:
            length = n - i                           # trust the frame if the length field is odd
        body = i + HDR.size
        p = {"code": code, "seg": SEG_CODE.get(seg, str(seg)), "sid": str(sid)}
        try:
            if code == 2 and body + TICKER.size <= n:
                p["ltp"], p["ltt"] = TICKER.unpack_from(buf, body)
            elif code == 4 and body + QUOTE.size <= n:
                (p["ltp"], p["ltq"], p["ltt"], p["atp"], p["vol"], p["tsq"], p["tbq"],
                 p["o"], p["c"], p["h"], p["l"]) = QUOTE.unpack_from(buf, body)
            elif code == 8 and body + FULL.size <= n:
                (p["ltp"], p["ltq"], p["ltt"], p["atp"], p["vol"], p["tsq"], p["tbq"], _oi, _oh, _ol,
                 p["o"], p["c"], p["h"], p["l"]) = FULL.unpack_from(buf, body)
                d0 = body + FULL.size
                if d0 + DEPTH.size <= n:
                    bq, aq, _bo, _ao, bp, ap = DEPTH.unpack_from(buf, d0)
                    p["bid"], p["ask"], p["bidq"], p["askq"] = bp, ap, bq, aq
            elif code == 6 and body + 8 <= n:
                p["prev_close"], _ = struct.unpack_from("<fI", buf, body)
            elif code == 50 and body + 2 <= n:
                p["disconnect"] = struct.unpack_from("<h", buf, body)[0]
        except struct.error:
            pass
        out.append(p)
        i += max(length, HDR.size)
    return out


# ═══════════════════════════════════════════════════════════════════════════
# Constituents → Dhan security ids
# ═══════════════════════════════════════════════════════════════════════════
def _download(url: str, timeout: int = 20) -> bytes | None:
    try:
        import requests
        r = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200 and len(r.content) > 200:
            return r.content
    except Exception as e:                                  # noqa: BLE001
        print(f"  ! download failed {url}: {e}")
    return None


def constituents(r: dict) -> list[str]:
    cache = OF_DIR / "nifty50_constituents.csv"
    raw = _download(r.get("constituents_url", ""))
    if raw:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(raw)
    if cache.exists():
        try:
            rows = list(csv.DictReader(io.StringIO(cache.read_text(errors="ignore"))))
            col = next((c for c in rows[0] if c.strip().lower() == "symbol"), None) if rows else None
            syms = [x[col].strip() for x in rows if col and x.get(col)]
            if len(syms) >= 45:
                return syms
        except Exception:
            pass
    print("  ! using fallback constituent list from orderflow_rules.json")
    return list(r.get("fallback_constituents", []))


def security_ids(symbols: list[str], r: dict) -> dict[str, str]:
    """symbol → Dhan NSE_EQ security id, from Dhan's public scrip master (cached a week)."""
    cache = OF_DIR / "dhan_scrip_master.csv"
    age_ok = cache.exists() and (time.time() - cache.stat().st_mtime) < r.get("scrip_master_max_age_days", 7) * 86400
    if not age_ok:
        raw = _download(r.get("scrip_master_url", ""), timeout=60)
        if raw:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_bytes(raw)
    if not cache.exists():
        sys.exit("Could not download Dhan's scrip master (needed to map symbols to security ids). Check internet and retry.")
    want, out = set(symbols), {}
    with cache.open(newline="", errors="ignore") as f:
        rd = csv.DictReader(f)
        cols = {c.strip().upper(): c for c in (rd.fieldnames or [])}

        def col(*names):
            return next((cols[n] for n in names if n in cols), None)
        c_ex, c_seg = col("SEM_EXM_EXCH_ID", "EXCH_ID"), col("SEM_SEGMENT", "SEGMENT")
        c_id, c_sym = col("SEM_SMST_SECURITY_ID", "SECURITY_ID"), col("SEM_TRADING_SYMBOL", "SYMBOL_NAME", "TRADING_SYMBOL")
        c_ser, c_ins = col("SEM_SERIES", "SERIES"), col("SEM_INSTRUMENT_NAME", "INSTRUMENT")
        for row in rd:
            if c_ex and row.get(c_ex, "").strip().upper() != "NSE":
                continue
            if c_seg and row.get(c_seg, "").strip().upper() not in ("E", "EQ", "NSE_EQ"):
                continue
            if c_ser and row.get(c_ser, "").strip().upper() not in ("EQ", ""):
                continue
            if c_ins and row.get(c_ins, "").strip().upper() not in ("EQUITY", ""):
                continue
            s = row.get(c_sym, "").strip().upper()
            if s in want and s not in out:
                out[s] = row.get(c_id, "").strip()
    missing = sorted(want - set(out))
    if missing:
        print(f"  ! {len(missing)} constituent(s) not found in Dhan scrip master: {', '.join(missing)}")
    return out


# ═══════════════════════════════════════════════════════════════════════════
# Recorder
# ═══════════════════════════════════════════════════════════════════════════
class Recorder:
    def __init__(self, sym2id: dict[str, str], r: dict, day: str, diagnose: int = 0, url: str | None = None):
        self.r, self.day, self.diagnose = r, day, diagnose
        self.id2sym = {v: k for k, v in sym2id.items()}
        self.idx_id = str(r.get("index_security_id", "13"))
        self.url = url
        self.state: dict[str, dict] = {}
        self.idx_ltp = 0.0
        self.idx_seen = 0.0
        self.idx_source = "feed"
        self.tick_buf, self.book_buf, self.idx_buf = [], [], []
        self.lock = threading.Lock()
        self.n_packets = self.n_trades = 0
        self.seen: set[str] = set()
        self.last_book = 0.0
        self.last_flush = time.time()
        self.stop_flag = False
        self.ws = None
        self.status = "starting"
        self.err = ""
        for sub in ("ticks", "book", "index"):
            (OF_DIR / sub).mkdir(parents=True, exist_ok=True)
        (OF_DIR / "constituents.json").write_text(json.dumps({"day": day, "map": self.id2sym}, indent=1))

    # ── websocket callbacks ──────────────────────────────────────────────
    def on_open(self, ws):
        self.status = "connected"
        ids = list(self.id2sym)
        rc_s, rc_i = int(self.r.get("stock_request_code", 21)), int(self.r.get("index_request_code", 15))
        seg_s, seg_i = self.r.get("stock_segment", "NSE_EQ"), self.r.get("index_segment", "IDX_I")
        for k in range(0, len(ids), 100):
            chunk = ids[k:k + 100]
            ws.send(json.dumps({"RequestCode": rc_s, "InstrumentCount": len(chunk),
                                "InstrumentList": [{"ExchangeSegment": seg_s, "SecurityId": s} for s in chunk]}))
        ws.send(json.dumps({"RequestCode": rc_i, "InstrumentCount": 1,
                            "InstrumentList": [{"ExchangeSegment": seg_i, "SecurityId": self.idx_id}]}))
        print(f"  subscribed {len(ids)} stocks (code {rc_s}) + NIFTY index (code {rc_i})")

    def on_message(self, ws, msg):
        if isinstance(msg, str):
            if self.diagnose:
                print("  text:", msg[:200])
            return
        now = time.time()
        for p in decode(msg):
            self.n_packets += 1
            if self.diagnose:
                nm = "NIFTY 50" if p["sid"] == self.idx_id and p["seg"] == "IDX_I" else self.id2sym.get(p["sid"], p["sid"])
                print(f"  {datetime.now():%H:%M:%S} code={p['code']} {p['seg']}:{nm} "
                      + " ".join(f"{k}={v:.2f}" if isinstance(v, float) else f"{k}={v}"
                                 for k, v in p.items() if k not in ("code", "seg", "sid")))
                if p["seg"] == "IDX_I":
                    self.idx_diag = True
                if self.n_packets >= self.diagnose and (getattr(self, "idx_diag", False) or self.n_packets >= 5 * self.diagnose):
                    if not getattr(self, "idx_diag", False):
                        print("  ! no NIFTY 50 index packet yet — normal when the market is closed; during market hours "
                              "the recorder also falls back to REST for the index price.")
                    self.stop_flag = True
                    ws.close()
                    return
            if p.get("disconnect"):
                self.err = f"Dhan disconnect code {p['disconnect']}"
                print("  !", self.err)
            if p["seg"] == "IDX_I" and p["sid"] == self.idx_id and p.get("ltp"):
                self.idx_ltp = float(p["ltp"])
                self.idx_seen = now
                with self.lock:
                    self.idx_buf.append((round(now, 3), round(self.idx_ltp, 2)))
                continue
            if p["code"] not in (4, 8) or "vol" not in p:
                continue
            self._on_trade_packet(p, now)
        if now - self.last_flush >= self.r.get("flush_seconds", 5):
            self.flush()

    def _on_trade_packet(self, p: dict, now: float):
        sid = p["sid"]
        self.seen.add(sid)
        st = self.state.get(sid)
        ltp, vol = float(p["ltp"]), int(p["vol"])
        bid, ask = float(p.get("bid") or 0), float(p.get("ask") or 0)
        if st is None:                                   # first packet: baseline only, no trade inferred
            self.state[sid] = dict(vol=vol, ltp=ltp, bid=bid, ask=ask, side=0, tbq=p.get("tbq", 0), tsq=p.get("tsq", 0))
            return
        dq = vol - st["vol"]
        if dq > 0 and self.idx_ltp > 0:
            pb, pa = st["bid"], st["ask"]
            if pa > 0 and ltp >= pa:
                side = 1
            elif pb > 0 and ltp <= pb:
                side = -1
            elif ltp > st["ltp"]:
                side = 1
            elif ltp < st["ltp"]:
                side = -1
            else:
                side = st["side"]
            st["side"] = side
            self.n_trades += 1
            with self.lock:
                self.tick_buf.append((round(now, 3), sid, round(ltp, 2), dq, side, round(pb, 2), round(pa, 2),
                                      round(self.idx_ltp, 2)))
        if dq >= 0:
            st["vol"] = vol
        st["ltp"], st["tbq"], st["tsq"] = ltp, p.get("tbq", 0), p.get("tsq", 0)
        st["bid"], st["ask"] = bid, ask          # 0 = that side is empty → quote rule skipped, tick rule used

    def on_error(self, ws, err):
        self.err = str(err)[:300]
        print("  ! websocket error:", self.err)

    def on_close(self, ws, code, reason):
        self.status = "closed"
        print(f"  websocket closed ({code} {reason or ''})")

    # ── files ────────────────────────────────────────────────────────────
    def _append(self, sub: str, header: str, rows: list):
        if not rows:
            return
        f = OF_DIR / sub / f"{self.day}.csv"
        new = not f.exists()
        with f.open("a", newline="") as fh:
            w = csv.writer(fh)
            if new:
                fh.write(header + "\n")
            w.writerows(rows)

    def flush(self):
        now = time.time()
        if now - self.last_book >= self.r.get("book_snapshot_seconds", 60):
            self.last_book = now
            with self.lock:
                self.book_buf += [(round(now, 3), sid, round(s["ltp"], 2), s["tbq"], s["tsq"]) for sid, s in self.state.items()]
        with self.lock:
            t, b, x = self.tick_buf, self.book_buf, self.idx_buf
            self.tick_buf, self.book_buf, self.idx_buf = [], [], []
        if not self.diagnose:
            self._append("ticks", "ts,sid,ltp,qty,side,bid,ask,idx", t)
            self._append("book", "ts,sid,ltp,tbq,tsq", b)
            self._append("index", "ts,ltp", x)
        self.last_flush = now
        self.heartbeat()

    def heartbeat(self):
        hb = dict(updated=datetime.now().isoformat(timespec="seconds"), day=self.day, status=self.status,
                  packets=self.n_packets, trades=self.n_trades, stocks_streaming=len(self.seen),
                  stocks_subscribed=len(self.id2sym), index_ltp=self.idx_ltp, index_source=self.idx_source,
                  error=self.err, pid=os.getpid())
        try:
            (OF_DIR / "recorder_heartbeat.json").write_text(json.dumps(hb, indent=1))
        except Exception:
            pass

    # ── index price safety net ───────────────────────────────────────────
    def _index_poller(self):
        """If the websocket stops sending NIFTY 50 ticks for >10 s, fetch the index LTP over REST every 2 s,
        so stock trades can still be placed on the index price."""
        import dhan_client as dc
        while not self.stop_flag:
            time.sleep(2)
            if self.diagnose or time.time() - self.idx_seen < 10:
                continue
            try:
                r = dc._post("/marketfeed/ltp", {self.r.get("index_segment", "IDX_I"): [int(self.idx_id)]})
                d = (r.get("data") or {}).get(self.r.get("index_segment", "IDX_I"), {}).get(self.idx_id, {})
                px = float(d.get("last_price") or 0)
                if px > 0:
                    self.idx_ltp, self.idx_source = px, "rest"
                    with self.lock:
                        self.idx_buf.append((round(time.time(), 3), round(px, 2)))
            except Exception as e:                           # noqa: BLE001
                self.err = f"index REST fallback: {str(e)[:120]}"

    # ── main loop with reconnect ─────────────────────────────────────────
    def run(self, stop_at: datetime | None):
        import websocket                                    # pip install websocket-client
        import dhan_client as dc
        url = self.url
        if url is None:
            cid = os.environ.get("DHAN_CLIENT_ID", "").strip()
            if not cid:
                sys.exit("DHAN_CLIENT_ID is missing from .env — the live feed needs it (token alone is not enough).")
            url = (f"{self.r.get('ws_url', 'wss://api-feed.dhan.co')}?version={self.r.get('feed_version', '2')}"
                   f"&token={dc.token()}&clientId={cid}&authType={self.r.get('auth_type', '2')}")
        if self.url is None and not self.diagnose:
            threading.Thread(target=self._index_poller, daemon=True).start()
        wait = 2
        while not self.stop_flag:
            if stop_at and datetime.now() >= stop_at:
                break
            ws = websocket.WebSocketApp(url, on_open=self.on_open, on_message=self.on_message,
                                        on_error=self.on_error, on_close=self.on_close)
            self.ws = ws
            timer = None
            if stop_at:
                secs = max(1.0, (stop_at - datetime.now()).total_seconds())
                timer = threading.Timer(secs, lambda: (setattr(self, "stop_flag", True), ws.close()))
                timer.daemon = True
                timer.start()
            t0 = time.time()
            ws.run_forever(ping_interval=20, ping_timeout=10)
            if timer:
                timer.cancel()
            self.flush()
            if self.stop_flag or (stop_at and datetime.now() >= stop_at):
                break
            if "401" in self.err or "403" in self.err or "Invalid" in self.err:
                print("  ! Dhan rejected the connection — check the token in .env (they last 24 h) and the Data API plan.")
            wait = 2 if time.time() - t0 > 120 else min(wait * 2, int(self.r.get("reconnect_max_wait_s", 60)))
            self.status = f"reconnecting in {wait}s"
            self.heartbeat()
            print(f"  reconnecting in {wait}s …")
            time.sleep(wait)
            self.state.clear()                               # volumes re-baseline after a gap
        self.status = "stopped"
        self.flush()


def main():
    import runlog
    runlog.start("orderflow_recorder")
    ap = argparse.ArgumentParser()
    ap.add_argument("--diagnose", type=int, default=0, help="print N decoded packets and exit (nothing saved)")
    ap.add_argument("--now", action="store_true", help="start immediately, ignore the 09:08–15:35 window")
    ap.add_argument("--minutes", type=float, default=0, help="with --now: stop after N minutes")
    ap.add_argument("--url", help=argparse.SUPPRESS)       # test hook: fake feed server
    ap.add_argument("--symbols", help=argparse.SUPPRESS)   # test hook: SYM=ID,SYM=ID
    a = ap.parse_args()
    R = rules().get("recorder", {})
    import dhan_client as dc
    dc._load_env()

    if a.symbols:
        sym2id = dict(x.split("=") for x in a.symbols.split(","))
    else:
        syms = constituents(R)
        sym2id = security_ids([s.upper() for s in syms], R)
    if len(sym2id) < 10:
        sys.exit(f"Only {len(sym2id)} constituents mapped to Dhan ids — refusing to record a partial index.")
    print(f"NIFTY 50 order-flow recorder · {len(sym2id)} stocks mapped")

    now = datetime.now()
    day = now.strftime("%Y-%m-%d")
    start = datetime.combine(now.date(), _hm(R.get("start", "09:08")))
    stop = datetime.combine(now.date(), _hm(R.get("stop", "15:35")))
    if not (a.diagnose or a.now):
        if now.weekday() >= 5:
            print("Weekend — nothing to record.")
            return
        if now >= stop:
            print(f"Market session is over (stop {stop:%H:%M}). Nothing to record today.")
            return
        if now < start:
            print(f"Waiting until {start:%H:%M} …")
            time.sleep((start - now).total_seconds())
    rec = Recorder(sym2id, R, day, diagnose=a.diagnose, url=a.url)

    def _sig(*_):
        rec.stop_flag = True
        print("\n  stopping …")
        if rec.ws is not None:
            rec.ws.close()
    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)
    if a.diagnose:
        stop_at = None
    elif a.now:
        stop_at = datetime.now() + timedelta(minutes=a.minutes) if a.minutes else None
    else:
        stop_at = stop
    rec.run(stop_at)
    print(f"Done · {rec.n_packets:,} packets · {rec.n_trades:,} classified trades · "
          f"{len(rec.seen)}/{len(sym2id)} stocks streamed" + ("" if a.diagnose else f" → {OF_DIR / 'ticks' / (day + '.csv')}"))


if __name__ == "__main__":
    main()
