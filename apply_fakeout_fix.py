"""
apply_fakeout_fix.py — makes the Fakeout Avoidance tab (Liquidity Sweep → MSS → FVG → "SELL HERE") honest,
and stops the Breakout & Trap scanner signalling on a candle that has not closed.

Needs fakeout_engine.py in the same OC folder (it holds the rules and the signal log).

  1. Closed candles only — "SELL HERE", sweeps, FVGs and trap markers never paint on the forming candle.
  2. FVG must be ≥ 3 pts or 0.1 × ATR (today's 0.3-pt "killzone" no longer counts).
  3. A sweep is cancelled when a later candle closes above the swept high (14:35 today).
  4. Entry only after the MSS candle; stop = highest high since the sweep + 2; T1 = 1R, T2 = 2R;
     the TP line is the swing low that was known at the entry, not a low that formed later.
  5. Every entry is logged in data/signals/fakeout_log.csv with the time it first appeared + its result;
     a "Track record" panel shows the live hit rate (signals first seen after hours are not counted).
  +  IMMEDIATE_REVERSAL now needs a real break in the last 1–2 candles (a plain support break was being
     called "breakout buyers trapped"); the made-up "78% failure rate" line is removed.

Edits (backups *.bak_fakeout): breakout_tab.py, price_action_breakout.py, fakeout_avoidance_ui.py.
Safe to run twice.

  .venv/bin/python apply_fakeout_fix.py --check
  .venv/bin/python apply_fakeout_fix.py
"""
from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).parent
MARK = "FAKEOUTFIX"

# ── breakout_tab.py ──────────────────────────────────────────────────────────
BT_SETUP_RE = re.compile(r"        # Run structural scan for active sweep setup\n.*?(?=        # Component 2: Annotation Switchboard\n)", re.S)
BT_SETUP_NEW = r'''        # FAKEOUTFIX — rules live in fakeout_engine.py: closed candles only, FVG ≥ 3 pts / 0.1×ATR, sweep cancelled
        # by a close above the swept high, entry only after the MSS, stop above the true high, every entry logged.
        import fakeout_engine as _fx
        _closed = _fx.closed_only(df_raw)
        _fxs = _fx.find_setup(_closed)
        active_setup = {k: v for k, v in _fxs.items()
                        if k in ("sweep_bar_idx", "sweep_price", "mss_level", "mss_bar_idx", "fvg_top",
                                 "fvg_bottom", "fvg_start_idx", "entry_bar_idx", "entry_price")}
        all_sweeps = [_fxs] if "sweep_bar_idx" in _fxs else []
        try:
            _fx.log_signal(_fxs, tf, now=now)
        except Exception as _log_err:                      # noqa: BLE001
            _fx.LAST_ERROR = f"signal log not saved: {_log_err}"
        _status = _fxs.get("status", "")
        # TP line only when there is a trade, and only the swing low KNOWN at the entry candle
        equal_lows = float(_fxs.get("ssl_target") or 0.0) if _status == "ENTRY" else 0.0

        is_forming_last = bool(not df_raw.iloc[-1].get("closed", True)) if "closed" in df_raw.columns else False
        h_last = float(df_raw["high"].iloc[-1])
        is_mid_testing = (h_last >= bsl_level) and is_forming_last
        entry_done = _status == "ENTRY"
        _sw_ok = "sweep_bar_idx" in _fxs
        _mss_ok = "mss_level" in _fxs
        _fvg_ok = "fvg_top" in _fxs
        kanban_state = {
            "bsl_swept": _sw_ok,
            "bsl_waiting_close": is_mid_testing,
            "bsl_detail": (f"{_fxs['sweep_time']} wick {_fxs['sweep_price']:,.1f} over {_fxs['sweep_level']:,.1f}" if _sw_ok
                           else ("Last sweep cancelled — closed back above" if _fxs.get("cancelled") else f"BSL {bsl_level:,.1f}")),
            "rejection_closed": _sw_ok,
            "rejection_detail": "Closed back below, no close above since" if _sw_ok else "Waiting for a CLOSED rejection",
            "mss_shifted": _mss_ok,
            "mss_detail": f"{_fxs['mss_time']} closed below {_fxs['mss_level']:,.1f}" if _mss_ok else "Needs a CLOSE below a swing low",
            "fvg_formed": _fvg_ok,
            "fvg_detail": (f"{_fxs['fvg_bottom']:,.1f}–{_fxs['fvg_top']:,.1f} ({_fxs['fvg_top'] - _fxs['fvg_bottom']:.1f} pts)" if _fvg_ok
                           else f"Needs a gap ≥ {_fx.MIN_FVG_PTS:g} pts / {_fx.MIN_FVG_ATR:g}×ATR"),
            "entry_active": entry_done,
            "entry_detail": (f"{_fxs['entry_time']} close {_fxs['entry_price']:,.1f}" if entry_done
                             else "After the MSS: a CLOSED candle inside the FVG"),
        }

'''

