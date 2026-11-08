"""Scoped execution-state persistence; never reads credentials or alters host settings."""
import json
import sqlite3
from dataclasses import asdict,fields

def save(engine):
    p=engine.active_position
    data={'session':engine.current_session_date,'position':asdict(p)if p else None,'daily_trades':engine.daily_trades_count,'daily_pnl':engine.daily_pnl_inr,
      'closed_positions':engine.closed_positions,'rally':asdict(engine.evaluator.rally),'catcher_15m':asdict(engine.catcher_15m),'catcher_1h':asdict(engine.catcher_1h),
      'setup_stopped':engine.evaluator.stage2_stopped_today,'setup_stops':engine.evaluator.stage2_stops_count,'hour_stopped':engine.evaluator_1h.stage_stopped_today,'hour_trades':engine.evaluator_1h.daily_trades_count}
    with sqlite3.connect(engine.gateway.db_file())as c:
        c.execute('CREATE TABLE IF NOT EXISTS algo_execution_state(id TEXT PRIMARY KEY,data TEXT)')
        c.execute('INSERT OR REPLACE INTO algo_execution_state VALUES(?,?)',('v206',json.dumps(data)))

def restore(engine):
    from algo_execution_engine.strategy_setup1 import ActivePosition
    names={f.name for f in fields(ActivePosition)}
    with sqlite3.connect(engine.gateway.db_file())as c:
        c.row_factory=sqlite3.Row;c.execute('CREATE TABLE IF NOT EXISTS algo_execution_state(id TEXT PRIMARY KEY,data TEXT)')
        raw=c.execute("SELECT data FROM algo_execution_state WHERE id='v206'").fetchone()
        state=json.loads(raw[0])if raw else None
        if state:
            if state.get('position'):engine.active_position=ActivePosition(**{k:v for k,v in state['position'].items()if k in names})
            if state['session']==engine.current_session_date:
                engine.daily_trades_count=state['daily_trades'];engine.daily_pnl_inr=state['daily_pnl'];engine.closed_positions=state['closed_positions']
                for target,key in [(engine.evaluator.rally,'rally'),(engine.catcher_15m,'catcher_15m'),(engine.catcher_1h,'catcher_1h')]:
                    for k,v in state.get(key,{}).items():
                        if hasattr(target,k):setattr(target,k,v)
                engine.evaluator.stage2_stopped_today=state['setup_stopped'];engine.evaluator.stage2_stops_count=state.get('setup_stops',0);engine.evaluator_1h.stage_stopped_today=state['hour_stopped'];engine.evaluator_1h.daily_trades_count=state['hour_trades']
            return
        entries=c.execute('SELECT * FROM algo_setup1_sandbox_trades WHERE session_date=?',(engine.current_session_date,)).fetchall()
        engine.daily_trades_count=len(entries);engine.daily_pnl_inr=sum(float(x['net_pnl_inr']or 0)for x in entries if x['status']!='OPEN')
        engine.closed_positions=[dict(x)for x in entries if x['status']!='OPEN']
        open_rows=c.execute("SELECT * FROM algo_setup1_sandbox_trades WHERE status='OPEN'").fetchall()
        if len(open_rows)>1:
            engine.recovery_error='MULTIPLE_OPEN_PAPER_POSITIONS_REQUIRE_RECONCILIATION';return
        if open_rows:
            x=open_rows[0];meta=json.loads(x['confluence_snapshot']or '{}')
            if '_position' in meta:engine.active_position=ActivePosition(**{k:v for k,v in meta['_position'].items()if k in names})
            else:
                # Preserve exposure even when legacy logs did not store contract identity.
                side='PE'if x['trade_id'].startswith('1H')and x['target_spot']<x['spot_entry']else'CE'
                engine.active_position=ActivePosition(trade_id=x['trade_id'],symbol='RECOVERED_UNRESOLVED',security_id='',option_type=side,
                  strike=x['option_strike'],quantity=engine.gateway.dynamic_lot_size,entry_spot=x['spot_entry'],entry_option_price=x['option_entry_ltp'],
                  entry_time=x['entry_time'],entry_ts=x['entry_ts'],spot_sl=x['initial_sl_spot'],spot_target=x['target_spot'],option_sl=0.,option_target=0.,delta=x['delta_est'],status='RECOVERY_IDENTITY_UNVERIFIED')
                engine.recovery_error='LEGACY_POSITION_CONTRACT_IDENTITY_MISSING'
        for intent in engine.gateway.intents():
            if intent.get('status')in ('OPEN','OPEN_UNPROTECTED','EXIT_PENDING'):
                if engine.active_position:engine.recovery_error='MULTIPLE_EXECUTION_EXPOSURES_REQUIRE_RECONCILIATION';return
                engine.active_position=ActivePosition(**{k:v for k,v in intent['position'].items()if k in names})
