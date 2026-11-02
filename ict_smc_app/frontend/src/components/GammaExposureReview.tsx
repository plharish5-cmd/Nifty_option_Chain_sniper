import React, { useEffect, useRef, useState } from "react";
import rules from "../../../../analyst/analyst_config.json";
import "./gammaExposureReview.css";

const POLICY = rules.gamma_exposure;
type GStrike = {strike:number;call:number;put:number;net:number;gross:number};
type GPoint = {ok:boolean;message?:string;ts:number;spot:number;net:number;gross:number;balance:number;regime:string;coverage:number;valid_strikes:number;total_strikes:number;call_wall:number;put_wall:number;concentration:number;strike_cross:number|null;model_flip:number|null;greek_source:string};
type GProfile = GPoint & {strikes:GStrike[]};
export type GammaContext = {bar_ts:number;known_at:number;tf:string;direction:string;level:number;close:number;displacement:boolean;slot_rvol:number|null;cumulative_rvol:number|null;baseline:boolean;big_activity:boolean;eligible:boolean;gamma_ts:number|null;gamma_regime:string;gamma_net:number|null;gamma_age_seconds:number|null;prior_gamma_wall:number|null;prior_gamma_wall_ts:number|null;gamma_wall_close_cross:boolean|null;context:string;volume_source:string};
export type GammaView = {ok:boolean;message?:string;day:string;days:string[];expiry:string;expiries:string[];selected:GProfile;history:GPoint[];contexts:GammaContext[];timeline:number[];end_ts:number;sign:number;method:{formula:string;unit:string;oi_unit:string;scope:string;source:string;sign_reconciliation:string[];limitations:string[]}};
type Arm = {baseline:{events:number;sessions:number;mean_points:number|null;positive_fraction:number|null};gamma_filtered:{events:number;sessions:number;mean_points:number|null;positive_fraction:number|null};difference_points:number|null;session_bootstrap_interval:number[]|null};
type GStudy = {ok:boolean;message?:string;specification:{id:string;variant_count:number;forward_minutes:number};sessions_available:number;train_days:string[];holdout_days:string[];train:Arm;holdout:Arm;all:Arm;excluded:Record<string,number>;verdict:string;sufficient_sample:boolean;event_count:number;caveats:string[]};
export const gexCr = (n:number|null|undefined) => n == null || !Number.isFinite(n) ? "—" : `${n < 0 ? "−" : ""}${(Math.abs(n)/1e7).toLocaleString("en-IN",{maximumFractionDigits:2})}Cr`;
const price = (n:number|null|undefined) => n == null || !Number.isFinite(n) ? "—" : n.toLocaleString("en-IN",{maximumFractionDigits:1});
const time = (t:number) => new Date(t*1000).toLocaleTimeString("en-IN",{timeZone:"Asia/Kolkata",hour:"2-digit",minute:"2-digit",second:"2-digit",hour12:false});
const num = (n:number|null|undefined) => n == null ? "—" : n.toFixed(2);
function segments(points: Array<[number,number]|null>) {
  const parts:string[]=[];let run:string[]=[];
  for(const p of points){if(p)run.push(`${p[0]},${p[1]}`);else if(run.length){parts.push(run.join(" "));run=[];}}
  if(run.length)parts.push(run.join(" "));return parts;
}
function ProfileChart({data,layout,range,walls,flip,onSelect,selected}:{data:GammaView;layout:string;range:string;walls:boolean;flip:boolean;onSelect:(s:GStrike)=>void;selected:number|null}){
  const p=data.selected;
  if(!p?.ok)return <div className="gx-empty">{p?.message||"Gamma profile unavailable"}</div>;
  const nearest=Math.round(p.spot/50)*50;
  const rows=p.strikes.filter(r=>range==="all"||Math.abs(r.strike-nearest)<=Number(range)*50);
  if(!rows.length)return <div className="gx-empty">No strikes in this range.</div>;
  const W=1080,H=440,L=100,R=970,T=35,B=380;
  const mx=Math.max(1,...rows.flatMap(r=>layout==="net"?[Math.abs(r.net)]:layout==="legs"?[r.call,r.put]:[Math.abs(r.net),r.gross]));
  const minK=rows[0].strike-25,maxK=rows[rows.length-1].strike+25;
  const x=(k:number)=>L+(k-minK)/(maxK-minK)*(R-L);
  const y=(v:number)=>B/2+T/2-v/mx*(B-T)/2;
  const base=y(0),barW=Math.min(32,(R-L)/rows.length*.72);
  if(layout==="ladder"){
    const rh=34,sh=Math.max(240,rows.length*rh+70),center=530;
    return <svg className="gx-svg" viewBox={`0 0 ${W} ${sh}`} role="img" aria-label="Gamma exposure by strike, price on the vertical axis">
      <text x="30" y="20" className="gx-label">Strike</text><text x={center} y="20" className="gx-label">Signed net ← 0 → positive · blue outline = gross exposure</text>
      <line x1={center} y1="35" x2={center} y2={sh-20} className="gx-grid"/>
      {[...rows].reverse().map((r,i)=>{const py=52+i*rh,w=r.net/mx*360;return <g key={r.strike} onClick={()=>onSelect(r)} className="gx-interactive"><text x="25" y={py+4} className="gx-label">{price(r.strike)}</text><rect x={Math.min(center,center+w)} y={py-9} width={Math.max(.5,Math.abs(w))} height="18" fill={r.net>=0?"#42c98b":"#fa777c"}/><rect x={center} y={py-12} width={r.gross/mx*360} height="24" fill="none" stroke="#46a7ff"/><text x="910" y={py+4} className="gx-label">{gexCr(r.net)}</text><title>{r.strike}: net {gexCr(r.net)}, gross {gexCr(r.gross)}</title></g>;})}
    </svg>;
  }
  return <svg className="gx-svg" viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Gamma exposure strike profile">
    {[-1,-.5,0,.5,1].map(f=><g key={f}><line x1={L} x2={R} y1={y(mx*f)} y2={y(mx*f)} className="gx-grid"/><text x="88" y={y(mx*f)+4} textAnchor="end" className="gx-label">{gexCr(mx*f)}</text></g>)}
    {rows.map(r=><g key={r.strike} onClick={()=>onSelect(r)} className="gx-interactive">
      {layout==="legs"?<><rect x={x(r.strike)-barW/2} y={y(r.call)} width={barW/2} height={base-y(r.call)} fill="#f6b252"/><rect x={x(r.strike)} y={y(r.put)} width={barW/2} height={base-y(r.put)} fill="#46d5ec"/></>:<rect x={x(r.strike)-barW/2} y={Math.min(base,y(r.net))} width={barW} height={Math.max(.5,Math.abs(base-y(r.net)))} fill={r.net>=0?"#42c98b":"#fa777c"} stroke={r.strike===selected?"#fff":"none"}/>}
      <title>{r.strike} · Call {gexCr(r.call)} · Put {gexCr(r.put)} · Net {gexCr(r.net)} · Gross {gexCr(r.gross)}</title>
      {(rows.length<=21||r.strike%100===0)&&<text x={x(r.strike)} y={B+24} className="gx-label" textAnchor="middle">{r.strike}</text>}
    </g>)}
    {layout!=="legs"&&layout!=="net"&&<polyline points={rows.map(r=>`${x(r.strike)},${y(r.gross)}`).join(" ")} stroke="#46a7ff" strokeWidth="2" fill="none"/>}
    {p.spot>=minK&&p.spot<=maxK&&<g><line x1={x(p.spot)} x2={x(p.spot)} y1={T} y2={B} stroke="#e2e8f0" strokeDasharray="3 4"/><text x={x(p.spot)} y="20" className="gx-label" textAnchor="middle">Spot {price(p.spot)}</text></g>}
    {walls&&[p.call_wall,p.put_wall].map((k,i)=>k>=minK&&k<=maxK&&<g key={i}><line x1={x(k)} x2={x(k)} y1={T+20} y2={B} stroke={i?"#f9ae52":"#a9a4ff"} strokeDasharray="6 4"/><text x={x(k)} y={B+44} textAnchor="middle" className="gx-label">{i?"PE γ wall":"CE γ wall"}</text></g>)}
    {flip&&p.model_flip!=null&&p.model_flip>=minK&&p.model_flip<=maxK&&<line x1={x(p.model_flip)} x2={x(p.model_flip)} y1={T} y2={B} stroke="#ffd166" strokeWidth="2"><title>Fixed-IV repriced model zero {price(p.model_flip)}</title></line>}
    <text x="16" y="18" className="gx-label">₹ / 1% move</text>
  </svg>;
}
function HistoryChart({data,walls,flip,tf}:{data:GammaView;walls:boolean;flip:boolean;tf:string}){
  const rows=data.history;if(!rows?.length)return <div className="gx-empty">No recorded history</div>;
  const first=rows[0].ts,last=rows[rows.length-1].ts;
  const W=1080,L=95,R=965,H=410;
  const x=(t:number)=>L+(t-first)/Math.max(1,last-first)*(R-L);
  const vals=rows.filter(r=>r.ok).flatMap(r=>[r.spot,...(walls?[r.call_wall,r.put_wall]:[]),...(flip&&r.model_flip!=null?[r.model_flip]:[])]);
  if(!vals.length)return <div className="gx-empty">No valid gamma/price observations</div>;
  const lo=Math.min(...vals),hi=Math.max(...vals);
  const y=(v:number)=>175-(v-lo)/Math.max(1,hi-lo)*140;
  const maxNet=Math.max(1,...rows.filter(r=>r.ok).map(r=>Math.abs(r.net))),maxGross=Math.max(1,...rows.filter(r=>r.ok).map(r=>r.gross));
  const ny=(v:number)=>315-v/maxNet*65,gy=(v:number)=>385-v/maxGross*130;
  const sampled=(key:keyof GPoint,project:(v:number)=>number):Array<[number,number]|null>=>rows.flatMap((r,i)=>{
    const value=r[key];
    const point:[number,number]|null=r.ok&&typeof value==='number'?[x(r.ts),project(value)]:null;
    return i>0&&r.ts-rows[i-1].ts>TF_SECONDS_FRONT[tf]*2?[null,point]:[point];
  });
  const lines: [keyof GPoint,string][]=[["spot","#cbd5e1"],...(walls?[["call_wall","#b1a5ff"],["put_wall","#fbbf66"]] as [keyof GPoint,string][]:[]),...(flip?[["model_flip","#f4db65"]] as [keyof GPoint,string][]:[])];
  return <svg className="gx-svg" viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Recorded gamma, spot and wall history">
    {[0,.5,1].map(f=><g key={f}><line x1={L} x2={R} y1={y(lo+f*(hi-lo))} y2={y(lo+f*(hi-lo))} className="gx-grid"/><text x="85" y={y(lo+f*(hi-lo))+4} textAnchor="end" className="gx-label">{price(lo+f*(hi-lo))}</text></g>)}
    {lines.map(([key,color])=>segments(sampled(key,y)).map((pts,i)=><polyline key={key+String(i)} points={pts} fill="none" stroke={color} strokeWidth="1.8"/>))}
    <text x="14" y="18" className="gx-label">NIFTY</text><text x={L} y="210" className="gx-label">Net (left scale) · blue gross (right scale), ₹ per 1% move</text>
    <line x1={L} x2={R} y1="315" y2="315" className="gx-grid"/>
    {[-1,0,1].map(f=><g key={f}><text x="85" y={ny(maxNet*f)+4} textAnchor="end" className="gx-label">{gexCr(maxNet*f)}</text><text x="980" y={gy(maxGross*(f+1)/2)+4} className="gx-label">{gexCr(maxGross*(f+1)/2)}</text></g>)}
    {segments(sampled("net",ny)).map((pts,i)=><polyline key={'n'+i} points={pts} fill="none" stroke="#ff8a95" strokeWidth="2"/>)}
    {segments(sampled("gross",gy)).map((pts,i)=><polyline key={'g'+i} points={pts} fill="none" stroke="#46a7ff" strokeWidth="2"/>)}
    {rows.filter((_,i)=>i===0||i===rows.length-1||i%Math.max(1,Math.ceil(rows.length/6))===0).map(r=><text key={r.ts} x={x(r.ts)} y="403" className="gx-label" textAnchor="middle">{time(r.ts).slice(0,5)}</text>)}
    {rows.map(r=><circle key={r.ts} cx={x(r.ts)} cy={r.ok?ny(r.net):315} r="4" fill="transparent"><title>{time(r.ts)} · spot {price(r.spot)} · net {gexCr(r.net)} · gross {gexCr(r.gross)} · {r.ok?r.regime:r.message}</title></circle>)}
  </svg>;
}
const TF_SECONDS_FRONT:Record<string,number>={"1m":60,"3m":180,"5m":300,"15m":900};
export function ContextTable({rows}:{rows:GammaContext[]}){return <div className="gx-table-wrap"><table className="gx-table"><thead><tr><th>BOS closed · IST</th><th>Direction / level</th><th>Candle / cumulative RVOL</th><th>Gamma as of close</th><th>Explanatory context</th></tr></thead><tbody>{[...rows].reverse().map(r=><tr key={r.bar_ts}><td>{time(r.known_at)}<small>bar starts {time(r.bar_ts)}</small></td><td>{r.direction.toUpperCase()} · {price(r.level)}<small>{r.displacement?"Displacement":"No displacement flag"}</small><small>Pre-candle γ wall {price(r.prior_gamma_wall)}{r.prior_gamma_wall_ts?` · ${time(r.prior_gamma_wall_ts)}`:""}</small></td><td>{num(r.slot_rvol)}× / {num(r.cumulative_rvol)}×<small>{r.baseline?"Existing RVOL thresholds pass":"Not RVOL-confirmed"}{r.big_activity?" · big activity (same input)":""}</small></td><td><span className={`gx-badge ${r.gamma_regime}`}>{r.gamma_regime}</span><small>{r.gamma_ts?`${time(r.gamma_ts)} · ${Math.round(r.gamma_age_seconds??0)}s old`:"No valid snapshot"}</small></td><td>{r.context}<small>{r.eligible?"Timestamp/coverage gate passed":"Data gate incomplete — no combined confirmation"}</small></td></tr>)}</tbody></table>{!rows.length&&<div className="gx-empty">No closed BOS events in the selected recorded window.</div>}</div>;}
export function gammaAlertWindow(ts:number, tf:string, basis:"close"|"bar_start") {
  const seconds=TF_SECONDS_FRONT[tf];
  if(!Number.isFinite(ts)||ts<=0||!seconds)return null;
  return basis==="bar_start"?{start:ts,close:ts+seconds}:{start:ts-seconds,close:ts};
}
export function GammaAlertContext({ts,tf}:{ts:number;tf:string}){
  const [open,setOpen]=useState(false),[text,setText]=useState(""),[busy,setBusy]=useState(false);
  const [basis,setBasis]=useState<"close"|"bar_start">("close");
  const request=useRef(0);
  async function load(chosen=basis){
    const id=++request.current;
    setOpen(true);setBusy(true);setText("");
    try{
      const window=gammaAlertWindow(ts,tf,chosen);
      if(!window)throw new Error('Unsupported alert timeframe or timestamp; no alignment was assumed');
      const day=new Date(window.close*1000).toLocaleDateString('en-CA',{timeZone:'Asia/Kolkata'});
      const q=new URLSearchParams({day,tf,end_ts:String(window.close)});
      const res=await fetch('/api/gamma-exposure-review?'+q);
      if(!res.ok)throw new Error(`HTTP ${res.status}`);
      const d:GammaView=await res.json();
      if(!d.ok)throw new Error(d.message||'Gamma context unavailable');
      // Require both endpoints; matching a convenient neighbouring BOS is not valid.
      const r=d.contexts.find(r=>r.known_at===window.close&&r.bar_ts===window.start);
      if(id===request.current)setText(r?`${r.context} Gamma input ${r.gamma_ts?time(r.gamma_ts):'unavailable'}. ${r.eligible?'Quality gate passed.':'Quality gate incomplete.'}`:'No exact BOS interval match. The alert may use a different timestamp convention or structure rule; no gamma context was assigned.');
    }catch(e){if(id===request.current)setText(e instanceof Error?e.message:'Gamma context unavailable');}
    finally{if(id===request.current)setBusy(false);}
  }
  return <div className="gx-alert-context"><button className="pill text-xs text-violet-300" onClick={()=>open?setOpen(false):load()}>{open?'Hide gamma context':'Inspect gamma context (recorded)'}</button>{open&&<><label className="text-xs text-slate-400 ml-2">Alert timestamp means <select aria-label="BOS alert timestamp basis" className="bg-ink-900 text-slate-200 rounded p-1" value={basis} onChange={e=>{const value=e.target.value as "close"|"bar_start";setBasis(value);load(value);}}><option value="close">Candle close</option><option value="bar_start">Candle start</option></select></label><p className="text-xs text-slate-300 mt-2">{busy?'Loading timestamp-aligned context…':text}</p><p className="text-xs text-slate-400">Explicit timestamp interpretation only. No automatic timezone correction or neighbouring-event substitution.</p></>}</div>;
}
export default function GammaExposureReview(){
  const [data,setData]=useState<GammaView|null>(null),[error,setError]=useState(''),[busy,setBusy]=useState(false),[responseKey,setResponseKey]=useState('');
  const [day,setDay]=useState(''),[expiry,setExpiry]=useState(''),[tf,setTf]=useState(POLICY.default_tf),[sign,setSign]=useState(POLICY.dealer_sign),[end,setEnd]=useState<number|null>(null);
  const [tab,setTab]=useState('latest'),[layout,setLayout]=useState('strike'),[range,setRange]=useState(String(POLICY.display_strike_steps)),[walls,setWalls]=useState(true),[flip,setFlip]=useState(false),[fullscreen,setFullscreen]=useState(false),[selected,setSelected]=useState<GStrike|null>(null);
  const [study,setStudy]=useState<GStudy|null>(null),[studyBusy,setStudyBusy]=useState(false),[studyError,setStudyError]=useState('');
  const scopeKey=JSON.stringify([day,expiry,tf,sign,end]);
  const waiting=responseKey!==scopeKey;
  const seq=useRef(0);
  useEffect(()=>{let active=true;const abort=new AbortController();const request=async()=>{const id=++seq.current;setBusy(true);setError('');try{const q=new URLSearchParams({tf,sign:String(sign)});if(day)q.set('day',day);if(expiry)q.set('expiry',expiry);if(end!==null)q.set('end_ts',String(end));const res=await fetch('/api/gamma-exposure-review?'+q,{signal:abort.signal});if(!res.ok)throw new Error(`Gamma API HTTP ${res.status}; restart backend if this is an older build`);const d:GammaView=await res.json();if(active&&id===seq.current){if(!d.ok)throw new Error(d.message||'No gamma inputs');setData(d);setResponseKey(JSON.stringify([day,expiry,tf,sign,end]));setSelected(null);}}catch(e){if(active&&id===seq.current)setError(e instanceof Error?e.message:'Gamma API unavailable');}finally{if(active&&id===seq.current)setBusy(false);}};const delayed=window.setTimeout(request,end!==null?POLICY.replay_debounce_ms:0);const timer=end===null?window.setInterval(request,POLICY.poll_seconds*1000):undefined;return()=>{active=false;window.clearTimeout(delayed);abort.abort();if(timer)window.clearInterval(timer);};},[day,expiry,tf,sign,end]);
  useEffect(()=>{const escape=(e:KeyboardEvent)=>{if(e.key==='Escape')setFullscreen(false);};window.addEventListener('keydown',escape);return()=>window.removeEventListener('keydown',escape);},[]);
  async function runStudy(){setStudyBusy(true);setStudyError('');try{const res=await fetch('/api/gamma-exposure-review?study=true');if(!res.ok)throw new Error(`HTTP ${res.status}`);const d:GStudy=await res.json();if(!d.ok)throw new Error(d.message||'Study unavailable');setStudy(d);}catch(e){setStudyError(e instanceof Error?e.message:'Replay failed');}finally{setStudyBusy(false);}}
  const p=data?.selected;
  const age=p?Math.max(0,Math.floor(Date.now()/1000-p.ts)):null;
  const timeline=data?.timeline||[],idx=end===null?timeline.length-1:Math.max(0,timeline.findIndex(t=>t>=end));
  return <section className={`gx-root ${fullscreen?'gx-full':''}`} aria-label="Gamma Exposure Review">
    <header className="gx-header"><div><h2>◈ GAMMA EXPOSURE <span>MODEL • CONTEXT • EVIDENCE</span></h2><p>Separate research view · existing gamma panels and BOS alerts remain unchanged</p></div><button onClick={()=>setFullscreen(v=>!v)}>{fullscreen?'Exit fullscreen':'⛶ Enlarge'}</button></header>
    <div className="gx-controls"><label>Session <select aria-label="Gamma session" value={day||data?.day||''} onChange={e=>{setDay(e.target.value);setExpiry('');setEnd(null);}}>{(data?.days||[]).map(d=><option key={d}>{d}</option>)}</select></label><label>Expiry <select aria-label="Gamma expiry" value={expiry||data?.expiry||''} onChange={e=>{setExpiry(e.target.value);setEnd(null);}}>{(data?.expiries||[]).map(e=><option key={e}>{e}</option>)}</select></label><label>Timeframe <select aria-label="Gamma timeframe" value={tf} onChange={e=>setTf(e.target.value)}>{POLICY.timeframes.map(t=><option key={t}>{t}</option>)}</select></label><label>Assumed dealer sign <select aria-label="Gamma sign convention" value={sign} onChange={e=>setSign(Number(e.target.value))}><option value={1}>+ Calls − Puts</option><option value={-1}>− Calls + Puts</option></select></label><span className="gx-muted">{busy?'Refreshing…':p?`${time(p.ts)} IST · ${age!>86400?`${Math.floor(age!/86400)}d old`:`${Math.floor(age!/60)}m old`} · recorded inputs`:"Loading recorded inputs…"}</span></div>
    {error&&<div className="gx-warning" role="alert">{error}. No stale selection is being displayed.</div>}
    {p&&!waiting&&!error&&<div className="gx-metrics"><div><small>MODELED NET · ₹ / 1% move</small><b className={p.net>=0?'gx-pos':'gx-neg'}>{gexCr(p.net)}</b><span>{p.ok?`${(p.balance*100).toFixed(1)}% of gross`:'Unavailable'}</span></div><div><small>GROSS · CE + PE</small><b className="gx-blue">{gexCr(p.gross)}</b><span>Not absolute value of net</span></div><div><small>MODEL CONTEXT</small><b className={`gx-badge ${p.regime}`}>{p.ok?p.regime:'unavailable'}</b><span>Not bullish / bearish</span></div><div><small>NIFTY / VALID STRIKES</small><b>{price(p.spot)}</b><span>{p.valid_strikes??0}/{p.total_strikes??0} · {((p.coverage??0)*100).toFixed(0)}% coverage</span></div></div>}
    <nav className="gx-tabs">{[['latest','Strike profile'],['intraday','Intraday'],['context','BOS + RVOL context'],['study','Replay comparison']].map(([key,title])=><button key={key} aria-pressed={tab===key} onClick={()=>setTab(key)}>{title}</button>)}</nav>
    {!data||waiting||!!error?<div className="gx-empty">{busy?'Reading local snapshots and matching closed candles…':'No gamma data'}</div>:<>
      {(tab==='latest'||tab==='intraday')&&<><div className="gx-chart-tools">{tab==='latest'&&<><select aria-label="Gamma chart orientation" value={layout} onChange={e=>setLayout(e.target.value)}><option value="strike">Strike X · net + gross</option><option value="net">Net only · expanded scale</option><option value="ladder">Strike Y · exposure ladder</option><option value="legs">Separate Call / Put exposure</option></select><label>ATM ± <select aria-label="Gamma strike range" value={range} onChange={e=>setRange(e.target.value)}>{POLICY.display_ranges.map(n=><option key={n}>{n}</option>)}<option value="all">all</option></select></label></>}<label><input type="checkbox" checked={walls} onChange={e=>setWalls(e.target.checked)}/> Gamma-weighted walls</label><label><input type="checkbox" checked={flip} onChange={e=>setFlip(e.target.checked)}/> Fixed-IV model zero</label><span>{tab==='latest'&&layout==='legs'?'Call amber / Put cyan · unsigned exposure':'Net bars + green / − red · gross blue'} · CE wall violet / PE wall amber · lines connect recorded samples</span></div>
      <div className="gx-chart">{tab==='latest'?<ProfileChart data={data} layout={layout} range={range} walls={walls} flip={flip} onSelect={setSelected} selected={selected?.strike??null}/>:<HistoryChart data={data} walls={walls} flip={flip} tf={tf}/>}</div>
      {selected&&tab==='latest'&&<div className="gx-selection"><b>Strike {price(selected.strike)}</b><span>Call {gexCr(selected.call)}</span><span>Put {gexCr(selected.put)}</span><span>Net {gexCr(selected.net)}</span><span>Gross {gexCr(selected.gross)}</span></div>}
      <div className="gx-replay"><button onClick={()=>setEnd(timeline[Math.max(0,idx-1)]??null)}>←</button><input aria-label="Gamma recorded snapshot time" type="range" min={0} max={Math.max(0,timeline.length-1)} value={Math.max(0,idx)} onChange={e=>setEnd(timeline[Number(e.target.value)])}/><button onClick={()=>setEnd(timeline[Math.min(timeline.length-1,idx+1)]??null)}>→</button><button onClick={()=>setEnd(null)}>Latest available</button><b>{timeline[idx]?time(timeline[idx]):'—'} IST {end!==null?'· REPLAY':''}</b></div></>}
      {tab==='context'&&<><div className="gx-warning">Context only. Direction comes from the closed BOS; volume measures participation. Big-activity candles reuse RVOL and do not add an independent vote.</div><ContextTable rows={data.contexts}/></>}
      {tab==='study'&&<div className="gx-study"><h3>Pre-specified test · {POLICY.study.id}</h3><p>Baseline: closed 5m BOS with existing candle + cumulative RVOL thresholds. One variant: keep negative modeled gamma (default sign +CE−PE, net/gross &lt; −5%). Entry next minute open; outcome after 30 minutes. One common data gate and baseline spacing for both arms. The original development/holdout dates stay fixed as new sessions arrive. This test ignores the exploratory sign selector above.</p><button onClick={runStudy} disabled={studyBusy}>{studyBusy?'Running chronological replay…':'Run recorded replay comparison'}</button>{studyError&&<div className="gx-warning">{studyError}</div>}{study&&<><div className="gx-warning">{study.verdict} {study.sufficient_sample?'':'Sample-size requirement not met.'}</div><p>{study.sessions_available} available sessions · {study.train_days.length} development / {study.holdout_days.length} holdout · {study.event_count} eligible non-overlapping events · {study.specification.variant_count} pre-specified variant</p><table className="gx-table"><thead><tr><th>Slice</th><th>BOS + RVOL</th><th>+ negative gamma</th><th>Mean difference · index points</th><th>Session-bootstrap interval</th></tr></thead><tbody>{[['Development',study.train],['Holdout',study.holdout],['All · descriptive',study.all]].map(([title,a])=>{const arm=a as Arm;return <tr key={String(title)}><td>{String(title)}</td><td>{arm.baseline.events} events · {num(arm.baseline.mean_points)} pts</td><td>{arm.gamma_filtered.events} events · {num(arm.gamma_filtered.mean_points)} pts</td><td>{num(arm.difference_points)}</td><td>{arm.session_bootstrap_interval?.map(num).join(' to ')??'Unavailable'}</td></tr>;})}</tbody></table><p>Exclusions: {Object.entries(study.excluded).map(([k,v])=>`${k}: ${v}`).join(' · ')||'none'}</p>{study.caveats.map(c=><p key={c}>• {c}</p>)}</>}</div>}
      <div className="gx-levels"><span>CE γ wall <b>{price(p?.call_wall)}</b></span><span>PE γ wall <b>{price(p?.put_wall)}</b></span><span>Gross concentration <b>{price(p?.concentration)}</b></span><span>Repriced model zero <b>{price(p?.model_flip)}</b></span><span>Cumulative strike cross <b>{price(p?.strike_cross)}</b></span></div>
      <details className="gx-method"><summary>Method, sign reconciliation and limitations</summary><p>{data.method.formula}</p><p>{data.method.unit}. Configured OI unit: {data.method.oi_unit}; lot multiplier applied once. {data.method.scope}. Display range does not change regime totals.</p><p>{data.method.source}. Greeks: {p?.greek_source}.</p>{data.method.sign_reconciliation.map(s=><p key={s}>• {s}</p>)}{data.method.limitations.map(s=><p key={s}>• {s}</p>)}<p>Neutral band: |net/gross| ≤ {(POLICY.neutral_net_to_gross*100).toFixed(0)}%. This modeling threshold is not an established trading edge. No automatic orders or new directional alerts.</p></details>
    </>}
  </section>;
}