BT_CARD_RE = re.compile(r"        # Execution Advice Card\n        if entry_done:\n.*?(?=\n    # ═+\n    # VIEW B)", re.S)
BT_CARD_NEW = r'''        # Execution card — FAKEOUTFIX
        if entry_done:
            _res = _fxs.get("result", "OPEN")
            _rr = float(_fxs.get("r") or 0.0)
            _res_txt = {"OPEN": "running", "T1": f"T1 hit at {_fxs.get('result_time', '')} (+1R)",
                        "SL": f"stopped at {_fxs.get('result_time', '')} (−1R)",
                        "EOD": f"no target or stop by the close ({_rr:+.2f}R)"}.get(_res, _res)
            (st.success if _res in ("OPEN", "T1") else st.warning)(
                f"🎯 **SHORT / BUY PE — {_fxs['entry_time']} candle CLOSED inside the FVG**  \n"
                f"• **Entry:** `{_fxs['entry_price']:,.1f}` | **Stop:** `{_fxs['sl']:,.1f}` (highest high since the sweep "
                f"+ {_fx.SL_BUFFER:g} · risk `{_fxs['risk']:,.1f}` pts)  \n"
                f"• **T1 (1R):** `{_fxs['t1']:,.1f}` | **T2 (2R):** `{_fxs['t2']:,.1f}`"
                + (f" | SSL known at entry: `{_fxs['ssl_target']:,.1f}`" if _fxs.get("ssl_target") else "")
                + f"  \n• **Status:** {_res_txt}" + (" · T2 reached" if _fxs.get("t2_hit") else "")
            )
        elif is_mid_testing:
            st.warning(
                f"⚠️ **MID-CANDLE ALERT:** Price is actively probing BSL ({bsl_level:,.1f}). **DO NOT ENTER.** "
                f"Wait for the {tf} candle to close — nothing on this tab is painted from a forming candle."
            )
        else:
            _why = {"NO_SWEEP": "no sweep above a swing high yet", "CANCELLED": "the last sweep was cancelled",
                    "WAIT_MSS": "sweep standing, waiting for the MSS close", "WAIT_FVG": "MSS done, no qualifying FVG",
                    "WAIT_FVG_TAP": "waiting for a closed candle back inside the FVG"}.get(_status, _status)
            st.info(f"💡 **No short setup** — {_why}.")

        with st.expander("🧾 Rule check — why the tab says what it says", expanded=not entry_done):
            for _n in _fxs.get("notes", []):
                st.markdown(f"- {_n}")
            st.caption(f"Rules: closed candles only · FVG ≥ {_fx.MIN_FVG_PTS:g} pts or {_fx.MIN_FVG_ATR:g}×ATR · "
                       f"sweep cancelled by a close above the swept high · entry only after the MSS candle · "
                       f"stop = highest high since the sweep + {_fx.SL_BUFFER:g} · T1 1R, T2 2R.")

        with st.expander("📒 Track record — every entry, logged when the app first showed it", expanded=False):
            _tr = _fx.track_record(live_only=True)
            if _tr["n"] == 0:
                st.info("No live signals logged yet. Entries first seen after the market closes are kept as "
                        "'backfill' and not counted — only what you could actually have traded counts.")
            else:
                _m = st.columns(5)
                _m[0].metric("Live signals", _tr["n"])
                _m[1].metric("T1 hit", f"{_tr['t1']}/{_tr['done']}" if _tr["done"] else "—")
                _m[2].metric("Stopped", f"{_tr['sl']}/{_tr['done']}" if _tr["done"] else "—")
                _m[3].metric("T2 reached", f"{_tr['t2']}/{_tr['done']}" if _tr["done"] else "—")
                _m[4].metric("Avg R", f"{_tr['avg_r']:+.2f}" if _tr["done"] else "—")
                _cols = ["date", "tf", "entry_time", "entry", "sl", "t1", "t2", "first_seen", "lag_min", "result", "result_time", "r"]
                st.dataframe(_tr["rows"][_cols].tail(20), use_container_width=True, hide_index=True)
                if _tr["done"] < 20:
                    st.caption(f"Only {_tr['done']} finished live signal(s) — fewer than 20 is too few to call it an edge.")
            if _tr.get("backfill"):
                st.caption(f"{_tr['backfill']} backfill signal(s) (first seen after the candle was long closed) not counted.")
            if getattr(_fx, "LAST_ERROR", ""):
                st.caption(_fx.LAST_ERROR)
'''

BT_VIEWB_OLD = "        df_signals = engine.apply_breakout_signals(df_raw, key_levels)\n"
BT_VIEWB_NEW = BT_VIEWB_OLD + r'''        # FAKEOUTFIX — the forming candle never carries a signal; it is judged once it closes
        if "closed" in df_raw.columns and len(df_signals) and not bool(df_raw["closed"].iloc[-1]):
            _last = df_signals.index[-1]
            for _col in ("breakout_signal", "trap_signal", "actionable_retest_entry", "primary_signal"):
                if _col in df_signals.columns:
                    df_signals.loc[_last, _col] = ""
            for _col in ("entry_price", "stop_loss", "target_1", "target_2", "risk_pts", "rr_ratio"):
                if _col in df_signals.columns:
                    df_signals.loc[_last, _col] = np.nan
'''

