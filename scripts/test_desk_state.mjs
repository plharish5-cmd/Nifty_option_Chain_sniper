// scripts/test_desk_state.mjs — unit tests for the desk state machine.
//
// Mirrors ict_smc_app/frontend/src/components/deskState.ts. Run with:
//   node scripts/test_desk_state.mjs
//
// The headline test replays the real 29 Oct session. The claim being checked is that a trend day
// produces almost NO state changes -- that the banner sits still for hours -- because that stillness
// is what has to argue with the urge to fade.

const DAY_TREND_CR = 2.0, WARMUP_UNTIL = "09:45", EOD_FROM = "15:10";
const dayRegime = (cr) => (cr == null || !Number.isFinite(cr)) ? "none"
  : cr >= DAY_TREND_CR ? "bull" : cr <= -DAY_TREND_CR ? "bear" : "none";

const isSideAllowed = (side, p) => side == null ? true : p === "NONE" ? false : p === "BOTH" ? true
  : (p === "LONG_ONLY" && side === "bull") || (p === "SHORT_ONLY" && side === "bear");

function deskState(r) {
  const reg = dayRegime(r.pe_ce_day_cr);
  const regime = r.ts >= EOD_FROM ? "EOD" : r.ts < WARMUP_UNTIL ? "WARMUP"
    : reg === "none" ? "CHOP" : "TREND";
  const permission = (regime === "WARMUP" || regime === "EOD") ? "NONE"
    : reg === "bull" ? "LONG_ONLY" : reg === "bear" ? "SHORT_ONLY" : "BOTH";
  const isTicket = !r.suppressed && r.actionText.includes("🎯");
  const side = r.actionTone === "bull" ? "bull" : r.actionTone === "bear" ? "bear" : null;
  const allowed = isSideAllowed(side, permission);
  const isHold = /HOLD|TRAIL/i.test(r.actionText) && !isTicket;
  const action = (isTicket && allowed) ? "ENTER" : isHold ? "HOLD" : "WAIT";
  return { permission, regime, action, side, key: `${permission}|${regime}|${action}`,
           speechKey: `${permission}|${regime}${action === "ENTER" ? "|ENTER" : ""}` };
}
function stateChangeIndices(rows, field = "key") {
  const out = []; let prev = null;
  for (let i = rows.length - 1; i >= 0; i--) {
    const k = deskState(rows[i])[field];
    if (k !== prev) { out.push(i); prev = k; }
  }
  return out.reverse();
}

let pass = 0, fail = 0;
const ok = (n, c, x = "") => { if (c) { pass++; console.log(`  ok   ${n}`); }
  else { fail++; console.log(`  FAIL ${n}${x ? "  -> " + x : ""}`); } };

// ── the real session, oldest first: [time, cumulative Net PE−CE (Cr), tone, action text] ─────────
const HOLD = "🛡️ HOLD CE · TRAIL SL → 22850 (DO NOT CHASE)";
const WAITT = "Wait for clean structural setup";
const WARM = "⏳ WARM-UP (Walls & Liquidity Forming) · WAIT";
const session = [
  ["09:15", -0.21, "flat", WARM], ["09:18", -0.53, "flat", WARM], ["09:21", -0.74, "flat", WARM],
  ["09:24", -0.94, "flat", WARM], ["09:27", -0.92, "flat", WARM], ["09:30", -0.80, "flat", WAITT],
  ["09:33", -0.95, "bear", "🎯 PULLBACK SELL: BUY 22750 PE · SL 22757"],
  ["09:42", -0.69, "flat", WAITT], ["09:48", -0.66, "flat", "Noise — too small to read · Stand aside"],
  ["09:57", -0.22, "flat", "🔥 BULLISH WRITER SURGE — SCALE-IN / HOLD CE · Trail SL 22708"],
  ["10:03", -0.17, "flat", "🟡 BOOK PE PROFITS / FLIP TO CE ON RETEST"],
  ["10:09", +0.50, "flat", "🟡 BOOK PE PROFITS / FLIP TO CE ON RETEST"],
  ["10:18", +0.97, "flat", "🟡 BOOK PE PROFITS / FLIP TO CE ON RETEST"],
  ["10:30", +2.15, "flat", "🟡 BOOK PE PROFITS / FLIP TO CE ON RETEST"],
  ["10:36", +2.46, "flat", HOLD], ["10:45", +2.62, "flat", HOLD],
  ["10:51", +2.75, "bull", "🎯 DIP BUY: BUY 22850 CE · SL 22824"],
  ["11:15", +2.06, "warn", "⚠️ COUNTER-TREND LONG UNWINDING SHOCK — Tighten CE Trail SL to 22814"],
  ["12:09", +2.56, "flat", HOLD], ["12:48", +2.31, "warn", "⚠️ COUNTER-TREND SHOCK — Tighten CE Trail SL to 22797"],
  ["13:18", +2.11, "bull", "🎯 DIP BUY: BUY 22850 CE · SL 22796"],
  ["13:36", +2.53, "flat", "🔥 BULLISH WRITER SURGE — SCALE-IN / HOLD CE · Trail SL 22830"],
  ["14:27", +3.70, "flat", "🔥 BULLISH WRITER SURGE — SCALE-IN / HOLD CE · Trail SL 22897"],
  ["14:39", +4.00, "flat", HOLD], ["15:09", +3.85, "flat", HOLD],
  ["15:15", +3.81, "flat", "🏁 EOD CLOSING WINDOW — Book Intraday Profits"],
  ["15:27", +3.77, "flat", "🏁 EOD CLOSING WINDOW — Book Intraday Profits"],
].map(([ts, cr, tone, txt]) => ({ ts, pe_ce_day_cr: cr, actionTone: tone, actionText: txt,
                                  stageBadge: "", dynamicSl: 22850 }));
