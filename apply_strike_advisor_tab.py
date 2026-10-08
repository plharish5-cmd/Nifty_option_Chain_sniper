"""
apply_strike_advisor_tab.py — adds a "🧭 Strike Advisor" sub-tab (strike_advisor_pro.py) as the LAST tab of 🧪 Options Lab.
Edits YOUR options_lab_tab.py in place (backup first: options_lab_tab.py.bak_strikeadv). Every existing tab is untouched.
Needs the ⏱ Time-series Chain tab already added (apply_ts_chain_tab.py) — run that first.

  .venv/bin/python apply_strike_advisor_tab.py --check
  .venv/bin/python apply_strike_advisor_tab.py
Undo: copy options_lab_tab.py.bak_strikeadv back.
"""
from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).parent
P = ROOT / "options_lab_tab.py"
TABS_RE = re.compile(r"^(    )(t0(?:, t\d+)+) = st\.tabs\(\[(.*?)\]\)(\s*# TABMOVE[^\n]*)$", re.S | re.M)
TAILS = ['''            st.error(f"Time-series Chain encountered an error: {_tsc_err}")''']
BEFORE = [("ts_chain_pro.py", "apply_ts_chain_tab.py", "TSCHAINPRO")]


def main():
    check = "--check" in sys.argv
    if not (ROOT / "strike_advisor_pro.py").exists():
        print("✘ strike_advisor_pro.py not found — copy it into the OC folder first"); return
    if not P.exists():
        print("✘ options_lab_tab.py not found — run from the OC folder"); return
    t = P.read_text(encoding="utf-8")
    if "STRIKEADVPRO" in t:
        print("• options_lab_tab.py: Strike Advisor tab already added — nothing to do"); return
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
                f'{" " * 46}"🧭 Strike Advisor"]){m.group(4)} STRIKEADVPRO')
    block = f'''
    with {new_var}:                                                    # STRIKEADVPRO — move map + best CE / PE strike
        try:
            import strike_advisor_pro
            strike_advisor_pro.render(day)
        except Exception as _sad_err:                           # noqa: BLE001
            st.error(f"Strike Advisor encountered an error: {{_sad_err}}")'''
    new = t[:m.start()] + tabs_new + t[m.end():]
    new = new.replace(tail, tail + block, 1)
    compile(new, "options_lab_tab.py", "exec")
    if check:
        print(f"✔ options_lab_tab.py: can add the Strike Advisor tab (as {new_var})"); return
    shutil.copy2(P, P.with_name(P.name + ".bak_strikeadv"))
    P.write_text(new, encoding="utf-8")
    print("✔ options_lab_tab.py: Strike Advisor tab added (backup options_lab_tab.py.bak_strikeadv) — refresh the dashboard")


if __name__ == "__main__":
    main()
