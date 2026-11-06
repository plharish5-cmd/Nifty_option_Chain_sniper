"""Counterfactual Stage 2B snapshot-price replay and descriptive premium time drift.
No estimated premium fallback. LTP results are not broker fills or a proven edge.
"""
import json,math
from pathlib import Path
from datetime import datetime
import numpy as np
import pandas as pd
from analyst.trend_continuation_entry_8510 import config,historical_signals
from analyst.trend_capture_research_8510 import ROOT,IST,load_inputs,day_labels,select_contract,premium_series,distribution,report_mfe

def master_lot(master=None):
    f=pd.read_csv(master or ROOT/'data/terminal8500/instrument_master.csv',low_memory=False)
    f=f[(f.SEM_INSTRUMENT_NAME=='OPTIDX')&f.SEM_TRADING_SYMBOL.str.startswith('NIFTY-',na=False)]
    lots=f.SEM_LOT_UNITS.dropna().astype(int).unique()
    if len(lots)!=1 or lots[0]<=0:raise ValueError('No unambiguous verified current-master NIFTY lot')
    return int(lots[0])

def baseline_replay(signal,minutes,chain,lot,super_cfg):
    # Reuse unchanged v1.1 functions, with no backend/engine instance and no broker.
    from analyst.terminal8510_backend import compute_trailing_sl,compute_trailing_target
    from paper_trading import calculate_trade_charges, DEFAULT_CONFIG, CONFIG_FILE
    fee_cfg={**DEFAULT_CONFIG,**(json.loads(CONFIG_FILE.read_text()) if CONFIG_FILE.exists() else {})}
    contract=select_contract(signal,chain)
    base={k:signal[k] for k in ('ts','day','side','setup','spot_entry')}
    if not contract:return {**base,'status':'NO_EXACT_ENTRY_SNAPSHOT','net':None,'gaps':0}
    quotes=premium_series(contract,chain);entry=contract['entry'];high=entry;sl=entry-super_cfg['initial_sl_pts'];target=entry+super_cfg['initial_target_pts'];status='FIXED';peak=entry;gaps=0
    eod=int(datetime.fromisoformat(signal['day']+'T15:20:00').replace(tzinfo=IST).timestamp())
    for ts in range(signal['ts']+60,eod+1,60):
        p=quotes.get(ts)
        if p is None:gaps+=1;continue
        high,sl=compute_trailing_sl(entry,p,high,sl,super_cfg['initial_sl_pts'],super_cfg['trail_activation_profit_pts'],super_cfg['trailing_jump_pts'])
        tr=compute_trailing_target(entry,p,status,target,peak,super_cfg['initial_target_pts'],super_cfg['trail_target_step_pts'],super_cfg['trail_target_pullback_pts'],super_cfg.get('trailing_target_enabled',True))
        status=tr['target_status'];target=tr['trailing_target_level'];peak=tr['highest_after_target']
        reason='INITIAL_OR_TRAIL_SL' if p<=sl else 'TRAIL_TARGET' if tr['should_exit'] else 'EOD' if ts==eod else None
        if reason:
            charges=calculate_trade_charges(entry,p,lot,cfg=fee_cfg)['total_charges'];gross=(p-entry)*lot
            return {**base,**contract,'status':'SNAPSHOT_PRICED','exit_ts':ts,'exit_premium':p,'exit_reason':reason,'gross':gross,'charges':charges,'net':gross-charges,'gaps':gaps,
                'hold_minutes':(ts-signal['ts'])/60,'premium_points':p-entry,'fill_basis':'Recorded LTP; missing minutes unobserved, no spread/slippage estimate'}
    return {**base,**contract,'status':'MISSING_EXACT_EOD_EXIT','net':None,'gaps':gaps}

def performance(rows,days,cfg=None):
    cfg=config(cfg);known=[r for r in rows if r.get('net') is not None];wins=[r['net'] for r in known if r['net']>0];loss=[r['net'] for r in known if r['net']<0]
    avgw=float(np.mean(wins)) if wins else None;avgl=float(np.mean(loss)) if loss else None
    byday={d:sum(r['net'] for r in known if r['day']==d) for d in sorted(set(r['day'] for r in known))};vals=np.array(list(byday.values()))
    # Session blocks preserve within-day dependence. All predefined variants and failures remain visible.
    rng=np.random.default_rng(cfg['random_seed']);band=np.quantile([rng.choice(vals,len(vals),replace=True).mean() for _ in range(cfg['bootstrap_trials'])],[.025,.975]) if len(vals) else []
    return {'candidates':len(rows),'priced_trades':len(known),'unpriced':len(rows)-len(known),'sessions_with_prices':len(set(r['day'] for r in known)),
        'net_per_trade':float(np.mean([r['net'] for r in known])) if known else None,'total_net':sum(r['net'] for r in known),
        'win_rate':len(wins)/len(known) if known else None,'avg_win_net':avgw,'avg_loss_net':avgl,
        'break_even_wr':abs(avgl)/(avgw+abs(avgl)) if avgw is not None and avgl is not None else None,
        'worst_day_net':min(byday.values()) if known else None,'session_mean_net_95_block_band':[float(v) for v in band],
        'gapped_trades':sum(r.get('gaps',0)>0 for r in known),'premium_data_gap_minutes':sum(r.get('gaps',0) for r in rows)}

