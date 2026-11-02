// WriterMap.tsx — StockMojo 1:1 Open Interest & Writer Map Studio.
// Replaces the old cramped writer map with a full-bleed, fullscreen-capable studio.
// Features:
//   1. 3 Primary Tabs: "OI Change+Total" (Hatched increases / Hollow decreases), "OI Change" (Net delta), "Total OI"
//   2. Dual-Thumb Intraday Range Slider (09:15 AM to 03:40 PM) + 9 Quick Window Buttons (3m to 3h / All)
//   3. Interactive Replay Mode & Floating Player Dock (Step Back, Play/Pause, Step Forward, Live)
//   4. Bottom 3-Card Summary Row (OI Change, Total OI, PCR Donut with centered PCR)
//   5. Collapsible Sidebar with Expiry Selector, Min/Max Steppers, ATM Range Filter & Sentiment Gauge
//   6. 1-Click ⛶ Enlarge Fullscreen mode with ESC key support
import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";

import StrikePositioning, { type PositionHistory } from "./StrikePositioning";
import analystConfig from "../../../../analyst/analyst_config.json";

export type TabMode = "change_total" | "change" | "total";
export type ExpiryMode = "this_wk" | "next_wk" | "both";
export type RangeMode = "all" | "atm" | "5" | "10" | "20" | "custom";

export interface StrikeRow {
  strike: number;
  call_oi_total: number;
  put_oi_total: number;
  call_oi_base: number;
  put_oi_base: number;
  call_oi_chg: number;
  put_oi_chg: number;
  call_chg_pct: number;
  put_chg_pct: number;
  call_solid: number;
  call_inc_hatched: number;
  call_dec_hollow: number;
  put_solid: number;
  put_inc_hatched: number;
  put_dec_hollow: number;
  net_pe_ce_total: number;
  net_pe_ce_chg: number;
  strike_pcr: number;
}

export interface TimelinePoint {
  ts: number;
  time_label: string;
  spot: number;
}

export interface StudioSummary {
  call_oi_chg: number;
  put_oi_chg: number;
  call_oi_total: number;
  put_oi_total: number;
  pcr: number;
  pcr_chg: number;
  put_share_pct: number;
  call_share_pct: number;
  sentiment_score: number;
  sentiment_label: string;
  sentiment_sub: string;
}

export interface ExpiriesMeta {
  cur_expiry: string;
  cur_expiry_label: string;
  next_expiry: string;
  next_expiry_label: string;
  both_label: string;
  active_mode: string;
  active_expiry_str: string;
}

export interface OiStudioPayload {
  ok: boolean;
  positioning?: PositionHistory;
  message?: string;
  spot: number;
  atm_strike: number;
  max_pain: number;
  lot_size: number;
  min_strike: number;
  max_strike: number;
  all_min_strike: number;
  all_max_strike: number;
  window_start_ts: number;
  window_start_label: string;
  window_end_ts: number;
  window_end_label: string;
  last_updated: string;
  strikes: StrikeRow[];
  timeline: TimelinePoint[];
  summary: StudioSummary;
  expiries_meta: ExpiriesMeta;
}

// Formatters
function formatUnits(val: number, showLot: boolean, lotSize = 65): string {
  if (showLot) {
    const lots = Math.round(val / lotSize);
    return `${lots >= 0 ? "+" : ""}${lots.toLocaleString("en-IN")} Lots`;
  }
  const sign = val < 0 ? "-" : val > 0 ? "+" : "";
  const abs = Math.abs(val);
  if (abs >= 1e7) return `${sign}${(abs / 1e7).toFixed(2)}Cr`;
  if (abs >= 1e5) return `${sign}${(abs / 1e5).toFixed(1)}L`;
  if (abs >= 1e3) return `${sign}${(abs / 1e3).toFixed(1)}k`;
  return `${sign}${Math.round(abs)}`;
}

function formatTotal(val: number, showLot: boolean, lotSize = 65): string {
  if (showLot) {
    const lots = Math.round(val / lotSize);
    return `${lots.toLocaleString("en-IN")} Lots`;
  }
  const abs = Math.abs(val);
  if (abs >= 1e7) return `${(abs / 1e7).toFixed(2)}Cr`;
  if (abs >= 1e5) return `${(abs / 1e5).toFixed(1)}L`;
  if (abs >= 1e3) return `${(abs / 1e3).toFixed(1)}k`;
  return `${Math.round(abs)}`;
}

const QUICK_LOOKBACKS = [
  { label: "Last 3 min", mins: 3 },
  { label: "Last 5 min", mins: 5 },
  { label: "Last 10 min", mins: 10 },
  { label: "Last 15 min", mins: 15 },
  { label: "Last 30 min", mins: 30 },
  { label: "Last 1 hr", mins: 60 },
  { label: "Last 2 hr", mins: 120 },
  { label: "Last 3 hr", mins: 180 },
  { label: "All", mins: null },
];

