// deskState.ts — the one line you read while a trade is on.
//
// WHY THIS EXISTS
// The PE−CE table carries seventeen fields per row across 126 rows a session. That is the right
// density for UNDERSTANDING the day, which is a ten-minute job done after the close. It is hopeless
// for DECIDING, which is a one-second job done with money on and no working memory to spare. The
// table was being asked to do both and could only do one.
//
// So this does not change the table. It reduces what is already there into three fields:
//
//     PERMISSION  ·  REGIME  ·  ACTION
//
// PERMISSION  which side may be taken at all (and nothing else may)
// REGIME      which GAME is being played -- trend (continuation only) or chop (fade only). This is
//             the field that was never on screen, and its absence is the whole loss: chop-day
//             tactics brought to a trend day.
// ACTION      WAIT, ENTER or HOLD. Three values. Never more.
//
// No new signals: every input already exists on the row. This is a reduction, not an addition, which
// is why it cannot change a single number that is traded off.
//
// Pure and unit-tested in scripts/test_desk_state.mjs.

import { DAY_TREND_CR, dayRegime, type DayRegime } from "./regime";

export type Permission = "LONG_ONLY" | "SHORT_ONLY" | "BOTH" | "NONE";
export type Regime = "WARMUP" | "CHOP" | "TREND" | "EOD";
export type ActionKind = "WAIT" | "ENTER" | "HOLD";

export type DeskState = {
  permission: Permission;
  regime: Regime;
  action: ActionKind;
  /** The side a live ticket points at, when there is one. */
  side: "bull" | "bear" | null;
  /** Short enough to read without reading. This is the whole point. */
  line: string;
  /** Drives the colour band. */
  tone: "bull" | "bear" | "flat" | "warn";
  /** Changes when the state changes -- what the collapsed table keys off. */
  key: string;
  /** Coarser: what the VOICE keys off.
   *
   *  `key` flips every time the desk moves between WAIT and HOLD, which happens repeatedly inside a
   *  single trend as positions open and close. Speaking on each of those is how the alerts became
   *  background noise in the first place. The voice should only ever say something when the
   *  PERMISSION or the REGIME changes -- the things that change what you are allowed to do -- or
   *  when a ticket actually fires. On 29 Oct that is 11 table rows but only 6 utterances. */
  speechKey: string;
};

/** Walls are still forming before this; nothing before it is tradable. */
export const WARMUP_UNTIL = "09:45";
/** No fresh entries after this; manage only. */
export const EOD_FROM = "15:10";

/** The subset of a SmartRow this needs. Structural, so it cannot drift with the row's other fields. */
export type StateInput = {
  ts: string;
  pe_ce_day_cr: number;
  actionTone: string;
  actionText: string;
  stageBadge: string;
  dynamicSl: number | null;
  suppressed?: boolean;
};

function permissionFor(reg: DayRegime, regime: Regime): Permission {
  if (regime === "WARMUP" || regime === "EOD") return "NONE";
  if (reg === "bull") return "LONG_ONLY";
  if (reg === "bear") return "SHORT_ONLY";
  return "BOTH";
}

/** Is this side allowed to trade right now? The question the "refuse" mode asks before rendering. */
export function isSideAllowed(side: "bull" | "bear" | null, p: Permission): boolean {
  if (side == null) return true;
  if (p === "NONE") return false;
  if (p === "BOTH") return true;
  return (p === "LONG_ONLY" && side === "bull") || (p === "SHORT_ONLY" && side === "bear");
}

