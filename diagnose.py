"""
NSE connectivity diagnostic.

    python diagnose.py

Run this when the dashboard says "could not fetch the chain". It reports the
ACTUAL cause instead of a swallowed retry count: your public IP, whether the
cookie bootstrap works, and the exact status code from every candidate
endpoint. Paste the output back and the fix is usually one line.

Nothing here is cached and nothing is written to disk.
"""
from __future__ import annotations

import json
import sys
import textwrap

import requests

import config

HEADERS = {
    "User-Agent": config.USER_AGENT,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",   # not br - see nse_client.py
    "Connection": "keep-alive",
}

# Every option-chain endpoint NSE has used. v3 is the current one; the older
# two are kept so the diagnostic can tell you which still answers.
CANDIDATES = [
    ("contract-info (expiry list)",
     "https://www.nseindia.com/api/option-chain-contract-info",
     {"symbol": "NIFTY"}),
    ("v3 indices",
     "https://www.nseindia.com/api/option-chain-v3",
     {"type": "Indices", "symbol": "NIFTY"}),
    ("legacy option-chain-indices",
     "https://www.nseindia.com/api/option-chain-indices",
     {"symbol": "NIFTY"}),
]


def line(ch="-", n=70):
    print(ch * n)


def show(label, ok, detail=""):
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}")
    if detail:
        print(textwrap.indent(detail, "         "))


