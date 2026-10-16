// flowband.ts — fills the gap between the Call-OI and Put-OI lines on the writers chart.
// Green where PUT writing leads (bullish), red where CALL writing leads (bearish). Display only.
import type { IChartApi, ISeriesApi, ISeriesPrimitive, IPrimitivePaneView, IPrimitivePaneRenderer,
  PrimitivePaneViewZOrder, SeriesAttachedParameter, SeriesType, Time } from "lightweight-charts";

export type GapPt = { time: number; ce: number; pe: number };

export class FlowBand implements ISeriesPrimitive<Time> {
  private chart: IChartApi | null = null;
  private series: ISeriesApi<SeriesType> | null = null;   // attach to PE line (shares the right scale with CE)
  private req: (() => void) | null = null;
  data: GapPt[] = [];
  private views = [new BandV(this)];
  attached(p: SeriesAttachedParameter<Time>) { this.chart = p.chart as IChartApi; this.series = p.series; this.req = p.requestUpdate; }
  detached() { this.chart = null; this.series = null; this.req = null; }
  paneViews() { return this.views; }
  set(d: GapPt[]) { this.data = d; this.req?.(); }
  _chart() { return this.chart; }
  _series() { return this.series; }
}

class BandV implements IPrimitivePaneView {
  constructor(private p: FlowBand) {}
  zOrder(): PrimitivePaneViewZOrder { return "bottom"; }
  renderer(): IPrimitivePaneRenderer {
    return { draw: (t) => {
      const ch = this.p._chart(), s = this.p._series(), d = this.p.data;
      if (!ch || !s || d.length < 2) return;
      const ts = ch.timeScale();
      t.useMediaCoordinateSpace(({ context: ctx }: { context: CanvasRenderingContext2D }) => {
        // draw each segment as a quad between (ce,pe) at t0 and (ce,pe) at t1, tinted by who leads
        for (let i = 1; i < d.length; i++) {
          const a = d[i - 1], b = d[i];
          const xa = ts.timeToCoordinate(a.time as Time), xb = ts.timeToCoordinate(b.time as Time);
          if (xa === null || xb === null) continue;
          const caY = s.priceToCoordinate(a.ce), paY = s.priceToCoordinate(a.pe);
          const cbY = s.priceToCoordinate(b.ce), pbY = s.priceToCoordinate(b.pe);
          if (caY === null || paY === null || cbY === null || pbY === null) continue;
          const bull = (a.pe + b.pe) / 2 >= (a.ce + b.ce) / 2;      // puts above calls = bullish
          ctx.beginPath();
          ctx.moveTo(xa, paY); ctx.lineTo(xb, pbY); ctx.lineTo(xb, cbY); ctx.lineTo(xa, caY); ctx.closePath();
          ctx.fillStyle = bull ? "rgba(34,197,94,0.14)" : "rgba(239,68,68,0.14)";
          ctx.fill();
        }
      });
    } };
  }
}
