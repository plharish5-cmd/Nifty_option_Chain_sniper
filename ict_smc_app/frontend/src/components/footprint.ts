// footprint.ts — series primitive that draws the ESTIMATED footprint on the main chart, per closed candle:
//   • delta number under the candle (green + / red −)  • gold POC tick across the candle
//   • tag letters above the candle: A absorption · D distribution · C accumulation · M manipulation · S stacked · I initiative
// Information only. Drawn on top of the candles but small, and only where the candle is wide enough.
import type {
  IChartApi, IPrimitivePaneRenderer, IPrimitivePaneView, ISeriesApi, ISeriesPrimitive, SeriesAttachedParameter,
  SeriesType, Time,
} from "lightweight-charts";

export type FpRow = { p: number; buy: number; sell: number; d: number; v: number; bi: boolean; si: boolean; poc: boolean; va: boolean };
export type FpTag = { k: string; dir: "bull" | "bear"; t: string };
export type FpBar = { time: number; at: string; o: number; h: number; l: number; c: number; delta: number; vol: number; cvd: number;
  poc?: number; delta_pct?: number; coverage: number; rows: FpRow[]; tags: FpTag[]; read: string;
  conf?: { dir: "bull" | "bear"; why: string[] } };

// U (unfinished) and V (divergence) fire on most candles → hidden from the chart, still in the hover card / ladder panel
export const FP_LETTER: Record<string, string> = { ABSORPTION: "A", DISTRIBUTION: "D", ACCUMULATION: "C", MANIPULATION: "M",
  STACKED_BUY: "S", STACKED_SELL: "S", INITIATIVE: "I" };
export const FP_WORD: Record<string, string> = { ABSORPTION: "ABSORB", DISTRIBUTION: "DISTRIB", ACCUMULATION: "ACCUM",
  MANIPULATION: "TRAP", STACKED_BUY: "STACK▲", STACKED_SELL: "STACK▼", INITIATIVE: "PUSH" };
export const FP_MEANING: Record<string, string> = {
  ABSORPTION: "Absorption — heavy volume but price didn't move: big passive orders soaking it up (turn risk)",
  DISTRIBUTION: "Distribution — volume built at the TOP, negative delta, weak close: sellers unloading",
  ACCUMULATION: "Accumulation — volume built at the BOTTOM, positive delta, strong close: buyers loading",
  MANIPULATION: "Trap — wick swept a level, orders hit there, price closed back: trapped traders",
  STACKED_BUY: "Stacked buying — 3+ rows in a row with buyers ≥ 3× sellers: strong push up",
  STACKED_SELL: "Stacked selling — 3+ rows in a row with sellers ≥ 3× buyers: strong push down",
  INITIATIVE: "Initiative — strong one-sided delta that moved price: aggressive side in control",
  DIVERGENCE: "Divergence — new high/low but delta disagreed: momentum fading",
  UNFINISHED_HIGH: "Unfinished high — both sides still trading at the top: price often returns",
  UNFINISHED_LOW: "Unfinished low — both sides still trading at the bottom: price often returns",
};

export const kfmt = (v: number) => { const a = Math.abs(v); const s = v < 0 ? "−" : "+";
  return a >= 1e5 ? `${s}${(a / 1e5).toFixed(1)}L` : a >= 1e3 ? `${s}${(a / 1e3).toFixed(0)}K` : `${s}${Math.round(a)}`; };

type MediaScope = { context: CanvasRenderingContext2D; mediaSize: { width: number; height: number } };
type Target = { useMediaCoordinateSpace: (f: (s: MediaScope) => void) => void };