export function deskState(r: StateInput): DeskState {
  const reg = dayRegime(r.pe_ce_day_cr);
  const regime: Regime =
    r.ts >= EOD_FROM ? "EOD"
    : r.ts < WARMUP_UNTIL ? "WARMUP"
    : reg === "none" ? "CHOP" : "TREND";
  const permission = permissionFor(reg, regime);

  // A live ticket is a 🎯 that survived arbitration and dedup. A suppressed one is deliberately not
  // an instruction, so it can never produce an ENTER.
  const isTicket = !r.suppressed && r.actionText.includes("🎯");
  const side: "bull" | "bear" | null =
    r.actionTone === "bull" ? "bull" : r.actionTone === "bear" ? "bear" : null;
  const allowed = isSideAllowed(side, permission);

  // HOLD is a position being managed, not a new trade -- so it survives EOD, where ENTER does not.
  const isHold = /HOLD|TRAIL/i.test(r.actionText) && !isTicket;

  const action: ActionKind =
    isTicket && allowed ? "ENTER"
    : isHold ? "HOLD"
    : "WAIT";

  const permText =
    permission === "LONG_ONLY" ? "LONG ONLY"
    : permission === "SHORT_ONLY" ? "SHORT ONLY"
    : permission === "BOTH" ? "BOTH SIDES" : "NO TRADE";
  const regText =
    regime === "TREND" ? "TREND DAY · continuation only"
    : regime === "CHOP" ? "CHOP · fade only, small"
    : regime === "WARMUP" ? "WARM-UP" : "EOD";

  let actText: string;
  if (action === "ENTER") {
    // Carry the strike and stop through verbatim -- this line must be actionable on its own, without
    // the trader going back to the table to find out what to buy.
    const m = /BUY\s+(\d+)\s*(CE|PE)/i.exec(r.actionText);
    const sl = r.dynamicSl != null ? ` · SL ${r.dynamicSl}` : "";
    actText = m ? `ENTER ${m[1]} ${m[2].toUpperCase()}${sl}` : `ENTER${sl}`;
  } else if (action === "HOLD") {
    const m = /(?:TRAIL\s*SL\s*(?:→|->)?\s*|Trail SL\s*)(\d+)/i.exec(r.actionText);
    actText = m ? `HOLD · TRAIL ${m[1]}` : "HOLD";
  } else {
    // Say WHY there is nothing to do, because "WAIT" with no reason invites inventing a trade.
    actText =
      regime === "WARMUP" ? "WAIT · walls still forming"
      : regime === "EOD" ? "MANAGE ONLY · no fresh entries"
      : isTicket && !allowed ? `WAIT · ${side === "bear" ? "short" : "long"} setup BLOCKED by the day`
      : regime === "TREND" ? `WAIT · for a ${permission === "LONG_ONLY" ? "dip" : "pullback"}`
      : "WAIT · no edge";
  }

  const tone: DeskState["tone"] =
    permission === "LONG_ONLY" ? "bull"
    : permission === "SHORT_ONLY" ? "bear"
    : permission === "NONE" ? "warn" : "flat";

  return {
    permission, regime, action, side, tone,
    line: `${permText} · ${regText} · ${actText}`,
    // The key deliberately EXCLUDES prices: a trail level ticking 22902 -> 22903 is not a state
    // change, and if it were, the banner would flicker and the speech would never shut up.
    key: `${permission}|${regime}|${action}`,
    speechKey: `${permission}|${regime}${action === "ENTER" ? "|ENTER" : ""}`,
  };
}

/** Indices whose state DIFFERS from the row before — what the collapsed table shows.
 *  Rows are newest-first, so this walks them chronologically and returns newest-first indices. */
export function stateChangeIndices(rowsNewestFirst: StateInput[]): number[] {
  const out: number[] = [];
  let prev: string | null = null;
  for (let i = rowsNewestFirst.length - 1; i >= 0; i--) {
    const k = deskState(rowsNewestFirst[i]).key;
    if (k !== prev) { out.push(i); prev = k; }
  }
  return out.reverse();
}

/** What the voice says on a state change. Short: it has to land in one hearing, while you are busy. */
export function spokenFor(s: DeskState): string {
  if (s.action === "ENTER") return `Enter. ${s.permission === "LONG_ONLY" ? "Long" : "Short"}.`;
  switch (s.permission) {
    case "LONG_ONLY": return "Trend day. Long only. Do not fade.";
    case "SHORT_ONLY": return "Trend day. Short only. Do not fade.";
    case "BOTH": return "Chop. Both sides. Small size.";
    case "NONE": return s.regime === "EOD" ? "Closing window. Manage only." : "Warm up. No trade.";
  }
}

export { DAY_TREND_CR };
