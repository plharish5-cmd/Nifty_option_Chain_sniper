import {useEffect,useRef,useState} from 'react';
import {createChart,CandlestickSeries,ColorType,type UTCTimestamp,type Time} from 'lightweight-charts';
import {buildWalls,OrderFlowPrimitive,type Wall,type OiLevel} from './orderflow';
type Row={strike:number;ce:{oi:number;oi_chg:number};pe:{oi:number;oi_chg:number}};
type Payload={ok:boolean;message?:string;ts:number;spot:number;expiry:string;expiries:string[];source:string;rows:Row[];candles:{ts:number;o:number;h:number;l:number;c:number}[]};
const clock=(ts:number)=>new Date(ts*1000).toLocaleTimeString('en-IN',{timeZone:'Asia/Kolkata',hour12:false});
const units=(n:number)=>(n/1e5).toFixed(2)+' L';
export default function OiWallsReplay({day,ts}:{day:string;ts:number}){
 const [expiry,setExpiry]=useState(''),[data,setData]=useState<Payload|null>(null),[error,setError]=useState(''),[hover,setHover]=useState<Wall|null>(null);
 const host=useRef<HTMLDivElement>(null);
 useEffect(()=>{const abort=new AbortController();setData(null);setError('');setHover(null);const q=new URLSearchParams({day,at:String(ts)});if(expiry)q.set('expiry',expiry);
 fetch('/api/desk/oi-walls-replay?'+q,{signal:abort.signal}).then(async r=>{if(!r.ok||!r.headers.get('content-type')?.includes('application/json'))throw Error('Restart the ICT backend to load the OI walls replay API');return r.json();}).then(d=>{if(!abort.signal.aborted)setData(d);}).catch(e=>{if(!abort.signal.aborted)setError(e.message);});return()=>abort.abort();},[day,ts,expiry]);
 useEffect(()=>{if(!host.current||!data?.ok)return;
 const chart=createChart(host.current,{autoSize:true,height:600,layout:{background:{type:ColorType.Solid,color:'#0b1220'},textColor:'#94a3b8'},grid:{vertLines:{color:'#152036'},horzLines:{color:'#152036'}},timeScale:{timeVisible:true,secondsVisible:false,tickMarkFormatter:(t:Time)=>clock(Number(t))},localization:{timeFormatter:(t:Time)=>clock(Number(t))}});
 const series=chart.addSeries(CandlestickSeries,{upColor:'#10b981',downColor:'#ef4444',borderVisible:false,wickUpColor:'#10b981',wickDownColor:'#ef4444'});
 series.setData(data.candles.map(c=>({time:c.ts as UTCTimestamp,open:c.o,high:c.h,low:c.l,close:c.c})));
 const walls=buildWalls(data.rows,data.spot);
 const oiLevels:OiLevel[]=data.rows.filter(r=>Math.abs(r.strike-data.spot)<=data.spot*0.02).flatMap(r=>(['CE','PE'] as const).map(side=>{const leg=side==='CE'?r.ce:r.pe;return {p:r.strike,side,oi:leg.oi,chg:leg.oi_chg,pct:leg.oi?leg.oi_chg/leg.oi*100:null};}));
 const primitive=new OrderFlowPrimitive();series.attachPrimitive(primitive);primitive.set({walls,oiLevels});chart.timeScale().fitContent();
 chart.subscribeCrosshairMove(p=>{if(!p.point){setHover(null);return;}const price=series.coordinateToPrice(p.point.y);if(price==null)return;setHover([...walls].sort((a,b)=>Math.abs(a.p-price)-Math.abs(b.p-price))[0]||null);});
 return()=>chart.remove();},[data]);
 return <section><h2>Recorded OI walls · call / put levels and percentage bubbles</h2><label>Expiry <select value={expiry||data?.expiry||''} onChange={e=>setExpiry(e.target.value)}>{(data?.expiries||[]).map(e=><option key={e}>{e}</option>)}</select></label>
 <p>Use the shared time slider above. This is a fixed historical snapshot, with no auto-play or live refresh.</p>
 {error?<p role="alert">{error}</p>:!data?<p>Loading recorded walls…</p>:!data.ok?<p>{data.message}</p>:<><p>OI captured {clock(data.ts)} IST · target {clock(ts)} · snapshot age {Math.round(ts-data.ts)}s · recorded spot {data.spot.toFixed(2)} · {data.expiry}</p><p>Original wall selection and bubble formulas reused. Percentage = daily ΔOI ÷ current OI. Historical wall velocity unavailable; no cooling/acceleration is inferred.</p><div ref={host}/><p>{hover?`${hover.side} ${hover.p.toLocaleString('en-IN')} · OI ${units(hover.oi)} · daily ΔOI ${units(hover.chg)} · ${(hover.fresh*100).toFixed(1)}% of current OI · ${hover.status}`:'Move the mouse over the chart to inspect the closest OI wall.'}</p><p>Walls describe the selected snapshot across the chart, not the walls at each earlier candle. Scrub the slider to change the snapshot. OI alone does not identify writer intent.</p></>}
 </section>;
}
