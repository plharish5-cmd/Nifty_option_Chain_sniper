"""
paper_desk.py — 📝 PAPER DESK: one-click NIFTY option paper trading, pinned in the dashboard's left sidebar on EVERY tab.

  • Shows ONLY the best CE and PE strikes, ranked by your Greek weights (Delta › Theta › Gamma › Vega, editable).
  • BUY CE / BUY PE in one click → fills at the live best ASK.  SELL (exit) in one click → fills at the live best BID.
    Real bid/ask fills + exact Dhan charges (from paper_trading.calculate_trade_charges) = the P&L you would really get.
  • Live P&L per position and for the day, refreshed every few seconds WITHOUT reloading the dashboard (st.fragment).
  • Optional stop-loss / target in points (auto-exit at the bid), and 15:20 intraday square-off.
  • Uses the same account / trades.csv as the 📒 Paper Trading tab, so its journal and analytics keep working.

Prices: Dhan /optionchain (cached 3 s, shared by every browser tab) → if that fails, /marketfeed/quote for open
positions → if that fails, the last stored snapshot, clearly marked STALE (BUY disabled when > 2 min old so no fake fills).
Settings in data/paper_trading/desk.json (edit from the ⚙ in the desk). Information only — not financial advice.
"""
from __future__ import annotations

import json
import math
import time as _time
from datetime import datetime, time, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

import config
import paper_trading as pt

DESK_FILE = pt.PAPER_DIR / "desk.json"
DEFAULTS = {
    "lots": 1,
    "sl_pts": 0.0,              # 0 = no stop
    "tgt_pts": 0.0,             # 0 = no target
    "weights": {"delta": 40, "theta": 30, "gamma": 20, "vega": 10},
    "delta_target": 0.50,       # |delta| the ranking prefers
    "top_n": 3,
    "window_strikes": 8,        # rank strikes within ATM ± this many
    "min_premium": 5.0,
    "refresh_s": 3,
    "stale_s": 120,
    "squareoff": "15:20",
}
STEP = float(getattr(config, "STRIKE_STEP", 50))
LOT = int(getattr(config, "LOT_SIZE", 65))
_mem = {"chain": None, "exps": (0.0, []), "ids": {}}


# ═══════════════════════════════════════════════════════════════════════════
# settings
# ═══════════════════════════════════════════════════════════════════════════
def settings() -> dict:
    try:
        d = json.loads(DESK_FILE.read_text())
    except Exception:                                           # noqa: BLE001
        d = {}
    out = {**DEFAULTS, **{k: v for k, v in d.items() if k in DEFAULTS}}
    out["weights"] = {**DEFAULTS["weights"], **(d.get("weights") or {})}
    out["ids"] = d.get("ids", {})                               # position_id → {"sid", "expiry"}
    return out


def save_settings(s: dict) -> None:
    try:
        DESK_FILE.write_text(json.dumps(s, indent=2, default=str))
    except Exception:                                           # noqa: BLE001
        pass


def ist_now() -> datetime:
    return datetime.utcnow() + timedelta(hours=5, minutes=30)


def market_open(now: datetime) -> bool:
    return now.weekday() < 5 and time(9, 15) <= now.time() <= time(15, 30)


# ═══════════════════════════════════════════════════════════════════════════
# prices
# ═══════════════════════════════════════════════════════════════════════════
def _expiry(now: datetime) -> str | None:
    import dhan_client as dc
    t, exps = _mem["exps"]
    if not exps or _time.time() - t > 600:
        exps = dc.fetch_dhan_expiries() or exps
        _mem["exps"] = (_time.time(), exps)
    for e in sorted(exps):
        d = pd.to_datetime(e, errors="coerce")
        if pd.isna(d):
            continue
        if d.date() > now.date() or (d.date() == now.date() and now.time() < time(15, 30)):
            return e
    return exps[0] if exps else None


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
                             sid=str(g.get("security_id", "")).split(".")[0], oi=_f(g.get("oi"))))
    return pd.DataFrame(rows), _f(data.get("last_price"))


