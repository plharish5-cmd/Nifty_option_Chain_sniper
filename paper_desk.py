"""
paper_desk.py — 📝 PAPER DESK v2: broker-style NIFTY option paper trading, pinned in the sidebar on EVERY tab.

Feels like Dhan / Kite, fills like the exchange:
  ORDER TICKET   tap any price → BUY ticket: lots stepper, MARKET / LIMIT, INTRADAY / CARRY, SL · Target · Trailing SL,
                 live 5-level market depth, value, margin, charges & breakeven, max loss at SL.  AMO when market is shut.
  ★ PICKS        best CE / PE strikes by your Greek weights (Delta › Theta › Gamma › Vega) — one-click BUY.
  ⛓ CHAIN        tap-to-trade option-chain ladder (ATM ± 6), expiry selector (weekly expiries roll automatically:
                 after 15:30 on expiry Tuesday the next week's series is used; new Wednesday strikes appear on their own).
  POSITIONS      netted per instrument like a broker: avg price, LTP, P&L ₹ / %, EXIT · +1 lot · ½ exit · ✎ modify.
  🧾 ORDERS       open (resting LIMIT / AMO / waiting) orders with cancel, and today's order book.

Fill rules (exchange-like, no free lunches):
  BUY MARKET at the best ASK · exit MARKET at the best BID · resting BUY LIMIT fills at its limit once ask ≤ limit
  (fills at the ask at once if already marketable) · TARGET = resting sell limit (fills at the target when bid ≥ it) ·
  STOP-LOSS = SL-M (exits at the bid once bid ≤ stop) · trailing SL follows the highest bid · INTRADAY squared off 15:20 ·
  AMO orders fill at the first live price after 09:15 · CARRY positions left at expiry settle at intrinsic value on the
  expiry-day close · charges = paper_trading.calculate_trade_charges (Dhan tariff).
  No live price → the order WAITS (shown in Orders) — never filled at a made-up price.

Shares the account / trades.csv of the 📒 Paper Trading tab. Settings & order book: data/paper_trading/desk.json.
Information only — not financial advice.
"""
from __future__ import annotations

import json
import math
import time as _time
import uuid
from datetime import date, datetime, time, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

import config
import paper_trading as pt

DESK_FILE = pt.PAPER_DIR / "desk.json"
DEFAULTS = {
    "lots": 1, "sl_pts": 0.0, "tgt_pts": 0.0, "trail_pts": 0.0, "product": "INTRADAY",
    "weights": {"delta": 40, "theta": 30, "gamma": 20, "vega": 10},
    "delta_target": 0.50, "top_n": 3, "window_strikes": 8, "ladder": 6, "min_premium": 5.0,
    "refresh_s": 1, "stale_s": 120, "squareoff": "15:20", "_feed_v1": False,
}
STEP = float(getattr(config, "STRIKE_STEP", 50))
LOT = int(getattr(config, "LOT_SIZE", 65))
OPEN_T, CLOSE_T, EXP_T = time(9, 15), time(15, 30), time(15, 30)
_mem: dict = {"exps": (0.0, []), "chain": {}, "frag": {}, "cc": {}, "qq": {}}
# FEED — background prices so a click never waits for Dhan: chain every 3 s (Dhan limit), position quotes every 1 s
_feed: dict = {"chain": {}, "q": {}, "want_exp": {}, "want_sid": {}, "started": False, "err": "", "beat": 0.0}
CHAIN_EVERY, QUOTE_EVERY = 3.0, 1.0


# ═══════════════════════════════════════════════════════════════════════════
# 1 · state (settings + id map + order book) — data/paper_trading/desk.json
# ═══════════════════════════════════════════════════════════════════════════
def load_state() -> dict:
    try:
        d = json.loads(DESK_FILE.read_text())
    except Exception:                                           # noqa: BLE001
        d = {}
    s = {**DEFAULTS, **{k: v for k, v in d.items() if k in DEFAULTS}}
    s["weights"] = {**DEFAULTS["weights"], **(d.get("weights") or {})}
    s["ids"] = d.get("ids", {})            # position_id → {sid, expiry, hi}
    s["orders"] = d.get("orders", [])
    s["meta"] = d.get("meta", {})          # position_id → {expiry, strike, side, product} — kept for P&L reports
    return s


_RANK = {"OPEN": 0, "FILLING": 1}


class _Lock:
    """Cross-process lock — the dashboard sidebar and the phone page both write desk.json."""
    def __enter__(self):
        self.f = None
        try:
            import fcntl
            self.f = open(DESK_FILE.with_suffix(".lock"), "w")
            fcntl.flock(self.f, fcntl.LOCK_EX)
        except Exception:                                   # noqa: BLE001
            pass
        return self

    def __exit__(self, *a):
        try:
            if self.f:
                import fcntl
                fcntl.flock(self.f, fcntl.LOCK_UN)
                self.f.close()
        except Exception:                                   # noqa: BLE001
            pass


def save_state(s: dict) -> None:
    """MERGE with what is on disk (never overwrite another page's newer orders), then write atomically."""
    import os
    with _Lock():
        try:
            disk = json.loads(DESK_FILE.read_text())
        except Exception:                                   # noqa: BLE001
            disk = {}
        by = {}
        for o in (disk.get("orders") or []) + list(s.get("orders", [])):
            k = o.get("oid") or f"{o.get('ts')}{o.get('side_')}{o.get('strike')}{o.get('side')}"
            old = by.get(k)
            if old is None or _RANK.get(o.get("status"), 2) >= _RANK.get(old.get("status"), 2):
                by[k] = o
        orders = sorted(by.values(), key=lambda o: str(o.get("fill_ts") or o.get("ts") or ""))
        cut = (ist_now() - timedelta(days=3)).isoformat()
        s["orders"] = [o for o in orders if o.get("status") in ("OPEN", "FILLING") or str(o.get("ts", "")) >= cut][-500:]
        try:
            open_ids = {p.position_id for p in pt.load_paper_account().open_positions}
        except Exception:                                   # noqa: BLE001
            open_ids = None
        ids = {**(disk.get("ids") or {}), **s.get("ids", {})}
        s["ids"] = {k: v for k, v in ids.items() if open_ids is None or k in open_ids}
        s["meta"] = {**(disk.get("meta") or {}), **s.get("meta", {})}
        try:
            tmp = DESK_FILE.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(s, indent=1, default=str))
            tmp.replace(DESK_FILE)
        except Exception:                                   # noqa: BLE001
            pass


def _claim(oid: str) -> bool:
    """Mark a resting order FILLING on disk before filling it, so two pages can never both fill it."""
    with _Lock():
        try:
            disk = json.loads(DESK_FILE.read_text())
        except Exception:                                   # noqa: BLE001
            return True
        for o in disk.get("orders") or []:
            if o.get("oid") == oid:
                if o.get("status") != "OPEN":
                    return False
                o["status"] = "FILLING"
                tmp = DESK_FILE.with_suffix(".claim.tmp")
                tmp.write_text(json.dumps(disk, indent=1, default=str))
                tmp.replace(DESK_FILE)
                return True
        return True


settings = load_state                      # v1 name kept


def ist_now() -> datetime:
    return datetime.utcnow() + timedelta(hours=5, minutes=30)


def market_open(now: datetime) -> bool:
    return now.weekday() < 5 and OPEN_T <= now.time() <= CLOSE_T


def _d(e) -> date | None:
    x = pd.to_datetime(str(e), errors="coerce", dayfirst=not str(e)[:4].isdigit())
    return None if pd.isna(x) else x.date()


def expired(e, now: datetime) -> bool:
    d = _d(e)
    return bool(d and (d < now.date() or (d == now.date() and now.time() >= EXP_T)))


def name(strike, side, e) -> str:
    d = _d(e)
    return f"NIFTY {d:%d %b}".upper() + f" {int(strike)} {side}" if d else f"NIFTY {int(strike)} {side}"


# ═══════════════════════════════════════════════════════════════════════════
# 2 · prices
# ═══════════════════════════════════════════════════════════════════════════
def live_expiries(now: datetime) -> list[str]:
    import dhan_client as dc
    t, exps = _mem["exps"]
    if not exps or _time.time() - t > 600:
        try:
            exps = dc.fetch_dhan_expiries() or exps
        except Exception:                                       # noqa: BLE001
            pass
        _mem["exps"] = (_time.time(), exps)
    return sorted([e for e in exps if _d(e) and not expired(e, now)], key=_d)


def _f(x) -> float:
    try:
        v = float(x)
        return v if math.isfinite(v) else np.nan
    except (TypeError, ValueError):
        return np.nan


def parse_chain(data: dict) -> tuple[pd.DataFrame, float]:
    rows = []
    for k, legs in (data.get("oc") or {}).items():
        for side in ("ce", "pe"):
            g = (legs or {}).get(side) or {}
            if not g:
                continue
            gr = g.get("greeks") or {}
            rows.append(dict(strike=round(_f(k)), side=side.upper(), ltp=_f(g.get("last_price")),
                             bid=_f(g.get("top_bid_price")), ask=_f(g.get("top_ask_price")),
                             delta=_f(gr.get("delta")), theta=_f(gr.get("theta")), gamma=_f(gr.get("gamma")),
                             vega=_f(gr.get("vega")), iv=_f(g.get("implied_volatility")),
                             sid=str(g.get("security_id", "")).split(".")[0], oi=_f(g.get("oi")),
                             prev_oi=_f(g.get("previous_oi"))))
    return pd.DataFrame(rows), _f(data.get("last_price"))


def _snapshot_chain() -> dict | None:
    try:
        root = Path(config.SNAPSHOT_DIR)
        day = sorted(p for p in root.iterdir() if p.is_dir())[-1]
        meta = sorted(day.glob("*.meta.json"))[-1]
        info = json.loads(meta.read_text())
        df = pd.read_parquet(meta.with_suffix("").with_suffix(".parquet"))
        rows = []
        for side, p in (("CE", "ce"), ("PE", "pe")):
            g = lambda c: pd.to_numeric(df[c], errors="coerce") if c in df else np.nan
            rows.append(pd.DataFrame({"strike": pd.to_numeric(df["strike"]).round(), "side": side, "ltp": g(f"{p}_ltp"),
                                      "bid": np.nan, "ask": np.nan, "delta": g(f"{p}_delta"), "theta": g(f"{p}_theta"),
                                      "gamma": g(f"{p}_gamma"), "vega": g(f"{p}_vega"), "iv": g(f"{p}_iv"),
                                      "sid": "", "oi": g(f"{p}_oi"), "prev_oi": np.nan}))
        at = datetime.fromisoformat(info["timestamp"]).replace(tzinfo=None)
        exp = str(df["expiry"].iloc[0]) if "expiry" in df else ""
        return dict(rows=pd.concat(rows, ignore_index=True), spot=float(info["spot"]), expiry=exp, at=at,
                    source=f"STALE snapshot {at:%d %b %H:%M}", live=False)
    except Exception:                                           # noqa: BLE001
        return None


def get_chain(expiry: str | None, now: datetime) -> dict | None:
    try:
        import dhan_client as dc
        exp = expiry or (live_expiries(now) or [None])[0]
        data = dc.fetch_dhan_option_chain(exp) if exp else None
        if data:
            rows, spot = parse_chain(data)
            if len(rows):
                c = dict(rows=rows, spot=spot, expiry=exp, at=now, source="Dhan live", live=True, t=_time.time())
                _mem["chain"][exp] = c
                return c
    except Exception:                                           # noqa: BLE001
        pass
    if expiry in _mem["chain"]:
        c = dict(_mem["chain"][expiry])
        c["source"] = f"Dhan {c['at']:%H:%M:%S} · retrying"
        return c
    return _snapshot_chain()


def cached_chain(expiry: str | None = None) -> dict | None:
    """Latest chain from the background feed (no network wait); falls back to a direct fetch on first use."""
    if expiry:
        _feed["want_exp"][expiry] = _time.time()
        got = _feed["chain"].get(expiry)
        if got and _time.time() - got[0] <= 2 * CHAIN_EVERY + 1:
            try:
                fresh = abs((ist_now() - got[1]["at"]).total_seconds()) <= 2 * CHAIN_EVERY + 1
            except Exception:                               # noqa: BLE001
                fresh = False
            if fresh:
                return got[1]
    return _cached_chain_direct(expiry)


