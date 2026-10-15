from app.engine.primitives import Bars
from app.engine.rjb import find_rjbs


class S:
    def __init__(self, kind, i, confirm_i):
        self.kind, self.i, self.confirm_i = kind, i, confirm_i


def _bars(rows):
    return Bars.from_rows([{"ts": 1_700_000_000 + 60 * k, "o": o, "h": h, "l": l, "c": c} for k, (o, h, l, c) in enumerate(rows)])


def test_bearish_rjb_from_upper_wick_and_invalidation():
    rows = [(100, 101, 99, 100), (100, 110, 99.5, 101),   # swing high, long upper wick: zone 101–110
            (101, 102, 98, 99), (99, 100, 97, 98),
            (98, 105, 97, 104),                              # touches zone (high 105 ≥ 101)
            (104, 112, 103, 111)]                            # close 111 > 110 → invalid
    b = _bars(rows)
    x = find_rjbs(b, [S("H", 1, 3)])[0]
    assert x.dir == "bear" and (x.bottom, x.top) == (101, 110)
    assert x.touched_i == 4 and x.invalid_i == 5 and x.state_at(4) == "touched"


def test_small_wick_is_not_an_rjb():
    rows = [(100, 101, 99, 100), (100, 103, 99, 102.5), (102, 102, 100, 101), (101, 101, 99, 100)]
    assert find_rjbs(_bars(rows), [S("H", 1, 3)]) == []
