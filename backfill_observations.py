#!/usr/bin/env python3
"""
backfill_observations.py — Replay stored snapshots to rebuild daily observation logs.

Usage:
    python backfill_observations.py --date 2026-09-18
    python backfill_observations.py --date 2026-09-18 --from 09:15 --to 15:30 --interval 180
    python backfill_observations.py --date 2026-09-18 --dry-run
    python backfill_observations.py --last 5

Defaults:
    --from 09:15
    --to 15:30
    --interval from strategy1_rules.json observations (fall back 180s)
"""
from __future__ import annotations

import argparse
import functools
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import time
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Base path of OC
BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import config
import store
import rvol_gate
import agt_briefing

try:
    from scheduler import send_telegram_alert
except ImportError:
    send_telegram_alert = None

logger = logging.getLogger("backfill_observations")

DEFAULT_NOTE_PLACEHOLDER = "> Note: [chart observation / what price did]"


def load_rules(rules_path: Optional[Path] = None) -> dict:
    """Load configuration dictionary from strategy1_rules.json."""
    path = rules_path or (BASE_DIR / "strategy1_rules.json")
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Could not read rules from %s: %s", path, exc)
    return {}


def get_default_interval(rules: dict) -> int:
    """Determine default step interval in seconds from rules or fallback to 180."""
    obs_cfg = rules.get("observations", {})
    if "interval_seconds" in obs_cfg:
        return int(obs_cfg["interval_seconds"])
    if "interval" in obs_cfg:
        return int(obs_cfg["interval"])
    return 180


def parse_obs_file(text: str) -> Tuple[str, Dict[str, str], bool, Optional[str]]:
    """
    Parse an observation markdown file.

    Returns:
        header: Title line(s)
        entries: dict mapping 'HH:MM' -> entry_text
        freeze_end: bool indicating if == FREEZE END == was present
        eod_summary: Optional[str] containing the == EOD SUMMARY == block
    """
    lines = text.splitlines(keepends=True)
    header = ""
    entries: Dict[str, str] = {}
    freeze_end = False
    eod_summary = None

    current_key: Optional[str] = None
    current_lines: List[str] = []

    def save_current():
        nonlocal current_key, current_lines, freeze_end, eod_summary
        if not current_key:
            return
        block = "".join(current_lines).strip()
        if current_key == "FREEZE_END":
            freeze_end = True
        elif current_key == "EOD_SUMMARY":
            eod_summary = block
        else:
            entries[current_key] = block
        current_lines = []
        current_key = None

    for line in lines:
        m_entry = re.match(r"^##\s+(\d{2}:\d{2})", line)
        m_freeze = re.match(r"^==\s*FREEZE END\s*==", line)
        m_eod = re.match(r"^==\s*EOD SUMMARY\s*==", line)

        if m_entry:
            save_current()
            current_key = m_entry.group(1)
            current_lines.append(line)
        elif m_freeze:
            save_current()
            current_key = "FREEZE_END"
            current_lines.append(line)
        elif m_eod:
            save_current()
            current_key = "EOD_SUMMARY"
            current_lines.append(line)
        else:
            if current_key is None:
                header += line
            else:
                current_lines.append(line)

    save_current()
    return header.strip(), entries, freeze_end, eod_summary


def extract_note(entry_text: str) -> Optional[str]:
    """
    Extract handwritten note lines from an entry if different from the default placeholder.
    Returns the note string if custom, or None if placeholder / absent.
    """
    lines = entry_text.splitlines()
    note_lines = []
    in_note = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("> Note:"):
            in_note = True
            note_lines.append(line)
        elif in_note and stripped.startswith(">"):
            note_lines.append(line)
        elif in_note:
            break
    if not note_lines:
        return None
    joined = "\n".join(note_lines).strip()
    if joined == DEFAULT_NOTE_PLACEHOLDER:
        return None
    return joined


