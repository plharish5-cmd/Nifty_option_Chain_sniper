"""Shared offline/forward MATRIX index-paper simulator. No broker imports or calls.

Only existing Matrix Tier 3 episodes enter, after the signal candle closes.
Both OHLC orders are replayed. Different exit prices or exit-vs-open outcomes
terminate as AMBIGUOUS with bounds; no invented realized PnL is booked.
"""
import argparse
import csv
import hashlib
import json
import math
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

IST = ZoneInfo('Asia/Kolkata')
ROOT = Path(__file__).resolve().parents[1]
RULES = Path(__file__).with_name('matrix_scalp_rules.json')


def read_rules(path=RULES):
    r = json.loads(Path(path).read_text())
    for k in ('initial_stop_pts', 'activation_pts', 'step_pts'):
        if not math.isfinite(float(r[k])) or float(r[k]) <= 0:
            raise ValueError(k + ' must be positive and finite')
    if not 0 <= float(r['first_lock_pts']) < float(r['activation_pts']):
        raise ValueError('first lock must be below activation')
    if r['mode'] != 'REPLAY_ONLY_INDEX' or r['setup_name'] != 'MATRIX':
        raise ValueError('MATRIX is historical replay only')
    return r


def locked_points(peak, rules):
    if peak < rules['activation_pts']:
        return -float(rules['initial_stop_pts'])
    n = math.floor((peak - rules['activation_pts'] + 1e-9) / rules['step_pts'])
    return float(rules['first_lock_pts'] + n * rules['step_pts'])


def path_result(points, peak, stop, rules):
    """Linear path in favourable index-point coordinates, including adverse gaps."""
    if points[0] <= stop:
        return dict(exit=points[0], peak=peak, stop=stop)
    previous = points[0]
    for value in points:
        if value < previous and value <= stop:
            return dict(exit=stop, peak=peak, stop=stop)
        peak = max(peak, value)
        stop = max(stop, locked_points(peak, rules))
        previous = value
    return dict(exit=None, peak=peak, stop=stop)


def advance(position, bar, rules):
    p = dict(position)
    p['stop_trace'] = list(p.get('stop_trace', []))
    if bar['ts'] != p['last_ts'] + 60:
        return {**p, 'status': 'DATA_GAP', 'exit_ts': bar['ts'], 'points': None}
    sign = 1 if p['direction'] == 'CE' else -1
    convert = lambda price: sign * (float(price) - p['entry'])
    a = path_result([convert(bar[k]) for k in ('o','h','l','c')], p['peak'], p['stop_points'], rules)
    b = path_result([convert(bar[k]) for k in ('o','l','h','c')], p['peak'], p['stop_points'], rules)
    p.update(last_ts=bar['ts'], current=float(bar['c']), peak=max(a['peak'],b['peak']))
    exits = [a['exit'], b['exit']]
    if exits[0] != exits[1]:
        # An open path can have further unknown outcomes: don't pretend finite bounds.
        return {**p, 'status':'AMBIGUOUS', 'points':None, 'exit_ts':bar['ts']+60,
                'path_exit_a':exits[0], 'path_exit_b':exits[1],
                'exit':None, 'stop_points':max(a['stop'],b['stop'])}
    p['stop_points'] = max(a['stop'], b['stop'])
    if p['stop_points'] != position['stop_points']:
        p['stop_trace'].append(dict(ts=bar['ts']+60, lock_points=p['stop_points'],
            stop_spot=round(p['entry']+sign*p['stop_points'],4),
            note='One-minute OHLC trail update; not a tick timestamp'))
    if exits[0] is not None:
        pts = round(exits[0], 4)
        p.update(status='STOP', points=pts, exit=p['entry']+sign*pts, exit_ts=bar['ts']+60)
    elif datetime.fromtimestamp(bar['ts']+60, IST).strftime('%H:%M') >= rules['square_off']:
        p.update(status='EOD', points=round(convert(bar['c']),4), exit=float(bar['c']), exit_ts=bar['ts']+60)
    return p


def simulate(day, bars, matrix_rows, rules, now=None, allow_entries=True):
    """One non-overlapping position. Adjacent Tier 3 rows aren't separate entries."""
    signals = {int(r['ts']):r for r in matrix_rows}
    trades = []
    active = None
    pending = None
    previous_key = None
    for bar in sorted(bars, key=lambda b:b['ts']):
        ts = int(bar['ts'])
        hhmm = datetime.fromtimestamp(ts, IST).strftime('%H:%M')
        if hhmm < rules['session_start'] or hhmm >= rules['square_off']:
            continue
        exited = False
        if active:
            active = advance(active, bar, rules)
            if active['status'] != 'OPEN':
                trades.append(active); active = None; exited = True
        if pending and not active and not exited:
            if ts == pending['ts'] + 60 and allow_entries:
                side = 'CE' if pending['ema_4'] >= pending['ema_8'] else 'PE'
                active = dict(id=f'MATRIX:{day}:{ts}', setup='MATRIX', mode='REPLAY_ONLY_INDEX', day=day,
                    direction=side, signal_ts=pending['ts']+60, entry_ts=ts, entry=float(bar['o']),
                    current=float(bar['o']), peak=0.0, stop_points=-float(rules['initial_stop_pts']),
                    last_ts=ts-60, points=None, status='OPEN', signal=pending.get('action_recommendation',''))
                active = advance(active, bar, rules)
                if active['status'] != 'OPEN':
                    trades.append(active); active=None; exited=True
            pending = None
        r = signals.get(ts)
        side = ('CE' if r['ema_4'] >= r['ema_8'] else 'PE') if r else None
        key = (r.get('proximity_state'), side) if r else None
        if r and key[0] == rules['entry_tier'] and key != previous_key and not active and not exited:
            pending = r
        previous_key = key
    if active:
        if now is not None and now - (active['last_ts']+60) > rules['freshness_seconds']:
            active['status'] = 'STALE_UNRESOLVED'
        trades.append(active)
    return trades


