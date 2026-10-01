"""
acceptance_test_order_flow.py — Replay Acceptance Test for Module I (Order Flow DOM Layer).

Replays NIFTY option recordings with 4 pre-paper-trade fixes:
1. Tightened absorption filter (within 3 ticks of persisted wall + >= 2 pulses)
2. Direction (LONG/SHORT) and Signal Type (wall-hold / absorption / break) cell performance matrix
3. Exclusion of stock recordings (GRASIM dropped; NIFTY options only)
4. Dedicated reporting for wall-break signals
5. Multi-day gate evaluation (5 days spanning trend-up, trend-down, chop; clock held in SIM-ONLY)
"""
from __future__ import annotations

import json
from pathlib import Path
import sys

# Ensure repo root is on sys.path
REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import order_flow_dom


def run_test() -> int:
    print("=" * 78)
    print("MODULE I — ORDER FLOW LAYER (STRIKE-LEVEL DOM) ACCEPTANCE REPLAY TEST")
    print("=" * 78)

    cfg = order_flow_dom.load_order_flow_config()
    print("Active Configuration (JSON Thresholds):")
    for k in [
        "wall_threshold", "wall_min_seconds", "absorb_threshold", "absorb_window_seconds",
        "absorb_near_wall_ticks", "absorb_min_pulses", "sweep_ratio", "break_multiplier",
        "min_confirm_win_rate", "min_multi_day_required", "paper_trade_clock_started", "veto_mode"
    ]:
        if k in cfg:
            print(f"  • {k}: {cfg[k]}")
    print("-" * 78)

    results = order_flow_dom.run_order_flow_acceptance_test(
        sample_date="2026-08-24",
        sample_symbol="NIFTY24250-CE",
        live_day_date="2026-08-21",
        live_day_symbol="GRASIM",
    )

    for target_name, res in results.items():
        if res.get("status") == "DROPPED_PER_FIX3":
            print(f"\n[FIX 3] Target: {target_name.upper()} ({res.get('symbol')}) -> DROPPED")
            print(f"  Reason: {res.get('reason')}")
            continue

        print(f"\nTarget: {target_name.upper()} ({res.get('symbol')}) | Date: {res.get('date')}")
        print(f"  Duration (1s bars): {res.get('total_bars_seconds'):,} seconds")
        print(f"  Persisted Walls (>=20s, >=25k contracts): {res.get('walls_detected')}")
        print(f"  Defended S/R Walls Survived: {res.get('survived_sweeps')} | Broken Walls: {res.get('broken_walls')}")
        print(f"  Sweep Survival Precision: {res.get('sweep_survival_precision')}%")
        print(f"  CVD Divergences Detected: {res.get('cvd_divergences')}")
        print(f"  Liquidity Breaks Detected: {res.get('liquidity_breaks')}")

        # Fix 1: Before/After absorption count
        print("\n  [FIX 1] ABSORPTION FILTER BEFORE / AFTER COUNT:")
        print(f"    • Raw candidate events (volume >= 15k, <= 2 ticks): {res.get('raw_candidate_absorptions')}")
        print(f"    • Rejected by near-wall / pulse filter:             {res.get('rejected_absorptions_count')}")
        rej_breakdown = res.get("rejected_by_reason", {})
        for reason_k, reason_cnt in rej_breakdown.items():
            print(f"        - {reason_k}: {reason_cnt}")
        print(f"    • Passed tightened absorption events:              {res.get('passed_absorptions_count')}")

        # Fix 2 & 4: Cell Performance Matrix
        print("\n  [FIX 2 & 4] REPLAY PERFORMANCE BY CELL (DIRECTION × TYPE):")
        matrix = res.get("cell_matrix", {})
        hdr = "  {:<20} | {:<5} | {:<8} | {:<11} | {:<8} | {:<11}".format("Cell", "Count", "+5m WR", "+5m Avg Ret", "+15m WR", "+15m Avg Ret")
        print(hdr)
        print("  " + "-" * (len(hdr) - 2))
        for cell_k, c_data in sorted(matrix.items()):
            cnt = c_data.get("count", 0)
            wr5 = f"{c_data.get('win_rate_5m_pct', 0.0)}%" if cnt > 0 else "N/A"
            ar5 = f"{c_data.get('avg_return_5m_pct', 0.0):+.2f}%" if cnt > 0 else "N/A"
            wr15 = f"{c_data.get('win_rate_15m_pct', 0.0)}%" if cnt > 0 else "N/A"
            ar15 = f"{c_data.get('avg_return_15m_pct', 0.0):+.2f}%" if cnt > 0 else "N/A"
            print("  {:<20} | {:<5} | {:<8} | {:<11} | {:<8} | {:<11}".format(cell_k, cnt, wr5, ar5, wr15, ar15))

        dom_cell = res.get("dominant_cell")
        print(f"\n  Dominant Cell: {dom_cell}")
        print(f"    • Count: {res.get('dominant_cell_count')}")
        print(f"    • Win Rate (+15m): {res.get('dominant_cell_win_rate_15m_pct')}% (Avg Return: {res.get('dominant_cell_avg_return_15m_pct'):+.2f}%)")
        print(f"    • Single-Day Dominant Cell Gate (>= 60%): {'PASSED ✅' if res.get('dominant_cell_meets_60pct') else 'FAILED ✗'}")

        # Multi-Day Gate Status
        mg = res.get("multi_day_gate", {})
        print("\n  [MULTI-DAY GATE & PAPER-TRADE CLOCK STATUS]:")
        print(f"    • Days Recorded: {mg.get('days_recorded')} / {mg.get('days_required')} required")
        print(f"    • Regimes Spanned: {mg.get('regimes_spanned')} / {mg.get('regimes_required')} required")
        print(f"    • 2-Week Paper-Trade Clock: {mg.get('paper_trade_clock_status')}")
        print(f"    • Live Execution Enforcement: {mg.get('veto_mode')} (Veto stays SIM-only throughout)")

    print("\n" + "=" * 78)
    print("JOURNAL & REPLAY AUDIT COMPLETE")
    print(f"Journal logged to: {cfg.get('journal_log_path')}")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(run_test())

