// SmartOISuite.tsx — World-Class "Smart OI" Institutional Suite.
// Features:
//   1. StockMojo 4-Panel Parity Layout:
//      - Left Column (3 vertically synced panes):
//        * Top (60%): 3m NIFTY Candlesticks + clean Cyan (#38bdf8) Smart OI cumulative trend line.
//        * Middle (20%): Smart OI Diverging Net Delta Histogram (Crimson below zero, Emerald above).
//        * Bottom (20%): Candle Volume Histogram (Green if close >= open, Red if close < open).
//        * Vertically synchronized logical time range across all 3 panes.
//      - Right Column (2 stacked charts):
//        * Top: Intraday Put OI (Chg Day) vs Call OI (Chg Day) + Purple (#a855f7) PE - CE (Chg Day) + FUT.
//        * Bottom: Dual PCR (Blue #3b82f6 OI PCR + Orange #f59e0b Vol PCR) with 0.8, 1.0, 1.2 reference lines.
//        * Tab to toggle to Institutional Signal Strikes Table.
//   2. Color Theme Toggle:
//      - StockMojo: Call = Green (#22c55e), Put = Red (#ef4444), PE-CE = Purple (#a855f7).
//      - Institutional: Put = Emerald (#10b981), Call = Crimson (#ef4444), PE-CE = Purple (#a855f7).
//   3. Retail Noise & Stale Strike Filter (Deep OTM & stale strikes filtered out).
//   4. Fullscreen modal enlarge on every panel.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { inr } from "./OcLab";
import {
  CandlestickSeries, ColorType, createChart, createSeriesMarkers, HistogramSeries, LineSeries, LineStyle,
  type IChartApi, type ISeriesApi, type LogicalRange, type SeriesMarker, type Time, type UTCTimestamp,
} from "lightweight-charts";

export function formatInrUnits(v: number | null | undefined, sign = true): string {
  if (v == null || !isFinite(v)) return "—";
  const a = Math.abs(v);
  const s = v < 0 ? "-" : sign && v > 0 ? "+" : "";
  if (a < 0.5) return "0";
  if (a >= 1e7) return `${s}${(a / 1e7).toFixed(2)}Cr`;
  if (a >= 1e5) return `${s}${(a / 1e5).toFixed(2)}L`;
  if (a >= 1e3) return `${s}${(a / 1e3).toFixed(1)}K`;
  return `${s}${a.toFixed(0)}`;
}

export function formatChartTime(t: number): string {
  const d = new Date(t * 1000);
  const hh = String(d.getUTCHours()).padStart(2, "0");
  const mm = String(d.getUTCMinutes()).padStart(2, "0");
  return `${hh}:${mm} IST`;
}

export function getOptionVolumeGuidance(callVol: number, putVol: number, smartOiDelta: number): string {
  if (callVol > putVol * 1.25 && smartOiDelta > 0) {
    return "💡 Meaning: Call Volume Surge (+Put Writing Support) — Heavy Call buying / Call writer short-covering driving price UP (Bullish Momentum / Hold CE).";
  }
  if (callVol > putVol * 1.25 && smartOiDelta < 0) {
    return "💡 Meaning: Aggressive Call Writing Activity — Institutions selling Calls into resistance (Bearish Cap / Watch for Rejection).";
  }
  if (putVol > callVol * 1.25 && smartOiDelta < 0) {
    return "💡 Meaning: Heavy Put Activity + Bearish Flow — Put buying / Put unwinding on downside flush (Bearish Pressure).";
  }
  if (putVol > callVol * 1.25 && smartOiDelta > 0) {
    return "💡 Meaning: Aggressive Put Writing Defense — Institutions selling Puts to build a support floor (Bullish Absorption / Dip Buy).";
  }
  return "💡 Meaning: Balanced Option Volume — Routine two-way participation; rely on Smart OI Net Flow above.";
}

// Normalizes candle series to IST wall-clock chart time (UTC + 5:30) and strips pre-market (< 09:15) / post-market (> 15:35)
export function normalizeCandlesToChart(candles: SmartOICandle[]): SmartOICandle[] {
  if (!candles || !candles.length) return [];
  const firstT = candles[0].time;
  const secFirst = firstT % 86400;
  const isRawUtc = secFirst < 25000;

  return candles
    .map((c) => {
      const chartT = isRawUtc ? c.time + 19800 : c.time;
      const istSec = chartT % 86400;
      return {
        ...c,
        time: chartT,
        isMarket: istSec >= 33300 && istSec <= 56100,
      };
    })
    .filter((c) => c.isMarket);
}

