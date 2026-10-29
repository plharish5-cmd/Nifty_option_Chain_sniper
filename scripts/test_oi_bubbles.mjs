// scripts/test_oi_bubbles.mjs — unit tests for the OI bubble scaling and divergence readout.
//
// The frontend has no test runner, so these mirror the pure functions from
// ict_smc_app/frontend/src/components/oiBubbles.ts and assert the properties that matter. Run with:
//   node scripts/test_oi_bubbles.mjs
//
// Keep in step with oiBubbles.ts: if the scaling there changes, change it here and see these fail.

const CFG = { minRadiusPx: 3, maxRadiusPx: 15, targetBubbles: 22,
  tiers: [{ min: 5.0, label: "EXTREME" }, { min: 2.5, label: "WIDE" }, { min: 1.5, label: "NOTABLE" }] };

function bubbleRadius(value, maxAbs, minR = CFG.minRadiusPx, maxR = CFG.maxRadiusPx) {
  if (!Number.isFinite(value) || !Number.isFinite(maxAbs) || maxAbs <= 0) return minR;
  return Math.max(minR, maxR * Math.sqrt(Math.min(1, Math.abs(value) / maxAbs)));
}
function sampleIndices(len, target = CFG.targetBubbles) {
  if (len <= 0) return [];
  const step = Math.max(1, Math.ceil(len / Math.max(1, target)));
  const out = [];
  for (let i = 0; i < len; i += step) out.push(i);
  if (out[out.length - 1] !== len - 1) out.push(len - 1);
  return out;
}
function gapRead(put, call, fmt) {
  if (!Number.isFinite(put) || !Number.isFinite(call)) return null;
  const ap = Math.abs(put), ac = Math.abs(call);
  const hi = Math.max(ap, ac), lo = Math.min(ap, ac);
  if (hi <= 0) return null;
  const ratio = lo <= 0 ? Infinity : hi / lo;
  const tier = CFG.tiers.find((t) => ratio >= t.min);
  if (!tier) return null;
  const side = ap >= ac ? "put" : "call";
  const r = Number.isFinite(ratio) ? `${ratio.toFixed(1)}×` : "∞";
  return { ratio, label: tier.label, side,
    text: `Put ${fmt(put)} vs Call ${fmt(call)} · ${side === "put" ? "PUT" : "CALL"} ${r} wider · ${tier.label}` };
}
const inr = (v) => { const a = Math.abs(v), s = v < 0 ? "-" : "+";
  if (a >= 1e7) return `${s}${(a/1e7).toFixed(2)}Cr`;
  if (a >= 1e5) return `${s}${(a/1e5).toFixed(2)}L`;
  return `${s}${a.toFixed(0)}`; };

let pass = 0, fail = 0;
const ok = (name, cond, extra = "") => { if (cond) { pass++; console.log(`  ok   ${name}`); }
  else { fail++; console.log(`  FAIL ${name}${extra ? "  -> " + extra : ""}`); } };
const near = (a, b, tol = 0.02) => Math.abs(a - b) <= tol * Math.max(1, Math.abs(b));

console.log("\nbubble area is proportional to magnitude (the whole point)");
{
  // 29 Oct close: Call +65.4L vs Put +5.58Cr -> a true 8.53x gap.
  const call = 65.4e5, put = 5.58e7, maxAbs = put;
  const rCall = bubbleRadius(call, maxAbs), rPut = bubbleRadius(put, maxAbs);
  const areaRatio = (rPut / rCall) ** 2, valueRatio = put / call;
  ok("area ratio equals the OI ratio", near(areaRatio, valueRatio),
     `area ${areaRatio.toFixed(2)}x vs value ${valueRatio.toFixed(2)}x`);
  ok("largest value gets the max radius", near(rPut, CFG.maxRadiusPx));
  ok("call bubble is visibly smaller", rCall < rPut * 0.45, `rCall=${rCall.toFixed(2)} rPut=${rPut.toFixed(2)}`);
  console.log(`       Call ${inr(call)} r=${rCall.toFixed(1)}px · Put ${inr(put)} r=${rPut.toFixed(1)}px · area ${areaRatio.toFixed(1)}x`);
}
{
  // Radius-proportional scaling would have understated this badly — guard against regressing to it.
  const naive = CFG.maxRadiusPx * (65.4e5 / 5.58e7);
  ok("sqrt scaling is not linear scaling", !near(bubbleRadius(65.4e5, 5.58e7), naive, 0.05));
}

console.log("\nedge cases do not blow up");
ok("zero value floors to minRadius", bubbleRadius(0, 5.58e7) === CFG.minRadiusPx);
ok("maxAbs of zero floors", bubbleRadius(1e6, 0) === CFG.minRadiusPx);
ok("NaN floors", bubbleRadius(NaN, 1e7) === CFG.minRadiusPx);
ok("negative OI uses magnitude", near(bubbleRadius(-5.58e7, 5.58e7), CFG.maxRadiusPx));
ok("value above maxAbs is clamped", near(bubbleRadius(9e7, 5.58e7), CFG.maxRadiusPx));

console.log("\ndivergence readout");
{
  const g = gapRead(5.58e7, 65.4e5, inr);
  ok("lakh-vs-crore reads EXTREME", g && g.label === "EXTREME", g && g.label);
  ok("names the dominant side", g && g.side === "put");
  ok("states the ratio in words", g && g.text.includes("8.5×"), g && g.text);
  console.log(`       ${g.text}`);
}
{
  // He asked specifically that crore-vs-crore still shows: 2Cr vs 5Cr is a real 2.5x separation.
  const g = gapRead(5e7, 2e7, inr);
  ok("2Cr vs 5Cr still flags", g !== null);
  ok("...as WIDE, not EXTREME", g && g.label === "WIDE", g && g.label);
  console.log(`       ${g.text}`);
}
{
  const g = gapRead(3.43e7, 3.00e7, inr);
  ok("near-equal crores stay quiet", g === null, g && g.text);
}
ok("1.5x is the floor for NOTABLE", gapRead(1.5e7, 1.0e7, inr)?.label === "NOTABLE");
ok("1.4x says nothing", gapRead(1.4e7, 1.0e7, inr) === null);
ok("call-dominant is labelled CALL", gapRead(1e6, 1e7, inr)?.side === "call");
ok("zero on one side is not a crash", gapRead(5e7, 0, inr)?.text.includes("∞"));
ok("both zero returns null", gapRead(0, 0, inr) === null);
ok("NaN returns null", gapRead(NaN, 1e7, inr) === null);

console.log("\nsampling");
ok("always includes the newest point", sampleIndices(127).at(-1) === 126);
ok("always includes the first point", sampleIndices(127)[0] === 0);
ok("stays near the target count", Math.abs(sampleIndices(127).length - CFG.targetBubbles) <= 3,
   `${sampleIndices(127).length}`);
ok("short series draws every point", sampleIndices(5).length === 5);
ok("single point works", sampleIndices(1).length === 1);
ok("empty series draws nothing", sampleIndices(0).length === 0);
ok("no duplicate indices", new Set(sampleIndices(127)).size === sampleIndices(127).length);

console.log(`\n${pass} passed, ${fail} failed\n`);
process.exit(fail === 0 ? 0 : 1);
