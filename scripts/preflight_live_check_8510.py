#!/usr/bin/env python3
"""Pre-market live pipeline check for 8510 — no broker orders placed."""
from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CONFIG_PATH = ROOT / "analyst" / "terminal8510_config.json"
TARGET_EXPIRY = "2026-12-01"  # 01 Dec 2026 weekly
EXPECTED_SECONDARY_IP = "106.51.60.58"

Line = Tuple[str, str, str]  # status, label, detail


def _line(status: str, label: str, detail: str = "") -> None:
    mark = {"PASS": "✅", "FAIL": "❌", "WARN": "⚠️", "INFO": "·"}.get(status, "·")
    txt = f"{mark} [{status}] {label}"
    if detail:
        txt += f" — {detail}"
    print(txt, flush=True)


def _results_summary(lines: List[Line], *, echo: bool = True) -> int:
    fails = sum(1 for s, _, _ in lines if s == "FAIL")
    if echo:
        for s, label, detail in lines:
            _line(s, label, detail)
        print()
    if fails:
        print(f"PREFLIGHT OVERALL: FAIL ({fails} check(s) failed)")
        return 1
    print("PREFLIGHT OVERALL: PASS")
    return 0


def check_verify_locks() -> Line:
    script = ROOT / "scripts" / "verify_locks.py"
    try:
        proc = subprocess.run(
            [sys.executable, str(script)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=120,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        if proc.returncode == 0 and "11" in out and "PASS" in out:
            return ("PASS", "Immutable feature lock (verify_locks.py)", "11/11 locked modules intact")
        return ("FAIL", "Immutable feature lock (verify_locks.py)", out.strip()[:240] or f"exit {proc.returncode}")
    except Exception as exc:
        return ("FAIL", "Immutable feature lock (verify_locks.py)", str(exc))


def _parse_get_ip_body(body: Any) -> Dict[str, Any]:
    if not isinstance(body, dict):
        return {}
    data = body.get("data") if isinstance(body.get("data"), dict) else body
    if not isinstance(data, dict):
        return {}
    out: Dict[str, Any] = {}
    for key in ("detectedIP", "detectedIp", "publicIp", "ip", "currentIp"):
        if data.get(key):
            out["detectedIP"] = data.get(key)
            break
    for key in ("ordersAllowed", "orderAllowed", "orders_allowed"):
        if key in data:
            out["ordersAllowed"] = data.get(key)
            break
    for key in ("ipMatchType", "matchType", "ip_match", "ipMatch"):
        if data.get(key):
            out["ipMatchType"] = str(data.get(key))
            break
    return out


def _classify_ip_match(
    detected: Optional[str],
    *,
    dhan_cfg: Dict[str, Any],
) -> Tuple[bool, str]:
    from analyst.dhan_whitelist_ips_8510 import allowlist_from_cfg, parse_public_ipv4

    det = parse_public_ipv4(detected)
    if not det:
        return False, "NOT_IPV4"
    primary = parse_public_ipv4(dhan_cfg.get("whitelisted_ip"))
    allowed = allowlist_from_cfg(dhan_cfg, primary)
    if primary and det == primary:
        return True, "PRIMARY_MATCH"
    if det in allowed:
        if det == EXPECTED_SECONDARY_IP:
            return True, "SECONDARY_MATCH"
        return True, "SECONDARY_MATCH"
    return False, "NO_MATCH"


def check_dhan_token_ip(broker: Any, cfg: Dict[str, Any]) -> List[Line]:
    from analyst.dhan_ipv4_guard_8510 import ipv4_sync_client
    from analyst.terminal8510_backend import decode_token_expiry, load_env_safe

    lines: List[Line] = []
    load_env_safe()
    token = __import__("os").environ.get("DHAN_ACCESS_TOKEN", "")
    if not broker.is_configured():
        lines.append(("FAIL", "Dhan token configured", "missing DHAN_ACCESS_TOKEN or CLIENT_ID"))
        return lines
    exp = decode_token_expiry(token)
    if exp.get("is_expired"):
        lines.append(("FAIL", "Dhan access token expiry", f"expired ({exp.get('token_valid_till')})"))
    else:
        lines.append(
            (
                "PASS",
                "Dhan access token expiry",
                f"valid till {exp.get('token_valid_till')} ({exp.get('time_left')})",
            )
        )

    dhan_cfg = cfg.get("dhan") if isinstance(cfg.get("dhan"), dict) else {}
    ip_parsed: Dict[str, Any] = {}
    http_err = ""
    try:
        with ipv4_sync_client(10.0) as client:
            for path in ("/ip/getIP", "/ip/getip"):
                res = client.get(f"{broker.base_url}{path}", headers=broker._headers())
                if res.status_code == 200:
                    try:
                        ip_parsed = _parse_get_ip_body(res.json())
                    except Exception:
                        ip_parsed = {}
                    if ip_parsed:
                        break
                elif res.status_code != 404:
                    http_err = f"HTTP {res.status_code}"
    except Exception as exc:
        http_err = str(exc)

    detected = ip_parsed.get("detectedIP")
    orders_allowed = ip_parsed.get("ordersAllowed")
    match_type = str(ip_parsed.get("ipMatchType") or "")

    if not detected:
        lines.append(("FAIL", "Dhan GET /v2/ip/getIP", http_err or "no detectedIP in response"))
        return lines

    from analyst.dhan_whitelist_ips_8510 import parse_public_ipv4

    if not parse_public_ipv4(detected):
        lines.append(("FAIL", "Dhan detectedIP is public IPv4", str(detected)))
    else:
        lines.append(("PASS", "Dhan detectedIP (IPv4)", str(detected)))

    if orders_allowed is True:
        lines.append(("PASS", "Dhan ordersAllowed", "True"))
    elif orders_allowed is False:
        lines.append(("FAIL", "Dhan ordersAllowed", "False — static IP / token may block live orders"))
    else:
        lines.append(("WARN", "Dhan ordersAllowed", "field absent; inferred from IP match"))

    ok_match, inferred = _classify_ip_match(detected, dhan_cfg=dhan_cfg)
    label = match_type.upper() if match_type else inferred
    if label in ("PRIMARY_MATCH", "SECONDARY_MATCH") and ok_match:
        lines.append(("PASS", "Static IP whitelist match", label))
    elif ok_match:
        lines.append(("PASS", "Static IP whitelist match", inferred))
    else:
        lines.append(("FAIL", "Static IP whitelist match", f"{label} (detected {detected})"))
    return lines


def check_spot_rest(broker: Any, cfg: Dict[str, Any]) -> Tuple[List[Line], float]:
    from analyst.dhan_spot_rest_fallback_8510 import entry_rest_ltp_lookup, recover_nifty_spot_for_entry
    from analyst.ema8_display_audit_8510 import spot_quote_stale

    lines: List[Line] = []
    spot_blob = {
        "price": 0.0,
        "updated_epoch": time.time() - 12.0,
        "is_stale": False,
    }
    stale, age = spot_quote_stale(spot_blob, max_age_sec=5.0)
    lines.append(
        (
            "INFO",
            "WebSocket spot age (simulated stale probe)",
            f"age={age:.1f}s stale={stale}" if age is not None else "unknown",
        )
    )

    backend = SimpleNamespace(
        cached_telemetry={"spot": dict(spot_blob)},
        broker=broker,
    )
    ok, audit = asyncio.run(recover_nifty_spot_for_entry(backend, market_open=True))
    if not ok:
        prices, err = asyncio.run(entry_rest_ltp_lookup(broker, {"IDX_I": [13]}))
        spot = float(prices.get("13") or 0.0)
        if spot > 0:
            ok = True
            audit = f"REST spot {spot:.2f}"
        else:
            lines.append(("FAIL", "Nifty spot REST fallback", err or audit))
            return lines, 0.0
    spot_px = float((backend.cached_telemetry.get("spot") or {}).get("price") or 0.0)
    if spot_px <= 0:
        prices, err = asyncio.run(entry_rest_ltp_lookup(broker, {"IDX_I": [13]}))
        spot_px = float(prices.get("13") or 0.0)
    if spot_px > 0:
        lines.append(("PASS", "Nifty spot LTP (REST)", f"{spot_px:.2f} — {audit or 'direct LTP'}"))
    else:
        lines.append(("FAIL", "Nifty spot LTP (REST)", "price <= 0"))
    return lines, spot_px


def check_option_quotes(broker: Any, cfg: Dict[str, Any], spot: float) -> List[Line]:
    from analyst.dhan_spot_rest_fallback_8510 import entry_rest_ltp_lookup, recover_option_ltp_for_entry
    from analyst.terminal8510_backend import InstrumentManager

    lines: List[Line] = []
    scrip_url = cfg.get("scrip_master_url", "")
    master_path = cfg.get("instrument_master_path", "data/terminal8500/instrument_master.csv")
    inst = InstrumentManager(master_path, scrip_url)
    inst.ensure_loaded()
    expiry = TARGET_EXPIRY
    if expiry not in inst.cached_expiries:
        near = inst.get_nearest_expiry(TARGET_EXPIRY)
        lines.append(("WARN", "Target expiry 01 Dec 2026", f"using nearest {near}"))
        expiry = near
    else:
        lines.append(("PASS", "Target expiry 01 Dec 2026", expiry))

    if spot <= 0:
        lines.append(("FAIL", "Option chain (needs spot)", "spot unavailable"))
        return lines

    atm = round(spot / 100.0) * 100
    min_prem = float(
        ((cfg.get("scanner") or {}).get("ema8_prototype") or {}).get("min_option_premium_inr") or 200.0
    )
    sec_ids: List[int] = []
    rows: List[Tuple[str, Any, float]] = []
    for side in ("CE", "PE"):
        row = inst.resolve_option(expiry, atm, side)
        if not row:
            lines.append(("FAIL", f"Resolve NIFTY {atm} {side}", f"expiry {expiry}"))
            continue
        sec = int(row.get("security_id"))
        sec_ids.append(sec)
        rows.append((side, row, atm))
    batch: Dict[str, float] = {}
    if sec_ids:
        batch, _ = asyncio.run(entry_rest_ltp_lookup(broker, {"NSE_FNO": sec_ids}))
    for side, row, strike in rows:
        sec = str(row.get("security_id"))
        ltp = float(batch.get(sec) or batch.get(int(sec)) or 0.0)
        err = ""
        if ltp <= 0:
            ltp, err = asyncio.run(recover_option_ltp_for_entry(broker, sec))
            ltp = float(ltp or 0.0)
        if ltp and ltp > 0:
            ok_prem = ltp >= min_prem
            lines.append(
                (
                    "PASS" if ok_prem else "WARN",
                    f"{side} LTP sec {sec} ({strike})",
                    f"₹{ltp:.2f} vs min_option_premium_inr ₹{min_prem:.0f}",
                )
            )
        else:
            lines.append(("FAIL", f"{side} LTP sec {sec}", err or "no quote"))
    return lines


def check_lane_arming(cfg: Dict[str, Any], backend: Any) -> List[Line]:
    from analyst.dhan_live_lane_gateway_8510 import live_dhan_entry_allowed
    from analyst.lane_live_routing_8510 import order_will_be_paper

    lines: List[Line] = []
    sc = cfg.get("scanner") or {}
    exec_mode = str(cfg.get("execution_mode") or "PAPER_ONLY").upper()
    desk_live = exec_mode == "BOTH" and bool(getattr(backend, "_real_money_enabled", False))
    lines.append(
        (
            "PASS" if desk_live else "WARN",
            "Global desk LIVE_ALLOWED (execution_mode=BOTH + armed)",
            exec_mode if not desk_live else "BOTH — live money armed",
        )
    )

    proto = sc.get("ema8_prototype") or {}
    rev = sc.get("reversal") or {}
    for lane, block in (("ema8_prototype", proto), ("reversal", rev)):
        lines.append(
            (
                "INFO",
                f"{lane}",
                (
                    f"enabled={block.get('enabled')} "
                    f"execution_mode={block.get('execution_mode')} "
                    f"live_trade_enabled={block.get('live_trade_enabled')}"
                ),
            )
        )
    lines.append(
        (
            "INFO",
            "ema8_prototype touch / premium",
            f"arm_mode={proto.get('arm_mode')} touch_tol_pts={proto.get('touch_tol_pts')} "
            f"min_option_premium_inr={proto.get('min_option_premium_inr')}",
        )
    )

    ribbon_gate = live_dhan_entry_allowed(backend, "ribbon_scalp_long")
    if not ribbon_gate.get("ok") and "paper" in str(ribbon_gate.get("reason", "")).lower():
        lines.append(("PASS", "Ribbon Scalp live BUY hard-block", ribbon_gate.get("reason", "")))
    else:
        lines.append(
            (
                "FAIL",
                "Ribbon Scalp live BUY hard-block",
                ribbon_gate.get("reason", "expected LIVE_GATE_BLOCKED paper-only"),
            )
        )
    paper_ribbon = order_will_be_paper(backend, "ribbon_scalp_long")
    lines.append(
        (
            "PASS" if paper_ribbon else "FAIL",
            "Ribbon order_will_be_paper",
            str(paper_ribbon),
        )
    )
    return lines


def _build_probe_backend(cfg: Dict[str, Any]) -> Any:
    from analyst.offhours_cleanup_8510 import new_position_entry_allowed as entry_guard
    from analyst.terminal8510_backend import DhanBroker, QuantTerminalBackend

    cfg = dict(cfg)
    safety = dict(cfg.get("execution_safety_v194") or {})
    safety["live_dry_run"] = True
    cfg["execution_safety_v194"] = safety

    b = object.__new__(QuantTerminalBackend)
    b.cfg = cfg
    b.scanner_cfg = dict(cfg.get("scanner") or {})
    b.execution_mode = str(cfg.get("execution_mode") or "PAPER_ONLY")
    b._real_money_enabled = b.execution_mode.upper() == "BOTH"
    b.kill_switch_active = False
    b.paper_execution_enabled = bool(cfg.get("paper_execution_enabled", True))
    b._ema_lane_lock = {}
    b.cached_telemetry = {"spot": {"price": 0.0, "is_stale": False, "updated_epoch": time.time()}}
    b.broker = DhanBroker(cfg=cfg)
    b.broker._live_gate_backend = b
    b.nse_holidays = set()
    b.is_nse_market_open = lambda dt=None: (True, "OPEN")
    b.live_trades_today = lambda: 0
    b.live_pnl_today = lambda: 0.0

    def _new_entry_allowed() -> Tuple[bool, str]:
        return entry_guard(
            market_open=True,
            market_status="OPEN",
            spot_blob=(b.cached_telemetry or {}).get("spot"),
        )

    b.new_position_entry_allowed = _new_entry_allowed
    b.live_execution_decision = lambda: QuantTerminalBackend.live_execution_decision(b)
    b._dhan_credentials_live_ok = lambda: QuantTerminalBackend._dhan_credentials_live_ok(b)
    return b


def traverse_live_gates(backend: Any, strategy_id: str) -> Optional[str]:
    from analyst.dhan_live_lane_gateway_8510 import live_dhan_entry_allowed
    from analyst.ema_lane_entry_gate_8510 import try_reserve_lane_for_signal
    from analyst.lane_live_routing_8510 import order_will_be_paper

    sid = str(strategy_id).upper()
    if sid.startswith("EMA8_PROTOTYPE") or sid.startswith("CROSSOVER"):
        ok, why = try_reserve_lane_for_signal(backend._ema_lane_lock, sid, open_position_count=0)
        if not ok:
            return why
    if order_will_be_paper(backend, sid):
        return f"Lane routes to PAPER for {sid}"
    gate = live_dhan_entry_allowed(backend, sid)
    if not gate.get("ok"):
        return str(gate.get("reason") or "live_dhan_entry_allowed blocked")
    try:
        ok_mkt, why_mkt = backend.new_position_entry_allowed()
    except Exception as exc:
        return f"new_position_entry_allowed error: {exc}"
    if not ok_mkt:
        return why_mkt
    route = backend.live_execution_decision()
    if route.get("route") not in ("LIVE", "DRY_RUN"):
        return str(route.get("reason") or route.get("route"))
    if route.get("route") == "DRY_RUN":
        return None
    return None


def check_gate_dry_run(backend: Any, spot_price: float) -> List[Line]:
    """Simulate desk LIVE arm in-memory only so gates mirror a fully armed session."""
    lines: List[Line] = []
    backend.execution_mode = "BOTH"
    backend._real_money_enabled = True
    backend.cached_telemetry = {
        "spot": {
            "price": spot_price,
            "is_stale": False,
            "updated_epoch": time.time(),
            "status": "LIVE",
        }
    }
    candidates = [
        ("EMA8_PROTOTYPE_LONG", "8 EMA Prototype LONG"),
        ("CROSSOVER_BULLISH", "8 EMA Reversal (CROSSOVER_BULLISH)"),
    ]
    for sid, label in candidates:
        blocker = traverse_live_gates(backend, sid)
        if blocker:
            lines.append(("FAIL", f"Gate dry-run {label}", blocker))
            continue
        route = backend.live_execution_decision()
        route_txt = route.get("route", "?")
        if route_txt == "DRY_RUN":
            lines.append(
                (
                    "PASS",
                    f"Gate dry-run {label}",
                    "READY FOR LIVE ORDERS ✅ (pre-POST pipeline; route=DRY_RUN, live_dry_run=True)",
                )
            )
        elif route_txt == "LIVE":
            lines.append(
                (
                    "PASS",
                    f"Gate dry-run {label}",
                    "READY FOR LIVE ORDERS ✅ (pre-POST pipeline; route=LIVE)",
                )
            )
        else:
            lines.append(("FAIL", f"Gate dry-run {label}", str(route.get("reason") or route_txt)))
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description="8510 pre-market live pipeline preflight (no orders).")
    parser.add_argument("--json", action="store_true", help="Print machine-readable summary JSON at end")
    args = parser.parse_args()

    print("=" * 72)
    print(f"8510 PREFLIGHT LIVE CHECK — {datetime.now(timezone.utc).astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print("No POST /v2/orders — dry-run gate uses live_dry_run=True in memory only.")
    print("=" * 72)
    print()

    all_lines: List[Line] = []

    if not CONFIG_PATH.is_file():
        all_lines.append(("FAIL", "Load terminal8510_config.json", "file missing"))
        return _results_summary(all_lines)

    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        all_lines.append(("FAIL", "Load terminal8510_config.json", str(exc)))
        return _results_summary(all_lines)

    all_lines.append(check_verify_locks())

    from analyst.terminal8510_backend import DhanBroker

    broker = DhanBroker(cfg=cfg)
    all_lines.extend(check_dhan_token_ip(broker, cfg))
    spot_lines, spot_px = check_spot_rest(broker, cfg)
    all_lines.extend(spot_lines)

    if spot_px <= 0:
        spot_px = float(broker._ltp_cache.get("13") or 0.0)
    all_lines.extend(check_option_quotes(broker, cfg, spot_px))

    backend = _build_probe_backend(cfg)
    all_lines.extend(check_lane_arming(cfg, backend))
    all_lines.extend(check_gate_dry_run(backend, spot_px))

    if args.json:
        payload = {"lines": [{"status": s, "label": l, "detail": d} for s, l, d in all_lines]}
        print(json.dumps(payload, indent=2))
        return _results_summary(all_lines, echo=False)

    return _results_summary(all_lines)


if __name__ == "__main__":
    raise SystemExit(main())