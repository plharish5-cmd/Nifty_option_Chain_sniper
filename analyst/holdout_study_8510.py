"""Hold-out study for the 8510 strategies.

WHY THIS EXISTS
    Tuning a rule on some days and then quoting its result on those same days
    always finds something. On 11 Nov 2026 the Ribbon filter "improved" the
    8 EMA Prototype by +126 points in one run and "hurt" it by -74 in the
    next, on the same seven days. A rule is only believable if it was chosen
    on one set of days and then still works on days it has never seen.

WHAT IT DOES
    1. Lists the trading days in the market database.
    2. Splits them by date: the last `test_days` are held out; the
       `train_days` before them are for choosing.
    3. Runs every variant through the app's own `run_date_range_backtest`
       (no separate re-implementation) on both sets.
    4. Charges `cost_pts` spot points per trade.
    5. Picks the winner on the TRAIN days only, and reports that one
       variant's TEST result as the answer. The other test results are
       printed for reference and marked: choosing from them is the mistake
       this study exists to prevent.

    A variant is a set of overrides for one config block (by default
    `scanner.ema8_prototype`). The list is fixed before the run.

RUN
    python3 -m analyst.holdout_study_8510
    python3 -m analyst.holdout_study_8510 --train-days 170 --test-days 84 --cost-pts 2.0

Nothing here places an order, changes the live desk's state, or edits the
config file: overrides live on a copy for the length of one backtest call.
"""
from __future__ import annotations

import argparse
import copy
import math
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Fixed before any run. Add a line here to test a new idea; do not add one
# after looking at the test-day column.
PROTOTYPE_VARIANTS: Dict[str, Dict[str, Any]] = {
    "baseline (config as it is)": {},
    "morning only (no new entry from 11:00)": {"no_entry_from_hm": "11:00"},
    "first cross only (no re-entry)": {"reentry_same_side": False},
    "SuperTrend(5,1) ON": {"supertrend_enabled": True},
    "Ribbon ON": {"ribbon_filter_enabled": True},
    "EMA separation ON": {"ema_separation_enabled": True},
    "exact line (tolerance 0)": {"slow_market_exact_line": True},
    "touch 3 points": {"touch_tol_pts": 3.0, "slow_market_exact_line": False},
    "touch 10 points": {"touch_tol_pts": 10.0, "slow_market_exact_line": False},
}

# Ideas that cannot be tested yet, and why. Printed in every report so they
# are not mistaken for "tested and rejected".
NOT_TESTABLE_YET: Dict[str, str] = {
    "OI direction filter (CE/PE change in OI)": (
        "placeholder only: nothing writes the OI bias, and no historical OI bias series exists to replay"
    ),
    "daily + 1H trend agreement for the Prototype": "not built for this lane (exists only on the Flagship OR entry)",
    "option +/-40 exit": "cannot be modelled from spot candles; needs recorded option candles",
}

MIN_TRADES_TO_CHOOSE = 30      # a variant with fewer train trades cannot be the pick
PASS_T_STAT = 2.0              # the pick must clear this on the test days, after cost
MIN_MINUTES_FOR_A_SESSION = 200  # skips the 1-hour Muhurat session


def trading_days(market_db: Path) -> List[str]:
    """IST dates with a normal session of NIFTY spot 1-minute candles, oldest first."""
    with sqlite3.connect(f"file:{market_db}?mode=ro", uri=True, timeout=30) as conn:
        rows = conn.execute(
            "SELECT date(ts, 'unixepoch', '+330 minutes') AS d, COUNT(*) "
            "FROM candles_1m WHERE sec_id='13' GROUP BY d ORDER BY d"
        ).fetchall()
    return [str(d) for d, n in rows if int(n) >= MIN_MINUTES_FOR_A_SESSION]


def split_days(days: List[str], train_days: int, test_days: int) -> Tuple[List[str], List[str]]:
    """Last `test_days` are held out; the `train_days` before them are for choosing."""
    if train_days <= 0 or test_days <= 0:
        raise ValueError("train_days and test_days must be positive")
    if len(days) < train_days + test_days:
        raise ValueError(
            f"Only {len(days)} full sessions in the database; "
            f"{train_days} + {test_days} = {train_days + test_days} were asked for."
        )
    test = days[-test_days:]
    train = days[-(train_days + test_days):-test_days]
    return train, test


