"""Append-only price provenance and reversible, test-only ledger visibility."""
import json
import math
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

IST=ZoneInfo('Asia/Kolkata')
CONFIG=Path(__file__).with_suffix('.json')


def rules():return json.loads(CONFIG.read_text())


def positive(value):
    try:
        x=float(value)
        return x if x>0 and math.isfinite(x) else None
    except (ValueError,TypeError):return None


def category(mode,stamp):
    # Mixed historic rows may contain a real leg: never call them disposable tests.
    if mode in ('BOTH','ALGO_LIVE_ONLY','LIVE'):return 'REAL_OR_MIXED'
    if mode!='PAPER_ONLY':return 'LEGACY_UNVERIFIED'
    try:
        dt=datetime.fromisoformat(str(stamp).replace(' IST',''))
        if dt.tzinfo:dt=dt.astimezone(IST)
    except (ValueError,TypeError):return 'LEGACY_UNVERIFIED'
    cfg=rules();hhmm=dt.strftime('%H:%M')
    if dt.weekday()<5 and cfg['protected_session_start']<=hhmm<=cfg['protected_session_end']:
        return 'MARKET_SESSION'
    return 'OFF_MARKET_TEST'


def install(cur):
    cur.execute('''CREATE TABLE IF NOT EXISTS execution_provenance(
        entity TEXT,id TEXT,category TEXT,parent_position TEXT,payload TEXT,
        hidden INTEGER DEFAULT 0,PRIMARY KEY(entity,id))''')
    # No cleanup endpoint is allowed to erase either table. Tests are soft-hidden.
    for table in ('orders','positions'):
        cur.execute(f'''CREATE TRIGGER IF NOT EXISTS protect_{table}_delete
            BEFORE DELETE ON {table} BEGIN
            SELECT RAISE(ABORT,'Execution history cannot be deleted; hide off-market paper tests instead'); END''')
        key='order_id' if table=='orders' else 'position_id'
        guard=" AND order_status IN ('FILLED','TRADED')" if table=='orders' else ''
        cur.execute(f'''CREATE TRIGGER IF NOT EXISTS protect_{table}_replace
            BEFORE INSERT ON {table} WHEN EXISTS(SELECT 1 FROM {table} WHERE {key}=NEW.{key}{guard})
            BEGIN SELECT RAISE(ABORT,'Existing execution cannot be replaced'); END''')
    for table,fields,closed in [('orders',['fill_price','qty','created_at','symbol','security_id','mode'],"OLD.order_status IN ('FILLED','TRADED')"),
            ('positions',['entry_price','entry_time','qty','symbol','security_id','mode'],"1"),
            ('positions',['exit_price','exit_time','net_pnl','gross_pnl','exit_reason','status'],"OLD.status='CLOSED'")]:
        suffix='entry' if table=='positions' and closed=='1' else 'settled'
        changed=' OR '.join(f'NEW.{f} IS NOT OLD.{f}' for f in fields)
        cur.execute(f'''CREATE TRIGGER IF NOT EXISTS protect_{table}_{suffix}
            BEFORE UPDATE OF {','.join(fields)} ON {table} WHEN ({closed}) AND ({changed})
            AND NOT EXISTS(SELECT 1 FROM execution_provenance WHERE entity='{table}' AND id=OLD.{ 'order_id' if table=='orders' else 'position_id'} AND category='OFF_MARKET_TEST')
            BEGIN SELECT RAISE(ABORT,'Recorded market/live execution fields are immutable'); END''')


