// gamma.ts — NIFTY gamma map + "3 confirmations" filter. Information only — never places orders.
//   GEX per strike = (CE γ × CE OI − PE γ × PE OI) × lot × spot² × 1 %   (US dealer convention: dealers long calls,
//   short puts). In India much option selling is by prop / institutions, so the SIGN is a hint, not a fact.
//   Levels: Call resistance (largest + GEX above spot) · Put support (largest − GEX below) · Flip (cumulative GEX
//   crosses 0) · Magnet (largest |GEX|). Positive total → pinning / mean-revert. Negative total → moves accelerate.
//   3 confirmations on a CLOSED candle: ① EMA 9 > 21 > 50 stacked + rising ② bull/bear FLAG at a gamma level, broken
//   ③ volume expansion (RVOL ≥ 1.5×) on the break. All 3 = QUALIFIES. Any missing = WALK AWAY.
import type { IChartApi, IPrimitivePaneRenderer, IPrimitivePaneView, ISeriesApi, ISeriesPrimitive, PrimitivePaneViewZOrder,
  SeriesAttachedParameter, SeriesType, Time } from "lightweight-charts";
import { FP_SCALE } from "./footprint";

type Target = Parameters<IPrimitivePaneRenderer["draw"]>[0];
type Row = { strike: number; ce: { oi: number; gamma: number }; pe: { oi: number; gamma: number } };
export type GLevel = { p: number; kind: "CALL_RES" | "PUT_SUP" | "FLIP" | "MAGNET"; gex: number };
export type GexMap = { strikes: { k: number; gex: number; cum: number }[]; levels: GLevel[]; total: number; regime: "positive" | "negative";
  flip: number | null; spot: number; step: number };
type C = { time: number; open: number; high: number; low: number; close: number; closed?: boolean };
export type Conf = { ok: boolean; side: "BUY" | "SELL" | null; checks: { name: string; pass: boolean; why: string }[]; level?: GLevel;
  plan?: string; head: string };

const F = (px: number) => `bold ${Math.round(px * FP_SCALE)}px Inter, system-ui, sans-serif`;
let GMAX = 1;
const cr = (v: number) => `${v >= 0 ? "+" : "−"}${Math.round(100 * Math.abs(v) / GMAX)} %`;

export function computeGex(rows: Row[], spot: number, lot = 65): GexMap | null {
  const near = rows.filter((r) => Math.abs(r.strike - spot) <= spot * 0.04 && (r.ce.gamma || r.pe.gamma)).sort((a, b) => a.strike - b.strike);
  if (near.length < 5) return null;
  const k1 = lot * spot * spot * 0.01;
  let cum = 0;
  const strikes = near.map((r) => { const g = (r.ce.gamma * r.ce.oi - r.pe.gamma * r.pe.oi) * k1; cum += g; return { k: r.strike, gex: g, cum }; });
  const total = cum;
  let flip: number | null = null;                        // where cumulative GEX crosses zero nearest spot
  for (let i = 1; i < strikes.length; i++) {
    const a = strikes[i - 1], b = strikes[i];
    if ((a.cum <= 0) !== (b.cum <= 0)) { const x = a.k + (b.k - a.k) * (0 - a.cum) / ((b.cum - a.cum) || 1);
      if (flip == null || Math.abs(x - spot) < Math.abs(flip - spot)) flip = Math.round(x); }
  }
  const above = strikes.filter((s) => s.k >= spot), below = strikes.filter((s) => s.k <= spot);
  const levels: GLevel[] = [];
  const cres = [...above].sort((a, b) => b.gex - a.gex)[0]; if (cres && cres.gex > 0) levels.push({ p: cres.k, kind: "CALL_RES", gex: cres.gex });
  const psup = [...below].sort((a, b) => Math.abs(b.gex) - Math.abs(a.gex))[0]; if (psup) levels.push({ p: psup.k, kind: "PUT_SUP", gex: psup.gex });
  const mag = [...strikes].sort((a, b) => Math.abs(b.gex) - Math.abs(a.gex))[0];
  if (mag && !levels.some((l) => l.p === mag.k)) levels.push({ p: mag.k, kind: "MAGNET", gex: mag.gex });
  if (flip != null) levels.push({ p: flip, kind: "FLIP", gex: 0 });
  return { strikes, levels, total, regime: total >= 0 ? "positive" : "negative", flip, spot,
    step: strikes.length > 1 ? strikes[1].k - strikes[0].k : 50 };
}

