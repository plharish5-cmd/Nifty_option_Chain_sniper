#!/usr/bin/env python3
"""scripts/test_scalp_tier_spot_8510.py — can Tier 2 / Tier 3 be scalped on SPOT?

THE QUESTION BEING TESTED
-------------------------
Harish's rule, in his words, in NIFTY SPOT POINTS only (option strike, delta,
theta and VIX deliberately out of scope):

  1. A Tier 2 or Tier 3 proximity signal fires.
  2. Enter at that minute's close, in the signal's direction.
  3. Initial stop: a fixed number of spot points against.
  4. Once price is +BREAKEVEN_AT points in favour, move the stop to
     cost-to-cost (+1.5 pts) so a reversal scratches instead of losing.
  5. Target: +15 points. Square off.

The only thing that decides whether this works is: DOES THE TARGET COME BEFORE
THE STOP? Average moves, maximum moves and "it went 30 points eventually" do
not pay. First passage does.

WHAT THIS DOES AND DOES NOT MEASURE
-----------------------------------
Signals come from analyst/ema_proximity_engine.py itself -- the real tier rules,
not a reimplementation -- so the signals are the ones the desk would actually
show. Evaluation walks forward one minute at a time on recorded 1-minute OHLC.

AMBIGUITY IS REPORTED, NEVER RESOLVED. When one minute's HIGH reaches the
target and its LOW reaches the stop, the order inside that minute is unknowable
from OHLC. Counting it a win inflates the result; counting it a loss deflates
it. Those bars are reported separately and the win rate is given as a
pessimistic-to-optimistic BRACKET.

COSTS, in spot-point terms, so the break-even is honest:
one spot point on a well-chosen strike is worth about 0.59 x 65 = Rs38.35, and
round-trip friction (statutory charges ~Rs78 + spread ~Rs32.50) is ~Rs110.50,
which is ~2.9 spot points. So a +15 target nets ~12.1 effective points and a
-8 stop costs ~10.9. Break-even win rate is therefore ~47%, NOT 35%.

USAGE
  python3 scripts/test_scalp_tier_spot_8510.py
  python3 scripts/test_scalp_tier_spot_8510.py --target 20 --stop 10
  python3 scripts/test_scalp_tier_spot_8510.py --no-breakeven-shift
  python3 scripts/test_scalp_tier_spot_8510.py --json
"""
from __future__ import annotations

import argparse
import json
import statistics as st
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Spot-point economics. Measured values, not invented:
#   0.59 = measured win-side delta; 65 = lot; 78 = measured statutory charges;
#   32.50 = round-trip cost of crossing a 0.5-pt spread.
DELTA_WIN = 0.59
DELTA_LOSS = 0.67
LOT = 65
CHARGES_INR = 78.0
SPREAD_INR = 32.50
FRICTION_INR = CHARGES_INR + SPREAD_INR

LAST_ENTRY = "15:05"
FREEZE = "15:20"


def hhmm(ts) -> str:
    import pandas as pd
    return pd.to_datetime(ts, unit="s" if float(ts) > 1e9 else None).strftime("%H:%M")


def friction_points() -> float:
    """Round-trip friction expressed in spot points on the win-side delta."""
    return FRICTION_INR / (DELTA_WIN * LOT)


def breakeven_win_rate(target: float, stop: float) -> float:
    """The win rate below which this target/stop pair loses money."""
    fp = friction_points()
    win_pts = (target - fp) * DELTA_WIN * LOT
    loss_pts = (stop * DELTA_LOSS * LOT) + FRICTION_INR
    if win_pts + loss_pts <= 0:
        return float("nan")
    return 100.0 * loss_pts / (win_pts + loss_pts)


def net_inr(points: float, won: bool) -> float:
    d = DELTA_WIN if won else DELTA_LOSS
    return (points * d * LOT) - FRICTION_INR


