"""Read-only ICT SMC Desk Signals; no order-engine dependency.

Classification verdict port: locked OcLab.tsx:124-699, deskState.ts, regime.ts.
Classic Writer Flow's histogram is flow=delta put minus delta call (OcLab:2291),
NOT ce_vol/pe_vol. Active Corridor is context, not a directional setup.
Writer Stack-up has no identifiable rule in SmartOISuite; remains NOT BUILT.
Only source numeric observations are accepted. OI does not identify seller intent.
"""
import math
import json
from pathlib import Path

RULES = Path(__file__).with_name('ict_smc_rules_8510.json')
def rules():
    return json.loads(RULES.read_text())

def jr(x):
    return math.floor(x + .5)  # JS Math.round, including negative ties

def classify_rows(newest, tf='3m', settings=None):
    """Pure verdict port. Caller must supply only the prefix observable at T.
    Returns newest first. English order tickets deliberately are not reproduced.
    """
    q=settings or rules(); b={'1m':q['numeric_literals']['1'],'3m':q['numeric_literals']['3'],'5m':q['numeric_literals']['5'],'15m':q['numeric_literals']['15']}.get(tf,q['numeric_literals']['60'])
    n15=max(q['numeric_literals']['1'],jr(q['numeric_literals']['15']/b));n30=max(q['numeric_literals']['2'],jr(q['numeric_literals']['30']/b));rows=list(reversed(newest));out=[]
    allcr=[r[k]/q['numeric_literals']['1e7'] for r in rows for k in ('ce_day','pe_day') if r.get(k) is not None]
    span=max(q['numeric_literals']['2'],max(allcr,default=q['numeric_literals']['5'])-min(allcr,default=-q['numeric_literals']['5']))
    cell=q['numeric_literals']['1'] if len(rows)>q['numeric_literals']['30'] else q['numeric_literals']['2.5'] if span>q['numeric_literals']['15'] else q['numeric_literals']['1'] if span>q['numeric_literals']['6'] else q['numeric_literals']['.5']
    hi=-math.inf;lo=math.inf;lasttone=None;lastidx=-q['numeric_literals']['99'];lastfire=None
    for i,r in enumerate(rows):
        spot=r.get('close') or q['numeric_literals']['0']; prev=rows[i-q['numeric_literals']['1']] if i else {};get=lambda k: r.get(k) or q['numeric_literals']['0']
        if spot>q['numeric_literals']['0']:hi=max(hi,spot);lo=min(lo,spot)
        d=get('pe_ce_chg')/q['numeric_literals']['1e5'];c=get('ce_chg')/q['numeric_literals']['1e5'];p=get('pe_chg')/q['numeric_literals']['1e5'];day=get('pe_ce_day')/q['numeric_literals']['1e7']
        impulse=sum((v.get('pe_ce_chg') or q['numeric_literals']['0']) for v in rows[max(q['numeric_literals']['0'],i-n15+q['numeric_literals']['1']):i+q['numeric_literals']['1']])/q['numeric_literals']['1e5']
        change=spot-(rows[max(q['numeric_literals']['0'],i-n15)].get('close') if rows[max(q['numeric_literals']['0'],i-n15)].get('close') is not None else spot)
        window=[v['close'] for v in rows[max(q['numeric_literals']['0'],i-n30+q['numeric_literals']['1']):i+q['numeric_literals']['1']] if v.get('close') is not None and v['close']>q['numeric_literals']['0']]
        sh=max(window,default=spot);sl=min(window,default=spot);bounce=spot-sl;dip=sh-spot
        read=r.get('read') or ['', 'flat'];pro=r.get('pro') or ['', '', '', ''];lv=r.get('lv') or ['', '', '']
        read=(read+['',''])[:q['numeric_literals']['2']];pro=(pro+['','','',''])[:q['numeric_literals']['4']];lv=(lv+['','',''])[:q['numeric_literals']['3']]
        rt=read[q['numeric_literals']['0']] or '';pt=pro[q['numeric_literals']['0']] or '';wall=pro[q['numeric_literals']['3']] or (lv[q['numeric_literals']['0']] if lv[q['numeric_literals']['2']] in ('break_dn','break_up') else '')
        wb=any(x in wall for x in q['wall_bear']) or (r.get('sup') is not None and prev.get('sup') is not None and r['sup']<prev['sup'])
        wu=any(x in wall for x in q['wall_bull']) or (r.get('res') is not None and prev.get('res') is not None and r['res']>prev['res'])
        zb=(prev.get('pe_ce_day') or q['numeric_literals']['0'])>=q['numeric_literals']['0'] and get('pe_ce_day')<q['numeric_literals']['0'] and d<=q['numeric_literals']['0']
        zu=(prev.get('pe_ce_day') or q['numeric_literals']['0'])<=q['numeric_literals']['0'] and get('pe_ce_day')>q['numeric_literals']['0'] and d>=q['numeric_literals']['0']
        bull=(day>=q['numeric_literals']['.25'] or impulse>=q['numeric_literals']['25']) and day>=-q['numeric_literals']['.20'];bear=(day<=-q['numeric_literals']['.25'] or impulse<=-q['numeric_literals']['25']) and day<=q['numeric_literals']['.20']
        supply=bear and q['numeric_literals']['18']<=bounce<=q['numeric_literals']['65'] and d<q['numeric_literals']['5'] and impulse<=q['numeric_literals']['10'] and (c>q['numeric_literals']['0'] or 'Rally sold' in rt or 'Call writing' in rt or 'rally being sold' in pt or read[q['numeric_literals']['1']]=='bear' or pro[q['numeric_literals']['1']]=='bear')
        demand=bull and q['numeric_literals']['18']<=dip<=q['numeric_literals']['65'] and d>-q['numeric_literals']['5'] and impulse>=-q['numeric_literals']['10'] and (p>q['numeric_literals']['0'] or 'Dip defended' in rt or 'Put writing' in rt or read[q['numeric_literals']['1']]=='bull')
        cover=bounce>=q['numeric_literals']['45'] and ('Confirmed ▲' in pt or 'Confirmed ▲' in rt or c+(prev.get('ce_chg') or q['numeric_literals']['0'])/q['numeric_literals']['1e5']<=-q['numeric_literals']['50'] or c<=-q['numeric_literals']['50'])
        unwind=dip>=q['numeric_literals']['45'] and ('Confirmed ▼' in pt or 'Confirmed ▼' in rt or p+(prev.get('pe_chg') or q['numeric_literals']['0'])/q['numeric_literals']['1e5']<=-q['numeric_literals']['50'] or p<=-q['numeric_literals']['50'])
        reversal=hi-spot>=q['numeric_literals']['160'] and (c<=-q['numeric_literals']['5'] or 'Short covering' in rt) and (spot>=sl+q['numeric_literals']['15'] or bool(prev) and spot>(prev.get('close') or q['numeric_literals']['0']))
        stage=q['numeric_literals']['0'];badge='WAIT';tone='flat';ticket=False;hold=False;risk=None
        ts=r['ts'];riskof=lambda x:min(max(jr(x+q['numeric_literals']['12']),q['numeric_literals']['15']),q['numeric_literals']['35'])
        if ts>=q['time_thresholds']['15:30']:badge='AFTER CLOSE';tone='skip'
        elif ts>=q['time_thresholds']['15:10']:badge='🏁 EOD SETTLEMENT';tone='skip';hold=True
        elif ts<q['time_thresholds']['09:30']:badge='WARM-UP'
        elif cover:
            risk=riskof(bounce)
            if get('pe_ce_day')>=q['numeric_literals']['5000000'] and (r.get('pcr') if r.get('pcr') is not None else q['numeric_literals']['1'])>=q['numeric_literals']['.88']:stage=q['numeric_literals']['4'];badge='🛡️ HOLD CE';tone='bull';hold=True
            else:stage=q['numeric_literals']['5'];badge='🟡 BOOK PE';tone='warn'
        elif unwind:
            risk=riskof(dip)
            if get('pe_ce_day')<=-q['numeric_literals']['5000000'] and (r.get('pcr') if r.get('pcr') is not None else q['numeric_literals']['1'])<=q['numeric_literals']['1.12']:stage=q['numeric_literals']['4'];badge='🛡️ HOLD PE';tone='bear';hold=True
            else:stage=q['numeric_literals']['5'];badge='🟡 BOOK CE';tone='warn'
        elif reversal:stage=q['numeric_literals']['5'];badge='💰 BOOK / REVERSAL';tone='bull';risk=riskof(bounce)
        elif supply and day<q['numeric_literals']['1']:
            stage=q['numeric_literals']['2'];badge='🎯 PULLBACK SELL';tone='bear';risk=riskof(dip);ticket=True
            if dip>q['numeric_literals']['75']:stage=q['numeric_literals']['4'];badge='🛡️ HOLD PE';hold=True;ticket=False
        elif demand and day>-q['numeric_literals']['1']:
            stage=q['numeric_literals']['2'];badge='🎯 DIP BUY CE';tone='bull';risk=riskof(bounce);ticket=True
            if bounce>q['numeric_literals']['75']:stage=q['numeric_literals']['4'];badge='🛡️ HOLD CE';hold=True;ticket=False
        elif (zb or wb or lv[q['numeric_literals']['2']]=='break_dn' and '●' in (pro[q['numeric_literals']['2']] or '')) and d<=q['numeric_literals']['5'] and day<=q['numeric_literals']['.5']:
            stage=q['numeric_literals']['1'];risk=riskof(dip)
            if change<-q['numeric_literals']['85']:badge='⏳ WAIT PULLBACK';tone='warn'
            else:badge='🎯 SNIPER BREAKDOWN';tone='bear';ticket=True
        elif (zu or wu or lv[q['numeric_literals']['2']]=='break_up' and '●' in (pro[q['numeric_literals']['2']] or '')) and d>=-q['numeric_literals']['5'] and day>=-q['numeric_literals']['.5']:
            stage=q['numeric_literals']['1'];risk=riskof(bounce)
            if change>q['numeric_literals']['85']:badge='⏳ WAIT DIP';tone='warn'
            else:badge='🎯 SNIPER BREAKOUT';tone='bull';ticket=True
        elif abs(d)>=q['numeric_literals']['20'] and (pro[q['numeric_literals']['2']] in ('●●','●●●') or 'BUY' in ((r.get('act') or [''])[q['numeric_literals']['0']] or '')):
            stage=q['numeric_literals']['3'];hold=True
            if d>q['numeric_literals']['0']:
                if bear and day<-q['numeric_literals']['.3']:badge='⚠️ SHOCK';tone='warn';risk=riskof(dip)
                else:badge='🔥 ADD CE';tone='bull';risk=riskof(bounce)
            else:
                if bull and day>q['numeric_literals']['.3']:badge='⚠️ SHOCK';tone='warn';risk=riskof(bounce)
                else:badge='🔥 ADD PE';tone='bear';risk=riskof(dip)
        elif hi-spot>q['numeric_literals']['100'] and day<-q['numeric_literals']['.8']:stage=q['numeric_literals']['4'];badge='🛡️ HOLD PE';tone='bear';hold=True
        elif spot-lo>q['numeric_literals']['100'] and day>q['numeric_literals']['.8']:stage=q['numeric_literals']['4'];badge='🛡️ HOLD CE';tone='bull';hold=True
        elif abs(d)<q['numeric_literals']['8']:badge='NOISE';tone='skip'
        p5=rows[i-q['numeric_literals']['5']] if i>=q['numeric_literals']['5'] else rows[q['numeric_literals']['0']] if i>q['numeric_literals']['0'] else None;factor=q['numeric_literals']['1'] if i>=q['numeric_literals']['5'] or i==q['numeric_literals']['0'] else q['numeric_literals']['5']/i
        ca=math.degrees(math.atan(((get('ce_day')-(p5.get('ce_day') or q['numeric_literals']['0']))/q['numeric_literals']['1e7']*factor if p5 else q['numeric_literals']['0'])/cell))
        pa=math.degrees(math.atan(((get('pe_day')-(p5.get('pe_day') or q['numeric_literals']['0']))/q['numeric_literals']['1e7']*factor if p5 else q['numeric_literals']['0'])/cell))
        sc=abs(ca-pa)>=q['numeric_literals']['45'] and (ca>q['numeric_literals']['15'] and pa<-q['numeric_literals']['15'] or ca<-q['numeric_literals']['15'] and pa>q['numeric_literals']['15']);supp=False
        if ticket:
            if lasttone and lasttone!=tone and i-lastidx<=q['numeric_literals']['3']:supp=True;stage=q['numeric_literals']['0'];badge='⚠️ CONFLICT';tone='warn'
            else:lasttone=tone;lastidx=i
        if not supp and ticket:
            sig=badge+'|'+tone;tol=max(q['numeric_literals']['5'],jr((risk if risk is not None else q['numeric_literals']['20'])/q['numeric_literals']['2']))
            if lastfire and lastfire[q['numeric_literals']['0']]==sig and i-lastfire[q['numeric_literals']['1']]<=q['numeric_literals']['5'] and abs(spot-lastfire[q['numeric_literals']['2']])<tol:supp=True;stage=q['numeric_literals']['0'];badge='🔁 RE-FIRE';tone='warn'
            else:lastfire=(sig,i,spot)
        conviction={'●':q['numeric_literals']['55'],'●●':q['numeric_literals']['70'],'●●●':q['numeric_literals']['85']}.get(pro[q['numeric_literals']['2']],q['numeric_literals']['35'])
        if abs(ca)>=q['numeric_literals']['30'] or abs(pa)>=q['numeric_literals']['30']:conviction=min(q['numeric_literals']['95'],conviction+q['numeric_literals']['15'])
        against=(day>=q['numeric_literals']['2'] and tone=='bear' or day<=-q['numeric_literals']['2'] and tone=='bull')
        synergy=(tone=='bear' and (ca>=q['numeric_literals']['25'] or sc or pa<=-q['numeric_literals']['20']) or tone=='bull' and (pa>=q['numeric_literals']['25'] or sc and ca<-q['numeric_literals']['15'] or ca<=-q['numeric_literals']['20'])) and not against
        if synergy:conviction=q['numeric_literals']['95']
        if against:conviction=min(conviction,q['numeric_literals']['55']);synergy=False
        actionable=not supp and (stage in (q['numeric_literals']['1'],q['numeric_literals']['2'],q['numeric_literals']['3'],q['numeric_literals']['5']) or wb or wu or synergy)
        high=conviction>=q['numeric_literals']['65'] or abs(d)>=q['numeric_literals']['15'] or synergy
        regime='EOD' if ts>=q['time_thresholds']['15:10'] else 'WARMUP' if ts<q['time_thresholds']['09:45'] else 'TREND' if abs(day)>=q['numeric_literals']['2'] else 'CHOP'
        perm='NONE' if regime in ('EOD','WARMUP') else 'LONG_ONLY' if day>=q['numeric_literals']['2'] else 'SHORT_ONLY' if day<=-q['numeric_literals']['2'] else 'BOTH'
        allowed=perm=='BOTH' or perm=='LONG_ONLY' and tone=='bull' or perm=='SHORT_ONLY' and tone=='bear'
        action='ENTER' if ticket and not supp and allowed else 'HOLD' if (hold and not supp or badge=='⚠️ CONFLICT') else 'WAIT'
        key=perm+'|'+regime+'|'+action
        out.append(dict(ts=ts,spot=spot,stage=stage,stageBadge=badge,actionTone=tone,suppressed=supp,convictionPct=conviction,isActionable=bool(actionable),isHighConviction=bool(high),isSynergy=bool(synergy),isScissor=bool(sc),isPullbackToSupply=bool(supply),isPullbackToDemand=bool(demand),isShortCoveringReversal=bool(reversal),spotChange15m=change,stateKey=key,stateChange=not out or out[-q['numeric_literals']['1']]['stateKey']!=key,callAngleDeg=ca,putAngleDeg=pa))
    return out[::-q['numeric_literals']['1']]