const LCOL: Record<GLevel["kind"], [string, string]> = { CALL_RES: ["#f87171", "CALL RESISTANCE"], PUT_SUP: ["#4ade80", "PUT SUPPORT"],
  MAGNET: ["#c084fc", "γ MAGNET"], FLIP: ["#facc15", "γ FLIP (HVL)"] };

export class GammaPrimitive implements ISeriesPrimitive<Time> {
  chart: IChartApi | null = null; series: ISeriesApi<SeriesType> | null = null;
  map: GexMap | null = null; hits: { time: number; side: "BUY" | "SELL"; px: number }[] = [];
  private views = [new MapV(this), new HitV(this)];
  private request: (() => void) | null = null;
  attached(p: SeriesAttachedParameter<Time>) { this.chart = p.chart as IChartApi; this.series = p.series; this.request = p.requestUpdate; }
  detached() { this.chart = null; this.series = null; this.request = null; }
  paneViews() { return this.views; }
  set(m: GexMap | null, hits: GammaPrimitive["hits"]) { this.map = m; this.hits = hits; this.request?.(); }
}

class MapV implements IPrimitivePaneView {
  constructor(private p: GammaPrimitive) {}
  zOrder(): PrimitivePaneViewZOrder { return "bottom"; }
  renderer(): IPrimitivePaneRenderer { return { draw: (t: Target) => {
    const { series, map } = this.p; if (!series || !map) return;
    t.useMediaCoordinateSpace(({ context: ctx, mediaSize }) => {
      const W = mediaSize.width, axis = W - 150, half = 90;    // GEX bars around a zero axis near the right edge
      const mx = Math.max(...map.strikes.map((s) => Math.abs(s.gex)), 1); GMAX = mx; const cmx = Math.max(...map.strikes.map((s) => Math.abs(s.cum)), 1);
      ctx.strokeStyle = "rgba(148,163,184,0.5)"; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(axis, 0); ctx.lineTo(axis, mediaSize.height); ctx.stroke();
      const pts: [number, number][] = [];
      for (const s of map.strikes) {
        const y0 = series.priceToCoordinate(s.k + map.step * 0.4), y1 = series.priceToCoordinate(s.k - map.step * 0.4);
        const yc = series.priceToCoordinate(s.k);
        if (y0 === null || y1 === null || yc === null) continue;
        const w = half * s.gex / mx;
        ctx.fillStyle = s.gex >= 0 ? "rgba(74,222,128,0.75)" : "rgba(249,115,22,0.8)";
        ctx.fillRect(Math.min(axis, axis + w), y0, Math.abs(w), Math.max(2, y1 - y0));
        pts.push([axis + half * s.cum / cmx, yc]);
      }
      ctx.strokeStyle = "#fde047"; ctx.lineWidth = 1.5; ctx.beginPath();
      pts.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y))); ctx.stroke();
      ctx.font = F(9); ctx.fillStyle = "#fde047"; ctx.textAlign = "center"; ctx.fillText("GEX (yellow = cumulative)", axis, 62);
      ctx.fillStyle = map.regime === "positive" ? "#4ade80" : "#fb923c";
      ctx.fillText(map.regime === "positive" ? "+γ PIN regime" : "−γ TREND regime", axis, 76);
      // key levels across the chart
      for (const l of map.levels) {
        const y = series.priceToCoordinate(l.p); if (y === null) continue;
        const [col, name] = LCOL[l.kind];
        ctx.strokeStyle = col; ctx.lineWidth = l.kind === "FLIP" ? 1.5 : 2; ctx.setLineDash(l.kind === "FLIP" ? [8, 5] : l.kind === "MAGNET" ? [2, 4] : []);
        ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(axis - half - 6, y); ctx.stroke(); ctx.setLineDash([]);
        const txt = `${name} ${l.p.toLocaleString("en-IN")}${l.gex ? ` · ${cr(l.gex)}` : ""}`;
        ctx.font = F(9.5); ctx.textAlign = "left"; ctx.textBaseline = "bottom";
        const tw = ctx.measureText(txt).width + 8;
        ctx.fillStyle = "rgba(6,11,22,0.85)"; ctx.fillRect(6, y - 15 * FP_SCALE, tw, 14 * FP_SCALE);
        ctx.fillStyle = col; ctx.fillText(txt, 10, y - 2);
      }
    });
  } }; }
}

