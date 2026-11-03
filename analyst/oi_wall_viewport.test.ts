import assert from 'node:assert/strict';
import {test} from 'node:test';
import {nearestOiWalls,oiWallRange} from '../ict_smc_app/frontend/src/components/oiWallViewport';
import {OrderFlowPrimitive,buildWalls} from '../ict_smc_app/frontend/src/components/orderflow';
const walls=[{p:24000,side:'CE' as const,oi:200},{p:23500,side:'CE' as const,oi:100},{p:23300,side:'PE' as const,oi:100},{p:23000,side:'PE' as const,oi:200}];
const base={priceRange:{minValue:23430,maxValue:23450},margins:{above:20,below:20}};
test('narrow candle scale includes nearest floor/ceiling without stretching to distant macro walls',()=>{
 assert.deepEqual(oiWallRange(base,walls,23440),{...base,priceRange:{minValue:23300,maxValue:23500}});
 assert.deepEqual(base.priceRange,{minValue:23430,maxValue:23450});
});
test('empty and invalid observations do not invent levels',()=>{
 assert.deepEqual(oiWallRange(base,[],23440),base);
 assert.equal(oiWallRange(null,walls,23440),null);
 assert.deepEqual(oiWallRange({priceRange:null},walls,23440),{priceRange:null});
 assert.equal(oiWallRange(base,walls,NaN),base);
 assert.deepEqual(nearestOiWalls([{p:NaN,side:'CE',oi:1},{p:23500,side:'CE',oi:0}],23440),{ceiling:undefined,floor:undefined});
});
test('existing wider price ranges and margins are preserved',()=>{
 const wide={...base,priceRange:{minValue:22000,maxValue:25000}};
 assert.deepEqual(oiWallRange(wide,walls,23440),wide);
});
test('side-specific selection works after price crosses a selected wall and does not mutate inputs',()=>{
 const before=JSON.stringify(walls);
 assert.equal(nearestOiWalls(walls,23520).ceiling?.p,24000);
 assert.equal(nearestOiWalls(walls,23200).floor?.p,23000);
 assert.equal(JSON.stringify(walls),before);
});
test('unchanged original wall renderer draws bands when candle scale includes wall prices',()=>{
 const rows=walls.map(w=>({strike:w.p,ce:{oi:w.side==='CE'?w.oi:0,oi_chg:0},pe:{oi:w.side==='PE'?w.oi:0,oi_chg:0}}));
 const selected=buildWalls(rows,23440).filter(w=>w.oi>0);
 const fitted=oiWallRange(base,selected,23440)!;
 let drawn=0;
 const ctx=new Proxy({createLinearGradient:()=>({addColorStop(){}}),measureText:()=>({width:100}),fillRect(){drawn++;}}, {get:(o,k)=>k in o?o[k]:()=>{},set:(o,k,v)=>{o[k]=v;return true;}});
 const primitive=new OrderFlowPrimitive();
 primitive.attached({chart:{},series:{priceToCoordinate:(p:number)=>500-(p-fitted.priceRange.minValue)/(fitted.priceRange.maxValue-fitted.priceRange.minValue)*500},requestUpdate(){}} as any);
 primitive.set({walls:selected});
 primitive.paneViews()[0].renderer()!.draw({useMediaCoordinateSpace:(fn:any)=>fn({context:ctx,mediaSize:{width:900,height:500}})} as any);
 assert.ok(drawn>0,'Original bands must draw within the fitted viewport');
});
