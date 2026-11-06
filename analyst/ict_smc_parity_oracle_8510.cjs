const fs=require('fs'), vm=require('vm');
const ts=require(process.argv[2]+'/ict_smc_app/frontend/node_modules/typescript/lib/typescript.js');
const root=process.argv[2], input=JSON.parse(fs.readFileSync(0,'utf8'));
function declarations(name,names){let txt=fs.readFileSync(root+'/ict_smc_app/frontend/src/components/'+name,'utf8'),sf=ts.createSourceFile(name,txt,99,true,ts.ScriptKind.TSX);return sf.statements.filter(n=>(ts.isFunctionDeclaration(n)&&names.includes(n.name?.text)) || (ts.isVariableStatement(n)&&n.declarationList.declarations.some(d=>names.includes(d.name.getText(sf))))).map(n=>n.getText(sf).replace(/^export /,'')).join('\n');}
let source=declarations('regime.ts',['DAY_TREND_CR','COUNTER_TREND_CAP','dayRegime','fightsTheDay','counterTrendNote'])+'\n'+declarations('deskState.ts',['WARMUP_UNTIL','EOD_FROM','permissionFor','isSideAllowed','deskState'])+'\n'+declarations('OcLab.tsx',['computeSmartRows']);
const original=fs.readFileSync(root+'/ict_smc_app/frontend/src/components/OcLab.tsx','utf8');
const corridor=original.match(/const sup = activePlanRow\.sup \?\? 22500;[\s\S]*?const posPct = .*?;/)[0];
source+='\nfunction corridor(activePlanRow){'+corridor+'return posPct;}';
// Conditions extracted verbatim from the original JSX, labels normalized only.
const radarStart=original.indexOf('{activePlanRow.isPullbackToSupply ? (');
const radarChunk=original.slice(radarStart,original.indexOf('{/* Card 4:',radarStart));
const conditions=[...radarChunk.matchAll(/(?:\{|\) : )([^{}\n]+?) \? \(/g)].map(m=>m[1].trim());
if(conditions.length!==3)throw Error('Radar source structure changed');
source+='\nfunction radar(activePlanRow){return '+conditions.map((c,i)=>'('+c+') ? '+JSON.stringify(['AT_PULLBACK_SELL_ZONE','OVER_EXTENDED_DROP','SHORT_COVERING_REVERSAL'][i])+' : ').join('')+'"MID_RANGE_CONSOLIDATION";}';
let code=ts.transpileModule(source,{compilerOptions:{target:ts.ScriptTarget.ES2020,module:ts.ModuleKind.CommonJS}}).outputText;
const ctx={};vm.createContext(ctx);vm.runInContext(code,ctx);
let result=input.map(x=>{let rows=ctx.computeSmartRows(x.rows,x.tf||'3m');return rows.map((r,i)=>{let state=ctx.deskState(r);return {...Object.fromEntries(['stage','stageBadge','actionTone','suppressed','convictionPct','isActionable','isHighConviction','isSynergy','isScissor','isPullbackToSupply','isPullbackToDemand','isShortCoveringReversal','spotChange15m'].map(k=>[k,r[k]])),radar:ctx.radar(r),corridorPct:ctx.corridor(r),stateKey:state.key,stateChange:i===rows.length-1 || ctx.deskState(rows[i+1]).key!==state.key};});});
process.stdout.write(JSON.stringify(result));
