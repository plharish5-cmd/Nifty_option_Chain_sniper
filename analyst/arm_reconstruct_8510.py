"""Rebuild Prototype / Reversal arm state from today's Dhan-backed candles on every startup."""
from __future__ import annotations

import copy
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import pandas as pd

from analyst.dhan_session_backfill_8510 import backfill_today_session
from analyst.ema8_prototype_8510 import (
    backtest_ema8_prototype_at_bar,
    load_ema8_prototype_cfg,
    note_prototype_exit,
    reentry_enabled,
)
from analyst.ema_arm_persistence_8510 import mj_state_persist_slice
from analyst.ema_lane_lock_8510 import LANE_PROTOTYPE, LANE_REVERSAL, try_acquire_lane_lock
from analyst.mj_sir_stage_reversal_8510 import backtest_mj_dynamic_at_bar, load_mj_cfg
from analyst.nse_spot_candles_8510 import ema4_ema8, epoch_seconds, resample_nse_15m

IST = ZoneInfo("Asia/Kolkata")
log = logging.getLogger("arm_reconstruct_8510")

EMA_EPSILON = 1e-4


@dataclass
class ReconstructResult:
    ok: bool = False
    degraded: bool = False
    applied: bool = False
    session_day: str = ""
    proto_arm: Optional[Dict[str, Any]] = None
    mj_state: Optional[Dict[str, Any]] = None
    offline_misses: List[str] = field(default_factory=list)
    logs: List[str] = field(default_factory=list)
    ema_checks: Dict[str, float] = field(default_factory=dict)
    replayed: bool = False


def _session_open_now() -> Tuple[bool, str]:
    now = datetime.now(IST)
    day = now.strftime("%Y-%m-%d")
    hm = now.strftime("%H:%M")
    in_session = "09:15" <= hm < "15:45"
    return in_session, day


