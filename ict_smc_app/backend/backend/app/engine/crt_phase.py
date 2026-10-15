"""
engine/crt_phase.py — DISPLAY-ONLY phase tracker for Setup D (CRT + TBS). It reads what crt_tbs.evaluate_pair
already produced (ranges, TBS / Model / entry events, signals) plus closed LTF candles, and labels where each CRT is
in its 3-candle story. It never changes a range, an event, a signal, a paper trade or the scorecard.

  C1  ACCUMULATION          the CRT candle closed: CRH / CRL / CRM (50 %) are set, waiting for the sweep
  C2  TBS SWEEP             an LTF body closed beyond CRH (bear) / CRL (bull) inside the manipulation candle
  C3  LTF MSS CONFIRMED     Model #1 / #2 / #3 confirmed → entry (Setup D signal)
      50 % CRM HIT          after the entry, price reached the 50 % (TP-1: book half, SL → breakeven)
      OPPOSING EXTREME HIT  price reached CRL (bear) / CRH (bull) (TP-2)
      INVALIDATED           stop hit before TP-1, or the window closed without a TBS / entry (expired / no-entry)
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..timeutil import hm

ORDER = ["ACCUMULATION", "TBS SWEEP", "LTF MSS CONFIRMED", "50% CRM HIT", "OPPOSING EXTREME HIT"]


def phases(res: Dict[str, Any], ltf_rows: List[Dict[str, Any]], rvol_at: Optional[Dict[int, Dict[str, Any]]] = None,
           absorb_ts: Optional[set] = None, strikes: Optional[Dict[str, Any]] = None, keep: int = 4) -> List[Dict[str, Any]]:
    rows = [r for r in ltf_rows if r.get("closed", True)]
    ev = res.get("events", [])
    sigs = res.get("signals", [])
    out: List[Dict[str, Any]] = []
    for cr in res.get("ranges", [])[-keep:]:
        k = int(cr["k_ts"])
        mine = [e for e in ev if f"|{k}|" in e["id"]]
        tbs = next((e for e in mine if e["kind"] == "CRT_TBS"), None)
        m1 = next((e for e in mine if e["kind"] == "CRT_M1"), None)
        ent = next((e for e in mine if e["kind"] == "CRT_ENTRY"), None)
        sig = next((s for s in sigs if getattr(s, "key", "").endswith(f"|{k}")), None)
        bear = (tbs or ent or {}).get("dir", None) == "bear" if (tbs or ent) else (cr["biases"] == ["bear"])
        phase, done, note = "ACCUMULATION", [], ""
        steps = [{"phase": "ACCUMULATION", "at": cr["time"], "text": f"C1 {cr['htf']} CRT candle {cr['time']} · CRH {cr['crth']:,.1f} · "
                  f"CRM {cr['mid']:,.1f} · CRL {cr['crtl']:,.1f}"}]
        sweep = None
        if tbs:
            phase = "TBS SWEEP"
            rv = (rvol_at or {}).get(int(tbs["ts"]) // 60 * 60) or {}
            sweep = {"at": tbs["time"], "price": tbs["price"], "rvol": rv.get("rvol"),
                     "absorption": bool(absorb_ts and any(abs(int(tbs["ts"]) - t) <= 300 for t in absorb_ts))}
            steps.append({"phase": "TBS SWEEP", "at": tbs["time"], "text": tbs.get("text", "TBS")
                          + (f" · RVOL {rv['rvol']:.1f}×" if rv.get("rvol") is not None else "")
                          + (" · delta absorption at the sweep" if sweep["absorption"] else "")})
        if m1:
            steps.append({"phase": "LTF MSS", "at": m1["time"], "text": m1.get("text", "Model confirmed")})
        trade = None
        if ent and sig is not None:
            phase = "LTF MSS CONFIRMED"
            steps.append({"phase": "LTF MSS CONFIRMED", "at": ent["time"], "text": ent.get("text", "entry")})
            sell = sig.side == "short"
            trade = {"side": "SHORT · buy PE" if sell else "LONG · buy CE", "entry": sig.entry, "sl": sig.sl,
                     "tp1": cr["mid"], "tp2": sig.tp, "rr": sig.rr, "status": sig.status, "reasons": sig.reasons}
            after = [r for r in rows if int(r["ts"]) > int(sig.ts)]
            hit1 = None
            for r in after:
                if (r["h"] >= sig.sl) if sell else (r["l"] <= sig.sl):
                    if hit1 is None:
                        phase, note = "INVALIDATED", f"stop {sig.sl:,.1f} hit at {hm(int(r['ts']))} before TP-1"
                    else:
                        note = f"breakeven stop after TP-1 ({hm(int(r['ts']))})"
                    break
                if hit1 is None and ((r["l"] <= cr["mid"]) if sell else (r["h"] >= cr["mid"])):
                    hit1, phase = r, "50% CRM HIT"
                    steps.append({"phase": "50% CRM HIT", "at": hm(int(r["ts"])), "text": f"TP-1 {cr['mid']:,.1f} reached — book half, SL → breakeven"})
                if (r["l"] <= sig.tp) if sell else (r["h"] >= sig.tp):
                    phase = "OPPOSING EXTREME HIT"
                    steps.append({"phase": "OPPOSING EXTREME HIT", "at": hm(int(r["ts"])), "text": f"TP-2 {sig.tp:,.1f} reached"})
                    break
            pick = (strikes or {}).get("PE" if sell else "CE") or {}
            ins = pick.get("instrument") or {}
            if ins.get("strike") is not None:
                trade["strike"] = f"{ins.get('expiry', '')} {int(ins['strike'])} {'PE' if sell else 'CE'}"
        elif cr["status"] in ("expired", "no-entry"):
            phase = "INVALIDATED"
            note = "window closed without a TBS" if cr["status"] == "expired" else "TBS printed but no entry model confirmed in time"
        ready = ("TRADE LIVE" if phase in ("LTF MSS CONFIRMED", "50% CRM HIT") and trade and trade["status"] == "TRADE"
                 else "WATCH — sweep done, waiting for the LTF MSS" if phase == "TBS SWEEP" and cr.get("live")
                 else "WAITING for the manipulation candle" if phase == "ACCUMULATION" and cr.get("live")
                 else "SKIP (desk filters)" if trade and trade["status"] == "SKIP" else "done")
        out.append({"pair": res.get("pair"), "name": res.get("name"), "htf": cr["htf"], "ltf": cr["ltf"], "time": cr["time"],
                    "k_ts": k, "crh": cr["crth"], "crm": cr["mid"], "crl": cr["crtl"], "aplus": cr.get("aplus"),
                    "bias": "bear" if bear else "bull" if (tbs or ent or cr["biases"] == ["bull"]) else "both",
                    "range_status": cr["status"], "live": cr.get("live"), "phase": phase,
                    "progress": ORDER.index(phase) + 1 if phase in ORDER else 0, "note": note, "sweep": sweep,
                    "trade": trade, "ready": ready, "steps": steps,
                    "keylevels": [x.get("kind") for x in cr.get("keylevels", [])], "rvol_c1": cr.get("rvol")})
    return out
