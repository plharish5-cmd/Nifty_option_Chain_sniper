"""Observability only. Records decisions already made; never evaluates a gate.

Reconstructed from ChatGPT's v254-gate-diagnostics working tree (lost before
commit; code recovered verbatim from the session transcript). This module only
RECORDS why each lane (Prototype / Reversal / 1H Flagship) took or skipped a
trade — the exact evaluator reason, the selected cockpit settings, and the OI
gate readings at decision time. It never evaluates a gate and never blocks a
trade: every failure path here is swallowed so diagnostics can never break
execution.
"""
import hashlib
import json
import logging
import math
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)
RULES = json.loads(Path(__file__).with_name('decision_audit_rules_8510.json').read_text())
IST = ZoneInfo('Asia/Kolkata')
SCHEMA = '''CREATE TABLE IF NOT EXISTS strategy_decision_audit (
 id INTEGER PRIMARY KEY, recorded_at TEXT NOT NULL, session_day TEXT NOT NULL,
 lane TEXT NOT NULL, strategy_id TEXT, side TEXT, stage TEXT, reason TEXT,
 context_json TEXT NOT NULL)'''


def lane_for(sid):
    sid = str(sid or '').upper()
    if sid.startswith('EMA8_PROTOTYPE'):
        return 'prototype'
    if sid.startswith('CROSSOVER'):
        return 'reversal'
    if 'FLAGSHIP' in sid or sid.startswith(('H1_', 'PA_1H')):
        return 'flagship'
    return None


def _safe(value):
    if isinstance(value, dict):
        return {str(k): _safe(v) for k, v in value.items()
                if not any(x in str(k).lower() for x in ('token', 'secret', 'password', 'credential', 'api_key'))}
    if isinstance(value, (list, tuple)):
        return [_safe(x) for x in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    return str(value)


def record(backend, sig, stage, reason, *, lane=None, state=None):
    """Return True when appended. All failures remain observational, not trading errors."""
    lane = lane or lane_for(sig.get('strategy_id'))
    if not lane:
        return False
    try:
        cfg = getattr(backend, 'scanner_cfg', {}) or {}
        settings = {k: v for k, v in cfg.items() if not isinstance(v, (dict, list))}
        for key in ('ema8_prototype', 'reversal', 'mj_sir', 'desk_scope_8510', 'oi_3m_feature', 'desk8600_board'):
            if key in cfg:
                settings[key] = cfg[key]
        row = state or {}
        context = _safe({'selected_settings': settings, 'evaluator_state': row,
                         'candidate': sig, 'desk_live_armed': bool(getattr(backend, '_real_money_enabled', False)),
                         'kill_switch_active': bool(getattr(backend, 'kill_switch_active', False)),
                         'directional_bias': getattr(backend, 'directional_bias', None),
                         'lane_lock': getattr(getattr(backend, '_ema_lane_lock', None), 'holder', None)})
        text = str(reason or 'No reason supplied by evaluator')
        extra = row.get('extra') or {}
        oi = extra.get('oi_filters') or sig.get('reversal_oi_telemetry') or {}
        if isinstance(oi, dict) and oi.get('reads'):
            votes = []
            for name, read in oi['reads'].items():
                if not isinstance(read, dict) or not read.get('switch_on'):
                    continue
                votes.append(str(read.get('label') or name) + ': ' + str(read.get('vote'))
                             + ' (' + str(read.get('direction') or 'no direction') + ', age ' + str(read.get('age_sec')) + 's)')
            text += ' | OI ' + str(oi.get('mode') or 'unknown mode') + '/' + str(oi.get('rule') or '') + ': ' + '; '.join(votes)
            if str(oi.get('mode')) == 'log_only':
                text += '; informational only — OI did not block'
        outcome = sig.get('execution_result') or {}
        if stage == 'ORDER_REJECTED' and outcome.get('position_id'):
            text += ' | Execution rejected; separate paper position recorded: ' + str(outcome['position_id'])
        signature = hashlib.sha256(json.dumps([lane, sig.get('direction'), stage, text, settings], sort_keys=True, default=str).encode()).hexdigest()
        last = backend.__dict__.setdefault('_decision_audit_seen', {})
        now = time.monotonic()
        if stage in ('STATE', 'TOUCH_BLOCKED') and now - last.get(signature, -1e20) < RULES['repeat_state_seconds']:
            return False
        at = datetime.now(IST)
        with sqlite3.connect(backend.ledger_db, timeout=RULES['db_timeout_seconds']) as c:
            c.execute(SCHEMA)
            c.execute('INSERT INTO strategy_decision_audit(recorded_at,session_day,lane,strategy_id,side,stage,reason,context_json) VALUES(?,?,?,?,?,?,?,?)',
                      (at.isoformat(), at.date().isoformat(), lane, str(sig.get('strategy_id') or ''),
                       str(sig.get('direction') or row.get('direction') or ''), stage, text,
                       json.dumps(context, default=str, allow_nan=False)))
        last[signature] = now
        legacy = getattr(backend, 'log_ema8_prototype_event', None)
        if lane == 'prototype' and stage in ('TOUCH_BLOCKED', 'BLOCKED', 'WAITING', 'ORDER_REJECTED', 'ORDER_ACCEPTED') and callable(legacy):
            legacy(event=stage, direction=sig.get('direction') or row.get('direction'),
                   reason=at.date().isoformat() + ' | ' + text)
        return True
    except Exception as exc:
        log.warning('Decision audit unavailable: %s', type(exc).__name__)
        return False


def record_evaluation(backend, lane, row, signals=()):
    row = row or {}
    side = row.get('direction') or row.get('reversal_side') or row.get('pre_arm_side')
    sid = {'prototype': 'EMA8_PROTOTYPE', 'reversal': 'CROSSOVER', 'flagship': 'FLAGSHIP_1H'}[lane]
    sig = {'strategy_id': sid, 'direction': side}
    touch = any(x.get('type') == 'RETOUCH_SEEN' for x in row.get('actions', []) if isinstance(x, dict))
    stage = 'CANDIDATE' if signals else 'TOUCH_BLOCKED' if touch else 'STATE'
    reason = (row.get('block_reason') or row.get('mj_no_trade') or row.get('diagnostic_reason')
              or row.get('phase') or row.get('state') or 'Evaluator supplied no setup reason')
    if signals:
        reason = 'Technical candidate emitted; waiting for downstream gates, contract and execution result'
    return record(backend, sig, stage, reason, lane=lane, state=row)


def read(db, day=None, lane=None, limit=None):
    limit = max(1, min(int(limit or RULES['default_read_limit']), RULES['max_read_limit']))
    day = day or datetime.now(IST).date().isoformat()
    if not Path(db).is_file():
        return []
    try:
        with sqlite3.connect(f'file:{db}?mode=ro', uri=True, timeout=1) as c:
            c.row_factory = sqlite3.Row
            where = 'session_day=?'
            args = [day]
            if lane:
                where += ' AND lane=?'
                args.append(lane)
            rows = c.execute('SELECT * FROM strategy_decision_audit WHERE ' + where + ' ORDER BY id DESC LIMIT ?', (*args, limit)).fetchall()
        return [dict(r) | {'context': json.loads(r['context_json'])} for r in rows]
    except sqlite3.Error:
        return []
