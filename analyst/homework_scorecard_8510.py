"""Daily homework -> algo reconciliation scorecard (owner request, 27 Nov 2026).

You do your homework on the finished 15m chart and write what each of your three
strategies SHOULD have done. This module replays the algo's own trades for the
same day and grades them against your homework, trade by trade, so the gap
between "what I see" and "what the algo did" becomes a named, fixable list
instead of a feeling.

Homework CSV (one row per trade you expected), header exactly:
    date,strategy,side,entry_time,entry_level,ema8_at_entry,exit_time,exit_level,notes
  - date         2026-11-26
  - strategy     prototype | reversal | flagship   (blank = any lane)
  - side         CE | PE
  - entry_time   09:15            (IST, 24h HH:MM)
  - entry_level  25768            (NIFTY index level where YOU entered)
  - ema8_at_entry 25760           (the 8 EMA value at entry — for audit; optional)
  - exit_time    11:00
  - exit_level   25927
  - notes        free text

NIFTY points are measured from the index levels you wrote (your chart truth):
    CE:  exit_level - entry_level      PE:  entry_level - exit_level
The algo's trades are matched by side + entry time (within a tolerance) and the
scorecard reports, per homework trade, one verdict:
    MATCH            algo took it, same side, comparable capture
    UNDER_CAPTURED   algo took it but exited far short of your points
    PREMATURE_SL     algo entered right but exited on a full stop you didn't
    WRONG_SIDE       algo traded the opposite side at that time
    MISSED           algo took nothing in the window (feed / cadence / gate / entry rule)
and lists EXTRA algo trades that your homework did not mark.

Pure/offline: parse + match + score take plain dicts and are unit-tested. The
CLI pulls the algo's trades from the recorded ledger on your machine.
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

HOMEWORK_HEADER = [
    "date", "strategy", "side", "entry_time", "entry_level",
    "ema8_at_entry", "exit_time", "exit_level", "notes",
]
DEFAULT_MATCH_TOL_MIN = 20
# Algo counts as capturing the move if it keeps at least this fraction of your pts.
DEFAULT_CAPTURE_OK = 0.6


@dataclass
class HomeworkTrade:
    date: str
    strategy: str
    side: str            # CE | PE
    entry_time: str      # HH:MM
    entry_level: float
    exit_time: str
    exit_level: float
    ema8_at_entry: Optional[float] = None
    notes: str = ""

    @property
    def nifty_points(self) -> float:
        return (self.exit_level - self.entry_level) if self.side == "CE" else (self.entry_level - self.exit_level)


@dataclass
class AlgoTrade:
    side: str                     # CE | PE
    entry_hm: str                 # HH:MM
    exit_hm: str
    option_points: float
    net_pnl: Optional[float]
    exit_reason: str
    strategy: str = ""
    nifty_points: Optional[float] = None   # filled only if index levels were enriched


@dataclass
class TradeVerdict:
    homework: Optional[HomeworkTrade]
    algo: Optional[AlgoTrade]
    verdict: str
    detail: str


@dataclass
class ScoreCard:
    date: str
    verdicts: List[TradeVerdict] = field(default_factory=list)
    extra_algo: List[AlgoTrade] = field(default_factory=list)

    @property
    def homework_points(self) -> float:
        return round(sum(v.homework.nifty_points for v in self.verdicts if v.homework), 1)

    @property
    def matched_count(self) -> int:
        return sum(1 for v in self.verdicts if v.verdict == "MATCH")

    @property
    def missed_count(self) -> int:
        return sum(1 for v in self.verdicts if v.verdict == "MISSED")


def _hm(value: Any) -> str:
    """Extract HH:MM from a loose timestamp ('09:15', '09:15:03', '26 Nov 2026 09:15:03 IST')."""
    m = re.search(r"(\d{1,2}):(\d{2})", str(value or ""))
    if not m:
        return ""
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def _min_of(hm: str) -> Optional[int]:
    if not hm or ":" not in hm:
        return None
    h, m = hm.split(":")[:2]
    try:
        return int(h) * 60 + int(m)
    except ValueError:
        return None


def _side_from_direction(direction: str, symbol: str = "") -> str:
    d = str(direction or "").upper()
    s = str(symbol or "").upper()
    if d == "LONG" or "CE" in s or "CALL" in s:
        return "CE"
    if d == "SHORT" or "PE" in s or "PUT" in s:
        return "PE"
    return ""


def parse_homework_csv(path: str) -> List[HomeworkTrade]:
    out: List[HomeworkTrade] = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                side = str(row.get("side") or "").strip().upper()
                if side not in ("CE", "PE"):
                    continue
                ema = row.get("ema8_at_entry")
                out.append(
                    HomeworkTrade(
                        date=str(row.get("date") or "").strip(),
                        strategy=str(row.get("strategy") or "").strip().lower(),
                        side=side,
                        entry_time=_hm(row.get("entry_time")),
                        entry_level=float(row.get("entry_level")),
                        exit_time=_hm(row.get("exit_time")),
                        exit_level=float(row.get("exit_level")),
                        ema8_at_entry=float(ema) if str(ema or "").strip() else None,
                        notes=str(row.get("notes") or "").strip(),
                    )
                )
            except (TypeError, ValueError):
                continue
    return out


def normalize_algo_trades(rows: List[Dict[str, Any]]) -> List[AlgoTrade]:
    out: List[AlgoTrade] = []
    for r in rows:
        side = _side_from_direction(r.get("direction") or r.get("Direction"), r.get("symbol") or r.get("Contract_Symbol"))
        if not side:
            continue
        ie, ix = r.get("index_entry"), r.get("index_exit")
        nifty_pts = None
        if isinstance(ie, (int, float)) and isinstance(ix, (int, float)):
            nifty_pts = (ix - ie) if side == "CE" else (ie - ix)
        out.append(
            AlgoTrade(
                side=side,
                entry_hm=_hm(r.get("entry_time") or r.get("Entry_Time")),
                exit_hm=_hm(r.get("exit_time") or r.get("Exit_Time")),
                option_points=float(r.get("Option_Points") or 0.0),
                net_pnl=(float(r["net_pnl"]) if isinstance(r.get("net_pnl"), (int, float)) else None),
                exit_reason=str(r.get("exit_reason") or r.get("Result") or "").strip(),
                strategy=str(r.get("strategy_setup") or r.get("Strategy") or "").strip(),
                nifty_points=nifty_pts,
            )
        )
    return out


def _is_stop_exit(reason: str) -> bool:
    return bool(re.search(r"\bSL\b|STOP|SL_HIT", str(reason or "").upper()))


def match_and_score(
    homework: List[HomeworkTrade],
    algo: List[AlgoTrade],
    *,
    tol_min: int = DEFAULT_MATCH_TOL_MIN,
    capture_ok: float = DEFAULT_CAPTURE_OK,
) -> ScoreCard:
    """Greedy nearest-time match of each homework trade to an algo trade."""
    date = homework[0].date if homework else ""
    card = ScoreCard(date=date)
    used = [False] * len(algo)

    for hw in homework:
        hw_min = _min_of(hw.entry_time)
        best_i, best_gap = -1, None
        for i, at in enumerate(algo):
            if used[i]:
                continue
            a_min = _min_of(at.entry_hm)
            if hw_min is None or a_min is None:
                continue
            gap = abs(a_min - hw_min)
            if gap <= tol_min and (best_gap is None or gap < best_gap):
                best_i, best_gap = i, gap

        if best_i < 0:
            card.verdicts.append(TradeVerdict(hw, None, "MISSED",
                f"No algo trade within {tol_min}m of {hw.entry_time}. Check: spot LIVE at touch? gate blocked? entry tolerance?"))
            continue

        at = algo[best_i]
        used[best_i] = True
        if at.side != hw.side:
            card.verdicts.append(TradeVerdict(hw, at, "WRONG_SIDE",
                f"You marked {hw.side} at {hw.entry_time}; algo traded {at.side} at {at.entry_hm}."))
            continue
        if _is_stop_exit(at.exit_reason) and hw.nifty_points > 0:
            card.verdicts.append(TradeVerdict(hw, at, "PREMATURE_SL",
                f"You caught +{hw.nifty_points:.0f} NIFTY; algo exited on stop ({at.exit_reason})."))
            continue
        # Capture comparison needs the algo's NIFTY points; fall back to a note if absent.
        if at.nifty_points is None:
            card.verdicts.append(TradeVerdict(hw, at, "MATCH",
                f"Algo took it ({at.option_points:+.1f} opt pts, exit {at.exit_reason or 'n/a'}); "
                f"NIFTY-point capture unknown (index levels not in ledger)."))
            continue
        if hw.nifty_points > 0 and at.nifty_points < capture_ok * hw.nifty_points:
            card.verdicts.append(TradeVerdict(hw, at, "UNDER_CAPTURED",
                f"You caught +{hw.nifty_points:.0f}; algo kept +{at.nifty_points:.0f} NIFTY "
                f"({at.nifty_points / hw.nifty_points * 100:.0f}%). Exit: {at.exit_reason}."))
            continue
        card.verdicts.append(TradeVerdict(hw, at, "MATCH",
            f"You +{hw.nifty_points:.0f}, algo +{at.nifty_points:.0f} NIFTY. Exit: {at.exit_reason}."))

    card.extra_algo = [at for i, at in enumerate(algo) if not used[i]]
    return card


def render_scorecard(card: ScoreCard) -> str:
    lines: List[str] = []
    lines.append(f"=== Homework vs Algo — {card.date} ===")
    for v in card.verdicts:
        hw = v.homework
        tag = {
            "MATCH": "[MATCH] ", "MISSED": "[MISS]  ", "PREMATURE_SL": "[SL]    ",
            "UNDER_CAPTURED": "[SHORT] ", "WRONG_SIDE": "[SIDE]  ",
        }.get(v.verdict, "[?]     ")
        head = f"{tag}{hw.side} {hw.entry_time}->{hw.exit_time} ({hw.strategy or 'any'}) +{hw.nifty_points:.0f} NIFTY" if hw else tag
        lines.append(head)
        lines.append(f"         {v.detail}")
    if card.extra_algo:
        lines.append("--- EXTRA algo trades (no homework) ---")
        for at in card.extra_algo:
            lines.append(f"[EXTRA] {at.side} {at.entry_hm}->{at.exit_hm} {at.option_points:+.1f} opt pts, exit {at.exit_reason or 'n/a'}")
    n = len(card.verdicts)
    lines.append(f"--- {card.matched_count}/{n} matched · {card.missed_count} missed · "
                 f"homework {card.homework_points:+.0f} NIFTY pts · {len(card.extra_algo)} extra ---")
    return "\n".join(lines)


def score_date_from_ledger(csv_path: str, target_date: str) -> ScoreCard:
    """CLI helper: read homework CSV, pull algo trades for the date from the recorded ledger."""
    from analyst.terminal8510_backend import get_replay_trades_for_date

    hw_all = parse_homework_csv(csv_path)
    hw = [h for h in hw_all if h.date == target_date]
    algo = normalize_algo_trades(get_replay_trades_for_date(target_date))
    return match_and_score(hw, algo)


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Grade the algo's day against your homework.")
    ap.add_argument("--csv", default="data/homework/expected_trades.csv", help="homework CSV path")
    ap.add_argument("--date", required=True, help="YYYY-MM-DD")
    args = ap.parse_args()
    print(render_scorecard(score_date_from_ledger(args.csv, args.date)))
