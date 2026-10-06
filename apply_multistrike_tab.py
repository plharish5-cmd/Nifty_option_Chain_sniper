"""
apply_multistrike_tab.py — adds the "📈 Multi-strike OI" sub-tab to 🧪 Options Lab (after "🟥 PE − CE OI Table"),
editing YOUR options_lab_tab.py in place (backup first: options_lab_tab.py.bak_multistrike). Needs multistrike_panel.py
in the same folder. Nothing else in Options Lab changes.

  .venv/bin/python apply_multistrike_tab.py --check
  .venv/bin/python apply_multistrike_tab.py
Then refresh the dashboard. Undo: copy options_lab_tab.py.bak_multistrike back.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).parent
P = ROOT / "options_lab_tab.py"

TABS_OLD = '''    t0, t1, t2, t3, t4, t5, t6 = st.tabs(["📊 Open Interest", "📈 PCR & OI", "🕯️ Smart OI", "🧲 Gamma & Vega", "📅 IV − HV",
                                          "📊 Call vs Put Writers (3m / 5m / 15m)", "🟥 PE − CE OI Table"])   # TABMOVE'''
TABS_NEW = '''    t0, t1, t2, t3, t4, t5, t6, t7 = st.tabs(["📊 Open Interest", "📈 PCR & OI", "🕯️ Smart OI", "🧲 Gamma & Vega", "📅 IV − HV",
                                              "📊 Call vs Put Writers (3m / 5m / 15m)", "🟥 PE − CE OI Table",
                                              "📈 Multi-strike OI"])   # TABMOVE MULTISTRIKE'''
TAIL_OLD = '''            import pe_ce_panel
            pe_ce_panel.render()
        except Exception as _pece_err:                          # noqa: BLE001
            st.error(f"PE − CE OI Table encountered an error: {_pece_err}")'''
TAIL_NEW = TAIL_OLD + '''
    with t7:                                                    # MULTISTRIKE — top-N strikes' OI through the day
        try:
            import multistrike_panel
            multistrike_panel.render(day)
        except Exception as _ms_err:                            # noqa: BLE001
            st.error(f"Multi-strike OI encountered an error: {_ms_err}")'''


def main():
    check = "--check" in sys.argv
    if not (ROOT / "multistrike_panel.py").exists():
        print("✘ multistrike_panel.py not found — copy it into the OC folder first"); return
    if not P.exists():
        print("✘ options_lab_tab.py not found — run from the OC folder"); return
    t = P.read_text(encoding="utf-8")
    if "MULTISTRIKE" in t:
        print("• options_lab_tab.py: Multi-strike OI tab already added — nothing to do"); return
    if t.count(TABS_OLD) != 1 or t.count(TAIL_OLD) != 1:
        print("✘ options_lab_tab.py differs from the version I know (run apply_tab_move.py first?) — NOT changed"); return
    new = t.replace(TABS_OLD, TABS_NEW, 1).replace(TAIL_OLD, TAIL_NEW, 1)
    compile(new, "options_lab_tab.py", "exec")
    if check:
        print("✔ options_lab_tab.py: can add the Multi-strike OI tab"); return
    shutil.copy2(P, P.with_name(P.name + ".bak_multistrike"))
    P.write_text(new, encoding="utf-8")
    print("✔ options_lab_tab.py: Multi-strike OI tab added (backup options_lab_tab.py.bak_multistrike) — refresh the dashboard")


if __name__ == "__main__":
    main()
