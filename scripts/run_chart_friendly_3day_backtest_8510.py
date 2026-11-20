#!/usr/bin/env python3
"""3-day spot backtest with chart-friendly scanner defaults (private config only)."""
from __future__ import annotations

import csv
import json
import os
import sys
import tempfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from analyst.backtest_lab_8510 import LANE_LABEL, normalize_rows, stats
from analyst.terminal8510_backend import QuantTerminalBackend

FROM_DAY = "2026-11-16"
TO_DAY = "2026-11-18"
COST_PTS = 2.0
LANES = ["EMA8_PROTOTYPE", "STAGE_2", "STAGE_3", "CROSSOVER", "FLAGSHIP"]
REPORT_DIR = ROOT / "analyst" / "reports"
TRADES_CSV = REPORT_DIR / f"chart_friendly_3day_trades_{FROM_DAY}_{TO_DAY}.csv"
SUMMARY_MD = REPORT_DIR / f"chart_friendly_3day_summary_{FROM_DAY}_{TO_DAY}.md"


def apply_chart_friendly(cfg: Dict[str, Any]) -> None:
    """Same knobs as scripts/patch_chart_friendly_defaults_8510.py (in-memory)."""
    proto = cfg.setdefault("scanner", {}).setdefault("ema8_prototype", {})
    proto["retouch_requires_away"] = False
    proto["reentry_same_side"] = True
    proto["reentry_after_stop_loss"] = True
    proto.setdefault("execution_mode", "LIVE_ALLOWED")
    proto.setdefault("live_trade_enabled", False)
    oi = proto.setdefault("oi_filters", {})
    oi["mode"] = "log_only"
    rev = cfg["scanner"].setdefault("reversal", {})
    rev_oi = rev.setdefault("oi_filters", {})
    rev_oi["mode"] = "log_only"
    rev.setdefault("execution_mode", "LIVE_ALLOWED")
    rev.setdefault("live_trade_enabled", False)
    fp = cfg["scanner"].setdefault("flagship_1h_pa", {})
    fp.setdefault("execution_mode", "LIVE_ALLOWED")
    fp.setdefault("live_trade_enabled", False)
    radar = cfg.setdefault("ema_proximity_radar", {})
    radar["enabled"] = False
    radar["auto_highlight_strike_matrix"] = False
    syn = cfg.setdefault("dhan_oi_synergy", {})
    syn["show_pe_ce_table_in_tab1"] = False
    syn.setdefault("filter_stage2_8ema_synergy", False)
    syn.setdefault("filter_strategy2_reversal_synergy", False)
    syn.setdefault("filter_strategy3_1h_synergy", False)
    syn.setdefault("block_pe_when_bullish", False)
    syn.setdefault("block_ce_when_bearish", False)
    ui = cfg.setdefault("desk_ui", {})
    ui["show_trend_watchlist"] = False
    ui["show_five_factor_confluence"] = False
    ui["show_eod_summary_card"] = False
    ui["show_desk_link_strip"] = False
    ui["show_dhan_api_panel"] = False
    ff = cfg.setdefault("five_factor_confluence", {})
    ff["enabled"] = False
    ff["scanner_gate_mode"] = "off"
    ff["scanner_min_score"] = 0


def private_backend(tmp: Path) -> QuantTerminalBackend:
    cfg_path = ROOT / "analyst/terminal8510_config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    market = Path(str(cfg.get("market_db_path") or "ict_smc_app/backend/data/ict_smc.sqlite3"))
    cfg["market_db_path"] = str(market if market.is_absolute() else (ROOT / market))
    cfg["ledger_db_path"] = str(tmp / "ledger.sqlite3")
    apply_chart_friendly(cfg)
    private = tmp / "config.json"
    private.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    try:
        os.chmod(private, 0o600)
    except OSError:
        pass
    return QuantTerminalBackend(str(private))


def flatten_trade(r: Dict[str, Any], norm: Dict[str, Any]) -> Dict[str, Any]:
    entry_day = str(r.get("entry_day") or r.get("date_only") or "")[:10]
    exit_day = str(r.get("exit_day") or entry_day)[:10]
    ent_hm = str(r.get("entry_hm") or "")[:5]
    ex_hm = str(r.get("exit_hm") or "")[:5]
    gross = float(r.get("st_pnl_pts") or 0.0)
    return {
        "trade_id": r.get("Trade_ID"),
        "lane": norm.get("lane") or "",
        "lane_label": LANE_LABEL.get(norm.get("lane") or "", norm.get("lane")),
        "strategy_id": r.get("strategy_id"),
        "setup_name": r.get("Setup_Name"),
        "direction": r.get("Direction"),
        "entry_time_ist": f"{entry_day} {ent_hm}" if entry_day and ent_hm else r.get("Date"),
        "exit_time_ist": f"{exit_day} {ex_hm}" if exit_day and ex_hm else "",
        "entry_spot": r.get("Index_Entry"),
        "exit_spot": r.get("Index_Exit"),
        "gross_pnl_pts": round(gross, 2),
        "cost_pts": COST_PTS,
        "net_pnl_pts": round(gross - COST_PTS, 2),
        "exit_reason": r.get("exit_reason") or r.get("Exit_Reason"),
        "ambiguous_1m_bar": bool(r.get("is_ambiguous")),
        "data_source": r.get("data_source_tag") or r.get("Data_Type"),
    }