def _snapshot_chain() -> dict | None:
    """Latest stored snapshot (no bid/ask, no ids) — used only when Dhan cannot be reached."""
    try:
        root = Path(config.SNAPSHOT_DIR)
        day = sorted(p for p in root.iterdir() if p.is_dir())[-1]
        meta = sorted(day.glob("*.meta.json"))[-1]
        info = json.loads(meta.read_text())
        df = pd.read_parquet(meta.with_suffix("").with_suffix(".parquet"))
        rows = []
        for side, p in (("CE", "ce"), ("PE", "pe")):
            rows.append(pd.DataFrame({"strike": pd.to_numeric(df["strike"]).round(), "side": side,
                                      "ltp": pd.to_numeric(df.get(f"{p}_ltp"), errors="coerce"),
                                      "bid": np.nan, "ask": np.nan,
                                      "delta": pd.to_numeric(df.get(f"{p}_delta"), errors="coerce"),
                                      "theta": pd.to_numeric(df.get(f"{p}_theta"), errors="coerce"),
                                      "gamma": pd.to_numeric(df.get(f"{p}_gamma"), errors="coerce"),
                                      "vega": pd.to_numeric(df.get(f"{p}_vega"), errors="coerce"),
                                      "iv": pd.to_numeric(df.get(f"{p}_iv"), errors="coerce"), "sid": "", "oi": np.nan}))
        at = datetime.fromisoformat(info["timestamp"]).replace(tzinfo=None)
        exp = str(df["expiry"].iloc[0]) if "expiry" in df else ""
        return dict(rows=pd.concat(rows, ignore_index=True), spot=float(info["spot"]), expiry=exp, at=at,
                    source=f"STALE snapshot {at:%H:%M}", live=False)
    except Exception:                                           # noqa: BLE001
        return None


def get_chain(now: datetime) -> dict | None:
    """Live chain from Dhan; keeps the last good one on a hiccup (e.g. rate limit); snapshot as last resort."""
    try:
        import dhan_client as dc
        exp = _expiry(now)
        data = dc.fetch_dhan_option_chain(exp) if exp else None
        if data:
            rows, spot = parse_chain(data)
            if len(rows):
                c = dict(rows=rows, spot=spot, expiry=exp, at=now, source="Dhan live", live=True)
                _mem["chain"] = c
                return c
    except Exception:                                           # noqa: BLE001
        pass
    if _mem["chain"] is not None:
        c = dict(_mem["chain"])
        c["source"] = f"Dhan {c['at']:%H:%M:%S} (retrying)"
        return c
    return _snapshot_chain()


def quotes(sids: list[str]) -> dict:
    """{sid: (ltp, bid, ask)} from /marketfeed/quote — for positions not in the current chain."""
    sids = [s for s in sids if s]
    if not sids:
        return {}
    try:
        import requests
        import dhan_client as dc
        r = requests.post(f"{dc.BASE}/marketfeed/quote", json={"NSE_FNO": [int(s) for s in sids]},
                          headers=dc._headers(), timeout=5)
        d = (r.json().get("data") or {}).get("NSE_FNO", {}) if r.status_code == 200 else {}
        out = {}
        for s in sids:
            q = d.get(str(s)) or {}
            dp = q.get("depth") or {}
            b = (dp.get("buy") or [{}])[0].get("price")
            a = (dp.get("sell") or [{}])[0].get("price")
            out[s] = (_f(q.get("last_price")), _f(b), _f(a))
        return out
    except Exception:                                           # noqa: BLE001
        return {}