def score(rows: List[Dict[str, Any]], cost_pts: float) -> Dict[str, Any]:
    """Points statistics for one set of backtested rows, after a per-trade cost."""
    rows = sorted(rows, key=lambda r: (str(r.get("entry_day") or ""), str(r.get("entry_hm") or "")))
    gross = [float(r.get("st_pnl_pts") or 0.0) for r in rows]
    net = [g - cost_pts for g in gross]
    n = len(net)
    out: Dict[str, Any] = {
        "trades": n, "wins": 0, "win_rate_pct": 0.0, "gross_pts": 0.0, "net_pts": 0.0,
        "avg_net_pts": 0.0, "t_stat": 0.0, "max_drawdown_pts": 0.0, "max_losing_run": 0,
        "ambiguous": 0, "net_pts_if_ambiguous_were_targets": 0.0, "exit_reasons": {},
    }
    if n == 0:
        return out
    mean = sum(net) / n
    var = sum((x - mean) ** 2 for x in net) / (n - 1) if n > 1 else 0.0
    sd = math.sqrt(var)
    peak = run = dd = 0.0
    losing = worst = 0
    for x in net:
        run += x
        peak = max(peak, run)
        dd = min(dd, run - peak)
        losing = losing + 1 if x <= 0 else 0
        worst = max(worst, losing)
    reasons: Dict[str, Dict[str, float]] = {}
    amb = 0
    amb_swing = 0.0
    for r, g in zip(rows, gross):
        key = str(r.get("exit_reason") or "?")
        slot = reasons.setdefault(key, {"trades": 0, "gross_pts": 0.0})
        slot["trades"] += 1
        slot["gross_pts"] = round(slot["gross_pts"] + g, 2)
        if r.get("is_ambiguous"):
            amb += 1
            # booked as a stop; had it been the target instead
            amb_swing += float(r.get("spot_target_pts") or 40.0) - g
    out.update(
        trades=n,
        wins=sum(1 for x in gross if x > 0),
        win_rate_pct=round(100.0 * sum(1 for x in gross if x > 0) / n, 1),
        gross_pts=round(sum(gross), 1),
        net_pts=round(sum(net), 1),
        avg_net_pts=round(mean, 2),
        t_stat=round(mean / (sd / math.sqrt(n)), 2) if sd > 0 and n > 1 else 0.0,
        max_drawdown_pts=round(dd, 1),
        max_losing_run=worst,
        ambiguous=amb,
        net_pts_if_ambiguous_were_targets=round(sum(net) + amb_swing, 1),
        exit_reasons=reasons,
    )
    return out


def _backtest_rows(backend: Any, days: List[str], strategy_filter: str) -> List[Dict[str, Any]]:
    res = backend.run_date_range_backtest(
        days[0], days[-1], include_today_paper=False,
        strategy_filter=strategy_filter, data_type_filter="SYNTHESIZED",
    )
    if not res.get("ok", True):
        raise RuntimeError(f"Backtest failed for {days[0]}..{days[-1]}: {res.get('error')}")
    wanted = set(days)
    return [r for r in (res.get("trades") or []) if str(r.get("entry_day") or "") in wanted]


def run_study(
    backend: Any,
    *,
    train_days: int = 170,
    test_days: int = 84,
    cost_pts: float = 2.0,
    variants: Optional[Dict[str, Dict[str, Any]]] = None,
    config_block: str = "ema8_prototype",
    strategy_filter: str = "EMA8_PROTOTYPE",
) -> Dict[str, Any]:
    variants = dict(variants if variants is not None else PROTOTYPE_VARIANTS)
    days = trading_days(Path(backend.market_db))
    train, test = split_days(days, train_days, test_days)

    live_cfg = backend.scanner_cfg
    results: List[Dict[str, Any]] = []
    try:
        for name, overrides in variants.items():
            cfg = copy.deepcopy(live_cfg)
            block = dict(cfg.get(config_block) or {})
            block.update(overrides)
            cfg[config_block] = block
            backend.scanner_cfg = cfg
            results.append({
                "variant": name,
                "overrides": overrides,
                "train": score(_backtest_rows(backend, train, strategy_filter), cost_pts),
                "test": score(_backtest_rows(backend, test, strategy_filter), cost_pts),
            })
    finally:
        backend.scanner_cfg = live_cfg      # the live desk's config object, untouched

    eligible = [r for r in results if r["train"]["trades"] >= MIN_TRADES_TO_CHOOSE]
    pick = max(eligible, key=lambda r: r["train"]["avg_net_pts"]) if eligible else None
    verdict = "NO PICK — no variant had enough trades on the train days"
    if pick is not None:
        t = pick["test"]
        passed = t["net_pts"] > 0 and t["t_stat"] >= PASS_T_STAT
        verdict = (
            f"PASS — '{pick['variant']}' held up on unseen days"
            if passed
            else f"FAIL — '{pick['variant']}' was the best on the train days and did not hold up on unseen days"
        )
    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "strategy_filter": strategy_filter,
        "cost_pts": cost_pts,
        "train_range": (train[0], train[-1], len(train)),
        "test_range": (test[0], test[-1], len(test)),
        "results": results,
        "pick": pick["variant"] if pick else None,
        "verdict": verdict,
        "pass_rule": f"test net points > 0 and t-stat >= {PASS_T_STAT}, after {cost_pts:g} points cost per trade",
        "not_testable_yet": NOT_TESTABLE_YET,
    }


