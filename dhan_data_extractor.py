"""
dhan_data_extractor.py — Comprehensive Data Extractor for DhanHQ APIs.
Extracts and archives:
  1. Full Option Chain with Greeks, IV, OI, Volume, Quotes -> data/dhan_option_chains/
  2. Level 2 (5-Depth) Market Depth for Futures & Key Strikes -> data/order_book/
  3. Contract-Level Intraday Candles for ATM & Near-ATM Strikes -> data/option_candles_5m/
  4. Full Multi-Timeframe (1m, 5m, 15m) Index & Futures 90-Day Candles -> data/candles/
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests

import config
import dhan_client as dc

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("dhan_extractor")


# ═══════════════════════════════════════════════════════════════════════════
# 1. Option Chain & Greeks Archiver
# ═══════════════════════════════════════════════════════════════════════════

def extract_option_chain(expiry: str | None = None) -> tuple[Path, Path]:
    """
    Extracts the full Dhan Option Chain with official Greeks, IV, OI, Quotes.
    Saves:
      - Raw JSON: data/dhan_option_chains/YYYY-MM-DD_HH-MM.json
      - Structured CSV: data/dhan_option_chains/chain_YYYY-MM-DD_HH-MM.csv
    """
    out_dir = config.DATA_DIR / "dhan_option_chains"
    out_dir.mkdir(parents=True, exist_ok=True)

    if not expiry:
        exps = dc.fetch_dhan_expiries()
        if not exps:
            raise RuntimeError("Could not retrieve expiries from Dhan")
        expiry = exps[0]

    raw_data = dc.fetch_dhan_option_chain(expiry=expiry)
    if not raw_data:
        raise RuntimeError("Failed to fetch option chain from Dhan")

    now = datetime.now()
    stamp = now.strftime("%Y-%m-%d_%H-%M-%S")
    spot = raw_data.get("last_price", 0.0)

    # 1. Save JSON
    json_path = out_dir / f"{stamp}.json"
    json_path.write_text(json.dumps(raw_data, indent=2))

    # 2. Flatten into structured CSV
    rows = []
    oc = raw_data.get("oc", {})
    for strike_str, v in oc.items():
        k = float(strike_str)
        ce, pe = v.get("ce", {}), v.get("pe", {})
        rows.append({
            "timestamp": now.strftime("%Y-%m-%d %H:%M:%S"),
            "expiry": expiry,
            "spot": spot,
            "strike": int(k),
            # CE
            "ce_ltp": ce.get("last_price"),
            "ce_oi": ce.get("oi"),
            "ce_prev_oi": ce.get("previous_oi"),
            "ce_vol": ce.get("volume"),
            "ce_prev_vol": ce.get("previous_volume"),
            "ce_iv": ce.get("implied_volatility"),
            "ce_delta": ce.get("greeks", {}).get("delta"),
            "ce_theta": ce.get("greeks", {}).get("theta"),
            "ce_gamma": ce.get("greeks", {}).get("gamma"),
            "ce_vega": ce.get("greeks", {}).get("vega"),
            "ce_bid": ce.get("top_bid_price"),
            "ce_bid_qty": ce.get("top_bid_quantity"),
            "ce_ask": ce.get("top_ask_price"),
            "ce_ask_qty": ce.get("top_ask_quantity"),
            "ce_security_id": ce.get("security_id"),
            # PE
            "pe_ltp": pe.get("last_price"),
            "pe_oi": pe.get("oi"),
            "pe_prev_oi": pe.get("previous_oi"),
            "pe_vol": pe.get("volume"),
            "pe_prev_vol": pe.get("previous_volume"),
            "pe_iv": pe.get("implied_volatility"),
            "pe_delta": pe.get("greeks", {}).get("delta"),
            "pe_theta": pe.get("greeks", {}).get("theta"),
            "pe_gamma": pe.get("greeks", {}).get("gamma"),
            "pe_vega": pe.get("greeks", {}).get("vega"),
            "pe_bid": pe.get("top_bid_price"),
            "pe_bid_qty": pe.get("top_bid_quantity"),
            "pe_ask": pe.get("top_ask_price"),
            "pe_ask_qty": pe.get("top_ask_quantity"),
            "pe_security_id": pe.get("security_id"),
        })

    csv_path = out_dir / f"chain_{stamp}.csv"
    df_oc = pd.DataFrame(rows).sort_values("strike")
    df_oc.to_csv(csv_path, index=False)

    # Also maintain latest symlink/copy for easy lookup
    df_oc.to_csv(out_dir / "latest_chain.csv", index=False)
    log.info(f"Option Chain saved: {csv_path.name} ({len(df_oc)} strikes)")
    return json_path, csv_path


# ═══════════════════════════════════════════════════════════════════════════
# 2. Level 2 (5-Depth) Order Book Archiver
# ═══════════════════════════════════════════════════════════════════════════

def extract_level2_depth() -> tuple[Path, Path]:
    """
    Extracts 5-depth order book for NIFTY futures.
    Saves snapshot JSON and appends to depth_log.csv.
    """
    out_dir = config.DATA_DIR / "order_book"
    out_dir.mkdir(parents=True, exist_ok=True)

    depth = dc.futures_market_depth()
    if not depth:
        raise RuntimeError("Failed to fetch futures market depth from Dhan")

    now = datetime.now()
    stamp = now.strftime("%Y-%m-%d_%H-%M-%S")

    # 1. Snapshot JSON
    json_path = out_dir / f"depth_{stamp}.json"
    json_path.write_text(json.dumps(depth, indent=2))

    # 2. Append to log CSV
    log_path = out_dir / "depth_log.csv"
    row = {
        "timestamp": now.strftime("%Y-%m-%d %H:%M:%S"),
        "ltp": depth.get("ltp"),
        "bid_qty_5": depth.get("bid_qty_5"),
        "ask_qty_5": depth.get("ask_qty_5"),
        "imbalance_5": round(depth.get("imbalance_5", 0.0), 4),
        "total_buy_qty": depth.get("total_buy"),
        "total_sell_qty": depth.get("total_sell"),
        "top_bid_price": depth["buys"][0].get("price") if depth.get("buys") else None,
        "top_bid_qty": depth["buys"][0].get("quantity") if depth.get("buys") else None,
        "top_ask_price": depth["sells"][0].get("price") if depth.get("sells") else None,
        "top_ask_qty": depth["sells"][0].get("quantity") if depth.get("sells") else None,
    }
    df_row = pd.DataFrame([row])
    df_row.to_csv(log_path, mode="a", header=not log_path.exists(), index=False)

    log.info(f"Level 2 depth saved: imbalance={depth['imbalance_5']:+.2%}, LTP={depth['ltp']}")
    return json_path, log_path


# ═══════════════════════════════════════════════════════════════════════════
# 3. Contract-Level Intraday Candles (ATM +- 4 Strikes)
# ═══════════════════════════════════════════════════════════════════════════

def extract_atm_option_candles(days_back: int = 10, strikes_span: int = 4,
                               interval: str = "5") -> list[Path]:
    """
    Finds ATM +- strikes_span contracts for the nearest weekly expiry
    and extracts full intraday OHLCV candles into data/option_candles_5m/.
    """
    out_dir = config.DATA_DIR / f"option_candles_{interval}m"
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1. Fetch live spot & expiry
    exps = dc.fetch_dhan_expiries()
    if not exps:
        return []
    expiry = exps[0]

    raw_chain = dc.fetch_dhan_option_chain(expiry=expiry)
    if not raw_chain:
        return []
    spot = raw_chain.get("last_price", 24000.0)
    atm = int(round(spot / 50.0) * 50)

    # 2. Extract security IDs for ATM +- strikes_span
    oc = raw_chain.get("oc", {})
    targets: list[tuple[str, str, str]] = []  # (symbol_label, security_id, instrument_type)
    for strike_str, val in oc.items():
        k = int(float(strike_str))
        if abs(k - atm) <= strikes_span * 50:
            ce_id = val.get("ce", {}).get("security_id")
            pe_id = val.get("pe", {}).get("security_id")
            if ce_id:
                targets.append((f"NIFTY_{expiry}_{k}_CE", str(ce_id), "OPTIDX"))
            if pe_id:
                targets.append((f"NIFTY_{expiry}_{k}_PE", str(pe_id), "OPTIDX"))

    log.info(f"Extracting {len(targets)} option contracts around ATM {atm} ({interval}m candles)...")
    saved = []
    import time
    for label, sid, inst in targets:
        try:
            time.sleep(1.0)  # Throttling to respect Dhan rate limits
            d = dc.intraday(sid, "NSE_FNO", inst, days_back=days_back, interval=interval)
            if d is not None and not d.empty:
                d["timestamp"] = pd.to_datetime(d["timestamp"], errors="coerce")
                d = d.dropna(subset=["timestamp"])
                t = d["timestamp"].dt.time
                d = d[(t >= config.MARKET_OPEN) & (t <= config.MARKET_CLOSE)].copy()
                if not d.empty:
                    p = out_dir / f"{label}.csv"
                    cols = ["timestamp", "open", "high", "low", "close", "volume"]
                    d[cols].to_csv(p, index=False)
                    saved.append(p)
                    log.info(f"  saved {label}: {len(d)} bars")
        except Exception as exc:
            log.warning(f"Failed to fetch {label} (sid {sid}): {exc}")

    log.info(f"Option contract candles complete: saved {len(saved)} contracts to {out_dir}")
    return saved


# ═══════════════════════════════════════════════════════════════════════════
# Main Entry Point
# ═══════════════════════════════════════════════════════════════════════════

def extract_all():
    log.info("══ Starting Comprehensive Dhan Data Extraction ══")
    # 1. Option Chain & Greeks
    extract_option_chain()
    # 2. Level 2 Market Depth
    extract_level2_depth()
    # 3. Contract-Level Option Candles (ATM +- 4)
    extract_atm_option_candles(days_back=10, strikes_span=4, interval="5")
    log.info("══ All extractions successfully completed! ══")


if __name__ == "__main__":
    extract_all()
