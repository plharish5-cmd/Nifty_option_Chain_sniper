#!/usr/bin/env python3
"""Frozen Stage 2/3 cohort; only initial stop width varies. Offline, no execution.
Fixed existing 40-point Stage 2/3 target, initial stop / closed 15m 8EMA violation / 15:20.
The MFE study had no target; this trade-outcome experiment fixes the existing
Stage 2/3 40-point target before any run. Winning/losing final spot moves use
the requested 0.59/0.67 *scenario* deltas; these are not recorded option prices.
Charges use real tariff and exact entry snapshots only. Unpriced rows stay blank.
"""
import argparse
import csv
import hashlib
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import numpy as np
import pandas as pd
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from analyst.trend_continuation_entry_8510 import closed_bars
from analyst.trend_capture_research_8510 import day_labels,select_contract
from analyst.trend_capture_evaluation_8510 import master_lot
from paper_trading import calculate_trade_charges,DEFAULT_CONFIG,CONFIG_FILE
IST=ZoneInfo('Asia/Kolkata')


def replay(signal,minutes,bars,width,target=40):
    """Existing MFE risk horizon, no favorable ordering in a stop-hit bar.
    A stop and target in the same bar is ambiguous for final exit.
    Opening gaps use the first observed open, never a fictitious better stop fill.
    """
    entry=signal['spot_entry'];sign=1 if signal['side']=='CE' else -1
    end=int(datetime.fromisoformat(signal['day']+'T15:20:00').replace(tzinfo=IST).timestamp())
    f=minutes[(minutes.ts>=signal['ts'])&(minutes.ts<end)].sort_values('ts')
    s=bars[(bars.day==signal['day'])&(bars.end>signal['ts'])&(bars.end<=end)]
    violations=s[s.c<s.ema8] if sign==1 else s[s.c>s.ema8]
    structure_ts=int(violations.end.iloc[0]) if len(violations) else end
    expected=signal['ts'];stop=entry-sign*width
    result=dict(day=signal['day'],ts=signal['ts'],side=signal['side'],setup=signal['setup'],stop=width,
                entry=entry,spot_points=None,status='MISSING_MINUTE',exit_ts=None)
    for b in f.itertuples():
        if int(b.ts)!=expected:return result
        expected=int(b.ts)+60
        adverse=b.l if sign==1 else b.h
        hit=sign*(adverse-stop)<=0
        gap=sign*(b.o-stop)<=0
        favorable=b.h if sign==1 else b.l
        target_hit=sign*(favorable-entry)>=target
        if hit:
            if target_hit and not gap:
                return {**result,'status':'AMBIGUOUS','exit_ts':int(b.ts)+60}
            px=float(b.o) if gap else stop
            return {**result,'status':'INITIAL_SL','spot_points':round(sign*(px-entry),4),'exit_ts':int(b.ts)+60}
        if target_hit:
            return {**result,'status':'TARGET','spot_points':float(target),'exit_ts':int(b.ts)+60}
        if int(b.ts)+60>=structure_ts:
            return {**result,'status':'EMA8_STRUCTURE' if structure_ts<end else 'EOD_1520',
                    'spot_points':round(sign*(b.c-entry),4),'exit_ts':int(b.ts)+60}
    return result


