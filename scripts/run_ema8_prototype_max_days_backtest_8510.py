#!/usr/bin/env python3
"""EMA8 Prototype — max-history spot backtest (baseline + optional filters).

Uses run_date_range_backtest / backtest_ema8_prototype_at_bar only (no invented rules).
Private config copy; does not write analyst/terminal8510_config.json.

.venv/bin/python scripts/run_ema8_prototype_max_days_backtest_8510.py
"""
from __future__ import annotations

import csv
import json
import logging
import os
import sys
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from analyst.backtest_lab_8510 import (  # noqa: E402
    build_report,
    month_chunks,
    normalize_rows,
    stats,
    trading_days,
    verdict,
)
from analyst.ema8_prototype_8510 import DEFAULT_CFG
from analyst.recorded_history_8510 import RecordedHistory

IST = ZoneInfo("Asia/Kolkata")
COST_PTS = 2.0
HOLDOUT_FRAC = 0.30
COMMIT = "051dff9"
REPORT_DIR = ROOT / "analyst" / "reports"
TRADES_CSV = REPORT_DIR / "ema8_prototype_max_days_trades.csv"
DAILY_CSV = REPORT_DIR / "ema8_prototype_max_days_daily.csv"
SUMMARY_MD = REPORT_DIR / "ema8_prototype_max_days_summary.md"

log = logging.getLogger("ema8_max_bt")

FILTER_PRESETS: List[Tuple[str, Dict[str, Any]]] = [
    ("baseline_all_off", {}),
    ("ribbon", {"ribbon_filter_enabled": True}),
    ("ema_separation", {"ema_separation_enabled": True}),
    ("supertrend", {"supertrend_enabled": True}),
    ("slow_market_exact_line", {"slow_market_exact_line": True}),
]


def _private_backend(tmp: Path, proto_block: Dict[str, Any]):
    from analyst.terminal8510_backend import QuantTerminalBackend

    cfg_path = ROOT / "analyst/terminal8510_config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    market = Path(str(cfg.get("market_db_path") or ""))
    cfg["market_db_path"] = str(market if market.is_absolute() else (ROOT / market))
    cfg["ledger_db_path"] = str(tmp / "ledger.sqlite3")
    scanner = cfg.setdefault("scanner", {})
    block = dict(DEFAULT_CFG)
    block.update(proto_block)
    block["enabled"] = True
    scanner["ema8_prototype"] = block
    private = tmp / "config.json"
    private.write_text(json.dumps(cfg), encoding="utf-8")
    try:
        os.chmod(private, 0o600)
    except OSError:
        pass
    return QuantTerminalBackend(str(private))


_WORKER: Any = None
_WORKER_TMP: Any = None
_WORKER_PROTO: Dict[str, Any] = {}


def _worker_init(proto_json: str) -> None:
    global _WORKER, _WORKER_TMP, _WORKER_PROTO
    _WORKER_PROTO = json.loads(proto_json)
    _WORKER_TMP = tempfile.TemporaryDirectory(prefix="ema8max_w_")
    _WORKER = _private_backend(Path(_WORKER_TMP.name), _WORKER_PROTO)


def _worker_chunk(args: Tuple[str, str]) -> Dict[str, Any]:
    c_from, c_to = args
    try:
        res = _WORKER.run_date_range_backtest(
            c_from,
            c_to,
            include_today_paper=False,
            strategy_filter="EMA8_PROTOTYPE",
            data_type_filter="SYNTHESIZED",
        )
        return {"ok": True, "from": c_from, "trades": res.get("trades") or []}
    except Exception as exc:
        return {"ok": False, "from": c_from, "error": f"{type(exc).__name__}: {exc}"[:400]}


