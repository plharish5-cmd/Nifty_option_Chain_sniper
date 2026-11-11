#!/usr/bin/env python3
"""
scripts/take_back_ema8_prototype.py — One-command safety and rollback utility for 8 EMA Prototype.

Provides four safe ways to pause, disarm, or completely roll back the 8 EMA Prototype:

Usage:
    python3 scripts/take_back_ema8_prototype.py --status
        Shows current prototype configuration, live execution route, and git branch.

    python3 scripts/take_back_ema8_prototype.py --disable
        Instantly pauses the prototype by setting enabled=false in config.
        Leaves all other code and strategies completely intact.

    python3 scripts/take_back_ema8_prototype.py --paper-only
        Forces prototype into PAPER_ONLY mode (blocks any real Dhan orders).

    python3 scripts/take_back_ema8_prototype.py --enable
        Re-enables the prototype in config.

    python3 scripts/take_back_ema8_prototype.py --git-rollback
        Full git rollback: checks out tag 'pre-agy-ema8-proto-corrections' (commit ef3ded4)
        to restore the exact codebase state prior to the remediation work.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("take_back_ema8_prototype")

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "analyst" / "terminal8510_config.json"
PRE_CORRECTIONS_TAG = "pre-agy-ema8-proto-corrections"
BACKUP_BRANCH = "backup-pre-ema8-proto-corrections"


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        log.error("Config file not found at %s", CONFIG_PATH)
        sys.exit(1)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_config(cfg: dict) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    log.info("Saved configuration to %s", CONFIG_PATH)


def get_git_status() -> dict:
    info = {"branch": "unknown", "head": "unknown"}
    try:
        branch = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(REPO_ROOT),
            text=True,
        ).strip()
        head = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(REPO_ROOT),
            text=True,
        ).strip()
        info["branch"] = branch
        info["head"] = head
    except Exception as exc:
        log.warning("Could not read git metadata: %s", exc)
    return info


def show_status() -> None:
    cfg = load_config()
    git_info = get_git_status()
    proto = cfg.get("scanner", {}).get("ema8_prototype", {})
    enabled = proto.get("enabled", True)
    mode = proto.get("execution_mode", "LIVE_ALLOWED")
    tol = proto.get("touch_tol_pts", 3.0)
    slow_line = proto.get("slow_market_exact_line", False)

    print("=" * 60)
    print("8 EMA PROTOTYPE STATUS")
    print("=" * 60)
    print(f"Git Branch        : {git_info['branch']} ({git_info['head']})")
    print(f"Prototype Enabled : {'ACTIVE (True)' if enabled else 'PAUSED / OFF (False)'}")
    print(f"Execution Route   : {mode}")
    print(f"Touch Tolerance   : {tol} pts (Slow exact line: {slow_line})")
    print(f"Safety Tag        : {PRE_CORRECTIONS_TAG}")
    print(f"Backup Branch     : {BACKUP_BRANCH}")
    print("=" * 60)


def set_enabled(value: bool) -> None:
    cfg = load_config()
    proto = cfg.setdefault("scanner", {}).setdefault("ema8_prototype", {})
    proto["enabled"] = bool(value)
    save_config(cfg)
    status_label = "ENABLED (ACTIVE)" if value else "DISABLED (PAUSED)"
    log.info("8 EMA Prototype master status set to: %s", status_label)
    print(f"\n[SUCCESS] 8 EMA Prototype is now {status_label}.")
    print("The engine will apply this on its next 30s evaluation pass without needing a restart.")


def set_mode(mode: str) -> None:
    cfg = load_config()
    proto = cfg.setdefault("scanner", {}).setdefault("ema8_prototype", {})
    proto["execution_mode"] = str(mode).upper()
    save_config(cfg)
    log.info("8 EMA Prototype execution route set to: %s", mode)
    print(f"\n[SUCCESS] 8 EMA Prototype execution route set to {mode.upper()}.")


def git_rollback() -> None:
    print("\n" + "=" * 60)
    print("GIT CODEBASE ROLLBACK TO PRE-REMEDIATION STATE")
    print("=" * 60)
    print(f"Target commit/tag: {PRE_CORRECTIONS_TAG} (ef3ded4)")

    try:
        subprocess.check_call(
            ["git", "checkout", PRE_CORRECTIONS_TAG],
            cwd=str(REPO_ROOT),
        )
        print(f"\n[SUCCESS] Checked out {PRE_CORRECTIONS_TAG} cleanly.")
        print("To restart the 8510 Algo Terminal on this rolled-back version, run:")
        print("    ./scripts/desk_launch.sh 8510")
    except Exception as exc:
        log.error("Git rollback failed: %s", exc)
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="One-command safety and rollback utility for 8 EMA Prototype."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--status", action="store_true", help="Display prototype configuration and git branch.")
    group.add_argument("--disable", action="store_true", help="Pause prototype in config (enabled=false).")
    group.add_argument("--enable", action="store_true", help="Re-enable prototype in config (enabled=true).")
    group.add_argument("--paper-only", action="store_true", help="Force prototype route to PAPER_ONLY.")
    group.add_argument("--live-allowed", action="store_true", help="Restore prototype route to LIVE_ALLOWED.")
    group.add_argument("--git-rollback", action="store_true", help=f"Check out tag {PRE_CORRECTIONS_TAG} to revert all code.")

    args = parser.parse_args()

    if args.status:
        show_status()
    elif args.disable:
        set_enabled(False)
    elif args.enable:
        set_enabled(True)
    elif args.paper_only:
        set_mode("PAPER_ONLY")
    elif args.live_allowed:
        set_mode("LIVE_ALLOWED")
    elif args.git_rollback:
        git_rollback()


if __name__ == "__main__":
    main()