def trade_deltas(rows,minutes,cfg=None):
    cfg=config(cfg);spot=dict(zip(minutes.ts.astype(int),minutes.o.astype(float)))
    for row in rows:
        if row.get('exit_ts') in spot:
            row['spot_exit_minute_open']=spot[row['exit_ts']]
            ds=row['spot_exit_minute_open']-row['spot_entry']
            row['realised_delta']=(row['exit_premium']-row['entry'])/ds if ds else None
            row['spot_move_abs']=abs(ds)
    return [{'side':side,'move':move,**distribution([r.get('realised_delta') for r in rows if r['side']==side and r.get('net') is not None and ((r.get('spot_move_abs',0)<cfg['normal_delta_move_pts']) if move=='NORMAL_<60' else (r.get('spot_move_abs',0)>=cfg['large_delta_move_pts']))])} for side in ('CE','PE') for move in ('NORMAL_<60','LARGE_>=100')]


def delta_theta(chain,minutes,cfg=None):
    cfg=config(cfg);spot=dict(zip(minutes.ts.astype(int),minutes.o.astype(float)));pairs=[]
    for (day,expiry,strike),g in chain.groupby(['day','expiry','strike']):
        g=g.sort_values('ts');dte=(datetime.fromisoformat(expiry)-datetime.fromisoformat(day)).days
        for side,col in [('CE','ce_ltp'),('PE','pe_ltp')]:
            f=g[g[col].notna()&(g[col]>0)&g.ts.isin(spot)].reset_index(drop=True)
            times=f.ts.to_numpy(dtype=int);premiums=f[col].to_numpy(dtype=float)
            for i in range(0,len(f),max(1,cfg.get('drift_anchor_stride',10))):
                at=times[i]
                selected_strike=round(spot[int(at)]/100)*100+(-100 if side=='CE' else 100)
                if strike!=selected_strike:continue
                # Descriptive observed pairs; binary search never interpolates missing timestamps.
                for horizon in cfg.get('drift_horizons_minutes',[15,60,120]):
                    j=int(np.searchsorted(times,at+horizon*60))
                    if j>=len(times) or times[j]>at+(horizon+5)*60:continue
                    zt=times[j];dt=(zt-at)/3600;ds=spot[int(zt)]-spot[int(at)];dp=float(premiums[j]-premiums[i]);duration=dt*60
                    pairs.append({'day':day,'dte':'0DTE' if dte==0 else '1DTE' if dte==1 else '2+DTE','side':side,'hours':dt,'ds':ds,'dp':dp,
                      'bucket':'<30m' if duration<30 else '30–90m' if duration<=90 else '90m+','delta':dp/ds if ds else None,
                      'move':'NORMAL_<60' if abs(ds)<cfg['normal_delta_move_pts'] else 'LARGE_>=100' if abs(ds)>=cfg['large_delta_move_pts'] else '60–100'})
    deltas=[{'side':side,'move':move,**distribution([p['delta'] for p in pairs if p['side']==side and p['move']==move])} for side in ('CE','PE') for move in ('NORMAL_<60','LARGE_>=100')]
    theta=[]
    for side in ('CE','PE'):
      for dte in ('0DTE','1DTE','2+DTE'):
       for bucket in ('<30m','30–90m','90m+'):
        ps=[p for p in pairs if p['side']==side and p['dte']==dte and p['bucket']==bucket]
        row={'side':side,'dte':dte,'hold_duration':bucket,'n':len(ps),'time_drift_pts_per_hour':None}
        if len(ps)>=cfg.get('drift_min_pairs',20):
            x=np.array([[p['ds'],p['hours'],1.] for p in ps]);y=np.array([p['dp'] for p in ps])
            if np.linalg.matrix_rank(x)==3:
                coef=np.linalg.lstsq(x,y,rcond=None)[0];res=y-x@coef
                row.update(time_drift_pts_per_hour=float(coef[1]),delta_coefficient=float(coef[0]),residual_std=float(np.std(res)),condition_number=float(np.linalg.cond(x)))
                drift=[(p['dp']-coef[0]*p['ds'])/p['hours'] for p in ps]
                row.update(mean_conditional_drift=float(np.mean(drift)),median_conditional_drift=float(np.median(drift)))
        theta.append(row)
    return {'realised_delta':deltas,'conditional_time_drift':theta,'pair_count':len(pairs),
        'limitation':'Descriptive fixed-strike/expiry/day pairs selected at ATM±100 ITM anchors, adjacent minute-open spot proxy. IV, spreads and liquidity are not controlled; this is NOT identified pure theta. Overlapping pairs are dependent. No estimated delta or separate theta charge is applied to snapshot-priced exits.'}

