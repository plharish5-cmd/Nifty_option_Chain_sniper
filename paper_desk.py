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
    "refresh_s": 3, "stale_s": 120, "squareoff": "15:20",
}
STEP = float(getattr(config, "STRIKE_STEP", 50))
LOT = int(getattr(config, "LOT_SIZE", 65))
OPEN_T, CLOSE_T, EXP_T = time(9, 15), time(15, 30), time(15, 30)
_mem: dict = {"exps": (0.0, []), "chain": {}, "frag": {}, "cc": {}, "qq": {}}


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
    return s


def save_state(s: dict) -> None:
    cut = (ist_now() - timedelta(days=3)).isoformat()
    s["orders"] = [o for o in s.get("orders", []) if o.get("status") == "OPEN" or o.get("ts", "") >= cut][-300:]
    try:
        tmp = DESK_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(s, indent=1, default=str))
        tmp.replace(DESK_FILE)
    except Exception:                                           # noqa: BLE001
        pass


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
                c = dict(rows=rows, spot=spot, expiry=exp, at=now, source="Dhan live", live=True)
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
    key = (tuple(sids), int(_time.time() // 2))
    if key in _mem["qq"]:
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
    """{ltp, bid, ask, src} for one instrument: chain of the same expiry first, else quote by security id."""
    if chain is not None and str(chain.get("expiry")) == str(expiry):
        r = chain["rows"]
        m = r[(r["strike"] == round(float(strike))) & (r["side"] == side)]
        if len(m):
            x = m.iloc[0]
            return dict(ltp=float(x.ltp), bid=float(x.bid), ask=float(x.ask), src="chain", sid=str(x.sid))
    if sid and sid in q and np.isfinite(q[sid]["ltp"]):
        return dict(**{k: q[sid][k] for k in ("ltp", "bid", "ask")}, src="quote", sid=sid)
    return None


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
    live = bool(ch and ch.get("live") and (now - ch["at"]).total_seconds() <= float(s["stale_s"]))
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
    save_state(s)
    why = "AMO — fills at the first live price after 09:15" if o["amo"] else "waiting for a live price"
    return True, f"⏳ {o['otype']} BUY {name(o['strike'], o['side'], o['expiry'])} queued · {why}"


def _exit(pos, px: float, reason: str, s: dict, spot: float, lots: int | None = None) -> tuple[bool, str]:
    meta = dict(s["ids"].get(pos.position_id) or {})
    if lots and lots < pos.lots:
        ok, msg, _ = pt.partial_exit_paper_position(pos.position_id, lots, spot=float(spot or 0), exit_reason=reason,
                                                    custom_exit_price=float(px))
    else:
        ok, msg, _ = pt.exit_paper_position(pos.position_id, spot=float(spot or 0), exit_reason=reason,
                                            custom_exit_price=float(px), notes=f"{pos.notes} · {reason} {px:.2f}")
        if ok:
            s["ids"].pop(pos.position_id, None)
    if ok:
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
        ch = chains.get(str(o["expiry"]))
        p = price(o["strike"], o["side"], o["expiry"], o.get("sid", ""), ch, q)
        if not p or not np.isfinite(p["ltp"]):
            continue
        a = _ask(p)
        spot = ch["spot"] if ch else (sel_chain or {}).get("spot", 0)
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


def day_stats(open_mtm: float) -> dict:
    h = pt.load_trade_history()
    today = ist_now().strftime("%Y-%m-%d")
    t = h[h["exit_time"].astype(str).str.startswith(today)] if len(h) and "exit_time" in h else pd.DataFrame()
    net = pd.to_numeric(t["net_pnl_rupees"], errors="coerce").fillna(0) if len(t) and "net_pnl_rupees" in t else pd.Series(dtype=float)
    return dict(realised=float(net.sum()), open=open_mtm, trades=len(t), wins=int((net > 0).sum()),
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


def cb_quick_buy(side: str, strike: int, expiry: str, sid: str) -> None:
    import streamlit as st
    if not _once(st, f"q{side}{strike}"):
        return
    s = load_state()
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


def cb_exit(key: str, frac: float = 1.0) -> None:
    import streamlit as st
    if not _once(st, f"x{key}{frac}"):
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
    left = total if frac >= 1 else max(1, int(total * frac))
    for x in sorted(ps, key=lambda y: -y.lots):
        if left <= 0:
            break
        n = min(left, x.lots)
        _exit(x, b, "Manual exit" if frac >= 1 else "Half exit", s, spot, n if n < x.lots else None)
        left -= n
    save_state(s)
    _msg(st, f"✅ SOLD {name(strike, side, expiry)} ×{(total if frac >= 1 else max(1, int(total * frac))) * LOT} @ {b:.2f}")


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


def cb_modify_open(key: str) -> None:
    import streamlit as st
    s, ps = _group_now(key)
    if not ps:
        return
    p = ps[0]
    st.session_state["pd_mod"] = key
    st.session_state["pd_m_sl"] = float(p.stop_loss or 0)
    st.session_state["pd_m_tg"] = float(p.target_1 or 0)
    st.session_state["pd_m_tr"] = float(p.trailing_sl_pts or 0)


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
    if not g:
        return mtm
    sids = []
    for k, ps in g.items():
        e = k.split("|")[2]
        ch = chain_for(e)
        if not (ch and str(ch.get("expiry")) == e):
            sids.append((s["ids"].get(ps[0].position_id) or {}).get("sid", ""))
    q = quotes(sids) if sids else {}
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
        pct = 100 * (ltp - avg) / avg if avg else 0
        sl = ps[0].stop_loss
        tg = ps[0].target_1
        tr = ps[0].trailing_sl_pts
        extra = " · ".join(x for x in (f"SL {sl:.2f}" if sl else "", f"T {tg:.2f}" if tg else "",
                                        f"trail {tr:.0f}" if tr else "") if x)
        st.markdown(f"<div class='pd-row {side.lower()}'><b>{name(strike, side, e)}</b> "
                    f"<span class='pd-m'>{'MIS' if prod == 'INTRADAY' else 'NRML'}</span>"
                    f"<span style='float:right' class='pd-big'>{_money(pnl)}</span><br>"
                    f"{qty} @ {avg:.2f} · LTP <b>{ltp:.2f}</b> "
                    f"<span class='{'pd-g' if pct >= 0 else 'pd-r'}'>({ltp - avg:+.2f}, {pct:+.1f}%)</span>"
                    f"{f'<br><span class=pd-m>{extra}</span>' if extra else ''}</div>", unsafe_allow_html=True)
        c = st.columns([1.3, 0.8, 0.8, 0.8])
        c[0].button("EXIT", key=f"pd_x_{k}", use_container_width=True, disabled=not live_ok, on_click=cb_exit, args=(k,),
                    help="Sell everything at the bid")
        c[1].button("+1", key=f"pd_a_{k}", use_container_width=True, on_click=cb_add, args=(k,),
                    help=f"Buy {int(s['lots'])} more lot(s) at the ask")
        c[2].button("½", key=f"pd_h_{k}", use_container_width=True, disabled=not live_ok or qty < 2 * LOT,
                    on_click=cb_exit, args=(k, 0.5), help="Sell half at the bid")
        c[3].button("✎", key=f"pd_e_{k}", use_container_width=True, on_click=cb_modify_open, args=(k,),
                    help="Modify SL / target / trailing SL")
        if st.session_state.get("pd_mod") == k:
            c = st.columns(3)
            c[0].number_input("SL ₹", 0.0, 100000.0, step=0.5, key="pd_m_sl", format="%.2f")
            c[1].number_input("Target ₹", 0.0, 100000.0, step=0.5, key="pd_m_tg", format="%.2f")
            c[2].number_input("Trail pts", 0.0, 1000.0, step=1.0, key="pd_m_tr")
            c = st.columns(2)
            c[0].button("Save", key="pd_m_save", use_container_width=True, on_click=cb_modify_save)
            c[1].button("Close", key="pd_m_close", use_container_width=True, on_click=cb_modify_close)
    if len(g) > 1:
        st.button("⏹ EXIT ALL at bid", key="pd_xall", use_container_width=True, disabled=not live_ok, on_click=cb_exit_all)
    return mtm


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


def _orders(st, s) -> None:
    op = [o for o in s["orders"] if o.get("status") == "OPEN"]
    if op:
        st.markdown("<div class='pd-sec'>OPEN ORDERS</div>", unsafe_allow_html=True)
        for o in op:
            c = st.columns([3.6, 0.8])
            lab = "AMO " if o.get("amo") else ""
            c[0].markdown(f"<div class='pd-row {o['side'].lower()}'>{lab}{o['otype']} BUY {name(o['strike'], o['side'], o['expiry'])}"
                          f"<br><span class='pd-m'>{int(o['lots']) * LOT} qty · "
                          f"{'@ ' + format(float(o['limit']), '.2f') if o['otype'] == 'LIMIT' else 'at market'} · "
                          f"{o['ts'][11:16]}</span></div>", unsafe_allow_html=True)
            c[1].button("✕", key=f"pd_c_{o['oid']}", use_container_width=True, on_click=cb_cancel, args=(o["oid"],))
    today = ist_now().strftime("%Y-%m-%d")
    done = [o for o in s["orders"] if o.get("status") != "OPEN" and o.get("ts", "").startswith(today)][::-1][:20]
    st.markdown("<div class='pd-sec'>TODAY'S ORDERS</div>", unsafe_allow_html=True)
    if not done:
        st.caption("none yet")
    for o in done:
        col = {"FILLED": "pd-g", "REJECTED": "pd-r", "CANCELLED": "pd-m"}.get(o["status"], "pd-m")
        px = f" @ {o['fill_px']:.2f}" if o.get("fill_px") is not None else ""
        st.markdown(f"<div class='pd-m'>{(o.get('fill_ts') or o['ts'])[11:19]} · <b class='pd-w'>{o.get('side_', 'BUY')}</b> "
                    f"{name(o['strike'], o['side'], o.get('expiry', ''))} ×{int(o['lots']) * LOT}{px} · "
                    f"<span class='{col}'>{o['status']}</span> {o.get('msg', '') if o['status'] != 'FILLED' else ''}</div>",
                    unsafe_allow_html=True)


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
        opts = [2, 3, 5, 10]
        s["refresh_s"] = c[1].selectbox("Refresh (s)", opts, index=opts.index(int(s["refresh_s"])) if int(s["refresh_s"]) in opts else 1,
                                        key="pd_rf")
        disk = load_state()
        keep = ("ids", "orders")
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


def desk_body() -> None:
    import streamlit as st
    st.markdown(CSS, unsafe_allow_html=True)
    for text, icon in st.session_state.pop("_pd_msgs", []):
        _toast(st, text, icon)
    s = load_state()
    now = ist_now()
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
    for m in process(s, now, chain):
        _toast(st, m, "🔔")
    s = load_state()

    hdr = st.container()
    _ticket(st, s, now, chain_for)
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
        f"{ds['trades']} trades · {ds['win_pct']:.0f}% win · cash ₹{acc.cash_balance:,.0f}</span></div>",
        unsafe_allow_html=True)

    view = _seg(st, "View", ["★ Picks", "⛓ Chain", "🧾 Orders"], "pd_view", "★ Picks")
    if exps and view != "🧾 Orders":
        st.selectbox("Expiry", exps[:4], key="pd_exp", label_visibility="collapsed",
                     format_func=lambda x: f"Expiry {_d(x):%a %d %b}" + (" · nearest" if x == exps[0] else ""))
    if view == "⛓ Chain":
        _ladder(st, s, chain)
    elif view == "🧾 Orders":
        _orders(st, s)
    else:
        _picks(st, s, chain)
    _settings_ui(st, s)


def render_sidebar() -> None:
    """Call once near the top of dashboard.py — draws the desk at the top of the sidebar on every tab."""
    import streamlit as st
    s = load_state()
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
