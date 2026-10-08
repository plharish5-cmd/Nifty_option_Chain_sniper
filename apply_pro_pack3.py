"""
apply_pro_pack3.py — adds two sub-tabs to the END of 🧪 Options Lab:
    🌏 Pre-market & Gap (global_cues.py) · 📅 Events (events_cal.py)
Edits YOUR options_lab_tab.py in place (backup first: options_lab_tab.py.bak_pack3). Every existing tab is untouched.
Needs apply_pro_pack2.py already run. Safe to run twice.

  .venv/bin/python apply_pro_pack3.py --check
  .venv/bin/python apply_pro_pack3.py
Undo: copy options_lab_tab.py.bak_pack3 back.
"""
from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).parent
P = ROOT / "options_lab_tab.py"
TABS_RE = re.compile(r"^(    )(t0(?:, t\d+)+) = st\.tabs\(\[(.*?)\]\)(\s*# TABMOVE[^\n]*)$", re.S | re.M)
FIRST_TAIL = '''            st.error(f"NIFTY Heavyweights encountered an error: {_ihp_err}")'''
TABS = [  # label, module, marker, error variable, error name
    ("🌏 Pre-market & Gap", "global_cues", "GLOBALCUES", "_gc_err", "Pre-market & Gap"),
    ("📅 Events", "events_cal", "EVENTSCAL", "_ev_err", "Events"),
]


def add(t: str, tail: str, label: str, mod: str, mark: str, ev: str, nm: str) -> tuple[str | None, str]:
    m = list(TABS_RE.finditer(t))
    if len(m) != 1 or t.count(tail) != 1:
        return None, tail
    m = m[0]
    var = f"t{len(m.group(2).split(', '))}"
    tabs_new = (f'{m.group(1)}{m.group(2)}, {var} = st.tabs([{m.group(3)},\n'
                f'{" " * 46}"{label}"]){m.group(4)} {mark}')
    new_tail = f'''            st.error(f"{nm} encountered an error: {{{ev}}}")'''
    block = f'''
    with {var}:                                                    # {mark}
        try:
            import {mod}
            {mod}.render(day)
        except Exception as {ev}:                           # noqa: BLE001
{new_tail}'''
    t = t[:m.start()] + tabs_new + t[m.end():]
    return t.replace(tail, tail + block, 1), new_tail


def main():
    check = "--check" in sys.argv
    for _, mod, *_ in TABS:
        if not (ROOT / f"{mod}.py").exists():
            print(f"✘ {mod}.py not found — copy it into the OC folder first"); return
    if not P.exists():
        print("✘ options_lab_tab.py not found — run from the OC folder"); return
    t = P.read_text(encoding="utf-8")
    if "HEAVYPRO" not in t:
        print("✘ run  apply_pro_pack2.py  first (then this one) — NOT changed"); return
    tail, done = FIRST_TAIL, []
    for label, mod, mark, ev, nm in TABS:
        if mark in t:
            tail = f'''            st.error(f"{nm} encountered an error: {{{ev}}}")'''
            continue
        new, tail2 = add(t, tail, label, mod, mark, ev, nm)
        if new is None:
            print("✘ options_lab_tab.py differs from the version I know — NOT changed (send me the file)"); return
        t, tail = new, tail2
        done.append(label)
    if not done:
        print("• options_lab_tab.py: both tabs already added — nothing to do"); return
    compile(t, "options_lab_tab.py", "exec")
    if check:
        print("✔ options_lab_tab.py: can add " + ", ".join(done)); return
    shutil.copy2(P, P.with_name(P.name + ".bak_pack3"))
    P.write_text(t, encoding="utf-8")
    print("✔ options_lab_tab.py: added " + ", ".join(done) + " (backup options_lab_tab.py.bak_pack3) — refresh the dashboard")


if __name__ == "__main__":
    main()
