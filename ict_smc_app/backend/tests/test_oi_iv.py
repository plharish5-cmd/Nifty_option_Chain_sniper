"""Futures OI build-up and IV / delta tracking."""
import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("ICT_DATA_DIR", str(Path(__file__).resolve().parent / ".data"))

from app import db  # noqa: E402
from app.engine import oi as oi_mod  # noqa: E402
from app.engine.primitives import Bars, atr  # noqa: E402
from app.market import ivtrack  # noqa: E402

D8 = 1791431100


def an_of(closes):
    rows, pc = [], closes[0]
    for k, c in enumerate(closes):
        rows.append({"ts": D8 + 300 * k, "o": pc, "h": max(pc, c) + 2, "l": min(pc, c) - 2, "c": c})
        pc = c
    b = Bars.from_rows(rows)
    return SimpleNamespace(tf="5m", bars=b, atr=atr(b, 14))


def test_four_build_ups():
    an = an_of([22000, 22020, 22000, 22020, 22000])
    oi = {D8 + 240: 1e7, D8 + 540: 1.01e7, D8 + 840: 1.02e7, D8 + 1140: 1.01e7, D8 + 1440: 1.0e7}
    r = oi_mod.per_bar(an, oi)
    assert "build" not in r[D8]                                   # nothing before it to compare with
    assert [r[D8 + 300 * k]["build"] for k in (1, 2, 3, 4)] == ["long build-up", "short build-up", "short covering", "long unwinding"]


def test_small_change_is_flat_and_new_day_resets():
    an = an_of([22000, 22020])
    r = oi_mod.per_bar(an, {D8 + 240: 1e7, D8 + 540: 1e7 + 100})
    assert r[D8 + 300]["build"] == "flat"
    an2 = SimpleNamespace(tf="5m", bars=Bars.from_rows([{"ts": D8 - 86400, "o": 1, "h": 2, "l": 0, "c": 1},
                                                         {"ts": D8, "o": 1, "h": 30, "l": 0, "c": 25}]), atr=[1, 1])
    r2 = oi_mod.per_bar(an2, {D8 - 86400 + 240: 1e7, D8 + 240: 2e7})
    assert "build" not in r2[D8]                                  # no cross-day ΔOI (rollover / new day)


def test_summary_dominant():
    an = an_of([22000, 21980, 21960, 21940])
    oi = {D8 + 240 + 300 * k: 1e7 * (1 + 0.002 * k) for k in range(4)}
    s = oi_mod.summary(an, oi_mod.per_bar(an, oi))
    assert s["dominant"] == "short build-up" and s["bias"] == "bear" and s["net_doi"] > 0


def test_iv_track_atm_and_strikes_and_trend():
    db.execute("DELETE FROM iv_series")
    T = ivtrack.IVTrack()
    ch = lambda ts, iv, d: {"ts": ts, "atm": 22000.0, "rows": [   # noqa: E731
        {"strike": 22000.0, "ce": {"iv": iv, "delta": d, "ltp": 100}, "pe": {"iv": iv + 1, "delta": d - 1, "ltp": 90}}]}
    picks = {"CE": {"strike": 22000.0, "key": "K-CE"}, "PE": None}
    for k in range(7):
        T.ingest(ch(D8 + 600 * k, 14.0 - 0.3 * k, 0.55 - 0.03 * k), picks, force=True)
    atm = ivtrack.series("ATM", D8)
    assert atm[0]["iv"] == 14.5 and len(atm) == 7
    tr = ivtrack.trend(atm, 60)
    assert tr["word"] == "falling" and abs(tr["chg"] + 1.8) < 1e-6
    d = ivtrack.trend(ivtrack.series("K-CE", D8), 375, "delta")
    assert d["word"] == "falling"
    assert ivtrack.per_bar(atm, "15m")[D8 + 1800]["iv"] == round(14.0 - 0.3 * 4 + 0.5, 2)


def test_oi_line_ema_and_surge():
    closes = [22000 + 2 * k for k in range(30)]
    an = an_of(closes)
    oi = {}
    v = 1e7
    for k in range(30):
        v += 3000 if k != 25 else 40000                     # one candle adds OI far faster than normal
        oi[D8 + 300 * k + 240] = v
    line = oi_mod.oi_line(an, oi_mod.per_bar(an, oi))
    by = {x["ts"]: x for x in line}
    assert by[D8 + 300 * 25]["surge"] and by[D8 + 300 * 25]["surge_x"] >= 2
    assert not any(x["surge"] for x in line if x["ts"] != D8 + 300 * 25)
    assert by[D8 + 300 * 29]["ema"] < by[D8 + 300 * 29]["oi"]        # EMA lags a rising OI


def test_nifty_banknifty_compare():
    bear = {"dominant": "short build-up", "bias": "bear"}
    bull = {"dominant": "short covering", "bias": "bull"}
    assert oi_mod.compare(bear, bear, None, None)["verdict"] == "aligned"
    d = oi_mod.compare(bear, bull, None, None)
    assert d["verdict"] == "diverging" and "more likely to fail" in d["text"]
    assert oi_mod.compare(bear, None, None, None) is None


