#!/usr/bin/env python3
"""
scripts/verify_locks.py — Verifies integrity of protected modules against baseline manifest.

Usage:
    python scripts/verify_locks.py
"""
import sys
import json
import hashlib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = REPO_ROOT / "ict_smc_app" / "LOCKED_MANIFEST.json"


def verify_locks(manifest_path: Path = MANIFEST_PATH) -> int:
    if not manifest_path.exists():
        print(f"[FAIL] Manifest not found: {manifest_path}", file=sys.stderr)
        return 1

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    locked_files = manifest.get("locked_files", {})
    if not locked_files:
        print("[FAIL] Manifest contains no locked_files entries", file=sys.stderr)
        return 1

    mismatches = []
    missing = []
    verified = 0

    for rel_path, meta in locked_files.items():
        file_path = REPO_ROOT / rel_path
        if not file_path.exists():
            missing.append(rel_path)
            continue

        actual_sha = hashlib.sha256(file_path.read_bytes()).hexdigest()
        expected_sha = meta["sha256"]
        if actual_sha != expected_sha:
            mismatches.append((rel_path, expected_sha, actual_sha))
        else:
            verified += 1

    if missing:
        print(f"[FAIL] {len(missing)} locked file(s) missing:", file=sys.stderr)
        for m in missing:
            print(f"  - MISSING: {m}", file=sys.stderr)

    if mismatches:
        print(f"[FAIL] {len(mismatches)} locked file(s) modified without explicit UNLOCK:", file=sys.stderr)
        for rel_path, exp, act in mismatches:
            print(f"  - MODIFIED: {rel_path}", file=sys.stderr)
            print(f"      expected: {exp}", file=sys.stderr)
            print(f"      actual:   {act}", file=sys.stderr)
        print("\nTo modify these modules, the user prompt must explicitly contain 'UNLOCK [Module Name]'.", file=sys.stderr)
        return 1

    if missing:
        return 1

    print(f"[PASS] All {verified} locked core modules verified intact against {manifest_path.name}.")
    return 0


if __name__ == "__main__":
    sys.exit(verify_locks())