def collect_signals(engine, date_str: str, timeframe: str) -> List[Dict[str, Any]]:
    """Tier 2 / Tier 3 rows straight from the real engine."""
    res = engine.compute_matrix(date_str=date_str, timeframe=timeframe, filter_state="ALL")
    rows = res.get("rows") or res.get("matrix") or []
    out = []
    for r in rows:
        state = str(r.get("proximity_state") or r.get("proximity_status") or "")
        if state not in ("TIER_2_TOUCH_ZONE", "TIER_3_CONFIRMATION_TRIGGER"):
            continue
        trend = str(r.get("trend") or "").upper()
        cross = str(r.get("crossover_state") or r.get("crossover_status") or "").upper()
        # Direction from the structure the engine already decided, never guessed.
        if "BULL" in trend or "UPSIDE" in cross or "SUPPORT" in str(r.get("support_resistance", "")).upper():
            direction = "LONG"
        elif "BEAR" in trend or "DOWNSIDE" in cross or "RESISTANCE" in str(r.get("support_resistance", "")).upper():
            direction = "SHORT"
        else:
            continue          # no stated direction -> not tradeable, skip honestly
        out.append({
            "date": date_str, "tier": state, "direction": direction,
            "ts": r.get("ts"), "time_ist": r.get("time_ist") or hhmm(r.get("ts")),
            "spot": float(r.get("close_price") or r.get("nifty_spot_price") or 0.0),
            "timeframe": timeframe,
        })
    return [s for s in out if s["spot"] > 0]


def dedupe(signals: List[Dict[str, Any]], cooldown_min: int = 15) -> List[Dict[str, Any]]:
    """Hovering at the 8 EMA for four minutes is ONE signal, not four."""
    kept: List[Dict[str, Any]] = []
    last: Dict[tuple, Any] = {}
    for s in sorted(signals, key=lambda x: str(x["time_ist"])):
        key = (s["date"], s["timeframe"], s["tier"], s["direction"])
        t = s["time_ist"]
        mins = int(t[:2]) * 60 + int(t[3:5])
        prev = last.get(key)
        if prev is None or (mins - prev) >= cooldown_min:
            kept.append(s)
            last[key] = mins
    return kept


def evaluate(sig: Dict[str, Any], candles: List[Dict[str, Any]],
             target: float, stop: float, breakeven_at: Optional[float],
             breakeven_lock: float, hold_min: int) -> Dict[str, Any]:
    """Walk forward minute by minute. First passage decides."""
    entry = sig["spot"]
    long_ = sig["direction"] == "LONG"
    t0 = sig["time_ist"]
    start = int(t0[:2]) * 60 + int(t0[3:5])

    cur_stop = -stop          # in favourable-points terms
    moved_to_be = False
    mfe = 0.0
    mae = 0.0
    bars = 0

    for c in candles:
        tm = hhmm(c["ts"])
        m = int(tm[:2]) * 60 + int(tm[3:5])
        if m <= start:
            continue
        if m > int(FREEZE[:2]) * 60 + int(FREEZE[3:5]):
            break
        if m - start > hold_min:
            break
        bars += 1
        hi, lo = float(c["high"]), float(c["low"])
        fav = (hi - entry) if long_ else (entry - lo)     # best in favour this bar
        adv = (lo - entry) if long_ else (entry - hi)     # worst against this bar
        mfe = max(mfe, fav)
        mae = min(mae, adv)

        hit_t = fav >= target
        hit_s = adv <= cur_stop

        if hit_t and hit_s:
            # Both touched inside one minute. Unknowable from OHLC.
            return {"outcome": "AMBIGUOUS", "points": None, "bars": bars,
                    "mfe": round(mfe, 2), "mae": round(mae, 2),
                    "moved_to_be": moved_to_be, "exit_time": tm}
        if hit_t:
            return {"outcome": "TARGET", "points": target, "bars": bars,
                    "mfe": round(mfe, 2), "mae": round(mae, 2),
                    "moved_to_be": moved_to_be, "exit_time": tm}
        if hit_s:
            out = "BREAKEVEN_SCRATCH" if moved_to_be else "STOP"
            return {"outcome": out, "points": cur_stop, "bars": bars,
                    "mfe": round(mfe, 2), "mae": round(mae, 2),
                    "moved_to_be": moved_to_be, "exit_time": tm}

        # The breakeven shift. Checked AFTER stop/target on the same bar, so a
        # bar that reaches +10 and then collapses to the original stop in the
        # same minute is not quietly rescued.
        if breakeven_at is not None and not moved_to_be and fav >= breakeven_at:
            moved_to_be = True
            cur_stop = breakeven_lock

    return {"outcome": "TIMEOUT", "points": 0.0, "bars": bars,
            "mfe": round(mfe, 2), "mae": round(mae, 2),
            "moved_to_be": moved_to_be, "exit_time": None}