class Renderer implements IPrimitivePaneRenderer {
  constructor(private p: FootprintPrimitive) {}
  draw(target: Target) {
    const { chart, series, bars } = this.p;
    if (!chart || !series || !bars.length) return;
    const ts = chart.timeScale();
    const spacing = ts.options().barSpacing;
    target.useMediaCoordinateSpace(({ context: ctx, mediaSize }) => {
      const showNum = spacing >= 18, showTags = spacing >= 9;
      ctx.textAlign = "center";
      for (const b of bars) {
        const x = ts.timeToCoordinate(b.time as Time);
        if (x === null || x < -20 || x > mediaSize.width + 20) continue;
        const yl = series.priceToCoordinate(b.l), yh = series.priceToCoordinate(b.h);
        if (yl === null || yh === null) continue;
        // POC tick
        if (b.poc != null) {
          const yp = series.priceToCoordinate(b.poc);
          if (yp !== null) {
            ctx.strokeStyle = "rgba(250,204,21,0.95)"; ctx.lineWidth = 2;
            const w = Math.max(4, spacing * 0.45);
            ctx.beginPath(); ctx.moveTo(x - w, yp); ctx.lineTo(x + w, yp); ctx.stroke();
          }
        }
        // delta under the candle
        if (showNum && b.rows.length) {
          ctx.font = "bold 10px Inter, system-ui, sans-serif";
          ctx.fillStyle = b.delta >= 0 ? "#34d399" : "#f87171";
          ctx.fillText(kfmt(b.delta), x, (yl as number) + 13);
        } else if (b.rows.length) {                      // narrow candles: a coloured dot instead of a number
          ctx.fillStyle = b.delta >= 0 ? "#34d399" : "#f87171";
          ctx.beginPath(); ctx.arc(x, (yl as number) + 6, 2, 0, Math.PI * 2); ctx.fill();
        }
        // ✓3 confluence badge: below the delta for BUY, above the pills for SELL
        if (b.conf) {
          const buy = b.conf.dir === "bull", txt = buy ? "BUY ✓3" : "SELL ✓3";
          ctx.font = "bold 11px Inter, system-ui, sans-serif";
          const w = ctx.measureText(txt).width + 10;
          const yy = buy ? (yl as number) + 30 : (yh as number) - 22 - 14 * b.tags.filter((t) => FP_LETTER[t.k]).length;
          ctx.fillStyle = buy ? "#22c55e" : "#ef4444";
          ctx.fillRect(x - w / 2, yy - 11, w, 15);
          ctx.strokeStyle = "#facc15"; ctx.lineWidth = 1; ctx.strokeRect(x - w / 2, yy - 11, w, 15);
          ctx.fillStyle = "#ffffff"; ctx.fillText(txt, x, yy);
        }
        // tags above: full words when the candle is wide, letters when narrow, coloured pill per tag
        if (showTags && b.tags.length) {
          const seen = new Set<string>(); const items: { txt: string; bear: boolean }[] = [];
          for (const t of b.tags) {
            const L = FP_LETTER[t.k]; if (!L || seen.has(L)) continue; seen.add(L);
            items.push({ txt: spacing >= 34 ? FP_WORD[t.k] : L, bear: t.dir === "bear" });
          }
          ctx.font = "bold 9px Inter, system-ui, sans-serif";
          let yy = (yh as number) - 6;
          for (const it of items.reverse()) {
            const w = ctx.measureText(it.txt).width + 6;
            ctx.fillStyle = it.bear ? "rgba(244,63,94,0.85)" : "rgba(20,184,166,0.85)";
            ctx.beginPath(); (ctx as CanvasRenderingContext2D & { roundRect?: Function }).roundRect
              ? (ctx as any).roundRect(x - w / 2, yy - 10, w, 12, 3) : ctx.rect(x - w / 2, yy - 10, w, 12);
            ctx.fill();
            ctx.fillStyle = "#0b1220"; ctx.fillText(it.txt, x, yy - 1);
            yy -= 14;
          }
        }
      }
    });
  }
}

class View implements IPrimitivePaneView {
  private r: Renderer;
  constructor(p: FootprintPrimitive) { this.r = new Renderer(p); }
  zOrder() { return "top" as const; }
  renderer() { return this.r; }
}

// 3-way confluence: footprint tag + delta sign + a structure / signal mark (same or previous 2 bars) all point the same way
const FLOW_TAGS = new Set(["STACKED_BUY", "STACKED_SELL", "ACCUMULATION", "DISTRIBUTION", "INITIATIVE", "MANIPULATION", "ABSORPTION"]);
export type StructMark = { time: number; dir: string; label: string };
export function confluence(bars: FpBar[], marks: StructMark[], barSec: number): FpBar[] {
  return bars.map((b) => {
    if (!b.rows.length) return { ...b, conf: undefined };
    for (const dir of ["bull", "bear"] as const) {
      const tag = b.tags.find((t) => FLOW_TAGS.has(t.k) && t.dir === dir);
      const dOk = dir === "bull" ? b.delta > 0 : b.delta < 0;
      const colOk = dir === "bull" ? b.c >= b.o : b.c <= b.o;
      const st = marks.filter((m) => m.dir === dir && m.time <= b.time && m.time >= b.time - 2 * barSec);
      if (tag && dOk && colOk && st.length) {
        return { ...b, conf: { dir, why: [`footprint ${FP_WORD[tag.k] ?? tag.k}`, `delta ${kfmt(b.delta)}`, ...st.slice(0, 2).map((m) => m.label)] } };
      }
    }
    return { ...b, conf: undefined };
  });
}

export class FootprintPrimitive implements ISeriesPrimitive<Time> {
  chart: IChartApi | null = null;
  series: ISeriesApi<SeriesType> | null = null;
  bars: FpBar[] = [];
  private views = [new View(this)];
  private request: (() => void) | null = null;
  attached(p: SeriesAttachedParameter<Time>) { this.chart = p.chart as IChartApi; this.series = p.series; this.request = p.requestUpdate; }
  detached() { this.chart = null; this.series = null; this.request = null; }
  paneViews() { return this.views; }
  set(bars: FpBar[]) { this.bars = bars; this.request?.(); }
}
