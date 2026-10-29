"""Read-only adapter to the unchanged 8501 Time-series Chain engines."""
from __future__ import annotations
import contextlib
import io
import json
import logging
import math
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
log = logging.getLogger("time_series_chain")

def policy():
    return json.loads((ROOT / "analyst/analyst_config.json").read_text())["time_series_chain"]

def number(v):
    try:
        n = float(v)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError):
        return None

def build(params, snaps=None, days=None):
    import pandas as pd
    import store
    import ts_chain_pro as tc
    import ts_signals as sig
    cfg = policy()
    days = days if days is not None else tc.ol._days_available()
    sessions = [str(d) for d in days]
    if not sessions:
        return {"ok": False, "message": "No option-chain snapshots stored yet.", "sessions": [], "policy": cfg}
    day = params.get("day") or sessions[0]
    if day not in sessions:
        return {"ok": False, "message": "Session is not available. Choose a stored session.", "sessions": sessions, "day": day, "policy": cfg}
    snaps = snaps if snaps is not None else store.load_day(date.fromisoformat(day))
    exps = tc.expiries(snaps)
    current = [e for e in exps if pd.notna(pd.to_datetime(e, errors="coerce", dayfirst=not e[:4].isdigit())) and pd.to_datetime(e, errors="coerce", dayfirst=not e[:4].isdigit()).date() >= date.fromisoformat(day)]
    exp = params.get("expiry") or (current[0] if current else exps[0] if exps else None)
    meta = dict(sessions=sessions, day=day, expiries=exps, expiry=exp, policy=cfg, live=day==max(sessions) and day==date.today().isoformat())
    meta["expiry_labels"] = {e: e + " (" + str((pd.to_datetime(e, errors="coerce", dayfirst=not e[:4].isdigit()).date()-date.fromisoformat(day)).days) + "d)" for e in exps if pd.notna(pd.to_datetime(e, errors="coerce", dayfirst=not e[:4].isdigit()))}
    if exp not in exps:
        return dict(meta, ok=False, message="No snapshots for this expiry.")
    x = tc.long_frame(snaps, exp)
    if x.empty or x.ts.nunique() < 2:
        return dict(meta, ok=False, message="Not enough snapshots for this expiry yet.")
    tf = params.get("tf", cfg["default_tf"])
    mode = params.get("mode", "OI Change")
    ncols = int(params.get("columns", cfg["default_columns"]))
    rng = int(params.get("range_n", cfg["default_range"]))
    selection = params.get("selection", "Range")
    if tf not in cfg["timeframes"] or mode not in tc.MODES or ncols not in cfg["columns"] or rng not in cfg["ranges"] or selection not in ("Range", "Custom"):
        raise ValueError("Invalid grid controls")
    times = sorted(x.ts.unique())
    end = pd.Timestamp(params.get("end") or times[-1])
    if end.tzinfo is not None:
        raise ValueError("Replay end must use session-local time")
    if end < pd.Timestamp(times[0]) or end > pd.Timestamp(times[-1]):
        raise ValueError("Replay end is outside this session")
    asof = x[x.ts <= end]
    spot = float(asof[asof.ts == asof.ts.max()].spot.iloc[0])
    have = sorted(int(k) for k in x.strike.unique())
    atm = min(have, key=lambda k: abs(k - spot))
    step = float(tc.ol._rules().get("strike_step", 50) or 50)
    candidates = sorted(sorted(have, key=lambda k: abs(k - atm))[:cfg["custom_candidates"]])
    meta.update(times=[pd.Timestamp(t).isoformat() for t in times], strikes=have, candidates=candidates, default_custom=[k for k in candidates if abs(k-atm)<=cfg["default_custom_steps"]*step], atm=atm, end=end.isoformat())
    custom = params.get("custom") or []
    strikes = [k for k in have if k in custom] if selection == "Custom" else [k for k in have if atm-rng*step <= k <= atm+rng*step]
    if not strikes:
        return dict(meta, ok=False, message="Pick at least one strike.")
    mult, unit = tc.share_mult(x)
    lot = tc.lot_size()
    g = tc.grid(x, tf, mode, ncols, strikes, mult, end)
    if not g or not len(g["cols"]):
        return dict(meta, ok=False, message="No buckets yet.")
    opts = params.get("settings") or {}
    if opts.get("font", "Small") not in ("Small", "Medium", "Large"):
        raise ValueError("Invalid font size")
    show_sig = params.get("signals", True)
    sg = sig.compute(x, tc.TF[tf], end) if show_sig else None
    sig_map = {pd.Timestamp(r.ts): r.badge for r in sg.itertuples()} if sg is not None and len(sg) else {}
    html = tc.grid_html(g, mode, atm, tf, dict(opts, lot_div=lot if opts.get("lot") else 1, sig=sig_map))
    if sg is not None and len(sg):
        html += tc._card("🎯 LEVEL SIGNALS — "+tf+" · buy / sell · built / broken / reversal / fake-out",
                         sig.card_html(sg, g["cols"], g["labels"], tc.WHITE, tc.MUTED, tc.GOLD, tc.GREEN, tc.RED))
    chg = g if mode == "OI Change" else g["chg"]
    rd = tc.pro_read(chg, atm, step, spot)
    html += tc._card("★ PRO READ — "+tf+" · last "+str(len(chg["cols"]))+" buckets", "<br>".join(rd["lines"]), border=rd["bcol"])
    narrative, trs = [], []
    for col in chg["cols"]:
        tag, text = tc.column_read(chg["ce"][col], chg["pe"][col], atm, step)
        colour = tc.GREEN if tag == "BULLISH" else tc.RED if tag == "BEARISH" else tc.GOLD
        px = number(chg["spot"].get(col))
        label = chg["labels"].get(col, "") + (" ⏳" if col == chg["cols"][-1] and chg["partial"] else "")
        narrative.append(dict(bucket=pd.Timestamp(col).isoformat(), label=label, spot=px, read=tag, text=text))
        trs.append(f"<tr><td style='color:{tc.GOLD};white-space:nowrap'><b>{label}</b></td><td>{px:,.0f}</td><td style='color:{colour}'><b>{tag}</b></td><td>{text}</td></tr>" if px is not None else f"<tr><td>{label}</td><td>—</td><td>{tag}</td><td>{text}</td></tr>")
    html += tc._card("🕐 BUCKET-BY-BUCKET NARRATIVE", "<div style='overflow-x:auto'><table class='narrative'><tr><th>Bucket</th><th>NIFTY</th><th>Read</th><th>Biggest moves</th></tr>"+"".join(trs)+"</table></div>")
    html += tc._card("WHAT TO DO WITH IT", "<ul>"+"".join("<li>"+v+"</li>" for v in rd["do"])+"</ul>")
    html += "<details><summary>How to read the time-series grid</summary><ul>"+"".join("<li>"+v+"</li>" for v in tc.HOW)+"</ul></details>"
    future = None
    try:
        import pcr_oi_pro as po
        cache = po._fut_dir() / (day + ".csv")
        f = pd.read_csv(cache, parse_dates=["ts"]) if cache.exists() else None
        if f is not None and len(f):
            eligible = f[pd.to_datetime(f.ts).dt.tz_localize(None) <= end]
            if len(eligible):
                future = number(eligible.fut.iloc[-1])
    except Exception as exc:
        log.warning("Time-series future caption unavailable: %s", exc)
    matrix = lambda key: [[number(v) for v in row] for row in g[key].values]
    signals = [] if sg is None else [dict(time=pd.Timestamp(r.ts).isoformat(), close=number(r.close), support=number(r.sup), resistance=number(r.res), event=list(r.lv) if isinstance(r.lv, tuple) else None, action=list(r.act) if isinstance(r.act, tuple) else None, badge=list(r.badge)) for r in sg.itertuples()]
    return dict(meta, ok=True, html=html, mode=mode, tf=tf, spot=spot, future=future, mult=mult, unit=unit, lot=lot,
                now=str(g["now_ts"]), partial=g["partial"],
                elapsed=g["elapsed"], step_min=g["step_min"], selected_strikes=strikes,
                grid=dict(cols=[pd.Timestamp(c).isoformat() for c in g["cols"]], labels=[g["labels"][c] for c in g["cols"]], ce=matrix("ce"), pe=matrix("pe")),
                signals=signals, narrative=narrative, pro=rd)

def main():
    try:
        params = json.load(sys.stdin)
        with contextlib.redirect_stdout(io.StringIO()):
            out = build(params)
        sys.stdout.write(json.dumps(out, default=str, allow_nan=False))
    except Exception as exc:
        log.exception("Time-series export failed")
        sys.stdout.write(json.dumps({"ok": False, "message": f"{type(exc).__name__}: {exc}"}))

if __name__ == "__main__":
    main()
