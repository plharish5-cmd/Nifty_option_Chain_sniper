#!/usr/bin/env python3
"""
run.py — the thing you actually launch.

    python run.py --once                       one snapshot, print, exit
    python run.py --loop                       poll every 5 min until 12:30 IST
    python run.py --loop --score 11            supply your pre-market view score
    python run.py --once --symbol BANKNIFTY    other index
    python run.py --once --no-ai               numbers only, no API call
    python run.py --once --dump-chain          also print the full table

Outputs per day:
    data/NIFTY/summary_2026-08-06.csv          one row per poll
    data/NIFTY/chains/NIFTY_20260806_0920.csv  full table per poll
    data/NIFTY/ai_2026-08-06.txt               every AI read, appended
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import pandas as pd

import ai_view
import chain_parser
import render
import config
import metrics
from nse_client import NSEClient

IST = ZoneInfo(config.TZ)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("run")

C = {"g": "\033[92m", "r": "\033[91m", "y": "\033[93m", "c": "\033[96m",
     "b": "\033[1m", "x": "\033[0m"}


# ----------------------------------------------------------------------
# paths
# ----------------------------------------------------------------------
def paths(now):
    base = os.path.join(config.DATA_DIR, config.SYMBOL)
    os.makedirs(os.path.join(base, "chains"), exist_ok=True)
    d = now.strftime("%Y-%m-%d")
    return {
        "summary": os.path.join(base, f"summary_{d}.csv"),
        "ai": os.path.join(base, f"ai_{d}.txt"),
        "chain": os.path.join(base, "chains",
                              f"{config.SYMBOL}_{now.strftime('%Y%m%d_%H%M')}.csv"),
    }


def load_history(path):
    if not os.path.exists(path):
        return []
    try:
        return pd.read_csv(path).to_dict("records")
    except Exception:                                    # noqa: BLE001
        return []


# ----------------------------------------------------------------------
# console report
# ----------------------------------------------------------------------
def colour_tag(tag):
    return {"GREEN": C["g"] + "GREEN" + C["x"],
            "RED": C["r"] + "RED  " + C["x"],
            "FLAT": C["y"] + "FLAT " + C["x"]}.get(tag, tag)


def report(snap, ai_text, premarket=None):
    q, st, wb, pf = (snap["quadrants"], snap["straddle"],
                     snap["writer_bias"], snap["premium_flow"])
    w = 74
    print("\n" + "=" * w)
    print(f"{C['b']}{snap['symbol']}  spot {snap['spot']}  "
          f"NSE {snap['nse_timestamp']}  expiry {snap['expiry']}{C['x']}")
    print("=" * w)

    print(f"{C['c']}QUADRANTS{C['x']} (mode={snap['quadrant_mode']}, "
          f"band ±{snap['band']}, {snap['strikes_in_band']} strikes)")
    for k, label in [("call_itm", "Call ITM"), ("call_otm", "Call OTM"),
                     ("put_itm", "Put  ITM"), ("put_otm", "Put  OTM")]:
        print(f"   {label:9s} {colour_tag(q[k]['colour'])}  "
              f"score {q[k]['score']:+.3f}  ({q[k]['n']} strikes)")
    print(f"   cross-check → price-mode: {snap['condition_price_mode']}"
          f" | oi-mode: {snap['condition_oi_mode']}")

    tone = C["g"] if snap["param15_score"] > 0 else \
        C["r"] if snap["param15_score"] < 0 else C["y"]
    print(f"\n{C['c']}PARAM #15{C['x']}  Condition {snap['condition_no']} → "
          f"{tone}{snap['condition']}{C['x']}  score "
          f"{snap['param15_score']:+d}")
    print(f"   action: {snap['action']}")

    if premarket is not None:
        b = config.SCORE_BANDS
        pl = ("BULLISH" if premarket > b["bullish_above"]
              else "BEARISH" if premarket < b["bearish_below"] else "NEUTRAL")
        clash = ((premarket > b["bullish_above"] and snap["param15_score"] < 0)
                 or (premarket < b["bearish_below"] and snap["param15_score"] > 0)
                 or (pl != "NEUTRAL" and snap["condition_no"] == 5))
        print(f"   pre-market score {premarket} → {pl}"
              + (f"   {C['r']}⚠ CONFLICT RULE: trust the chain, "
                 f"cut size or wait to 9:45{C['x']}" if clash else "   (aligned)"))

    print(f"\n{C['c']}STRUCTURE{C['x']}")
    print(f"   PCR band ±{snap['band']}: {snap['pcr_band']:.3f}   "
          f"full chain: {snap['pcr_label']}")
    print(f"   Max pain {snap['max_pain']:.0f}  "
          f"(spot is {snap['max_pain_gap']:+.1f} away)")
    print(f"   Writer bias: {wb['label']}")
    print(f"      OTM put OI added {wb['otm_put_oi_added']:,.0f} vs "
          f"call {wb['otm_call_oi_added']:,.0f}  net {wb['net']:+,.0f}")
    print(f"   Premium flow: calls ₹{pf['call_flow_cr']:+.1f} Cr | "
          f"puts ₹{pf['put_flow_cr']:+.1f} Cr")
    print(f"      net bullish premium pressure ₹{pf['net_cr']:+.1f} Cr"
          f"  (raw OI notional: call ₹{pf['call_notional_cr']:+.1f} Cr, "
          f"put ₹{pf['put_notional_cr']:+.1f} Cr)")

    if st:
        print(f"\n{C['c']}ATM {st['atm_strike']}{C['x']}  straddle "
              f"{st['straddle']:.1f} → expected range "
              f"{st['expected_low']:.0f} – {st['expected_high']:.0f}")
        print(f"   IV: CE {st['ce_iv']:.1f} / PE {st['pe_iv']:.1f}  "
              f"skew {st['iv_skew']:+.2f}"
              + ("  (puts bid up — downside fear)" if st["iv_skew"] > 1
                 else "  (calls bid up — upside chase)" if st["iv_skew"] < -1
                 else ""))

    print(f"\n{C['c']}WALLS{C['x']} (total OI)")
    print("   resistance: " + "  ".join(
        f"{s} ({o/1e5:.1f}L)" for s, o in snap["resistance_walls"]))
    print("   support   : " + "  ".join(
        f"{s} ({o/1e5:.1f}L)" for s, o in snap["support_walls"]))
    print(f"{C['c']}BUILT TODAY{C['x']}")
    print("   call OI added: " + "  ".join(
        f"{s} (+{o/1e5:.1f}L)" for s, o in snap["call_oi_added"]))
    print("   put  OI added: " + "  ".join(
        f"{s} (+{o/1e5:.1f}L)" for s, o in snap["put_oi_added"]))

    if "delta" in snap:
        d = snap["delta"]
        print(f"\n{C['c']}SINCE LAST POLL{C['x']}")
        print(f"   spot {d['spot']:+.2f}  PCR {d['pcr_band']:+.3f}  "
              f"max pain {d['max_pain']:+.0f}  straddle {d['straddle']:+.1f}")
        print(f"   {d['straddle_read']}")
        if d["condition_changed"]:
            print(f"   {C['y']}⚠ condition flipped: {d['prev_condition']} "
                  f"→ {snap['condition']}{C['x']}")

    print(f"\n{C['c']}AI READ{C['x']}\n{ai_text}")
    print("=" * w + "\n")


def push_telegram(text):
    if not config.TELEGRAM_ENABLED:
        return
    tok, chat = os.environ.get("TG_TOKEN"), os.environ.get("TG_CHAT_ID")
    if not (tok and chat):
        return
    try:
        import requests
        requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                      json={"chat_id": chat, "text": text[:4000]}, timeout=10)
    except Exception as exc:                             # noqa: BLE001
        log.warning("telegram push failed: %s", exc)


# ----------------------------------------------------------------------
# one cycle
# ----------------------------------------------------------------------
def one_cycle(client, args):
    now = datetime.now(IST)
    p = paths(now)
    hist = load_history(p["summary"])
    prev = hist[-1] if hist else None

    if args.offline:
        import selftest
        raw = selftest.synth(scenario=args.offline)
        log.info("OFFLINE MODE — synthetic '%s' chain, not live data", args.offline)
    else:
        raw = client.fetch_chain()
    df, meta = chain_parser.parse(raw, expiry=None if args.offline else args.expiry)
    snap = metrics.build_snapshot(df, meta, prev=prev)

    ai_text = ("[--no-ai]" if args.no_ai else
               ai_view.get_view(snap, premarket_score=args.score, history=hist))

    report(snap, ai_text, premarket=args.score)
    if args.dump_chain:
        band = metrics._band(df, meta["spot"], snap["band"])
        render.console_table(band, meta)
    if args.html:
        p_html = p["chain"].replace(".csv", ".html")
        render.html_table(metrics._band(df, meta["spot"], snap["band"]),
                          meta, p_html, snap=snap)
        log.info("verification view -> %s", p_html)

    # persist
    row = metrics.to_row(snap)
    pd.DataFrame([row]).to_csv(p["summary"], mode="a", header=not
                               os.path.exists(p["summary"]), index=False)
    if config.SAVE_FULL_CHAIN:
        df.to_csv(p["chain"], index=False)
    with open(p["ai"], "a") as fh:
        fh.write(f"\n===== {snap['nse_timestamp']} | spot {snap['spot']} | "
                 f"{snap['condition']} ({snap['param15_score']:+d}) =====\n"
                 f"{ai_text}\n")
    if args.json:
        print(json.dumps(snap, indent=2, default=str))

    push_telegram(f"{snap['symbol']} {snap['spot']} | {snap['condition']} "
                  f"({snap['param15_score']:+d}) | PCR {snap['pcr_band']:.2f} | "
                  f"MP {snap['max_pain']:.0f}\n\n{ai_text}")
    return snap


# ----------------------------------------------------------------------
def parse_hhmm(s):
    h, m = s.split(":")
    return dtime(int(h), int(m))


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--once", action="store_true")
    g.add_argument("--loop", action="store_true")
    ap.add_argument("--symbol", default=None)
    ap.add_argument("--expiry", default=None, help="DD-Mon-YYYY, default nearest")
    ap.add_argument("--score", type=float, default=None,
                    help="your pre-market 17-parameter score, for conflict rule")
    ap.add_argument("--minutes", type=int, default=config.POLL_MINUTES)
    ap.add_argument("--until", default=config.RUN_UNTIL)
    ap.add_argument("--no-ai", action="store_true")
    ap.add_argument("--dump-chain", action="store_true",
                    help="print the coloured chain in the terminal")
    ap.add_argument("--html", action="store_true",
                    help="write an HTML verification view to hold up against NSE")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--offline", choices=["bullish", "bearish", "range"],
                    default=None, help="synthetic chain, for testing any time")
    ap.add_argument("--ignore-clock", action="store_true",
                    help="run outside market hours (data will be stale)")
    args = ap.parse_args()

    if args.symbol:
        config.SYMBOL = args.symbol.upper()
    if args.no_ai:
        config.AI_ENABLED = False

    client = NSEClient()
    open_t, close_t = parse_hhmm(config.MARKET_OPEN), parse_hhmm(args.until)

    if args.once:
        one_cycle(client, args)
        return

    while True:
        now = datetime.now(IST)
        if not args.ignore_clock:
            if now.weekday() >= 5 or now.strftime("%Y-%m-%d") in config.NSE_HOLIDAYS_2026:
                log.info("market closed today — exiting")
                return
            if now.time() > close_t:
                log.info("past %s IST — done for the day", args.until)
                return
            if now.time() < open_t:
                wait = (datetime.combine(now.date(), open_t, IST) - now).total_seconds()
                log.info("pre-open, sleeping %.0f min till %s",
                         wait / 60, config.MARKET_OPEN)
                time.sleep(min(wait, 300))
                continue
        try:
            one_cycle(client, args)
        except Exception as exc:                          # noqa: BLE001
            log.error("cycle failed: %s: %s", type(exc).__name__, exc)
        time.sleep(args.minutes * 60)


if __name__ == "__main__":
    sys.exit(main())
