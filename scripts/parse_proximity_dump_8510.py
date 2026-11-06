#!/usr/bin/env python3
"""scripts/parse_proximity_dump_8510.py — read a pasted EMA Proximity table.

WHY
---
The CSV export produced a header and zero rows, because only 2026-11-04 and
2026-11-05 were ever recorded. Harish pasted the rendered 06 Nov table instead,
which carries everything needed: per-minute close, the proximity state, the tier
badge, and the engine's own forward "+MFE / -MAE" pair.

WHAT IT ANSWERS
---------------
"Do Tier 2 / Tier 3 signals give a good amount of NIFTY points?"

Two different statistics get confused here, and the whole question turns on the
difference:

  MFE WIN      -- price reached +15 in favour at some point in the window.
                  This is what the desk's "78%" reports. It is real, but it is
                  measured PER BAR and it never asks whether the stop was hit
                  first.
  FIRST PASSAGE-- price reached +15 BEFORE it reached the stop. This is the only
                  one that pays.

The dump gives MFE and MAE over the same forward window, so first passage can be
BRACKETED exactly even without tick order:
    MFE >= target and |MAE| <  stop  -> WIN   (stop was never touched)
    |MAE| >= stop  and MFE  <  target-> LOSS  (target was never reached)
    both                             -> AMBIGUOUS (order unknowable)
    neither                          -> NO_RESOLUTION within the window

Nothing is guessed. Ambiguity is reported, never resolved.

USAGE
  python3 scripts/parse_proximity_dump_8510.py <dump.txt>
  python3 scripts/parse_proximity_dump_8510.py <dump.txt> --target 15 --stop 8
  python3 scripts/parse_proximity_dump_8510.py <dump.txt> --json
"""
from __future__ import annotations

import argparse
import json
import re
import statistics as st
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROW = re.compile(r"^\d{2}\s\w{3}\s\d{4}\t")
MFE_MAE = re.compile(r"^\s*([+-][\d.]+)\s*/\s*([+-][\d.]+)\s*$")
MICRO_SL = re.compile(r"Micro-SL:\s*([\d.]+)\s*\(Risk:\s*([\d.]+)\s*pts\)")

# Costs, in spot points, from measured figures: Rs78 statutory + Rs32.50 spread
# over a 0.59 delta on lot 65.
FRICTION_PTS = (78.0 + 32.50) / (0.59 * 65)


def parse(path: Path) -> List[Dict[str, Any]]:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    recs: List[Dict[str, Any]] = []
    cur: Optional[Dict[str, Any]] = None
    for ln in lines:
        if ROW.match(ln):
            if cur:
                recs.append(cur)
            f = ln.split("\t")
            def num(s):
                try:
                    return float(re.sub(r"[^\d.\-+]", "", s.split("(")[0]))
                except (ValueError, IndexError):
                    return None
            cur = {
                "date": f[0].strip(),
                "time": f[1].strip()[:5] if len(f) > 1 else None,
                "close": num(f[2]) if len(f) > 2 else None,
                "ma4": num(f[3]) if len(f) > 3 else None,
                "ema8": num(f[4]) if len(f) > 4 else None,
                "gap_pts": num(f[5]) if len(f) > 5 else None,
                "proximity": f[9].strip() if len(f) > 9 else "",
                "signal": f[10].strip() if len(f) > 10 else "",
                "tier": None, "mfe": None, "mae": None,
                "risk_pts": None, "chop": False, "extras": [],
            }
            continue
        if cur is None:
            continue
        s = ln.strip()
        if not s:
            continue
        m = MFE_MAE.match(s)
        if m:
            cur["mfe"] = float(m.group(1))
            cur["mae"] = float(m.group(2))
            continue
        if "TIER 3" in s:
            cur["tier"] = "TIER_3"
        elif "TIER 2" in s:
            cur["tier"] = "TIER_2"
        elif "TIER 1" in s:
            cur["tier"] = "TIER_1"
        if "CHOP SUPPRESSED" in s or s == "🚫 CHOP":
            cur["chop"] = True
        ms = MICRO_SL.search(s)
        if ms:
            cur["risk_pts"] = float(ms.group(2))
        cur["extras"].append(s)
    if cur:
        recs.append(cur)
    return [r for r in recs if r.get("close") and r.get("time")]


