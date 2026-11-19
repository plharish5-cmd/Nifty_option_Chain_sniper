"""Per-opportunity replay for owner-marked chart times (read-only, spot + rules)."""
from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import pandas as pd

IST = ZoneInfo("Asia/Kolkata")
ROOT = Path(__file__).resolve().parents[1]

MARKED_PROTOTYPE: List[Tuple[str, str, str]] = [
    ("09:41", "PE", "morning PE retouch (ledger 25300 PE attempt)"),
    ("10:15", "CE", "owner chart CE"),
    ("10:30", "CE", "owner chart CE / mentor e8_trig"),
    ("13:00", "CE", "owner chart CE"),
    ("14:00", "CE", "owner chart CE"),
    ("15:00", "CE", "owner chart CE"),
]

MARKED_REVERSAL: List[Tuple[str, str, str]] = [
    ("09:45", "CE", "mentor e8_trig 09:45 CE"),
    ("10:30", "CE", "mentor e8_trig 10:30 CE"),
    ("13:15", "CE", "mentor e8_trig 13:15 CE"),
]


@dataclass
class OppVerdict:
    lane: str
    hm: str
    side: str
    verdict: str
    reason: str
    entry_ts: str = ""
    entry_spot: float = 0.0

    def as_row(self) -> Dict[str, str]:
        return {
            "lane": self.lane,
            "time": self.hm,
            "side": self.side,
            "verdict": self.verdict,
            "reason": self.reason,
            "entry_ts": self.entry_ts,
            "entry_spot": f"{self.entry_spot:.2f}" if self.entry_spot else "",
        }


def _private_backend(day: str):
    from analyst.terminal8510_backend import QuantTerminalBackend

    cfg_path = ROOT / "analyst/terminal8510_config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    market = Path(str(cfg.get("market_db_path") or "ict_smc_app/backend/data/ict_smc.sqlite3"))
    cfg["market_db_path"] = str(market if market.is_absolute() else (ROOT / market))
    tmp = tempfile.mkdtemp(prefix="marked_replay_")
    cfg["ledger_db_path"] = str(Path(tmp) / "ledger.sqlite3")
    # Match live prototype cadence in replay
    proto = cfg.setdefault("scanner", {}).setdefault("ema8_prototype", {})
    proto["backtest_tick_seconds"] = float(proto.get("e8_refresh_seconds", 3.0) or 3.0)
    private = Path(tmp) / "config.json"
    private.write_text(json.dumps(cfg), encoding="utf-8")
    return QuantTerminalBackend(str(private)), cfg


def _normalize_trades(bt: Dict[str, Any], lane_prefix: str) -> List[Dict[str, Any]]:
    rows = bt.get("rows") or bt.get("trades") or []
    out = []
    for r in rows:
        sid = str(r.get("strategy_id") or r.get("Strategy_ID") or "").upper()
        if lane_prefix == "PROTOTYPE" and "EMA8_PROTOTYPE" not in sid:
            continue
        if lane_prefix == "REVERSAL" and "CROSSOVER" not in sid:
            continue
        hm = str(r.get("entry_hm") or r.get("Entry_Time") or "")[:8]
        if len(hm) == 5:
            hm = hm + ":00"
        out.append(
            {
                "hm": hm[:8],
                "side": "CE" if "LONG" in sid or "BULL" in sid else "PE",
                "spot": float(r.get("Index_Entry") or r.get("entry_spot") or 0),
                "meta": r,
            }
        )
    return out


def _hm_to_sec(hm: str) -> int:
    h, m = hm.split(":")[:2]
    return int(h) * 3600 + int(m) * 60


def _in_open_trade(trade: Dict[str, Any], hm: str) -> bool:
    meta = trade.get("meta") or {}
    try:
        ent = _hm_to_sec(str(meta.get("entry_hm") or trade["hm"])[:5])
        ex = _hm_to_sec(str(meta.get("exit_hm") or "15:30")[:5])
        t = _hm_to_sec(hm)
    except (ValueError, IndexError):
        return False
    return ent < t < ex


