// SmartOISuite.tsx — World-Class "Smart OI" Institutional Suite.
// Features:
//   1. Retail Noise & Stale Position Filtering:
//      - Deep OTM Noise Exclusion (OI < 5L & Vol < 10K filtered out).
//      - Stale Position Filter (abs(oi_chg) < 1K filtered out).
//      - Isolates high-conviction institutional "Signal Strikes".
//   2. Smart OI Candlestick Price-Action Confluence:
//      - Synchronized NIFTY Futures candlestick chart overlaid with Smart Support & Resistance lines.
//      - Live flags for Bullish Confluence, Bearish Confluence, and Divergence Traps/Absorptions.
//   3. Smart OI Net Delta & Cumulative Volume Sub-Pane:
//      - Institutional color semantics (Put support = Emerald #10b981, Call resistance = Crimson #ef4444).
//   4. Multi-Line Trend Telemetry & Multi-PCR Sub-Chains:
//      - Put OI Chg vs Call OI Chg vs PE-CE Net Spread.
//      - OI PCR vs Vol PCR with 0.8 & 1.2 reference lines.
//   5. 🗖 Enlarge / Fullscreen Modal on every chart.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { inr } from "./OcLab";
import {
  CandlestickSeries, ColorType, createChart, createSeriesMarkers, HistogramSeries, LineSeries, LineStyle,
  type IChartApi, type ISeriesApi, type SeriesMarker, type Time, type UTCTimestamp,
} from "lightweight-charts";

export interface SmartOIStrike {
  strike: number;
  is_atm: boolean;
  dist_atm: number;
  call_oi: number;
  call_oi_chg: number;
  call_vol: number;
  put_oi: number;
  put_oi_chg: number;
  put_vol: number;
  net_oi: number;
  net_oi_chg: number;
  is_signal: boolean;
  signal_type: "PUT_SUPPORT_FLOOR" | "CALL_RESISTANCE_CEILING" | "CALL_SQUEEZE_BREAKOUT" | "PUT_UNWINDING_BREAKDOWN" | "BALANCED_WALL" | "NEUTRAL";
  signal_label: string;
  conviction: number;
  is_retail_noise: boolean;
  is_stale: boolean;
}

export interface SmartOICandle {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  smart_oi_delta: number;
  cum_smart_oi: number;
  pcr_oi: number;
  pcr_vol: number;
  markers: Array<{
    time: number;
    position: "aboveBar" | "belowBar";
    color: string;
    shape: "arrowUp" | "arrowDown" | "circle";
    text: string;
    kind: string;
  }>;
}

export interface SmartOITimeSeriesPoint {
  time: number;
  fut_close: number;
  smart_oi_delta: number;
  cum_smart_oi: number;
  pcr_oi: number;
  pcr_vol: number;
  volume: number;
}

export interface SmartOIPayload {
  ok: boolean;
  day: string;
  expiry: string;
  expiry_mode: string;
  tf: string;
  range_mode: string;
  span: number;
  spot: number;
  atm_strike: number;
  key_support_level: number;
  key_support_oi: number;
  key_resistance_level: number;
  key_resistance_oi: number;
  confluence_summary: string;
  signal_strikes: SmartOIStrike[];
  all_strikes: SmartOIStrike[];
  candles: SmartOICandle[];
  markers: Array<any>;
  time_series: SmartOITimeSeriesPoint[];
  stats: {
    total_strikes: number;
    signal_strikes: number;
    retail_noise_filtered: number;
    bullish_confluences: number;
    bearish_confluences: number;
    divergence_warnings: number;
  };
  message?: string;
}

export interface ExpiriesMeta {
  cur_date: string;
  next_date: string;
  cur_expiry?: string;
  next_expiry?: string;
  near_fut: string;
  next_fut: string;
  rollover_pct: number;
  fut_src?: string;
}

export interface SmartOISuiteProps {
  tick?: number;
  onClose?: () => void;
  modeToggle?: React.ReactNode;
  optExpiry?: "cur" | "next" | "both";
  setOptExpiry?: (e: "cur" | "next" | "both") => void;
  futContract?: "near" | "next" | "combined";
  setFutContract?: (f: "near" | "next" | "combined") => void;
  expMeta?: ExpiriesMeta | null;
  onMetaUpdate?: (meta: ExpiriesMeta) => void;
}

