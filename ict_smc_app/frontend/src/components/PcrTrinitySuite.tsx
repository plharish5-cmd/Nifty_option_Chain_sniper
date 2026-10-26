// PcrTrinitySuite.tsx — World-class 3-Chart Institutional PCR & Open Interest Positioning Suite.
// Surpasses commercial platforms with:
//   1. Live Put-Call Ratio (PCR-OI + PCR-Vol vs NIFTY Future)
//   2. Intraday Fresh Change in OI (Fresh PE ΔOI vs Fresh CE ΔOI vs NIFTY Future)
//   3. Cumulative Total OI (Total PE OI vs Total CE OI vs NIFTY Future with Dynamic Gap Readout)
//   4. Automated 3-Chart Institutional Alignment Engine & Checkpoint Strip
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  AreaSeries, ColorType, createChart, createSeriesMarkers, HistogramSeries, LineSeries, LineStyle,
  type IChartApi, type ISeriesApi, type LogicalRange, type SeriesMarker, type Time, type UTCTimestamp,
} from "lightweight-charts";

export interface PcrPoint {
  time: number;
  fut_close: number;
  fut_oi?: number;
  fut_oi_chg?: number;
  fut_vol?: number;
  pcr_oi: number;
  pcr_vol: number;
  call_oi: number;
  put_oi: number;
  total_oi_gap: number;
  call_oi_chg: number;
  put_oi_chg: number;
  net_oi_chg: number;
}

export interface PcrCheckpoint {
  key: string;
  label: string;
  time_str: string;
  available: boolean;
  status?: "COMPLETED" | "PENDING" | "UNAVAILABLE";
  pcr_oi: number | null;
  pcr_vol: number | null;
  fut_close: number | null;
  fut_oi?: number | null;
  fut_oi_chg?: number | null;
  net_oi_chg: number | null;
  total_oi_gap: number | null;
  delta_from_open: number | null;
  direction: string;
  shift_text: string;
}

export interface PcrTrinityData {
  alignment_state: "BULLISH_TRINITY_LOCK" | "BEARISH_TRINITY_LOCK" | "PRICE_PCR_DIVERGENCE" | "MIXED_WAIT";
  verdict_badge: string;
  verdict_summary: string;
  pcr_oi: number;
  pcr_vol: number;
  fut_close?: number;
  fut_oi?: number;
  fut_oi_chg?: number;
  fut_vol?: number;
  pcr_band: string;
  pcr_band_label: string;
  pcr_slope_30m: number;
  pcr_delta_open: number;
  chg_oi_slope: string;
  net_oi_slope_30m: number;
  total_oi_gap: number;
  total_oi_gap_delta: number;
  net_oi_chg: number;
  call_oi_chg: number;
  put_oi_chg: number;
  call_oi: number;
  put_oi: number;
  checkpoints: PcrCheckpoint[];
  selected_expiry: string;
  cur_expiry?: string;
  next_expiry?: string;
  expiries_meta?: ExpiriesMeta;
  series?: {
    cur?: PcrPoint[];
    next?: PcrPoint[];
    both?: PcrPoint[];
  };
  verdicts?: Record<string, any>;
  pcr_oi_series?: PcrPoint[];
}

export function inr(v: number | null | undefined, sign = false): string {
  if (v == null || !isFinite(v)) return "—";
  const a = Math.abs(v), s = v < 0 ? "-" : sign && v > 0 ? "+" : "";
  if (a < 0.5) return "0";
  if (a >= 1e7) return `${s}${(a / 1e7).toFixed(2)}Cr`;
  if (a >= 1e5) return `${s}${(a / 1e5).toFixed(2)}L`;
  if (a >= 1e3) return `${s}${(a / 1e3).toFixed(1)}K`;
  return `${s}${a.toFixed(0)}`;
}

