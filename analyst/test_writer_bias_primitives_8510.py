from analyst.writer_bias_primitives_8510 import aggregate,buildup,regime,slope

CFG=dict(oi_window_strikes=6,oi_regime_band=.20,oi_use_delta_weight=True,oi_use_premium_weight=True,oi_writing_requires_price_confirm=True)

def row(side,strike=25000,delta=.5,doi=10,dp=-1,oi=100,price=100):
    return dict(side=side,strike=strike,delta=delta,d_oi=doi,d_ltp=dp,oi=oi,ltp=price,baseline_kind='MATCHED_TEST_PERIOD')

def test_writing_and_covering_are_heuristics():
    assert buildup(10,-1)=='SHORT_BUILDUP_HEURISTIC'
    assert buildup(10,1)=='LONG_BUILDUP'
    assert buildup(-10,1)=='SHORT_COVERING_HEURISTIC'
    assert buildup(-10,-1)=='LONG_UNWINDING'
    assert buildup(None,-1)=='UNAVAILABLE'

def test_buying_does_not_contribute_writing():
    a=aggregate([row('CE',dp=1),row('PE')],25000,CFG)
    assert a['masses']['CE']['writing']==0
    assert a['writing']['regime']=='BULLISH'

def test_delta_weights_and_window():
    rows=[row('CE',delta=.05),row('PE',delta=.5)]
    a=aggregate(rows,25000,CFG)
    assert a['masses']['PE']['writing']==10*a['masses']['CE']['writing']
    # Zero strikes either side: far strike is excluded, not clamped to ATM.
    rows += [row('CE',strike=26000,doi=100000)]
    a=aggregate(rows,25000,dict(CFG,oi_window_strikes=0))
    assert a['writing']['regime']=='BULLISH'
    assert a['scope']==[25000]

def test_bands_and_divergence():
    assert regime(20,100,.2)['regime']=='BULLISH'
    assert regime(-20,100,.2)['regime']=='BEARISH'
    assert regime(19,100,.2)['regime']=='NEUTRAL'
    a=aggregate([row('CE',oi=10,doi=20),row('PE',oi=1000,doi=1)],25000,CFG)
    assert a['divergence']
    assert a['writing']['regime']=='BEARISH'
    assert a['positional']['regime']=='BULLISH'

def test_missing_delta_and_mixed_periods_unavailable():
    assert aggregate([row('CE',delta=None),row('PE')],25000,CFG)['state']=='UNAVAILABLE'
    other=row('PE');other['baseline_kind']='OTHER_PERIOD'
    assert aggregate([row('CE'),other],25000,CFG)['state']=='UNAVAILABLE'

def test_slope_requires_history_and_same_scope():
    assert slope(200,20,180,'same','same',180)==1
    assert slope(200,20,179,'same','same',180) is None
    assert slope(200,20,180,'new_atm','old_atm',180) is None
