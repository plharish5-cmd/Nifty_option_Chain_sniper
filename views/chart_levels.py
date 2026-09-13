"""
views/chart_levels.py — the first migrated view.

THE PATTERN, WHICH IS THE POINT OF THIS FILE
────────────────────────────────────────────
    def render(ctx) -> None

That is the whole contract. Everything the view needs comes off `ctx`, and
nothing comes from a module-level global in dashboard.py. Copy this shape
for each remaining view and the router at the end becomes trivial.

WHAT CHANGED FROM THE ORIGINAL TAB BODY
───────────────────────────────────────
Almost nothing, deliberately. `now`, `spot` and `config` are read off ctx
instead of the enclosing module, `refresh_control` arrives as a callable,
and the code is otherwise identical — including the comments explaining why
PDC is a separate block.

That restraint matters during a migration. Moving code and improving it at
the same time means that when the output differs afterwards, you cannot
tell which change caused it. Move first, verify identical output, improve
later.

NAMED chart_levels, NOT levels
──────────────────────────────
There is already a top-level levels.py — the price-structure engine that
confluence.py imports PriceLevel and WEIGHTS from. A view called levels.py
collides with it the moment either file is copied into the project root,
and the failure is ugly: `ImportError: cannot import name 'PriceLevel'`,
raised from a module that has nothing to do with the change you made.

Match the tab name, not the domain, when naming views.

WHY THIS VIEW WENT FIRST
────────────────────────
It reads levels and writes nothing. No other view depends on anything it
computes, so getting it wrong cannot break tab 18 the way `chain_levels_now`
would.
"""

from __future__ import annotations

from datetime import timedelta

import pandas as pd
import streamlit as st

import config
import market_data as md


