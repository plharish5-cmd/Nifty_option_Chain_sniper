"""
engine/smc.py — Smart Money Concepts (SMC) Engine.
Exposes FVG_Tracker, detect_sweep_iofed_setup, and full SMC pipeline.
"""
from __future__ import annotations

import sys
from pathlib import Path

_root_dir = str(Path(__file__).resolve().parent.parent)
if _root_dir not in sys.path:
    sys.path.insert(0, _root_dir)

from smc import *
import smc as _root_smc

__all__ = [name for name in dir(_root_smc) if not name.startswith("_")]
