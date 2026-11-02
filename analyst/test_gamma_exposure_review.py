"""Read-only gamma projection: algebra, provenance, timestamps and replay isolation."""
import importlib.util
from pathlib import Path
import json
from datetime import datetime
import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('gamma_review_tests', ROOT/'analyst/gamma_exposure_review.py')
g = importlib.util.module_from_spec(spec); spec.loader.exec_module(g)
CFG = g.policy()


def frame():
    return pd.DataFrame(dict(strike=[22900,22950,23000,23050,23100],ce_oi=[1000]*5,pe_oi=[600]*5,ce_gamma=[.001]*5,pe_gamma=[.001]*5,iv_used=[20.]*5))


def test_positive_and_negative_conventions_reverse_net_not_gross():
    a,b=g.exposure(frame(),23000,CFG,1),g.exposure(frame(),23000,CFG,-1)
    assert a['ok'] and b['ok']
    assert a['net']==-b['net'] and a['gross']==b['gross']
    assert a['regime']=='positive' and b['regime']=='negative'


def test_contract_and_share_units_are_equivalent():
    a=g.exposure(frame(),23000,CFG)
    shares=frame();shares[['ce_oi','pe_oi']]*=CFG['lot_size']
    b=g.exposure(shares,23000,dict(CFG,oi_unit='shares'))
    assert a['net']==pytest.approx(b['net'])
    assert a['gross']==pytest.approx(b['gross'])


def test_gross_is_leg_sum_not_absolute_net():
    d=frame();d.pe_oi=d.ce_oi
    p=g.exposure(d,23000,CFG)
    assert p['net']==0 and p['gross']>0 and p['regime']=='neutral'


def test_small_negative_is_neutral():
    d=frame();d.pe_oi=1010
    p=g.exposure(d,23000,CFG)
    assert p['net']<0 and p['regime']=='neutral'


def test_invalid_greek_coverage_is_not_zero_or_neutral():
    d=frame();d.loc[0,'ce_gamma']=np.nan
    p=g.exposure(d,23000,CFG)
    assert not p['ok'] and 'net' not in p


def test_negative_oi_and_duplicate_strikes_rejected():
    d=frame();d.loc[0,'ce_oi']=-1
    assert not g.exposure(d,23000,CFG)['ok']
    assert not g.exposure(pd.concat([frame(),frame()]),23000,CFG)['ok']


def test_zero_gross_is_unavailable():
    d=frame();d[['ce_oi','pe_oi']]=0
    assert not g.exposure(d,23000,CFG)['ok']


def test_missing_repriced_zero_not_replaced_by_spot():
    assert g.repriced_flip(frame(),23000,datetime(2026,11,3,15,30),datetime(2026,10,30,12),CFG) is None


def test_repriced_zero_is_sign_invariant_and_distinct_from_strike_cumulative():
    d=frame();d.ce_oi=[1600,1300,1000,700,400];d.pe_oi=[400,700,1000,1300,1600]
    exp,now=datetime(2026,11,3,15,30),datetime(2026,10,30,12)
    a=g.repriced_flip(d,23000,exp,now,CFG,1);b=g.repriced_flip(d,23000,exp,now,CFG,-1)
    assert a is not None and b==pytest.approx(a)
    assert abs(a-23000)<300


def test_iso_expiry_is_not_day_month_swapped():
    assert g.iso_exp('2026-11-03')=='2026-11-03'
    assert g.iso_exp('03-Nov-2026')=='2026-11-03'


def test_asof_does_not_use_future_snapshot(monkeypatch):
    calls=[]
    monkeypatch.setattr(g,'profile',lambda r,*a,**kw:calls.append(r['ts']) or {'ok':True,'ts':r['ts']})
    recs=[dict(ts=100,file='a'),dict(ts=200,file='b')]
    p,_=g.asof_profile(recs,180,CFG,1,{})
    assert p['ts']==100 and calls==[100]
    assert g.asof_profile(recs,99,CFG,1,{})[0] is None
    assert g.asof_profile(recs,200+CFG['max_gamma_age_seconds']+1,CFG,1,{})[0] is None


def test_missing_greeks_are_rebuilt_at_captured_time(monkeypatch,tmp_path):
    d=frame().drop(columns=['ce_gamma','pe_gamma']);d['expiry']='03-Nov-2026'
    f=tmp_path/'snapshot.parquet';d.to_parquet(f)
    seen=[]
    def rebuild(df,spot,exp,now=None):
        seen.append(now);df=df.copy();df['ce_gamma']=.001;df['pe_gamma']=.001;return df
    monkeypatch.setattr(g.greeks,'add_greeks',rebuild)
    ts=g.epoch('2026-10-30T10:00:00')
    p=g.profile(dict(ts=ts,spot=23000,expiry='2026-11-03',file=f),CFG)
    assert p['ok'] and seen==[datetime(2026,10,30,10)]


