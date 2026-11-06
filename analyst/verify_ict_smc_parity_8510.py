"""analyst/verify_ict_smc_parity_8510.py — differential parity harness.

WHY THIS EXISTS
---------------
analyst/ict_smc_signals_8510.py is a Python PORT of verdict logic that lives in
the locked frontend (OcLab.tsx, regime.ts, deskState.ts). A port is worthless
unless it is proved to agree with the original, and "I read the TypeScript
carefully" is not a proof.

ict_smc_parity_8510.json claims 100% agreement on 17 fields across 357
recorded timestamps. That file is a RESULT, not evidence: it was committed
without the code that produces it, so the claim could not be re-run. This
module is that missing code.

HOW IT WORKS
------------
ict_smc_parity_oracle_8510.cjs is the ground truth. It does NOT reimplement
anything: it reads the locked .tsx/.ts sources, lifts the real declarations out
via the TypeScript AST, transpiles them, and runs them in a VM. The corridor and
radar expressions are sliced verbatim out of the original JSX, guarded by a
structural check that throws if the source shape changes. So the oracle executes
the frontend's own logic, and the lock is respected -- the sources are read,
never written.

This module runs the Python port over the same 357 fixtures and compares field
by field. Disagreement is reported, never smoothed over.

USAGE
  python3 -m analyst.verify_ict_smc_parity_8510
  python3 -m analyst.verify_ict_smc_parity_8510 --write   # refresh the result file
  python3 -m analyst.verify_ict_smc_parity_8510 --json

Exit status is 0 only on total agreement, so this is usable as a gate.
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
ORACLE = HERE / "ict_smc_parity_oracle_8510.cjs"
FIXTURES = HERE / "ict_smc_parity_fixtures_8510.json.gz"
RESULT = HERE / "ict_smc_parity_8510.json"

FIELDS = (
    "stage", "stageBadge", "actionTone", "suppressed", "convictionPct",
    "isActionable", "isHighConviction", "isSynergy", "isScissor",
    "isPullbackToSupply", "isPullbackToDemand", "isShortCoveringReversal",
    "spotChange15m", "radar", "corridorPct", "stateKey", "stateChange",
)

# Floats are compared with a tolerance; everything else must match exactly.
FLOAT_FIELDS = {"convictionPct", "spotChange15m", "corridorPct"}
TOL = 1e-6


def load_fixtures():
    with gzip.open(FIXTURES, "rt", encoding="utf-8") as fh:
        return json.load(fh)


def run_oracle(fixtures):
    """Execute the locked TypeScript and return its verdicts."""
    if not ORACLE.exists():
        raise FileNotFoundError("oracle missing: {}".format(ORACLE))
    proc = subprocess.run(
        ["node", str(ORACLE), str(ROOT)],
        input=json.dumps(fixtures), capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError("oracle failed (rc={}):\n{}".format(
            proc.returncode, (proc.stderr or "")[-4000:]))
    return json.loads(proc.stdout)


def run_port(fixtures):
    """Execute the Python port over the same prefixes."""
    from analyst.ict_smc_signals_8510 import classify_rows, context_verdicts

    out = []
    for fx in fixtures:
        raw_rows = fx["rows"]
        smart = classify_rows(raw_rows, fx.get("tf") or "3m")
        rows = []
        for idx, s in enumerate(smart):
            rec = {k: s.get(k) for k in FIELDS if k in s}
            # radar / corridorPct come from the context helper, not classify_rows.
            raw = raw_rows[idx] if idx < len(raw_rows) else {}
            try:
                rec.update(context_verdicts(raw, s))
            except Exception as exc:          # recorded, never swallowed silently
                rec["radar"] = "ERROR:{}".format(exc)
                rec["corridorPct"] = None
            rows.append(rec)
        out.append(rows)
    return out


def same(field, a, b):
    if field in FLOAT_FIELDS:
        try:
            fa, fb = float(a), float(b)
        except (TypeError, ValueError):
            return a == b
        if math.isnan(fa) and math.isnan(fb):
            return True
        return abs(fa - fb) <= TOL * max(1.0, abs(fa), abs(fb))
    return a == b


def compare(oracle, port):
    checked = {f: 0 for f in FIELDS}
    agreed = {f: 0 for f in FIELDS}
    mismatches = []
    n_ts = 0

    if len(oracle) != len(port):
        mismatches.append({"fixture": None, "error": "fixture count differs: "
                           "oracle={} port={}".format(len(oracle), len(port))})

    for fi, (o_rows, p_rows) in enumerate(zip(oracle, port)):
        if len(o_rows) != len(p_rows):
            mismatches.append({"fixture": fi, "error": "row count differs: "
                               "oracle={} port={}".format(len(o_rows), len(p_rows))})
        for ri, (o, p) in enumerate(zip(o_rows, p_rows)):
            n_ts += 1
            for f in FIELDS:
                if f not in o:
                    continue          # oracle did not produce it; nothing to prove
                checked[f] += 1
                if same(f, o.get(f), p.get(f)):
                    agreed[f] += 1
                elif len(mismatches) < 50:
                    mismatches.append({"fixture": fi, "row": ri, "field": f,
                                       "oracle": o.get(f), "port": p.get(f)})

    agreement = {}
    for f in FIELDS:
        # A field the oracle never emitted is NOT_COMPARED, not 100%.
        agreement[f] = (round(100.0 * agreed[f] / checked[f], 4)
                        if checked[f] else "NOT_COMPARED")
    return {
        "recorded_timestamps": n_ts,
        "fields_compared": {f: checked[f] for f in FIELDS},
        "agreement": agreement,
        "mismatches": mismatches,
        "total_mismatches": sum(checked[f] - agreed[f] for f in FIELDS),
    }


def verify():
    """Returns the comparison report. Raises if the oracle cannot run."""
    fixtures = load_fixtures()
    return compare(run_oracle(fixtures), run_port(fixtures))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--write", action="store_true",
                    help="write the report to ict_smc_parity_8510.json")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    a = ap.parse_args()

    rep = verify()

    if a.json:
        print(json.dumps(rep, indent=2, default=str))
    else:
        print("ICT/SMC PORT PARITY  (Python port vs the locked TypeScript)")
        print("=" * 66)
        print("fixtures     : {}".format(FIXTURES.name))
        print("timestamps   : {}".format(rep["recorded_timestamps"]))
        print()
        print("{:<28}{:>10}{:>14}".format("field", "compared", "agreement %"))
        print("-" * 66)
        for f in FIELDS:
            print("{:<28}{:>10}{:>14}".format(
                f, rep["fields_compared"][f], rep["agreement"][f]))
        print()
        bad = rep["total_mismatches"]
        notcmp = [f for f in FIELDS if rep["agreement"][f] == "NOT_COMPARED"]
        if bad:
            print("*** {} MISMATCHES — THE PORT DOES NOT AGREE ***".format(bad))
            for m in rep["mismatches"][:20]:
                print("  {}".format(m))
        else:
            print("No mismatches on any compared field.")
        if notcmp:
            print()
            print("NOT COMPARED (oracle emitted nothing): {}".format(", ".join(notcmp)))
            print("These fields are UNPROVEN, not agreed.")

    if a.write:
        RESULT.write_text(json.dumps(rep, indent=2, default=str) + "\n", encoding="utf-8")
        print("\nwrote {}".format(RESULT))

    return 0 if rep["total_mismatches"] == 0 else 1


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    raise SystemExit(main())
