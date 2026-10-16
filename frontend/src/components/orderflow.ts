// orderflow.ts — Order-flow Pro layers (information only — never places orders).
//   🧱 OI walls     — bold liquidity bands at the biggest CE (resistance) / PE (support) OI strikes (REAL chain data)
//   🫧 Big trades   — bubbles on candles with RVOL ≥ 2× (REAL FUT volume); colour = who won (delta, else candle colour)
//   ▦  FP cells     — bid × ask cells beside each candle; imbalances bright; stacked imbalance zones extended right
//   〽 CVD          — running cumulative delta in its own pane (ESTIMATED delta from recorded FUT ticks)
// readAt() turns all four into one plain verdict for a candle: BUY / SELL / WAIT + why + a plan idea.
import type {
  IChartApi, IPrimitivePaneRenderer, IPrimitivePaneView, ISeriesApi, ISeriesPrimitive, PrimitivePaneViewZOrder,
  SeriesAttachedParameter, SeriesType, Time,
} from "lightweight-charts";
import { FP_SCALE, kfmt, type FpBar } from "./footprint";

type Target = Parameters<IPrimitivePaneRenderer["draw"]>[0];
export type Wall = { p: number; side: "CE" | "PE"; oi: number; chg: number; rank: number; share: number };
export type Bubble = { time: number; price: number; rvol: number; bull: boolean; vol: number; delta: number | null; absorbed: boolean };
export type Stack = { time: number; lo: number; hi: number; bull: boolean; until: number | null };
export const BIG_X = 2;              // RVOL that counts as a "big trade" candle

const F = (px: number) => `bold ${Math.round(px * FP_SCALE)}px Inter, system-ui, sans-serif`;
const crore = (n: number) => (n >= 1e7 ? `${(n / 1e7).toFixed(2)} Cr` : `${(n / 1e5).toFixed(1)} L`);
const nf = (v: number) => (v >= 1000 ? `${(v / 1000).toFixed(1)}k` : String(Math.round(v)));
const step = (b: FpBar) => (b.rows.length > 1 ? Math.abs(b.rows[1].p - b.rows[0].p) || 5 : 5);

// ── stacked imbalance zones: 3+ consecutive imbalance rows on one side, extended until price trades back through ──
export function stacks(bars: FpBar[]): Stack[] {
  const out: Stack[] = [];
  for (const b of bars) {
    const rows = [...b.rows].sort((x, y) => x.p - y.p);
    for (const bull of [true, false]) {
      let run: number[] = [];
      const flush = () => { if (run.length >= 3) out.push({ time: b.time, lo: run[0], hi: run[run.length - 1] + step(b), bull, until: null }); run = []; };
      for (const r of rows) { if (bull ? r.bi : r.si) run.push(r.p); else flush(); }
      flush();
    }
  }
  for (const s of out) {                 // a zone dies when a later candle closes through it
    const after = bars.filter((b) => b.time > s.time);
    const k = after.find((b) => (s.bull ? b.c < s.lo : b.c > s.hi));
    s.until = k ? k.time : null;
  }
  return out;
}

export class OrderFlowPrimitive implements ISeriesPrimitive<Time> {
  chart: IChartApi | null = null;
  series: ISeriesApi<SeriesType> | null = null;
  walls: Wall[] = []; bubbles: Bubble[] = []; cells: FpBar[] = []; zones: Stack[] = [];
  private views = [new BandView(this), new TopView(this)];
  private request: (() => void) | null = null;
  attached(p: SeriesAttachedParameter<Time>) { this.chart = p.chart as IChartApi; this.series = p.series; this.request = p.requestUpdate; }
  detached() { this.chart = null; this.series = null; this.request = null; }
  paneViews() { return this.views; }
  set(o: { walls?: Wall[]; bubbles?: Bubble[]; cells?: FpBar[] }) {
    this.walls = o.walls ?? []; this.bubbles = o.bubbles ?? []; this.cells = o.cells ?? []; this.zones = stacks(this.cells);
    this.request?.();
  }
}

