"""Live writer-bias aggregates for the 8510 desk board (poll vs session baselines).

Uses analyst.writer_bias_primitives_8510 only; no orders or protected imports.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from analyst.writer_bias_primitives_8510 import aggregate, buildup, slope

IST = timezone(timedelta(hours=5, minutes=30))
_RULES_PATH = Path(__file__).with_name("live_sheet_rules.json")

DEFAULT_WRITER_BIAS_CFG: Dict[str, Any] = {
    "oi_window_strikes": 6,
    "oi_regime_band": 0.20,
    "oi_slope_window_sec": 180,
    "oi_use_delta_weight": True,
    "oi_use_premium_weight": True,
    "oi_writing_requires_price_confirm": True,
}

_SCORE_HISTORY: Dict[str, List[Tuple[int, float, str]]] = {}


def load_writer_bias_cfg(scanner_cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    out = dict(DEFAULT_WRITER_BIAS_CFG)
    try:
        raw = json.loads(_RULES_PATH.read_text())
        wb = raw.get("writer_bias") if isinstance(raw, dict) else {}
        if isinstance(wb, dict):
            out.update(wb)
    except Exception:
        pass
    extra = (scanner_cfg or {}).get("writer_bias")
    if isinstance(extra, dict):
        out.update({k: v for k, v in extra.items() if not str(k).startswith("_")})
    return out


def _hm_ts(day: str, hm: str) -> int:
    h, m = (int(x) for x in hm.split(":"))
    return int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=IST, hour=h, minute=m).timestamp())


def _direction_from_regime(regime: Optional[str]) -> Optional[str]:
    # BULLISH (PE-writers dominant = support) favours a CE entry; BEARISH favours PE.
    # This was reversed (BULLISH->PE), which blocked the correct side and contradicted
    # fresh_oi_regime_to_direction. Aligned here so all sources agree.
    if regime == "BULLISH":
        return "CE"
    if regime == "BEARISH":
        return "PE"
    if regime == "NEUTRAL":
        return "NEUTRAL"
    return None


def _estimate_delta(spot: float, strike: float, ltp: float, call: bool, t_years: float, r: float) -> float:
    if spot <= 0 or strike <= 0 or ltp <= 0 or t_years <= 0:
        return 0.5
    try:
        from analyst.chain_reads_8510 import _bs_price, _cdf, implied_vol
    except Exception:
        return 0.5
    iv = implied_vol(spot, strike, ltp, t_years, r, call)
    if not iv or iv <= 0:
        return 0.5
    d1 = (math.log(spot / strike) + (r + 0.5 * iv * iv) * t_years) / (iv * math.sqrt(t_years))
    # Put delta is negative (cdf(d1)-1 in [-1,0]). Clamping BEFORE abs floored every put
    # to 0.01, destroying put weighting. Take |delta| first, then clamp to [0.01, 0.99]
    # so a near-ATM put keeps its true ~0.5 weight.
    raw = _cdf(d1) if call else _cdf(d1) - 1.0
    return max(0.01, min(0.99, abs(raw)))


def _contract_rows(
    chain_now: Dict[float, Dict[str, float]],
    chain_then: Dict[float, Dict[str, float]],
    *,
    baseline_kind: str,
    spot: float,
    band: float,
    t_years: float,
    r: float,
    cfg: Dict[str, Any],
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    strikes = {k for k in chain_now if abs(k - spot) <= band} | {k for k in chain_then if abs(k - spot) <= band}
    for k in sorted(strikes):
        a = chain_now.get(k) or {}
        b = chain_then.get(k) or {}
        for side, oi_k, ltp_k in (("CE", "ce_oi", "ce_ltp"), ("PE", "pe_oi", "pe_ltp")):
            oi_n = float(a.get(oi_k) or 0)
            oi_p = float(b.get(oi_k) or 0)
            ltp_n = float(a.get(ltp_k) or 0)
            ltp_p = float(b.get(ltp_k) or 0)
            if oi_n <= 0 and oi_p <= 0 and ltp_n <= 0 and ltp_p <= 0:
                continue
            delta = _estimate_delta(spot, k, ltp_n or ltp_p, side == "CE", t_years, r) if cfg.get("oi_use_delta_weight") else 1.0
            rows.append(
                {
                    "side": side,
                    "strike": k,
                    "oi": oi_n,
                    "d_oi": oi_n - oi_p,
                    "ltp": ltp_n or ltp_p,
                    "d_ltp": ltp_n - ltp_p,
                    "delta": abs(delta),
                    "baseline_kind": baseline_kind,
                }
            )
    return rows


def _slope_for(day: str, signal_id: str, scope_key: str, now_ts: int, score: Optional[float], cfg: Dict[str, Any]) -> Optional[float]:
    if score is None:
        return None
    hist = _SCORE_HISTORY.setdefault(f"{day}:{signal_id}", [])
    hist.append((now_ts, score, scope_key))
    hist[:] = [(t, s, sc) for t, s, sc in hist if now_ts - t <= int(cfg.get("oi_slope_window_sec", 180)) + 30]
    need = int(cfg.get("oi_slope_window_sec", 180))
    old = next(((t, s, sc) for t, s, sc in reversed(hist) if now_ts - t >= need and sc == scope_key), None)
    if old is None:
        return None
    return slope(score, old[1], now_ts - old[0], scope_key, old[2], need)


def _fmt_detail(label: str, regime: str, score: Optional[float], slope_val: Optional[float], extra: str = "") -> str:
    sc = f"{score:+.2f}" if score is not None else "—"
    sl = f"{slope_val:+.4f}/s" if slope_val is not None else "—"
    tail = f" · {extra}" if extra else ""
    return f"{label} {regime} (score {sc}, slope {sl}){tail}"


def apply_writer_bias_to_board_reads(
    *,
    by_ts: Dict[int, Dict[float, Dict[str, float]]],
    ts_sorted: List[int],
    spot: float,
    band: float,
    day: str,
    now_ts: int,
    last_t: int,
    age_sec: int,
    scanner_cfg: Optional[Dict[str, Any]],
    momentum_oi: Dict[str, Dict[str, Any]],
    expiry: str = "",
    prior_day_close: Optional[Dict[float, Dict[str, float]]] = None,
) -> Dict[str, Any]:
    """Return patches for board: day_oi, momentum fresh/total enrichments, facts."""
    cfg = load_writer_bias_cfg(scanner_cfg)
    session_open = _hm_ts(day, "09:15")
    session_ts = [t for t in ts_sorted if t >= session_open]
    poll_prev: Optional[int] = None
    if last_t in ts_sorted:
        idx = ts_sorted.index(last_t)
        if idx > 0:
            poll_prev = ts_sorted[idx - 1]

    base_t = next((t for t in ts_sorted if session_open <= t <= last_t), None)
    try:
        from analyst.chain_reads_8510 import _years_to_expiry

        t_years = _years_to_expiry(expiry, now_ts)
    except Exception:
        t_years = 1 / 365.0
    r = float(((scanner_cfg or {}).get("chain_reads") or {}).get("risk_free_rate", 0.065))

    out: Dict[str, Any] = {"day_oi": None, "momentum_patch": {}, "facts": {}, "unclassified_poll": False}
    if prior_day_close:
        ce_d = pe_d = 0.0
        for k, v in (by_ts.get(last_t) or {}).items():
            if abs(k - spot) > band:
                continue
            p = prior_day_close.get(k) or {}
            ce_d += float(v.get("ce_oi") or 0) - float(p.get("ce_oi") or 0)
            pe_d += float(v.get("pe_oi") or 0) - float(p.get("pe_oi") or 0)
        out["facts"]["previous_day_pe_minus_ce_oi"] = round(pe_d - ce_d, 0)
        out["facts"]["previous_day_ce_oi_delta"] = round(ce_d, 0)
        out["facts"]["previous_day_pe_oi_delta"] = round(pe_d, 0)

    if base_t is None or base_t >= last_t or len(session_ts) < 2:
        out["unclassified_poll"] = True
        out["day_oi"] = {
            "direction": None,
            "detail": "first poll of day unclassified — no matched poll baseline",
            "age_sec": age_sec,
            "feed_state": "STALE",
            "writer_bias_regime": "UNAVAILABLE",
        }
    elif poll_prev is None:
        out["unclassified_poll"] = True
        out["day_oi"] = {
            "direction": None,
            "detail": "first poll unclassified — no prior snapshot for poll Δ",
            "age_sec": age_sec,
            "feed_state": "STALE",
        }
    else:
        poll_rows = _contract_rows(
            by_ts[last_t], by_ts[poll_prev], baseline_kind="MATCHED_POLL", spot=spot, band=band, t_years=t_years, r=r, cfg=cfg
        )
        sess_rows = _contract_rows(
            by_ts[last_t], by_ts[base_t], baseline_kind="SESSION_SINCE_0915", spot=spot, band=band, t_years=t_years, r=r, cfg=cfg
        )
        poll_agg_full = aggregate(poll_rows, spot, cfg)
        sess_agg = aggregate(sess_rows, spot, cfg)
        poll_agg = poll_agg_full if poll_agg_full.get("state") == "RESEARCH_ONLY" else {}
        # Headline PE-CE change gates off the MATCHED-POLL baseline (Harish's
        # "Option 1 — matched last-poll changes"), NOT since-09:15. The
        # since-09:15 leg is still reported separately for context.
        if not poll_agg:
            out["day_oi"] = {
                "direction": None,
                "detail": "PE-CE matched-poll writing unavailable (poll baseline unmatched)",
                "age_sec": age_sec,
                "feed_state": "STALE",
            }
        else:
            w = poll_agg["writing"]
            regime = w.get("regime") or "UNAVAILABLE"
            score = w.get("score")
            scope = str(poll_agg.get("scope"))
            sl = _slope_for(day, "pe_ce_change", scope, last_t, score, cfg)
            sess_w = (sess_agg.get("writing") or {}) if sess_agg.get("state") == "RESEARCH_ONLY" else {}
            sess_net = sess_w.get("net")
            sess_txt = f"; since-09:15 net {sess_net:.0f}" if isinstance(sess_net, (int, float)) else ""
            out["day_oi"] = {
                "direction": _direction_from_regime(regime),
                "detail": _fmt_detail(
                    "PE-CE change",
                    regime,
                    score,
                    sl,
                    f"net matched-poll writing {w.get('net'):.0f} (poll Δ {int(last_t - poll_prev)}s{sess_txt})",
                ),
                "age_sec": age_sec,
                "feed_state": "LIVE" if _direction_from_regime(regime) is not None else "STALE",
                "writer_bias_score": score,
                "writer_bias_slope": sl,
                "writer_bias_regime": regime,
                "writer_bias_net": w.get("net"),
                "writer_bias_net_since_0915": sess_net,
                "divergence": bool(poll_agg.get("divergence")),
            }
        if sess_agg.get("state") == "RESEARCH_ONLY":
            pos = sess_agg.get("positional") or {}
            wr = sess_agg.get("writing") or {}
            scope_s = str(sess_agg.get("scope"))
            sl_tot = _slope_for(day, "cumulative_total_oi", scope_s, last_t, pos.get("score"), cfg)
            div = bool(sess_agg.get("divergence"))
            total = momentum_oi.get("total_oi") or {}
            patched = dict(total)
            regime = pos.get("regime") or "UNAVAILABLE"
            patched["direction"] = _direction_from_regime(regime) or patched.get("direction")
            extra = " · divergence" if div else ""
            patched["detail"] = (str(patched.get("detail") or "Total OI")[:200] + " · " + _fmt_detail("positional", regime, pos.get("score"), sl_tot) + extra).strip()
            patched["writer_bias_score"] = pos.get("score")
            patched["writer_bias_slope"] = sl_tot
            patched["divergence"] = div
            if patched.get("direction") is not None:
                patched["feed_state"] = "LIVE"
            out["momentum_patch"]["total_oi"] = patched

            fresh_w = sess_agg.get("fresh") or wr
            sl_f = _slope_for(day, "fresh_change_oi", scope_s, last_t, fresh_w.get("score"), cfg)
            fresh = momentum_oi.get("fresh_oi") or {}
            patched_f = dict(fresh)
            freg = fresh_w.get("regime") or "UNAVAILABLE"
            try:
                from analyst.nifty_four_oi_8510 import classify_fresh_oi_regime, fresh_oi_regime_to_direction

                nf_regime = classify_fresh_oi_regime(
                    float(fresh_w.get("score") or 0),
                    float((sess_agg.get("writing") or {}).get("score") or 0),
                    desk8600_regime=None,
                )
                patched_f["direction"] = fresh_oi_regime_to_direction(nf_regime) or _direction_from_regime(freg)
                freg = nf_regime if nf_regime != "NEUTRAL" else freg
            except Exception:
                patched_f["direction"] = _direction_from_regime(freg) or patched_f.get("direction")
            labels = [lb for lb in poll_agg.get("labels") or [] if lb.get("writing")]
            buildup_txt = ", ".join(f"{lb['side']} {lb['strike']} {lb['buildup']}" for lb in labels[:6])
            patched_f["detail"] = (
                str(patched_f.get("detail") or "Fresh OI")[:160]
                + " · "
                + _fmt_detail("writing-only ΔOI", freg, fresh_w.get("score"), sl_f, buildup_txt or "no writing strikes")
            ).strip()
            patched_f["writer_bias_score"] = fresh_w.get("score")
            patched_f["writer_bias_slope"] = sl_f
            if patched_f.get("direction") is not None:
                patched_f["feed_state"] = "LIVE"
            out["momentum_patch"]["fresh_oi"] = patched_f
            out["facts"]["writer_bias_poll_baseline"] = "MATCHED_POLL"
            out["facts"]["writer_bias_session_baseline"] = datetime.fromtimestamp(base_t, IST).strftime("%H:%M:%S")
            for lb in (poll_agg.get("labels") or [])[:12]:
                out["facts"].setdefault("writer_buildup", []).append(
                    f"{lb.get('side')} {lb.get('strike')}: {lb.get('buildup')} writing={lb.get('writing')}"
                )
    return out