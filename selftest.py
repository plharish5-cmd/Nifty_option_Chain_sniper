"""
Offline pipeline test. No network, no market hours, no Streamlit.

    python selftest.py

Runs parse -> spot inference -> levels -> bias -> regime -> signal against
data/sample_chain.csv and prints every intermediate value. Use it whenever you
change the scoring logic: if the numbers here move in a way you did not
intend, you broke something.
"""
from __future__ import annotations

from datetime import datetime

import numpy as np

import config
import chain_parser as cp
import analytics as an


def levels_check() -> None:
    """Price-structure and confluence, run against whatever is in data/ohlc."""
    import levels as L, confluence as C
    print("\n" + "=" * 68); print("PRICE STRUCTURE"); print("=" * 68)
    try:
        ohlc = L.load_ohlc()
    except FileNotFoundError as exc:
        print(f"  skipped: {exc}")
        return
    spot = float(ohlc.Close.iloc[-1])
    print(f"history           {len(ohlc)} days, "
          f"{ohlc.Date.min():%d-%b-%Y} to {ohlc.Date.max():%d-%b-%Y}")
    print(f"last close        {spot:,.2f}")

    for note in L.history_health(ohlc):
        print(f"  WARNING: {note}")

    pl = L.all_levels(ohlc, spot)
    by_src: dict[str, int] = {}
    for l in pl:
        by_src[l.source] = by_src.get(l.source, 0) + 1
    print("levels by source  " + ", ".join(f"{k}={v}" for k, v in by_src.items()))

    _, cpr = L.central_pivot_range(ohlc, spot)
    if cpr:
        print(f"CPR ({cpr['date']})      {cpr['bottom']:,.0f} / {cpr['pivot']:,.0f} "
              f"/ {cpr['top']:,.0f}   width {cpr['width']:,.0f} pts "
              f"({cpr['width_pct']:.2f}%) -> {cpr['verdict']}")
        print(f"                  {cpr['hint']}"
              + ("  [spot is inside the CPR]" if cpr["spot_inside"] else ""))

    atm = round(spot / 50) * 50
    oi = [(atm + 150, "resistance", "R1 call wall"),
          (atm - 200, "support", "S1 put wall")]
    z = C.build_zones(pl, oi)
    print(f"\nzones             {len(z)}")
    for zz in z[:6]:
        print(f"  {zz.price:>10,.0f}  {zz.strength:<12} score {zz.score:>4}  "
              f"{len(zz.members)} levels")
    print(f"\nsummary           {C.summarise(z, spot)}")
    for a in C.proximity_alerts(z, spot):
        print(f"  ALERT: {a}")
    for c in L.time_cycles(ohlc):
        print(f"  cycle: {c}")


