"""
apply_alignment_fix.py — makes the "🧠 Participant Alignment Signal" panel (participant_sheet_tab.py) follow the
TRAINER'S workbook exactly, and shows what his own 7-year history says about each reading.

What was wrong in the old panel
  1. Thresholds 1.2 / 0.85 — not his. His conditional formatting: AM / AI / AB > 1 green; FII AM < 0.55 green and
     > 1.3 red (rows 1334+). The panel now uses > 1 / < 1 and flags his two FII extremes separately.
  2. Wording: "FII AM > 1 = FII net buying calls" and "ratio < 1 = all buying puts" — wrong. AM = Put SHORT ÷ Call
     SHORT (=R/P): > 1 means more PUTS WRITTEN (writers think it won't fall), < 1 more CALLS WRITTEN.
  3. "All three aligned" only checked FII and Retail — PRO was ignored. Now all three are checked.
  4. "Retail = contrarian" and "FII > 1 & Retail < 1 → buy CE" were stated as facts. His own data (Dec-2019 →
     Oct-2026) says: Retail > 1 days were MORE bullish, not less; the FII>1 & Retail<1 split was a coin-flip next day
     (≈50%) and only a mild 5-day lean (≈60%, few days). The panel now shows these numbers live, recomputed every
     time the workbook updates, instead of fixed claims.
Edits YOUR participant_sheet_tab.py in place (backup participant_sheet_tab.py.bak_align). Safe to run twice.

  .venv/bin/python apply_alignment_fix.py --check
  .venv/bin/python apply_alignment_fix.py
"""
from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).parent
P = ROOT / "participant_sheet_tab.py"
MARK = "ALIGNFIX"
BLOCK_RE = re.compile(r"# ─+ Participant Alignment Signal ─+\n.*?(?=\ndef render\(\) -> None:)", re.S)
CALL_OLD = "    _render_alignment(last, df.iloc[-2])"
CALL_NEW = "    _render_alignment(last, df.iloc[-2], df)                 # ALIGNFIX"