def direction(r: Dict[str, Any]) -> Optional[str]:
    """Direction from what the engine already decided. Never guessed."""
    blob = (r["proximity"] + " " + r["signal"] + " " + " ".join(r["extras"])).upper()
    long_hits = ("CE LONG", "CROSS UP", "BOUNCE", "BULLISH", "SUPPORT", "CE MICRO-SL")
    short_hits = ("PE SHORT", "CROSS DOWN", "REJECT", "BEARISH", "RESISTANCE", "PE MICRO-SL")
    L = any(h in blob for h in long_hits)
    S = any(h in blob for h in short_hits)
    if L and not S:
        return "LONG"
    if S and not L:
        return "SHORT"
    return None


def dedupe(rows: List[Dict[str, Any]], cooldown: int = 15) -> List[Dict[str, Any]]:
    """Four consecutive minutes hovering at the 8 EMA is ONE signal."""
    out, last = [], {}
    for r in rows:
        key = (r["tier"], r.get("dir"))
        mins = int(r["time"][:2]) * 60 + int(r["time"][3:5])
        if key not in last or mins - last[key] >= cooldown:
            out.append(r)
            last[key] = mins
    return out


def bracket(rows: List[Dict[str, Any]], target: float, stop: float) -> Dict[str, Any]:
    win = loss = amb = unres = 0
    for r in rows:
        mfe, mae = r.get("mfe"), r.get("mae")
        if mfe is None or mae is None:
            unres += 1
            continue
        hit_t = mfe >= target
        hit_s = abs(mae) >= stop
        if hit_t and hit_s:
            amb += 1
        elif hit_t:
            win += 1
        elif hit_s:
            loss += 1
        else:
            unres += 1
    n = len(rows)
    if not n:
        return {"n": 0}
    be = 100.0 * ((stop * 0.67 * 65) + 110.5) / (
        ((target - FRICTION_PTS) * 0.59 * 65) + ((stop * 0.67 * 65) + 110.5))
    return {
        "n": n, "win": win, "loss": loss, "ambiguous": amb, "unresolved": unres,
        "win_rate_pessimistic": round(100.0 * win / n, 1),
        "win_rate_optimistic": round(100.0 * (win + amb) / n, 1),
        "pct_ambiguous": round(100.0 * amb / n, 1),
        "break_even_win_rate": round(be, 1),
    }