GROUPS = ('State Change','Actionable Setups','High Conviction','Burst','Active Corridor','Pullback Radar','Writer Stack-up','Matrix 2.0 Diverging bars','PCR OI vs Futures','Fresh Change in OI','Cumulative Total OI','Classic Writer Flow','Option volume')
ENDPOINTS = ('/api/oclab/pece','/api/oclab/flow','/api/chain/trinity','/api/chain','/api/pe_ce_matrix','/api/smart_oi','/api/desk/itm-atm-oi-walls','/api/chain/oi-studio')

def unavailable(group, reason, endpoint=''):
    return dict(signal_group=group,signal_name=group,verdict='UNAVAILABLE',direction='NEUTRAL',significant=False,reason=reason,source_endpoint=endpoint,spot=None)

def pece_groups(payload, now, cfg):
    """Only closed current-session rows; never convert yesterday into live advice."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    tz=ZoneInfo('Asia/Kolkata');day=payload.get('day')
    if day!=now.astimezone(tz).date().isoformat():raise ValueError('Source session is not current')
    raw=payload.get('rows');tf=cfg.get('timeframe','3m');minutes={'1m':1,'3m':3,'5m':5,'15m':15}.get(tf,60)
    if not isinstance(raw,list) or not raw:raise ValueError('No source rows')
    rows=[];ends=[]
    for r in raw:
        if not isinstance(r,dict):raise ValueError('Malformed row')
        end=datetime.fromisoformat(day+'T'+r['ts']).replace(tzinfo=tz)+timedelta(minutes=minutes)
        if end>now:continue
        if not all(isinstance(r.get(k),(float,int)) and not isinstance(r[k],bool) and math.isfinite(r[k]) for k in ('close','pe_day','ce_day','pe_chg','ce_chg','pe_ce_day','pe_ce_chg')):raise ValueError('Missing/nonfinite numeric source')
        if not isinstance(r.get('read'),list) or len(r['read'])<2:raise ValueError('Missing source read classification')
        rows.append(r);ends.append(end)
    if not rows:raise ValueError('No closed source bar')
    if (now-max(ends)).total_seconds()>cfg['max_source_age_seconds']:raise ValueError('Closed source data stale')
    rows=sorted(rows,key=lambda r:r['ts'],reverse=True);smart=classify_rows(rows,tf);r=smart[0];rawrow=rows[0]
    tone={'bull':'CE','bear':'PE'}.get(r['actionTone'],'NEUTRAL');items=[]
    flags=cfg['significance']
    for group,key in [('State Change','stateChange'),('Actionable Setups','isActionable'),('High Conviction','isHighConviction'),('Burst','isHighConviction')]:
        enabled=flags.get(group,True)
        items.append(dict(signal_group=group,signal_name=r['stageBadge'],verdict=r['stageBadge'],direction=tone,significant=bool(r[key] and enabled),spot=r['spot'],source_endpoint='/api/oclab/pece',source_ts=max(ends).isoformat(),reason='ADVISORY; original UI verdict, not a trade instruction'))
    radar='AT_PULLBACK_SELL_ZONE' if r['isPullbackToSupply'] else 'OVER_EXTENDED_DROP' if r['spotChange15m']<-80 else 'SHORT_COVERING_REVERSAL' if r['isShortCoveringReversal'] else 'MID_RANGE_CONSOLIDATION'
    items.append(dict(signal_group='Pullback Radar',signal_name=radar,verdict=radar,direction='NEUTRAL',significant=False,spot=r['spot'],source_endpoint='/api/oclab/pece',source_ts=max(ends).isoformat(),reason='Context only; source card has no independent entry gate'))
    if all(isinstance(rawrow.get(k),(float,int)) and math.isfinite(rawrow[k]) for k in ('sup','res')):
        pos=min(100,max(0,(r['spot']-rawrow['sup'])/max(50,rawrow['res']-rawrow['sup'])*100))
        items.append(dict(signal_group='Active Corridor',signal_name='POSITION',verdict=f"{pos:.1f}% between observed walls",direction='NEUTRAL',significant=False,spot=r['spot'],source_endpoint='/api/oclab/pece',source_ts=max(ends).isoformat(),reason='Numeric context; no invented direction'))
    else:items.append(unavailable('Active Corridor','Actual source walls missing; UI fallback levels are not observations','/api/oclab/pece'))
    return items

class SignalBridge:
    """Bounded local reads, append-only SQLite logging; cannot call broker APIs."""
    def __init__(self, cfg, db_path, fetch=None):
        import threading
        self.cfg=dict(cfg);self.db_path=Path(db_path);self.fetch=fetch or self._fetch
        self.lock=threading.Lock();self.last_poll=0;self.last={};self.last_error=None
    def _fetch(self, endpoint):
        import urllib.request
        from urllib.parse import urlencode,urlparse
        base=self.cfg['base_url'].rstrip('/');host=urlparse(base)
        if host.hostname not in ('127.0.0.1','localhost','::1'):raise ValueError('Only local read-only source allowed')
        query=urlencode(dict(tf=self.cfg.get('timeframe','3m'),mode='all',n=10)) if endpoint.startswith('/api/oclab/') else ''
        req=urllib.request.Request(base+endpoint+('?' +query if query else ''),method='GET')
        with urllib.request.urlopen(req,timeout=self.cfg['timeout_seconds']) as response:
            data=response.read(self.cfg.get('max_payload_bytes',4000000)+1)
        if len(data)>self.cfg.get('max_payload_bytes',4000000):raise ValueError('Source payload too large')
        obj=json.loads(data)
        if not isinstance(obj,dict):raise ValueError('Object payload required')
        return obj
    def evaluate(self, now=None):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from concurrent.futures import ThreadPoolExecutor
        import time,sqlite3
        now=now or datetime.now(ZoneInfo('Asia/Kolkata'))
        with self.lock:
            if self.last and time.monotonic()-self.last_poll<self.cfg['cache_seconds']:return self.last
            raw={};errors={}
            def one(ep):
                try:return ep,self.fetch(ep),None
                except Exception as exc:return ep,None,type(exc).__name__+': '+str(exc)[:180]
            if self.cfg.get('enabled',True):
                with ThreadPoolExecutor(max_workers=len(ENDPOINTS)) as pool:
                    for ep,obj,err in pool.map(one,ENDPOINTS):
                        if err:errors[ep]=err
                        else:raw[ep]=obj
            items=[]
            if '/api/oclab/pece' in raw:
                try:items=pece_groups(raw['/api/oclab/pece'],now,self.cfg)
                except (ValueError,TypeError,KeyError,IndexError,OverflowError) as exc:errors['/api/oclab/pece']=str(exc)
            items.extend(other_source_context(raw,now,self.cfg))
            known={x['signal_group'] for x in items}
            for group in GROUPS:
                if group not in known:
                    reason='NOT BUILT: source classification/provenance has not passed parity' if group not in GROUPS[:6] else errors.get('/api/oclab/pece','Source unavailable')
                    items.append(unavailable(group,reason,'/api/oclab/pece' if group in GROUPS[:6] else ''))
            stamp=now.isoformat()
            for item in items:
                item.update(ts=stamp,session_date=now.date().isoformat(),session_time=now.strftime('%H:%M:%S'),age_seconds=(now-datetime.fromisoformat(item['source_ts'])).total_seconds() if item.get('source_ts') else None)
            self.db_path.parent.mkdir(parents=True,exist_ok=True)
            try:
                with sqlite3.connect(self.db_path,timeout=2) as db:
                    db.execute("CREATE TABLE IF NOT EXISTS ict_smc_signal_log (id INTEGER PRIMARY KEY,ts TEXT,session_date TEXT,session_time TEXT,signal_group TEXT,signal_name TEXT,verdict TEXT,direction TEXT,significant INTEGER,raw_inputs TEXT,spot REAL,source_endpoint TEXT)")
                    db.execute('CREATE TABLE IF NOT EXISTS ict_smc_evaluation_inputs (id INTEGER PRIMARY KEY, ts TEXT NOT NULL, payloads TEXT NOT NULL)')
                    evaluation_id = db.execute('INSERT INTO ict_smc_evaluation_inputs(ts,payloads) VALUES (?,?)', (stamp,json.dumps(safe_raw({'payloads':raw,'errors':errors,'rules':rules()}),allow_nan=False))).lastrowid
                    db.executemany('INSERT INTO ict_smc_signal_log (ts,session_date,session_time,signal_group,signal_name,verdict,direction,significant,raw_inputs,spot,source_endpoint) VALUES (?,?,?,?,?,?,?,?,?,?,?)',[(stamp,x['session_date'],x['session_time'],x['signal_group'],x['signal_name'],x['verdict'],x['direction'],int(x['significant']),json.dumps({'evaluation_id':evaluation_id,'source_ts':x.get('source_ts'),'reason':x.get('reason')},allow_nan=False),x.get('spot'),x['source_endpoint']) for x in items])
                self.last_error=None
            except (sqlite3.Error,ValueError,OSError) as exc:self.last_error=type(exc).__name__+': '+str(exc)
            self.last=dict(name='ICT SMC Desk Signals',evaluated_at=stamp,rows=items,use_for_algo_trades=False,status='ADVISORY',log_error=self.last_error)
            self.last_poll=time.monotonic();return self.last


def context_verdicts(raw, smart):
    """OcLab.tsx:1310-1350. UI fallback geometry kept for parity only.
    Live card separately refuses missing observed walls; no fake level is advised.
    """
    q=rules()['context']
    radar='AT_PULLBACK_SELL_ZONE' if smart['isPullbackToSupply'] else 'OVER_EXTENDED_DROP' if smart['spotChange15m']<q['overextended_drop'] else 'SHORT_COVERING_REVERSAL' if smart['isShortCoveringReversal'] else 'MID_RANGE_CONSOLIDATION'
    sup=raw['sup'] if raw.get('sup') is not None else q['ui_fallback_sup']
    res=raw['res'] if raw.get('res') is not None else q['ui_fallback_res']
    return {'radar':radar,'corridorPct':min(100,max(0,(smart['spot']-sup)/max(q['minimum_span'],res-sup)*100))}


def numeric_context(kind, point, previous=None):
    """Exact numeric context predicates, not standalone entry signals.
    PcrTrinitySuite.tsx:946-984, OcLab.tsx:2884-2900,
    SmartOISuite.tsx:588-592. No writer-intent claims are added.
    """
    q=rules()['numeric_context'];previous=previous or point
    def number(key):
        v=point[key]
        if isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v):raise ValueError('Invalid '+key)
        return v
    if kind=='PCR OI vs Futures':
        x=number('pcr_oi');return ('BULLISH_MULTI_DAY','CE') if x>=q['pcr_bull'] else ('BEARISH_MULTI_DAY','PE') if x<=q['pcr_bear'] else ('NEUTRAL_EQUILIBRIUM','NEUTRAL')
    if kind=='Fresh Change in OI':
        c=number('call_oi_chg');p=number('put_oi_chg')
        return ('BULLISH_REGIME','CE') if p>c else ('BULLISH_RECOVERY','CE') if c>p and c<previous['call_oi_chg'] and p>previous['put_oi_chg'] else ('BEARISH_REGIME','PE')
    if kind=='Cumulative Total OI':return ('POSITIONAL_PUT_EXCESS','CE') if number('put_oi')>number('call_oi') else ('POSITIONAL_CALL_CEILING','PE')
    if kind=='Classic Writer Flow':
        f=number('flow');b=number('bal');threshold=q['flow_balance']
        if f>0 and b>threshold:return 'BULLISH_DOMINANCE','CE'
        if f<0 and b<-threshold:return 'BEARISH_DOMINANCE','PE'
        if f>0 and b<-threshold:return 'COUNTER_FLOW_COVERING','NEUTRAL'
        if f<0 and b>threshold:return 'PROFIT_BOOKING_DIP','NEUTRAL'
        return 'NEUTRAL_ROTATION','NEUTRAL'
    if kind=='Matrix 2.0 Diverging bars':return ('NONNEGATIVE_OI_DELTA','NEUTRAL') if number('smart_oi_delta')>=0 else ('NEGATIVE_OI_DELTA','NEUTRAL')
    raise ValueError('No audited rule for '+kind)


def other_source_context(raw, now, cfg):
    from datetime import datetime,timedelta,timezone
    from zoneinfo import ZoneInfo
    tz=ZoneInfo('Asia/Kolkata');items=[]
    def fresh(stamp):
        age=(now-stamp).total_seconds()
        if stamp.date()!=now.date() or age<0 or age>cfg['max_source_age_seconds']:raise ValueError('Missing, future or stale observation')
        return stamp
    def emit(group,point,stamp,endpoint,previous=None):
        verdict,direction=numeric_context(group,point,previous)
        return dict(signal_group=group,signal_name=verdict,verdict=verdict,direction=direction,significant=False,spot=point.get('fut_close',point.get('fut')),source_endpoint=endpoint,source_ts=stamp.isoformat(),reason='ADVISORY context only; no independently validated entry significance')
    tr=raw.get('/api/chain/trinity',{});series=tr.get('series',{}).get('cur',[]) if isinstance(tr.get('series',{}),dict) else []
    try:
        # Only explicitly recorded signal points. A smoothed/reconstructed series is insufficient.
        observed=[x for x in series if isinstance(x.get('signal_point'),dict) and x['signal_point'].get('sample_time')]
        observed.sort(key=lambda x:x['signal_point']['sample_time']);pt=observed[-1];stamp=fresh(datetime.fromtimestamp(pt['signal_point']['sample_time'],tz))
        prev=observed[max(0,len(observed)-4)]
        for group in ('PCR OI vs Futures','Fresh Change in OI','Cumulative Total OI'):items.append(emit(group,pt,stamp,'/api/chain/trinity',prev))
    except (ValueError,TypeError,KeyError,IndexError,OverflowError,AttributeError):
        # No invented "latest" from a verdict timestamp or interpolated line.
        for group in ('PCR OI vs Futures','Fresh Change in OI','Cumulative Total OI'):items.append(unavailable(group,'Verified recorded source point unavailable','/api/chain/trinity'))
    flow=raw.get('/api/oclab/flow',{})
    try:
        if flow.get('day')!=now.date().isoformat():raise ValueError('Wrong source session')
        pt=flow['points'][-1]
        # Source _ct is wall-clock IST encoded as UTC, not an actual UTC receipt time.
        stamp=datetime.fromtimestamp(pt['t'],timezone.utc).replace(tzinfo=tz)+timedelta(minutes={'1m':1,'3m':3,'5m':5,'15m':15}.get(cfg.get('timeframe'),60))
        fresh(stamp);items.append(emit('Classic Writer Flow',pt,stamp,'/api/oclab/flow'))
    except (ValueError,TypeError,KeyError,IndexError,OverflowError,AttributeError):items.append(unavailable('Classic Writer Flow','Verified closed source bar unavailable','/api/oclab/flow'))
    # Smart OI candles can be reconstructed from spot volume; without an explicit
    # recorded source sample and session, histogram freshness cannot be certified.
    items.append(unavailable('Matrix 2.0 Diverging bars','Numeric display port exists; observed smart-OI timestamp provenance is not exposed','/api/smart_oi'))
    return items


def safe_raw(value):
    if isinstance(value,float) and not math.isfinite(value):return {'invalid_nonfinite':str(value)}
    if isinstance(value,dict):return {str(k):safe_raw(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)):return [safe_raw(v) for v in value]
    return value