def render(ctx, refresh_control=None, ohlc_cache=None,
           daily_levels_chart=None) -> None:
    """Chart levels — dated levels for marking up a chart.

    `refresh_control`, `ohlc_cache` and `daily_levels_chart` all live in
    dashboard.py and are passed in rather than imported. A view importing
    from the orchestrator it was extracted from would be circular, and it
    would defeat the exercise: the point is that a view depends on what it
    is handed, not on the file it used to live inside.

    A static scan for unresolved names found these one at a time. Worth
    running over each view as you migrate it — the first two only surfaced
    at runtime, on the tab, after the app was up.
    """
    now = ctx.now
    spot = ctx.spot

    # ONE refresh_control call.
    #
    # Extracting the tab body carried its original call across, and the
    # header I wrote added a second — so the view called it twice, and the
    # surviving copy still referenced load_ohlc_cached, a Streamlit cache
    # that lives in dashboard.py and does not exist here. That is the exact
    # failure the ctx contract is meant to prevent: a view reaching for a
    # module-level global from the file it was extracted from.
    if refresh_control is not None:
        refresh_control("chartlevels", (ohlc_cache,) if ohlc_cache else (),
                        pull_ohlc=True, note="daily OHLC")
    st.caption("Dated levels for marking up your chart. Copy the block, paste "
               "into Kite or TradingView. Rebuilt automatically whenever the "
               "daily OHLC updates after 15:30.")

    try:
        import build_master as _bm
        _ohlc = _bm.load_all(config.OHLC_DIR, quiet=True)
        _lv = _bm.dated_levels(_ohlc)
        _txt = _bm.format_copy_paste(_lv)

        # PDC appended to the same block, so one copy takes everything.
        # Kept as its own section rather than folded into supports and
        # resistances: a PDC line is a closing price, not a swing high or
        # low, and it is only support or resistance depending on which side
        # price is currently on. Merging them would lose that distinction
        # exactly where you need it — at the moment you are drawing lines.
        try:
            import pdc as _pdcC
            _pdctxt = _pdcC.format_copy_paste(4)
            if _pdctxt:
                _txt += "\n\n" + _pdctxt
            else:
                _txt += ("\n\n🟣 PDC — none recorded yet. Run `python eod.py` "
                         "or press Backfill PDC in the VWAP/EMA tab.")
        except Exception as _pe:                            # noqa: BLE001
            _txt += f"\n\n🟣 PDC unavailable: {_pe}"

        # How old is the underlying file? Check against completed trading sessions
        # rather than pure calendar days, so pre-market, intraday, and weekends
        # correctly reflect the most recent settled close.
        _last = _ohlc["Date"].max()
        _last_dt = _last.date() if hasattr(_last, "date") else _last
        _append_cutoff = getattr(config, "OHLC_APPEND_AFTER", config.MARKET_CLOSE)
        _missing_sessions = [
            _last_dt + timedelta(days=i)
            for i in range(1, (now.date() - _last_dt).days + 1)
            if (_last_dt + timedelta(days=i)).weekday() < 5
            and ((_last_dt + timedelta(days=i)) < now.date() or now.time() >= _append_cutoff)
        ]
        _sessions_behind = len(_missing_sessions)

        if _sessions_behind > 0:
            _b1, _b2 = st.columns([3, 1])
            _b1.error(
                f"**Levels are from {_last:%d-%b-%Y} — {_sessions_behind} session(s) "
                f"behind.** Today is {now:%d-%b-%Y}. The OHLC file has not "
                f"been updated, so these levels do not include the most "
                f"recent completed session(s). Refresh only re-reads the file; "
                f"it does not fetch a new bar.")
            if _b2.button("Pull latest bar now", use_container_width=True,
                          key="pull_bar_chart"):
                try:
                    _ok, _m = False, ""
                    # 1. Primary: DhanHQ
                    try:
                        _ok, _m = md.backfill_ohlc_dhan(config.OHLC_BACKFILL_DAYS)
                    except Exception:
                        pass
                    # 2. Fallback: NSE
                    if not _ok:
                        try:
                            import nse_client as _pc
                            _pcl = _pc.NSEClient(); _pcl._bootstrap()
                            _ok, _m = md.backfill_ohlc(_pcl, config.OHLC_BACKFILL_DAYS)
                        except Exception as _ne:
                            _m = f"NSE unreachable: {_ne}"

                    if _ok:
                        st.success(_m)
                        import build_master as _b
                        _b.main()
                        if ohlc_cache is not None:
                            ohlc_cache.clear()
                        st.rerun()
                    else:
                        st.info(_m)
                except Exception as _e:                     # noqa: BLE001
                    st.error(f"Could not pull: {_e}")
        else:
            st.success(f"Levels are current — from the {_last:%d-%b-%Y} close.")

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("From session", _lv["date"])
        c2.metric("Close", f"{_lv['close']:,.2f}")
        c3.metric("Sessions loaded", f"{len(_ohlc):,}")
        _nbars = c4.number_input("Bars to show", 20, 250, 60, step=10)

        st.plotly_chart(daily_levels_chart(_ohlc, _lv, spot, int(_nbars)),
                        use_container_width=True)

        st.code(_txt, language="text")
        st.caption("Hover the block and use the copy icon in its top-right "
                   "corner.")

        st.download_button("Download as .txt", _txt,
                           file_name=f"nifty_levels_{_lv['date']}.txt",
                           mime="text/plain")

        with st.expander("Levels against live spot"):
            _rows = []
            for lab, items, kind in (("R", _lv["resistances"], "resistance"),
                                     ("S", _lv["supports"], "support"),
                                     ("UG", _lv["upper_gaps"], "upper gap"),
                                     ("LG", _lv["lower_gaps"], "lower gap")):
                for i, (d, pr) in enumerate(items, 1):
                    _rows.append({"Level": f"{lab}{i}", "Type": kind,
                                  "Formed on": d, "Price": f"{pr:,.2f}",
                                  "vs spot": f"{pr - spot:+,.0f}"})
            _ldf = pd.DataFrame(_rows)
            _ldf["_d"] = _ldf["vs spot"].str.replace(",", "").astype(float).abs()

            def _band(dist: float) -> str:
                if dist <= config.LEVEL_NEAR_PTS:
                    return "AT LEVEL"
                if dist <= config.LEVEL_APPROACH_PTS:
                    return "APPROACHING"
                return ""

            _ldf["Alert"] = _ldf["_d"].map(_band)
            _ldf = _ldf.sort_values("_d").drop(columns="_d")
            _order = ["Alert", "Level", "Type", "Formed on", "Price", "vs spot"]
            _ldf = _ldf[[c for c in _order if c in _ldf.columns]]

            def _style(row):
                if row["Alert"] == "AT LEVEL":
                    return ["background-color: #7A1F1A; color: #FFE8E5"] * len(row)
                if row["Alert"] == "APPROACHING":
                    return ["background-color: #6B5310; color: #FFF3CC"] * len(row)
                return [""] * len(row)

            st.dataframe(_ldf.style.apply(_style, axis=1),
                         use_container_width=True, hide_index=True)

            _hot = _ldf[_ldf.Alert != ""]
            if len(_hot):
                st.warning(f"**Spot {spot:,.0f} is near {len(_hot)} level(s).** "
                           f"Red = within {config.LEVEL_NEAR_PTS} pts, amber = "
                           f"within {config.LEVEL_APPROACH_PTS}.")
            st.caption("R1 is the latest session's high, then progressively "
                       "higher highs walking back. Supports mirror it. Gaps "
                       "are unfilled closes, dated to the session that made "
                       "them.")

        # ── Historical Day High/Low & 15m Long Wick Engine ──────────────────
        try:
            import historical_levels as _hl
            _idx_c = getattr(ctx, "index_candles", None)
            _t_hi = float(_idx_c["high"].max()) if (_idx_c is not None and not _idx_c.empty and "high" in _idx_c) else spot
            _t_lo = float(_idx_c["low"].min()) if (_idx_c is not None and not _idx_c.empty and "low" in _idx_c) else spot
            _hl.render_chart_levels_section(spot, today_high=_t_hi, today_low=_t_lo, current_date=now.date())
        except Exception as _hle:
            st.warning(f"Historical Key Levels Engine error: {_hle}")

    except Exception as exc:                                # noqa: BLE001
        st.warning("Could not build the level list.")
        st.code(str(exc), language="text")
        st.caption("Needs daily OHLC in data/ohlc/. Run `python "
                   "update_daily.py` after 15:30, or `python build_master.py`.")