def dist(vals: List[float], label: str) -> Dict[str, Any]:
    if not vals:
        return {"label": label, "n": 0}
    s = sorted(vals)
    return {
        "label": label, "n": len(s),
        "min": round(s[0], 1), "p25": round(s[len(s) // 4], 1),
        "median": round(st.median(s), 1), "p75": round(s[3 * len(s) // 4], 1),
        "p90": round(s[int(0.9 * (len(s) - 1))], 1), "max": round(s[-1], 1),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dump")
    ap.add_argument("--target", type=float, default=15.0)
    ap.add_argument("--stop", type=float, default=8.0)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    recs = parse(Path(a.dump))
    if not recs:
        print("No parseable rows.")
        return 2
    for r in recs:
        r["dir"] = direction(r)

    tiers = {}
    for t in ("TIER_3", "TIER_2"):
        rows = [r for r in recs if r["tier"] == t]
        with_dir = [r for r in rows if r["dir"]]
        tradeable = [r for r in with_dir if not r["chop"]]
        ded = dedupe(tradeable)
        tiers[t] = {
            "bars_raw": len(rows),
            "bars_with_direction": len(with_dir),
            "bars_after_chop_filter": len(tradeable),
            "signals_deduped_15m": len(ded),
            "mfe_per_bar": dist([r["mfe"] for r in rows if r["mfe"] is not None],
                                "MFE, every bar"),
            "mae_per_bar": dist([abs(r["mae"]) for r in rows if r["mae"] is not None],
                                "MAE, every bar"),
            "mfe_deduped": dist([r["mfe"] for r in ded if r["mfe"] is not None],
                                "MFE, de-duplicated"),
            "mae_deduped": dist([abs(r["mae"]) for r in ded if r["mae"] is not None],
                                "MAE, de-duplicated"),
            "risk_pts": dist([r["risk_pts"] for r in rows if r["risk_pts"]],
                             "engine Micro-SL risk"),
            "mfe_hit_rate_per_bar": {
                str(k): round(100.0 * sum(1 for r in rows if (r["mfe"] or 0) >= k) /
                              max(1, len([r for r in rows if r["mfe"] is not None])), 1)
                for k in (5, 10, 15, 20, 25, 30, 40)},
            "first_passage_per_bar": bracket(
                [r for r in rows if r["mfe"] is not None], a.target, a.stop),
            "first_passage_deduped": bracket(ded, a.target, a.stop),
        }

    report = {"date": recs[0]["date"], "minutes_parsed": len(recs),
              "target": a.target, "stop": a.stop,
              "friction_spot_points": round(FRICTION_PTS, 2), "tiers": tiers}

    if a.json:
        print(json.dumps(report, indent=2))
        return 0

    print("EMA PROXIMITY — TIER 2 / TIER 3 MOVE ANALYSIS")
    print("=" * 76)
    print("date            : {}".format(report["date"]))
    print("minutes parsed  : {}".format(report["minutes_parsed"]))
    print("friction        : {} spot points round trip".format(report["friction_spot_points"]))

    for t, d in tiers.items():
        if not d["bars_raw"]:
            continue
        print()
        print(t)
        print("-" * 76)
        print("  bars: {} raw -> {} with a stated direction -> {} after CHOP filter"
              .format(d["bars_raw"], d["bars_with_direction"], d["bars_after_chop_filter"]))
        print("  DE-DUPLICATED SIGNALS (15m cooldown): {}".format(d["signals_deduped_15m"]))
        for key in ("mfe_per_bar", "mfe_deduped", "mae_per_bar", "mae_deduped", "risk_pts"):
            x = d[key]
            if x.get("n"):
                print("  {:<22} n={:<4} min {:>6} p25 {:>6} med {:>6} p75 {:>6} max {:>6}"
                      .format(x["label"], x["n"], x["min"], x["p25"], x["median"],
                              x["p75"], x["max"]))
        print()
        print("  MFE HIT RATE PER BAR  (this is what the desk's win % reports)")
        for k, v in d["mfe_hit_rate_per_bar"].items():
            print("    reached +{:<3} in favour : {}%".format(k, v))
        print()
        for label, key in (("PER BAR", "first_passage_per_bar"),
                           ("DE-DUPLICATED", "first_passage_deduped")):
            b = d[key]
            if not b.get("n"):
                continue
            print("  FIRST PASSAGE +{}/-{}  [{}]  n={}".format(
                a.target, a.stop, label, b["n"]))
            print("    target first {} | stop first {} | AMBIGUOUS {} | unresolved {}"
                  .format(b["win"], b["loss"], b["ambiguous"], b["unresolved"]))
            print("    win rate {}% - {}%   (break-even needs {}%)".format(
                b["win_rate_pessimistic"], b["win_rate_optimistic"],
                b["break_even_win_rate"]))
            if b["pct_ambiguous"] > 10:
                print("    {}% ambiguous: MFE and MAE both breached in the same window,"
                      .format(b["pct_ambiguous"]))
                print("    so the order is unknowable. Use the range, not a single number.")
    print()
    print("ONE SESSION. This is a read, not an edge.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