def summarise(results: List[Dict[str, Any]], target: float, stop: float) -> Dict[str, Any]:
    n = len(results)
    if not n:
        return {"n": 0}
    c = {k: sum(1 for r in results if r["outcome"] == k)
         for k in ("TARGET", "STOP", "BREAKEVEN_SCRATCH", "TIMEOUT", "AMBIGUOUS")}
    decided = c["TARGET"] + c["STOP"] + c["BREAKEVEN_SCRATCH"] + c["TIMEOUT"]
    wr_pess = 100.0 * c["TARGET"] / n if n else 0.0
    wr_opt = 100.0 * (c["TARGET"] + c["AMBIGUOUS"]) / n if n else 0.0
    be = breakeven_win_rate(target, stop)

    net = 0.0
    for r in results:
        o = r["outcome"]
        if o == "TARGET":
            net += net_inr(target, True)
        elif o == "STOP":
            net += -((stop * DELTA_LOSS * LOT) + FRICTION_INR)
        elif o == "BREAKEVEN_SCRATCH":
            net += -FRICTION_INR + (r["points"] * DELTA_WIN * LOT)
        elif o == "TIMEOUT":
            net += -FRICTION_INR
        # AMBIGUOUS contributes nothing: its sign is unknown.

    mfes = [r["mfe"] for r in results]
    maes = [r["mae"] for r in results]
    tts = [r["bars"] for r in results if r["outcome"] == "TARGET"]
    return {
        "n": n, **{k.lower(): v for k, v in c.items()},
        "win_rate_pessimistic": round(wr_pess, 1),
        "win_rate_optimistic": round(wr_opt, 1),
        "pct_ambiguous": round(100.0 * c["AMBIGUOUS"] / n, 1),
        "break_even_win_rate": round(be, 1),
        "clears_break_even_pessimistic": bool(wr_pess > be),
        "net_inr_excl_ambiguous": round(net, 0),
        "net_inr_per_trade": round(net / max(1, decided), 0),
        "median_mfe_pts": round(st.median(mfes), 1),
        "p90_mfe_pts": round(sorted(mfes)[int(0.9 * (len(mfes) - 1))], 1),
        "median_mae_pts": round(st.median(maes), 1),
        "worst_mae_pts": round(min(maes), 1),
        "median_minutes_to_target": round(st.median(tts), 1) if tts else None,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--target", type=float, default=15.0)
    ap.add_argument("--stop", type=float, default=8.0)
    ap.add_argument("--breakeven-at", type=float, default=10.0)
    ap.add_argument("--breakeven-lock", type=float, default=1.5)
    ap.add_argument("--no-breakeven-shift", action="store_true")
    ap.add_argument("--hold", type=int, default=60)
    ap.add_argument("--timeframes", default="1m,15m")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    from analyst.ema_proximity_engine import EmaProximityEngine
    eng = EmaProximityEngine()

    dates = sorted(p.stem for p in (ROOT / "analyst" / "replay_data").glob("*.json"))
    if not dates:
        print("No recorded sessions in analyst/replay_data/. Nothing to test.")
        return 2

    be_at = None if a.no_breakeven_shift else a.breakeven_at
    report: Dict[str, Any] = {
        "dates": dates, "n_sessions": len(dates),
        "target_pts": a.target, "stop_pts": a.stop,
        "breakeven_shift_at": be_at, "breakeven_lock_pts": a.breakeven_lock,
        "hold_minutes": a.hold,
        "friction_inr_round_trip": FRICTION_INR,
        "friction_in_spot_points": round(friction_points(), 2),
        "by_tier": {}, "by_timeframe": {},
    }

    all_rows: List[Dict[str, Any]] = []
    for tf in [t.strip() for t in a.timeframes.split(",") if t.strip()]:
        for d in dates:
            try:
                df1, _, _ = eng.load_candles_for_date(d, timeframe="1m")
            except Exception as exc:
                print("could not load candles for {}: {}".format(d, exc))
                continue
            candles = df1.to_dict("records")
            sigs = dedupe(collect_signals(eng, d, tf))
            sigs = [s for s in sigs if s["time_ist"] <= LAST_ENTRY]
            for s in sigs:
                r = evaluate(s, candles, a.target, a.stop, be_at,
                             a.breakeven_lock, a.hold)
                r.update(s)
                all_rows.append(r)

    if not all_rows:
        print("No Tier 2 / Tier 3 signals with a stated direction were produced.")
        return 1

    for tier in ("TIER_3_CONFIRMATION_TRIGGER", "TIER_2_TOUCH_ZONE"):
        rs = [r for r in all_rows if r["tier"] == tier]
        if rs:
            report["by_tier"][tier] = summarise(rs, a.target, a.stop)
    for tf in sorted({r["timeframe"] for r in all_rows}):
        rs = [r for r in all_rows if r["timeframe"] == tf]
        report["by_timeframe"][tf] = summarise(rs, a.target, a.stop)
    report["overall"] = summarise(all_rows, a.target, a.stop)
    report["signals"] = all_rows

    if a.json:
        print(json.dumps(report, indent=2, default=str))
        return 0

    print("TIER 2 / TIER 3 SPOT SCALP — FIRST-PASSAGE TEST")
    print("=" * 78)
    print("sessions        : {}  ({})".format(len(dates), ", ".join(dates)))
    print("rule            : enter at signal close, target +{} pts, stop -{} pts".format(
        a.target, a.stop))
    if be_at is not None:
        print("                  stop -> +{} (cost-to-cost) once +{} in favour".format(
            a.breakeven_lock, be_at))
    print("hold            : {} min, no entry after {}, freeze {}".format(
        a.hold, LAST_ENTRY, FREEZE))
    print("friction        : Rs{} round trip = {} spot points".format(
        FRICTION_INR, round(friction_points(), 2)))
    print()
    hdr = "{:<30}{:>5}{:>7}{:>6}{:>7}{:>6}{:>6}{:>15}{:>11}{:>9}"
    print(hdr.format("group", "n", "target", "stop", "scratch", "t/out", "amb",
                     "win% (pes-opt)", "break-even", "net/tr"))
    print("-" * 110)

    def line(label, s):
        if not s.get("n"):
            return
        print(hdr.format(
            label[:29], s["n"], s["target"], s["stop"], s["breakeven_scratch"],
            s["timeout"], s["ambiguous"],
            "{}-{}".format(s["win_rate_pessimistic"], s["win_rate_optimistic"]),
            s["break_even_win_rate"], s["net_inr_per_trade"]))

    for k, v in report["by_tier"].items():
        line(k.replace("_CONFIRMATION_TRIGGER", " (T3)").replace("_TOUCH_ZONE", " (T2)"), v)
    for k, v in report["by_timeframe"].items():
        line("timeframe " + k, v)
    line("ALL", report["overall"])

    o = report["overall"]
    print()
    print("MOVE DISTRIBUTION (what was actually available)")
    print("-" * 78)
    print("  median best-case move in favour : {} pts".format(o["median_mfe_pts"]))
    print("  p90 best-case move in favour    : {} pts".format(o["p90_mfe_pts"]))
    print("  median worst move against       : {} pts".format(o["median_mae_pts"]))
    print("  worst move against              : {} pts".format(o["worst_mae_pts"]))
    if o["median_minutes_to_target"]:
        print("  median minutes to target        : {}".format(o["median_minutes_to_target"]))
    print()
    print("VERDICT")
    print("-" * 78)
    print("  break-even win rate at +{}/-{} is {}%".format(
        a.target, a.stop, o["break_even_win_rate"]))
    print("  achieved (pessimistic)            {}%".format(o["win_rate_pessimistic"]))
    print("  achieved (optimistic)             {}%".format(o["win_rate_optimistic"]))
    if o["pct_ambiguous"] > 10:
        print("  *** {}% of trades are AMBIGUOUS: one minute touched both the".format(
            o["pct_ambiguous"]))
        print("      target and the stop, and OHLC cannot say which came first.")
        print("      There is no single headline win rate. Use the range.")
    print()
    print("  SAMPLE SIZE: {} sessions, {} de-duplicated signals.".format(
        len(dates), o["n"]))
    print("  This is far too small to establish an edge. It shows whether the")
    print("  machinery works and gives a first read, nothing more.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
