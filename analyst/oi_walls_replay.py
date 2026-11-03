"""Recorded OI wall inputs. No live fallbacks, no strategy calculations."""
import json
import logging
import math
from pathlib import Path
import sqlite3
import sys
import pandas as pd
from desk_time_machine import epoch, IST
from datetime import datetime, date
ROOT = Path(__file__).resolve().parents[1]

def expiry_iso(value):
    text=str(value)
    return date.fromisoformat(text[:10]).isoformat() if len(text)>=10 and text[4]=='-' and text[7]=='-' else pd.to_datetime(text,dayfirst=True).date().isoformat()

def replay(day, at, expiry=None):
    date.fromisoformat(day)
    policy = json.loads((ROOT/'analyst/analyst_config.json').read_text())['time_machine']
    start, end = epoch(day+'T'+policy['open']), epoch(day+'T'+policy['end'])
    if not start <= at <= end:
        raise ValueError('Target outside selected session')
    records = {}
    for source in ('snapshots', 'snapshots_multi'):
        for path in sorted((ROOT/'data'/source/day).glob('*.meta.json')):
            try:
                m=json.loads(path.read_text()); ts=epoch(m['timestamp'])
                exp=expiry_iso(m['expiry'])
                file=path.with_suffix('').with_suffix('.parquet')
                if start <= ts <= at and exp >= day and file.exists():
                    records.setdefault((ts,exp),dict(ts=ts,expiry=exp,file=file,spot=float(m['spot'])))
            except (OSError,ValueError,KeyError,TypeError) as exc:
                logging.warning('Excluded OI metadata %s: %s',path,exc)
    expiries=sorted({r['expiry'] for r in records.values()})
    if not expiries or (expiry and expiry not in expiries):
        return dict(ok=False,message='No recorded OI chain for this expiry at or before the target',expiries=expiries)
    expiry=expiry or expiries[0]
    rec=max((r for r in records.values() if r['expiry']==expiry),key=lambda r:r['ts'])
    frame=pd.read_parquet(rec['file'])
    if 'expiry' in frame:
        frame=frame[frame.expiry.map(expiry_iso)==expiry]
    required=['strike','ce_oi','pe_oi','ce_chg_oi','pe_chg_oi']
    if not set(required)<=set(frame.columns) or frame.empty:
        return dict(ok=False,message='Recorded chain lacks original daily OI-change fields',expiries=expiries)
    frame=frame[required]
    if not frame.map(lambda x: pd.notna(x) and math.isfinite(float(x))).all().all():
        return dict(ok=False,message='Recorded OI values incomplete; no zero substitution',expiries=expiries)
    db=ROOT/'ict_smc_app/backend/data/ict_smc.sqlite3'
    with sqlite3.connect(f'file:{db}?mode=ro',uri=True) as conn:
        conn.row_factory=sqlite3.Row
        candles=[dict(r) for r in conn.execute('SELECT ts,o,h,l,c FROM candles_1m WHERE sec_id=? AND ts>=? AND ts+60<=? ORDER BY ts',('13',start,at))]
    if not candles:
        return dict(ok=False,message='No closed NIFTY candle existed by this time',expiries=expiries)
    rows=[dict(strike=float(r.strike),ce=dict(oi=float(r.ce_oi),oi_chg=float(r.ce_chg_oi)),pe=dict(oi=float(r.pe_oi),oi_chg=float(r.pe_chg_oi))) for r in frame.itertuples()]
    return dict(ok=True,ts=rec['ts'],target=at,expiry=expiry,expiries=expiries,spot=rec['spot'],rows=rows,candles=candles,source=rec['file'].relative_to(ROOT).as_posix())

if __name__=='__main__':
    try:
        print(json.dumps(replay(sys.argv[1],int(sys.argv[2]),sys.argv[3] if len(sys.argv)>3 else None),allow_nan=False))
    except (ValueError,OSError,KeyError,sqlite3.Error) as exc:
        print(json.dumps(dict(ok=False,message=str(exc))))
