"""
apply_olab_pro.py — adds the ★ PRO layer to 🧪 Options Lab (needs olab_pro.py; the new pcr_oi_pro.py replaces the old one).

  • ★ PRO DECISION BRIEF above the sub-tabs (walls · who is writing · expected move · day type · gamma · IV · CE/PE filter)
  • ★ PRO panel at the top of: Open Interest · Gamma & Vega · IV − HV · Call vs Put Writers · Multi-strike OI · Premium Decay
  • Open Interest chart: ONE clean tooltip per strike (was 3 per side) with call + put OI, change, strike PCR and a read
Edits YOUR options_lab_tab.py and oi_profile_panel.py in place (backups *.bak_olabpro). Every existing chart stays.

  .venv/bin/python apply_olab_pro.py --check
  .venv/bin/python apply_olab_pro.py
Undo: copy the .bak_olabpro files back.
"""
from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).parent
TABS_RE = re.compile(r"^(    )(t0(?:, t\d+)+) = st\.tabs\(\[(.*?)\]\)", re.S | re.M)
WANT = [("Open Interest", "oi"), ("Gamma", "gex"), ("IV − HV", "iv"), ("Call vs Put Writers", "writers"),
        ("Multi-strike", "roll"), ("Premium Decay", "decay")]

HOVER_OLD = '''    side(p["strike"] + off, p["pe0"], p["pe1"], RED, "Put")'''
HOVER_NEW = '''    side(p["strike"] + off, p["pe0"], p["pe1"], RED, "Put")
    # OLABPRO — one tooltip per strike (invisible full-height bar) instead of 3 per side
    def _rd(dc, dp):
        if dp > 0 and dc <= 0:
            return "put writing + call covering → support / bullish"
        if dc > 0 and dp <= 0:
            return "call writing + put covering → resistance / bearish"
        if dc > 0 and dp > 0:
            return "both sides writing → range being set here" if min(dc, dp) > 0.5 * max(dc, dp) else (
                "more call writing → cap" if dc > dp else "more put writing → floor")
        if dc < 0 and dp < 0:
            return "both sides unwinding → this strike losing importance"
        return "little change"
    top = np.maximum(np.maximum(p["ce0"], p["ce1"]), np.maximum(p["pe0"], p["pe1"]))
    pcr_k = [f"{pe / ce:.2f}" if ce else "—" for ce, pe in zip(p["ce1"], p["pe1"])]
    cdh = np.c_[p["strike"], [fmt(v) for v in p["ce1"]], [fmt(v) for v in p["ce1"] - p["ce0"]],
                [fmt(v) for v in p["pe1"]], [fmt(v) for v in p["pe1"] - p["pe0"]], pcr_k,
                [_rd(c, q) for c, q in zip(p["ce1"] - p["ce0"], p["pe1"] - p["pe0"])]]
    fig.add_trace(go.Bar(x=p["strike"], y=top, width=step * 0.95, marker_color="rgba(0,0,0,0)", showlegend=False,
                         name="", customdata=cdh,
                         hovertemplate=("<b>%{customdata[0]:,.0f}</b><br><span style='color:#22c55e'>CALL</span> OI "
                                        "%{customdata[1]} · chg %{customdata[2]}<br><span style='color:#ef4444'>PUT</span> OI "
                                        "%{customdata[3]} · chg %{customdata[4]}<br>strike PCR %{customdata[5]}<br>"
                                        "<i>%{customdata[6]}</i><extra></extra>")))'''


def patch_olab(t: str):
    m = list(TABS_RE.finditer(t))
    if len(m) != 1:
        return None
    m = m[0]
    labels = re.findall(r'"([^"]+)"', m.group(3))
    names = m.group(2).split(", ")
    brief = ('    try:                                                    # OLABPRO — ★ PRO DECISION BRIEF\n'
             '        __import__("olab_pro").brief(day)\n'
             '    except Exception as _opb_err:                           # noqa: BLE001\n'
             '        st.caption(f"PRO brief unavailable: {_opb_err}")\n')
    line_start = t.rfind("\n", 0, m.start()) + 1
    t = t[:line_start] + brief + t[line_start:]
    done = []
    for sub, key in WANT:
        idx = next((i for i, l in enumerate(labels) if sub in l), None)
        if idx is None or idx >= len(names):
            continue
        var = names[idx]
        mm = re.search(rf"^    with {var}:[^\n]*\n", t, re.M)
        if not mm:
            continue
        ins = f'        __import__("olab_pro").tab("{key}", day)                       # OLABPRO\n'
        t = t[:mm.end()] + ins + t[mm.end():]
        done.append(f"{labels[idx]}→{key}")
    print("   panels:", ", ".join(done))
    return t


def patch_hover(t: str):
    if t.count(HOVER_OLD) != 1 or t.count("customdata=cd, hovertemplate=ht))") != 3:
        return None
    return t.replace("customdata=cd, hovertemplate=ht))", 'hoverinfo="skip"))').replace(HOVER_OLD, HOVER_NEW, 1)


def patch(name, fn, check):
    p = ROOT / name
    if not p.exists():
        print(f"✘ {name} not found — run from the OC folder"); return False
    t = p.read_text(encoding="utf-8")
    if "OLABPRO" in t:
        print(f"• {name}: already done"); return True
    new = fn(t)
    if new is None:
        print(f"✘ {name}: differs from the version I know — NOT changed (send me the file)"); return False
    compile(new, name, "exec")
    if check:
        print(f"✔ {name}: can be patched"); return True
    shutil.copy2(p, p.with_name(p.name + ".bak_olabpro"))
    p.write_text(new, encoding="utf-8")
    print(f"✔ {name}: patched (backup {name}.bak_olabpro)")
    return True


def main():
    check = "--check" in sys.argv
    for f in ("olab_pro.py", "pcr_oi_pro.py"):
        if not (ROOT / f).exists():
            print(f"✘ {f} not found — copy it into the OC folder first"); return
    ok = patch("options_lab_tab.py", patch_olab, check)
    ok = patch("oi_profile_panel.py", patch_hover, check) and ok
    if ok and not check:
        print("Done — refresh the dashboard.")


if __name__ == "__main__":
    main()
