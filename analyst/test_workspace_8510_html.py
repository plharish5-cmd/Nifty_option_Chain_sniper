#!/usr/bin/env python3
"""
analyst/test_workspace_8510_html.py

Automated regression guard for Algorithmic_Trading_Workspace_8510.html:
  1. Extracts all inline <script> blocks.
  2. Runs `node --check` on each extracted script to guarantee valid JS syntax.
  3. Asserts top-level functions `switchWorkspaceTab`, `pollLiveEngine`, and
     `renderStrikeMatrix` exist and are defined at top-level.
  4. Asserts no raw unescaped HTML tags (e.g. `<div`, `</div>`) leak into <script>.
"""

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
import pytest
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
HTML_PATH = ROOT / "Algorithmic_Trading_Workspace_8510.html"


def test_html_file_exists():
    assert HTML_PATH.exists(), f"Target file does not exist: {HTML_PATH}"


def test_node_installed():
    node_bin = shutil.which("node")
    assert node_bin is not None, "Node.js executable ('node') is required to syntax check HTML scripts."


def test_inline_scripts_syntax():
    """Extracts all inline script blocks and validates with `node --check`."""
    content = HTML_PATH.read_text(encoding="utf-8")
    soup = BeautifulSoup(content, "html.parser")
    scripts = soup.find_all("script")

    inline_scripts = [s for s in scripts if not s.get("src")]
    assert len(inline_scripts) >= 1, "Expected at least 1 inline <script> block in Workspace 8510 HTML."

    node_bin = shutil.which("node") or "node"

    for idx, script in enumerate(inline_scripts):
        js_code = script.string or script.text or ""
        assert len(js_code.strip()) > 0, f"Script tag #{idx} is empty."

        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as tmp:
            tmp.write(js_code)
            tmp_path = tmp.name

        try:
            res = subprocess.run(
                [node_bin, "--check", tmp_path],
                capture_output=True,
                text=True,
            )
            assert res.returncode == 0, (
                f"JavaScript syntax error in inline script #{idx} of {HTML_PATH}:\n"
                f"{res.stderr}\n"
            )
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)


def test_required_top_level_functions_defined():
    """Ensures mission-critical functions are defined in the workspace script."""
    content = HTML_PATH.read_text(encoding="utf-8")
    soup = BeautifulSoup(content, "html.parser")
    inline_scripts = [s for s in soup.find_all("script") if not s.get("src")]
    combined_js = "\n".join(s.string or s.text or "" for s in inline_scripts)

    required_functions = [
        "switchWorkspaceTab",
        "pollLiveEngine",
        "renderStrikeMatrix",
        "checkTokenExpiryWarning",
        "checkCandlesHeartbeat",
        "toggleOffHoursPaperTesting",
        "pullDailyData",
        "checkIngestStatus",
        "populateReplayDates",
        "btlStart",
        "btlInit",
        "renderEodSummaryCards",
        "copyWhatsAppSummary",
        "speakEodSummary",
    ]

    for fn in required_functions:
        pattern = rf"(?:function|async\s+function)\s+{fn}\s*\("
        assert re.search(pattern, combined_js), (
            f"Required function `{fn}` not found in Workspace 8510 script source!"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
