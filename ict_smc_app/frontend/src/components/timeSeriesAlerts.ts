// Significant-event adapter only: no new price/OI/volume strategy calculations.
import analystConfig from "../../../../analyst/analyst_config.json";
export const VOICE_POLICY=analystConfig.time_series_chain.voice_alerts;
export type LevelSignal={time:string;close:number|null;support:number|null;resistance:number|null;event:[string,string,string]|null;action:[string,string]|null;badge?:string[]};
export type AlertGroups={breaks:boolean;reversals:boolean;traps:boolean;builds:boolean};
export const DEFAULT_GROUPS:AlertGroups={breaks:true,reversals:true,traps:true,builds:false};
export type SeriesAlertData={ok:boolean;day?:string;expiry?:string;tf?:string;now?:string;live?:boolean;alert_signals?:LevelSignal[];signals?:LevelSignal[]};
export type SeriesAlert={key:string;time:string;title:string;body:string;speech:string;side:"BUY"|"SELL"|"INFO";price:number;tag:string};
export type SeriesCursor={scope:string;last:string;lastAlert?:{signature:string;time:number}};
export function localEpoch(value:string){return Date.parse(value.replace(" ","T").replace(/(\.\d{3})\d+/,"$1")+"Z")/1000-19800;}
export function significantSignal(s:LevelSignal,groups:AlertGroups):boolean{
  const tag=s.event?.[2]??"";
  if(["break_up","break_dn"].includes(tag))return groups.breaks&&!!s.action?.[0].startsWith("BUY");
  if(["rev_up","rev_dn"].includes(tag))return groups.reversals&&!!s.action?.[0].startsWith("BUY");
  if(["fake","fail"].includes(tag))return groups.traps;
  return groups.builds&&["s_build","r_build"].includes(tag);
}
export function makeSeriesAlert(s:LevelSignal,data:SeriesAlertData,replay=false):SeriesAlert{
  const tag=s.event![2],time=s.time.slice(11,16);
  const names:Record<string,string>={break_up:"Bullish breakout",break_dn:"Bearish breakdown",rev_up:"Reversal from support",rev_dn:"Reversal from resistance",fake:"False breakout risk",fail:"Break failed",s_build:"Support building",r_build:"Resistance building"};
  const action=s.action?.[0]||"Wait for price confirmation";
  const direction=s.event?.[1];
  const side=["fake","fail","s_build","r_build"].includes(tag)?"INFO":direction==="bull"?"BUY":"SELL";
  const body=(s.event?.[0]??"")+" · "+action+" · original level rules; information only.";
  const spoken=(replay?"Historical replay. ":"")+time+". "+(names[tag]??"Level signal")+". "+(s.event?.[0]??"").replace(/[^\p{L}\p{N}\s.,%+−-]/gu,"")+". "+action.replace(/\bCE\b/g,"call option").replace(/\bPE\b/g,"put option").replace(/\bSL\b/g,"stop loss")+".";
  return {key:"TS|"+[data.day,data.expiry,data.tf,s.time,tag].join("|"),time:s.time,title:(replay?"REPLAY · ":"")+(names[tag]??"Level signal")+" · "+time,body,speech:spoken,side,price:s.close??0,tag};
}
export function planSeriesAlerts(data:SeriesAlertData,groups:AlertGroups,cursor:SeriesCursor|null,now=Date.now()/1000):{cursor:SeriesCursor|null;alerts:SeriesAlert[]}{
  if(!data.ok||!data.now)return {cursor,alerts:[]};
  const asof=localEpoch(data.now);
  if(!Number.isFinite(asof))return {cursor,alerts:[]};
  const closed=(data.alert_signals??data.signals??[]).filter(s=>localEpoch(s.time)<=asof).sort((a,b)=>a.time.localeCompare(b.time));
  const scope=[data.day,data.expiry,data.tf].join("|"),last=closed.at(-1)?.time??"";
  const next:SeriesCursor={scope,last:cursor?.scope===scope&&cursor.last>last?cursor.last:last,lastAlert:cursor?.scope===scope?cursor.lastAlert:undefined};
  if(!cursor||cursor.scope!==scope)return {cursor:next,alerts:[]}; // Arm at latest; never read the opening backlog.
  const clock=new Date((now+19800)*1000).toISOString();
  const live=data.live&&clock.slice(0,10)===data.day&&clock.slice(11,16)>=VOICE_POLICY.live_start&&clock.slice(11,16)<=VOICE_POLICY.live_end;
  if(!live||now-asof>VOICE_POLICY.max_feed_age_seconds||asof-now>VOICE_POLICY.clock_skew_seconds)return {cursor:next,alerts:[]};
  const alerts:SeriesAlert[]=[];
  for(const signal of closed.filter(s=>s.time>cursor.last&&significantSignal(s,groups)&&now-localEpoch(s.time)<=VOICE_POLICY.max_event_age_seconds)){
    const level=signal.event?.[0].match(/\d{2},\d{3}|\b\d{5}\b/)?.[0]??String(signal.support)+"|"+String(signal.resistance);
    const signature=signal.event?.[2]+"|"+level,at=localEpoch(signal.time);
    if(next.lastAlert?.signature===signature&&at-next.lastAlert.time<VOICE_POLICY.repeat_minutes*60)continue;
    alerts.push(makeSeriesAlert(signal,data));next.lastAlert={signature,time:at};
  }
  return {cursor:next,alerts};
}
export function selectedReplayAlert(data:SeriesAlertData,groups:AlertGroups):SeriesAlert|null{
  if(!data.now)return null;
  const s=(data.alert_signals??data.signals??[]).filter(s=>localEpoch(s.time)<=localEpoch(data.now!)&&significantSignal(s,groups)).sort((a,b)=>a.time.localeCompare(b.time)).at(-1);
  return s?makeSeriesAlert(s,data,true):null;
}
export function chooseVoice(voices:SpeechSynthesisVoice[],saved:string):SpeechSynthesisVoice|undefined{
  const exact=voices.find(v=>v.voiceURI===saved);
  if(exact)return exact;
  return voices.find(v=>/^en/i.test(v.lang)&&VOICE_POLICY.preferred_voices.some(n=>v.name.toLowerCase().includes(n.toLowerCase())))??voices.find(v=>/^en-IN/i.test(v.lang))??voices.find(v=>/^en/i.test(v.lang));
}
