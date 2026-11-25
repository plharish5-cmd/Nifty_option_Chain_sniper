"""Per-minute NIFTY live-sheet rows — display only; uses writer-bias primitives."""
from __future__ import annotations

import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

IST = timezone(timedelta(hours=5, minutes=30))
_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


def _top_active(rows: List[Dict[str, Any]], n: int = 5) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for row in rows or []:
        strike = row.get("strike")
        for side, key in (("PE", "pe"), ("CE", "ce")):
            leg = row.get(key) or {}
            oi = leg.get("oi")
            if oi is None:
                continue
            items.append(
                {
                    "side": side,
                    "strike": strike,
                    "oi": oi,
                    "label": f"{strike:g} {side}",
                    "tone": "pe_writer" if side == "PE" else "ce_writer",
                }
            )
    items.sort(key=lambda x: float(x["oi"] or 0), reverse=True)
    return items[:n]


def _chain_maps(ch: Dict[str, Any]) -> Dict[float, Dict[str, float]]:
    out: Dict[float, Dict[str, float]] = {}
    for row in ch.get("rows") or []:
        k = float(row["strike"])
        ce, pe = row.get("ce") or {}, row.get("pe") or {}
        out[k] = {
            "ce_oi": float(ce.get("oi") or 0),
            "pe_oi": float(pe.get("oi") or 0),
            "ce_ltp": float(ce.get("ltp") or 0),
            "pe_ltp": float(pe.get("ltp") or 0),
            "ce_vol": float(ce.get("vol") or 0),
            "pe_vol": float(pe.get("vol") or 0),
        }
    return out


def _winner(net: Optional[float]) -> str:
    if net is None or net == 0:
        return "BALANCED"
    return "PUT WRITERS" if net > 0 else "CALL WRITERS"


def _maps_to_fake_chain(maps: Dict[float, Dict[str, float]], fetched_at: float, expiry: str, spot: float) -> Dict[str, Any]:
    rows: List[Dict[str, Any]] = []
    for strike, v in (maps or {}).items():
        rows.append(
            {
                "strike": strike,
                "ce": {"oi": v.get("ce_oi"), "oi_chg": 0, "ltp": v.get("ce_ltp"), "vol": v.get("ce_vol")},
                "pe": {"oi": v.get("pe_oi"), "oi_chg": 0, "ltp": v.get("pe_ltp"), "vol": v.get("pe_vol")},
            }
        )
    return {"fetched_at": fetched_at, "rows": rows, "expiry": expiry, "spot": spot}