def test_relative_uses_only_earlier_bars_and_flags_reduction():
    an=an_of([22000+k*2 for k in range(15)])
    value=1e7; samples={}
    for k in range(15):
        value+=-40000 if k==10 else 3000
        samples[D8+k*300+240]=value
    line=oi_mod.oi_line(an,oi_mod.per_bar(an,samples))
    assert line[10]['reduction'] and not line[10]['surge']
    assert line[10]['relative']==round(-40000/3000,3)
    prefix=SimpleNamespace(tf='5m',bars=Bars.from_rows([{'ts':int(an.bars.ts[i]),'o':float(an.bars.o[i]),'h':float(an.bars.h[i]),'l':float(an.bars.l[i]),'c':float(an.bars.c[i])} for i in range(11)]),atr=an.atr[:11])
    assert oi_mod.oi_line(prefix,oi_mod.per_bar(prefix,samples))==line[:11]


def test_contract_change_missing_and_stale_do_not_create_doi():
    an=an_of([22000+k*10 for k in range(5)])
    def sample(value,contract='A'):
        return dict(oi=value,contract=contract,verified=True,source='rest')
    rows={D8+240:sample(1e7),D8+540:sample(2e7,'B'),D8+840:sample(2.001e7,'B'),D8+1200:sample(2.002e7,'B')}
    r=oi_mod.per_bar(an,rows)
    assert r[D8+300]['quality']=='contract_change' and r[D8+300]['doi'] is None
    assert r[D8+600]['doi']==10000
    assert r[D8+900]['quality']=='missing'
    assert r[D8+1200]['quality']=='stale_sample' and r[D8+1200]['doi'] is None
    unknown=oi_mod.per_bar(an,{D8+240:dict(oi=1e7,contract=None,source='rest',verified=False)})
    assert unknown[D8]['quality']=='unverified_contract'


def test_oi_session_reset_does_not_use_previous_day_ema():
    an=SimpleNamespace(tf='5m',bars=Bars.from_rows([
        dict(ts=D8-86400,o=1,h=2,l=.5,c=1),dict(ts=D8,o=1,h=2,l=.5,c=1)]),atr=[1,1])
    bars=oi_mod.per_bar(an,{D8-86400+240:1e7,D8+240:2e7})
    line=oi_mod.oi_line(an,bars)
    assert line[-1]['ema']==2e7 and line[-1]['relative'] is None


def test_contract_bound_reader_prefers_rest_and_does_not_relabel_legacy(monkeypatch):
    import sqlite3
    from app.market import futures
    from app.timeutil import now_epoch
    connection=sqlite3.connect(':memory:',isolation_level=None)
    connection.row_factory=sqlite3.Row;connection.executescript(db.SCHEMA)
    monkeypatch.setattr(db,'_conn',connection)
    ts=int(now_epoch())//60*60-60
    db.kv_set('fut_sid_FUT','A')
    db.execute('INSERT INTO fut_oi VALUES (?,?,?)',(ts,900,'rest'))
    assert futures.oi_samples()[ts]['verified'] is False
    for contract,source,value in [('A','feed',1000),('A','rest',1100),('B','rest',999999)]:
        db.execute('INSERT INTO fut_oi_samples VALUES (?,?,?,?,?,?)',('FUT',contract,ts,source,value,int(now_epoch())))
    result=futures.oi_samples()[ts]
    assert result['oi']==1100 and result['contract']=='A' and result['source']=='rest'
    connection.close()


def test_rest_refresh_records_actual_requested_contract(monkeypatch):
    import sqlite3,asyncio
    from app.market import futures
    from app.timeutil import session_bounds,now_ist
    connection=sqlite3.connect(':memory:',isolation_level=None)
    connection.row_factory=sqlite3.Row;connection.executescript(db.SCHEMA)
    monkeypatch.setattr(db,'_conn',connection)
    # Most recent session start that is NOT in the future. Pinning this to today's 09:15 made the test fail
    # every morning before the open — refresh() correctly drops a future-dated candle, so it returned 0 and
    # the assert below blamed the contract id. Behaviour is unchanged once the market is open.
    from datetime import timedelta
    _d=now_ist().date()
    while int(session_bounds(_d)[0])>now_ist().timestamp(): _d-=timedelta(days=1)
    ts=int(session_bounds(_d)[0])
    class Rest:
        async def intraday(self,sid,*args,**kwargs):
            assert sid=='ACTUAL-ID'
            return [dict(ts=ts,o=100,h=101,l=99,c=100,v=1000,oi=123456)]
    assert asyncio.run(futures.refresh(Rest(),'ACTUAL-ID',1))==1
    row=db.query('SELECT * FROM fut_oi_samples')[0]
    assert row['contract']=='ACTUAL-ID' and row['source']=='rest' and row['oi']==123456
    connection.close()