def main() -> int:
    print()
    line("=")
    print("NSE CONNECTIVITY DIAGNOSTIC")
    line("=")
    print(f"User-Agent: {config.USER_AGENT[:70]}...")
    print()

    # ── 1. public IP ────────────────────────────────────────────────────────
    print("1. Your public IP")
    line()
    try:
        ip = requests.get("https://api.ipify.org?format=json", timeout=8).json()["ip"]
        show(f"public IP is {ip}", True)
        try:
            info = requests.get(f"https://ipapi.co/{ip}/json/", timeout=8).json()
            org = info.get("org", "?")
            print(f"         network: {org}")
            print(f"         country: {info.get('country_name','?')}")
            hosting_words = ("amazon", "aws", "google", "azure", "digitalocean",
                             "linode", "vultr", "oracle", "hetzner", "ovh", "cloud")
            if any(w in org.lower() for w in hosting_words):
                print("         >>> This looks like a DATACENTER network. NSE blocks these.")
            if info.get("country_name") and info["country_name"] != "India":
                print("         >>> Not an Indian IP. If you are on a VPN, turn it off.")
        except Exception:
            pass
    except Exception as exc:
        show("could not determine public IP", False, str(exc))
    print()

    # ── 2. reach nseindia.com at all ────────────────────────────────────────
    print("2. Can we reach nseindia.com")
    line()
    s = requests.Session()
    s.headers.update(HEADERS)
    try:
        r = s.get("https://www.nseindia.com", timeout=config.REQUEST_TIMEOUT)
        show(f"homepage responded {r.status_code}", r.status_code == 200,
             f"cookies now held: {len(s.cookies)}")
    except Exception as exc:
        show("homepage unreachable", False, f"{type(exc).__name__}: {exc}")
        print("\n  DNS or firewall problem, not an NSE problem. Check your connection.")
        return 1
    print()

    # ── 3. cookie bootstrap via the option-chain page ───────────────────────
    print("3. Cookie bootstrap (option-chain page)")
    line()
    try:
        r = s.get(config.CHAIN_PAGE, timeout=config.REQUEST_TIMEOUT)
        names = sorted(c.name for c in s.cookies)
        show(f"option-chain page responded {r.status_code}", r.status_code == 200,
             f"cookies: {', '.join(names) if names else '(none)'}")
        if not any(n.startswith("nsit") or n.startswith("nseappid") for n in names):
            print("         >>> Missing nsit / nseappid. The API will reject you.")
    except Exception as exc:
        show("bootstrap failed", False, f"{type(exc).__name__}: {exc}")
    print()

    # ── 4. try every candidate endpoint ─────────────────────────────────────
    print("4. Endpoint test")
    line()
    s.headers["Referer"] = config.CHAIN_PAGE
    working: list[str] = []

    for label, url, params in CANDIDATES:
        try:
            r = s.get(url, params=params, timeout=config.REQUEST_TIMEOUT)
            ct = r.headers.get("Content-Type", "?")
            ok = r.status_code == 200 and "json" in ct.lower()
            detail = f"status {r.status_code} · content-type {ct} · {len(r.content):,} bytes"
            if ok:
                try:
                    payload = r.json()
                    keys = list(payload.keys())[:6]
                    detail += f"\ntop-level keys: {keys}"
                    if "records" in payload:
                        rec = payload["records"]
                        detail += f"\nrecords keys: {list(rec.keys())[:6]}"
                        if "expiryDates" in rec:
                            detail += f"\nexpiries: {rec['expiryDates'][:3]}"
                        if "underlyingValue" in rec:
                            detail += f"\nunderlyingValue: {rec['underlyingValue']}"
                        if "data" in rec:
                            detail += f"\nrows: {len(rec['data'])}"
                    if "expiryDates" in payload:
                        detail += f"\nexpiries: {payload['expiryDates'][:3]}"
                    working.append(label)
                except Exception as exc:
                    ok = False
                    detail += f"\nJSON parse failed: {exc}"
            else:
                detail += f"\nbody starts: {r.text[:160]!r}"
            show(label, ok, detail)
        except Exception as exc:
            show(label, False, f"{type(exc).__name__}: {exc}")
        print()

    # ── 5. v3 with an actual expiry (it returns empty without one) ──────────
    print("5. v3 with an expiry parameter")
    line()
    try:
        info = s.get("https://www.nseindia.com/api/option-chain-contract-info",
                     params={"symbol": "NIFTY"}, timeout=config.REQUEST_TIMEOUT)
        expiries = info.json().get("expiryDates") or []
        if not expiries:
            show("no expiries from contract-info", False)
        else:
            exp = expiries[0]
            print(f"         using nearest expiry: {exp}")
            for key in ("expiry", "expiryDate"):
                r = s.get("https://www.nseindia.com/api/option-chain-v3",
                          params={"type": "Indices", "symbol": "NIFTY", key: exp},
                          timeout=config.REQUEST_TIMEOUT)
                detail = f"status {r.status_code} · {len(r.content):,} bytes"
                rows = 0
                try:
                    pl = r.json()
                    rows = len(pl.get("records", {}).get("data")
                               or pl.get("filtered", {}).get("data") or [])
                    detail += f"\ntop-level keys: {list(pl)[:6]}\nrows: {rows}"
                    uv = pl.get("records", {}).get("underlyingValue")
                    if uv:
                        detail += f"\nunderlyingValue: {uv}"
                except Exception as exc:
                    detail += f"\nJSON parse failed: {exc}"
                if rows:
                    working.append(f"v3 with {key}=")
                show(f"v3 using '{key}' parameter", bool(rows), detail)
    except Exception as exc:
        show("v3 expiry probe failed", False, f"{type(exc).__name__}: {exc}")
    print()

    # ── 6. market context endpoints ─────────────────────────────────────────
    print("6. India VIX and FII/DII endpoints")
    line()
    for label, path, params in (
        ("India VIX (allIndices)", "/api/allIndices", {}),
        ("FII/DII", "/api/fiidiiTradeReact", {}),
    ):
        try:
            r = s.get("https://www.nseindia.com" + path, params=params,
                      timeout=config.REQUEST_TIMEOUT)
            detail = f"status {r.status_code} · {len(r.content):,} bytes"
            ok = False
            try:
                pl = r.json()
                rows = pl if isinstance(pl, list) else pl.get("data", [])
                detail += f"\nrows: {len(rows)}"
                if "VIX" in label:
                    hit = [x for x in rows
                           if "VIX" in str(x.get("index", x.get("indexSymbol",""))).upper()]
                    if hit:
                        detail += f"\nINDIA VIX = {hit[0].get('last', hit[0].get('lastPrice'))}"
                        ok = True
                elif rows:
                    detail += f"\nfirst row keys: {list(rows[0])[:6]}"
                    ok = True
            except Exception as exc:
                detail += f"\nJSON parse failed: {exc}"
            show(label, ok, detail)
        except Exception as exc:
            show(label, False, f"{type(exc).__name__}: {exc}")
    print()

    # ── 7. NSE futures quote — the free VWAP candidate ──────────────────────
    print("7. NSE derivatives quote (free futures volume / VWAP)")
    line()
    try:
        r = s.get("https://www.nseindia.com/api/quote-derivative",
                  params={"symbol": "NIFTY"}, timeout=config.REQUEST_TIMEOUT)
        if r.status_code != 200:
            show("quote-derivative", False,
                 f"status {r.status_code} · body {r.text[:120]!r}")
        else:
            pl = r.json()
            top = list(pl)[:8]
            detail = f"status 200 · {len(r.content):,} bytes\ntop-level keys: {top}"
            stocks = pl.get("stocks") or []
            detail += f"\ncontracts returned: {len(stocks)}"

            # find the nearest-expiry FUTIDX contract
            fut = None
            for st_ in stocks:
                meta = st_.get("metadata", {})
                if str(meta.get("instrumentType", "")).upper().startswith("INDEX FUT") \
                   or "FUT" in str(meta.get("instrumentType", "")).upper():
                    fut = st_
                    break
            if fut:
                meta = fut.get("metadata", {})
                detail += (f"\n\nnearest futures contract:"
                           f"\n  expiry      {meta.get('expiryDate')}"
                           f"\n  last price  {meta.get('lastPrice')}"
                           f"\n  volume      {meta.get('numberOfContractsTraded')}"
                           f"\n  metadata keys: {list(meta)[:10]}")
                ti = (fut.get("marketDeptOrderBook", {}) or {}).get("tradeInfo", {})
                if ti:
                    detail += f"\n  tradeInfo keys: {list(ti)}"
                    for k in ti:
                        if "vmap" in k.lower() or "vwap" in k.lower() or "average" in k.lower():
                            detail += f"\n  >>> {k} = {ti[k]}   <-- EXCHANGE VWAP"
                    for k in ("tradedVolume", "totalTradedVolume", "value",
                              "openInterest", "changeinOpenInterest"):
                        if k in ti:
                            detail += f"\n  {k} = {ti[k]}"
                else:
                    detail += "\n  no marketDeptOrderBook.tradeInfo block"
                show("futures contract found", True, detail)
            else:
                types_seen = {str(x.get("metadata", {}).get("instrumentType"))
                              for x in stocks}
                show("no futures contract identified", False,
                     detail + f"\ninstrumentTypes seen: {sorted(types_seen)[:8]}")
    except Exception as exc:
        show("quote-derivative failed", False, f"{type(exc).__name__}: {exc}")
    print()

    # ── 8. NSE intraday price data — the free price-action candidate ────────
    print("8. NSE intraday price series (free price action)")
    line()

    # (a) the endpoint that draws the chart on NSE's own index pages
    try:
        r = s.get("https://www.nseindia.com/api/chart-databyindex",
                  params={"index": "NIFTY 50", "indices": "true"},
                  timeout=config.REQUEST_TIMEOUT)
        detail = f"status {r.status_code} · {len(r.content):,} bytes"
        ok = False
        if r.status_code == 200:
            try:
                pl = r.json()
                detail += f"\ntop-level keys: {list(pl)[:8]}"
                gd = pl.get("grapthData") or pl.get("graphData") or []
                detail += f"\npoints: {len(gd)}"
                if gd:
                    import datetime as _dt
                    first, last = gd[0], gd[-1]
                    def fmt(pt):
                        try:
                            t = _dt.datetime.fromtimestamp(pt[0] / 1000)
                            return f"{t:%d-%b %H:%M} -> {pt[1]}"
                        except Exception:
                            return str(pt)[:40]
                    detail += f"\nfirst point: {fmt(first)}"
                    detail += f"\nlast  point: {fmt(last)}"
                    detail += f"\npoint shape: {type(first).__name__} len={len(first)}"
                    detail += ("\n>>> This is a PRICE SERIES (timestamp, value), not OHLC."
                               "\n    Candles can be built by resampling it.")
                    ok = True
            except Exception as exc:
                detail += f"\nJSON parse failed: {exc}"
        else:
            detail += f"\nbody: {r.text[:120]!r}"
        show("chart-databyindex (NIFTY 50)", ok, detail)
    except Exception as exc:
        show("chart-databyindex", False, f"{type(exc).__name__}: {exc}")
    print()

    # (b) the dedicated charting host — proper OHLC if it answers
    for label, url, params in (
        ("charting host, index OHLC",
         "https://charting.nseindia.com/Charts/GetSymbolIntradayData",
         {"symbol": "NIFTY 50", "type": "index"}),
        ("charting host, chart data",
         "https://charting.nseindia.com/Charts/ChartData",
         {"index": "NIFTY 50"}),
    ):
        try:
            r = s.get(url, params=params, timeout=config.REQUEST_TIMEOUT)
            detail = (f"status {r.status_code} · "
                      f"content-type {r.headers.get('Content-Type','?')} · "
                      f"{len(r.content):,} bytes")
            ok = False
            if r.status_code == 200 and r.content:
                try:
                    pl = r.json()
                    keys = list(pl)[:10] if isinstance(pl, dict) else f"list[{len(pl)}]"
                    detail += f"\nshape: {keys}"
                    ok = True
                except Exception:
                    detail += f"\nnon-JSON, starts: {r.text[:100]!r}"
            else:
                detail += f"\nbody: {r.text[:100]!r}"
            show(label, ok, detail)
        except Exception as exc:
            show(label, False, f"{type(exc).__name__}: {exc}")
    print()

    # ── verdict ─────────────────────────────────────────────────────────────
    line("=")
    if working:
        print(f"WORKING ENDPOINT(S): {', '.join(working)}")
        print("\nSet API_STYLE in config.py to match and you are done.")
    else:
        print("NO ENDPOINT ANSWERED.")
        print(textwrap.dedent("""
            Most likely causes, in order:
              1. VPN or proxy is on           -> turn it off, rerun
              2. Running on a VPS / cloud box -> must be a home connection
              3. Corporate or ISP DNS filter  -> try a phone hotspot to test
              4. NSE changed the API again    -> paste this output back
        """))
    line("=")
    return 0 if working else 1


if __name__ == "__main__":
    sys.exit(main())
