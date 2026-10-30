// Verifies the label thinning: every marker survives, only text is dropped.
const DIV_LABEL_MIN_GAP_S = 20*60, DIV_FLIP_MIN_GAP_S = 6*60;
function thin(evs){let lastT=-Infinity,lastDir="";return evs.map(e=>{
  if(e.kind!=="divergence")return e;const gap=e.time-lastT;
  const keep=gap>=DIV_LABEL_MIN_GAP_S||(e.dir!==lastDir&&gap>=DIV_FLIP_MIN_GAP_S);
  if(!keep)return{...e,text:""};lastT=e.time;lastDir=e.dir;return e;});}
let pass=0,fail=0;
const ok=(c,m)=>{c?pass++:(fail++,console.log("FAIL:",m));};
const d=(t,dir)=>({time:t,dir,kind:"divergence",text:`${dir} div`});

// the 30 Oct cluster: six bearish divergences on consecutive 3-min samples
const cluster=[0,180,360,540,720,900].map(t=>d(t,"bear"));
const r1=thin(cluster);
ok(r1.length===6,"every dot survives");
ok(r1.filter(e=>e.text!=="").length===1,"exactly one label in a 15min run (gap is 20min)");
ok(r1[0].text!=="","the FIRST of the run keeps the sentence");
ok(r1.slice(1).every(e=>e.text===""),"the rest are bare dots");
ok(r1.every((e,i)=>e.time===cluster[i].time&&e.dir===cluster[i].dir&&e.kind==="divergence"),
   "time, dir and kind are untouched");

// a label is allowed again once the gap is wide enough
const spread=[0,21*60,42*60].map(t=>d(t,"bear"));
ok(thin(spread).every(e=>e.text!==""),"20min+ apart all keep labels");

// a direction flip speaks sooner, but not instantly
ok(thin([d(0,"bear"),d(7*60,"bull")])[1].text!=="","a flip after 6min is labelled");
ok(thin([d(0,"bear"),d(120,"bull")])[1].text==="","a flip after 2min is not");

// crossovers are never thinned
const cross=[{time:0,dir:"bull",kind:"cross",text:"x"},{time:60,dir:"bear",kind:"cross",text:"y"}];
ok(thin(cross).every(e=>e.text!==""),"crossover labels always kept");
// ...and a crossover does not consume a divergence's slot
const mixed=thin([d(0,"bear"),{time:60,dir:"bull",kind:"cross",text:"x"},d(120,"bear")]);
ok(mixed[1].text==="x"&&mixed[2].text==="","crossover does not reset divergence spacing");

// empty / single
ok(thin([]).length===0,"empty is safe");
ok(thin([d(0,"bull")])[0].text!=="","a lone divergence is labelled");


// Label width is the whole reason for the spacing: assert the tag stays short.
function short(t){if(t==="")return"";
  if(t.includes("Bullish divergence"))return"\u26a0 Bull div";
  if(t.includes("Bearish divergence"))return"\u26a0 Bear div";
  if(t.includes("Bullish crossover"))return"\u25b2 Bull cross";
  if(t.includes("Bearish crossover"))return"\u25bc Bear cross";
  return t.length>28?t.slice(0,27)+"\u2026":t;}
ok(short("Bearish divergence warning: price up, PE\u2212CE gap weakening").length<=12,
   "bear tag fits the 20min spacing");
ok(short("Bullish divergence warning: price down, PE\u2212CE gap improving").length<=12,
   "bull tag fits the 20min spacing");
ok(short("")==="","a thinned event stays blank, never a stray tag");
ok(short("Some unexpected future wording that is quite long indeed").length===28,
   "unknown wording is truncated, not left to overflow");

console.log(`${pass} passed, ${fail} failed`);
process.exit(fail?1:0);