const newestFirst = [...session].reverse();

console.log("\nthe 29 Oct session, replayed");
const changes = stateChangeIndices(newestFirst).map((i) => ({ ...newestFirst[i], s: deskState(newestFirst[i]) }));
for (const c of [...changes].reverse()) console.log(`  ${c.ts}  ${c.s.key}`);
ok("the whole day collapses to a readable handful of rows", changes.length <= 14, `${changes.length}`);
ok("...down from 126 raw rows", changes.length < 126 / 5, `${changes.length}`);
{
  const utter = stateChangeIndices(newestFirst, "speechKey");
  console.log("\n  what the VOICE says (permission / regime / entries only):");
  for (const i of [...utter].reverse()) {
    const r = newestFirst[i], s2 = deskState(r);
    console.log(`    ${r.ts}  ${s2.speechKey}`);
  }
  ok("the voice speaks a handful of times, not constantly", utter.length <= 8, `${utter.length}`);
  ok("WAIT<->HOLD churn does NOT speak", utter.length < changes.length, `${utter.length} vs ${changes.length}`);
}

console.log("\nthe permission actually held during the trend");
{
  const at = (hm) => deskState(session.find((r) => r.ts === hm));
  ok("09:15 is NO TRADE (warm-up)", at("09:15").permission === "NONE", at("09:15").permission);
  // 09:33 falls inside the warm-up window, so the bar says NO TRADE -- the losing short would have
  // been blocked, but by the TIME rule, not the regime gate (the day was genuinely undecided then).
  // Keeping the two mechanisms distinct matters: only one of them is evidence about the day.
  ok("09:33 is NO TRADE (still warm-up at 09:45)", at("09:33").permission === "NONE", at("09:33").permission);
  ok("09:33 cannot ENTER", at("09:33").action !== "ENTER", at("09:33").action);
  ok("09:48, just after warm-up, is BOTH SIDES", at("09:48").permission === "BOTH", at("09:48").permission);
  ok("10:30 flips to LONG ONLY", at("10:30").permission === "LONG_ONLY");
  ok("13:18 dip buy is ENTER (with the day)", at("13:18").action === "ENTER");
  ok("14:39 is HOLD, not a new entry", at("14:39").action === "HOLD");
  ok("15:15 is NO TRADE (EOD)", at("15:15").permission === "NONE");
}

console.log("\nthe refusal: a short on a long-only day");
{
  const r = { ts: "13:18", pe_ce_day_cr: 3.0, actionTone: "bear", stageBadge: "",
              actionText: "🎯 PULLBACK SELL: BUY 22750 PE · SL 22757", dynamicSl: 22757 };
  const s = deskState(r);
  ok("permission is LONG ONLY", s.permission === "LONG_ONLY");
  ok("the short does NOT become ENTER", s.action === "WAIT", s.action);
  ok("the side is reported as not allowed", !isSideAllowed("bear", s.permission));
  ok("a long on the same day IS allowed", isSideAllowed("bull", s.permission));
}

console.log("\nsuppressed tickets can never produce an ENTER");
{
  const r = { ts: "11:00", pe_ce_day_cr: 3.0, actionTone: "bull", stageBadge: "", dynamicSl: 1,
              actionText: "🎯 DIP BUY: BUY 22850 CE · SL 22824", suppressed: true };
  ok("a suppressed ticket is WAIT", deskState(r).action === "WAIT", deskState(r).action);
}

console.log("\nthe key ignores prices, so the banner cannot flicker");
{
  const base = { ts: "12:00", pe_ce_day_cr: 3.0, actionTone: "flat", stageBadge: "" };
  const a = deskState({ ...base, actionText: "🛡️ HOLD CE · TRAIL SL → 22902", dynamicSl: 22902 });
  const b = deskState({ ...base, actionText: "🛡️ HOLD CE · TRAIL SL → 22903", dynamicSl: 22903 });
  ok("a trail ticking one point is NOT a state change", a.key === b.key);
}

console.log("\nEOD manages but never enters");
{
  const r = { ts: "15:20", pe_ce_day_cr: 3.0, actionTone: "bull", stageBadge: "", dynamicSl: 1,
              actionText: "🎯 DIP BUY: BUY 22850 CE · SL 22824" };
  ok("a ticket after 15:10 cannot ENTER", deskState(r).action !== "ENTER", deskState(r).action);
}

console.log(`\n${pass} passed, ${fail} failed\n`);
process.exit(fail === 0 ? 0 : 1);
