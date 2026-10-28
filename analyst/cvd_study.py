"""Predeclared CVD research; never imports an order API or writes the source DB.

Usage: python analyst/cvd_study.py --db path --output path [--phase development|final]
Final reads only the first predeclared number of complete verified holdout sessions.
"""
from __future__ import annotations
import argparse,collections,datetime,hashlib,json,math,random,sqlite3,statistics,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'ict_smc_app/backend'))
from app.delta.recorder import quality_gate,provenance_config


def digest(obj):
    return hashlib.sha256(json.dumps(obj,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def append_ledger(path, record):
    path.parent.mkdir(parents=True,exist_ok=True)
    # Exclusive create of registration/final marker is handled separately. Every run is retained.
    with path.open('a') as f:
        f.write(json.dumps(record,sort_keys=True)+'\n')


def eligible_sessions(connection, phase, plan, gate):
    """ONE gate feeds the observed and random arms. No legacy backfill allowed."""
    exists=connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cvd_observations'").fetchone()
    if not exists:
        return {},{'no_provenance_table':1}
    comparator,cutoff= ('<=',plan['development_end']) if phase=='development' else ('>=',plan['holdout_start'])
    records=connection.execute(f'SELECT session,contract,ts,source,payload,conflict,digest FROM cvd_observations WHERE session{comparator}? ORDER BY session,ts',(cutoff,)).fetchall()
    days=collections.defaultdict(list);reject=collections.Counter()
    for session,contract,ts,source,payload,conflict,stored_digest in records:
        if hashlib.sha256(payload.encode()).hexdigest()!=stored_digest:
            reject["payload_digest_mismatch"]+=1
            continue
        row=json.loads(payload)
        # Stored envelope, including contract, must agree with immutable payload.
        if (row.get('session'),row.get('contract'),row.get('ts'),row.get('source')) != (session,contract,ts,source):
            reject['envelope_mismatch']+=1
            continue
        row['conflict']=conflict
        days[session].append(row)
    clean={}
    for day,rows in sorted(days.items()):
        if len({r['contract'] for r in rows})!=1:
            reject['session_contract_switch']+=1;continue
        if len(rows)!=375 or len({r['ts'] for r in rows})!=375:
            reject['session_incomplete']+=1;continue
        errors=[error for row in rows for error in quality_gate(row,gate)]
        if errors:
            reject.update(errors);continue
        start=rows[0]['ts']
        if [r['ts'] for r in rows] != list(range(start,start+22500,60)):
            reject['session_discontinuity']+=1;continue
        clean[day]=rows
    if phase=='final':clean=dict(list(clean.items())[:plan['holdout_sessions']])
    return clean,dict(reject)


def aggregate(rows, minutes):
    result=[];cvd=pv=vol=0.
    for i in range(0,len(rows),minutes):
        group=rows[i:i+minutes]
        if len(group)!=minutes:break
        for r in group:
            pv+=(r['h']+r['l']+r['c'])/3*r['v'];vol+=r['v'];cvd+=r['delta']
        result.append(dict(ts=group[0]['ts'],o=group[0]['o'],h=max(r['h'] for r in group),l=min(r['l'] for r in group),c=group[-1]['c'],delta=sum(r['delta'] for r in group),cvd=cvd,vwap=pv/vol))
    return result


def bucket(value,bins):
    return next((i for i in range(len(bins)-1) if bins[i]<=value<bins[i+1]),None)


def opportunities(rows,minutes,plan):
    """Price-pivot structure entries are the shared opportunity universe, without CVD selection."""
    c=plan['rules'];bs=aggregate(rows,minutes);piv={1:[],-1:[]};pending=[];out=[];half=c['pivot_half']
    for i,b in enumerate(bs):
        j=i-half
        if j>=half:
            p=bs[j];window=bs[j-half:j+half+1]
            for sign,field in [(1,'l'),(-1,'h')]:
                values=[x[field] for x in window]
                if p[field]!=(min(values) if sign==1 else max(values)) or values.count(p[field])!=1:continue
                sample=bs[max(0,j-c['normalization_bars']+1):j+1]
                pf=max(c['min_price_diff'],statistics.median(x['h']-x['l'] for x in sample)*c['price_range_mult'])
                df=max(c['min_cvd_diff'],statistics.median(abs(x['delta']) for x in sample)*c['delta_scale_mult'])
                for k in reversed(piv[sign]):
                    if j-k>c['max_gap']:break
                    if j-k<c['min_gap'] or sign*(p[field]-bs[k][field])>-pf:continue
                    middle=bs[k+1:j]
                    pending.append(dict(known=i,sign=sign,stop=p[field]-sign*c['stop_buffer'],trigger=max(x['h'] for x in middle) if sign==1 else min(x['l'] for x in middle),cvd_ok=sign*(p['cvd']-bs[k]['cvd'])>=df,done=False,pivot=p['ts']))
                    break
                piv[sign].append(j)
        for p in pending:
            if p['done']:continue
            sign=p['sign']
            if sign*((b['l'] if sign==1 else b['h'])-p['stop'])<=0 or i-p['known']>=c['expiry_bars']:
                p['done']=True;continue
            if i<=p['known'] or sign*(b['c']-p['trigger'])<=c['structure_buffer']:continue
            p['done']=True
            if i+1>=len(bs):continue
            entry=bs[i+1]['o'];risk=sign*(entry-p['stop'])
            if risk<=0:continue
            history=bs[max(0,i-c['normalization_bars']+1):i+1]
            volatility=statistics.median(x['h']-x['l'] for x in history)
            minutes_held=min(c['horizon_bars']*minutes,(len(bs)-i-1)*minutes)
            key=(rows[0]['session'],sign,(bs[i+1]['ts']-rows[0]['ts'])//(plan['matching']['time_bucket_minutes']*60),bucket(volatility,plan['matching']['volatility_bins_points']),bucket(risk,plan['matching']['risk_bins_points']),minutes_held)
            t=dict(day=rows[0]['session'],ts=bs[i+1]['ts'],sign=sign,entry=entry,stop=p['stop'],risk=risk,match_key=key,
                   selected={'matched_cvd':p['cvd_ok'],'matched_cvd_vwap':p['cvd_ok'] and sign*(b['c']-b['vwap'])>c['vwap_buffer'],'matched_cvd_vwap_delta':p['cvd_ok'] and sign*(b['c']-b['vwap'])>c['vwap_buffer'] and sign*b['delta']>0})
            path=rows[(i+1)*minutes:(i+1)*minutes+minutes_held]
            t.update(outcome(path,t,c));out.append(t)
    # One price structure opportunity per direction/timestamp; deterministic earliest pivot.
    unique={}
    for o in out:unique.setdefault((o['ts'],o['sign']),o)
    return list(unique.values())


def outcome(path,event,c):
    sign=event['sign'];entry=event['entry'];stop=event['stop'];target=entry+sign*event['risk']*c['rr']
    exit_price=path[-1]['c'];reason='TIME'
    for row in path:
        if sign*(row['o']-stop)<=0:exit_price=row['o'];reason='STOP_GAP';break
        if sign*(row['o']-target)>=0:exit_price=row['o'];reason='TARGET_GAP';break
        stop_hit=row['l']<=stop if sign==1 else row['h']>=stop
        target_hit=row['h']>=target if sign==1 else row['l']<=target
        if stop_hit or target_hit:
            exit_price=stop if stop_hit else target;reason='STOP' if stop_hit else 'TARGET';break
    net=sign*(exit_price-entry)-c['roundtrip_cost_points']
    return dict(net_points=net,net_r=net/event['risk'],exit_reason=reason)


def quantile(values,p):
    values=sorted(values)
    if not values:return None
    f=(len(values)-1)*p;i=int(f)
    return values[i]+(values[min(i+1,len(values)-1)]-values[i])*(f-i)


def evaluate(events,variant,plan,rng,all_days):
    groups=collections.defaultdict(list)
    for e in events:groups[tuple(e['match_key'])].append(e)
    matched=[];unmatched=0
    for key,pool in groups.items():
        selected=[e for e in pool if e['selected'][variant]]
        # Without selective variation within a stratum this leg cannot be distinguished from controls.
        if selected and len(selected)<len(pool):matched.append((key,pool,selected))
        else:unmatched+=len(selected)
    if not matched:return dict(status='insufficient_matched_controls',selected=sum(e['selected'][variant] for e in events),unmatched=unmatched)
    def metric(chosen):
        by_day={d:[] for d in all_days}
        for e in chosen:by_day[e['day']].append(e['net_r'])
        return {d:statistics.mean(v) if v else 0. for d,v in by_day.items()}
    selected=[e for _,_,ss in matched for e in ss];observed=metric(selected);obs=statistics.mean(observed.values())
    draws=[];controls_by_day={d:[] for d in all_days}
    for _ in range(plan['random_repetitions']):
        picked=[e for _,pool,ss in matched for e in rng.sample(pool,len(ss))]
        daily=metric(picked);draws.append(statistics.mean(daily.values()))
        for d,v in daily.items():controls_by_day[d].append(v)
    difference={d:observed[d]-statistics.mean(controls_by_day[d]) for d in all_days}
    delta=statistics.mean(difference.values());days=list(all_days)
    # Bootstrap paired whole-session aggregates, never shuffle intraday returns.
    boots=[statistics.mean(difference[d] for d in rng.choices(days,k=len(days))) for _ in range(plan['bootstrap_repetitions'])]
    p=(1+sum(x>=obs for x in draws))/(len(draws)+1)
    adjusted=min(1.,p*plan['family_size'])
    interval=[quantile(boots,plan['alpha']/(2*plan['family_size'])),quantile(boots,1-plan['alpha']/(2*plan['family_size']))]
    sufficient=len(days)>=plan['minimum_sessions']
    return dict(status='evaluated' if sufficient else 'insufficient_sessions',selected=sum(e['selected'][variant] for e in events),matched=len(selected),unmatched=unmatched,sessions=len(days),observed_mean_session_net_r=obs,random_mean_session_net_r=statistics.mean(draws),difference=delta,
        random_luck_band=[quantile(draws,plan['alpha']/2),quantile(draws,1-plan['alpha']/2)],
        familywise_bootstrap_interval=interval,randomization_p=p,bonferroni_p=adjusted,
        clears_predeclared_test=bool(sufficient and adjusted<plan['alpha'] and interval[0]>0),
        note='Conditional random selection within same-session matched price-pivot opportunities; identical gate, direction, time, volatility, risk and holding duration. Independent event outcomes, potentially overlapping; no portfolio claim.')


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--db',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--plan',type=Path,default=Path(__file__).with_name('cvd_study_plan.json'));parser.add_argument('--phase',choices=['development','final'],default='development');parser.add_argument('--unlock-final',action='store_true')
    args=parser.parse_args();plan=json.loads(args.plan.read_text());gate=provenance_config()
    if len(plan['variants'])*len(plan['timeframes'])!=plan['family_size']:raise ValueError('Family size must count every variant/timeframe')
    if args.phase=='final' and not args.unlock_final:raise ValueError('Final is single-use. Supply --unlock-final only after development is frozen.')
    args.output.mkdir(parents=True,exist_ok=True)
    # Central ledger stays with the study, not an arbitrary report directory.
    ledger_dir=Path(__file__).parent/'cvd_study_registry';ledger_dir.mkdir(exist_ok=True)
    fingerprint=digest(dict(plan=plan,gate=gate,code=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),recorder_code=hashlib.sha256((ROOT/'ict_smc_app/backend/app/delta/recorder.py').read_bytes()).hexdigest()))
    run_id=datetime.datetime.now(datetime.timezone.utc).isoformat()
    record=dict(run_id=run_id,study_id=plan['study_id'],phase=args.phase,fingerprint=fingerprint,plan=plan,gate=gate)
    registration=ledger_dir/(fingerprint+'.json')
    if not registration.exists():
        with registration.open('x') as f:json.dump(record,f,indent=2)
    append_ledger(ledger_dir/'trials.jsonl',dict(record,status='started'))
    try:
        marker=ledger_dir/(hashlib.sha256(plan['study_id'].encode()).hexdigest()+'.final')
        if args.phase=='final' and marker.exists():
            raise ValueError('Final evaluation for this study was already consumed; holdout not read')
        with sqlite3.connect('file:'+str(args.db.resolve())+'?mode=ro',uri=True) as db:
            sessions,rejections=eligible_sessions(db,args.phase,plan,gate)
        result=dict(fingerprint=fingerprint,input_digest=digest(sessions),phase=args.phase,eligible_sessions=list(sessions),gate_rejections=rejections,results=[])
        if args.phase=='final' and len(sessions)<plan['holdout_sessions']:
            result['status']='awaiting_verified_holdout'
        else:
            if args.phase=='final':
                # Across plan edits, the study id still has only one final release.
                marker=ledger_dir/(hashlib.sha256(plan['study_id'].encode()).hexdigest()+'.final')
                with marker.open('x') as f:f.write(fingerprint)
            for tf in plan['timeframes']:
                events=[e for rows in sessions.values() for e in opportunities(rows,tf,plan)]
                for v in plan['variants']:
                    seed=int(hashlib.sha256(f'{plan["seed"]}:{tf}:{v}'.encode()).hexdigest()[:16],16)
                    result['results'].append(dict(timeframe=tf,variant=v,**evaluate(events,v,plan,random.Random(seed),list(sessions))))
            result['status']='evaluated' if len(sessions)>=plan['minimum_sessions'] else 'insufficient_verified_sessions'
        (args.output/'controlled_replay.json').write_text(json.dumps(result,indent=2))
        append_ledger(ledger_dir/'trials.jsonl',dict(run_id=run_id,fingerprint=fingerprint,status=result['status'],eligible_sessions=len(sessions)))
        print(json.dumps(result,indent=2))
        print('Registration:',registration.resolve())
        print('Trial ledger:',(ledger_dir/'trials.jsonl').resolve())
        print('Report:',(args.output/'controlled_replay.json').resolve())
    except Exception as exc:
        append_ledger(ledger_dir/'trials.jsonl',dict(run_id=run_id,fingerprint=fingerprint,status='failed',error=type(exc).__name__))
        raise


if __name__=='__main__':main()
