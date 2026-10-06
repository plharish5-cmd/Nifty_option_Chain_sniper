"""
apply_pece_pro.py — adds the "★ PRO READ" column to the PE − CE OI Table (needs pece_pro.py in the same folder).
Edits YOUR pe_ce_panel.py in place (backup first: pe_ce_panel.py.bak_pecepro). Every other column, the charts and the
Smart OI / PCR pages that reuse pe_ce_panel keep working exactly as before.

  .venv/bin/python apply_pece_pro.py --check
  .venv/bin/python apply_pece_pro.py
Undo: copy pe_ce_panel.py.bak_pecepro back.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).parent
P = ROOT / "pe_ce_panel.py"

EDITS = [
    # 1 · walls + expiry flag per snapshot
    ('''                         ce_vol=num("ce_vol").sum(), pe_vol=num("pe_vol").sum()))''',
     '''                         ce_vol=num("ce_vol").sum(), pe_vol=num("pe_vol").sum(),
                         **__import__("pece_pro").extra(d, ts)))                     # PECEPRO'''),
    # 2 · PRO READ per candle
    ('''    t["read"] = [_label(r) for r in t.itertuples()]''',
     '''    t["read"] = [_label(r) for r in t.itertuples()]
    t = __import__("pece_pro").add(t, s, tf_min)                                     # PECEPRO'''),
    # 3 · header
    ('''"OI", "Vol", "Spot", "Signal", "Meaning"]''',
     '''"OI", "Vol", "Spot", "Signal", "Meaning", "Context · strength"]'''),
    ('''("NIFTY", 1, "#e0f2fe", "#0369a1"), ("READ", 2, NAVY, GOLD)]''',
     '''("NIFTY", 1, "#e0f2fe", "#0369a1"), ("READ", 2, NAVY, GOLD), ("★ PRO READ", 1, GOLD, NAVY)]'''),
    # 4 · cell
    ('''                     f"white-space:normal;min-width:240px;font-size:0.8rem'>{html.escape(MEANING.get(txt, ''))}</td>")''',
     '''                     f"white-space:normal;min-width:240px;font-size:0.8rem'>{html.escape(MEANING.get(txt, ''))}</td>")
        cells.append(__import__("pece_pro").cell(r))                                  # PECEPRO'''),
    # 5 · guide under the table
    ('''        st.markdown(render_table(t, show, int(r["table_height_px"])), unsafe_allow_html=True)''',
     '''        st.markdown(render_table(t, show, int(r["table_height_px"])), unsafe_allow_html=True)
        with st.expander("★ How to read the PRO READ column"):                        # PECEPRO
            st.markdown("<br>".join(__import__("pece_pro").GUIDE), unsafe_allow_html=True)'''),
]


def main():
    check = "--check" in sys.argv
    if not (ROOT / "pece_pro.py").exists():
        print("✘ pece_pro.py not found — copy it into the OC folder first"); return
    if not P.exists():
        print("✘ pe_ce_panel.py not found — run from the OC folder"); return
    t = P.read_text(encoding="utf-8")
    if "PECEPRO" in t:
        print("• pe_ce_panel.py: PRO READ column already added — nothing to do"); return
    bad = [i + 1 for i, (o, _) in enumerate(EDITS) if t.count(o) != 1]
    if bad:
        print(f"✘ pe_ce_panel.py differs from the version I know (edit {bad}) — NOT changed (send me the file)"); return
    for o, n in EDITS:
        t = t.replace(o, n, 1)
    compile(t, "pe_ce_panel.py", "exec")
    if check:
        print("✔ pe_ce_panel.py: can add the PRO READ column"); return
    shutil.copy2(P, P.with_name(P.name + ".bak_pecepro"))
    P.write_text(t, encoding="utf-8")
    print("✔ pe_ce_panel.py: PRO READ column added (backup pe_ce_panel.py.bak_pecepro) — refresh the dashboard")


if __name__ == "__main__":
    main()