def _match_trade(trades: List[Dict[str, Any]], hm: str, side: str, window_sec: int = 360) -> Optional[Dict[str, Any]]:
    target = _hm_to_sec(hm)
    best = None
    best_d = 10**9
    for t in trades:
        if t["side"] != side:
            continue
        try:
            ts = _hm_to_sec(t["hm"][:5])
        except (ValueError, IndexError):
            continue
        d = abs(ts - target)
        if d <= window_sec and d < best_d:
            best_d = d
            best = t
    return best


def _proto_state_at_hm(backend, day: str, hm: str) -> Dict[str, Any]:
    """Lightweight state snapshot just before mark (spot replay, no option quotes)."""
    import sqlite3

    from analyst.ema8_prototype_8510 import evaluate_ema8_prototype_intrabar, load_ema8_prototype_cfg
    from analyst.nse_spot_candles_8510 import resample_nse_15m

    proto_state: Dict[str, Any] = {}
    proto_cfg = load_ema8_prototype_cfg(backend.scanner_cfg)
    mark_dt = datetime.strptime(f"{day} {hm}", "%Y-%m-%d %H:%M").replace(tzinfo=IST)
    mark_ts = int(mark_dt.timestamp())
    warmup = (datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=IST) - timedelta(days=2)).strftime("%Y-%m-%d")
    with sqlite3.connect(f"file:{backend.market_db}?mode=ro", uri=True) as conn:
        q = """
            SELECT ts, o, h, l, c, v FROM candles_1m
            WHERE sec_id='13'
              AND date(ts, 'unixepoch', '+330 minutes') >= ?
              AND date(ts, 'unixepoch', '+330 minutes') <= ?
              AND ts <= ?
            ORDER BY ts
        """
        df = pd.read_sql_query(q, conn, params=(warmup, day, mark_ts + 60))
    if df.empty:
        return {"error": "no 1m candles"}
    df = df.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
    df["dt"] = pd.to_datetime(df["ts"], unit="s", utc=True).dt.tz_convert(IST)
    b15 = resample_nse_15m(df.set_index("dt"))
    spot_row = df.iloc[-1]
    spot = float(spot_row["close"])
    last_st: Dict[str, Any] = {}
    # Replay 3s grid from 09:15 to mark (coarse: every 30s for speed, exact at mark)
    tick_ts = int(datetime.strptime(f"{day} 09:15", "%Y-%m-%d %H:%M").replace(tzinfo=IST).timestamp())
    refresh = 60  # coarse grid for miss explanations only (entries come from full backtest)
    while tick_ts <= mark_ts:
        sub = df[df["ts"] <= tick_ts]
        if len(sub) < 30:
            tick_ts += refresh
            continue
        sub = sub.copy()
        sub["dt"] = pd.to_datetime(sub["ts"], unit="s", utc=True).dt.tz_convert(IST)
        b15 = resample_nse_15m(sub.set_index("dt"))
        row = sub.iloc[-1]
        sp = float(row["close"])
        th, tl = float(row["high"]), float(row["low"])
        _, st = evaluate_ema8_prototype_intrabar(
            b15,
            sp,
            cfg=proto_cfg,
            proto_state=proto_state,
            session_day=day,
            gate_meta=lambda _k: {},
            tick_high=th,
            tick_low=tl,
            now_hm=datetime.fromtimestamp(tick_ts, IST).strftime("%H:%M"),
        )
        last_st = st
        tick_ts += refresh
    arm = proto_state.get("arm") or {}
    return {
        "arm": arm,
        "open": proto_state.get("open") or proto_state.get("bt_open"),
        "away_seen": bool(arm.get("away_seen")),
        "direction": arm.get("direction"),
        "sl_blocked": proto_state.get("sl_blocked_side"),
        "block_reason": str(last_st.get("block_reason") or last_st.get("phase") or ""),
        "spot": spot,
    }


