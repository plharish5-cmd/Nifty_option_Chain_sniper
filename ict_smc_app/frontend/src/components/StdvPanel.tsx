// StdvPanel.tsx — ICT Standard Deviation (STDV) Manipulation Leg Cockpit
// Display-only institutional decision-support tool.
// Provides multi-timeframe detection (15m, 30m, 1h, 4h, 1D, 1W),
// Bullish (Low to High) & Bearish (High to Low) manipulation sweeps,
// Anchor Mode toggle (Order Block Run vs Full Judas Swing),
// Interactive 2-Click Chart Anchoring, and mechanical targets: Minor Reversal (-2 to -2.5) & Major Reversal (-4 to -4.5).

import { useEffect, useMemo, useState } from "react";
import type { Candle, TF } from "../api";
import { api } from "../api";
import {
  aggregateBars,
  computeStdvLevels,
  detectManipulationLegs,
  type StdvAnchorMode,
  type StdvLeg,
} from "./stdv";

interface StdvPanelProps {
  candles: Candle[];
  spot: number | null;
  chartTf: TF;
  selectedLeg: StdvLeg | null;
  onSelectLeg: (leg: StdvLeg | null) => void;
  isLayerActive: boolean;
  onToggleLayer: () => void;
  isPicking?: boolean;
  onStartPicking?: () => void;
  onCancelPicking?: () => void;
  onSetChartTf?: (tf: TF) => void;
}

const SUPPORTED_TFS = [
  { id: "15m", label: "15m Intraday", sec: 900, baseTf: "15m" as TF, days: 5 },
  { id: "30m", label: "30m Tactical", sec: 1800, baseTf: "15m" as TF, days: 10 },
  { id: "1h", label: "1H Session", sec: 3600, baseTf: "1h" as TF, days: 20 },
  { id: "4h", label: "4H Swing", sec: 14400, baseTf: "1h" as TF, days: 40 },
  { id: "1D", label: "1D Daily", sec: 86400, baseTf: "1D" as TF, days: 60 },
  { id: "1W", label: "1W Weekly", sec: 604800, baseTf: "1D" as TF, days: 180 },
];

