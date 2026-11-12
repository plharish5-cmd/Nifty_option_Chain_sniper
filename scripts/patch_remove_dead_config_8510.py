#!/usr/bin/env python3
"""Owner request, 12 Nov 2026: remove config that no code reads any more.

Narrow config patch for analyst/terminal8510_config.json. Deletes ONLY:
    trend_filters                                (never read by the 8510 live path)
    writer_flow_gate                             (gate removed)
    stockmojo_live                               (external PE-CE poll removed)
    proven_edges.goldilocks_separation_enabled   (gate removed)
    proven_edges.futures_volume_surge_enabled    (engine removed)
    proven_edges.futures_volume_surge_min_ratio  (engine removed)
Everything else stays exactly as it is, including your local edits. A copy of the
file as it was is written next to it first (terminal8510_config.before_dead_config_patch.json).

Kept on purpose: proven_edges.goldilocks_min_atr / max_atr (the EMA proximity engine and
the five-factor score use them as band widths), futures_volume_engine (five-factor score
reads its thresholds), trend_capture_v192 (the research scripts read it).

Run from the repo root:   python3 scripts/patch_remove_dead_config_8510.py
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CFG = ROOT / "analyst/terminal8510_config.json"
TOP = ("trend_filters", "writer_flow_gate", "stockmojo_live")
PROVEN = ("goldilocks_separation_enabled", "futures_volume_surge_enabled", "futures_volume_surge_min_ratio")


def main() -> None:
    cfg = json.loads(CFG.read_text(encoding="utf-8"))
    removed = []
    for k in TOP:
        if k in cfg:
            del cfg[k]
            removed.append(k)
    pe = cfg.get("proven_edges")
    if isinstance(pe, dict):
        for k in PROVEN:
            if k in pe:
                del pe[k]
                removed.append("proven_edges." + k)
    if not removed:
        print("Nothing to remove; the config is already clean.")
        return
    shutil.copy(CFG, CFG.with_name("terminal8510_config.before_dead_config_patch.json"))
    CFG.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    for r in removed:
        print("removed", r)
    print("Restart the 8510 app.")


if __name__ == "__main__":
    main()
