"""
Dhan connectivity diagnostic.

    python diagnose_dhan.py

Checks the token, the index candle feed, the futures lookup and whether VWAP
is actually computable. Run this before trusting the Price action tab.

I could not test the Dhan calls from my build environment — it has no route to
dhan.co — so this script exists to tell you exactly what came back rather than
leaving you with a vague failure. Paste the output if anything fails.
"""
from __future__ import annotations

import sys
import textwrap

import config


def line(ch="-", n=70):
    print(ch * n)


def show(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    if detail:
        print(textwrap.indent(str(detail), "         "))


def main() -> int:
    print()
    line("=")
    print("DHAN CONNECTIVITY DIAGNOSTIC")
    line("=")

    # ── 1. token ────────────────────────────────────────────────────────────
    print("\n1. Access token")
    line()
    try:
        import dhan_client as dc
        t = dc.token()
        ok, msg = dc.verify_token()
        if ok:
            show(f"token found and working ({len(t)} chars, ends ...{t[-6:]})", True, msg)
        else:
            show(f"token found ({len(t)} chars, ends ...{t[-6:]}) but verification failed", False, msg)
    except Exception as exc:                                # noqa: BLE001
        show("no usable token", False, exc)
        return 1

    # ── 2. index candles ────────────────────────────────────────────────────
    print("\n2. NIFTY 50 index candles (IDX_I / 13)")
    line()
    idx = None
    try:
        idx = dc.nifty_index_candles(days_back=1)
        if idx.empty:
            show("call succeeded but returned no candles", False,
                 "Outside market hours this can be normal — retry during a session.")
        else:
            show(f"{len(idx)} one-minute candles", True,
                 f"{idx.timestamp.min():%d-%b %H:%M} to {idx.timestamp.max():%d-%b %H:%M}\n"
                 f"last close {idx.close.iloc[-1]:,.2f}\n"
                 f"volume sum {idx.volume.sum():,.0f}  "
                 f"(ZERO IS CORRECT — an index has no volume)")
    except Exception as exc:                                # noqa: BLE001
        show("index candles failed", False, exc)

    # ── 3. futures lookup ───────────────────────────────────────────────────
    print("\n3. NIFTY futures security ID (needed for VWAP)")
    line()
    sid = None
    try:
        sid = dc.nifty_futures_security_id(force=True)
        if sid:
            show(f"resolved security ID {sid}", True,
                 "Looked up from Dhan's scrip master — it changes every expiry, "
                 "so it is resolved rather than hardcoded.")
        else:
            show("could not resolve a futures contract", False,
                 "VWAP will show as unavailable. Everything else still works.")
    except Exception as exc:                                # noqa: BLE001
        show("futures lookup failed", False, exc)

    # ── 4. futures candles / VWAP viability ─────────────────────────────────
    print("\n4. Futures candles and VWAP")
    line()
    if sid:
        try:
            fut = dc.nifty_futures_candles(days_back=1)
            if fut is None or fut.empty:
                show("no futures candles returned", False)
            else:
                vol = fut.volume.sum()
                show(f"{len(fut)} candles, volume {vol:,.0f}", vol > 0,
                     f"{fut.timestamp.min():%d-%b %H:%M} to "
                     f"{fut.timestamp.max():%d-%b %H:%M}\n"
                     f"last close {fut.close.iloc[-1]:,.2f}\n"
                     + ("VWAP is computable." if vol > 0 else
                        "Volume is zero — VWAP will not be available."))
        except Exception as exc:                            # noqa: BLE001
            show("futures candles failed", False, exc)
    else:
        show("skipped — no futures ID", False)

    # ── 5. full pipeline ────────────────────────────────────────────────────
    print("\n5. Indicator pipeline")
    line()
    if idx is not None and not idx.empty:
        try:
            import price_action as pa
            fut = dc.nifty_futures_candles(days_back=1) if sid else None
            frames = pa.build_all(idx, fut)
            for tf, d in frames.items():
                r = pa.trend_read(d, tf)
                print(f"  {tf:>4}  {len(d):>4} bars  "
                      f"{'FORMING' if r.forming else 'closed '}  {r.summary}")
            v, why = pa.cross_check(
                {tf: pa.trend_read(d, tf) for tf, d in frames.items()}, "RANGEBOUND")
            print(f"\n  cross-check sample: {v}")
            p = pa.store_candles(idx)
            print(f"  candles stored to: {p}")
            show("pipeline ran end to end", True)
        except Exception as exc:                            # noqa: BLE001
            show("pipeline failed", False, exc)
    else:
        show("skipped — no candles to work with", False)

    line("=")
    print("If step 2 passed you have EMAs and structure. If step 4 also")
    print("passed you have real VWAP. Anything else, paste this output.")
    line("=")
    return 0


if __name__ == "__main__":
    sys.exit(main())
