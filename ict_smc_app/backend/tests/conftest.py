"""Give every pytest session a clean database.

Every test module points ICT_DATA_DIR at tests/.data and they all share one sqlite file, but nothing ever
removed it — so state carried over between runs and the suite was not idempotent. Running it twice in a
row on the same .data produced different failures each time:

    run 1 (clean)   1 failed, 301 passed   test_pcr_trinity
    run 2 (dirty)   3 failed, 299 passed   test_gap_sniper, test_pe_ce_matrix, test_setup_log

None of those were real regressions, which is exactly the problem: a suite that fails differently each
run teaches you to ignore it, and then it cannot tell you when something has genuinely broken. The two
test_footprints failures seen on 28 Oct 2026 were the same thing — leftover footprint_levels rows from an
earlier run, so candles the test expects to have no ladder had one.

This wipes tests/.data once, at collection time. It runs at import rather than in a fixture because the
test modules read ICT_DATA_DIR and open the database while they are being imported, which happens before
any fixture would get a chance to run.

Deliberately session-scoped, not per-test: several tests seed the database for a later test in the same
module to read, and isolating each test would break that. The contract is that a RUN starts clean, which
is what makes two consecutive runs agree.
"""
import os
import shutil
from pathlib import Path

_DATA = Path(__file__).resolve().parent / ".data"

if os.environ.get("ICT_KEEP_TEST_DATA") != "1":          # escape hatch for inspecting a failure's state
    shutil.rmtree(_DATA, ignore_errors=True)

os.environ.setdefault("ICT_DATA_DIR", str(_DATA))
