#!/usr/bin/env python3
"""Read-only postmortem: homework-style 8 EMA vs current system & paper ledger."""
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
MARKET = ROOT / "ict_smc_app/backend/data/ict_smc.sqlite3"
LEDGER = ROOT / "data/terminal8510/ledger.sqlite3"

from analyst.terminal8510_backend import QuantTerminalBackend, simulate_backtest_spot_exit  # noqa: E402

DAYS = ["2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06"]
TOL = 20.0


def load_day_bars(day: str) -> Tuple[pd.DataFrame, List[Dict[str, Any]]]:
    q = """
        SELECT ts, o AS open, h AS high, l AS low, c AS close, v AS volume,
               date(ts, 'unixepoch', '+330 minutes') AS day,
               strftime('%H:%M', ts, 'unixepoch', '+330 minutes') AS hm
        FROM candles_1m WHERE sec_id='13' AND day=? ORDER BY ts
    """
    with sqlite3.connect(f"file:{MARKET}?mode=ro", uri=True, timeout=10) as conn:
        df = pd.read_sql_query(q, conn, params=(day,))
    if df.empty:
        return df, []
    df = df[(df["hm"] >= "09:15") & (df["hm"] <= "15:30")].copy()
    df["dt"] = pd.to_datetime(df["ts"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
    df15 = (
        df.set_index("dt")
        .resample("15min", closed="left", label="left")
        .agg(
            {
                "ts": "first",
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last",
                "volume": "sum",
                "day": "first",
                "hm": "first",
            }
        )
        .dropna()
        .reset_index()
    )
    if len(df15) < 20:
        return df, []
    df15["e4"] = df15["close"].ewm(span=4, adjust=False).mean()
    df15["e8"] = df15["close"].ewm(span=8, adjust=False).mean()
    mid = df15["close"].rolling(20).mean()
    sd = df15["close"].rolling(20).std(ddof=0).fillna(0.0)
    df15["ubb"] = mid + 2.0 * sd
    df15["lbb"] = mid - 2.0 * sd
    df15["bbw"] = df15["ubb"] - df15["lbb"]
    tr0 = df15["high"] - df15["low"]
    tr1 = (df15["high"] - df15["close"].shift(1)).abs()
    tr2 = (df15["low"] - df15["close"].shift(1)).abs()
    tr = pd.concat([tr0, tr1, tr2], axis=1).max(axis=1)
    df15["atr14"] = tr.ewm(alpha=1.0 / 14, adjust=False).mean()
    return df, df15.to_dict(orient="records")


def homework_signals(bars: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for idx in range(1, len(bars)):
        b, prev = bars[idx], bars[idx - 1]
        hm = str(b["hm"])
        if hm < "09:30" or hm >= "15:00":
            continue
        e4, e8 = float(b["e4"]), float(b["e8"])
        pc, cc = float(prev["close"]), float(b["close"])
        lo, hi = float(b["low"]), float(b["high"])
        touch = (abs(lo - e8) <= TOL) or (abs(hi - e8) <= TOL) or (lo <= e8 <= hi)
        cross_up = pc < float(prev["e8"]) and cc >= e8
        cross_dn = pc > float(prev["e8"]) and cc <= e8
        if e4 > e8 and (touch or cross_up):
            out.append({"dir": "LONG", "hm": hm, "kind": "cross_up" if cross_up else "touch"})
        if e4 < e8 and (touch or cross_dn):
            out.append({"dir": "SHORT", "hm": hm, "kind": "cross_dn" if cross_dn else "touch"})
    return out


def classify_block(
    backend: QuantTerminalBackend,
    b: Dict[str, Any],
    b_prev: Dict[str, Any],
    hw: Dict[str, Any],
    party: str,
) -> str:
    e4_v, e8_v = float(b["e4"]), float(b["e8"])
    c_p = float(b["close"])
    bbw = float(b["bbw"])
    ubb_up = float(b["ubb"]) > float(b_prev["ubb"])
    lbb_dn = float(b["lbb"]) < float(b_prev["lbb"])
    bb_min = float(backend.scanner_cfg.get("bb_width_min", 60.0))
    bb_on = backend._bollinger_gate_active("stage2" if hw["dir"] == "LONG" else "stage3")
    touch_sys = float(b["low"]) <= e8_v + 3.0 and float(b["high"]) >= e8_v - 3.0
    if hw["dir"] == "LONG":
        if e4_v <= e8_v:
            return "BLOCKED_MA_STACK"
        if party == "BEARISH":
            return "PARTY_SWITCH_BEARISH"
        if not touch_sys and hw["kind"] == "touch":
            return "TOUCH_TOLERANCE_3PT_VS_HOMEWORK_20PT"
        if bb_on and not (ubb_up and bbw >= bb_min):
            return "BOLLINGER_SLOPE_OR_WIDTH" if bbw >= bb_min else "BOLLINGER_BB_WIDTH"
        if c_p < e8_v - 2.5:
            return "CLOSE_BELOW_8EMA_BUFFER"
    else:
        if e4_v >= e8_v:
            return "BLOCKED_MA_STACK"
        if party == "BULLISH":
            return "PARTY_SWITCH_BULLISH"
        if not touch_sys and hw["kind"] == "touch":
            return "TOUCH_TOLERANCE_3PT_VS_HOMEWORK_20PT"
        if bb_on and not (lbb_dn and bbw >= bb_min):
            return "BOLLINGER_SLOPE_OR_WIDTH" if bbw >= bb_min else "BOLLINGER_BB_WIDTH"
        if c_p > e8_v + 2.5:
            return "CLOSE_ABOVE_8EMA_BUFFER"
    return "LIVE_ONLY_GAP_NOT_IN_BACKTEST"


def paper_for_day(day: str) -> List[Dict[str, Any]]:
    if not LEDGER.exists():
        return []
    q = """
        SELECT * FROM positions
        WHERE status='CLOSED' AND mode='PAPER_ONLY' AND substr(entry_time,1,10)=?
    """
    with sqlite3.connect(f"file:{LEDGER}?mode=ro", uri=True, timeout=5) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(q, (day,))]


def est_spot_pts(opt_pts: float, delta: float = 0.6) -> float:
    return round(opt_pts / delta, 1) if delta else 0.0


def sim_pure_homework_day(bars: List[Dict[str, Any]]) -> float:
    hw = homework_signals(bars)
    total = 0.0
    seen = set()
    for idx in range(20, len(bars) - 1):
        hm = str(bars[idx]["hm"])
        for h in [x for x in hw if x["hm"] == hm]:
            key = (hm, h["dir"])
            if key in seen:
                continue
            seen.add(key)
            b = bars[idx]
            is_long = h["dir"] == "LONG"
            if is_long and float(b["e4"]) <= float(b["e8"]):
                continue
            if not is_long and float(b["e4"]) >= float(b["e8"]):
                continue
            entry = round(float(bars[idx + 1]["open"]), 2)
            _, fx, *_ = simulate_backtest_spot_exit(
                bars, idx + 1, entry, is_long, 40.0, 40.0, False, {}, "15:20", 15.0
            )
            total += fx
    return round(total, 1)


def sim_current_system_day(backend: QuantTerminalBackend, bars: List[Dict[str, Any]]) -> Tuple[float, float, int]:
    st_sum = fx_sum = 0.0
    n = 0
    for idx in range(20, len(bars) - 1):
        b, b_prev = bars[idx], bars[idx - 1]
        hm = str(b["hm"])
        if hm < "09:30" or hm >= "15:00":
            continue
        cands = backend._backtest_legacy_cand_signals(
            b,
            b_prev,
            float(b["e4"]),
            float(b["e8"]),
            float(b["bbw"]),
            float(b["ubb"]) > float(b_prev["ubb"]),
            float(b["lbb"]) < float(b_prev["lbb"]),
            abs(float(b["ubb"]) - float(b_prev["ubb"])) <= 1.8,
            abs(float(b["lbb"]) - float(b_prev["lbb"])) <= 1.8,
            float(b.get("atr14") or 15.0),
        )
        for sid, sname, direc, tgt, sl, meta in cands:
            if "FLAGSHIP" in sid or "CROSSOVER" in sid:
                continue
            entry = round(float(bars[idx + 1]["open"]), 2)
            is_long = direc == "LONG"
            st, fx, *_ = simulate_backtest_spot_exit(
                bars, idx + 1, entry, is_long, float(sl), float(tgt), False, {}, "15:20", 15.0
            )
            st_sum += st
            fx_sum += fx
            n += 1
    return round(st_sum, 1), round(fx_sum, 1), n


def run_day(backend: QuantTerminalBackend, day: str) -> Dict[str, Any]:
    _, bars = load_day_bars(day)
    if not bars:
        return {"day": day, "missing_data": True}
    hw = homework_signals(bars)
    party = "NEUTRAL"
    system_fired: List[str] = []
    block_reasons: Counter = Counter()
    blocked_samples: List[str] = []

    for idx in range(20, len(bars) - 1):
        b, b_prev = bars[idx], bars[idx - 1]
        hm = str(b["hm"])
        if hm < "09:30" or hm >= "15:00":
            continue
        if float(b_prev["e4"]) >= float(b_prev["e8"]) and float(b["e4"]) < float(b["e8"]):
            party = "BEARISH"
        elif float(b_prev["e4"]) <= float(b_prev["e8"]) and float(b["e4"]) > float(b["e8"]):
            party = "BULLISH"

        cands = backend._backtest_legacy_cand_signals(
            b,
            b_prev,
            float(b["e4"]),
            float(b["e8"]),
            float(b["bbw"]),
            float(b["ubb"]) > float(b_prev["ubb"]),
            float(b["lbb"]) < float(b_prev["lbb"]),
            abs(float(b["ubb"]) - float(b_prev["ubb"])) <= 1.8,
            abs(float(b["lbb"]) - float(b_prev["lbb"])) <= 1.8,
            float(b.get("atr14") or 15.0),
        )
        cand_dirs = {c[2] for c in cands}
        for sid, *_ in cands:
            system_fired.append(f"{sid}@{hm}")
        for h in [x for x in hw if x["hm"] == hm]:
            if h["dir"] in cand_dirs:
                continue
            reason = classify_block(backend, b, b_prev, h, party)
            block_reasons[reason] += 1
            if len(blocked_samples) < 8:
                blocked_samples.append(f"{hm} {h['dir']}:{reason}")

    papers = paper_for_day(day)
    stage = [p for p in papers if "Stage" in str(p.get("strategy_setup") or "")]
    manual = [p for p in papers if "Manual" in str(p.get("strategy_setup") or "")]
    opt_stage = [float(p.get("exit_price") or 0) - float(p.get("entry_price") or 0) for p in stage]
    pure = sim_pure_homework_day(bars)
    st_pts, fx_pts, sys_n = sim_current_system_day(backend, bars)

    return {
        "day": day,
        "homework_signals": len(hw),
        "system_backtest_signals": len(system_fired),
        "homework_blocked_by_system_rules": sum(block_reasons.values()),
        "blocked_samples": blocked_samples,
        "block_reasons": dict(block_reasons),
        "paper_trades_all": len(papers),
        "paper_trades_stage2_3": len(stage),
        "paper_trades_manual_noise": len(manual),
        "paper_inr_all": round(sum(float(p.get("net_pnl") or 0) for p in papers), 2),
        "paper_inr_stage": round(sum(float(p.get("net_pnl") or 0) for p in stage), 2),
        "paper_premium_pts_stage": round(sum(opt_stage), 1),
        "paper_nifty_pts_est_stage": round(sum(est_spot_pts(x) for x in opt_stage), 1),
        "PURE_8EMA_40_40_nifty_pts": pure,
        "CURRENT_SYSTEM_super_trail_pts": st_pts,
        "CURRENT_SYSTEM_fixed_40_40_pts": fx_pts,
        "CURRENT_SYSTEM_signal_count_stage": sys_n,
    }


def exit_analysis(papers: List[Dict[str, Any]]) -> Dict[str, Any]:
    core = [p for p in papers if "Stage" in str(p.get("strategy_setup") or "")]
    trail_prem_loss = 0
    spot_ok_prem_bad = 0
    for p in core:
        opt = float(p.get("exit_price") or 0) - float(p.get("entry_price") or 0)
        spot_est = est_spot_pts(opt)
        reason = str(p.get("exit_reason") or "")
        if "TRAIL_SL" in reason and opt < 0:
            trail_prem_loss += 1
        if spot_est >= 30 and opt < 20:
            spot_ok_prem_bad += 1
    return {
        "stage_trades": len(core),
        "trail_sl_on_premium": trail_prem_loss,
        "spot_est_30plus_but_prem_under_20pts": spot_ok_prem_bad,
        "exit_reason_mix": dict(Counter(str(p.get("exit_reason") or "") for p in papers)),
        "manual_trade_count": sum(1 for p in papers if "Manual" in str(p.get("strategy_setup"))),
    }


def main() -> None:
    backend = QuantTerminalBackend()
    rows = []
    all_blocks: Counter = Counter()
    all_papers: List[Dict[str, Any]] = []

    for day in DAYS:
        r = run_day(backend, day)
        rows.append(r)
        if not r.get("missing_data"):
            all_blocks.update(r.get("block_reasons") or {})
            all_papers.extend(paper_for_day(day))

    exits = exit_analysis(all_papers)
    total_blocks = sum(all_blocks.values())
    block_pct = {
        k: round(100.0 * v / total_blocks, 1) if total_blocks else 0.0 for k, v in all_blocks.items()
    }
    report = {
        "window": DAYS,
        "daily": rows,
        "block_reason_counts": dict(all_blocks),
        "block_reason_pct": block_pct,
        "totals": {
            "homework_signals": sum(r.get("homework_signals", 0) for r in rows),
            "homework_blocked": total_blocks,
            "PURE_8EMA_40_40_pts": sum(r.get("PURE_8EMA_40_40_nifty_pts", 0) for r in rows),
            "CURRENT_super_trail_pts": sum(r.get("CURRENT_SYSTEM_super_trail_pts", 0) for r in rows),
            "CURRENT_fixed_40_40_pts": sum(r.get("CURRENT_SYSTEM_fixed_40_40_pts", 0) for r in rows),
            "paper_inr_all": sum(r.get("paper_inr_all", 0) for r in rows),
            "paper_inr_stage": sum(r.get("paper_inr_stage", 0) for r in rows),
        },
        "exit_analysis": exits,
    }
    out = ROOT / "analyst/postmortem_paper_vs_homework_report.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    print("Wrote", out)


if __name__ == "__main__":
    main()
