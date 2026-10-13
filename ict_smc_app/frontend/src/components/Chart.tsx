// Chart.tsx — candlesticks + ICT/SMC overlays (Lightweight Charts v5).
import { useEffect, useRef, useState } from "react";
import {
  CandlestickSeries, ColorType, createChart, createSeriesMarkers, CrosshairMode, HistogramSeries, LineSeries, LineStyle,
  type IChartApi, type ISeriesApi, type ISeriesMarkersPluginApi, type MouseEventParams, type Time, type UTCTimestamp,
} from "lightweight-charts";
import type { Analysis, Candle, TF } from "../api";
import { bucket } from "../api";
import { ZonesPrimitive, type Box, type Seg } from "./zones";
import { BAR_SEC, index, MEANING, shortText, toMarkers, type Ev } from "./chartKit";

export type Layers = { ob: boolean; fvg: boolean; liq: boolean; structure: boolean; swings: boolean; range: boolean; session: boolean; delta: boolean; deltaDetail: boolean; crt: boolean; auction: boolean; volume: boolean; oi: boolean; rvol: boolean };

const C = {
  bull: "#10b981", bear: "#ef4444", gold: "#e8c35a", grid: "#131d33", text: "#94a3b8",
  obBull: "rgba(16,185,129,0.14)", obBear: "rgba(239,68,68,0.14)", fvgBull: "rgba(56,189,248,0.14)", fvgBear: "rgba(244,114,182,0.14)",
};

export default function Chart({ tf, candles, analysis, layers, spot, spotTs, marketOpen, onExpand, expanded }: {
  tf: TF; candles: Candle[]; analysis: Analysis | null; layers: Layers; spot: number | null; spotTs: number; marketOpen: boolean;
  onExpand?: () => void; expanded?: boolean;
}) {
  const box = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const series = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const zz = useRef<ISeriesApi<"Line"> | null>(null);
  const markers = useRef<ISeriesMarkersPluginApi<Time> | null>(null);
  const zones = useRef(new ZonesPrimitive());
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
    const dayStarts: number[] = [];
    if (tf !== "1D") for (let i = 1; i < candles.length; i++) if (Math.floor(candles[i].time / 86400) !== Math.floor(candles[i - 1].time / 86400)) dayStarts.push(candles[i].time);
    zones.current.set(boxes, segs, lastTime, dayStarts);

    // ── markers: every layer emits Ev records → merged per bar/side, labelled only where it matters ──
    const evs: Ev[] = [];
    const push = (e: Ev) => evs.push(e);
    const side = (bull: boolean): "above" | "below" => (bull ? "below" : "above");
    if (a && (layers.structure || layers.delta)) {
      for (const m of a.markers) {
        const bull = m.dir === "bull";
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
    if (a && layers.rvol) {
      const RC: Record<string, string> = { RVOL_ACTIVATED: "#22c55e", BREAKOUT_WITH_RVOL: "#22c55e", RVOL_SPIKE: "#e8c35a", RVOL_FADING: "#94a3b8", BREAKOUT_NO_RVOL: "#ef4444" };
      const RT: Record<string, string> = { RVOL_ACTIVATED: "RVOL▲", BREAKOUT_WITH_RVOL: "BO✓vol", RVOL_SPIKE: "RVOL⚡", RVOL_FADING: "RVOL▼", BREAKOUT_NO_RVOL: "BO✗vol" };
      for (const e of a.volume?.rvol_events ?? []) {
        push({ time: bucket(e.time, tf), side: "above", dir: "flat", kind: e.kind, layer: "RVOL",
          short: `${RT[e.kind] ?? e.kind} ${e.value.toFixed(1)}×`, full: `${RT[e.kind] ?? e.kind} ${e.value.toFixed(2)}× — ${e.title ?? ""}`,
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
    zz.current.setData(a && layers.swings ? dedupe(a.zigzag).map((p) => ({ time: p.time as UTCTimestamp, value: p.price })) : []);
  }, [analysis, layers, candles]);

  const hm = (t: number) => { const d = new Date(t * 1000); return `${String(d.getUTCHours()).padStart(2, "0")}:${String(d.getUTCMinutes()).padStart(2, "0")}`; };
  const hc = hover ? cMap.current.get(hover.t) : undefined;
  const hev = hover ? evRef.current.get(hover.t) ?? [] : [];
  const vb = hover && analysis?.volume?.bars ? analysis.volume.bars.find((b) => b.time === hover.t) : undefined;
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
      {/* chart tools (top-right, left of the price axis) */}
      <div className="absolute right-[86px] top-2 z-10 flex gap-1">
        <button className="tab !py-0.5 !text-[11px]" title="Fit all candles + auto price scale"
          onClick={() => { chart.current?.timeScale().fitContent(); series.current?.priceScale().setAutoScale(true); }}>Fit</button>
        <button className="tab !py-0.5 !text-[11px]" title="Back to automatic price scale (or double-click the price axis)"
          onClick={() => series.current?.priceScale().setAutoScale(true)}>Auto ↕</button>
        {onExpand && <button className="tab !py-0.5 !text-[11px]" title="Full screen (Esc to exit)" onClick={onExpand}>{expanded ? "▣ Normal" : "⛶ Full"}</button>}
      </div>
      {/* hover card */}
      {hover && hc && (
        <div className="pointer-events-none absolute z-20 w-[290px] rounded-lg border border-ink-700 bg-[#060b16]/95 p-2 text-[11px] shadow-xl"
          style={{ left: hover.x > W - 320 ? hover.x - 300 : hover.x + 16, top: Math.max(8, Math.min(hover.y - 20, (box.current?.clientHeight ?? 400) - 220)) }}>
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
          {vb && <div className="num mt-0.5 text-slate-400">FUT vol <b className="text-slate-200">{(vb.v / 1000).toFixed(0)}k</b>
            {vb.rvol != null && <> · RVOL <b className={vb.rvol >= 1.5 ? "text-bull-400" : vb.rvol <= 0.7 ? "text-slate-500" : "text-slate-200"}>{vb.rvol.toFixed(1)}×</b></>}</div>}
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
  ["ob", "Order block", "rgba(16,185,129,0.6)"], ["fvg", "FVG", "rgba(56,189,248,0.7)"], ["liq", "Liquidity BSL/SSL", "#f59e0b"],
  ["structure", "BOS ▲▼ · MSS (big) · bos (weak, faint)", C.bull], ["range", "OTE / EQ", C.gold], ["session", "PDH · PDL · OR", "#38bdf8"],
  ["delta", "ABS ● · Δ± ■", "#e8c35a"], ["crt", "CRT box · TBS ■ · CRT▶", C.gold], ["auction", "Value · ACC / REJ", "#38bdf8"],
  ["volume", "VWAP · vPOC · vol", "#f5f5f4"], ["oi", "FUT OI build-up bars", "#22c55e"], ["rvol", "cRVOL", "#22c55e"],
];

function dedupe(pts: { time: number; price: number }[]) {
  const out: { time: number; price: number }[] = [];
  for (const p of pts) {
    if (out.length && out[out.length - 1].time >= p.time) continue;     // line series needs strictly increasing time
    out.push(p);
  }
  return out;
}