def run_range(
    market_db: Path,
    from_day: str,
    to_day: str,
    proto_block: Dict[str, Any],
    workers: int = 3,
) -> List[Dict[str, Any]]:
    days = trading_days(market_db, from_day, to_day)
    if not days:
        return []
    chunks = month_chunks(days)
    proto_json = json.dumps(proto_block)
    all_rows: List[Dict[str, Any]] = []
    if workers <= 1 or len(chunks) == 1:
        with tempfile.TemporaryDirectory(prefix="ema8max_") as td:
            be = _private_backend(Path(td), proto_block)
            for c_from, c_to in chunks:
                res = be.run_date_range_backtest(
                    c_from,
                    c_to,
                    include_today_paper=False,
                    strategy_filter="EMA8_PROTOTYPE",
                    data_type_filter="SYNTHESIZED",
                )
                all_rows.extend(res.get("trades") or [])
        return all_rows

    with ProcessPoolExecutor(
        max_workers=min(workers, len(chunks)),
        initializer=_worker_init,
        initargs=(proto_json,),
    ) as pool:
        futs = {pool.submit(_worker_chunk, ch): ch for ch in chunks}
        for fut in as_completed(futs):
            out = fut.result()
            if not out.get("ok"):
                log.warning("chunk %s failed: %s", out.get("from"), out.get("error"))
                continue
            all_rows.extend(out.get("trades") or [])
    return all_rows


def arm_hm_from_cross(cross_ts: int) -> str:
    if not cross_ts:
        return ""
    t0 = datetime.fromtimestamp(int(cross_ts), tz=IST)
    arm = t0 + timedelta(minutes=15)
    return arm.strftime("%H:%M:%S")


def ledger_rows(raw: List[Dict[str, Any]], cost: float) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for r in raw:
        norm = normalize_rows([r])
        if not norm:
            continue
        t = norm[0]
        cross = int(r.get("cross_bar_open_ts") or 0)
        rows.append(
            {
                **t,
                "arm_day": t["day"],
                "arm_hm": arm_hm_from_cross(cross)[:8],
                "net_pts": round(t["gross"] - cost, 2),
            }
        )
    rows.sort(key=lambda x: (x["day"], x.get("entry_hm") or "", x.get("arm_hm") or ""))
    return rows


def summary_row(label: str, s: Dict[str, Any], v: Optional[Dict[str, Any]] = None) -> str:
    v = v or {}
    grade = v.get("grade") or "—"
    return (
        f"| {label} | {s['trades']} | {s['win_rate_pct']} | {s['avg_win_pts']} | {s['avg_loss_pts']} | "
        f"{s['gross_pts']} | {s['net_pts']} | {s['max_drawdown_pts']} | {s['net_if_ambiguous_were_targets']} | "
        f"{s['t_stat']} | {grade} |"
    )