def _cached_chain_direct(expiry: str | None = None) -> dict | None:
    """Shared by every browser tab; at most one Dhan option-chain call per expiry per 3 s."""
    import streamlit as st
    if "f" not in _mem["cc"]:
        _mem["cc"]["f"] = st.cache_data(ttl=3, show_spinner=False, max_entries=8)(
            lambda e, bucket: get_chain(e, ist_now()))
    return _mem["cc"]["f"](expiry, int(_time.time() // 3))


def quotes(sids: list[str]) -> dict:
    """{sid: {ltp, bid, ask, depth:{buy:[], sell:[]}}} from /marketfeed/quote (positions in other expiries, ticket depth)."""
    sids = sorted({s for s in sids if s})
    if not sids:
        return {}
    t0 = _time.time()
    for x in sids:
        _feed["want_sid"][x] = t0
    fresh = {x: _feed["q"][x][1] for x in sids if x in _feed["q"] and t0 - _feed["q"][x][0] <= 2.5}
    if len(fresh) == len(sids):
        return fresh
    return _quotes_direct(sids)


def _quotes_direct(sids: list[str], cache: bool = True) -> dict:
    key = (tuple(sids), int(_time.time() // 2))
    if cache and key in _mem["qq"]:
        return _mem["qq"][key]
    out = {}
    try:
        import requests
        import dhan_client as dc
        r = requests.post(f"{dc.BASE}/marketfeed/quote", json={"NSE_FNO": [int(s) for s in sids]},
                          headers=dc._headers(), timeout=5)
        d = (r.json().get("data") or {}).get("NSE_FNO", {}) if r.status_code == 200 else {}
        for s in sids:
            q = d.get(str(s)) or {}
            dp = q.get("depth") or {}
            b = (dp.get("buy") or [{}])[0].get("price")
            a = (dp.get("sell") or [{}])[0].get("price")
            out[s] = dict(ltp=_f(q.get("last_price")), bid=_f(b), ask=_f(a), depth=dp)
    except Exception:                                           # noqa: BLE001
        pass
    _mem["qq"] = {key: out}
    return out


def price(strike, side, expiry, sid, chain: dict | None, q: dict) -> dict | None:
    """{ltp, bid, ask, src}: a feed quote < 1.5 s old first (freshest), then the chain of that expiry, then any quote."""
    sid_ = str(sid or "")
    if not sid_ and chain is not None and str(chain.get("expiry")) == str(expiry):
        r = chain["rows"]
        m = r[(r["strike"] == round(float(strike))) & (r["side"] == side)]
        sid_ = str(m.iloc[0].sid) if len(m) else ""
    if sid_:
        _feed["want_sid"].setdefault(sid_, _time.time())
        fq = _feed["q"].get(sid_)
        if fq and _time.time() - fq[0] <= 1.5 and np.isfinite(fq[1].get("ltp", np.nan)):
            x = fq[1]
            return dict(ltp=x["ltp"], bid=x["bid"], ask=x["ask"], src="quote", sid=sid_)
    if chain is not None and str(chain.get("expiry")) == str(expiry):
        r = chain["rows"]
        m = r[(r["strike"] == round(float(strike))) & (r["side"] == side)]
        if len(m):
            x = m.iloc[0]
            return dict(ltp=float(x.ltp), bid=float(x.bid), ask=float(x.ask), src="chain", sid=str(x.sid))
    if sid and sid in q and np.isfinite(q[sid]["ltp"]):
        return dict(**{k: q[sid][k] for k in ("ltp", "bid", "ask")}, src="quote", sid=sid)
    return None


def _feed_loop() -> None:
    import threading  # noqa: F401
    i = 0
    while True:
        t0 = _time.time()
        try:
            now = ist_now()
            live = market_open(now)
            _feed["beat"] = t0
            if i % int(CHAIN_EVERY / QUOTE_EVERY) == 0:
                exps = [e for e, ts in list(_feed["want_exp"].items()) if t0 - ts < 60] or live_expiries(now)[:1]
                if exps:
                    e = exps[(i // 3) % len(exps)]
                    c = get_chain(e, now)
                    if c and c.get("live"):
                        _feed["chain"][e] = (_time.time(), c)
            sids = [x for x, ts in list(_feed["want_sid"].items()) if t0 - ts < 60]
            if sids and live:
                q = _quotes_direct(sorted(sids), cache=False)
                ts = _time.time()
                for k, v in q.items():
                    if np.isfinite(v.get("ltp", np.nan)):
                        _feed["q"][k] = (ts, v)
            _feed["err"] = ""
        except Exception as exc:                            # noqa: BLE001
            _feed["err"] = str(exc)[:200]
        i += 1
        n_ = ist_now()
        if market_open(n_):
            wait = QUOTE_EVERY
        else:                                               # closed: slow poll, but wake right at 09:15 for AMO fills
            o_ = n_.replace(hour=9, minute=15, second=0, microsecond=0)
            to_open = (o_ - n_).total_seconds()
            wait = min(15.0, to_open) if 0 < to_open else 15.0
        _time.sleep(max(0.05, wait - (_time.time() - t0)))


def ensure_feed() -> None:
    if not _feed["started"]:
        import threading
        _feed["started"] = True
        threading.Thread(target=_feed_loop, name="paper-desk-feed", daemon=True).start()


def _bid(p):
    return p["bid"] if np.isfinite(p["bid"]) and p["bid"] > 0 else p["ltp"]


def _ask(p):
    return p["ask"] if np.isfinite(p["ask"]) and p["ask"] > 0 else p["ltp"]


# ═══════════════════════════════════════════════════════════════════════════
# 3 · strike ranking — Delta › Theta › Gamma › Vega (weights editable)
# ═══════════════════════════════════════════════════════════════════════════
def rank(rows: pd.DataFrame, spot: float, side: str, s: dict) -> pd.DataFrame:
    """Buyer score 0–100: |Δ| near target · low θ/premium · high γ/premium · low ν/premium (vs strikes on screen)."""
    w = s["weights"]
    x = rows[(rows["side"] == side) & (rows["ltp"] >= float(s["min_premium"]))].copy()
    if x.empty or not np.isfinite(spot):
        return x
    atm = round(spot / STEP) * STEP
    x = x[(x["strike"] - atm).abs() <= float(s["window_strikes"]) * STEP].dropna(subset=["delta"])
    x = x[x["delta"].abs() > 0.03]
    if x.empty:
        return x
    x["s_delta"] = (1 - (x["delta"].abs() - float(s["delta_target"])).abs() / 0.35).clip(0, 1)
    pr = lambda v, hi: (v.rank(pct=True) if hi else 1 - v.rank(pct=True) + 1 / len(v)).clip(0, 1)
    x["s_theta"] = pr((x["theta"].abs() / x["ltp"]).fillna(0), False) if x["theta"].notna().any() else 0.5
    x["s_gamma"] = pr((x["gamma"] / x["ltp"]).fillna(0), True) if x["gamma"].notna().any() else 0.5
    x["s_vega"] = pr((x["vega"] / x["ltp"]).fillna(0), False) if x["vega"].notna().any() else 0.5
    tot = sum(max(0.0, float(v)) for v in w.values()) or 1.0
    x["score"] = 100 * (float(w["delta"]) * x["s_delta"] + float(w["theta"]) * x["s_theta"]
                        + float(w["gamma"]) * x["s_gamma"] + float(w["vega"]) * x["s_vega"]) / tot
    x["spread_pct"] = 100 * (x["ask"] - x["bid"]) / x["ltp"]
    return x.sort_values("score", ascending=False).head(int(s["top_n"]))


# ═══════════════════════════════════════════════════════════════════════════
# 4 · order engine
# ═══════════════════════════════════════════════════════════════════════════
def _log(s, **o) -> dict:
    o.setdefault("oid", uuid.uuid4().hex[:8])
    o.setdefault("ts", ist_now().isoformat(timespec="seconds"))
    s["orders"].append(o)
    return o


def _fill_buy(o: dict, px: float, s: dict, spot: float, src: str) -> tuple[bool, str]:
    slip = float(pt.load_paper_config().get("slippage_pts", 0.0) or 0.0)     # engine adds it on BUY → net fill = px
    sl = round(px - float(o.get("sl_pts") or 0), 2) if float(o.get("sl_pts") or 0) > 0 else 0.0
    tg = round(px + float(o.get("tgt_pts") or 0), 2) if float(o.get("tgt_pts") or 0) > 0 else 0.0
    note = f"desk {o.get('otype', 'MARKET')} · fill {src} {px:.2f} · {name(o['strike'], o['side'], o['expiry'])}"
    ok, msg, acc = pt.place_paper_order(strike=float(o["strike"]), opt_type=o["side"], lots=int(o["lots"]),
                                        spot=float(spot or 0), side="BUY", product_type=o.get("product", "INTRADAY"),
                                        stop_loss=sl, target_1=tg, setup_tag="Paper Desk", custom_price=px - slip,
                                        trailing_sl_pts=float(o.get("trail_pts") or 0), notes=note,
                                        override_risk_limit=True)
    if ok:
        pid = acc.open_positions[-1].position_id
        s["ids"][pid] = {"sid": o.get("sid", ""), "expiry": o["expiry"], "hi": px}
        s.setdefault("meta", {})[pid] = {"expiry": str(o["expiry"]), "strike": int(o["strike"]), "side": o["side"],
                                         "product": o.get("product", "INTRADAY")}
        o.update(status="FILLED", fill_px=round(px, 2), fill_ts=ist_now().isoformat(timespec="seconds"), pid=pid,
                 msg=f"Bought {int(o['lots']) * LOT} @ {px:.2f}")
    else:
        o.update(status="REJECTED", msg=msg)
    return ok, (f"✅ BUY {name(o['strike'], o['side'], o['expiry'])} ×{int(o['lots']) * LOT} @ {px:.2f}" if ok else msg)


def place(o: dict, s: dict, now: datetime) -> tuple[bool, str]:
    """New BUY order → fill now (MARKET, or marketable LIMIT) or rest as OPEN (limit / AMO / waiting for a live price)."""
    o = _log(s, status="OPEN", side_=("BUY"), **o)
    o["amo"] = not market_open(now)
    ch = cached_chain(o["expiry"])
    p = price(o["strike"], o["side"], o["expiry"], o.get("sid", ""), ch, {}) if ch else None
    live = bool(ch and ch.get("live") and (now - ch["at"]).total_seconds() <= float(s["stale_s"])) or \
        bool(p and p.get("src") == "quote")                 # a < 1.5 s feed quote is live too
    if p and not o.get("sid"):
        o["sid"] = p.get("sid", "")
    if market_open(now) and live and p and np.isfinite(p["ltp"]):
        a = _ask(p)
        if o["otype"] == "MARKET" or a <= float(o["limit"]):
            r = _fill_buy(o, a, s, ch["spot"], "ask")
            save_state(s)
            return r
        save_state(s)
        return True, f"⏳ LIMIT BUY {name(o['strike'], o['side'], o['expiry'])} @ {o['limit']:.2f} placed (ask {a:.2f})"
    if market_open(now) and o["otype"] == "MARKET":         # never leave a market order waiting — it could fill much later
        o.update(status="REJECTED", msg="no live price at the moment")
        save_state(s)
        return False, (f"⚠ No live price for {name(o['strike'], o['side'], o['expiry'])} right now — MARKET order NOT "
                       f"placed. Tap again in a few seconds.")
    save_state(s)
    why = "AMO — fills at the first live price after 09:15" if o["amo"] else "waiting for a live price"
    return True, f"⏳ {o['otype']} BUY {name(o['strike'], o['side'], o['expiry'])} queued · {why}"


def _less_brokerage(d: dict, n: int = 1) -> dict:
    """Round-trip charges minus n orders' ₹20 (+GST) that were already charged elsewhere."""
    d = dict(d)
    n = min(int(n), 2)
    b = float(_rates().get("brokerage_per_order", 20.0)) * n
    b = min(b, d["brokerage"])
    g = b * float(_rates().get("gst_rate", 0.18))
    d["brokerage"] = d["brokerage"] - b
    d["gst"] = max(0.0, round(d["gst"] - g, 2))
    d["total_charges"] = round(d["total_charges"] - b - g, 2)
    return d


def _exit(pos, px: float, reason: str, s: dict, spot: float, lots: int | None = None,
          same_sell_order: bool = False, log: bool = True) -> tuple[bool, str]:
    meta = dict(s["ids"].get(pos.position_id) or {})
    # Dhan charges ₹20 per ORDER: one buy sold in 2 parts = 1 buy + 2 sells = ₹60 (not 2 × ₹40);
    # 5 buys of one strike exited together = 5 buys + ONE sell order.
    orig = pt.calculate_trade_charges
    skip = int(bool(meta.get("buy_brok_done"))) + int(bool(same_sell_order))
    if skip:
        pt.calculate_trade_charges = lambda *a, **k: _less_brokerage(orig(*a, **k), skip)
    try:
        if lots and lots < pos.lots:
            ok, msg, _ = pt.partial_exit_paper_position(pos.position_id, lots, spot=float(spot or 0), exit_reason=reason,
                                                        custom_exit_price=float(px))
            if ok and pos.position_id in s["ids"]:
                s["ids"][pos.position_id]["buy_brok_done"] = True
        else:
            ok, msg, _ = pt.exit_paper_position(pos.position_id, spot=float(spot or 0), exit_reason=reason,
                                                custom_exit_price=float(px), notes=f"{pos.notes} · {reason} {px:.2f}")
            if ok:
                s["ids"].pop(pos.position_id, None)
    finally:
        pt.calculate_trade_charges = orig
    if ok and log:
        _log(s, side_="SELL", strike=pos.strike, side=pos.opt_type, expiry=meta.get("expiry", ""),
             lots=lots or pos.lots, otype="MARKET" if reason not in ("Target",) else "LIMIT", status="FILLED",
             fill_px=round(px, 2), fill_ts=ist_now().isoformat(timespec="seconds"), msg=reason, pid=pos.position_id)
    return ok, msg


def groups(acc, s: dict) -> dict:
    """Net positions per instrument (strike · side · expiry · product), broker style."""
    g = {}
    for p in acc.open_positions:
        if p.side.upper() != "BUY":
            continue
        e = (s["ids"].get(p.position_id) or {}).get("expiry", "")
        k = f"{int(p.strike)}|{p.opt_type}|{e}|{p.product_type}"
        g.setdefault(k, []).append(p)
    return g


def _close_spot(e, ch: dict | None) -> float | None:
    d = _d(e)
    try:
        folder = Path(config.SNAPSHOT_DIR) / f"{d:%Y-%m-%d}"
        metas = sorted(folder.glob("*.meta.json"))
        good = [m for m in metas if m.stem[:4] <= "1531"] or metas
        if good:
            return float(json.loads(good[-1].read_text())["spot"])
    except Exception:                                           # noqa: BLE001
        pass
    if ch and d == ist_now().date():
        return float(ch["spot"])
    return None


def process(s: dict, now: datetime, sel_chain: dict | None) -> list[str]:
    """Settlement, resting orders, trailing SL, SL / target, square-off. Called on every desk refresh."""
    msgs = []
    acc = pt.load_paper_account()
    # ── expiry settlement (carry positions left in an expired contract) ──
    for p in list(acc.open_positions):
        e = (s["ids"].get(p.position_id) or {}).get("expiry", "")
        if e and expired(e, now):
            cs = _close_spot(e, sel_chain)
            if cs:
                iv = max(0.0, cs - p.strike) if p.opt_type == "CE" else max(0.0, p.strike - cs)
                ok, m = _exit(p, round(iv, 2), f"Expiry settlement @ NIFTY {cs:,.1f}", s, cs)
                if ok:
                    msgs.append(f"⚖ {name(p.strike, p.opt_type, e)} settled at intrinsic {iv:.2f}")
    if not market_open(now):
        if msgs:
            save_state(s)
        return msgs
    acc = pt.load_paper_account()
    exps = {str(o["expiry"]) for o in s["orders"] if o.get("status") == "OPEN"} | \
           {str((s["ids"].get(p.position_id) or {}).get("expiry", "")) for p in acc.open_positions}
    chains = {e: cached_chain(e) for e in exps if e}
    chains = {e: c for e, c in chains.items() if c and c.get("live") and (now - c["at"]).total_seconds() <= float(s["stale_s"])}
    need = [o.get("sid", "") for o in s["orders"] if o.get("status") == "OPEN" and str(o["expiry"]) not in chains] + \
           [(s["ids"].get(p.position_id) or {}).get("sid", "") for p in acc.open_positions
            if str((s["ids"].get(p.position_id) or {}).get("expiry", "")) not in chains]
    q = quotes(need)
    # ── resting / queued BUY orders ──
    for o in s["orders"]:
        if o.get("status") != "OPEN":
            continue
        if o["otype"] == "MARKET" and not o.get("amo"):
            try:
                waited = (now.replace(tzinfo=None) - datetime.fromisoformat(str(o["ts"])).replace(tzinfo=None)).total_seconds()
            except Exception:                               # noqa: BLE001
                waited = 999
            if waited > 30:
                o.update(status="CANCELLED", msg="no live price within 30 s — not filled")
                msgs.append(f"🗑️ MARKET BUY {name(o['strike'], o['side'], o['expiry'])} from {str(o['ts'])[11:16]} "
                            f"cancelled — no live price at the time")
                continue
        ch = chains.get(str(o["expiry"]))
        p = price(o["strike"], o["side"], o["expiry"], o.get("sid", ""), ch, q)
        if not p or not np.isfinite(p["ltp"]):
            continue
        a = _ask(p)
        spot = ch["spot"] if ch else (sel_chain or {}).get("spot", 0)
        if (o["otype"] == "MARKET" or a <= float(o["limit"])) and not _claim(o.get("oid", "")):
            o["status"] = "FILLING"                         # another page is filling it
            continue
        if o["otype"] == "MARKET":
            ok, m = _fill_buy(o, a, s, spot, "ask (AMO)" if o.get("amo") else "ask")
            msgs.append(m)
        elif a <= float(o["limit"]):
            fill = a if o.get("amo") else float(o["limit"])          # AMO limit matches at the open price
            ok, m = _fill_buy(o, min(fill, float(o["limit"])), s, spot, "limit")
            msgs.append(m)
    # ── positions: trail, SL, target, square-off ──
    acc = pt.load_paper_account()
    sq = datetime.strptime(str(s["squareoff"]), "%H:%M").time()
    for p in list(acc.open_positions):
        if p.side.upper() != "BUY":
            continue
        meta = s["ids"].setdefault(p.position_id, {"sid": "", "expiry": (sel_chain or {}).get("expiry", ""), "hi": p.entry_price})
        ch = chains.get(str(meta.get("expiry")))
        pr_ = price(p.strike, p.opt_type, meta.get("expiry"), meta.get("sid", ""), ch, q)
        if not pr_ or not np.isfinite(pr_["ltp"]):
            continue
        b = _bid(pr_)
        spot = ch["spot"] if ch else (sel_chain or {}).get("spot", 0)
        meta["hi"] = max(float(meta.get("hi") or p.entry_price), b)
        if p.trailing_sl_pts and p.trailing_sl_pts > 0:
            new_sl = round(meta["hi"] - p.trailing_sl_pts, 2)
            if new_sl > (p.stop_loss or 0) + 0.04:
                pt.update_paper_position_sl_target(p.position_id, new_sl=new_sl)
                p.stop_loss = new_sl
        reason, px = None, b
        if p.stop_loss and b <= p.stop_loss:
            reason = "Trailing SL" if p.trailing_sl_pts else "Stop-loss"
        elif p.target_1 and b >= p.target_1:
            reason, px = "Target", p.target_1
        elif p.product_type.upper() == "INTRADAY" and now.time() >= sq:
            reason = f"Square-off {s['squareoff']}"
        if reason:
            ok, m = _exit(p, px, reason, s, spot)
            if ok:
                msgs.append(f"🔔 {reason}: {name(p.strike, p.opt_type, meta.get('expiry'))} @ {px:.2f}")
    save_state(s)
    return msgs


# ═══════════════════════════════════════════════════════════════════════════
# Charges — Dhan options tariff, per ORDER (leg). Rates live in paper_config.json.
#   Brokerage ₹20/executed order · STT 0.15% of premium, SELL only (from 1 Apr 2026; was 0.10%)
#   NSE transaction 0.03503% · SEBI ₹10/crore · IPFT 0.0005%ore (both legs)
#   Stamp duty 0.003%, BUY only · GST 18% on (brokerage + NSE txn + SEBI)
# ═══════════════════════════════════════════════════════════════════════════
STT_2026 = 0.0015
IPFT_RATE = 0.000005                       # Dhan "Txn. Charges" = NSE 0.03503 % + IPFT 0.0005 % = 0.03553 %
CHG_LABELS = (("brokerage", "Brokerage"), ("stt", "STT"), ("exchange", "NSE txn"), ("sebi", "SEBI"),
              ("stamp", "Stamp duty"), ("ipft", "IPFT"), ("gst", "GST"))


def _rates() -> dict:
    c = pt.load_paper_config()
    changed = False
    if abs(float(c.get("stt_sell_rate", STT_2026)) - 0.001) < 1e-9:          # old pre-Apr-2026 rate → upgrade once
        c["stt_sell_rate"] = STT_2026
        changed = True
    if abs(float(c.get("ipft_rate", IPFT_RATE)) - 0.0000005) < 1e-12:        # IPFT is 0.0005 %, not ₹0.5/crore
        c["ipft_rate"] = IPFT_RATE                                             # (verified: Dhan txn = 0.03553 %)
        changed = True
    if changed:
        try:
            pt.save_paper_config(c)
        except Exception:                                   # noqa: BLE001
            pass
    return c


def leg_charges(px: float, qty: float, leg: str, c: dict | None = None) -> dict:
    """Charges for ONE executed order. leg = 'BUY' or 'SELL'."""
    c = c or _rates()
    tv = max(0.0, float(px) * float(qty))
    buy = leg.upper() == "BUY"
    out = dict(turnover=tv,
               brokerage=float(c.get("brokerage_per_order", 20.0)) if tv > 0 else 0.0,
               stt=0.0 if buy else tv * float(c.get("stt_sell_rate", STT_2026)),
               exchange=tv * float(c.get("exchange_txn_fee_rate", 0.0003503)),
               sebi=tv * float(c.get("sebi_turnover_rate", 0.000001)),
               stamp=tv * float(c.get("stamp_duty_buy_rate", 0.00003)) if buy else 0.0,
               ipft=tv * float(c.get("ipft_rate", IPFT_RATE)))
    out["gst"] = (out["brokerage"] + out["exchange"] + out["ipft"] + out["sebi"]) * float(c.get("gst_rate", 0.18))
    out["total"] = sum(out[k] for k, _ in CHG_LABELS)
    return out


def _chg_line(d: dict, short: bool = True) -> str:
    parts = [f"{lab} ₹{d[k]:.2f}" for k, lab in CHG_LABELS if d.get(k, 0) >= 0.005]
    return " · ".join(parts)


def day_stats(open_mtm: float) -> dict:
    h = pt.load_trade_history()
    today = ist_now().strftime("%Y-%m-%d")
    t = h[h["exit_time"].astype(str).str.startswith(today)] if len(h) and "exit_time" in h else pd.DataFrame()
    net = pd.to_numeric(t["net_pnl_rupees"], errors="coerce").fillna(0) if len(t) and "net_pnl_rupees" in t else pd.Series(dtype=float)
    chg = float(pd.to_numeric(t["charges_rupees"], errors="coerce").fillna(0).sum()) if len(t) and "charges_rupees" in t else 0.0
    return dict(realised=float(net.sum()), open=open_mtm, trades=len(t), wins=int((net > 0).sum()), charges=chg,
                win_pct=100 * float((net > 0).mean()) if len(net) else 0.0)


# ═══════════════════════════════════════════════════════════════════════════
# 5 · callbacks (run BEFORE the desk redraws → instant)
# ═══════════════════════════════════════════════════════════════════════════
def _once(st, key: str) -> bool:
    now = _time.time()
    last = st.session_state.get("_pd_last", {})
    if now - last.get(key, 0) < 1.2:
        return False
    last[key] = now
    st.session_state["_pd_last"] = last
    return True


def _msg(st, text, icon="✅"):
    st.session_state.setdefault("_pd_msgs", []).append((text, icon))


def _recent_buy(s: dict, side: str, strike: int, expiry: str, within_s: float = 8.0) -> float | None:
    now = ist_now()
    for o in reversed(s.get("orders", [])):
        if o.get("side_", "BUY") == "BUY" and o.get("status") in ("FILLED", "OPEN") and int(o.get("strike", 0)) == int(strike) \
                and o.get("side") == side and str(o.get("expiry")) == str(expiry):
            try:
                t = datetime.fromisoformat(str(o.get("fill_ts") or o.get("ts")))
                age = (now.replace(tzinfo=None) - t.replace(tzinfo=None)).total_seconds()
            except Exception:                               # noqa: BLE001
                return None
            return age if 0 <= age <= within_s else None
    return None


def cb_quick_buy(side: str, strike: int, expiry: str, sid: str) -> None:
    import streamlit as st
    if not _once(st, f"q{side}{strike}"):
        return
    s = load_state()
    key = f"{side}{strike}{expiry}"
    age = _recent_buy(s, side, strike, expiry)
    conf = st.session_state.get("_pd_confirm", {})
    if age is not None and _time.time() - conf.get(key, 0) > 8:
        conf[key] = _time.time()
        st.session_state["_pd_confirm"] = conf
        _msg(st, f"You bought {strike} {side} {age:.0f}s ago — tap BUY again to add another {int(s['lots'])} lot(s)", "✋")
        return
    conf.pop(key, None)
    ok, m = place(dict(strike=strike, side=side, expiry=expiry, sid=sid, lots=int(s["lots"]), otype="MARKET", limit=0.0,
                       product=s["product"], sl_pts=s["sl_pts"], tgt_pts=s["tgt_pts"], trail_pts=s["trail_pts"]), s, ist_now())
    _msg(st, m, "🛒" if ok else "⚠️")


def cb_open_ticket(side: str, strike: int, expiry: str, sid: str, ref: float) -> None:
    import streamlit as st
    s = load_state()
    st.session_state["pd_ticket"] = dict(side=side, strike=strike, expiry=expiry, sid=sid, ref=float(round(ref, 1)))
    for k, v in (("pd_t_lots", int(s["lots"])), ("pd_t_type", "MARKET"), ("pd_t_prod", s["product"]),
                 ("pd_t_lim", float(round(ref, 1))), ("pd_t_sl", float(s["sl_pts"])), ("pd_t_tg", float(s["tgt_pts"])),
                 ("pd_t_tr", float(s["trail_pts"]))):
        st.session_state[k] = v


def cb_close_ticket() -> None:
    import streamlit as st
    st.session_state.pop("pd_ticket", None)


def cb_submit_ticket() -> None:
    import streamlit as st
    t = st.session_state.get("pd_ticket")
    if not t or not _once(st, f"ticket{t['side']}{t['strike']}"):
        return
    ss = st.session_state
    s = load_state()
    ok, m = place(dict(strike=t["strike"], side=t["side"], expiry=t["expiry"], sid=t["sid"], lots=int(ss["pd_t_lots"]),
                       otype=ss["pd_t_type"], limit=float(ss["pd_t_lim"]), product=ss["pd_t_prod"],
                       sl_pts=float(ss["pd_t_sl"]), tgt_pts=float(ss["pd_t_tg"]), trail_pts=float(ss["pd_t_tr"])),
                  s, ist_now())
    _msg(st, m, "🛒" if ok else "⚠️")
    if ok:
        ss.pop("pd_ticket", None)


def _group_now(key: str):
    s = load_state()
    acc = pt.load_paper_account()
    return s, groups(acc, s).get(key, [])


def _live_px(s, strike, side, expiry, sid):
    ch = cached_chain(expiry)
    now = ist_now()
    if not (market_open(now) and ch and ch.get("live") and (now - ch["at"]).total_seconds() <= float(s["stale_s"])):
        q = quotes([sid]) if market_open(now) else {}
        p = price(strike, side, expiry, sid, None, q) if q else None
        return p, (ch or {}).get("spot", 0)
    return price(strike, side, expiry, sid, ch, quotes([sid]) if str(ch.get("expiry")) != str(expiry) else {}), ch["spot"]


def cb_exit(key: str, frac: float = 1.0, n_lots: int | None = None) -> None:
    import streamlit as st
    if not _once(st, f"x{key}{frac}{n_lots or ''}"):
        return
    s, ps = _group_now(key)
    if not ps:
        return
    strike, side, expiry, _ = key.split("|")
    sid = (s["ids"].get(ps[0].position_id) or {}).get("sid", "")
    p, spot = _live_px(s, float(strike), side, expiry, sid)
    if not p or not np.isfinite(p["ltp"]):
        _msg(st, "No live price — exit not done (market closed or feed down)", "⚠️")
        return
    b = _bid(p)
    total = sum(x.lots for x in ps)
    left = min(int(n_lots), total) if n_lots else (total if frac >= 1 else max(1, int(total * frac)))
    sold = left
    reason = "Manual exit" if (frac >= 1 and not n_lots) else f"Sold {sold} lot"
    first, done_lots, pid0 = True, 0, ps[0].position_id
    for x in sorted(ps, key=lambda y: -y.lots):
        if left <= 0:
            break
        n = min(left, x.lots)
        ok_, _ = _exit(x, b, reason, s, spot, n if n < x.lots else None, same_sell_order=not first, log=False)
        if ok_:
            done_lots += n
            first = False
        left -= n
    if done_lots:                                           # ONE sell order in the book, like the broker
        _log(s, side_="SELL", strike=int(strike), side=side, expiry=expiry, lots=done_lots, otype="MARKET",
             status="FILLED", fill_px=round(b, 2), fill_ts=ist_now().isoformat(timespec="seconds"), msg=reason, pid=pid0)
    save_state(s)
    _msg(st, f"✅ SOLD {name(strike, side, expiry)} ×{sold * LOT} @ {b:.2f}")


def cb_exit_all() -> None:
    import streamlit as st
    if not _once(st, "xall"):
        return
    s = load_state()
    for k in list(groups(pt.load_paper_account(), s)):
        st.session_state["_pd_last"].pop(f"x{k}1.0", None)
        cb_exit(k)


def cb_add(key: str) -> None:
    strike, side, expiry, prod = key.split("|")
    import streamlit as st
    if not _once(st, f"a{key}"):
        return
    s, ps = _group_now(key)
    sid = (s["ids"].get(ps[0].position_id) or {}).get("sid", "") if ps else ""
    ok, m = place(dict(strike=int(strike), side=side, expiry=expiry, sid=sid, lots=int(s["lots"]), otype="MARKET",
                       limit=0.0, product=prod, sl_pts=s["sl_pts"], tgt_pts=s["tgt_pts"], trail_pts=s["trail_pts"]),
                  s, ist_now())
    _msg(st, m, "🛒" if ok else "⚠️")


def cb_modify_open(key: str, where: str = "pos") -> None:
    import streamlit as st
    st.session_state["pd_mod_where"] = where
    s, ps = _group_now(key)
    if not ps:
        return
    p = ps[0]
    st.session_state["pd_mod"] = key
    st.session_state["pd_m_sl"] = float(p.stop_loss or 0)
    st.session_state["pd_m_tg"] = float(p.target_1 or 0)
    st.session_state["pd_m_tr"] = float(p.trailing_sl_pts or 0)


def cb_preset(field: str, val: float) -> None:
    import streamlit as st
    st.session_state[{"sl": "pd_m_sl", "tg": "pd_m_tg", "tr": "pd_m_tr"}[field]] = float(val)


def cb_modify_save() -> None:
    import streamlit as st
    key = st.session_state.pop("pd_mod", None)
    if not key:
        return
    s, ps = _group_now(key)
    ss = st.session_state
    for p in ps:
        pt.update_paper_position_sl_target(p.position_id, new_sl=float(ss["pd_m_sl"]) or None,
                                           new_t1=float(ss["pd_m_tg"]) or None, trailing_sl_pts=float(ss["pd_m_tr"]))
        if not float(ss["pd_m_sl"]) or not float(ss["pd_m_tg"]):                 # 0 = remove
            acc = pt.load_paper_account()
            for q_ in acc.open_positions:
                if q_.position_id == p.position_id:
                    q_.stop_loss = float(ss["pd_m_sl"]) or 0.0
                    q_.target_1 = float(ss["pd_m_tg"]) or 0.0
            pt.save_paper_account(acc)
    _msg(st, f"✎ SL {ss['pd_m_sl']:.2f} · T {ss['pd_m_tg']:.2f} · trail {ss['pd_m_tr']:.0f}", "✏️")


def cb_modify_close() -> None:
    import streamlit as st
    st.session_state.pop("pd_mod", None)


def cb_cancel(oid: str) -> None:
    import streamlit as st
    s = load_state()
    for o in s["orders"]:
        if o.get("oid") == oid and o.get("status") == "OPEN":
            o.update(status="CANCELLED", msg="Cancelled by you")
            _msg(st, f"✕ Cancelled {name(o['strike'], o['side'], o['expiry'])}", "🗑️")
    save_state(s)


# ═══════════════════════════════════════════════════════════════════════════
# 6 · UI
# ═══════════════════════════════════════════════════════════════════════════
CSS = """<style>
@keyframes pdpulse{0%{opacity:1}50%{opacity:.25}100%{opacity:1}}
.pd-box{background:linear-gradient(135deg,#0b1a33 0%,#13284d 100%);border:1px solid #d4af37;border-radius:10px;
        padding:8px 10px;margin:2px 0 6px;color:#f1f5f9;box-shadow:0 0 12px rgba(212,175,55,.15)}
.pd-h{color:#d4af37;font-weight:800;font-size:1.02rem;letter-spacing:.5px}
.pd-live{color:#22c55e;animation:pdpulse 1.4s infinite}
.pd-m{color:#94a3b8;font-size:0.72rem}
.pd-big{font-weight:800;font-size:1.25rem}
.pd-row{background:#0b1a33;border-left:3px solid #d4af37;border-radius:6px;padding:4px 8px;font-size:0.8rem;
        line-height:1.3;color:#f1f5f9;margin-bottom:2px}
.pd-row.ce{border-left-color:#22c55e}.pd-row.pe{border-left-color:#ef4444}
.pd-tk{background:#0b1a33;border:1px solid #22c55e;border-radius:10px;padding:8px 10px;color:#f1f5f9;margin-bottom:4px}
.pd-tk.pe{border-color:#ef4444}
.pd-g{color:#22c55e}.pd-r{color:#ef4444}.pd-y{color:#eab308}.pd-w{color:#f1f5f9}
.pd-sec{font-size:0.78rem;font-weight:700;margin:6px 0 2px}
.pd-k{text-align:center;font-weight:700;font-size:0.8rem;padding-top:6px}
.pd-k.atm{color:#d4af37}
.pd-dp{width:100%;font-size:0.7rem;border-collapse:collapse}.pd-dp td{padding:0 3px}
[class*="st-key-pd_b_CE"] button,[class*="st-key-pd_tbuy_CE"] button{background:#16a34a!important;color:#fff!important;border:0!important;font-weight:800}
[class*="st-key-pd_b_PE"] button,[class*="st-key-pd_tbuy_PE"] button{background:#dc2626!important;color:#fff!important;border:0!important;font-weight:800}
[class*="st-key-pd_x_"] button,[class*="st-key-pd_xall"] button{background:#d4af37!important;color:#0b1a33!important;border:0!important;font-weight:800}
[class*="st-key-pd_ch_CE_itm"] button{background:rgba(34,197,94,.18)!important}
[class*="st-key-pd_ch_PE_itm"] button{background:rgba(239,68,68,.18)!important}
[class*="st-key-pd_ch_"] button{padding:0 2px!important;min-height:1.9rem!important;font-size:0.78rem!important}
[class*="st-key-pd_a_"] button,[class*="st-key-pd_h_"] button,[class*="st-key-pd_e_"] button,[class*="st-key-pd_t_"] button{
        padding:0 4px!important;min-height:1.9rem!important}
</style>"""


def _toast(st, text, icon=None):
    try:
        st.toast(text, icon=icon)
    except Exception:                                           # noqa: BLE001
        st.toast(text)


def _money(v: float) -> str:
    return f"<span class='{'pd-g' if v >= 0 else 'pd-r'}'>{'+' if v >= 0 else '−'}₹{abs(v):,.0f}</span>"


def _ago(sec: float) -> str:
    return f"{sec:.0f}s ago" if sec < 120 else f"{sec / 60:.0f} min ago" if sec < 7200 else f"{sec / 3600:.0f} h ago"


def _seg(st, label, options, key, default):
    if key not in st.session_state:
        st.session_state[key] = default
    if hasattr(st, "segmented_control"):
        v = st.segmented_control(label, options, key=key, label_visibility="collapsed")
        return v or default
    return st.radio(label, options, key=key, horizontal=True, label_visibility="collapsed")


def _ticket(st, s, now, chain_for) -> None:
    t = st.session_state.get("pd_ticket")
    if not t:
        return
    ch = chain_for(t["expiry"])
    q = quotes([t["sid"]]) if t.get("sid") else {}
    p = price(t["strike"], t["side"], t["expiry"], t["sid"], ch, q)
    cls = t["side"].lower()
    if p:
        dp = (q.get(t["sid"]) or {}).get("depth") or {}
        rows = ""
        for i in range(5):
            b = (dp.get("buy") or [])[i] if i < len(dp.get("buy") or []) else {}
            a = (dp.get("sell") or [])[i] if i < len(dp.get("sell") or []) else {}
            rows += (f"<tr><td class='pd-m'>{b.get('quantity', '')}</td><td class='pd-g'>{b.get('price', '')}</td>"
                     f"<td class='pd-r' style='text-align:right'>{a.get('price', '')}</td>"
                     f"<td class='pd-m' style='text-align:right'>{a.get('quantity', '')}</td></tr>")
        depth = (f"<table class='pd-dp'><tr><td class='pd-m'>Qty</td><td class='pd-m'>Bid</td><td class='pd-m' "
                 f"style='text-align:right'>Ask</td><td class='pd-m' style='text-align:right'>Qty</td></tr>{rows}</table>"
                 if rows.count("<td class='pd-g'></td>") < 5 else "")
        spr = 100 * (p["ask"] - p["bid"]) / p["ltp"] if np.isfinite(p["ask"]) and np.isfinite(p["bid"]) and p["ltp"] else np.nan
        st.markdown(f"<div class='pd-tk {cls}'><b class='{'pd-g' if cls == 'ce' else 'pd-r'}'>BUY</b> "
                    f"<b>{name(t['strike'], t['side'], t['expiry'])}</b><br>"
                    f"<span class='pd-w'>LTP {p['ltp']:.2f}</span> <span class='pd-m'>· bid {p['bid']:.2f} | ask "
                    f"{p['ask']:.2f}{f' · spr {spr:.1f}%' if np.isfinite(spr) else ''}</span>{depth}</div>",
                    unsafe_allow_html=True)
    else:
        st.markdown(f"<div class='pd-tk {cls}'><b>BUY {name(t['strike'], t['side'], t['expiry'])}</b><br>"
                    "<span class='pd-m'>no live price — order will wait</span></div>", unsafe_allow_html=True)
    c = st.columns(2)
    lots = c[0].number_input("Lots", 1, 100, step=1, key="pd_t_lots")
    c[1].markdown(f"<div class='pd-m' style='padding-top:30px'>= {int(lots) * LOT} qty</div>", unsafe_allow_html=True)
    otype = _seg(st, "Type", ["MARKET", "LIMIT"], "pd_t_type", "MARKET")
    prod = _seg(st, "Product", ["INTRADAY", "NORMAL"], "pd_t_prod", s["product"])
    if float(st.session_state.get("pd_t_lim") or 0) < 0.1:
        st.session_state["pd_t_lim"] = float(t.get("ref") or (_ask(p) if p else 1.0))
    st.number_input("Limit price" if otype == "LIMIT" else "Limit price (LIMIT only)", 0.05, 100000.0, step=0.05,
                    key="pd_t_lim", format="%.2f", disabled=(otype != "LIMIT"))
    c = st.columns(3)
    c[0].number_input("SL pts", 0.0, 1000.0, step=1.0, key="pd_t_sl")
    c[1].number_input("Target", 0.0, 2000.0, step=1.0, key="pd_t_tg")
    c[2].number_input("Trail", 0.0, 1000.0, step=1.0, key="pd_t_tr")
    px = (_ask(p) if otype == "MARKET" else float(st.session_state["pd_t_lim"])) if p else float(st.session_state["pd_t_lim"])
    qty = int(lots) * LOT
    acc = pt.load_paper_account()
    ch_ = pt.calculate_trade_charges(px, px, qty, "BUY")
    sl = float(st.session_state["pd_t_sl"])
    mx = f" · max loss ₹{sl * qty + ch_['total_charges']:,.0f}" if sl > 0 else ""
    st.markdown(f"<div class='pd-row'>Value <b>₹{px * qty:,.0f}</b> <span class='pd-m'>· available ₹{acc.cash_balance:,.0f}</span><br>"
                f"<span class='pd-m'>"
                f"charges ~₹{ch_['total_charges']:,.0f} · breakeven +{ch_['breakeven_pts']:.2f} pts{mx}"
                f"{' · CARRY → held overnight, settles at expiry' if prod == 'NORMAL' else ''}</span></div>", unsafe_allow_html=True)
    amo = not market_open(now)
    c = st.columns([3, 1])
    c[0].button(("Place AMO " if amo else "BUY ") + (f"@ {px:.2f}" if otype == "LIMIT" else "MKT") + f" · {qty}",
                key=f"pd_tbuy_{t['side']}", use_container_width=True, on_click=cb_submit_ticket)
    c[1].button("✕", key="pd_tclose", use_container_width=True, on_click=cb_close_ticket)


def _positions(st, s, now, chain_for, live_ok) -> float:
    acc = pt.load_paper_account()
    g = groups(acc, s)
    mtm = 0.0
    st.session_state["_pd_open"] = {}
    st.session_state["_pd_ltp_pid"] = {}
    if not g:
        return mtm
    sids = []
    for k, ps in g.items():
        e = k.split("|")[2]
        ch = chain_for(e)
        if not (ch and str(ch.get("expiry")) == e):
            sids.append((s["ids"].get(ps[0].position_id) or {}).get("sid", ""))
    q = quotes(sids) if sids else {}
    for ps_ in g.values():                                  # every open position gets a 1-second quote
        x_ = (s["ids"].get(ps_[0].position_id) or {}).get("sid", "")
        if x_:
            _feed["want_sid"][x_] = _time.time()
    st.markdown("<div class='pd-sec'>POSITIONS</div>", unsafe_allow_html=True)
    for k, ps in g.items():
        strike, side, e, prod = k.split("|")
        qty = sum(p.quantity for p in ps)
        avg = sum(p.entry_price * p.quantity for p in ps) / qty
        sid = (s["ids"].get(ps[0].position_id) or {}).get("sid", "")
        p = price(float(strike), side, e, sid, chain_for(e), q)
        ltp = p["ltp"] if p and np.isfinite(p["ltp"]) else float(ps[0].current_price or avg)
        pnl = (ltp - avg) * qty
        mtm += pnl
        st.session_state.setdefault("_pd_open", {})[k] = dict(strike=int(strike), side=side, expiry=e, qty=qty, avg=avg,
                                                              ltp=ltp, pnl=pnl, product=prod)
        pct = 100 * (ltp - avg) / avg if avg else 0
        for x in ps:
            st.session_state.setdefault("_pd_ltp_pid", {})[x.position_id] = ltp
        bid = _bid(p) if p and np.isfinite(p.get("ltp", np.nan)) else ltp
        ask = _ask(p) if p and np.isfinite(p.get("ltp", np.nan)) else ltp
        rc = _rates()
        c_buy = sum(leg_charges(x.entry_price, x.quantity, "BUY", rc)["total"] for x in ps)
        c_sell = leg_charges(bid, qty, "SELL", rc)["total"]
        be = avg + (c_buy + c_sell) / qty if qty else avg
        net_now = (bid - avg) * qty - c_buy - c_sell
        hi = max(float((s["ids"].get(x.position_id) or {}).get("hi") or x.entry_price) for x in ps)
        lots_n = sum(x.lots for x in ps)
        st.session_state.setdefault("_pd_pos_info", {})[k] = dict(avg=avg, ltp=ltp, bid=bid, be=be, hi=hi, qty=qty)
        sl = ps[0].stop_loss
        tg = ps[0].target_1
        tr = ps[0].trailing_sl_pts
        extra = " · ".join(x for x in (f"SL {sl:.2f}" if sl else "", f"T {tg:.2f}" if tg else "",
                                        f"trail {tr:.0f}" if tr else "") if x)
        st.markdown(f"<div class='pd-row {side.lower()}'><b>{name(strike, side, e)}</b> "
                    f"<span class='pd-m'>{'MIS' if prod == 'INTRADAY' else 'NRML'} · {lots_n} lot{'s' if lots_n > 1 else ''}</span>"
                    f"<span style='float:right' class='pd-big'>{_money(pnl)}</span><br>"
                    f"<span class='pd-m'>BOUGHT</span> {qty} @ <b>{avg:.2f}</b>{' avg' if len(ps) > 1 else ''} "
                    f"<span class='pd-m'>→ NOW</span> <b>{ltp:.2f}</b> "
                    f"<span class='{'pd-g' if pct >= 0 else 'pd-r'}'>({ltp - avg:+.2f} pts, {pct:+.1f}%)</span><br>"
                    f"<span class='pd-m'>bid {bid:.2f} · ask {ask:.2f} · high since buy {hi:.2f}</span><br>"
                    f"<span class='pd-m'>charges: buy ₹{c_buy:.0f} paid + sell ≈₹{c_sell:.0f} → breakeven <b class='pd-w'>{be:.2f}</b></span><br>"
                    f"<span class='pd-m'>if sold now at bid, NET after all charges</span> "
                    f"<b class='{'pd-g' if net_now >= 0 else 'pd-r'}'>{_money(net_now)}</b>"
                    f"{f'<br><span class=pd-m>{extra}</span>' if extra else ''}</div>", unsafe_allow_html=True)
        c = st.columns([1.3, 0.8, 0.8, 0.8])
        c[0].button("EXIT", key=f"pd_x_{k}", use_container_width=True, disabled=not live_ok, on_click=cb_exit, args=(k,),
                    help="Sell everything at the bid")
        c[1].button("+1", key=f"pd_a_{k}", use_container_width=True, on_click=cb_add, args=(k,),
                    help=f"Buy {int(s['lots'])} more lot(s) at the ask")
        c[2].button("−1", key=f"pd_h_{k}", use_container_width=True, disabled=not live_ok or lots_n < 2,
                    on_click=cb_exit, args=(k, 1.0, 1), help="Sell ONE lot at the bid (needs 2+ lots)")
        c[3].button("✎", key=f"pd_e_{k}", use_container_width=True, on_click=cb_modify_open, args=(k,),
                    help="Modify SL / target / trailing SL")
        if st.session_state.get("pd_mod") == k and st.session_state.get("pd_mod_where", "pos") == "pos":
            _mod_panel(st, k, avg, ltp, be, hi)
    if len(g) > 1:
        st.button("⏹ EXIT ALL at bid", key="pd_xall", use_container_width=True, disabled=not live_ok, on_click=cb_exit_all)
    return mtm


def _mod_panel(st, k: str, avg: float, ltp: float, be: float, hi: float) -> None:
    """SL / target / trail editor for one open position (used by Positions and by Orders → working orders)."""
    st.markdown(f"<div class='pd-row'><span class='pd-m'>avg</span> <b>{avg:.2f}</b> · <span class='pd-m'>now</span> "
                f"<b>{ltp:.2f}</b> · <span class='pd-m'>breakeven</span> <b>{be:.2f}</b> · <span class='pd-m'>high</span> "
                f"<b>{hi:.2f}</b><br><span class='pd-m'>SL = premium price that exits you · Target = price that books "
                f"profit · Trail = pts below the highest bid</span></div>", unsafe_allow_html=True)
    pc = st.columns(2)
    pc[0].button("SL = breakeven", key=f"pd_ps_be_{k}", use_container_width=True, on_click=cb_preset, args=("sl", round(be, 2)),
                 help="Stop at breakeven (no loss after charges)")
    pc[1].button("SL −15%", key=f"pd_ps_sl_{k}", use_container_width=True, on_click=cb_preset,
                 args=("sl", round(avg * 0.85, 2)), help=f"Stop at {avg * 0.85:.2f}")
    pc = st.columns(2)
    pc[0].button("Target +25%", key=f"pd_ps_tg_{k}", use_container_width=True, on_click=cb_preset,
                 args=("tg", round(avg * 1.25, 2)), help=f"Target {avg * 1.25:.2f}")
    pc[1].button("Trail 10 pts", key=f"pd_ps_tr_{k}", use_container_width=True, on_click=cb_preset, args=("tr", 10.0),
                 help="Exit if the bid falls 10 pts below its highest since entry")
    c = st.columns(3)
    c[0].number_input("SL ₹", 0.0, 100000.0, step=0.5, key="pd_m_sl", format="%.2f")
    c[1].number_input("Target ₹", 0.0, 100000.0, step=0.5, key="pd_m_tg", format="%.2f")
    c[2].number_input("Trail pts", 0.0, 1000.0, step=1.0, key="pd_m_tr")
    c = st.columns(2)
    c[0].button("Save", key="pd_m_save", use_container_width=True, on_click=cb_modify_save)
    c[1].button("Close", key="pd_m_close", use_container_width=True, on_click=cb_modify_close)

def _picks(st, s, chain) -> None:
    lots = int(s["lots"])
    w = s["weights"]
    for side, col in (("CE", "pd-g"), ("PE", "pd-r")):
        top = rank(chain["rows"], chain["spot"], side, s)
        st.markdown(f"<div class='pd-sec'><b class='{col}'>BUY {side}</b><span class='pd-m'> · Δ{w['delta']} θ{w['theta']} "
                    f"γ{w['gamma']} ν{w['vega']} · {lots} lot = {lots * LOT}</span></div>", unsafe_allow_html=True)
        if top.empty:
            st.caption("no tradable strikes")
            continue
        for r in top.itertuples():
            px = r.ask if np.isfinite(r.ask) and r.ask > 0 else r.ltp
            warn = " ⚠" if np.isfinite(r.spread_pct) and r.spread_pct > 2 else ""
            c = st.columns([3.0, 1.15, 0.55])
            c[0].markdown(f"<div class='pd-row {side.lower()}'><b>{int(r.strike)} {side}</b> {px:.2f}{warn}<br>"
                          f"<span class='pd-m'>Δ {r.delta:+.2f} · θ {r.theta:.1f} · ★{r.score:.0f} · ₹{px * lots * LOT:,.0f}"
                          f"</span></div>", unsafe_allow_html=True)
            c[1].button("BUY", key=f"pd_b_{side}_{int(r.strike)}", use_container_width=True,
                        on_click=cb_quick_buy, args=(side, int(r.strike), chain["expiry"], r.sid),
                        help=f"Market BUY {lots} lot at the ask (~{px:.2f})")
            c[2].button("⋯", key=f"pd_t_{side}_{int(r.strike)}", use_container_width=True,
                        on_click=cb_open_ticket, args=(side, int(r.strike), chain["expiry"], r.sid, px),
                        help="Order ticket (limit, lots, SL/target)")


def _ladder(st, s, chain) -> None:
    rows = chain["rows"]
    atm = round(chain["spot"] / STEP) * STEP
    top = {(side, int(k)) for side in ("CE", "PE") for k in rank(rows, chain["spot"], side, s)["strike"]}
    n = int(s["ladder"])
    c = st.columns([1, 0.9, 1])
    c[0].markdown("<div class='pd-m pd-g' style='text-align:center'>CALL</div>", unsafe_allow_html=True)
    c[1].markdown(f"<div class='pd-m' style='text-align:center'>{chain['spot']:,.0f}</div>", unsafe_allow_html=True)
    c[2].markdown("<div class='pd-m pd-r' style='text-align:center'>PUT</div>", unsafe_allow_html=True)
    for k in [atm + i * STEP for i in range(n, -n - 1, -1)]:
        ce = rows[(rows["strike"] == k) & (rows["side"] == "CE")]
        pe = rows[(rows["strike"] == k) & (rows["side"] == "PE")]
        c = st.columns([1, 0.9, 1])
        for col, leg, side, itm in ((c[0], ce, "CE", k < chain["spot"]), (c[2], pe, "PE", k > chain["spot"])):
            if len(leg) and np.isfinite(leg.iloc[0].ltp):
                r = leg.iloc[0]
                star = "★" if (side, int(k)) in top else ""
                col.button(f"{star}{r.ltp:.1f}", key=f"pd_ch_{side}_{'itm' if itm else 'otm'}_{int(k)}",
                           use_container_width=True, on_click=cb_open_ticket,
                           args=(side, int(k), chain["expiry"], r.sid, _ask(dict(ltp=r.ltp, bid=r.bid, ask=r.ask))))
        c[1].markdown(f"<div class='pd-k {'atm' if k == atm else ''}'>{int(k)}</div>", unsafe_allow_html=True)
    st.caption("Tap a price → order ticket · ★ = top-ranked by your Greek weights · shaded = in-the-money")


def _working(st, s) -> None:
    """Open positions as WORKING orders (Dhan-style): entry, live price, target / SL / trail — editable here."""
    acc = pt.load_paper_account()
    g = groups(acc, s)
    if not g:
        return
    info = st.session_state.get("_pd_pos_info", {})
    live_ok = bool(st.session_state.get("_pd_live"))
    st.markdown("<div class='pd-sec'>OPEN · WORKING (not squared off)</div>", unsafe_allow_html=True)
    for k, ps in g.items():
        strike, side, e, prod = k.split("|")
        i = info.get(k)
        if not i:
            continue
        p0 = ps[0]
        sl, tg, tr = float(p0.stop_loss or 0), float(p0.target_1 or 0), float(p0.trailing_sl_pts or 0)
        pnl = (i["ltp"] - i["avg"]) * i["qty"]
        t_txt = (f"<b class='pd-g'>TARGET {tg:.2f}</b> <span class='pd-m'>({tg - i['ltp']:+.2f} pts to go = "
                 f"{_money((tg - i['avg']) * i['qty'])} gross at target)</span>" if tg else "<span class='pd-m'>no target</span>")
        s_txt = (f"<b class='pd-r'>SL {sl:.2f}</b> <span class='pd-m'>({sl - i['ltp']:+.2f} pts · "
                 f"{_money((sl - i['avg']) * i['qty'])} if hit)</span>" if sl else "<span class='pd-m'>no SL</span>")
        r_txt = (f"<b class='pd-y'>TRAIL {tr:.0f} pts</b> <span class='pd-m'>(high {i['hi']:.2f})</span>" if tr else "")
        st.markdown(f"<div class='pd-row {side.lower()}'><b class='pd-g'>BUY</b> {name(strike, side, e)} ×{i['qty']} "
                    f"@ <b>{i['avg']:.2f}</b> <span class='pd-m'>{'MIS' if prod == 'INTRADAY' else 'NRML'} · OPEN</span>"
                    f"<span style='float:right' class='pd-big'>{_money(pnl)}</span><br>"
                    f"<span class='pd-m'>now</span> <b>{i['ltp']:.2f}</b> <span class='pd-m'>· breakeven {i['be']:.2f}</span><br>"
                    f"{t_txt}<br>{s_txt}{'<br>' + r_txt if r_txt else ''}</div>", unsafe_allow_html=True)
        c = st.columns([1.2, 1.2, 1.0])
        c[0].button("✎ Edit T/SL", key=f"pd_oe_{k}", use_container_width=True, on_click=cb_modify_open,
                    args=(k, "orders"))
        c[1].button("EXIT at bid", key=f"pd_ox_{k}", use_container_width=True, disabled=not live_ok, on_click=cb_exit, args=(k,))
        c[2].button("Clear T/SL", key=f"pd_oc_{k}", use_container_width=True, on_click=cb_clear_exits, args=(k,),
                    disabled=not (sl or tg or tr), help="Remove target, SL and trail")
        if st.session_state.get("pd_mod") == k and st.session_state.get("pd_mod_where") == "orders":
            _mod_panel(st, k, i["avg"], i["ltp"], i["be"], i["hi"])


def cb_clear_exits(key: str) -> None:
    import streamlit as st
    s, ps = _group_now(key)
    acc = pt.load_paper_account()
    ids = {p.position_id for p in ps}
    for q_ in acc.open_positions:
        if q_.position_id in ids:
            q_.stop_loss, q_.target_1, q_.trailing_sl_pts = 0.0, 0.0, 0.0
    pt.save_paper_account(acc)
    _msg(st, "Target / SL / trail removed", "🧹")


def _orders(st, s) -> None:
    _working(st, s)
    op = [o for o in s["orders"] if o.get("status") == "OPEN"]
    if op:
        st.markdown("<div class='pd-sec'>PENDING ORDERS · AMO / LIMIT (fill later)</div>", unsafe_allow_html=True)
        for o in op:
            c = st.columns([3.6, 0.8])
            lab = "AMO " if o.get("amo") else ""
            now_px = (st.session_state.get("_pd_ask", {}) or {}).get(f"{o['strike']}{o['side']}{o['expiry']}")
            what = (f"limit <b class='pd-w'>{float(o['limit']):.2f}</b>" + (f" · ask now {now_px:.2f}" if now_px else "")
                    if o["otype"] == "LIMIT" else "at market")
            when = "fills at 09:15 open" if o.get("amo") else ("fills when ask ≤ limit" if o["otype"] == "LIMIT"
                                                             else "waiting for a live price")
            c[0].markdown(f"<div class='pd-row {o['side'].lower()}'>{lab}{o['otype']} BUY {name(o['strike'], o['side'], o['expiry'])}"
                          f"<br><span class='pd-m'>{int(o['lots']) * LOT} qty · {what} · placed {o['ts'][11:16]} · {when}"
                          f"</span></div>", unsafe_allow_html=True)
            c[1].button("✕", key=f"pd_c_{o['oid']}", use_container_width=True, on_click=cb_cancel, args=(o["oid"],))
    today = ist_now().strftime("%Y-%m-%d")
    done_all = [o for o in s["orders"] if o.get("status") != "OPEN" and
                str(o.get("fill_ts") or o.get("ts", "")).startswith(today)][::-1]
    show_all = st.toggle(f"Show all {len(done_all)} orders", key="pd_ord_all") if len(done_all) > 15 else True
    done = done_all if show_all else done_all[:15]
    st.markdown("<div class='pd-sec'>EXECUTED TODAY (newest first)</div>", unsafe_allow_html=True)
    if not done:
        st.caption("none yet")
    rc = _rates()
    tot = order_charges(s, ist_now().date(), rc)            # ALL of today's orders, not just the ones shown
    ltp_pid = st.session_state.get("_pd_ltp_pid", {})
    for o in done:
        col = {"FILLED": "pd-g", "REJECTED": "pd-r", "CANCELLED": "pd-m"}.get(o["status"], "pd-m")
        px = f" @ {o['fill_px']:.2f}" if o.get("fill_px") is not None else ""
        leg = o.get("side_", "BUY")
        extra = ""
        if o["status"] == "FILLED" and o.get("fill_px") is not None:
            qty = int(o["lots"]) * LOT
            ch = leg_charges(float(o["fill_px"]), qty, leg, rc)
            now_px = ltp_pid.get(o.get("pid")) if leg == "BUY" else None
            if now_px is not None:
                d = now_px - float(o["fill_px"])
                extra += (f"<br>&nbsp;&nbsp;still open · now <b class='pd-w'>{now_px:.2f}</b> "
                          f"<span class='{'pd-g' if d >= 0 else 'pd-r'}'>({d:+.2f} pts = {_money(d * qty)})</span>")
            extra += f"<br>&nbsp;&nbsp;charges <b class='pd-w'>₹{ch['total']:.2f}</b> = {_chg_line(ch)}"
        st.markdown(f"<div class='pd-row {o['side'].lower()}' style='font-size:.8rem'>{(o.get('fill_ts') or o['ts'])[11:19]} · <b class='{'pd-g' if leg == 'BUY' else 'pd-y'}'>{leg}</b> "
                    f"{name(o['strike'], o['side'], o.get('expiry', ''))} ×{int(o['lots']) * LOT}{px} · "
                    f"<span class='{col}'>{o['status']}</span> {o.get('msg', '') if o['status'] != 'FILLED' else ''}{extra}</div>",
                    unsafe_allow_html=True)
    if any(tot.values()):
        tt = sum(tot.values())
        st.markdown(f"<div class='pd-row'><b>Charges on today's orders: ₹{tt:,.2f}</b><br><span class='pd-m'>"
                    + "<br>".join(f"{lab}: ₹{tot[k]:,.2f}" for k, lab in CHG_LABELS if tot[k] >= 0.005)
                    + f"<br>STT {100 * float(rc.get('stt_sell_rate', STT_2026)):.2f}% sell-side · rates editable in "
                      f"data/paper_trading/paper_config.json</span></div>", unsafe_allow_html=True)


# ═══════════════════════════════════════════════════════════════════════════
# 7 · P&L by instrument + day report for the mentor
# ═══════════════════════════════════════════════════════════════════════════
import re as _re

_MON = {m: i for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}


def _expiry_of(row, s: dict) -> str:
    """Expiry of a closed trade: desk meta → order log → the 'NIFTY 13 OCT 22750 CE' text in its notes."""
    pid = _re.sub(r"-P\d+$", "", str(row.get("trade_id", "")))
    m = (s.get("meta") or {}).get(pid)
    if m and m.get("expiry"):
        return str(m["expiry"])
    for o in s.get("orders", []):
        if o.get("pid") == pid and o.get("expiry"):
            return str(o["expiry"])
    mm = _re.search(r"NIFTY (\d{2}) ([A-Z]{3}) \d+ (?:CE|PE)", str(row.get("notes", "")))
    if mm:
        d = int(mm.group(1)); mo = _MON.get(mm.group(2), 0)
        y = int(str(row.get("entry_time", ""))[:4] or ist_now().year)
        if mo:
            yy = y + (1 if mo < int(str(row.get("entry_time", "0000-01"))[5:7] or 1) else 0)
            return f"{yy:04d}-{mo:02d}-{d:02d}"
    return ""


def day_trades(day: date, s: dict) -> pd.DataFrame:
    """Every closed trade (and partial exit) whose exit was on `day`, with its expiry and instrument name."""
    h = pt.load_trade_history()
    if h.empty or "exit_time" not in h:
        return pd.DataFrame()
    t = h[h["exit_time"].astype(str).str.startswith(day.strftime("%Y-%m-%d"))].copy()
    if t.empty:
        return t
    t["expiry"] = [_expiry_of(r, s) for r in t.to_dict("records")]
    t["instrument"] = [name(k, sd, e) for k, sd, e in zip(t["strike"], t["opt_type"], t["expiry"])]
    for c in ("quantity", "entry_price", "exit_price", "pnl_rupees", "charges_rupees", "net_pnl_rupees"):
        t[c] = pd.to_numeric(t[c], errors="coerce").fillna(0)
    return t.sort_values("exit_time")


def by_instrument(t: pd.DataFrame, open_pos: dict) -> pd.DataFrame:
    """One row per strike · side · expiry: qty, avg buy, avg sell, gross, charges, NET realised, open MTM, total."""
    rows = {}
    for r in t.to_dict("records"):
        k = (int(float(r["strike"])), r["opt_type"], r["expiry"])
        x = rows.setdefault(k, dict(strike=k[0], side=k[1], expiry=k[2], qty=0, buy_val=0.0, sell_val=0.0, gross=0.0,
                                    charges=0.0, net=0.0, trades=0, open_qty=0, open_pnl=0.0))
        q = float(r["quantity"])
        x["qty"] += q; x["buy_val"] += q * r["entry_price"]; x["sell_val"] += q * r["exit_price"]
        x["gross"] += r["pnl_rupees"]; x["charges"] += r["charges_rupees"]; x["net"] += r["net_pnl_rupees"]; x["trades"] += 1
    for o in (open_pos or {}).values():
        k = (int(o["strike"]), o["side"], o["expiry"])
        x = rows.setdefault(k, dict(strike=k[0], side=k[1], expiry=k[2], qty=0, buy_val=0.0, sell_val=0.0, gross=0.0,
                                    charges=0.0, net=0.0, trades=0, open_qty=0, open_pnl=0.0))
        x["open_qty"] += o["qty"]; x["open_pnl"] += o["pnl"]
    out = pd.DataFrame(rows.values())
    if out.empty:
        return out
    out["avg_buy"] = out["buy_val"] / out["qty"].replace(0, np.nan)
    out["avg_sell"] = out["sell_val"] / out["qty"].replace(0, np.nan)
    out["total"] = out["net"] + out["open_pnl"]
    out["instrument"] = [name(k, sd, e) for k, sd, e in zip(out["strike"], out["side"], out["expiry"])]
    return out.sort_values("total")


def report_text(day: date, t: pd.DataFrame, inst: pd.DataFrame, chg: dict | None = None) -> str:
    """Plain text for WhatsApp / e-mail to the mentor."""
    L = [f"PAPER TRADES — {day:%a %d %b %Y}", ""]
    if inst.empty:
        return "\n".join(L + ["No trades."])
    L.append("P&L BY STRIKE (net of charges)")
    for r in inst.sort_values("total", ascending=False).to_dict("records"):
        sign = "+" if r["total"] >= 0 else "−"
        extra = f"  [open {int(r['open_qty'])} qty, MTM {'+' if r['open_pnl'] >= 0 else '−'}₹{abs(r['open_pnl']):,.0f}]" if r["open_qty"] else ""
        bs = (f"  {int(r['qty'])} qty · buy {r['avg_buy']:.2f} → sell {r['avg_sell']:.2f} · gross "
              f"{'+' if r['gross'] >= 0 else '−'}₹{abs(r['gross']):,.0f} − charges ₹{r['charges']:,.0f}" if r["qty"] else "")
        L.append(f"{r['instrument']:<28} {sign}₹{abs(r['total']):,.0f}{bs}{extra}")
    L += ["", "TRADES"]
    for i, r in enumerate(t.to_dict("records"), 1):
        sign = "+" if r["net_pnl_rupees"] >= 0 else "−"
        L.append(f"{i:>2}. {str(r['entry_time'])[11:16]}→{str(r['exit_time'])[11:16]}  {r['instrument']}  "
                 f"{int(r['quantity'])} @ {r['entry_price']:.2f} → {r['exit_price']:.2f}  {sign}₹{abs(r['net_pnl_rupees']):,.0f}"
                 f"  ({r.get('exit_reason', '')})")
    has = len(t) and "net_pnl_rupees" in t
    net, gross, ch = ((t["net_pnl_rupees"].sum(), t["pnl_rupees"].sum(), t["charges_rupees"].sum()) if has else (0.0, 0.0, 0.0))
    wins = int((t["net_pnl_rupees"] > 0).sum()) if has else 0
    op = float(inst["open_pnl"].sum())
    chg_txt = (" · ".join(f"{lab} ₹{chg[k]:,.2f}" for k, lab in CHG_LABELS if chg.get(k, 0) >= 0.005) if chg else "")
    L += ["", f"Booked NET: {'+' if net >= 0 else '−'}₹{abs(net):,.0f}  (gross {'+' if gross >= 0 else '−'}₹{abs(gross):,.0f}, "
              f"charges ₹{ch:,.0f})",
          f"Trades: {len(t)} · wins {wins} · losses {len(t) - wins} · win rate {100 * wins / len(t):.0f}%" if len(t) else "Trades: 0"]
    if chg_txt:
        L.append(f"Charges: {chg_txt}")
    if op:
        L.append(f"Open positions MTM: {'+' if op >= 0 else '−'}₹{abs(op):,.0f}")
    if has:
        best, worst = t.loc[t["net_pnl_rupees"].idxmax()], t.loc[t["net_pnl_rupees"].idxmin()]
        L.append(f"Best: {best['instrument']} +₹{best['net_pnl_rupees']:,.0f} · Worst: {worst['instrument']} "
                 f"{'+' if worst['net_pnl_rupees'] >= 0 else '−'}₹{abs(worst['net_pnl_rupees']):,.0f}")
    return "\n".join(L)


def order_charges(s: dict, day: date, rc: dict | None = None) -> dict:
    """Charges by type for every FILLED order on `day` (buy and sell legs)."""
    rc = rc or _rates()
    tot = {k: 0.0 for k, _ in CHG_LABELS}
    for o in s.get("orders", []):
        if o.get("status") == "FILLED" and o.get("fill_px") is not None and \
                str(o.get("fill_ts") or o.get("ts", "")).startswith(day.strftime("%Y-%m-%d")):
            ch = leg_charges(float(o["fill_px"]), int(o["lots"]) * LOT, o.get("side_", "BUY"), rc)
            for k, _ in CHG_LABELS:
                tot[k] += ch[k]
    return tot


def ch_total_booked(t: pd.DataFrame) -> float:
    return float(pd.to_numeric(t["charges_rupees"], errors="coerce").fillna(0).sum()) if "charges_rupees" in t else 0.0


def _pnl_view(st, s: dict) -> None:
    today = datetime.now().date()                 # same clock the engine stamps trades with
    day = st.date_input("Day", value=today, max_value=max(today, ist_now().date()), key="pd_pnl_day",
                        label_visibility="collapsed", format="DD/MM/YYYY")
    t = day_trades(day, s)
    open_pos = st.session_state.get("_pd_open", {}) if day in (today, ist_now().date()) else {}
    inst = by_instrument(t, open_pos)
    if inst.empty:
        st.caption(f"No paper trades on {day:%d %b %Y}.")
        return
    tot = float(inst["total"].sum())
    net = float(t["net_pnl_rupees"].sum()) if len(t) else 0.0
    ch = float(t["charges_rupees"].sum()) if len(t) else 0.0
    wins = int((t["net_pnl_rupees"] > 0).sum()) if len(t) else 0
    st.markdown(f"<div class='pd-box'><span class='pd-h'>P&L {day:%d %b}</span><br><span class='pd-big'>{_money(tot)}</span>"
                f"<span class='pd-m'> total · booked {_money(net)} · charges ₹{ch:,.0f}<br>"
                f"{len(t)} closed · {wins} wins · {len(t) - wins} losses"
                f"{f' · win {100 * wins / len(t):.0f}%' if len(t) else ''}</span></div>", unsafe_allow_html=True)
    st.markdown("<div class='pd-sec'>BY STRIKE · EXPIRY</div>", unsafe_allow_html=True)
    for r in inst.sort_values("total").to_dict("records"):
        cls = r["side"].lower()
        line2 = (f"{int(r['qty'])} qty · buy {r['avg_buy']:.2f} → sell {r['avg_sell']:.2f}<br>gross {_money(r['gross'])} − "
                 f"charges ₹{r['charges']:,.2f} = net {_money(r['net'])}" if r["qty"] else "")
        op = (f"{' · ' if line2 else ''}open {int(r['open_qty'])} · MTM {_money(r['open_pnl'])}" if r["open_qty"] else "")
        st.markdown(f"<div class='pd-row {cls}'><b>{r['instrument']}</b><span style='float:right' class='pd-big'>"
                    f"{_money(r['total'])}</span><br><span class='pd-m'>{line2}{op}</span></div>", unsafe_allow_html=True)
    if len(t):
        rc = _rates()
        tot = order_charges(s, day, rc)                     # per ORDER, exactly like the broker's contract note
        if not any(tot.values()):                           # trades from before the desk logged orders
            tot = {k: 0.0 for k, _ in CHG_LABELS}
            for r in t.to_dict("records"):
                for leg, px_ in (("BUY", r["entry_price"]), ("SELL", r["exit_price"])):
                    ch = leg_charges(px_, r["quantity"], leg, rc)
                    for kk, _ in CHG_LABELS:
                        tot[kk] += ch[kk]
        gross = float(t["pnl_rupees"].sum())
        st.markdown(f"<div class='pd-row'><b>Gross {_money(gross)} − charges ₹{ch_total_booked(t):,.2f} = NET "
                    f"{_money(float(t['net_pnl_rupees'].sum()))}</b><br><span class='pd-m'>"
                    + " · ".join(f"{lab} ₹{tot[k]:,.2f}" for k, lab in CHG_LABELS if tot[k] >= 0.005)
                    + "</span></div>", unsafe_allow_html=True)
    txt = report_text(day, t, inst, tot if len(t) else None)
    c = st.columns(2)
    csv = t[["entry_time", "exit_time", "instrument", "strike", "opt_type", "expiry", "quantity", "entry_price", "exit_price",
             "pnl_rupees", "charges_rupees", "net_pnl_rupees", "exit_reason"]].rename(columns={
        "opt_type": "type", "quantity": "qty", "entry_price": "buy", "exit_price": "sell", "pnl_rupees": "gross_pnl",
        "charges_rupees": "charges", "net_pnl_rupees": "net_pnl", "exit_reason": "exit_reason"}) if len(t) else pd.DataFrame()
    c[0].download_button("⬇ Excel (CSV)", csv.to_csv(index=False).encode("utf-8"), file_name=f"paper_trades_{day:%Y-%m-%d}.csv",
                         mime="text/csv", use_container_width=True, key="pd_dl_csv", disabled=csv.empty)
    c[1].download_button("⬇ Report (TXT)", txt.encode("utf-8"), file_name=f"paper_report_{day:%Y-%m-%d}.txt",
                         mime="text/plain", use_container_width=True, key="pd_dl_txt")
    with st.expander("📋 Copy for WhatsApp / mentor"):
        st.code(txt, language=None)


def _settings_ui(st, s: dict) -> None:
    pop = st.popover("⚙ Desk settings", use_container_width=True) if hasattr(st, "popover") else st.expander("⚙ Desk settings")
    with pop:
        c = st.columns(2)
        s["lots"] = c[0].number_input("Default lots", 1, 100, int(s["lots"]), key="pd_lots")
        s["product"] = c[1].selectbox("Product", ["INTRADAY", "NORMAL"], index=["INTRADAY", "NORMAL"].index(s["product"]),
                                      key="pd_prod")
        c = st.columns(3)
        s["sl_pts"] = c[0].number_input("SL pts", 0.0, 1000.0, float(s["sl_pts"]), 1.0, key="pd_sl")
        s["tgt_pts"] = c[1].number_input("Target", 0.0, 2000.0, float(s["tgt_pts"]), 1.0, key="pd_tg")
        s["trail_pts"] = c[2].number_input("Trail", 0.0, 1000.0, float(s["trail_pts"]), 1.0, key="pd_tr")
        st.caption("0 = off. Used by one-click BUY and as ticket defaults.")
        st.caption("Greek weights — order of importance: Delta › Theta › Gamma › Vega")
        w = s["weights"]
        for g_, lab in (("delta", "Delta"), ("theta", "Theta"), ("gamma", "Gamma"), ("vega", "Vega")):
            w[g_] = st.slider(lab, 0, 100, int(w[g_]), key=f"pd_w{g_}")
        s["delta_target"] = st.slider("Preferred |Delta|", 0.20, 0.80, float(s["delta_target"]), 0.05, key="pd_dt")
        c = st.columns(2)
        s["top_n"] = c[0].selectbox("Picks per side", [1, 2, 3, 4, 5], index=[1, 2, 3, 4, 5].index(int(s["top_n"])), key="pd_n")
        opts = [1, 2, 3, 5, 10]
        s["refresh_s"] = c[1].selectbox("Refresh (s)", opts, index=opts.index(int(s["refresh_s"])) if int(s["refresh_s"]) in opts else 1,
                                        key="pd_rf")
        disk = load_state()
        keep = ("ids", "orders", "meta")
        if {k: v for k, v in s.items() if k not in keep} != {k: v for k, v in disk.items() if k not in keep}:
            for k in keep:
                s[k] = disk[k]
            save_state(s)
            st.caption("✔ saved")
        acc = pt.load_paper_account()
        st.caption(f"Cash ₹{acc.cash_balance:,.0f} · used ₹{acc.used_margin:,.0f} · equity ₹{acc.total_equity:,.0f}")
        if st.button("➕ Add ₹1,00,000 paper cash", use_container_width=True, key="pd_refill"):
            pt.refill_paper_capital(100000.0, reset_all=False)
            st.toast("₹1,00,000 added")


def _marks(s: dict, chain_for) -> float:
    """Open-position MTM WITHOUT drawing anything (phone tabs) — prices come from the in-memory feed, so it is instant."""
    import streamlit as st
    acc = pt.load_paper_account()
    g = groups(acc, s)
    st.session_state["_pd_open"], st.session_state["_pd_ltp_pid"], st.session_state["_pd_pos_info"] = {}, {}, {}
    sids = [(s["ids"].get(ps[0].position_id) or {}).get("sid", "") for ps in g.values()]
    q = quotes([x for x in sids if x]) if sids else {}
    mtm = 0.0
    for k, ps in g.items():
        strike, side, e, prod = k.split("|")
        qty = sum(p.quantity for p in ps)
        avg = sum(p.entry_price * p.quantity for p in ps) / qty
        sid = (s["ids"].get(ps[0].position_id) or {}).get("sid", "")
        p = price(float(strike), side, e, sid, chain_for(e), q)
        ltp = p["ltp"] if p and np.isfinite(p["ltp"]) else float(ps[0].current_price or avg)
        pnl = (ltp - avg) * qty
        mtm += pnl
        st.session_state["_pd_open"][k] = dict(strike=int(strike), side=side, expiry=e, qty=qty, avg=avg, ltp=ltp,
                                               pnl=pnl, product=prod)
        rc = _rates()
        bid = _bid(p) if p and np.isfinite(p.get("ltp", np.nan)) else ltp
        c_all = sum(leg_charges(x.entry_price, x.quantity, "BUY", rc)["total"] for x in ps) + leg_charges(bid, qty, "SELL", rc)["total"]
        hi = max(float((s["ids"].get(x.position_id) or {}).get("hi") or x.entry_price) for x in ps)
        st.session_state.setdefault("_pd_pos_info", {})[k] = dict(avg=avg, ltp=ltp, bid=bid, be=avg + c_all / qty, hi=hi, qty=qty)
        for x in ps:
            st.session_state["_pd_ltp_pid"][x.position_id] = ltp
    return mtm


def _buckets(st, s: dict) -> None:
    """Today per strike · side · expiry: open MTM + booked net (after charges) — the Dhan 'positions' list."""
    today = ist_now().date()
    inst = by_instrument(day_trades(today, s), st.session_state.get("_pd_open", {}))
    st.markdown("<div class='pd-sec'>TODAY BY STRIKE (booked net + open)</div>", unsafe_allow_html=True)
    if inst.empty:
        st.caption("no trades yet today")
        return
    g_, c_, n_ = float(inst["gross"].sum()), float(inst["charges"].sum()), float(inst["net"].sum())
    o_ = float(inst["open_pnl"].sum())
    st.markdown(f"<div class='pd-box'><span class='pd-m'>Booked today</span><br>Gross <b class='{'pd-g' if g_ >= 0 else 'pd-r'}'>"
                f"{_money(g_)}</b> − charges <b class='pd-r'>₹{c_:,.2f}</b> = Net <b class='pd-big {'pd-g' if n_ >= 0 else 'pd-r'}'>"
                f"{_money(n_)}</b>" + (f"<br><span class='pd-m'>+ open MTM {_money(o_)} (charges on open buys come off when sold)</span>"
                                       if inst["open_qty"].sum() else "") + "</div>", unsafe_allow_html=True)
    for r in inst.sort_values("total").to_dict("records"):
        bits = []
        if r["qty"]:
            bits.append(f"closed {int(r['qty'])} qty · buy {r['avg_buy']:.2f} → sell {r['avg_sell']:.2f}<br>"
                        f"gross {_money(r['gross'])} − charges ₹{r['charges']:,.2f} = <b>net {_money(r['net'])}</b>")
        if r["open_qty"]:
            bits.append(f"<b class='pd-y'>OPEN</b> {int(r['open_qty'])} qty · MTM {_money(r['open_pnl'])}")
        st.markdown(f"<div class='pd-row {r['side'].lower()}'><b>{r['instrument']}</b><span style='float:right' "
                    f"class='pd-big'>{_money(r['total'])}</span><br><span class='pd-m'>{'<br>'.join(bits)}</span></div>",
                    unsafe_allow_html=True)


def _funds(st, s: dict, ds: dict) -> None:
    acc = pt.load_paper_account()
    op = st.session_state.get("_pd_open", {})
    used = sum(o["avg"] * o["qty"] for o in op.values())
    legs = day_legs(ist_now().date())
    open_buy_chg = sum(x["total"] for x in legs if x.get("open"))
    lg = ledger()
    closed_bal = float(lg["balance"].iloc[-1]) if len(lg) else float(acc.initial_capital)
    eq = acc.cash_balance + used + ds["open"]
    rows = [("Starting paper money", f"₹{acc.initial_capital:,.0f}"),
            ("Balance after all closed trades", f"₹{closed_bal:,.2f}"),
            ("− premium in open positions", f"₹{used:,.2f}"),
            ("= Available cash now", f"<b>₹{acc.cash_balance:,.2f}</b>"),
            ("Open positions MTM (live)", _money(ds["open"])),
            ("Charges on open buys (deducted at sell)", f"₹{open_buy_chg:,.2f}"),
            ("Booked today (net of charges)", _money(ds["realised"])),
            ("Charges today (all orders)", f"₹{(legs[-1]['cum'] if legs else 0):,.2f}"),
            ("Account value now", f"<b>₹{eq:,.2f}</b>"),
            ("Overall P&L since start", _money(eq - acc.initial_capital))]
    st.markdown("<div class='pd-sec'>FUNDS · live</div><div class='pd-box'>" + "".join(
        f"<div style='display:flex;justify-content:space-between;padding:3px 0;border-bottom:1px solid #1e293b'>"
        f"<span class='pd-m'>{a}</span><span class='pd-w'>{b}</span></div>" for a, b in rows) + "</div>",
        unsafe_allow_html=True)
    gap = closed_bal - used - acc.cash_balance
    if abs(gap) >= 1:
        st.caption(f"Engine cash differs from the ledger by ₹{gap:,.2f} (trades from the older 📒 Paper Trading tab "
                   f"or a manual cash change).")
    if len(lg):
        rows = "".join(
            f"<tr><td style='white-space:nowrap'>{pd.Timestamp(r.day):%d %b}</td><td>{r.trades}</td>"
            f"<td>{_money(r.gross)}</td><td class='pd-r'>−₹{r.charges:,.0f}</td>"
            f"<td style='white-space:nowrap'><b class='{'pd-g' if r.net >= 0 else 'pd-r'}'>{'Cr' if r.net >= 0 else 'Dr'} ₹{abs(r.net):,.0f}</b></td>"
            f"<td>₹{r.balance:,.0f}</td></tr>" for r in lg.iloc[::-1].itertuples())
        tot = (f"<tr style='border-top:1px solid #334155'><td><b>Total</b></td><td><b>{int(lg.trades.sum())}</b></td>"
               f"<td><b>{_money(lg.gross.sum())}</b></td><td class='pd-r'><b>−₹{lg.charges.sum():,.0f}</b></td>"
               f"<td><b>{_money(lg.net.sum())}</b></td><td></td></tr>")
        st.markdown("<div class='pd-sec'>DAY-WISE LEDGER (credit / debit)</div><div class='pd-box' style='overflow-x:auto'>"
                    "<style>.pdt td{padding:3px 3px !important;border:none !important}</style><table class='pdt' style='width:100%;font-size:.72rem;color:#f1f5f9;border-collapse:collapse'>"
                    "<tr class='pd-m'><td>Date</td><td>Trades</td><td>Gross P&L</td><td>Charges</td><td>Net Cr/Dr</td>"
                    f"<td>Balance</td></tr>{rows}{tot}</table></div>", unsafe_allow_html=True)
        st.download_button("⬇ Ledger (CSV)", lg.to_csv(index=False).encode(), file_name="paper_ledger.csv",
                           mime="text/csv", use_container_width=True, key="pd_dl_ledger")
    _settings_ui(st, s)


def day_legs(day: date, st_: dict | None = None) -> list[dict]:
    """Every executed order of `day`, rebuilt from trades.csv (+ open positions), oldest first.
    Charges are allocated so the running total equals exactly what the engine deducted."""
    rc = _rates()
    h = pt.load_trade_history()
    ds = day.strftime("%Y-%m-%d")
    legs = []
    sd = st_ if st_ is not None else load_state()
    if len(h) and "exit_time" in h:
        h = h.copy()
        for c in ("quantity", "entry_price", "exit_price", "charges_rupees", "strike"):
            h[c] = pd.to_numeric(h[c], errors="coerce").fillna(0)
        h["base"] = h["trade_id"].astype(str).str.replace(r"-P\d+$", "", regex=True)
        h = h[h["entry_time"].astype(str).str.startswith(ds) | h["exit_time"].astype(str).str.startswith(ds)]
        for base, g in h.groupby("base"):
            qty_all = float(g["quantity"].sum())
            r0 = g.iloc[0]
            buy = leg_charges(float(r0["entry_price"]), qty_all, "BUY", rc)
            if str(r0["entry_time"]).startswith(ds):
                legs.append(dict(ts=str(r0["entry_time"]), leg="BUY", strike=int(r0["strike"]), side=r0["opt_type"],
                                 expiry=_expiry_of(dict(r0), sd),
                                 qty=int(qty_all), px=float(r0["entry_price"]), brokerage=buy["brokerage"], total=buy["total"],
                                 cat=buy))
            fixed = buy["brokerage"] * (1 + float(rc.get("gst_rate", 0.18)))   # buy order's ₹20 + GST: booked on the 1st exit
            var = buy["total"] - fixed
            first_exit = str(g.sort_values("exit_time").iloc[0]["trade_id"])
            for r in g.to_dict("records"):
                share = var * float(r["quantity"]) / qty_all if qty_all else 0.0
                share += fixed if str(r["trade_id"]) == first_exit else 0.0
                if not str(r["exit_time"]).startswith(ds):
                    continue
                sell = leg_charges(float(r["exit_price"]), float(r["quantity"]), "SELL", rc)
                tot = float(r["charges_rupees"]) - share           # what was actually deducted for this sell
                legs.append(dict(ts=str(r["exit_time"]), leg="SELL", strike=int(r["strike"]), side=r["opt_type"],
                                 expiry=_expiry_of(r, sd),
                                 qty=int(r["quantity"]), px=float(r["exit_price"]), brokerage=sell["brokerage"], total=tot,
                                 cat=sell, why=str(r.get("exit_reason", ""))))
    for p in pt.load_paper_account().open_positions:                # buys still open
        if str(p.entry_time).startswith(ds) and p.side.upper() == "BUY":
            b = leg_charges(p.entry_price, p.quantity, "BUY", rc)
            legs.append(dict(ts=str(p.entry_time), leg="BUY", strike=int(p.strike), side=p.opt_type, qty=int(p.quantity),
                             expiry=str((sd.get("ids", {}).get(p.position_id) or {}).get("expiry", "")),
                             px=float(p.entry_price), brokerage=b["brokerage"], total=b["total"], cat=b, open=True))
    legs.sort(key=lambda x: x["ts"])
    merged = []                                                      # one netted exit = one SELL order
    for x in legs:
        y = merged[-1] if merged else None
        if y and x["leg"] == "SELL" and y["leg"] == "SELL" and x["ts"] == y["ts"] and x["strike"] == y["strike"] \
                and x["side"] == y["side"] and abs(x["px"] - y["px"]) < 1e-6:
            y["qty"] += x["qty"]; y["total"] += x["total"]
            y["cat"] = leg_charges(y["px"], y["qty"], "SELL", rc)          # ONE sell order → one ₹20
            continue
        merged.append(dict(x))
    run = 0.0
    for x in merged:
        run += x["total"]
        x["cum"] = run
    return merged


STMT_COLS = ["Date", "Scrip Name", "Exchange", "Bill No.", "Buy Qty.", "Buy Value", "Sell Qty.", "Sell Value", "Brokerage",
             "GST", "STT", "SEBI Fees", "Stamp Duty", "Txn. Charges", "Oth. Charges", "Gross Amount"]


def statement(d0: date, d1: date) -> pd.DataFrame:
    """Paper trades in Dhan's Global-Transaction layout: one row per day × contract, every charge in its own column.
    Gross Amount = Sell − Buy − all charges (Dhan's sign). Totals tie to the money the engine deducted."""
    h = pt.load_trade_history()
    days_ = set()
    if len(h) and "exit_time" in h:
        for c in ("entry_time", "exit_time"):
            days_ |= set(pd.to_datetime(h[c].astype(str).str[:10], errors="coerce").dropna().dt.date)
    days_ |= {ist_now().date()}
    sd = load_state()
    rows = []
    for d in sorted(x for x in days_ if d0 <= x <= d1):
        for x in day_legs(d, sd):
            if x.get("open"):                       # unsold buys join the statement when they are sold
                continue
            e = _d(x.get("expiry")) if x.get("expiry") else None
            scrip = f"OPT NIFTY {e:%d %b %Y} {x['strike']} {x['side']}" if e else f"OPT NIFTY {x['strike']} {x['side']}"
            buy = x["leg"] == "BUY"
            c = x["cat"]
            rows.append({"Date": f"{d:%d %b %Y} 00:00:00", "Scrip Name": scrip, "Exchange": "NSE", "Bill No.": "PAPER",
                         "Buy Qty.": x["qty"] if buy else 0, "Buy Value": x["qty"] * x["px"] if buy else 0.0,
                         "Sell Qty.": 0 if buy else x["qty"], "Sell Value": 0.0 if buy else x["qty"] * x["px"],
                         "Brokerage": c.get("brokerage", 0), "GST": c.get("gst", 0), "STT": c.get("stt", 0),
                         "SEBI Fees": c.get("sebi", 0), "Stamp Duty": c.get("stamp", 0),
                         "Txn. Charges": c.get("exchange", 0) + c.get("ipft", 0),
                         "Oth. Charges": x["total"] - sum(c.get(k, 0) for k, _ in CHG_LABELS),   # older-version booking diff
                         "_open": bool(x.get("open"))})
    if not rows:
        return pd.DataFrame(columns=STMT_COLS)
    df = pd.DataFrame(rows)
    num = [c for c in STMT_COLS[4:-1]]
    g = df.groupby(["Date", "Scrip Name", "Exchange", "Bill No."], sort=False)[num].sum().reset_index()
    ch = g[["Brokerage", "GST", "STT", "SEBI Fees", "Stamp Duty", "Txn. Charges", "Oth. Charges"]].sum(axis=1)
    g["Gross Amount"] = g["Sell Value"] - g["Buy Value"] - ch
    g["_d"] = pd.to_datetime(g["Date"], format="%d %b %Y %H:%M:%S")
    g = g.sort_values(["_d", "Scrip Name"]).drop(columns="_d")
    return g[STMT_COLS].round(2)


def statement_csv(df: pd.DataFrame, d0: date, d1: date) -> bytes:
    """Same layout as Dhan's report (trade_report.py can read it)."""
    ch = df[["Brokerage", "GST", "STT", "SEBI Fees", "Stamp Duty", "Txn. Charges", "Oth. Charges"]].sum().sum() if len(df) else 0.0
    gross = float((df["Sell Value"] - df["Buy Value"]).sum()) if len(df) else 0.0
    head = (f"Global transction report,From {d0:%d-%m-%Y} to {d1:%d-%m-%Y}\nName,Paper Desk\nUCC,PAPER\nMobile,-\n"
            f"Email ID,-\n\n")
    body = ",".join(STMT_COLS) + "\n" + df.to_csv(index=False, header=False, quoting=1)   # header plain, values quoted (Dhan)
    foot = (f"\nNet P&L,{gross - ch:.4f},Brokerage,{df['Brokerage'].sum() if len(df) else 0:.2f},Gross P&L,{gross:.4f},"
            f"Total Charges,{ch:.4f}\n")
    return (head + body + foot).encode("utf-8")


def _statement_ui(st) -> None:
    today = ist_now().date()
    per = _seg(st, "Period", ["Today", "This week", "This month", "From – To"], "pd_st_per", "Today")
    if per == "Today":
        d0 = d1 = today
    elif per == "This week":
        d0, d1 = today - timedelta(days=today.weekday()), today
    elif per == "This month":
        d0, d1 = today.replace(day=1), today
    else:
        c = st.columns(2)
        d0 = c[0].date_input("From", value=today - timedelta(days=7), max_value=today, key="pd_st_d0", format="DD/MM/YYYY")
        d1 = c[1].date_input("To", value=today, max_value=today, key="pd_st_d1", format="DD/MM/YYYY")
        if d0 > d1:
            d0, d1 = d1, d0
    df = statement(d0, d1)
    if df.empty:
        st.caption(f"No paper trades {d0:%d %b} – {d1:%d %b}.")
        return
    chc = ["Brokerage", "GST", "STT", "SEBI Fees", "Stamp Duty", "Txn. Charges", "Oth. Charges"]
    ch = float(df[chc].sum().sum())
    gross = float((df["Sell Value"] - df["Buy Value"]).sum())
    st.markdown(f"<div class='pd-row'><b>{d0:%d %b} – {d1:%d %b}</b> · {df['Date'].nunique()} day(s) · "
                f"{int(round(df['Brokerage'].sum() / 20))} orders<br>Gross P&L <b class='{'pd-g' if gross >= 0 else 'pd-r'}'>"
                f"{_money(gross)}</b> − charges <b class='pd-r'>₹{ch:,.2f}</b> = Net <b class='{'pd-g' if gross - ch >= 0 else 'pd-r'}'>"
                f"{_money(gross - ch)}</b><br><span class='pd-m'>"
                + " · ".join(f"{c} ₹{df[c].sum():,.2f}" for c in chc
                             if abs(df[c].sum()) >= (0.5 if c == "Oth. Charges" else 0.005)) + "</span></div>",
                unsafe_allow_html=True)
    rows = "".join(
        f"<tr><td style='white-space:nowrap'>{r['Date'][:6]}</td><td style='white-space:nowrap'>"
        f"{r['Scrip Name'].replace('OPT NIFTY ', '').replace(' 2026', '')}</td><td>{int(r['Buy Qty.'])}/{int(r['Sell Qty.'])}</td>"
        f"<td>{_money(r['Sell Value'] - r['Buy Value'])}</td><td>₹{sum(r[c] for c in chc):,.2f}</td>"
        f"<td><b class='{'pd-g' if r['Gross Amount'] >= 0 else 'pd-r'}'>{_money(r['Gross Amount'])}</b></td></tr>"
        for _, r in df.iterrows())
    st.markdown("<div class='pd-box' style='overflow-x:auto'><style>.pdt td{padding:3px 3px !important;border:none !important}</style>"
                "<table class='pdt' style='width:100%;font-size:.72rem;color:#f1f5f9;border-collapse:collapse'>"
                "<tr class='pd-m'><td>Date</td><td>Contract</td><td>B/S qty</td><td>Gross</td><td>Charges</td><td>Net</td></tr>"
                f"{rows}</table><span class='pd-m'>Net = Sell − Buy − all charges · full charge columns in the CSV</span></div>",
                unsafe_allow_html=True)
    st.download_button("⬇ Statement CSV (Dhan format)", statement_csv(df, d0, d1),
                       file_name=f"Paper_GlobalTransaction_{d0:%d-%m-%Y}_{d1:%d-%m-%Y}.csv", mime="text/csv",
                       use_container_width=True, key="pd_dl_stmt")


def ledger() -> pd.DataFrame:
    """Date-wise: trades, gross P&L, charges, net (credit / debit) and running balance from the starting paper money."""
    acc = pt.load_paper_account()
    h = pt.load_trade_history()
    if not len(h) or "exit_time" not in h:
        return pd.DataFrame()
    h = h.copy()
    for c in ("pnl_rupees", "charges_rupees", "net_pnl_rupees"):
        h[c] = pd.to_numeric(h[c], errors="coerce").fillna(0)
    h["day"] = h["exit_time"].astype(str).str[:10]
    d = h.groupby("day").agg(trades=("net_pnl_rupees", "size"), gross=("pnl_rupees", "sum"),
                             charges=("charges_rupees", "sum"), net=("net_pnl_rupees", "sum"),
                             wins=("net_pnl_rupees", lambda v: int((v > 0).sum()))).reset_index().sort_values("day")
    d["balance"] = float(acc.initial_capital) + d["net"].cumsum()
    return d


def _charges_tab(st, s: dict) -> None:
    st.markdown("<div class='pd-sec'>STATEMENT · like Dhan's report (any period)</div>", unsafe_allow_html=True)
    _statement_ui(st)
    rc = _rates()
    legs = day_legs(ist_now().date())
    run = legs[-1]["cum"] if legs else 0.0
    open_part = sum(x["total"] for x in legs if x.get("open"))
    grp = {}
    cat = {k: 0.0 for k, _ in CHG_LABELS}
    for x in legs:
        g = grp.setdefault(f"{x['side']} {x['leg']}", [0, 0.0, 0.0])
        g[0] += 1; g[1] += x["total"]; g[2] += x["brokerage"]
        for k, _ in CHG_LABELS:
            cat[k] += x["cat"].get(k, 0.0)
    adj = run - sum(cat.values())
    cards = "".join(
        f"<div class='pd-row {k.split()[0].lower()}' style='flex:1;min-width:46%;margin:2px'><b>{k}</b><br>"
        f"<span class='pd-m'>{grp[k][0] if k in grp else 0} orders · brokerage ₹{grp[k][2] if k in grp else 0:,.0f}</span><br>"
        f"<b>₹{grp[k][1] if k in grp else 0:,.2f}</b></div>"
        for k in ("CE BUY", "CE SELL", "PE BUY", "PE SELL"))
    st.markdown(f"<div class='pd-sec'>TODAY'S CHARGES · ₹{run:,.2f} on {len(legs)} orders</div>"
                f"<div class='pd-m' style='color:#64748b;margin:-4px 0 4px'>₹{run - open_part:,.2f} deducted on closed trades + "
                f"₹{open_part:,.2f} on buys still open (deducted when you sell)</div>"
                f"<div style='display:flex;flex-wrap:wrap'>{cards}</div>", unsafe_allow_html=True)
    st.markdown("<div class='pd-box'>" + "".join(
        f"<div style='display:flex;justify-content:space-between'><span class='pd-m'>{lab}</span>"
        f"<span class='pd-w'>₹{cat[k]:,.2f}</span></div>" for k, lab in CHG_LABELS) +
        (f"<div style='display:flex;justify-content:space-between'><span class='pd-m'>Booking adjustment "
         f"(older desk versions)</span><span class='pd-w'>₹{adj:,.2f}</span></div>" if abs(adj) >= 0.5 else "") +
        f"<div style='display:flex;justify-content:space-between;border-top:1px solid #334155;margin-top:4px'>"
        f"<b class='pd-w'>Total today</b><b class='pd-w'>₹{run:,.2f}</b></div></div>", unsafe_allow_html=True)
    lg = ledger()
    if len(lg):
        st.markdown(f"<div class='pd-row'><b>Cumulative since start: ₹{lg['charges'].sum() + open_part:,.2f}</b><br>"
                    f"<span class='pd-m'>{int(lg['trades'].sum())} closed trades over {len(lg)} day(s) · ≈ "
                    f"₹{lg['charges'].sum() / max(1, lg['trades'].sum()):.0f} per round trip</span></div>",
                    unsafe_allow_html=True)
    if legs:
        rows = "".join(
            f"<tr><td>{x['ts'][11:16]}</td><td class='{'pd-g' if x['leg'] == 'BUY' else 'pd-y'}'>{x['leg']}"
            f"{'*' if x.get('open') else ''}</td><td style='white-space:nowrap'>{x['strike']} {x['side']}</td>"
            f"<td>{x['qty']}</td><td>{x['px']:.2f}</td><td>₹{x['total']:.2f}</td>"
            f"<td style='white-space:nowrap'><b>₹{x['cum']:,.2f}</b></td></tr>" for x in reversed(legs))
        st.markdown("<div class='pd-sec'>EVERY ORDER (newest first · Cumulative adds up from the day's 1st order)</div>"
                    "<div class='pd-box' style='overflow-x:auto'>"
                    "<style>.pdt td{padding:3px 3px !important;border:none !important}</style><table class='pdt' style='width:100%;font-size:.72rem;color:#f1f5f9;border-collapse:collapse'>"
                    "<tr class='pd-m'><td>Time</td><td>Side</td><td>Strike</td><td>Qty</td><td>Price</td>"
                    f"<td>Charges</td><td>Cumul.</td></tr>{rows}</table>"
                    "<span class='pd-m'>* = buy still open · every order ₹20 brokerage + taxes</span></div>",
                    unsafe_allow_html=True)
    st.caption(f"Dhan: ₹{float(rc.get('brokerage_per_order', 20)):.0f}/order · STT {100 * float(rc.get('stt_sell_rate', STT_2026)):.2f}% "
               "sell · NSE 0.03503% · SEBI ₹10/cr · stamp 0.003% buy · IPFT 0.0005% · GST 18%")


def desk_body(mobile: bool = False) -> None:
    import streamlit as st
    st.markdown(CSS, unsafe_allow_html=True)
    for text, icon in st.session_state.pop("_pd_msgs", []):
        _toast(st, text, icon)
    s = load_state()
    now = ist_now()
    _rates()                                                # one-time STT 0.10 % → 0.15 % upgrade of paper_config.json
    exps = live_expiries(now)
    if exps and st.session_state.get("pd_exp") not in exps:
        st.session_state["pd_exp"] = exps[0]
    sel = st.session_state.get("pd_exp") or (exps[0] if exps else None)
    chain = cached_chain(sel)
    seen: dict = {}

    def chain_for(e):
        if e not in seen:
            seen[e] = chain if str(e) == str(sel) else (cached_chain(e) if e in exps[:2] else None)
        return seen[e]

    if chain is None:
        st.markdown("<div class='pd-box'><span class='pd-h'>📝 PAPER DESK</span><br><span class='pd-m'>No prices yet — "
                    "check the Dhan token below.</span></div>", unsafe_allow_html=True)
        return
    age = (now - chain["at"]).total_seconds()
    live_ok = bool(chain.get("live") and age <= float(s["stale_s"]) and market_open(now))
    st.session_state["_pd_live"] = live_ok
    for m in process(s, now, chain):
        _toast(st, m, "🔔")
    try:
        rr = chain["rows"]
        st.session_state["_pd_ask"] = {f"{int(r.strike)}{r.side}{chain['expiry']}": float(r.ask if np.isfinite(r.ask) and r.ask > 0 else r.ltp)
                                       for r in rr.itertuples()}
    except Exception:                                       # noqa: BLE001
        pass
    s = load_state()

    hdr = st.container()
    _ticket(st, s, now, chain_for)
    if mobile:                                              # phone: Dhan-style tabs, each draws only itself
        tab = _seg(st, "Tab", ["★ Watch", "Positions", "Orders", "Funds", "Charges"], "pd_mtab", "★ Watch")
        if tab == "Positions":
            mtm = _positions(st, s, now, chain_for, live_ok)
            _buckets(st, s)
        else:
            mtm = _marks(s, chain_for)
        ds = day_stats(mtm)
    else:
        mtm = _positions(st, s, now, chain_for, live_ok)
        ds = day_stats(mtm)
    e = _d(chain.get("expiry"))
    dte = f"{(e - now.date()).days}d" if e else ""
    status = ("<span class='pd-live'>● LIVE</span>" if live_ok else
              "<span class='pd-y'>● AMO</span> <span class='pd-m'>market closed — orders fill at 09:15</span>"
              if not market_open(now) else "<span class='pd-r'>● STALE</span>")
    acc = pt.load_paper_account()
    hdr.markdown(
        f"<div class='pd-box'><span class='pd-h'>📝 PAPER DESK</span> &nbsp;{status}<br>"
        f"<span class='pd-m'>NIFTY <b class='pd-w'>{chain['spot']:,.1f}</b> · {f'{e:%d %b}' if e else '?'} ({dte}) · {chain['source']} · "
        f"{_ago(age)}</span><br><span class='pd-big'>{_money(ds['realised'] + ds['open'])}</span>"
        f"<span class='pd-m'> today · booked {_money(ds['realised'])} · open {_money(ds['open'])}<br>"
        f"{ds['trades']} trades · {ds['win_pct']:.0f}% win · charges ₹{ds['charges']:,.0f} · cash ₹{acc.cash_balance:,.0f}</span></div>",
        unsafe_allow_html=True)

    if mobile:
        _header_done = True
        if tab == "★ Watch":
            v2 = _seg(st, "Watch", ["★ Picks", "⛓ Chain"], "pd_mwatch", "★ Picks")
            if exps:
                st.selectbox("Expiry", exps[:4], key="pd_exp", label_visibility="collapsed",
                             format_func=lambda x: f"Expiry {_d(x):%a %d %b}" + (" · nearest" if x == exps[0] else ""))
            _ladder(st, s, chain) if v2 == "⛓ Chain" else _picks(st, s, chain)
        elif tab == "Orders":
            _orders(st, s)
            with st.expander("📊 Day report · mentor · CSV"):
                _pnl_view(st, s)
        elif tab == "Funds":
            _funds(st, s, ds)
        elif tab == "Charges":
            _charges_tab(st, s)
        return
    view = _seg(st, "View", ["★ Picks", "⛓ Chain", "🧾 Orders", "📊 P&L"], "pd_view", "★ Picks")
    if exps and view in ("★ Picks", "⛓ Chain"):
        st.selectbox("Expiry", exps[:4], key="pd_exp", label_visibility="collapsed",
                     format_func=lambda x: f"Expiry {_d(x):%a %d %b}" + (" · nearest" if x == exps[0] else ""))
    if view == "⛓ Chain":
        _ladder(st, s, chain)
    elif view == "🧾 Orders":
        _orders(st, s)
    elif view == "📊 P&L":
        _pnl_view(st, s)
    else:
        _picks(st, s, chain)
    _settings_ui(st, s)


def render_page() -> None:
    """Standalone desk (paper_desk_app.py): same desk in the main area — no heavy dashboard reruns to wait behind."""
    import streamlit as st
    ensure_feed()
    s = load_state()
    frag = getattr(st, "fragment", None) or getattr(st, "experimental_fragment", None)
    if frag is None:
        desk_body(mobile=True)
        return
    n = int(s["refresh_s"])
    k = f"page{n}"
    if k not in _mem["frag"]:
        _mem["frag"][k] = frag(run_every=n)(lambda: desk_body(mobile=True))
    _mem["frag"][k]()


def render_sidebar() -> None:
    """Call once near the top of dashboard.py — draws the desk at the top of the sidebar on every tab."""
    import streamlit as st
    ensure_feed()
    s = load_state()
    if not s.get("_feed_v1"):                               # one-time: 3 s → 1 s refresh now that redraws are instant
        s["_feed_v1"] = True
        if int(s.get("refresh_s", 1)) == 3:
            s["refresh_s"] = 1
        save_state(s)
    frag = getattr(st, "fragment", None) or getattr(st, "experimental_fragment", None)
    with st.sidebar:
        if frag is None:
            desk_body()
            st.caption("Upgrade Streamlit (≥1.37) for the live 3-second desk.")
        else:
            n = int(s["refresh_s"])
            if n not in _mem["frag"]:
                _mem["frag"][n] = frag(run_every=n)(desk_body)
            _mem["frag"][n]()
        st.markdown("---")
