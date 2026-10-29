// Separate 8501 parity view. Calculations and HTML come from the original Python engines.
import { useEffect, useMemo, useRef, useState } from "react";
import analystConfig from "../../../../analyst/analyst_config.json";
const UI_POLICY=analystConfig.time_series_chain;

type Settings={full:boolean;day:boolean;pct:boolean;lot:boolean;hl:boolean;font:string};
type Controls={day:string;expiry:string;tf:string;mode:string;columns:number;range_n:number;selection:string;custom:number[];replay:boolean;end:string;signals:boolean;auto:boolean;settings:Settings};
type Policy={refresh_seconds:number;timeframes:string[];columns:number[];ranges:number[];default_tf:string;default_columns:number;default_range:number};
type Result={ok:boolean;message?:string;sessions?:string[];expiries?:string[];expiry_labels?:Record<string,string>;expiry?:string;day?:string;times?:string[];candidates?:number[];default_custom?:number[];atm?:number;html?:string;live?:boolean;spot?:number;future?:number;partial?:boolean;elapsed?:number;step_min?:number;now?:string;unit?:string;policy?:Policy};
const KEY="ict_time_series_chain_v1";
const initial:Controls={day:"",expiry:"",tf:UI_POLICY.default_tf,mode:"OI Change",columns:UI_POLICY.default_columns,range_n:UI_POLICY.default_range,selection:"Range",custom:[],replay:false,end:"",signals:true,auto:true,settings:{full:false,day:false,pct:false,lot:false,hl:true,font:"Small"}};
const label="flex flex-col gap-1 text-[11px] text-slate-400";
const select="rounded-md border border-slate-600 bg-[#142034] px-3 py-2 text-sm text-slate-100";
export default function TimeSeriesChain(){
  const [controls,setControls]=useState<Controls>(()=>{try{const v=JSON.parse(localStorage.getItem(KEY)||"{}");return {...initial,...v,settings:{...initial.settings,...v.settings}};}catch{return initial;}});
  const [data,setData]=useState<Result|null>(null),[busy,setBusy]=useState(true),[error,setError]=useState(""),[refresh,setRefresh]=useState(0),[height,setHeight]=useState(1000),[expanded,setExpanded]=useState(false);
  const frame=useRef<HTMLIFrameElement>(null);
  const patch=(v:Partial<Controls>)=>setControls(c=>({...c,...v}));
  useEffect(()=>{try{localStorage.setItem(KEY,JSON.stringify(controls));}catch{/* storage optional */}},[controls]);
  useEffect(()=>{
    const abort=new AbortController();setBusy(true);setError("");
    const q=new URLSearchParams();
    for(const k of ["day","expiry","tf","mode","columns","range_n","selection","signals"] as const)if(controls[k]!=="")q.set(k,String(controls[k]));
    q.set("custom",controls.custom.join(","));q.set("settings",JSON.stringify(controls.settings));
    if(controls.replay&&controls.end)q.set("end",controls.end);
    const timer=window.setTimeout(()=>{fetch("/api/time-series-chain?"+q,{signal:abort.signal}).then(async r=>{if(!r.ok)throw Error("Time-series API HTTP "+r.status);if(!r.headers.get("content-type")?.includes("application/json"))throw Error("The running 8600 backend has not loaded this endpoint. Restart the ICT / SMC backend, then refresh.");return r.json() as Promise<Result>;}).then(j=>{
      if(abort.signal.aborted)return;setData(j);
    }).catch(e=>{if(!abort.signal.aborted)setError(String(e.message||e));}).finally(()=>{if(!abort.signal.aborted)setBusy(false);});},UI_POLICY.control_debounce_ms);
    return()=>{window.clearTimeout(timer);abort.abort();};
  },[controls,refresh]);
  useEffect(()=>{
    if(!controls.auto||controls.replay||!data?.live||!data.policy)return;
    const id=window.setInterval(()=>setRefresh(v=>v+1),data.policy.refresh_seconds*1000);
    return()=>window.clearInterval(id);
  },[controls.auto,controls.replay,data?.live,data?.policy?.refresh_seconds]);
  useEffect(()=>{
    const resize=(e:MessageEvent)=>{if(e.source===frame.current?.contentWindow&&e.data?.type==="time-series-height"&&Number.isFinite(e.data.height))setHeight(Math.min(20000,Math.max(300,e.data.height)));};
    window.addEventListener("message",resize);return()=>window.removeEventListener("message",resize);
  },[]);
  useEffect(()=>{
    if(!expanded)return;
    const close=(e:KeyboardEvent)=>{if(e.key==="Escape")setExpanded(false);};
    window.addEventListener("keydown",close);return()=>window.removeEventListener("keydown",close);
  },[expanded]);
  const src=useMemo(()=>data?.ok&&data.html?"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><style>html{color-scheme:dark}body{margin:0;padding:8px;color:#e2e8f0;background:#071523;font:14px Inter,system-ui,sans-serif}table.narrative{width:100%;border-collapse:collapse;text-align:left;font-size:13px}.narrative th,.narrative td{padding:6px 8px;border:1px solid #26344a}.narrative th{color:#94a3b8}summary{cursor:pointer;padding:12px;border:1px solid #334155;border-radius:6px}details li{margin:8px 0}table.tsc-g td:hover{outline:2px solid #facc15;outline-offset:-2px}</style></head><body>"+data.html+"<script>const send=()=>parent.postMessage({type:'time-series-height',height:Math.ceil(document.body.getBoundingClientRect().height)+24},'*');new ResizeObserver(send).observe(document.body);addEventListener('load',send);send();</script></body></html>":"",[data]);
  const policy=data?.policy;
  const times=data?.times??[];
  const endpoint=controls.end||times.at(-1)||"";
  const replayIndex=Math.max(0,times.indexOf(endpoint));
  const dateName=(d:string)=>new Date(d+"T12:00:00Z").toLocaleDateString("en-IN",{weekday:"short",day:"2-digit",month:"short",year:"numeric",timeZone:"Asia/Kolkata"});
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
      <label className={label}>Timestamp columns<select className={select} value={controls.columns} onChange={e=>patch({columns:Number(e.target.value)})}>{(policy?.columns??[controls.columns]).map(v=><option key={v}>{v}</option>)}</select></label>
      <label className={label}>Show<select className={select} value={controls.mode} onChange={e=>patch({mode:e.target.value})}>{["OI Change","OI","Price","Volume"].map(v=><option key={v}>{v}</option>)}</select></label>
      <label className={label}>Selection<select className={select} value={controls.selection} onChange={e=>patch({selection:e.target.value,custom:e.target.value==="Custom"?(data?.default_custom??[]):controls.custom})}><option>Range</option><option>Custom</option></select></label>
      <label className={label}>± ATM<select disabled={controls.selection==="Custom"} className={select} value={controls.range_n} onChange={e=>patch({range_n:Number(e.target.value)})}>{(policy?.ranges??[controls.range_n]).map(v=><option key={v}>{v}</option>)}</select></label>
    </div>
    {controls.selection==="Custom"&&<div className="mb-3 flex flex-wrap gap-1 rounded border border-slate-700 p-3" aria-label="Custom strikes">{(data?.candidates??controls.custom).map(k=><button key={k} onClick={()=>patch({custom:controls.custom.includes(k)?controls.custom.filter(v=>v!==k):[...controls.custom,k].sort((a,b)=>a-b)})} className={controls.custom.includes(k)?"pill bg-cyan-800 text-cyan-100":"pill text-slate-400"}>{k}</button>)}</div>}
    <div className="mb-3 flex flex-wrap items-center gap-5 text-sm">
      <label><input type="checkbox" checked={controls.replay} onChange={e=>patch({replay:e.target.checked,end:e.target.checked?(times.at(-1)??""):""})}/> Replay</label>
      <label><input type="checkbox" checked={controls.signals} onChange={e=>patch({signals:e.target.checked})}/> 🎯 Signals</label>
      <label><input type="checkbox" checked={controls.auto} onChange={e=>patch({auto:e.target.checked})}/> Auto-refresh{controls.replay?" (paused in replay)":data?.live?"":" (historical session)"}</label>
      <details className="rounded border border-slate-600 px-3 py-2"><summary className="cursor-pointer">⚙ Settings</summary><div className="flex flex-wrap gap-3 pt-3">{(["full","day","pct","lot","hl"] as const).map(k=><label key={k}><input type="checkbox" checked={controls.settings[k]} onChange={e=>patch({settings:{...controls.settings,[k]:e.target.checked}})}/> {{full:"Full numbers",day:"Day Change",pct:"Day Change %",lot:"Lots",hl:"Highlight High/Low"}[k]}</label>)}<label>Font <select className={select} value={controls.settings.font} onChange={e=>patch({settings:{...controls.settings,font:e.target.value}})}>{["Small","Medium","Large"].map(v=><option key={v}>{v}</option>)}</select></label></div></details>
    </div>
    {controls.replay&&times.length>0&&<label className="mb-3 flex flex-col gap-2 text-sm text-cyan-200">Replay end: {endpoint.replace("T"," ").slice(0,19)}<input aria-label="Replay end" type="range" min={0} max={times.length-1} value={replayIndex} onChange={e=>patch({end:times[Number(e.target.value)]})}/></label>}
    {busy?<div role="status" className="rounded border border-cyan-900 p-8 text-center text-cyan-300">Loading original 8501 calculations…</div>:warning?<div role="alert" className="rounded border border-amber-700 p-5 text-amber-200">{warning}<p className="mt-2 text-xs">Choose an available session and expiry, or refresh. The time-series adapter requires a restarted 8600 backend.</p></div>:<>
      <div className="mb-2 flex flex-wrap justify-between gap-2 text-xs text-slate-400"><span>{controls.mode} · {controls.tf} buckets · {controls.mode==="Price"?"price ₹":controls.settings.lot?"lots":"shares"} ({data?.unit}) · {data?.partial?"⏳ newest bucket still running ("+data.elapsed+" of "+data.step_min+" min)":"all buckets closed"}</span><span>{data?.future!=null?"Future "+data.future.toLocaleString("en-IN")+" · ":""}NIFTY {data?.spot?.toLocaleString("en-IN")} · {data?.now?.replace(/\.\d+$/,"")}{controls.replay?" · REPLAY":""}</span></div>
      <iframe ref={frame} title="Time-series Chain grid and original 8501 interpretation" sandbox="allow-scripts" srcDoc={src} className="w-full border-0" style={{height}}/>
    </>}
    <p className="mt-3 text-xs text-slate-500">Information only. Reads are heuristics. The running bucket can change; judge closed buckets. This separate view places no orders.</p>
  </section>;
}