def render_markdown(study: Dict[str, Any]) -> str:
    tr, te = study["train_range"], study["test_range"]
    lines = [
        f"# Hold-out study — {study['strategy_filter']}",
        "",
        f"Generated {study['generated_at']} · cost {study['cost_pts']:g} spot points per trade · results in spot points",
        "",
        f"- **Train (choose here):** {tr[0]} to {tr[1]} — {tr[2]} sessions",
        f"- **Test (never used for choosing):** {te[0]} to {te[1]} — {te[2]} sessions",
        f"- **Pass rule, fixed before the run:** {study['pass_rule']}",
        "",
        f"## Verdict: {study['verdict']}",
        "",
        "## Train days",
        "",
        "| Variant | Trades | Win % | Net pts | Avg / trade | t | Max drawdown | Worst losing run |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in study["results"]:
        s = r["train"]
        mark = " **← pick**" if r["variant"] == study["pick"] else ""
        lines.append(
            f"| {r['variant']}{mark} | {s['trades']} | {s['win_rate_pct']} | {s['net_pts']} | "
            f"{s['avg_net_pts']} | {s['t_stat']} | {s['max_drawdown_pts']} | {s['max_losing_run']} |"
        )
    lines += [
        "",
        "## Test days",
        "",
        "Only the row marked **pick** is the answer. The other rows are shown for reference; "
        "choosing a different variant because its test row looks better turns the test days into train days.",
        "",
        "| Variant | Trades | Win % | Net pts | Avg / trade | t | Max drawdown | Worst losing run | Ambiguous | Net if ambiguous were targets |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in study["results"]:
        s = r["test"]
        mark = " **← pick**" if r["variant"] == study["pick"] else ""
        lines.append(
            f"| {r['variant']}{mark} | {s['trades']} | {s['win_rate_pct']} | {s['net_pts']} | "
            f"{s['avg_net_pts']} | {s['t_stat']} | {s['max_drawdown_pts']} | {s['max_losing_run']} | "
            f"{s['ambiguous']} | {s['net_pts_if_ambiguous_were_targets']} |"
        )
    pick = next((r for r in study["results"] if r["variant"] == study["pick"]), None)
    if pick:
        lines += ["", f"## Where the pick's test-day points came from", "",
                  "| Exit | Trades | Gross pts |", "|---|---:|---:|"]
        for reason, v in sorted(pick["test"]["exit_reasons"].items()):
            lines.append(f"| {reason} | {int(v['trades'])} | {v['gross_pts']} |")
    lines += ["", "## Not testable yet", ""]
    for idea, why in study["not_testable_yet"].items():
        lines.append(f"- **{idea}** — {why}")
    lines += [
        "",
        "## Limits",
        "",
        "- Spot points only. The option ±40 exit is not modelled, so live exits can differ.",
        "- Entry is booked at spot on the tick of the touch; real fills will be worse by the spread.",
        "- The cost per trade is one number for every trade; it does not change with premium or volatility.",
        "- One split of the calendar. A different year can behave differently.",
        "",
    ]
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Hold-out study for the 8 EMA Prototype")
    ap.add_argument("--train-days", type=int, default=170)
    ap.add_argument("--test-days", type=int, default=84)
    ap.add_argument("--cost-pts", type=float, default=2.0)
    ap.add_argument("--out-dir", default="data/terminal8510/holdout")
    args = ap.parse_args(argv)

    from analyst.terminal8510_backend import QuantTerminalBackend

    backend = QuantTerminalBackend()
    study = run_study(backend, train_days=args.train_days, test_days=args.test_days, cost_pts=args.cost_pts)
    text = render_markdown(study)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"holdout_{datetime.now().strftime('%Y%m%d_%H%M')}.md"
    path.write_text(text, encoding="utf-8")
    print(text)
    print(f"\nSaved: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
