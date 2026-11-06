"""Read-only, causal research inputs and MFE audit. Sparse snapshots stay sparse."""
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
import json,sqlite3,re
import numpy as np
import pandas as pd
from analyst.trend_continuation_entry_8510 import config,closed_bars,historical_signals
ROOT=Path(__file__).resolve().parents[1];IST=ZoneInfo('Asia/Kolkata')

def load_inputs(market=None,ledger=None,cfg=None):
    cfg=config(cfg);market=Path(market or ROOT/'ict_smc_app/backend/data/ict_smc.sqlite3')
    with sqlite3.connect('file:'+str(market)+'?mode=ro',uri=True) as c:
        minutes=pd.read_sql_query("SELECT ts,o,h,l,c,v FROM candles_1m WHERE sec_id='13' ORDER BY ts",c)
        chain=pd.read_sql_query('SELECT expiry,ts,strike,ce_ltp,pe_ltp FROM chain_snap ORDER BY ts',c)
    bars=closed_bars(minutes);days=sorted(bars.day.unique())[-cfg['sessions']:]
    # Keep earlier bars for indicator warm-up; restrict evaluation only after features are computed.
    selected=bars[bars.day.isin(days)].copy()
    try:
        from analyst.futures_volume_engine import FuturesVolumeEngine
        vol=FuturesVolumeEngine(db_path=market)
        selected['slot_rvol_median']=[vol.get_vsr_at_timestamp(int(t))['slot_rvol_median'] for t in selected.end]
    except (OSError,sqlite3.Error,KeyError):selected['slot_rvol_median']=None
    minutes['day']=pd.to_datetime(minutes.ts,unit='s',utc=True).dt.tz_convert(IST).dt.strftime('%Y-%m-%d')
    minutes=minutes[minutes.day.isin(days)].copy()
    chain['day']=pd.to_datetime(chain.ts,unit='s',utc=True).dt.tz_convert(IST).dt.strftime('%Y-%m-%d')
    chain=chain[chain.day.isin(days)].copy()
    ledger=Path(ledger or ROOT/'data/terminal8510/ledger.sqlite3');positions=[]
    if ledger.exists():
        with sqlite3.connect('file:'+str(ledger)+'?mode=ro',uri=True) as c:
            c.row_factory=sqlite3.Row
            if c.execute("select 1 from sqlite_master where name='positions'").fetchone():positions=[dict(r) for r in c.execute("select * from positions where status='CLOSED'")]
    return minutes,selected,chain,positions,days

def day_labels(minutes,cfg=None):
    cfg=config(cfg);out={}
    for day,g in minutes.groupby('day'):
        start=int(datetime.fromisoformat(day+'T09:15:00').replace(tzinfo=IST).timestamp());end=start+cfg.get('session_minutes',375)*60
        g=g[(g.ts>=start)&(g.ts<end)].sort_values('ts')
        if g.empty:continue
        body=abs(float(g.c.iloc[-1]-g.o.iloc[0]));span=float(g.h.max()-g.l.min())
        out[day]={'trend_body':body>=cfg['trend_body_pts'],'trend_detector_truth':body>=cfg['trend_body_pts'] or span>=cfg['trend_range_pts'],
                  'body_pts':body,'range_pts':span,'recorded_minutes':len(g),'expiry':False}
    return out

def distribution(values):
    x=np.asarray([v for v in values if v is not None and np.isfinite(v)],dtype=float)
    return {'n':len(x),**({k:round(float(v),3) for k,v in zip(['p25','p50','p75','p90','max'],np.quantile(x,[.25,.5,.75,.9,1]))} if len(x) else {})}

def stamp(value):
    if not value:return None
    try:return datetime.fromisoformat(str(value).replace(' IST','')).replace(tzinfo=IST).timestamp()
    except ValueError:return None

