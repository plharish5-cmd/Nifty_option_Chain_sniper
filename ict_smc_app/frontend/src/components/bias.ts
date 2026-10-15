// bias.ts — 7-factor confluence per CLOSED candle: FVG/IFVG · Order block · CRT/TBS · Footprint · RVOL · VWAP · Structure.
// Each factor votes bull / bear / none. A candle gets a BUY/SELL bias badge when ≥ 5 of 7 agree and ≤ 1 disagrees.
// Information only — not backtested, never an order, never changes Setups A–D or paper trades.
import type { Analysis, Candle } from "../api";
import type { FpBar } from "./footprint";

export type Vote = "bull" | "bear" | null;
export const FACTORS = ["FVG/IFVG", "Order block", "CRT/TBS", "Footprint", "RVOL", "VWAP", "Structure"] as const;
export type Factor = typeof FACTORS[number];
export type BiasRow = { time: number; o: number; h: number; l: number; c: number; votes: Record<Factor, Vote>; why: Record<Factor, string>;
  bull: number; bear: number; avail: number; verdict: "BUY" | "SELL" | "MIXED"; strong: boolean };

const FLOW = new Set(["STACKED_BUY", "STACKED_SELL", "ACCUMULATION", "DISTRIBUTION", "INITIATIVE", "MANIPULATION", "ABSORPTION"]);

