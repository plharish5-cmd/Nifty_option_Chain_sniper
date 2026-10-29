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
  conf?: { dir: "bull" | "bear"; why: string[] }; buy?: number; sell?: number; };

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

// label size for footprint / heat annotations (A− / A+ on the chart; remembered per browser)
export let FP_SCALE = (() => { try { return Number(localStorage.getItem("ict_fp_scale")) || 1.5; } catch { return 1.5; } })();
export function setFpScale(v: number) { FP_SCALE = Math.min(2.5, Math.max(0.8, v)); try { localStorage.setItem("ict_fp_scale", String(FP_SCALE)); } catch { /* ignore */ } }
const F = (px: number) => `bold ${Math.round(px * FP_SCALE)}px Inter, system-ui, sans-serif`;

class Renderer implements IPrimitivePaneRenderer {
  constructor(private p: FootprintPrimitive) {}
  draw(target: Target) {
    const { chart, series, bars, bias } = this.p;
    if (!chart || !series || (!bars.length && !bias.length)) return;
    const ts = chart.timeScale();
    const spacing = ts.options().barSpacing;
    target.useMediaCoordinateSpace(({ context: ctx, mediaSize }) => {
      const S = FP_SCALE, showNum = spacing >= 18 * Math.min(S, 1.3), showTags = spacing >= 5;
      ctx.textBaseline = "alphabetic";
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
          ctx.font = F(10);
          ctx.fillStyle = b.delta >= 0 ? "#34d399" : "#f87171";
          ctx.fillText(kfmt(b.delta), x, (yl as number) + 13 * S);
        } else if (b.rows.length) {                      // narrow candles: a coloured dot instead of a number
          ctx.fillStyle = b.delta >= 0 ? "#34d399" : "#f87171";
          ctx.beginPath(); ctx.arc(x, (yl as number) + 6 * S, 2.5 * S, 0, Math.PI * 2); ctx.fill();
        }
        // ✓3 confluence badge: below the delta for BUY, above the pills for SELL
        if (b.conf) {
          const buy = b.conf.dir === "bull", txt = buy ? "BUY ✓3" : "SELL ✓3";
          ctx.font = F(11);
          const w = ctx.measureText(txt).width + 10;
          const yy = buy ? (yl as number) + 30 * S : (yh as number) - (22 + 14 * b.tags.filter((t) => FP_LETTER[t.k]).length) * S;
          ctx.fillStyle = buy ? "#22c55e" : "#ef4444";
          ctx.fillRect(x - w / 2, yy - 11 * S, w, 15 * S);
          ctx.strokeStyle = "#facc15"; ctx.lineWidth = 1; ctx.strokeRect(x - w / 2, yy - 11 * S, w, 15 * S);
          ctx.fillStyle = "#ffffff"; ctx.fillText(txt, x, yy + 1 * S);
        }
        // tags above: full words when the candle is wide, letters when narrow, coloured pill per tag
        if (showTags && b.tags.length) {
          const seen = new Set<string>(); const items: { txt: string; bear: boolean }[] = [];
          for (const t of b.tags) {
            const L = FP_LETTER[t.k]; if (!L || seen.has(L)) continue; seen.add(L);
            items.push({ txt: spacing >= 34 ? FP_WORD[t.k] : L, bear: t.dir === "bear" });
          }
          ctx.font = F(9);
          let yy = (yh as number) - 6 * S;
          for (const it of items.reverse()) {
            const w = ctx.measureText(it.txt).width + 6;
            ctx.fillStyle = it.bear ? "rgba(225,29,72,0.95)" : "rgba(13,148,136,0.95)";
            ctx.beginPath(); (ctx as CanvasRenderingContext2D & { roundRect?: Function }).roundRect
              ? (ctx as any).roundRect(x - w / 2, yy - 10 * S, w, 12 * S, 3) : ctx.rect(x - w / 2, yy - 10 * S, w, 12 * S);
            ctx.fill();
            ctx.fillStyle = "#ffffff"; ctx.fillText(it.txt, x, yy - 1 * S);
            yy -= 14 * S;
          }
        }
      }
      // ⚖ 7-factor bias badges (outlined so they don't clash with ✓3)
      const S2 = FP_SCALE; ctx.font = F(10);
      for (const r of bias) {
        const x = ts.timeToCoordinate(r.time as Time);
        if (x === null || x < -20 || x > mediaSize.width + 20) continue;
        const buy = r.verdict === "BUY", n = buy ? r.bull : r.bear;
        const y0 = series.priceToCoordinate(buy ? r.l : r.h);
        if (y0 === null) continue;
        const txt = `${r.warn ? "⚠ " : ""}${buy ? "▲" : "▼"}${n}/${r.avail}${r.strong ? "★" : ""}`;
        const w = ctx.measureText(txt).width + 8;
        const yy = buy ? (y0 as number) + (bars.length ? 50 : 16) * S2 : (y0 as number) - (bars.length ? 58 : 12) * S2;
        ctx.fillStyle = "rgba(11,18,32,0.9)"; ctx.fillRect(x - w / 2, yy - 10 * S2, w, 14 * S2);
        ctx.strokeStyle = buy ? "#22c55e" : "#ef4444"; ctx.lineWidth = 1.5; ctx.strokeRect(x - w / 2, yy - 10 * S2, w, 14 * S2);
        ctx.fillStyle = buy ? "#4ade80" : "#f87171"; ctx.fillText(txt, x, yy + 1 * S2);
      }
    });
  }
}

