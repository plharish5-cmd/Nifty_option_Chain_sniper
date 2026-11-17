#!/usr/bin/env python3
"""Add scanner.oi_3m_feature defaults (all disabled). Backs up config first."""
from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CFG = ROOT / "analyst/terminal8510_config.json"
BACKUP = CFG.with_suffix(f".json.bak-{datetime.now().strftime('%Y%m%d-%H%M%S')}")

DEFAULT = {
    "enabled": False,
    "gate": {
        "enabled": False,
        "window_min": 3,
        "sig_min": 0.15,
        "sig_extreme": 0.30,
        "mode": "log_only",
    },
    "context_panel": {
        "enabled": False,
        "show": [
            "pcr_zone",
            "walls",
            "day_oi",
            "pcr_futures",
            "ticket",
            "high_conviction",
            "pcr_trinity",
            "smart_oi",
            "pe_ce_matrix",
            "cvd",
        ],
    },
}


def main() -> None:
    if not CFG.is_file():
        raise SystemExit(f"missing {CFG}")
    shutil.copy2(CFG, BACKUP)
    print("backup:", BACKUP)
    cfg = json.loads(CFG.read_text(encoding="utf-8"))
    scanner = cfg.setdefault("scanner", {})
    before = scanner.get("oi_3m_feature")
    merged = dict(DEFAULT)
    if isinstance(before, dict):
        merged.update(before)
        g = dict(DEFAULT["gate"])
        g.update(before.get("gate") or {})
        merged["gate"] = g
        cp = dict(DEFAULT["context_panel"])
        cp.update(before.get("context_panel") or {})
        if isinstance(before.get("context_panel", {}).get("show"), list):
            cp["show"] = before["context_panel"]["show"]
        merged["context_panel"] = cp
    scanner["oi_3m_feature"] = merged
    CFG.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print("patched scanner.oi_3m_feature")
    print("before:", before)
    print("after:", merged)


if __name__ == "__main__":
    main()