# ═══════════════════════════════════════════════════════════════════════════
# strike ranking — your Greek weights, in the order Delta › Theta › Gamma › Vega
# ═══════════════════════════════════════════════════════════════════════════
def rank(rows: pd.DataFrame, spot: float, side: str, s: dict) -> pd.DataFrame:
    """
    Score 0–100 for an option BUYER (intraday):
      Delta — closeness of |Δ| to the target (0.50 = ATM: moves well with NIFTY without paying deep-ITM prices)
      Theta — time-decay cost as % of premium per day   (lower = better)
      Gamma — gamma per ₹ of premium                    (higher = delta grows faster when you are right)
      Vega  — vega as % of premium                      (lower = less hurt by an IV drop)
    Theta/Gamma/Vega are ranked against the other strikes on screen (0 = worst here, 1 = best here).
    """
    w = s["weights"]
    x = rows[(rows["side"] == side) & (rows["ltp"] >= float(s["min_premium"]))].copy()
    if x.empty or not np.isfinite(spot):
        return x
    atm = round(spot / STEP) * STEP
    x = x[(x["strike"] - atm).abs() <= float(s["window_strikes"]) * STEP]
    x = x.dropna(subset=["delta"])
    x = x[x["delta"].abs() > 0.03]
    if x.empty:
        return x
    tgt = float(s["delta_target"])
    x["s_delta"] = (1 - (x["delta"].abs() - tgt).abs() / 0.35).clip(0, 1)
    pr = lambda v, high_good: (v.rank(pct=True) if high_good else 1 - v.rank(pct=True) + 1 / len(v)).clip(0, 1)
    x["s_theta"] = pr((x["theta"].abs() / x["ltp"]).fillna(0), False) if x["theta"].notna().any() else 0.5
    x["s_gamma"] = pr((x["gamma"] / x["ltp"]).fillna(0), True) if x["gamma"].notna().any() else 0.5
    x["s_vega"] = pr((x["vega"] / x["ltp"]).fillna(0), False) if x["vega"].notna().any() else 0.5
    tot = sum(max(0.0, float(v)) for v in w.values()) or 1.0
    x["score"] = 100 * (float(w["delta"]) * x["s_delta"] + float(w["theta"]) * x["s_theta"]
                        + float(w["gamma"]) * x["s_gamma"] + float(w["vega"]) * x["s_vega"]) / tot
    x["spread_pct"] = 100 * (x["ask"] - x["bid"]) / x["ltp"]
    return x.sort_values("score", ascending=False).head(int(s["top_n"]))


# ═══════════════════════════════════════════════════════════════════════════
# orders (through the existing paper engine so the journal / analytics stay one)
# ═══════════════════════════════════════════════════════════════════════════
def buy(r, chain: dict, s: dict) -> tuple[bool, str]:
    price = r.ask if np.isfinite(r.ask) and r.ask > 0 else r.ltp
    src = "ask" if np.isfinite(r.ask) and r.ask > 0 else "LTP"
    lots = int(s["lots"])
    sl = round(price - float(s["sl_pts"]), 2) if float(s["sl_pts"]) > 0 else 0.0
    tg = round(price + float(s["tgt_pts"]), 2) if float(s["tgt_pts"]) > 0 else 0.0
    note = (f"desk · fill {src} {price:.2f} (LTP {r.ltp:.2f}) · score {r.score:.0f} · Δ {r.delta:+.2f} "
            f"θ {r.theta:.1f} · {chain['source']}")
    slip = float(pt.load_paper_config().get("slippage_pts", 0.0) or 0.0)    # engine adds this on BUY → net = the ask
    ok, msg, acc = pt.place_paper_order(strike=float(r.strike), opt_type=r.side, lots=lots, spot=float(chain["spot"]),
                                        side="BUY", product_type="INTRADAY", stop_loss=sl, target_1=tg,
                                        setup_tag="Paper Desk", custom_price=float(price) - slip, notes=note,
                                        override_risk_limit=True)
    if ok:
        pid = acc.open_positions[-1].position_id
        s["ids"][pid] = {"sid": r.sid, "expiry": chain.get("expiry", "")}
        save_settings(s)
    return ok, msg


def sell(pos, price: float, chain: dict, reason: str, s: dict) -> tuple[bool, str]:
    ok, msg, _ = pt.exit_paper_position(pos.position_id, spot=float(chain["spot"]), exit_reason=reason,
                                        custom_exit_price=float(price), notes=f"{pos.notes} · exit {reason} {price:.2f}")
    if ok:
        s["ids"].pop(pos.position_id, None)
        save_settings(s)
    return ok, msg