export default function SmartOISuite({
  tick = 0,
  onClose,
  modeToggle,
  optExpiry,
  setOptExpiry,
  futContract,
  setFutContract,
  expMeta,
  onMetaUpdate,
}: SmartOISuiteProps) {
  // Controls state
  const [tf, setTf] = useState<"1m" | "3m" | "5m" | "15m">(() => {
    try {
      const s = localStorage.getItem("ict_smart_oi_tf");
      if (s === "1m" || s === "3m" || s === "5m" || s === "15m") return s;
    } catch { /* ignore */ }
    return "3m";
  });

  const [expiryTab, setExpiryTab] = useState<"cur" | "next" | "both">(() => {
    try {
      const s = localStorage.getItem("ict_smart_oi_exp");
      if (s === "cur" || s === "next" || s === "both") return s;
    } catch { /* ignore */ }
    return "cur";
  });

  const [internalFutContract, setInternalFutContract] = useState<"near" | "next" | "combined">(() => {
    try {
      const saved = localStorage.getItem("ict_writers_fut_contract") as "near" | "next" | "combined";
      if (saved === "near" || saved === "next" || saved === "combined") return saved;
    } catch { /* ignore */ }
    return "combined";
  });

  const currentOptExpiry = optExpiry ?? expiryTab;
  const currentFutMode = futContract ?? internalFutContract;
  const activeMeta = expMeta;

  const [rangeMode, setRangeMode] = useState<"atm" | "fixed" | "all">(() => {
    try {
      const s = localStorage.getItem("ict_smart_oi_range");
      if (s === "atm" || s === "fixed" || s === "all") return s;
    } catch { /* ignore */ }
    return "atm";
  });

  const [span, setSpan] = useState<number>(10);
  const [telemetryTab, setTelemetryTab] = useState<"flow" | "pcr">("flow");

  // Fullscreen modal state
  const [enlargedPanel, setEnlargedPanel] = useState<"confluence" | "delta" | "telemetry" | "strikes" | null>(null);

  const [data, setData] = useState<SmartOIPayload | null>(null);
  const [busy, setBusy] = useState<boolean>(false);

  // Chart container refs
  const confluenceChartRef = useRef<HTMLDivElement>(null);
  const deltaChartRef = useRef<HTMLDivElement>(null);
  const telemetryChartRef = useRef<HTMLDivElement>(null);
  const modalChartRef = useRef<HTMLDivElement>(null);

  // Chart instances refs
  const chart1Api = useRef<IChartApi | null>(null);
  const chart2Api = useRef<IChartApi | null>(null);
  const chart3Api = useRef<IChartApi | null>(null);
  const modalChartApi = useRef<IChartApi | null>(null);

  const fetchData = useCallback(() => {
    setBusy(true);
    fetch(`/api/chain/smart_oi?opt_expiry=${currentOptExpiry}&fut_contract=${currentFutMode}&tf=${tf}&range_mode=${rangeMode}&span=${span}`)
      .then((r) => r.json())
      .then((json: SmartOIPayload & { expiries_meta?: ExpiriesMeta }) => {
        if (json && json.ok) {
          setData(json);
          if (json.expiries_meta && onMetaUpdate) {
            onMetaUpdate(json.expiries_meta);
          }
        }
      })
      .catch((e) => console.error("fetch smart_oi error:", e))
      .finally(() => setBusy(false));
  }, [currentOptExpiry, currentFutMode, tf, rangeMode, span, onMetaUpdate]);

  useEffect(() => {
    fetchData();
    const interval = window.setInterval(fetchData, 15000);
    return () => window.clearInterval(interval);
  }, [fetchData, tick]);

  // Keydown listener for ESC to close fullscreen
  useEffect(() => {
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setEnlargedPanel(null);
    };
    window.addEventListener("keydown", handleKey);
    return () => window.removeEventListener("keydown", handleKey);
  }, []);

  const handleSetTf = (v: "1m" | "3m" | "5m" | "15m") => {
    setTf(v);
    try { localStorage.setItem("ict_smart_oi_tf", v); } catch { /* ignore */ }
  };

  const handleSetExpiry = (e: "cur" | "next" | "both") => {
    if (setOptExpiry) {
      setOptExpiry(e);
    } else {
      setExpiryTab(e);
      try { localStorage.setItem("ict_smart_oi_exp", e); } catch { /* ignore */ }
    }
  };

  const handleSetFutMode = (f: "near" | "next" | "combined") => {
    if (setFutContract) {
      setFutContract(f);
    } else {
      setInternalFutContract(f);
      try { localStorage.setItem("ict_writers_fut_contract", f); } catch { /* ignore */ }
    }
  };

  const handleSetRange = (r: "atm" | "fixed" | "all") => {
    setRangeMode(r);
    try { localStorage.setItem("ict_smart_oi_range", r); } catch { /* ignore */ }
  };

  // ──────────────────────────── Render Panel 1: Confluence Candlestick Chart ────────────────────────────
  useEffect(() => {
    if (!confluenceChartRef.current || !data?.candles?.length) return;
    const container = confluenceChartRef.current;
    container.innerHTML = "";

    const chart = createChart(container, {
      layout: { background: { type: ColorType.Solid, color: "#070c17" }, textColor: "#94a3b8" },
      grid: { vertLines: { color: "#131f36" }, horzLines: { color: "#131f36" } },
      timeScale: {
        timeVisible: true,
        secondsVisible: false,
        borderColor: "#1e293b",
        fixLeftEdge: true,
        fixRightEdge: true,
      },
      rightPriceScale: { borderColor: "#1e293b" },
      crosshair: { mode: 0 },
    });
    chart1Api.current = chart;

    const candleSeries = chart.addSeries(CandlestickSeries, {
      upColor: "#10b981",
      downColor: "#ef4444",
      borderVisible: false,
      wickUpColor: "#10b981",
      wickDownColor: "#ef4444",
    });

    const candleData = data.candles.map((c) => ({
      time: c.time as UTCTimestamp,
      open: c.open,
      high: c.high,
      low: c.low,
      close: c.close,
    }));
    candleSeries.setData(candleData);

    // Overlaid Horizontal Price Lines: Smart Support Floor & Resistance Ceiling
    if (data.key_support_level > 0) {
      candleSeries.createPriceLine({
        price: data.key_support_level,
        color: "#10b981",
        lineWidth: 2,
        lineStyle: LineStyle.Dashed,
        axisLabelVisible: true,
        title: `🟢 Smart Support Floor (${data.key_support_level})`,
      });
    }

    if (data.key_resistance_level > 0) {
      candleSeries.createPriceLine({
        price: data.key_resistance_level,
        color: "#ef4444",
        lineWidth: 2,
        lineStyle: LineStyle.Dashed,
        axisLabelVisible: true,
        title: `🔴 Smart Resistance Ceiling (${data.key_resistance_level})`,
      });
    }

    // Candle Confluence / Divergence Markers
    const markers: SeriesMarker<Time>[] = [];
    for (const c of data.candles) {
      if (c.markers && c.markers.length > 0) {
        for (const m of c.markers) {
          markers.push({
            time: m.time as UTCTimestamp,
            position: m.position,
            color: m.color,
            shape: m.shape,
            text: m.text,
          });
        }
      }
    }
    if (markers.length > 0) {
      createSeriesMarkers(candleSeries, markers);
    }

    chart.timeScale().fitContent();

    const ro = new ResizeObserver((entries) => {
      for (const entry of entries) {
        if (entry.contentRect.width > 0 && entry.contentRect.height > 0) {
          chart.applyOptions({
            width: Math.floor(entry.contentRect.width),
            height: Math.floor(entry.contentRect.height),
          });
          chart.timeScale().fitContent();
        }
      }
    });
    ro.observe(container);

    return () => {
      ro.disconnect();
      chart.remove();
      chart1Api.current = null;
    };
  }, [data?.candles, data?.key_support_level, data?.key_resistance_level]);

  // ──────────────────────────── Render Panel 2: Net Delta & Volume Sub-Pane ────────────────────────────
  useEffect(() => {
    if (!deltaChartRef.current || !data?.time_series?.length) return;
    const container = deltaChartRef.current;
    container.innerHTML = "";

    const chart = createChart(container, {
      layout: { background: { type: ColorType.Solid, color: "#070c17" }, textColor: "#94a3b8" },
      grid: { vertLines: { color: "#131f36" }, horzLines: { color: "#131f36" } },
      timeScale: {
        timeVisible: true,
        secondsVisible: false,
        borderColor: "#1e293b",
        fixLeftEdge: true,
        fixRightEdge: true,
      },
      rightPriceScale: { borderColor: "#1e293b" },
    });
    chart2Api.current = chart;

    // Histogram Series for Net Delta
    const histSeries = chart.addSeries(HistogramSeries, {
      color: "#10b981",
      priceFormat: { type: "volume" },
    });

    const histData = data.time_series.map((pt) => ({
      time: pt.time as UTCTimestamp,
      value: pt.smart_oi_delta,
      color: pt.smart_oi_delta >= 0 ? "#10b981" : "#ef4444",
    }));
    histSeries.setData(histData);

    // Cumulative Smart OI line
    const cumSeries = chart.addSeries(LineSeries, {
      color: "#38bdf8",
      lineWidth: 2,
      priceScaleId: "cum",
    });
    chart.priceScale("cum").applyOptions({
      borderColor: "#38bdf8",
      scaleMargins: { top: 0.1, bottom: 0.1 },
    });

    const cumData = data.time_series.map((pt) => ({
      time: pt.time as UTCTimestamp,
      value: pt.cum_smart_oi,
    }));
    cumSeries.setData(cumData);

    chart.timeScale().fitContent();

    const ro = new ResizeObserver((entries) => {
      for (const entry of entries) {
        if (entry.contentRect.width > 0 && entry.contentRect.height > 0) {
          chart.applyOptions({
            width: Math.floor(entry.contentRect.width),
            height: Math.floor(entry.contentRect.height),
          });
          chart.timeScale().fitContent();
        }
      }
    });
    ro.observe(container);

    return () => {
      ro.disconnect();
      chart.remove();
      chart2Api.current = null;
    };
  }, [data?.time_series]);

  // ──────────────────────────── Render Panel 3: Multi-Line Trend & PCR Sub-Pane ────────────────────────────
  useEffect(() => {
    if (!telemetryChartRef.current || !data?.time_series?.length) return;
    const container = telemetryChartRef.current;
    container.innerHTML = "";

    const chart = createChart(container, {
      layout: { background: { type: ColorType.Solid, color: "#070c17" }, textColor: "#94a3b8" },
      grid: { vertLines: { color: "#131f36" }, horzLines: { color: "#131f36" } },
      timeScale: {
        timeVisible: true,
        secondsVisible: false,
        borderColor: "#1e293b",
        fixLeftEdge: true,
        fixRightEdge: true,
      },
      rightPriceScale: { borderColor: "#1e293b" },
    });
    chart3Api.current = chart;

    if (telemetryTab === "pcr") {
      // OI PCR Line
      const oiPcrSeries = chart.addSeries(LineSeries, {
        color: "#e8c35a",
        lineWidth: 2,
        title: "OI PCR",
      });
      oiPcrSeries.setData(data.time_series.map((pt) => ({ time: pt.time as UTCTimestamp, value: pt.pcr_oi })));

      // Vol PCR Line
      const volPcrSeries = chart.addSeries(LineSeries, {
        color: "#c084fc",
        lineWidth: 2,
        title: "Vol PCR",
      });
      volPcrSeries.setData(data.time_series.map((pt) => ({ time: pt.time as UTCTimestamp, value: pt.pcr_vol })));

      oiPcrSeries.createPriceLine({ price: 1.2, color: "#10b981", lineWidth: 1, lineStyle: LineStyle.Dotted, title: "Bull 1.2" });
      oiPcrSeries.createPriceLine({ price: 0.8, color: "#ef4444", lineWidth: 1, lineStyle: LineStyle.Dotted, title: "Bear 0.8" });
    } else {
      // Put vs Call Delta Flow
      const spreadSeries = chart.addSeries(LineSeries, {
        color: "#38bdf8",
        lineWidth: 2,
        title: "PE-CE Spread",
      });
      spreadSeries.setData(data.time_series.map((pt) => ({ time: pt.time as UTCTimestamp, value: pt.smart_oi_delta })));

      const futSeries = chart.addSeries(LineSeries, {
        color: "#94a3b8",
        lineWidth: 1,
        priceScaleId: "fut",
        title: "NIFTY Futures",
      });
      chart.priceScale("fut").applyOptions({
        borderColor: "#94a3b8",
        scaleMargins: { top: 0.15, bottom: 0.15 },
      });
      futSeries.setData(data.time_series.map((pt) => ({ time: pt.time as UTCTimestamp, value: pt.fut_close })));
    }

    chart.timeScale().fitContent();

    const ro = new ResizeObserver((entries) => {
      for (const entry of entries) {
        if (entry.contentRect.width > 0 && entry.contentRect.height > 0) {
          chart.applyOptions({
            width: Math.floor(entry.contentRect.width),
            height: Math.floor(entry.contentRect.height),
          });
          chart.timeScale().fitContent();
        }
      }
    });
    ro.observe(container);

    return () => {
      ro.disconnect();
      chart.remove();
      chart3Api.current = null;
    };
  }, [data?.time_series, telemetryTab]);

  // ──────────────────────────── Render Modal Fullscreen Chart ────────────────────────────
  useEffect(() => {
    if (!enlargedPanel || !modalChartRef.current || !data) return;
    const container = modalChartRef.current;
    container.innerHTML = "";

    const chart = createChart(container, {
      layout: { background: { type: ColorType.Solid, color: "#070c17" }, textColor: "#94a3b8" },
      grid: { vertLines: { color: "#131f36" }, horzLines: { color: "#131f36" } },
      timeScale: { timeVisible: true, secondsVisible: false, borderColor: "#1e293b", fixLeftEdge: true, fixRightEdge: true },
      rightPriceScale: { borderColor: "#1e293b" },
    });
    modalChartApi.current = chart;

    if (enlargedPanel === "confluence" && data.candles?.length) {
      const candleSeries = chart.addSeries(CandlestickSeries, {
        upColor: "#10b981", downColor: "#ef4444", borderVisible: false, wickUpColor: "#10b981", wickDownColor: "#ef4444",
      });
      candleSeries.setData(data.candles.map((c) => ({ time: c.time as UTCTimestamp, open: c.open, high: c.high, low: c.low, close: c.close })));
      if (data.key_support_level > 0) {
        candleSeries.createPriceLine({ price: data.key_support_level, color: "#10b981", lineWidth: 2, lineStyle: LineStyle.Dashed, title: `🟢 Smart Support Floor (${data.key_support_level})` });
      }
      if (data.key_resistance_level > 0) {
        candleSeries.createPriceLine({ price: data.key_resistance_level, color: "#ef4444", lineWidth: 2, lineStyle: LineStyle.Dashed, title: `🔴 Smart Resistance Ceiling (${data.key_resistance_level})` });
      }
      const markers: SeriesMarker<Time>[] = [];
      for (const c of data.candles) {
        if (c.markers) {
          for (const m of c.markers) {
            markers.push({ time: m.time as UTCTimestamp, position: m.position, color: m.color, shape: m.shape, text: m.text });
          }
        }
      }
      if (markers.length > 0) createSeriesMarkers(candleSeries, markers);
    } else if (enlargedPanel === "delta" && data.time_series?.length) {
      const histSeries = chart.addSeries(HistogramSeries, { color: "#10b981" });
      histSeries.setData(data.time_series.map((pt) => ({ time: pt.time as UTCTimestamp, value: pt.smart_oi_delta, color: pt.smart_oi_delta >= 0 ? "#10b981" : "#ef4444" })));
      const cumSeries = chart.addSeries(LineSeries, { color: "#38bdf8", lineWidth: 2, priceScaleId: "cum" });
      chart.priceScale("cum").applyOptions({ borderColor: "#38bdf8", scaleMargins: { top: 0.1, bottom: 0.1 } });
      cumSeries.setData(data.time_series.map((pt) => ({ time: pt.time as UTCTimestamp, value: pt.cum_smart_oi })));
    } else if (enlargedPanel === "telemetry" && data.time_series?.length) {
      const oiPcr = chart.addSeries(LineSeries, { color: "#e8c35a", lineWidth: 2, title: "OI PCR" });
      oiPcr.setData(data.time_series.map((pt) => ({ time: pt.time as UTCTimestamp, value: pt.pcr_oi })));
      const volPcr = chart.addSeries(LineSeries, { color: "#c084fc", lineWidth: 2, title: "Vol PCR" });
      volPcr.setData(data.time_series.map((pt) => ({ time: pt.time as UTCTimestamp, value: pt.pcr_vol })));
      oiPcr.createPriceLine({ price: 1.2, color: "#10b981", lineWidth: 1, lineStyle: LineStyle.Dotted, title: "Bull 1.2" });
      oiPcr.createPriceLine({ price: 0.8, color: "#ef4444", lineWidth: 1, lineStyle: LineStyle.Dotted, title: "Bear 0.8" });
    }

    chart.timeScale().fitContent();

    const ro = new ResizeObserver((entries) => {
      for (const entry of entries) {
        if (entry.contentRect.width > 0 && entry.contentRect.height > 0) {
          chart.applyOptions({ width: Math.floor(entry.contentRect.width), height: Math.floor(entry.contentRect.height) });
          chart.timeScale().fitContent();
        }
      }
    });
    ro.observe(container);

    return () => {
      ro.disconnect();
      chart.remove();
      modalChartApi.current = null;
    };
  }, [enlargedPanel, data]);

  const stats = data?.stats;

  return (
    <div className="flex h-full w-full flex-col bg-[#070c17] text-slate-200 select-none overflow-hidden font-sans">
      {/* ────────────────────────────────── Top Control Bar ────────────────────────────────── */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-[#162238] bg-[#091122] px-3 py-1.5 text-[11px] shrink-0">
        <div className="flex items-center gap-2">
          <span className="font-extrabold text-gold-400">⚡ Smart OI Intelligence Suite</span>
          {modeToggle}
          <span className="text-slate-500 hidden xl:inline">
            • Retail Noise Stripped • Institutional Signal Strikes • Price Confluence
          </span>
        </div>

        {/* Controls: Timeframe, Expiry, Range Mode */}
        <div className="flex flex-wrap items-center gap-2">
          {/* Timeframe selector */}
          <div className="flex rounded bg-[#0c1424] p-0.5 border border-slate-800">
            {(["1m", "3m", "5m", "15m"] as const).map((t) => (
              <button
                key={t}
                onClick={() => handleSetTf(t)}
                className={`rounded px-2 py-0.5 text-[11px] font-bold transition-all ${
                  tf === t
                    ? "bg-sky-500/30 text-sky-200 border border-sky-500/60 shadow-sm"
                    : "text-slate-400 hover:text-slate-200"
                }`}
              >
                {t}
              </button>
            ))}
          </div>

          {/* Options Expiry selector */}
          <div className="flex items-center rounded bg-[#0c1424] p-0.5 border border-slate-800">
            <span className="text-[10px] uppercase font-bold text-slate-500 px-1">Options:</span>
            {(["cur", "next", "both"] as const).map((e) => (
              <button
                key={e}
                onClick={() => handleSetExpiry(e)}
                className={`rounded px-2 py-0.5 text-[11px] font-bold transition-all ${
                  currentOptExpiry === e
                    ? "bg-sky-500/30 text-sky-200 border border-sky-500/60 shadow-sm"
                    : "text-slate-400 hover:text-slate-200"
                }`}
              >
                {e === "cur"
                  ? `This Wk (${activeMeta?.cur_date || "27 Oct"})`
                  : e === "next"
                  ? `Next Wk (${activeMeta?.next_date || "03 Nov"})`
                  : "Both"}
              </button>
            ))}
          </div>

          {/* Futures Contract selector */}
          <div className="flex items-center rounded bg-[#0c1424] p-0.5 border border-slate-800">
            <span className="text-[10px] uppercase font-bold text-slate-500 px-1">Futures:</span>
            {(["near", "next", "combined"] as const).map((f) => (
              <button
                key={f}
                onClick={() => handleSetFutMode(f)}
                className={`rounded px-2 py-0.5 text-[11px] font-bold transition-all ${
                  currentFutMode === f
                    ? "bg-indigo-500/30 text-indigo-200 border border-indigo-500/60 shadow-sm"
                    : "text-slate-400 hover:text-slate-200"
                }`}
              >
                {f === "near"
                  ? (activeMeta?.near_fut ? activeMeta.near_fut.replace("NIFTY-", "").replace("-FUT", "") + " FUT" : "Oct FUT")
                  : f === "next"
                  ? (activeMeta?.next_fut ? activeMeta.next_fut.replace("NIFTY-", "").replace("-FUT", "") + " FUT" : "Nov FUT")
                  : `Oct+Nov (${activeMeta?.rollover_pct != null ? "Roll " + activeMeta.rollover_pct + "%" : "Combined"})`}
              </button>
            ))}
          </div>

          {/* Range Mode */}
          <div className="flex rounded bg-[#0c1424] p-0.5 border border-slate-800">
            {(["atm", "fixed", "all"] as const).map((r) => (
              <button
                key={r}
                onClick={() => handleSetRange(r)}
                className={`rounded px-2 py-0.5 text-[11px] font-bold transition-all ${
                  rangeMode === r
                    ? "bg-gold-500/30 text-gold-200 border border-gold-500/60 shadow-sm"
                    : "text-slate-400 hover:text-slate-200"
                }`}
              >
                {r === "atm" ? "Auto ATM" : r === "fixed" ? "Fixed" : "All Strikes"}
              </button>
            ))}
          </div>

          {rangeMode !== "all" && (
            <div className="flex rounded bg-[#0c1424] p-0.5 border border-slate-800">
              {([5, 10, 15] as const).map((n) => (
                <button
                  key={n}
                  onClick={() => setSpan(n)}
                  className={`rounded px-1.5 py-0.5 text-[10.5px] font-mono font-bold ${
                    span === n ? "bg-slate-700 text-white" : "text-slate-400"
                  }`}
                >
                  ±{n}
                </button>
              ))}
            </div>
          )}

          <button
            onClick={fetchData}
            className="rounded bg-[#0c1424] px-2 py-0.5 text-[11px] font-bold text-slate-300 border border-slate-800 hover:bg-slate-800"
            title="Refresh Smart OI"
          >
            {busy ? "⟳" : "↻"}
          </button>

          {onClose && (
            <button
              onClick={onClose}
              className="text-slate-400 hover:text-rose-400 font-bold px-1"
              title="Close drawer"
            >
              ✕
            </button>
          )}
        </div>
      </div>

      {/* ────────────────────────────────── Executive Telemetry Cockpit ────────────────────────────────── */}
      <div className="grid grid-cols-1 md:grid-cols-4 gap-2 p-2 bg-[#091122] border-b border-[#142036] shrink-0">
        {/* Card 1: Confluence Status */}
        <div className="flex flex-col justify-between rounded-lg bg-[#070c17] p-2.5 border border-[#1b273d] shadow">
          <div className="text-[11px] font-extrabold uppercase text-slate-400 tracking-wider">
            ⚖ Smart OI Confluence State
          </div>
          <div className="my-1 text-[11.5px] font-bold text-slate-100">
            {data?.confluence_summary || "Analyzing institutional levels..."}
          </div>
          <div className="text-[10px] text-slate-400 flex justify-between">
            <span>Spot: <b className="text-sky-300 font-mono">{data?.spot}</b></span>
            <span>ATM: <b className="text-white font-mono">{data?.atm_strike}</b></span>
          </div>
        </div>

        {/* Card 2: Noise Filtering Stats */}
        <div className="flex flex-col justify-between rounded-lg bg-[#070c17] p-2.5 border border-[#1b273d] shadow">
          <div className="text-[11px] font-extrabold uppercase text-slate-400 tracking-wider">
            🛡️ Retail Noise Filter
          </div>
          <div className="space-y-1 my-1 text-[11px]">
            <div className="flex justify-between">
              <span className="text-slate-400">Institutional Signal Strikes:</span>
              <span className="font-mono font-bold text-emerald-400">{stats?.signal_strikes ?? 0}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-slate-400">Retail / Stale Strikes Stripped:</span>
              <span className="font-mono font-semibold text-rose-400">{stats?.retail_noise_filtered ?? 0}</span>
            </div>
          </div>
          <div className="text-[10px] text-slate-400">
            Noise rejection rate: <b className="text-white font-mono">{stats?.total_strikes ? Math.round((stats.retail_noise_filtered / stats.total_strikes) * 100) : 0}%</b>
          </div>
        </div>

        {/* Card 3: Key Walls & Floors */}
        <div className="flex flex-col justify-between rounded-lg bg-[#070c17] p-2.5 border border-[#1b273d] shadow">
          <div className="text-[11px] font-extrabold uppercase text-slate-400 tracking-wider">
            🏰 Institutional Level Lines
          </div>
          <div className="space-y-1 my-1 text-[11px]">
            <div className="flex justify-between items-center">
              <span className="text-emerald-400 font-bold">🟢 Put Support Floor:</span>
              <span className="font-mono font-black text-white">
                {data?.key_support_level} <span className="text-slate-400 text-[10px]">({inr(data?.key_support_oi)})</span>
              </span>
            </div>
            <div className="flex justify-between items-center">
              <span className="text-rose-400 font-bold">🔴 Call Resistance Ceiling:</span>
              <span className="font-mono font-black text-white">
                {data?.key_resistance_level} <span className="text-slate-400 text-[10px]">({inr(data?.key_resistance_oi)})</span>
              </span>
            </div>
          </div>
          <div className="text-[10px] text-slate-400">
            Channel Width: <b className="text-gold-300 font-mono">{data ? Math.abs(data.key_resistance_level - data.key_support_level) : 0} pts</b>
          </div>
        </div>

        {/* Card 4: Confluence Event Counts */}
        <div className="flex flex-col justify-between rounded-lg bg-[#070c17] p-2.5 border border-[#1b273d] shadow">
          <div className="text-[11px] font-extrabold uppercase text-slate-400 tracking-wider">
            🎯 Confluence Events
          </div>
          <div className="space-y-1 my-1 text-[11px]">
            <div className="flex justify-between">
              <span className="text-emerald-300">Bullish Bounces:</span>
              <span className="font-mono font-bold text-emerald-400">{stats?.bullish_confluences ?? 0}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-rose-300">Bearish Rejections:</span>
              <span className="font-mono font-bold text-rose-400">{stats?.bearish_confluences ?? 0}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-amber-300">Divergence Traps:</span>
              <span className="font-mono font-bold text-amber-400">{stats?.divergence_warnings ?? 0}</span>
            </div>
          </div>
          <div className="text-[10px] text-slate-400">
            Active Mode: <b className="text-sky-300">{rangeMode.toUpperCase()} ({tf})</b>
          </div>
        </div>
      </div>

      {/* ────────────────────────────────── Interactive Multi-Panel Grid ────────────────────────────────── */}
      <div className="flex-1 min-h-0 grid grid-cols-1 lg:grid-cols-12 gap-1 p-1 overflow-hidden bg-[#050914]">
        {/* Left Column (8 cols): Candlestick Confluence Chart & Delta Sub-Pane */}
        <div className="lg:col-span-8 flex flex-col h-full min-h-0 gap-1 overflow-hidden">
          {/* Panel 1: NIFTY Futures Smart OI Confluence Candlestick Chart */}
          <div className="flex-[1.4] min-h-0 flex flex-col rounded bg-[#070c17] border border-[#142036] overflow-hidden">
            <div className="flex items-center justify-between border-b border-[#142036] bg-[#091122] px-2.5 py-1 text-[11px] shrink-0">
              <div className="flex items-center gap-2">
                <span className="font-black text-sky-400">📊 NIFTY Futures Price Action Confluence</span>
                <span className="text-slate-500 hidden sm:inline">• Smart Support Floor & Resistance Ceiling</span>
              </div>
              <div className="flex items-center gap-1.5">
                <button
                  onClick={() => setEnlargedPanel("confluence")}
                  className="rounded bg-[#0c1424] hover:bg-slate-700 px-2 py-0.5 text-[10px] font-bold text-slate-300 border border-slate-700 flex items-center gap-1"
                  title="Expand to Fullscreen Modal"
                >
                  <span>🗖 Enlarge</span>
                </button>
              </div>
            </div>
            <div ref={confluenceChartRef} className="flex-1 min-h-0 w-full relative" />
          </div>

          {/* Panel 2: Smart OI Net Delta & Cumulative Volume Sub-Pane */}
          <div className="flex-1 min-h-0 flex flex-col rounded bg-[#070c17] border border-[#142036] overflow-hidden">
            <div className="flex items-center justify-between border-b border-[#142036] bg-[#091122] px-2.5 py-1 text-[11px] shrink-0">
              <div className="flex items-center gap-2">
                <span className="font-black text-emerald-400">⚡ Smart OI Net Money Flow</span>
                <span className="text-slate-500 hidden sm:inline">• Bars: Net ΔOI (PE − CE) · Line: Cumulative Smart OI</span>
              </div>
              <div className="flex items-center gap-1.5">
                <button
                  onClick={() => setEnlargedPanel("delta")}
                  className="rounded bg-[#0c1424] hover:bg-slate-700 px-2 py-0.5 text-[10px] font-bold text-slate-300 border border-slate-700 flex items-center gap-1"
                  title="Expand to Fullscreen Modal"
                >
                  <span>🗖 Enlarge</span>
                </button>
              </div>
            </div>
            <div ref={deltaChartRef} className="flex-1 min-h-0 w-full relative" />
          </div>
        </div>

        {/* Right Column (4 cols): Telemetry Sub-Pane & Signal Strikes Table */}
        <div className="lg:col-span-4 flex flex-col h-full min-h-0 gap-1 overflow-hidden">
          {/* Panel 3: Multi-Line Trend & Multi-PCR Sub-Chains */}
          <div className="flex-1 min-h-0 flex flex-col rounded bg-[#070c17] border border-[#142036] overflow-hidden">
            <div className="flex items-center justify-between border-b border-[#142036] bg-[#091122] px-2.5 py-1 text-[11px] shrink-0">
              <div className="flex items-center gap-1.5">
                <button
                  onClick={() => setTelemetryTab("flow")}
                  className={`rounded px-2 py-0.5 text-[10px] font-bold ${
                    telemetryTab === "flow" ? "bg-sky-500/30 text-sky-200 border border-sky-500/60" : "text-slate-400"
                  }`}
                >
                  Trend Telemetry
                </button>
                <button
                  onClick={() => setTelemetryTab("pcr")}
                  className={`rounded px-2 py-0.5 text-[10px] font-bold ${
                    telemetryTab === "pcr" ? "bg-purple-500/30 text-purple-200 border border-purple-500/60" : "text-slate-400"
                  }`}
                >
                  Multi-PCR
                </button>
              </div>
              <button
                onClick={() => setEnlargedPanel("telemetry")}
                className="rounded bg-[#0c1424] hover:bg-slate-700 px-2 py-0.5 text-[10px] font-bold text-slate-300 border border-slate-700 flex items-center gap-1"
                title="Expand to Fullscreen Modal"
              >
                <span>🗖 Enlarge</span>
              </button>
            </div>
            <div ref={telemetryChartRef} className="flex-1 min-h-0 w-full relative" />
          </div>

          {/* Panel 4: Filtered Signal Strikes Table */}
          <div className="flex-[1.2] min-h-0 flex flex-col rounded bg-[#070c17] border border-[#142036] overflow-hidden">
            <div className="flex items-center justify-between border-b border-[#142036] bg-[#091122] px-2.5 py-1 text-[11px] shrink-0">
              <div className="flex items-center gap-2">
                <span className="font-bold text-gold-400">🛡️ Institutional Signal Strikes</span>
                <span className="text-[10px] text-slate-500 font-mono">({data?.signal_strikes?.length ?? 0} active)</span>
              </div>
              <button
                onClick={() => setEnlargedPanel("strikes")}
                className="rounded bg-[#0c1424] hover:bg-slate-700 px-2 py-0.5 text-[10px] font-bold text-slate-300 border border-slate-700 flex items-center gap-1"
                title="Expand to Fullscreen Modal"
              >
                <span>🗖 Enlarge</span>
              </button>
            </div>

            <div className="flex-1 min-h-0 overflow-auto scrollbar-thin text-[10.5px]">
              <table className="w-full border-collapse font-mono text-left">
                <thead className="sticky top-0 bg-[#091122] text-slate-400 text-[9.5px] uppercase border-b border-slate-800">
                  <tr>
                    <th className="px-2 py-1">Strike</th>
                    <th className="px-2 py-1">Signal Type</th>
                    <th className="px-2 py-1 text-right">Put OI</th>
                    <th className="px-2 py-1 text-right">Call OI</th>
                    <th className="px-2 py-1 text-right">Net Flow</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-[#101b2e]">
                  {data?.signal_strikes && data.signal_strikes.length > 0 ? (
                    data.signal_strikes.map((s) => (
                      <tr key={s.strike} className={`hover:bg-[#0c1629] ${s.is_atm ? "bg-sky-950/20" : ""}`}>
                        <td className="px-2 py-1 font-bold text-white">
                          {s.strike}
                          {s.is_atm && <span className="ml-1 text-[8.5px] text-sky-400">ATM</span>}
                        </td>
                        <td className="px-2 py-1 whitespace-nowrap text-[10px]">
                          <span className={`px-1.5 py-0.5 rounded font-bold ${
                            s.signal_type === "PUT_SUPPORT_FLOOR"
                              ? "bg-emerald-950/80 text-emerald-300 border border-emerald-500/60"
                              : s.signal_type === "CALL_RESISTANCE_CEILING"
                              ? "bg-rose-950/80 text-rose-300 border border-rose-500/60"
                              : "bg-slate-900 text-slate-300 border border-slate-700"
                          }`}>
                            {s.signal_label}
                          </span>
                        </td>
                        <td className="px-2 py-1 text-right text-emerald-400">{inr(s.put_oi)}</td>
                        <td className="px-2 py-1 text-right text-rose-400">{inr(s.call_oi)}</td>
                        <td className={`px-2 py-1 text-right font-bold ${s.net_oi_chg > 0 ? "text-emerald-400" : "text-rose-400"}`}>
                          {inr(s.net_oi_chg, true)}
                        </td>
                      </tr>
                    ))
                  ) : (
                    <tr>
                      <td colSpan={5} className="p-4 text-center text-slate-500">
                        No signal strikes in active scope.
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      </div>

      {/* ────────────────────────────────── Fullscreen Enlarge Modal ────────────────────────────────── */}
      {enlargedPanel && (
        <div className="fixed inset-0 z-50 flex flex-col bg-[#070c17]/98 backdrop-blur-xl p-4 border border-slate-700/80 shadow-2xl animate-in fade-in">
          <div className="flex items-center justify-between border-b border-slate-800 pb-2 mb-3 shrink-0">
            <div className="flex items-center gap-3">
              <span className="text-lg font-black text-gold-400">
                🗖 {enlargedPanel === "confluence"
                  ? "NIFTY Futures Price Action Confluence (Fullscreen)"
                  : enlargedPanel === "delta"
                  ? "Smart OI Net Money Flow & Cumulative Commitment (Fullscreen)"
                  : enlargedPanel === "telemetry"
                  ? "Institutional Trend Telemetry & Multi-PCR (Fullscreen)"
                  : "Institutional Signal Strikes Matrix (Fullscreen)"}
              </span>
              <span className="text-xs text-slate-400">
                Timeframe: <b className="text-white">{tf}</b> • Expiry: <b className="text-white">{expiryTab}</b>
              </span>
            </div>
            <button
              onClick={() => setEnlargedPanel(null)}
              className="rounded bg-rose-950/80 border border-rose-500/80 px-3 py-1 text-xs font-bold text-rose-300 hover:bg-rose-900 transition-colors"
            >
              ✕ Close Fullscreen [ESC]
            </button>
          </div>

          <div className="flex-1 min-h-0 w-full relative">
            {enlargedPanel === "strikes" ? (
              <div className="h-full w-full overflow-auto scrollbar-thin text-xs">
                <table className="w-full border-collapse font-mono text-left">
                  <thead className="sticky top-0 bg-[#091122] text-slate-400 text-xs uppercase border-b border-slate-800">
                    <tr>
                      <th className="px-3 py-2">Strike</th>
                      <th className="px-3 py-2">Signal Status</th>
                      <th className="px-3 py-2 text-right">Call OI</th>
                      <th className="px-3 py-2 text-right">Call ΔOI</th>
                      <th className="px-3 py-2 text-right">Put OI</th>
                      <th className="px-3 py-2 text-right">Put ΔOI</th>
                      <th className="px-3 py-2 text-right">Net Δ Flow</th>
                      <th className="px-3 py-2 text-right">Conviction</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-[#101b2e]">
                    {data?.signal_strikes.map((s) => (
                      <tr key={s.strike} className="hover:bg-[#0c1629]">
                        <td className="px-3 py-2 font-black text-white">{s.strike}</td>
                        <td className="px-3 py-2">{s.signal_label}</td>
                        <td className="px-3 py-2 text-right text-rose-300">{inr(s.call_oi)}</td>
                        <td className="px-3 py-2 text-right text-rose-400">{inr(s.call_oi_chg, true)}</td>
                        <td className="px-3 py-2 text-right text-emerald-300">{inr(s.put_oi)}</td>
                        <td className="px-3 py-2 text-right text-emerald-400">{inr(s.put_oi_chg, true)}</td>
                        <td className={`px-3 py-2 text-right font-bold ${s.net_oi_chg > 0 ? "text-emerald-400" : "text-rose-400"}`}>
                          {inr(s.net_oi_chg, true)}
                        </td>
                        <td className="px-3 py-2 text-right text-gold-400">{s.conviction}%</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <div ref={modalChartRef} className="h-full w-full relative" />
            )}
          </div>
        </div>
      )}
    </div>
  );
}
