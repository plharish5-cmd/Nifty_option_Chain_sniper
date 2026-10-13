// zones.ts — a Lightweight Charts v5 series primitive that draws rectangles (order blocks, FVGs, OTE band,
// premium/discount) and labelled horizontal segments (liquidity pools) UNDER the candles.
import type {
  IChartApi, IPrimitivePaneRenderer, IPrimitivePaneView, ISeriesApi, ISeriesPrimitive, SeriesAttachedParameter,
  SeriesType, Time,
} from "lightweight-charts";

export interface Box { from: number; to: number; top: number; bottom: number; fill: string; stroke?: string; label?: string; labelColor?: string; dash?: boolean }
export interface Seg { from: number; price: number; color: string; label: string; dash?: boolean; width?: number }

type MediaScope = { context: CanvasRenderingContext2D; mediaSize: { width: number; height: number } };
type Target = { useMediaCoordinateSpace: (f: (s: MediaScope) => void) => void };

class Renderer implements IPrimitivePaneRenderer {
  rects: [number, number, number, number][] = [];
  constructor(private p: ZonesPrimitive) {}
  draw() { /* everything is drawn in drawBackground so candles stay on top */ }
  drawBackground(target: Target) {
    const { chart, series } = this.p;
    if (!chart || !series) return;
    const ts = chart.timeScale();
    target.useMediaCoordinateSpace(({ context: ctx, mediaSize }) => {
      const W = mediaSize.width;
      const x = (t: number, fallback: number) => {
        const c = ts.timeToCoordinate(t as Time);
        if (c !== null) return c as number;
        // outside loaded data: before → left edge, after → right edge
        const r = ts.getVisibleRange();
        if (!r) return fallback;
        return t < (r.from as number) ? 0 : W;
      };
      const y = (p: number) => series.priceToCoordinate(p);
      ctx.font = "10px Inter, system-ui, sans-serif";
      this.rects = [];
      for (const b of this.p.boxes) {
        const y1 = y(b.top), y2 = y(b.bottom);
        if (y1 === null || y2 === null) continue;
        const x1 = x(b.from, 0), x2 = b.to >= this.p.lastTime ? W : x(b.to, W);
        if (x2 < 0 || x1 > W) continue;
        const live = b.to >= this.p.lastTime;
        ctx.globalAlpha = live ? 1 : 0.45;                                   // finished / mitigated zones fade back
        ctx.fillStyle = b.fill;
        ctx.fillRect(x1, Math.min(y1, y2), Math.max(1, x2 - x1), Math.max(1, Math.abs(y2 - y1)));
        if (b.stroke) {
          ctx.strokeStyle = b.stroke;
          ctx.setLineDash(b.dash ? [4, 3] : []);
          ctx.lineWidth = 1;
          ctx.strokeRect(x1 + 0.5, Math.min(y1, y2) + 0.5, Math.max(1, x2 - x1) - 1, Math.max(1, Math.abs(y2 - y1)) - 1);
          ctx.setLineDash([]);
        }
        ctx.globalAlpha = 1;
        if (b.label && live && x2 - x1 >= 60) {                             // label live zones only, as a chip, never overlapping
          const maxW = Math.min(x2 - x1 - 8, 260);
          let txt = b.label;
          while (txt.length > 4 && ctx.measureText(txt).width > maxW) txt = txt.slice(0, -2);
          if (txt !== b.label) txt = txt.replace(/.$/, "…");
          const tw = ctx.measureText(txt).width;
          const lx = Math.max(4, x1 + 4), ly = Math.min(y1, y2) + 2;
          const hit = this.rects.some((r) => lx < r[0] + r[2] && lx + tw + 6 > r[0] && ly < r[1] + r[3] && ly + 13 > r[1]);
          if (!hit && ly > 0) {
            this.rects.push([lx, ly, tw + 6, 13]);
            ctx.fillStyle = "rgba(6,11,22,0.72)";
            ctx.fillRect(lx - 2, ly, tw + 6, 13);
            ctx.fillStyle = b.labelColor || "#cbd5e1";
            ctx.fillText(txt, lx + 1, ly + 10);
          }
        }
      }
      // session separators: a faint vertical line + date at each new trading day
      ctx.strokeStyle = "rgba(148,163,184,0.18)";
      ctx.setLineDash([2, 4]);
      for (const d of this.p.days) {
        const c = ts.timeToCoordinate(d as Time);
        if (c === null) continue;
        ctx.beginPath(); ctx.moveTo(Math.round(c) + 0.5, 0); ctx.lineTo(Math.round(c) + 0.5, mediaSize.height); ctx.stroke();
        const dt = new Date(d * 1000);
        const lab = dt.toLocaleDateString("en-IN", { day: "2-digit", month: "short", timeZone: "UTC" });
        ctx.fillStyle = "rgba(148,163,184,0.55)";
        ctx.fillText(lab, c + 4, mediaSize.height - 6);
      }
      ctx.setLineDash([]);
      // horizontal levels: draw every line, then lay labels out so they never overlap — merge labels on the same
      // price, push neighbours apart, and drop a label that would sit too far from its own line
      const labs: { y: number; text: string; color: string }[] = [];
      for (const s of this.p.segs) {
        const yy = y(s.price);
        if (yy === null || yy < -20 || yy > mediaSize.height + 20) continue;
        const x1 = Math.max(0, x(s.from, 0));
        ctx.strokeStyle = s.color;
        ctx.lineWidth = s.width ?? 1;
        ctx.setLineDash(s.dash ? [6, 4] : []);
        ctx.beginPath();
        ctx.moveTo(x1, Math.round(yy) + 0.5);
        ctx.lineTo(W, Math.round(yy) + 0.5);
        ctx.stroke();
        ctx.setLineDash([]);
        const same = labs.find((l) => Math.abs(l.y - yy) < 2.5);
        if (same) { if (!same.text.includes(s.label.split(" ")[0])) same.text = `${s.label.replace(/\s[\d,.]+$/, "")} · ${same.text}`; }
        else labs.push({ y: yy, text: s.label, color: s.color });
      }
      labs.sort((a, b) => a.y - b.y);
      const H = 13;
      let prevBottom = -Infinity;
      for (const l of labs) {
        const want = l.y - H / 2;
        const top = Math.max(want, prevBottom + 1);
        if (top - want > 18) continue;                     // too crowded here → hover the line / see the axis instead
        prevBottom = top + H;
        const tw = ctx.measureText(l.text).width;
        ctx.fillStyle = "rgba(6,11,22,0.88)";
        ctx.fillRect(W - tw - 12, top, tw + 10, H);
        ctx.fillStyle = l.color;
        ctx.fillRect(W - tw - 12, top, 2, H);
        ctx.fillText(l.text, W - tw - 6, top + 10);
      }
    });
  }
}

class View implements IPrimitivePaneView {
  private r: Renderer;
  constructor(p: ZonesPrimitive) { this.r = new Renderer(p); }
  zOrder() { return "bottom" as const; }
  renderer() { return this.r; }
}

export class ZonesPrimitive implements ISeriesPrimitive<Time> {
  chart: IChartApi | null = null;
  series: ISeriesApi<SeriesType> | null = null;
  boxes: Box[] = [];
  segs: Seg[] = [];
  lastTime = 0;
  days: number[] = [];
  private views = [new View(this)];
  private request: (() => void) | null = null;

  attached(p: SeriesAttachedParameter<Time>) {
    this.chart = p.chart as IChartApi;
    this.series = p.series;
    this.request = p.requestUpdate;
  }
  detached() { this.chart = null; this.series = null; this.request = null; }
  paneViews() { return this.views; }
  set(boxes: Box[], segs: Seg[], lastTime: number, days: number[] = []) {
    this.days = days;
    this.boxes = boxes;
    this.segs = segs;
    this.lastTime = lastTime;
    this.request?.();
  }
}
