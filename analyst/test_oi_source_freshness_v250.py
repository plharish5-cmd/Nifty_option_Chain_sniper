import sqlite3
from analyst.oi_context_8510 import load_day_snapshots, _day_bounds


def test_fresh_same_expiry_source_beats_stale_canonical_without_splicing(tmp_path):
    start, _ = _day_bounds('2026-11-20')
    paths = [tmp_path/'canonical.db', tmp_path/'shadow.db']
    for index, path in enumerate(paths):
        with sqlite3.connect(path) as c:
            c.execute('CREATE TABLE chain_snap(expiry TEXT, ts REAL, strike REAL, ce_oi REAL, pe_oi REAL, ce_vol REAL, pe_vol REAL, ce_ltp REAL, pe_ltp REAL)')
            c.execute('INSERT INTO chain_snap VALUES(?,?,?,?,?,?,?,?,?)',('2026-11-23',start+index*600,25000,100+index,100,1,1,1,1))
            if index == 0:
                c.execute('INSERT INTO chain_snap VALUES(?,?,?,?,?,?,?,?,?)',('2026-12-01',start+1200,25000,999,100,1,1,1,1))
    rows = load_day_snapshots(paths,'2026-11-20')
    assert len(rows)==1 and rows[0]['ce_oi']==101
    assert rows[0]['expiry']=='2026-11-23'
    past = load_day_snapshots(paths,'2026-11-20',asof_ts=start+300)
    assert len(past)==1 and past[0]['ce_oi']==100
