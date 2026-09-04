"""
Dump the actual structure of the NSE index-tracker responses.

    python dump_nse.py

No guessing. It walks whatever comes back and prints the shape — keys, types,
lengths and a couple of sample values at each level — so the parser can be
written against reality instead of against my assumptions.

Writes the raw JSON to data/nse_dump_*.json too, in case a closer look helps.
"""
from __future__ import annotations

import json
import sys

import config


def shape(obj, path="", depth=0, max_depth=5, out=None):
    """Recursively describe a JSON structure without printing all of it."""
    out = out if out is not None else []
    pad = "  " * depth

    if isinstance(obj, dict):
        out.append(f"{pad}{path or '<root>'}  dict, {len(obj)} keys: "
                   f"{list(obj)}")
        if depth < max_depth:
            for k, v in obj.items():
                shape(v, f"{path}.{k}" if path else k, depth + 1, max_depth, out)

    elif isinstance(obj, list):
        out.append(f"{pad}{path}  list, {len(obj)} items")
        if obj and depth < max_depth:
            out.append(f"{pad}  [first item]")
            shape(obj[0], f"{path}[0]", depth + 1, max_depth, out)
            if len(obj) > 1:
                out.append(f"{pad}  [last item]")
                shape(obj[-1], f"{path}[-1]", depth + 1, max_depth, out)

    else:
        v = str(obj)
        if len(v) > 60:
            v = v[:60] + "..."
        out.append(f"{pad}{path} = {v}   ({type(obj).__name__})")

    return out


def dump_url(url: str) -> int:
    """
    Dump any URL's JSON structure, using the scanner's NSE cookie session.

        python dump_nse.py "https://charting.nseindia.com/Charts/history?symbol=NIFTY&resolution=1&from=1787100000&to=1787150000"

    Use this whenever you capture a new endpoint from the browser Network tab.
    Beats waiting for me to write a bespoke parser — paste the output and the
    shape is known immediately.
    """
    import nse_client
    client = nse_client.NSEClient()
    try:
        client._bootstrap()
    except Exception as exc:                                # noqa: BLE001
        print("bootstrap failed:", exc)
        return 1

    print("=" * 72)
    print(url)
    print("=" * 72)
    try:
        r = client.session.get(url, timeout=config.REQUEST_TIMEOUT)
        print(f"HTTP {r.status_code} · {len(r.content):,} bytes · "
              f"type {r.headers.get('Content-Type','?')} · "
              f"enc {r.headers.get('Content-Encoding','none')}")
        if r.status_code != 200:
            print("body:", r.text[:400])
            return 1
        try:
            pl = r.json()
        except ValueError:
            print("not JSON. starts:", r.text[:400])
            return 1

        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        out = config.DATA_DIR / "nse_dump_custom.json"
        out.write_text(json.dumps(pl, indent=2)[:400_000])
        print(f"raw json saved to {out}\n")

        for ln in shape(pl):
            print(ln)

        # TradingView UDF responses are parallel arrays — detect and describe
        if isinstance(pl, dict) and {"t", "o", "h", "l", "c"} <= set(pl):
            n = len(pl["t"])
            print(f"\n>>> TradingView UDF format detected: {n} candles")
            print(f"    status: {pl.get('s')}")
            has_v = "v" in pl and any(pl["v"])
            print(f"    volume present and non-zero: {has_v}")
            if n:
                import datetime as _dt
                for label, i in (("first", 0), ("last", n - 1)):
                    t = pl["t"][i]
                    ts = _dt.datetime.utcfromtimestamp(t if t < 1e11 else t / 1000)
                    print(f"    {label}: {ts:%d-%b %H:%M}  "
                          f"O{pl['o'][i]} H{pl['h'][i]} L{pl['l'][i]} C{pl['c'][i]}"
                          + (f" V{pl['v'][i]}" if "v" in pl else ""))
                real_wicks = sum(1 for i in range(n) if pl["h"][i] != pl["l"][i])
                print(f"    candles with real wicks: {real_wicks}/{n} "
                      f"({real_wicks/n*100:.0f}%)")
                if has_v:
                    print("    >>> THIS IS THE GOOD SOURCE — real OHLC and real")
                    print("        volume means real wicks on 1m AND real VWAP.")
    except Exception as exc:                                # noqa: BLE001
        print(f"FAILED: {type(exc).__name__}: {exc}")
        return 1
    return 0


def main() -> int:
    if len(sys.argv) > 1:
        return dump_url(sys.argv[1])
    import nse_client
    client = nse_client.NSEClient()
    try:
        client._bootstrap()
    except Exception as exc:                                # noqa: BLE001
        print("bootstrap failed:", exc)
        return 1

    config.DATA_DIR.mkdir(parents=True, exist_ok=True)

    targets = [
        ("getIndexChart_1D", {"functionName": "getIndexChart",
                              "index": config.INDEX_NAME, "flag": "1D"}),
        ("getIndexData", {"functionName": "getIndexData",
                          "index": config.INDEX_NAME}),
    ]

    for name, params in targets:
        print("\n" + "=" * 72)
        print(name)
        print("=" * 72)
        try:
            r = client._get(config.INDEX_TRACKER_API, params)
            print(f"HTTP {r.status_code} · {len(r.content):,} bytes")
            if r.status_code != 200:
                print("body:", r.text[:300])
                continue
            pl = r.json()

            path = config.DATA_DIR / f"nse_dump_{name}.json"
            path.write_text(json.dumps(pl, indent=2)[:400_000])
            print(f"raw json saved to {path}\n")

            for ln in shape(pl):
                print(ln)

            # If a long numeric series is in there, find and characterise it —
            # that is what candles get built from.
            print("\n--- hunting for a time series ---")
            found = False

            def hunt(o, path=""):
                nonlocal found
                if isinstance(o, list) and len(o) > 20:
                    kinds = {type(x).__name__ for x in o[:5]}
                    print(f"  {path}: list of {len(o)} ({kinds})")
                    print(f"    first: {str(o[0])[:120]}")
                    print(f"    last : {str(o[-1])[:120]}")
                    found = True
                elif isinstance(o, dict):
                    for k, v in o.items():
                        hunt(v, f"{path}.{k}" if path else k)
                elif isinstance(o, list):
                    for i, v in enumerate(o[:3]):
                        hunt(v, f"{path}[{i}]")

            hunt(pl)
            if not found:
                print("  no list longer than 20 items found")

        except Exception as exc:                            # noqa: BLE001
            print(f"FAILED: {type(exc).__name__}: {exc}")

    print("\n" + "=" * 72)
    print("Paste the output above and the parser gets written against reality.")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