NEW_BLOCK = r'''# ─────────────────────────────── Participant Alignment Signal ──────────────── ALIGNFIX
# Ratios exactly as the trainer's workbook: FII!AM, Retailers!AI, PRO!AB = Put SHORT ÷ Call SHORT (R / P).
#   > 1  more puts written than calls → writers expect NO fall → bullish lean   (his green)
#   < 1  more calls written than puts → writers expect NO rise → bearish lean
#   FII AM < 0.55 / > 1.3 → his two EXTREME colours (green / red) — shown as "extreme", with their record.

import html as _h


def _ratio_direction(val: float, fii: bool = False) -> tuple[str, str]:
    """(label, colour) using HIS thresholds."""
    if fii and val > 1.3:
        return "▲ BULLISH · EXTREME (his red)", "#d97706"
    if fii and val < 0.55:
        return "▼ BEARISH · EXTREME (his green)", "#d97706"
    if val > 1.0:
        return "▲ BULLISH (> 1)", "#1a7a1a"
    if val < 1.0:
        return "▼ BEARISH (< 1)", "#b30000"
    return "↔ NEUTRAL (= 1)", "#7f6000"


def _edge(df: "pd.DataFrame", mask: "pd.Series", years: float | None = None) -> tuple[int, float, float]:
    """(days, next-day up %, next-5-day up %) for a reading, from the workbook's own NIFTY column."""
    n = df["nifty"]
    r1, r5 = n.shift(-1) / n - 1, n.shift(-5) / n - 1
    m = mask & r5.notna()
    if years:
        m &= df.index >= df.index.max() - pd.Timedelta(days=int(365 * years))
    if m.sum() == 0:
        return 0, float("nan"), float("nan")
    return int(m.sum()), 100 * float((r1[m] > 0).mean()), 100 * float((r5[m] > 0).mean())


def _render_alignment(last: "pd.Series", prev: "pd.Series", df: "pd.DataFrame | None" = None) -> None:
    st.markdown("### 🧠 Participant Alignment Signal")
    st.caption("Put SHORT ÷ Call SHORT for FII (AM), Retail (AI) and PRO (AB) — exactly the trainer's columns and "
               "colours. Below each reading: what NIFTY did next in HIS sheet's own history.")
    F, R, P = float(last.fii_AM), float(last.retail_AI), float(last.pro_AB)
    cards = ((F, "FII (AM)", True, "Big money — the lead signal"),
             (R, "Retail (AI)", False, "Clients"),
             (P, "PRO (AB)", False, "Prop desks — confirms FII when aligned"))
    cols = st.columns(3)
    for col, (val, name, is_fii, note) in zip(cols, cards):
        lab, colour = _ratio_direction(val, is_fii)
        col.markdown(
            f"<div style='border:1px solid {colour};border-radius:8px;padding:10px;text-align:center;background:#0b1a33'>"
            f"<div style='color:#aaa;font-size:0.8em'>{name}</div>"
            f"<div style='font-size:1.6em;font-weight:700;color:{colour}'>{val:.2f}</div>"
            f"<div style='color:{colour};font-weight:600'>{_h.escape(lab)}</div>"
            f"<div style='color:#777;font-size:0.75em;margin-top:4px'>{note}</div></div>", unsafe_allow_html=True)

    fb, rb, pb = F > 1, R > 1, P > 1
    if fb and rb and pb:
        state, colour = "ALL THREE > 1 — every group writing more puts than calls (bullish alignment)", "#1a7a1a"
    elif not fb and not rb and not pb:
        state, colour = "ALL THREE < 1 — every group writing more calls than puts (bearish alignment)", "#b30000"
    elif fb and not rb:
        state, colour = "DIVERGENCE: FII > 1 (puts written) · Retail < 1 (calls written)", "#c8a000"
    elif not fb and rb:
        state, colour = "DIVERGENCE: FII < 1 (calls written) · Retail > 1 (puts written)", "#c8a000"
    else:
        state, colour = "MIXED — FII and Retail agree, PRO does not", "#7f6000"

    lines = []
    if df is not None and "nifty" in df:
        Fs, Rs, Ps = df.fii_AM, df.retail_AI, df.pro_AB
        today = {
            "ALL THREE > 1": (Fs > 1) & (Rs > 1) & (Ps > 1), "ALL THREE < 1": (Fs < 1) & (Rs < 1) & (Ps < 1),
            "DIVERGENCE: FII > 1": (Fs > 1) & (Rs < 1), "DIVERGENCE: FII < 1": (Fs < 1) & (Rs > 1),
            "MIXED": ((Fs > 1) == (Rs > 1)) & ((Fs > 1) != (Ps > 1)),
        }
        key = next(k for k in today if state.startswith(k))
        base_all, base_2y = _edge(df, Fs.notna()), _edge(df, Fs.notna(), 2)
        for lab, m in (("This alignment", today[key]),
                       ("FII > 1" if fb else "FII < 1", (Fs > 1) if fb else (Fs < 1))):
            a, b = _edge(df, m), _edge(df, m, 2)
            lines.append(f"<b>{_h.escape(lab)}</b>: since 2019 {a[0]} days → next day up <b>{a[1]:.0f}%</b>, 5 days up <b>{a[2]:.0f}%</b>"
                         f" · last 2 years {b[0]} days → {b[1]:.0f}% / {b[2]:.0f}%")
        if F < 0.55 or F > 1.3:
            m = (Fs < 0.55) if F < 0.55 else (Fs > 1.3)
            a, b = _edge(df, m), _edge(df, m, 2)
            lines.append(f"<b>FII extreme ({'&lt; 0.55' if F < 0.55 else '&gt; 1.3'})</b>: since 2019 {a[0]} days → "
                         f"{a[1]:.0f}% / {a[2]:.0f}% · last 2 years {b[0]} days → {b[1]:.0f}% / {b[2]:.0f}%")
        lines.append(f"<span style='color:#94a3b8'>Baseline, any day: since 2019 {base_all[1]:.0f}% / {base_all[2]:.0f}% · "
                     f"last 2 years {base_2y[1]:.0f}% / {base_2y[2]:.0f}%. A reading only matters if it beats this.</span>")
        below = (Fs < 1)[::-1]
        streak = int(below.cumprod().sum()) if bool(below.iloc[0]) else 0
        if streak >= 3:
            lines.append(f"FII AM has been below 1 for <b>{streak} sessions</b> in a row.")
    st.markdown(f"<div style='background:#0b1a33;border:1px solid {colour};border-radius:8px;padding:10px 14px;margin-top:8px;color:#f1f5f9'>"
                f"<b style='color:{colour}'>{_h.escape(state)}</b><br>" + "<br>".join(lines) + "</div>", unsafe_allow_html=True)

    with st.expander("📖 How to read it — trainer's rules + what his data shows"):
        st.markdown("""
**Ratio = Put Short ÷ Call Short** (FII!AM, Retailers!AI, PRO!AB — formula `=R/P` in his sheet)
- **> 1** → more puts written than calls → writers expect NIFTY **not to fall** → bullish lean (his green)
- **< 1** → more calls written than puts → writers expect NIFTY **not to rise** → bearish lean
- **FII AM < 0.55** and **> 1.3** are his two *extreme* colours. Treat them as "positioning stretched — watch", not as a
  trade by themselves; their record is shown live above.

**What his own sheet shows (Dec-2019 → Oct-2026, next-day / 5-day NIFTY up %; any day ≈ 54 / 55):**
- **All three > 1** ≈ 63 / 60 — the strongest bullish reading. **All three < 1** ≈ 48 / 51 (last 2 years ≈ 44 / 42 — bearish).
- **FII > 1** ≈ 59 / 59 vs **FII < 1** ≈ 49 / 52 — FII direction helps, mildly.
- **Retail > 1** ≈ 58 / 58 — Retail moved WITH the market in this data, not against it.
- **FII > 1 & Retail < 1** ≈ 50 / 61 — no next-day edge; at most a few-day long lean (few days).
- **FII < 0.55** ≈ 50 / 40 — his green extreme did NOT mark bottoms; the following week leaned down.

Use it as the **day's bias**, then take entries only with your price-action setup. Not financial advice.
""")


'''


def main():
    check = "--check" in sys.argv
    if not P.exists():
        print("✘ participant_sheet_tab.py not found — run from the OC folder"); return
    t = P.read_text(encoding="utf-8")
    if MARK in t:
        print("• participant_sheet_tab.py: alignment panel already fixed"); return
    m = BLOCK_RE.search(t)
    if not m or t.count(CALL_OLD) != 1:
        print("✘ participant_sheet_tab.py differs from the version I know — NOT changed (send me the file)"); return
    new = t[:m.start()] + NEW_BLOCK.rstrip("\n") + "\n\n" + t[m.end():]
    new = new.replace(CALL_OLD, CALL_NEW, 1)
    compile(new, "participant_sheet_tab.py", "exec")
    if check:
        print("✔ participant_sheet_tab.py: can be fixed"); return
    shutil.copy2(P, P.with_name(P.name + ".bak_align"))
    P.write_text(new, encoding="utf-8")
    print("✔ participant_sheet_tab.py: alignment panel now follows the trainer's sheet (backup .bak_align) — refresh")


if __name__ == "__main__":
    main()