export default function WriterMap({ replayAt }: { replayAt?: { day: string; ts: number } } = {}) {
  // Navigation & Control States
  const [positionView, setPositionView] = useState(false);
  const [positionBucket, setPositionBucket] = useState(analystConfig.oi_studio.positioning.bucket_minutes);
  const [fetchError, setFetchError] = useState("");
  const requestSeq = useRef(0);
  const [dataQuery, setDataQuery] = useState("");
  const [tab, setTab] = useState<TabMode>("change_total");
  const [expiryMode, setExpiryMode] = useState<ExpiryMode>("this_wk");
  const [rangeMode, setRangeMode] = useState<RangeMode>("10");
  const [showLot, setShowLot] = useState<boolean>(false);
  const [sidebarOpen, setSidebarOpen] = useState<boolean>(true);
  const [isFullscreen, setIsFullscreen] = useState<boolean>(false);

  // Time & Slicing States
  const [selectedStartTs, setSelectedStartTs] = useState<number | null>(null);
  const [selectedEndTs, setSelectedEndTs] = useState<number | null>(null);
  const [activeQuickWindow, setActiveQuickWindow] = useState<number | null>(null);

  // Replay Player States
  const [isReplayOn, setIsReplayOn] = useState<boolean>(false);
  const [isPlaying, setIsPlaying] = useState<boolean>(false);
  const [replayIdx, setReplayIdx] = useState<number>(-1);

  // Steppers for Custom Min / Max
  const [minStrikeInput, setMinStrikeInput] = useState<number | null>(null);
  const [maxStrikeInput, setMaxStrikeInput] = useState<number | null>(null);

  // Data & Hover States
  const [data, setData] = useState<OiStudioPayload | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [hoverStrike, setHoverStrike] = useState<StrikeRow | null>(null);
  const [mousePos, setMousePos] = useState<{ x: number; y: number } | null>(null);

  // Fullscreen ESC handler
  useEffect(() => {
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && isFullscreen) {
        setIsFullscreen(false);
      }
    };
    window.addEventListener("keydown", handleKey);
    return () => window.removeEventListener("keydown", handleKey);
  }, [isFullscreen]);

  const requestQuery = useMemo(() => {
    const q = new URLSearchParams();
    if (replayAt) q.set("day", replayAt.day);
    q.set("expiry_mode", expiryMode);
    q.set("strike_range", rangeMode);
    if (!replayAt && selectedStartTs !== null) q.set("start_ts", String(selectedStartTs));
    if (replayAt) q.set("end_ts", String(replayAt.ts));
    else if (selectedEndTs !== null) q.set("end_ts", String(selectedEndTs));
    if (minStrikeInput !== null) q.set("min_strike", String(minStrikeInput));
    if (maxStrikeInput !== null) q.set("max_strike", String(maxStrikeInput));
    if (positionView) { q.set("include_positioning", "true"); q.set("bucket_minutes", String(positionBucket)); }
    return q.toString();
  }, [expiryMode, rangeMode, selectedStartTs, selectedEndTs, minStrikeInput, maxStrikeInput, positionView, positionBucket, replayAt?.day, replayAt?.ts]);

  // Fetch telemetry from GET /api/chain/oi-studio
  const fetchData = useCallback(async () => {
    const seq = ++requestSeq.current;
    try {
      const res = await fetch(`/api/chain/oi-studio?${requestQuery}`);
      if (seq !== requestSeq.current) return;
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const json: OiStudioPayload = await res.json();
      if (seq !== requestSeq.current) return;
      if (!json.ok) throw new Error(json.message || "No recorded chain data");
      if (json.ok) {
        setFetchError("");
        setDataQuery(requestQuery);
        setData(json);
        if (minStrikeInput === null) setMinStrikeInput(json.min_strike);
        if (maxStrikeInput === null) setMaxStrikeInput(json.max_strike);
        if (json.timeline?.length && replayIdx === -1) {
          setReplayIdx(json.timeline.length - 1);
        }
      }
    } catch (err) {
      if (seq !== requestSeq.current) return;
      console.error("fetch oi studio failed:", err);
      setFetchError(err instanceof Error ? err.message : "Unable to load history");
    } finally {
      if (seq === requestSeq.current) setLoading(false);
    }
  }, [requestQuery, replayIdx]);

  // Initial and periodic fetch
  useEffect(() => {
    fetchData();
    if (!isReplayOn && !replayAt) {
      const id = window.setInterval(fetchData, 25000);
      return () => window.clearInterval(id);
    }
  }, [fetchData, isReplayOn, replayAt]);

  // Replay playback timer (advances 1 bar every 600ms)
  useEffect(() => {
    if (!isPlaying || !data?.timeline?.length) return;
    const interval = window.setInterval(() => {
      setReplayIdx((curr) => {
        if (curr >= data.timeline.length - 1) {
          setIsPlaying(false);
          return curr;
        }
        const next = curr + 1;
        setSelectedEndTs(data.timeline[next].ts);
        return next;
      });
    }, 600);
    return () => window.clearInterval(interval);
  }, [isPlaying, data?.timeline]);

  // Quick lookback click handler
  const handleQuickWindow = (mins: number | null) => {
    setActiveQuickWindow(mins);
    if (!data?.timeline?.length) return;
    const latest = data.timeline[data.timeline.length - 1].ts;
    const currentEnd = selectedEndTs || latest;
    if (mins === null) {
      setSelectedStartTs(null);
      setSelectedEndTs(null);
    } else {
      setSelectedEndTs(currentEnd);
      setSelectedStartTs(currentEnd - mins * 60);
    }
  };

  // Stepper handlers
  const handleMinStep = (delta: number) => {
    setMinStrikeInput((prev) => {
      const next = (prev ?? data?.min_strike ?? 22200) + delta;
      setRangeMode("custom");
      return next;
    });
  };

  const handleMaxStep = (delta: number) => {
    setMaxStrikeInput((prev) => {
      const next = (prev ?? data?.max_strike ?? 23200) + delta;
      setRangeMode("custom");
      return next;
    });
  };

  // Replay Step Back / Forward
  const handleStepBack = () => {
    if (!data?.timeline?.length || replayIdx <= 0) return;
    const prev = replayIdx - 1;
    setReplayIdx(prev);
    setSelectedEndTs(data.timeline[prev].ts);
  };

  const handleStepForward = () => {
    if (!data?.timeline?.length || replayIdx >= data.timeline.length - 1) return;
    const next = replayIdx + 1;
    setReplayIdx(next);
    setSelectedEndTs(data.timeline[next].ts);
  };

  const handleLiveJump = () => {
    setIsPlaying(false);
    setIsReplayOn(false);
    setSelectedStartTs(null);
    setSelectedEndTs(null);
    setActiveQuickWindow(null);
    if (data?.timeline?.length) {
      setReplayIdx(data.timeline.length - 1);
    }
  };

  const lotSize = data?.lot_size || 65;
  const strikes = data?.strikes || [];
  const spot = data?.spot || 22700;
  const atm = data?.atm_strike || 22700;
  const maxPain = data?.max_pain || 22700;
  const summary = data?.summary || {
    call_oi_chg: 0,
    put_oi_chg: 0,
    call_oi_total: 0,
    put_oi_total: 0,
    pcr: 1.0,
    pcr_chg: 1.0,
    put_share_pct: 50,
    call_share_pct: 50,
    sentiment_score: 50,
    sentiment_label: "Neutral",
    sentiment_sub: "Balanced market conditions",
  };

  // SVG Chart Geometry
  const chartHeight = isFullscreen ? 600 : 480;
  const chartWidth = 980;
  const padLeft = 60;
  const padRight = 30;
  const padTop = 40;
  const padBottom = 50;
  const plotWidth = chartWidth - padLeft - padRight;
  const plotHeight = chartHeight - padTop - padBottom;

  // Max value calculation based on active tab
  const { maxVal, minVal } = useMemo(() => {
    if (!strikes.length) return { maxVal: 1e7, minVal: 0 };
    if (tab === "change_total") {
      const m = Math.max(...strikes.flatMap((s) => [s.call_solid + s.call_inc_hatched + s.call_dec_hollow, s.put_solid + s.put_inc_hatched + s.put_dec_hollow]), 1e5);
      return { maxVal: m * 1.12, minVal: 0 };
    }
    if (tab === "change") {
      const maxC = Math.max(...strikes.flatMap((s) => [s.call_oi_chg, s.put_oi_chg]), 1e5);
      const minC = Math.min(...strikes.flatMap((s) => [s.call_oi_chg, s.put_oi_chg]), -1e5);
      const boundary = Math.max(Math.abs(maxC), Math.abs(minC));
      return { maxVal: boundary * 1.15, minVal: -boundary * 1.15 };
    }
    // Total OI
    const mT = Math.max(...strikes.flatMap((s) => [s.call_oi_total, s.put_oi_total]), 1e5);
    return { maxVal: mT * 1.12, minVal: 0 };
  }, [strikes, tab]);

  const nStrikes = strikes.length || 1;
  const colWidth = plotWidth / nStrikes;
  const barWidth = Math.max(4, Math.min(18, (colWidth - 10) / 2));

  // Y coordinate mapper
  const getY = useCallback((val: number) => {
    if (tab === "change") {
      const zeroY = padTop + plotHeight / 2;
      const scale = (plotHeight / 2) / (maxVal || 1);
      return zeroY - val * scale;
    }
    const scale = plotHeight / (maxVal || 1);
    return padTop + plotHeight - val * scale;
  }, [tab, plotHeight, padTop, maxVal]);

  const zeroLineY = tab === "change" ? padTop + plotHeight / 2 : padTop + plotHeight;

  // Spot X position calculation
  const spotX = useMemo(() => {
    if (strikes.length < 2) return padLeft + plotWidth / 2;
    const firstS = strikes[0].strike;
    const lastS = strikes[strikes.length - 1].strike;
    if (spot <= firstS) return padLeft;
    if (spot >= lastS) return padLeft + plotWidth;
    const frac = (spot - firstS) / (lastS - firstS);
    return padLeft + frac * plotWidth;
  }, [strikes, spot, padLeft, plotWidth]);

  if (replayAt && (fetchError || !data)) return <div className="p-10 text-center text-slate-300" role={fetchError ? "alert" : undefined}>{fetchError || "Loading recorded OI at the shared time…"}<p className="mt-3 text-xs">No live price, zero OI or later snapshot is being substituted.</p></div>;

  return (
    <div
      className={
        isFullscreen
          ? "fixed inset-0 z-[9999] w-screen h-screen overflow-y-auto bg-[#070c17] p-4 flex flex-col text-slate-100"
          : "flex flex-col h-full w-full bg-[#070c17] text-slate-100 overflow-y-auto min-h-[720px] p-2"
      }
    >
      {/* ──────────────── Top Super-Header / Enlarge Bar ──────────────── */}
      <div className="flex flex-wrap items-center justify-between border-b border-[#142036] pb-2 mb-2 px-1">
        <div className="flex items-center gap-3">
          <span className="text-base font-extrabold text-gold-400 tracking-wide flex items-center gap-1.5">
            <span>🎯</span>
            <span>StockMojo OI & Writer Map Studio</span>
          </span>
          <span className="text-[11px] px-2 py-0.5 rounded bg-blue-950/80 border border-blue-800/60 text-blue-300 font-mono">
            {positionView ? "NIFTY closed:" : "Spot:"} <b>{positionView ? (data?.positioning?.spot?.value?.toFixed(2) ?? "—") : spot.toFixed(2)}</b>
          </span>
          <span className="text-[11px] px-2 py-0.5 rounded bg-amber-950/80 border border-amber-800/60 text-amber-300 font-mono">
            Max Pain: <b>{maxPain}</b>
          </span>
        </div>

        <div className="flex items-center gap-2">
          {/* Replay Switch */}
          <button
            onClick={() => {
              setIsReplayOn((v) => !v);
              if (isReplayOn) handleLiveJump();
            }}
            className={`px-2.5 py-1 rounded text-[11px] font-bold border transition-all flex items-center gap-1.5 ${
              isReplayOn
                ? "bg-amber-600/30 border-amber-500 text-amber-200 shadow-sm"
                : "bg-[#0b1324] border-slate-700 text-slate-400 hover:text-slate-200"
            }`}
          >
            <span>⏱ Replay</span>
            <span className={`inline-block w-2 h-2 rounded-full ${isReplayOn ? "bg-amber-400 animate-pulse" : "bg-slate-600"}`} />
          </button>

          {/* Show Lot Switch */}
          <button
            onClick={() => setShowLot((v) => !v)}
            className={`px-2.5 py-1 rounded text-[11px] font-bold border transition-all ${
              showLot
                ? "bg-purple-950/60 border-purple-500 text-purple-200"
                : "bg-[#0b1324] border-slate-700 text-slate-400 hover:text-slate-200"
            }`}
          >
            Show Lot: <b>{showLot ? "ON (÷65)" : "OFF (Shares)"}</b>
          </button>

          {/* Live Timestamp Pill */}
          <div className="flex items-center gap-1.5 px-2.5 py-1 rounded bg-[#0b1324] border border-slate-700 font-mono text-[11px] text-slate-300">
            <span>🕒 {data?.window_end_label || "03:40 PM"} IST</span>
            <span className="inline-block w-2 h-2 rounded-full bg-emerald-400 animate-pulse" />
          </div>

          {/* ⛶ Fullscreen Enlarge Toggle */}
          <button
            onClick={() => setIsFullscreen((v) => !v)}
            className="px-3 py-1 rounded bg-blue-600 hover:bg-blue-500 text-white font-bold text-[11px] shadow-md transition-all flex items-center gap-1"
            title={isFullscreen ? "Exit Fullscreen (ESC)" : "Enlarge Fullscreen"}
          >
            <span>{isFullscreen ? "✖ Exit" : "⛶ Enlarge Fullscreen"}</span>
          </button>
        </div>
      </div>

      {/* ──────────────── Main Workspace Layout (Sidebar + Chart) ──────────────── */}
      <div className="flex flex-1 min-h-0 gap-3">
        {/* Collapsible Left Settings Sidebar */}
        {sidebarOpen ? (
          <div className="w-[230px] shrink-0 flex flex-col gap-3 rounded-lg bg-[#0b1324] border border-[#142036] p-3 text-[12px]">
            <div className="flex items-center justify-between border-b border-slate-800 pb-1.5 font-bold text-slate-300">
              <span>SETTINGS & FILTERS</span>
              <button
                onClick={() => setSidebarOpen(false)}
                className="text-slate-400 hover:text-white px-1 font-mono text-sm"
                title="Collapse Sidebar"
              >
                «
              </button>
            </div>

            {/* Expiry Selector (3-Way: This Wk, Next Wk, Both) */}
            <div className="flex flex-col gap-1">
              <span className="text-[10.5px] uppercase font-bold text-slate-400">Expiry Contract</span>
              <div className="flex flex-col gap-1">
                <button
                  onClick={() => setExpiryMode("this_wk")}
                  className={`w-full py-1 px-2 rounded text-left text-[11px] font-semibold border transition-all ${
                    expiryMode === "this_wk"
                      ? "bg-blue-600/30 border-blue-500 text-blue-200"
                      : "bg-[#070c17] border-slate-800 text-slate-400 hover:text-slate-200"
                  }`}
                >
                  {data?.expiries_meta?.cur_expiry_label || "This Wk"}
                </button>
                <button
                  onClick={() => setExpiryMode("next_wk")}
                  className={`w-full py-1 px-2 rounded text-left text-[11px] font-semibold border transition-all ${
                    expiryMode === "next_wk"
                      ? "bg-blue-600/30 border-blue-500 text-blue-200"
                      : "bg-[#070c17] border-slate-800 text-slate-400 hover:text-slate-200"
                  }`}
                >
                  {data?.expiries_meta?.next_expiry_label || "Next Wk"}
                </button>
                <button
                  onClick={() => setExpiryMode("both")}
                  className={`w-full py-1 px-2 rounded text-left text-[11px] font-semibold border transition-all ${
                    expiryMode === "both"
                      ? "bg-purple-600/30 border-purple-500 text-purple-200"
                      : "bg-[#070c17] border-slate-800 text-slate-400 hover:text-slate-200"
                  }`}
                >
                  Both (Combined Expiry)
                </button>
              </div>
            </div>

            {/* Strikes Above-Below ATM Quick Selector */}
            <div className="flex flex-col gap-1">
              <span className="text-[10.5px] uppercase font-bold text-slate-400">Strikes Around ATM</span>
              <div className="grid grid-cols-5 gap-1 text-center font-mono text-[11px]">
                {(["all", "atm", "5", "10", "20"] as RangeMode[]).map((r) => (
                  <button
                    key={r}
                    onClick={() => {
                      setRangeMode(r);
                      setMinStrikeInput(null);
                      setMaxStrikeInput(null);
                    }}
                    className={`py-1 rounded border capitalize transition-all ${
                      rangeMode === r
                        ? "bg-blue-600 text-white border-blue-400 font-bold"
                        : "bg-[#070c17] border-slate-800 text-slate-400 hover:text-slate-200"
                    }`}
                  >
                    {r}
                  </button>
                ))}
              </div>
            </div>

            {/* Strike Range Steppers (Min / Max) */}
            <div className="flex flex-col gap-1.5 pt-1 border-t border-slate-800">
              <span className="text-[10.5px] uppercase font-bold text-slate-400">Custom Strike Window</span>
              <div className="flex items-center justify-between text-[11px]">
                <span className="text-slate-400">Min:</span>
                <div className="flex items-center gap-1 font-mono">
                  <button onClick={() => handleMinStep(-50)} className="px-1.5 py-0.5 rounded bg-slate-800 hover:bg-slate-700">-</button>
                  <span className="w-14 text-center font-bold text-slate-200">{minStrikeInput ?? data?.min_strike ?? 22200}</span>
                  <button onClick={() => handleMinStep(+50)} className="px-1.5 py-0.5 rounded bg-slate-800 hover:bg-slate-700">+</button>
                </div>
              </div>
              <div className="flex items-center justify-between text-[11px]">
                <span className="text-slate-400">Max:</span>
                <div className="flex items-center gap-1 font-mono">
                  <button onClick={() => handleMaxStep(-50)} className="px-1.5 py-0.5 rounded bg-slate-800 hover:bg-slate-700">-</button>
                  <span className="w-14 text-center font-bold text-slate-200">{maxStrikeInput ?? data?.max_strike ?? 23200}</span>
                  <button onClick={() => handleMaxStep(+50)} className="px-1.5 py-0.5 rounded bg-slate-800 hover:bg-slate-700">+</button>
                </div>
              </div>
            </div>

            {/* Market Sentiment Semi-Circle Gauge Card */}
            <div className="mt-auto flex flex-col items-center rounded-lg bg-[#070c17] border border-slate-800 p-2.5 text-center">
              <span className="text-[10px] font-bold uppercase tracking-wider text-slate-400 mb-1">Market Sentiment</span>
              <svg width="120" height="65" viewBox="0 0 120 65" className="overflow-visible">
                {/* Arc Track */}
                <path d="M 15 60 A 45 45 0 0 1 105 60" fill="none" stroke="#1e293b" strokeWidth="10" strokeLinecap="round" />
                {/* Active Colored Arc */}
                <path
                  d="M 15 60 A 45 45 0 0 1 105 60"
                  fill="none"
                  stroke={
                    summary.sentiment_score >= 58
                      ? "#22c55e"
                      : summary.sentiment_score <= 42
                      ? "#ef4444"
                      : "#f59e0b"
                  }
                  strokeWidth="10"
                  strokeDasharray="141.37"
                  strokeDashoffset={141.37 * (1 - summary.sentiment_score / 100)}
                  strokeLinecap="round"
                  className="transition-all duration-500"
                />
              </svg>
              <span
                className={`text-sm font-extrabold mt-1 ${
                  summary.sentiment_score >= 58
                    ? "text-emerald-400"
                    : summary.sentiment_score <= 42
                    ? "text-rose-400"
                    : "text-amber-400"
                }`}
              >
                {summary.sentiment_label} ({summary.sentiment_score}%)
              </span>
              <span className="text-[9.5px] text-slate-400 leading-tight mt-0.5">{summary.sentiment_sub}</span>
            </div>
          </div>
        ) : (
          <button
            onClick={() => setSidebarOpen(true)}
            className="w-6 shrink-0 rounded bg-[#0b1324] border border-slate-800 flex items-center justify-center text-slate-400 hover:text-white font-mono text-sm py-2"
            title="Expand Sidebar"
          >
            »
          </button>
        )}

        {/* ──────────────── Main Chart Canvas Container ──────────────── */}
        <div className="flex-1 min-w-0 flex flex-col rounded-lg bg-[#0b1324] border border-[#142036] p-3 shadow-xl">
          {/* Sub-Header: The 3 Exact StockMojo Primary Tabs & Visual Legends */}
          <div className="flex flex-wrap items-center justify-between border-b border-slate-800 pb-2 mb-2">
            <div className="flex items-center gap-1.5">
              <button
                onClick={() => { setTab("change_total"); setPositionView(false); }}
                className={`px-3 py-1 rounded text-xs font-bold transition-all border ${
                  !positionView && tab === "change_total"
                    ? "bg-blue-600 text-white border-blue-400 shadow"
                    : "bg-[#070c17] text-slate-400 border-slate-800 hover:text-slate-200"
                }`}
              >
                📊 OI Change+Total
              </button>
              <button
                onClick={() => { setTab("change"); setPositionView(false); }}
                className={`px-3 py-1 rounded text-xs font-bold transition-all border ${
                  !positionView && tab === "change"
                    ? "bg-blue-600 text-white border-blue-400 shadow"
                    : "bg-[#070c17] text-slate-400 border-slate-800 hover:text-slate-200"
                }`}
              >
                OI Change
              </button>
              <button
                onClick={() => { setTab("total"); setPositionView(false); }}
                className={`px-3 py-1 rounded text-xs font-bold transition-all border ${
                  !positionView && tab === "total"
                    ? "bg-blue-600 text-white border-blue-400 shadow"
                    : "bg-[#070c17] text-slate-400 border-slate-800 hover:text-slate-200"
                }`}
              >
                Total OI
              </button>
              <button onClick={() => setPositionView(true)} aria-pressed={positionView} className={`px-3 py-1 rounded text-xs font-bold border ${positionView ? "bg-cyan-950 text-cyan-200 border-cyan-400" : "bg-[#070c17] text-cyan-400 border-slate-800"}`}>Strike Positioning</button>
            </div>

            {/* 6 Visual Legend Items (Matching StockMojo pixel-for-pixel) */}
            <div className="flex flex-wrap items-center gap-2 text-[10.5px]">
              {positionView ? <span className="text-cyan-300">Recorded size → change → time</span> : tab === "change_total" ? (
                <>
                  <span className="flex items-center gap-1 text-slate-300">
                    <span className="w-3 h-3 rounded-sm bg-[#22c55e]" /> Base Call
                  </span>
                  <span className="flex items-center gap-1 text-slate-300">
                    <span className="w-3 h-3 rounded-sm border border-[#22c55e] bg-[#22c55e]/30 bg-repeat [background-image:linear-gradient(45deg,#22c55e_25%,transparent_25%,transparent_50%,#22c55e_50%,#22c55e_75%,transparent_75%,transparent)] [background-size:6px_6px]" /> Call Added
                  </span>
                  <span className="flex items-center gap-1 text-slate-300">
                    <span className="w-3 h-3 rounded-sm border border-[#22c55e] border-dashed bg-transparent" /> Call Covered
                  </span>
                  <span className="flex items-center gap-1 text-slate-300 ml-2">
                    <span className="w-3 h-3 rounded-sm bg-[#ef4444]" /> Base Put
                  </span>
                  <span className="flex items-center gap-1 text-slate-300">
                    <span className="w-3 h-3 rounded-sm border border-[#ef4444] bg-[#ef4444]/30 bg-repeat [background-image:linear-gradient(45deg,#ef4444_25%,transparent_25%,transparent_50%,#ef4444_50%,#ef4444_75%,transparent_75%,transparent)] [background-size:6px_6px]" /> Put Added
                  </span>
                  <span className="flex items-center gap-1 text-slate-300">
                    <span className="w-3 h-3 rounded-sm border border-[#ef4444] border-dashed bg-transparent" /> Put Unwound
                  </span>
                </>
              ) : tab === "change" ? (
                <>
                  <span className="flex items-center gap-1 text-emerald-400 font-semibold">
                    <span className="w-3 h-3 rounded-sm bg-[#22c55e]" /> Call OI Chg (Resistance)
                  </span>
                  <span className="flex items-center gap-1 text-rose-400 font-semibold ml-2">
                    <span className="w-3 h-3 rounded-sm bg-[#ef4444]" /> Put OI Chg (Support)
                  </span>
                </>
              ) : (
                <>
                  <span className="flex items-center gap-1 text-emerald-400 font-semibold">
                    <span className="w-3 h-3 rounded-sm bg-[#22c55e]" /> Total Call OI
                  </span>
                  <span className="flex items-center gap-1 text-rose-400 font-semibold ml-2">
                    <span className="w-3 h-3 rounded-sm bg-[#ef4444]" /> Total Put OI
                  </span>
                </>
              )}
            </div>
          </div>

          {positionView && <StrikePositioning data={dataQuery === requestQuery ? data : null} lots={showLot} bucket={positionBucket} options={analystConfig.oi_studio.positioning.bucket_options} onBucket={setPositionBucket} busy={dataQuery !== requestQuery} error={fetchError} onReplay={ts => { setSelectedEndTs(ts); setIsReplayOn(true); setIsPlaying(false); const idx = data?.timeline.findIndex(t => t.ts === ts) ?? -1; if (idx >= 0) setReplayIdx(idx); }} />}
          <div style={{ display: positionView ? "none" : undefined }} className="flex flex-1 flex-col min-h-0">
          {/* ──────────────── High-Precision SVG Grouped Bar Canvas ──────────────── */}
          <div className="relative flex-1 min-h-[460px] w-full select-none" onMouseLeave={() => setHoverStrike(null)}>
            {loading ? (
              <div className="absolute inset-0 flex items-center justify-center text-slate-500 font-mono">
                Loading StockMojo OI Studio telemetry...
              </div>
            ) : (
              <svg
                width="100%"
                height="100%"
                viewBox={`0 0 ${chartWidth} ${chartHeight}`}
                preserveAspectRatio="none"
                className="overflow-visible"
                onMouseMove={(e) => {
                  const rect = e.currentTarget.getBoundingClientRect();
                  const relX = ((e.clientX - rect.left) / rect.width) * chartWidth;
                  setMousePos({ x: e.clientX, y: e.clientY });
                  // Find nearest strike
                  if (strikes.length) {
                    const idx = Math.max(0, Math.min(strikes.length - 1, Math.floor((relX - padLeft) / colWidth)));
                    setHoverStrike(strikes[idx]);
                  }
                }}
              >
                <defs>
                  {/* Call Added Diagonal Hatch */}
                  <pattern id="callHatch" width="8" height="8" patternTransform="rotate(45 0 0)" patternUnits="userSpaceOnUse">
                    <rect width="8" height="8" fill="#22c55e" fillOpacity="0.25" />
                    <line x1="0" y1="0" x2="0" y2="8" stroke="#22c55e" strokeWidth="2.5" />
                  </pattern>
                  {/* Put Added Diagonal Hatch */}
                  <pattern id="putHatch" width="8" height="8" patternTransform="rotate(45 0 0)" patternUnits="userSpaceOnUse">
                    <rect width="8" height="8" fill="#ef4444" fillOpacity="0.25" />
                    <line x1="0" y1="0" x2="0" y2="8" stroke="#ef4444" strokeWidth="2.5" />
                  </pattern>
                </defs>

                {/* Horizontal Gridlines & Values */}
                {[0, 0.25, 0.5, 0.75, 1].map((frac) => {
                  const val = tab === "change" ? minVal + frac * (maxVal - minVal) : frac * maxVal;
                  const y = getY(val);
                  return (
                    <g key={frac}>
                      <line x1={padLeft} y1={y} x2={padLeft + plotWidth} y2={y} stroke="#142036" strokeDasharray="3,3" />
                      <text x={padLeft - 8} y={y + 3.5} fill="#64748b" fontSize="9.5" textAnchor="end" fontFamily="monospace">
                        {formatTotal(val, showLot, lotSize)}
                      </text>
                    </g>
                  );
                })}

                {/* Center Baseline for OI Change */}
                {tab === "change" && (
                  <line x1={padLeft} y1={zeroLineY} x2={padLeft + plotWidth} y2={zeroLineY} stroke="#475569" strokeWidth="1.5" />
                )}

                {/* Hover Column Highlight Band */}
                {hoverStrike && (
                  <rect
                    x={padLeft + strikes.findIndex((s) => s.strike === hoverStrike.strike) * colWidth}
                    y={padTop}
                    width={colWidth}
                    height={plotHeight}
                    fill="rgba(148, 163, 184, 0.08)"
                    stroke="rgba(148, 163, 184, 0.2)"
                    strokeDasharray="2,2"
                  />
                )}

                {/* Vertical Dashed Line for Spot & Max Pain */}
                <line x1={spotX} y1={padTop - 15} x2={spotX} y2={padTop + plotHeight} stroke="#eab308" strokeWidth="1.5" strokeDasharray="4,4" />

                {/* Render Strike Bars */}
                {strikes.map((s, idx) => {
                  const colX = padLeft + idx * colWidth;
                  const callX = colX + (colWidth / 2) - barWidth - 1.5;
                  const putX = colX + (colWidth / 2) + 1.5;
                  const isAtm = Math.abs(s.strike - atm) <= 25;

                  return (
                    <g key={s.strike}>
                      {/* Strike X-Axis Label */}
                      <text
                        x={colX + colWidth / 2}
                        y={padTop + plotHeight + 16}
                        fill={isAtm ? "#38bdf8" : "#94a3b8"}
                        fontWeight={isAtm ? "bold" : "normal"}
                        fontSize="9.5"
                        textAnchor="middle"
                        fontFamily="monospace"
                      >
                        {s.strike}
                      </text>

                      {/* ── TAB 1: OI CHANGE + TOTAL (Base + Hatched Inc + Hollow Dec) ── */}
                      {tab === "change_total" && (
                        <>
                          {/* Call Side (Left bar) */}
                          <rect
                            x={callX}
                            y={getY(s.call_solid)}
                            width={barWidth}
                            height={Math.max(0, zeroLineY - getY(s.call_solid))}
                            fill="#22c55e"
                          />
                          {s.call_inc_hatched > 0 && (
                            <rect
                              x={callX}
                              y={getY(s.call_solid + s.call_inc_hatched)}
                              width={barWidth}
                              height={Math.max(0, getY(s.call_solid) - getY(s.call_solid + s.call_inc_hatched))}
                              fill="url(#callHatch)"
                              stroke="#22c55e"
                              strokeWidth="0.8"
                            />
                          )}
                          {s.call_dec_hollow > 0 && (
                            <rect
                              x={callX}
                              y={getY(s.call_solid + s.call_dec_hollow)}
                              width={barWidth}
                              height={Math.max(0, getY(s.call_solid) - getY(s.call_solid + s.call_dec_hollow))}
                              fill="none"
                              stroke="#22c55e"
                              strokeWidth="1.2"
                              strokeDasharray="2.5,2.5"
                            />
                          )}

                          {/* Put Side (Right bar) */}
                          <rect
                            x={putX}
                            y={getY(s.put_solid)}
                            width={barWidth}
                            height={Math.max(0, zeroLineY - getY(s.put_solid))}
                            fill="#ef4444"
                          />
                          {s.put_inc_hatched > 0 && (
                            <rect
                              x={putX}
                              y={getY(s.put_solid + s.put_inc_hatched)}
                              width={barWidth}
                              height={Math.max(0, getY(s.put_solid) - getY(s.put_solid + s.put_inc_hatched))}
                              fill="url(#putHatch)"
                              stroke="#ef4444"
                              strokeWidth="0.8"
                            />
                          )}
                          {s.put_dec_hollow > 0 && (
                            <rect
                              x={putX}
                              y={getY(s.put_solid + s.put_dec_hollow)}
                              width={barWidth}
                              height={Math.max(0, getY(s.put_solid) - getY(s.put_solid + s.put_dec_hollow))}
                              fill="none"
                              stroke="#ef4444"
                              strokeWidth="1.2"
                              strokeDasharray="2.5,2.5"
                            />
                          )}
                        </>
                      )}

                      {/* ── TAB 2: OI CHANGE (Pure delta above/below zero) ── */}
                      {tab === "change" && (
                        <>
                          {/* Call Chg */}
                          <rect
                            x={callX}
                            y={s.call_oi_chg >= 0 ? getY(s.call_oi_chg) : zeroLineY}
                            width={barWidth}
                            height={Math.max(1, Math.abs(getY(s.call_oi_chg) - zeroLineY))}
                            fill="#22c55e"
                          />
                          {/* Put Chg */}
                          <rect
                            x={putX}
                            y={s.put_oi_chg >= 0 ? getY(s.put_oi_chg) : zeroLineY}
                            width={barWidth}
                            height={Math.max(1, Math.abs(getY(s.put_oi_chg) - zeroLineY))}
                            fill="#ef4444"
                          />
                        </>
                      )}

                      {/* ── TAB 3: TOTAL OI (Clean standing Call vs Put) ── */}
                      {tab === "total" && (
                        <>
                          <rect
                            x={callX}
                            y={getY(s.call_oi_total)}
                            width={barWidth}
                            height={Math.max(0, zeroLineY - getY(s.call_oi_total))}
                            fill="#22c55e"
                          />
                          <rect
                            x={putX}
                            y={getY(s.put_oi_total)}
                            width={barWidth}
                            height={Math.max(0, zeroLineY - getY(s.put_oi_total))}
                            fill="#ef4444"
                          />
                        </>
                      )}
                    </g>
                  );
                })}
              </svg>
            )}

            {/* Top Floating Spot & Max Pain Badges */}
            <div
              className="absolute top-1 pointer-events-none -translate-x-1/2 flex flex-col items-center gap-1 z-20"
              style={{ left: `${(spotX / chartWidth) * 100}%` }}
            >
              <span className="px-2 py-0.5 rounded bg-white text-slate-900 border border-slate-300 font-mono text-[10.5px] font-extrabold shadow-lg">
                Spot : {spot.toFixed(2)}
              </span>
              <span className="px-2 py-0.5 rounded bg-[#b45309] text-amber-100 border border-amber-500 font-mono text-[10px] font-bold shadow-md">
                Max Pain : {maxPain}
              </span>
            </div>

            {/* ──────────────── Interactive Hover Crosshair & Rich Tooltip ──────────────── */}
            {hoverStrike && mousePos && (
              <div
                className="fixed z-50 pointer-events-none rounded-lg bg-[#070c17]/95 border border-sky-500/70 p-3 shadow-2xl backdrop-blur text-[11px] font-mono text-slate-200"
                style={{
                  left: Math.min(window.innerWidth - 280, mousePos.x + 16),
                  top: Math.min(window.innerHeight - 220, mousePos.y - 40),
                }}
              >
                <div className="flex items-center justify-between border-b border-slate-700 pb-1 mb-1.5 font-bold">
                  <span className="text-white text-xs">Strike: {hoverStrike.strike}</span>
                  <div className="flex gap-1">
                    {hoverStrike.strike === atm && <span className="px-1.5 rounded bg-cyan-900/60 text-cyan-300 text-[9.5px]">ATM</span>}
                    {hoverStrike.strike === maxPain && <span className="px-1.5 rounded bg-amber-900/60 text-amber-300 text-[9.5px]">Max Pain</span>}
                  </div>
                </div>

                <div className="flex flex-col gap-1 text-[11px]">
                  <div className="flex justify-between items-center text-emerald-400">
                    <span>🟢 Call OI Chg:</span>
                    <b>
                      {formatUnits(hoverStrike.call_oi_chg, showLot, lotSize)} ({hoverStrike.call_chg_pct > 0 ? "+" : ""}{hoverStrike.call_chg_pct}%)
                    </b>
                  </div>
                  <div className="text-[10px] text-slate-400 pl-4">
                    Standing Call OI: {formatTotal(hoverStrike.call_oi_total, showLot, lotSize)}
                  </div>

                  <div className="flex justify-between items-center text-rose-400 pt-1">
                    <span>🔴 Put OI Chg:</span>
                    <b>
                      {formatUnits(hoverStrike.put_oi_chg, showLot, lotSize)} ({hoverStrike.put_chg_pct > 0 ? "+" : ""}{hoverStrike.put_chg_pct}%)
                    </b>
                  </div>
                  <div className="text-[10px] text-slate-400 pl-4">
                    Standing Put OI: {formatTotal(hoverStrike.put_oi_total, showLot, lotSize)}
                  </div>

                  <div className="flex justify-between items-center border-t border-slate-800 pt-1 text-slate-300">
                    <span>Net (PE − CE):</span>
                    <b className={hoverStrike.net_pe_ce_chg >= 0 ? "text-emerald-400" : "text-rose-400"}>
                      {formatUnits(hoverStrike.net_pe_ce_chg, showLot, lotSize)}
                    </b>
                  </div>
                  <div className="flex justify-between items-center text-slate-400">
                    <span>Strike PCR:</span>
                    <b className="text-amber-300">{hoverStrike.strike_pcr.toFixed(2)}</b>
                  </div>
                </div>

                {/* Trader Action Verdict */}
                <div className="mt-2 pt-1.5 border-t border-slate-800/80 text-[10px] leading-tight">
                  <span className="font-bold text-gold-400">💡 Trader Verdict: </span>
                  {hoverStrike.put_oi_chg > hoverStrike.call_oi_chg * 1.5 && hoverStrike.put_oi_chg > 0 ? (
                    <span className="text-emerald-300">🟢 Strong Put Writing Floor — Institutions defending this strike (Bullish Support).</span>
                  ) : hoverStrike.call_oi_chg > hoverStrike.put_oi_chg * 1.5 && hoverStrike.call_oi_chg > 0 ? (
                    <span className="text-rose-300">🔴 Heavy Call Writing Ceiling — Institutions capping upside at this strike (Bearish Resistance).</span>
                  ) : hoverStrike.call_oi_chg < 0 && hoverStrike.put_oi_chg > 0 ? (
                    <span className="text-emerald-400 font-bold">🚀 Short Covering Squeeze — Call writers panic-exiting while Put writers add!</span>
                  ) : hoverStrike.put_oi_chg < 0 && hoverStrike.call_oi_chg > 0 ? (
                    <span className="text-rose-400 font-bold">🩸 Long Unwinding / Breakdown — Put writers fleeing while Call writers press!</span>
                  ) : (
                    <span className="text-slate-300">⚖️ Balanced Activity — Two-way writer positioning.</span>
                  )}
                </div>
              </div>
            )}
          </div>

          </div>
          {/* ──────────────── Dual-Thumb Range Slider & 9 Quick Buttons ──────────────── */}
          <div className="mt-3 pt-2.5 border-t border-slate-800 flex flex-col gap-2">
            <div className="flex items-center gap-3">
              <button
                onClick={() => {
                  setSelectedStartTs(null);
                  setSelectedEndTs(null);
                  setActiveQuickWindow(null);
                }}
                className="px-2.5 py-1 rounded bg-[#070c17] hover:bg-slate-800 border border-slate-700 text-[11px] font-bold text-slate-300"
              >
                Reset
              </button>

              {/* Slider Track with Timeline Bubbles */}
              <div className="relative flex-1 flex items-center h-6">
                <input
                  type="range"
                  min="0"
                  max={Math.max(0, (data?.timeline?.length || 1) - 1)}
                  value={
                    selectedEndTs !== null && data?.timeline?.length
                      ? Math.max(0, data.timeline.findIndex((t) => t.ts >= selectedEndTs))
                      : (data?.timeline?.length || 1) - 1
                  }
                  onChange={(e) => {
                    const idx = Number(e.target.value);
                    if (data?.timeline?.[idx]) {
                      setSelectedEndTs(data.timeline[idx].ts);
                      setReplayIdx(idx);
                    }
                  }}
                  className="w-full accent-blue-500 cursor-pointer"
                />
              </div>

              <div className="font-mono text-[11px] text-slate-300 shrink-0">
                Window: <b>{data?.window_start_label || "09:15 AM"}</b> ↔ <b>{data?.window_end_label || "03:40 PM"}</b>
              </div>
            </div>

            {/* 9 Quick Lookback Buttons */}
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="text-[10px] font-bold uppercase text-slate-400 mr-1">Quick Windows:</span>
              {QUICK_LOOKBACKS.map((qw) => (
                <button
                  key={qw.label}
                  onClick={() => handleQuickWindow(qw.mins)}
                  className={`px-2 py-0.5 rounded text-[11px] font-medium border transition-all ${
                    activeQuickWindow === qw.mins
                      ? "bg-blue-900/40 border-blue-400 text-blue-200 font-bold"
                      : "bg-[#070c17] border-slate-800 text-slate-400 hover:text-slate-200"
                  }`}
                >
                  {qw.label}
                </button>
              ))}
            </div>
          </div>

          {/* ──────────────── Floating Replay Dock ──────────────── */}
          {isReplayOn && (
            <div className="mt-2 p-2 rounded-lg bg-[#070c17]/95 border border-amber-500/70 shadow-2xl flex flex-wrap items-center justify-between gap-3 text-[11px]">
              <div className="flex items-center gap-1.5">
                <span className="font-bold text-amber-400">⏱ REPLAY DOCK:</span>
                <button onClick={handleStepBack} className="px-2 py-1 rounded bg-slate-800 hover:bg-slate-700 text-white font-bold">
                  ⏪ Step
                </button>
                <button
                  onClick={() => setIsPlaying((p) => !p)}
                  className={`px-3 py-1 rounded font-bold transition-all ${
                    isPlaying ? "bg-rose-600 text-white" : "bg-emerald-600 text-white"
                  }`}
                >
                  {isPlaying ? "⏸ PAUSE" : "▶️ PLAY"}
                </button>
                <button onClick={handleStepForward} className="px-2 py-1 rounded bg-slate-800 hover:bg-slate-700 text-white font-bold">
                  Step ⏩
                </button>
                <button onClick={handleLiveJump} className="px-2 py-1 rounded bg-blue-600 text-white font-bold ml-1">
                  LIVE 🔴
                </button>
              </div>

              <div className="font-mono text-slate-300">
                Replay Cursor: <b className="text-amber-300">{data?.timeline?.[replayIdx]?.time_label || "09:15 AM"}</b> (Spot: {data?.timeline?.[replayIdx]?.spot?.toFixed(1) || spot.toFixed(1)})
              </div>
            </div>
          )}

          {/* ──────────────── Bottom 3-Card Summary Row ──────────────── */}
          <div className="grid grid-cols-1 md:grid-cols-3 gap-3 mt-3 pt-3 border-t border-slate-800">
            {/* Card 1: Open Interest Change */}
            <div className="flex flex-col rounded-lg bg-[#070c17] border border-slate-800 p-3">
              <div className="flex items-center justify-between mb-2">
                <span className="text-[11px] font-bold text-slate-300">📊 Open Interest Change</span>
                <span
                  className={`text-[10px] font-mono px-1.5 py-0.5 rounded ${
                    summary.put_oi_chg - summary.call_oi_chg >= 0
                      ? "bg-emerald-950 text-emerald-300 border border-emerald-800"
                      : "bg-rose-950 text-rose-300 border border-rose-800"
                  }`}
                >
                  PE−CE: {formatUnits(summary.put_oi_chg - summary.call_oi_chg, showLot, lotSize)}
                </span>
              </div>
              <div className="grid grid-cols-2 gap-2 h-20 items-end">
                <div className="flex flex-col items-center gap-1">
                  <span className="text-[10px] font-mono font-bold text-emerald-400">
                    {formatUnits(summary.call_oi_chg, showLot, lotSize)}
                  </span>
                  <div className="w-full rounded-t bg-[#22c55e] h-12" />
                  <span className="text-[10.5px] font-bold text-slate-400">CALL</span>
                </div>
                <div className="flex flex-col items-center gap-1">
                  <span className="text-[10px] font-mono font-bold text-rose-400">
                    {formatUnits(summary.put_oi_chg, showLot, lotSize)}
                  </span>
                  <div className="w-full rounded-t bg-[#ef4444] h-12" />
                  <span className="text-[10.5px] font-bold text-slate-400">PUT</span>
                </div>
              </div>
            </div>

            {/* Card 2: Total Open Interest */}
            <div className="flex flex-col rounded-lg bg-[#070c17] border border-slate-800 p-3">
              <div className="flex items-center justify-between mb-2">
                <span className="text-[11px] font-bold text-slate-300">📊 Total Open Interest</span>
                <span className="text-[10px] font-mono text-slate-400">Standing OI</span>
              </div>
              <div className="grid grid-cols-2 gap-2 h-20 items-end">
                <div className="flex flex-col items-center gap-1">
                  <span className="text-[10px] font-mono font-bold text-emerald-400">
                    {formatTotal(summary.call_oi_total, showLot, lotSize)}
                  </span>
                  <div className="w-full rounded-t bg-[#22c55e] h-14" />
                  <span className="text-[10.5px] font-bold text-slate-400">CALL</span>
                </div>
                <div className="flex flex-col items-center gap-1">
                  <span className="text-[10px] font-mono font-bold text-rose-400">
                    {formatTotal(summary.put_oi_total, showLot, lotSize)}
                  </span>
                  <div className="w-full rounded-t bg-[#ef4444] h-14" />
                  <span className="text-[10.5px] font-bold text-slate-400">PUT</span>
                </div>
              </div>
            </div>

            {/* Card 3: Put/Call Ratio Donut Chart */}
            <div className="flex flex-col rounded-lg bg-[#070c17] border border-slate-800 p-3 items-center justify-between">
              <div className="w-full flex items-center justify-between mb-1">
                <span className="text-[11px] font-bold text-slate-300">📊 Put/Call Ratio</span>
                <span className="text-[10px] font-mono text-amber-300">PCR: {summary.pcr.toFixed(2)}</span>
              </div>
              <div className="relative w-24 h-24 flex items-center justify-center">
                <svg width="96" height="96" viewBox="0 0 96 96">
                  {/* Call Arc (Green) */}
                  <circle
                    cx="48"
                    cy="48"
                    r="36"
                    fill="none"
                    stroke="#22c55e"
                    strokeWidth="12"
                    strokeDasharray="226.19"
                    strokeDashoffset="0"
                  />
                  {/* Put Arc (Red) */}
                  <circle
                    cx="48"
                    cy="48"
                    r="36"
                    fill="none"
                    stroke="#ef4444"
                    strokeWidth="12"
                    strokeDasharray="226.19"
                    strokeDashoffset={226.19 * (summary.call_share_pct / 100)}
                    strokeLinecap="round"
                    transform="rotate(-90 48 48)"
                  />
                </svg>
                <div className="absolute inset-0 flex flex-col items-center justify-center font-mono">
                  <span className="text-[13px] font-extrabold text-white">{summary.pcr.toFixed(2)}</span>
                  <span className="text-[8.5px] text-slate-400">PCR</span>
                </div>
              </div>
              <div className="flex items-center gap-3 text-[10px] font-mono mt-1">
                <span className="text-rose-400 font-bold">{summary.put_share_pct}% Put OI</span>
                <span className="text-slate-600">|</span>
                <span className="text-emerald-400 font-bold">{summary.call_share_pct}% Call OI</span>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