def main() -> None:
    path = config.DATA_DIR / "sample_chain.csv"
    df = cp.parse_csv(str(path))
    spot = cp.infer_spot(df)

    print("=" * 68)
    print(f"PARSE   {path.name}")
    print("=" * 68)
    print(f"strikes           {len(df)}  ({df.strike.min():.0f} … {df.strike.max():.0f})")
    print(f"spot (parity)     {spot:,.2f}")
    print(f"expired chain?    {cp.is_expired_chain(df, spot)}")

    read = an.analyse(df, spot)
    lv = read.levels

    print("\n" + "=" * 68); print("LEVELS"); print("=" * 68)
    print(f"ATM               {lv.atm}")
    print(f"Support   S1 {lv.s1}  (OI {lv.s1_oi:,.0f})   S2 {lv.s2}")
    print(f"Resistance R1 {lv.r1}  (OI {lv.r1_oi:,.0f})   R2 {lv.r2}")
    print(f"Max pain          {lv.max_pain}")
    print(f"Range             {lv.r1 - lv.s1} pts  ({lv.range_pct:.2f}% of spot)")
    print(f"Position in range {lv.position:.2f}   (0 = at S1, 1 = at R1)")
    print(f"Wall dominance    R1/R2 {lv.r1_dominance:.2f}   S1/S2 {lv.s1_dominance:.2f}")

    print("\n" + "=" * 68); print("BIAS"); print("=" * 68)
    for k, v in read.components.items():
        bar = ("+" if v > 0 else "-") * int(abs(v) * 4)
        print(f"  {k:16s} {v:+6.2f}  {bar}")
    print(f"  {'TOTAL':16s} {read.bias_score:+6.2f}")
    print(f"\nPCR (OI)          {read.pcr_oi:.3f}")
    print(f"PCR (day change)  {read.pcr_chg:.3f}")
    print(f"ATM IV            CE {read.atm_iv_ce:.2f}  PE {read.atm_iv_pe:.2f}  "
          f"skew {read.iv_skew:+.2f}")
    print(f"Total OI          CE {read.total_ce_oi:,.0f}   PE {read.total_pe_oi:,.0f}")
    print(f"\nREGIME            {read.regime}")
    for n in read.notes:
        print(f"  note: {n}")

    # Signal at a mid-session time so guard rails don't mask the logic.
    now = datetime.now().replace(hour=11, minute=30, second=0)
    sig = an.make_signal(df, read, now, days_to_expiry=0.1,
                         confirmed=True, expiry_today=True)

    print("\n" + "=" * 68); print(f"SIGNAL  (simulated {now:%H:%M})"); print("=" * 68)
    print(f"action            {sig.action}")
    print(f"reason            {sig.reason}")
    if sig.strike:
        print(f"strike            {sig.strike}")
        print(f"premium           Rs {sig.premium:,.2f}")
        print(f"cost per lot      Rs {sig.cost_per_lot:,.0f}  "
              f"({sig.cost_per_lot / config.CAPITAL * 100:.0f}% of capital)")
        print(f"delta             {sig.delta:.3f}" if sig.delta == sig.delta else "delta  n/a")
    for b in sig.blocks:
        print(f"  BLOCKED: {b}")
    for w in sig.warnings:
        print(f"  WARNING: {w}")

    print("\n" + "=" * 68); print("QUADRANTS (ATM +/- 3)"); print("=" * 68)
    w = an.window(df, spot, 3)
    for _, r in w.iterrows():
        qc = an.quadrant(r.ce_chg, r.ce_chg_oi)
        qp = an.quadrant(r.pe_chg, r.pe_chg_oi)
        print(f"  {r.strike:>6.0f}  CE {qc:16s} {r.ce_chg_oi:>12,.0f}"
              f"   PE {qp:16s} {r.pe_chg_oi:>12,.0f}")

    levels_check()
    strategy2_check()

    print("\nOK — pipeline ran end to end.")


def strategy2_check() -> None:
    print("\n" + "=" * 68); print("STRATEGY 2 (VWAP / 8 EMA CONFLUENCE)"); print("=" * 68)
    import strategy2 as s2
    import pandas as pd

    dates = pd.date_range("2026-09-04 09:15", periods=10, freq="15min")
    sample_df = pd.DataFrame({
        "timestamp": dates,
        "open": [23910.0, 23920.0, 23940.0, 23960.0, 23970.0, 23965.0, 23950.0, 23935.0, 23945.0, 23940.0],
        "high": [23930.0, 23945.0, 23965.0, 23985.0, 24000.0, 23975.0, 23955.0, 23940.0, 23950.0, 23945.0],
        "low":  [23900.0, 23915.0, 23935.0, 23955.0, 23965.0, 23945.0, 23930.0, 23920.0, 23935.0, 23930.0],
        "close": [23920.0, 23940.0, 23960.0, 23975.0, 23970.0, 23950.0, 23935.0, 23925.0, 23940.0, 23935.0],
        "ema8":  [23910.0, 23920.0, 23935.0, 23950.0, 23960.0, 23960.0, 23950.0, 23940.0, 23938.0, 23936.0],
        "vwap":  [23915.0, 23925.0, 23938.0, 23952.0, 23962.0, 23962.0, 23952.0, 23942.0, 23940.0, 23938.0],
        "ubb":   [23960.0, 23970.0, 23990.0, 24010.0, 24020.0, 24020.0, 24010.0, 24000.0, 23990.0, 23980.0],
        "lbb":   [23880.0, 23890.0, 23900.0, 23910.0, 23920.0, 23920.0, 23910.0, 23900.0, 23890.0, 23880.0],
    })
    tr_mentor = s2.run(sample_df, mode="mentor")
    sm_mentor = s2.summary(tr_mentor)
    print(f"Mentor Mode: {sm_mentor['trades']} trades · {sm_mentor['wins']} wins · {sm_mentor['losses']} losses · {sm_mentor['points']:+.1f} pts")
    assert "win_rate" in sm_mentor
    assert "avg_points" in sm_mentor

    tr_legacy = s2.run(sample_df, mode="legacy")
    sm_legacy = s2.summary(tr_legacy)
    print(f"Legacy Mode: {sm_legacy['trades']} trades · {sm_legacy['wins']} wins · {sm_legacy['losses']} losses · {sm_legacy['points']:+.1f} pts")
    print("Strategy 2 mentor and legacy checks PASSED.")


if __name__ == "__main__":
    main()