def test_old_snapshot_cannot_be_reused_after_expiry(tmp_path):
    f=tmp_path/'snapshot.parquet';frame().to_parquet(f)
    p=g.profile(dict(ts=g.epoch('2026-11-03T15:31:00'),spot=23000,expiry='2026-11-03',file=f),CFG)
    assert not p['ok']


def test_study_spec_is_single_variant_and_chronological():
    s=CFG['study']
    assert s['variant_count']==1 and s['event_kind']=='BOS'
    assert s['non_overlap_minutes']>=s['forward_minutes']
    assert 0<s['holdout_fraction']<1


def test_missing_expiry_is_not_guessed_from_calendar(monkeypatch,tmp_path):
    base=tmp_path/'data/snapshots/2026-10-30';base.mkdir(parents=True)
    (base/'100000.meta.json').write_text(json.dumps({'timestamp':'2026-10-30T10:00:00','spot':23000}))
    frame().to_parquet(base/'100000.parquet')
    monkeypatch.setattr(g,'ROOT',tmp_path)
    assert g.records('2026-10-30')==[]


def test_duplicate_archive_records_do_not_double_count(monkeypatch,tmp_path):
    for source in ('snapshots','snapshots_multi'):
        base=tmp_path/'data'/source/'2026-10-30';base.mkdir(parents=True)
        (base/'100000.meta.json').write_text(json.dumps({'timestamp':'2026-10-30T10:00:00','spot':23000,'expiry':'03-Nov-2026'}))
        frame().to_parquet(base/'100000.parquet')
    monkeypatch.setattr(g,'ROOT',tmp_path)
    rows=g.records('2026-10-30')
    assert len(rows)==1 and rows[0]['source']=='snapshots'


def test_nonspot_greeks_are_not_reused_against_spot(monkeypatch,tmp_path):
    d=frame();d['expiry']='03-Nov-2026';d['underlying_source']='futures'
    f=tmp_path/'snapshot.parquet';d.to_parquet(f)
    calls=[]
    def rebuild(df,spot,exp,now=None):
        calls.append(spot);df=df.copy();df['underlying_source']='spot';return df
    monkeypatch.setattr(g.greeks,'add_greeks',rebuild)
    p=g.profile(dict(ts=g.epoch('2026-10-30T10:00:00'),spot=23000,expiry='2026-11-03',file=f),CFG)
    assert p['ok'] and calls==[23000]


def test_later_candles_do_not_change_earlier_bos_events(monkeypatch):
    import math
    from datetime import timedelta
    rows=[]
    for day_i, day in enumerate(('2026-10-29','2026-10-30')):
        opening, close=g.day_bounds(day)
        for i in range(375):
            j=day_i*375+i
            a=23000+j*.12+12*math.sin(j/11)
            b=23000+(j+1)*.12+12*math.sin((j+1)/11)
            rows.append(dict(ts=opening+i*60,o=a,h=max(a,b)+.1,l=min(a,b)-.1,c=b,v=1000))
    monkeypatch.setattr(g,'candles',lambda sec,start,end:[r for r in rows if start<=r['ts']<end])
    original=g.desk_config.rules()
    copied=dict(original,engine=dict(original['engine'],lookback_bars=20))
    monkeypatch.setattr(g.desk_config,'rules',lambda:copied)
    monkeypatch.setattr(g,'asof_profile',lambda *args:(None,'test: gamma unavailable'))
    opening,close=g.day_bounds('2026-10-30')
    cutoff=opening+180*60
    full,_=g.bos_context('2026-10-30',[],CFG,cutoff=close)
    prefix,_=g.bos_context('2026-10-30',[],CFG,cutoff=cutoff)
    expected=[(r['bar_ts'],r['direction'],r['level']) for r in prefix]
    observed=[(r['bar_ts'],r['direction'],r['level']) for r in full if r['known_at']<=cutoff]
    assert expected and observed==expected


def test_study_dates_stay_fixed_when_new_sessions_arrive():
    spec=CFG['study']
    planned=spec['development_days']+spec['holdout_days']
    days,train,holdout,missing=g.study_days(spec,planned+['2026-11-02','2026-11-03'])
    assert days==planned and train==spec['development_days'] and holdout==spec['holdout_days']
    assert not missing


def test_missing_study_day_does_not_reassign_holdout_dates():
    spec=CFG['study'];planned=spec['development_days']+spec['holdout_days']
    days,train,holdout,missing=g.study_days(spec,planned[1:])
    assert missing==planned[:1] and train==spec['development_days'] and holdout==spec['holdout_days']
    assert days==planned[1:]


def test_overlapping_or_out_of_order_study_slices_are_rejected():
    with pytest.raises(ValueError):
        g.study_days(dict(development_days=['2026-10-30'],holdout_days=['2026-10-29']),['2026-10-30','2026-10-29'])