def metrics(rows):
    resolved=[r for r in rows if r['spot_points'] is not None]
    wins=[r['spot_points'] for r in resolved if r['spot_points']>0]
    losses=[r['spot_points'] for r in resolved if r['spot_points']<0]
    aw=float(np.mean(wins)) if wins else None;al=abs(float(np.mean(losses))) if losses else None
    ratio=aw/al if aw is not None and al else None;be=100/(1+ratio) if ratio else None
    wr=100*len(wins)/len(resolved) if resolved else None
    priced=[r for r in rows if r.get('matched_net') is not None]
    byday={d:sum(r['matched_net'] for r in priced if r['day']==d) for d in sorted({r['day'] for r in priced})}
    gross_profit=sum(max(0,r['scenario_gross']) for r in priced)
    gross_byday={d:sum(max(0,r['scenario_gross']) for r in priced if r['day']==d) for d in byday}
    nw=[r['matched_net'] for r in priced if r['matched_net']>0];nl=[-r['matched_net'] for r in priced if r['matched_net']<0]
    net_be=100*np.mean(nl)/(np.mean(nw)+np.mean(nl)) if nw and nl else None
    return dict(n=len(rows),resolved=len(resolved),ambiguous=sum(r['status']=='AMBIGUOUS' for r in rows),
        missing=sum(r['status']=='MISSING_MINUTE' for r in rows),win_rate_pct=wr,avg_win_spot=aw,avg_loss_spot=al,
        payoff_ratio=ratio,break_even_WR_pct=be,clears_spot_BE=wr>be if wr is not None and be is not None else None,
        matched_priced_n=len(priced),net_per_trade=float(np.mean([r['matched_net'] for r in priced])) if priced else None,
        net_break_even_WR_pct=float(net_be) if net_be is not None else None,
        matched_net_WR_pct=100*len(nw)/len(priced) if priced else None,
        estimated_avg_win_rupees=float(np.mean(nw)) if nw else None,estimated_avg_loss_rupees=float(np.mean(nl)) if nl else None,
        estimated_payoff_ratio=float(np.mean(nw)/np.mean(nl)) if nw and nl else None,
        estimated_clears_BE=bool(100*len(nw)/len(priced)>net_be) if net_be is not None else None,
        worst_day=min(byday,key=byday.get) if byday else None,worst_day_net=min(byday.values()) if byday else None,
        best_day_profit_concentration_pct=100*max(gross_byday.values())/gross_profit if gross_profit else None)


