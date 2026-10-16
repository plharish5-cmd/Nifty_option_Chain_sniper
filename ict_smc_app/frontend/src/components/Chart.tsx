// Chart.tsx — candlesticks + ICT/SMC overlays (Lightweight Charts v5).
import { useEffect, useMemo, useRef, useState } from "react";
import {
  BaselineSeries, CandlestickSeries, ColorType, createChart, createSeriesMarkers, CrosshairMode, HistogramSeries, LineSeries, LineStyle,
  type IChartApi, type ISeriesApi, type ISeriesMarkersPluginApi, type MouseEventParams, type Time, type UTCTimestamp,
} from "lightweight-charts";
import type { Analysis, Candle, TF } from "../api";
import { bucket } from "../api";
import { computeBias, FACTORS, type BiasRow } from "./bias";
import { useOcFeed, useSmt } from "./ocfeed";
import { api } from "../api";
import { computeGex, confirm3, emas, GammaPrimitive, gammaHits, type GexMap } from "./gamma";
import { findEntries, NodesPrimitive, nodesRead, type NodesView } from "./nodes";
import { buildBubbles, buildWalls, OrderFlowPrimitive, readAt, type Wall } from "./orderflow";
import { computeDol } from "./dol";
import { dispatch, scanSignals, type SigPrefs } from "./signalAlerts";
import type { AlertRow } from "../api";
import { EV_LAYER, GUIDE, KIND_ACT } from "./guide";
import { confluence, FootprintPrimitive, FP_SCALE, setFpScale, FP_MEANING, FP_WORD, kfmt, type FpBar } from "./footprint";
import { ZonesPrimitive, type Box, type Seg } from "./zones";
import { BAR_SEC, index, MEANING, shortText, toMarkers, type Ev } from "./chartKit";
import MtfPanel from "./MtfPanel";

export type Layers = { ob: boolean; fvg: boolean; liq: boolean; structure: boolean; swings: boolean; range: boolean; session: boolean; delta: boolean; deltaDetail: boolean; crt: boolean; auction: boolean; volume: boolean; oi: boolean; rvol: boolean; qml?: boolean; fp?: boolean; bias?: boolean; dol?: boolean; smt?: boolean; heat?: boolean; ocAct?: boolean; cvd?: boolean; cells?: boolean; walls?: boolean; bubbles?: boolean; nodes?: boolean; gamma?: boolean };

const C = {
  bull: "#10b981", bear: "#ef4444", gold: "#e8c35a", grid: "#131d33", text: "#94a3b8",
  obBull: "rgba(16,185,129,0.14)", obBear: "rgba(239,68,68,0.14)", fvgBull: "rgba(56,189,248,0.14)", fvgBear: "rgba(244,114,182,0.14)",
};

