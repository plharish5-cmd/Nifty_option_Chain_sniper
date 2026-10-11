"""
apply_futures_oi_tab.py — adds the "📈 Futures OI · EMA 8" sub-tab (NIFTY + BANKNIFTY futures OI build-up, OI surge,
confirmation) to 🧪 Options Lab, as the LAST tab. Edits YOUR options_lab_tab.py in place (backup first:
options_lab_tab.py.bak_futoi). Needs futures_oi_panel.py in the same folder. Nothing else in Options Lab changes.

  .venv/bin/python apply_futures_oi_tab.py --check     # tells you whether it can patch, changes nothing
  .venv/bin/python apply_futures_oi_tab.py             # patch
Then refresh the dashboard. Undo: copy options_lab_tab.py.bak_futoi back.
"""
from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).parent
P = ROOT / "options_lab_tab.py"
MARK = "# FUTOI"

BLOCK = '''
    with t_futoi:                                               # FUTOI — NIFTY + BANKNIFTY futures OI · EMA 8
        try:
            import futures_oi_panel
            futures_oi_panel.render(day, tf)
        except Exception as _fo_err:                            # noqa: BLE001
            st.error(f"Futures OI encountered an error: {_fo_err}")
'''


def main() -> int:
    check = "--check" in sys.argv
    if not (ROOT / "futures_oi_panel.py").exists():
        print("✘ futures_oi_panel.py not found — copy it into the OC folder first")
        return 1
    if not P.exists():
        print(f"✘ {P.name} not found — run this from the OC folder")
        return 1
    s = P.read_text()
    if MARK in s:
        print("✔ already patched (FUTOI marker found) — nothing to do")
        return 0
    # the tab line: "    t0, t1, …, tN = st.tabs([ … ])   # comments"  (may span several lines)
    m = re.search(r"^(    )((?:t\d+, )*t\d+) = st\.tabs\(\[(.*?)\]\)([^\n]*)$", s, flags=re.S | re.M)
    if not m:
        print("✘ could not find the Options Lab st.tabs([...]) line — send me options_lab_tab.py")
        return 1
    names, body, tail = m.group(2), m.group(3), m.group(4)
    if "def render" not in s[:m.start()]:
        print("✘ the st.tabs line is not inside render() as expected — send me options_lab_tab.py")
        return 1
    new_line = f"    {names}, t_futoi = st.tabs([{body.rstrip()},\n                                              \"📈 Futures OI · EMA 8\"]){tail} {MARK}"
    s2 = s[:m.start()] + new_line + s[m.end():]
    # append the tab body at the end of render(): render() is the last function in the file
    if not re.search(r"^def render\(", s2, flags=re.M) or re.search(r"^def ", s2[s2.index("def render("):][10:], flags=re.M):
        print("✘ render() is not the last function in options_lab_tab.py — send me the file")
        return 1
    s2 = s2.rstrip("\n") + "\n" + BLOCK
    try:
        compile(s2, str(P), "exec")
    except SyntaxError as e:
        print(f"✘ the patched file would not compile ({e}) — nothing changed; send me options_lab_tab.py")
        return 1
    if check:
        print(f"✔ can patch: tabs '{names}' → + t_futoi; tab body appended to render()")
        return 0
    shutil.copy(P, P.with_name(P.name + ".bak_futoi"))
    P.write_text(s2)
    print("✔ patched options_lab_tab.py (backup: options_lab_tab.py.bak_futoi) — refresh the dashboard")
    return 0


if __name__ == "__main__":
    sys.exit(main())
