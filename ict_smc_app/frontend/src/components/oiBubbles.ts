// oiBubbles.ts — magnitude bubbles + divergence readout for the OI Trinity charts.
//
// WHY THIS EXISTS
// On "Fresh Change in OI" and "Cumulative Total OI" the Call and Put curves share one axis while
// living in different orders of magnitude: 29 Oct closed with Call +65.4L against Put +5.58Cr. Two
// lines on an axis say "apart"; they do not say "8.5 times apart", and that ratio is the whole read.
// This draws a translucent bubble on each curve whose AREA is proportional to the value, so the size
// difference you see IS the magnitude difference, plus a plain-language gap label when they diverge.
//
// It is purely additive: it reads the series that already exist and draws on top. No OI maths, no
// payload, no curve, no existing label is touched by anything in this file.
//
// The drawing lives in the primitive at the bottom; everything above it is pure and unit-tested in
// scripts/test_oi_bubbles.mjs, because that is where the reasoning that can be wrong actually sits.

import type {
  IChartApi, IPrimitivePaneRenderer, IPrimitivePaneView, ISeriesApi, ISeriesPrimitive,
  PrimitivePaneViewZOrder, SeriesAttachedParameter, SeriesType, Time,
} from "lightweight-charts";

/** Tunables, in one place. Frontend-side: rules.json is read by the backend only, so if these should
 *  become server-driven they need an endpoint — say the word and that is a small follow-up. */
export const BUBBLE_CFG = {
  minRadiusPx: 3,          // floor so a near-zero value is still visible as a dot
  maxRadiusPx: 15,         // radius of the largest value in view
  targetBubbles: 22,       // roughly how many sampled points to draw across the visible range
  fillAlpha: 0.22,         // translucent, so the curve underneath stays readable
  strokeAlpha: 0.95,
  tiers: [                 // ratio thresholds, biggest first; crore-vs-crore gaps qualify too
    { min: 5.0, label: "EXTREME", color: "#fbbf24" },
    { min: 2.5, label: "WIDE", color: "#f59e0b" },
    { min: 1.5, label: "NOTABLE", color: "#94a3b8" },
  ],
};

export const CALL_RGB = "34,197,94";   // green — matches the existing Call OI curve
export const PUT_RGB = "239,68,68";    // red   — matches the existing Put OI curve

export type OiPoint = { time: Time; put: number; call: number };

/**
 * Radius for a value, scaled so AREA is proportional to magnitude.
 *
 * Radius-proportional scaling would understate the difference badly: the eye judges a disc by its
 * area, so r = maxR * sqrt(v / maxAbs) is what makes "8.5x the OI" look 8.5x as big. A floor keeps
 * tiny values visible; above the floor the proportionality is exact.
 */
export function bubbleRadius(value: number, maxAbs: number,
                             minR = BUBBLE_CFG.minRadiusPx, maxR = BUBBLE_CFG.maxRadiusPx): number {
  if (!Number.isFinite(value) || !Number.isFinite(maxAbs) || maxAbs <= 0) return minR;
  const frac = Math.min(1, Math.abs(value) / maxAbs);
  return Math.max(minR, maxR * Math.sqrt(frac));
}

/** Every Nth index, always including the newest point — bubbles on every snapshot would be a smear. */
export function sampleIndices(len: number, target = BUBBLE_CFG.targetBubbles): number[] {
  if (len <= 0) return [];
  // ceil, not floor: floor(127/22)=5 yields 27 bubbles when 22 were asked for.
  const step = Math.max(1, Math.ceil(len / Math.max(1, target)));
  const out: number[] = [];
  for (let i = 0; i < len; i += step) out.push(i);
  if (out[out.length - 1] !== len - 1) out.push(len - 1);
  return out;
}

export type GapRead = { ratio: number; label: string; color: string; text: string; side: "put" | "call" } | null;

/**
 * The divergence sentence. Returns null when the two are close enough not to be worth saying.
 *
 * Deliberately a RATIO, not a difference: "Put 5.58Cr vs Call 65.4L" is a 8.5x gap, and a 2Cr vs 5Cr
 * gap is 2.5x — both are real separations worth flagging, which a raw subtraction would rank wrongly
 * (3.43Cr vs 3.00Cr would look almost identical while being completely different situations).
 */
export function gapRead(put: number, call: number, fmt: (v: number) => string): GapRead {
  if (!Number.isFinite(put) || !Number.isFinite(call)) return null;
  const ap = Math.abs(put), ac = Math.abs(call);
  const hi = Math.max(ap, ac), lo = Math.min(ap, ac);
  if (hi <= 0) return null;
  const ratio = lo <= 0 ? Infinity : hi / lo;
  const tier = BUBBLE_CFG.tiers.find((t) => ratio >= t.min);
  if (!tier) return null;
  const side: "put" | "call" = ap >= ac ? "put" : "call";
  const r = Number.isFinite(ratio) ? `${ratio.toFixed(1)}×` : "∞";
  return {
    ratio, label: tier.label, color: tier.color, side,
    text: `Put ${fmt(put)} vs Call ${fmt(call)} · ${side === "put" ? "PUT" : "CALL"} ${r} wider · ${tier.label}`,
  };
}