// ── walls: drawn UNDER the candles ──
class BandView implements IPrimitivePaneView {
  constructor(private p: OrderFlowPrimitive) {}
  zOrder(): PrimitivePaneViewZOrder { return "bottom"; }
  renderer(): IPrimitivePaneRenderer { return { draw: (t) => this.draw(t) }; }
  private draw(target: Target) {
    const { series, walls } = this.p;
    if (!series || !walls.length) return;
    target.useMediaCoordinateSpace(({ context: ctx, mediaSize }) => {
      const S = FP_SCALE;
      for (const w of walls) {
        const y = series.priceToCoordinate(w.p);
        if (y === null || y < -20 || y > mediaSize.height + 20) continue;
        const ce = w.side === "CE", top = w.rank === 1;
        const h = Math.max(4, 4 + 14 * w.share);                   // thicker = more OI
        const rgb = ce ? "239,68,68" : "34,197,94";
        const g = ctx.createLinearGradient(0, 0, mediaSize.width, 0);
        g.addColorStop(0, `rgba(${rgb},0.04)`); g.addColorStop(0.75, `rgba(${rgb},${0.18 + 0.3 * w.share})`); g.addColorStop(1, `rgba(${rgb},${0.35 + 0.4 * w.share})`);
        ctx.fillStyle = g; ctx.fillRect(0, y - h / 2, mediaSize.width, h);
        ctx.strokeStyle = `rgba(${rgb},${top ? 0.95 : 0.6})`; ctx.lineWidth = top ? 2 : 1;
        ctx.setLineDash(top ? [] : [6, 4]); ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(mediaSize.width, y); ctx.stroke(); ctx.setLineDash([]);
        const txt = `${ce ? "🧱 CE WALL" : "🧱 PE WALL"} ${w.p.toLocaleString("en-IN")} · ${crore(w.oi)}${w.chg ? ` ${w.chg > 0 ? "▲+" : "▼"}${crore(Math.abs(w.chg))}` : ""}${top ? (ce ? " · resistance" : " · support") : ""}`;
        ctx.font = F(top ? 10.5 : 9.5); ctx.textAlign = "right"; ctx.textBaseline = "middle";
        const tw = ctx.measureText(txt).width + 10, x1 = mediaSize.width - 8;
        ctx.fillStyle = ce ? "rgba(127,29,29,0.92)" : "rgba(20,83,45,0.92)";
        ctx.fillRect(x1 - tw, y - 8 * S, tw, 16 * S);
        ctx.fillStyle = "#ffffff"; ctx.fillText(txt, x1 - 5, y);
      }
    });
  }
}