class HeatRenderer implements IPrimitivePaneRenderer {
  constructor(private p: FootprintPrimitive) {}
  draw() { /* background only */ }
  drawBackground(target: Target) {
    const { chart, series, heat } = this.p;
    if (!chart || !series || !heat.length) return;
    const ts = chart.timeScale(), spacing = ts.options().barSpacing;
    const maxV = Math.max(1, ...heat.flatMap((b) => b.rows.map((r) => r.v)));
    target.useMediaCoordinateSpace(({ context: ctx, mediaSize }) => {
      for (const b of heat) {
        const x = ts.timeToCoordinate(b.time as Time);
        if (x === null || x < -spacing || x > mediaSize.width + spacing) continue;
        const step = b.rows.length > 1 ? Math.abs(b.rows[0].p - b.rows[1].p) : 5;
        for (const r of b.rows) {
          const y1 = series.priceToCoordinate(r.p + step / 2), y2 = series.priceToCoordinate(r.p - step / 2);
          if (y1 === null || y2 === null) continue;
          const k = Math.sqrt(r.v / maxV);                         // sqrt → mid volumes still visible
          // cold → hot: blue → teal → yellow → orange → red
          const hue = 220 - 220 * k, light = 22 + 33 * k;
          ctx.fillStyle = `hsla(${hue}, 85%, ${light}%, ${0.18 + 0.62 * k})`;
          ctx.fillRect(x - spacing * 0.48, y1 as number, spacing * 0.96, Math.max(1, (y2 as number) - (y1 as number)));
        }
      }
    });
  }
}

class HeatView implements IPrimitivePaneView {
  private r: HeatRenderer;
  constructor(p: FootprintPrimitive) { this.r = new HeatRenderer(p); }
  zOrder() { return "bottom" as const; }
  renderer() { return this.r; }
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
  bias: { time: number; h: number; l: number; verdict: string; bull: number; bear: number; avail: number; strong: boolean; warn?: string }[] = [];
  heat: FpBar[] = [];
  private views = [new HeatView(this), new View(this)];
  private request: (() => void) | null = null;
  attached(p: SeriesAttachedParameter<Time>) { this.chart = p.chart as IChartApi; this.series = p.series; this.request = p.requestUpdate; }
  detached() { this.chart = null; this.series = null; this.request = null; }
  paneViews() { return this.views; }
  refresh() { this.request?.(); }
  set(bars: FpBar[], bias: FootprintPrimitive["bias"] = [], heat: FpBar[] = []) { this.bars = bars; this.bias = bias; this.heat = heat; this.request?.(); }
}