def _writer_bias_from_chains(
    now_ch: Dict[str, Any],
    prev_ch: Optional[Dict[str, Any]],
    *,
    day: str,
    spot: float,
    scanner_cfg: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    out = {"score": None, "regime": "unavailable", "slope": None, "direction": None}
    if not prev_ch:
        return out
    try:
        from analyst.writer_bias_live_reads_8510 import apply_writer_bias_to_board_reads

        t1 = int(float(now_ch.get("fetched_at") or 0))
        t0 = int(float(prev_ch.get("fetched_at") or t1 - 60))
        by_ts = {t0: _chain_maps(prev_ch), t1: _chain_maps(now_ch)}
        wb = apply_writer_bias_to_board_reads(
            by_ts=by_ts,
            ts_sorted=sorted(by_ts),
            spot=spot,
            band=1000.0,
            day=day,
            now_ts=t1,
            last_t=t1,
            age_sec=0,
            scanner_cfg=scanner_cfg,
            momentum_oi={},
            expiry=str(now_ch.get("expiry") or ""),
        )
        day = wb.get("day_oi") or {}
        out["score"] = day.get("writer_bias_score")
        out["regime"] = str(day.get("writer_bias_regime") or "unavailable").upper()
        out["slope"] = day.get("writer_bias_slope")
        out["direction"] = day.get("direction")
    except Exception:
        pass
    return out


def build_minute_row(
    ch: Dict[str, Any],
    *,
    prev_ch: Optional[Dict[str, Any]] = None,
    minute: str,
    market_open: bool,
    stale_after: float,
    advance_decline: Optional[Dict[str, Any]] = None,
    futures: Optional[Dict[str, Any]] = None,
    scanner_cfg: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    stamp = ch.get("fetched_at")
    now = float(stamp) if isinstance(stamp, (int, float)) else None
    age = None if now is None else max(0.0, datetime.now(IST).timestamp() - now)
    fresh = market_open and age is not None and 0 <= age <= stale_after and not ch.get("is_stale")
    rows = ch.get("rows") or []
    spot = ch.get("spot")
    net_pe = net_ce = vol_pe = vol_ce = 0.0
    for row in rows:
        ce, pe = row.get("ce") or {}, row.get("pe") or {}
        co, po = ce.get("oi_chg"), pe.get("oi_chg")
        if co is not None:
            net_ce += float(co)
        if po is not None:
            net_pe += float(po)
        vol_ce += float(ce.get("vol") or 0)
        vol_pe += float(pe.get("vol") or 0)
    net_raw = net_pe - net_ce
    ad = advance_decline or {}
    ad_fields = {
        "nifty_ad": ad.get("nifty") or "unavailable",
        "banknifty_ad": ad.get("banknifty") or "unavailable",
        "mid_ad": ad.get("mid") or "unavailable",
        "all_nse_ad": ad.get("all_nse") or "unavailable",
    }
    fut = futures or {}
    day = datetime.fromtimestamp(now or datetime.now(IST).timestamp(), IST).strftime("%Y-%m-%d")
    wb = _writer_bias_from_chains(ch, prev_ch, day=day, spot=float(spot or 0), scanner_cfg=scanner_cfg)
    return {
        "time": minute,
        "spot": spot,
        **ad_fields,
        "most_active_oi_top5": _top_active(rows, 5),
        "net_delta_oi_raw": round(net_raw, 0) if rows else None,
        "ce_vol_minus_pe_vol": round(vol_ce - vol_pe, 0) if rows else None,
        "futures_oi": fut.get("oi"),
        "futures_ltp": fut.get("ltp"),
        "winner_min": _winner(net_raw if rows else None),
        "writer_bias_score": wb.get("score"),
        "writer_bias_regime": wb.get("regime"),
        "writer_bias_slope": wb.get("slope"),
        "feed_state": "LIVE" if fresh else "STALE",
        "age_sec": round(age) if age is not None else None,
        "observed_at": now,
        "expiry": ch.get("expiry"),
    }


def minute_row_csv_header() -> List[str]:
    return [
        "time",
        "spot",
        "nifty_ad",
        "banknifty_ad",
        "mid_ad",
        "all_nse_ad",
        "net_delta_oi_raw",
        "ce_vol_minus_pe_vol",
        "futures_oi",
        "futures_ltp",
        "winner_min",
        "writer_bias_score",
        "writer_bias_regime",
        "writer_bias_slope",
        "most_active_oi_top5",
        "feed_state",
        "age_sec",
    ]


def minute_row_to_csv_cells(row: Dict[str, Any]) -> List[str]:
    top = "; ".join(f"{x.get('label')}({x.get('oi')})" for x in (row.get("most_active_oi_top5") or []))
    def cell(k: str) -> str:
        v = row.get(k)
        return "" if v is None else str(v)

    return [
        cell("time"),
        cell("spot"),
        cell("nifty_ad"),
        cell("banknifty_ad"),
        cell("mid_ad"),
        cell("all_nse_ad"),
        cell("net_delta_oi_raw"),
        cell("ce_vol_minus_pe_vol"),
        cell("futures_oi"),
        cell("futures_ltp"),
        cell("winner_min"),
        cell("writer_bias_score"),
        cell("writer_bias_regime"),
        cell("writer_bias_slope"),
        top,
        cell("feed_state"),
        cell("age_sec"),
    ]