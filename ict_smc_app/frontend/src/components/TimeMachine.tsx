import React, { useEffect, useState } from "react";
import rules from "../../../../analyst/analyst_config.json";
import TimeSeriesChain from "./TimeSeriesChain";
import WriterMap from "./WriterMap";
import GammaExposureReview from "./GammaExposureReview";
import "./timeMachine.css";
const P=rules.time_machine;
const minute=(s:string)=>Number(s.slice(0,2))*60+Number(s.slice(3,5));
const OPEN=minute(P.open),END=minute(P.end);
export const atEpoch=(day:string,m:number)=>Date.parse(`${day}T${String(Math.floor(m/60)).padStart(2,'0')}:${String(m%60).padStart(2,'0')}:00+05:30`)/1000;
const hm=(m:number)=>`${String(Math.floor(m/60)).padStart(2,'0')}:${String(m%60).padStart(2,'0')}`;
const actual=(ts:number|null|undefined)=>ts==null?'No recorded snapshot':new Date(ts*1000).toLocaleTimeString('en-IN',{timeZone:'Asia/Kolkata',hour12:false});
type Availability={available:boolean;actual_ts:number|null;actual_local?:string;first_ts:number|null;last_ts:number|null;count:number};
type Manifest={ok:boolean;message?:string;days:string[];day:string;selected_ts:number;views:Record<string,Availability>};
const VIEWS=[['time_series','Time-series Chain'],['wmap','Writer map / OI'],['gamma_review','Gamma Exposure'],['candles','Candles / OI walls'],['writers','OI · Writers / Trinity'],['bias','Bias meter'],['alerts','Alerts log'],['footprint','Footprint / CVD'],['auction','Auction'],['qml','QML / RBS / SBR'],['crt','CRT cockpit'],['stdv','STD DEV'],['book','Order book'],['advpa','Advanced PA'],['ict_levels','ICT levels'],['paper','Paper trades']];
export default function TimeMachine({children}:{children:React.ReactNode}){
 const [replay,setReplay]=useState(false),[day,setDay]=useState(''),[draft,setDraft]=useState(END),[value,setValue]=useState(END),[view,setView]=useState('wmap');
 const [meta,setMeta]=useState<Manifest|null>(null),[error,setError]=useState(''),[busy,setBusy]=useState(false);
 useEffect(()=>{const id=setTimeout(()=>setValue(draft),P.debounce_ms);return()=>clearTimeout(id);},[draft]);
 useEffect(()=>{const abort=new AbortController();setBusy(true);setError('');const q=new URLSearchParams();if(day){q.set('day',day);q.set('at',String(atEpoch(day,value)));}fetch('/api/desk/time-machine?'+q,{signal:abort.signal}).then(async r=>{if(!r.ok)throw Error(`Time Machine API HTTP ${r.status}; restart the backend if it has not loaded the update`);if(!r.headers.get('content-type')?.includes('application/json'))throw Error('The running backend has not loaded Time Machine. Restart the ICT / SMC backend, then refresh');return r.json();}).then((d:Manifest)=>{if(!d.ok)throw Error(d.message||'No recorded session');setMeta(d);if(!day)setDay(d.day);}).catch(e=>{if(!abort.signal.aborted)setError(String(e.message||e));}).finally(()=>{if(!abort.signal.aborted)setBusy(false);});return()=>abort.abort();},[day,value]);
 const target=day?atEpoch(day,value):null;
 const ready=draft===value&&!!meta&&meta.day===day&&meta.selected_ts===target&&!busy&&!error;
 const source=ready?meta?.views[view]:null;
 const supported=['time_series','wmap','gamma_review'].includes(view);
 const at=source?.actual_ts;
 return <><section className="tm-toolbar" aria-label="Shared Time Machine">
  <div className="tm-title"><b>◷ TIME MACHINE</b><button className={!replay?'on':''} onClick={()=>setReplay(false)}>Live desk</button><button className={replay?'on':''} onClick={()=>setReplay(true)}>Replay recorded session</button><span>{replay?'Historical view • the live desk and its existing monitoring are not paused':'Live views use their existing calculations and controls'}</span></div>
  {replay&&<><div className="tm-clock"><label>Session <select aria-label="Time Machine session" value={day} onChange={e=>setDay(e.target.value)}>{(meta?.days||[]).map(d=><option key={d}>{d}</option>)}</select></label><button onClick={()=>setDraft(OPEN)}>⏮ 09:15</button><button aria-label="Previous replay minute" onClick={()=>setDraft(v=>Math.max(OPEN,v-P.step_minutes))}>−1m</button><input aria-label="Shared session time" type="range" min={OPEN} max={END} step={P.step_minutes} value={draft} onChange={e=>setDraft(Number(e.target.value))}/><button aria-label="Next replay minute" onClick={()=>setDraft(v=>Math.min(END,v+P.step_minutes))}>+1m</button><button onClick={()=>setDraft(END)}>15:40 ⏭</button><label>Target <input aria-label="Exact replay time" type="time" onInput={e=>{const text=e.currentTarget.value;if(text)setDraft(Math.min(END,Math.max(OPEN,minute(text))));}} min={P.open} max={P.end} value={hm(draft)} onChange={e=>{if(e.target.value)setDraft(Math.min(END,Math.max(OPEN,minute(e.target.value))));}}/></label><b>{day} {hm(draft)} IST</b></div><p>Target time is not a fabricated snapshot. Each view uses its latest recorded input at or before the target; 15:40 does not imply data exists through 15:40. {busy?'Finding recorded inputs…':''}</p></>}
 </section>
 <div hidden={replay} aria-hidden={replay}>{children}</div>
 {replay&&<main className="tm-workspace"><nav className="tm-tabs">{VIEWS.map(([key,title])=><button key={key} aria-pressed={view===key} onClick={()=>setView(key)}>{title}{!['time_series','wmap','gamma_review'].includes(key)&&<small>not connected</small>}</button>)}</nav>
  <div className="tm-banner"><b>REPLAY — {day} {hm(value)} IST</b><span>{supported?(!ready?"Loading selection…":`Archive available through: ${actual(source?.actual_ts)} IST; view may use earlier inputs`):'This tab is not connected to historical replay'}</span><button onClick={()=>setReplay(false)}>Return to live desk</button></div>
  {error?<div role="alert" className="tm-empty">{error}. Your live desk remains available.</div>:!supported?<div className="tm-empty"><h3>{VIEWS.find(v=>v[0]===view)?.[1]} — historical replay not connected</h3><p>No current values are being presented as past data.</p><p>This panel needs a dedicated historical connection before it can follow the shared clock. Some inputs may not have been recorded.</p><p>The shared clock is ready. Time-series Chain, Writer map and Gamma Exposure are connected now.</p></div>:!ready?<div className="tm-empty">Loading the selected historical time…</div>:!source?.available||at==null?<div className="tm-empty">No recorded {VIEWS.find(v=>v[0]===view)?.[1]} input existed by {hm(value)} on {day}.<p>First available: {actual(source?.first_ts)}. Nothing from a later time has been substituted.</p></div>:<div className="tm-surface" key={`${view}:${day}:${target}`}>
    {view==='wmap'&&<WriterMap replayAt={{day,ts:target!}}/>}
    {view==='time_series'&&<TimeSeriesChain replayAt={{day,end:source.actual_local!}}/>}
    {view==='gamma_review'&&<GammaExposureReview replayAt={{day,ts:target!}}/>}
  </div>}
 </main>}
 </>;
}