class HitV implements IPrimitivePaneView {
  constructor(private p: GammaPrimitive) {}
  zOrder(): PrimitivePaneViewZOrder { return "top"; }
  renderer(): IPrimitivePaneRenderer { return { draw: (t: Target) => {
    const { chart, series, hits } = this.p; if (!chart || !series) return;
    t.useMediaCoordinateSpace(({ context: ctx }) => {
      for (const h of hits) {
        const x = chart.timeScale().timeToCoordinate(h.time as Time), y = series.priceToCoordinate(h.px);
        if (x === null || y === null) continue;
        const buy = h.side === "BUY", txt = buy ? "✓✓✓ γ BUY" : "✓✓✓ γ SELL", S = FP_SCALE;
        ctx.font = F(10.5); const w = ctx.measureText(txt).width + 12, hh = 17 * S, yy = buy ? y + 12 * S : y - 12 * S - hh;
        ctx.fillStyle = buy ? "#15803d" : "#b91c1c"; ctx.fillRect(x - w / 2, yy, w, hh);
        ctx.strokeStyle = "#c084fc"; ctx.lineWidth = 2; ctx.strokeRect(x - w / 2, yy, w, hh);
        ctx.fillStyle = "#fff"; ctx.textAlign = "center"; ctx.textBaseline = "middle"; ctx.fillText(txt, x, yy + hh / 2);
      }
    });
  } }; }
}

// ── 3 confirmations ──
export function emas(cs: C[], n: number): number[] {
  const k = 2 / (n + 1); const out: number[] = []; let e = cs[0]?.close ?? 0;
  cs.forEach((c, i) => { e = i ? c.close * k + e * (1 - k) : c.close; out.push(e); }); return out;
}

export function confirm3(i: number, cs: C[], e9: number[], e21: number[], e50: number[], rv: Map<number, number | null>,
  map: GexMap | null, atr: number): Conf {
  const c = cs[i], checks: Conf["checks"] = [];
  const A = atr || 20;
  // ① EMA stack
  const bullS = e9[i] > e21[i] && e21[i] > e50[i] && e21[i] > e21[Math.max(0, i - 3)];
  const bearS = e9[i] < e21[i] && e21[i] < e50[i] && e21[i] < e21[Math.max(0, i - 3)];
  const side: "BUY" | "SELL" | null = bullS ? "BUY" : bearS ? "SELL" : null;
  checks.push({ name: "EMA stack", pass: !!side, why: side ? `EMA 9 ${side === "BUY" ? ">" : "<"} 21 ${side === "BUY" ? ">" : "<"} 50, 21 ${side === "BUY" ? "rising" : "falling"} → ${side} side only`
    : `EMAs tangled (9 ${e9[i].toFixed(0)} · 21 ${e21[i].toFixed(0)} · 50 ${e50[i].toFixed(0)}) — no trend` });
  // ② flag at a gamma level, broken on this candle
  let flagWhy = "no flag: needs an impulse ≥ 1.2 ATR, then 3–8 candles of shallow (≤ 60 %) tight pullback, then a break of the flag";
  let flagOk = false, lvl: GLevel | undefined, flagLo = 0, flagHi = 0;
  if (side) {
    for (let n = 3; n <= 8 && !flagOk; n++) {
      const f = cs.slice(i - n, i), pole = cs.slice(Math.max(0, i - n - 6), i - n);
      if (f.length < n || pole.length < 3) continue;
      const fh = Math.max(...f.map((x) => x.high)), fl = Math.min(...f.map((x) => x.low));
      const ps = side === "BUY" ? Math.min(...pole.map((x) => x.low)) : Math.max(...pole.map((x) => x.high));
      const pe = side === "BUY" ? Math.max(...pole.map((x) => x.high), fh) : Math.min(...pole.map((x) => x.low), fl);
      const poleLen = Math.abs(pe - ps), retr = side === "BUY" ? (pe - fl) / (poleLen || 1) : (fh - pe) / (poleLen || 1);
      const brk = side === "BUY" ? c.close > fh : c.close < fl;
      if (poleLen >= 1.2 * A && retr <= 0.6 && fh - fl <= 0.8 * poleLen && brk) {
        const near = (map?.levels ?? []).find((l) => (side === "BUY" ? fl : fh) >= l.p - 0.4 * A && (side === "BUY" ? fl : fh) <= l.p + 0.4 * A
          || (l.p >= fl - 0.3 * A && l.p <= fh + 0.3 * A));
        flagLo = fl; flagHi = fh;
        if (near) { flagOk = true; lvl = near; flagWhy = `${side === "BUY" ? "bull" : "bear"} flag ${fl.toFixed(0)}–${fh.toFixed(0)} built AT ${LCOL[near.kind][1]} ${near.p} and broke ${side === "BUY" ? "up" : "down"} this candle`; }
        else flagWhy = `flag ${fl.toFixed(0)}–${fh.toFixed(0)} broke, but NOT at a gamma level — random flag, skip`;
      }
    }
  } else flagWhy = "not checked — no EMA stack";
  checks.push({ name: "Flag at γ level", pass: flagOk, why: flagWhy });
  // ③ volume
  const r = rv.get(c.time);
  checks.push({ name: "Volume", pass: r != null && r >= 1.5, why: r == null ? "no futures volume for this candle" : `break candle RVOL ${r.toFixed(1)}× ${r >= 1.5 ? "— expansion ✓" : "— under 1.5×, no conviction"}` });
  const ok = checks.every((x) => x.pass);
  const miss = checks.filter((x) => !x.pass).map((x) => x.name);
  let plan: string | undefined;
  if (ok && side && map) {
    const tgt = side === "BUY" ? map.levels.filter((l) => l.p > c.close + 0.3 * A).sort((a, b) => a.p - b.p)[0]
      : map.levels.filter((l) => l.p < c.close - 0.3 * A).sort((a, b) => b.p - a.p)[0];
    const sl = side === "BUY" ? flagLo : flagHi;
    plan = `${side === "BUY" ? "CE" : "PE"} · SL ${sl.toFixed(0)} (flag ${side === "BUY" ? "low" : "high"}, ${Math.abs(c.close - sl).toFixed(0)} pts) · target ${tgt ? `${LCOL[tgt.kind][1]} ${tgt.p}` : "2R"}${map.regime === "positive" ? " · +γ: expect a pin there, book fully" : " · −γ: moves run, trail the rest"}`;
  }
  const head = ok ? `QUALIFIES ✓ — ${side} (all 3 confirmations at a gamma level)` : `WALK AWAY — missing: ${miss.join(", ")}`;
  return { ok, side, checks, level: lvl, plan, head };
}

