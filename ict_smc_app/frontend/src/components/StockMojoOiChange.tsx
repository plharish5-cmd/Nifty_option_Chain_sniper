// StockMojoOiChange.tsx — StockMojo Parity "OI CHANGE (CALL VS PUT)" Component
// Real-time Exchange ΔOI = Σ(current_strike_oi - yesterday_eod_close_oi)
// Features: Full contract/expiry selectors, futures overlay, crosshair hover card, theme flip.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ColorType,
  createChart,
  LineSeries,
  LineStyle,
  type IChartApi,
  type ISeriesApi,
  type Time,
  type UTCTimestamp,
} from "lightweight-charts";

export interface StockMojoPoint {
  ts: number;
  time: string;
  time_label: string;
  fut_close: number;
  call_oi_chg: number;
  put_oi_chg: number;
  call_oi_chg_cr: number;
  put_oi_chg_cr: number;
  net_diff_cr: number;
  call_oi: number;
  put_oi: number;
  pcr_oi: number;
}

export interface StockMojoPayload {
  status: string;
  opt_expiry: string;
  fut_contract: string;
  baseline_mode: string;
  expiries_meta: {
    cur_date?: string;
    next_date?: string;
    monthly_date?: string;
    cur_expiry?: string;
    next_expiry?: string;
    monthly_expiry?: string;
    near_fut?: string;
    next_fut?: string;
    combined_fut?: string;
    rollover_pct?: number;
  };
  series: StockMojoPoint[];
  latest: StockMojoPoint | null;
  checkpoints: StockMojoPoint[];
  scale: string;
}

export interface StockMojoOiChangeProps {
  tick?: number;
  onClose?: () => void;
}

