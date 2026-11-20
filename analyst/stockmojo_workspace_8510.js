/* Additive panels inside the existing 8510 replay and lab tabs. No order endpoints. */
(() => {
  'use strict';
  const API='/api/stockmojo/research';
  const make=(tag,text,cls)=>{const e=document.createElement(tag);if(text!==undefined)e.textContent=text;if(cls)e.className=cls;return e;};
  const style=make('style');style.textContent='.sm-panel{margin:16px 0;padding:18px;border:1px solid #334155;border-radius:12px;background:#0c1526;color:#e2e8f0}.sm-controls{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin:12px 0}.sm-panel button,.sm-panel select{background:#13263d;color:#e2e8f0;padding:8px;border:1px solid #456;border-radius:6px}.sm-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px}.sm-scroll{overflow:auto;max-height:350px}.sm-panel table{border-collapse:collapse;font-size:12px;width:100%}.sm-panel th,.sm-panel td{padding:6px;border-bottom:1px solid #263649;white-space:nowrap}.sm-badge{font-weight:bold;color:#fbbf24}.sm-panel p{line-height:1.5}.sm-panel input[type=range]{flex:1;min-width:180px}';document.head.append(style);
  function panel(tab,live){
    const host=document.getElementById(tab);if(!host)return;
    const section=make('section',undefined,'sm-panel');host.prepend(section);
    section.append(make('h3','StockMojo PE–CE · '+(live?'Research feature':'Historical archive replay')));
    section.append(make('p',live?'The existing seven gates and 128 combinations are unchanged. This separate feature uses causal source observations; it does not tune or rank configurations.':'Replay dates come from source timestamps, not download folders. Reset creates a new isolated replay account.'));
    const controls=make('div',undefined,'sm-controls');section.append(controls);
    const instrument=make('select'),date=make('select'),expiry=make('select'),session=make('select');
    [instrument,date,expiry,session].forEach((e,i)=>{e.setAttribute('aria-label',['Instrument','Source date','Option expiry','Archive session'][i]);controls.append(e);});
    const reload=make('button','Refresh archives');controls.append(reload);
    const actions=make('div',undefined,'sm-controls');section.append(actions);
    const paper=make('input');paper.type='checkbox';const label=make('label','Enable isolated PAPER fills ');label.prepend(paper);actions.append(label);
    const play=make('button',live?'Inspect latest':'▶ Play'),pause=make('button','Pause'),step=make('button','Step'),reset=make('button','Reset'),qwen=make('button','Explain with local Qwen');[play,pause,step,reset,qwen].forEach(e=>actions.append(e));
    const speed=make('select');[1,2,5].forEach(n=>speed.add(new Option(n+'×',String(n))));actions.append(speed);
    const slider=make('input');slider.type='range';slider.min=0;slider.max=0;slider.value=0;slider.setAttribute('aria-label','Replay observation');actions.append(slider);
    const compare=make('button','Chronological comparison');if(live)actions.append(compare);
    const status=make('p','Load archives to begin','sm-badge');section.append(status);
    const metadata=make('p');section.append(metadata);const grid=make('div',undefined,'sm-grid');section.append(grid);
    const evidence=make('div'),narrative=make('div');grid.append(evidence,narrative);
    const output=make('div');section.append(output);
    let sessions=[],run=crypto.randomUUID(),cursor=0,timer=null,playing=false,busy=false,last=null,latestCursor=-1;
    const stop=()=>{playing=false;clearTimeout(timer);timer=null;};
    function options(el,values){el.replaceChildren();values.forEach(x=>el.add(new Option(x,x)));}
    function choose(){stop();slider.max=Math.max(0,(sessions.find(s=>s.id===session.value)?.count||1)-1);run=crypto.randomUUID();cursor=0;latestCursor=-1;slider.value=0;last=null;}
    function filter(level){
      if(level===0)options(instrument,[...new Set(sessions.map(s=>s.instrument.underlying||'UNVERIFIED request '+s.request.symbol))]);
      let rows=sessions.filter(s=>(s.instrument.underlying||'UNVERIFIED request '+s.request.symbol)===instrument.value);
      if(level<=1)options(date,[...new Set(rows.map(s=>s.date))].sort().reverse());rows=rows.filter(s=>s.date===date.value);
      if(level<=2)options(expiry,[...new Set(rows.map(s=>s.instrument.options_expiry||'UNVERIFIED expiry '+s.request.expiry))]);
      rows=rows.filter(s=>(s.instrument.options_expiry||'UNVERIFIED expiry '+s.request.expiry)===expiry.value);
      session.replaceChildren();rows.forEach(s=>session.add(new Option(s.count+' observations · '+(s.verified?'reviewed':'UNVERIFIED')+' · '+s.id.slice(0,6),s.id)));
      slider.max=Math.max(0,(rows[0]?.count||1)-1);choose();
    }
    function table(title,rows,fields){
      const wrap=make('div');wrap.append(make('h4',title));const scroll=make('div',undefined,'sm-scroll');wrap.append(scroll);const t=make('table');scroll.append(t);const head=make('tr');fields.forEach(f=>head.append(make('th',f)));t.append(head);
      rows.forEach(row=>{const tr=make('tr');fields.forEach(f=>{let v=row[f];tr.append(make('td',v===null||v===undefined?'—':typeof v==='object'?JSON.stringify(v):String(v)));});t.append(tr);});return wrap;
    }
    async function evaluate(explain=false){
      if(busy||!session.value)return;busy=true;
      try{
        const requestRun=run;
        const res=await fetch(API+'/evaluate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({session:session.value,run,cursor,mode:live?'LIVE_FRESH':'HISTORICAL_REPLAY',paper_enabled:paper.checked,qwen:explain})});
        const data=await res.json();if(requestRun!==run)return;if(!res.ok)throw Error(data.detail||'Unable to evaluate');last=data;cursor=data.cursor;latestCursor=cursor;slider.value=cursor;
        if(data.commentary.status==='PENDING')setTimeout(()=>{if(run===requestRun&&cursor===data.cursor&&!playing)evaluate(false);},500);
        const s=data.signal;status.textContent=(s.state||'BLOCKED')+' · '+s.signal+' · Bias score '+s.score+'/100 · HEURISTIC, not a probability';
        const row=data.rows.at(-1),i=s.instrument||{};
        metadata.textContent='Underlying: '+(i.underlying||'UNVERIFIED')+' | Option expiry: '+(i.options_expiry||'UNVERIFIED')+' | Futures expiry: '+(i.futures_expiry||'UNVERIFIED')+' | Source: '+(s.source_time||'—')+' | Retrieved: '+(row?.retrieved_at||'—')+' | Paper: '+(paper.checked?'opted in, subject to quotes and checks':'OFF');
        evidence.replaceChildren(make('h4','Python evidence'));
        [...(s.reasons||[]),...(s.contradictions||[]),...(s.quality_flags||[])].forEach(x=>evidence.append(make('p',x)));
        evidence.append(make('p','Option fills require reviewed contracts and aligned recorded bid/ask. Missing quotes means BLOCKED; futures prices are never option premiums.'));
        narrative.replaceChildren(make('h4','Qwen · '+data.commentary.status));
        Object.entries(data.commentary).filter(([k])=>!['status','authoritative_signal'].includes(k)).forEach(([k,v])=>narrative.append(make('p',k+': '+(Array.isArray(v)?v.join('; '):v))));
        output.replaceChildren(table('Factor breakdown',[s.factors||{}],Object.keys(s.factors||{})),table('Source rows · normalized; raw retained separately',data.rows,['timestamp','put_oi_day','put_oi_change','call_oi_day','call_oi_change','pe_ce_day','pe_ce_change','pcr_total','future_ltp','future_oi_day','quality_flags']),table('Raw provider values',data.rows.map(r=>({timestamp:r.timestamp,...r.raw})),['timestamp',...Object.keys(row?.raw||{})]),table('Signal timeline',data.timeline,['time','signal','score','paper','reason']),table('Isolated paper positions',data.paper.position?[data.paper.position]:[],['underlying','expiry','strike','side','quantity','entry_time','entry_price','entry_future_ltp','status']),table('Isolated paper ledger',data.paper.fills,['entry_source_time','exit_source_time','expiry','strike','side','entry_price','exit_price','quantity','gross_pnl','charges','net_pnl','exit_reason']),table('Replay metrics · not out-of-sample validation',[data.metrics],Object.keys(data.metrics)));
      }catch(e){status.textContent='BLOCKED · '+e.message;stop();}finally{busy=false;}
    }
    async function tick(){if(!playing)return;await evaluate();if(!playing)return;if(!live&&cursor>=Number(slider.max)){stop();return;}if(!live)cursor++;timer=setTimeout(tick,(live?10000:1000)/Number(speed.value));}
    reload.onclick=async()=>{stop();try{const r=await fetch(API+'/sessions');const data=await r.json();sessions=data.sessions||[];filter(0);status.textContent=sessions.length?'Select source session and inspect. Paper remains OFF.':'No recorded archives. No fabricated data.';}catch(e){status.textContent='Archive API unavailable';}};
    compare.onclick=async()=>{stop();try{const r=await fetch(API+'/compare',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({session:session.value})});const d=await r.json();if(!r.ok)throw Error(d.detail);output.append(table('Pre-registered chronological comparison · directional only',d.comparison,Object.keys(d.comparison[0]||{})));}catch(e){status.textContent=e.message;}};
    instrument.onchange=()=>filter(1);date.onchange=()=>filter(2);expiry.onchange=()=>filter(3);session.onchange=choose;paper.onchange=choose;
    play.onclick=()=>{stop();playing=true;tick();};pause.onclick=stop;reset.onclick=()=>{choose();output.replaceChildren();status.textContent='Reset · new replay account, paper checkbox preference retained';};
    step.onclick=()=>{stop();if(last)cursor=Math.min(cursor+1,Number(slider.max));evaluate();};qwen.onclick=()=>evaluate(true);
    slider.onchange=()=>{stop();const n=Number(slider.value);if(n<latestCursor){run=crypto.randomUUID();last=null;}cursor=n;evaluate();};
    if(live){slider.hidden=true;step.hidden=true;speed.hidden=true;}reload.click();
  }
  const start=()=>{panel('tabReplayExplorer',false);panel('tabTrendLab',true);};
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',start);else start();
})();
