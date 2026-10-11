// Chart.tsx — candlesticks + ICT/SMC overlays (Lightweight Charts v5).
import { useEffect, useRef } from "react";
import {
  CandlestickSeries, ColorType, createChart, createSeriesMarkers, CrosshairMode, HistogramSeries, LineSeries, LineStyle,
  type IChartApi, type ISeriesApi, type ISeriesMarkersPluginApi, type SeriesMarker, type Time, type UTCTimestamp,
} from "lightweight-charts";
import type { Analysis, Candle, TF } from "../api";
import { bucket } from "../api";
import { ZonesPrimitive, type Box, type Seg } from "./zones";

export type Layers = { ob: boolean; fvg: boolean; liq: boolean; structure: boolean; swings: boolean; range: boolean; session: boolean; delta: boolean; deltaDetail: boolean; crt: boolean; auction: boolean; volume: boolean; oi: boolean };

const C = {
  bull: "#10b981", bear: "#ef4444", gold: "#e8c35a", grid: "#131d33", text: "#94a3b8",
  obBull: "rgba(16,185,129,0.14)", obBear: "rgba(239,68,68,0.14)", fvgBull: "rgba(56,189,248,0.14)", fvgBear: "rgba(244,114,182,0.14)",
};

export default function Chart({ tf, candles, analysis, layers, spot, spotTs, marketOpen }: {
  tf: TF; candles: Candle[]; analysis: Analysis | null; layers: Layers; spot: number | null; spotTs: number; marketOpen: boolean;
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
  const oiS = useRef<{ oi: ISeriesApi<"Line">; ema: ISeriesApi<"Line">; doi: ISeriesApi<"Histogram"> } | null>(null);          // [vwap, +1σ, −1σ, +2σ, −2σ, avwap1, avwap2]
  const last = useRef<Candle | null>(null);

  // create once
  useEffect(() => {
    if (!box.current) return;
    const ch = createChart(box.current, {
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: "#0b1220" }, textColor: C.text, fontFamily: "Inter, system-ui" },
      grid: { vertLines: { color: C.grid }, horzLines: { color: C.grid } },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderColor: "#1d2a45" },
      timeScale: { borderColor: "#1d2a45", timeVisible: true, secondsVisible: false, rightOffset: 6 },
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
    return () => { ch.remove(); chart.current = null; series.current = null; };
  }, []);

  // candles
  useEffect(() => {
    if (!series.current) return;
    series.current.setData(candles.map((c) => ({ time: c.time as UTCTimestamp, open: c.open, high: c.high, low: c.low, close: c.close })));
    last.current = candles.length ? { ...candles[candles.length - 1] } : null;
  }, [candles]);

  // fit when the timeframe changes
  useEffect(() => {
    const ch = chart.current;
    if (!ch || !candles.length) return;
    const n = candles.length;
    const bars = Math.max(40, Math.min(160, Math.round((box.current?.clientWidth ?? 900) / 7)));  // ~7 px per candle
    ch.timeScale().setVisibleLogicalRange({ from: Math.max(0, n - bars), to: n + 6 });
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
    zones.current.set(boxes, segs, lastTime);

    const ms: SeriesMarker<Time>[] = [];
    if (a && (layers.structure || layers.delta)) {  // Delta = absorption + flip; Δ detail adds aggression + imbalance
      for (const m of a.markers) {
        const bull = m.dir === "bull";
        const isDelta = m.kind.startsWith("DELTA_") || m.kind === "FOOTPRINT_IMBALANCE" || m.kind.startsWith("PROXY_FLOW");
        if (isDelta ? !layers.delta : !layers.structure) continue;
        // main layer: aggression + absorption (+ proxy fade); detail layer adds flips, imbalances, proxy surges
        if (isDelta && !layers.deltaDetail && (m.kind === "DELTA_FLIP" || m.kind === "FOOTPRINT_IMBALANCE" || m.kind === "PROXY_FLOW_SURGE")) continue;
        if (m.kind.startsWith("PROXY_FLOW")) {
          ms.push({ time: m.time as UTCTimestamp, position: bull ? "belowBar" : "aboveBar", shape: "circle",
            color: "#fb923c", text: `${m.text} (proxy)`, size: 0.6 });
          continue;
        }
        if (isDelta) {
          const abs = m.kind === "DELTA_ABSORPTION", flip = m.kind === "DELTA_FLIP";
          ms.push({
            time: m.time as UTCTimestamp,
            position: m.kind === "DELTA_AGGRESSION" ? (bull ? "belowBar" : "aboveBar") : (bull ? "belowBar" : "aboveBar"),
            shape: abs ? "circle" : "square",
            color: abs ? "#e8c35a" : flip ? "#e2e8f0" : m.kind === "FOOTPRINT_IMBALANCE" ? (bull ? "#22d3ee" : "#f472b6") : (bull ? "#34d399" : "#f87171"),
            text: m.text,
            size: abs ? 0.9 : 0.5,
          });
          continue;
        }
        const isStruct = m.kind === "BOS" || m.kind === "MSS";
        ms.push({
          time: m.time as UTCTimestamp,
          position: isStruct ? (bull ? "belowBar" : "aboveBar") : (bull ? "belowBar" : "aboveBar"),
          shape: isStruct ? (bull ? "arrowUp" : "arrowDown") : "circle",
          color: isStruct ? (bull ? C.bull : C.bear) : (m.kind === "SWEEP" ? "#f59e0b" : "#a78bfa"),
          text: m.text,
          size: isStruct ? 1 : 0.6,
        });
      }
    }
    if (a && layers.crt && a.crt) {
      for (const m of a.crt.markers) {
        const bull = m.dir === "bull";
        const entry = m.kind === "CRT_ENTRY", tbs = m.kind === "CRT_TBS";
        ms.push({
          time: m.time as UTCTimestamp,
          position: tbs ? (bull ? "belowBar" : "aboveBar") : (bull ? "belowBar" : "aboveBar"),
          shape: entry ? (bull ? "arrowUp" : "arrowDown") : tbs ? "square" : "circle",
          color: entry ? (bull ? "#22c55e" : "#ef4444") : tbs ? "#e8c35a" : "#cbd5e1",
          text: m.text, size: entry ? 1.2 : 0.7,
        });
      }
    }
    if (a && layers.auction && a.auction) {
      for (const m of a.auction.markers) {
        const acc = m.kind === "AUCTION_ACCEPT", rej = m.kind === "AUCTION_REJECT";
        const bull = m.dir === "bull";
        ms.push({ time: m.time as UTCTimestamp, position: bull ? "belowBar" : "aboveBar", shape: acc ? (bull ? "arrowUp" : "arrowDown") : "circle",
          color: acc ? "#38bdf8" : rej ? "#fb923c" : "#94a3b8", text: m.text, size: acc ? 0.9 : 0.7 });
      }
    }
    if (a && layers.oi) {
      for (const x of a.volume?.oi_line ?? []) {
        if (x.surge) ms.push({ time: x.time as UTCTimestamp, position: "aboveBar", shape: "square", color: "#f59e0b", text: `OI⚡${x.surge_x ?? ""}×`, size: 0.6 });
      }
    }
    ms.sort((x, y) => (x.time as number) - (y.time as number));
    markers.current.setMarkers(ms);

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
    if (ch && ol.length && !oiS.current) {
      const o = ch.addSeries(LineSeries, { color: "#38bdf8", lineWidth: 2, priceLineVisible: false, title: "FUT OI",
        priceFormat: { type: "volume" } }, 1);
      const e = ch.addSeries(LineSeries, { color: "#e8c35a", lineWidth: 1, priceLineVisible: false, lastValueVisible: false,
        title: "EMA 8", priceFormat: { type: "volume" } }, 1);
      const d = ch.addSeries(HistogramSeries, { priceLineVisible: false, lastValueVisible: false, title: "ΔOI", priceFormat: { type: "volume" } }, 2);
      try {
        const P = ch.panes();
        P[0]?.setStretchFactor(5); P[1]?.setStretchFactor(1.6); P[2]?.setStretchFactor(1);
      } catch { /* older lib */ }
      oiS.current = { oi: o, ema: e, doi: d };
    } else if (ch && !ol.length && oiS.current) {
      ch.removeSeries(oiS.current.oi); ch.removeSeries(oiS.current.ema); ch.removeSeries(oiS.current.doi);
      oiS.current = null;
    }
    if (oiS.current && ol.length) {
      const BC: Record<string, string> = { "long build-up": "#22c55e", "short covering": "#86efac", "short build-up": "#ef4444", "long unwinding": "#fca5a5" };
      oiS.current.oi.setData(ol.map((x) => ({ time: x.time as UTCTimestamp, value: x.oi })));
      oiS.current.ema.setData(ol.map((x) => ({ time: x.time as UTCTimestamp, value: x.ema })));
      oiS.current.doi.setData(ol.filter((x) => x.doi != null).map((x) => ({ time: x.time as UTCTimestamp, value: x.doi as number,
        color: x.surge ? "#f59e0b" : (BC[x.build ?? ""] ?? "rgba(148,163,184,0.5)") })));
    }
    zz.current.setData(a && layers.swings ? dedupe(a.zigzag).map((p) => ({ time: p.time as UTCTimestamp, value: p.price })) : []);
  }, [analysis, layers, candles]);

  return <div ref={box} className="h-full w-full" />;
}

function dedupe(pts: { time: number; price: number }[]) {
  const out: { time: number; price: number }[] = [];
  for (const p of pts) {
    if (out.length && out[out.length - 1].time >= p.time) continue;     // line series needs strictly increasing time
    out.push(p);
  }
  return out;
}