def _load_session_frames(
    market_db: Path,
    session_day: str,
    *,
    warmup_days: int = 28,
) -> Tuple[pd.DataFrame, pd.DataFrame, Dict[str, pd.DataFrame]]:
    if not market_db.exists():
        return pd.DataFrame(), pd.DataFrame(), {}
    warmup_from = (datetime.fromisoformat(session_day) - timedelta(days=warmup_days)).strftime("%Y-%m-%d")
    with sqlite3.connect(f"file:{market_db}?mode=ro", uri=True, timeout=5) as conn:
        q = """
            SELECT ts, o AS open, h AS high, l AS low, c AS close, v AS volume,
                   date(ts, 'unixepoch', '+330 minutes') AS day,
                   strftime('%H:%M', ts, 'unixepoch', '+330 minutes') AS hm
            FROM candles_1m
            WHERE sec_id = '13'
              AND date(ts, 'unixepoch', '+330 minutes') >= ?
              AND date(ts, 'unixepoch', '+330 minutes') <= ?
            ORDER BY ts ASC
        """
        df_all = pd.read_sql_query(q, conn, params=(warmup_from, session_day))
    if df_all.empty:
        return pd.DataFrame(), pd.DataFrame(), {}
    df_all = df_all[(df_all["hm"] >= "09:15") & (df_all["hm"] <= "15:30")].copy()
    df_all["dt"] = pd.to_datetime(df_all["ts"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
    df_15m = resample_nse_15m(df_all.set_index("dt")).reset_index()
    dt_col = "datetime" if "datetime" in df_15m.columns else df_15m.columns[0]
    df_15m["ts"] = epoch_seconds(df_15m[dt_col])
    df_15m["day"] = pd.to_datetime(df_15m[dt_col]).dt.strftime("%Y-%m-%d")
    df_15m["hm"] = pd.to_datetime(df_15m[dt_col]).dt.strftime("%H:%M")
    if len(df_15m) < 20:
        return df_all, df_15m, {}
    df_15m["e4"], df_15m["e8"] = ema4_ema8(df_15m["close"])
    day_to_1m = {str(d): g for d, g in df_all.groupby("day")}
    return df_all, df_15m, day_to_1m


def ema_closed_bar_delta(df_a: pd.DataFrame, df_b: pd.DataFrame) -> Tuple[float, float]:
    """Max |Δ| on last fully closed 15m bar between two frames (e4/e8)."""
    if len(df_a) < 3 or len(df_b) < 3:
        return float("inf"), float("inf")
    ia, ib = -2, -2
    de4 = abs(float(df_a["e4"].iloc[ia]) - float(df_b["e4"].iloc[ib]))
    de8 = abs(float(df_a["e8"].iloc[ia]) - float(df_b["e8"].iloc[ib]))
    return de4, de8


def _process_touch_signal(
    *,
    backend: Any,
    bars: List[Dict[str, Any]],
    day_to_1m: Dict[str, pd.DataFrame],
    proto_state: Dict[str, Any],
    mj_state: Dict[str, Any],
    sig: Tuple[str, str, str, float, float, Dict[str, Any]],
    eod_hm: str,
    offline_from: int,
    offline_to: int,
    now_ts: int,
) -> Optional[Dict[str, Any]]:
    from analyst.terminal8510_backend import simulate_prototype_spot_exit

    sid, sname, direc, tgt_pts, sl_pts, meta = sig
    is_proto = bool(meta.get("ema8_prototype"))
    is_mj = bool(meta.get("mj_dynamic"))
    if not is_proto and not is_mj:
        return None
    is_long = direc == "LONG"
    day_key = str(meta.get("entry_day") or bars[0].get("day") or "")
    entry_spot = float(meta.get("entry_spot") or 0.0)
    entry_ts = int(meta.get("entry_ts") or 0)
    start_idx = next((i for i, b in enumerate(bars) if int(b.get("ts") or 0) <= entry_ts), 0)
    proto_res = simulate_prototype_spot_exit(
        bars=bars,
        start_idx=start_idx,
        entry_spot=entry_spot,
        is_long=is_long,
        sl_pts=float(sl_pts),
        tgt_pts=float(tgt_pts),
        eod_hm=eod_hm,
        day_1m=day_to_1m.get(day_key),
        entry_ts=entry_ts,
        exit_on_opposite_close=is_proto,
    )
    exit_ts = int(proto_res.get("exit_ts") or 0)
    offline_rec = {
        "setup": sname,
        "side": "CE" if is_long else "PE",
        "entry_ts": entry_ts,
        "exit_ts": exit_ts,
        "entry_hm": meta.get("entry_hm") or proto_res.get("entry_hm"),
        "exit_hm": proto_res.get("exit_hm"),
    }
    if offline_from <= entry_ts <= offline_to and exit_ts and exit_ts <= offline_to:
        return offline_rec
    if is_proto:
        proto_state["bt_open"] = {
            "direction": direc,
            "entry_ts": entry_ts,
            "exit_ts": exit_ts,
        }
        cfg = load_ema8_prototype_cfg(backend.scanner_cfg)
        note_prototype_exit(proto_state, cfg, direc, proto_res.get("exit_reason"))
        if not reentry_enabled(cfg):
            proto_state["last_traded_direction"] = direc
            proto_state["consumed_cross_ts"] = meta.get("cross_bar_open_ts") or entry_ts
        if exit_ts and exit_ts <= now_ts:
            proto_state.pop("bt_open", None)
    if is_mj:
        mj_state.setdefault("day", day_key)
    if exit_ts and exit_ts <= now_ts and proto_state.get("bt_open"):
        proto_state.pop("bt_open", None)
    return None


def derive_states_from_candles(
    backend: Any,
    session_day: str,
    *,
    now_ts: Optional[int] = None,
    offline_from: Optional[int] = None,
    offline_to: Optional[int] = None,
    skip_dhan: bool = False,
) -> ReconstructResult:
    now_ts = int(now_ts or datetime.now(IST).timestamp())
    offline_to = int(offline_to or now_ts)
    offline_from = int(offline_from or now_ts)
    out = ReconstructResult(session_day=session_day)
    degraded = False
    if not skip_dhan:
        bf = backfill_today_session(backend, session_day)
        if not bf.get("ok"):
            out.degraded = True
            degraded = True
            out.logs.append(
                "RECONSTRUCT DEGRADED — Dhan history unavailable, using persisted arm only"
            )
    df_all, df_15m, day_to_1m = _load_session_frames(Path(backend.market_db), session_day)
    if df_15m.empty or session_day not in set(df_15m["day"].astype(str)):
        if degraded:
            return out
        out.logs.append("RECONSTRUCT SKIP — insufficient candle history")
        return out
    # Replay through the last 15m bar whose close is <= now.
    bars = df_15m.to_dict(orient="records")
    if sum(1 for b in bars if str(b.get("day")) == session_day) < 2:
        return out
    last_idx = len(bars) - 1
    while last_idx >= 20:
        close_ts = int(bars[last_idx].get("ts") or 0) + 900
        if close_ts <= now_ts and str(bars[last_idx].get("day")) == session_day:
            break
        last_idx -= 1
    if last_idx < 20:
        return out

    proto_state: Dict[str, Any] = {}
    mj_state: Dict[str, Any] = {}
    offline_hits: List[Dict[str, Any]] = []
    eod_hm = str(backend.scanner_cfg.get("flagship_eod_square_off", "15:20"))
    mj_dynamic = bool(load_mj_cfg(backend.scanner_cfg).get("dynamic_8ema_entry", True))

    for idx in range(20, last_idx + 1):
        b = bars[idx]
        if str(b.get("day") or "") != session_day:
            continue
        hm = str(b.get("hm") or "")
        if hm < "09:15" or hm >= "15:30":
            continue
        if proto_state.get("bt_open"):
            open_exit_ts = int(proto_state["bt_open"].get("exit_ts") or 0)
            if int(b.get("ts") or 0) >= open_exit_ts:
                proto_state.pop("bt_open", None)

        cand: List[Tuple[str, str, str, float, float, Dict[str, Any]]] = []
        if mj_dynamic:
            cand.extend(
                backtest_mj_dynamic_at_bar(
                    bars,
                    idx,
                    mj_state,
                    backend.scanner_cfg,
                    b15_df=df_15m,
                    day_1m=day_to_1m.get(session_day),
                    bb_stage2=backend._bollinger_gate_active("stage2"),
                    bb_stage3=backend._bollinger_gate_active("stage3"),
                    bb_cross=backend._bollinger_gate_active("crossover"),
                )
            )
        cand.extend(
            backtest_ema8_prototype_at_bar(
                bars,
                idx,
                proto_state,
                backend.scanner_cfg,
                bb_gate_active=False,
                b15_df=df_15m,
                day_1m=day_to_1m.get(session_day),
                market_db=getattr(backend, "market_db", None),
            )
        )
        for sig in cand:
            miss = _process_touch_signal(
                backend=backend,
                bars=bars,
                day_to_1m=day_to_1m,
                proto_state=proto_state,
                mj_state=mj_state,
                sig=sig,
                eod_hm=eod_hm,
                offline_from=offline_from,
                offline_to=offline_to,
                now_ts=now_ts,
            )
            if miss:
                offline_hits.append(miss)

    proto_state["day"] = session_day
    mj_state["day"] = session_day

    live_df = df_15m.copy()
    live_df["e4"], live_df["e8"] = ema4_ema8(live_df["close"])
    de4, de8 = ema_closed_bar_delta(df_15m, live_df)
    out.ema_checks = {"de4": de4, "de8": de8, "ema_ok": de4 < EMA_EPSILON and de8 < EMA_EPSILON}
    out.ok = True
    out.replayed = True
    out.proto_arm = copy.deepcopy(proto_state)
    out.mj_state = copy.deepcopy(mj_state)
    for hit in offline_hits:
        ent = str(hit.get("entry_hm") or "")[:5]
        ex = str(hit.get("exit_hm") or "")[:5]
        msg = (
            f"MISSED WHILE OFFLINE (open {ent}–{ex}): "
            f"{hit.get('setup')} {hit.get('side')} touch at {ent} — not fillable retroactively"
        )
        out.offline_misses.append(msg)
        log.info(msg)
    return out


def _proto_arm_equal(a: Optional[Dict[str, Any]], b: Optional[Dict[str, Any]]) -> bool:
    if not a and not b:
        return True
    arm_a = (a or {}).get("arm") or {}
    arm_b = (b or {}).get("arm") or {}
    if bool(arm_a) != bool(arm_b):
        return False
    if not arm_a:
        return True
    keys = ("direction", "cross_bar_open_ts", "away_seen")
    return all(arm_a.get(k) == arm_b.get(k) for k in keys)


def apply_reconstructed_state(backend: Any, result: ReconstructResult) -> None:
    if not result.proto_arm and not result.mj_state:
        return
    persisted_proto = copy.deepcopy(getattr(backend, "_ema8_prototype_state", {}) or {})
    persisted_mj = mj_state_persist_slice(getattr(backend, "_mj_sir_state", {}) or {})
    recon_mj = mj_state_persist_slice(result.mj_state or {})

    if result.proto_arm:
        arm = (result.proto_arm.get("arm") or {})
        if getattr(backend, "_proto_arm_loaded_from_db", False) and (persisted_proto.get("arm") or {}).get("direction"):
            backend._ema8_prototype_state = copy.deepcopy(persisted_proto)
            arm = (persisted_proto.get("arm") or {})
            backend.log_ema8_prototype_event(
                event="ARM RESUMED",
                direction=arm.get("direction"),
                dynamic_8ema=arm.get("cross_e8"),
                reason="ARM RESUMED (persisted) — evaluating live touches from current tick",
            )
        elif _proto_arm_equal(persisted_proto, result.proto_arm) and getattr(
            backend, "_proto_arm_loaded_from_db", False
        ):
            backend._ema8_prototype_state = copy.deepcopy(result.proto_arm)
            backend.log_ema8_prototype_event(
                event="ARM RESUMED",
                direction=arm.get("direction"),
                dynamic_8ema=arm.get("cross_e8"),
                reason="ARM RESUMED (persisted, confirmed by candles)",
            )
        elif arm:
            backend._ema8_prototype_state = copy.deepcopy(result.proto_arm)
            side = str(arm.get("direction") or "").upper()
            backend.log_ema8_prototype_event(
                event="ARM REBUILT",
                direction=arm.get("direction"),
                dynamic_8ema=arm.get("cross_e8"),
                reason=(
                    f"ARM REBUILT FROM DHAN HISTORY (Prototype, armed {side}, waiting for touch)"
                ),
            )
        backend._save_ema8_prototype_state()

    if result.mj_state:
        backend._mj_sir_state = copy.deepcopy(result.mj_state)
        if persisted_mj == recon_mj and getattr(backend, "_mj_sir_loaded_from_db", False):
            log.info(
                "Reversal runtime RESUMED (persisted, confirmed by candles) for %s",
                result.session_day,
            )
        elif recon_mj:
            log.info(
                "ARM REBUILT FROM DHAN HISTORY (Reversal) — mj pre-arm / fired keys from session replay"
            )
        backend._save_mj_sir_runtime_state()

    lock = getattr(backend, "_ema_lane_lock", None) or {}
    open_proto = bool((result.proto_arm or {}).get("bt_open"))
    if open_proto:
        try_acquire_lane_lock(lock, LANE_PROTOTYPE, note="reconstructed open position")
    elif (result.mj_state or {}).get("open_trade"):
        try_acquire_lane_lock(lock, LANE_REVERSAL, note="reconstructed open position")
    backend._ema_lane_lock = lock
    result.applied = True


def reconstruct_arms_on_startup(backend: Any, session_day: Optional[str] = None) -> Optional[ReconstructResult]:
    in_sess, day = _session_open_now()
    session_day = session_day or day
    if not in_sess:
        return None
    try:
        result = derive_states_from_candles(backend, session_day)
    except Exception as exc:
        log.exception("arm reconstruct failed: %s", exc)
        result = ReconstructResult(session_day=session_day, degraded=True)
        result.logs.append(f"RECONSTRUCT DEGRADED — {exc}")
        return result
    if result.degraded:
        return result
    if result.replayed:
        apply_reconstructed_state(backend, result)
    return result