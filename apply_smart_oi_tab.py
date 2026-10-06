"""
apply_smart_oi_tab.py — puts the new StockMojo-style Smart OI dashboard (smart_oi_pro.py) at the TOP of
🧪 Options Lab → "🕯️ Smart OI". Your existing Smart OI chart and its read stay underneath, unchanged.
Edits YOUR options_lab_tab.py in place (backup first: options_lab_tab.py.bak_smartoi). Nothing else changes.

  .venv/bin/python apply_smart_oi_tab.py --check
  .venv/bin/python apply_smart_oi_tab.py
Then refresh the dashboard. Undo: copy options_lab_tab.py.bak_smartoi back.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).parent
P = ROOT / "options_lab_tab.py"

OLD = '''    with t2:
        if len(c):
            st.plotly_chart(chart_flow(c, tf), use_container_width=True, key="olab_flow")'''
NEW = '''    with t2:
        try:                                                    # SMARTOI_PRO — StockMojo-style Smart OI dashboard
            import smart_oi_pro
            smart_oi_pro.render(day)
        except Exception as _sop_err:                           # noqa: BLE001
            st.error(f"Smart OI dashboard encountered an error: {_sop_err}")
        st.markdown("---")
        st.caption("Earlier Smart OI view (unchanged)")
        if len(c):
            st.plotly_chart(chart_flow(c, tf), use_container_width=True, key="olab_flow")'''


def main():
    check = "--check" in sys.argv
    for f in ("smart_oi_pro.py", "pe_ce_panel.py"):
        if not (ROOT / f).exists():
            print(f"✘ {f} not found — copy it into the OC folder first"); return
    if not P.exists():
        print("✘ options_lab_tab.py not found — run from the OC folder"); return
    t = P.read_text(encoding="utf-8")
    if "SMARTOI_PRO" in t:
        print("• options_lab_tab.py: Smart OI dashboard already added — nothing to do"); return
    if t.count(OLD) != 1:
        print("✘ options_lab_tab.py: could not find the Smart OI tab block I expect — NOT changed (send me the file)"); return
    new = t.replace(OLD, NEW, 1)
    compile(new, "options_lab_tab.py", "exec")
    if check:
        print("✔ options_lab_tab.py: can add the Smart OI dashboard"); return
    shutil.copy2(P, P.with_name(P.name + ".bak_smartoi"))
    P.write_text(new, encoding="utf-8")
    print("✔ options_lab_tab.py: Smart OI dashboard added (backup options_lab_tab.py.bak_smartoi) — refresh the dashboard")


if __name__ == "__main__":
    main()
