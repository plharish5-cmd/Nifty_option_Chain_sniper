"""
apply_paper_desk.py — pins the 📝 PAPER DESK (paper_desk.py) at the TOP of the dashboard's left sidebar, so it is there
on every tab. Edits YOUR dashboard.py in place (backup first: dashboard.py.bak_paperdesk). Nothing else changes —
the 📒 Paper Trading tab stays and shares the same account, journal and analytics.

  .venv/bin/python apply_paper_desk.py --check
  .venv/bin/python apply_paper_desk.py
Then restart the dashboard (streamlit run dashboard.py). Undo: copy dashboard.py.bak_paperdesk back.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).parent
P = ROOT / "dashboard.py"
ANCHOR = 'st.sidebar.title("Scanner")'
BLOCK = '''try:                                                        # PAPERDESK — one-click paper trading on every tab
    import paper_desk
    paper_desk.render_sidebar()
except Exception as _pdesk_err:                             # noqa: BLE001
    st.sidebar.caption(f"Paper desk error: {_pdesk_err}")
'''


def main():
    check = "--check" in sys.argv
    if not (ROOT / "paper_desk.py").exists():
        print("✘ paper_desk.py not found — copy it into the OC folder first"); return
    if not P.exists():
        print("✘ dashboard.py not found — run from the OC folder"); return
    t = P.read_text(encoding="utf-8")
    if "PAPERDESK" in t:
        print("• dashboard.py: Paper Desk already added — nothing to do"); return
    if t.count(ANCHOR) != 1:
        print(f"✘ dashboard.py: could not find  {ANCHOR}  exactly once — NOT changed (send me the file)"); return
    i = t.index(ANCHOR)
    line_start = t.rfind("\n", 0, i) + 1
    if t[line_start:i].strip():
        print("✘ dashboard.py: the sidebar title is indented differently than expected — NOT changed"); return
    new = t[:line_start] + BLOCK + t[line_start:]
    compile(new, "dashboard.py", "exec")
    try:
        import streamlit as st
        v = tuple(int(x) for x in st.__version__.split(".")[:2])
        if v < (1, 37):
            print(f"  ⚠ Streamlit {st.__version__}: the desk works but refreshes only with the page. "
                  "For live 3-second refresh:  .venv/bin/pip install -U 'streamlit>=1.37'")
    except Exception:                                       # noqa: BLE001
        pass
    if check:
        print("✔ dashboard.py: can add the Paper Desk"); return
    shutil.copy2(P, P.with_name(P.name + ".bak_paperdesk"))
    P.write_text(new, encoding="utf-8")
    print("✔ dashboard.py: Paper Desk added at the top of the sidebar (backup dashboard.py.bak_paperdesk) — restart the dashboard")


if __name__ == "__main__":
    main()
