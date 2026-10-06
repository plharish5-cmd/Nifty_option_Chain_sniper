"""
apply_gap_fix.py — S1–S4 / R1–R4 / Upper & Lower gaps now use EXACTLY your master-sheet rules,
recomputed from the latest daily OHLC (including today's appended candle).

Your rules (from your generate_nifty_master_sheet script):
  R1 = today's high; R2..R4 = walking back, the next prior high STRICTLY higher than the last one.
  S1 = today's low;  S2..S4 = walking back, the next prior low STRICTLY lower than the last one.
  Gaps: |open − previous close| > 5 pts creates a gap at the previous close.
        Each day: FIRST remove gaps that day's range filled (gap-up: low ≤ price, gap-down: high ≥ price),
        THEN add the gap that day opened with. Upper gaps = open gaps above the close (nearest first),
        lower gaps = below the close (nearest first), 3 each.

What was wrong in the app:
  • a 15-pt minimum gap (yours is 5) → e.g. Lower_Gap_2 22,421.95 on 06-Oct-2026 was missing
  • a 20-pt minimum spacing between S/R levels (yours: just strictly higher / lower)
  • today's gap could be "filled" by the same day's range (yours checks fills before adding)
  • the dashboard cached the daily candles for 15 min → the 15:40 appended candle could show late

Patches (backups *.bak_gapfix; compile-checked; safe to run twice):
  build_master.py   dated_levels() and build_rows()   → SL No 8 & 9, master_levels.csv, scheduler, update_daily
  levels.py         unfilled_gaps()                   → gap levels used elsewhere in the scanner
  dashboard.py      daily-OHLC cache refreshes the moment a CSV in data/ohlc changes

  .venv/bin/python apply_gap_fix.py --check
  .venv/bin/python apply_gap_fix.py
  .venv/bin/python apply_gap_fix.py --verify "NIFTY 50_Historical_PR_07102025to06102026.csv"
Then rerun build_master.py once and restart scheduler.py + dashboard.
"""
from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).parent
MARK = "GAPFIX"
GAP_PTS = 5.0

# ── build_master.dated_levels ──────────────────────────────────────────────
DL_RE = re.compile(
    r"(    # ── resistances: today's high.*?\n)(.*?)(\n    c = float\(closes\[i\]\)\n)", re.S)
DL_NEW = '''    # ── GAPFIX — your master-sheet rules ─────────────────────────────────────
    # R: today's high, then walking back each prior high STRICTLY higher than the last
    res = [(dstr(i), float(highs[i]))]
    for j in range(i - 1, -1, -1):
        if highs[j] > res[-1][1]:
            res.append((dstr(j), float(highs[j])))
        if len(res) == n_levels:
            break
    # S: today's low, then walking back each prior low STRICTLY lower than the last
    sup = [(dstr(i), float(lows[i]))]
    for j in range(i - 1, -1, -1):
        if lows[j] < sup[-1][1]:
            sup.append((dstr(j), float(lows[j])))
        if len(sup) == n_levels:
            break
    # Gaps: each day FIRST drop the gaps its range filled, THEN add the gap it opened with (> 5 pts)
    open_gaps: list[dict] = []
    for k in range(0, i + 1):
        h, l = highs[k], lows[k]
        open_gaps = [g for g in open_gaps
                     if not ((g["type"] == "GAP_UP" and l <= g["price"])
                             or (g["type"] == "GAP_DOWN" and h >= g["price"]))]
        if k > 0:
            pc = closes[k - 1]
            if opens[k] - pc > GAPFIX_PTS:
                open_gaps.append({"type": "GAP_UP", "price": round(float(pc), 2), "date": dstr(k)})
            elif opens[k] - pc < -GAPFIX_PTS:
                open_gaps.append({"type": "GAP_DOWN", "price": round(float(pc), 2), "date": dstr(k)})
'''

# ── build_master.build_rows ────────────────────────────────────────────────
BR_RE = re.compile(
    r"(        # ── R1-R4: today's high.*?\n)(.*?)(\n        up = sorted\(\(g for g in open_gaps if g\[\"price\"\] > C\),)", re.S)
BR_NEW = '''        # ── GAPFIX — your master-sheet rules ─────────────────────────────────
        R = [np.nan] * 4
        RD = [""] * 4
        seq_r = [(H, i)]
        for j in range(i - 1, -1, -1):
            if highs[j] > seq_r[-1][0]:
                seq_r.append((highs[j], j))
            if len(seq_r) == 4:
                break
        for k, (v, j_idx) in enumerate(seq_r):
            R[k] = round(v, 2)
            RD[k] = dstr(j_idx)
        S = [np.nan] * 4
        SD = [""] * 4
        seq_s = [(L, i)]
        for j in range(i - 1, -1, -1):
            if lows[j] < seq_s[-1][0]:
                seq_s.append((lows[j], j))
            if len(seq_s) == 4:
                break
        for k, (v, j_idx) in enumerate(seq_s):
            S[k] = round(v, 2)
            SD[k] = dstr(j_idx)
        # gaps: FIRST drop the ones today's range filled, THEN add today's (> 5 pts)
        open_gaps = [g for g in open_gaps
                     if not ((g["type"] == "GAP_UP" and L <= g["price"])
                             or (g["type"] in ("GAP_DOWN", "DOWN") and H >= g["price"]))]
        if i:
            pc = closes[i - 1]
            if O - pc > GAPFIX_PTS:
                open_gaps.append({"type": "GAP_UP", "price": round(pc, 2), "date": dstr(i)})
            elif O - pc < -GAPFIX_PTS:
                open_gaps.append({"type": "GAP_DOWN", "price": round(pc, 2), "date": dstr(i)})
'''
CONST_ANCHOR = "def gann_sq9("
CONST = f"GAPFIX_PTS = {GAP_PTS}   # GAPFIX — your rule: a gap is |open − previous close| > 5 pts\n\n\n"