def record(cur,entity,key,mode,stamp,*,parent=None,entry_spot=None,entry_premium=None,
           exit_spot=None,exit_premium=None,price_basis='UNVERIFIED_LEGACY',broker_order_id=None,is_algo=None):
    parent_row=cur.execute("SELECT category,payload FROM execution_provenance WHERE entity='positions' AND id=?",(parent,)).fetchone() if parent else None
    legacy_parent=cur.execute('SELECT mode,entry_time FROM positions WHERE position_id=?',(parent,)).fetchone() if parent and not parent_row else None
    cat=parent_row[0] if parent_row else category(legacy_parent[0],legacy_parent[1]) if legacy_parent else category(mode,stamp)
    payload=json.loads(parent_row[1]) if parent_row else {}
    values=dict(entry_spot=entry_spot,entry_premium=entry_premium,exit_spot=exit_spot,exit_premium=exit_premium)
    for name,val in values.items():
        if positive(val) is not None:payload[name]=float(val)
    payload.update(price_basis=price_basis,broker_order_id=broker_order_id)
    if type(is_algo) is bool:payload['is_algo']=is_algo
    if price_basis=='BROKER_FILL_UNCONFIRMED':payload['entry_premium']=None
    cur.execute('INSERT OR IGNORE INTO execution_provenance(entity,id,category,parent_position,payload) VALUES(?,?,?,?,?)',
                (entity,str(key),cat,parent,json.dumps(payload)))


def stamp_exit(cur,position_id,exit_spot,exit_premium):
    row=cur.execute("SELECT payload FROM execution_provenance WHERE entity='positions' AND id=?",(position_id,)).fetchone()
    if not row:return
    d=json.loads(row[0])
    # First exit observation is frozen; don't replace it with a later quote.
    if 'exit_captured' in d:return
    d.update(exit_spot=positive(exit_spot),exit_premium=positive(exit_premium),exit_captured=True)
    cur.execute("UPDATE execution_provenance SET payload=? WHERE entity='positions' AND id=?",(json.dumps(d),position_id))


def algo_session(row,entry_time):
    """Presentation eligibility only; never changes execution or risk management."""
    origin=' '.join(str(row.get(k) or '') for k in ('source','placed_by')).upper()
    setup=' '.join(str(row.get(k) or '') for k in ('strategy_name','strategy_setup')).upper()
    words=re.sub('[^A-Z0-9]+',' ',origin+' '+setup)
    if re.search(r'\b(MANUAL|TEST\d*|TESTING|REPLAY|BACKTEST|BACKTESTING|BENCHMARK|CALIBRATED|SYNTHETIC)\b',words):return False
    if not ('ALGO' in origin or 'AUTO' in origin):return False
    return category('PAPER_ONLY',entry_time)=='MARKET_SESSION' and not row.get('test_hidden',False)


def enrich(cur,entity,row):
    key=row['order_id' if entity=='orders' else 'position_id']
    meta=cur.execute('SELECT category,hidden,payload,parent_position FROM execution_provenance WHERE entity=? AND id=?',(entity,key)).fetchone()
    if meta:
        cat,hidden,payload,parent=meta;detail=json.loads(payload)
    else:
        cat=category(row.get('mode'),row.get('entry_time') if entity=='positions' else row.get('created_at'))
        hidden=0;detail={};parent=row.get('position_id')
        if entity=='orders' and not parent and str(row.get('correlation_id') or '').startswith('POS_'):
            parent=row['correlation_id']
        # A late SELL could close a protected market entry. Untagged exits aren't disposable.
        if entity=='orders' and row.get('side')=='SELL' and cat=='OFF_MARKET_TEST':cat='LEGACY_UNVERIFIED'
    row.update(record_category=cat,test_hidden=bool(hidden),price_provenance=detail,
               recorded_entry_spot=detail.get('entry_spot'),recorded_exit_spot=detail.get('exit_spot'),
               observed_entry_premium=detail.get('entry_premium'),observed_exit_premium=detail.get('exit_premium'),
               price_basis=detail.get('price_basis','UNVERIFIED_LEGACY'))
    row['is_real_order']=bool(row.get('dhan_order_id') or detail.get('broker_order_id'))
    if parent:
        parent_meta=cur.execute("SELECT payload FROM execution_provenance WHERE entity='positions' AND id=?",(parent,)).fetchone()
        if parent_meta:
            d=json.loads(parent_meta[0])
            row['recorded_exit_spot']=d.get('exit_spot')
            row['observed_exit_premium']=d.get('exit_premium') if not row['is_real_order'] else None
        p=cur.execute('SELECT entry_price,exit_price,entry_time,exit_time,duration,net_pnl,status FROM positions WHERE position_id=?',(parent,)).fetchone()
        if p:row.update(position_entry_price=p[0],position_exit_price=p[1],position_entry_time=p[2],position_exit_time=p[3],position_duration=p[4],position_net_pnl=p[5],position_status=p[6])
    entry_time=row.get('entry_time') if entity=='positions' else row.get('position_entry_time') or (row.get('created_at') if row.get('side')=='BUY' else None)
    row['is_algo_session']=algo_session(row,entry_time) and detail.get('is_algo') is not False
    row['execution_book']='REAL' if row['is_real_order'] else 'PAPER' if row.get('mode')=='PAPER_ONLY' or row['price_basis'].startswith('OBSERVED_QUOTE_PAPER') else 'UNVERIFIED'
    return row