// ── drawing ──────────────────────────────────────────────────────────────────────────────────────

export class OiBubblesPrimitive implements ISeriesPrimitive<Time> {
  chart: IChartApi | null = null;
  series: ISeriesApi<SeriesType> | null = null;      // the PUT series this is attached to
  points: OiPoint[] = [];
  on = false;
  private views = [new BubblesV(this)];
  private request: (() => void) | null = null;

  /** @param callSeries the Call curve, for its own price→y mapping
   *  @param fmt the component's existing inr() — passed in so the Cr/L wording cannot drift from it */
  constructor(public callSeries: () => ISeriesApi<SeriesType> | null,
              public fmt: (v: number) => string) {}

  attached(p: SeriesAttachedParameter<Time>) {
    this.chart = p.chart as IChartApi; this.series = p.series; this.request = p.requestUpdate;
  }
  detached() { this.chart = null; this.series = null; this.request = null; }
  paneViews() { return this.views; }
  set(points: OiPoint[], on: boolean) { this.points = points; this.on = on; this.request?.(); }
}

type Target = Parameters<IPrimitivePaneRenderer["draw"]>[0];

class BubblesV implements IPrimitivePaneView {
  constructor(private p: OiBubblesPrimitive) {}
  zOrder(): PrimitivePaneViewZOrder { return "top"; }
  renderer(): IPrimitivePaneRenderer {
    return { draw: (t: Target) => {
      const { chart, series, points, on, fmt } = this.p;
      const callS = this.p.callSeries();
      if (!on || !chart || !series || !callS || points.length === 0) return;

      t.useMediaCoordinateSpace(({ context: ctx, mediaSize }) => {
        const ts = chart.timeScale();
        const idxs = sampleIndices(points.length);
        // Scale against the largest magnitude actually on screen, so the biggest bubble is always
        // the max and the rest are honestly relative to it.
        let maxAbs = 0;
        for (const i of idxs) maxAbs = Math.max(maxAbs, Math.abs(points[i].put), Math.abs(points[i].call));
        if (maxAbs <= 0) return;

        const disc = (x: number, y: number, r: number, rgb: string) => {
          ctx.beginPath(); ctx.arc(x, y, r, 0, Math.PI * 2);
          ctx.fillStyle = `rgba(${rgb},${BUBBLE_CFG.fillAlpha})`; ctx.fill();
          ctx.lineWidth = 1.25; ctx.strokeStyle = `rgba(${rgb},${BUBBLE_CFG.strokeAlpha})`; ctx.stroke();
        };

        for (const i of idxs) {
          const pt = points[i];
          const x = ts.timeToCoordinate(pt.time);
          if (x === null) continue;
          const yPut = series.priceToCoordinate(pt.put);
          const yCall = callS.priceToCoordinate(pt.call);
          if (yPut === null || yCall === null) continue;

          // Connector first, so the discs sit on top of it.
          const g = gapRead(pt.put, pt.call, fmt);
          if (g) {
            ctx.beginPath(); ctx.moveTo(x, yPut); ctx.lineTo(x, yCall);
            ctx.strokeStyle = `${g.color}55`; ctx.lineWidth = g.ratio >= 5 ? 2.5 : 1.5; ctx.stroke();
          }
          disc(x, yPut, bubbleRadius(pt.put, maxAbs), PUT_RGB);
          disc(x, yCall, bubbleRadius(pt.call, maxAbs), CALL_RGB);
        }

        // Latest point: spell the separation out in words rather than leaving it to be eyeballed.
        const last = points[points.length - 1];
        const g = gapRead(last.put, last.call, fmt);
        if (!g) return;
        const xl = ts.timeToCoordinate(last.time);
        const yp = series.priceToCoordinate(last.put), yc = callS.priceToCoordinate(last.call);
        if (xl === null || yp === null || yc === null) return;

        ctx.font = "600 11px ui-sans-serif, system-ui, sans-serif";
        const padX = 7, h = 19, w = ctx.measureText(g.text).width + padX * 2;
        // Keep the badge on screen and clear of the two discs it describes.
        const x0 = Math.max(4, Math.min(mediaSize.width - w - 4, xl - w - 12));
        const y0 = Math.max(2, Math.min(mediaSize.height - h - 2, (yp + yc) / 2 - h / 2));
        ctx.fillStyle = "rgba(2,6,23,0.88)";
        ctx.strokeStyle = g.color; ctx.lineWidth = 1;
        ctx.beginPath(); ctx.roundRect(x0, y0, w, h, 4); ctx.fill(); ctx.stroke();
        ctx.fillStyle = g.color; ctx.textAlign = "left"; ctx.textBaseline = "middle";
        ctx.fillText(g.text, x0 + padX, y0 + h / 2);
      });
    } };
  }
}
