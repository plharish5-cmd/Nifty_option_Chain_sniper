"""Read-only gamma projection and pre-specified BOS+RVOL comparison.

Runs in the OC virtualenv. Root greeks and backend structure/volume engines are reused,
never rewritten. All prices are NIFTY spot; FUT is used for participation only.
"""
from __future__ import annotations

import argparse
from bisect import bisect_right
from datetime import date, datetime, time, timedelta
import json
import logging
import math
from pathlib import Path
import sqlite3
import sys
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(1, str(ROOT / 'ict_smc_app/backend'))
import greeks
from app import config as desk_config
from app.engine.primitives import Bars, atr, pivots
from app.engine.structure import structure
from app.engine.fvg import find_fvgs
from app.engine import volume, rvol
from app.market.candles import resample, session_filter
from app.timeutil import TF_SECONDS, bucket_start

IST = ZoneInfo('Asia/Kolkata')
LOG = logging.getLogger('gamma_review')


def policy():
    return json.loads((ROOT / 'analyst/analyst_config.json').read_text())['gamma_exposure']


def epoch(value):
    t = datetime.fromisoformat(str(value))
    return t.replace(tzinfo=IST).timestamp() if t.tzinfo is None else t.timestamp()


def iso_exp(value):
    text = str(value)
    if len(text) >= 10 and text[4] == '-' and text[7] == '-':
        return date.fromisoformat(text[:10]).isoformat()
    return pd.to_datetime(text, dayfirst=True).date().isoformat()


def day_bounds(day):
    d = datetime.fromisoformat(day).date()
    return int(datetime.combine(d, time(9, 15), IST).timestamp()), int(datetime.combine(d, time(15, 30), IST).timestamp())


def archive_days():
    return sorted({p.name for base in ('snapshots', 'snapshots_multi') for p in (ROOT / 'data' / base).glob('????-??-??') if p.is_dir()})


def records(day):
    # Prefer the richer single-expiry archive; add multi-expiry records if no duplicate.
    found, skipped = {}, {}
    for source in ('snapshots', 'snapshots_multi'):
        for p in sorted((ROOT / 'data' / source / day).glob('*.meta.json')):
            try:
                m = json.loads(p.read_text())
                stamp = epoch(m['timestamp'])
                exp = iso_exp(m['expiry'])
                if (stamp, exp) in found:
                    continue
                file = p.with_suffix('').with_suffix('.parquet')
                if file.exists():
                    found[stamp, exp] = dict(ts=stamp, expiry=exp, spot=float(m['spot']), file=file, source=source)
            except (ValueError, KeyError, OSError, TypeError) as e:
                reason = type(e).__name__ + ': ' + str(e)
                skipped[reason] = skipped.get(reason, 0) + 1
    if skipped:
        LOG.warning('Snapshot metadata excluded for %s: %s', day, skipped)
    return sorted(found.values(), key=lambda s: s['ts'])


def cumulative_cross(strikes):
    crossings, run = [], 0.0
    prev = None
    for s in sorted(strikes, key=lambda s: s['strike']):
        run += s['net']
        if prev and prev[1] * run < 0:
            crossings.append(prev[0] + (s['strike'] - prev[0]) * (-prev[1]) / (run - prev[1]))
        prev = (s['strike'], run)
    return crossings


