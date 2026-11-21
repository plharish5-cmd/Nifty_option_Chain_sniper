#!/usr/bin/env python3
"""Run baseline diff tests between current branch/target and a baseline SHA/branch.

Usage:
    .venv/bin/python scripts/run_baseline_diff_tests.py [--baseline <sha_or_ref>] [--target <ref>]
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, Optional, Set, Tuple


REPO_ROOT = Path(__file__).resolve().parent.parent
PYTHON_EXE = REPO_ROOT / ".venv" / "bin" / "python"
if not PYTHON_EXE.exists():
    PYTHON_EXE = Path(sys.executable)

DEFAULT_BASELINE = "origin/reconcile/v2.6.0-consolidated"
DEFAULT_TEST_PATHS = ["analyst/", "ict_smc_app/backend/tests/"]
DEFAULT_WORKTREE_DIR = Path("/tmp/base_verify")


def run_cmd(cmd: list[str], cwd: Optional[Path] = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=str(cwd or REPO_ROOT),
        capture_output=True,
        text=True,
        check=check,
    )


def resolve_sha(ref: str) -> str:
    res = run_cmd(["git", "rev-parse", ref], check=False)
    if res.returncode != 0:
        # Try local or origin
        res = run_cmd(["git", "rev-parse", f"origin/{ref}"], check=False)
        if res.returncode != 0:
            return ref
    return res.stdout.strip()


def parse_junit_xml(xml_path: Path) -> Dict[str, str]:
    """Parse JUnit XML and return map of test_id -> status (PASSED | FAILED | SKIPPED | ERROR)."""
    if not xml_path.exists():
        return {}
    results = {}
    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()
        for tc in root.iter("testcase"):
            classname = tc.get("classname", "")
            name = tc.get("name", "")
            test_id = f"{classname}::{name}" if classname else name
            
            # Check status
            if tc.find("failure") is not None:
                status = "FAILED"
            elif tc.find("error") is not None:
                status = "ERROR"
            elif tc.find("skipped") is not None:
                status = "SKIPPED"
            else:
                status = "PASSED"
            results[test_id] = status
    except Exception as e:
        print(f"Warning: failed to parse {xml_path}: {e}", file=sys.stderr)
    return results


def run_pytest(cwd: Path, test_paths: list[str], xml_out: Path) -> Tuple[int, Dict[str, str], str]:
    """Run pytest with exact arguments on given directory and return (exit_code, results, stdout)."""
    cmd = [
        str(PYTHON_EXE),
        "-m",
        "pytest",
        *test_paths,
        "-q",
        "-p",
        "no:cacheprovider",
        f"--junitxml={xml_out}",
    ]
    print(f"Running pytest in {cwd} ...", flush=True)
    res = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True)
    results = parse_junit_xml(xml_out)
    return res.returncode, results, res.stdout + "\n" + res.stderr


def create_detached_worktree(worktree_dir: Path, commit_sha: str) -> None:
    """Safely create detached worktree at worktree_dir."""
    if worktree_dir.exists():
        remove_worktree(worktree_dir)
    worktree_dir.parent.mkdir(parents=True, exist_ok=True)
    run_cmd(["git", "worktree", "add", "--detach", str(worktree_dir), commit_sha])
    # Symlink untracked data folders so data-dependent tests have parity
    for folder in ("data", "analyst/replay_data"):
        src = REPO_ROOT / folder
        dst = worktree_dir / folder
        if src.exists() and not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.symlink(str(src), str(dst))
            except OSError:
                pass


def remove_worktree(worktree_dir: Path) -> None:
    """Remove detached worktree cleanly."""
    if not worktree_dir.exists():
        return
    try:
        run_cmd(["git", "worktree", "remove", "--force", str(worktree_dir)], check=False)
    except Exception:
        pass
    if worktree_dir.exists():
        shutil.rmtree(worktree_dir, ignore_errors=True)
    try:
        run_cmd(["git", "worktree", "prune"], check=False)
    except Exception:
        pass


def compare_results(
    target_results: Dict[str, str],
    base_results: Dict[str, str],
) -> Dict[str, any]:
    """Calculate pre-existing reds, new regressions, fixed reds, and new passes."""
    target_failed = {t for t, s in target_results.items() if s in ("FAILED", "ERROR")}
    target_passed = {t for t, s in target_results.items() if s == "PASSED"}
    target_skipped = {t for t, s in target_results.items() if s == "SKIPPED"}

    base_failed = {t for t, s in base_results.items() if s in ("FAILED", "ERROR")}
    base_passed = {t for t, s in base_results.items() if s == "PASSED"}

    pre_existing_reds = target_failed & base_failed
    new_regressions = target_failed - base_failed
    fixed_reds = base_failed & target_passed
    new_passing = target_passed - set(base_results.keys())

    return {
        "target_total": len(target_results),
        "target_passed": len(target_passed),
        "target_failed": len(target_failed),
        "target_skipped": len(target_skipped),
        "base_total": len(base_results),
        "base_passed": len(base_passed),
        "base_failed": len(base_failed),
        "pre_existing_reds": sorted(pre_existing_reds),
        "new_regressions": sorted(new_regressions),
        "fixed_reds": sorted(fixed_reds),
        "new_passing": sorted(new_passing),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Baseline diff test runner for Nifty Scanner & AI Analyst")
    parser.add_argument("--baseline", default=DEFAULT_BASELINE, help=f"Baseline commit/branch (default: {DEFAULT_BASELINE})")
    parser.add_argument("--target", default=None, help="Target branch/commit (default: current HEAD / working tree)")
    parser.add_argument("--worktree-dir", default=str(DEFAULT_WORKTREE_DIR), help="Temporary worktree directory")
    parser.add_argument("--test-paths", nargs="*", default=DEFAULT_TEST_PATHS, help="Pytest test paths")
    parser.add_argument("--baseline-xml", default=None, help="Path to precomputed baseline XML (skips baseline run)")
    parser.add_argument("--save-baseline-xml", default=None, help="Save baseline XML to this path for reuse")
    parser.add_argument("--target-xml", default=None, help="Save target XML to this path")

    args = parser.parse_args()

    worktree_dir = Path(args.worktree_dir)
    baseline_sha = resolve_sha(args.baseline)
    current_head = run_cmd(["git", "rev-parse", "HEAD"]).stdout.strip()

    print("================================================================================")
    print("BASELINE DIFF TEST HARNESS")
    print(f"Target:   {args.target or 'Current HEAD'} ({current_head[:10]})")
    print(f"Baseline: {args.baseline} ({baseline_sha[:10]})")
    print(f"Paths:    {' '.join(args.test_paths)}")
    print("================================================================================")

    tmp_dir = Path(tempfile.mkdtemp(prefix="baseline_diff_"))
    target_xml = Path(args.target_xml) if args.target_xml else tmp_dir / "target.xml"
    base_xml = Path(args.baseline_xml) if args.baseline_xml else tmp_dir / "baseline.xml"

    try:
        # Step 1: Run pytest on target (current working tree or target worktree)
        target_dir = REPO_ROOT
        target_worktree_used = False
        if args.target and resolve_sha(args.target) != current_head:
            target_worktree = tmp_dir / "target_worktree"
            create_detached_worktree(target_worktree, resolve_sha(args.target))
            target_dir = target_worktree
            target_worktree_used = True

        try:
            target_code, target_results, target_out = run_pytest(target_dir, args.test_paths, target_xml)
        finally:
            if target_worktree_used:
                remove_worktree(target_dir)

        # Step 2: Run pytest on baseline
        if args.baseline_xml and Path(args.baseline_xml).exists():
            print(f"Using precomputed baseline XML: {args.baseline_xml}")
            base_results = parse_junit_xml(Path(args.baseline_xml))
        else:
            print(f"Creating detached worktree for baseline at {worktree_dir} ...")
            create_detached_worktree(worktree_dir, baseline_sha)
            try:
                base_code, base_results, base_out = run_pytest(worktree_dir, args.test_paths, base_xml)
                if args.save_baseline_xml:
                    shutil.copyfile(base_xml, args.save_baseline_xml)
                    print(f"Saved baseline XML to {args.save_baseline_xml}")
            finally:
                remove_worktree(worktree_dir)

        # Step 3: Compare results
        diff = compare_results(target_results, base_results)

        total = diff["target_total"]
        passed = diff["target_passed"]
        failed = diff["target_failed"]
        pre_existing = len(diff["pre_existing_reds"])
        new_reg = len(diff["new_regressions"])

        print("\n================================================================================")
        print("BASELINE DIFF SUMMARY")
        print("================================================================================")
        print(f"Total: {total} | Passed: {passed} | Failed: {failed} | Pre-existing Reds: {pre_existing} | NEW Regressions: {new_reg}")
        print("--------------------------------------------------------------------------------")
        print(f"Baseline Total: {diff['base_total']} (Passed: {diff['base_passed']}, Failed: {diff['base_failed']})")
        print(f"Fixed Pre-existing Reds: {len(diff['fixed_reds'])}")
        print(f"New Tests Added:         {len(diff['new_passing'])}")

        if diff["pre_existing_reds"]:
            print(f"\nPre-existing Reds ({pre_existing}):")
            for t in diff["pre_existing_reds"]:
                print(f"  [PRE-EXISTING] {t}")

        if diff["new_regressions"]:
            print(f"\n[ALERT] NEW REGRESSIONS ({new_reg}):")
            for t in diff["new_regressions"]:
                print(f"  [NEW REGRESSION] {t}")
            print("\nVERDICT: BLOCK (New regressions detected)")
            return 1

        print("\nVERDICT: PASS (0 new regressions)")
        return 0

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        remove_worktree(worktree_dir)


if __name__ == "__main__":
    sys.exit(main())
