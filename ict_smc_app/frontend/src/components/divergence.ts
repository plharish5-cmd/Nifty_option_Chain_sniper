// divergence.ts — client for /api/desk/divergence (RSI watch/trigger/expire + VIX context).
//
// The engine's whole point is that a divergence is a WATCH, not a trade: it ARMS with a trigger
// level and becomes something only when price CLOSES through it. So this file keeps that
// distinction visible all the way to the screen. An ARMED watch is rendered quietly and never
// speaks; only STRUCTURE_CONFIRMED is worth interrupting the trader for.
//
// On 29 Oct the OI divergence raised five bearish warnings with no such gate, on a session that ran
// +217 points, and one of them became an A+ short that stopped out. Rendering an armed watch as
// though it were a signal would rebuild that mistake in a nicer font.

import { useEffect, useRef, useState } from "react";

export type DivState =
  | "ARMED" | "STRUCTURE_CONFIRMED" | "INVALIDATED" | "EXPIRED" | "VETOED";

export type DivEvent = {
  type: "REGULAR_BEARISH" | "REGULAR_BULLISH" | "HIDDEN_BEARISH" | "HIDDEN_BULLISH";
  kind: "high" | "low";
  side: "short" | "long";
  is_reversal: boolean;
  pivot_from_time: string; pivot_to_time: string;
  price_from: number; price_to: number;
  rsi_from: number; rsi_to: number;
  armed_time: string;
  structure_trigger: number;
  invalidation: number;
  state: DivState;
  vetoed: boolean;
  action: string;
  entry?: number; sl?: number; risk_pts?: number;
  resolved_time?: string;
};

export type VixRead = {
  ok: boolean;
  state: "NORMAL_RISK_ON" | "NORMAL_RISK_OFF" | "DISTRIBUTION_WARNING"
       | "CAPITULATION_WATCH" | "FLAT" | "UNAVAILABLE";
  label: string; available: boolean; detail: string;
  price_move_pts: number | null; vix_move_pct: number | null;
  vix_from: number | null; vix_to: number | null;
  is_context_only: boolean;
};

export type DivergencePayload = {
  ok: boolean; tf: string; day_regime: string;
  rsi: {
    ok: boolean; message: string; current_rsi: number | null; total_bars: number;
    events: DivEvent[]; active: DivEvent | null; continuation: DivEvent | null;
    pivots?: { highs: number; lows: number; high_times: string[]; low_times: string[] };
    rejected?: Record<string, number>;
    counts?: Record<string, number>;
  };
  vix: VixRead;
};

export function useDivergence(tick: number, tf: string, dayRegime: string) {
  const [d, setD] = useState<DivergencePayload | null>(null);
  useEffect(() => {
    let dead = false;
    fetch(`/api/desk/divergence?tf=${encodeURIComponent(tf)}&day_regime=${encodeURIComponent(dayRegime)}`)
      .then((r) => r.json())
      .then((j) => { if (!dead) setD(j); })
      .catch(() => { /* read-only enhancement: the desk behaves identically without it */ });
    return () => { dead = true; };
  }, [tick, tf, dayRegime]);
  return d;
}

/** Short text for the state-bar chip. Null when there is nothing worth a pixel. */
export function chipFor(d: DivergencePayload | null): { text: string; tone: string; title: string } | null {
  if (!d?.rsi) return null;
  const a = d.rsi.active;
  if (a) {
    const dir = a.side === "short" ? "bearish" : "bullish";
    const kindTxt = a.is_reversal ? "reversal" : "continuation";
    return {
      text: `RSI ${dir} ${kindTxt} · ${a.state === "STRUCTURE_CONFIRMED" ? "CONFIRMED" : `watch, trigger ${a.structure_trigger}`}`,
      tone: a.state === "STRUCTURE_CONFIRMED" ? (a.side === "short" ? "bear" : "bull") : "warn",
      title: `${a.type.replace(/_/g, " ")} between ${a.pivot_from_time} (${a.price_from}, RSI ${a.rsi_from}) `
           + `and ${a.pivot_to_time} (${a.price_to}, RSI ${a.rsi_to}).\n\n${a.action}\n\n`
           + `Invalidation ${a.invalidation}. A watch is not a trade: nothing here is an instruction `
           + `until price CLOSES through the trigger.`,
    };
  }
  const vetoed = d.rsi.events?.filter((e) => e.state === "VETOED").length ?? 0;
  if (vetoed > 0) {
    return {
      text: `RSI ${vetoed} counter-trend div · vetoed`,
      tone: "flat",
      title: `${vetoed} reversal divergence(s) point against the day's decided direction, so they are `
           + `recorded but cannot confirm. On 29 Oct five such warnings fired on a +217 point day.`,
    };
  }
  if (d.rsi.ok && d.rsi.current_rsi != null) {
    return { text: `RSI ${d.rsi.current_rsi} · no divergence`, tone: "flat",
             title: `No qualifying divergence on ${d.rsi.total_bars} closed ${d.tf} bars. `
                  + `Pivots found: ${d.rsi.pivots?.highs ?? "?"} highs / ${d.rsi.pivots?.lows ?? "?"} lows.` };
  }
  return { text: "RSI —", tone: "flat", title: d.rsi.message || "no RSI read yet" };
}

/** VIX chip. Only the two informative states get colour; normal and flat stay quiet. */
export function vixChipFor(d: DivergencePayload | null): { text: string; tone: string; title: string } | null {
  const v = d?.vix;
  if (!v) return null;
  if (v.state === "UNAVAILABLE") {
    return { text: "VIX —", tone: "flat", title: v.detail };
  }
  const loud = v.state === "DISTRIBUTION_WARNING" || v.state === "CAPITULATION_WATCH";
  return {
    text: loud ? v.label : `VIX ${v.vix_to ?? "—"} (${(v.vix_move_pct ?? 0) >= 0 ? "+" : ""}${v.vix_move_pct ?? 0}%)`,
    tone: v.state === "DISTRIBUTION_WARNING" ? "warn" : v.state === "CAPITULATION_WATCH" ? "bull" : "flat",
    title: `${v.detail}\n\nContext only — VIX has no entry, stop or invalidation level and never fires a ticket.`,
  };
}

/**
 * Fire a callback the moment a watch CONFIRMS — never when one merely arms.
 *
 * Keyed on the event's identity plus its state, so a confirm speaks exactly once and a re-poll of the
 * same confirmed event is silent.
 */
export function useConfirmAlert(d: DivergencePayload | null, onConfirm: (e: DivEvent) => void) {
  const seen = useRef<Set<string>>(new Set());
  const primed = useRef(false);
  useEffect(() => {
    const evs = d?.rsi?.events ?? [];
    const confirmed = evs.filter((e) => e.state === "STRUCTURE_CONFIRMED");
    // Don't announce history on first load; only transitions seen while watching.
    if (!primed.current) {
      for (const e of confirmed) seen.current.add(`${e.pivot_to_time}|${e.type}`);
      primed.current = true;
      return;
    }
    for (const e of confirmed) {
      const k = `${e.pivot_to_time}|${e.type}`;
      if (seen.current.has(k)) continue;
      seen.current.add(k);
      onConfirm(e);
    }
  }, [d, onConfirm]);
}