def replace_note(entry_text: str, custom_note: str) -> str:
    """Replace note line(s) in entry_text with custom_note."""
    lines = entry_text.splitlines()
    new_lines = []
    replaced = False
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("> Note:"):
            new_lines.append(custom_note)
            replaced = True
            i += 1
            while i < len(lines) and lines[i].strip().startswith(">"):
                i += 1
            continue
        new_lines.append(line)
        i += 1
    if not replaced:
        if new_lines and new_lines[-1].strip() == "---":
            new_lines.insert(-1, custom_note)
            new_lines.insert(-1, "")
        else:
            new_lines.append(custom_note)
    return "\n".join(new_lines)


def assemble_obs_content(
    date_str: str,
    entries: Dict[str, str],
    freeze_end: bool,
    eod_summary: Optional[str],
    freeze_time: str = "13:15",
    eod_time: str = "15:25",
) -> str:
    """Reassemble observation log markdown with entries and markers in chronological order."""
    out_parts = [f"# NIFTY Daily Observation Log — {date_str}\n\n"]
    sorted_times = sorted(entries.keys())
    emitted_fe = False
    emitted_eod = False

    for t in sorted_times:
        if freeze_end and not emitted_fe and t >= freeze_time:
            out_parts.append("== FREEZE END ==\n\n")
            emitted_fe = True
        if eod_summary and not emitted_eod and t >= eod_time:
            summary_clean = eod_summary.strip()
            if not summary_clean.startswith("=="):
                summary_clean = f"== EOD SUMMARY ==\n{summary_clean}"
            out_parts.append(f"{summary_clean}\n\n")
            emitted_eod = True

        entry_txt = entries[t].strip()
        if not entry_txt.endswith("---"):
            entry_txt = entry_txt + "\n\n---"
        out_parts.append(f"{entry_txt}\n\n")

    if freeze_end and not emitted_fe:
        out_parts.append("== FREEZE END ==\n\n")
    if eod_summary and not emitted_eod:
        summary_clean = eod_summary.strip()
        if not summary_clean.startswith("=="):
            summary_clean = f"== EOD SUMMARY ==\n{summary_clean}"
        out_parts.append(f"{summary_clean}\n\n")

    return "".join(out_parts)


def get_snapshot_dates(base_dir: Optional[Path] = None) -> List[str]:
    """Return all dates (YYYY-MM-DD) that have snapshot folders containing .parquet files."""
    snap_dir = (base_dir / "snapshots") if base_dir else config.SNAPSHOT_DIR
    if not snap_dir.exists():
        return []
    dates = []
    for d in sorted(snap_dir.iterdir()):
        if d.is_dir() and re.match(r"^\d{4}-\d{2}-\d{2}$", d.name):
            if any(d.glob("*.parquet")):
                dates.append(d.name)
    return dates


class BackfillExecutionHelper:
    """Manages performance caching and safe redirection during briefing generation."""

    def __init__(
        self,
        all_snaps: List[Tuple[datetime, float, Any]],
        staging_dir: Path,
        telegram_enabled: bool,
    ):
        self.all_snaps = all_snaps
        self.staging_dir = staging_dir
        self.telegram_enabled = telegram_enabled
        self.orig_load_day = store.load_day
        self.orig_load_session = rvol_gate._load_session_df
        self.orig_load_rules = agt_briefing.load_briefing_rules

    def __enter__(self):
        store.load_day = lambda d=None: self.all_snaps
        rvol_gate._load_session_df = functools.lru_cache(maxsize=128)(self.orig_load_session)

        def custom_rules():
            r = self.orig_load_rules()
            r["observations_dir"] = str(self.staging_dir)
            r["telegram_briefing_enabled"] = self.telegram_enabled
            return r

        agt_briefing.load_briefing_rules = custom_rules
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        store.load_day = self.orig_load_day
        rvol_gate._load_session_df = self.orig_load_session
        agt_briefing.load_briefing_rules = self.orig_load_rules


