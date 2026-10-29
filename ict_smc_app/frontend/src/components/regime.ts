// regime.ts — does the DAY have a side, and is this signal fighting it?
//
// WHY THIS EXISTS
// The desk judged conviction from the CURRENT BAR only. In computeSmartRows, a bear ticket collected
// A+ 95% whenever `callAngleDeg >= 25 || isScissor || putAngleDeg <= -20` -- all instantaneous reads
// of one candle -- with no reference to `pe_ce_day_cr`, the day's cumulative Net PE−CE, which was
// sitting in scope two lines away. So on 29 Oct the 09:33 PULLBACK SELL was stamped 95% on a session
// that closed +3.77Cr bullish and +217 points. A bar can look bearish inside a bull trend all day;
// that is what a trend IS.
//
// Separately, every D.O.L hit emitted "book profit / trail SL" unconditionally, so each new session
// high on an expansion day told the trader the move was finished while it was still running.
//
// Both faults are the same missing idea: nothing asked whether the DAY had already picked a side.
// This module is that question, in one place, so the two callers cannot drift apart.
//
// Pure and unit-tested in scripts/test_regime.mjs.

/** Cumulative Net PE−CE, in crores, beyond which the day is treated as having chosen a direction.
 *
 *  2.0Cr is not arbitrary: on 29 Oct the running total crossed +2.15Cr at 10:30 and never looked back,
 *  closing +3.77Cr. It sat under ±1Cr through the indecisive morning (09:33 was −0.95Cr, genuinely
 *  bearish at the time). So this threshold separates "the day is still arguing" from "the day has
 *  decided" on the one session we can check it against. Revisit once the setup_log has more days. */
export const DAY_TREND_CR = 2.0;

/** Ceiling on a ticket that fights the day. Deliberately below the A (75%) band: such a ticket is not
 *  forbidden -- a trend does end eventually -- but it must never again present as A+ conviction. */
export const COUNTER_TREND_CAP = 55;

export type DayRegime = "bull" | "bear" | "none";

/** The day's chosen side from cumulative Net PE−CE (crores). "none" while it is still undecided. */
export function dayRegime(peCeDayCr: number | null | undefined): DayRegime {
  if (peCeDayCr == null || !Number.isFinite(peCeDayCr)) return "none";
  if (peCeDayCr >= DAY_TREND_CR) return "bull";
  if (peCeDayCr <= -DAY_TREND_CR) return "bear";
  return "none";
}

/** True when a signal points against a day that has already chosen the other way.
 *  An undecided day fights nothing, and a non-directional tone ("warn", "neutral") cannot fight. */
export function fightsTheDay(tone: string | null | undefined, reg: DayRegime): boolean {
  if (reg === "none") return false;
  return (tone === "bear" && reg === "bull") || (tone === "bull" && reg === "bear");
}

/** The sentence appended to a counter-trend ticket. Says what it is fighting and by how much, so the
 *  trader can overrule it knowingly rather than not noticing. */
export function counterTrendNote(peCeDayCr: number, reg: DayRegime): string {
  const dir = reg === "bull" ? "BULLISH" : "BEARISH";
  return `⚠ AGAINST THE DAY — cumulative Net PE−CE ${peCeDayCr >= 0 ? "+" : ""}${peCeDayCr.toFixed(2)}Cr (${dir}). `
       + `Counter-trend: size down or stand aside.`;
}

/** Is this a NEW session extreme in the direction the day is already running?
 *  A new high on a bullish day is expansion. The same high on an undecided day is just a target hit. */
export function isExpansionBreak(dolDir: "bull" | "bear", isSessionExtreme: boolean,
                                 reg: DayRegime): boolean {
  if (!isSessionExtreme) return false;           // an FVG fill is a target, not an expansion
  return (dolDir === "bull" && reg === "bull") || (dolDir === "bear" && reg === "bear");
}
