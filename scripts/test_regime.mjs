// scripts/test_regime.mjs — unit tests for the day-regime gate.
//
// Mirrors the pure functions in ict_smc_app/frontend/src/components/regime.ts. Run with:
//   node scripts/test_regime.mjs
//
// The cases are taken from the 29 Oct session, because that is the day the gate exists to have
// caught: a +3.77Cr bullish close on which a counter-trend PULLBACK SELL was stamped A+ 95%.

const DAY_TREND_CR = 2.0, COUNTER_TREND_CAP = 55;
const dayRegime = (cr) => (cr == null || !Number.isFinite(cr)) ? "none"
  : cr >= DAY_TREND_CR ? "bull" : cr <= -DAY_TREND_CR ? "bear" : "none";
const fightsTheDay = (tone, reg) => reg === "none" ? false
  : (tone === "bear" && reg === "bull") || (tone === "bull" && reg === "bear");
const isExpansionBreak = (dir, isExtreme, reg) => !isExtreme ? false
  : (dir === "bull" && reg === "bull") || (dir === "bear" && reg === "bear");

let pass = 0, fail = 0;
const ok = (n, c, x = "") => { if (c) { pass++; console.log(`  ok   ${n}`); }
  else { fail++; console.log(`  FAIL ${n}${x ? "  -> " + x : ""}`); } };

console.log("\nthe 29 Oct timeline, bar by bar");
// Cumulative Net PE−CE straight from that day's tape.
const tape = [
  ["09:15", -0.21, "none"], ["09:33", -0.95, "none"], ["09:57", -0.22, "none"],
  ["10:09", +0.50, "none"], ["10:18", +0.97, "none"], ["10:30", +2.15, "bull"],
  ["12:51", +2.13, "bull"], ["13:18", +2.11, "bull"], ["14:42", +4.01, "bull"],
  ["15:27", +3.77, "bull"],
];
for (const [hm, cr, want] of tape) {
  ok(`${hm}  ${cr >= 0 ? "+" : ""}${cr.toFixed(2)}Cr -> ${want}`, dayRegime(cr) === want, dayRegime(cr));
}

console.log("\nthe gate on the trades that actually happened");
{
  // 09:33 PULLBACK SELL: the day was genuinely undecided (-0.95Cr), so the gate must NOT fire.
  // Being honest about this matters -- the gate does not retro-save that loss, and claiming it would
  // is exactly the overselling this is meant to replace.
  ok("09:33 bear ticket is NOT gated (day undecided)", !fightsTheDay("bear", dayRegime(-0.95)));
  // Any bear ticket from 10:30 onward IS gated.
  ok("10:30 bear ticket is gated", fightsTheDay("bear", dayRegime(+2.15)));
  ok("13:18 bull ticket is NOT gated (with the day)", !fightsTheDay("bull", dayRegime(+2.11)));
  ok("15:27 bear ticket is gated", fightsTheDay("bear", dayRegime(+3.77)));
}

console.log("\nthresholds and non-directional tones");
ok("exactly +2.00Cr counts as bull", dayRegime(2.0) === "bull");
ok("+1.99Cr is still undecided", dayRegime(1.99) === "none");
ok("exactly -2.00Cr counts as bear", dayRegime(-2.0) === "bear");
ok("a bear day gates bull tickets", fightsTheDay("bull", dayRegime(-3.0)));
ok("'warn' tone cannot fight", !fightsTheDay("warn", dayRegime(3.77)));
ok("null tone cannot fight", !fightsTheDay(null, dayRegime(3.77)));
ok("undecided day gates nothing", !fightsTheDay("bear", dayRegime(0.5)));
ok("null Cr is undecided", dayRegime(null) === "none");
ok("NaN Cr is undecided", dayRegime(NaN) === "none");
ok("counter-trend cap sits below the A band", COUNTER_TREND_CAP < 75);

console.log("\nD.O.L: expansion vs target hit");
ok("new day HIGH on a bull day = expansion", isExpansionBreak("bull", true, dayRegime(3.77)));
ok("new day LOW on a bear day = expansion", isExpansionBreak("bear", true, dayRegime(-3.0)));
ok("new day high on an UNDECIDED day is just a target", !isExpansionBreak("bull", true, dayRegime(0.5)));
ok("new day high on a BEAR day is not expansion", !isExpansionBreak("bull", true, dayRegime(-3.0)));
ok("an FVG fill is never expansion", !isExpansionBreak("bull", false, dayRegime(3.77)));
{
  // 29 Oct 14:25: "D.O.L up day high 22952 reached" with the day at +3.70Cr. This is the alert that
  // said "book profit / trail SL" 29 minutes before the session high actually printed.
  ok("the 14:25 day-high alert flips to expansion", isExpansionBreak("bull", true, dayRegime(3.70)));
}

console.log(`\n${pass} passed, ${fail} failed\n`);
process.exit(fail === 0 ? 0 : 1);
