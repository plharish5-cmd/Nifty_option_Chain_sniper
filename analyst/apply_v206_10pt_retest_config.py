"""Apply Harish's approved 8 EMA Prototype 10-point retest setup to terminal8510_config.json.
Preserves all runtime settings, secrets, broker tokens, IP, and other strategies.

Parameters applied:
- touch_tol_pts: 10.0 (expanded from 3.0 to capture 8-10 pt wick retests)
- no_entry_before_hm: "09:15" (with 09:15 candle strictly arming-only; entries start from 09:30)
- away_min_pts: 2.0 (owner confirmed 2 points)
- reentry_same_side: True (re-entry on same side after profit)
- reentry_after_stop_loss: False (stop-loss cancels the side until opposite flip)
- slow_market_exact_line: False (ensures 10.0 pt tolerance is active)
- ribbon_filter_enabled: False
- no_entry_from_hm: "15:20" (EOD square-off cutoff)
"""
from pathlib import Path
import json
import os
import tempfile


def apply(path: Path) -> Path:
    path = Path(path)
    cfg = json.loads(path.read_text())
    scanner = cfg.setdefault("scanner", {})
    proto = scanner.setdefault("ema8_prototype", {})
    proto["touch_tol_pts"] = 10.0
    proto["no_entry_before_hm"] = "09:15"
    proto["away_min_pts"] = 2.0
    proto["reentry_same_side"] = True
    proto["reentry_after_stop_loss"] = False
    proto["slow_market_exact_line"] = False
    proto["ribbon_filter_enabled"] = False
    proto["no_entry_from_hm"] = "15:20"

    fd, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(cfg, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    return path


if __name__ == "__main__":
    target = Path(__file__).resolve().parent / "terminal8510_config.json"
    applied = apply(target)
    print(f"Applied 10-point retest configuration to {applied}")
