"""Read-only rendering of the exact existing execution gate evaluator."""
from datetime import datetime, timezone
from analyst.desk8600_lane_gating_8510 import LANES, evaluate_lane_gating


def snapshot(backend):
    board = backend.desk8600_board()
    sources = board.get('sources') or []
    return {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'scope': 'OI permission only; does not create a setup or confirm a broker fill',
        'sources': sources,
        'lanes': {lane: {side: evaluate_lane_gating(lane, side == 'CE', sources, backend.scanner_cfg)
                         for side in ('CE', 'PE')} for lane in LANES},
    }