def exit_price(pos, chain: dict, q: dict, s: dict) -> tuple[float, float, str]:
    """(bid used for exit/MTM, ltp, source) for an open position."""
    sid = (s["ids"].get(pos.position_id) or {}).get("sid", "")
    if sid and sid in q and np.isfinite(q[sid][0]):
        l, b, _ = q[sid]
        return (b if np.isfinite(b) and b > 0 else l), l, "quote"
    rows = chain["rows"]
    m = rows[(rows["strike"] == round(pos.strike)) & (rows["side"] == pos.opt_type)]
    same_exp = (s["ids"].get(pos.position_id) or {}).get("expiry", chain.get("expiry")) == chain.get("expiry")
    if len(m) and same_exp:
        r = m.iloc[0]
        b = r.bid if np.isfinite(r.bid) and r.bid > 0 else r.ltp
        return float(b), float(r.ltp), "chain"
    return float(pos.current_price or pos.entry_price), float(pos.current_price or pos.entry_price), "last known"


def auto_exits(acc, prices: dict, chain: dict, s: dict, now: datetime) -> list[str]:
    """SL / target at the bid, and the intraday square-off. Runs on every desk refresh."""
    msgs = []
    sq = datetime.strptime(str(s["squareoff"]), "%H:%M").time()
    for p in list(acc.open_positions):
        if p.side.upper() != "BUY" or p.position_id not in prices:
            continue
        bid = prices[p.position_id][0]
        reason = None
        if p.stop_loss and bid <= p.stop_loss:
            reason = "Stop-loss"
        elif p.target_1 and bid >= p.target_1:
            reason = "Target"
        elif p.product_type.upper() == "INTRADAY" and chain.get("live") and now.time() >= sq:
            reason = f"Square-off {s['squareoff']}"
        if reason:
            ok, m = sell(p, bid, chain, reason, s)
            if ok:
                msgs.append(f"{reason}: {m}")
    return msgs


def day_stats(acc, open_mtm: float) -> dict:
    h = pt.load_trade_history()
    today = ist_now().strftime("%Y-%m-%d")
    if h.empty or "exit_time" not in h:
        t = h
    else:
        t = h[h["exit_time"].astype(str).str.startswith(today)]
    net = pd.to_numeric(t.get("net_pnl_rupees", pd.Series(dtype=float)), errors="coerce").fillna(0) if len(t) else pd.Series(dtype=float)
    return dict(realised=float(net.sum()), open=open_mtm, trades=len(t), wins=int((net > 0).sum()),
                win_pct=100 * float((net > 0).mean()) if len(net) else 0.0)


# ═══════════════════════════════════════════════════════════════════════════
# UI — sidebar fragment (reruns alone every few seconds; clicks rerun only the desk)
# ═══════════════════════════════════════════════════════════════════════════
CSS = """<style>
.pd-box{background:#0b1a33;border:1px solid #d4af37;border-radius:8px;padding:8px 10px;margin:2px 0 6px;color:#f1f5f9}
.pd-h{color:#d4af37;font-weight:800;font-size:1.02rem}
.pd-m{color:#94a3b8;font-size:0.74rem}
.pd-big{font-weight:800;font-size:1.05rem;color:#f1f5f9}
.pd-row{background:#0b1a33;border-left:3px solid #d4af37;border-radius:6px;padding:4px 8px;font-size:0.82rem;
        line-height:1.3;color:#f1f5f9}
.pd-row.ce{border-left-color:#22c55e}.pd-row.pe{border-left-color:#ef4444}
.pd-g{color:#22c55e}.pd-r{color:#ef4444}.pd-y{color:#eab308}
.pd-sec{font-size:0.78rem;font-weight:700;margin:8px 0 2px}
[class*="st-key-pd_b_CE"] button{background:#16a34a!important;color:#fff!important;border:0!important;font-weight:700}
[class*="st-key-pd_b_PE"] button{background:#dc2626!important;color:#fff!important;border:0!important;font-weight:700}
[class*="st-key-pd_x_"] button,[class*="st-key-pd_xall"] button{background:#d4af37!important;color:#0b1a33!important;
        border:0!important;font-weight:800}
</style>"""


