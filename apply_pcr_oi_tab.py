"""
apply_pcr_oi_tab.py — puts the new PCR · OI Change · Total OI vs NIFTY FUTURE page (pcr_oi_pro.py) at the TOP of
🧪 Options Lab → "📈 PCR & OI". Your existing five charts stay underneath, unchanged.
Edits YOUR options_lab_tab.py in place (backup first: options_lab_tab.py.bak_pcroi).

Run apply_future_fix.py FIRST — it adds dhan_client.nifty_futures_contract(), which this page uses to fetch the
current-month future (without it the page still works, using the synth future).

  .venv/bin/python apply_pcr_oi_tab.py --check
  .venv/bin/python apply_pcr_oi_tab.py
Undo: copy options_lab_tab.py.bak_pcroi back.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).parent
P = ROOT / "options_lab_tab.py"

OLD = '''    with t1:
        for key, fn in (("pcr", chart_pcr), ("chg", chart_chg), ("total", chart_total),'''
NEW = '''    with t1:
        try:                                                    # PCROI_PRO — PCR / OI Change / Total OI vs NIFTY future
            import pcr_oi_pro
            pcr_oi_pro.render(day)
        except Exception as _pcr_err:                           # noqa: BLE001
            st.error(f"PCR & OI vs Future encountered an error: {_pcr_err}")
        st.markdown("---")
        st.caption("Earlier PCR & OI charts (unchanged)")
        for key, fn in (("pcr", chart_pcr), ("chg", chart_chg), ("total", chart_total),'''


def main():
    check = "--check" in sys.argv
    for f in ("pcr_oi_pro.py", "pe_ce_panel.py"):
        if not (ROOT / f).exists():
            print(f"✘ {f} not found — copy it into the OC folder first"); return
    if not P.exists():
        print("✘ options_lab_tab.py not found — run from the OC folder"); return
    t = P.read_text(encoding="utf-8")
    if "PCROI_PRO" in t:
        print("• options_lab_tab.py: PCR & OI vs Future already added — nothing to do"); return
    if t.count(OLD) != 1:
        print("✘ options_lab_tab.py: could not find the PCR & OI tab block I expect — NOT changed (send me the file)"); return
    new = t.replace(OLD, NEW, 1)
    compile(new, "options_lab_tab.py", "exec")
    if check:
        print("✔ options_lab_tab.py: can add PCR & OI vs Future"); return
    shutil.copy2(P, P.with_name(P.name + ".bak_pcroi"))
    P.write_text(new, encoding="utf-8")
    print("✔ options_lab_tab.py: PCR & OI vs Future added (backup options_lab_tab.py.bak_pcroi) — refresh the dashboard")
    if "FUTFIX" not in (ROOT / "dhan_client.py").read_text(encoding="utf-8", errors="ignore"):
        print("  ⚠ run apply_future_fix.py too — until then the page uses the synth future")


if __name__ == "__main__":
    main()