def run(market,cohort_file,output,master=None,config_file=None):
    cfg=json.loads(Path(config_file or ROOT/'analyst/terminal8510_config.json').read_text());spec=cfg['recording_v193']
    frozen=json.loads(Path(cohort_file).read_text())
    signals=[r for r in frozen['rows'] if r['setup'] in ('STAGE_2','STAGE_3')]
    if len(signals)!=321:raise ValueError('Expected frozen existing 321 Stage 2/3 signals; refusing to regenerate/tune cohort')
    days=frozen['sessions']
    with sqlite3.connect('file:'+str(market)+'?mode=ro',uri=True) as c:
        minutes=pd.read_sql_query("SELECT ts,o,h,l,c,v FROM candles_1m WHERE sec_id='13' ORDER BY ts",c)
        chain=pd.read_sql_query('SELECT * FROM chain_snap ORDER BY ts',c)
    bars=closed_bars(minutes);minutes['day']=pd.to_datetime(minutes.ts,unit='s',utc=True).dt.tz_convert(IST).dt.strftime('%Y-%m-%d')
    selected=minutes[minutes.day.isin(days)];labels=day_labels(selected)
    master_path=Path(master or ROOT/'data/terminal8500/instrument_master.csv');lot=master_lot(master_path)
    m=pd.read_csv(master_path,low_memory=False)
    # Expiry dates verified from master; historical expiry classification abstains when unknown.
    expiries=set(str(x)[:10] for x in m.loc[(m.SEM_INSTRUMENT_NAME=='OPTIDX') & m.SEM_TRADING_SYMBOL.str.startswith('NIFTY-',na=False),'SEM_EXPIRY_DATE'].dropna())
    expiries |= set(chain.expiry.dropna().astype(str))
    fee_cfg={**DEFAULT_CONFIG,**(json.loads(CONFIG_FILE.read_text()) if CONFIG_FILE.exists() else {})}
    rows=[]
    for signal in signals:
        day=signal['day'];f=selected[selected.day==day]
        contract=select_contract(signal,chain)
        for width in spec['stop_widths']:
            r=replay(signal,f,bars,width,spec['study_target_pts']);r.update(trend=labels.get(day,{}).get('trend_body'),expiry=day in expiries,
                entry_premium=contract['entry'] if contract else None,scenario_gross=None,scenario_net=None,charges=None,matched_net=None)
            points=r['spot_points']
            if points is not None and contract:
                delta=spec['study_win_delta'] if points>0 else spec['study_loss_delta']
                exit_premium=contract['entry']+points*delta
                if exit_premium>0:
                    charges=calculate_trade_charges(contract['entry'],exit_premium,lot,cfg=fee_cfg)['total_charges']
                    r.update(assumed_delta=delta,scenario_exit_premium=exit_premium,charges=charges,
                             scenario_gross=points*delta*lot,scenario_net=points*delta*lot-charges)
            rows.append(r)
    # Identical priced/resolved cohort across widths, rather than cherry-picking coverage per width.
    matched={s['ts'] for s in signals if all(next(r for r in rows if r['ts']==s['ts'] and r['stop']==w)['scenario_net'] is not None for w in spec['stop_widths'])}
    for r in rows:r['matched_net']=r['scenario_net'] if r['ts'] in matched else None
    table=[]
    for width in spec['stop_widths']:
        for segment in ('ALL','TREND','CHOP','EXPIRY'):
            f=[r for r in rows if r['stop']==width and (segment=='ALL' or segment=='TREND' and r['trend'] or segment=='CHOP' and r['trend'] is False or segment=='EXPIRY' and r['expiry'])]
            table.append(dict(stop=width,segment=segment,**metrics(f)))
    base=next(r for r in table if r['stop']==20 and r['segment']=='ALL')
    for r in table:
        r['beats_20_ALL_priced_subset_only']=r['segment']=='ALL' and r['stop']!=20 and r['net_per_trade'] is not None and base['net_per_trade'] is not None and r['net_per_trade']>base['net_per_trade']
    result=dict(status='ADVISORY_NOT_VALIDATED',cohort_n=len(signals),cohort_sha256=hashlib.sha256(Path(cohort_file).read_bytes()).hexdigest(),sessions=days,
        lot_size=lot,lot_basis='Current instrument master only; historical lot changes unavailable, not historical executed PnL',
        method='Fixed existing 40-point Stage 2/3 target, initial stop/closed 15m 8EMA violation/15:20; MFE horizon retains EMA exit, but fixed target adds a trade-outcome exit absent from descriptive MFE. No tuning or trailing rule. Same-minute target/SL conflicts AMBIGUOUS. Missing minutes unresolved. Stops gap to first open. Frozen signals may overlap; not an executable portfolio. No partial-exit model.',
        pricing='ESTIMATED SCENARIOS ONLY: recorded exact entry premium + requested sign-specific delta (.59 win/.67 loss); real tariff; matched same contracts across widths; no spread/slippage estimate; not fills. Index outcomes use all resolvable signals. Expiry subset only from verified dates; unknown expiry is excluded, not assumed non-expiry.',
        verdict='NOT BUILT: no live stop change; no statistical edge established',table=table,rows=rows)
    out=Path(output);out.mkdir(parents=True,exist_ok=True);(out/'stop_width.json').write_text(json.dumps(result,indent=2,allow_nan=False))
    for name,data in [('stop_width_summary',table),('stop_width_trades',rows)]:
        with (out/(name+'.csv')).open('w',newline='') as fh:
            w=csv.DictWriter(fh,fieldnames=sorted(set().union(*(r.keys() for r in data))));w.writeheader();w.writerows(data)
    return result

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--market',required=True);p.add_argument('--cohort',default=str(ROOT/'analyst/stop_width_cohort_8510.json'));p.add_argument('--output',required=True);p.add_argument('--master');a=p.parse_args()
    r=run(a.market,a.cohort,a.output,a.master)
    print(json.dumps({k:v for k,v in r.items() if k!='rows'},indent=2))
