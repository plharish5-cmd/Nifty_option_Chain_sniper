"""
replay_desk.py — ⏪ REPLAY TRADING: paper trade a PAST session on the SAME Paper Desk, at 1× … 30× speed.

How it works
  • Prices come from your stored option-chain snapshots (data/snapshots/<day>/), one per minute, revealed only up to
    the replay clock — no peeking at the future.
  • The desk is the real paper_desk.py (picks, chain, tickets, SL / target / trail, square-off 15:20, charges exactly as
    Dhan, funds, statement, mentor report). Only the clock and the price source are swapped.
  • Its own account: profile "replay" (data/paper_trading/profiles/replay/) — never mixes with your live paper P&L.
    Trades are stamped with the REPLAY date/time, so every replayed day shows up as its own day in Funds / Charges /
    statement and in   .venv/bin/python trade_report.py --profile replay
  • Bid / ask: the snapshots carry last price only, so the desk uses LTP ± a small spread (0.2 % of premium, min ₹0.05).
    SL / target are checked once per snapshot minute (intraminute spikes are not seen) — a little kinder than live.
"""
from __future__ import annotations

import json
import time as _time
from datetime import date, datetime, time, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

SPEEDS = [1, 2, 5, 10, 20, 30]
OPEN, CLOSE = time(9, 15), time(15, 30)
_R: dict = {"day": None, "snaps": [], "times": [], "chains": {}, "clk": None, "clk_t": 0.0}
pdk = None                                                    # paper_desk module, set by install()


# ═══════════════════════════════════════════════════════════════════════════
# clock (shared by every rerun / fragment through a small JSON file)
# ═══════════════════════════════════════════════════════════════════════════
def _clock_file() -> Path:
    return pdk.pt.PAPER_DIR / "replay_clock.json"


def clock() -> dict:
    if _R["clk"] is not None and _time.time() - _R["clk_t"] < 0.25:
        return _R["clk"]
    try:
        c = json.loads(_clock_file().read_text())
    except Exception:                                         # noqa: BLE001
        c = {}
    _R.update(clk=c, clk_t=_time.time())
    return c


def save_clock(c: dict) -> None:
    _clock_file().write_text(json.dumps(c))
    _R.update(clk=c, clk_t=_time.time())


def sim_now() -> datetime:
    c = clock()
    if not c.get("day"):
        return datetime.combine(date.today(), time(9, 15))
    t = datetime.fromisoformat(c["t_sim"])
    if not c.get("paused"):
        t += timedelta(seconds=(_time.time() - float(c["t_real"])) * float(c.get("speed", 5)))
    end = datetime.combine(date.fromisoformat(c["day"]), CLOSE)
    if t >= end and not c.get("paused"):
        c.update(t_sim=end.isoformat(), t_real=_time.time(), paused=True)
        save_clock(c)
        return end
    return min(t, end)


def set_clock(day: date, t: datetime, speed: float, paused: bool) -> None:
    save_clock(dict(day=day.isoformat(), t_sim=t.isoformat(), t_real=_time.time(), speed=speed, paused=paused))


# ═══════════════════════════════════════════════════════════════════════════
# prices from snapshots
# ═══════════════════════════════════════════════════════════════════════════
def _load_day(day: date) -> None:
    if _R["day"] == day:
        return
    import store
    snaps = store.load_day(day)
    _R.update(day=day, snaps=snaps, times=[pd.Timestamp(s[0]).to_pydatetime().replace(tzinfo=None) for s in snaps],
              chains={})


def _idx(now: datetime) -> int | None:
    import bisect
    if not _R["times"]:
        return None
    i = bisect.bisect_right(_R["times"], now) - 1
    return i if i >= 0 else None


def _expiry(df: pd.DataFrame) -> str:
    import options_lab_tab as ol
    return str(ol._one_expiry(df)["expiry"].iloc[0]) if "expiry" in df and len(df) else ""


