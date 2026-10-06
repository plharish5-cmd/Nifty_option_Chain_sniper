"""
apply_vega_tab.py — adds a "📉 Vega Analysis" sub-tab (vega_pro.py) as the LAST tab of 🧪 Options Lab.
Works whether or not you already added the Multi-strike OI tab. Edits YOUR options_lab_tab.py in place
(backup first: options_lab_tab.py.bak_vega). Every existing tab stays exactly as it is
(the small vega chart inside "Gamma & Vega" is kept too).

  .venv/bin/python apply_vega_tab.py --check
  .venv/bin/python apply_vega_tab.py
Then refresh the dashboard. Undo: copy options_lab_tab.py.bak_vega back.
"""
from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).parent
P = ROOT / "options_lab_tab.py"

# the tabs line(s) written by apply_tab_move.py / apply_multistrike_tab.py:  "    t0, ..., tN = st.tabs([ ... ])   # TABMOVE ..."
TABS_RE = re.compile(r"^(    )(t0(?:, t\d+)+) = st\.tabs\(\[(.*?)\]\)(\s*# TABMOVE[^\n]*)$", re.S | re.M)

TAILS = [  # the last tab's block, newest layout first
    '''            import multistrike_panel
            multistrike_panel.render(day)
        except Exception as _ms_err:                            # noqa: BLE001
            st.error(f"Multi-strike OI encountered an error: {_ms_err}")''',
    '''            import pe_ce_panel
            pe_ce_panel.render()
        except Exception as _pece_err:                          # noqa: BLE001
            st.error(f"PE − CE OI Table encountered an error: {_pece_err}")''',
]


def main():
    check = "--check" in sys.argv
    if not (ROOT / "vega_pro.py").exists():
        print("✘ vega_pro.py not found — copy it into the OC folder first"); return
    if not P.exists():
        print("✘ options_lab_tab.py not found — run from the OC folder"); return
    t = P.read_text(encoding="utf-8")
    if "VEGAPRO" in t:
        print("• options_lab_tab.py: Vega Analysis tab already added — nothing to do"); return
    if (ROOT / "multistrike_panel.py").exists() and (ROOT / "apply_multistrike_tab.py").exists() and "MULTISTRIKE" not in t:
        print("✘ run  apply_multistrike_tab.py  first (then this one) — NOT changed"); return
    m = list(TABS_RE.finditer(t))
    tail = next((x for x in TAILS if t.count(x) == 1), None)
    if len(m) != 1 or tail is None:
        print("✘ options_lab_tab.py differs from the version I know — NOT changed (send me the file)"); return
    m = m[0]
    names = m.group(2).split(", ")
    new_var = f"t{len(names)}"
    tabs_new = (f'{m.group(1)}{m.group(2)}, {new_var} = st.tabs([{m.group(3)},\n'
                f'{" " * 46}"📉 Vega Analysis"]){m.group(4)} VEGAPRO')
    block = f'''
    with {new_var}:                                                    # VEGAPRO — StockMojo-style Vega Analysis
        try:
            import vega_pro
            vega_pro.render(day)
        except Exception as _vg_err:                            # noqa: BLE001
            st.error(f"Vega Analysis encountered an error: {{_vg_err}}")'''
    new = t[:m.start()] + tabs_new + t[m.end():]
    new = new.replace(tail, tail + block, 1)
    compile(new, "options_lab_tab.py", "exec")
    if check:
        print(f"✔ options_lab_tab.py: can add the Vega Analysis tab (as {new_var})"); return
    shutil.copy2(P, P.with_name(P.name + ".bak_vega"))
    P.write_text(new, encoding="utf-8")
    print("✔ options_lab_tab.py: Vega Analysis tab added (backup options_lab_tab.py.bak_vega) — refresh the dashboard")


if __name__ == "__main__":
    main()