def exposure(frame, spot, cfg, sign=1):
    """Normalized exposure in rupees per 1% underlying move, contracts × lot exactly once."""
    near = frame[(frame.strike - spot).abs() <= cfg['calculation_band_points']].copy()
    if near.empty or near.strike.duplicated().any():
        return {'ok': False, 'message': 'Missing or duplicate strike inputs'}
    cols = ['ce_oi', 'pe_oi', 'ce_gamma', 'pe_gamma']
    numbers = near[cols].apply(pd.to_numeric, errors='coerce')
    good = np.isfinite(numbers).all(axis=1) & (numbers >= 0).all(axis=1)
    valid = near[good]
    coverage = len(valid) / len(near)
    if len(valid) < cfg['minimum_strikes'] or coverage < cfg['minimum_coverage']:
        return {'ok': False, 'message': 'Insufficient valid OI/Greek coverage', 'coverage': coverage, 'valid_strikes': len(valid), 'total_strikes': len(near)}
    multiplier = cfg['lot_size'] if cfg['oi_unit'] == 'contracts' else 1
    unit = spot * spot * .01 * multiplier
    out = []
    for _, r in valid.sort_values('strike').iterrows():
        ce, pe = float(r.ce_gamma * r.ce_oi * unit), float(r.pe_gamma * r.pe_oi * unit)
        out.append({'strike': float(r.strike), 'call': ce, 'put': pe, 'net': sign * (ce - pe), 'gross': ce + pe})
    net, gross = sum(r['net'] for r in out), sum(r['gross'] for r in out)
    if gross <= 0:
        return {'ok': False, 'message': 'Zero gross exposure; regime unavailable'}
    balance = net / gross
    regime = 'neutral' if abs(balance) <= cfg['neutral_net_to_gross'] else 'negative' if balance < 0 else 'positive'
    crosses = cumulative_cross(out)
    return {'ok': True, 'strikes': out, 'net': net, 'gross': gross, 'balance': balance, 'regime': regime,
            'coverage': coverage, 'valid_strikes': len(valid), 'total_strikes': len(near),
            'call_wall': max(out, key=lambda r: r['call'])['strike'], 'put_wall': max(out, key=lambda r: r['put'])['strike'],
            'concentration': max(out, key=lambda r: r['gross'])['strike'],
            'strike_cross': min(crosses, key=lambda k: abs(k - spot)) if crosses else None}


def repriced_flip(frame, spot, expiry, captured, cfg, sign=1):
    """True net-zero search across hypothetical spot prices; IV/OI held fixed (model, not a forecast)."""
    n = frame[(frame.strike - spot).abs() <= cfg['calculation_band_points']].copy()
    need = ['iv_used', 'ce_oi', 'pe_oi', 'strike']
    if not set(need).issubset(n):
        return None
    a = n[need].apply(pd.to_numeric, errors='coerce')
    a = a[np.isfinite(a).all(axis=1) & (a.iv_used > 0) & (a.ce_oi >= 0) & (a.pe_oi >= 0)]
    if len(a) < cfg['minimum_strikes'] or len(a) / max(len(n), 1) < cfg['minimum_coverage']:
        return None
    t = greeks.time_to_expiry(expiry, captured)
    if t <= 0:
        return None
    sig = a.iv_used.to_numpy() / 100
    strikes = a.strike.to_numpy()
    diff = (a.ce_oi - a.pe_oi).to_numpy()
    grid = np.linspace(spot * (1 - cfg['reprice_band_fraction']), spot * (1 + cfg['reprice_band_fraction']), cfg['reprice_grid_points'])
    values = []
    for s in grid:
        d1 = (np.log(s / strikes) + (greeks.RISK_FREE - greeks.DIVIDEND_YIELD + .5 * sig ** 2) * t) / (sig * math.sqrt(t))
        g = math.exp(-greeks.DIVIDEND_YIELD * t) * np.exp(-.5 * d1 ** 2) / (math.sqrt(2 * math.pi) * s * sig * math.sqrt(t))
        values.append(sign * float(np.sum(g * diff)) * s * s * .01)
    found = [float(grid[i]) for i, v in enumerate(values) if v == 0]
    for i in range(1, len(grid)):
        if values[i - 1] * values[i] < 0:
            found.append(float(grid[i - 1] + (grid[i] - grid[i - 1]) * -values[i - 1] / (values[i] - values[i - 1])))
    return min(found, key=lambda k: abs(k - spot)) if found else None