def chain_at(now: datetime) -> dict | None:
    c = clock()
    if not c.get("day"):
        return None
    _load_day(date.fromisoformat(c["day"]))
    i = _idx(now)
    if i is None:
        i = 0 if _R["snaps"] else None
    if i is None:
        return None
    if i not in _R["chains"]:
        ts, spot, df = _R["snaps"][i]
        exp = _expiry(df)
        d = df[df["expiry"].astype(str) == exp] if "expiry" in df and exp else df
        d = d.drop_duplicates("strike")
        rows = []
        for side, p in (("CE", "ce"), ("PE", "pe")):
            g = lambda col: pd.to_numeric(d[col], errors="coerce") if col in d else pd.Series(np.nan, index=d.index)
            ltp = g(f"{p}_ltp")
            spr = np.maximum(0.05, 0.002 * ltp)
            k = pd.to_numeric(d["strike"], errors="coerce").round()
            bid, ask = g(f"{p}_bid"), g(f"{p}_ask")
            rows.append(pd.DataFrame({"strike": k, "side": side, "ltp": ltp,
                                      "bid": bid.where(bid > 0, ((ltp - spr / 2) * 20).round().clip(lower=1) / 20),
                                      "ask": ask.where(ask > 0, ((ltp + spr / 2) * 20).round() / 20),
                                      "delta": g(f"{p}_delta"), "theta": g(f"{p}_theta"), "gamma": g(f"{p}_gamma"),
                                      "vega": g(f"{p}_vega"), "iv": g(f"{p}_iv"),
                                      "sid": [f"R{int(x)}{side}" if np.isfinite(x) else "" for x in k],
                                      "oi": g(f"{p}_oi"), "prev_oi": np.nan}))
        r = pd.concat(rows, ignore_index=True)
        r = r[r.ltp > 0]
        _R["chains"][i] = dict(rows=r, spot=float(spot), expiry=exp, snap_ts=pd.Timestamp(ts).to_pydatetime().replace(tzinfo=None))
    base = _R["chains"][i]
    return dict(base, at=now, source=f"⏪ REPLAY {base['snap_ts']:%d %b %H:%M}", live=True, t=_time.time())


def _quotes(sids: list[str]) -> dict:
    ch = chain_at(sim_now())
    if ch is None:
        return {}
    r = ch["rows"].set_index("sid")
    out = {}
    for s in sids:
        if s in r.index:
            x = r.loc[s]
            out[s] = dict(ltp=float(x.ltp), bid=float(x.bid), ask=float(x.ask), depth={"buy": [], "sell": []})
    return out


def install(paper_desk_module) -> None:
    """Swap the desk's clock and price source for the replay ones."""
    global pdk
    pdk = paper_desk_module
    pdk.ist_now = sim_now
    pdk.live_expiries = lambda now: ([chain_at(now)["expiry"]] if chain_at(now) else [])
    pdk.get_chain = lambda expiry, now: chain_at(now)
    pdk.cached_chain = lambda expiry=None: chain_at(sim_now())
    pdk._cached_chain_direct = lambda expiry=None: chain_at(sim_now())
    pdk.quotes = _quotes
    pdk._quotes_direct = lambda sids, cache=True: _quotes(sids)
    pdk._quotes_fetch = lambda sids, cache=False: _quotes(sids)
    pdk.ensure_feed = lambda: None
    pdk._feed["started"] = True

    class _SimDT(datetime):                                     # paper_trading.py stamps trades with datetime.now()
        @classmethod
        def now(cls, tz=None):                                  # noqa: ARG003
            return sim_now()

    pdk.pt.datetime = _SimDT
    pdk.datetime = _SimDT


# ═══════════════════════════════════════════════════════════════════════════
# UI: controls + chart
# ═══════════════════════════════════════════════════════════════════════════
def days_available() -> list[date]:
    import options_lab_tab as ol
    return [d for d in ol._days_available() if d < date.today() or datetime.now().time() > CLOSE]


def replayed_days() -> set:
    try:
        t = pd.read_csv(pdk.pt.TRADES_CSV)
        return set(pd.to_datetime(t["entry_time"]).dt.date)
    except Exception:                                         # noqa: BLE001
        return set()


