"""analyst/test_config_not_polluted_8510.py — the test suite must not ship its
own fixtures as live settings.

THE DEFECT THIS CATCHES
-----------------------
Several tests in this repo call update_dhan_config / testing-override endpoints
against the REAL analyst/terminal8510_config.json instead of a tmp_path copy.
Running the suite therefore rewrites live settings and leaves them rewritten:

  - whitelisted_ip  49.205.39.195 -> 127.0.0.1
  - allow_off_hours_paper_testing  false -> true

This is not hypothetical. chatgpt/v1.9.4-ict-signals COMMITTED
"whitelisted_ip": "127.0.0.1" after a test run. Dhan authorises API calls from
a whitelisted address, so that value reaching the desk breaks live broker
connect; and allow_off_hours_paper_testing=true removes the gate that stops the
scanner acting outside market hours.

The honest fix is for those tests to use a temp config. Until they do, this file
is the tripwire: it fails if polluted values are ever committed again, which is
the point at which the damage becomes permanent.

These assertions are about the SHIPPED file on disk, deliberately. They are not
parametrised and must not be relaxed to make a suite run green -- a green suite
that has rewritten the trading config is the failure mode being guarded against.
"""
import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "analyst" / "terminal8510_config.json"

LOCALHOST = {"127.0.0.1", "0.0.0.0", "localhost", "::1"}


@pytest.fixture(scope="module")
def cfg():
    return json.loads(CONFIG.read_text(encoding="utf-8"))


def _whitelisted_ip(cfg):
    return (cfg.get("dhan") or {}).get("whitelisted_ip") or cfg.get("whitelisted_ip")


def test_whitelisted_ip_is_not_a_test_fixture(cfg):
    """Dhan authorises by source address; localhost here breaks live connect."""
    ip = _whitelisted_ip(cfg)
    assert ip, "whitelisted_ip missing from config"
    assert ip not in LOCALHOST, (
        "whitelisted_ip is {!r} -- a test fixture value has been committed. "
        "Dhan will refuse API calls. Restore the desk's real public IP.".format(ip)
    )


def test_off_hours_paper_testing_is_off_in_the_shipped_config(cfg):
    """This gate is what stops the scanner acting outside market hours."""
    assert cfg.get("allow_off_hours_paper_testing") is False, (
        "allow_off_hours_paper_testing is {!r} in the shipped config. A test run "
        "flips this to true; it must not be committed that way."
        .format(cfg.get("allow_off_hours_paper_testing"))
    )


def test_paper_and_algo_only_remain_on_in_the_shipped_config(cfg):
    """v1.9.4 makes these permanent in code; config must not imply otherwise."""
    assert cfg.get("paper_execution_enabled") is True
    assert cfg.get("algo_only_execution") is True


def test_real_money_is_not_armed_in_the_shipped_config(cfg):
    """Real money must never be armed by a file sitting in version control."""
    assert cfg.get("real_money_enabled") is not True
    assert cfg.get("execution_mode") in (None, "", "PAPER_ONLY")
    safety = cfg.get("execution_safety_v194") or {}
    assert safety.get("live_validation_status") != "APPROVED", (
        "live_validation_status is APPROVED in the committed config. Real-money "
        "approval is a deliberate, evidenced decision, not a repo default."
    )


def test_no_live_credential_secret_is_committed(cfg):
    """AGENTS.md: secrets live only in .env. A client id is an account
    identifier, not a secret, so it is allowed; tokens and keys are not."""
    blob = json.dumps(cfg)
    for key in ("DHAN_ACCESS_TOKEN", "access_token", "DHAN_API_SECRET",
                "api_secret", "DHAN_API_KEY", "api_key"):
        val = cfg.get(key) or (cfg.get("dhan") or {}).get(key)
        assert not (isinstance(val, str) and val.strip()
                    and not val.strip().startswith(("****", "••"))), \
            "{} appears to hold a real value in the committed config".format(key)
    assert "eyJhbGciOi" not in blob, "a JWT is committed in the config"


