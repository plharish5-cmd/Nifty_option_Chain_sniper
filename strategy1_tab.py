"""
strategy1_tab.py — the Price Action Strategy tab.

Kept out of strategy1.py so the engine stays importable without Streamlit and
stays testable from selftest.py. Wire it up with three lines in dashboard.py:

    import strategy1_tab                                  # with the imports

    t1, ..., t7, t8 = st.tabs([..., "Price Action Strategy"])

    with t8:
        strategy1_tab.render(pa_frames, prev_close=prev_close)

`pa_frames` is the dict the dashboard already builds. `prev_close` is
yesterday's close — without it the gap gate cannot run, and the tab says so
rather than guessing.
"""
from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import strategy1 as s1

GREEN, RED, SLATE, GOLD = "#2FBF71", "#E8503A", "#8FA6B8", "#C9A227"
BLUE, AMBER = "#4A9BD1", "#E0A030"

URGENCY = {"action": ("🔔", GOLD), "warn": ("⚠️", RED), "info": ("·", SLATE)}


def _chart(d: pd.DataFrame, pats: list[list[str]], res: s1.Result) -> go.Figure:
    fig = go.Figure()
    fig.add_trace(go.Candlestick(
        x=s1._times(d), open=d["open"], high=d["high"],
        low=d["low"], close=d["close"], name="NIFTY 15m",
        increasing_line_color=GREEN, decreasing_line_color=RED))

    for col, colour, width, name in (
            ("ubb", RED, 2, "UBB"), ("lbb", GREEN, 2, "LBB"),
            ("ema4", GREEN, 1.5, "4 EMA"), ("ema8", RED, 1.5, "8 EMA")):
        if col in d.columns:
            fig.add_trace(go.Scatter(
                x=s1._times(d), y=d[col], mode="lines", name=name,
                line=dict(color=colour, width=width)))

    # pattern markers, above the candle for bearish and below for bullish
    for i, tags in enumerate(pats):
        if not tags:
            continue
        show = [t for t in tags if t != "BIG CANDLE"]
        if not show:
            continue
        tone = s1.PATTERN_MEANING.get(show[0], ("neutral", ""))[0]
        above = tone == "bearish"
        fig.add_trace(go.Scatter(
            x=[s1._times(d).iloc[i]],
            y=[float(d["high"].iloc[i]) + 12 if above
               else float(d["low"].iloc[i]) - 12],
            mode="markers+text",
            marker=dict(symbol="triangle-down" if above else "triangle-up",
                        size=11,
                        color=RED if tone == "bearish" else
                              (GREEN if tone == "bullish" else SLATE)),
            text=[show[0]], textposition="top center" if above else "bottom center",
            textfont=dict(size=9, color=SLATE),
            showlegend=False, hovertext=" · ".join(show)))

    for s in res.signals:
        if s.entry_ts is not None:
            fig.add_annotation(
                x=s.entry_ts, y=s.trigger, ax=0, ay=40 if s.direction == "LONG" else -40,
                text=("BUY" if s.direction == "LONG" else "SELL"),
                showarrow=True, arrowhead=2, arrowsize=1.2,
                arrowcolor=GREEN if s.direction == "LONG" else RED,
                font=dict(color=GREEN if s.direction == "LONG" else RED, size=11))

    fig.update_layout(
        height=520, margin=dict(l=8, r=8, t=8, b=8),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#F2F5F8"), xaxis_rangeslider_visible=False,
        legend=dict(orientation="h", y=1.02, yanchor="bottom"))
    # Collapse the hours the market is shut.
    #
    # A datetime axis draws real time, so 15:30 to 09:15 becomes a long flat
    # stretch and several sessions squeeze into narrow clumps with straight
    # lines between them. That is not a rendering nicety — the candles get
    # so compressed they cannot be read, which is exactly what happened when
    # the frame grew from one session to five.
    fig.update_xaxes(
        gridcolor="#12314d",
        rangebreaks=[
            dict(bounds=["sat", "mon"]),               # weekends
            dict(bounds=[15.6, 9.25], pattern="hour"),  # 15:35 -> 09:15
        ])
    fig.update_yaxes(gridcolor="#12314d")
    return fig


