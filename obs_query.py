#!/usr/bin/env python3
"""
obs_query.py — CLI query tool for the Daily Observation Log.

Usage:
  .venv/bin/python obs_query.py 10:16
  .venv/bin/python obs_query.py 10:16 --date 2026-09-21
  .venv/bin/python obs_query.py 10:16 2026-09-21

Reads data/observations/YYYY-MM-DD.md, finds the matching or nearest entry to the target time,
and prints that entry along with the 2 entries before and 2 entries after (5-entry window).

All configuration loaded dynamically from strategy1_rules.json under 'observations'.
Zero external dependencies (pure Python standard library).
"""
from __future__ import annotations

import json
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent
RULES_PATH = REPO_ROOT / "strategy1_rules.json"


def load_observation_config() -> Dict[str, Any]:
    """Loads observation settings from strategy1_rules.json under 'observations'."""
    defaults = {
        "enabled": True,
        "dir": "data/observations",
        "market_open_time": "09:15",
        "freeze_end_time": "13:15",
        "eod_summary_time": "15:25",
    }
    if RULES_PATH.exists():
        try:
            with open(RULES_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                obs_cfg = data.get("observations", {})
                defaults.update(obs_cfg)
        except Exception as exc:
            print(f"Warning: Could not read {RULES_PATH}: {exc}", file=sys.stderr)
    return defaults


def parse_time_str(time_str: str) -> Optional[int]:
    """Parses 'HH:MM' string to minutes from midnight."""
    match = re.match(r"^(\d{1,2}):(\d{2})$", time_str.strip())
    if not match:
        return None
    hh, mm = int(match.group(1)), int(match.group(2))
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    return hh * 60 + mm


def parse_date_arg(arg: str) -> Optional[date]:
    """Parses date from YYYY-MM-DD string."""
    try:
        return datetime.strptime(arg.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def parse_log_entries(file_path: Path) -> List[Dict[str, Any]]:
    """
    Parses entries from an observation markdown file.
    Each entry starts with '## HH:MM | Spot ...' and continues until next '## ' or '== ' or EOF.
    Returns list of dicts with:
      - 'time_str': 'HH:MM'
      - 'minutes': minutes from midnight
      - 'raw_text': complete markdown chunk for the entry
    """
    if not file_path.exists():
        return []

    content = file_path.read_text(encoding="utf-8")
    lines = content.splitlines()

    entries: List[Dict[str, Any]] = []
    current_entry: Optional[Dict[str, Any]] = None
    current_lines: List[str] = []

    header_pattern = re.compile(r"^##\s+(\d{1,2}:\d{2})\b")

    def flush_entry():
        nonlocal current_entry, current_lines
        if current_entry and current_lines:
            current_entry["raw_text"] = "\n".join(current_lines).strip()
            entries.append(current_entry)
        current_entry = None
        current_lines = []

    for line in lines:
        match = header_pattern.match(line)
        if match:
            flush_entry()
            time_str = match.group(1)
            minutes = parse_time_str(time_str)
            current_entry = {
                "time_str": time_str,
                "minutes": minutes,
                "header": line,
            }
            current_lines.append(line)
        elif line.startswith("== ") and line.endswith(" =="):
            # Milestone marker reached — flush current entry
            flush_entry()
        else:
            if current_entry is not None:
                current_lines.append(line)

    flush_entry()
    entries.sort(key=lambda e: (e["minutes"] if e["minutes"] is not None else 0))
    return entries


def query_observations(
    target_time_str: str,
    target_date: Optional[date] = None,
) -> int:
    """
    Finds matching or nearest entry and prints 5-entry window (2 before, target, 2 after).
    """
    target_minutes = parse_time_str(target_time_str)
    if target_minutes is None:
        print(f"Error: Invalid time format '{target_time_str}'. Expected 'HH:MM' (e.g. 10:16).", file=sys.stderr)
        return 1

    if target_date is None:
        target_date = date.today()

    cfg = load_observation_config()
    obs_dir = Path(cfg.get("dir", "data/observations"))
    if not obs_dir.is_absolute():
        obs_dir = REPO_ROOT / obs_dir

    date_str = target_date.strftime("%Y-%m-%d")
    log_file = obs_dir / f"{date_str}.md"

    if not log_file.exists():
        print(f"Observation log not found: {log_file}")
        available = sorted([f.stem for f in obs_dir.glob("*.md")]) if obs_dir.exists() else []
        if available:
            print(f"Available dates in {obs_dir.relative_to(REPO_ROOT)}: {', '.join(available)}")
        return 1

    entries = parse_log_entries(log_file)
    if not entries:
        print(f"Observation log {log_file} exists but contains no entries.")
        return 1

    # Find closest entry in minutes
    best_idx = 0
    best_diff = float("inf")
    for idx, e in enumerate(entries):
        diff = abs(e["minutes"] - target_minutes)
        if diff < best_diff:
            best_diff = diff
            best_idx = idx

    matched = entries[best_idx]
    is_exact = (best_diff == 0)

    # 5-entry window: 2 before, matched entry, 2 after
    start_idx = max(0, best_idx - 2)
    end_idx = min(len(entries), best_idx + 3)
    window = entries[start_idx:end_idx]

    exact_note = "exact match" if is_exact else f"nearest entry, Δ{int(best_diff)}m"
    print("=" * 65)
    print(f"OBSERVATION QUERY: {target_time_str} on {date_str} ({exact_note})")
    print(f"File: {log_file}")
    print(f"Window: showing {len(window)} entries (indices {start_idx + 1}..{end_idx} of {len(entries)})")
    print("=" * 65)
    print()

    for idx, e in enumerate(window, start=start_idx):
        prefix = "🎯 TARGET -> " if idx == best_idx else "            "
        print(f"[{idx + 1}/{len(entries)}] {prefix}{e['header']}")
        # Print entry body (excluding header if already printed)
        body_lines = e["raw_text"].splitlines()
        for bl in body_lines[1:]:
            print(bl)
        print()

    return 0


def main() -> None:
    target_time_str: Optional[str] = None
    target_date: Optional[date] = None

    args = sys.argv[1:]
    if not args:
        print("Usage: .venv/bin/python obs_query.py HH:MM [--date YYYY-MM-DD]")
        print("Example: .venv/bin/python obs_query.py 10:16")
        sys.exit(1)

    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("--date", "-d") and i + 1 < len(args):
            parsed_d = parse_date_arg(args[i + 1])
            if parsed_d:
                target_date = parsed_d
            i += 2
            continue
        elif parse_time_str(arg) is not None and target_time_str is None:
            target_time_str = arg
        elif parse_date_arg(arg) is not None and target_date is None:
            target_date = parse_date_arg(arg)
        elif arg in ("--help", "-h"):
            print("Usage: .venv/bin/python obs_query.py HH:MM [--date YYYY-MM-DD]")
            print("Example: .venv/bin/python obs_query.py 10:16")
            sys.exit(0)
        i += 1

    if target_time_str is None:
        print("Error: Target time 'HH:MM' required (e.g. 10:16).", file=sys.stderr)
        print("Usage: .venv/bin/python obs_query.py HH:MM [--date YYYY-MM-DD]", file=sys.stderr)
        sys.exit(1)

    exit_code = query_observations(target_time_str, target_date)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