export function computeBias(candles: Candle[], a: Analysis | null, fp: FpBar[], barSec: number, need = 5): BiasRow[] {
  if (!a) return [];
  const closed = candles.filter((c) => c.closed !== false);
  const zones = a.zones ?? [];
  const vbar = new Map((a.volume?.bars ?? []).map((b) => [b.time, b]));
  const vwap = new Map((a.volume?.vwap ?? []).map((p) => [p.time, p.v]));
  const fpm = new Map(fp.map((b) => [b.time, b]));
  const struct = (a.markers ?? []).filter((m) => m.kind === "BOS" || m.kind === "MSS").sort((x, y) => x.time - y.time);
  const crt = (a.crt?.markers ?? []).filter((m) => m.kind === "CRT_ENTRY" || m.kind === "CRT_TBS").sort((x, y) => x.time - y.time);
  const ifvgSig = (a.markers ?? []).filter((m) => m.kind === "IFVG_BUY" || m.kind === "IFVG_SELL");

  const atr = a.atr || 20;
  const zoneVote = (types: string[], c: Candle): [Vote, string] => {
    let bull = "", bear = "", bullD = Infinity, bearD = Infinity;
    for (const z of zones) {
      if (!types.includes(z.type) || z.from >= c.time || z.to < c.time) continue;   // zone alive on this candle
      if (z.dir === "bull" && c.close >= z.bottom) {           // support below (tested & held, or within 1 ATR above it)
        const d = c.low <= z.top ? 0 : c.close - z.top;
        if (d <= atr && d < bullD) { bullD = d; bull = `${z.type} support ${z.bottom.toFixed(0)}–${z.top.toFixed(0)} ${d === 0 ? "tested & held" : "just below"}`; }
      }
      if (z.dir === "bear" && c.close <= z.top) {
        const d = c.high >= z.bottom ? 0 : z.bottom - c.close;
        if (d <= atr && d < bearD) { bearD = d; bear = `${z.type} resistance ${z.bottom.toFixed(0)}–${z.top.toFixed(0)} ${d === 0 ? "tested & held" : "just above"}`; }
      }
    }
    if (bull && bear) return bullD < bearD ? ["bull", bull] : bearD < bullD ? ["bear", bear] : [null, "between support and resistance"];
    return bull ? ["bull", bull] : bear ? ["bear", bear] : [null, ""];
  };
  const hasVol = (a.volume?.bars ?? []).length > 0, hasVwap = (a.volume?.vwap ?? []).length > 0, hasFp = fp.length > 0;

  return closed.slice(-80).map((c) => {
    const votes = {} as Record<Factor, Vote>, why = {} as Record<Factor, string>;
    // 1 FVG / IFVG (zone held, or an IFVG buy/sell signal on this or the previous 2 bars)
    let [v, w] = zoneVote(["FVG", "IFVG"], c);
    const sig = ifvgSig.find((m) => m.time <= c.time && m.time >= c.time - 2 * barSec);
    if (sig) { v = sig.kind === "IFVG_BUY" ? "bull" : "bear"; w = sig.kind.replace("_", " "); }
    votes["FVG/IFVG"] = v; why["FVG/IFVG"] = w;
    // 2 Order block / breaker / RJB
    [v, w] = zoneVote(["OB", "BREAKER", "RJB"], c); votes["Order block"] = v; why["Order block"] = w;
    // 3 CRT / TBS (last 6 bars)
    const cr = [...crt].reverse().find((m) => m.time <= c.time && m.time >= c.time - 6 * barSec);
    votes["CRT/TBS"] = cr ? cr.dir : null; why["CRT/TBS"] = cr ? `${cr.kind === "CRT_ENTRY" ? "CRT entry" : "TBS"} ${cr.at}` : "";
    // 4 Footprint (flow tag + delta agree, or a strong delta ≥ 15 %)
    const f = fpm.get(c.time);
    let fv: Vote = null, fw = "";
    if (f && f.rows.length) {
      const dd: Vote = f.delta > 0 ? "bull" : f.delta < 0 ? "bear" : null;
      const tag = f.tags.find((t) => FLOW.has(t.k) && t.dir === dd);
      if (tag) { fv = dd; fw = `${tag.k.toLowerCase()} · Δ ${Math.round(f.delta / 1000)}K`; }
      else if (dd && Math.abs(f.delta_pct ?? 0) >= 15) { fv = dd; fw = `Δ ${f.delta_pct}%`; }
    }
    votes.Footprint = fv; why.Footprint = fw;
    // 5 RVOL ≥ 1.2× → votes with the candle colour
    const vb = vbar.get(c.time);
    votes.RVOL = vb?.rvol != null && vb.rvol >= 1.2 && c.close !== c.open ? (c.close > c.open ? "bull" : "bear") : null;
    why.RVOL = vb?.rvol != null ? `RVOL ${vb.rvol.toFixed(1)}×` : "";
    // 6 VWAP side (futures VWAP, basis-adjusted)
    const vw = vwap.get(c.time);
    votes.VWAP = vw != null ? (c.close > vw ? "bull" : c.close < vw ? "bear" : null) : null;
    why.VWAP = vw != null ? `close ${c.close > vw ? "above" : "below"} VWAP ${vw.toFixed(0)}` : "";
    // 7 Structure: direction of the last BOS / MSS so far
    const s = [...struct].reverse().find((m) => m.time <= c.time);
    votes.Structure = s ? s.dir : null; why.Structure = s ? `last ${s.kind} ${s.dir === "bull" ? "▲" : "▼"} ${s.at}` : "";

    // factors with no data today don't count (e.g. futures volume missing → RVOL & VWAP can't vote)
    if (!hasVol) why.RVOL = "no futures volume today";
    if (!hasVwap) why.VWAP = "no VWAP (futures volume missing)";
    if (!hasFp) why.Footprint = "no recorded ticks for this tf";
    const avail = 7 - (hasVol ? 0 : 1) - (hasVwap ? 0 : 1) - (hasFp ? 0 : 1);
    const vals = Object.values(votes);
    const bull = vals.filter((x) => x === "bull").length, bear = vals.filter((x) => x === "bear").length;
    const req = Math.min(need, avail - 1);                       // with 5 factors available, need 4; with 7, need 5
    const verdict = bull >= req && bear <= 1 ? "BUY" : bear >= req && bull <= 1 ? "SELL" : "MIXED";
    return { time: c.time, o: c.open, h: c.high, l: c.low, c: c.close, votes, why, bull, bear, avail, verdict,
      strong: Math.max(bull, bear) >= avail - 1 && Math.max(bull, bear) >= 5 };
  });
}