# ── levels.unfilled_gaps ───────────────────────────────────────────────────
LV_OLD_SIG = 'min_points: float = getattr(config, "GAP_MIN", 15.0), n: int = 3)'
LV_NEW_SIG = f'min_points: float = {GAP_PTS}, n: int = 3)'
LV_RE = re.compile(r"(        gap_pts = round\(o - prev_c, 2\)\n)(.*?)(\n    out: list\[PriceLevel\] = \[\])", re.S)
LV_NEW = '''        # GAPFIX — FIRST drop gaps today's range filled, THEN add today's gap
        open_gaps = [g for g in open_gaps
                     if not ((g["type"] == "up" and lo <= g["price"])
                             or (g["type"] == "down" and h >= g["price"]))]
        if gap_pts > min_points:
            open_gaps.append({"type": "up", "price": prev_c,
                              "date": df.loc[i, "Date"], "size": gap_pts})
        elif gap_pts < -min_points:
            open_gaps.append({"type": "down", "price": prev_c,
                              "date": df.loc[i, "Date"], "size": -gap_pts})
'''

# ── dashboard daily cache ──────────────────────────────────────────────────
DB_OLD = '''@st.cache_data(ttl=900, show_spinner=False)
def load_ohlc_cached():
    """Daily history changes once a day; no need to re-read it every poll."""
    return lv_mod.load_ohlc()'''
DB_NEW = '''@st.cache_data(ttl=900, show_spinner=False)
def _load_ohlc_cached(_stamp):
    return lv_mod.load_ohlc()


def load_ohlc_cached():
    """GAPFIX — re-read the moment any CSV in data/ohlc changes (e.g. today's 15:40 appended candle)."""
    try:
        stamp = tuple(sorted((p.name, p.stat().st_mtime, p.stat().st_size)
                             for p in __import__("pathlib").Path(config.OHLC_DIR).glob("*.csv")))
    except Exception:                                       # noqa: BLE001
        stamp = None
    return _load_ohlc_cached(stamp)'''


def fix_bm(t):
    m1, m2 = DL_RE.search(t), BR_RE.search(t)
    if not m1 or not m2 or CONST_ANCHOR not in t:
        return None
    t = t[:m1.start(2)] + DL_NEW + t[m1.end(2):]
    m2 = BR_RE.search(t)
    t = t[:m2.start(1)] + BR_NEW + t[m2.end(2):]
    i = t.index(CONST_ANCHOR)
    return t[:i] + CONST + t[i:]


def fix_lv(t):
    m = LV_RE.search(t)
    if LV_OLD_SIG not in t or not m:
        return None
    t = t.replace(LV_OLD_SIG, LV_NEW_SIG, 1)
    m = LV_RE.search(t)
    return t[:m.start(2)] + LV_NEW + t[m.end(2):]


def fix_db(t):
    if t.count(DB_OLD) != 1:
        return None
    return t.replace(DB_OLD, DB_NEW, 1)


def patch(name, fn, check, optional=False):
    p = ROOT / name
    if not p.exists():
        print(f"{'•' if optional else '✘'} {name} not found{' — skipped' if optional else ' — run from the OC folder'}")
        return optional
    t = p.read_text(encoding="utf-8")
    if MARK in t:
        print(f"• {name}: already done"); return True
    new = fn(t)
    if new is None:
        print(f"✘ {name}: differs from the version I know — NOT changed (send me the file)"); return False
    compile(new, name, "exec")
    if check:
        print(f"✔ {name}: can be fixed"); return True
    shutil.copy2(p, p.with_name(p.name + ".bak_gapfix"))
    p.write_text(new, encoding="utf-8")
    print(f"✔ {name}: fixed (backup {name}.bak_gapfix)")
    return True


def verify(csv: str):
    import pandas as pd
    sys.path.insert(0, str(ROOT))
    import build_master as bm
    df = pd.read_csv(csv)
    df.columns = [c.strip() for c in df.columns]
    df["Date"] = pd.to_datetime(df["Date"])
    for c in ("Open", "High", "Low", "Close"):
        df[c] = pd.to_numeric(df[c].astype(str).str.replace(",", ""), errors="coerce")
    df = df.sort_values("Date").drop_duplicates(subset=["Date"]).reset_index(drop=True)
    d = bm.dated_levels(df)
    f = lambda xs: " | ".join(f"{p:,.2f} ({dt})" for dt, p in xs) or "none"
    print(f"As of {d['date']} close {d['close']:,.2f}")
    print("  R1–R4      ", f(d["resistances"]))
    print("  S1–S4      ", f(d["supports"]))
    print("  Upper gaps ", f(d["upper_gaps"]))
    print("  Lower gaps ", f(d["lower_gaps"]))


def main():
    if "--verify" in sys.argv:
        verify(sys.argv[sys.argv.index("--verify") + 1]); return
    check = "--check" in sys.argv
    ok = patch("build_master.py", fix_bm, check)
    ok = patch("levels.py", fix_lv, check) and ok
    ok = patch("dashboard.py", fix_db, check) and ok
    if ok and not check:
        print("Done — run build_master.py once, then restart scheduler.py and the dashboard.")


if __name__ == "__main__":
    main()
