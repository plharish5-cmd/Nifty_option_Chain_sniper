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
      for (const b of this.p.boxes) {
        const y1 = y(b.top), y2 = y(b.bottom);
        if (y1 === null || y2 === null) continue;
        const x1 = x(b.from, 0), x2 = b.to >= this.p.lastTime ? W : x(b.to, W);
        if (x2 < 0 || x1 > W) continue;
        ctx.fillStyle = b.fill;
        ctx.fillRect(x1, Math.min(y1, y2), Math.max(1, x2 - x1), Math.max(1, Math.abs(y2 - y1)));
        if (b.stroke) {
          ctx.strokeStyle = b.stroke;
          ctx.setLineDash(b.dash ? [4, 3] : []);
          ctx.lineWidth = 1;
          ctx.strokeRect(x1 + 0.5, Math.min(y1, y2) + 0.5, Math.max(1, x2 - x1) - 1, Math.max(1, Math.abs(y2 - y1)) - 1);
          ctx.setLineDash([]);
        }
        if (b.label && Math.abs(y2 - y1) >= 12 && x2 - x1 >= 70) {          // label only boxes big enough to read
          ctx.fillStyle = b.labelColor || "#cbd5e1";
          ctx.fillText(b.label, Math.max(4, x1 + 4), Math.min(y1, y2) + 11);
        }
      }
      const used: number[] = [];
      for (const s of [...this.p.segs].sort((a, b) => b.price - a.price)) {
        const yy = y(s.price);
        if (yy === null) continue;
        const x1 = Math.max(0, x(s.from, 0));
        ctx.strokeStyle = s.color;
        ctx.lineWidth = s.width ?? 1;
        ctx.setLineDash(s.dash ? [6, 4] : []);
        ctx.beginPath();
        ctx.moveTo(x1, Math.round(yy) + 0.5);
        ctx.lineTo(W, Math.round(yy) + 0.5);
        ctx.stroke();
        ctx.setLineDash([]);
        const tw = ctx.measureText(s.label).width;
        // stagger labels that would collide with the previous one
        let ly = yy - 4;
        for (const prev of used) if (Math.abs(prev - ly) < 12) ly = prev - 12;
        used.push(ly);
        ctx.fillStyle = "rgba(6,11,22,0.85)";
        ctx.fillRect(W - tw - 10, ly - 9, tw + 8, 12);
        ctx.fillStyle = s.color;
        ctx.fillText(s.label, W - tw - 6, ly);
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
  private views = [new View(this)];
  private request: (() => void) | null = null;

  attached(p: SeriesAttachedParameter<Time>) {
    this.chart = p.chart as IChartApi;
    this.series = p.series;
    this.request = p.requestUpdate;
  }
  detached() { this.chart = null; this.series = null; this.request = null; }
  paneViews() { return this.views; }
  set(boxes: Box[], segs: Seg[], lastTime: number) {
    this.boxes = boxes;
    this.segs = segs;
    this.lastTime = lastTime;
    this.request?.();
  }
}
