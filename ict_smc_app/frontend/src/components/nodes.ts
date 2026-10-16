// nodes.ts — STACKED volume nodes (D / W / M overlap) + "overextension + zone = entry". Information only.
//   bands      : where the previous day, 5-session and 20-session volume peaks overlap (A+ = all three, gold)
//   profiles   : the three profiles drawn small at the right edge (blue D · white W · orange M) so you SEE the overlap
//   ENTRY ▲/▼  : a CLOSED candle that stretched beyond VWAP ±2σ INTO a stacked zone and closed back out of the stretch
import type { IChartApi, IPrimitivePaneRenderer, IPrimitivePaneView, ISeriesApi, ISeriesPrimitive, PrimitivePaneViewZOrder,
  SeriesAttachedParameter, SeriesType, Time } from "lightweight-charts";
import { FP_SCALE } from "./footprint";

type Target = Parameters<IPrimitivePaneRenderer["draw"]>[0];
export type NZone = { lo: number; hi: number; mid: number; members: string[]; grade: "A+" | "A"; label: string };
export type NProf = { from: string; to: string; sessions: number; poc: number; vah: number; val: number; peaks: number[]; hist: [number, number][] };
export type NodesView = { available: boolean; message?: string; zones?: NZone[]; profiles?: Record<string, NProf>; note?: string; today?: string; by_day?: Record<string, NZone[]> };
export type NEntry = { time: number; side: "BUY" | "SELL"; zone: NZone; px: number; sd: number; vwap: number; z: number };

const F = (px: number) => `bold ${Math.round(px * FP_SCALE)}px Inter, system-ui, sans-serif`;
const PCOL: Record<string, string> = { D: "56,189,248", W: "226,232,240", M: "251,146,60" };

// chart times are IST-shifted epoch seconds → the IST date of a candle
const dayOf = (t: number) => new Date(t * 1000).toISOString().slice(0, 10);
export function findEntries(candles: { time: number; open: number; high: number; low: number; close: number; closed?: boolean }[],
  vwap: { time: number; v: number; sd: number }[], v: NodesView, k = 2, gap = 6): NEntry[] {
  const vm = new Map(vwap.map((p) => [p.time, p]));
  const out: NEntry[] = [];
  const last: Record<string, number> = {};
  for (let i = 0; i < candles.length; i++) {
    const c = candles[i];
    if (c.closed === false) continue;
    const d = dayOf(c.time);
    // zones that EXISTED that day: today's live zones, or the zones rebuilt from sessions before that day
    const zones = d === v.today ? (v.zones ?? []) : (v.by_day?.[d] ?? []);
    if (!zones.length) continue;
    const w = vm.get(c.time); if (!w || !(w.sd > 0)) continue;
    const lo2 = w.v - k * w.sd, hi2 = w.v + k * w.sd;
    if (c.low <= lo2) {              // stretched down into a zone, closed back above the zone's low (rejection)
      const z = zones.find((z) => c.low <= z.hi && c.low >= z.lo - (z.hi - z.lo) && c.close >= z.lo);
      const key = `B${z?.mid}`;
      if (z && !(i - (last[key] ?? -99) < gap)) { last[key] = i; out.push({ time: c.time, side: "BUY", zone: z, px: c.low, sd: w.sd, vwap: w.v, z: (c.low - w.v) / w.sd }); }
    } else if (c.high >= hi2) {
      const z = zones.find((z) => c.high >= z.lo && c.high <= z.hi + (z.hi - z.lo) && c.close <= z.hi);
      const key = `S${z?.mid}`;
      if (z && !(i - (last[key] ?? -99) < gap)) { last[key] = i; out.push({ time: c.time, side: "SELL", zone: z, px: c.high, sd: w.sd, vwap: w.v, z: (c.high - w.v) / w.sd }); }
    }
  }
  return out;
}

export class NodesPrimitive implements ISeriesPrimitive<Time> {
  chart: IChartApi | null = null; series: ISeriesApi<SeriesType> | null = null;
  view: NodesView | null = null; entries: NEntry[] = [];
  private views = [new BandsV(this), new TopV(this)];
  private request: (() => void) | null = null;
  attached(p: SeriesAttachedParameter<Time>) { this.chart = p.chart as IChartApi; this.series = p.series; this.request = p.requestUpdate; }
  detached() { this.chart = null; this.series = null; this.request = null; }
  paneViews() { return this.views; }
  set(v: NodesView | null, e: NEntry[]) { this.view = v; this.entries = e; this.request?.(); }
}