def position_signals(positions,minutes):
    out=[];excluded={}
    master=pd.read_csv(ROOT/'data/terminal8500/instrument_master.csv',low_memory=False)
    master=master[(master.SEM_INSTRUMENT_NAME=='OPTIDX')&master.SEM_TRADING_SYMBOL.str.startswith('NIFTY-',na=False)]
    identities={str(int(row.SEM_SMST_SECURITY_ID)):row for _,row in master.iterrows()}
    for p in positions:
        t=stamp(p.get('entry_time'));exit_t=stamp(p.get('exit_time'));symbol=str(p.get('symbol',''))
        if not t or not exit_t or not (symbol.endswith('CE') or symbol.endswith('PE')):excluded['bad_identity_or_time']=excluded.get('bad_identity_or_time',0)+1;continue
        day=datetime.fromtimestamp(t,IST).date().isoformat();eod=datetime.fromisoformat(day+'T15:20:00').replace(tzinfo=IST).timestamp()
        g=minutes[(minutes.ts>=int(np.ceil(t/60)*60))&(minutes.ts<eod)&(minutes.day==day)]
        if g.empty:excluded['no_regular_session_entry']=excluded.get('no_regular_session_entry',0)+1;continue
        identity=identities.get(str(p.get('security_id')));recorded_contract=None
        if identity is not None and symbol==identity.SEM_TRADING_SYMBOL:
            expiry=str(identity.SEM_EXPIRY_DATE)[:10]
            if expiry>=day:recorded_contract={'expiry':expiry,'strike':float(identity.SEM_STRIKE_PRICE),'side':symbol[-2:],'entry':float(p['entry_price']),'ts':int(g.ts.iloc[0])}
        out.append({'ts':int(g.ts.iloc[0]),'day':day,'side':symbol[-2:],'spot_entry':float(g.o.iloc[0]),'setup':'RECORDED_POSITION',
            'position_id':p['position_id'],'actual_exit_ts':exit_t,'entry_premium':p.get('entry_price'),'actual_exit_premium':p.get('exit_price'),
            'recorded_mode':p.get('mode'),'recorded_strategy':p.get('strategy_setup'),'exit_reason':p.get('exit_reason') or 'UNSPECIFIED','recorded_hold_minutes':(exit_t-t)/60,
            'spot_entry_basis':'Next observed minute open; actual entry spot was not recorded','recorded_contract':recorded_contract,'record':p})
    return out,excluded

def select_contract(signal,chain,cfg=None):
    cfg=config(cfg);ts=signal['ts'];day=signal['day'];side=signal['side']
    if signal.get('setup')=='RECORDED_POSITION':return signal.get('recorded_contract')
    # Selection uses only the option-chain snapshot exactly at decision time. No future expiries/strikes selected retrospectively.
    f=chain[(chain.ts==ts)&(chain.expiry>=day)]
    if f.empty:return None
    expiry=min(f.expiry);strike=round(signal['spot_entry']/100)*100+(-100 if side=='CE' else 100)
    f=f[(f.expiry==expiry)&(f.strike==strike)];col='ce_ltp' if side=='CE' else 'pe_ltp'
    if f.empty or not pd.notna(f.iloc[-1][col]) or f.iloc[-1][col]<=0:return None
    return {'expiry':expiry,'strike':strike,'side':side,'entry':float(f.iloc[-1][col]),'ts':ts}

def premium_series(contract,chain):
    f=chain[(chain.expiry==contract['expiry'])&(chain.strike==contract['strike'])].copy();col='ce_ltp' if contract['side']=='CE' else 'pe_ltp'
    f=f[f[col].notna()&(f[col]>0)];return dict(zip(f.ts.astype(int),f[col].astype(float)))

def measure_mfe(signals,minutes,bars,chain,cfg=None):
    cfg=config(cfg);result=[]
    for sig in signals:
        t=sig['ts'];day=sig['day'];entry=sig['spot_entry'];sgn=1 if sig['side']=='CE' else -1
        end=int(datetime.fromisoformat(day+'T15:20:00').replace(tzinfo=IST).timestamp())
        f=minutes[(minutes.ts>=t)&(minutes.ts<end)&(minutes.day==day)].sort_values('ts')
        if f.empty:continue
        hit=f[f.l<=entry-cfg['initial_spot_sl_pts']] if sgn==1 else f[f.h>=entry+cfg['initial_spot_sl_pts']]
        structure=bars[(bars.day==day)&(bars.end>t)&(bars.end<=end)]
        structure=structure[structure.c<structure.ema8] if sgn==1 else structure[structure.c>structure.ema8]
        risk_end=min(end,int(hit.ts.iloc[0]) if len(hit) else end,int(structure.end.iloc[0]) if len(structure) else end)
        contract=select_contract(sig,chain,cfg);premium=premium_series(contract,chain) if contract else {}
        rec={k:v for k,v in sig.items() if k not in ('record','state','recorded_contract')};rec['risk_end']=risk_end;rec['premium_entry_available']=bool(contract)
        actual=sig.get('actual_exit_ts');spot_exit=f[f.ts+60<=actual].c.iloc[-1] if actual and len(f[f.ts+60<=actual]) else None
        # Actual spot capture is only a minute-close proxy; recorded premium capture is separate.
        rec['actual_spot_capture_proxy']=sgn*(float(spot_exit)-entry) if spot_exit is not None else None
        for name,finish in [('in_trade',actual),('60m',min(t+3600,risk_end)),('120m',min(t+7200,risk_end)),('eod',risk_end),('unrestricted_eod',end)]:
            if finish is None:continue
            w=f[f.ts+60<=finish]
            favorable=sgn*((w.h if sgn==1 else w.l)-entry);adverse=sgn*((w.l if sgn==1 else w.h)-entry)
            mfe=max(0,float(favorable.max())) if len(w) else None if name=='in_trade' else 0.;rec[name+'_mfe']=mfe;rec[name+'_mae']=min(0,float(adverse.min())) if len(w) else 0.
            rec[name+'_mfe_timing_minutes']=(int(w.loc[favorable.idxmax(),'ts'])-t)/60 if len(w) else None
            rec[name+'_captured_fraction_proxy']=rec['actual_spot_capture_proxy']/max(mfe,.01) if spot_exit is not None and mfe is not None else None
            observed=[p for pt,p in premium.items() if t<=pt<finish]
            rec[name+'_premium_mfe_observed']=max(0,max(observed)-contract['entry']) if observed else None
            rec[name+'_premium_observations']=len(observed)
        result.append(rec)
    return result