export function normalizeTimeSeriesToChart(timeSeries: SmartOITimeSeriesPoint[]): SmartOITimeSeriesPoint[] {
  if (!timeSeries || !timeSeries.length) return [];
  const firstT = timeSeries[0].time;
  const secFirst = firstT % 86400;
  const isRawUtc = secFirst < 25000;

  return timeSeries
    .map((pt) => {
      const chartT = isRawUtc ? pt.time + 19800 : pt.time;
      const istSec = chartT % 86400;
      return {
        ...pt,
        time: chartT,
        isMarket: istSec >= 33300 && istSec <= 56100,
      };
    })
    .filter((pt) => pt.isMarket);
}

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
  call_vol?: number;
  put_vol?: number;
  call_vol_delta?: number;
  put_vol_delta?: number;
  step_call_vol?: number;
  step_put_vol?: number;
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
  call_vol?: number;
  put_vol?: number;
  call_vol_delta?: number;
  put_vol_delta?: number;
  step_call_vol?: number;
  step_put_vol?: number;
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
  trinity_series?: Record<string, any[]>;
  pcr_oi_series?: any[];
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
    return "near";
  });

  const currentOptExpiry = optExpiry ?? expiryTab;
  const currentFutMode = futContract ?? internalFutContract;
  const activeMeta = expMeta;

  const [rangeMode, setRangeMode] = useState<"atm" | "fixed" | "all">(() => {
    try {
      const s = localStorage.getItem("ict_smart_oi_range");
      if (s === "all") return s;
    } catch { /* ignore */ }
    return "all";
  });

  const [span, setSpan] = useState<number>(10);

  // Color Theme state: Locked to StockMojo by default (Call = Green #22c55e, Put = Red #ef4444)
  const [colorTheme, setColorTheme] = useState<"institutional" | "stockmojo">(() => {
    try {
      const s = localStorage.getItem("ict_oi_color_theme");
      if (s === "stockmojo") return "stockmojo";
      localStorage.setItem("ict_oi_color_theme", "stockmojo");
    } catch { /* ignore */ }
    return "stockmojo";
  });

  const handleSetColorTheme = (t: "institutional" | "stockmojo") => {
    setColorTheme(t);
    try { localStorage.setItem("ict_oi_color_theme", t); } catch { /* ignore */ }
  };

  // Toggle for Smart OI trend line overlay on Candlesticks (Default = false for 100% clean candles)
  const [showSmartOiLine, setShowSmartOiLine] = useState<boolean>(false);

  // Hover telemetry state for interactive inspection
  const [hoveredCandle, setHoveredCandle] = useState<SmartOICandle | null>(null);
  const [hoveredTrinity, setHoveredTrinity] = useState<any | null>(null);

  // Right Column view tab: "charts" (StockMojo 2 stacked charts) vs "strikes" (Signal Strikes table)
  const [rightTab, setRightTab] = useState<"charts" | "strikes">("charts");

  // Fullscreen modal state
  const [enlargedPanel, setEnlargedPanel] = useState<"confluence" | "delta" | "volume" | "oichg" | "pcr" | "strikes" | null>(null);

  const [data, setData] = useState<SmartOIPayload | null>(null);
  const [busy, setBusy] = useState<boolean>(false);

  const cleanCandles = useMemo(() => {
    if (!data?.candles?.length) return [];
    const valid = normalizeCandlesToChart(data.candles);
    const map = new Map<number, SmartOICandle>();
    for (const c of valid) map.set(c.time, c);
    return Array.from(map.values()).sort((a, b) => a.time - b.time);
  }, [data?.candles]);

  const activeCandle = hoveredCandle ?? (cleanCandles.length ? cleanCandles[cleanCandles.length - 1] : null);
  const activeVols = useMemo(() => {
    if (!activeCandle) return { callVol: 0, putVol: 0 };
    const cVol = (activeCandle.call_vol_delta && activeCandle.call_vol_delta > 0)
      ? activeCandle.call_vol_delta
      : ((activeCandle.step_call_vol && activeCandle.step_call_vol > 0) ? activeCandle.step_call_vol : Math.round(activeCandle.volume * 0.45));
    const pVol = (activeCandle.put_vol_delta && activeCandle.put_vol_delta > 0)
      ? activeCandle.put_vol_delta
      : ((activeCandle.step_put_vol && activeCandle.step_put_vol > 0) ? activeCandle.step_put_vol : Math.round(activeCandle.volume * 0.55));
    return { callVol: cVol, putVol: pVol };
  }, [activeCandle]);

  // Left column chart containers
  const confluenceChartRef = useRef<HTMLDivElement>(null);
  const deltaChartRef = useRef<HTMLDivElement>(null);
  const volumeChartRef = useRef<HTMLDivElement>(null);

  // Right column chart containers
  const oiChgChartRef = useRef<HTMLDivElement>(null);
  const pcrChartRef = useRef<HTMLDivElement>(null);

  // Modal chart container
  const modalChartRef = useRef<HTMLDivElement>(null);

  // Chart API refs
  const chart1Api = useRef<IChartApi | null>(null);
  const chart2Api = useRef<IChartApi | null>(null);
  const chart3Api = useRef<IChartApi | null>(null);
  const oiChgChartApi = useRef<IChartApi | null>(null);
  const pcrChartApi = useRef<IChartApi | null>(null);
  const modalChartApi = useRef<IChartApi | null>(null);

  // Series refs for dynamic theme updates
  const oiChgSeriesRef = useRef<{
    putLine: ISeriesApi<"Line">;
    callLine: ISeriesApi<"Line">;
    peCeLine: ISeriesApi<"Line">;
  } | null>(null);

  // Syncing lock guards
  const isSyncingLeftRef = useRef(false);
  const isSyncingRightRef = useRef(false);

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

  // Common chart styling theme
  const chartBaseTheme = {
    layout: {
      background: { type: ColorType.Solid as const, color: "#070c17" },
      textColor: "#94a3b8",
      fontSize: 10.5,
      fontFamily: "Inter, system-ui, sans-serif",
    },
    grid: {
      vertLines: { color: "#111d33" },
      horzLines: { color: "#111d33" },
    },
    timeScale: {
      borderColor: "#1e293b",
      timeVisible: true,
      secondsVisible: false,
      fixLeftEdge: true,
      fixRightEdge: true,
    },
    localization: { locale: "en-IN" },
  };

  // ──────────────────────────── Left Pane 1: Candlesticks + Smart OI Cumulative Line ────────────────────────────
  useEffect(() => {
    if (!confluenceChartRef.current || !data?.candles?.length) return;
    const container = confluenceChartRef.current;
    container.innerHTML = "";

    const chart = createChart(container, {
      ...chartBaseTheme,
      rightPriceScale: { borderColor: "#1e293b", visible: true },
    });
    chart1Api.current = chart;

    const candleSeries = chart.addSeries(CandlestickSeries, {
      upColor: "#10b981",
      downColor: "#ef4444",
      borderVisible: false,
      wickUpColor: "#10b981",
      wickDownColor: "#ef4444",
    });

    const validCandles = normalizeCandlesToChart(data.candles || []);
    const candleMap = new Map<number, (typeof validCandles)[0]>();
    for (const c of validCandles) {
      candleMap.set(c.time, c);
    }
    const cleanCandles = Array.from(candleMap.values()).sort((a, b) => a.time - b.time);

    candleSeries.setData(cleanCandles.map((c) => ({
      time: c.time as UTCTimestamp,
      open: c.open,
      high: c.high,
      low: c.low,
      close: c.close,
    })));

    // Clean Cyan (#38bdf8) Smart OI Cumulative Trend Line overlaid (Only if toggled ON)
    if (showSmartOiLine) {
      const smartOiLine = chart.addSeries(LineSeries, {
        priceScaleId: "smart_oi_left",
        color: "#38bdf8",
        lineWidth: 2,
        title: "Smart OI Line",
        priceLineVisible: false,
        priceFormat: { type: "custom", formatter: (p: number) => formatInrUnits(p, true) },
      });
      chart.priceScale("smart_oi_left").applyOptions({
        visible: false,
        scaleMargins: { top: 0.12, bottom: 0.12 },
      });
      smartOiLine.setData(cleanCandles.map((c) => ({
        time: c.time as UTCTimestamp,
        value: c.cum_smart_oi,
      })));
    }

    // Support Floor & Resistance Ceiling
    if (data.key_support_level > 0) {
      candleSeries.createPriceLine({
        price: data.key_support_level,
        color: "#10b981",
        lineWidth: 1,
        lineStyle: LineStyle.Dashed,
        axisLabelVisible: true,
        title: `Floor ${data.key_support_level}`,
      });
    }

    if (data.key_resistance_level > 0) {
      candleSeries.createPriceLine({
        price: data.key_resistance_level,
        color: "#ef4444",
        lineWidth: 1,
        lineStyle: LineStyle.Dashed,
        axisLabelVisible: true,
        title: `Ceiling ${data.key_resistance_level}`,
      });
    }

    // Clean indicator arrow markers (no overlapping verbose text spam!)
    const markers: SeriesMarker<Time>[] = [];
    for (const c of cleanCandles) {
      if (c.markers && c.markers.length > 0) {
        for (const m of c.markers) {
          markers.push({
            time: c.time as UTCTimestamp,
            position: m.position,
            color: m.color,
            shape: m.shape,
            text: "", // Clean arrow markers without text clutter
          });
        }
      }
    }
    if (markers.length > 0) {
      createSeriesMarkers(candleSeries, markers);
    }

    chart.subscribeCrosshairMove((param) => {
      if (!param.time) {
        setHoveredCandle(null);
        return;
      }
      const t = param.time as number;
      const found = candleMap.get(t);
      if (found) setHoveredCandle(found);
    });

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
      chart1Api.current = null;
    };
  }, [data?.candles, data?.key_support_level, data?.key_resistance_level, showSmartOiLine]);

  // ──────────────────────────── Left Pane 2: Smart OI Net Delta Diverging Histogram ────────────────────────────
  useEffect(() => {
    if (!deltaChartRef.current || !data?.candles?.length) return;
    const container = deltaChartRef.current;
    container.innerHTML = "";

    const chart = createChart(container, {
      ...chartBaseTheme,
      localization: {
        locale: "en-IN",
        priceFormatter: (p: number) => formatInrUnits(p, true),
      },
      rightPriceScale: { borderColor: "#1e293b", visible: true },
    });
    chart2Api.current = chart;

    const validCandles = normalizeCandlesToChart(data.candles || []);
    const candleMap = new Map<number, (typeof validCandles)[0]>();
    for (const c of validCandles) {
      candleMap.set(c.time, c);
    }
    const cleanCandles = Array.from(candleMap.values()).sort((a, b) => a.time - b.time);

    const histSeries = chart.addSeries(HistogramSeries, {
      priceScaleId: "right",
      priceLineVisible: true,
      priceLineColor: "#38bdf8",
      lastValueVisible: true,
      title: "Smart OI",
      priceFormat: { type: "custom", formatter: (p: number) => formatInrUnits(p, true) },
    });

    histSeries.setData(cleanCandles.map((c) => ({
      time: c.time as UTCTimestamp,
      value: c.smart_oi_delta,
      color: c.smart_oi_delta >= 0 ? "#34d399" : "#f87171",
    })));

    histSeries.createPriceLine({
      price: 0,
      color: "#475569",
      lineWidth: 1,
      lineStyle: LineStyle.Solid,
      title: "0 Flat",
    });

    chart.subscribeCrosshairMove((param) => {
      if (!param.time) {
        setHoveredCandle(null);
        return;
      }
      const t = param.time as number;
      const found = candleMap.get(t);
      if (found) setHoveredCandle(found);
    });

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
      chart2Api.current = null;
    };
  }, [data?.candles]);

  // ──────────────────────────── Left Pane 3: Paired Option Volume Histogram ────────────────────────────
  useEffect(() => {
    if (!volumeChartRef.current || !data?.candles?.length) return;
    const container = volumeChartRef.current;
    container.innerHTML = "";

    const chart = createChart(container, {
      ...chartBaseTheme,
      localization: {
        locale: "en-IN",
        priceFormatter: (p: number) => formatInrUnits(p, false),
      },
      rightPriceScale: { borderColor: "#1e293b", visible: true },
    });
    chart3Api.current = chart;

    const validCandles = normalizeCandlesToChart(data.candles || []);
    const candleMap = new Map<number, (typeof validCandles)[0]>();
    for (const c of validCandles) {
      candleMap.set(c.time, c);
    }
    const cleanCandles = Array.from(candleMap.values()).sort((a, b) => a.time - b.time);

    // Call Option Volume Histogram (Green #22c55e)
    const callVolSeries = chart.addSeries(HistogramSeries, {
      priceScaleId: "right",
      priceFormat: { type: "custom", formatter: (p: number) => formatInrUnits(p, false) },
      priceLineVisible: false,
      color: "rgba(34, 197, 94, 0.75)",
      title: "Call Vol",
    });

    callVolSeries.setData(cleanCandles.map((c) => ({
      time: c.time as UTCTimestamp,
      value: (c.call_vol_delta && c.call_vol_delta > 0) ? c.call_vol_delta : ((c.step_call_vol && c.step_call_vol > 0) ? c.step_call_vol : Math.round(c.volume * 0.45)),
    })));

    // Put Option Volume Histogram (Red/Coral #ef4444)
    const putVolSeries = chart.addSeries(HistogramSeries, {
      priceScaleId: "right",
      priceFormat: { type: "custom", formatter: (p: number) => formatInrUnits(p, false) },
      priceLineVisible: false,
      color: "rgba(239, 68, 68, 0.75)",
      title: "Put Vol",
    });

    putVolSeries.setData(cleanCandles.map((c) => ({
      time: c.time as UTCTimestamp,
      value: (c.put_vol_delta && c.put_vol_delta > 0) ? c.put_vol_delta : ((c.step_put_vol && c.step_put_vol > 0) ? c.step_put_vol : Math.round(c.volume * 0.55)),
    })));

    chart.subscribeCrosshairMove((param) => {
      if (!param.time) {
        setHoveredCandle(null);
        return;
      }
      const t = param.time as number;
      const found = candleMap.get(t);
      if (found) setHoveredCandle(found);
    });

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
      chart3Api.current = null;
    };
  }, [data?.candles]);

  // ──────────────────────────── Left Panes Synchronizer ────────────────────────────
  useEffect(() => {
    const ch1 = chart1Api.current;
    const ch2 = chart2Api.current;
    const ch3 = chart3Api.current;
    if (!ch1 || !ch2 || !ch3) return;

    const syncRange = (source: IChartApi, targets: IChartApi[]) => {
      source.timeScale().subscribeVisibleLogicalRangeChange((range: LogicalRange | null) => {
        if (isSyncingLeftRef.current || !range) return;
        isSyncingLeftRef.current = true;
        for (const target of targets) {
          try { target.timeScale().setVisibleLogicalRange(range); } catch { /* ignore */ }
        }
        isSyncingLeftRef.current = false;
      });
    };

    syncRange(ch1, [ch2, ch3]);
    syncRange(ch2, [ch1, ch3]);
    syncRange(ch3, [ch1, ch2]);
  }, [data?.candles]);

  // Active Trinity Series for Right Column
  const activeTrinitySeries: any[] = useMemo(() => {
    if (data?.trinity_series) {
      if (currentOptExpiry === "both" && data.trinity_series.both) return data.trinity_series.both;
      if (currentOptExpiry === "next" && data.trinity_series.next) return data.trinity_series.next;
      if (data.trinity_series.cur) return data.trinity_series.cur;
    }
    if (data?.pcr_oi_series?.length) return data.pcr_oi_series;
    return [];
  }, [data?.trinity_series, data?.pcr_oi_series, currentOptExpiry]);

  // ──────────────────────────── Right Pane Top: OI Change (Call vs Put) + PE-CE (Chg Day) ────────────────────────────
  useEffect(() => {
    if (!oiChgChartRef.current || !activeTrinitySeries.length || rightTab !== "charts") return;
    const container = oiChgChartRef.current;
    container.innerHTML = "";

    const chart = createChart(container, {
      ...chartBaseTheme,
      leftPriceScale: { borderColor: "#1e293b", visible: true, scaleMargins: { top: 0.1, bottom: 0.1 } },
      rightPriceScale: { borderColor: "#1e293b", visible: true, scaleMargins: { top: 0.1, bottom: 0.1 } },
    });
    oiChgChartApi.current = chart;

    const isSm = colorTheme === "stockmojo";
    const putColor = isSm ? "#ef4444" : "#10b981";
    const callColor = isSm ? "#22c55e" : "#ef4444";

    // Left Scale: NIFTY Futures Price
    const futSeries = chart.addSeries(LineSeries, {
      priceScaleId: "left",
      color: "#94a3b8",
      lineWidth: 2,
      lineStyle: LineStyle.Dotted,
      title: "Future",
      priceLineVisible: false,
      priceFormat: { type: "custom", formatter: (p: number) => p.toFixed(1) },
    });
    futSeries.setData(activeTrinitySeries.map((p) => ({ time: p.time as UTCTimestamp, value: p.fut_close })));

    // Right Scale: Put OI (Chg Day)
    const putLine = chart.addSeries(LineSeries, {
      priceScaleId: "right",
      color: putColor,
      lineWidth: 2,
      title: isSm ? "Put OI (Chg Day)" : "Put ΔOI (Floor)",
      priceFormat: { type: "custom", formatter: (p: number) => inr(p, true) },
    });
    putLine.setData(activeTrinitySeries.map((p) => ({ time: p.time as UTCTimestamp, value: p.put_oi_chg })));

    // Right Scale: Call OI (Chg Day)
    const callLine = chart.addSeries(LineSeries, {
      priceScaleId: "right",
      color: callColor,
      lineWidth: 2,
      title: isSm ? "Call OI (Chg Day)" : "Call ΔOI (Cap)",
      priceFormat: { type: "custom", formatter: (p: number) => inr(p, true) },
    });
    callLine.setData(activeTrinitySeries.map((p) => ({ time: p.time as UTCTimestamp, value: p.call_oi_chg })));

    // Right Scale: PE - CE (Chg Day) Purple Line
    const peCeLine = chart.addSeries(LineSeries, {
      priceScaleId: "right",
      color: "#a855f7",
      lineWidth: 2,
      title: "PE - CE (Chg Day)",
      priceLineVisible: false,
      priceFormat: { type: "custom", formatter: (p: number) => inr(p, true) },
    });
    peCeLine.setData(activeTrinitySeries.map((p) => ({ time: p.time as UTCTimestamp, value: p.pe_ce_diff ?? p.net_oi_chg })));

    putLine.createPriceLine({
      price: 0,
      color: "#475569",
      lineWidth: 1,
      lineStyle: LineStyle.Solid,
      title: "0 Flat",
    });

    // Crossover markers (🟢 PE > CE Bullish Crossover | 🔴 CE > PE Bearish Crossover)
    const markers: SeriesMarker<Time>[] = [];
    for (let i = 1; i < activeTrinitySeries.length; i++) {
      const prev = activeTrinitySeries[i - 1];
      const curr = activeTrinitySeries[i];
      const prevGap = (prev.pe_ce_diff ?? prev.net_oi_chg ?? (prev.put_oi_chg - prev.call_oi_chg));
      const currGap = (curr.pe_ce_diff ?? curr.net_oi_chg ?? (curr.put_oi_chg - curr.call_oi_chg));
      if (prevGap <= 0 && currGap > 0) {
        markers.push({
          time: curr.time as UTCTimestamp,
          position: "belowBar",
          color: "#22c55e",
          shape: "arrowUp",
          text: "🟢 PE > CE (Bullish Crossover)",
        });
      } else if (prevGap >= 0 && currGap < 0) {
        markers.push({
          time: curr.time as UTCTimestamp,
          position: "aboveBar",
          color: "#ef4444",
          shape: "arrowDown",
          text: "🔴 CE > PE (Bearish Crossover)",
        });
      }
    }
    if (markers.length > 0) {
      createSeriesMarkers(putLine, markers);
    }

    const trinityMap = new Map<number, any>();
    for (const p of activeTrinitySeries) {
      trinityMap.set(p.time, p);
    }
    chart.subscribeCrosshairMove((param) => {
      if (!param.time) {
        setHoveredTrinity(null);
        return;
      }
      const t = param.time as number;
      const found = trinityMap.get(t);
      if (found) setHoveredTrinity(found);
    });

    oiChgSeriesRef.current = { putLine, callLine, peCeLine };
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
      oiChgChartApi.current = null;
      oiChgSeriesRef.current = null;
    };
  }, [activeTrinitySeries, rightTab]);

  // Update line colors dynamically when colorTheme changes
  useEffect(() => {
    if (!oiChgSeriesRef.current) return;
    const isSm = colorTheme === "stockmojo";
    const putColor = isSm ? "#ef4444" : "#10b981";
    const callColor = isSm ? "#22c55e" : "#ef4444";

    oiChgSeriesRef.current.putLine.applyOptions({
      color: putColor,
      title: isSm ? "Put OI (Chg Day)" : "Put ΔOI (Floor)",
    });
    oiChgSeriesRef.current.callLine.applyOptions({
      color: callColor,
      title: isSm ? "Call OI (Chg Day)" : "Call ΔOI (Cap)",
    });
  }, [colorTheme]);

  // ──────────────────────────── Right Pane Bottom: OI PCR vs Vol PCR (Total) ────────────────────────────
  useEffect(() => {
    if (!pcrChartRef.current || !activeTrinitySeries.length || rightTab !== "charts") return;
    const container = pcrChartRef.current;
    container.innerHTML = "";

    const chart = createChart(container, {
      ...chartBaseTheme,
      leftPriceScale: { borderColor: "#1e293b", visible: true, scaleMargins: { top: 0.1, bottom: 0.1 } },
      rightPriceScale: { borderColor: "#1e293b", visible: true, scaleMargins: { top: 0.1, bottom: 0.1 } },
    });
    pcrChartApi.current = chart;

    // Left Scale: FUT Price
    const fut = chart.addSeries(LineSeries, {
      priceScaleId: "left",
      color: "#94a3b8",
      lineWidth: 1,
      lineStyle: LineStyle.Dotted,
      title: "FUT",
      priceLineVisible: false,
      priceFormat: { type: "custom", formatter: (p: number) => p.toFixed(1) },
    });
    fut.setData(activeTrinitySeries.map((p) => ({ time: p.time as UTCTimestamp, value: p.fut_close })));

    // Right Scale: Blue OI PCR
    const oiPcr = chart.addSeries(LineSeries, {
      priceScaleId: "right",
      color: "#3b82f6",
      lineWidth: 2,
      title: "OI PCR",
      priceFormat: { type: "custom", minMove: 0.001, formatter: (p: number) => p.toFixed(3) },
    });
    oiPcr.setData(activeTrinitySeries.map((p) => ({ time: p.time as UTCTimestamp, value: p.pcr_oi })));

    // Right Scale: Orange Vol PCR
    const volPcr = chart.addSeries(LineSeries, {
      priceScaleId: "right",
      color: "#f59e0b",
      lineWidth: 1,
      lineStyle: LineStyle.Dashed,
      title: "Vol PCR",
      priceLineVisible: false,
      priceFormat: { type: "custom", minMove: 0.001, formatter: (p: number) => p.toFixed(2) },
    });
    volPcr.setData(activeTrinitySeries.map((p) => ({ time: p.time as UTCTimestamp, value: p.pcr_vol })));

    // Reference lines
    oiPcr.createPriceLine({ price: 1.20, color: "#10b981", lineWidth: 1, lineStyle: LineStyle.Dotted, title: "1.20 Bull" });
    oiPcr.createPriceLine({ price: 1.00, color: "#94a3b8", lineWidth: 1, lineStyle: LineStyle.Solid, title: "1.00 EQ" });
    oiPcr.createPriceLine({ price: 0.80, color: "#ef4444", lineWidth: 1, lineStyle: LineStyle.Dotted, title: "0.80 Bear" });

    const trinityMap = new Map<number, any>();
    for (const p of activeTrinitySeries) {
      trinityMap.set(p.time, p);
    }
    chart.subscribeCrosshairMove((param) => {
      if (!param.time) {
        setHoveredTrinity(null);
        return;
      }
      const t = param.time as number;
      const found = trinityMap.get(t);
      if (found) setHoveredTrinity(found);
    });

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
      pcrChartApi.current = null;
    };
  }, [activeTrinitySeries, rightTab]);

  // Synchronize Right Panes
  useEffect(() => {
    const ch1 = oiChgChartApi.current;
    const ch2 = pcrChartApi.current;
    if (!ch1 || !ch2) return;

    const syncRange = (source: IChartApi, target: IChartApi) => {
      source.timeScale().subscribeVisibleLogicalRangeChange((range: LogicalRange | null) => {
        if (isSyncingRightRef.current || !range) return;
        isSyncingRightRef.current = true;
        try { target.timeScale().setVisibleLogicalRange(range); } catch { /* ignore */ }
        isSyncingRightRef.current = false;
      });
    };

    syncRange(ch1, ch2);
    syncRange(ch2, ch1);
  }, [activeTrinitySeries, rightTab]);

  // ──────────────────────────── Render Modal Fullscreen Chart ────────────────────────────
  useEffect(() => {
    if (!enlargedPanel || !modalChartRef.current || !data) return;
    const container = modalChartRef.current;
    container.innerHTML = "";

    const chart = createChart(container, {
      ...chartBaseTheme,
      rightPriceScale: { borderColor: "#1e293b", visible: true },
    });
    modalChartApi.current = chart;

    if (enlargedPanel === "confluence" && data.candles?.length) {
      const validCandles = normalizeCandlesToChart(data.candles || []);
      const candleMap = new Map<number, (typeof validCandles)[0]>();
      for (const c of validCandles) candleMap.set(c.time, c);
      const cleanCandles = Array.from(candleMap.values()).sort((a, b) => a.time - b.time);

      const candleSeries = chart.addSeries(CandlestickSeries, {
        upColor: "#10b981", downColor: "#ef4444", borderVisible: false, wickUpColor: "#10b981", wickDownColor: "#ef4444",
      });
      candleSeries.setData(cleanCandles.map((c) => ({ time: c.time as UTCTimestamp, open: c.open, high: c.high, low: c.low, close: c.close })));

      if (showSmartOiLine) {
        const smartOiLine = chart.addSeries(LineSeries, {
          priceScaleId: "smart_oi_modal",
          color: "#38bdf8",
          lineWidth: 2,
          title: "Smart OI",
          priceFormat: { type: "custom", formatter: (p: number) => formatInrUnits(p, true) },
        });
        chart.priceScale("smart_oi_modal").applyOptions({ visible: false });
        smartOiLine.setData(cleanCandles.map((c) => ({ time: c.time as UTCTimestamp, value: c.cum_smart_oi })));
      }
    } else if (enlargedPanel === "delta" && data.candles?.length) {
      chart.applyOptions({
        localization: {
          locale: "en-IN",
          priceFormatter: (p: number) => formatInrUnits(p, true),
        },
      });
      const validCandles = normalizeCandlesToChart(data.candles || []);
      const histSeries = chart.addSeries(HistogramSeries, {
        title: "Smart OI",
        priceLineVisible: true,
        priceLineColor: "#38bdf8",
        lastValueVisible: true,
        priceFormat: { type: "custom", formatter: (p: number) => formatInrUnits(p, true) },
      });
      histSeries.setData(validCandles.map((c) => ({ time: c.time as UTCTimestamp, value: c.smart_oi_delta, color: c.smart_oi_delta >= 0 ? "#34d399" : "#f87171" })));
    } else if (enlargedPanel === "volume" && data.candles?.length) {
      chart.applyOptions({
        localization: {
          locale: "en-IN",
          priceFormatter: (p: number) => formatInrUnits(p, false),
        },
      });
      const validCandles = normalizeCandlesToChart(data.candles || []);
      const callVolSeries = chart.addSeries(HistogramSeries, { color: "rgba(34, 197, 94, 0.75)", title: "Call Vol", priceFormat: { type: "custom", formatter: (p: number) => formatInrUnits(p, false) } });
      callVolSeries.setData(validCandles.map((c) => ({ time: c.time as UTCTimestamp, value: (c.call_vol_delta && c.call_vol_delta > 0) ? c.call_vol_delta : ((c.step_call_vol && c.step_call_vol > 0) ? c.step_call_vol : Math.round(c.volume * 0.45)) })));
      const putVolSeries = chart.addSeries(HistogramSeries, { color: "rgba(239, 68, 68, 0.75)", title: "Put Vol", priceFormat: { type: "custom", formatter: (p: number) => formatInrUnits(p, false) } });
      putVolSeries.setData(validCandles.map((c) => ({ time: c.time as UTCTimestamp, value: (c.put_vol_delta && c.put_vol_delta > 0) ? c.put_vol_delta : ((c.step_put_vol && c.step_put_vol > 0) ? c.step_put_vol : Math.round(c.volume * 0.55)) })));
    } else if (enlargedPanel === "oichg" && activeTrinitySeries.length) {
      const isSm = colorTheme === "stockmojo";
      chart.applyOptions({
        localization: {
          locale: "en-IN",
          priceFormatter: (p: number) => formatInrUnits(p, true),
        },
        leftPriceScale: { borderColor: "#1e293b", visible: true },
        rightPriceScale: { borderColor: "#1e293b", visible: true },
      });
      const fut = chart.addSeries(LineSeries, { priceScaleId: "left", color: "#94a3b8", lineWidth: 2, lineStyle: LineStyle.Dotted, title: "Future" });
      fut.setData(activeTrinitySeries.map((p) => ({ time: p.time as UTCTimestamp, value: p.fut_close })));
      const put = chart.addSeries(LineSeries, { priceScaleId: "right", color: isSm ? "#ef4444" : "#10b981", lineWidth: 2, title: "Put OI (Chg Day)", priceFormat: { type: "custom", formatter: (p: number) => formatInrUnits(p, true) } });
      put.setData(activeTrinitySeries.map((p) => ({ time: p.time as UTCTimestamp, value: p.put_oi_chg })));
      const call = chart.addSeries(LineSeries, { priceScaleId: "right", color: isSm ? "#22c55e" : "#ef4444", lineWidth: 2, title: "Call OI (Chg Day)", priceFormat: { type: "custom", formatter: (p: number) => formatInrUnits(p, true) } });
      call.setData(activeTrinitySeries.map((p) => ({ time: p.time as UTCTimestamp, value: p.call_oi_chg })));
      const peCe = chart.addSeries(LineSeries, { priceScaleId: "right", color: "#a855f7", lineWidth: 2, title: "PE - CE", priceFormat: { type: "custom", formatter: (p: number) => formatInrUnits(p, true) } });
      peCe.setData(activeTrinitySeries.map((p) => ({ time: p.time as UTCTimestamp, value: p.pe_ce_diff ?? p.net_oi_chg })));
    } else if (enlargedPanel === "pcr" && activeTrinitySeries.length) {
      chart.applyOptions({ leftPriceScale: { borderColor: "#1e293b", visible: true }, rightPriceScale: { borderColor: "#1e293b", visible: true } });
      const fut = chart.addSeries(LineSeries, { priceScaleId: "left", color: "#94a3b8", lineWidth: 1, lineStyle: LineStyle.Dotted, title: "FUT" });
      fut.setData(activeTrinitySeries.map((p) => ({ time: p.time as UTCTimestamp, value: p.fut_close })));
      const oiPcr = chart.addSeries(LineSeries, { priceScaleId: "right", color: "#3b82f6", lineWidth: 2, title: "OI PCR" });
      oiPcr.setData(activeTrinitySeries.map((p) => ({ time: p.time as UTCTimestamp, value: p.pcr_oi })));
      const volPcr = chart.addSeries(LineSeries, { priceScaleId: "right", color: "#f59e0b", lineWidth: 1, lineStyle: LineStyle.Dashed, title: "Vol PCR" });
      volPcr.setData(activeTrinitySeries.map((p) => ({ time: p.time as UTCTimestamp, value: p.pcr_vol })));
      oiPcr.createPriceLine({ price: 1.20, color: "#10b981", lineWidth: 1, lineStyle: LineStyle.Dotted, title: "1.20 Bull" });
      oiPcr.createPriceLine({ price: 1.00, color: "#94a3b8", lineWidth: 1, lineStyle: LineStyle.Solid, title: "1.00 EQ" });
      oiPcr.createPriceLine({ price: 0.80, color: "#ef4444", lineWidth: 1, lineStyle: LineStyle.Dotted, title: "0.80 Bear" });
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
  }, [enlargedPanel, data, activeTrinitySeries, colorTheme]);

  const stats = data?.stats;
  const isSm = colorTheme === "stockmojo";

  return (
    <div className="flex h-full w-full flex-col bg-[#070c17] text-slate-200 select-none overflow-hidden font-sans">
      {/* ────────────────────────────────── Top Control Bar ────────────────────────────────── */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-[#162238] bg-[#091122] px-3 py-1.5 text-[11px] shrink-0">
        <div className="flex items-center gap-2">
          <span className="font-extrabold text-gold-400">⚡ Smart OI Intelligence Suite</span>
          {modeToggle}
          <span className="text-slate-500 hidden xl:inline">
            • Retail Noise Stripped • Institutional Signal Strikes • StockMojo Parity
          </span>
        </div>

        {/* Controls: Timeframe, Expiry, Futures Mode, Range, Theme */}
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
                  ? `This Wk (${activeMeta?.cur_date || "03 Nov"})`
                  : e === "next"
                  ? `Next Wk (${activeMeta?.next_date || "10 Nov"})`
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

          {/* Color Theme Toggle */}
          <div className="flex items-center rounded bg-[#0c1424] p-0.5 border border-slate-800">
            <span className="text-[10px] uppercase font-bold text-slate-500 px-1">Theme:</span>
            <button
              onClick={() => handleSetColorTheme("stockmojo")}
              className={`rounded px-2 py-0.5 text-[11px] font-bold transition-all ${
                colorTheme === "stockmojo"
                  ? "bg-purple-500/30 text-purple-200 border border-purple-500/60 shadow-sm"
                  : "text-slate-400 hover:text-slate-200"
              }`}
              title="StockMojo (Default): Call = Green (#22c55e), Put = Red (#ef4444)"
            >
              StockMojo
            </button>
            <button
              onClick={() => handleSetColorTheme("institutional")}
              className={`rounded px-2 py-0.5 text-[11px] font-bold transition-all ${
                colorTheme === "institutional"
                  ? "bg-emerald-500/30 text-emerald-200 border border-emerald-500/60 shadow-sm"
                  : "text-slate-400 hover:text-slate-200"
              }`}
              title="Institutional: Put Support = Green, Call Resistance = Red"
            >
              Institutional
            </button>
          </div>

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
            Active Scope: <b className="text-sky-300">{rangeMode.toUpperCase()} ({tf})</b>
          </div>
        </div>
      </div>

      {/* ────────────────────────────────── Interactive Multi-Panel Grid (StockMojo 4-Panel Parity Layout) ────────────────────────────────── */}
      <div className="flex-1 min-h-0 grid grid-cols-1 lg:grid-cols-12 gap-1 p-1 overflow-hidden bg-[#050914]">
        {/* Left Column (7 cols): 3 Vertically Synced Panes (Candles + Net Delta + Volume) */}
        <div className="lg:col-span-7 flex flex-col h-full min-h-0 gap-1 overflow-hidden">
          {/* Pane 1 (Top 58% height): 3m NIFTY Futures Price + Smart OI */}
          <div className="flex-[3] min-h-0 flex flex-col rounded bg-[#070c17] border border-[#142036] overflow-hidden relative">
            <div className="flex items-center justify-between border-b border-[#142036] bg-[#091122] px-2.5 py-1 text-[11px] shrink-0">
              <div className="flex items-center gap-2">
                <span className="font-black text-sky-400">📊 1. NIFTY Futures Price + Smart OI</span>
                {hoveredCandle ? (
                  <span className="font-mono text-[10.5px] text-slate-300">
                    O: <b className="text-white">{hoveredCandle.open}</b> H: <b className="text-emerald-400">{hoveredCandle.high}</b> L: <b className="text-rose-400">{hoveredCandle.low}</b> C: <b className="text-sky-300">{hoveredCandle.close}</b>
                  </span>
                ) : (
                  <span className="text-slate-500 hidden sm:inline">• 3m Price Action</span>
                )}
              </div>
              <div className="flex items-center gap-2">
                <label className="flex items-center gap-1.5 text-[10.5px] font-semibold text-sky-400 hover:text-sky-300 cursor-pointer bg-sky-950/40 px-2 py-0.5 rounded border border-sky-800/60">
                  <input
                    type="checkbox"
                    checked={showSmartOiLine}
                    onChange={(e) => setShowSmartOiLine(e.target.checked)}
                    className="h-3.5 w-3.5 rounded border-slate-700 bg-slate-900 text-sky-500 focus:ring-0 cursor-pointer"
                  />
                  <span>☑ Show Cyan Smart OI Line</span>
                </label>
                <button
                  onClick={() => setEnlargedPanel("confluence")}
                  className="rounded bg-[#0c1424] hover:bg-slate-700 px-2 py-0.5 text-[10px] font-bold text-slate-300 border border-slate-700 flex items-center gap-1"
                  title="Expand to Fullscreen Modal"
                >
                  <span>🗖 Enlarge</span>
                </button>
              </div>
            </div>

            {/* Floating Mouse-Over Guidance Card in Pane 1 */}
            {activeCandle && (
              <div className="absolute top-8 left-2 right-2 sm:right-auto z-10 pointer-events-none rounded bg-[#070c17]/95 border border-sky-500/50 p-1.5 shadow-2xl backdrop-blur text-[10.5px]">
                <div className="flex flex-wrap items-center gap-x-2.5 gap-y-0.5 font-mono text-slate-300">
                  <span>🕒 Time: <b className="text-white">{formatChartTime(activeCandle.time)}</b></span>
                  <span className="text-slate-600">|</span>
                  <span>O: <b className="text-white">{activeCandle.open}</b> H: <b className="text-emerald-400">{activeCandle.high}</b> L: <b className="text-rose-400">{activeCandle.low}</b> C: <b className="text-sky-300">{activeCandle.close}</b></span>
                  <span className="text-slate-600">|</span>
                  <span className="text-cyan-400">Cyan Smart OI: <b>{formatInrUnits(activeCandle.cum_smart_oi, true)}</b></span>
                </div>
                <div className="mt-0.5 font-medium text-[10.5px]">
                  {activeCandle.cum_smart_oi > 0 ? (
                    <span className="text-emerald-300">🟢 Cumulative Smart OI Positive ({formatInrUnits(activeCandle.cum_smart_oi, true)}) — Intraday Put writers backing the rally (Buy Dips / Hold CE).</span>
                  ) : activeCandle.cum_smart_oi < 0 ? (
                    <span className="text-rose-300">🔴 Cumulative Smart OI Negative ({formatInrUnits(activeCandle.cum_smart_oi, true)}) — Intraday Call writers capping the market (Sell Rallies / Hold PE).</span>
                  ) : (
                    <span className="text-slate-400">⚪ Cumulative Smart OI Neutral at zero.</span>
                  )}
                </div>
              </div>
            )}
            <div ref={confluenceChartRef} className="flex-1 min-h-0 w-full relative" />
          </div>

          {/* Pane 2 (Middle 21% height): Smart OI Net Money Flow */}
          <div className="flex-1 min-h-0 flex flex-col rounded bg-[#070c17] border border-[#142036] overflow-hidden relative">
            <div className="flex items-center justify-between border-b border-[#142036] bg-[#091122] px-2.5 py-1 text-[11px] shrink-0">
              <div className="flex items-center gap-2">
                <span className="font-black text-emerald-400">⚡ 2. Smart OI Net Money Flow (3m Bar ΔPut − ΔCall)</span>
                {hoveredCandle ? (
                  <span className="font-mono text-[10.5px]">
                    Smart OI: <b className={hoveredCandle.smart_oi_delta >= 0 ? "text-[#34d399]" : "text-[#f87171]"}>{formatInrUnits(hoveredCandle.smart_oi_delta, true)}</b>
                  </span>
                ) : (
                  <span className="text-slate-400 font-mono text-[10px] hidden sm:inline">
                    🟢 Green Up (+ΔPut &gt; ΔCall) · 🔴 Red Down (+ΔCall &gt; ΔPut)
                  </span>
                )}
              </div>
              <button
                onClick={() => setEnlargedPanel("delta")}
                className="rounded bg-[#0c1424] hover:bg-slate-700 px-2 py-0.5 text-[10px] font-bold text-slate-300 border border-slate-700 flex items-center gap-1"
                title="Expand to Fullscreen Modal"
              >
                <span>🗖 Enlarge</span>
              </button>
            </div>

            {/* Floating Mouse-Over Guidance Card in Pane 2 */}
            {activeCandle && (
              <div className="absolute top-8 left-2 right-2 sm:right-auto z-10 pointer-events-none rounded bg-[#070c17]/95 border border-emerald-500/50 p-1.5 shadow-2xl backdrop-blur text-[10.5px]">
                <div className="flex flex-wrap items-center gap-x-2.5 gap-y-0.5 font-mono text-slate-300">
                  <span>🕒 Time: <b className="text-white">{formatChartTime(activeCandle.time)}</b></span>
                  <span className="text-slate-600">|</span>
                  <span>
                    Net Money Flow (ΔPut − ΔCall):{" "}
                    <b className={activeCandle.smart_oi_delta >= 0 ? "text-[#34d399]" : "text-[#f87171]"}>
                      {formatInrUnits(activeCandle.smart_oi_delta, true)}
                    </b>{" "}
                    <span className="text-[10px]">({activeCandle.smart_oi_delta >= 0 ? "🟢 GREEN UP BAR" : "🔴 RED DOWN BAR"})</span>
                  </span>
                </div>
                <div className="mt-0.5 font-medium text-[10.5px]">
                  {activeCandle.smart_oi_delta >= 1000000 ? (
                    <span className="text-emerald-300">
                      🟢 GREEN BAR UP ({formatInrUnits(activeCandle.smart_oi_delta, true)}): Put Writers added MORE than Call Writers (or Call Writers covered) in this 3m candle → BULLISH INSTITUTIONAL FLOW (Buy Dips / Hold CE).
                    </span>
                  ) : activeCandle.smart_oi_delta <= -1000000 ? (
                    <span className="text-rose-300">
                      🔴 RED BAR DOWN ({formatInrUnits(activeCandle.smart_oi_delta, true)}): Call Writers added MORE than Put Writers (or Put Writers exited) in this 3m candle → BEARISH RESISTANCE FLOW (Sell Rallies / Hold PE).
                    </span>
                  ) : (
                    <span className="text-slate-300">
                      ⚪ SMALL BAR ({formatInrUnits(activeCandle.smart_oi_delta, true)}): Neutral 3m writer flow — No aggressive institutional dominance in this candle.
                    </span>
                  )}
                </div>
              </div>
            )}
            <div ref={deltaChartRef} className="flex-1 min-h-0 w-full relative" />
          </div>

          {/* Pane 3 (Bottom 21% height): Option Volume Activity */}
          <div className="flex-1 min-h-0 flex flex-col rounded bg-[#070c17] border border-[#142036] overflow-hidden relative">
            <div className="flex items-center justify-between border-b border-[#142036] bg-[#091122] px-2.5 py-1 text-[11px] shrink-0">
              <div className="flex items-center gap-2">
                <span className="font-black text-purple-400">📊 3. Option Volume Activity (🟢 Call Vol vs 🔴 Put Vol)</span>
                {hoveredCandle ? (
                  <span className="font-mono text-[10.5px] flex items-center gap-2">
                    <span className="text-[#22c55e]">🟢 Call Vol: <b>{formatInrUnits(activeVols.callVol, false)}</b></span>
                    <span className="text-[#ef4444]">🔴 Put Vol: <b>{formatInrUnits(activeVols.putVol, false)}</b></span>
                  </span>
                ) : (
                  <span className="text-slate-400 font-mono text-[10px] hidden sm:inline">
                    🟢 Call Vol · 🔴 Put Vol
                  </span>
                )}
              </div>
              <button
                onClick={() => setEnlargedPanel("volume")}
                className="rounded bg-[#0c1424] hover:bg-slate-700 px-2 py-0.5 text-[10px] font-bold text-slate-300 border border-slate-700 flex items-center gap-1"
                title="Expand to Fullscreen Modal"
              >
                <span>🗖 Enlarge</span>
              </button>
            </div>

            {/* Floating Mouse-Over Guidance Card in Pane 3 */}
            {activeCandle && (
              <div className="absolute top-8 left-2 right-2 sm:right-auto z-10 pointer-events-none rounded bg-[#070c17]/95 border border-purple-500/50 p-1.5 shadow-2xl backdrop-blur text-[10.5px]">
                <div className="flex flex-wrap items-center gap-x-2.5 gap-y-0.5 font-mono text-slate-300">
                  <span>🕒 Time: <b className="text-white">{formatChartTime(activeCandle.time)}</b></span>
                  <span className="text-slate-600">|</span>
                  <span className="text-[#22c55e]">🟢 Call Vol: <b>{formatInrUnits(activeVols.callVol, false)}</b></span>
                  <span className="text-slate-600">|</span>
                  <span className="text-[#ef4444]">🔴 Put Vol: <b>{formatInrUnits(activeVols.putVol, false)}</b></span>
                  <span className="text-slate-600">|</span>
                  <span className="text-sky-300">Vol Ratio (P/C): <b>{(activeVols.callVol > 0 ? (activeVols.putVol / activeVols.callVol) : 1).toFixed(2)}</b></span>
                </div>
                <div className="mt-0.5 font-medium text-gold-300 text-[10.5px]">
                  {getOptionVolumeGuidance(activeVols.callVol, activeVols.putVol, activeCandle.smart_oi_delta)}
                </div>
              </div>
            )}
            <div ref={volumeChartRef} className="flex-1 min-h-0 w-full relative" />
          </div>
        </div>

        {/* Right Column (5 cols): Stacked OI Charts (StockMojo Parity) or Signal Strikes Table */}
        <div className="lg:col-span-5 flex flex-col h-full min-h-0 gap-1 overflow-hidden">
          {/* Sub-header / Tab Toggle */}
          <div className="flex items-center justify-between border-b border-[#162238] bg-[#091122] px-2.5 py-1 text-[11px] shrink-0 rounded-t">
            <div className="flex items-center gap-1.5">
              <button
                onClick={() => setRightTab("charts")}
                className={`rounded px-2.5 py-0.5 text-[10.5px] font-bold transition-all ${
                  rightTab === "charts"
                    ? "bg-sky-500/30 text-sky-200 border border-sky-500/60 shadow-sm"
                    : "text-slate-400 hover:text-slate-200"
                }`}
              >
                📊 StockMojo OI Charts
              </button>
              <button
                onClick={() => setRightTab("strikes")}
                className={`rounded px-2.5 py-0.5 text-[10.5px] font-bold transition-all ${
                  rightTab === "strikes"
                    ? "bg-gold-500/30 text-gold-200 border border-gold-500/60 shadow-sm"
                    : "text-slate-400 hover:text-slate-200"
                }`}
              >
                🛡️ Signal Strikes ({data?.signal_strikes?.length ?? 0})
              </button>
            </div>

            {rightTab === "charts" && (
              <span className="text-[10px] text-slate-400 font-mono">
                {isSm ? "Theme: StockMojo (Call=Green, Put=Red)" : "Theme: Institutional (Put=Green, Call=Red)"}
              </span>
            )}
          </div>

          {rightTab === "charts" ? (
            <div className="flex-1 min-h-0 flex flex-col gap-1 overflow-hidden">
              {/* Stacked Chart 1: OI Change (Call vs Put) + PE-CE (Chg Day) */}
              <div className="flex-1 min-h-0 flex flex-col rounded bg-[#070c17] border border-[#142036] overflow-hidden">
                <div className="flex items-center justify-between border-b border-[#142036] bg-[#091122] px-2.5 py-1 text-[11px] shrink-0">
                  <div className="flex items-center gap-2">
                    <span className="font-black text-slate-200">
                      Chart A: OI Change
                    </span>
                    {hoveredTrinity ? (
                      <span className="font-mono text-[10.5px] flex items-center gap-2">
                        <span className="text-slate-400">Fut: <b>{hoveredTrinity.fut_close?.toFixed(1)}</b></span>
                        <span className="text-[#22c55e]">🟢 Call: <b>{inr(hoveredTrinity.call_oi_chg, true)}</b></span>
                        <span className="text-[#ef4444]">🔴 Put: <b>{inr(hoveredTrinity.put_oi_chg, true)}</b></span>
                        <span className="text-[#8b5cf6]">🟣 Δ: <b>{inr(hoveredTrinity.pe_ce_diff ?? hoveredTrinity.net_oi_chg, true)}</b></span>
                      </span>
                    ) : (
                      <span className="text-slate-400 font-mono text-[10px] hidden sm:inline">
                        <span className="text-[#22c55e]">🟢 Call OI (Chg Day)</span> · <span className="text-[#ef4444]">🔴 Put OI (Chg Day)</span> · <span className="text-[#8b5cf6]">🟣 PE-CE</span>
                      </span>
                    )}
                  </div>
                  <button
                    onClick={() => setEnlargedPanel("oichg")}
                    className="rounded bg-[#0c1424] hover:bg-slate-700 px-2 py-0.5 text-[10px] font-bold text-slate-300 border border-slate-700 flex items-center gap-1"
                    title="Expand to Fullscreen Modal"
                  >
                    <span>🗖 Enlarge</span>
                  </button>
                </div>
                <div ref={oiChgChartRef} className="flex-1 min-h-0 w-full relative" />
              </div>

              {/* Stacked Chart 2: Dual PCR (OI PCR + Vol PCR) */}
              <div className="flex-1 min-h-0 flex flex-col rounded bg-[#070c17] border border-[#142036] overflow-hidden">
                <div className="flex items-center justify-between border-b border-[#142036] bg-[#091122] px-2.5 py-1 text-[11px] shrink-0">
                  <div className="flex items-center gap-2">
                    <span className="font-black text-sky-400">Chart B: Dual PCR</span>
                    {hoveredTrinity ? (
                      <span className="font-mono text-[10.5px] flex items-center gap-2">
                        <span className="text-[#3b82f6]">🔵 OI PCR: <b>{hoveredTrinity.pcr_oi?.toFixed(3)}</b></span>
                        <span className="text-[#f59e0b]">🟠 Vol PCR: <b>{hoveredTrinity.pcr_vol?.toFixed(2)}</b></span>
                      </span>
                    ) : (
                      <span className="text-slate-500 hidden sm:inline">• Blue: OI PCR · Orange: Vol PCR</span>
                    )}
                  </div>
                  <button
                    onClick={() => setEnlargedPanel("pcr")}
                    className="rounded bg-[#0c1424] hover:bg-slate-700 px-2 py-0.5 text-[10px] font-bold text-slate-300 border border-slate-700 flex items-center gap-1"
                    title="Expand to Fullscreen Modal"
                  >
                    <span>🗖 Enlarge</span>
                  </button>
                </div>
                <div ref={pcrChartRef} className="flex-1 min-h-0 w-full relative" />
              </div>
            </div>
          ) : (
            /* Panel: Filtered Signal Strikes Table */
            <div className="flex-1 min-h-0 flex flex-col rounded bg-[#070c17] border border-[#142036] overflow-hidden">
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
                          <td className={`px-2 py-1 text-right ${isSm ? "text-rose-400" : "text-emerald-400"}`}>{inr(s.put_oi)}</td>
                          <td className={`px-2 py-1 text-right ${isSm ? "text-emerald-400" : "text-rose-400"}`}>{inr(s.call_oi)}</td>
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
          )}
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
                  ? "Smart OI Net Money Flow (Fullscreen)"
                  : enlargedPanel === "volume"
                  ? "Candle Volume Histogram (Fullscreen)"
                  : enlargedPanel === "oichg"
                  ? "StockMojo OI Change: Call vs Put (Fullscreen)"
                  : enlargedPanel === "pcr"
                  ? "Dual PCR: OI PCR vs Vol PCR (Fullscreen)"
                  : "Institutional Signal Strikes Matrix (Fullscreen)"}
              </span>
              <span className="text-xs text-slate-400">
                Timeframe: <b className="text-white">{tf}</b> • Expiry: <b className="text-white">{expiryTab}</b> • Theme: <b className="text-white">{colorTheme}</b>
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
                        <td className={`px-3 py-2 text-right ${isSm ? "text-emerald-300" : "text-rose-300"}`}>{inr(s.call_oi)}</td>
                        <td className={`px-3 py-2 text-right ${isSm ? "text-emerald-400" : "text-rose-400"}`}>{inr(s.call_oi_chg, true)}</td>
                        <td className={`px-3 py-2 text-right ${isSm ? "text-rose-300" : "text-emerald-300"}`}>{inr(s.put_oi)}</td>
                        <td className={`px-3 py-2 text-right ${isSm ? "text-rose-400" : "text-emerald-400"}`}>{inr(s.put_oi_chg, true)}</td>
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