"""Display-only two-sided OI eligibility. Never evaluates/changes an entry.

Reversal uses the same nested `oi_filters` shape as the execution gate.
"""
from __future__ import annotations

import copy
import math
from typing import Any, Dict

from analyst.ema8_prototype_8510 import load_ema8_prototype_cfg
from analyst.proto_oi_filters_8510 import (
    PROTO_OI_READ_IDS, PROTO_OI_READ_LABELS, board_reads_from_board,
    load_oi_filters_cfg, oi_switch_block,
)
from analyst.reversal_revamp_8510 import load_reversal_cfg, reversal_oi_filters_for_gate


def _number(value):
    try:
        value=float(value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError):
        return None


def _side(value: Any):
    return {'LONG':'CE','BUY':'CE','UP':'CE','CE':'CE','SHORT':'PE','SELL':'PE','DOWN':'PE','PE':'PE'}.get(str(value or '').upper())


def armed_status(lane, row, state, positions):
    """Use reported lane state; never infer an armed side from OI direction."""
    row=row or {};state=state or {}
    for p in positions or []:
        sid=str(p.get('strategy_id') or p.get('setup_name') or p.get('strategy') or '').upper()
        matches=('EMA8_PROTOTYPE' in sid or '8 EMA PROTOTYPE' in sid) if lane=='prototype' else ('CROSSOVER' in sid or 'REVERSAL' in sid)
        if matches and str(p.get('status','OPEN')).upper() in ('OPEN','EXIT_PENDING','EXIT_FAILED'):
            return {'state':'IN_TRADE','side':_side(p.get('option_type') or p.get('direction')),'waiting_for':'position management','line':'in trade · position management'}
    arm=state.get('arm') or {} if lane=='prototype' else {}
    if lane=='prototype' and state.get('open'):
        return {'state':'IN_TRADE','side':_side(state['open'].get('direction')),'waiting_for':'position management','line':'in trade · position management'}
    current=str(row.get('state') or 'UNKNOWN').upper();phase=str(row.get('phase') or '')
    direction=_side(arm.get('direction') or row.get('direction') or row.get('reversal_side') or row.get('pre_arm_side'))
    if row.get('enabled') is False or current in ('OFF','PAUSED','DISABLED'):
        return {'state':'OFF','side':None,'waiting_for':'lane disabled','line':'none · lane disabled'}
    armed=bool(arm) or bool(row.get('armed')) or current in ('ARMED','TRIGGERED')
    if not armed:
        return {'state':'NONE','side':None,'waiting_for':phase or str(row.get('block_reason') or 'no setup'),'line':'none · '+(phase.lower() or str(row.get('block_reason') or 'no setup'))}
    if lane=='prototype' and arm and row.get('retouch_requires_away',True) and not arm.get('away_seen'):
        wait='waiting for away from 8 EMA'
    elif row.get('ema_stale') or 'STALE' in str(row.get('block_reason') or '').upper():
        wait='stale spot — touch paused (arm kept)'
    elif 'WAIT' in phase.upper() and phase:
        wait=phase.lower()
    else:
        wait=str(row.get('block_reason') or 'waiting for touch ±3')
    return {'state':'ARMED','side':direction,'waiting_for':wait,'line':(direction or 'side not reported')+' · '+wait}