// ── stacked zones, footprint cells and bubbles: drawn OVER the candles ──
class TopView implements IPrimitivePaneView {
  constructor(private p: OrderFlowPrimitive) {}
  zOrder(): PrimitivePaneViewZOrder { return "top"; }
  renderer(): IPrimitivePaneRenderer { return { draw: (t) => this.draw(t) }; }
  private draw(target: Target) {
    const { chart, series, cells, bubbles, zones } = this.p;
    if (!chart || !series) return;
    const ts = chart.timeScale(), sp = ts.options().barSpacing;
    target.useMediaCoordinateSpace(({ context: ctx, mediaSize }) => {
      const S = FP_SCALE, X = (t: number) => ts.timeToCoordinate(t as Time);
      // stacked imbalance zones (support / resistance until price closes through)
      for (const z of zones) {
        const x0 = X(z.time); if (x0 === null) continue;
        const x1 = z.until != null ? (X(z.until) ?? mediaSize.width) : mediaSize.width;
        const y0 = series.priceToCoordinate(z.hi), y1 = series.priceToCoordinate(z.lo);
        if (y0 === null || y1 === null) continue;
        ctx.fillStyle = z.bull ? "rgba(34,197,94,0.16)" : "rgba(239,68,68,0.16)";
        ctx.fillRect(x0, y0, Math.max(2, x1 - x0), Math.max(2, y1 - y0));
        ctx.strokeStyle = z.bull ? "rgba(74,222,128,0.8)" : "rgba(248,113,113,0.8)"; ctx.lineWidth = 1;
        ctx.strokeRect(x0, y0, Math.max(2, x1 - x0), Math.max(2, y1 - y0));
        if (z.until == null) { ctx.font = F(8.5); ctx.textAlign = "left"; ctx.textBaseline = "bottom";
          ctx.fillStyle = z.bull ? "#86efac" : "#fca5a5"; ctx.fillText(z.bull ? "STACKED BUY ▲ support" : "STACKED SELL ▼ resistance", x0 + 3, y0 - 1); }
      }
      // footprint cells: bid (sellers hitting) left · ask (buyers lifting) right, beside the candle
      for (const b of cells) {
        const x = X(b.time); if (x === null || x < -60 || x > mediaSize.width + 60 || !b.rows.length) continue;
        const st = step(b), maxV = Math.max(...b.rows.map((r) => Math.max(r.buy, r.sell)), 1);
        if (sp >= 16) {
          const half = Math.min(60, (sp - 4) / 2), x0 = x + 3;
          for (const r of b.rows) {
            const yA = series.priceToCoordinate(r.p + st), yB = series.priceToCoordinate(r.p);
            if (yA === null || yB === null) continue;
            const h = Math.max(1, yB - yA - 0.5);
            ctx.fillStyle = r.si ? "rgba(239,68,68,0.95)" : `rgba(244,114,182,${0.12 + 0.55 * r.sell / maxV})`;
            ctx.fillRect(x0, yA, half - 1, h);
            ctx.fillStyle = r.bi ? "rgba(34,197,94,0.95)" : `rgba(110,231,183,${0.12 + 0.55 * r.buy / maxV})`;
            ctx.fillRect(x0 + half, yA, half - 1, h);
            if (r.poc) { ctx.strokeStyle = "#facc15"; ctx.lineWidth = 1.5; ctx.strokeRect(x0, yA, half * 2 - 1, h); }
            if (h >= 9 && half >= 20) {
              ctx.font = `${r.si || r.bi ? "bold " : ""}${Math.min(11, Math.floor(h - 1))}px Inter, system-ui`; ctx.textBaseline = "middle";
              ctx.fillStyle = "#ffffff"; ctx.textAlign = "right"; ctx.fillText(nf(r.sell), x0 + half - 3, yA + h / 2);
              ctx.textAlign = "left"; ctx.fillText(nf(r.buy), x0 + half + 2, yA + h / 2);
            }
          }
        } else {                                         // narrow: only the imbalance ticks
          for (const r of b.rows) {
            if (!r.bi && !r.si) continue;
            const y = series.priceToCoordinate(r.p + st / 2); if (y === null) continue;
            ctx.fillStyle = r.bi ? "#22c55e" : "#ef4444";
            ctx.fillRect(r.bi ? x + 2 : x - 2 - Math.max(2, sp * 0.4), y - 1.5, Math.max(2, sp * 0.4), 3);
          }
        }
      }
      // big-trade bubbles: area ∝ RVOL
      for (const u of bubbles) {
        const x = X(u.time), y = series.priceToCoordinate(u.price);
        if (x === null || y === null || x < -40 || x > mediaSize.width + 40) continue;
        const r = Math.min(34, (5 + 6 * Math.sqrt(u.rvol - 1)) * Math.min(S, 1.6));
        const rgb = u.bull ? "163,230,53" : "239,68,68";
        const g = ctx.createRadialGradient(x - r / 3, y - r / 3, r / 6, x, y, r);
        g.addColorStop(0, `rgba(${rgb},0.95)`); g.addColorStop(1, `rgba(${rgb},0.55)`);
        ctx.fillStyle = g; ctx.beginPath(); ctx.arc(x, y, r, 0, Math.PI * 2); ctx.fill();
        ctx.lineWidth = u.absorbed ? 3 : 1; ctx.strokeStyle = u.absorbed ? "#facc15" : `rgba(${rgb},1)`; ctx.stroke();
        if (r >= 11) { ctx.font = F(Math.min(10, r / 2.2)); ctx.textAlign = "center"; ctx.textBaseline = "middle";
          ctx.fillStyle = u.bull ? "#0b1220" : "#ffffff"; ctx.fillText(`${u.rvol.toFixed(1)}×`, x, y); }
      }
    });
  }
}

// ── builders ──
export function buildWalls(rows: { strike: number; ce: { oi: number; oi_chg: number }; pe: { oi: number; oi_chg: number } }[], spot: number): Wall[] {
  const near = rows.filter((r) => Math.abs(r.strike - spot) <= spot * 0.025);
  const pick = (side: "CE" | "PE") => {
    const list = near.map((r) => ({ p: r.strike, oi: side === "CE" ? r.ce.oi : r.pe.oi, chg: side === "CE" ? r.ce.oi_chg : r.pe.oi_chg }))
      .filter((x) => (side === "CE" ? x.p >= spot - spot * 0.003 : x.p <= spot + spot * 0.003)).sort((a, b) => b.oi - a.oi).slice(0, 3);
    const mx = list[0]?.oi || 1;
    return list.map((x, i) => ({ ...x, side, rank: i + 1, share: x.oi / mx } as Wall));
  };
  return [...pick("CE"), ...pick("PE")];
}

