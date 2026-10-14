import numpy as np
from app.engine.primitives import Bars
from app.engine.fvg import find_fvgs, find_ifvgs


def _bars(rows):
    return Bars.from_rows([{"ts": 1_700_000_000 + 60 * i, "o": o, "h": h, "l": l, "c": c}
                           for i, (o, h, l, c) in enumerate(rows)])


def test_bull_fvg_inverts_to_sell_zone_and_rejection():
    rows = [(100, 101, 99, 100), (100, 110, 100, 109), (109, 115, 106, 114),   # bull FVG 101–106
            (114, 114, 104, 105), (105, 105, 96, 97),                           # close 97 < 101 → inverted (sell zone)
            (97, 103, 96, 99),                                                  # retest high 103 into zone, close 99 < 101 → SELL
            (99, 100, 94, 95)]
    b = _bars(rows)
    g = find_fvgs(b, np.ones(len(b)), 1.0, 0.0)
    assert g and g[0].dir == "bull" and g[0].invalid_i == 4
    x = find_ifvgs(b, g)[0]
    assert x.dir == "bear" and x.i == 4 and x.retest_i == 5 and x.reject_i == 5 and x.state_at(6) == "rejected"


def test_ifvg_invalid_when_closed_back_through():
    rows = [(100, 101, 99, 100), (100, 110, 100, 109), (109, 115, 106, 114),
            (114, 114, 96, 97),                                                 # inverted
            (97, 108, 97, 107.5)]                                               # close above top 106 → IFVG invalid
    b = _bars(rows)
    x = find_ifvgs(b, find_fvgs(b, np.ones(len(b)), 1.0, 0.0))[0]
    assert x.invalid_i == 4 and x.reject_i is None