export default function StockMojoOiChange({ tick = 0, onClose }: StockMojoOiChangeProps) {
  // Selector states
  const [optExpiry, setOptExpiry] = useState<"cur" | "next" | "monthly" | "both">("cur");
  const [futContract, setFutContract] = useState<"near" | "next" | "combined">("near");
  const [baselineMode, setBaselineMode] = useState<"exchange" | "intraday">("exchange");
  const [colorTheme, setColorTheme] = useState<"stockmojo" | "institutional">("stockmojo");

  // Series visibility toggles
  const [showFut, setShowFut] = useState(true);
  const [showCall, setShowCall] = useState(true);
  const [showPut, setShowPut] = useState(true);

  // Data states
  const [payload, setPayload] = useState<StockMojoPayload | null>(null);
  const [loading, setLoading] = useState(false);
  const [hoverPt, setHoverPt] = useState<StockMojoPoint | null>(null);

  // Chart DOM refs
  const chartBoxRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const futSeriesRef = useRef<ISeriesApi<"Line"> | null>(null);
  const callSeriesRef = useRef<ISeriesApi<"Line"> | null>(null);
  const putSeriesRef = useRef<ISeriesApi<"Line"> | null>(null);

  // Color mapping based on theme
  const colors = useMemo(() => {
    if (colorTheme === "stockmojo") {
      return {
        call: "#22c55e", // Green for Call in StockMojo
        put: "#ef4444",  // Red / Coral for Put in StockMojo
        fut: "#94a3b8",  // Dotted grey
      };
    }
    return {
      call: "#ef4444", // Red for Call in Institutional
      put: "#22c55e",  // Green for Put in Institutional
      fut: "#94a3b8",
    };
  }, [colorTheme]);

  // Fetch telemetry
  const fetchData = useCallback(() => {
    setLoading(true);
    const url = `/api/desk/stockmojo-oi-change?opt_expiry=${optExpiry}&fut_contract=${futContract}&baseline_mode=${baselineMode}`;
    fetch(url)
      .then((r) => r.json())
      .then((res: StockMojoPayload) => {
        if (res?.status === "ok") {
          setPayload(res);
        }
      })
      .catch((err) => {
        console.error("Failed to fetch StockMojo OI change data:", err);
      })
      .finally(() => {
        setLoading(false);
      });
  }, [optExpiry, futContract, baselineMode]);

  useEffect(() => {
    fetchData();
  }, [fetchData, tick]);

  // Initialize Lightweight Chart
  useEffect(() => {
    const el = chartBoxRef.current;
    if (!el) return;

    const ch = createChart(el, {
      layout: {
        background: { type: ColorType.Solid, color: "#070c17" },
        textColor: "#94a3b8",
        fontSize: 11,
      },
      grid: {
        vertLines: { color: "rgba(30, 41, 59, 0.45)" },
        horzLines: { color: "rgba(30, 41, 59, 0.45)" },
      },
      leftPriceScale: {
        visible: true,
        borderColor: "#1e293b",
        scaleMargins: { top: 0.15, bottom: 0.15 },
      },
      rightPriceScale: {
        visible: true,
        borderColor: "#1e293b",
        scaleMargins: { top: 0.15, bottom: 0.15 },
      },
      timeScale: {
        borderColor: "#1e293b",
        timeVisible: true,
        secondsVisible: false,
      },
      crosshair: {
        vertLine: { color: "rgba(148, 163, 184, 0.4)", width: 1, style: LineStyle.Dashed },
        horzLine: { color: "rgba(148, 163, 184, 0.4)", width: 1, style: LineStyle.Dashed },
      },
    });

    // 1. Futures price line (Left Y-Axis, Dotted Grey)
    const futS = ch.addSeries(LineSeries, {
      priceScaleId: "left",
      color: colors.fut,
      lineWidth: 2,
      lineStyle: LineStyle.Dotted,
      title: "Future",
      priceFormat: {
        type: "price",
        precision: 2,
        minMove: 0.05,
      },
    });

    // 2. Call OI Change line (Right Y-Axis, Crores)
    const callS = ch.addSeries(LineSeries, {
      priceScaleId: "right",
      color: colors.call,
      lineWidth: 2,
      title: "Call OI Change",
      priceFormat: {
        type: "custom",
        formatter: (p: number) => `${p >= 0 ? "+" : ""}${p.toFixed(2)}Cr`,
      },
    });

    // 3. Put OI Change line (Right Y-Axis, Crores)
    const putS = ch.addSeries(LineSeries, {
      priceScaleId: "right",
      color: colors.put,
      lineWidth: 2,
      title: "Put OI Change",
      priceFormat: {
        type: "custom",
        formatter: (p: number) => `${p >= 0 ? "+" : ""}${p.toFixed(2)}Cr`,
      },
    });

    chartRef.current = ch;
    futSeriesRef.current = futS;
    callSeriesRef.current = callS;
    putSeriesRef.current = putS;

    // Crosshair hover listener for floating telemetry card
    ch.subscribeCrosshairMove((param) => {
      if (!param.time || !param.point) {
        setHoverPt(null);
        return;
      }
      const t = Number(param.time);
      if (payload?.series) {
        const found = payload.series.find((p) => p.ts === t);
        if (found) {
          setHoverPt(found);
        }
      }
    });

    const handleResize = () => {
      if (chartBoxRef.current) {
        ch.applyOptions({
          width: chartBoxRef.current.clientWidth,
          height: chartBoxRef.current.clientHeight,
        });
      }
    };

    window.addEventListener("resize", handleResize);
    handleResize();

    return () => {
      window.removeEventListener("resize", handleResize);
      ch.remove();
      chartRef.current = null;
      futSeriesRef.current = null;
      callSeriesRef.current = null;
      putSeriesRef.current = null;
    };
  }, [colors]);

  // Update series data whenever payload or visibility changes
  useEffect(() => {
    if (!payload?.series || !chartRef.current) return;

    const series = payload.series;
    const cleanFut = series.map((p) => ({ time: p.ts as UTCTimestamp, value: p.fut_close }));
    const cleanCall = series.map((p) => ({ time: p.ts as UTCTimestamp, value: p.call_oi_chg_cr }));
    const cleanPut = series.map((p) => ({ time: p.ts as UTCTimestamp, value: p.put_oi_chg_cr }));

    if (futSeriesRef.current) {
      futSeriesRef.current.applyOptions({ visible: showFut });
      futSeriesRef.current.setData(showFut ? cleanFut : []);
    }
    if (callSeriesRef.current) {
      callSeriesRef.current.applyOptions({ visible: showCall, color: colors.call });
      callSeriesRef.current.setData(showCall ? cleanCall : []);
    }
    if (putSeriesRef.current) {
      putSeriesRef.current.applyOptions({ visible: showPut, color: colors.put });
      putSeriesRef.current.setData(showPut ? cleanPut : []);
    }

    chartRef.current.timeScale().fitContent();
  }, [payload, showFut, showCall, showPut, colors]);

  const latest = payload?.latest || (payload?.series && payload.series.length > 0 ? payload.series[payload.series.length - 1] : null);
  const activeHover = hoverPt || latest;
  const meta = payload?.expiries_meta;

  return (
    <div className="flex flex-col h-full w-full bg-[#070c17] text-slate-200 select-none overflow-hidden border border-slate-800 rounded-lg">
      {/* ── Top Bar: Selectors & Configuration ── */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-800 bg-[#091122] px-3 py-1.5 text-xs shrink-0">
        <div className="flex flex-wrap items-center gap-2">
          {/* Brand & Live status tag */}
          <div className="flex items-center gap-1.5 mr-1">
            <span className="font-black text-sm tracking-tight text-white flex items-center gap-1">
              <span className="text-emerald-400">📈</span> STOCKMOJO
            </span>
            <span className="text-[11px] font-bold text-slate-400 uppercase tracking-wider">
              OI Change (Call vs Put)
            </span>
            {loading && <span className="text-[10px] text-sky-400 animate-pulse">● Loading</span>}
          </div>

          {/* 1. Options Expiry Selector */}
          <div className="flex items-center rounded bg-[#0b1324] p-0.5 border border-slate-700">
            <span className="text-[10px] uppercase font-bold text-slate-500 px-1.5">Expiry:</span>
            <button
              onClick={() => setOptExpiry("cur")}
              className={`rounded px-2 py-0.5 text-[11px] font-bold transition-all ${
                optExpiry === "cur"
                  ? "bg-sky-500/30 text-sky-200 border border-sky-500/60 shadow-sm"
                  : "text-slate-400 hover:text-slate-200"
              }`}
            >
              This Wk ({meta?.cur_date || "03 Nov"})
            </button>
            <button
              onClick={() => setOptExpiry("next")}
              className={`rounded px-2 py-0.5 text-[11px] font-bold transition-all ${
                optExpiry === "next"
                  ? "bg-sky-500/30 text-sky-200 border border-sky-500/60 shadow-sm"
                  : "text-slate-400 hover:text-slate-200"
              }`}
            >
              Next Wk ({meta?.next_date || "10 Nov"})
            </button>
            <button
              onClick={() => setOptExpiry("monthly")}
              className={`rounded px-2 py-0.5 text-[11px] font-bold transition-all ${
                optExpiry === "monthly"
                  ? "bg-sky-500/30 text-sky-200 border border-sky-500/60 shadow-sm"
                  : "text-slate-400 hover:text-slate-200"
              }`}
            >
              Monthly ({meta?.monthly_date || "26 Nov"})
            </button>
            <button
              onClick={() => setOptExpiry("both")}
              className={`rounded px-2 py-0.5 text-[11px] font-bold transition-all ${
                optExpiry === "both"
                  ? "bg-sky-500/30 text-sky-200 border border-sky-500/60 shadow-sm"
                  : "text-slate-400 hover:text-slate-200"
              }`}
            >
              Both (This + Next)
            </button>
          </div>

          {/* 2. Futures Overlay Selector */}
          <div className="flex items-center rounded bg-[#0b1324] p-0.5 border border-slate-700">
            <span className="text-[10px] uppercase font-bold text-slate-500 px-1.5">FUT:</span>
            <button
              onClick={() => setFutContract("near")}
              className={`rounded px-2 py-0.5 text-[11px] font-bold transition-all ${
                futContract === "near"
                  ? "bg-indigo-500/30 text-indigo-200 border border-indigo-500/60 shadow-sm"
                  : "text-slate-400 hover:text-slate-200"
              }`}
            >
              Current FUT (Nov)
            </button>
            <button
              onClick={() => setFutContract("next")}
              className={`rounded px-2 py-0.5 text-[11px] font-bold transition-all ${
                futContract === "next"
                  ? "bg-indigo-500/30 text-indigo-200 border border-indigo-500/60 shadow-sm"
                  : "text-slate-400 hover:text-slate-200"
              }`}
            >
              Next FUT (Dec)
            </button>
            <button
              onClick={() => setFutContract("combined")}
              className={`rounded px-2 py-0.5 text-[11px] font-bold transition-all ${
                futContract === "combined"
                  ? "bg-indigo-500/30 text-indigo-200 border border-indigo-500/60 shadow-sm"
                  : "text-slate-400 hover:text-slate-200"
              }`}
            >
              Combined FUT
            </button>
          </div>

          {/* 3. Baseline Mode Toggle */}
          <div className="flex items-center rounded bg-[#0b1324] p-0.5 border border-slate-700">
            <button
              onClick={() => setBaselineMode("exchange")}
              className={`rounded px-2 py-0.5 text-[11px] font-extrabold transition-all ${
                baselineMode === "exchange"
                  ? "bg-emerald-500/30 text-emerald-300 border border-emerald-500/60 shadow-sm"
                  : "text-slate-400 hover:text-slate-200"
              }`}
              title="Exchange ΔOI = Current Strike OI - Yesterday EOD Close OI (Exact StockMojo Parity)"
            >
              📊 Exchange ΔOI (vs Prev EOD)
            </button>
            <button
              onClick={() => setBaselineMode("intraday")}
              className={`rounded px-2 py-0.5 text-[11px] font-bold transition-all ${
                baselineMode === "intraday"
                  ? "bg-amber-500/30 text-amber-200 border border-amber-500/60 shadow-sm"
                  : "text-slate-400 hover:text-slate-200"
              }`}
              title="Intraday ΔOI since first seen snapshot"
            >
              ⏱️ Intraday ΔOI
            </button>
          </div>
        </div>

        {/* Right side controls: Theme Flip & Close */}
        <div className="flex items-center gap-2">
          <button
            onClick={() => setColorTheme((t) => (t === "stockmojo" ? "institutional" : "stockmojo"))}
            className="flex items-center gap-1 rounded bg-[#0b1324] px-2 py-1 text-[11px] font-bold text-slate-300 border border-slate-700 hover:bg-slate-800 transition-all"
            title="Toggle between StockMojo and Institutional Color Themes"
          >
            🎨 {colorTheme === "stockmojo" ? "StockMojo Colors (Put=Red, Call=Green)" : "Institutional (Put=Green, Call=Red)"}
          </button>
          {onClose && (
            <button
              onClick={onClose}
              className="rounded bg-slate-800 px-2 py-0.5 text-xs text-slate-400 hover:text-white"
            >
              ✕
            </button>
          )}
        </div>
      </div>

      {/* ── Sub-Header: Interactive Legend & Real-Time Price Badges ── */}
      <div className="flex flex-wrap items-center justify-between border-b border-slate-800/80 bg-[#080e1c] px-4 py-1 text-xs shrink-0">
        <div className="flex items-center gap-4">
          {/* Futures Legend */}
          <button
            onClick={() => setShowFut(!showFut)}
            className={`flex items-center gap-1.5 font-bold transition-opacity ${showFut ? "opacity-100" : "opacity-40 line-through"}`}
          >
            <span className="text-[#94a3b8] text-base leading-none">•</span>
            <span className="text-slate-300">Future</span>
            {activeHover && <span className="font-mono text-slate-400">({activeHover.fut_close.toFixed(2)})</span>}
          </button>

          {/* Call OI Change Legend */}
          <button
            onClick={() => setShowCall(!showCall)}
            className={`flex items-center gap-1.5 font-bold transition-opacity ${showCall ? "opacity-100" : "opacity-40 line-through"}`}
          >
            <span className="text-base leading-none" style={{ color: colors.call }}>👁</span>
            <span style={{ color: colors.call }}>Call OI Change</span>
            {activeHover && (
              <span className="font-mono font-black" style={{ color: colors.call }}>
                ({activeHover.call_oi_chg_cr >= 0 ? "+" : ""}{activeHover.call_oi_chg_cr.toFixed(2)}Cr)
              </span>
            )}
          </button>

          {/* Put OI Change Legend */}
          <button
            onClick={() => setShowPut(!showPut)}
            className={`flex items-center gap-1.5 font-bold transition-opacity ${showPut ? "opacity-100" : "opacity-40 line-through"}`}
          >
            <span className="text-base leading-none" style={{ color: colors.put }}>👁</span>
            <span style={{ color: colors.put }}>Put OI Change</span>
            {activeHover && (
              <span className="font-mono font-black" style={{ color: colors.put }}>
                ({activeHover.put_oi_chg_cr >= 0 ? "+" : ""}{activeHover.put_oi_chg_cr.toFixed(2)}Cr)
              </span>
            )}
          </button>

          {/* Net (PE - CE) Difference */}
          {activeHover && (
            <div className="flex items-center gap-1 px-2 py-0.5 rounded bg-slate-900 border border-slate-800 text-[11px]">
              <span className="text-slate-400">Net (PE − CE):</span>
              <span
                className={`font-mono font-extrabold ${
                  activeHover.net_diff_cr >= 0 ? "text-emerald-400" : "text-rose-400"
                }`}
              >
                {activeHover.net_diff_cr >= 0 ? "+" : ""}{activeHover.net_diff_cr.toFixed(2)}Cr
              </span>
            </div>
          )}
        </div>

        {/* As-Of Timestamp Indicator */}
        <div className="text-[11px] text-slate-500 font-mono">
          {activeHover ? activeHover.time_label : "Waiting for live feed..."}
        </div>
      </div>

      {/* ── Chart Container with Floating Hover Card ── */}
      <div className="flex-1 min-h-[300px] w-full relative">
        <div ref={chartBoxRef} className="h-full w-full" />

        {/* Floating White/Dark StockMojo Crosshair Card on Hover */}
        {activeHover && (
          <div className="absolute top-3 left-3 z-20 pointer-events-none flex flex-col gap-1 rounded bg-[#0b1324]/95 p-2.5 shadow-xl border border-slate-700/80 backdrop-blur text-xs">
            <div className="font-mono font-bold text-slate-300 border-b border-slate-700/60 pb-1">
              📅 {activeHover.time_label}
            </div>
            <div className="flex items-center justify-between gap-4">
              <span className="text-slate-400 flex items-center gap-1">
                <span className="text-[#94a3b8]">•</span> Future:
              </span>
              <span className="font-mono font-black text-slate-200">
                {activeHover.fut_close.toLocaleString("en-IN", { minimumFractionDigits: 2 })}
              </span>
            </div>
            <div className="flex items-center justify-between gap-4">
              <span className="flex items-center gap-1" style={{ color: colors.call }}>
                • Call OI Change:
              </span>
              <span className="font-mono font-black" style={{ color: colors.call }}>
                {activeHover.call_oi_chg_cr >= 0 ? "+" : ""}{activeHover.call_oi_chg_cr.toFixed(2)}Cr
              </span>
            </div>
            <div className="flex items-center justify-between gap-4">
              <span className="flex items-center gap-1" style={{ color: colors.put }}>
                • Put OI Change:
              </span>
              <span className="font-mono font-black" style={{ color: colors.put }}>
                {activeHover.put_oi_chg_cr >= 0 ? "+" : ""}{activeHover.put_oi_chg_cr.toFixed(2)}Cr
              </span>
            </div>
            <div className="flex items-center justify-between gap-4 border-t border-slate-700/60 pt-1">
              <span className="text-slate-400">Net (PE − CE):</span>
              <span
                className={`font-mono font-black ${
                  activeHover.net_diff_cr >= 0 ? "text-emerald-400" : "text-rose-400"
                }`}
              >
                {activeHover.net_diff_cr >= 0 ? "+" : ""}{activeHover.net_diff_cr.toFixed(2)}Cr
              </span>
            </div>
          </div>
        )}
      </div>

      {/* ── Bottom Checkpoint Bar: StockMojo Session Milestones ── */}
      <div className="flex flex-wrap items-center justify-between border-t border-slate-800 bg-[#080e1c] px-3 py-1 text-[11px] shrink-0 text-slate-400">
        <div className="flex flex-wrap items-center gap-3">
          <span className="font-bold text-slate-500 uppercase tracking-wider text-[10px]">Session Checkpoints:</span>
          {payload?.checkpoints && payload.checkpoints.length > 0 ? (
            payload.checkpoints.map((ck) => (
              <div key={ck.time} className="flex items-center gap-1">
                <span className="font-mono text-slate-400 font-bold">{ck.time}:</span>
                <span style={{ color: colors.call }} className="font-mono">
                  C:{ck.call_oi_chg_cr >= 0 ? "+" : ""}{ck.call_oi_chg_cr.toFixed(1)}Cr
                </span>
                <span style={{ color: colors.put }} className="font-mono">
                  P:{ck.put_oi_chg_cr >= 0 ? "+" : ""}{ck.put_oi_chg_cr.toFixed(1)}Cr
                </span>
                <span className={`font-mono font-bold ${ck.net_diff_cr >= 0 ? "text-emerald-400" : "text-rose-400"}`}>
                  (Δ{ck.net_diff_cr >= 0 ? "+" : ""}{ck.net_diff_cr.toFixed(1)}Cr)
                </span>
              </div>
            ))
          ) : (
            <span>Computing session milestones...</span>
          )}
        </div>
        <div className="font-mono text-[10px] text-slate-500">
          Scale: 1 Cr = 1,00,00,000 shares • True Exchange Baseline
        </div>
      </div>
    </div>
  );
}
