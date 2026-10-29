// Separate 8501 parity view. Calculations and HTML come from the original Python engines.
import { useEffect, useMemo, useRef, useState } from "react";
import analystConfig from "../../../../analyst/analyst_config.json";
import { api } from "../api";
import { DEFAULT_GROUPS, VOICE_POLICY, chooseVoice, localEpoch, planSeriesAlerts, selectedReplayAlert, type AlertGroups, type LevelSignal, type SeriesAlert, type SeriesCursor } from "./timeSeriesAlerts";
const UI_POLICY=analystConfig.time_series_chain;

type Settings={full:boolean;day:boolean;pct:boolean;lot:boolean;hl:boolean;font:string};
type Controls={day:string;expiry:string;tf:string;mode:string;columns:number;range_n:number;selection:string;custom:number[];replay:boolean;end:string;signals:boolean;auto:boolean;settings:Settings};
type Policy={refresh_seconds:number;timeframes:string[];columns:number[];ranges:number[];default_tf:string;default_columns:number;default_range:number};
type Result={ok:boolean;message?:string;sessions?:string[];expiries?:string[];expiry_labels?:Record<string,string>;expiry?:string;day?:string;times?:string[];candidates?:number[];default_custom?:number[];atm?:number;html?:string;live?:boolean;spot?:number;future?:number;partial?:boolean;elapsed?:number;step_min?:number;now?:string;unit?:string;policy?:Policy;tf?:string;end?:string;alert_signals?:LevelSignal[];signals?:LevelSignal[];grid?:{cols:string[]}};
const KEY="ict_time_series_chain_v1";
const initial:Controls={day:"",expiry:"",tf:UI_POLICY.default_tf,mode:"OI Change",columns:UI_POLICY.default_columns,range_n:UI_POLICY.default_range,selection:"Range",custom:[],replay:false,end:"",signals:true,auto:true,settings:{full:false,day:false,pct:false,lot:false,hl:true,font:"Small"}};
const label="flex flex-col gap-1 text-[11px] text-slate-400";
const select="rounded-md border border-slate-600 bg-[#142034] px-3 py-2 text-sm text-slate-100";
export default function TimeSeriesChain({active=true}:{active?:boolean}){
  const [controls,setControls]=useState<Controls>(()=>{try{const v=JSON.parse(localStorage.getItem(KEY)||"{}");return {...initial,...v,settings:{...initial.settings,...v.settings}};}catch{return initial;}});
  const [data,setData]=useState<Result|null>(null),[busy,setBusy]=useState(true),[error,setError]=useState(""),[refresh,setRefresh]=useState(0),[height,setHeight]=useState(1000),[expanded,setExpanded]=useState(false);
  const [voicePrefs,setVoicePrefs]=useState<{enabled:boolean;voiceURI:string;groups:AlertGroups}>(()=>{try{const p=JSON.parse(localStorage.getItem("ict_ts_voice_v1")||"{}");return {enabled:!!p.enabled,voiceURI:p.voiceURI||"",groups:{...DEFAULT_GROUPS,...p.groups}};}catch{return {enabled:false,voiceURI:"",groups:DEFAULT_GROUPS};}});
  const [voices,setVoices]=useState<SpeechSynthesisVoice[]>([]),[lastSpoken,setLastSpoken]=useState<SeriesAlert|null>(null),[voiceStatus,setVoiceStatus]=useState("");
  const cursor=useRef<SeriesCursor|null>(null);
  const alerted=useRef<Set<string>>((()=>{try{return new Set<string>(JSON.parse(sessionStorage.getItem("ict_ts_voice_seen")||"[]"));}catch{return new Set<string>();}})());
  const speechSupported=typeof window!=="undefined"&&"speechSynthesis" in window;
  const frame=useRef<HTMLIFrameElement>(null);
  const patch=(v:Partial<Controls>)=>setControls(c=>({...c,...v}));
  useEffect(()=>{try{localStorage.setItem(KEY,JSON.stringify(controls));}catch{/* storage optional */}},[controls]);
  useEffect(()=>{
    if(!active&&!voicePrefs.enabled)return;
    const abort=new AbortController();setBusy(true);setError("");
    const q=new URLSearchParams();
    for(const k of ["day","expiry","tf","mode","columns","range_n","selection","signals"] as const)if(controls[k]!=="")q.set(k,String(controls[k]));
    q.set("custom",controls.custom.join(","));q.set("settings",JSON.stringify(controls.settings));
    if(controls.replay&&controls.end)q.set("end",controls.end);
    const timer=window.setTimeout(()=>{fetch("/api/time-series-chain?"+q,{signal:abort.signal}).then(async r=>{if(!r.ok)throw Error("Time-series API HTTP "+r.status);if(!r.headers.get("content-type")?.includes("application/json"))throw Error("The running 8600 backend has not loaded this endpoint. Restart the ICT / SMC backend, then refresh.");return r.json() as Promise<Result>;}).then(j=>{
      if(abort.signal.aborted)return;setData(j);
    }).catch(e=>{if(!abort.signal.aborted)setError(String(e.message||e));}).finally(()=>{if(!abort.signal.aborted)setBusy(false);});},UI_POLICY.control_debounce_ms);
    return()=>{window.clearTimeout(timer);abort.abort();};
  },[controls,refresh,active,voicePrefs.enabled]);
  useEffect(()=>{
    if((!controls.auto&&!voicePrefs.enabled)||controls.replay||(!active&&!voicePrefs.enabled)||(!data?.live&&!(voicePrefs.enabled&&!controls.day))||!data?.policy)return;
    const id=window.setInterval(()=>setRefresh(v=>v+1),data.policy.refresh_seconds*1000);
    return()=>window.clearInterval(id);
  },[controls.auto,controls.replay,controls.day,data?.live,data?.policy?.refresh_seconds,active,voicePrefs.enabled]);
  useEffect(()=>{try{localStorage.setItem("ict_ts_voice_v1",JSON.stringify(voicePrefs));}catch{/* optional storage */}},[voicePrefs]);
  useEffect(()=>{
    if(!speechSupported)return;
    const update=()=>setVoices(window.speechSynthesis.getVoices());
    update();window.speechSynthesis.addEventListener("voiceschanged",update);
    return()=>window.speechSynthesis.removeEventListener("voiceschanged",update);
  },[speechSupported]);
  const speak=(text:string)=>{
    if(!speechSupported){setVoiceStatus("Speech is unavailable in this browser.");return;}
    const utterance=new SpeechSynthesisUtterance(text),voice=chooseVoice(voices,voicePrefs.voiceURI);
    if(voice)utterance.voice=voice;
    utterance.lang=voice?.lang??"en-IN";utterance.rate=VOICE_POLICY.speech_rate;
    utterance.onerror=()=>setVoiceStatus("Browser blocked speech. Click Test voice to enable audio.");
    window.speechSynthesis.resume();window.speechSynthesis.speak(utterance);
  };
  useEffect(()=>{cursor.current=null;},[voicePrefs.enabled]);
  useEffect(()=>{
    if(!voicePrefs.enabled||controls.replay||!data){cursor.current=null;return;}
    const planned=planSeriesAlerts(data,voicePrefs.groups,cursor.current);
    cursor.current=planned.cursor;
    for(const event of planned.alerts){
      if(alerted.current.has(event.key))continue;
      alerted.current.add(event.key);
      try{sessionStorage.setItem("ict_ts_voice_seen",JSON.stringify([...alerted.current].slice(-VOICE_POLICY.remembered_alerts)));}catch{/* optional storage */}
      setLastSpoken(event);speak(event.speech);
      api.postAlert({key:event.key,ts:localEpoch(event.time),tf:data.tf,kind:"SIG_TS_"+event.tag.toUpperCase(),grade:"info",side:event.side,title:event.title,body:event.body,price:event.price})
        .catch(()=>setVoiceStatus("Voice delivered; alert-log save failed."));
    }
  },[data,voicePrefs.enabled,voicePrefs.groups,controls.replay]);
  useEffect(()=>{
    const resize=(e:MessageEvent)=>{if(e.source===frame.current?.contentWindow&&e.data?.type==="time-series-height"&&Number.isFinite(e.data.height))setHeight(Math.min(20000,Math.max(300,e.data.height)));};
    window.addEventListener("message",resize);return()=>window.removeEventListener("message",resize);
  },[]);
  useEffect(()=>{
    if(!expanded)return;
    const close=(e:KeyboardEvent)=>{if(e.key==="Escape")setExpanded(false);};
    window.addEventListener("keydown",close);return()=>window.removeEventListener("keydown",close);
  },[expanded]);
  const src=useMemo(()=>data?.ok&&data.html?"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><style>html{color-scheme:dark}body{margin:0;padding:8px;color:#e2e8f0;background:#071523;font:14px Inter,system-ui,sans-serif}table.narrative{width:100%;border-collapse:collapse;text-align:left;font-size:13px}.narrative th,.narrative td{padding:6px 8px;border:1px solid #26344a}.narrative th{color:#94a3b8}summary{cursor:pointer;padding:12px;border:1px solid #334155;border-radius:6px}details li{margin:8px 0}table.tsc-g{min-width:"+((data.grid?.cols.length??controls.columns)*2*80+80)+"px}table.tsc-g td:hover{outline:2px solid #facc15;outline-offset:-2px}</style></head><body>"+data.html+"<script>const send=()=>parent.postMessage({type:'time-series-height',height:Math.ceil(document.body.getBoundingClientRect().height)+24},'*');new ResizeObserver(send).observe(document.body);addEventListener('load',send);send();</script></body></html>":"",[data]);
  const policy=data?.policy;
  const times=data?.times??[];
  const endpoint=controls.replay?(controls.end||times.at(-1)||""):(times.at(-1)||"");
  const replayIndex=Math.max(0,times.indexOf(endpoint));
  const dateName=(d:string)=>new Date(d+"T12:00:00Z").toLocaleDateString("en-IN",{weekday:"short",day:"2-digit",month:"short",year:"numeric",timeZone:"Asia/Kolkata"});
  const moveTimeline=(offset:number)=>{
    if(!times.length)return;
    const at=Date.parse(endpoint),minutes=UI_POLICY.timeframes.includes(controls.tf)?(controls.tf==="1h"?60:Number.parseInt(controls.tf)):3;
    const target=at+offset*minutes*60000;
    let index=times.reduce((best,t,i)=>Date.parse(t)<=target?i:best,0);
    if(offset>0)index=Math.max(index,Math.min(times.length-1,replayIndex+1));
    patch({replay:true,end:times[index]});
  };
  const readReplay=()=>{
    if(busy||!data||!controls.replay||(controls.end&&data.end!==controls.end)){setVoiceStatus("Wait for the selected replay time to finish loading.");return;}
    const event=data?selectedReplayAlert(data,voicePrefs.groups):null;
    if(!event){setVoiceStatus("No selected significant closed-bucket signal at or before this replay time.");return;}
    setLastSpoken(event);speak(event.speech);
  };
  const warning=error||(!data?.ok?data?.message:"");
  return <section aria-label="Time-series Chain workspace" role={expanded?"dialog":"region"} aria-modal={expanded?true:undefined} className={expanded?"fixed inset-2 z-50 overflow-auto rounded-xl border border-cyan-700 bg-[#071523] p-4 shadow-2xl":"h-full overflow-auto bg-[#071523] p-4 text-slate-100"}>
    <header className="mb-4 flex flex-wrap items-start justify-between gap-3">
      <div><h2 className="text-base font-bold text-cyan-300">⏱ Time-series Chain</h2><p className="text-xs text-slate-400">8501 parity · mirrored Call / Put buckets · original level-signal and Pro Read engines</p></div>
      <div className="flex gap-2"><button className="pill" onClick={()=>setRefresh(v=>v+1)}>↻ Refresh</button><button className="pill" onClick={()=>setExpanded(v=>!v)}>{expanded?"Close full view":"Full view"}</button></div>
    </header>
    <div className="mb-3 flex flex-wrap gap-3 rounded-lg border border-slate-700 bg-[#0b192b] p-3">
      <label className={label}>Session<select className={select} value={controls.day||data?.day||""} onChange={e=>patch({day:e.target.value,expiry:"",end:"",custom:[]})}>{(data?.sessions??[]).map(d=><option key={d} value={d}>{dateName(d)}</option>)}</select></label>
      <label className={label}>Expiry<select className={select} value={controls.expiry||data?.expiry||""} onChange={e=>patch({expiry:e.target.value,end:"",custom:[]})}>{(data?.expiries??[]).map(e=><option key={e} value={e}>{data?.expiry_labels?.[e]??e}</option>)}</select></label>
      <label className={label}>Timeframe<select className={select} value={controls.tf} onChange={e=>patch({tf:e.target.value})}>{(policy?.timeframes??[controls.tf]).map(v=><option key={v}>{v}</option>)}</select></label>
      <label className={label}>Timestamp columns<select className={select} value={controls.columns} onChange={e=>patch({columns:Number(e.target.value)})}>{(policy?.columns??[controls.columns]).map(v=><option key={v} value={v}>{v===0?"All day":v}</option>)}</select></label>
      <label className={label}>Show<select className={select} value={controls.mode} onChange={e=>patch({mode:e.target.value})}>{["OI Change","OI","Price","Volume"].map(v=><option key={v}>{v}</option>)}</select></label>
      <label className={label}>Selection<select className={select} value={controls.selection} onChange={e=>patch({selection:e.target.value,custom:e.target.value==="Custom"?(data?.default_custom??[]):controls.custom})}><option>Range</option><option>Custom</option></select></label>
      <label className={label}>± ATM<select disabled={controls.selection==="Custom"} className={select} value={controls.range_n} onChange={e=>patch({range_n:Number(e.target.value)})}>{(policy?.ranges??[controls.range_n]).map(v=><option key={v}>{v}</option>)}</select></label>
    </div>
    {controls.selection==="Custom"&&<div className="mb-3 flex flex-wrap gap-1 rounded border border-slate-700 p-3" aria-label="Custom strikes">{(data?.candidates??controls.custom).map(k=><button key={k} onClick={()=>patch({custom:controls.custom.includes(k)?controls.custom.filter(v=>v!==k):[...controls.custom,k].sort((a,b)=>a-b)})} className={controls.custom.includes(k)?"pill bg-cyan-800 text-cyan-100":"pill text-slate-400"}>{k}</button>)}</div>}
    <div className="mb-3 flex flex-wrap items-center gap-5 text-sm">
      <label><input type="checkbox" checked={controls.replay} onChange={e=>patch({replay:e.target.checked,end:e.target.checked?(times.at(-1)??""):""})}/> Replay</label>
      <label><input type="checkbox" checked={controls.signals} onChange={e=>patch({signals:e.target.checked})}/> 🎯 Signals</label>
      <label><input type="checkbox" checked={controls.auto} onChange={e=>patch({auto:e.target.checked})}/> Auto-refresh{voicePrefs.enabled&&!controls.auto?" (voice monitor still refreshes)":controls.replay?" (paused in replay)":data?.live?"":" (historical session)"}</label>
      <details className="rounded border border-slate-600 px-3 py-2"><summary className="cursor-pointer">⚙ Settings</summary><div className="flex flex-wrap gap-3 pt-3">{(["full","day","pct","lot","hl"] as const).map(k=><label key={k}><input type="checkbox" checked={controls.settings[k]} onChange={e=>patch({settings:{...controls.settings,[k]:e.target.checked}})}/> {{full:"Full numbers",day:"Day Change",pct:"Day Change %",lot:"Lots",hl:"Highlight High/Low"}[k]}</label>)}<label>Font <select className={select} value={controls.settings.font} onChange={e=>patch({settings:{...controls.settings,font:e.target.value}})}>{["Small","Medium","Large"].map(v=><option key={v}>{v}</option>)}</select></label></div></details>
    </div>
    <div className="mb-3 rounded-lg border border-cyan-800 bg-[#0b192b] p-3" aria-label="Full-session timeline">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2 text-sm text-cyan-200">
        <b>Full-session timeline · {times[0]?.slice(11,16)??"09:15"} → {times.at(-1)?.slice(11,16)??"latest recorded"}</b>
        <span>{controls.replay?"Historical / replay":"Latest available"} · {endpoint.replace("T"," ").slice(0,19)}</span>
      </div>
      <input className="w-full" aria-label="Session timeline" type="range" min={0} max={Math.max(0,times.length-1)} disabled={!times.length} value={replayIndex} onChange={e=>patch({replay:true,end:times[Number(e.target.value)]})}/>
      <div className="mt-2 flex flex-wrap gap-2">
        <button className="pill" disabled={!times.length} onClick={()=>patch({replay:true,end:times[0]})}>⏮ Session start</button>
        <button className="pill" disabled={!times.length} onClick={()=>moveTimeline(-1)}>← Previous bucket</button>
        <button className="pill" disabled={!times.length} onClick={()=>moveTimeline(1)}>Next bucket →</button>
        <button className="pill" onClick={()=>patch({replay:false,end:""})}>Latest available</button>
        <button className="pill" onClick={()=>patch({columns:0,replay:false,end:""})}>Show all day</button>
        <button className="pill" onClick={()=>patch({day:"",expiry:"",replay:false,end:""})}>Latest session</button>
      </div>
      <p className="mt-2 text-xs text-slate-400">Slide through the entire recorded day. Column count controls only the visible window. All day uses horizontal scrolling. Buckets show ending times; 09:15 is the opening baseline. Times without recordings are not invented.</p>
    </div>
    <div className="mb-3 rounded-lg border border-amber-800 bg-[#0b192b] p-3 text-sm" aria-label="Time-series voice alerts">
      <div className="flex flex-wrap items-center gap-3">
        <label><input type="checkbox" aria-label="Live time-series voice alerts" checked={voicePrefs.enabled} disabled={!speechSupported} onChange={e=>{setVoicePrefs(p=>({...p,enabled:e.target.checked}));if(e.target.checked){patch({day:"",expiry:"",replay:false,end:""});speak("Time-series live alerts enabled. Waiting for a new significant closed bucket.");}}}/> Live voice alerts</label>
        <label>Voice <select aria-label="Time-series voice" className={select} value={voicePrefs.voiceURI} onChange={e=>setVoicePrefs(p=>({...p,voiceURI:e.target.value}))}><option value="">Prefer available female English voice</option>{voices.map(v=><option key={v.voiceURI} value={v.voiceURI}>{v.name} ({v.lang})</option>)}</select></label>
        <button className="pill" disabled={!speechSupported} onClick={()=>speak("Voice test. Important time-series breakouts, reversals and false breakout warnings will be announced.")}>Test voice</button>
        <button className="pill" disabled={!controls.replay||!speechSupported||busy} onClick={readReplay}>Read replay signal</button>
      </div>
      <div className="mt-2 flex flex-wrap gap-3">{(["breaks","reversals","traps","builds"] as const).map(group=><label key={group}><input type="checkbox" checked={voicePrefs.groups[group]} onChange={e=>setVoicePrefs(p=>({...p,groups:{...p.groups,[group]:e.target.checked}}))}/> {{breaks:"Breakout / breakdown",reversals:"Support / resistance reversal",traps:"False / failed break",builds:"Support / resistance built (optional)"}[group]}</label>)}</div>
      <p className="mt-2 text-xs text-slate-400">{voicePrefs.enabled?(controls.replay?"Live speech paused during replay.":data?.live?"Watching fresh closed buckets; continues on other dashboard tabs.":controls.day?"Historical session selected — choose Latest session to resume live alerts.":"Waiting for today's recorded session; historical data is not announced live."):"Live speech off."} Minor cell changes are silent. Voice availability depends on the browser; Test voice unlocks audio. Alerts require this dashboard to remain open.</p>
      {lastSpoken&&<p className="mt-2 text-amber-200" role="status">{lastSpoken.title} · {lastSpoken.body}</p>}
      {voiceStatus&&<p className="mt-2 text-amber-300">{voiceStatus}</p>}
    </div>
    {busy?<div role="status" className="rounded border border-cyan-900 p-8 text-center text-cyan-300">Loading original 8501 calculations…</div>:warning?<div role="alert" className="rounded border border-amber-700 p-5 text-amber-200">{warning}<p className="mt-2 text-xs">Choose an available session and expiry, or refresh. The time-series adapter requires a restarted 8600 backend.</p></div>:<>
      <div className="mb-2 flex flex-wrap justify-between gap-2 text-xs text-slate-400"><span>{controls.mode} · {controls.tf} buckets · {controls.mode==="Price"?"price ₹":controls.settings.lot?"lots":"shares"} ({data?.unit}) · {data?.partial?"⏳ newest bucket still running ("+data.elapsed+" of "+data.step_min+" min)":"all buckets closed"}</span><span>{data?.future!=null?"Future "+data.future.toLocaleString("en-IN")+" · ":""}NIFTY {data?.spot?.toLocaleString("en-IN")} · {data?.now?.replace(/\.\d+$/,"")}{controls.replay?" · REPLAY":""}</span></div>
      <iframe ref={frame} title="Time-series Chain grid and original 8501 interpretation" sandbox="allow-scripts" srcDoc={src} className="w-full border-0" style={{height}}/>
    </>}
    <p className="mt-3 text-xs text-slate-500">Information only. Reads are heuristics. The running bucket can change; judge closed buckets. This separate view places no orders.</p>
  </section>;
}