def controls(st) -> None:
    c = clock()
    days = days_available()
    if not days:
        st.warning("No stored sessions to replay yet (data/snapshots).")
        return
    done = replayed_days()
    cur = date.fromisoformat(c["day"]) if c.get("day") else None
    st.markdown("<div class='pd-box'><span class='pd-h'>⏪ REPLAY TRADING</span> <span class='pd-m'>· a past session on "
                "your desk · separate 'replay' account · no peeking ahead</span></div>", unsafe_allow_html=True)
    a, b = st.columns([1.6, 1])
    day = a.selectbox("Session", days, index=days.index(cur) if cur in days else 0, key="rp_day",
                      format_func=lambda d: d.strftime("%a %d %b %Y") + ("  ✓ replayed" if d in done else ""))
    speed = b.selectbox("Speed", SPEEDS, index=SPEEDS.index(int(c.get("speed", 5))) if int(c.get("speed", 5)) in SPEEDS else 2,
                        key="rp_speed", format_func=lambda v: f"{v}×")
    now = sim_now()
    paused = bool(c.get("paused", True))
    k = st.columns(5)
    if k[0].button("▶ Start" if cur != day else ("▶ Play" if paused else "⏸ Pause"), key="rp_play", use_container_width=True):
        if cur != day:
            set_clock(day, datetime.combine(day, OPEN), speed, False)
        else:
            set_clock(day, now, speed, not paused)
        st.rerun()
    for col, mins, lab in ((k[1], 1, "+1m"), (k[2], 5, "+5m"), (k[3], 15, "+15m")):
        if col.button(lab, key=f"rp_j{mins}", use_container_width=True, disabled=cur != day):
            set_clock(day, min(now + timedelta(minutes=mins), datetime.combine(day, CLOSE)), speed, paused)
            st.rerun()
    if k[4].button("⟲ 09:15", key="rp_restart", use_container_width=True, disabled=cur != day,
                   help="Back to the open of this session (trades already taken stay — they are history)"):
        set_clock(day, datetime.combine(day, OPEN), speed, True)
        st.rerun()
    if cur == day and int(c.get("speed", 5)) != int(speed):
        set_clock(day, now, speed, paused)
    if day in done and cur != day:
        st.caption("⚠ You already replayed this session — new trades add to that day's results. Pick a ✓-free day for a "
                   "fair test.")


def chart(st) -> None:
    import plotly.graph_objects as go
    c = clock()
    if not c.get("day"):
        return
    _load_day(date.fromisoformat(c["day"]))
    now = sim_now()
    i = _idx(now)
    if i is None:
        st.caption("Replay starts at 09:15 — press ▶.")
        return
    sp = pd.Series([float(s[1]) for s in _R["snaps"][: i + 1]], index=pd.to_datetime(_R["times"][: i + 1]))
    tf = st.session_state.get("rp_tf", "5m")
    m = int(tf[:-1])
    o = sp.resample(f"{m}min", origin="start_day", offset="15min").ohlc().dropna()
    if o.empty:
        return
    e8 = o.close.ewm(span=8, adjust=False).mean()
    e4 = o.close.ewm(span=4, adjust=False).mean()
    fig = go.Figure(go.Candlestick(x=o.index, open=o.open, high=o.high, low=o.low, close=o.close, name="NIFTY",
                                   increasing_line_color="#22c55e", decreasing_line_color="#ef4444"))
    fig.add_trace(go.Scatter(x=o.index, y=e8, name="8 EMA", line=dict(color="#ef4444", width=1.4)))
    fig.add_trace(go.Scatter(x=o.index, y=e4, name="4 EMA", line=dict(color="#22c55e", width=1.2)))
    fig.add_hline(y=float(sp.iloc[0]), line=dict(color="#d4af37", dash="dot", width=1))
    fig.update_layout(height=300, margin=dict(l=4, r=4, t=26, b=4), paper_bgcolor="#0b1a33", plot_bgcolor="#10223f",
                      font=dict(color="#f1f5f9", size=11), xaxis_rangeslider_visible=False, showlegend=False,
                      title=dict(text=f"<b>NIFTY {tf} · {now:%d %b %H:%M}</b> · 8 EMA red · 4 EMA green · open gold",
                                 font=dict(color="#d4af37", size=12), x=0.01))
    fig.update_xaxes(gridcolor="#1e3358", range=[datetime.combine(now.date(), OPEN), datetime.combine(now.date(), CLOSE)])
    fig.update_yaxes(gridcolor="#1e3358", tickformat=",.0f")
    st.plotly_chart(fig, use_container_width=True, key="rp_chart", config={"displayModeBar": False})


def status(st) -> None:
    c = clock()
    if not c.get("day"):
        return
    now = sim_now()
    state = "⏸ paused" if c.get("paused") else "▶ " + str(c.get("speed", 5)) + "×"
    over = " · session over — see Funds / Charges / Orders" if now.time() >= CLOSE else ""
    st.markdown(f"<div class='pd-row'><b class='pd-y'>⏪ {now:%a %d %b · %H:%M:%S}</b> <span class='pd-m'>· "
                f"{state}{over}</span></div>", unsafe_allow_html=True)