def build_oi_decision_board(lane: str, scanner_cfg: Dict[str,Any], board: Dict[str,Any], row=None, state=None, positions=None):
    if lane not in ('prototype','reversal'):raise ValueError('Unknown OI lane')
    # Copy inputs so this display path cannot mutate scanner config/state or a cached board.
    scanner_cfg=copy.deepcopy(scanner_cfg or {});board=copy.deepcopy(board or {});row=copy.deepcopy(row or {})
    reads=board_reads_from_board(board);raw_sources={s.get('id'):s for s in board.get('sources',[]) if isinstance(s,dict)}
    warnings=[]
    attempt=row.get('execution_decision') or {}
    if attempt:
        warnings.append("Last entry attempt " + str(attempt.get('time') or '') + " · " + str(attempt.get('stage') or '') + ": " + str(attempt.get('reason') or '') + "; separate from the OI vote below.")
    if lane=='prototype':
        call_cfg=load_ema8_prototype_cfg(scanner_cfg);configured=load_oi_filters_cfg(call_cfg)
        enabled=True;mode=configured['mode']
    else:
        rev=load_reversal_cfg(scanner_cfg);raw=rev.get('oi_filters') or {};gate=reversal_oi_filters_for_gate(scanner_cfg)
        configured=load_oi_filters_cfg({'oi_filters':gate or raw});enabled=gate is not None
        call_cfg={'oi_filters': gate or {}};mode='block' if gate and str(gate.get('mode') or 'log_only').lower()=='block' and not bool(raw.get('log_only',True)) else 'log_only'
        if raw.get('mode')=='block' and bool(raw.get('log_only',True)):
            warnings.append('Reversal log_only override is ON: configured Block is observational only.')
    buy_block,buy=oi_switch_block(True,call_cfg,reads);sell_block,sell=oi_switch_block(False,call_cfg,reads)
    if not enabled:buy_block=sell_block=False
    configured_ids=[rid for rid in PROTO_OI_READ_IDS if configured['switches'].get(rid)]
    consumed=buy.get('switches_on',[]) if enabled else []
    if enabled and set(configured_ids)!=set(consumed):
        warnings.append('CONFIG/GATE MISMATCH: configured OI switches are not consumed by the current Reversal execution call. No gate logic was changed; combined verdict below reflects the actual call.')
    if configured.get('neutral_blocks'):
        warnings.append('Neutral blocks is configured: the existing gate counts NEUTRAL_BLOCK as opposition. The display preserves that rule; stale still fails open.')
    rows=[]
    for rid in configured_ids:
        raw=reads.get(rid) or {};source=raw_sources.get(rid) or {};b=(buy.get('reads') or {}).get(rid) or {};s=(sell.get('reads') or {}).get(rid) or {}
        # OFF is honest when the execution gate did not consume a configured switch.
        vb=b.get('vote','OFF') if enabled else 'OFF';vs=s.get('vote','OFF') if enabled else 'OFF'
        detail=str(raw.get('detail') or '')
        if vb=='STALE':detail='— stale / no data'+(' · '+detail if detail else '')
        elif not detail:detail=str(raw.get('direction') or '— no data')
        slope=_number(raw.get('S'))
        if slope is not None:detail+=f" · S {slope:+.4f} → {raw.get('direction') or 'neutral'}"
        rows.append({'id':rid,'label':PROTO_OI_READ_LABELS[rid],'direction':raw.get('direction'),'age_sec':raw.get('age_sec'),
          'vote_buy':vb,'vote_sell':vs,'live_detail':detail,'counted':rid in consumed,'S':slope,
          'price_confirm':source.get('price_confirm'),'volume_backing':source.get('volume_backing')})
    contexts=[];momentum=configured.get('momentum') or {}
    for x in rows:
        if x['id'] in ('writer_flow','total_oi','fresh_oi'):
            if x['S'] is not None:
                contexts.append({'id':'momentum_'+x['id'],'label':x['label']+' · momentum S','direction':x['direction'],
                  'live_detail':f"S {x['S']:+.4f} → {x['direction'] or 'neutral'}; already included in this read, not another vote",'counted':False})
            if momentum.get('require_price_confirm'):
                contexts.append({'id':'price_confirm_'+x['id'],'label':x['label']+' · price confirmation','direction':None,
                  'live_detail':str(x.get('price_confirm') or '— not reported by source')+'; upstream context, not a separate vote','counted':False})
    def verdict(block,detail):
        would=bool(block and enabled);actual=would and mode=='block'
        if would and mode=='log_only':
            word='WOULD BLOCK (trading still allowed)'
        elif actual:
            word='BLOCKED'
        else:
            word='ALLOWED'
        return {'would_block':would,'actual_block':actual,'verdict':word,
          'blockers':detail.get('blocked_by',[]) if would else [],'opposing_filters':detail.get('blocked_by',[]),
          'agree':detail.get('agree',0),'oppose':detail.get('oppose',0),'gate_detail':detail}
    bv=verdict(buy_block,buy);sv=verdict(sell_block,sell)
    def brief(name,v):
        if mode=='log_only' and v['would_block']:
            tail='would block — trading still allowed'
        elif v['actual_block']:
            tail='blocked'
        else:
            tail='allowed'
        return name+' '+tail+(' by '+', '.join(PROTO_OI_READ_LABELS.get(i,i) for i in v['blockers']) if v['blockers'] else '')
    return {'version':'OI_DECISION_BOARD_V241','lane':lane,'title':'8 EMA Prototype' if lane=='prototype' else '8 EMA Reversal',
      'armed':armed_status(lane,row,state,positions),'mode':mode,'configured_mode':configured['mode'],
      'rule':buy.get('rule',configured['rule']),'configured_rule':configured['rule'],'enabled':enabled,
      'rows':rows,'context_rows':contexts,'buy':bv,'sell':sv,'warnings':warnings,'as_of':board.get('as_of'),
      'summary':'Right now (OI only): '+brief('BUY (CE)',bv)+' · '+brief('SELL (PE)',sv)+'.',
      'scope':'OI eligibility only, not an entry instruction. Setup/touch, 3m and other execution gates remain separate. Read-only display; no settings changed.'}


def enrich_strategy_rows(scanner_cfg, strategy_status, board, proto_state=None, positions=None):
    out={k:dict(v) if isinstance(v,dict) else v for k,v in (strategy_status or {}).items()}
    for key,lane in (('ema8_prototype','prototype'),('crossover_reversal','reversal')):
        row=dict(out.get(key) or {})
        row['oi_decision_board']=build_oi_decision_board(lane,scanner_cfg,board,row,proto_state if lane=='prototype' else {},positions)
        out[key]=row
    return out