class BandsV implements IPrimitivePaneView {
  constructor(private p: NodesPrimitive) {}
  zOrder(): PrimitivePaneViewZOrder { return "bottom"; }
  renderer(): IPrimitivePaneRenderer { return { draw: (t: Target) => {
    const { series, view } = this.p; if (!series || !view?.available) return;
    t.useMediaCoordinateSpace(({ context: ctx, mediaSize }) => {
      const W = mediaSize.width;
      // mini profiles at the right edge, side by side (D | W | M), each scaled to its own max
      const keys = ["D", "W", "M"].filter((k) => view.profiles?.[k]);
      const colW = Math.min(70, W * 0.07), x0 = W - 90 - colW * keys.length;
      keys.forEach((k, i) => {
        const p = view.profiles![k]; const mx = Math.max(...p.hist.map((h) => h[1]), 1);
        const step = p.hist.length > 1 ? p.hist[1][0] - p.hist[0][0] : 5;
        for (const [px, v] of p.hist) {
          const y0 = series.priceToCoordinate(px + step), y1 = series.priceToCoordinate(px);
          if (y0 === null || y1 === null || y1 < -5 || y0 > mediaSize.height + 5) continue;
          const isPeak = p.peaks.some((q) => Math.abs(q - (px + step / 2)) <= step);
          ctx.fillStyle = `rgba(${PCOL[k]},${isPeak ? 0.85 : 0.35})`;
          ctx.fillRect(x0 + i * colW, y0, Math.max(1, (colW - 6) * v / mx), Math.max(1, y1 - y0 - 0.5));
        }
        const yt = 14; ctx.font = F(9); ctx.textAlign = "left"; ctx.fillStyle = `rgb(${PCOL[k]})`;
        ctx.fillText(k === "D" ? "Day" : k === "W" ? "Week(5)" : "Month(20)", x0 + i * colW, yt);
      });
      // stacked zones across the whole chart
      for (const z of view.zones ?? []) {
        const y0 = series.priceToCoordinate(z.hi), y1 = series.priceToCoordinate(z.lo);
        if (y0 === null || y1 === null || y1 < -10 || y0 > mediaSize.height + 10) continue;
        const ap = z.grade === "A+";
        ctx.fillStyle = ap ? "rgba(250,204,21,0.16)" : "rgba(148,163,184,0.14)";
        ctx.fillRect(0, y0, W, Math.max(3, y1 - y0));
        ctx.strokeStyle = ap ? "rgba(250,204,21,0.85)" : "rgba(148,163,184,0.6)"; ctx.lineWidth = ap ? 1.5 : 1;
        ctx.strokeRect(0, y0, W, Math.max(3, y1 - y0));
        ctx.font = F(ap ? 10 : 9); ctx.textAlign = "right"; ctx.textBaseline = "middle";
        ctx.fillStyle = ap ? "#fde047" : "#cbd5e1";
        ctx.fillText(`${z.label}  ${z.mid.toFixed(0)}`, x0 - 8, (y0 + y1) / 2);
      }
    });
  } }; }
}

class TopV implements IPrimitivePaneView {
  constructor(private p: NodesPrimitive) {}
  zOrder(): PrimitivePaneViewZOrder { return "top"; }
  renderer(): IPrimitivePaneRenderer { return { draw: (t: Target) => {
    const { chart, series, entries } = this.p; if (!chart || !series || !entries.length) return;
    const ts = chart.timeScale();
    t.useMediaCoordinateSpace(({ context: ctx }) => {
      const S = FP_SCALE;
      for (const e of entries) {
        const x = ts.timeToCoordinate(e.time as Time), y = series.priceToCoordinate(e.px);
        if (x === null || y === null) continue;
        const buy = e.side === "BUY", txt = buy ? "● ENTRY ▲" : "● ENTRY ▼";
        ctx.font = F(10.5); const w = ctx.measureText(txt).width + 12, h = 17 * S;
        const yy = buy ? y + 10 * S : y - 10 * S - h;
        ctx.fillStyle = buy ? "#16a34a" : "#dc2626"; ctx.fillRect(x - w / 2, yy, w, h);
        ctx.strokeStyle = "#fde047"; ctx.lineWidth = 1.5; ctx.strokeRect(x - w / 2, yy, w, h);
        ctx.fillStyle = "#fff"; ctx.textAlign = "center"; ctx.textBaseline = "middle"; ctx.fillText(txt, x, yy + h / 2);
      }
    });
  } }; }
}

// hover narrative for a candle / price
export function nodesRead(t: number, px: number | null, v: NodesView | null, ents: NEntry[], last: number | null): string[] {
  if (!v?.available) return v?.message ? [`Stacked nodes: ${v.message}`] : [];
  const out: string[] = [];
  const e = ents.find((x) => x.time === t);
  if (e) out.push(`● ENTRY ${e.side === "BUY" ? "▲ BUY (CE)" : "▼ SELL (PE)"} — price stretched to ${e.z.toFixed(1)}σ from VWAP ${e.vwap.toFixed(0)} INTO the ${e.zone.label} zone ${e.zone.lo.toFixed(0)}–${e.zone.hi.toFixed(0)} and closed back. Overextension + stacked zone = reversal entry. SL just beyond the zone (${(e.side === "BUY" ? e.zone.lo - 5 : e.zone.hi + 5).toFixed(0)}), first target VWAP ${e.vwap.toFixed(0)}.`);
  const dz = dayOf(t) === v.today ? (v.zones ?? []) : (v.by_day?.[dayOf(t)] ?? []);
  const z = px != null ? dz.find((q) => px >= q.lo - 2 && px <= q.hi + 2) : undefined;
  if (z) out.push(`${z.grade === "A+" ? "🟨" : "⬜"} ${z.label} zone ${z.lo.toFixed(0)}–${z.hi.toFixed(0)} — ${z.members.map((m) => (m === "D" ? "yesterday" : m === "W" ? "last 5 sessions" : "last 20 sessions")).join(" + ")} all traded heavily here. ${z.grade === "A+" ? "Strongest reversal area." : "Good reversal area."} Fade it only when price arrives OVER-EXTENDED (beyond VWAP ±2σ); a calm drift into it can break through.`);
  if (!e && last != null && v.zones?.length) {
    const up = v.zones.filter((q) => q.lo > last).sort((a, b) => a.lo - b.lo)[0], dn = v.zones.filter((q) => q.hi < last).sort((a, b) => b.hi - a.hi)[0];
    out.push(`Nearest stacked zones: ${up ? `above ${up.label} ${up.lo.toFixed(0)} (+${(up.lo - last).toFixed(0)} pts)` : "none above"} · ${dn ? `below ${dn.label} ${dn.hi.toFixed(0)} (−${(last - dn.hi).toFixed(0)} pts)` : "none below"}.`);
  }
  return out;
}