def main() -> int:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="chart_bt3_") as td:
        be = private_backend(Path(td))
        res = be.run_date_range_backtest(
            FROM_DAY,
            TO_DAY,
            include_today_paper=False,
            strategy_filter="ALL",
            data_type_filter="SYNTHESIZED",
        )
        if not res.get("ok", True):
            print("backtest failed:", res.get("error"), file=sys.stderr)
            return 1
        raw_rows: List[Dict[str, Any]] = list(res.get("trades") or [])
        normed = normalize_rows(raw_rows)
        norm_by_id = {}
        for n in normed:
            key = (n["day"], n["entry_hm"], n["lane"], n.get("side"))
            norm_by_id[key] = n

        out_rows: List[Dict[str, Any]] = []
        for r in raw_rows:
            day = str(r.get("entry_day") or r.get("date_only") or "")[:10]
            if day < FROM_DAY or day > TO_DAY:
                continue
            ent_hm = str(r.get("entry_hm") or "")[:5]
            from analyst.backtest_lab_8510 import lane_of

            lane = lane_of(r.get("strategy_id"), r.get("Setup_Name"))
            if lane not in LANES:
                continue
            n = norm_by_id.get((day, ent_hm, lane, "PE" if "SHORT" in str(r.get("Direction") or "").upper() else "CE"))
            out_rows.append(flatten_trade(r, n or {"lane": lane}))

        out_rows.sort(key=lambda x: (x["entry_time_ist"] or "", x["lane"] or ""))

        fields = list(out_rows[0].keys()) if out_rows else [
            "trade_id", "lane", "entry_time_ist", "exit_time_ist", "entry_spot", "exit_spot", "net_pnl_pts", "exit_reason",
        ]
        with TRADES_CSV.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            w.writerows(out_rows)

        by_day: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for row in out_rows:
            d = (row.get("entry_time_ist") or "")[:10]
            if d:
                by_day[d].append(row)

        lab_trades = [
            {
                "day": (t["entry_time_ist"] or "")[:10],
                "gross": t["gross_pnl_pts"],
                "lane": t["lane"],
            }
            for t in out_rows
        ]
        overall = stats(lab_trades, COST_PTS)

        lines = [
            f"# Chart-friendly 3-day backtest ({FROM_DAY} .. {TO_DAY})",
            "",
            f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} IST",
            "",
            "## Assumptions (matches desk chart-friendly patch)",
            "",
            "- `retouch_requires_away` = **OFF** (Prototype)",
            "- Prototype + Reversal OI filters = **log_only** (not replayed in synth backtest)",
            "- Re-entry same side + after SL = **ON**",
            "- Dhan OI synergy gates, radar, five-factor confluence = **OFF**",
            "- Simulation: **1m NIFTY spot** walk; **spot points**; **₹2 cost/trade** deducted for net",
            "- **8600 board OI filters are not replayed** in this engine path",
            "",
            f"Market DB: `{be.market_db}`",
            "",
            "## Totals",
            "",
            f"- Trades: **{overall['trades']}**",
            f"- Gross spot pts: **{overall['gross_pts']:+}**",
            f"- Net after {COST_PTS} pts/trade: **{overall['net_pts']:+}**",
            f"- Win rate: **{overall['win_rate_pct']}%**",
            f"- Wins / losses: **{overall['wins']}** / **{overall['losses']}**",
            "",
            "## By day",
            "",
        ]
        for day in sorted(by_day.keys()):
            ds = stats(
                [{"day": day, "gross": r["gross_pnl_pts"], "lane": r["lane"]} for r in by_day[day]],
                COST_PTS,
            )
            lines.append(
                f"- **{day}**: {ds['trades']} trades, net **{ds['net_pts']:+.1f}** pts "
                f"(W/L {ds['wins']}/{ds['losses']})"
            )

        by_lane: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for t in out_rows:
            by_lane[t["lane"]].append(t)
        lines.extend(["", "## By lane", ""])
        for lane in LANES:
            if lane not in by_lane:
                lines.append(f"- **{LANE_LABEL.get(lane, lane)}**: 0 trades")
                continue
            ls = stats(
                [{"day": (r["entry_time_ist"] or "")[:10], "gross": r["gross_pnl_pts"], "lane": lane} for r in by_lane[lane]],
                COST_PTS,
            )
            lines.append(
                f"- **{LANE_LABEL.get(lane, lane)}**: {ls['trades']} trades, net **{ls['net_pts']:+.1f}** pts"
            )

        lines.extend(
            [
                "",
                "## Trade log",
                "",
                f"Full CSV: `{TRADES_CSV}`",
                "",
                "| Entry (IST) | Exit (IST) | Lane | Dir | Entry spot | Exit spot | Gross | Net | Reason |",
                "|---|---|---|---|---:|---:|---:|---:|---|",
            ]
        )
        for t in out_rows:
            lines.append(
                f"| {t.get('entry_time_ist','')} | {t.get('exit_time_ist','')} | {t.get('lane_label','')} "
                f"| {t.get('direction','')} | {t.get('entry_spot','')} | {t.get('exit_spot','')} "
                f"| {t.get('gross_pnl_pts',0):+.1f} | {t.get('net_pnl_pts',0):+.1f} | {t.get('exit_reason','')} |"
            )

        SUMMARY_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print("trades_csv:", TRADES_CSV)
        print("summary_md:", SUMMARY_MD)
        print(f"trades={overall['trades']} net_pts={overall['net_pts']:+.1f}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())