export default function StdvPanel({
  candles: chartCandles,
  spot,
  chartTf,
  selectedLeg,
  onSelectLeg,
  isLayerActive,
  onToggleLayer,
  isPicking = false,
  onStartPicking,
  onCancelPicking,
  onSetChartTf,
}: StdvPanelProps) {
  const [activeTfId, setActiveTfId] = useState<string>("15m");
  const [dirFilter, setDirFilter] = useState<"all" | "bull" | "bear">("all");
  const [anchorMode, setAnchorMode] = useState<"orderblock" | "swing">("orderblock");
  const [customCandles, setCustomCandles] = useState<Candle[]>([]);
  const [loading, setLoading] = useState(false);

  // Manual fine-tune adjustments for currently selected leg
  const [manual0, setManual0] = useState<number | null>(null);
  const [manual1, setManual1] = useState<number | null>(null);

  // MTF Confluence cache
  const [mtfLegs, setMtfLegs] = useState<Record<string, StdvLeg | null>>({});

  // Fetch candles when active TF changes
  useEffect(() => {
    const tfCfg = SUPPORTED_TFS.find((t) => t.id === activeTfId) || SUPPORTED_TFS[0];
    let dead = false;

    // If active TF matches chart TF and it's a native TF, use chartCandles directly
    if (activeTfId === chartTf && (activeTfId === "15m" || activeTfId === "1h" || activeTfId === "1D")) {
      setCustomCandles(chartCandles);
      return;
    }

    setLoading(true);
    api
      .candles(tfCfg.baseTf, tfCfg.days, "NIFTY")
      .then((res) => {
        if (dead) return;
        const raw = res.candles || [];
        if (tfCfg.sec === 1800) {
          setCustomCandles(aggregateBars(raw, 1800));
        } else if (tfCfg.sec === 14400) {
          setCustomCandles(aggregateBars(raw, 14400));
        } else if (tfCfg.sec === 604800) {
          setCustomCandles(aggregateBars(raw, 604800));
        } else {
          setCustomCandles(raw);
        }
      })
      .catch((err) => {
        console.error("Failed to load candles for STDV:", err);
      })
      .finally(() => {
        if (!dead) setLoading(false);
      });

    return () => {
      dead = true;
    };
  }, [activeTfId, chartTf, chartCandles]);

  // Load MTF background reads for Weekly, Daily, 1H, 15m confluence matrix
  useEffect(() => {
    let dead = false;
    const fetchMtf = async () => {
      try {
        const [c15m, c1h, c1d] = await Promise.all([
          api.candles("15m", 5, "NIFTY"),
          api.candles("1h", 20, "NIFTY"),
          api.candles("1D", 60, "NIFTY"),
        ]);
        if (dead) return;

        const leg15m = detectManipulationLegs(c15m.candles || [], "15m", spot ?? undefined, anchorMode)[0] || null;
        const leg1h = detectManipulationLegs(c1h.candles || [], "1h", spot ?? undefined, anchorMode)[0] || null;
        const leg1d = detectManipulationLegs(c1d.candles || [], "1D", spot ?? undefined, anchorMode)[0] || null;
        const weeklyBars = aggregateBars(c1d.candles || [], 604800);
        const leg1w = detectManipulationLegs(weeklyBars, "1W", spot ?? undefined, anchorMode)[0] || null;

        setMtfLegs({ "15m": leg15m, "1h": leg1h, "1D": leg1d, "1W": leg1w });
      } catch (e) {
        console.error("Failed to fetch MTF legs:", e);
      }
    };

    fetchMtf();
    const interval = window.setInterval(fetchMtf, 60000);
    return () => {
      dead = true;
      window.clearInterval(interval);
    };
  }, [spot, anchorMode]);

  // Detect legs for the active view
  const currentCandles = customCandles.length ? customCandles : chartCandles;
  const detectedLegs = useMemo(() => {
    return detectManipulationLegs(currentCandles, activeTfId, spot ?? undefined, anchorMode);
  }, [currentCandles, activeTfId, spot, anchorMode]);

  // Filtered legs
  const filteredLegs = useMemo(() => {
    if (dirFilter === "all") return detectedLegs;
    return detectedLegs.filter((l) => l.dir === dirFilter);
  }, [detectedLegs, dirFilter]);

  // Active leg being inspected / displayed
  const activeLeg = useMemo(() => {
    if (manual0 != null && manual1 != null && selectedLeg) {
      const computed = computeStdvLevels(selectedLeg.dir, manual0, manual1, spot ?? undefined);
      return {
        ...selectedLeg,
        anchor0: manual0,
        anchor1: manual1,
        range: Math.round(Math.abs(manual0 - manual1) * 10) / 10,
        high: Math.max(manual0, manual1),
        low: Math.min(manual0, manual1),
        levels: computed.levels,
        minorZone: computed.minorZone,
        majorZone: computed.majorZone,
        status: computed.status,
        statusText: computed.statusText,
      };
    }
    return selectedLeg || filteredLegs[0] || null;
  }, [selectedLeg, filteredLegs, manual0, manual1, spot]);

  // Select first leg automatically if none selected
  useEffect(() => {
    if (!selectedLeg && filteredLegs.length > 0) {
      onSelectLeg(filteredLegs[0]);
    }
  }, [filteredLegs, selectedLeg, onSelectLeg]);

  // Reset manual overrides when changing leg
  const handleSelectLeg = (leg: StdvLeg) => {
    setManual0(null);
    setManual1(null);
    onSelectLeg(leg);
  };

  // Reset to auto-calculated anchors
  const handleResetAnchors = () => {
    setManual0(null);
    setManual1(null);
  };

  const formatTs = (t: number) => {
    const d = new Date(t * 1000);
    return `${d.toLocaleDateString("en-IN", { day: "2-digit", month: "short" })} ${String(d.getUTCHours()).padStart(2, "0")}:${String(d.getUTCMinutes()).padStart(2, "0")}`;
  };

  return (
    <div className="flex h-full flex-col text-[12px] bg-[#0b1220] text-slate-200 overflow-hidden">
      {/* ── Control Bar ──────────────────────────────────────────────────────── */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-ink-700 px-3 py-2 bg-ink-900/60">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-[11px] font-bold uppercase tracking-wider text-gold-400">Timeframe:</span>
          <div className="flex gap-1">
            {SUPPORTED_TFS.map((t) => (
              <button
                key={t.id}
                onClick={() => {
                  setActiveTfId(t.id);
                  setManual0(null);
                  setManual1(null);
                  if (onSetChartTf) onSetChartTf(t.id as TF);
                }}
                className={`tab ${activeTfId === t.id ? "tab-on !bg-gold-500/20 !border-gold-500/60 !text-gold-300 font-bold" : ""}`}
              >
                {t.label}
              </button>
            ))}
          </div>

          <span className="mx-1 h-4 w-px bg-ink-700" />

          {/* Option 2: Anchor Mode Toggle (Order Block Run vs Full Judas Swing) */}
          <span className="text-[11px] font-bold uppercase tracking-wider text-cyan-400">Anchor Mode:</span>
          <div className="flex items-center gap-1 bg-ink-800 p-0.5 rounded border border-ink-700">
            <button
              onClick={() => {
                setAnchorMode("orderblock");
                setManual0(null);
                setManual1(null);
              }}
              title="Anchor strictly on the consecutive downclose/upclose candle run leading into the sweep (Screenshot 6)"
              className={`px-2 py-0.5 rounded text-[11px] font-semibold transition-colors ${
                anchorMode === "orderblock"
                  ? "bg-cyan-500/20 text-cyan-300 border border-cyan-500/50 shadow-sm"
                  : "text-slate-400 hover:text-slate-200"
              }`}
            >
              📦 Order Block Run (Downclose)
            </button>
            <button
              onClick={() => {
                setAnchorMode("swing");
                setManual0(null);
                setManual1(null);
              }}
              title="Anchor from the preceding swing pivot crest down to the sweep (Screenshot 7)"
              className={`px-2 py-0.5 rounded text-[11px] font-semibold transition-colors ${
                anchorMode === "swing"
                  ? "bg-purple-500/20 text-purple-300 border border-purple-500/50 shadow-sm"
                  : "text-slate-400 hover:text-slate-200"
              }`}
            >
              🌊 Full Judas Swing
            </button>
          </div>

          <span className="mx-1 h-4 w-px bg-ink-700" />

          <span className="text-[11px] font-bold uppercase tracking-wider text-slate-400">Direction:</span>
          <div className="flex gap-1">
            <button
              onClick={() => setDirFilter("all")}
              className={`tab ${dirFilter === "all" ? "tab-on" : ""}`}
            >
              All ({detectedLegs.length})
            </button>
            <button
              onClick={() => setDirFilter("bull")}
              className={`tab ${dirFilter === "bull" ? "tab-on !border-emerald-500/50 !text-emerald-300" : ""}`}
            >
              🟢 Bullish Low→High
            </button>
            <button
              onClick={() => setDirFilter("bear")}
              className={`tab ${dirFilter === "bear" ? "tab-on !border-rose-500/50 !text-rose-300" : ""}`}
            >
              🔴 Bearish High→Low
            </button>
          </div>
        </div>

        <div className="flex items-center gap-2">
          {selectedLeg && (
            <button
              onClick={() => {
                onSelectLeg(null);
                setManual0(null);
                setManual1(null);
              }}
              title="Clear custom 2-click marking and let the system automatically detect & mark the ICT manipulation leg"
              className="tab font-bold flex items-center gap-1.5 !border-cyan-400 !bg-cyan-500/20 !text-cyan-200 hover:!bg-cyan-500/30 shadow-md"
            >
              <span>🤖</span>
              <span>Use System Auto Marking</span>
            </button>
          )}
          {/* Option 3: Interactive 2-Click Chart Tool Button */}
          {onStartPicking && (
            <button
              onClick={isPicking ? onCancelPicking : onStartPicking}
              title="Click any two points directly on the candlestick chart to project custom STDV"
              className={`tab font-semibold flex items-center gap-1.5 transition-all ${
                isPicking
                  ? "tab-on !bg-red-600/30 !border-red-500 !text-white animate-pulse"
                  : "!border-gold-500/70 !text-gold-300 hover:!border-gold-400"
              }`}
            >
              <span className="text-[13px]">{isPicking ? "❌" : "📍"}</span>
              {isPicking ? "Cancel Chart Picking (Esc)" : "2-Click Chart Tool"}
            </button>
          )}

          <button
            onClick={onToggleLayer}
            title="Toggle STDV projections overlay directly on the main candlestick chart"
            className={`tab font-semibold ${
              isLayerActive
                ? "tab-on !border-cyan-500 !text-cyan-300 !bg-cyan-500/20"
                : "!border-slate-600 !text-slate-400"
            }`}
          >
            {isLayerActive ? "📐 Chart Overlay: ON" : "📐 Chart Overlay: OFF"}
          </button>
        </div>
      </div>

      {/* ── Active Picking Banner ─────────────────────────────────────────────── */}
      {isPicking && (
        <div className="bg-gradient-to-r from-red-950 via-ink-900 to-red-950 border-b border-red-500/50 px-4 py-2 flex items-center justify-between text-white animate-pulse">
          <div className="flex items-center gap-2">
            <span className="text-[14px]">📍</span>
            <span className="font-bold text-red-300">INTERACTIVE 2-CLICK CHART TOOL ACTIVE:</span>
            <span className="text-slate-200">
              <b>Step 1:</b> Click on the Sweep Wick on the main chart. &nbsp;<b>Step 2:</b> Click on the Shift Pivot High/Low.
            </span>
          </div>
          <button
            onClick={onCancelPicking}
            className="px-2.5 py-0.5 bg-red-600 hover:bg-red-500 text-white font-bold rounded text-[11px]"
          >
            Cancel (Esc)
          </button>
        </div>
      )}

      {/* ── Main Body Split Grid ──────────────────────────────────────────────── */}
      <div className="flex-1 min-h-0 grid grid-cols-1 xl:grid-cols-[1.4fr_1.1fr] gap-3 p-3 overflow-y-auto">
        {/* Left Side: Active Manipulation Leg Spotlight & Mechanical Target Dashboard */}
        <div className="flex flex-col gap-3 min-w-0">
          {activeLeg ? (
            <>
              {/* Spotlight Banner */}
              <div className="card p-3 bg-gradient-to-r from-ink-900 to-ink-850 border border-ink-700 rounded-lg">
                <div className="flex items-center justify-between border-b border-ink-800 pb-2 mb-2">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span
                      className={`px-2 py-0.5 rounded text-[11px] font-bold uppercase ${
                        activeLeg.dir === "bull"
                          ? "bg-emerald-500/20 text-emerald-300 border border-emerald-500/40"
                          : "bg-rose-500/20 text-rose-300 border border-rose-500/40"
                      }`}
                    >
                      {activeLeg.dir === "bull" ? "▲ Bullish Manipulation" : "▼ Bearish Manipulation"}
                    </span>
                    <span className="text-[12px] font-semibold text-slate-300">
                      {activeLeg.tf} · {formatTs(activeLeg.from)} → {formatTs(activeLeg.to)}
                    </span>
                    <span className="text-[10px] bg-cyan-950/40 text-cyan-300 border border-cyan-500/30 px-1.5 py-0.5 rounded">
                      {activeLeg.anchorMode === "orderblock"
                        ? "📦 Order Block Run"
                        : activeLeg.anchorMode === "swing"
                        ? "🌊 Full Judas Swing"
                        : "📍 Interactive 2-Click"}
                    </span>
                    {activeLeg.confluences.map((c, i) => (
                      <span key={i} className="text-[10px] bg-ink-800 text-slate-400 px-1.5 py-0.5 rounded border border-ink-700">
                        {c}
                      </span>
                    ))}
                  </div>

                  <span className="text-gold-400 font-bold text-[12px]">
                    Range: {activeLeg.range} pts
                  </span>
                </div>

                {/* Anchor Controls & Fine Tuner with Visual Badges */}
                <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 pt-1 text-[11px]">
                  <div className="bg-ink-800/80 p-2 rounded border border-ink-700 relative overflow-hidden">
                    <div className="flex items-center justify-between">
                      <span className="text-slate-400 font-medium">Anchor 1 (Sweep Origin)</span>
                      <span className="px-1.5 py-0.2 bg-red-600 text-white rounded-full font-bold text-[10px]">1</span>
                    </div>
                    <div className="text-[14px] font-bold text-white num mt-0.5">{activeLeg.anchor1.toFixed(1)}</div>
                    <div className="text-[10px] text-slate-500">
                      {activeLeg.dir === "bull" ? "Lowest wick of sweep" : "Highest wick of sweep"}
                    </div>
                  </div>

                  <div className="bg-ink-800/80 p-2 rounded border border-ink-700 relative overflow-hidden">
                    <div className="flex items-center justify-between">
                      <span className="text-slate-400 font-medium">Anchor 0 (Shift Delivery)</span>
                      <span className="px-1.5 py-0.2 bg-red-600 text-white rounded-full font-bold text-[10px]">0</span>
                    </div>
                    <div className="text-[14px] font-bold text-cyan-300 num mt-0.5">{activeLeg.anchor0.toFixed(1)}</div>
                    <div className="text-[10px] text-slate-500">
                      {activeLeg.dir === "bull" ? "Shift high before sweep" : "Shift low before sweep"}
                    </div>
                  </div>

                  <div className="bg-ink-800/80 p-2 rounded border border-ink-700 flex flex-col justify-between">
                    <div className="text-slate-400 font-medium">Fine-Tune Anchor 0</div>
                    <div className="flex items-center gap-1 mt-1">
                      <button
                        onClick={() => setManual0((manual0 ?? activeLeg.anchor0) - 2)}
                        className="px-1.5 py-0.5 bg-ink-700 rounded text-slate-300 hover:bg-ink-600"
                      >
                        -2
                      </button>
                      <input
                        type="number"
                        step="1"
                        value={manual0 ?? activeLeg.anchor0}
                        onChange={(e) => setManual0(parseFloat(e.target.value) || 0)}
                        className="w-16 bg-ink-900 border border-ink-600 rounded px-1 py-0.5 text-center text-[11px] text-white"
                      />
                      <button
                        onClick={() => setManual0((manual0 ?? activeLeg.anchor0) + 2)}
                        className="px-1.5 py-0.5 bg-ink-700 rounded text-slate-300 hover:bg-ink-600"
                      >
                        +2
                      </button>
                    </div>
                  </div>

                  <div className="bg-ink-800/80 p-2 rounded border border-ink-700 flex flex-col justify-between">
                    <div className="text-slate-400 font-medium">Fine-Tune Anchor 1</div>
                    <div className="flex items-center gap-1 mt-1">
                      <button
                        onClick={() => setManual1((manual1 ?? activeLeg.anchor1) - 2)}
                        className="px-1.5 py-0.5 bg-ink-700 rounded text-slate-300 hover:bg-ink-600"
                      >
                        -2
                      </button>
                      <input
                        type="number"
                        step="1"
                        value={manual1 ?? activeLeg.anchor1}
                        onChange={(e) => setManual1(parseFloat(e.target.value) || 0)}
                        className="w-16 bg-ink-900 border border-ink-600 rounded px-1 py-0.5 text-center text-[11px] text-white"
                      />
                      <button
                        onClick={() => setManual1((manual1 ?? activeLeg.anchor1) + 2)}
                        className="px-1.5 py-0.5 bg-ink-700 rounded text-slate-300 hover:bg-ink-600"
                      >
                        +2
                      </button>
                    </div>
                  </div>
                </div>

                {(manual0 != null || manual1 != null) && (
                  <div className="mt-2 flex items-center justify-between text-[11px] bg-amber-500/10 border border-amber-500/30 px-2 py-1 rounded text-amber-300">
                    <span>⚠️ Manual Anchor Override Active</span>
                    <button onClick={handleResetAnchors} className="underline text-amber-200 hover:text-white">
                      Reset to Auto-detected Wicks
                    </button>
                  </div>
                )}
              </div>

              {/* Target Cards: Minor Reversal & Major Reversal */}
              <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                {/* Minor Reversal Card */}
                <div className="card p-3 border border-red-500/40 bg-gradient-to-b from-red-950/20 to-ink-900 rounded-lg">
                  <div className="flex items-center justify-between border-b border-red-900/40 pb-1.5 mb-2">
                    <span className="font-bold text-red-300 flex items-center gap-1.5">
                      <span className="inline-block w-2 h-2 rounded-full bg-red-500 animate-pulse" />
                      🎯 MINOR REVERSAL ZONE
                    </span>
                    <span className="text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded bg-red-900/40 text-red-200">
                      -2.0 to -2.5 STD DEV
                    </span>
                  </div>

                  <div className="text-[20px] font-extrabold text-white num mb-1">
                    {activeLeg.minorZone[0].toFixed(1)} – {activeLeg.minorZone[1].toFixed(1)}
                  </div>

                  <div className="text-[11px] text-slate-300 mb-2">
                    {spot != null ? (
                      spot >= activeLeg.minorZone[0] && spot <= activeLeg.minorZone[1] ? (
                        <span className="text-red-400 font-bold">★ CURRENTLY INSIDE ZONE — Watch for MSS / Pullback</span>
                      ) : activeLeg.dir === "bull" ? (
                        spot < activeLeg.minorZone[0] ? (
                          <span>Distance: <b className="text-white">{(activeLeg.minorZone[0] - spot).toFixed(1)} pts</b> away</span>
                        ) : (
                          <span className="text-slate-400">Target Cleared ({(spot - activeLeg.minorZone[1]).toFixed(1)} pts above)</span>
                        )
                      ) : (
                        spot > activeLeg.minorZone[1] ? (
                          <span>Distance: <b className="text-white">{(spot - activeLeg.minorZone[1]).toFixed(1)} pts</b> away</span>
                        ) : (
                          <span className="text-slate-400">Target Cleared ({(activeLeg.minorZone[0] - spot).toFixed(1)} pts below)</span>
                        )
                      )
                    ) : (
                      "Spot data loading..."
                    )}
                  </div>

                  <p className="text-[10px] text-slate-400 leading-relaxed border-t border-ink-800 pt-2">
                    <b>Institutional Rule:</b> Represents normal market expansion where short-term reactions and retracements are most likely. Take Profit 1 / protect profits.
                  </p>
                </div>

                {/* Major Reversal Card */}
                <div className="card p-3 border border-purple-500/40 bg-gradient-to-b from-purple-950/20 to-ink-900 rounded-lg">
                  <div className="flex items-center justify-between border-b border-purple-900/40 pb-1.5 mb-2">
                    <span className="font-bold text-purple-300 flex items-center gap-1.5">
                      <span className="inline-block w-2 h-2 rounded-full bg-purple-500" />
                      💥 MAJOR REVERSAL ZONE
                    </span>
                    <span className="text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded bg-purple-900/40 text-purple-200">
                      -4.0 to -4.5 STD DEV
                    </span>
                  </div>

                  <div className="text-[20px] font-extrabold text-white num mb-1">
                    {activeLeg.majorZone[0].toFixed(1)} – {activeLeg.majorZone[1].toFixed(1)}
                  </div>

                  <div className="text-[11px] text-slate-300 mb-2">
                    {spot != null ? (
                      spot >= activeLeg.majorZone[0] && spot <= activeLeg.majorZone[1] ? (
                        <span className="text-purple-400 font-bold">⚠️ EXTREME EXHAUSTION — Macro Reversal High Probability</span>
                      ) : activeLeg.dir === "bull" ? (
                        spot < activeLeg.majorZone[0] ? (
                          <span>Distance: <b className="text-white">{(activeLeg.majorZone[0] - spot).toFixed(1)} pts</b> away</span>
                        ) : (
                          <span className="text-slate-400">Overshoot Target ({(spot - activeLeg.majorZone[1]).toFixed(1)} pts above)</span>
                        )
                      ) : (
                        spot > activeLeg.majorZone[1] ? (
                          <span>Distance: <b className="text-white">{(spot - activeLeg.majorZone[1]).toFixed(1)} pts</b> away</span>
                        ) : (
                          <span className="text-slate-400">Overshoot Target ({(activeLeg.majorZone[0] - spot).toFixed(1)} pts below)</span>
                        )
                      )
                    ) : (
                      "Spot data loading..."
                    )}
                  </div>

                  <p className="text-[10px] text-slate-400 leading-relaxed border-t border-ink-800 pt-2">
                    <b>Institutional Rule:</b> Represents extreme price expansion where strong reversals or multi-session corrections become probable. Take Profit 2 / Terminal target.
                  </p>
                </div>
              </div>

              {/* Status Banner */}
              <div className="p-2.5 rounded bg-ink-850 border border-ink-700 flex items-center justify-between">
                <span className="text-[12px] font-semibold text-slate-200">
                  Readout: <span className="text-gold-300">{activeLeg.statusText}</span>
                </span>
                <span className="text-[11px] text-slate-400">
                  Spot: <b className="text-white num">{spot ? spot.toFixed(1) : "—"}</b>
                </span>
              </div>
            </>
          ) : (
            <div className="p-8 text-center text-slate-400 card border border-ink-800">
              {loading ? "Scanning manipulation sweeps..." : "No manipulation leg detected on this timeframe. Try selecting 15m, 1H, or 1D, or use the 2-Click Chart Tool."}
            </div>
          )}
        </div>

        {/* Right Side: MTF Confluence Matrix & Detected Legs Table */}
        <div className="flex flex-col gap-3 min-w-0">
          {/* MTF Confluence Matrix */}
          <div className="card p-3 border border-ink-700 bg-ink-900 rounded-lg">
            <div className="flex items-center justify-between border-b border-ink-800 pb-2 mb-2">
              <span className="font-bold text-gold-400 uppercase text-[11px] tracking-wider">
                Multi-Timeframe STDV Matrix (Weekly · Daily · 1H · 15m)
              </span>
              <span className="text-[10px] text-slate-500">Auto-synced</span>
            </div>

            <div className="grid grid-cols-4 gap-1 text-[11px] mb-2 text-center">
              {["1W", "1D", "1h", "15m"].map((tfKey) => {
                const leg = mtfLegs[tfKey];
                return (
                  <div
                    key={tfKey}
                    className={`p-2 rounded border ${
                      leg
                        ? leg.dir === "bull"
                          ? "bg-emerald-950/20 border-emerald-500/30 text-emerald-300"
                          : "bg-rose-950/20 border-rose-500/30 text-rose-300"
                        : "bg-ink-800 border-ink-700 text-slate-500"
                    }`}
                  >
                    <div className="font-bold text-[12px]">{tfKey}</div>
                    <div className="text-[10px] font-medium mt-0.5">
                      {leg ? (leg.dir === "bull" ? "▲ Bull" : "▼ Bear") : "No Leg"}
                    </div>
                    {leg && (
                      <div className="text-[10px] text-slate-400 mt-1 num">
                        Minor: {leg.minorZone[0].toFixed(0)}–{leg.minorZone[1].toFixed(0)}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>

            <p className="text-[10px] text-slate-400 leading-relaxed border-t border-ink-800 pt-1.5">
              💡 <b>Cluster Confluence Strategy:</b> High probability turning points occur when lower timeframe Major Reversal (-4σ) overlaps with higher timeframe Minor Reversal (-2σ).
            </p>
          </div>

          {/* Historical / Detected Legs Table */}
          <div className="card flex-1 flex flex-col p-3 border border-ink-700 bg-ink-900 rounded-lg min-h-[220px]">
            <div className="flex items-center justify-between border-b border-ink-800 pb-2 mb-2">
              <span className="font-bold text-slate-300 text-[11px] uppercase tracking-wider">
                Detected Manipulation Legs ({activeTfId})
              </span>
              <span className="text-[10px] text-slate-500">Click row to inspect & project</span>
            </div>

            <div className="flex-1 overflow-y-auto">
              {filteredLegs.length === 0 ? (
                <div className="p-4 text-center text-slate-500 text-[11px]">No sweeps found with current filter</div>
              ) : (
                <table className="w-full text-[11px] num">
                  <thead>
                    <tr className="text-slate-500 border-b border-ink-800 text-left text-[10px] uppercase">
                      <th className="pb-1.5 font-medium">Type</th>
                      <th className="pb-1.5 font-medium">Time</th>
                      <th className="pb-1.5 font-medium">Sweep Wick</th>
                      <th className="pb-1.5 font-medium">Range</th>
                      <th className="pb-1.5 font-medium">Target (-2..-2.5)</th>
                      <th className="pb-1.5 font-medium text-right">Action</th>
                    </tr>
                  </thead>
                  <tbody>
                    {filteredLegs.map((l) => {
                      const isCur = activeLeg?.id === l.id;
                      return (
                        <tr
                          key={l.id}
                          onClick={() => handleSelectLeg(l)}
                          className={`border-b border-ink-800/60 cursor-pointer transition-colors ${
                            isCur ? "bg-gold-500/10 text-white font-semibold" : "hover:bg-ink-800/50 text-slate-300"
                          }`}
                        >
                          <td className="py-1.5">
                            <span
                              className={`px-1.5 py-0.5 rounded text-[10px] font-bold ${
                                l.dir === "bull" ? "bg-emerald-500/20 text-emerald-300" : "bg-rose-500/20 text-rose-300"
                              }`}
                            >
                              {l.dir === "bull" ? "▲ Low→High" : "▼ High→Low"}
                            </span>
                          </td>
                          <td className="py-1.5 text-slate-400">{formatTs(l.to)}</td>
                          <td className="py-1.5 text-white font-medium">{l.anchor1.toFixed(1)}</td>
                          <td className="py-1.5 text-gold-400">{l.range} pts</td>
                          <td className="py-1.5 text-red-300">
                            {l.minorZone[0].toFixed(0)} – {l.minorZone[1].toFixed(0)}
                          </td>
                          <td className="py-1.5 text-right">
                            <button
                              onClick={(e) => {
                                e.stopPropagation();
                                handleSelectLeg(l);
                              }}
                              className={`px-2 py-0.5 rounded text-[10px] ${
                                isCur ? "bg-cyan-500 text-black font-bold" : "bg-ink-700 text-slate-300 hover:bg-ink-600"
                              }`}
                            >
                              {isCur ? "Active" : "Project"}
                            </button>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
