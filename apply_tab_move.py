"""
apply_tab_move.py — moves two sub-tabs IN PLACE in your own files (backups first: <name>.bak_tabmove):

  FROM  ⚡ Option Chain & Open Interest Analysis   (option_chain_oi_tab.py)
          📊 Call vs Put Writers Graph (3m / 5m / 15m)
          🟥 PE − CE OI Table & Smart OI
  TO    🧪 Options Lab                             (options_lab_tab.py), right after "📅 IV − HV"

Nothing inside the two panels changes — they call the same functions as before (writers_graph_engine.render_writers_graph_ui,
pe_ce_panel.render). The other sub-tabs ("⚡ OI Breakout & Anti-Trap Analysis", "🎯 Strike Selection …", Open Interest,
PCR & OI, Smart OI, Gamma & Vega, IV − HV) are not touched.

  .venv/bin/python apply_tab_move.py --check     # report only
  .venv/bin/python apply_tab_move.py             # apply (safe to run twice)
Then refresh the Streamlit page. Undo: copy the .bak_tabmove files back.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).parent

# ── option_chain_oi_tab.py ─────────────────────────────────────────────────
OC_TABS_OLD = '''    tab_oi, tab_strike_select, tab_writers_graph, tab_pe_ce = st.tabs([
        "⚡ OI Breakout & Anti-Trap Analysis",
        "🎯 Strike Selection & Premium Behaviour (₹200–₹300 Rule)",
        "📊 Call vs Put Writers Graph (3m / 5m / 15m)",
        "🟥 PE − CE OI Table & Smart OI",
    ])'''
OC_TABS_NEW = '''    tab_oi, tab_strike_select = st.tabs([
        "⚡ OI Breakout & Anti-Trap Analysis",
        "🎯 Strike Selection & Premium Behaviour (₹200–₹300 Rule)",
    ])
    # TABMOVE: "📊 Call vs Put Writers Graph" and "🟥 PE − CE OI Table & Smart OI" now live in 🧪 Options Lab'''
OC_BODY_OLD = '''    with tab_writers_graph:
        try:
            import writers_graph_engine
            writers_graph_engine.render_writers_graph_ui(
                df=df,
                spot=spot,
                pa_frames=pa_frames,
                read=read,
            )
        except Exception as _wge_err:
            log.exception(f"Call vs Put Writers Graph encountered an error: {_wge_err}")
            st.error(f"Call vs Put Writers Graph encountered an error: {_wge_err}")

    with tab_pe_ce:
        try:
            import pe_ce_panel
            pe_ce_panel.render()
        except Exception as _pece_err:
            log.exception(f"PE − CE OI Table encountered an error: {_pece_err}")
            st.error(f"PE − CE OI Table encountered an error: {_pece_err}")'''
OC_BODY_NEW = '''    # TABMOVE: the Writers Graph and PE − CE OI Table blocks moved to options_lab_tab.render()'''

# ── options_lab_tab.py ─────────────────────────────────────────────────────
OL_TABS_OLD = '''    t0, t1, t2, t3, t4 = st.tabs(["📊 Open Interest", "📈 PCR & OI", "🕯️ Smart OI", "🧲 Gamma & Vega", "📅 IV − HV"])'''
OL_TABS_NEW = '''    t0, t1, t2, t3, t4, t5, t6 = st.tabs(["📊 Open Interest", "📈 PCR & OI", "🕯️ Smart OI", "🧲 Gamma & Vega", "📅 IV − HV",
                                          "📊 Call vs Put Writers (3m / 5m / 15m)", "🟥 PE − CE OI Table"])   # TABMOVE'''
OL_TAIL_OLD = '''            st.info("IV − HV needs at least ~10 stored days (or a real daily OHLC file in data/ohlc).")
        _panel("ivhv", *reads["ivhv"])'''
OL_TAIL_NEW = OL_TAIL_OLD + '''
    with t5:                                                    # TABMOVE — moved from ⚡ Option Chain & OI Analysis
        try:
            import writers_graph_engine
            writers_graph_engine.render_writers_graph_ui(df=latest[2], spot=float(latest[1]), pa_frames=None, read=None)
        except Exception as _wge_err:                           # noqa: BLE001
            st.error(f"Call vs Put Writers Graph encountered an error: {_wge_err}")
    with t6:                                                    # TABMOVE — moved from ⚡ Option Chain & OI Analysis
        try:
            import pe_ce_panel
            pe_ce_panel.render()
        except Exception as _pece_err:                          # noqa: BLE001
            st.error(f"PE − CE OI Table encountered an error: {_pece_err}")'''


def _patch(text, pairs, name):
    if "TABMOVE" in text:
        return None, f"{name}: already moved — nothing to do"
    for old, _ in pairs:
        if text.count(old) != 1:
            return None, f"{name}: the code differs from the version I know (anchor found {text.count(old)}×) — NOT changed"
    for old, new in pairs:
        text = text.replace(old, new, 1)
    return text, f"{name}: ok"


def main():
    check = "--check" in sys.argv
    plan = [("option_chain_oi_tab.py", [(OC_TABS_OLD, OC_TABS_NEW), (OC_BODY_OLD, OC_BODY_NEW)]),
            ("options_lab_tab.py", [(OL_TABS_OLD, OL_TABS_NEW), (OL_TAIL_OLD, OL_TAIL_NEW)])]
    results = []
    for name, pairs in plan:                 # check BOTH files first — move only if both can be changed
        p = ROOT / name
        if not p.exists():
            print(f"✘ {name} not found — run from the OC folder"); return
        old = p.read_text(encoding="utf-8")
        new, msg = _patch(old, pairs, name)
        if new is not None:
            try:
                compile(new, name, "exec")
            except SyntaxError as e:
                new, msg = None, f"{name}: patched code would not compile ({e}) — NOT changed"
        results.append((p, new, msg))
        print(("✔ " if new is not None else "• ") + msg)
    if check:
        return
    if not all(new is not None for _, new, _ in results):
        if any(new is not None for _, new, _ in results):
            print("Nothing written: both files must be patchable, otherwise the tabs would disappear or appear twice.")
        return
    for p, new, _ in results:
        shutil.copy2(p, p.with_name(p.name + ".bak_tabmove"))
        p.write_text(new, encoding="utf-8")
        print(f"  written {p.name}  (backup {p.name}.bak_tabmove)")
    print("Done — refresh the dashboard page. The two tabs are now in 🧪 Options Lab after 📅 IV − HV.")


if __name__ == "__main__":
    main()