def mfe_summary(rows,labels):
    summary=[]
    for cohort in sorted(set(r['setup'] for r in rows)):
      for segment in ('ALL','TREND','CHOP'):
        selected=[r for r in rows if r['setup']==cohort and (segment=='ALL' or labels.get(r['day'],{}).get('trend_body')==(segment=='TREND'))]
        for horizon in ('in_trade','60m','120m','eod','unrestricted_eod'):
            summary.append({'cohort':cohort,'segment':segment,'horizon':horizon,**distribution([r.get(horizon+'_mfe') for r in selected]),
                'option_observed':distribution([r.get(horizon+'_premium_mfe_observed') for r in selected]),
                'captured_fraction_proxy':distribution([r.get(horizon+'_captured_fraction_proxy') for r in selected])})
    return summary

def evidence_gate(rows,labels,cfg=None):
    cfg=config(cfg);base=[r for r in rows if r['setup'] in ('STAGE_2','STAGE_3')]
    all_d=distribution([r.get('eod_mfe') for r in base]);trend=distribution([r.get('eod_mfe') for r in base if labels[r['day']]['trend_body']])
    # Headroom alone permits building an advisory experiment; it proves no net edge.
    passed=all_d.get('p50',0)>cfg['gate_p50'] or all_d.get('p75',0)>=cfg['gate_p75'] or trend.get('p50',0)>=cfg['gate_trend_p50']
    return {'passes':passed,'all_risk_bounded_eod':all_d,'trend_risk_bounded_eod':trend,
        'verdict':'BUILD_ADVISORY_EXPERIMENT' if passed else 'FAILED_GATE_NO_TRAILING_BUILD',
        'limitations':'Risk-bounded horizon still stops at initial SL / closed EMA8 breach; unrestricted EOD is reported separately. Headroom is not expectancy. Sparse option observations understate true MFE.'}

def report_mfe(market=None,ledger=None,cfg=None):
    cfg=config(cfg);m,b,c,p,days=load_inputs(market,ledger,cfg);labels=day_labels(m,cfg)
    sig,states=historical_signals(b,cfg);ps,excluded=position_signals(p,m);rows=measure_mfe(ps+sig,m,b,c,cfg)
    summary=mfe_summary(rows,labels)
    reasons=[]
    for reason in sorted(set(r.get('exit_reason') for r in rows if r.get('exit_reason'))):
        rs=[r for r in rows if r.get('exit_reason')==reason];reasons.append({'exit_reason':reason,'in_trade':distribution([r.get('in_trade_mfe') for r in rs]),'eod':distribution([r.get('eod_mfe') for r in rs])})
    return {'sessions':days,'n_sessions':len(days),'chain_sessions':int(c.day.nunique()),'chain_timestamps':int(c.ts.nunique()),'closed_positions':len(p),
        'position_exclusions':excluded,'summary':summary,'exit_reason_summary':reasons,'gate':evidence_gate(rows,labels,cfg),'rows':rows,'session_labels':labels,
        'method':'Closed 15m indicators; next-minute decision timestamps; exact chain snapshot only. Latest 40 sessions, no tuning. Premium MFE is observed-snapshot MFE, not tick-level extrema. Pattern signals are not executed trades. No rupee PnL inferred from spot MFE.'}