// Badges are RECORDED LIVE: only the latest closed candle is checked against the chain as it is now, and a pass is
// saved for the day. Re-checking old candles with a later chain would be look-ahead, so it never happens.
const HK = "ict_gamma_hits";
type Hit = { time: number; side: "BUY" | "SELL"; px: number };
const loadHits = (): Record<string, Hit[]> => { try { return JSON.parse(localStorage.getItem(HK) || "{}"); } catch { return {}; } };
export function gammaHits(cs: C[], rv: Map<number, number | null>, map: GexMap | null, atr: number, live: boolean): Hit[] {
  const all = loadHits();
  const day = cs.length ? new Date(cs[cs.length - 1].time * 1000).toISOString().slice(0, 10) : "";
  const today = all[day] ?? [];
  if (live && map && cs.length >= 60) {
    const i = cs.map((c) => c.closed !== false).lastIndexOf(true);
    if (i >= 60 && !today.some((h) => h.time === cs[i].time)) {
      const e9 = emas(cs, 9), e21 = emas(cs, 21), e50 = emas(cs, 50);
      const r = confirm3(i, cs, e9, e21, e50, rv, map, atr);
      const gap = 6 * ((cs[1]?.time ?? 300) - (cs[0]?.time ?? 0));
      if (r.ok && r.side && !today.some((h) => h.side === r.side && cs[i].time - h.time < gap)) {
        today.push({ time: cs[i].time, side: r.side, px: r.side === "BUY" ? cs[i].low : cs[i].high });
        const keep = Object.fromEntries(Object.entries({ ...all, [day]: today }).sort().slice(-10));
        try { localStorage.setItem(HK, JSON.stringify(keep)); } catch { /* ignore */ }
      }
    }
  }
  return Object.values(all).flat().concat(all[day] ? [] : today);
}

export const GLEVEL_NAME = LCOL;