export function buildBubbles(vbars: { time: number; v: number; rvol: number | null; up: boolean }[], fp: FpBar[],
  candles: { time: number; open: number; high: number; low: number; close: number; closed?: boolean }[]): Bubble[] {
  const fm = new Map(fp.map((b) => [b.time, b])), cm = new Map(candles.map((c) => [c.time, c]));
  const out: Bubble[] = [];
  for (const v of vbars) {
    if (v.rvol == null || v.rvol < BIG_X) continue;
    const c = cm.get(v.time); if (!c || c.closed === false) continue;
    const f = fm.get(v.time), d = f ? f.delta : null;
    const bull = d != null && Math.abs(d) > 0 ? d > 0 : c.close >= c.open;
    const rng = Math.max(0.5, c.high - c.low), body = Math.abs(c.close - c.open);
    out.push({ time: v.time, price: f?.poc ?? (c.high + c.low) / 2, rvol: v.rvol, bull, vol: v.v, delta: d, absorbed: body < 0.3 * rng });
  }
  return out;
}

// ── the reading: one candle → BUY / SELL / WAIT ──
export type OfLine = { txt: string; tone: "bull" | "bear" | "warn" | "info" };
export type OfRead = { verdict: "BUY" | "SELL" | "WAIT"; bull: number; bear: number; head: string; lines: OfLine[]; plan?: string; wall?: string };