def _ago(sec: float) -> str:
    return f"{sec:.0f}s ago" if sec < 120 else f"{sec / 60:.0f} min ago" if sec < 7200 else f"{sec / 3600:.0f} h ago"


def _money(v: float) -> str:
    c = "pd-g" if v >= 0 else "pd-r"
    return f"<span class='{c}'>{'+' if v >= 0 else '−'}₹{abs(v):,.0f}</span>"


def _settings_ui(st, s: dict) -> None:
    pop = st.popover("⚙ Desk settings", use_container_width=True) if hasattr(st, "popover") else st.expander("⚙ Desk settings")
    with pop:
        s["lots"] = st.number_input("Lots per order", 1, 50, int(s["lots"]), key="pd_lots")
        c = st.columns(2)
        s["sl_pts"] = c[0].number_input("Stop-loss pts (0 = off)", 0.0, 500.0, float(s["sl_pts"]), 1.0, key="pd_sl")
        s["tgt_pts"] = c[1].number_input("Target pts (0 = off)", 0.0, 1000.0, float(s["tgt_pts"]), 1.0, key="pd_tg")
        st.caption("Greek weights (order of importance: Delta › Theta › Gamma › Vega)")
        w = s["weights"]
        w["delta"] = st.slider("Delta", 0, 100, int(w["delta"]), key="pd_wd")
        w["theta"] = st.slider("Theta", 0, 100, int(w["theta"]), key="pd_wt")
        w["gamma"] = st.slider("Gamma", 0, 100, int(w["gamma"]), key="pd_wg")
        w["vega"] = st.slider("Vega", 0, 100, int(w["vega"]), key="pd_wv")
        s["delta_target"] = st.slider("Preferred |Delta|", 0.20, 0.80, float(s["delta_target"]), 0.05, key="pd_dt")
        c = st.columns(2)
        s["top_n"] = c[0].selectbox("Strikes per side", [1, 2, 3, 4, 5], index=[1, 2, 3, 4, 5].index(int(s["top_n"])), key="pd_n")
        s["refresh_s"] = c[1].selectbox("Refresh (s)", [2, 3, 5, 10], index=[2, 3, 5, 10].index(int(s["refresh_s"]))
                                        if int(s["refresh_s"]) in (2, 3, 5, 10) else 1, key="pd_rf")
        disk = settings()
        if {k: v for k, v in s.items() if k != "ids"} != {k: v for k, v in disk.items() if k != "ids"}:
            s["ids"] = disk["ids"]
            save_settings(s)                                    # changes apply at once (next click uses them)
            st.caption("✔ saved")
        acc = pt.load_paper_account()
        st.caption(f"Cash ₹{acc.cash_balance:,.0f} · equity ₹{acc.total_equity:,.0f}")
        if st.button("➕ Add ₹1,00,000 paper cash", use_container_width=True, key="pd_refill"):
            pt.refill_paper_capital(100000.0, reset_all=False)
            st.toast("₹1,00,000 added")


def _once(st, key: str) -> bool:
    """Ignore a second click on the same button within 1.5 s (double-click = one order)."""
    now = _time.time()
    last = st.session_state.get("_pd_last", {})
    if now - last.get(key, 0) < 1.5:
        return False
    last[key] = now
    st.session_state["_pd_last"] = last
    return True


_CC = {}