def load_market(market, recent=False):
    import pandas as pd
    with sqlite3.connect('file:'+str(market)+'?mode=ro', uri=True) as c:
        query = "SELECT ts,o,h,l,c,v FROM candles_1m WHERE sec_id='13'"
        if recent:
            query += " AND ts >= (SELECT max(ts)-604800 FROM candles_1m WHERE sec_id='13')"
        df = pd.read_sql_query(query + ' ORDER BY ts', c)
    df['day'] = pd.to_datetime(df.ts, unit='s', utc=True).dt.tz_convert(IST).dt.strftime('%Y-%m-%d')
    return df


def matrix_for_day(engine, df, day, radar_cfg=None):
    """Include previous sessions so higher-timeframe initial fallback isn't future data.
    The first available session lacks an anchor and is explicitly skipped.
    """
    days = sorted(df.day.unique())
    i = days.index(day)
    if i == 0:
        return [], 'NO_PRIOR_SESSION_WARMUP'
    source = df[df.day.isin(days[max(0,i-2):i+1])].copy()
    source = source.rename(columns={'o':'open','h':'high','l':'low','c':'close','v':'volume'})
    result = engine.compute_matrix(candles_input=source, date_str=day, timeframe='1m', custom_cfg=radar_cfg)
    return result.get('rows', []), 'RECONSTRUCTED_EXISTING_MATRIX'


def summary(rows):
    resolved = [r for r in rows if r.get('points') is not None]
    return dict(trades=len(rows), resolved=len(resolved), ambiguous=sum(r['status']=='AMBIGUOUS' for r in rows),
                gaps=sum(r['status']=='DATA_GAP' for r in rows),
                unresolved=sum(r['status'] in ('OPEN','STALE_UNRESOLVED') for r in rows),
                wins=sum(r['points']>0 for r in resolved),
                index_points=round(sum(r['points'] for r in resolved),4),
                win_rate=round(100*sum(r['points']>0 for r in resolved)/len(resolved),2) if resolved else None)


def replay_date(market, config, day):
    """Read-only archived-date replay, never inserts into any paper/live ledger."""
    from analyst.ema_proximity_engine import EmaProximityEngine
    datetime.strptime(day, '%Y-%m-%d')
    rules=read_rules()
    df=load_market(market)
    if day not in set(df.day):
        return dict(setup='MATRIX',rows=[],summary=summary([]),source='NO_RECORDED_CANDLES',rules=rules)
    engine=EmaProximityEngine(config_path=Path(config),db_path=Path(market))
    rows,source=matrix_for_day(engine,df,day)
    trades=simulate(day,df[df.day==day].to_dict('records'),rows,rules)
    return dict(setup='MATRIX',rows=trades,summary=summary(trades),source=source,rules=rules,
                units='NIFTY index points, not option profit; OHLC conflicts unresolved')


def run_backtest(market, config, output):
    from analyst.ema_proximity_engine import EmaProximityEngine
    rules=read_rules(); df=load_market(market)
    engine=EmaProximityEngine(config_path=Path(config),db_path=Path(market))
    trades=[]; daily=[]
    for day in sorted(df.day.unique()):
        rows,source=matrix_for_day(engine,df,day)
        result=simulate(day,df[df.day==day].to_dict('records'),rows,rules)
        trades.extend(result); daily.append(dict(day=day,source=source,**summary(result)))
    out=Path(output);out.mkdir(parents=True,exist_ok=True)
    report=dict(rules=rules,summary=summary(trades),daily=daily,rows=trades,
        methodology='Reconstructed existing 1m Matrix Tier 3 episodes. Next-minute open entry; one position; no re-entry until a fresh episode. Previous sessions warm anchors. Both OHLC paths tested; conflicting exits unresolved. No option PnL, costs, slippage or edge claim. Frozen configured rule; no tuning. Partial sessions and missing bars explicitly unresolved.',
        config_sha256=hashlib.sha256(Path(config).read_bytes()).hexdigest())
    (out/'matrix_backtest.json').write_text(json.dumps(report,indent=2))
    for name,data in [('matrix_trades',trades),('matrix_daily',daily)]:
        with (out/(name+'.csv')).open('w',newline='') as f:
            fields=sorted(set().union(*(r.keys() for r in data))) if data else ['day']
            w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(data)
    print(json.dumps(report['summary']))
    return report


if __name__ == '__main__':
    p=argparse.ArgumentParser();p.add_argument('--market',required=True);p.add_argument('--config',required=True);p.add_argument('--output',required=True)
    a=p.parse_args();run_backtest(a.market,a.config,a.output)