def test_config_is_not_dirty_relative_to_git(cfg):
    """Advisory: if the working copy differs from HEAD after a suite run, the
    suite wrote to the live file. Skipped where git is unavailable."""
    import subprocess
    try:
        out = subprocess.run(
            ["git", "-C", str(ROOT), "status", "--porcelain", "--",
             "analyst/terminal8510_config.json"],
            capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        pytest.skip("git unavailable")
    if out.returncode != 0:
        pytest.skip("not a git work tree")
    if out.stdout.strip() and os.environ.get("ALLOW_DIRTY_CONFIG") != "1":
        pytest.skip(
            "config is modified in the working tree: {!r}. If this appeared "
            "during a test run, a test is writing to the live config."
            .format(out.stdout.strip()))


# ─────────────────────────────────────────────────────────────────────────────
# v1.9.4c — the off-hours override must not survive anything
#
# ROOT CAUSE OF THE 06 NOV JUNK LEDGER. set_off_hours_paper_testing() used to
# write allow_off_hours_paper_testing=true to the live config. Tests call it
# against the real file, so the flag stuck true, and since it is the only thing
# permitting out-of-session execution the scanner recorded paper trades at
# 02:49, 03:50, 04:10 and 16:56 IST. Those rows plus duplicates of one fill
# were the Rs85,321 the replay reported.
# ─────────────────────────────────────────────────────────────────────────────

def _bare_backend(tmp_path):
    import sys
    sys.path.insert(0, str(ROOT))
    from analyst.terminal8510_backend import QuantTerminalBackend
    b = object.__new__(QuantTerminalBackend)
    b.cfg = {}
    b.config_path = tmp_path / "config.json"
    b.allow_off_hours_paper_testing = False
    b._off_hours_expires_at = 0.0
    return b


def test_enabling_off_hours_never_writes_to_config(tmp_path):
    b = _bare_backend(tmp_path)
    res = b.set_off_hours_paper_testing(True)
    assert res["ok"] is True
    assert res["persisted"] is False
    assert b.cfg.get("allow_off_hours_paper_testing") is False, \
        "the flag must not be recorded as enabled in config"
    assert not b.config_path.exists(), "no config file should have been written"


def test_off_hours_override_expires_on_its_own(tmp_path):
    b = _bare_backend(tmp_path)
    b.set_off_hours_paper_testing(True, minutes=1)
    assert b.off_hours_testing_active() is True
    # Wind the clock past the expiry rather than sleeping.
    b._off_hours_expires_at = 1.0
    assert b.off_hours_testing_active() is False, "a stale override must expire"
    assert b.allow_off_hours_paper_testing is False, "and must clear itself"


def test_disabling_is_immediate(tmp_path):
    b = _bare_backend(tmp_path)
    b.set_off_hours_paper_testing(True)
    b.set_off_hours_paper_testing(False)
    assert b.off_hours_testing_active() is False


def test_a_persisted_true_cannot_revive_it(tmp_path):
    """Even with the bad value still on disk, nothing resumes out-of-session
    execution: off_hours_testing_active() reads the in-memory flag, which the
    constructor forces to False."""
    b = _bare_backend(tmp_path)
    b.cfg["allow_off_hours_paper_testing"] = True      # the stale value
    assert b.off_hours_testing_active() is False


def test_the_execution_gates_use_the_expiring_check():
    """Source guard: both gates must call off_hours_testing_active(), not read
    the raw attribute, or an expired override still permits a trade."""
    src = (ROOT / "analyst" / "terminal8510_backend.py").read_text(encoding="utf-8")
    # Two call sites: the execution gate and the position gate. The
    # definition line reads "def off_hours_testing_active(self)" and so
    # does not match this pattern.
    assert src.count("off_hours_testing_active()") >= 2
    assert "is_paper_exec and self.allow_off_hours_paper_testing" not in src
    assert "is_paper_pos and self.allow_off_hours_paper_testing" not in src
