"""
oc_export.py — JSON export of two Options Lab views for the ICT · SMC desk (port 8600).

  python oc_export.py pece --day 2026-10-14 --tf 3m --mode auto --n 10
      → the PE − CE OI table, row for row (same pe_ce_panel.table_rows + 🎯 LEVELS + ★ PRO READ as 8501)
  python oc_export.py flow --day 2026-10-14 --tf 1m --mode all --n 10
      → "OI Change (Call vs Put) — since yesterday" + writer-flow strip (same pcr_oi_pro.build / flow_frame)

Runs in the OC folder with the OC virtualenv (the 8600 desk calls it as a subprocess). Read-only: reads stored
snapshots, never places orders. Prints one JSON object to stdout.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import calendar
import math
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _f(v):
    try:
        v = float(v)
        return None if math.isnan(v) or math.isinf(v) else round(v, 4)
    except Exception:                                           # noqa: BLE001
        return None


def _ct(ts) -> int:
    """'chart time': IST wall-clock as if it were UTC (what the 8600 charts use), independent of the machine's TZ."""
    import pandas as pd
    ts = pd.Timestamp(ts)
    if ts.tzinfo is not None:
        ts = ts.tz_convert("Asia/Kolkata").tz_localize(None)
    return calendar.timegm(ts.timetuple())


def _t(x):
    return list(x) if isinstance(x, tuple) else None


def pece(day: date, tf: str, mode: str, n: int) -> dict:
    import store
    import pe_ce_panel as pc
    r = pc._rules()
    snaps = store.load_day(day)
    if len(snaps) < 3:
        return {"ok": False, "message": "Not enough snapshots for this session yet."}
    m = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "1h": 60}[tf]
    win = pc.strike_window(snaps, "All" if mode == "all" else "Auto ATM", None if mode == "all" else n, None, r["strike_step"])
    s = pc.per_snapshot(snaps, win, r["oi_multiplier"])
    if s.empty:
        return {"ok": False, "message": "No strikes inside that range."}
    t = pc.table_rows(s, m)
    rows = []
    for x in t.itertuples():
        rd = x.read if isinstance(x.read, tuple) else ("—", "flat")
        rows.append(dict(
            ts=f"{x.ts:%H:%M}", pe_oi=_f(x.pe_oi), ce_oi=_f(x.ce_oi), pe_day=_f(x.pe_day), pe_chg=_f(x.pe_chg),
            ce_day=_f(x.ce_day), ce_chg=_f(x.ce_chg), pe_ce_day=_f(x.pe_ce_day), pe_ce_chg=_f(x.pe_ce_chg),
            pcr=_f(x.pcr), vol_pcr=_f(x.vol_pcr), close=_f(x.close),
            sup=_f(getattr(x, "sup", None)), res=_f(getattr(x, "res", None)),
            lv=_t(getattr(x, "lv", None)), act=_t(getattr(x, "act", None)),
            read=list(rd), meaning=pc.MEANING.get(rd[0], ""), pro=_t(getattr(x, "pro", None))))
    rng = f"{win[0]:,.0f} – {win[1]:,.0f}" if win else "all strikes"
    return {"ok": True, "day": str(day), "tf": tf, "range": rng, "mult": r["oi_multiplier"],
            "spot": _f(t.close.iloc[-1]), "rows": rows[::-1]}                     # newest first, like 8501


def flow(day: date, tf: str, mode: str, n: int) -> dict:
    import store
    import config
    import options_lab_tab as ol
    import pcr_oi_pro as po
    snaps = store.load_day(day)
    if len(snaps) < 3:
        return {"ok": False, "message": "Not enough snapshots for this session yet."}
    lot = float(getattr(config, "LOT_SIZE", 65) or 65)
    fut, why = po.future_1m(day)
    if fut is None or len(fut) < 3:
        fut, label = po.synth(snaps), f"Synth future ({why})"
    else:
        label = why
    t = po.build(snaps, tf, "All" if mode == "all" else "Auto ATM", n, lot, fut)
    if t.empty:
        return {"ok": False, "message": "No strikes inside that range."}
    try:
        import pandas as pd
        exp = pd.to_datetime(ol._one_expiry(snaps[-1][2])["expiry"].iloc[0], errors="coerce").date()
    except Exception:                                           # noqa: BLE001
        exp = None
    uw = po.unwind_window(t, exp == day)
    b = po.flow_frame(t)
    pts = []
    for (x, y) in zip(t.itertuples(), b.itertuples()):
        pts.append(dict(t=_ct(x.ts), hm=f"{x.ts:%H:%M}", fut=_f(x.fut), ce=_f(x.ce_day), pe=_f(x.pe_day),
                        flow=_f(y.flow), bal=_f(y.bal), regime=str(y.regime), sig=bool(y.sig),
                        flip=bool(y.tflip), conf=(None if not isinstance(getattr(y, "regime_confirmed", None), str)
                                                  else y.regime_confirmed)))
    try:
        note = po.flow_note(t)
    except Exception:                                           # noqa: BLE001
        note = ""
    try:
        rd = po.reads(t, uw).get("chg", "")
    except Exception:                                           # noqa: BLE001
        rd = ""
    return {"ok": True, "day": str(day), "tf": tf, "label": label, "expiry": str(exp) if exp else None,
            "unwind": ([_ct(uw[0]), _ct(uw[1])] if uw else None),
            "read": rd, "note": note, "points": pts}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("what", choices=["pece", "flow"])
    ap.add_argument("--day", default=None)
    ap.add_argument("--tf", default="3m")
    ap.add_argument("--mode", default="auto", choices=["auto", "all"])
    ap.add_argument("--n", type=int, default=10)
    a = ap.parse_args()
    day = datetime.strptime(a.day, "%Y-%m-%d").date() if a.day else None
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):                   # module prints / warnings must not break the JSON
            if day is None:
                import options_lab_tab as ol
                ds = ol._days_available()
                day = ds[0] if ds else date.today()
            out = pece(day, a.tf, a.mode, a.n) if a.what == "pece" else flow(day, a.tf, a.mode, a.n)
    except Exception as e:                                      # noqa: BLE001
        out = {"ok": False, "message": f"{type(e).__name__}: {e}"}
    sys.stdout.write(json.dumps(out, default=str))


if __name__ == "__main__":
    main()