def print_table(title: str, header: str, rows: List[str]) -> None:
    print()
    print(title)
    print(header)
    print("-" * len(header))
    for row in rows:
        print(row)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    market_db = ROOT / "ict_smc_app/backend/data/ict_smc.sqlite3"
    if not market_db.exists():
        print("market DB missing:", market_db, file=sys.stderr)
        return 2

    b_first, b_last = trading_days(market_db)[0], trading_days(market_db)[-1]
    b_req_from, b_req_to = "2021-01-01", "2026-11-13"
    b_from = max(b_req_from, b_first) if b_first else b_req_from
    b_to = min(b_req_to, b_last) if b_last else b_req_to

    rh = RecordedHistory(
        ROOT / "data/terminal8510/ledger.sqlite3",
        market_db,
    )
    rec_days = sorted({d for d in rh.dates() if b_from <= d <= b_to})
    a_from = rec_days[0] if rec_days else b_from
    a_to = rec_days[-1] if rec_days else b_to

    filter_results: Dict[str, Dict[str, Any]] = {}
    t0 = time.time()

    for name, overrides in FILTER_PRESETS:
        log.info("Running filter preset: %s (%s → %s)", name, b_from, b_to)
        proto = dict(overrides)
        raw = run_range(market_db, b_from, b_to, proto, workers=3)
        trades = ledger_rows(raw, COST_PTS)
        spec = {
            "from": b_from,
            "to": b_to,
            "lanes": ["EMA8_PROTOTYPE"],
            "cost_pts": COST_PTS,
            "holdout_frac": HOLDOUT_FRAC,
            "label": name,
        }
        cov = {
            "from": b_from,
            "to": b_to,
            "days_with_data": len({t["day"] for t in trades}),
            "first_day": trades[0]["day"] if trades else None,
            "last_day": trades[-1]["day"] if trades else None,
            "dataset_b_range": f"{b_from}..{b_to}",
            "dataset_a_range": f"{a_from}..{a_to}",
            "commit": COMMIT,
        }
        report = build_report([{k: v for k, v in t.items() if k != "net_pts"} for t in trades], spec, cov)
        filter_results[name] = {"trades": trades, "report": report, "raw_count": len(raw)}

    baseline = filter_results["baseline_all_off"]
    trades = baseline["trades"]
    report = baseline["report"]
    days_with_data = len({t["day"] for t in trades})

    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    with TRADES_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "day",
                "arm_hm",
                "entry_hm",
                "side",
                "entry",
                "exit_day",
                "exit_hm",
                "exit",
                "reason",
                "gross",
                "net_pts",
                "ambiguous",
                "filter_run",
            ],
        )
        w.writeheader()
        for t in trades:
            w.writerow(
                {
                    "day": t["day"],
                    "arm_hm": t.get("arm_hm"),
                    "entry_hm": t.get("entry_hm"),
                    "side": t["side"],
                    "entry": t.get("entry"),
                    "exit_day": t.get("exit_day"),
                    "exit_hm": t.get("exit_hm"),
                    "exit": t.get("exit"),
                    "reason": t.get("reason"),
                    "gross": t.get("gross"),
                    "net_pts": t.get("net_pts"),
                    "ambiguous": t.get("ambiguous"),
                    "filter_run": "baseline_all_off",
                }
            )

    cum = 0.0
    with DAILY_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["day", "trades", "gross_pts", "net_pts", "cumulative_net_pts"])
        w.writeheader()
        by_day: Dict[str, Dict[str, Any]] = {}
        for t in trades:
            d = by_day.setdefault(t["day"], {"trades": 0, "gross": 0.0, "net": 0.0})
            d["trades"] += 1
            d["gross"] += float(t["gross"])
            d["net"] += float(t["net_pts"])
        for day in sorted(by_day):
            row = by_day[day]
            cum += row["net"]
            w.writerow(
                {
                    "day": day,
                    "trades": row["trades"],
                    "gross_pts": round(row["gross"], 1),
                    "net_pts": round(row["net"], 1),
                    "cumulative_net_pts": round(cum, 1),
                }
            )

    lines: List[str] = [
        "# EMA8 Prototype — maximum available history backtest",
        "",
        f"- **Code anchor:** `{COMMIT}`",
        f"- **Instrument:** NIFTY 50 spot index points only",
        f"- **Cost:** {COST_PTS} spot points per trade (round trip)",
        "- **Option ±40 premium exits are not simulated** (spot ±40 / opposite 15m close / 15:20 only).",
        f"- **Elapsed:** {round(time.time() - t0, 1)} s",
        "",
        "## Data coverage",
        "",
        f"- **Dataset B (multi-year request):** `{b_req_from}` → `{b_req_to}` — "
        f"**{days_with_data} session days with 1m candles** in `{market_db.name}` (`{b_from}` → `{b_to}`).",
        f"- **Dataset A (app recorded / replay DB):** `{a_from}` → `{a_to}` ({len(rec_days)} calendar dates in ledger ∪ spot DB).",
        "- Local archive does not contain the full ~1,264-day 2021–2026 pull referenced in the design doc; "
        "results below reflect **all spot 1m data present in the repo**.",
        "",
        "## Baseline (all optional filters OFF — `DEFAULT_CFG` on private config)",
        "",
        "| Split | Trades | Win% | Avg win | Avg loss | Gross | Net | Max DD | Optimistic dual-hit swing | t-stat | Verdict |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
        summary_row("Whole period", report["all"], report["verdict"]),
        summary_row("Train (first 70% days)", report["earlier"]),
        summary_row("Held-out (last 30% days)", report["held_out"], report["verdict"]),
        "",
        f"**Held-out read:** {report['verdict']['text']}",
        "",
        "## Optional filters vs baseline (full Dataset B range)",
        "",
        "| Filter | Trades | Net pts | Win% | Held-out net | Held-out t | vs baseline net |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    base_net = float(report["all"]["net_pts"])
    for fname, bundle in filter_results.items():
        r = bundle["report"]
        h = r["held_out"]
        delta = round(float(r["all"]["net_pts"]) - base_net, 1)
        lines.append(
            f"| {fname} | {r['all']['trades']} | {r['all']['net_pts']} | {r['all']['win_rate_pct']} | "
            f"{h['net_pts']} | {h['t_stat']} | {delta:+g} |"
        )

    lines += [
        "",
        "## Daily breakdown (baseline)",
        "",
        "| Date | Trades | Gross | Net (after cost) | Cum. net |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    run_cum = 0.0
    for d in report["days"]:
        run_cum += float(d["net_pts"])
        lines.append(
            f"| {d['day']} | {d['trades']} | — | {d['net_pts']} | {round(run_cum, 1)} |"
        )

    first20 = trades[:20]
    last25 = trades[-25:] if len(trades) >= 25 else trades
    lines += [
        "",
        "## Sample trade ledger (baseline)",
        "",
        f"Full CSV: `{TRADES_CSV}`",
        "",
        "### First 20 trades",
        "",
        "| Date | Arm | Entry | Side | Entry spot | Exit | Exit spot | Reason | Net pts |",
        "| --- | --- | --- | --- | ---: | --- | ---: | --- | ---: |",
    ]
    for t in first20:
        lines.append(
            f"| {t['day']} | {t.get('arm_hm','')} | {t.get('entry_hm','')} | {t['side']} | "
            f"{t.get('entry')} | {t.get('exit_hm','')} | {t.get('exit')} | {t.get('reason')} | {t.get('net_pts')} |"
        )
    lines += [
        "",
        "### Last 25 trades (includes 2026-11-13 when present)",
        "",
        "| Date | Arm | Entry | Side | Entry spot | Exit | Exit spot | Reason | Net pts |",
        "| --- | --- | --- | --- | ---: | --- | ---: | --- | ---: |",
    ]
    for t in (trades[-25:] if len(trades) >= 25 else trades):
        lines.append(
            f"| {t['day']} | {t.get('arm_hm','')} | {t.get('entry_hm','')} | {t['side']} | "
            f"{t.get('entry')} | {t.get('exit_hm','')} | {t.get('exit')} | {t.get('reason')} | {t.get('net_pts')} |"
        )

    SUMMARY_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")

    hdr = (
        "| Split | Trades | Win% | AvgWin | AvgLoss | Gross | Net | MaxDD | Opt.dual-hit | t-stat | Verdict |"
    )
    print_table(
        "=== EMA8 PROTOTYPE BASELINE (spot, 2pt/trade, option exit NOT modelled) ===",
        hdr,
        [
            summary_row("ALL", report["all"], report["verdict"]),
            summary_row("TRAIN 70%", report["earlier"]),
            summary_row("HELD-OUT 30%", report["held_out"], report["verdict"]),
        ],
    )
    print(f"\nVerdict: {report['verdict']['grade']} — {report['verdict']['text']}")
    print(f"\nReports:\n  {TRADES_CSV}\n  {DAILY_CSV}\n  {SUMMARY_MD}")
    print(f"\nCreated: {TRADES_CSV}")
    print(f"Created: {DAILY_CSV}")
    print(f"Created: {SUMMARY_MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
