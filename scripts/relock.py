#!/usr/bin/env python3
"""relock.py — re-baseline a protected module's hashes, deliberately and on the record.

WHY THIS EXISTS
---------------
verify_locks.py compares the protected files against LOCKED_MANIFEST.json. But the manifest lives in
the same repo and is writable by the same commit, so any tool that edits a frozen file can edit the
manifest to match and still see a green [PASS]. That happened on 29 Oct: pe_ce_matrix.py and
PeceMatrix.tsx were modified and their hashes rewritten in the same commit. The check passed because
the baseline had moved, not because nothing had changed. A lock the locked party can re-issue for
itself is a changelog, not a lock.

This makes re-baselining a separate, named, auditable act:
  * you must name the module, exactly as PROTECTED_MODULES.md names it
  * you must pass --reason, which is written into the manifest
  * you must pass --yes, after seeing what would change
  * every re-lock is appended to `relock_history`, so the manifest records WHEN a baseline moved and
    WHY -- which is the question you actually want answered six weeks later

It cannot be used to lock something new or to unlock anything; it only re-baselines files that are
already listed in the manifest AND belong to the module you named.

USAGE
    python3 scripts/relock.py --list
    python3 scripts/relock.py --module "3-Chart Institutional PCR & OI Trinity Suite" --reason "..."
    python3 scripts/relock.py --module "..." --reason "..." --yes
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "ict_smc_app" / "LOCKED_MANIFEST.json"
PROTECTED_DOC = ROOT / "PROTECTED_MODULES.md"
IST = timezone(timedelta(hours=5, minutes=30))


def parse_modules() -> dict[str, list[str]]:
    """Module name -> file paths, read from PROTECTED_MODULES.md so there is ONE source of truth.

    The doc is the thing a human reads and CLAUDE.md points at; deriving the mapping from it means a
    module renamed there cannot silently keep working here against a stale copy.
    """
    if not PROTECTED_DOC.exists():
        sys.exit(f"missing {PROTECTED_DOC}")
    out: dict[str, list[str]] = {}
    current: str | None = None
    for line in PROTECTED_DOC.read_text().splitlines():
        m = re.match(r"^###\s+\d+\.\s+(.+?)\s*$", line)
        if m:
            current = m.group(1).strip()
            out.setdefault(current, [])
            continue
        if current:
            f = re.match(r"^\s*-\s*`([^`]+\.(?:py|tsx|ts))`\s*$", line)
            if f:
                out[current].append(f.group(1).strip())
    return {k: v for k, v in out.items() if v}


def sha256(p: Path) -> tuple[str, int]:
    b = p.read_bytes()
    return hashlib.sha256(b).hexdigest(), len(b)


def main() -> int:
    ap = argparse.ArgumentParser(description="Re-baseline a protected module's hashes, on the record.")
    ap.add_argument("--module", help='exact module name from PROTECTED_MODULES.md')
    ap.add_argument("--reason", help="why this baseline is moving; stored in the manifest")
    ap.add_argument("--yes", action="store_true", help="actually write (default is a dry run)")
    ap.add_argument("--list", action="store_true", help="list modules and exit")
    a = ap.parse_args()

    modules = parse_modules()
    if a.list or not a.module:
        print("Protected modules:\n")
        for name, files in modules.items():
            print(f"  {name}")
            for f in files:
                print(f"      {f}")
        if not a.module:
            print('\nName one with --module "<exact name>" --reason "<why>".')
            return 0 if a.list else 2
        return 0

    if a.module not in modules:
        print(f"Unknown module: {a.module!r}\n", file=sys.stderr)
        print("Known modules:", file=sys.stderr)
        for name in modules:
            print(f"  {name}", file=sys.stderr)
        return 2
    if not a.reason or not a.reason.strip():
        print("--reason is required: a baseline that moves without a recorded reason is "
              "indistinguishable from one that moved by accident.", file=sys.stderr)
        return 2

    man = json.loads(MANIFEST.read_text())
    locked: dict = man["locked_files"]

    changes, missing, untracked = [], [], []
    for rel in modules[a.module]:
        if rel not in locked:
            untracked.append(rel)
            continue
        p = ROOT / rel
        if not p.exists():
            missing.append(rel)
            continue
        new_hash, new_size = sha256(p)
        old = locked[rel]
        if old.get("sha256") == new_hash:
            continue
        changes.append((rel, old.get("sha256", ""), new_hash, old.get("size_bytes", 0), new_size))

    for rel in untracked:
        print(f"  note: {rel} is in PROTECTED_MODULES.md but not in the manifest — not touched.")
    if missing:
        print(f"  ERROR: file(s) missing on disk: {', '.join(missing)}", file=sys.stderr)
        return 1
    if not changes:
        print(f"Nothing to re-lock: every file in {a.module!r} already matches its baseline.")
        return 0

    print(f"\nModule : {a.module}")
    print(f"Reason : {a.reason}\n")
    for rel, oh, nh, osz, nsz in changes:
        print(f"  {rel}")
        print(f"      {oh[:16]}…  ->  {nh[:16]}…    {osz:,} -> {nsz:,} bytes ({nsz - osz:+,})")

    if not a.yes:
        print("\nDry run. Nothing written. Re-run with --yes to move the baseline.")
        return 0

    stamp = datetime.now(IST).isoformat(timespec="seconds")
    for rel, _oh, nh, _osz, nsz in changes:
        locked[rel]["sha256"] = nh
        locked[rel]["size_bytes"] = nsz
    man.setdefault("relock_history", []).append({
        "at": stamp,
        "module": a.module,
        "reason": a.reason.strip(),
        "files": [rel for rel, *_ in changes],
    })
    MANIFEST.write_text(json.dumps(man, indent=2) + "\n")
    print(f"\nRe-locked {len(changes)} file(s) at {stamp}. Recorded in relock_history.")
    print("Run scripts/verify_locks.py to confirm.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
