"""
apply_premium_tab.py — adds a "⏳ Premium Decay" sub-tab (premium_decay_pro.py) as the LAST tab of 🧪 Options Lab.
Edits YOUR options_lab_tab.py in place (backup first: options_lab_tab.py.bak_premium). Every existing tab is untouched.
Order if you have not run them yet: apply_multistrike_tab.py → apply_vega_tab.py → this one.

  .venv/bin/python apply_premium_tab.py --check
  .venv/bin/python apply_premium_tab.py
Undo: copy options_lab_tab.py.bak_premium back.
"""
from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).parent
P = ROOT / "options_lab_tab.py"
TABS_RE = re.compile(r"^(    )(t0(?:, t\d+)+) = st\.tabs\(\[(.*?)\]\)(\s*# TABMOVE[^\n]*)$", re.S | re.M)
TAILS = [   # last tab's block, newest layout first
    '''            st.error(f"Vega Analysis encountered an error: {_vg_err}")''',
    '''            st.error(f"Multi-strike OI encountered an error: {_ms_err}")''',
    '''            st.error(f"PE − CE OI Table encountered an error: {_pece_err}")''',
]
BEFORE = [("multistrike_panel.py", "apply_multistrike_tab.py", "MULTISTRIKE"),
          ("vega_pro.py", "apply_vega_tab.py", "VEGAPRO")]


def main():
    check = "--check" in sys.argv
    if not (ROOT / "premium_decay_pro.py").exists():
        print("✘ premium_decay_pro.py not found — copy it into the OC folder first"); return
    if not P.exists():
        print("✘ options_lab_tab.py not found — run from the OC folder"); return
    t = P.read_text(encoding="utf-8")
    if "PREMIUMPRO" in t:
        print("• options_lab_tab.py: Premium Decay tab already added — nothing to do"); return
    for mod, script, mark in BEFORE:
        if (ROOT / mod).exists() and (ROOT / script).exists() and mark not in t:
            print(f"✘ run  {script}  first (then this one) — NOT changed"); return
    m = list(TABS_RE.finditer(t))
    tail = next((x for x in TAILS if t.count(x) == 1), None)
    if len(m) != 1 or tail is None:
        print("✘ options_lab_tab.py differs from the version I know — NOT changed (send me the file)"); return
    m = m[0]
    new_var = f"t{len(m.group(2).split(', '))}"
    tabs_new = (f'{m.group(1)}{m.group(2)}, {new_var} = st.tabs([{m.group(3)},\n'
                f'{" " * 46}"⏳ Premium Decay"]){m.group(4)} PREMIUMPRO')
    block = f'''
    with {new_var}:                                                    # PREMIUMPRO — StockMojo-style Premium Decay
        try:
            import premium_decay_pro
            premium_decay_pro.render(day)
        except Exception as _pd_err:                            # noqa: BLE001
            st.error(f"Premium Decay encountered an error: {{_pd_err}}")'''
    new = t[:m.start()] + tabs_new + t[m.end():]
    new = new.replace(tail, tail + block, 1)
    compile(new, "options_lab_tab.py", "exec")
    if check:
        print(f"✔ options_lab_tab.py: can add the Premium Decay tab (as {new_var})"); return
    shutil.copy2(P, P.with_name(P.name + ".bak_premium"))
    P.write_text(new, encoding="utf-8")
    print("✔ options_lab_tab.py: Premium Decay tab added (backup options_lab_tab.py.bak_premium) — refresh the dashboard")


if __name__ == "__main__":
    main()