export function readAt(t: number, o: { fp: FpBar[]; bubbles: Bubble[]; walls: Wall[]; zones: Stack[]; atr: number;
  candles: { time: number; open: number; high: number; low: number; close: number }[]; px?: number | null }): OfRead | null {
  const ci = o.candles.findIndex((c) => c.time === t); if (ci < 0) return null;
  const c = o.candles[ci], fi = o.fp.findIndex((b) => b.time === t), b = fi >= 0 ? o.fp[fi] : undefined;
  const L: OfLine[] = []; let bull = 0, bear = 0;
  const up = c.close >= c.open, atr = o.atr || 20;
  // 1 delta vs candle colour
  if (b && b.rows.length) {
    const dUp = b.delta >= 0;
    if (dUp === up) { up ? bull++ : bear++; L.push({ tone: up ? "bull" : "bear", txt: `Delta ${kfmt(b.delta)} agrees with the ${up ? "green" : "red"} candle — ${up ? "buyers" : "sellers"} really pushed it.` }); }
    else L.push({ tone: "warn", txt: `Delta ${kfmt(b.delta)} DISAGREES with the ${up ? "green" : "red"} candle — the ${up ? "buyers got absorbed by sellers" : "sellers got absorbed by buyers"}. Don't chase this candle.` });
    // 2 CVD slope (3 bars)
    const p3 = o.fp[fi - 3];
    if (p3) { const dc = b.cvd - p3.cvd;
      if (Math.abs(dc) > 0) { dc > 0 ? bull++ : bear++; L.push({ tone: dc > 0 ? "bull" : "bear", txt: `CVD ${dc > 0 ? "rising" : "falling"} over 3 candles (${kfmt(dc)}) — ${dc > 0 ? "buyers" : "sellers"} in control of the flow.` }); } }
    // 3 CVD divergence (6 bars)
    const win = o.fp.slice(Math.max(0, fi - 6), fi);
    if (win.length >= 4) {
      const hh = c.high > Math.max(...win.map((x) => x.h)), ll = c.low < Math.min(...win.map((x) => x.l));
      if (hh && b.cvd < Math.max(...win.map((x) => x.cvd))) { bear++; L.push({ tone: "bear", txt: "⚠ BEARISH CVD divergence — price made a new high but CVD didn't. Buyers are exhausted; the high is likely a trap." }); }
      if (ll && b.cvd > Math.min(...win.map((x) => x.cvd))) { bull++; L.push({ tone: "bull", txt: "⚠ BULLISH CVD divergence — price made a new low but CVD didn't. Sellers are exhausted; the low is likely a trap." }); }
    }
    // 4 stacked imbalance on this candle
    const zs = o.zones.filter((z) => z.time === t);
    for (const z of zs) { z.bull ? bull++ : bear++;
      L.push({ tone: z.bull ? "bull" : "bear", txt: `▦ Stacked ${z.bull ? "BUY" : "SELL"} imbalance ${z.lo.toFixed(0)}–${z.hi.toFixed(0)} — ${z.bull ? "buyers lifted 3+ levels in a row. This zone is now SUPPORT; buy the retest" : "sellers hit 3+ levels in a row. This zone is now RESISTANCE; sell the retest"}.` }); }
  } else L.push({ tone: "info", txt: "No footprint for this candle (needs 5m / 15m / 1h with recorded ticks) — reading uses volume + walls only." });
  // 5 big trade
  const u = o.bubbles.find((x) => x.time === t);
  if (u) {
    if (u.absorbed) L.push({ tone: "warn", txt: `🫧 BIG volume ${u.rvol.toFixed(1)}× but price barely moved (yellow ring) — ABSORPTION. A big passive player is defending here; expect a turn ${u.bull ? "down" : "up"} if the next candle fails.` });
    else { u.bull ? bull++ : bear++; L.push({ tone: u.bull ? "bull" : "bear", txt: `🫧 BIG TRADE ${u.rvol.toFixed(1)}× normal volume — ${u.bull ? "a big BUYER" : "a big SELLER"} stepped in. Follow it while price holds ${u.bull ? "above" : "below"} ${u.price.toFixed(0)}.` }); }
  }
  // 6 location vs walls
  const ce = o.walls.filter((w) => w.side === "CE" && w.p > c.close).sort((a, b2) => a.p - b2.p)[0];
  const pe = o.walls.filter((w) => w.side === "PE" && w.p < c.close).sort((a, b2) => b2.p - a.p)[0];
  const roomUp = ce ? ce.p - c.close : null, roomDn = pe ? c.close - pe.p : null;
  if (ce) L.push({ tone: roomUp! < 0.5 * atr ? "warn" : "info", txt: `🧱 Next CE wall ${ce.p.toLocaleString("en-IN")} (${roomUp!.toFixed(0)} pts above, ${crore(ce.oi)}) — call writers defend it.` });
  if (pe) L.push({ tone: roomDn! < 0.5 * atr ? "warn" : "info", txt: `🧱 Next PE wall ${pe.p.toLocaleString("en-IN")} (${roomDn!.toFixed(0)} pts below, ${crore(pe.oi)}) — put writers defend it.` });
  // verdict
  let verdict: OfRead["verdict"] = "WAIT", head = "";
  const side = bull >= 3 && bull - bear >= 2 ? "BUY" : bear >= 3 && bear - bull >= 2 ? "SELL" : null;
  if (!side) head = bull === bear ? "Flow is balanced — no edge. WAIT." : `Flow leans ${bull > bear ? "BUY" : "SELL"} (${Math.max(bull, bear)} vs ${Math.min(bull, bear)}) but not strong enough. WAIT for one more confirmation.`;
  else if (side === "BUY" && roomUp != null && roomUp < 0.5 * atr) head = `Flow says BUY, but price is only ${roomUp.toFixed(0)} pts under the CE wall. Buying into a wall = poor R:R. WAIT for a break and hold above ${ce!.p}.`;
  else if (side === "SELL" && roomDn != null && roomDn < 0.5 * atr) head = `Flow says SELL, but price is only ${roomDn.toFixed(0)} pts above the PE wall. Selling into a wall = poor R:R. WAIT for a break and hold below ${pe!.p}.`;
  else { verdict = side; head = `${side === "BUY" ? "▲ BUY bias" : "▼ SELL bias"} — ${Math.max(bull, bear)} order-flow points vs ${Math.min(bull, bear)}, with room to the next wall.`; }
  const plan = verdict === "BUY" ? `Paper plan: CE above ${c.high.toFixed(0)} · SL below ${c.low.toFixed(0)} (${(c.high - c.low).toFixed(0)} pts) · target ${ce ? `CE wall ${ce.p}` : `${(c.high + 2 * (c.high - c.low)).toFixed(0)}`}`
    : verdict === "SELL" ? `Paper plan: PE below ${c.low.toFixed(0)} · SL above ${c.high.toFixed(0)} (${(c.high - c.low).toFixed(0)} pts) · target ${pe ? `PE wall ${pe.p}` : `${(c.low - 2 * (c.high - c.low)).toFixed(0)}`}` : undefined;
  // wall under the mouse
  let wall: string | undefined;
  if (o.px != null) { const w = o.walls.find((x) => Math.abs(x.p - o.px!) <= Math.max(3, atr * 0.15));
    if (w) wall = `🧱 ${w.side} WALL ${w.p.toLocaleString("en-IN")} — ${crore(w.oi)} OI${w.chg ? ` (${w.chg > 0 ? "added" : "cut"} ${crore(Math.abs(w.chg))} today)` : ""}. ${w.side === "CE" ? "Call writers sell here → first test usually REJECTS. Buy CE only after a 5m close above + retest holds." : "Put writers sell here → first test usually BOUNCES. Buy PE only after a 5m close below + retest fails."}${w.chg < 0 ? " ⚠ OI being cut = the wall is weakening." : ""}`; }
  return { verdict, bull, bear, head, lines: L, plan, wall };
}