def _explain_miss(side: str, snap: Dict[str, Any]) -> str:
    if snap.get("error"):
        return str(snap["error"])
    arm_dir = str(snap.get("direction") or "").upper()
    want = "LONG" if side == "CE" else "SHORT"
    if snap.get("open"):
        return "already in trade at this time — no second entry"
    if not arm_dir:
        if snap.get("sl_blocked"):
            return f"arm lost — {snap['sl_blocked']} side blocked after stop until opposite cross"
        return "arm was missing at mark (gap / restart would rebuild from cross only)"
    if arm_dir != want:
        return f"armed {arm_dir} ({'CE' if arm_dir == 'LONG' else 'PE'}) — mark expects {side}"
    if not snap.get("away_seen"):
        return "armed but price never moved away from 8 EMA before mark"
    br = snap.get("block_reason") or ""
    if br:
        if "STALE" in br.upper():
            return "stale spot — touch paused at mark"
        if "OI" in br.upper() or "FILTER" in br.upper():
            return f"blocked by filter: {br[:120]}"
        if "touch" in br.lower() or "RE-TOUCH" in br.upper():
            return f"armed with away seen but touch ±3 not met at mark ({br[:80]})"
        return br[:160]
    return "armed with away seen but touch ±3 not met at mark (spot replay; no option quote checked)"


def replay_marked_day(day: str = "2026-11-19") -> List[OppVerdict]:
    backend, _ = _private_backend(day)
    proto_bt = backend.run_date_range_backtest(
        day, day, strategy_filter="EMA8_PROTOTYPE", include_today_paper=False
    )
    rev_bt = backend.run_date_range_backtest(day, day, strategy_filter="CROSSOVER", include_today_paper=False)
    proto_trades = _normalize_trades(proto_bt, "PROTOTYPE")
    rev_trades = _normalize_trades(rev_bt, "REVERSAL")
    out: List[OppVerdict] = []

    for hm, side, note in MARKED_PROTOTYPE:
        win = 1200 if hm >= "14:30" else 360
        hit = _match_trade(proto_trades, hm, side, window_sec=win)
        if hit:
            out.append(
                OppVerdict(
                    "Prototype",
                    hm,
                    side,
                    "WOULD TAKE",
                    f"{note}; replay entry {hit['hm'][:5]} spot {hit['spot']:.1f}",
                    hit["hm"][:8],
                    hit["spot"],
                )
            )
            continue
        holding = next((t for t in proto_trades if _in_open_trade(t, hm) and t["side"] == side), None)
        if holding:
            meta = holding.get("meta") or {}
            out.append(
                OppVerdict(
                    "Prototype",
                    hm,
                    side,
                    "NO",
                    f"already in {side} trade from {str(meta.get('entry_hm',''))[:5]} "
                    f"— chart mark is mid-hold, not a new entry ({note})",
                )
            )
            continue
        else:
            near = min(proto_trades, key=lambda t: abs(_hm_to_sec(t["hm"][:5]) - _hm_to_sec(hm)), default=None)
            extra = ""
            if near:
                extra = f"; nearest replay entry {near['hm'][:5]} {near['side']} spot {near['spot']:.1f}"
            snap = _proto_state_at_hm(backend, day, hm)
            out.append(
                OppVerdict(
                    "Prototype",
                    hm,
                    side,
                    "NO",
                    _explain_miss(side, snap) + extra + f" ({note})",
                )
            )

    for hm, side, note in MARKED_REVERSAL:
        hit = _match_trade(rev_trades, hm, side, window_sec=900)
        if hit:
            out.append(
                OppVerdict(
                    "Reversal",
                    hm,
                    side,
                    "WOULD TAKE",
                    f"{note}; replay {hit['hm'][:5]} spot {hit['spot']:.1f}",
                    hit["hm"][:8],
                    hit["spot"],
                )
            )
        else:
            out.append(
                OppVerdict(
                    "Reversal",
                    hm,
                    side,
                    "NO",
                    f"no reversal entry in replay window — expired crossover window, filter, or touch not met ({note})",
                )
            )
    return out


def format_table(rows: List[OppVerdict]) -> str:
    lines = ["| time | side | lane | verdict | reason |", "|------|------|--------|---------|--------|"]
    for r in rows:
        lines.append(f"| {r.hm} | {r.side} | {r.lane} | {r.verdict} | {r.reason[:100]} |")
    return "\n".join(lines)


if __name__ == "__main__":
    verdicts = replay_marked_day("2026-11-19")
    print(format_table(verdicts))
    takes = sum(1 for v in verdicts if v.verdict == "WOULD TAKE")
    print(f"\nSummary: {takes}/{len(verdicts)} marked opportunities would take (spot replay, log_only OI)")