export default function Chart({ tf, candles, analysis, layers, spot, spotTs, marketOpen, onExpand, expanded, sig, onSignal }: {
  sig?: SigPrefs; onSignal?: (a: AlertRow) => void;
  tf: TF; candles: Candle[]; analysis: Analysis | null; layers: Layers; spot: number | null; spotTs: number; marketOpen: boolean;
  onExpand?: () => void; expanded?: boolean;
}) {
  const box = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const series = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const zz = useRef<ISeriesApi<"Line"> | null>(null);
  const spine = useRef<ISeriesApi<"Line"> | null>(null);           // QML layer: the close-price "line chart"
  const markers = useRef<ISeriesMarkersPluginApi<Time> | null>(null);
  const zones = useRef(new ZonesPrimitive());
  const fpPrim = useRef(new FootprintPrimitive());
  const ofPrim = useRef(new OrderFlowPrimitive());
  const ndPrim = useRef(new NodesPrimitive());
  const gHitsRef = useRef<{ time: number; side: "BUY" | "SELL"; px: number }[]>([]);
  const nEntRef = useRef<ReturnType<typeof findEntries>>([]);
  const ofArgsRef = useRef<Parameters<typeof readAt>[1]>({ fp: [], bubbles: [], walls: [], zones: [], atr: 20, candles: [] });
  const gmPrim = useRef(new GammaPrimitive());
  const emaS = useRef<ISeriesApi<"Line">[]>([]);
  const [gexMap, setGexMap] = useState<GexMap | null>(null);
  const [nodesV, setNodesV] = useState<NodesView | null>(null);
  const cvdS = useRef<ISeriesApi<"Baseline"> | null>(null);
  const [walls, setWalls] = useState<Wall[]>([]);
  const ocFeed = useOcFeed(!!layers.bias || !!layers.ocAct || !!sig?.on);
  const bankS = useRef<ISeriesApi<"Line"> | null>(null);
  const [bank, setBank] = useState<{ time: number; c: number }[]>([]);
  const smtMarks = useSmt(tf, !!layers.smt || !!layers.bias || !!sig?.on, setBank);
  const smtArg = tf === "5m" || tf === "15m" || tf === "1h" ? smtMarks : null;
  const biasMap = useRef<Map<number, BiasRow>>(new Map());
  const ocLane = useRef<ISeriesApi<"Histogram"> | null>(null);
  // PE−CE table 🎯 Action → per candle: +1 CE, −1 PE, 0 wait (last 3-min row inside each candle)
  const ocActs = useMemo(() => {
    if (!ocFeed?.pece?.length || !ocFeed.day) return [] as { time: number; v: number; txt: string }[];
    const [y, m, d] = ocFeed.day.split("-").map(Number);
    const m2 = new Map<number, { v: number; txt: string; t: number }>();
    for (const r of ocFeed.pece) {
      const [hh, mm] = r.ts.split(":").map(Number); const t = Date.UTC(y, m - 1, d, hh, mm) / 1000;
      const txt = (r.act?.[0] ?? "").replace(/<[^>]+>/g, "");
      const v = /no fresh|after close|warm/i.test(txt) ? 0 : /\bCE\b/.test(txt) ? 1 : /\bPE\b/.test(txt) ? -1 : 0;
      const b = bucket(t, tf); const prev = m2.get(b);
      if (!prev || prev.t < t) m2.set(b, { v, txt, t });
    }
    return [...m2.entries()].sort((a2, b2) => a2[0] - b2[0]).map(([time, x]) => ({ time, v: x.v, txt: x.txt }));
  }, [ocFeed, tf]);
  const dolRef = useRef<ReturnType<typeof computeDol>>([]);
  const [fpBars, setFpBars] = useState<FpBar[]>([]);
  const [fpMsg, setFpMsg] = useState<string>("");
  const [, setFpTick] = useState(0);
  const [heatHelp, setHeatHelp] = useState(() => { try { return localStorage.getItem("ict_heat_help") !== "0"; } catch { return true; } });
  const volS = useRef<ISeriesApi<"Histogram"> | null>(null);
  const vwapS = useRef<ISeriesApi<"Line">[]>([]);
  const ivS = useRef<ISeriesApi<"Line"> | null>(null);
  const rvS = useRef<{ h: ISeriesApi<"Histogram">; l: ISeriesApi<"Line"> } | null>(null);
  const oiS = useRef<{ doi: ISeriesApi<"Histogram"> } | null>(null);          // [vwap, +1σ, −1σ, +2σ, −2σ, avwap1, avwap2]
  const last = useRef<Candle | null>(null);
  const evRef = useRef<Map<number, Ev[]>>(new Map());
  const cMap = useRef<Map<number, Candle>>(new Map());
  const [hover, setHover] = useState<{ t: number; x: number; y: number } | null>(null);
  const [latest, setLatest] = useState<Ev[]>([]);
  const [layerNow, setLayerNow] = useState<Record<string, Ev>>({});
  const [guideOpen, setGuideOpen] = useState<boolean>(() => { try { return localStorage.getItem("ict_guide") !== "0"; } catch { return true; } });

  // create once
  useEffect(() => {
    if (!box.current) return;
    const ch = createChart(box.current, {
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: "#0b1220" }, textColor: C.text, fontFamily: "Inter, system-ui" },
      grid: { vertLines: { color: C.grid }, horzLines: { color: C.grid } },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderColor: "#1d2a45" },
      timeScale: { borderColor: "#1d2a45", timeVisible: true, secondsVisible: false, rightOffset: 14, barSpacing: 7, minBarSpacing: 2 },
      localization: { locale: "en-IN", priceFormatter: (p: number) => p.toLocaleString("en-IN", { minimumFractionDigits: 1, maximumFractionDigits: 1 }) },
    });
    const s = ch.addSeries(CandlestickSeries, {
      upColor: C.bull, downColor: C.bear, borderUpColor: C.bull, borderDownColor: C.bear, wickUpColor: C.bull, wickDownColor: C.bear,
      priceLineColor: C.gold,
    });
    s.attachPrimitive(zones.current);
    s.attachPrimitive(fpPrim.current);
    s.attachPrimitive(ofPrim.current);
    s.attachPrimitive(ndPrim.current);
    s.attachPrimitive(gmPrim.current);
    bankS.current = ch.addSeries(LineSeries, { color: "rgba(244,114,182,0.85)", lineWidth: 2, priceScaleId: "bank", title: "BANKNIFTY",
      priceLineVisible: false, lastValueVisible: true, crosshairMarkerVisible: false });
    ch.priceScale("bank").applyOptions({ visible: false, scaleMargins: { top: 0.1, bottom: 0.25 } });
    const z = ch.addSeries(LineSeries, { color: "rgba(232,195,90,0.55)", lineWidth: 1, lineStyle: LineStyle.Solid,
      priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
    // real volume (NIFTY FUT) in its own strip at the bottom; VWAP + σ bands + anchored VWAPs on the price scale
    const vs = ch.addSeries(HistogramSeries, { priceScaleId: "vol", priceFormat: { type: "volume" }, priceLineVisible: false, lastValueVisible: false });
    ch.priceScale("vol").applyOptions({ scaleMargins: { top: 0.84, bottom: 0 } });
    const line = (color: string, width: 1 | 2, style: LineStyle) => ch.addSeries(LineSeries, { color, lineWidth: width, lineStyle: style,
      priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
    // VWAP restarts each session; two line sets (alternate days) so no line bridges the overnight gap
    const set5 = () => [line("#f5f5f4", 2, LineStyle.Solid),
      line("rgba(245,245,244,0.45)", 1, LineStyle.Dashed), line("rgba(245,245,244,0.45)", 1, LineStyle.Dashed),
      line("rgba(245,245,244,0.25)", 1, LineStyle.Dotted), line("rgba(245,245,244,0.25)", 1, LineStyle.Dotted)];
    vwapS.current = [...set5(), line("#c084fc", 2, LineStyle.Solid), line("#fbbf24", 2, LineStyle.Solid), ...set5()];
    volS.current = vs;
    // ATM IV (live only) on its own scale, in a band just above the volume strip
    const iv = ch.addSeries(LineSeries, { color: "#fb923c", lineWidth: 1, priceScaleId: "iv", priceLineVisible: false,
      lastValueVisible: true, crosshairMarkerVisible: false, title: "ATM IV" });
    ch.priceScale("iv").applyOptions({ scaleMargins: { top: 0.66, bottom: 0.18 }, visible: false });
    ivS.current = iv;
    spine.current = ch.addSeries(LineSeries, { color: "rgba(34,211,238,0.75)", lineWidth: 1, priceLineVisible: false,
      lastValueVisible: false, crosshairMarkerVisible: false });
    chart.current = ch; series.current = s; zz.current = z;
    markers.current = createSeriesMarkers(s, []);
    const onMove = (p: MouseEventParams<Time>) => {
      if (!p.point || p.time == null || p.point.x < 0) { setHover(null); return; }
      setHover({ t: p.time as number, x: p.point.x, y: p.point.y });
    };
    ch.subscribeCrosshairMove(onMove);
    ch.applyOptions({ handleScale: { axisPressedMouseMove: { time: true, price: true }, axisDoubleClickReset: { time: true, price: true },
      mouseWheel: true, pinch: true }, handleScroll: { mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: true } });
    // wheel over the RIGHT PRICE AXIS → zoom the price range around the cursor (like TradingView);
    // wheel anywhere else → normal time zoom. Double-click the axis to go back to auto-scale.
    const el = box.current;
    const onWheel = (ev: WheelEvent) => {
      const ps = s.priceScale();
      const r = el.getBoundingClientRect();
      if (ev.clientX < r.right - ps.width()) return;
      ev.preventDefault(); ev.stopPropagation();
      const vr = ps.getVisibleRange();
      const at = s.coordinateToPrice(ev.clientY - r.top);
      if (!vr || at == null) return;
      const k = ev.deltaY > 0 ? 1.12 : 1 / 1.12;
      ps.setAutoScale(false);
      ps.setVisibleRange({ from: at - (at - vr.from) * k, to: at + (vr.to - at) * k });
    };
    el.addEventListener("wheel", onWheel, { passive: false, capture: true });
    return () => {
      ch.unsubscribeCrosshairMove(onMove);
      el.removeEventListener("wheel", onWheel, { capture: true } as EventListenerOptions); ch.remove(); chart.current = null; series.current = null; };
  }, []);

  // candles
  useEffect(() => {
    if (!series.current) return;
    series.current.setData(candles.map((c) => ({ time: c.time as UTCTimestamp, open: c.open, high: c.high, low: c.low, close: c.close })));
    last.current = candles.length ? { ...candles[candles.length - 1] } : null;
    cMap.current = new Map(candles.map((c) => [c.time, c]));
  }, [candles]);

  // fit when the timeframe changes
  useEffect(() => {
    const ch = chart.current;
    if (!ch || !candles.length) return;
    const n = candles.length;
    const bars = Math.max(40, Math.min(160, Math.round((box.current?.clientWidth ?? 900) / 7)));  // ~7 px per candle
    ch.timeScale().setVisibleLogicalRange({ from: Math.max(0, n - bars), to: n + 14 });
  }, [tf, candles.length > 0]);   // only when the timeframe changes or the first data arrives

  // live tick → update the forming candle
  useEffect(() => {
    const s = series.current;
    // after the close Dhan can keep sending a static LTP — never grow a phantom candle outside market hours
    if (!s || spot == null || !last.current || !marketOpen) return;
    const t = bucket(spotTs + 19800, tf);
    const lc = last.current;
    if (t < lc.time) return;
    if (t === lc.time) {
      lc.high = Math.max(lc.high, spot); lc.low = Math.min(lc.low, spot); lc.close = spot;
    } else {
      last.current = { time: t, open: spot, high: spot, low: spot, close: spot, closed: false };
    }
    const c = last.current;
    s.update({ time: c.time as UTCTimestamp, open: c.open, high: c.high, low: c.low, close: c.close });
  }, [spot, spotTs, tf, marketOpen]);

  // overlays
  useEffect(() => {
    const s = series.current;
    if (!s || !markers.current || !zz.current) return;
    const a = analysis;
    const boxes: Box[] = [];
    const segs: Seg[] = [];
    const lastTime = candles.length ? candles[candles.length - 1].time : 0;
    if (a) {
      if (layers.range && a.range) {
        const r = a.range;
        const [lo, hi] = r.ote_band;
        boxes.push({ from: r.from, to: r.to, top: r.high, bottom: r.eq, fill: "rgba(239,68,68,0.04)" });
        boxes.push({ from: r.from, to: r.to, top: r.eq, bottom: r.low, fill: "rgba(16,185,129,0.04)" });
        boxes.push({ from: r.from, to: r.to, top: hi, bottom: lo, fill: "rgba(232,195,90,0.10)", stroke: "rgba(232,195,90,0.7)", dash: true,
          label: `OTE 61.8–79 % (${r.dir === "bull" ? "buy" : "sell"} zone)`, labelColor: C.gold });
        segs.push({ from: r.from, price: r.eq, color: "rgba(232,195,90,0.8)", label: `EQ 50 % ${r.eq.toFixed(1)}`, dash: true });
      }
      for (const z of a.zones) {
        if (z.type === "RJB") {                      // rejection block: long-wick part of a swing candle (violet)
          if (!layers.ob) continue;
          const sell = z.dir === "bear";
          boxes.push({ from: z.from, to: z.to, top: z.top, bottom: z.bottom,
            fill: sell ? "rgba(192,132,252,0.13)" : "rgba(129,140,248,0.13)", stroke: sell ? "rgba(192,132,252,0.8)" : "rgba(129,140,248,0.8)",
            dash: true, label: z.label, labelColor: sell ? "#d8b4fe" : "#a5b4fc" });
          continue;
        }
        if (z.type === "IFVG") {                     // inversion FVG: failed gap that flipped role (orange = sell zone, teal = buy zone)
          if (!layers.fvg) continue;
          const sell = z.dir === "bear";
          boxes.push({ from: z.from, to: z.to, top: z.top, bottom: z.bottom,
            fill: sell ? "rgba(251,146,60,0.15)" : "rgba(45,212,191,0.13)", stroke: sell ? "rgba(251,146,60,0.85)" : "rgba(45,212,191,0.85)",
            dash: true, label: z.label, labelColor: sell ? "#fdba74" : "#5eead4" });
          continue;
        }
        const isOB = z.type !== "FVG";
        if (isOB && !layers.ob) continue;
        if (!isOB && !layers.fvg) continue;
        const bull = z.dir === "bull";
        boxes.push({
          from: z.from, to: z.to, top: z.top, bottom: z.bottom,
          fill: isOB ? (bull ? C.obBull : C.obBear) : (bull ? C.fvgBull : C.fvgBear),
          stroke: isOB ? (bull ? "rgba(16,185,129,0.6)" : "rgba(239,68,68,0.6)") : undefined,
          dash: z.type === "BREAKER", label: z.label, labelColor: isOB ? (bull ? "#6ee7b7" : "#fca5a5") : (bull ? "#7dd3fc" : "#f9a8d4"),
        });
      }
      if (layers.liq) {
        for (const p of a.pools) {
          const strong = p.src !== "SWING";
          segs.push({ from: p.from, price: p.price, color: p.side === "BSL" ? "#f59e0b" : "#a78bfa", dash: !strong, width: strong ? 1.5 : 1,
            label: `${p.side} ${p.src}${p.members > 1 ? `×${p.members}` : ""} ${p.price.toFixed(1)}` });
        }
      }
      if (layers.session && a.session) {
        const S = a.session;
        const t0 = candles.find((c) => Math.floor(c.time / 86400) === Math.floor(lastTime / 86400))?.time ?? lastTime;
        if (S.prev) {
          segs.push({ from: t0, price: S.prev.h, color: "#38bdf8", label: `PDH ${S.prev.h.toFixed(1)}` });
          segs.push({ from: t0, price: S.prev.l, color: "#38bdf8", label: `PDL ${S.prev.l.toFixed(1)}` });
          segs.push({ from: t0, price: S.prev.c, color: "rgba(148,163,184,0.7)", label: `PDC ${S.prev.c.toFixed(1)}`, dash: true });
        }
        if (S.or) {
          boxes.push({ from: t0, to: lastTime, top: S.or.h, bottom: S.or.l, fill: "rgba(148,163,184,0.04)", stroke: "rgba(148,163,184,0.25)", dash: true, label: "Opening range 09:15–09:30" });
        }
      }
      // setup D — CRT range (CRT candle → end of the manipulation candle), its 50 % (TP-1) and the CRTH / CRTL edges
      if (layers.crt && a.crt) {
        for (const r of a.crt.ranges) {
          const bear = r.biases.includes("bear") && !r.biases.includes("bull");
          const bull = r.biases.includes("bull") && !r.biases.includes("bear");
          const edge = bear ? "rgba(239,68,68,0.75)" : bull ? "rgba(16,185,129,0.75)" : "rgba(232,195,90,0.75)";
          const kl = r.keylevels.map((k) => k.kind).join("+");
          boxes.push({ from: r.chart_from, to: r.chart_to, top: r.crth, bottom: r.crtl, fill: "rgba(232,195,90,0.05)", stroke: edge,
            label: `CRT ${r.htf} ${r.time}${r.aplus ? " A+" : ""} · ${kl} · ${r.status}${r.rvol != null ? ` · vol ${r.rvol.toFixed(1)}×` : ""}`, labelColor: C.gold });
          boxes.push({ from: r.chart_from, to: r.chart_to, top: r.mid, bottom: r.mid, fill: "rgba(232,195,90,0.9)", stroke: "rgba(232,195,90,0.9)", dash: true });
        }
      }
      // auction: previous-day value (VAH / VAL / POC, TPO time profile), today's developing POC, balance boxes
      if (layers.auction && a.auction) {
        const A = a.auction;
        const t0 = candles.find((c) => Math.floor(c.time / 86400) === Math.floor(lastTime / 86400))?.time ?? lastTime;
        if (A.prev) {
          boxes.push({ from: t0, to: lastTime, top: A.prev.vah, bottom: A.prev.val, fill: "rgba(56,189,248,0.04)", stroke: "rgba(56,189,248,0.35)", dash: true,
            label: `Prev value (${(A.prev as { kind?: string }).kind === "volume" ? "volume" : "TPO"}) ${A.prev.val.toFixed(0)}–${A.prev.vah.toFixed(0)}`, labelColor: "#7dd3fc" });
          segs.push({ from: t0, price: A.prev.poc, color: "rgba(56,189,248,0.9)", width: 1.5, label: `pPOC ${A.prev.poc.toFixed(1)}` });
        }
        if (A.today && !(layers.volume && a.volume?.available)) segs.push({ from: t0, price: A.today.poc, color: "rgba(232,121,249,0.85)", dash: true, label: `dev POC ${A.today.poc.toFixed(1)}` });
        for (const x of A.boxes) {
          boxes.push({ from: x.from, to: x.active ? lastTime : x.to, top: x.hi, bottom: x.lo, fill: "rgba(226,232,240,0.05)",
            stroke: x.active ? "rgba(226,232,240,0.7)" : "rgba(226,232,240,0.3)", dash: !x.active,
            label: `Balance ${x.bars} bars${x.escaped ? ` · escaped ${x.escaped}` : x.active ? " · live" : ""}`, labelColor: "#cbd5e1" });
        }
      }
      // volume layer: today's volume POC, HVNs (acceptance) / LVNs (fast-move gaps), naked POCs (magnets)
      if (layers.volume && a.volume?.available) {
        const V = a.volume;
        const t0 = candles.find((c) => Math.floor(c.time / 86400) === Math.floor(lastTime / 86400))?.time ?? lastTime;
        if (V.today) {
          segs.push({ from: t0, price: V.today.poc, color: "rgba(250,204,21,0.9)", width: 2, label: `vPOC ${V.today.poc.toFixed(1)}` });
          for (const h of V.today.hvn) if (Math.abs(h - V.today.poc) > V.today.step) segs.push({ from: t0, price: h, color: "rgba(250,204,21,0.45)", dash: true, label: `HVN ${h.toFixed(0)}` });
          for (const l of V.today.lvn) segs.push({ from: t0, price: l, color: "rgba(148,163,184,0.5)", dash: true, label: `LVN ${l.toFixed(0)}` });
        }
        for (const n of V.naked ?? []) segs.push({ from: t0, price: n.price, color: "rgba(251,146,60,0.9)", dash: true, label: `naked POC ${n.day.slice(5)} ${n.price.toFixed(1)}` });
      }
    }
    if (a && layers.qml && a.qml) {
      const live = a.qml.setups.filter((x) => x.status === "ARMED" || x.status === "TRIGGERED" || x.status === "TP1 HIT").slice(-3);
      for (const x of live) {
        const bear = x.dir === "bear";
        const col = x.kind === "QML" ? (bear ? "#f472b6" : "#22d3ee") : bear ? "#fb7185" : "#34d399";
        boxes.push({ from: x.from, to: lastTime, top: x.level + x.tol, bottom: x.level - x.tol,
          fill: bear ? "rgba(244,114,182,0.10)" : "rgba(34,211,238,0.10)", stroke: col, dash: x.status === "ARMED",
          label: `${x.name} ${x.level.toFixed(0)} · ${x.status.toLowerCase()} · ${x.side}`, labelColor: col });
        segs.push({ from: x.from, price: x.sl, color: "rgba(239,68,68,0.85)", dash: true, label: `${x.kind} SL ${x.sl.toFixed(0)}` });
        segs.push({ from: x.from, price: x.tp1, color: "rgba(34,197,94,0.75)", dash: true, label: `${x.kind} TP1 ${x.tp1.toFixed(0)}` });
        segs.push({ from: x.from, price: x.tp2, color: "rgba(34,197,94,0.5)", dash: true, label: `${x.kind} TP2 ${x.tp2.toFixed(0)} (2.2R)` });
      }
    }
    const dayStarts: number[] = [];
    if (tf !== "1D") for (let i = 1; i < candles.length; i++) if (Math.floor(candles[i].time / 86400) !== Math.floor(candles[i - 1].time / 86400)) dayStarts.push(candles[i].time);
    if (a && layers.dol) {
      const dols = computeDol(candles, a);
      const act = [...dols].reverse().find((d) => !d.hit);
      if (act) {
        if (act.kind === "IRL") boxes.push({ from: act.from, to: lastTime, top: act.hi, bottom: act.lo, fill: "rgba(167,139,250,0.16)",
          stroke: "rgba(167,139,250,0.9)", label: `${act.label} · after ${act.why}`, labelColor: "#c4b5fd", dash: true });
        else segs.push({ from: act.from, price: act.lo, color: "rgba(167,139,250,0.95)", width: 2, dash: true, label: `${act.label} · after ${act.why}` });
      }
      dolRef.current = dols;
    } else dolRef.current = [];
    zones.current.set(boxes, segs, lastTime, dayStarts);

    // ── markers: every layer emits Ev records → merged per bar/side, labelled only where it matters ──
    const evs: Ev[] = [];
    const push = (e: Ev) => evs.push(e);
    const side = (bull: boolean): "above" | "below" => (bull ? "below" : "above");
    if (a && (layers.structure || layers.delta || layers.fvg)) {
      for (const m of a.markers) {
        const bull = m.dir === "bull";
        if (m.kind.startsWith("IFVG")) {
          if (!layers.fvg) continue;
          const sig = m.kind !== "IFVG_FLIP", sell = m.dir === "bear";
          push({ time: m.time, side: sell ? "above" : "below", dir: m.dir, kind: m.kind, layer: "IFVG", full: m.text,
            short: sig ? (sell ? "IFVG▼ sell" : "IFVG▲ buy") : "inv", pri: sig ? 3 : 1, size: sig ? 1.15 : 0.5,
            shape: sig ? (sell ? "arrowDown" : "arrowUp") : "circle", color: sell ? "#fb923c" : "#2dd4bf" });
          continue;
        }
        if (!(layers.structure || layers.delta)) continue;
        const isDelta = m.kind.startsWith("DELTA_") || m.kind === "FOOTPRINT_IMBALANCE" || m.kind.startsWith("PROXY_FLOW");
        if (isDelta ? !layers.delta : !layers.structure) continue;
        if (isDelta && !layers.deltaDetail && (m.kind === "DELTA_FLIP" || m.kind === "FOOTPRINT_IMBALANCE" || m.kind === "PROXY_FLOW_SURGE")) continue;
        const base = { time: m.time, side: side(bull), dir: m.dir, kind: m.kind, full: `${m.text}${m.price ? ` @ ${m.price.toFixed(1)}` : ""}` };
        if (m.kind.startsWith("PROXY_FLOW")) { push({ ...base, layer: "Delta", short: "flow", color: "#fb923c", shape: "circle", size: 0.5, pri: 1, full: base.full + " (proxy)" }); continue; }
        if (isDelta) {
          const abs = m.kind === "DELTA_ABSORPTION", flip = m.kind === "DELTA_FLIP";
          push({ ...base, layer: "Delta", short: shortText(m.kind, m.text), pri: abs ? 2 : 1, size: abs ? 0.8 : 0.5,
            shape: abs ? "circle" : "square",
            color: abs ? "#e8c35a" : flip ? "#e2e8f0" : m.kind === "FOOTPRINT_IMBALANCE" ? (bull ? "#22d3ee" : "#f472b6") : (bull ? "#34d399" : "#f87171") });
          continue;
        }
        const isStruct = m.kind === "BOS" || m.kind === "MSS";
        const weak = /weak/i.test(m.text);
        if (isStruct) {
          push({ ...base, layer: "Structure", short: m.kind === "MSS" ? "MSS" : weak ? "bos" : "BOS",
            pri: m.kind === "MSS" ? 3 : weak ? 1 : 2, size: m.kind === "MSS" ? 1.3 : weak ? 0.55 : 1,
            shape: bull ? "arrowUp" : "arrowDown",
            color: weak ? (bull ? "rgba(16,185,129,0.55)" : "rgba(239,68,68,0.55)") : (bull ? C.bull : C.bear) });
        } else {
          push({ ...base, layer: "Liquidity", short: shortText(m.kind, m.text), pri: m.kind === "SWEEP" ? 2 : 1, size: 0.55,
            shape: "circle", color: m.kind === "SWEEP" ? "#f59e0b" : "#a78bfa" });
        }
      }
    }
    if (a && layers.qml && a.qml) {
      for (const m of a.qml.markers) {
        const bear = m.dir === "bear", trig = m.kind.endsWith("_TRIG"), armed = m.kind.endsWith("_ARMED");
        const pt = m.kind === "QML_PT", snr = m.kind.startsWith("SNR_");
        const kind = m.kind.split("_")[0];
        push({ time: m.time, side: bear ? "above" : "below", dir: m.dir, kind: m.kind, layer: "QML", full: m.text,
          short: trig ? `${kind}${bear ? "▼" : "▲"}` : armed ? `${kind} armed` : pt ? m.text.split(" ")[0] : snr ? (bear ? "upthrust" : "spring") : m.text.slice(0, 8),
          pri: trig ? 3 : armed || snr ? 2 : 1, size: trig ? 1.2 : pt ? 0.4 : 0.7,
          shape: trig ? (bear ? "arrowDown" : "arrowUp") : pt ? "circle" : "square",
          color: snr ? "#fbbf24" : bear ? "#f472b6" : "#22d3ee" });
      }
    }
    if (a && layers.crt && a.crt) {
      for (const m of a.crt.markers) {
        const bull = m.dir === "bull";
        const entry = m.kind === "CRT_ENTRY", tbs = m.kind === "CRT_TBS";
        push({ time: m.time, side: side(bull), dir: m.dir, kind: entry ? "CRT_ENTRY" : tbs ? "CRT_TBS" : "CRT_M", layer: "CRT",
          short: entry ? "CRT▶" : tbs ? "TBS" : m.text.slice(0, 3), full: m.text, pri: entry ? 3 : tbs ? 2 : 1,
          shape: entry ? (bull ? "arrowUp" : "arrowDown") : tbs ? "square" : "circle",
          color: entry ? (bull ? "#22c55e" : "#ef4444") : tbs ? "#e8c35a" : "#cbd5e1", size: entry ? 1.3 : 0.7 });
      }
    }
    if (a && layers.auction && a.auction) {
      for (const m of a.auction.markers) {
        const acc = m.kind === "AUCTION_ACCEPT", rej = m.kind === "AUCTION_REJECT";
        const bull = m.dir === "bull";
        push({ time: m.time, side: side(bull), dir: m.dir, kind: m.kind, layer: "Auction",
          short: acc ? "ACC" : rej ? "REJ" : "–", full: m.text, pri: acc ? 3 : rej ? 2 : 1,
          shape: acc ? (bull ? "arrowUp" : "arrowDown") : "circle", color: acc ? "#38bdf8" : rej ? "#fb923c" : "#94a3b8", size: acc ? 0.9 : 0.7 });
      }
    }
    if (a && layers.dol) {
      for (const d of dolRef.current) {
        push({ time: d.from, side: d.dir === "bull" ? "below" : "above", dir: d.dir, kind: "DOL", layer: "D.O.L",
          short: d.kind === "IRL" ? "→FVG" : "→ERL", full: `${d.label} · after ${d.why}${d.hit ? " · ✓ reached" : " · active"}`,
          pri: 2, shape: "circle", color: "#a78bfa", size: 0.6 });
        if (d.hit) push({ time: d.hit, side: d.dir === "bull" ? "above" : "below", dir: d.dir, kind: "DOL_HIT", layer: "D.O.L",
          short: "DOL✓", full: `${d.label} reached`, pri: 1, shape: "square", color: "#a78bfa", size: 0.5 });
      }
    }
    if (layers.ocAct) {
      let pv = 0;
      for (const x of ocActs) {
        if (x.v !== 0 && x.v !== pv) push({ time: x.time, side: x.v > 0 ? "below" : "above", dir: x.v > 0 ? "bull" : "bear", kind: "OC_ACT", layer: "PE−CE action",
          short: x.v > 0 ? "OC: CE" : "OC: PE", full: `PE−CE table → ${x.txt}`, pri: 3, shape: x.v > 0 ? "arrowUp" : "arrowDown",
          color: x.v > 0 ? "#4ade80" : "#f87171", size: 1.2 });
        pv = x.v;
      }
    }
    if (layers.smt) {
      for (const m of smtMarks) push({ time: m.time, side: m.dir === "bull" ? "below" : "above", dir: m.dir, kind: "SMT", layer: "SMT",
        short: m.dir === "bull" ? "SMT▲" : "SMT▼", full: `${m.text} (pivot ${m.pivot_at}, confirmed ${m.at})`, pri: 3,
        shape: m.dir === "bull" ? "arrowUp" : "arrowDown", color: m.dir === "bull" ? "#34d399" : "#f472b6", size: 1 });
    }
    if (a && layers.rvol) {
      const RC: Record<string, string> = { RVOL_ACTIVATED: "#22c55e", BREAKOUT_WITH_RVOL: "#22c55e", RVOL_SPIKE: "#e8c35a", RVOL_FADING: "#94a3b8", BREAKOUT_NO_RVOL: "#ef4444" };
      const RT: Record<string, string> = { RVOL_ACTIVATED: "RVOL▲", BREAKOUT_WITH_RVOL: "BO✓vol", RVOL_SPIKE: "RVOL⚡", RVOL_FADING: "RVOL▼", BREAKOUT_NO_RVOL: "BO✗vol" };
      for (const e of a.volume?.rvol_events ?? []) {
        const bt = bucket(e.time, tf), cc = cMap.current.get(bt);
        const vw = [...(a.volume?.vwap ?? [])].reverse().find((q) => q.time <= bt);
        const up = cc ? cc.close >= cc.open : null;
        const who = up == null ? "" : up ? " ▲ buyers" : " ▼ sellers";
        const act = up == null ? "" : up ? (vw && cc!.close > vw.v ? " — institutions BUYING above VWAP: CE side only, never PE" : " — buying, but below VWAP: wait")
          : (vw && cc!.close < vw.v ? " — institutions SELLING below VWAP: PE side only, never CE" : " — selling, but above VWAP: likely a dip, wait");
        push({ time: bt, side: "above", dir: up == null ? "flat" : up ? "bull" : "bear", kind: e.kind, layer: "RVOL",
          short: `${RT[e.kind] ?? e.kind} ${e.value.toFixed(1)}×${e.kind === "RVOL_FADING" ? "" : who}`, full: `${RT[e.kind] ?? e.kind} ${e.value.toFixed(2)}×${who} — ${e.title ?? ""}${e.kind === "RVOL_FADING" ? "" : act}`,
          pri: e.kind === "RVOL_FADING" ? 2 : 3, shape: e.kind === "BREAKOUT_NO_RVOL" ? "arrowDown" : "circle", color: RC[e.kind] ?? "#e8c35a", size: 0.7 });
      }
    }
    if (a && layers.oi) {
      for (const x of a.volume?.oi_line ?? []) {
        if (x.surge) push({ time: x.time, side: "above", dir: "flat", kind: "OI_SURGE", layer: "Futures OI", short: `OI⚡${x.surge_x ?? ""}×`,
          full: `Futures OI surge ${x.surge_x ?? ""}× normal${x.build ? ` · ${x.build}` : ""}`, pri: 1, shape: "square", color: "#f59e0b", size: 0.6 });
      }
    }
    evRef.current = index(evs);
    const lastSig = [...evs].filter((e) => e.pri >= 2).sort((x, y) => y.time - x.time).slice(0, 4);
    setLatest(lastSig);
    const ln: Record<string, Ev> = {};
    for (const e of [...evs].sort((x, y) => x.time - y.time)) ln[e.layer] = e;
    setLayerNow(ln);
    markers.current.setMarkers(toMarkers(evs, lastTime, BAR_SEC[tf] ?? 300));

    const V = a?.volume;
    const on = !!(a && layers.volume && V?.available);
    volS.current?.setData(on ? (V!.bars ?? []).map((b) => ({ time: b.time as UTCTimestamp, value: b.v,
      color: b.rvol != null && b.rvol >= 1.5 ? (b.up ? "rgba(16,185,129,0.95)" : "rgba(239,68,68,0.95)")
        : b.rvol != null && b.rvol <= 0.7 ? "rgba(100,116,139,0.35)" : (b.up ? "rgba(16,185,129,0.45)" : "rgba(239,68,68,0.45)") })) : []);
    const vw = on ? V!.vwap ?? [] : [];
    const L = vwapS.current;
    const day = (t: number) => Math.floor(t / 86400);
    const days = [...new Set(vw.map((p) => day(p.time)))].slice(-2);     // today + yesterday: one line set each
    if (L.length) {
      const k = (V?.bands ?? [1, 2]);
      const fs = [(p: { v: number; sd: number }) => p.v, (p: { v: number; sd: number }) => p.v + k[0] * p.sd,
        (p: { v: number; sd: number }) => p.v - k[0] * p.sd, (p: { v: number; sd: number }) => p.v + (k[1] ?? 2) * p.sd,
        (p: { v: number; sd: number }) => p.v - (k[1] ?? 2) * p.sd];
      for (const par of [0, 1]) {
        const pts = vw.filter((p) => days.indexOf(day(p.time)) === par);
        fs.forEach((f, q) => L[(par ? 7 : 0) + q].setData(pts.map((p) => ({ time: p.time as UTCTimestamp, value: f(p) }))));
      }
      const av = on ? V!.avwaps ?? [] : [];
      for (let q = 0; q < 2; q++) L[5 + q].setData((av[q]?.points ?? []).map((p) => ({ time: p.time as UTCTimestamp, value: p.v })));
    }
    ivS.current?.setData(a && layers.volume ? (V?.iv ?? []).map((p) => ({ time: p.time as UTCTimestamp, value: p.iv })) : []);
    // futures OI pane (pane 1): OI line + its 8-EMA; ΔOI bars coloured by build-up (pane 2). Created only when shown,
    // removed when hidden, so the price chart keeps its full height otherwise.
    const ch = chart.current;
    const ol = a && layers.oi ? (a.volume?.oi_line ?? []) : [];
    // Futures OI: the raw OI line (+ EMA 8) is almost flat intraday (OI moves 1–3 % a day), so only the per-bar
    // ΔOI build-up bars are drawn — green long build-up, light green short covering, red short build-up,
    // pink long unwinding, amber = surge.
    if (ch && ol.length && !oiS.current) {
      const d = ch.addSeries(HistogramSeries, { priceLineVisible: false, lastValueVisible: false, title: "FUT OI build-up", priceFormat: { type: "volume" } }, 1);
      oiS.current = { doi: d };
    } else if (ch && !ol.length && oiS.current) {
      ch.removeSeries(oiS.current.doi);
      oiS.current = null;
    }
    if (oiS.current && ol.length) {
      const BC: Record<string, string> = { "long build-up": "#22c55e", "short covering": "#86efac", "short build-up": "#ef4444", "long unwinding": "#fca5a5" };
      oiS.current.doi.setData(ol.filter((x) => x.doi != null).map((x) => ({ time: x.time as UTCTimestamp, value: x.doi as number,
        color: x.surge ? "#f59e0b" : (BC[x.build ?? ""] ?? "rgba(148,163,184,0.5)") })));
    }
    // cumulative RVOL pane: histogram = 20-day cRVOL (5-day if 20 sessions aren't stored yet), white line = 5-day.
    // Lines at 1.0 (a normal day) and 1.5 (in play).
    const cr = a && layers.rvol ? (a.volume?.crvol ?? []).filter((p) => (p.c20 ?? p.c5) != null) : [];
    if (ch && cr.length && !rvS.current) {
      const idx = oiS.current ? 2 : 1;
      const h = ch.addSeries(HistogramSeries, { priceLineVisible: false, lastValueVisible: true, title: "cRVOL", base: 0 }, idx);
      const l = ch.addSeries(LineSeries, { color: "#f1f5f9", lineWidth: 1, priceLineVisible: false, lastValueVisible: false, title: "5d" }, idx);
      h.createPriceLine({ price: 1, color: "rgba(148,163,184,0.7)", lineWidth: 1, lineStyle: LineStyle.Dashed, axisLabelVisible: false, title: "" });
      h.createPriceLine({ price: 1.5, color: "rgba(34,197,94,0.8)", lineWidth: 1, lineStyle: LineStyle.Dashed, axisLabelVisible: true, title: "in play" });
      try { ch.panes()[idx]?.setStretchFactor(1.2); } catch { /* older lib */ }
      rvS.current = { h, l };
    } else if (ch && !cr.length && rvS.current) {
      ch.removeSeries(rvS.current.h); ch.removeSeries(rvS.current.l); rvS.current = null;
    }
    if (rvS.current && cr.length) {
      rvS.current.h.setData(cr.map((p) => { const v = (p.c20 ?? p.c5) as number; return { time: p.time as UTCTimestamp, value: v,
        color: v >= 2.5 ? "#e8c35a" : v >= 1.5 ? "#22c55e" : v >= 1 ? "rgba(45,212,191,0.6)" : "rgba(100,116,139,0.6)" }; }));
      rvS.current.l.setData(cr.filter((p) => p.c5 != null && p.c20 != null).map((p) => ({ time: p.time as UTCTimestamp, value: p.c5 as number })));
    }
    // pane heights: price always dominant, each indicator pane a thin strip
    try {
      const P = ch?.panes() ?? [];
      P.forEach((p, i) => p.setStretchFactor(i === 0 ? 7 : 1.3));
    } catch { /* older lib */ }
    spine.current?.setData(a && layers.qml ? candles.filter((c) => c.closed !== false).map((c) => ({ time: c.time as UTCTimestamp, value: c.close })) : []);
    zz.current.setData(a && layers.swings ? dedupe(a.zigzag).map((p) => ({ time: p.time as UTCTimestamp, value: p.price })) : []);
  }, [analysis, layers, candles, smtMarks, ocActs]);

  // ── Order-flow Pro: OI walls (real chain, every 60 s) ──
  useEffect(() => {
    if (!layers.walls) setWalls([]);
    if (!layers.gamma) setGexMap(null);
    if (!layers.walls && !layers.gamma) return;
    let dead = false;
    const go = () => api.chain().then((ch) => { if (dead || !ch?.rows?.length) return; const sp = ch.spot || spot || ch.atm;
      if (layers.walls) setWalls(buildWalls(ch.rows, sp));
      if (layers.gamma) setGexMap(computeGex(ch.rows, sp, 65)); }).catch(() => {});
    go(); const id = window.setInterval(go, 60000);
    return () => { dead = true; window.clearInterval(id); };
  }, [layers.walls, layers.gamma]); // eslint-disable-line react-hooks/exhaustive-deps
  const bubbles = useMemo(() => layers.bubbles || layers.cvd || layers.cells
    ? buildBubbles(analysis?.volume?.bars ?? [], fpBars, candles) : [], [layers.bubbles, layers.cvd, layers.cells, analysis, fpBars, candles]);
  useEffect(() => {
    ofPrim.current.set({ walls, bubbles: layers.bubbles ? bubbles : [], cells: layers.cells ? fpBars.filter((b) => b.rows.length) : [] });
  }, [walls, bubbles, fpBars, layers.bubbles, layers.cells]);
  // ── stacked D / W / M volume nodes (refreshed every 5 min) + overextension entries ──
  useEffect(() => {
    if (!layers.nodes) { setNodesV(null); return; }
    let dead = false;
    const go = () => fetch("/api/nodes").then((r) => r.json()).then((j) => { if (!dead) setNodesV(j); }).catch(() => {});
    go(); const id = window.setInterval(go, 300000);
    return () => { dead = true; window.clearInterval(id); };
  }, [layers.nodes]);
  const nEntries = useMemo(() => layers.nodes && nodesV?.available
    ? findEntries(candles, analysis?.volume?.vwap ?? [], nodesV) : [], [layers.nodes, nodesV, candles, analysis]);
  nEntRef.current = nEntries;
  useEffect(() => { ndPrim.current.set(layers.nodes ? nodesV : null, nEntries); }, [layers.nodes, nodesV, nEntries]);
  // ── gamma map + 3 confirmations ──
  const rvMap = useMemo(() => new Map((analysis?.volume?.bars ?? []).map((b) => [b.time, b.rvol])), [analysis]);
  const emaArr = useMemo(() => layers.gamma && candles.length ? [emas(candles, 9), emas(candles, 21), emas(candles, 50)] : null, [layers.gamma, candles]);
  const gHits = useMemo(() => layers.gamma ? gammaHits(candles, rvMap, gexMap, analysis?.atr ?? 20, marketOpen) : [], [layers.gamma, candles, rvMap, gexMap, analysis, marketOpen]);
  gHitsRef.current = gHits;
  useEffect(() => { gmPrim.current.set(layers.gamma ? gexMap : null, gHits); }, [layers.gamma, gexMap, gHits]);
  useEffect(() => {
    const ch = chart.current; if (!ch) return;
    if (!layers.gamma || !emaArr) { emaS.current.forEach((x) => ch.removeSeries(x)); emaS.current = []; return; }
    if (!emaS.current.length) emaS.current = [["#facc15", 9], ["#a78bfa", 21], ["#f87171", 50]].map(([c, n]) => ch.addSeries(LineSeries,
      { color: c as string, lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false, title: `EMA ${n}` }));
    emaS.current.forEach((x, k) => x.setData(candles.map((c, i) => ({ time: c.time as UTCTimestamp, value: emaArr[k][i] }))));
  }, [layers.gamma, emaArr, candles]);
  // CVD in its own pane under the chart (created / removed with the layer)
  useEffect(() => {
    const ch = chart.current; if (!ch) return;
    if (!layers.cvd) { if (cvdS.current) { ch.removeSeries(cvdS.current); cvdS.current = null; } return; }
    if (!cvdS.current) {
      cvdS.current = ch.addSeries(BaselineSeries, { baseValue: { type: "price", price: 0 }, lineWidth: 2,
        topLineColor: "#4ade80", topFillColor1: "rgba(34,197,94,0.35)", topFillColor2: "rgba(34,197,94,0.02)",
        bottomLineColor: "#f87171", bottomFillColor1: "rgba(239,68,68,0.02)", bottomFillColor2: "rgba(239,68,68,0.35)",
        priceLineVisible: false, lastValueVisible: true, title: "CVD (est.)",
        priceFormat: { type: "custom", formatter: (v: number) => kfmt(v), minMove: 1 } }, 1);
      try { ch.panes()[1]?.setHeight(140); } catch { /* older engine */ }
    }
    cvdS.current.setData(fpBars.filter((b) => b.rows.length).map((b) => ({ time: b.time as UTCTimestamp, value: b.cvd })));
  }, [layers.cvd, fpBars]);

  // ESTIMATED footprint on the chart (5m / 15m / 1h; closed candles from the desk's recorded FUT ticks)
  useEffect(() => {
    const want = !!layers.fp || !!layers.bias || !!layers.heat || !!layers.cvd || !!layers.cells || !!layers.bubbles;
    const ok = want && (tf === "5m" || tf === "15m" || tf === "1h");
    if (!ok) { fpPrim.current.set([]); setFpBars([]); setFpMsg(layers.fp ? "Footprint: 5m / 15m / 1h only" : ""); if (!want) return; if (!ok) return; }
    let dead = false;
    const go = () => fetch(`/api/footprints?tf=${tf}&n=${layers.cvd || layers.cells || layers.bubbles ? 75 : 30}`).then((r) => r.json()).then((j) => {
      if (dead) return;
      const raw: FpBar[] = j.available ? j.bars ?? [] : [];
      setFpBars(raw);
      setFpMsg(j.available ? "" : `Footprint: ${j.message ?? "no recorded ticks"}`);
    }).catch(() => {});
    go();
    const id = window.setInterval(go, 30000);
    return () => { dead = true; window.clearInterval(id); };
  }, [tf, layers.fp, layers.bias, layers.heat, layers.cvd, layers.cells, layers.bubbles]);

  useEffect(() => {
    const a = analysis;
    const marks: { time: number; dir: string; label: string }[] = [];
    const ok = (k: string) => ["BOS", "MSS", "IFVG_BUY", "IFVG_SELL"].includes(k);
    for (const m of a?.markers ?? []) if (ok(m.kind)) marks.push({ time: m.time, dir: m.dir, label: m.kind.replace("_", " ") });
    for (const m of a?.crt?.markers ?? []) if (m.kind === "CRT_ENTRY") marks.push({ time: m.time, dir: m.dir, label: "CRT entry" });
    for (const m of a?.auction?.markers ?? []) if (m.kind === "AUCTION_ACCEPT") marks.push({ time: m.time, dir: m.dir, label: "auction ACC" });
    for (const m of a?.qml?.markers ?? []) if (m.kind.endsWith("_TRIG")) marks.push({ time: m.time, dir: m.dir, label: m.kind.replace("_TRIG", "") });
    const sigOn = !!sig?.on && !!onSignal && marketOpen;
    const confAll = layers.fp || sigOn ? confluence(fpBars, marks, BAR_SEC[tf] ?? 300) : [];
    const withConf = layers.fp ? confAll : [];
    const allB = layers.bias || sigOn ? computeBias(candles, a, fpBars, BAR_SEC[tf] ?? 300, 0.7, ocFeed, false, smtArg) : [];
    if (sigOn && sig && onSignal) {
      try { const lc = [...candles].reverse().filter((c) => c.closed !== false);
      const fnow = lc[0] && (layers.cvd || layers.cells || layers.walls || layers.bubbles) ? readAt(lc[0].time, ofArgsRef.current) : null;
      const fprev = lc[1] && fnow ? readAt(lc[1].time, ofArgsRef.current) : null;
      dispatch(scanSignals({ tf, candles, a, prefs: sig, bias: allB, conf: confAll, ocActs, smt: smtArg, dols: computeDol(candles, a),
        gamma: gHitsRef.current, nodes: nEntRef.current,
        flow: fnow && lc[0] ? { time: lc[0].time, verdict: fnow.verdict, prev: fprev?.verdict ?? null, bull: fnow.bull, bear: fnow.bear, head: fnow.head } : null }), onSignal, tf); }
      catch { /* alerts must never break the chart */ }
    }
    biasMap.current = new Map(allB.map((r) => [r.time, r]));
    const bias = layers.bias ? allB.filter((r) => r.verdict !== "MIXED") : [];
    fpPrim.current.set(withConf, bias, layers.heat ? fpBars.filter((b) => b.rows.length) : []);
  }, [fpBars, analysis, tf, layers.fp, layers.bias, layers.heat, candles, ocFeed, smtArg, sig, onSignal, marketOpen, ocActs, layers.cvd, layers.cells, layers.walls, layers.bubbles, layers.gamma, layers.nodes]);

  // BANKNIFTY line (own hidden scale, so its shape lines up with NIFTY) — shown with the SMT layer
  useEffect(() => {
    bankS.current?.setData(layers.smt ? bank.map((b) => ({ time: b.time as UTCTimestamp, value: b.c })) : []);
  }, [bank, layers.smt]);

  // which line is the mouse on? (within 7 px) → name, value, meaning
  const LINE_MEAN: Record<string, string> = {
    VWAP: "average price paid today (volume-weighted). Above = buyers winning → prefer CE · below = sellers → prefer PE",
    "VWAP +1σ": "1 standard deviation above VWAP — normal upper stretch; first take-profit zone for longs",
    "VWAP −1σ": "1 standard deviation below VWAP — normal lower stretch; first take-profit zone for shorts",
    "VWAP +2σ": "very stretched above VWAP — don't chase CE here, pullback likely",
    "VWAP −2σ": "very stretched below VWAP — don't chase PE here, bounce likely",
    vPOC: "price with the MOST futures volume today — magnet; strong support / resistance",
    HVN: "high-volume price — smaller magnet / pause level", LVN: "low-volume price — price moves through it fast",
    "naked POC": "an old day's POC never revisited — price often returns to it",
    pPOC: "yesterday's point of control — reference level", "dev POC": "today's developing POC (time-based)",
    PDH: "previous day high — reaction / breakout level", PDL: "previous day low — reaction / breakdown level", PDC: "previous day close",
    EQ: "middle of the swing range — above = premium (sell zone), below = discount (buy zone)",
    BSL: "buy-side liquidity above old highs — stops sit here; expect a sweep", SSL: "sell-side liquidity below old lows — stops sit here; expect a sweep",
    AVWAP: "anchored VWAP — average price of everyone who entered since that swing; price above = those buyers in profit (support)",
    "ATM IV": "implied volatility of the ATM option — falling = premiums shrinking (bad for option buyers)",
    "D.O.L": "current draw-on-liquidity target — take profit there",
  };
  const linesAt = (t: number, y: number): { name: string; value: number; mean: string }[] => {
    const S = series.current; if (!S) return [];
    const out: { name: string; value: number; mean: string; d: number }[] = [];
    const near = (name: string, value: number, mean: string, ser = S as ISeriesApi<"Candlestick"> | ISeriesApi<"Line">) => {
      const yy = ser.priceToCoordinate(value); if (yy == null) return;
      const d = Math.abs((yy as number) - y); if (d <= 7) out.push({ name, value, mean, d });
    };
    const V = analysis?.volume;
    if (layers.volume && V) {
      const p = [...(V.vwap ?? [])].reverse().find((q) => q.time <= t);
      if (p) { const k = V.bands ?? [1, 2];
        near("VWAP", p.v, LINE_MEAN.VWAP); near("VWAP +1σ", p.v + k[0] * p.sd, LINE_MEAN["VWAP +1σ"]); near("VWAP −1σ", p.v - k[0] * p.sd, LINE_MEAN["VWAP −1σ"]);
        near("VWAP +2σ", p.v + (k[1] ?? 2) * p.sd, LINE_MEAN["VWAP +2σ"]); near("VWAP −2σ", p.v - (k[1] ?? 2) * p.sd, LINE_MEAN["VWAP −2σ"]); }
      for (const av of V.avwaps ?? []) { const q = [...av.points].reverse().find((z) => z.time <= t); if (q) near(`AVWAP · ${av.label}`, q.v, LINE_MEAN.AVWAP); }
      const iv = [...(V.iv ?? [])].reverse().find((q) => q.time <= t);
      if (iv && ivS.current) near("ATM IV", iv.iv, LINE_MEAN["ATM IV"], ivS.current);
    }
    for (const g of zones.current.segs) {
      if (g.from > t) continue;
      const key = Object.keys(LINE_MEAN).find((k) => g.label.startsWith(k) || g.label.includes(k)) ?? "";
      near(g.label, g.price, LINE_MEAN[key] ?? "");
    }
    return out.sort((a2, b2) => a2.d - b2.d).slice(0, 2);
  };

  // live reading of every volume line for the hovered candle
  const linesRead = (t: number): { txt: string; tone: "bull" | "bear" | "warn" | "flat" }[] => {
    const V = analysis?.volume, c = cMap.current.get(t);
    if (!layers.volume || !V?.available || !c) return [];
    const out: { txt: string; tone: "bull" | "bear" | "warn" | "flat" }[] = [];
    const p = [...(V.vwap ?? [])].reverse().find((q) => q.time <= t);
    if (p) {
      const z = p.sd > 0 ? (c.close - p.v) / p.sd : 0;
      out.push({ txt: `VWAP ${p.v.toFixed(0)}: close ${c.close > p.v ? "ABOVE" : "BELOW"} by ${Math.abs(c.close - p.v).toFixed(0)} pts → ${c.close > p.v ? "buyers winning, prefer CE" : "sellers winning, prefer PE"}`, tone: c.close > p.v ? "bull" : "bear" });
      out.push({ txt: `Stretch ${z >= 0 ? "+" : ""}${z.toFixed(1)}σ → ${z >= 2 ? "very stretched UP — don't chase CE, pullback likely" : z <= -2 ? "very stretched DOWN — don't chase PE, bounce likely" : z >= 1 ? "upper band — first TP zone for longs" : z <= -1 ? "lower band — first TP zone for shorts" : "normal range around VWAP"}`, tone: Math.abs(z) >= 2 ? "warn" : "flat" });
    }
    const vp = V.today?.poc;
    if (vp) { const d = c.close - vp;
      out.push({ txt: `vPOC ${vp.toFixed(0)} is ${Math.abs(d).toFixed(0)} pts ${d > 0 ? "below" : "above"} → ${Math.abs(d) <= 10 ? "price AT the magnet — expect a reaction" : d > 0 ? "support / magnet below" : "resistance / magnet above"}`, tone: "flat" });
      const hv = (V.today?.hvn ?? []).filter((h) => Math.abs(h - vp) > (V.today?.step ?? 5)).sort((x, y) => Math.abs(x - c.close) - Math.abs(y - c.close))[0];
      if (hv != null) out.push({ txt: `Nearest HVN ${hv.toFixed(0)} (${Math.abs(c.close - hv).toFixed(0)} pts ${c.close > hv ? "below" : "above"}) → pause level`, tone: "flat" });
    }
    for (const av of V.avwaps ?? []) { const q = [...av.points].reverse().find((z) => z.time <= t); if (!q) continue;
      const up = c.close > q.v, buyers = av.dir === "bull";
      out.push({ txt: `AVWAP ${av.label} ${q.v.toFixed(0)}: close ${up ? "above" : "below"} → ${buyers ? (up ? "buyers since that low in profit — up-move healthy" : "buyers since the low now losing — up-move weakening") : (up ? "sellers since that high losing — resistance broken" : "sellers since that high in profit — acts as resistance")}`,
        tone: (buyers ? up : up) ? "bull" : "bear" }); }
    const iv = (V.iv ?? []).filter((q) => q.time <= t); const ivn = iv[iv.length - 1], iv0 = iv[Math.max(0, iv.length - 7)];
    if (ivn && iv0) { const ch = ivn.iv - iv0.iv;
      out.push({ txt: `ATM IV ${ivn.iv.toFixed(1)} (${ch >= 0 ? "+" : ""}${ch.toFixed(1)} recent) → ${ch <= -0.3 ? "falling: premiums shrinking — bad for option BUYERS, need a fast move" : ch >= 0.3 ? "rising: premiums inflating — moves expected" : "flat"}`, tone: ch <= -0.3 ? "warn" : "flat" }); }
    const bars = V.bars ?? [], bi = bars.findIndex((b) => b.time === t), vb = bi >= 0 ? bars[bi] : undefined;
    if (vb) {
      const prev = bars.slice(Math.max(0, bi - 10), bi), avg = prev.length ? prev.reduce((x, b) => x + b.v, 0) / prev.length : 0;
      const rel = avg ? vb.v / avg : 1, body = Math.abs(c.close - c.open), rng = Math.max(c.high - c.low, 0.01);
      const col = vb.rvol != null && vb.rvol >= 1.5 ? "BRIGHT" : vb.rvol != null && vb.rvol <= 0.7 ? "dark / faded" : "medium";
      out.push({ txt: `Volume bar: ${(vb.v / 1000).toFixed(0)}k, ${col} (RVOL ${vb.rvol?.toFixed(1) ?? "—"}× vs normal for this time · ${rel.toFixed(1)}× the last 10 bars) → ${vb.rvol != null && vb.rvol >= 1.5 ? "institutions active — trust this candle's direction" : vb.rvol != null && vb.rvol <= 0.7 ? "thin volume — weak move, don't trust breakouts" : "normal participation"}`,
        tone: vb.rvol != null && vb.rvol >= 1.5 ? (c.close >= c.open ? "bull" : "bear") : "flat" });
      // effort vs result (Wyckoff): big volume + small body = absorption; small volume + big body = easy move / no opposition
      if (rel >= 1.8 && body / rng < 0.35) out.push({ txt: "Effort vs result: BIG volume but small body → absorption — the other side is soaking it up, turn risk", tone: "warn" });
      else if (rel <= 0.6 && body / rng > 0.6) out.push({ txt: `Effort vs result: small volume, big ${c.close >= c.open ? "green" : "red"} body → no opposition, but weak conviction — likely to be retested`, tone: "warn" });
      const last3 = bars.slice(Math.max(0, bi - 2), bi + 1);
      if (last3.length === 3 && last3.every((b) => !b.up) && last3.every((b) => (b.rvol ?? 1) <= 0.8)) out.push({ txt: "3 red bars on LOW volume → selling without participation — a pullback, not a real reversal (yet)", tone: "bull" });
      if (last3.length === 3 && last3.every((b) => b.up) && last3.every((b) => (b.rvol ?? 1) <= 0.8)) out.push({ txt: "3 green bars on LOW volume → buying without participation — rally may stall", tone: "bear" });
    }
    return out;
  };

  // PE−CE Action lane (own pane at the bottom): green = table says CE, red = PE, grey = WAIT
  useEffect(() => {
    const ch = chart.current; if (!ch) return;
    const on = !!layers.ocAct && ocActs.length > 0;
    if (on && !ocLane.current) {
      const idx = ch.panes().length;
      ocLane.current = ch.addSeries(HistogramSeries, { priceLineVisible: false, lastValueVisible: false, title: "PE−CE action", base: 0 }, idx);
      try { ch.panes()[idx]?.setStretchFactor(0.6); } catch { /* */ }
    } else if (!on && ocLane.current) { ch.removeSeries(ocLane.current); ocLane.current = null; }
    ocLane.current?.setData(ocActs.map((x) => ({ time: x.time as UTCTimestamp, value: x.v === 0 ? 0.25 : x.v,
      color: x.v > 0 ? "#22c55e" : x.v < 0 ? "#ef4444" : "rgba(100,116,139,0.45)" })));
  }, [ocActs, layers.ocAct]);

  // heatmap narrative for one candle: where the volume sat, what that means, what to do
  const heatRead = (t: number): string[] => {
    if (!layers.heat) return [];
    const b = fpBars.find((x) => x.time === t), c = cMap.current.get(t);
    if (!b || !b.rows.length || !c) return [];
    const rng = Math.max(c.high - c.low, 0.01), tot = b.rows.reduce((x, r) => x + r.v, 0) || 1;
    const third = (p: number) => ((p - c.low) / rng < 0.34 ? "low" : (p - c.low) / rng > 0.66 ? "high" : "mid");
    const share = { low: 0, mid: 0, high: 0 } as Record<string, number>;
    for (const r of b.rows) share[third(r.p)] += r.v / tot;
    const hot = [...b.rows].sort((x, y) => y.v - x.v)[0];
    const where = third(hot.p), closePos = (c.close - c.low) / rng, green = c.close >= c.open;
    const pct = (x: number) => `${Math.round(x * 100)} %`;
    const out = [`Hottest row ${hot.p.toLocaleString("en-IN")} (${pct(hot.v / tot)} of the candle's volume) — in the ${where.toUpperCase()} of the candle · split low ${pct(share.low)} / mid ${pct(share.mid)} / high ${pct(share.high)}`];
    if (where === "high" && closePos < 0.5) out.push("🔴 Heavy volume at the TOP but the close fell away → buyers got absorbed by sellers up there (distribution). PE bias; don't buy CE above this band.");
    else if (where === "low" && closePos > 0.5) out.push("🟢 Heavy volume at the BOTTOM and a strong close → sellers got absorbed by buyers (accumulation). CE bias; this band is support.");
    else if (where === "high" && closePos >= 0.5) out.push(`🟢 Volume built at the top AND it closed there → buyers ACCEPTED higher prices (initiative). Trend ${green ? "up" : ""} is healthy; next pullback to this band = CE entry area.`);
    else if (where === "low" && closePos <= 0.5) out.push("🔴 Volume built at the bottom AND it closed there → sellers accepted lower prices. Weak; this band is now resistance on a bounce.");
    else out.push(`⚪ Volume concentrated in the MIDDLE → two-sided, balanced candle (no one won). Wait for the next candle to leave this band.`);
    if (share.high + share.low > 0.75 && share.mid < 0.2) out.push("Both ends hot, empty middle → price jumped through the middle fast; expect a retest of that gap.");
    const bodyHot = b.rows.filter((r) => r.p <= Math.max(c.open, c.close) && r.p >= Math.min(c.open, c.close)).reduce((x, r) => x + r.v, 0) / tot;
    if (Math.abs(c.close - c.open) / rng > 0.6 && bodyHot < 0.35) out.push("Big body but little volume inside it → easy move with no fight, often retested.");
    if (b.delta !== 0) out.push(`Delta ${kfmt(b.delta)} → ${b.delta > 0 ? "aggressive buyers" : "aggressive sellers"} ${(b.delta > 0) === green ? "agree with the candle colour (genuine)" : "DISAGREE with the candle colour (absorption — be careful)"}.`);
    return out;
  };

  const hm = (t: number) => { const d = new Date(t * 1000); return `${String(d.getUTCHours()).padStart(2, "0")}:${String(d.getUTCMinutes()).padStart(2, "0")}`; };
  const hc = hover ? cMap.current.get(hover.t) : undefined;
  const hev = hover ? evRef.current.get(hover.t) ?? [] : [];
  const vb = hover && analysis?.volume?.bars ? analysis.volume.bars.find((b) => b.time === hover.t) : undefined;
  const ofOn = !!(layers.cvd || layers.cells || layers.walls || layers.bubbles);
  const ofArgs = { fp: fpBars, bubbles, walls, zones: ofPrim.current.zones, atr: analysis?.atr ?? 20, candles };
  ofArgsRef.current = ofArgs;
  const ofr = hover && ofOn ? readAt(hover.t, { ...ofArgs, px: series.current?.coordinateToPrice(hover.y) ?? null }) : null;
  const lastClosed = ofOn ? [...candles].reverse().find((c) => c.closed !== false) : undefined;
  const ofNow = lastClosed ? readAt(lastClosed.time, ofArgs) : null;
  const VC: Record<string, string> = { BUY: "bg-bull-500 text-ink-900", SELL: "bg-bear-500 text-white", WAIT: "bg-slate-600 text-white" };
  const TC: Record<string, string> = { bull: "text-bull-400", bear: "text-bear-400", warn: "text-amber-300", info: "text-slate-300" };
  const gIdx = hover && layers.gamma && emaArr ? candles.findIndex((c) => c.time === hover.t) : -1;
  const gconf = gIdx >= 60 && emaArr ? confirm3(gIdx, candles, emaArr[0], emaArr[1], emaArr[2], rvMap, gexMap, analysis?.atr ?? 20) : null;
  const gLast = layers.gamma && emaArr ? (() => { const i = candles.map((c) => c.closed !== false).lastIndexOf(true);
    return i >= 60 ? confirm3(i, candles, emaArr[0], emaArr[1], emaArr[2], rvMap, gexMap, analysis?.atr ?? 20) : null; })() : null;
  const nread = hover && layers.nodes ? nodesRead(hover.t, series.current?.coordinateToPrice(hover.y) ?? null, nodesV, nEntries, candles[candles.length - 1]?.close ?? null) : [];
  const ob = hover && layers.oi ? (analysis?.volume?.oi_line ?? []).find((x) => x.time === hover.t) : undefined;
  const OI_READ: Record<string, [string, string, string]> = {
    "long build-up": ["🟢 LONG BUILD-UP", "price ▲ + OI ▲ — fresh buyers opening longs. Real buying → CE bias, trust the move.", "text-bull-400"],
    "short covering": ["🟢 SHORT COVERING", "price ▲ + OI ▼ — old sellers exiting. Rally is weaker (fuel runs out) → CE only short-term, book fast.", "text-green-300"],
    "short build-up": ["🔴 SHORT BUILD-UP", "price ▼ + OI ▲ — fresh sellers opening shorts. Real selling → PE bias.", "text-bear-400"],
    "long unwinding": ["🔴 LONG UNWINDING", "price ▼ + OI ▼ — old buyers exiting. Fall is weaker → PE only short-term, expect a bounce.", "text-red-300"],
  };
  const lns = hover ? linesAt(hover.t, hover.y) : [];
  const lread = hover ? linesRead(hover.t) : [];
  const hread = hover ? heatRead(hover.t) : [];
  const br = hover && layers.bias ? biasMap.current.get(hover.t) : undefined;
  const fb = hover && layers.fp ? fpPrim.current.bars.find((b) => b.time === hover.t) : undefined;
  const W = box.current?.clientWidth ?? 900;
  const legend = LEGEND.filter(([k]) => layers[k]);
  return (
    <div className="relative h-full w-full">
      <div ref={box} className="h-full w-full" />
      {/* legend + latest signals (top-left) */}
      <div className="pointer-events-none absolute left-2 top-2 z-10 flex max-w-[70%] flex-col gap-1">
        <div className="flex flex-wrap gap-1">
          {legend.map(([k, label, color]) => (
            <span key={k} className="rounded bg-[#060b16]/80 px-1.5 py-0.5 text-[10px] text-slate-300 ring-1 ring-white/5">
              <span className="mr-1 inline-block h-2 w-2 rounded-sm align-middle" style={{ background: color }} />{label}
            </span>
          ))}
        </div>
        {(() => { const act = (Object.keys(GUIDE) as (keyof Layers)[]).filter((k) => layers[k]); if (!act.length) return null;
          const hm2 = (t: number) => { const d = new Date(t * 1000); return `${String(d.getUTCHours()).padStart(2, "0")}:${String(d.getUTCMinutes()).padStart(2, "0")}`; };
          const nowOf = (k: string): string => {
            const a = analysis;
            if (k === "range" && a?.range) { const r = a.range;
              return `price in ${r.zone.toUpperCase()} (${Math.round(r.pos_pct)} %) · EQ ${r.eq.toFixed(0)} · OTE ${r.ote_band[0].toFixed(0)}–${r.ote_band[1].toFixed(0)} → ${/prem/i.test(r.zone) ? "don't buy CE here; PE / wait" : "discount: CE on a bullish trigger"}`; }
            if (k === "volume") { const v = a?.volume?.vwap?.slice(-1)[0]; const c = candles[candles.length - 1];
              return v && c ? `close ${c.close > v.v ? "ABOVE" : "BELOW"} VWAP ${v.v.toFixed(0)} → ${c.close > v.v ? "prefer CE" : "prefer PE"}` : ""; }
            if (k === "dol") { const d = [...dolRef.current].reverse().find((x) => !x.hit); return d ? `${d.label} (after ${d.why})` : "no active target"; }
            if (k === "bias") { const b = [...biasMap.current.values()].filter((r) => !r.forming).slice(-1)[0];
              return b ? `${hm2(b.time)}: ${b.verdict === "MIXED" ? "mixed" : b.verdict} ${b.bull}▲ ${b.bear}▼ of ${b.avail}${b.warn ? " · " + b.warn : ""}` : ""; }
            if ((k === "delta" || k === "deltaDetail") && a?.delta && !a.delta.available) return `⚠ no delta data — ${a.delta.message || "set Delta source = Estimated (FUT quote rule)"}`;
            if (k === "fp") { const f = fpPrim.current.bars.slice(-1)[0]; return f ? `${f.at}: Δ ${kfmt(f.delta)} · ${f.read}` : ""; }
            const ev = (EV_LAYER[k] ?? []).map((n) => layerNow[n]).filter(Boolean).sort((x, y) => y.time - x.time)[0];
            return ev ? `${hm2(ev.time)} ${ev.short}${KIND_ACT[ev.kind] ? ` → ${KIND_ACT[ev.kind]}` : ""}` : "";
          };
          return <div className="pointer-events-auto max-h-[46vh] w-[460px] max-w-full overflow-auto rounded bg-[#060b16]/90 px-2 py-1 text-[10.5px] ring-1 ring-gold-500/30">
            <button className="font-semibold text-gold-400" onClick={() => { const v = !guideOpen; setGuideOpen(v); try { localStorage.setItem("ict_guide", v ? "1" : "0"); } catch { /* */ } }}>
              {guideOpen ? "▾" : "▸"} ⓘ What am I looking at? ({act.length} layer{act.length > 1 ? "s" : ""})</button>
            {guideOpen && act.map((k) => { const g = GUIDE[k]; const now = nowOf(k); return (
              <div key={k} className="mt-1 border-t border-ink-700 pt-1">
                <b className="text-slate-100">{g.name}</b> <span className="text-slate-400">— {g.what}</span>
                {now && <div className="text-cyan-200">▸ now: {now}</div>}
                <div className="text-amber-200">▸ do: {g.act}</div>
              </div>); })}
          </div>; })()}
        {layers.heat && <div className="pointer-events-auto w-[520px] max-w-full rounded bg-[#060b16]/92 px-2.5 py-1.5 text-[13px] leading-snug ring-1 ring-orange-400/40">
          <button className="font-semibold text-orange-300" onClick={() => { const v = !heatHelp; setHeatHelp(v); try { localStorage.setItem("ict_heat_help", v ? "1" : "0"); } catch { /* */ } }}>
            {heatHelp ? "▾" : "▸"} 🔥 How to read the heatmap</button>
          {heatHelp && <div className="mt-1 space-y-1 text-slate-200">
            <div><b>Where the volume sat:</b> the hottest price row, and how the volume splits across the low, middle and high of the candle. Hover a candle to read it.</div>
            <div className="text-bear-400">🔴 <b>Hot at the top, weak close</b> — sellers absorbed the buyers (distribution). PE bias.</div>
            <div className="text-bull-400">🟢 <b>Hot at the bottom, strong close</b> — buyers absorbed the sellers (accumulation). CE bias; that band is support.</div>
            <div className="text-bull-400">🟢 <b>Hot at the top and closed there</b> — buyers accepted higher prices. Healthy trend; the next pullback to that band is the CE area.</div>
            <div className="text-bear-400">🔴 <b>Hot at the bottom and closed there</b> — sellers accepted lower prices. That band becomes resistance.</div>
            <div className="text-slate-300">⚪ <b>Hot in the middle</b> — balanced, no winner. Wait for the next candle.</div>
            <div className="text-amber-300">⚠ <b>Extra warnings:</b> both ends hot with an empty middle (a gap that will get retested) · a big body with little volume inside (easy move, often retested) · delta disagreeing with the candle colour (absorption).</div>
          </div>}
        </div>}
        {layers.fp && !fpMsg && <div className="pointer-events-auto rounded bg-[#060b16]/85 px-2 py-1 text-[10px] ring-1 ring-white/5">
          <span className="mr-1 font-semibold text-amber-300">Footprint key</span>
          {([["A", "ABSORB", "big orders soak, price stuck"], ["D", "DISTRIB", "sellers unload at top"], ["C", "ACCUM", "buyers load at bottom"],
            ["M", "TRAP", "wick sweep, closed back"], ["S", "STACK", "one-sided push"], ["I", "PUSH", "aggressive side drives"]] as const).map(([l, w, m]) => (
            <span key={l} className="mr-2" title={m}><b className="rounded bg-teal-500/80 px-1 text-[#0b1220]">{l}</b> {w}</span>))}
          <span className="text-slate-500">teal = bull · pink = bear · gold tick = POC · <b className="text-slate-300">BUY/SELL ✓3</b> = footprint + delta + structure agree</span>
        </div>}
        {layers.bias && ocFeed?.gex?.ok && (() => { const g = ocFeed.gex!; const sp = g.spot ?? 0;
          const pinNear = g.pin != null && Math.abs(sp - g.pin) <= sp * 0.0025;
          const txt = pinNear ? `📌 PIN zone ${g.pin!.toLocaleString("en-IN")} — biggest gamma, spot ${Math.abs(sp - g.pin!).toFixed(0)} pts away: expect a range, fade the edges, don't chase breakouts`
            : g.regime === "negative" ? `⚡ TREND zone (negative gamma${g.flip ? `, flip ${g.flip.toLocaleString("en-IN")}` : ""}) — moves can run, breakout badges more reliable`
            : `〰 Range-prone (positive gamma${g.flip ? `, flip ${g.flip.toLocaleString("en-IN")}` : ""}) — moves get absorbed; magnet ${g.pin?.toLocaleString("en-IN")}`;
          return <div className="pointer-events-auto rounded bg-[#060b16]/85 px-2 py-0.5 text-[10.5px] text-violet-200 ring-1 ring-violet-400/30"
            title="GEX from 8501 Options Lab (latest snapshot). Context, not a vote. Dealer-sign convention is the US one — treat as a hint.">{txt}</div>; })()}
        {fpMsg && <div className="rounded bg-[#060b16]/85 px-2 py-0.5 text-[10px] text-amber-300">{fpMsg}</div>}
        {latest.length > 0 && (
          <div className="rounded bg-[#060b16]/85 px-2 py-1 text-[11px] ring-1 ring-white/5">
            <span className="mr-1 font-semibold text-gold-400">Latest</span>
            {latest.map((e, i) => (
              <span key={i} className="mr-2" style={{ color: e.color }}>
                <span className="text-slate-500">{hm(e.time)}</span> {e.short}
              </span>
            ))}
          </div>
        )}
      </div>
      {/* Multi-timeframe panel (bottom-left) */}
      <div className="pointer-events-none absolute bottom-9 left-2 z-10 flex">
        <MtfPanel />
      </div>
      {ofNow && <div className="pointer-events-none absolute left-1/2 top-2 z-10 -translate-x-1/2">
        <div className={`flex max-w-[560px] items-center gap-2 rounded-lg px-3 py-1.5 shadow-xl ring-2 ${ofNow.verdict === "BUY" ? "bg-bull-900/90 ring-bull-400" : ofNow.verdict === "SELL" ? "bg-bear-900/90 ring-bear-400" : "bg-ink-850/90 ring-slate-500"}`}>
          <span className={`rounded px-2 py-0.5 text-[16px] font-extrabold ${VC[ofNow.verdict]}`}>{ofNow.verdict}</span>
          <span className="text-[12.5px] leading-tight text-slate-100"><b className="text-cyan-200">ORDER FLOW · last closed {hm(lastClosed!.time)}</b><br />{ofNow.head}</span>
        </div>
      </div>}
      {layers.gamma && <div className="pointer-events-none absolute left-1/2 top-[64px] z-10 -translate-x-1/2">
        <div className={`max-w-[600px] rounded-lg px-3 py-1 text-[12.5px] shadow-xl ring-2 ${gLast?.ok ? "bg-violet-900/90 ring-violet-400 text-white" : "bg-ink-850/90 ring-slate-600 text-slate-200"}`}>
          <b className="text-violet-300">γ MAP</b>{gexMap ? <> · <b className={gexMap.regime === "positive" ? "text-bull-400" : "text-amber-300"}>{gexMap.regime === "positive" ? "+γ PIN day" : "−γ TREND day"}</b>
            {gexMap.flip ? ` · flip ${gexMap.flip}` : ""}</> : " · loading option chain…"}
          {gLast && <> · last closed: <b>{gLast.ok ? `✓✓✓ ${gLast.side}` : `WALK AWAY (${gLast.checks.filter((x) => !x.pass).map((x) => x.name).join(", ")})`}</b></>}
        </div>
      </div>}
      {/* chart tools (top-right, left of the price axis) */}
      <div className="absolute right-[86px] top-2 z-10 flex gap-1">
        {(layers.fp || layers.heat || layers.bias) && <>
          <button className="tab !py-0.5 !text-[11px]" title="Smaller footprint / heatmap / bias labels"
            onClick={() => { setFpScale(FP_SCALE - 0.25); fpPrim.current.refresh(); setFpTick((n) => n + 1); }}>A−</button>
          <span className="tab !py-0.5 !text-[11px] text-slate-400">{Math.round(FP_SCALE * 100)}%</span>
          <button className="tab !py-0.5 !text-[11px]" title="Bigger footprint / heatmap / bias labels"
            onClick={() => { setFpScale(FP_SCALE + 0.25); fpPrim.current.refresh(); setFpTick((n) => n + 1); }}>A+</button>
        </>}
        <button className="tab !py-0.5 !text-[11px]" title="Fit all candles + auto price scale"
          onClick={() => { chart.current?.timeScale().fitContent(); series.current?.priceScale().setAutoScale(true); }}>Fit</button>
        <button className="tab !py-0.5 !text-[11px]" title="Back to automatic price scale (or double-click the price axis)"
          onClick={() => series.current?.priceScale().setAutoScale(true)}>Auto ↕</button>
        {onExpand && <button className="tab !py-0.5 !text-[11px]" title="Full screen (Esc to exit)" onClick={onExpand}>{expanded ? "▣ Normal" : "⛶ Full"}</button>}
      </div>
      {/* hover card */}
      {hover && hc && (
        <div className="pointer-events-none absolute z-20 w-[420px] rounded-lg border border-ink-700 bg-[#060b16]/95 p-2.5 text-[13px] leading-snug shadow-xl"
          style={{ left: hover.x > W - 450 ? hover.x - 430 : hover.x + 16, top: Math.max(8, Math.min(hover.y - 20, (box.current?.clientHeight ?? 400) - ((layers.bias ? 300 : 0) + (layers.volume ? 160 : 0) + (layers.heat ? 140 : 0) + 160))) }}>
          {lns.length > 0 && <div className="mb-1 rounded bg-violet-900/40 px-1.5 py-1">
            {lns.map((l, i) => <div key={i}><b className="text-violet-200">〰 {l.name}</b> <span className="num text-slate-200">{l.name === "ATM IV" ? l.value.toFixed(1) : l.value.toLocaleString("en-IN", { maximumFractionDigits: 1 })}</span>
              {l.mean && <div className="text-slate-300">{l.mean}</div>}</div>)}
          </div>}
          <div className="mb-1 flex items-baseline justify-between">
            <b className="text-slate-200">{hm(hc.time)}</b>
            <span className={`num ${hc.close >= hc.open ? "text-bull-400" : "text-bear-400"}`}>
              {(hc.close - hc.open >= 0 ? "+" : "")}{(hc.close - hc.open).toFixed(1)} pts
            </span>
          </div>
          <div className="num grid grid-cols-4 gap-x-1 text-slate-400">
            <span>O <b className="text-slate-200">{hc.open.toFixed(1)}</b></span><span>H <b className="text-slate-200">{hc.high.toFixed(1)}</b></span>
            <span>L <b className="text-slate-200">{hc.low.toFixed(1)}</b></span><span>C <b className="text-slate-200">{hc.close.toFixed(1)}</b></span>
          </div>
          {gconf && <div className={`mt-1.5 rounded-md border p-1.5 ${gconf.ok ? "border-violet-400 bg-violet-950/40" : "border-slate-600 bg-ink-850/60"}`}>
            <div className={`text-[13.5px] font-bold ${gconf.ok ? "text-violet-200" : "text-slate-300"}`}>γ {gconf.head}</div>
            {gconf.checks.map((x, i) => <div key={i} className={`text-[13px] ${x.pass ? "text-bull-400" : "text-bear-400"}`}>{x.pass ? "✓" : "✗"} <b>{x.name}</b> — {x.why}</div>)}
            {gconf.plan && <div className="text-[13px] font-semibold text-cyan-200">🎯 {gconf.plan} — paper first.</div>}
            <div className="text-[12px] text-amber-200/80">✓✓✓ badges are recorded live only — this hover re-checks with the CURRENT chain, so on past candles treat it as study, not as a signal you had.</div>
            {gexMap && <div className="text-[12px] text-slate-400">Regime: {gexMap.regime === "positive" ? "+γ (pinning — price drawn to the big levels, fades work)" : "−γ (dealers chase — moves accelerate through levels)"}{gexMap.flip ? ` · flip ${gexMap.flip}` : ""}</div>}
          </div>}
          {nread.length > 0 && <div className="mt-1.5 rounded-md border border-yellow-400/40 bg-yellow-950/20 p-1.5">
            <div className="text-[13.5px] font-semibold text-yellow-300">🟨 Stacked volume nodes (Day · Week · Month)</div>
            {nread.map((l, i) => <div key={i} className={`text-[13px] ${l.startsWith("●") ? "font-semibold text-bull-400" : "text-slate-100"}`}>• {l}</div>)}
          </div>}
          {ofr && <div className="mt-1.5 rounded-md border border-cyan-500/40 bg-cyan-950/30 p-1.5">
            {ofr.wall && <div className="mb-1 rounded bg-ink-800 px-1.5 py-1 text-[13px] text-slate-100">{ofr.wall}</div>}
            <div className="flex items-center gap-2">
              <span className={`rounded px-2 py-0.5 text-[14px] font-extrabold ${VC[ofr.verdict]}`}>{ofr.verdict}</span>
              <span className="text-[12px] text-slate-300">🧭 Order flow · <b className="text-bull-400">{ofr.bull}▲</b> <b className="text-bear-400">{ofr.bear}▼</b></span>
            </div>
            <div className="mt-0.5 text-[13.5px] font-semibold text-slate-100">{ofr.head}</div>
            {ofr.lines.map((l, i) => <div key={i} className={`text-[13px] ${TC[l.tone]}`}>• {l.txt}</div>)}
            {ofr.plan && <div className="mt-0.5 text-[13px] font-semibold text-cyan-200">🎯 {ofr.plan} — paper first, 1 lot max.</div>}
          </div>}
          {vb && <div className="num mt-0.5 text-slate-400">FUT vol <b className="text-slate-200">{(vb.v / 1000).toFixed(0)}k</b>
            {vb.rvol != null && <> · RVOL <b className={vb.rvol >= 1.5 ? "text-bull-400" : vb.rvol <= 0.7 ? "text-slate-500" : "text-slate-200"}>{vb.rvol.toFixed(1)}×</b></>}</div>}
          {ob && <div className="mt-1.5 border-t border-ink-700 pt-1.5">
            <div className="flex items-baseline justify-between"><span className="font-semibold text-slate-200">📊 FUT OI bar</span>
              <span className="num text-slate-300">ΔOI <b className={(ob.doi ?? 0) >= 0 ? "text-bull-400" : "text-bear-400"}>{(ob.doi ?? 0) >= 0 ? "+" : ""}{(ob.doi ?? 0).toLocaleString("en-IN")}</b> · OI {(ob.oi / 1e5).toFixed(1)}L</span></div>
            {ob.build && OI_READ[ob.build] ? <div className={OI_READ[ob.build][2]}><b>{OI_READ[ob.build][0]}</b> — {OI_READ[ob.build][1]}</div>
              : <div className="text-slate-400">⚪ grey — OI barely changed; no new positions, ignore.</div>}
            {ob.surge && <div className="font-semibold text-amber-300">🟧 OI SURGE {ob.surge_x ?? ""}× normal — a big player just {(ob.doi ?? 0) >= 0 ? "OPENED" : "CLOSED"} positions. Watch the next 1–2 candles: the direction they break is the side that player took.</div>}
          </div>}
          {hread.length > 0 && <div className="mt-1.5 border-t border-ink-700 pt-1.5">
            <div className="text-[14px] font-semibold text-orange-300">🔥 Heatmap read ({tf})</div>
            {hread.map((l, i) => <div key={i} className={`text-[14px] ${i === 0 ? "text-slate-300" : "text-slate-100"}`}>{i === 0 ? "" : "• "}{l}</div>)}
          </div>}
          {lread.length > 0 && <div className="mt-1.5 border-t border-ink-700 pt-1.5">
            <div className="font-semibold text-slate-200">📏 Lines right now</div>
            {lread.map((l, i) => <div key={i} className={l.tone === "bull" ? "text-bull-400" : l.tone === "bear" ? "text-bear-400" : l.tone === "warn" ? "text-amber-300" : "text-slate-300"}>• {l.txt}</div>)}
          </div>}
          {br && <div className="mt-1.5 border-t border-ink-700 pt-1.5">
            <div className="mb-0.5 flex items-baseline justify-between">
              <span className="font-semibold text-gold-400">⚖ Bias</span>
              <span className={`font-bold ${br.verdict === "BUY" ? "text-bull-400" : br.verdict === "SELL" ? "text-bear-400" : "text-slate-400"}`}>
                {br.verdict === "MIXED" ? "mixed" : br.verdict} · <span className="text-bull-400">{br.bull}▲</span> <span className="text-bear-400">{br.bear}▼</span> of {br.avail}</span>
            </div>
            {br.warn && <div className="mb-0.5 font-semibold text-amber-300">{br.warn}</div>}
            {FACTORS.map((f) => { const v = br.votes[f]; return (
              <div key={f} className="flex gap-1 leading-tight">
                <span className={`w-3 shrink-0 font-bold ${v === "bull" ? "text-bull-400" : v === "bear" ? "text-bear-400" : "text-slate-600"}`}>{v === "bull" ? "▲" : v === "bear" ? "▼" : "·"}</span>
                <span className={`w-[100px] shrink-0 ${v ? "text-slate-200" : "text-slate-500"}`}>{f}</span>
                <span className="text-slate-400">{br.why[f] || "no vote"}</span>
              </div>); })}
          </div>}
          {fb && fb.rows.length > 0 && <div className="mt-1.5 border-t border-ink-700 pt-1.5">
            <div className="num"><span className="font-semibold text-amber-300">Footprint (est.)</span>{" "}
              <b className={fb.delta >= 0 ? "text-bull-400" : "text-bear-400"}>Δ {kfmt(fb.delta)}</b>
              {fb.delta_pct != null && <span className="text-slate-500"> ({fb.delta_pct > 0 ? "+" : ""}{fb.delta_pct}%)</span>}
              {fb.poc != null && <span className="text-gold-400"> · POC {fb.poc}</span>}</div>
            {fb.tags.map((t, i) => <div key={i} className={t.dir === "bear" ? "text-rose-300" : "text-teal-300"}>
              <b>{FP_WORD[t.k] ?? t.k.toLowerCase().replace("_", " ")}</b> <span className="text-slate-400">{(FP_MEANING[t.k] ?? "").split(" — ")[1] ?? ""}</span></div>)}
            <div className="num mt-0.5 grid grid-cols-[52px_1fr] gap-x-1 text-[10px]">
              {[...fb.rows].sort((r1, r2) => r2.p - r1.p).slice(0, 10).map((r) => [
                <span key={r.p + "p"} className={`text-right ${r.poc ? "text-gold-400 font-bold" : "text-slate-500"}`}>{r.p}</span>,
                <span key={r.p + "v"}><span className={r.si ? "font-bold text-bear-400" : "text-rose-200"}>{(r.sell / 1000).toFixed(1)}K</span>
                  <span className="text-slate-600"> × </span><span className={r.bi ? "font-bold text-bull-400" : "text-emerald-200"}>{(r.buy / 1000).toFixed(1)}K</span></span>])}
            </div>
            <div className="text-slate-400">{fb.read}</div>
            {fb.conf && <div className={`mt-1 rounded px-1.5 py-0.5 font-bold ${fb.conf.dir === "bull" ? "bg-bull-900/70 text-bull-400" : "bg-bear-900/70 text-bear-400"}`}>
              {fb.conf.dir === "bull" ? "BUY ✓3 (CE bias)" : "SELL ✓3 (PE bias)"}: {fb.conf.why.join(" + ")}
              <div className="font-normal text-slate-400">3 agree — still needs your SL. Info only, not an order.</div></div>}
          </div>}
          {hev.length > 0 && <div className="mt-1.5 space-y-1 border-t border-ink-700 pt-1.5">
            {hev.sort((a2, b2) => b2.pri - a2.pri).slice(0, 6).map((e, i) => (
              <div key={i}>
                <span className="font-semibold" style={{ color: e.color }}>{e.layer} · {e.full}</span>
                {MEANING[e.kind] && <div className="text-slate-400">{MEANING[e.kind]}</div>}
              </div>
            ))}
          </div>}
        </div>
      )}
    </div>
  );
}

const LEGEND: [keyof Layers, string, string][] = [
  ["ob", "Order block · Breaker · RJB (violet)", "rgba(192,132,252,0.8)"], ["fvg", "FVG · IFVG (orange sell / teal buy)", "rgba(251,146,60,0.85)"], ["liq", "Liquidity BSL/SSL", "#f59e0b"],
  ["structure", "BOS ▲▼ · MSS (big) · bos (weak, faint)", C.bull], ["range", "OTE / EQ", C.gold], ["session", "PDH · PDL · OR", "#38bdf8"],
  ["delta", "ABS ● · Δ± ■", "#e8c35a"], ["crt", "CRT box · TBS ■ · CRT▶", C.gold], ["auction", "Value · ACC / REJ", "#38bdf8"],
  ["volume", "VWAP · vPOC · vol", "#f5f5f4"], ["oi", "FUT OI build-up bars", "#22c55e"], ["rvol", "cRVOL", "#22c55e"], ["qml", "QML · RBS/SBR · cyan = close line", "#22d3ee"],
  ["fp", "Footprint (est.): Δ under candle · gold = POC · A/D/C/M/S/I", "#facc15"],
  ["dol", "D.O.L: ERL taken → FVG target · FVG tapped → old high/low", "#a78bfa"], ["smt", "SMT ▲▼ · pink line = BANKNIFTY (own scale)", "#f472b6"],
  ["heat", "Heatmap: traded volume per price (blue cold → red hot)", "#f97316"], ["ocAct", "PE−CE action: ▲ OC CE / ▼ OC PE · bottom lane", "#4ade80"],
  ["bias", "⚖ Bias 14: BUY/SELL n/N = ≥70 % of available factors agree", "#e8c35a"],
  ["walls", "🧱 OI walls: red CE resistance · green PE support", "#ef4444"], ["bubbles", "🫧 Big trades ≥2× vol · yellow ring = absorbed", "#a3e635"],
  ["cells", "▦ FP cells: bid | ask · bright = imbalance", "#f472b6"], ["cvd", "〽 CVD pane (est.)", "#4ade80"], ["gamma", "γ map: red call res · green put sup · yellow flip · ✓✓✓ = EMA stack + flag at level + volume", "#c084fc"], ["nodes", "🟨 Stacked nodes D·W·M (gold = A+) · ● ENTRY = ±2σ into zone", "#facc15"],
];

function dedupe(pts: { time: number; price: number }[]) {
  const out: { time: number; price: number }[] = [];
  for (const p of pts) {
    if (out.length && out[out.length - 1].time >= p.time) continue;     // line series needs strictly increasing time
    out.push(p);
  }
  return out;
}