def hide_tests(path):
    """Reversible cleanup, preserving open tests and every live/session/unknown row."""
    with sqlite3.connect(path) as c:
        c.row_factory=sqlite3.Row;cur=c.cursor();install(cur)
        hidden_orders=hidden_positions=0
        # Backfill labels only for provable paper-only off-session history.
        # No price is invented and no source execution field is changed.
        for raw in cur.execute("SELECT * FROM positions WHERE mode='PAPER_ONLY' AND status='CLOSED'").fetchall():
            p=dict(raw)
            if category(p['mode'],p['entry_time'])=='OFF_MARKET_TEST' and not p.get('sl_order_id') and not p.get('target_order_id'):
                record(cur,'positions',p['position_id'],p['mode'],p['entry_time'],price_basis='LEGACY_TEST_PRICE_UNVERIFIED')
        for raw in cur.execute("SELECT * FROM orders WHERE mode='PAPER_ONLY' AND (dhan_order_id IS NULL OR dhan_order_id='')").fetchall():
            o=dict(raw);parent=o.get('position_id')
            if not parent and str(o.get('correlation_id','')).startswith('POS_'):parent=o['correlation_id']
            if o['side']=='BUY' and category(o['mode'],o['created_at'])=='OFF_MARKET_TEST':
                if not cur.execute("SELECT 1 FROM positions WHERE symbol=? AND status='OPEN'",(o['symbol'],)).fetchone():
                    record(cur,'orders',o['order_id'],o['mode'],o['created_at'],parent=parent,price_basis='LEGACY_TEST_PRICE_UNVERIFIED')
            elif o['side']=='SELL' and parent:
                meta=cur.execute("SELECT category FROM execution_provenance WHERE entity='positions' AND id=?",(parent,)).fetchone()
                if meta and meta[0]=='OFF_MARKET_TEST':record(cur,'orders',o['order_id'],o['mode'],o['created_at'],parent=parent,price_basis='LEGACY_TEST_PRICE_UNVERIFIED')
        # New records carry provenance. Legacy records are intentionally not auto-deleted.
        for row in cur.execute("SELECT entity,id,parent_position FROM execution_provenance WHERE category='OFF_MARKET_TEST' AND hidden=0").fetchall():
            entity,key,parent=row
            if entity=='positions':
                p=cur.execute("SELECT status,mode FROM positions WHERE position_id=?",(key,)).fetchone()
                if not p or p['status']=='OPEN' or p['mode']!='PAPER_ONLY':continue
            else:
                o=cur.execute('SELECT mode,dhan_order_id,order_status FROM orders WHERE order_id=?',(key,)).fetchone()
                if not o or o['mode']!='PAPER_ONLY' or o['dhan_order_id'] or o['order_status'] in ('PENDING','PART_TRADED','TRANSIT'):continue
                if parent and cur.execute("SELECT 1 FROM positions WHERE position_id=? AND status='OPEN'",(parent,)).fetchone():continue
            cur.execute('UPDATE execution_provenance SET hidden=1 WHERE entity=? AND id=?',(entity,key))
            hidden_orders+=entity=='orders';hidden_positions+=entity=='positions'
    return dict(ok=True,hidden_orders=hidden_orders,hidden_positions=hidden_positions,
                message='Off-market paper tests hidden reversibly. Open, live, session and unclassified records preserved.')


def restore_tests(path):
    with sqlite3.connect(path) as c:
        cur=c.execute("UPDATE execution_provenance SET hidden=0 WHERE category='OFF_MARKET_TEST' AND hidden=1")
        return dict(ok=True,restored=cur.rowcount)
