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
export type NEntry = { time: number; side: "BUY" | "SELL"; zone: NZone; px: number; sd: number; vwap: number; z: number;
  entry?: number; sl?: number; t1?: number; t2?: number; risk?: number; rr1?: number; rr2?: number };

const F = (px: number) => `bold ${Math.round(px * FP_SCALE)}px Inter, system-ui, sans-serif`;
const PCOL: Record<string, string> = { D: "56,189,248", W: "226,232,240", M: "251,146,60" };

// chart times are IST-shifted epoch seconds → the IST date of a candle
const dayOf = (t: number) => new Date(t * 1000).toISOString().slice(0, 10);
// Exit plan for a ● ENTRY. The premise is narrow and so is the plan: price stretched k·σ from VWAP INTO a
// level several horizons have accepted, and closed back. That is a rejection of the extension, so the trade
// is a reversion to value — which tells you exactly where it dies and exactly what it is worth.
//
//   SL  beyond the FAR side of the zone. The zone is the premise; accepting price through it means the
//       market no longer treats that level as value, and there is nothing left to revert to. Taken beyond
//       the candle's own extreme too, so a wick that already pierced the zone is not an instant stop.
//   T1  VWAP — the mean the σ stretch is measured from. By construction it sits ~k·σ away, and it is where
//       most of a reversion actually completes. Book half here.
//   T2  the next stacked zone BEYOND VWAP in the direction of travel: the next price the market has already
//       agreed is fair, and therefore the next magnet. Falls back to VWAP ± 1σ when no such zone exists.
//
// Invalidation is a CLOSE beyond the zone, not a touch — these are bands, and noise inside them is expected.
// Thresholds are consts, not rules.json: this runs in the browser and the desk exposes no config endpoint.
const SL_BUFFER_PTS = 5;     // past the zone edge, so an exact re-tap of the boundary is not a stop
const T2_FALLBACK_SD = 1;    // when nothing is stacked beyond VWAP, take 1σ past it

export function entryPlan(side: "BUY" | "SELL", entry: number, px: number, zone: NZone, vwap: number, sd: number,
  zones: NZone[]): { sl: number; t1: number; t2: number; risk: number; rr1: number; rr2: number } {
  const buy = side === "BUY";
  const far = buy ? Math.min(zone.lo, px) : Math.max(zone.hi, px);
  const sl = buy ? far - SL_BUFFER_PTS : far + SL_BUFFER_PTS;
  const t1 = vwap;
  const beyond = zones
    .filter((z) => (buy ? z.lo > vwap : z.hi < vwap))
    .sort((a, b) => (buy ? a.lo - b.lo : b.hi - a.hi))[0];
  const t2 = beyond ? beyond.mid : (buy ? vwap + T2_FALLBACK_SD * sd : vwap - T2_FALLBACK_SD * sd);
  const risk = Math.abs(entry - sl);
  const safe = risk > 0 ? risk : 1;
  return { sl, t1, t2, risk, rr1: Math.abs(t1 - entry) / safe, rr2: Math.abs(t2 - entry) / safe };
}

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
      if (z && !(i - (last[key] ?? -99) < gap)) { last[key] = i; out.push({ time: c.time, side: "BUY", zone: z, px: c.low, sd: w.sd, vwap: w.v, z: (c.low - w.v) / w.sd,
        entry: c.close, ...entryPlan("BUY", c.close, c.low, z, w.v, w.sd, zones) }); }
    } else if (c.high >= hi2) {
      const z = zones.find((z) => c.high >= z.lo && c.high <= z.hi + (z.hi - z.lo) && c.close <= z.hi);
      const key = `S${z?.mid}`;
      if (z && !(i - (last[key] ?? -99) < gap)) { last[key] = i; out.push({ time: c.time, side: "SELL", zone: z, px: c.high, sd: w.sd, vwap: w.v, z: (c.high - w.v) / w.sd,
        entry: c.close, ...entryPlan("SELL", c.close, c.high, z, w.v, w.sd, zones) }); }
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
        // Second line carries the plan. An entry marker with no stop and no target is not a trade — the
        // numbers belong ON the chart, not only in a tooltip you have to go looking for.
        const sub = e.sl != null && e.t1 != null && e.t2 != null
          ? `SL ${e.sl.toFixed(0)} · T1 ${e.t1.toFixed(0)} · T2 ${e.t2.toFixed(0)}  ${(e.rr1 ?? 0).toFixed(1)}R`
          : "";
        ctx.font = F(10.5); const w0 = ctx.measureText(txt).width;
        ctx.font = F(9); const w1 = sub ? ctx.measureText(sub).width : 0;
        const w = Math.max(w0, w1) + 12, h = (sub ? 29 : 17) * S;
        const yy = buy ? y + 10 * S : y - 10 * S - h;
        ctx.fillStyle = buy ? "#16a34a" : "#dc2626"; ctx.fillRect(x - w / 2, yy, w, h);
        ctx.strokeStyle = "#fde047"; ctx.lineWidth = 1.5; ctx.strokeRect(x - w / 2, yy, w, h);
        ctx.fillStyle = "#fff"; ctx.textAlign = "center"; ctx.textBaseline = "middle";
        ctx.font = F(10.5); ctx.fillText(txt, x, yy + (sub ? 9 : 8.5) * S);
        if (sub) { ctx.font = F(9); ctx.fillStyle = "#fef9c3"; ctx.fillText(sub, x, yy + 21 * S); }
      }
    });
  } }; }
}