export function formatIST(epoch: number): string {
  const d = new Date(epoch * 1000);
  return `${String(d.getUTCHours()).padStart(2, "0")}:${String(d.getUTCMinutes()).padStart(2, "0")}`;
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

export interface PcrTrinitySuiteProps {
  initialExpiry?: string;
  onExpiryChange?: (exp: string) => void;
  tick?: number;
  compact?: boolean;
  onClose?: () => void;
  optExpiry?: "cur" | "next" | "both";
  setOptExpiry?: (e: "cur" | "next" | "both") => void;
  futContract?: "near" | "next" | "combined";
  setFutContract?: (f: "near" | "next" | "combined") => void;
  expMeta?: ExpiriesMeta | null;
  onMetaUpdate?: (meta: ExpiriesMeta) => void;
}

export default function PcrTrinitySuite({
  initialExpiry,
  onExpiryChange,
  tick = 0,
  compact = false,
  onClose,
  optExpiry,
  setOptExpiry,
  futContract,
  setFutContract,
  expMeta,
  onMetaUpdate,
}: PcrTrinitySuiteProps) {
  // Expiry mode: "cur" (This wk) | "next" (Next wk) | "both" (Both)
  const [expiryTab, setExpiryTab] = useState<"cur" | "next" | "both">(() => {
    try {
      const saved = localStorage.getItem("ict_pcr_suite_exp");
      if (saved === "cur" || saved === "next" || saved === "both") return saved;
    } catch { /* ignore */ }
    return "both";
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

  // View layout: "stacked" (3-Pane Stacked View) vs "tabbed" (Tabbed Focus View)
  const [layoutView, setLayoutView] = useState<"stacked" | "tabbed">(() => {
    try {
      const saved = localStorage.getItem("ict_pcr_suite_layout");
      if (saved === "stacked" || saved === "tabbed") return saved;
    } catch { /* ignore */ }
    return compact ? "tabbed" : "stacked";
  });

  // In tabbed view: active chart tab 1, 2, or 3
  const [activeTab, setActiveTab] = useState<1 | 2 | 3>(1);

  // Toggle for Vol PCR line on Chart 1
  const [showVolPcr, setShowVolPcr] = useState<boolean>(() => {
    try {
      return localStorage.getItem("ict_pcr_show_vol") !== "0";
    } catch {
      return true;
    }
  });

  const [data, setData] = useState<PcrTrinityData | null>(null);
  const [busy, setBusy] = useState(false);
  const [hoverPoint, setHoverPoint] = useState<PcrPoint | null>(null);

  // Chart DOM containers
  const box1 = useRef<HTMLDivElement>(null);
  const box2 = useRef<HTMLDivElement>(null);
  const box3 = useRef<HTMLDivElement>(null);

  // Chart and Series refs
  const chart1Ref = useRef<{
    ch: IChartApi;
    fut: ISeriesApi<"Line">;
    pcrOiArea: ISeriesApi<"Area">;
    pcrVol: ISeriesApi<"Line">;
  } | null>(null);

  const chart2Ref = useRef<{
    ch: IChartApi;
    fut: ISeriesApi<"Line">;
    putChg: ISeriesApi<"Line">;
    callChg: ISeriesApi<"Line">;
    spreadHist: ISeriesApi<"Histogram">;
    mk: ReturnType<typeof createSeriesMarkers<Time>>;
  } | null>(null);

  const chart3Ref = useRef<{
    ch: IChartApi;
    fut: ISeriesApi<"Line">;
    putTotal: ISeriesApi<"Line">;
    callTotal: ISeriesApi<"Line">;
    mk: ReturnType<typeof createSeriesMarkers<Time>>;
  } | null>(null);

  // Guard against sync loops
  const isSyncingTimeRef = useRef(false);

  // Fetch /api/chain/trinity
  const fetchData = useCallback(() => {
    setBusy(true);
    fetch(`/api/chain/trinity?opt_expiry=${currentOptExpiry}&fut_contract=${currentFutMode}`)
      .then((r) => r.json())
      .then((json: PcrTrinityData) => {
        if (json && json.series) {
          setData(json);
          if (json.expiries_meta && onMetaUpdate) {
            onMetaUpdate(json.expiries_meta);
          }
        }
      })
      .catch((e) => console.error("fetch pcr trinity error:", e))
      .finally(() => setBusy(false));
  }, [currentOptExpiry, currentFutMode, onMetaUpdate]);

  useEffect(() => {
    fetchData();
    const interval = window.setInterval(fetchData, 10000); // 10s live refresh
    return () => window.clearInterval(interval);
  }, [fetchData, tick]);

  // Active series based on expiry tab
  const activeSeries: PcrPoint[] = useMemo(() => {
    if (!data?.series) return [];
    if (currentOptExpiry === "both") return data.series.both || [];
    if (currentOptExpiry === "next") return data.series.next || [];
    return data.series.cur || [];
  }, [data, currentOptExpiry]);

  // Active verdict
  const activeVerdict = useMemo(() => {
    if (!data) return null;
    if (currentOptExpiry === "both" && data.verdicts?.both) return data.verdicts.both;
    if (currentOptExpiry === "next" && data.verdicts?.next) return data.verdicts.next;
    if (data.verdicts?.cur) return data.verdicts.cur;
    return data;
  }, [data, currentOptExpiry]);

  const activeMeta = expMeta || data?.expiries_meta;

  const setExpiry = (e: "cur" | "next" | "both") => {
    if (setOptExpiry) {
      setOptExpiry(e);
    } else {
      setExpiryTab(e);
      try { localStorage.setItem("ict_pcr_suite_exp", e); } catch { /* ignore */ }
    }
    if (onExpiryChange) onExpiryChange(e);
  };

  const setFutMode = (f: "near" | "next" | "combined") => {
    if (setFutContract) {
      setFutContract(f);
    } else {
      setInternalFutContract(f);
      try { localStorage.setItem("ict_writers_fut_contract", f); } catch { /* ignore */ }
    }
  };

  const setLayout = (l: "stacked" | "tabbed") => {
    setLayoutView(l);
    try { localStorage.setItem("ict_pcr_suite_layout", l); } catch { /* ignore */ }
  };

  const toggleVol = () => {
    setShowVolPcr((prev) => {
      const n = !prev;
      try { localStorage.setItem("ict_pcr_show_vol", n ? "1" : "0"); } catch { /* ignore */ }
      return n;
    });
  };

  const activeSeriesRef = useRef<PcrPoint[]>(activeSeries);
  activeSeriesRef.current = activeSeries;

  const crosshairHandlerRef = useRef<(time: number | undefined) => void>(() => {});
  crosshairHandlerRef.current = (time: number | undefined) => {
    if (!time || !activeSeriesRef.current.length) {
      setHoverPoint(null);
      return;
    }
    const series = activeSeriesRef.current;
    // Nearest timestamp lookup
    const matched = series.reduce((prev, curr) =>
      Math.abs(curr.time - time) < Math.abs(prev.time - time) ? curr : prev
    );
    if (matched && Math.abs(matched.time - time) <= 900) {
      setHoverPoint(matched);
    } else {
      setHoverPoint(null);
    }
  };

  // ────────────────────────────────────────── Init Charts ──────────────────────────────────────────
  // Helper for common chart theme
  const chartTheme = {
    layout: {
      background: { type: ColorType.Solid as const, color: "#070c17" },
      textColor: "#94a3b8",
      fontSize: 11,
      fontFamily: "Inter, system-ui, sans-serif",
    },
    grid: {
      vertLines: { color: "#111d33" },
      horzLines: { color: "#111d33" },
    },
    leftPriceScale: {
      visible: true,
      borderVisible: true,
      minimumWidth: 70,
      borderColor: "#1e293b",
      scaleMargins: { top: 0.1, bottom: 0.1 },
    },
    rightPriceScale: {
      visible: true,
      borderVisible: true,
      minimumWidth: 70,
      borderColor: "#1e293b",
      scaleMargins: { top: 0.1, bottom: 0.1 },
    },
    timeScale: {
      borderColor: "#1e293b",
      timeVisible: true,
      secondsVisible: false,
      fixLeftEdge: true,
      fixRightEdge: true,
      rightOffset: 2,
      lockVisibleTimeRangeOnResize: true,
    },
    crosshair: {
      vertLine: { color: "#38bdf8", width: 1 as const, style: LineStyle.Dashed },
      horzLine: { color: "#38bdf8", width: 1 as const, style: LineStyle.Dashed },
    },
    localization: { locale: "en-IN" },
  };

  // Initialize Chart 1 (PCR)
  useEffect(() => {
    if (!box1.current) return;
    const ch = createChart(box1.current, { ...chartTheme, autoSize: true });

    const futFmt = { type: "custom" as const, formatter: (p: number) => p.toFixed(1) };
    const fut = ch.addSeries(LineSeries, {
      priceScaleId: "left",
      color: "#94a3b8",
      lineWidth: 1,
      lineStyle: LineStyle.Dotted,
      title: "FUT",
      priceFormat: futFmt,
      priceLineVisible: false,
    });

    const pcrOiArea = ch.addSeries(AreaSeries, {
      priceScaleId: "right",
      topColor: "rgba(14, 165, 233, 0.4)",
      bottomColor: "rgba(14, 165, 233, 0.02)",
      lineColor: "#0ea5e9",
      lineWidth: 2,
      title: "PCR-OI",
      priceFormat: { type: "custom", minMove: 0.001, formatter: (p: number) => p.toFixed(3) },
    });

    const pcrVol = ch.addSeries(LineSeries, {
      priceScaleId: "right",
      color: "#f59e0b",
      lineWidth: 1,
      lineStyle: LineStyle.Dashed,
      title: "PCR-Vol",
      priceFormat: { type: "custom", minMove: 0.001, formatter: (p: number) => p.toFixed(2) },
      priceLineVisible: false,
    });

    // Horizontal PCR Reference Lines
    pcrOiArea.createPriceLine({ price: 1.50, color: "#10b981", lineWidth: 1, lineStyle: LineStyle.Dashed, axisLabelVisible: true, title: "1.50 Ext Bull" });
    pcrOiArea.createPriceLine({ price: 1.20, color: "#34d399", lineWidth: 1, lineStyle: LineStyle.Dotted, axisLabelVisible: true, title: "1.20 Bull" });
    pcrOiArea.createPriceLine({ price: 1.00, color: "#94a3b8", lineWidth: 1, lineStyle: LineStyle.Solid, axisLabelVisible: true, title: "1.00 EQ" });
    pcrOiArea.createPriceLine({ price: 0.80, color: "#f87171", lineWidth: 1, lineStyle: LineStyle.Dotted, axisLabelVisible: true, title: "0.80 Bear" });
    pcrOiArea.createPriceLine({ price: 0.50, color: "#ef4444", lineWidth: 1, lineStyle: LineStyle.Dashed, axisLabelVisible: true, title: "0.50 Ext Bear" });

    ch.subscribeCrosshairMove((param) => {
      crosshairHandlerRef.current(param.time as number | undefined);
    });

    chart1Ref.current = { ch, fut, pcrOiArea, pcrVol };

    return () => {
      ch.remove();
      chart1Ref.current = null;
    };
  }, []);

  // Initialize Chart 2 (Fresh Change in OI)
  useEffect(() => {
    if (!box2.current) return;
    const ch = createChart(box2.current, { ...chartTheme, autoSize: true });

    const futFmt = { type: "custom" as const, formatter: (p: number) => p.toFixed(1) };
    const fut = ch.addSeries(LineSeries, {
      priceScaleId: "left",
      color: "#94a3b8",
      lineWidth: 1,
      lineStyle: LineStyle.Dotted,
      title: "FUT",
      priceFormat: futFmt,
      priceLineVisible: false,
    });

    const spreadHist = ch.addSeries(HistogramSeries, {
      priceScaleId: "right",
      priceLineVisible: false,
      lastValueVisible: false,
      title: "Net Δ (PE-CE)",
      priceFormat: { type: "custom", formatter: (p: number) => inr(p, true) },
    });

    const putChg = ch.addSeries(LineSeries, {
      priceScaleId: "right",
      color: "#10b981", // Emerald Green Support
      lineWidth: 2,
      title: "Put ΔOI (Floor)",
      priceFormat: { type: "custom", formatter: (p: number) => inr(p, true) },
    });

    const callChg = ch.addSeries(LineSeries, {
      priceScaleId: "right",
      color: "#ef4444", // Crimson Red Resistance
      lineWidth: 2,
      title: "Call ΔOI (Cap)",
      priceFormat: { type: "custom", formatter: (p: number) => inr(p, true) },
    });

    // Zero reference line
    putChg.createPriceLine({ price: 0, color: "#475569", lineWidth: 1, lineStyle: LineStyle.Solid, axisLabelVisible: true, title: "0 Flat" });

    ch.subscribeCrosshairMove((param) => {
      crosshairHandlerRef.current(param.time as number | undefined);
    });

    const mk = createSeriesMarkers(putChg, []);
    chart2Ref.current = { ch, fut, putChg, callChg, spreadHist, mk };

    return () => {
      ch.remove();
      chart2Ref.current = null;
    };
  }, []);

  // Initialize Chart 3 (Cumulative Total OI)
  useEffect(() => {
    if (!box3.current) return;
    const ch = createChart(box3.current, { ...chartTheme, autoSize: true });

    const futFmt = { type: "custom" as const, formatter: (p: number) => p.toFixed(1) };
    const fut = ch.addSeries(LineSeries, {
      priceScaleId: "left",
      color: "#94a3b8",
      lineWidth: 1,
      lineStyle: LineStyle.Dotted,
      title: "FUT",
      priceFormat: futFmt,
      priceLineVisible: false,
    });

    const putTotal = ch.addSeries(LineSeries, {
      priceScaleId: "right",
      color: "#10b981", // Emerald Support
      lineWidth: 2,
      title: "Total Put OI",
      priceFormat: { type: "custom", minMove: 1, base: 100, formatter: (p: number) => inr(p, false) },
    });

    const callTotal = ch.addSeries(LineSeries, {
      priceScaleId: "right",
      color: "#ef4444", // Crimson Resistance
      lineWidth: 2,
      title: "Total Call OI",
      priceFormat: { type: "custom", minMove: 1, base: 100, formatter: (p: number) => inr(p, false) },
    });

    ch.priceScale("right").applyOptions({
      visible: true,
      autoScale: true,
      scaleMargins: { top: 0.12, bottom: 0.12 },
      borderVisible: true,
    });

    ch.subscribeCrosshairMove((param) => {
      crosshairHandlerRef.current(param.time as number | undefined);
    });

    const mk = createSeriesMarkers(putTotal, []);
    chart3Ref.current = { ch, fut, putTotal, callTotal, mk };

    return () => {
      ch.remove();
      chart3Ref.current = null;
    };
  }, []);

  // ────────────────────────────────────────── TimeScale Sync ──────────────────────────────────────────
  useEffect(() => {
    const ch1 = chart1Ref.current?.ch;
    const ch2 = chart2Ref.current?.ch;
    const ch3 = chart3Ref.current?.ch;
    if (!ch1 || !ch2 || !ch3) return;

    const syncRange = (source: IChartApi, targets: IChartApi[]) => {
      source.timeScale().subscribeVisibleLogicalRangeChange((range: LogicalRange | null) => {
        if (isSyncingTimeRef.current || !range) return;
        isSyncingTimeRef.current = true;
        for (const target of targets) {
          target.timeScale().setVisibleLogicalRange(range);
        }
        isSyncingTimeRef.current = false;
      });
    };

    syncRange(ch1, [ch2, ch3]);
    syncRange(ch2, [ch1, ch3]);
    syncRange(ch3, [ch1, ch2]);
  }, [data]);

  // ────────────────────────────────────────── Populate Data ──────────────────────────────────────────
  useEffect(() => {
    if (!activeSeries.length) return;

    // Series 1
    if (chart1Ref.current) {
      const { fut, pcrOiArea, pcrVol } = chart1Ref.current;
      fut.setData(activeSeries.map((p) => ({ time: p.time as UTCTimestamp, value: p.fut_close })));
      pcrOiArea.setData(activeSeries.map((p) => ({ time: p.time as UTCTimestamp, value: p.pcr_oi })));
      pcrVol.setData(showVolPcr ? activeSeries.map((p) => ({ time: p.time as UTCTimestamp, value: p.pcr_vol })) : []);
    }

    // Series 2
    if (chart2Ref.current) {
      const { fut, putChg, callChg, spreadHist } = chart2Ref.current;
      fut.setData(activeSeries.map((p) => ({ time: p.time as UTCTimestamp, value: p.fut_close })));
      putChg.setData(activeSeries.map((p) => ({ time: p.time as UTCTimestamp, value: p.put_oi_chg })));
      callChg.setData(activeSeries.map((p) => ({ time: p.time as UTCTimestamp, value: p.call_oi_chg })));
      spreadHist.setData(
        activeSeries.map((p) => ({
          time: p.time as UTCTimestamp,
          value: p.net_oi_chg,
          color: p.net_oi_chg >= 0 ? "rgba(16, 185, 129, 0.35)" : "rgba(239, 68, 68, 0.35)",
        }))
      );

      // Decisive PE / CE Net OI crossover & Zero-line crossing markers
      const rawMarkers: { time: UTCTimestamp; position: "aboveBar" | "belowBar"; shape: "arrowDown" | "arrowUp"; color: string; text: string; size: number; idx: number }[] = [];
      for (let i = 1; i < activeSeries.length; i++) {
        const prevNet = activeSeries[i - 1].net_oi_chg;
        const currNet = activeSeries[i].net_oi_chg;
        const nextNet = i + 1 < activeSeries.length ? activeSeries[i + 1].net_oi_chg : currNet;
        const next2Net = i + 2 < activeSeries.length ? activeSeries[i + 2].net_oi_chg : nextNet;

        const prevCall = activeSeries[i - 1].call_oi_chg;
        const currCall = activeSeries[i].call_oi_chg;
        const nextCall = i + 1 < activeSeries.length ? activeSeries[i + 1].call_oi_chg : currCall;

        const prevPut = activeSeries[i - 1].put_oi_chg;
        const currPut = activeSeries[i].put_oi_chg;
        const nextPut = i + 1 < activeSeries.length ? activeSeries[i + 1].put_oi_chg : currPut;

        // Bearish crossover: Call ΔOI crosses above Put ΔOI (net_oi_chg flips negative and persists >= 2 bars)
        if (prevNet >= 0 && currNet < 0 && nextNet < 0 && next2Net <= 0) {
          rawMarkers.push({
            time: activeSeries[i].time as UTCTimestamp,
            position: "aboveBar",
            shape: "arrowDown",
            color: "#ef4444",
            text: "🩸 CE > PE FLIP (BEARISH WRITERS IN CONTROL)",
            size: 2,
            idx: i,
          });
        }
        // Bullish crossover: Put ΔOI crosses above Call ΔOI (net_oi_chg flips positive and persists >= 2 bars)
        else if (prevNet <= 0 && currNet > 0 && nextNet > 0 && next2Net >= 0) {
          rawMarkers.push({
            time: activeSeries[i].time as UTCTimestamp,
            position: "belowBar",
            shape: "arrowUp",
            color: "#10b981",
            text: "🔥 PE > CE FLIP (BULLISH WRITERS IN CONTROL)",
            size: 2,
            idx: i,
          });
        }

        // Zero-line cross: Call Short Covering Squeeze (CE ΔOI drops below zero and stays negative)
        if (prevCall >= 0 && currCall < 0 && nextCall < 0 && currNet > 0) {
          rawMarkers.push({
            time: activeSeries[i].time as UTCTimestamp,
            position: "belowBar",
            shape: "arrowUp",
            color: "#06b6d4",
            text: "🚀 CE Δ < 0 (SHORT COVERING SQUEEZE)",
            size: 2,
            idx: i,
          });
        }
        // Zero-line cross: Put Unwinding Flush (PE ΔOI drops below zero and stays negative)
        else if (prevPut >= 0 && currPut < 0 && nextPut < 0 && currNet < 0) {
          rawMarkers.push({
            time: activeSeries[i].time as UTCTimestamp,
            position: "aboveBar",
            shape: "arrowDown",
            color: "#f59e0b",
            text: "🩸 PE Δ < 0 (PUT UNWINDING FLUSH)",
            size: 2,
            idx: i,
          });
        }
      }

      // De-bounce: If opposite flips or conflicting markers occur within 4 bars, keep the later confirmed sustained marker
      const crossoverMarkers: SeriesMarker<Time>[] = [];
      for (let m = 0; m < rawMarkers.length; m++) {
        const curr = rawMarkers[m];
        const next = m + 1 < rawMarkers.length ? rawMarkers[m + 1] : null;
        if (next && next.idx - curr.idx <= 4) {
          // Conflicting or rapid flip within 4 bars — skip the early unconfirmed whipsaw
          continue;
        }
        crossoverMarkers.push({
          time: curr.time,
          position: curr.position,
          shape: curr.shape,
          color: curr.color,
          text: curr.text,
          size: curr.size,
        });
      }
      crossoverMarkers.sort((a, b) => (a.time as number) - (b.time as number));
      chart2Ref.current.mk.setMarkers(crossoverMarkers);
    }

    // Series 3
    if (chart3Ref.current) {
      const { fut, putTotal, callTotal, mk } = chart3Ref.current;
      fut.setData(activeSeries.map((p) => ({ time: p.time as UTCTimestamp, value: p.fut_close })));
      putTotal.setData(activeSeries.map((p) => ({ time: p.time as UTCTimestamp, value: p.put_oi })));
      callTotal.setData(activeSeries.map((p) => ({ time: p.time as UTCTimestamp, value: p.call_oi })));

      // Auto-fit tightly around min/max to reveal slope and ensure both curves & labels are fully visible
      const allOis = activeSeries.flatMap((p) => [p.put_oi, p.call_oi]).filter((x) => x > 0);
      if (allOis.length) {
        const minVal = Math.min(...allOis);
        const maxVal = Math.max(...allOis);
        const span = Math.max(1000000, maxVal - minVal);
        const pad = span * 0.10;
        const minValue = Math.max(0, minVal - pad);
        const maxValue = maxVal + pad;
        chart3Ref.current.ch.priceScale("right").applyOptions({
          autoScale: true,
          scaleMargins: { top: 0.12, bottom: 0.12 },
          visible: true,
          borderVisible: true,
        });
        const unifiedProvider = () => ({
          priceRange: { minValue, maxValue },
        });
        putTotal.applyOptions({
          autoscaleInfoProvider: unifiedProvider,
        });
        callTotal.applyOptions({
          autoscaleInfoProvider: unifiedProvider,
        });
      }

      // Institutional Crossover Markers on Chart 3 (Total OI Resistance vs Support Flip)
      const totalMarkers: SeriesMarker<Time>[] = [];
      for (let i = 1; i < activeSeries.length; i++) {
        const prevGap = activeSeries[i - 1].total_oi_gap;
        const currGap = activeSeries[i].total_oi_gap;
        const nextGap = i + 1 < activeSeries.length ? activeSeries[i + 1].total_oi_gap : currGap;

        if (prevGap >= 0 && currGap < 0 && nextGap < 0) {
          totalMarkers.push({
            time: activeSeries[i].time as UTCTimestamp,
            position: "aboveBar",
            shape: "arrowDown",
            color: "#ef4444",
            text: "🩸 TOTAL CE > PE (BEARISH RESISTANCE WALL)",
            size: 2,
          });
        } else if (prevGap <= 0 && currGap > 0 && nextGap > 0) {
          totalMarkers.push({
            time: activeSeries[i].time as UTCTimestamp,
            position: "belowBar",
            shape: "arrowUp",
            color: "#10b981",
            text: "🔥 TOTAL PE > CE (BULLISH SUPPORT FLOOR)",
            size: 2,
          });
        }
      }
      totalMarkers.sort((a, b) => (a.time as number) - (b.time as number));
      mk.setMarkers(totalMarkers);
    }

    // Immediately stretch all 3 charts across 100% of container width on data arrival
    const timer = setTimeout(() => {
      chart1Ref.current?.ch.timeScale().fitContent();
      chart2Ref.current?.ch.timeScale().fitContent();
      chart3Ref.current?.ch.timeScale().fitContent();
    }, 40);

    return () => clearTimeout(timer);
  }, [activeSeries, showVolPcr]);

  // Handle container resizing (ResizeObserver + window resize + layout/tab switches)
  useEffect(() => {
    const boxes = [
      { el: box1.current, getCh: () => chart1Ref.current?.ch },
      { el: box2.current, getCh: () => chart2Ref.current?.ch },
      { el: box3.current, getCh: () => chart3Ref.current?.ch },
    ];

    const ro = new ResizeObserver((entries) => {
      for (const entry of entries) {
        const { width, height } = entry.contentRect;
        if (width > 0 && height > 0) {
          const match = boxes.find((b) => b.el === entry.target);
          const ch = match?.getCh();
          if (ch) {
            ch.applyOptions({ width, height });
            ch.timeScale().fitContent();
          }
        }
      }
    });

    for (const b of boxes) {
      if (b.el) ro.observe(b.el);
    }

    return () => ro.disconnect();
  }, []);

  // Ensure clean resizing & fitContent whenever layoutView (3-Pane vs Tabbed) or activeTab switches
  useEffect(() => {
    const timer = setTimeout(() => {
      const resizeOne = (boxEl: HTMLDivElement | null, chartRefObj: any) => {
        if (!boxEl || !chartRefObj?.ch) return;
        const w = boxEl.clientWidth;
        const h = boxEl.clientHeight;
        if (w > 0 && h > 0) {
          chartRefObj.ch.applyOptions({ width: w, height: h });
          chartRefObj.ch.timeScale().fitContent();
        }
      };
      resizeOne(box1.current, chart1Ref.current);
      resizeOne(box2.current, chart2Ref.current);
      resizeOne(box3.current, chart3Ref.current);
    }, 50);

    return () => clearTimeout(timer);
  }, [layoutView, activeTab]);

  // Current display point (either hovered point or latest point in series)
  const currentPt = hoverPoint || (activeSeries.length ? activeSeries[activeSeries.length - 1] : null);

  // Structural gap calculations
  const structuralGap = useMemo(() => {
    if (!currentPt) return { text: "—", tone: "neutral" };
    const gap = currentPt.total_oi_gap;
    const gapStr = inr(gap, true);
    const gapDelta = activeVerdict?.total_oi_gap_delta ?? 0;
    const deltaStr = inr(gapDelta, true);
    const tone = gap > 0 ? "bull" : gap < 0 ? "bear" : "neutral";
    const deltaTone = gapDelta > 0 ? "Narrowing / Expanding toward Bulls ↑" : "Narrowing / Expanding toward Bears ↓";
    return {
      gap,
      gapStr,
      deltaStr,
      deltaTone,
      tone,
      fullText: `PE - CE = ${gapStr} | ${deltaTone} (${deltaStr})`,
    };
  }, [currentPt, activeVerdict]);

  return (
    <div className="flex flex-col h-full w-full box-border px-3 bg-[#070c17] text-slate-200 select-none overflow-x-hidden">
      {/* ────────────────────────────────── Top Executive Alignment & Checkpoint Header ────────────────────────────────── */}
      <div className="flex flex-col gap-1.5 border-b border-[#172338] bg-[#091122] px-3 py-2 shadow-md">
        <div className="flex flex-wrap items-center justify-between gap-2">
          {/* 3-Chart Confluence Badge */}
          <div className="flex items-center gap-2">
            <span
              className={`flex items-center gap-1.5 rounded-lg px-2.5 py-1 text-xs font-black tracking-wide border shadow-sm ${
                activeVerdict?.alignment_state === "BULLISH_TRINITY_LOCK"
                  ? "bg-emerald-950/80 border-emerald-500/80 text-emerald-300 ring-1 ring-emerald-500/30"
                  : activeVerdict?.alignment_state === "BEARISH_TRINITY_LOCK"
                  ? "bg-rose-950/80 border-rose-500/80 text-rose-300 ring-1 ring-rose-500/30"
                  : activeVerdict?.alignment_state === "PRICE_PCR_DIVERGENCE"
                  ? "bg-amber-950/80 border-amber-500/80 text-amber-300 ring-1 ring-amber-500/30"
                  : "bg-slate-900 border-slate-700 text-slate-300"
              }`}
            >
              Intraday Momentum: {activeVerdict?.verdict_badge || "⚠️ 3-CHART ALIGNMENT INITIALIZING"}
            </span>

            {/* Regime Band Pill */}
            {activeVerdict?.pcr_band_label && (
              <span className="hidden sm:inline-flex rounded bg-[#101b30] px-2 py-0.5 text-[11px] font-semibold text-sky-300 border border-sky-500/30">
                Cumulative Band:{" "}
                {activeVerdict.alignment_state === "BULLISH_TRINITY_LOCK" && (currentPt?.pcr_oi ?? activeVerdict.pcr_oi) < 0.80
                  ? `${(currentPt?.pcr_oi ?? activeVerdict.pcr_oi).toFixed(3)} — Recovering from Call Control (<0.80) ↑`
                  : activeVerdict.alignment_state === "BEARISH_TRINITY_LOCK" && (currentPt?.pcr_oi ?? activeVerdict.pcr_oi) > 1.20
                  ? `${(currentPt?.pcr_oi ?? activeVerdict.pcr_oi).toFixed(3)} — Cooling from Put Control (>1.20) ↓`
                  : `${(currentPt?.pcr_oi ?? activeVerdict.pcr_oi).toFixed(3)} — ${activeVerdict.pcr_band_label}`}
              </span>
            )}
          </div>

          {/* Right Controls: Expiry Filter + Layout Switcher */}
          <div className="flex items-center gap-2 text-xs">
            {/* Expiry Pill */}
            <div className="flex items-center rounded bg-[#0f172a] p-0.5 border border-slate-800">
              <span className="text-[10px] uppercase font-bold text-slate-500 px-1">Options:</span>
              {(["cur", "next", "both"] as const).map((e) => (
                <button
                  key={e}
                  onClick={() => setExpiry(e)}
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

            {/* Futures Contract Pill */}
            <div className="flex items-center rounded bg-[#0f172a] p-0.5 border border-slate-800">
              <span className="text-[10px] uppercase font-bold text-slate-500 px-1">Futures:</span>
              {(["near", "next", "combined"] as const).map((f) => (
                <button
                  key={f}
                  onClick={() => setFutMode(f)}
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

            {/* Vol PCR Toggle */}
            <button
              onClick={toggleVol}
              className={`rounded px-2 py-0.5 text-[11px] font-bold border transition-all ${
                showVolPcr
                  ? "bg-amber-500/20 text-amber-300 border-amber-500/50"
                  : "bg-slate-900 text-slate-500 border-slate-800 hover:text-slate-300"
              }`}
              title="Toggle fast-reacting PCR Volume line on Chart 1"
            >
              Vol PCR
            </button>

            {/* Layout Toggle */}
            <div className="flex rounded bg-[#0f172a] p-0.5 border border-slate-800">
              <button
                onClick={() => setLayout("stacked")}
                className={`rounded px-2 py-0.5 text-[11px] font-bold transition-all ${
                  layoutView === "stacked"
                    ? "bg-emerald-500/30 text-emerald-200 border border-emerald-500/60 shadow-sm"
                    : "text-slate-400 hover:text-slate-200"
                }`}
                title="View all 3 synchronized charts stacked simultaneously"
              >
                3-Pane Stacked
              </button>
              <button
                onClick={() => setLayout("tabbed")}
                className={`rounded px-2 py-0.5 text-[11px] font-bold transition-all ${
                  layoutView === "tabbed"
                    ? "bg-emerald-500/30 text-emerald-200 border border-emerald-500/60 shadow-sm"
                    : "text-slate-400 hover:text-slate-200"
                }`}
                title="Focus on a single full-size chart at a time"
              >
                Tabbed Focus
              </button>
            </div>

            {/* Refresh / Close */}
            <button
              onClick={fetchData}
              className="text-slate-400 hover:text-white px-1.5 py-0.5 rounded bg-slate-800 text-[11px]"
              title="Refresh now"
            >
              {busy ? "⟳" : "↻"}
            </button>
            {onClose && (
              <button
                onClick={onClose}
                className="text-slate-400 hover:text-rose-400 px-1 text-sm font-bold ml-1"
                title="Close drawer"
              >
                ✕
              </button>
            )}
          </div>
        </div>

        {/* Institutional Checkpoint Strip */}
        <div className="flex items-center gap-1.5 overflow-x-auto text-[11px] py-0.5 scrollbar-none">
          <span className="text-slate-400 font-bold uppercase text-[10px] tracking-wider shrink-0 mr-1">
            Institutional Checkpoints:
          </span>
          {activeVerdict?.checkpoints && activeVerdict.checkpoints.length > 0 ? (
            (activeVerdict.checkpoints as PcrCheckpoint[]).map((cp: PcrCheckpoint) => {
              const isPending = cp.status === "PENDING" || !cp.available;
              if (isPending) {
                return (
                  <div
                    key={cp.key}
                    className="flex items-center gap-1 rounded px-2 py-0.5 border shrink-0 bg-[#0b101c]/60 border-slate-900/80 text-slate-500"
                  >
                    <span className="font-semibold text-slate-500">{cp.label}:</span>
                    <span className="font-medium text-slate-500">Pending (—)</span>
                  </div>
                );
              }
              return (
                <div
                  key={cp.key}
                  className="flex items-center gap-1 rounded px-2 py-0.5 border shrink-0 bg-[#0f172a]/90 border-slate-800 text-slate-300"
                >
                  <span className="font-semibold text-slate-400">{cp.label}:</span>
                  <span className="font-bold text-sky-300 font-mono">
                    {cp.pcr_oi != null ? cp.pcr_oi.toFixed(3) : "—"}
                  </span>
                  {cp.delta_from_open != null && cp.delta_from_open !== 0 && (
                    <span
                      className={`font-extrabold text-[10px] font-mono ${
                        cp.delta_from_open > 0 ? "text-emerald-400" : "text-rose-400"
                      }`}
                    >
                      ({cp.delta_from_open > 0 ? "+" : ""}
                      {cp.delta_from_open.toFixed(2)} {cp.direction})
                    </span>
                  )}
                </div>
              );
            })
          ) : (
            <span className="text-slate-500 text-[10px]">Loading session checkpoints...</span>
          )}
        </div>
      </div>

      {/* ────────────────────────────────── Tab Selector (Tabbed Mode Only) ────────────────────────────────── */}
      {layoutView === "tabbed" && (
        <div className="flex border-b border-[#172338] bg-[#0a1224] px-3 py-1 gap-2 text-xs">
          <button
            onClick={() => setActiveTab(1)}
            className={`flex items-center gap-1.5 rounded px-3 py-1 font-bold transition-all ${
              activeTab === 1
                ? "bg-sky-500/20 text-sky-300 border border-sky-500/50 shadow"
                : "text-slate-400 hover:text-slate-200"
            }`}
          >
            <span className="h-2 w-2 rounded-full bg-sky-400" />
            1. Put-Call Ratio (PCR-OI vs FUT)
          </button>
          <button
            onClick={() => setActiveTab(2)}
            className={`flex items-center gap-1.5 rounded px-3 py-1 font-bold transition-all ${
              activeTab === 2
                ? "bg-emerald-500/20 text-emerald-300 border border-emerald-500/50 shadow"
                : "text-slate-400 hover:text-slate-200"
            }`}
          >
            <span className="h-2 w-2 rounded-full bg-emerald-400" />
            2. Fresh Change in OI (PE Δ vs CE Δ)
          </button>
          <button
            onClick={() => setActiveTab(3)}
            className={`flex items-center gap-1.5 rounded px-3 py-1 font-bold transition-all ${
              activeTab === 3
                ? "bg-purple-500/20 text-purple-300 border border-purple-500/50 shadow"
                : "text-slate-400 hover:text-slate-200"
            }`}
          >
            <span className="h-2 w-2 rounded-full bg-purple-400" />
            3. Cumulative Total OI (PE Total vs CE Total)
          </button>
        </div>
      )}

      {/* ────────────────────────────────── Live Crosshair Telemetry Bar (Zero Chart Canvas Occlusion) ────────────────────────────────── */}
      <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1 border-b border-[#172338] bg-[#081020] px-3 py-1.5 text-xs shadow-inner shrink-0">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
          {/* Timestamp Indicator */}
          <span className="flex items-center gap-1.5 font-bold text-slate-300">
            <span className={`h-2 w-2 rounded-full ${hoverPoint ? "bg-sky-400 animate-pulse" : "bg-emerald-400"}`} />
            <span className="font-mono text-white text-[11px]">
              {hoverPoint ? `🕒 ${formatIST(hoverPoint.time)} IST` : `⚡ LIVE ${currentPt ? formatIST(currentPt.time) : "—"} IST`}
            </span>
          </span>

          {/* FUT Price + FUT OI Δ */}
          <span className="text-[11px] text-slate-400 font-medium">
            FUT ({currentFutMode === "near" ? "Oct" : currentFutMode === "next" ? "Nov" : "Oct+Nov"}):{" "}
            <b className="text-white font-mono">{currentPt ? currentPt.fut_close.toFixed(1) : "—"}</b>
            {currentPt && currentPt.fut_oi_chg != null && (
              <span className="ml-1 text-[10px] font-mono">
                · OI Δ <b className={currentPt.fut_oi_chg >= 0 ? "text-emerald-400" : "text-rose-400"}>{inr(currentPt.fut_oi_chg, true)}</b>
              </span>
            )}
          </span>

          <span className="text-slate-700">|</span>

          {/* Chart 1: PCR Readout */}
          <span className="text-[11px] font-medium text-slate-300">
            <span className="text-sky-400 font-bold">1. PCR:</span>{" "}
            OI <b className="text-sky-300 font-mono">{currentPt ? currentPt.pcr_oi.toFixed(3) : "—"}</b>
            {showVolPcr && currentPt && (
              <span className="text-amber-300 text-[10px] ml-1 font-mono">Vol {currentPt.pcr_vol.toFixed(2)}</span>
            )}
          </span>

          <span className="text-slate-700">|</span>

          {/* Chart 2: Fresh ΔOI */}
          <span className="text-[11px] font-medium text-slate-300">
            <span className="text-emerald-400 font-bold">2. Fresh Δ:</span>{" "}
            <span className="font-mono">
              <span className="text-emerald-400">Put {inr(currentPt?.put_oi_chg, true)}</span> vs{" "}
              <span className="text-rose-400">Call {inr(currentPt?.call_oi_chg, true)}</span>
            </span>{" "}
            <span className="text-slate-400 text-[10px]">
              (Net <b className={currentPt && currentPt.net_oi_chg >= 0 ? "text-emerald-400" : "text-rose-400"}>{inr(currentPt?.net_oi_chg, true)}</b>)
            </span>
          </span>

          <span className="text-slate-700">|</span>

          {/* Chart 3: Total OI */}
          <span className="text-[11px] font-medium text-slate-300">
            <span className="text-purple-400 font-bold">3. Total:</span>{" "}
            <span className="font-mono">
              <span className="text-emerald-400">PE {inr(currentPt?.put_oi)}</span> vs{" "}
              <span className="text-rose-400">CE {inr(currentPt?.call_oi)}</span>
            </span>{" "}
            <span className="text-slate-400 text-[10px]">
              (Gap <b className={structuralGap.tone === "bull" ? "text-emerald-400" : "text-rose-400"}>{structuralGap.gapStr}</b>)
            </span>
          </span>
        </div>

        {/* Alignment State Badge */}
        {activeVerdict?.verdict_badge && (
          <span className="text-[10px] font-extrabold tracking-wide text-right shrink-0">
            <span
              className={
                activeVerdict.alignment_state === "BULLISH_TRINITY_LOCK"
                  ? "text-emerald-400"
                  : activeVerdict.alignment_state === "BEARISH_TRINITY_LOCK"
                  ? "text-rose-400"
                  : activeVerdict.alignment_state === "PRICE_PCR_DIVERGENCE"
                  ? "text-amber-400"
                  : "text-slate-400"
              }
            >
              {activeVerdict.verdict_badge}
            </span>
          </span>
        )}
      </div>

      {/* ────────────────────────────────── Main Chart Panes ────────────────────────────────── */}
      <div className="relative flex-1 min-h-0 w-full flex flex-col overflow-hidden">
        {/* ── Chart 1 Container ── */}
        <div
          className={`relative w-full border-b border-[#142036] ${
            layoutView === "stacked" ? "flex-1 min-h-[140px]" : activeTab === 1 ? "flex-1 h-full" : "hidden"
          }`}
        >
          <div className="absolute top-1 left-2 z-10 flex items-center gap-1.5 pointer-events-none text-[10px] bg-[#070c17]/80 px-2 py-0.5 rounded border border-slate-800">
            <span className="h-2 w-2 rounded-full bg-sky-400" />
            <span className="font-bold text-sky-300">Chart 1: Live Put-Call Ratio</span>
            <span className="text-slate-400 font-mono">
              [Left: FUT Dotted | Right: PCR-OI {currentPt ? currentPt.pcr_oi.toFixed(3) : ""}]
            </span>
          </div>
          <div ref={box1} className="h-full w-full" />
        </div>

        {/* ── Chart 2 Container ── */}
        <div
          className={`relative w-full border-b border-[#142036] ${
            layoutView === "stacked" ? "flex-1 min-h-[140px]" : activeTab === 2 ? "flex-1 h-full" : "hidden"
          }`}
        >
          <div className="absolute top-1 left-2 z-10 flex items-center gap-1.5 pointer-events-none text-[10px] bg-[#070c17]/80 px-2 py-0.5 rounded border border-slate-800">
            <span className="h-2 w-2 rounded-full bg-emerald-400" />
            <span className="font-bold text-emerald-300">Chart 2: Fresh ΔOI (Call vs Put)</span>
            <span className="text-slate-400 font-mono">
              [Emerald: Put Δ | Crimson: Call Δ | Histogram: Net Spread]
            </span>
          </div>
          <div ref={box2} className="h-full w-full" />
        </div>

        {/* ── Chart 3 Container ── */}
        <div
          className={`relative w-full ${
            layoutView === "stacked" ? "flex-1 min-h-[140px]" : activeTab === 3 ? "flex-1 h-full" : "hidden"
          }`}
        >
          <div className="absolute top-1 left-2 z-10 flex items-center gap-1.5 pointer-events-none text-[10px] bg-[#070c17]/80 px-2 py-0.5 rounded border border-slate-800">
            <span className="h-2 w-2 rounded-full bg-purple-400" />
            <span className="font-bold text-purple-300">Chart 3: Cumulative Total OI</span>
            <span className="text-slate-400 font-mono">
              [PE Total vs CE Total | Auto-scaled Slope & Gap Focus]
            </span>
          </div>
          <div ref={box3} className="h-full w-full" />
        </div>
      </div>

      {/* ────────────────────────────────── Bottom Status / Legend Bar ────────────────────────────────── */}
      <div className="flex flex-wrap items-center justify-between border-t border-[#172338] bg-[#081020] px-3 py-1.5 text-[11px] text-slate-400">
        <div className="flex items-center gap-3">
          <span className="flex items-center gap-1">
            <span className="h-2 w-2 rounded-full bg-[#10b981]" />
            <span className="text-emerald-300 font-semibold">Put OI = Emerald Support</span>
          </span>
          <span className="flex items-center gap-1">
            <span className="h-2 w-2 rounded-full bg-[#ef4444]" />
            <span className="text-rose-300 font-semibold">Call OI = Crimson Resistance</span>
          </span>
          <span className="flex items-center gap-1">
            <span className="h-2 w-2 rounded-full bg-[#0ea5e9]" />
            <span className="text-sky-300 font-semibold">PCR-OI (Neon Cyan)</span>
          </span>
          {showVolPcr && (
            <span className="flex items-center gap-1">
              <span className="h-2 w-2 rounded-full bg-[#f59e0b]" />
              <span className="text-amber-300 font-semibold">PCR-Vol (Amber Dashed)</span>
            </span>
          )}
        </div>

        <div className="flex items-center gap-2">
          {activeVerdict?.verdict_summary && (
            <span className="font-medium text-slate-300 truncate max-w-md">
              {activeVerdict.verdict_summary}
            </span>
          )}
        </div>
      </div>
    </div>
  );
}