def cached_chain():
    """Chain shared by every browser tab, refreshed at most every 3 s (Dhan option-chain limit)."""
    import streamlit as st
    if "f" not in _CC:
        _CC["f"] = st.cache_data(ttl=3, show_spinner=False)(lambda bucket: get_chain(ist_now()))
    return _CC["f"](int(_time.time() // 3))


def _do_buy(side: str, strike: int) -> None:
    """Button callback — runs BEFORE the desk redraws, so the new position shows at once."""
    import streamlit as st
    if not _once(st, f"b{side}{strike}"):
        return
    s, chain = settings(), cached_chain()
    if not chain or not chain.get("live"):
        st.session_state["_pd_msg"] = ("No live price — order not placed", "⚠️"); return
    top = rank(chain["rows"], chain["spot"], side, {**s, "top_n": 99})
    m = top[top["strike"] == strike]
    if m.empty:
        st.session_state["_pd_msg"] = (f"{strike} {side} no longer tradable", "⚠️"); return
    ok, msg = buy(next(m.itertuples()), chain, s)
    st.session_state["_pd_msg"] = (msg, "🛒" if ok else "⚠️")


def _do_sell(pid: str | None) -> None:
    import streamlit as st
    if not _once(st, f"x{pid}"):
        return
    s, chain = settings(), cached_chain()
    if not chain or not chain.get("live"):
        st.session_state["_pd_msg"] = ("No live price — exit not placed", "⚠️"); return
    acc = pt.load_paper_account()
    sids = [(s["ids"].get(p.position_id) or {}).get("sid", "") for p in acc.open_positions
            if (s["ids"].get(p.position_id) or {}).get("expiry") != chain.get("expiry")]
    q = quotes(sids) if sids else {}
    done = []
    for p in acc.open_positions:
        if pid is None or p.position_id == pid:
            ok, msg = sell(p, exit_price(p, chain, q, s)[0], chain, "Manual exit" if pid else "Exit all", s)
            done.append(msg)
    st.session_state["_pd_msg"] = (" | ".join(done) or "nothing to exit", "✅")


def desk_body() -> None:
    import streamlit as st
    s = settings()
    now = ist_now()
    st.markdown(CSS, unsafe_allow_html=True)

    chain = cached_chain()
    msg = st.session_state.pop("_pd_msg", None)
    if msg:
        st.toast(msg[0], icon=msg[1])
    acc = pt.load_paper_account()
    if chain is None:
        st.markdown("<div class='pd-box'><span class='pd-h'>📝 PAPER DESK</span><br>"
                    "<span class='pd-m'>No prices yet — check the Dhan token below.</span></div>", unsafe_allow_html=True)
        return
    age = (now - chain["at"]).total_seconds()
    live_ok = chain.get("live") and age <= float(s["stale_s"]) and market_open(now)
    rows = chain["rows"]

    # prices for open positions
    q = {}
    need = [(s["ids"].get(p.position_id) or {}).get("sid", "") for p in acc.open_positions]
    off_chain = [sid for p, sid in zip(acc.open_positions, need)
                 if sid and (s["ids"].get(p.position_id) or {}).get("expiry") != chain.get("expiry")]
    if off_chain:
        q = quotes(off_chain)
    prices = {p.position_id: exit_price(p, chain, q, s) for p in acc.open_positions}
    if live_ok:
        for m in auto_exits(acc, prices, chain, s, now):
            st.toast(m, icon="🔔")
        acc = pt.load_paper_account()
        prices = {p.position_id: exit_price(p, chain, q, s) for p in acc.open_positions}

    mtm = 0.0
    for p in acc.open_positions:
        b = prices[p.position_id][0]
        mtm += (b - p.entry_price) * p.quantity if p.side.upper() == "BUY" else (p.entry_price - b) * p.quantity
    ds = day_stats(acc, mtm)
    exp = pd.to_datetime(chain.get("expiry"), errors="coerce")
    dte = f"{(exp.date() - now.date()).days}d" if pd.notna(exp) else ""
    status = ("<span class='pd-g'>● LIVE</span>" if live_ok else
              "<span class='pd-y'>● MARKET CLOSED</span>" if not market_open(now) else "<span class='pd-r'>● STALE</span>")
    st.markdown(
        f"<div class='pd-box'><span class='pd-h'>📝 PAPER DESK</span> &nbsp;{status}<br>"
        f"<span class='pd-m'>NIFTY {chain['spot']:,.1f} · exp {exp:%d %b} ({dte}) · {chain['source']} · {_ago(age)}</span><br>"
        f"<span class='pd-big'>Day {_money(ds['realised'] + ds['open'])}</span>"
        f"<span class='pd-m'> &nbsp;booked {_money(ds['realised'])} · open {_money(ds['open'])} · "
        f"{ds['trades']} trades · {ds['win_pct']:.0f}% win</span></div>", unsafe_allow_html=True)

    # ── open positions (exit first — fastest to reach) ──
    if acc.open_positions:
        for p in acc.open_positions:
            b, l, src = prices[p.position_id]
            pts = b - p.entry_price
            pnl = pts * p.quantity
            c = st.columns([3.2, 1.3])
            sl = f" · SL {p.stop_loss:.1f}" if p.stop_loss else ""
            tg = f" · T {p.target_1:.1f}" if p.target_1 else ""
            c[0].markdown(f"<div class='pd-row {p.opt_type.lower()}'><b>{int(p.strike)} {p.opt_type}</b> ×{p.quantity} @ {p.entry_price:.2f}<br>"
                          f"bid {b:.2f} · {('+' if pts >= 0 else '')}{pts:.2f} pts · {_money(pnl)}"
                          f"<span class='pd-m'>{sl}{tg}</span></div>", unsafe_allow_html=True)
            c[1].button("SELL", key=f"pd_x_{p.position_id}", use_container_width=True,
                        disabled=not live_ok, help=f"Exit at the bid (~{b:.2f})",
                        on_click=_do_sell, args=(p.position_id,))
        if len(acc.open_positions) > 1:
            st.button("⏹ EXIT ALL at bid", use_container_width=True, key="pd_xall", disabled=not live_ok,
                      on_click=_do_sell, args=(None,))

    # ── best strikes ──
    lots = int(s["lots"])
    for side, col in (("CE", "pd-g"), ("PE", "pd-r")):
        top = rank(rows, chain["spot"], side, s)
        st.markdown(f"<div class='pd-sec'><b class='{col}'>BUY {side}</b><span class='pd-m'> · top {len(top)} by "
                    f"Δ{s['weights']['delta']} θ{s['weights']['theta']} γ{s['weights']['gamma']} ν{s['weights']['vega']} · "
                    f"{lots} lot = {lots * LOT} qty</span></div>", unsafe_allow_html=True)
        if top.empty:
            st.caption("no tradable strikes")
            continue
        for r in top.itertuples():
            px = r.ask if np.isfinite(r.ask) and r.ask > 0 else r.ltp
            spr = f" · spr {r.spread_pct:.1f}%" if np.isfinite(r.spread_pct) else ""
            warn = " ⚠" if np.isfinite(r.spread_pct) and r.spread_pct > 2 else ""
            c = st.columns([3.2, 1.3])
            c[0].markdown(f"<div class='pd-row {side.lower()}'><b>{int(r.strike)} {side}</b> ask {px:.2f}{warn}<br>"
                          f"<span class='pd-m'>Δ {r.delta:+.2f} · θ {r.theta:.1f} · score {r.score:.0f}{spr} · "
                          f"₹{px * lots * LOT:,.0f}</span></div>", unsafe_allow_html=True)
            c[1].button("BUY", key=f"pd_b_{side}_{int(r.strike)}", use_container_width=True, disabled=not live_ok,
                        help=f"Buy {lots} lot at the ask (~{px:.2f})", on_click=_do_buy, args=(side, int(r.strike)))
    if not live_ok:
        st.caption("Orders are off: " + ("market closed." if not market_open(now) else
                                          f"prices are {_ago(age)} — no fake fills."))
    _settings_ui(st, s)


def render_sidebar() -> None:
    """Call once near the top of dashboard.py — draws the desk at the top of the sidebar on every tab."""
    import streamlit as st
    s = settings()
    frag = getattr(st, "fragment", None) or getattr(st, "experimental_fragment", None)
    with st.sidebar:
        if frag is None:
            desk_body()
            st.caption("Upgrade Streamlit (≥1.37) for live auto-refresh of the desk.")
        else:
            n = int(s["refresh_s"])
            if n not in _mem["ids"]:
                _mem["ids"][n] = frag(run_every=n)(desk_body)
            _mem["ids"][n]()
        st.markdown("---")
