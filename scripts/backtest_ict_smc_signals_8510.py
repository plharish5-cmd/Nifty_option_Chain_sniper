#!/usr/bin/env python3
"""Causal ICT signal study. Never imports/instantiates an execution engine.
Spot targets are not option-premium targets. Premium scenarios are estimates,
never broker fills. Missing history stays missing; no interpolation/backfill.
"""
from pathlib import Path
import sys,json,sqlite3,math,argparse,csv,html
from datetime import datetime
from zoneinfo import ZoneInfo
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));IST=ZoneInfo('Asia/Kolkata')
import pandas as pd
import numpy as np
from analyst.ict_smc_signals_8510 import classify_rows


def recorded_prefixes(market, max_per_day=None):
    import pe_ce_panel as pc
    from oc_export import _f,_t
    with sqlite3.connect('file:'+str(market)+'?mode=ro',uri=True) as db:
        chain=pd.read_sql_query('SELECT * FROM chain_snap ORDER BY ts',db)
        spot=pd.read_sql_query("SELECT ts,o,h,l,c,v FROM candles_1m WHERE sec_id='13' ORDER BY ts",db)
    chain['day']=pd.to_datetime(chain.ts,unit='s',utc=True).dt.tz_convert(IST).dt.strftime('%Y-%m-%d')
    spot['day']=pd.to_datetime(spot.ts,unit='s',utc=True).dt.tz_convert(IST).dt.strftime('%Y-%m-%d')
    days=sorted(spot[spot.ts.isin(spot.ts)].day.unique())[-40:];chain=chain[chain.day.isin(days)]
    fixtures=[];coverage=[]
    for day,dd in chain.groupby('day'):
        exp=sorted(e for e in dd.expiry.unique() if e>=day)
        if not exp:continue
        dd=dd[dd.expiry==exp[0]];snaps=[];pxday=spot[spot.day==day]
        for stamp,x in dd.groupby('ts',sort=True):
            px=pxday[pxday.ts+60<=stamp]
            when=datetime.fromtimestamp(int(stamp),IST)
            if px.empty or stamp-(int(px.iloc[-1].ts)+60)>300 or not '09:15'<=when.strftime('%H:%M')<'15:20':continue
            snaps.append((pd.Timestamp(when),float(px.iloc[-1].c),x.drop(columns=['day','ts'])))
        if len(snaps)<3:continue
        s=pc.per_snapshot(snaps,None,pc._rules()['oi_multiplier'])
        # Last observation for each bucket, known only at its own timestamp.
        points=list(range(2,len(s)))
        if max_per_day:points=points[::max(1,len(points)//max_per_day)]
        coverage.append({'day':day,'snapshots':len(s),'replayed_prefixes':len(points)})
        for j in points:
            t=pc.table_rows(s.iloc[:j+1].copy(),3);rows=[]
            for x in t.itertuples():
                rows.append(dict(ts=x.ts.strftime('%H:%M'),**{k:_f(getattr(x,k,None)) for k in ('pe_day','pe_chg','ce_day','ce_chg','pe_ce_day','pe_ce_chg','pcr','vol_pcr','close','sup','res')},lv=_t(getattr(x,'lv',None)),act=_t(getattr(x,'act',None)),read=_t(x.read),pro=_t(getattr(x,'pro',None))))
            fixtures.append({'day':day,'asof':snaps[j][0].isoformat(),'rows':rows[::-1],'tf':'3m'})
    return fixtures,spot,chain,days,coverage


def score_path(bars, entry, sign, target, stop):
    """Both arms see identical minute bars and gates; ambiguous stays unpriced."""
    mfe=mae=0.;status='HORIZON';exit_price=None;exit_ts=None
    for r in bars.itertuples():
        favorable=(r.h-entry) if sign>0 else (entry-r.l)
        adverse=(entry-r.l) if sign>0 else (r.h-entry)
        mfe=max(mfe,float(favorable));mae=max(mae,float(adverse))
        hit=favorable>=target;loss=adverse>=stop
        if hit and loss:return dict(status='AMBIGUOUS',mfe=mfe,mae=mae,points=None,exit_ts=int(r.ts)+60)
        if hit or loss:return dict(status='TARGET' if hit else 'STOP',mfe=mfe,mae=mae,points=target if hit else -stop,exit_ts=int(r.ts)+60)
        exit_price=float(r.c);exit_ts=int(r.ts)+60
    return dict(status=status,mfe=mfe,mae=mae,points=(exit_price-entry)*sign if exit_price is not None else None,exit_ts=exit_ts)


def run(fixtures, spot, chain, days, cfg, lot):
    from paper_trading import calculate_trade_charges,DEFAULT_CONFIG,CONFIG_FILE
    fees={**DEFAULT_CONFIG,**(json.loads(CONFIG_FILE.read_text()) if CONFIG_FILE.exists() else {})}
    observations=[];seen=set();candidates=0
    for f in fixtures:
        stamp=int(datetime.fromisoformat(f['asof']).timestamp());day=f['day']
        # Closed buckets only. Partial row may be visible live, but is not tradable here.
        rows=[r for r in f['rows'] if int(datetime.fromisoformat(day+'T'+r['ts']).replace(tzinfo=IST).timestamp())+180<=stamp]
        if not rows:continue
        classified=classify_rows(rows,f.get('tf','3m'));r=classified[0]
        side={'bull':'CE','bear':'PE'}.get(r['actionTone'])
        for group,key in [('State Change','stateChange'),('Actionable Setups','isActionable'),('High Conviction','isHighConviction'),('Burst','isHighConviction')]:
            signature=(day,rows[0]['ts'],group)
            if not side or not r[key] or signature in seen:continue
            seen.add(signature);candidates+=1
            eod=int(datetime.fromisoformat(day+'T15:20').replace(tzinfo=IST).timestamp())
            # Entry is next full observed minute open after decision, never backdated to bucket start.
            start=int(math.ceil(stamp/60)*60)
            entrybars=spot[(spot.ts==start)&(spot.day==day)]
            if entrybars.empty or start>=eod:continue
            entry=float(entrybars.o.iloc[0]);snap=chain[(chain.ts==stamp)&(chain.expiry>=day)]
            expiry=sorted(snap.expiry.unique());snap=snap[snap.expiry==expiry[0]] if expiry else snap
            for arm,sign in [('signal',1 if side=='CE' else -1),('opposite',-1 if side=='CE' else 1)]:
                opt_side='CE' if sign>0 else 'PE';contract=snap.copy();premium=None;strike=None
                if not contract.empty:
                    contract['dist']=abs(contract.strike-entry);contract=contract.sort_values(['dist','strike']);v=contract.iloc[0];strike=float(v.strike);premium=float(v.ce_ltp if opt_side=='CE' else v.pe_ltp)
                    if not math.isfinite(premium) or premium<=0:premium=None
                for horizon in cfg['horizons_minutes']:
                    end=eod if horizon=='EOD' else min(eod,start+int(horizon)*60)
                    path=spot[(spot.day==day)&(spot.ts>=start)&(spot.ts<end)].sort_values('ts')
                    if len(path)!=(end-start)//60:continue # same completeness gate for both arms
                    outcome=score_path(path,entry,sign,cfg['target_spot_points'],cfg['stop_spot_points'])
                    outcome['forward_mfe'] = max(0,float(path.h.max()-entry if sign>0 else entry-path.l.min()))
                    outcome['forward_mae'] = max(0,float(entry-path.l.min() if sign>0 else path.h.max()-entry))
                    for delta in cfg['measured_delta_scenarios']:
                        net=None;gross=None;basis='UNPRICED';exit_premium=None
                        if outcome['points'] is not None and premium is not None:
                            # Linear premium scenario, not a claimed observed fill or option profitability.
                            exit_premium=max(0,premium+outcome['points']*delta)
                            charges=calculate_trade_charges(premium,exit_premium,lot,cfg=fees)['total_charges'];gross=(exit_premium-premium)*lot;net=gross-charges;basis='ESTIMATED_LINEAR_DELTA; entry recorded LTP, exit estimated'
                        observations.append(dict(day=day,decision_ts=stamp,group=group,side=opt_side,arm=arm,horizon=horizon,delta_scenario=delta,lot=lot,strike=strike,entry_spot=entry,entry_premium=premium,exit_premium=exit_premium,net=net,gross=gross,premium_basis=basis,**outcome))
    return observations,candidates


def summarize(rows, cfg):
    groups=[]
    for key in sorted({(r['group'],str(r['horizon']),r['delta_scenario']) for r in rows}):
        g,h,d=key;x=[r for r in rows if (r['group'],str(r['horizon']),r['delta_scenario'])==key]
        result=dict(group=g,horizon=h,delta=d)
        for arm in ('signal','opposite'):
            vals=[r for r in x if r['arm']==arm];known=[r for r in vals if r['net'] is not None]
            day_gross={day:sum(r['gross'] for r in known if r['day']==day) for day in {r['day'] for r in known}}
            positive=sum(max(0,v) for v in day_gross.values());concentration=max(day_gross.values())/positive*100 if positive else None
            result[arm]=dict(mean_forward_mfe=float(np.mean([r.get('forward_mfe',r['mfe']) for r in vals])) if vals else None,mean_forward_mae=float(np.mean([r.get('forward_mae',r['mae']) for r in vals])) if vals else None,n=len(vals),priced=len(known),ambiguous=sum(r['status']=='AMBIGUOUS' for r in vals),target=sum(r['status']=='TARGET' for r in vals),stop=sum(r['status']=='STOP' for r in vals),mean_mfe=float(np.mean([r['mfe'] for r in vals])) if vals else None,mean_mae=float(np.mean([r['mae'] for r in vals])) if vals else None,net_per_trade=float(np.mean([r['net'] for r in known])) if known else None,best_day_gross_share_pct=concentration)
        # Paired session means, rather than treating overlapping alerts as independent.
        from scipy.stats import t as student_t
        signal={(r['day'],r['decision_ts']):r['net'] for r in x if r['arm']=='signal' and r['net'] is not None}
        opposite={(r['day'],r['decision_ts']):r['net'] for r in x if r['arm']=='opposite' and r['net'] is not None}
        paired={k:signal[k]-opposite[k] for k in signal.keys() & opposite.keys()}
        block=[float(np.mean([v for (day,stamp),v in paired.items() if day==d])) for d in sorted({k[0] for k in paired})]
        alpha=cfg['family_alpha']/cfg['hypotheses'];n=len(block)
        t_stat=float(np.mean(block)/(np.std(block,ddof=1)/math.sqrt(n))) if n>1 and np.std(block,ddof=1)>0 else None
        result['direction_control_session_blocks']={'n_sessions':n,'mean_paired_net_difference':float(np.mean(block)) if n else None,'t_stat':t_stat,'two_sided_p':float(student_t.sf(abs(t_stat),n-1)*2) if t_stat is not None else None,'bonferroni_alpha':alpha,'critical_t':float(student_t.ppf(1-alpha/2,n-1)) if n>1 else None,'validated':False}
        groups.append(result)
    return groups


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--market',default=str(ROOT/'ict_smc_app/backend/data/ict_smc.sqlite3'));parser.add_argument('--out',required=True);parser.add_argument('--fixtures');parser.add_argument('--sample-per-day',type=int)
    args=parser.parse_args();out=Path(args.out);out.mkdir(parents=True,exist_ok=True)
    cfg=json.loads((ROOT/'analyst/ict_smc_research_8510.json').read_text())
    if args.fixtures:
        fixtures=json.loads(Path(args.fixtures).read_text())
        with sqlite3.connect('file:'+args.market+'?mode=ro',uri=True) as db:
            spot=pd.read_sql_query("SELECT ts,o,h,l,c,v FROM candles_1m WHERE sec_id='13' ORDER BY ts",db);chain=pd.read_sql_query('SELECT * FROM chain_snap ORDER BY ts',db)
        for df in (spot,chain):df['day']=pd.to_datetime(df.ts,unit='s',utc=True).dt.tz_convert(IST).dt.strftime('%Y-%m-%d')
        days=sorted(spot.day.unique())[-cfg['sessions']:];fixtures=[f for f in fixtures if f['day'] in days];cov=[]
    else:fixtures,spot,chain,days,cov=recorded_prefixes(args.market,args.sample_per_day)
    from analyst.trend_capture_evaluation_8510 import master_lot
    lot=master_lot();rows,n=run(fixtures,spot,chain,days,cfg,lot)
    chain_days=len(set(chain[chain.day.isin(days)].day));priced=len({(r['day'],r['decision_ts'],r['group']) for r in rows if r['net'] is not None and r['arm']=='signal'})
    coverage=f'CHAIN COVERAGE FOR THIS BACKTEST: {chain_days} of {len(days)} sessions ({100*chain_days/len(days):.1f}%). Signal evaluations scenario-priced (ESTIMATED premium exits): {priced} of {n} ({100*priced/n if n else 0:.1f}%). VERDICT IS PROVISIONAL UNTIL RECORDING IMPROVES THIS.'
    summary=summarize(rows,cfg);report=dict(status='ADVISORY — NOT VALIDATED FOR EXECUTION',coverage=coverage,recorded_prefixes=len(fixtures),sampled=bool(args.fixtures or args.sample_per_day),lot_from_current_master=lot,historical_lot_caveat='Current instrument-master lot; historical contract lot changes not independently verified.',hypotheses=cfg['hypotheses'],bonferroni_alpha=cfg['family_alpha']/cfg['hypotheses'],verdict='NO_GO. No independent held-out validation; estimated exits are scenarios, not demonstrated option profits.',limitations=['Chain previous-close change absent: original adapter uses first-recorded-snapshot baseline.','All-strikes source scope; does not claim parity with an ATM-window screen.','Overlapping signals are correlated; t statistic not asserted from individual trades.','Synthetic premium exits use measured delta scenarios; they do not model IV, spread or slippage.','Direction control uses the same timestamps and data gates.','Active Corridor and Pullback Radar are context; Writer Stack-up remains NOT BUILT.'],summary=summary,session_coverage=cov)
    (out/'report.json').write_text(json.dumps(report,indent=2,allow_nan=False));(out/'observations.json').write_text(json.dumps(rows,allow_nan=False))
    if rows:
        with (out/'observations.csv').open('w') as f:w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
    sections=[]
    for group in sorted(set(r['group'] for r in summary)):
        table='<tr><th>Horizon / delta</th><th>n / priced / ambiguous</th><th>Signal net ₹ (estimated)</th><th>Opposite net ₹ (estimated)</th><th>Best day share</th></tr>'
        for r in summary:
            if r['group']!=group:continue
            a=r['signal'];b=r['opposite'];fmt=lambda x:'—' if x is None else f'{x:.2f}'
            table+=f"<tr><td>{r['horizon']} / {r['delta']}</td><td>{a['n']} / {a['priced']} / {a['ambiguous']}</td><td>{fmt(a['net_per_trade'])}</td><td>{fmt(b['net_per_trade'])}</td><td>{fmt(a['best_day_gross_share_pct'])}%</td></tr>"
        sections.append(f'<h2>{html.escape(group)}</h2><p class="coverage">{coverage}</p><table>{table}</table>')
    (out/'report.html').write_text('<!doctype html><meta charset="utf-8"><title>ICT SMC Desk Signals — research</title><style>body{background:#0b1220;color:#e2e8f0;font:16px system-ui;margin:32px}td,th{padding:10px;border:1px solid #334155}table{border-collapse:collapse}.coverage{color:#fbbf24}</style><h1>ICT SMC Desk Signals — provisional research</h1><p>'+html.escape(report['verdict'])+'</p><p>Multiple comparisons: '+str(cfg['hypotheses'])+'; adjusted alpha '+str(report['bonferroni_alpha'])+'</p><p>'+html.escape(' '.join(report['limitations']))+'</p>'+''.join(sections))
    print(json.dumps({k:v for k,v in report.items() if k not in ('summary','session_coverage')},indent=2))
if __name__=='__main__':main()