def run_study(mfe_path=None,market=None,ledger=None,output=None):
    cfg=config();m,b,c,p,days=load_inputs(market,ledger,cfg);labels=day_labels(m,cfg);signals,states=historical_signals(b,cfg)
    for day in labels:labels[day]['expiry']=day in set(c.expiry)
    truth=[d for d in days if labels[d]['trend_detector_truth']];chop=[d for d in days if not labels[d]['trend_detector_truth']]
    fired={d:any(v['trend_day_confirmed'] for t,v in states.items() if datetime.fromtimestamp(t,IST).date().isoformat()==d) for d in days}
    config_json=json.loads((ROOT/'analyst/terminal8510_config.json').read_text());lot=master_lot();rows=[]
    for sig in signals:
        if sig['setup']=='STAGE_2B':rows.append(baseline_replay(sig,m,c,lot,config_json['super_trail']))
    segments=[]
    for segment,ds in [('ALL',days),('TREND_BODY',[d for d in days if labels[d]['trend_body']]),('CHOP_BODY',[d for d in days if not labels[d]['trend_body']]),('EXPIRY_OBSERVED',[d for d in days if labels[d]['expiry']]),('FIRST_30',days[:-cfg['holdout_sessions']]),('LAST_10',days[-cfg['holdout_sessions']:])]:
        segments.append({'segment':segment,'n_sessions':len(ds),**performance([r for r in rows if r['day'] in ds],ds,cfg)})
    mfe=json.loads(Path(mfe_path).read_text()) if mfe_path else report_mfe(market,ledger,cfg)
    trade_delta=trade_deltas(rows,m,cfg)
    drift=delta_theta(c,m,cfg)
    drift['backtested_trade_realised_delta']=trade_delta
    for row in drift['conditional_time_drift']:
        value=row.get('time_drift_pts_per_hour');row['time_drift_rupees_per_hour']=value*lot if value is not None else None
    drift['estimated_delta_fallback_trades']=0
    drift['before_after_theta_adjustment']='Not applicable: no estimated-premium trades. Snapshot prices already include all premium drift; no second theta charge.'
    report={'mode':'ADVISORY_ONLY','parameters':cfg,'n_sessions':len(days),'sessions':days,'chain_sessions':int(c.day.nunique()),'dynamic_current_master_lot':lot,
        'gate':mfe['gate'],'mfe_summary':mfe['summary'],'position_exclusions':mfe['position_exclusions'],
        'detector':{'truth_definition':'EOD abs(close-open)>=150 OR range>=250; labels used only for evaluation, never entry',
            'true_positive_sessions':sum(fired[d] for d in truth),'trend_sessions':len(truth),'true_positive_rate':sum(fired[d] for d in truth)/len(truth) if truth else None,
            'false_positive_sessions':sum(fired[d] for d in chop),'chop_sessions':len(chop),'false_positive_rate':sum(fired[d] for d in chop)/len(chop) if chop else None,
            'fired_sessions':[d for d in days if fired[d]]},
        'stage2b_performance':segments,'stage2b_trades':rows,'delta_theta':drift,
        'trailing_comparison':{'status':'NOT_BUILT_FAILED_GATE','models_S_P':None,'reason':'Part 1 pre-registered runner-headroom thresholds did not pass; no trailing engine or invented comparison PnL.'},
        'trials_registered':['Stage2B defaults + unchanged v1.1 premium exit baseline','six trailing candidates CONDITIONAL on MFE gate: NOT RUN'],
        'limits':['All 40 spot sessions included; only 19 have any chain snapshots, fewer exact entries/exits.',
            'LTP-based counterfactuals omit unknown spread/slippage; gapped paths cannot establish missed intraminute stops.',
            'Current master lot sizes rupee scenarios; historical contract lots are not independently archived.',
            'Last 10 sessions form a chronological stability slice, NOT a pristine unseen holdout: recent sessions were already discussed.',
            'No tuning, no edge claim, no delta fallback; sparse prices cannot support a 40-session options-profitability verdict.',
            'Expiry segment includes only expiries present in the recorded chain; not a complete exchange expiry calendar.']}
    if output:Path(output).write_text(json.dumps(report,indent=2,allow_nan=False)+'\n')
    return report