def profile(record, cfg, sign=1, details=False):
    try:
        d = pd.read_parquet(record['file'])
        if 'expiry' in d:
            d = d[d.expiry.map(iso_exp) == record['expiry']].copy()
        exp = datetime.fromisoformat(record['expiry']).replace(hour=15, minute=30)
        captured = datetime.fromtimestamp(record['ts'], IST).replace(tzinfo=None)
        if exp <= captured:
            return dict(ok=False, message='Expiry has elapsed', ts=record['ts'], expiry=record['expiry'])
        # Reuse stored captured Greeks; reconstruct absent Greeks with recorded time, never wall-clock now.
        stored = ({'ce_gamma', 'pe_gamma', 'underlying_source'}.issubset(d)
                  and d[['ce_gamma', 'pe_gamma']].notna().any().any()
                  and d['underlying_source'].eq('spot').all())
        if not stored:
            d = greeks.add_greeks(d, record['spot'], exp, now=captured)
        out = exposure(d, record['spot'], cfg, sign)
        out.update(ts=record['ts'], spot=record['spot'], expiry=record['expiry'], greek_source='stored at snapshot' if stored else 'reconstructed at recorded timestamp')
        if details and out['ok']:
            out['model_flip'] = repriced_flip(d, record['spot'], exp, captured, cfg, sign)
        return out
    except (OSError, ValueError, KeyError, TypeError) as e:
        LOG.warning('Cannot project snapshot %s: %s', record['file'].name, e)
        return {'ok': False, 'message': str(e), 'ts': record['ts'], 'expiry': record['expiry']}


def candles(sec, start, end):
    path = ROOT / 'ict_smc_app/backend/data/ict_smc.sqlite3'
    with sqlite3.connect(f'file:{path}?mode=ro', uri=True) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute('SELECT ts,o,h,l,c,v FROM candles_1m WHERE sec_id=? AND ts>=? AND ts<? ORDER BY ts', (sec, start, end))]


def asof_profile(recs, cutoff, cfg, sign, cache):
    i = bisect_right([r['ts'] for r in recs], cutoff) - 1
    if i < 0:
        return None, 'No gamma snapshot existed at candle close'
    r = recs[i]
    age = cutoff - r['ts']
    if age > cfg['max_gamma_age_seconds']:
        return None, f'Gamma snapshot stale by {int(age)}s'
    key = r['file']
    if key not in cache:
        cache[key] = profile(r, cfg, sign)
    p = cache[key]
    if not p['ok']:
        return None, p.get('message', 'Invalid gamma coverage')
    return p, ''