def backfill_day(
    target_date: date,
    from_time: dtime = dtime(9, 15),
    to_time: dtime = dtime(15, 30),
    interval_seconds: Optional[int] = None,
    dry_run: bool = False,
    overwrite: bool = False,
    telegram: bool = False,
    base_dir: Optional[Path] = None,
    rules_override: Optional[dict] = None,
) -> Optional[Dict[str, Any]]:
    """
    Backfill observation logs for a single date.

    Returns summary dict on success, or None on failure / missing data.
    """
    date_str = target_date.strftime("%Y-%m-%d")
    snap_folder = (base_dir / "snapshots" / date_str) if base_dir else (config.SNAPSHOT_DIR / date_str)

    # 1. Missing or empty snapshot folder check
    if not snap_folder.exists() or not any(snap_folder.glob("*.parquet")):
        print(
            f"Data for {date_str} is unrecoverable: poller was not running that day "
            f"(snapshot directory '{snap_folder}' is missing or empty).",
            file=sys.stderr,
        )
        return None

    # Load snapshots for the target date
    all_snaps = store.load_day(target_date)
    if not all_snaps:
        print(
            f"Data for {date_str} is unrecoverable: poller was not running that day "
            f"(no snapshots loaded from '{snap_folder}').",
            file=sys.stderr,
        )
        return None

    all_snaps.sort(key=lambda s: s[0])
    total_snaps_found = len(all_snaps)

    # 2. Thresholds & Configuration
    rules = rules_override or load_rules()
    if interval_seconds is None:
        interval_seconds = get_default_interval(rules)

    freeze_time_str = rules.get("observations", {}).get("freeze_end_time", rules.get("freeze_end", "13:15"))
    eod_time_str = rules.get("observations", {}).get("eod_summary_time", rules.get("eod_summary_time", "15:25"))

    obs_dir_path = Path(rules.get("observations", {}).get("dir", "data/observations"))
    if not obs_dir_path.is_absolute():
        obs_dir_path = (base_dir or BASE_DIR) / obs_dir_path

    obs_file = obs_dir_path / f"{date_str}.md"

    # 3. Read existing log if present (Safety 1, 5)
    existing_entries: Dict[str, str] = {}
    existing_fe = False
    existing_eod = None
    if obs_file.exists():
        existing_content = obs_file.read_text(encoding="utf-8")
        _, existing_entries, existing_fe, existing_eod = parse_obs_file(existing_content)

    # 4. Generate expected bins between from_time and to_time
    dt_start = datetime.combine(target_date, from_time)
    dt_end = datetime.combine(target_date, to_time)
    expected_bins: List[datetime] = []
    cur_dt = dt_start
    step_delta = timedelta(seconds=interval_seconds)
    while cur_dt <= dt_end:
        expected_bins.append(cur_dt)
        cur_dt += step_delta

    entries_written = 0
    entries_already_present = 0
    gaps_skipped = 0
    matched_snaps_count = 0

    # 5. Process bins in a temporary staging environment to preserve safety and isolation
    with tempfile.TemporaryDirectory() as temp_staging:
        staging_dir = Path(temp_staging)
        with BackfillExecutionHelper(all_snaps, staging_dir, telegram):
            for bin_dt in expected_bins:
                matching = [s for s in all_snaps if abs((s[0] - bin_dt).total_seconds()) <= 90.0]
                if not matching:
                    gaps_skipped += 1
                    continue

                matched_snaps_count += 1
                closest_snap = min(matching, key=lambda s: abs((s[0] - bin_dt).total_seconds()))
                snap_ts = closest_snap[0]
                snap_time_str = snap_ts.strftime("%H:%M")

                if snap_time_str in existing_entries and not overwrite:
                    entries_already_present += 1
                    continue

                # Run briefing logic (append_obs=True to populate staging observation file)
                briefing_output = agt_briefing.build_briefing(target_date, snap_ts.time(), append_obs=True)

                # Send Telegram alert if requested
                if telegram and send_telegram_alert is not None:
                    tg_msg = getattr(agt_briefing.build_briefing, "telegram_briefing", None) or briefing_output
                    if tg_msg and tg_msg != "Market closed or no data yet":
                        try:
                            send_telegram_alert(tg_msg)
                        except Exception as exc:
                            logger.warning("Error dispatching telegram alert: %s", exc)

        # Parse generated staging file
        staging_file = staging_dir / f"{date_str}.md"
        gen_entries: Dict[str, str] = {}
        gen_fe = False
        gen_eod = None
        if staging_file.exists():
            _, gen_entries, gen_fe, gen_eod = parse_obs_file(staging_file.read_text(encoding="utf-8"))

    # 6. Merge generated entries with existing entries
    merged_entries: Dict[str, str] = dict(existing_entries)
    entries_modified = 0
    modified_timestamps: List[str] = []
    for t_str, new_text in gen_entries.items():
        if t_str in merged_entries:
            if overwrite:
                old_entry = merged_entries[t_str]
                old_note = extract_note(old_entry)
                if old_note:
                    new_text = replace_note(new_text, old_note)
                if old_entry.strip() != new_text.strip():
                    entries_modified += 1
                    modified_timestamps.append(t_str)
                merged_entries[t_str] = new_text
                entries_written += 1
            else:
                entries_already_present += 1
        else:
            merged_entries[t_str] = new_text
            entries_written += 1

    if overwrite and entries_modified > 0:
        preview = ", ".join(modified_timestamps[:10]) + ("..." if len(modified_timestamps) > 10 else "")
        print(f"[OVERWRITE] Modified {entries_modified} existing entries: {preview}")

    merged_fe = existing_fe or gen_fe
    merged_eod = existing_eod if (existing_eod and not overwrite) else (gen_eod or existing_eod)

    # 7. Safety Rule 1 verification: Never shorten a pre-populated log
    if len(merged_entries) < len(existing_entries):
        raise RuntimeError(
            f"Safety check failed: merged entries ({len(merged_entries)}) < "
            f"existing entries ({len(existing_entries)}). Aborting write!"
        )

    final_content = assemble_obs_content(
        date_str=date_str,
        entries=merged_entries,
        freeze_end=merged_fe,
        eod_summary=merged_eod,
        freeze_time=freeze_time_str,
        eod_time=eod_time_str,
    )

    if existing_entries and len(final_content.strip()) == 0:
        raise RuntimeError("Safety check failed: final content is blank. Aborting write!")

    backup_path_str: Optional[str] = None

    # 8. Dry-run or Atomic File Write (Safety 2, 3)
    if dry_run:
        print(f"[DRY-RUN] Simulation mode for {date_str} — no files modified.")
        print(f"[DRY-RUN] Generated / Merged {len(merged_entries)} entries:\n")
        print(final_content)
    else:
        obs_dir_path.mkdir(parents=True, exist_ok=True)
        # Safety Rule 2: Backup before writing
        if obs_file.exists() and entries_written > 0:
            ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
            bak_path = obs_file.parent / f"{obs_file.name}.bak-{ts_str}"
            shutil.copy2(obs_file, bak_path)
            backup_path_str = str(bak_path.resolve())

        # Safety Rule 3: Atomic replace
        if entries_written > 0 or not obs_file.exists():
            tmp_path = obs_dir_path / f".{obs_file.name}.tmp-{os.getpid()}-{int(time.time() * 1000)}"
            tmp_path.write_text(final_content, encoding="utf-8")
            os.replace(tmp_path, obs_file)

    # 9. Summary calculation
    total_bins = len(expected_bins)
    coverage_ratio = f"{entries_written}/{total_bins}"
    coverage_pct = (entries_written / total_bins * 100.0) if total_bins > 0 else 0.0

    summary: Dict[str, Any] = {
        "date": date_str,
        "snapshots_found": total_snaps_found,
        "matched_snaps": matched_snaps_count,
        "entries_written": entries_written,
        "entries_already_present": entries_already_present,
        "gaps_skipped": gaps_skipped,
        "coverage": f"{coverage_ratio} bins ({coverage_pct:.1f}%)",
        "backup_file_path": backup_path_str,
        "total_entries": len(merged_entries),
    }

    summary_banner = f"""
============================================================
BACKFILL SUMMARY — {date_str}
============================================================
Date:                       {date_str}
Snapshots found:            {total_snaps_found} ({matched_snaps_count} matched to expected bins)
Entries written:            {entries_written}
Entries already present:    {entries_already_present} (skipped)
Gaps skipped:               {gaps_skipped} (no snapshot within +/- 90s)
Coverage:                   {coverage_ratio} ({coverage_pct:.1f}%)
Backup file path:           {backup_path_str or 'None'}
============================================================
"""
    print(summary_banner.strip())
    return summary


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point for backfill_observations."""
    parser = argparse.ArgumentParser(
        description="Replay stored snapshots to rebuild daily observation logs."
    )
    parser.add_argument("--date", help="Target session date (YYYY-MM-DD)")
    parser.add_argument("--last", type=int, help="Backfill the N most recent dates that have snapshot folders")
    parser.add_argument("--from", dest="from_time", default="09:15", help="Start time HH:MM (default: 09:15)")
    parser.add_argument("--to", dest="to_time", default="15:30", help="End time HH:MM (default: 15:30)")
    parser.add_argument("--interval", type=int, default=None, help="Bin interval in seconds (default from rules or 180s)")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be written without modifying files")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing entries on conflict")
    parser.add_argument("--force-today", action="store_true", help="Force backfill on today's in-progress session")
    parser.add_argument("--telegram", action="store_true", help="Enable Telegram alert dispatches (default: False)")

    args = parser.parse_args(argv)

    if not args.date and args.last is None:
        print("Error: Either --date or --last must be specified.", file=sys.stderr)
        return 1

    if args.date and args.last is not None:
        print("Error: Cannot specify both --date and --last.", file=sys.stderr)
        return 1

    try:
        t_from = datetime.strptime(args.from_time, "%H:%M").time()
    except ValueError:
        print(f"Error: Invalid --from time format '{args.from_time}'. Expected HH:MM.", file=sys.stderr)
        return 1

    try:
        t_to = datetime.strptime(args.to_time, "%H:%M").time()
    except ValueError:
        print(f"Error: Invalid --to time format '{args.to_time}'. Expected HH:MM.", file=sys.stderr)
        return 1

    dates_to_run: List[date] = []
    if args.date:
        try:
            d_val = datetime.strptime(args.date, "%Y-%m-%d").date()
            dates_to_run.append(d_val)
        except ValueError:
            print(f"Error: Invalid date format '{args.date}'. Expected YYYY-MM-DD.", file=sys.stderr)
            return 1
    elif args.last is not None:
        if args.last <= 0:
            print("Error: --last must be a positive integer >= 1.", file=sys.stderr)
            return 1
        avail = get_snapshot_dates()
        if not avail:
            print("Error: No snapshot folders found in data/snapshots.", file=sys.stderr)
            return 1
        selected = avail[-args.last:]
        for s in selected:
            dates_to_run.append(datetime.strptime(s, "%Y-%m-%d").date())

    today_d = date.today()
    for d_target in dates_to_run:
        if d_target == today_d and not args.force_today:
            now_t = datetime.now().time()
            if dtime(9, 15) <= now_t <= dtime(15, 30):
                print(
                    f"Error: Refusing backfill for today ({d_target}) while session is in progress.\n"
                    "Backfilling during an active session creates mixed code vintages.\n"
                    "Pass --force-today to override.",
                    file=sys.stderr,
                )
                return 1

    overall_success = True
    for d_target in dates_to_run:
        res = backfill_day(
            target_date=d_target,
            from_time=t_from,
            to_time=t_to,
            interval_seconds=args.interval,
            dry_run=args.dry_run,
            overwrite=args.overwrite,
            telegram=args.telegram,
        )
        if res is None:
            overall_success = False

    return 0 if overall_success else 1


if __name__ == "__main__":
    sys.exit(main())