def render(pa_frames: dict | None, prev_close: float | None = None,
           params: dict | None = None) -> None:
    st.subheader("Price Action Strategy 1 — 4/8 EMA on the 15-minute")

    if not pa_frames or "15m" not in pa_frames:
        st.info("No 15-minute candles. This tab needs the price action feed — "
                "check the Price action tab for the source and its error.")
        return

    p = params or s1.load_params()
    df15 = pa_frames["15m"]

    # How many sessions to show. The engine wants history — Bollinger(20) on
    # a 15-minute chart needs twenty candles, and one session is only
    # twenty-five, so a today-only frame would leave the band slope UNKNOWN
    # until roughly 14:45 and the UBB filter is the master switch of this
    # entire strategy. The CHART, though, is for reading candles, and five
    # sessions of 15-minute candles squeezed across one axis cannot be read.
    #
    # So the engine keeps the full frame and only the chart is trimmed.
    _ts = s1._times(df15)
    _days = sorted({t.date() for t in pd.to_datetime(_ts)})
    _show = st.radio("Chart", ["Today", "2 sessions", "All"],
                     horizontal=True, index=0, key="s1_span")
    _keep = (1 if _show == "Today" else 2 if _show == "2 sessions"
             else len(_days))
    _cut = _days[-_keep] if len(_days) >= _keep else _days[0]
    _chart_df = df15[pd.to_datetime(_ts).dt.date >= _cut]
    if len(_days) > _keep:
        st.caption(
            f"Showing {_keep} of {len(_days)} sessions. The engine still uses "
            f"all {len(_days)} — Bollinger(20) needs twenty 15-minute candles "
            f"and a session is only twenty-five, so trimming its history "
            f"would blind the UBB slope filter until mid-afternoon.")

    if prev_close is None:
        st.warning("Yesterday's close was not supplied, so the gap rule is OFF. "
                   "Without it the app cannot tell you that a gap-up session "
                   "means no CE trades. Pass prev_close into render().")

    res = s1.evaluate(df15, prev_close=prev_close, p=p)
    if res.frame.empty:
        st.info(res.notes[0] if res.notes else "No closed candles yet.")
        return

    d, pats = res.frame, res.patterns
    summ = s1.summary(res)

    # ── the banner ──────────────────────────────────────────────────────────
    live = res.open_signal
    armed = res.armed_signal
    if live:
        colour, head = (GREEN if live.direction == "LONG" else RED), \
            f"IN {'CE' if live.direction == 'LONG' else 'PE'} TRADE"
        sub = (f"Entered {live.trigger:,.0f} at {live.entry_ts:%H:%M} · "
               f"stop now {live.stop:,.0f} · trailing every candle")
    elif armed:
        colour, head = GOLD, f"{'CE' if armed.direction == 'LONG' else 'PE'} ARMED"
        sub = (f"Break {armed.trigger:,.0f} to enter · stop {armed.stop:,.0f} · "
               f"risk {abs(armed.trigger - armed.stop):.0f} pts. Not in yet.")
    else:
        colour, head = SLATE, "NOTHING ARMED"
        sub = "No candle has closed beyond the 8 EMA into a tradable setup."

    st.markdown(
        f"<div style='background:#0B2035;border-left:5px solid {colour};"
        f"padding:14px 18px;border-radius:0 10px 10px 0;margin-bottom:12px'>"
        f"<div style='font-size:26px;font-weight:700;color:{colour}'>{head}</div>"
        f"<div style='color:{SLATE};font-size:14px'>{sub}</div></div>",
        unsafe_allow_html=True)

    c = st.columns(5)
    c[0].metric("Stage", res.stage.split(" — ")[0],
                res.stage.split(" — ")[1] if " — " in res.stage else "",
                delta_color="off")
    c[1].metric("UBB / LBB", f"{res.ubb_slope} / {res.lbb_slope}")
    c[2].metric("EMA order", "4 > 8" if "ABOVE" in res.ema_order else "4 < 8",
                "bullish" if "ABOVE" in res.ema_order else "bearish",
                delta_color="normal" if "ABOVE" in res.ema_order else "inverse")
    c[3].metric("First candle",
                f"{res.gates.first_candle_colour} {res.gates.first_candle_pts:.0f}p",
                "LARGE — no trade" if res.gates.first_candle_big else "normal",
                delta_color="off")
    c[4].metric("Gap", res.gates.gap_kind,
                f"{res.gates.gap:+.0f} pts" if res.gates.gap else "",
                delta_color="off")

    if res.gates.block_ce:
        st.error("**GAP UP — no CE trades today.** Look for PE. "
                 "Yesterday's close is resistance overhead.")
    if res.gates.block_pe:
        st.error("**GAP DOWN — no PE trades today.** It can jump back up "
                 "at any time.")
    for n in res.notes:
        st.warning(n)

    # ── alerts ──────────────────────────────────────────────────────────────
    st.markdown("#### What just happened")
    live_alerts = [a for a in res.alerts if a.kind != "GATE"]
    if not live_alerts:
        st.caption("Nothing on the last closed candle.")
    for a in live_alerts:
        icon, colour = URGENCY.get(a.urgency, ("·", SLATE))
        st.markdown(
            f"<div style='border-left:3px solid {colour};padding:6px 12px;"
            f"margin-bottom:6px'><b style='color:{colour}'>{icon} {a.text}</b>"
            f"<br><span style='color:{SLATE};font-size:13px'>{a.detail}</span></div>",
            unsafe_allow_html=True)

    # Trend strength on the open position — hold through, or take the exit?
    if getattr(res, "open_signal", None) is not None:
        try:
            _st = s1.trend_strength(d, len(d) - 1,
                                    res.open_signal.direction, p)
            _col = "#2E7D32" if _st.hold else "#E65100"
            st.markdown(
                f"<div style='padding:8px 12px;border-left:3px solid {_col};"
                f"background:#fafbfc'><b style='color:{_col}'>{_st.verdict}"
                f"</b> — {_st.note}</div>", unsafe_allow_html=True)
            for _n, (_ok, _dt) in _st.checks.items():
                _m = "✓" if _ok else ("·" if _ok is None else "✗")
                st.caption(f"{_m} **{_n}** — {_dt}")
        except Exception as _e:                             # noqa: BLE001
            st.caption(f"strength check unavailable: {_e}")

    with st.expander("Session gates and skipped setups"):
        for a in res.alerts:
            if a.kind == "GATE":
                st.write(f"**{a.text}**" + (f"  \n{a.detail}" if a.detail else ""))

    # Chart the trimmed span; the engine above already used the full frame.
    _mask = pd.to_datetime(s1._times(d)).dt.date >= _cut
    _dplot = d[_mask] if _mask.any() else d
    _pplot = [t for t, keep in zip(pats, _mask) if keep] if len(pats) == len(d) \
        else pats
    st.plotly_chart(_chart(_dplot, _pplot, res), use_container_width=True)

    # ── the trade list ──────────────────────────────────────────────────────
    st.markdown("#### Setups today")
    if not res.signals:
        st.caption("None armed yet.")
    else:
        rows = []
        for s in res.signals:
            # Column names say what they hold.
            #
            # This table had "time", "entry" and "exit" where the first two
            # were timestamps and the third was a REASON. Reading across a
            # row you got 09:15, 09:30, "trailing stop" — three columns, two
            # meanings, and no exit time anywhere. The exit was the one
            # moment you could not read off the table, which is also the
            # only moment you can check against a chart after the close.
            _held = ""
            if s.entry_ts is not None and s.exit_ts is not None:
                try:
                    _held = f"{(s.exit_ts - s.entry_ts).total_seconds() / 60:.0f}m"
                except Exception:                           # noqa: BLE001
                    _held = ""
            rows.append({
                "armed": f"{s.armed_ts:%H:%M}",
                "dir": "CE" if s.direction == "LONG" else "PE",
                "state": s.state + (" ⚠ ambiguous" if s.ambiguous else ""),
                "trigger": f"{s.trigger:,.0f}",
                "stop (init)": f"{s.stop0:,.0f}",
                "stop (now)": f"{s.stop:,.0f}",
                "risk": f"{abs(s.trigger - s.stop0):.0f}p",
                "entry time": (f"{s.entry_ts:%H:%M}"
                               if s.entry_ts is not None else "—"),
                "exit time": (f"{s.exit_ts:%H:%M}"
                              if s.exit_ts is not None else "—"),
                "held": _held or "—",
                "exit price": (f"{s.exit_price:,.0f}"
                               if getattr(s, "exit_price", None) is not None
                               else "—"),
                "exit reason": s.exit_reason or "—",
                "pts": f"{s.points:+.0f}" if s.points is not None else "—",
                "note": " ".join(s.notes) if s.notes else "",
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

        m = st.columns(5)
        m[0].metric("Armed", summ["armed"])
        m[1].metric("Taken", summ["taken"])
        m[2].metric("Won / lost", f"{summ['wins']} / {summ['losses']}")
        m[3].metric("Points", f"{summ['points']:+.0f}")
        m[4].metric("Ambiguous", summ["ambiguous"],
                    help="Trigger and stop broke in the same candle. Order "
                         "unknowable at 15m, so excluded from the win rate.")

    # ── patterns ────────────────────────────────────────────────────────────
    # ── session structure ───────────────────────────────────────────────
    import session_levels as _sl
    try:
        _now = s1._times(d).iloc[-1].to_pydatetime()
        _hi, _lo = _sl.extremes(d, _now)
        _px = float(d["close"].iloc[-1])
        _z = _sl.wick_zones(d)

        st.markdown("#### Session structure")
        _c = st.columns(3)
        if _hi: _c[0].metric("Day high", f"{_hi.price:,.1f}",
                             f"{_hi.at:%H:%M}", delta_color="off")
        if _lo: _c[1].metric("Day low", f"{_lo.price:,.1f}",
                             f"{_lo.at:%H:%M}", delta_color="off")
        if _z.width: _c[2].metric("Opening range", f"{_z.width:.0f} pts",
                                  f"tested {_z.tested}x", delta_color="off")

        _pend = _sl.pending_end(_hi, _lo, _now)
        if _pend:
            st.caption(_pend)
        st.caption("Position: " + _sl.zone_read(_px, _z))

        _near, _why = _sl.near_extreme(_px, _hi, _lo)
        if _near:
            st.warning(f"**Near a session extreme** — {_why}")

        _brk = [b for b in _sl.breaks(d) if b.state != "FAILED"][-3:]
        _fail = [b for b in _sl.breaks(d) if b.state == "FAILED"][-2:]
        if _brk or _fail:
            st.markdown("**Close + follow-through on the day extremes**")
            for _b in _brk:
                (st.success if _b.state == "REAL" else st.info)(
                    f"**{_b.close_at:%H:%M} · {_b.side} @ {_b.level:,.0f} · "
                    f"{_b.state}** ({_b.direction}) — {_b.note}")
            for _b in _fail:
                st.error(f"**{_b.close_at:%H:%M} · {_b.side} @ "
                         f"{_b.level:,.0f} · FAILED** — {_b.note}")
        else:
            st.caption("No candle has closed beyond the day high or low yet.")
    except Exception as _sle:                               # noqa: BLE001
        st.caption(f"session structure unavailable: {_sle}")

    # ── gap-down day: previous daily rejection wicks as support ─────────
    try:
        import wick_support as _ws, build_master as _bmw, config as _cfg
        _dly = _bmw.load_all(_cfg.OHLC_DIR, quiet=True)
        _dly["Date"] = pd.to_datetime(_dly["Date"]).dt.date
        _r = _ws.check(_dly, float(d["close"].iloc[-1]), prev_close)
        if _r.get("gap") is not None and _r["gap"] <= -60:
            st.markdown("#### Gap down — daily rejection wicks below")
            for _ln in _r["reasons"]:
                st.caption("· " + _ln)
            if _r["in_zone"]:
                st.success(
                    f"**Price is INSIDE a rejection zone** — "
                    f"{_r['in_zone'].label}  \nThis is where the rule says "
                    f"bearishness gets rejected. It is not an entry: wait for "
                    f"a candle close and a follow-through like any other "
                    f"trade.")
            for _a in _r["approaching"]:
                st.info(f"**Approaching a rejection zone** — {_a.label}")
            if _r["zones"] and not _r["in_zone"] and not _r["approaching"]:
                st.caption("Zones below, none reached yet: "
                           + " | ".join(z.label for z in _r["zones"][:3]))
    except Exception as _we:                                # noqa: BLE001
        pass

    st.markdown("#### Candlestick patterns — closed candles")

    # Levels in play, so a pattern can be read against what it formed AT.
    import pdc as _pdc
    _lvls = {}
    try:
        _px = float(d["close"].iloc[-1])
        _lvls = _pdc.all_levels(
            _px,
            st.session_state.get("chain_levels_now") or {},
            st.session_state.get("master_levels_now") or {})
    except Exception:                                       # noqa: BLE001
        pass

    prows = []
    for i, tags in enumerate(pats):
        if not tags:
            continue
        _cl = float(d["close"].iloc[i])
        _nr = _pdc.nearby(_cl, _lvls, 25.0)
        prows.append({
            "time": f"{s1._times(d).iloc[i]:%H:%M}",
            "patterns": ", ".join(tags),
            "where": s1.context(d, i, tags, p),
            "near support": _nr.support,
            "near resistance": _nr.resistance,
            "means": s1.PATTERN_MEANING.get(tags[0], ("", ""))[1],
        })
    if prows:
        st.dataframe(pd.DataFrame(prows), use_container_width=True, hide_index=True)
    else:
        st.caption("No patterns detected on today's closed candles.")

    st.caption(
        "Patterns are geometry — exact arithmetic on OHLC, with thresholds you "
        "set in `strategy1_rules.json`. Location is reported separately because "
        "a hammer mid-range and a hammer at the LBB are the same shape and "
        "different trades. Nothing here is an instruction to enter.")