def bos_context(day, recs, cfg, tf='5m', sign=1, cutoff=None, wall_context=True):
    """Same closed-bar BOS and RVOL engines; only prefix input is supplied to RVOL at each event."""
    o, close = day_bounds(day)
    stop = min(close, int(cutoff)) if cutoff is not None else close
    step = TF_SECONDS[tf]
    since = o - timedelta(days=int(rvol.cfg()['history_days'])).total_seconds()
    spot = session_filter(candles('13', since, stop))
    fut = session_filter(candles('FUT', since, stop))
    spot = [r for r in spot if r['ts'] + 60 <= stop]
    fut = [r for r in fut if r['ts'] + 60 <= stop]
    bars = [r for r in resample(spot, tf, now=stop) if r['closed']]
    E = desk_config.rules()['engine']
    # Fixed session-open warmup: later candles must not shift an earlier event's input window.
    prior = [r for r in bars if r['ts'] < o][-int(E['lookback_bars']):]
    bars = prior + [r for r in bars if r['ts'] >= o]
    if len(bars) < 10:
        return [], spot
    b = Bars.from_rows(bars)
    av = atr(b, int(E['atr_len']))
    sw = pivots(b, int(E['pivot_n'].get(tf, 2)))
    fv = find_fvgs(b, av, float(E['fvg_min_pts']), float(E['fvg_min_atr']))
    events, _ = structure(b, sw, av, {g.i:g.dir for g in fv}, float(E['displacement_body_atr']), float(E['displacement_body_ratio']))
    rc = rvol.cfg()
    fut_times = [r['ts'] for r in fut]
    fut_set = set(fut_times)
    spot_times = {r['ts'] for r in spot}
    cumulative = rvol.cumulative(fut, day) if fut else {}
    cum = {p['ts']: p for p in cumulative.get('points', [])}
    fut_days = {}
    for r in fut:
        key = datetime.fromtimestamp(r['ts'], IST).date().isoformat()
        opening, _ = day_bounds(key)
        fut_days.setdefault(key, set()).add((r['ts'] - opening) // 60)
    cache, out = {}, []
    for e in events:
        if e.kind != cfg['study']['event_kind'] or not o <= e.ts < stop:
            continue
        known = e.ts + step
        if known > stop:
            continue
        prefix = fut[:bisect_right(fut_times, known - 60)]
        slot = volume.rvol(prefix, tf).get(e.ts, {})  # full-series median floor must not see future bars
        rv = slot.get('rvol')
        point = cum.get(known - 60)
        crv = rvol.value(point) if point else None
        full_spot = all(t in spot_times for t in range(e.ts, known, 60))
        full_fut = all(t in fut_set for t in range(e.ts, known, 60))
        elapsed = (known - o) // 60
        history_days = sorted(d for d in fut_days if d < day)
        use_days = history_days[-20:] if point and point.get('c20') is not None else history_days[-5:]
        volume_coverage = [sum(m < elapsed for m in fut_days[d]) / max(elapsed, 1) for d in use_days + [day] if d in fut_days]
        full_cumulative = bool(volume_coverage) and all(v >= cfg['study']['min_volume_coverage'] for v in volume_coverage)
        gamma, reason = asof_profile(recs, known, cfg, sign, cache)
        fueled = rv is not None and crv is not None and rv >= rc['breakout_bar_rvol'] and crv >= rc['active']
        context = 'Gamma unavailable: ' + reason if not gamma else ('Modeled short-gamma context: expansion may amplify in either direction' if gamma['regime'] == 'negative' else 'Modeled long-gamma context: potential damping, not a veto' if gamma['regime'] == 'positive' else 'Near-balanced modeled gamma: no regime vote')
        reference, reference_note = asof_profile(recs, e.ts, cfg, sign, cache) if wall_context else (None, 'Not part of the tested filter')
        wall = reference['call_wall' if e.dir == 'bull' else 'put_wall'] if reference else None
        previous_close = float(b.c[e.i - 1]) if e.i > 0 else None
        wall_crossed = (previous_close <= wall < e.close if e.dir == 'bull' else previous_close >= wall > e.close) if wall is not None and previous_close is not None else None
        wall_note = (f" Prior-candle gamma wall {wall:,.1f}: {'new close across it' if wall_crossed else 'no new close across it'} (level known before this candle)." if wall is not None else ' Pre-candle gamma wall unavailable.') if wall_context else ''
        text = ('Participation present. ' if fueled else 'RVOL confirmation incomplete. ') + context + wall_note
        out.append(dict(bar_ts=e.ts, known_at=known, tf=tf, direction=e.dir, level=e.level, close=e.close,
                        displacement=e.displacement, atr=float(av[e.i]), slot_rvol=rv, cumulative_rvol=crv,
                        rvol_method=slot.get('method'), baseline=fueled,
                        big_activity=rv is not None and rv >= cfg['big_activity_rvol'], big_activity_note='Same RVOL input; not an independent vote or individual trade',
                        gamma_ts=gamma['ts'] if gamma else None, gamma_regime=gamma['regime'] if gamma else 'unavailable',
                        gamma_net=gamma['net'] if gamma else None, gamma_age_seconds=known-gamma['ts'] if gamma else None,
                        gamma_balance=gamma['balance'] if gamma else None,
                        prior_gamma_wall=wall, prior_gamma_wall_ts=reference['ts'] if reference else None,
                        gamma_wall_close_cross=bool(wall_crossed) if wall_crossed is not None else None,
                        eligible=full_spot and full_fut and full_cumulative and gamma is not None,
                        cumulative_coverage=min(volume_coverage) if volume_coverage else 0,
                        context=text, volume_source='Recorded FUT candle series; historical contract continuity not certified'))
    return out, spot


def view(day=None, expiry=None, tf='5m', end_ts=None, sign=1):
    cfg = policy()
    days = archive_days()
    day = day or (days[-1] if days else None)
    if not day or day not in days:
        return {'ok': False, 'message': 'No archived snapshots for the requested session', 'days': days}
    recs = records(day)
    exps = sorted({r['expiry'] for r in recs})
    expiry = expiry or (next((e for e in exps if e >= day), exps[-1] if exps else None))
    recs = [r for r in recs if r['expiry'] == expiry]
    if not recs:
        return {'ok': False, 'message': 'Selected expiry was not recorded in this session', 'days':days, 'expiries':exps}
    limit = end_ts if end_ts is not None else recs[-1]['ts']
    available = [r for r in recs if r['ts'] <= limit]
    if not available:
        return {'ok':False, 'message':'No snapshot at or before replay time', 'days':days, 'expiries':exps}
    # Intraday display sampled by bucket; selected latest snapshot is always included.
    samples = {}
    for r in available:
        samples[bucket_start(r['ts'], tf)] = r
    points = []
    for r in samples.values():
        p = profile(r, cfg, sign, details=True)
        points.append({k:v for k,v in p.items() if k != 'strikes'})
    latest = profile(available[-1], cfg, sign, details=True)
    contexts, _ = bos_context(day, recs, cfg, tf, sign, cutoff=limit)
    return dict(ok=True, day=day, days=days, expiry=expiry, expiries=exps, selected=latest,
                history=points, contexts=contexts, timeline=[r['ts'] for r in recs],
                end_ts=available[-1]['ts'], sign=sign, policy=cfg,
                method={'formula':'sign × (Call gamma × Call OI − Put gamma × Put OI) × lot × spot² × 0.01',
                        'unit':'Rupees per 1% underlying move; display in Cr', 'oi_unit':cfg['oi_unit'],
                        'scope':f"Selected expiry; ±{cfg['calculation_band_points']} points around recorded NIFTY",
                        'source':'Local archived option-chain snapshots; NIFTY spot; no StockMojo data or parity claim',
                        'sign_reconciliation':['Options Lab / local chart: + (CE−PE)', 'Institutional panel: − (CE−PE)', 'Briefing: different scaling and SHORT/LONG naming; not directly comparable'],
                        'limitations':['Dealer inventory is assumed, not observed.', 'Gross GEX is CE+PE exposure, not abs(net).', 'Model zero reprices spot with IV/OI fixed; cumulative strike cross is different.', 'RVOL and big-activity candles share the same input.', 'Stored snapshot capture time is used; original upstream publication latency is unknown.']})


def summarize(rows):
    if not rows:
        return dict(events=0, sessions=0, mean_points=None, positive_fraction=None)
    return dict(events=len(rows), sessions=len({r['day'] for r in rows}), mean_points=float(np.mean([r['points'] for r in rows])), positive_fraction=float(np.mean([r['points']>0 for r in rows])))


def study_days(spec, available):
    """The original split stays fixed; newly arriving days cannot enter a viewed holdout."""
    train, holdout = list(spec['development_days']), list(spec['holdout_days'])
    planned = train + holdout
    if not train or not holdout or planned != sorted(set(planned)):
        raise ValueError('Study dates must be unique, chronological, and nonempty')
    present = set(available)
    return [d for d in planned if d in present], train, holdout, [d for d in planned if d not in present]


def study():
    cfg = policy(); spec = cfg['study']
    days, train_days, holdout_days, missing_days = study_days(spec, archive_days())
    events, excluded = [], {}
    if missing_days:
        excluded['missing_planned_session'] = len(missing_days)
    for day in days:
        recs = records(day)
        exps = sorted({r['expiry'] for r in recs if r['expiry'] >= day})
        if not exps:
            excluded['no_expiry'] = excluded.get('no_expiry',0)+1; continue
        recs = [r for r in recs if r['expiry'] == exps[0]]
        rows, prices = bos_context(day, recs, cfg, spec['primary_tf'], sign=cfg['dealer_sign'], wall_context=False)
        # Future outcomes read separately; never supplied to entry-time features.
        o, cl = day_bounds(day)
        future = {r['ts']:r for r in candles('13',o,cl)}
        next_allowed = o
        for r in rows:
            if not r['baseline']:
                continue
            if not r['eligible']:
                excluded['quality_gate'] = excluded.get('quality_gate',0)+1; continue
            entry = r['known_at']; finish = entry + spec['forward_minutes']*60
            if entry < next_allowed:
                excluded['overlapping'] = excluded.get('overlapping',0)+1; continue
            if finish > cl or not all(t in future for t in range(entry,finish,60)):
                excluded['outcome_gap_or_close'] = excluded.get('outcome_gap_or_close',0)+1; continue
            points = (future[finish-60]['c'] - future[entry]['o']) * (1 if r['direction']=='bull' else -1)
            next_allowed = entry + spec['non_overlap_minutes']*60
            events.append(dict(day=day, known_at=entry, points=float(points), filtered=r['gamma_regime']=='negative', direction=r['direction']))
    def arm(rows):
        filt = [r for r in rows if r['filtered']]
        a,b = summarize(rows), summarize(filt)
        effect = b['mean_points'] - a['mean_points'] if rows and filt else None
        groups = {d:[r for r in rows if r['day']==d] for d in sorted({r['day'] for r in rows})}
        rng = np.random.default_rng(spec['random_seed']); differences=[]
        if len(groups)>1 and filt:
            dates=list(groups)
            for _ in range(spec['bootstrap_sessions']):
                sample=[r for d in rng.choice(dates,size=len(dates),replace=True) for r in groups[d]]
                chosen=[r for r in sample if r['filtered']]
                if chosen:
                    differences.append(float(np.mean([r['points'] for r in chosen])-np.mean([r['points'] for r in sample])))
        ci=[float(x) for x in np.quantile(differences,spec['interval_quantiles'])] if differences else None
        return dict(baseline=a, gamma_filtered=b, difference_points=effect, session_bootstrap_interval=ci)
    train=arm([r for r in events if r['day'] in train_days]); holdout=arm([r for r in events if r['day'] in holdout_days])
    sufficient=len({r['day'] for r in events})>=spec['min_sessions'] and holdout['gamma_filtered']['events']>=spec['min_filtered_events']
    return dict(ok=True, specification=spec, sessions_available=len(days), train_days=train_days, holdout_days=holdout_days,
                train=train, holdout=holdout, all=arm(events), excluded=excluded,
                verdict='Not validated for trading. Observational replay only; no rule promoted.',
                sufficient_sample=sufficient, event_count=len(events),
                caveats=['Index points, not option P&L or a tradable fill simulation; no costs or slippage.',
                         'One pre-specified variant. Original development/holdout dates are pinned; newer sessions do not alter this reported comparison.',
                         'Entire sessions resampled; intervals do not establish causality or independent confirmation.',
                         'Both arms share identical gamma/spot/volume availability gates and baseline event spacing.',
                         'FUT historical contract continuity and upstream snapshot latency remain unverified.',
                         'Structure warmup is fixed at session open; older untagged Greeks are reconstructed at recorded spot/time.',
                         'Matched random-pivot controls are not included in this baseline-only study.'])


def main():
    a=argparse.ArgumentParser(); a.add_argument('--day'); a.add_argument('--expiry'); a.add_argument('--tf',default='5m'); a.add_argument('--end-ts',type=float); a.add_argument('--sign',type=int,choices=[-1,1],default=1); a.add_argument('--study',action='store_true'); a.add_argument('--output')
    args=a.parse_args(); out=study() if args.study else view(args.day,args.expiry,args.tf,args.end_ts,args.sign)
    text=json.dumps(out, allow_nan=False)
    if args.output:
        Path(args.output).write_text(text)
    else:
        print(text)

if __name__=='__main__':
    main()