# ── price_action_breakout.py ─────────────────────────────────────────────────
PAB_OLD = '''                if i >= 1 and trap_signal[i] == "":
                    # Bullish Trap (Longs trapped above resistance):
                    c1_above = c_prev > L_prev
'''
PAB_NEW = '''                if i >= 1 and trap_signal[i] == "":
                    # FAKEOUTFIX: a trap needs a real break in the last 1–2 candles — a close on the other side of
                    # the level at i-2 or i-3. Before, a plain support break after hours above it was called a "bull trap".
                    def _inside(j, up, _lvl=lvl, _Lp=L_prev):
                        if j < 0:
                            return False
                        Lj = _lvl.get_price_at(j)
                        if np.isnan(Lj):
                            Lj = _Lp
                        return bool(c[j] <= Lj) if up else bool(c[j] >= Lj)
                    # Bullish Trap (Longs trapped above resistance):
                    c1_above = (c_prev > L_prev) and (_inside(i - 2, True) or _inside(i - 3, True))
'''
PAB_OLD2 = "                    c1_below = c_prev < L_prev\n"
PAB_NEW2 = "                    c1_below = (c_prev < L_prev) and (_inside(i - 2, False) or _inside(i - 3, False))   # FAKEOUTFIX\n"

# ── fakeout_avoidance_ui.py ──────────────────────────────────────────────────
UI_OLD = "Candle still forming! Unclosed breakouts have 78% failure rate."
UI_NEW = "Candle still forming — judge it only when it closes."     # the made-up "78%" claim is removed


def _plan():
    out = {}
    p = ROOT / "breakout_tab.py"
    t = p.read_text(encoding="utf-8")
    if MARK in t:
        out[p] = None
    else:
        m1, m2 = BT_SETUP_RE.search(t), BT_CARD_RE.search(t)
        if not m1 or not m2 or t.count(BT_VIEWB_OLD) != 1:
            raise SystemExit("✘ breakout_tab.py differs from the version I know — NOTHING changed (send me the file)")
        t = t[:m1.start()] + BT_SETUP_NEW + t[m1.end():]
        m2 = BT_CARD_RE.search(t)
        t = t[:m2.start()] + BT_CARD_NEW.rstrip("\n") + "\n" + t[m2.end():]
        t = t.replace(BT_VIEWB_OLD, BT_VIEWB_NEW, 1)
        out[p] = t

    p = ROOT / "price_action_breakout.py"
    t = p.read_text(encoding="utf-8")
    if MARK in t:
        out[p] = None
    else:
        if t.count(PAB_OLD) != 1 or t.count(PAB_OLD2) != 1:
            raise SystemExit("✘ price_action_breakout.py differs from the version I know — NOTHING changed")
        out[p] = t.replace(PAB_OLD, PAB_NEW, 1).replace(PAB_OLD2, PAB_NEW2, 1)

    p = ROOT / "fakeout_avoidance_ui.py"
    t = p.read_text(encoding="utf-8")
    if MARK in t:
        out[p] = None
    elif UI_OLD in t:
        out[p] = t.replace(UI_OLD, UI_NEW, 1)
    else:
        out[p] = None                                       # wording already changed by hand — fine
    return out


def main():
    check = "--check" in sys.argv
    for f in ("breakout_tab.py", "price_action_breakout.py", "fakeout_avoidance_ui.py", "smc.py"):
        if not (ROOT / f).exists():
            print(f"✘ {f} not found — run from the OC folder"); return
    if not (ROOT / "fakeout_engine.py").exists():
        print("✘ fakeout_engine.py missing — copy it into the OC folder first"); return
    plan = _plan()
    for p, new in plan.items():
        if new is not None:
            compile(new, p.name, "exec")
    compile((ROOT / "fakeout_engine.py").read_text(encoding="utf-8"), "fakeout_engine.py", "exec")
    if check:
        for p, new in plan.items():
            print(f"• {p.name}: " + ("already fixed" if new is None else "can be fixed"))
        return
    for p, new in plan.items():
        if new is None:
            print(f"• {p.name}: already fixed"); continue
        shutil.copy2(p, p.with_name(p.name + ".bak_fakeout"))
        p.write_text(new, encoding="utf-8")
        print(f"✔ {p.name}: fixed (backup {p.name}.bak_fakeout)")
    (ROOT / "data" / "signals").mkdir(parents=True, exist_ok=True)
    print("✔ signal log: data/signals/fakeout_log.csv (created on the first entry) — refresh the dashboard")


if __name__ == "__main__":
    main()
