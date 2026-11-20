import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock
from analyst.decision_audit_8510 import record, read, record_evaluation, lane_for


def backend(tmp_path):
    return SimpleNamespace(ledger_db=tmp_path / 'audit.sqlite3', scanner_cfg={'ema8_prototype': {'oi_filters': {'mode': 'block', 'switches': {'total_oi': True}}, 'api_secret': 'MUST_NOT_SAVE'}}, _real_money_enabled=False, kill_switch_active=False, directional_bias='BOTH')


def test_exact_gate_reason_and_settings_persist_without_trading_mutation(tmp_path):
    b = backend(tmp_path)
    original = json.dumps(b.scanner_cfg, sort_keys=True)
    b.log_ema8_prototype_event = Mock()
    state = {'direction': 'LONG', 'actions': [{'type': 'RETOUCH_SEEN'}], 'block_reason': 'NO TRADE — 8600 OI filters (total_oi)',
             'extra': {'oi_filters': {'mode': 'block', 'rule': 'any_agrees', 'reads': {'total_oi': {'switch_on': True, 'label': 'Cumulative Total OI', 'vote': 'OPPOSE', 'direction': 'PE', 'age_sec': 3}}}}}
    assert record_evaluation(b, 'prototype', state)
    rows = read(b.ledger_db)
    assert rows[0]['stage'] == 'TOUCH_BLOCKED'
    assert 'Cumulative Total OI: OPPOSE (PE, age 3s)' in rows[0]['reason']
    assert 'MUST_NOT_SAVE' not in rows[0]['context_json']
    assert json.dumps(b.scanner_cfg, sort_keys=True) == original
    b.log_ema8_prototype_event.assert_called_once()
    assert not record_evaluation(b, 'prototype', state)  # repeated state noise deduplicated


def test_log_only_is_explicit_and_all_lanes_have_dated_history(tmp_path):
    b = backend(tmp_path)
    for lane in ('prototype', 'reversal', 'flagship'):
        record_evaluation(b, lane, {'state': 'ARMED', 'block_reason': 'waiting for touch'})
    rows = read(b.ledger_db)
    assert {r['lane'] for r in rows} == {'prototype', 'reversal', 'flagship'}
    assert all(r['session_day'] in r['recorded_at'] for r in rows)
    assert len(read(b.ledger_db, lane='reversal')) == 1
    assert read(b.ledger_db, day='2000-01-01') == []
    assert lane_for('FLAGSHIP_LONG') == 'flagship'
    assert lane_for('H1_8EMA_SUPPORT') == 'flagship'


def test_rejected_real_attempt_with_paper_position_is_not_called_missing_trade(tmp_path):
    b = backend(tmp_path)
    sig = {'strategy_id': 'EMA8_PROTOTYPE_LONG', 'direction': 'LONG',
           'execution_result': {'ok': False, 'position_id': 'PAPER_123', 'rejection_reason': 'Invalid IP'}}
    record(b, sig, 'ORDER_REJECTED', 'DH-905: Invalid IP')
    row = read(b.ledger_db)[0]
    assert 'separate paper position recorded: PAPER_123' in row['reason']
    assert row['context']['candidate']['execution_result']['ok'] is False


def test_diagnostic_failure_never_raises_into_execution(tmp_path):
    b = backend(tmp_path)
    b.ledger_db = tmp_path / 'missing' / 'no.sqlite3'
    assert record(b, {'strategy_id': 'CROSSOVER_BULLISH'}, 'BLOCKED', 'lane filter') is False