// hover narrative for a candle / price
export function nodesRead(t: number, px: number | null, v: NodesView | null, ents: NEntry[], last: number | null): string[] {
  if (!v?.available) return v?.message ? [`Stacked nodes: ${v.message}`] : [];
  const out: string[] = [];
  const e = ents.find((x) => x.time === t);
  if (e) {
    const buy = e.side === "BUY";
    out.push(`● ENTRY ${buy ? "▲ BUY (CE)" : "▼ SELL (PE)"} — price stretched to ${e.z.toFixed(1)}σ from VWAP ${e.vwap.toFixed(0)} INTO the ${e.zone.label} zone ${e.zone.lo.toFixed(0)}–${e.zone.hi.toFixed(0)} and closed back. Overextension + stacked zone = reversal entry.`);
    if (e.sl != null && e.t1 != null && e.t2 != null && e.entry != null) {
      out.push(`PLAN — entry ${e.entry.toFixed(0)} · SL ${e.sl.toFixed(0)} (${(e.risk ?? 0).toFixed(0)} pts, beyond the zone) · T1 ${e.t1.toFixed(0)} = VWAP (${(e.rr1 ?? 0).toFixed(1)}R) · T2 ${e.t2.toFixed(0)} (${(e.rr2 ?? 0).toFixed(1)}R).`);
      out.push(`WHY THOSE LEVELS — the zone is the whole premise, so the stop sits beyond it: once price is accepted through, there is no agreed value left to revert to. T1 is VWAP because that is the mean the ${e.z.toFixed(1)}σ stretch is measured against, and it is where most of a reversion actually finishes. T2 is the next price several horizons already call fair.`);
      out.push(`MANAGE — book half at T1 and move the stop to entry; let the rest run to T2. Exit early on a CLOSE back beyond the zone (a wick through is normal, a close is not), or if VWAP has not been reached within roughly 8–10 candles — a reversion that stalls is being absorbed, and the edge decays with time, not with price.`);
      if ((e.rr1 ?? 0) < 1) out.push(`⚠ T1 is only ${(e.rr1 ?? 0).toFixed(1)}R away — the stop is wide relative to the reversion on offer. This is a thin version of the setup; size down or stand aside.`);
    }
  }
  const dz = dayOf(t) === v.today ? (v.zones ?? []) : (v.by_day?.[dayOf(t)] ?? []);
  const z = px != null ? dz.find((q) => px >= q.lo - 2 && px <= q.hi + 2) : undefined;
  if (z) out.push(`${z.grade === "A+" ? "🟨" : "⬜"} ${z.label} zone ${z.lo.toFixed(0)}–${z.hi.toFixed(0)} — ${z.members.map((m) => (m === "D" ? "yesterday" : m === "W" ? "last 5 sessions" : "last 20 sessions")).join(" + ")} all traded heavily here. ${z.grade === "A+" ? "Strongest reversal area." : "Good reversal area."} Fade it only when price arrives OVER-EXTENDED (beyond VWAP ±2σ); a calm drift into it can break through.`);
  if (!e && last != null && v.zones?.length) {
    const up = v.zones.filter((q) => q.lo > last).sort((a, b) => a.lo - b.lo)[0], dn = v.zones.filter((q) => q.hi < last).sort((a, b) => b.hi - a.hi)[0];
    out.push(`Nearest stacked zones: ${up ? `above ${up.label} ${up.lo.toFixed(0)} (+${(up.lo - last).toFixed(0)} pts)` : "none above"} · ${dn ? `below ${dn.label} ${dn.hi.toFixed(0)} (−${(last - dn.hi).toFixed(0)} pts)` : "none below"}.`);
  }
  return out;
}
