"""api/nodes_api.py — /api/nodes : stacked D / W / M volume nodes (display only). Cached 5 minutes."""
from __future__ import annotations

import time
from typing import Any, Dict

from fastapi import APIRouter

from .. import config
from ..engine.nodes import stacked
from ..market import futures as fut_mod

router = APIRouter()
_cache: Dict[str, Any] = {"t": 0.0, "j": None}


@router.get("/api/nodes")
def nodes():
    if _cache["j"] is not None and time.time() - _cache["t"] < 300:
        return _cache["j"]
    from .main import _h
    h = _h()
    try:
        mp = fut_mod.mapped(h.rows_1m(32), fut_mod.rows_1m(32))
        j = stacked(mp, float(config.rule("nodes", "step_pts", 5.0)), float(config.rule("nodes", "tol_pts", 15.0)),
                    float(config.rule("nodes", "half_pts", 7.5)))
    except Exception as e:                                   # never break the desk over a display layer
        j = {"available": False, "message": f"nodes error: {e}"}
    _cache.update(t=time.time(), j=j)
    return j
