// OIMatrixSuite.tsx — Institutional Open Interest Matrix & Strike Map 2.0.
// Surpasses commercial platforms like StockMojo with:
//   1. Real-time strike-wise Open Interest distribution & change.
//   2. Automated Buildup Classification (Long Buildup, Short Buildup, Short Covering, Long Unwinding).
//   3. Institutional color semantics (Put Support = Emerald #10b981, Call Resistance = Crimson #ef4444).
//   4. Interactive ATM Strike Range filter (Auto ATM ±10, Auto ATM ±5, Full Chain).
//   5. Multi-Tab Visual Chart Architecture:
//      - [ 📊 OI Change + Total ] (Grouped dual-bar with fresh ΔOI hatched pattern overlay)
//      - [ ⚡ OI Change Only ] (Center-zero fresh institutional money flow per strike)
//      - [ 🌊 Total OI Only ] (Clean structural support and resistance walls)
//      - [ 📋 Buildup Matrix Table ] (Detailed strike-by-strike tabular breakdown)
//   6. Side-by-side grouped vertical bars, Spot Price marker & Max Pain target lines.
//   7. Glassmorphic interactive hover tooltip.
//   8. Automated Market Sentiment Speedometer Gauge (0–100% Bullish / Bearish score).
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { inr } from "./OcLab";

export interface StrikeData {
  strike: number;
  is_atm: boolean;
  dist_atm: number;
  ce_oi: number;
  pe_oi: number;
  ce_oi_chg: number;
  pe_oi_chg: number;
  call_oi?: number;
  call_oi_chg?: number;
  put_oi?: number;
  put_oi_chg?: number;
  ce_ltp: number;
  pe_ltp: number;
  ce_chg: number;
  pe_chg: number;
  ce_vol: number;
  pe_vol: number;
  ce_buildup: "LONG_BUILDUP" | "SHORT_BUILDUP" | "SHORT_COVERING" | "LONG_UNWINDING" | "NEUTRAL";
  ce_buildup_label: string;
  ce_meaning: string;
  pe_buildup: "LONG_BUILDUP" | "SHORT_BUILDUP" | "SHORT_COVERING" | "LONG_UNWINDING" | "NEUTRAL";
  pe_buildup_label: string;
  pe_meaning: string;
  buildup?: string;
  call_buildup?: string;
  put_buildup?: string;
  net_oi: number;
  net_oi_chg: number;
  dominant_bias: string;
  dominant_tag: string;
  spot?: number;
  max_pain?: number;
}

export interface OIMatrixPayload {
  ok: boolean;
  day: string;
  expiry: string;
  selected_expiry: string;
  scope: "atm_10" | "atm_5" | "full";
  strike_range_label: string;
  spot: number;
  atm_strike: number;
  step: number;
  max_pain: number;
  call_wall: number;
  put_wall: number;
  call_wall_oi: number;
  put_wall_oi: number;
  totals: {
    ce_oi: number;
    pe_oi: number;
    ce_oi_chg: number;
    pe_oi_chg: number;
    net_oi: number;
    net_oi_chg: number;
    pcr_oi: number;
    pcr_vol: number;
    ce_vol: number;
    pe_vol: number;
  };
  sentiment: {
    score: number;
    bias: "BULLISH" | "BEARISH" | "NEUTRAL";
    bullish_pct: number;
    bearish_pct: number;
    gauge_label: string;
    summary: string;
    score_delta: number;
    score_buildup: number;
    score_pcr: number;
  };
  strikes: StrikeData[];
  strike_map?: StrikeData[];
  top_buildups: StrikeData[];
  message?: string;
}

// ────────────────────────────────────────── Speedometer Gauge ──────────────────────────────────────────
export function SentimentSpeedometer({
  score,
  bias,
  label,
}: {
  score: number;
  bias: "BULLISH" | "BEARISH" | "NEUTRAL";
  label: string;
}) {
  const angle = Math.min(90, Math.max(-90, (score / 100) * 180 - 90));
  const rad = (angle * Math.PI) / 180;
  const needleLen = 52;
  const cx = 80;
  const cy = 72;
  const nx = cx + needleLen * Math.sin(rad);
  const ny = cy - needleLen * Math.cos(rad);

  const gaugeColor =
    bias === "BULLISH" ? "#10b981" : bias === "BEARISH" ? "#ef4444" : "#eab308";

  return (
    <div className="flex flex-col items-center justify-center">
      <svg width="160" height="92" viewBox="0 0 160 92" className="overflow-visible select-none">
        <defs>
          <linearGradient id="gaugeArcGrad" x1="0%" y1="0%" x2="100%" y2="0%">
            <stop offset="0%" stopColor="#ef4444" />
            <stop offset="35%" stopColor="#f87171" />
            <stop offset="50%" stopColor="#eab308" />
            <stop offset="65%" stopColor="#34d399" />
            <stop offset="100%" stopColor="#10b981" />
          </linearGradient>
          <filter id="needleGlow" x="-20%" y="-20%" width="140%" height="140%">
            <feDropShadow dx="0" dy="1" stdDeviation="2" floodColor={gaugeColor} floodOpacity="0.8" />
          </filter>
        </defs>

        <path
          d="M 16 72 A 64 64 0 0 1 144 72"
          fill="none"
          stroke="#1b253b"
          strokeWidth="11"
          strokeLinecap="round"
        />
        <path
          d="M 16 72 A 64 64 0 0 1 144 72"
          fill="none"
          stroke="url(#gaugeArcGrad)"
          strokeWidth="8"
          strokeLinecap="round"
          strokeOpacity="0.9"
        />

        <line x1="80" y1="8" x2="80" y2="16" stroke="#eab308" strokeWidth="2" strokeOpacity="0.8" />
        <line x1="16" y1="72" x2="24" y2="72" stroke="#ef4444" strokeWidth="2" strokeOpacity="0.8" />
        <line x1="136" y1="72" x2="144" y2="72" stroke="#10b981" strokeWidth="2" strokeOpacity="0.8" />

        <text x="14" y="87" fill="#ef4444" fontSize="9" fontWeight="bold">0%</text>
        <text x="73" y="27" fill="#eab308" fontSize="9" fontWeight="bold">50%</text>
        <text x="132" y="87" fill="#10b981" fontSize="9" fontWeight="bold">100%</text>

        <line
          x1={cx}
          y1={cy}
          x2={nx}
          y2={ny}
          stroke={gaugeColor}
          strokeWidth="3.2"
          strokeLinecap="round"
          filter="url(#needleGlow)"
          className="transition-all duration-500 ease-out"
        />
        <circle cx={cx} cy={cy} r="6" fill="#0f172a" stroke={gaugeColor} strokeWidth="2.5" />
      </svg>

      <div className="mt-[-8px] text-center">
        <div className="text-base font-black tracking-tight" style={{ color: gaugeColor }}>
          {label}
        </div>
        <div className="text-[10px] font-semibold text-slate-400 uppercase tracking-wider">
          Market Sentiment Bias
        </div>
      </div>
    </div>
  );
}

// ────────────────────────────────────────── Buildup Badge Component ──────────────────────────────────────────
export function BuildupTag({
  code,
  label,
  isCe,
}: {
  code: string;
  label: string;
  isCe: boolean;
}) {
  if (code === "NEUTRAL") {
    return <span className="text-slate-500 text-[10.5px]">Neutral</span>;
  }

  let bgClass = "bg-slate-900 border-slate-700 text-slate-300";
  if (code === "SHORT_BUILDUP") {
    bgClass = isCe
      ? "bg-rose-950/80 text-rose-300 border-rose-600/80"
      : "bg-emerald-950/80 text-emerald-300 border-emerald-600/80";
  } else if (code === "LONG_BUILDUP") {
    bgClass = isCe
      ? "bg-emerald-950/70 text-emerald-300 border-emerald-600/70"
      : "bg-rose-950/70 text-rose-300 border-rose-600/70";
  } else if (code === "SHORT_COVERING") {
    bgClass = isCe
      ? "bg-cyan-950/80 text-cyan-300 border-cyan-500/80"
      : "bg-amber-950/80 text-amber-300 border-amber-500/80";
  } else if (code === "LONG_UNWINDING") {
    bgClass = "bg-slate-900 text-slate-400 border-slate-700";
  }

  return (
    <span className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-bold border ${bgClass}`}>
      <span>{label}</span>
    </span>
  );
}

// ────────────────────────────────────────── Interactive Visual Strike Bar Chart ──────────────────────────────────────────
interface InteractiveStrikeBarChartProps {
  strikes: StrikeData[];
  spot: number;
  maxPain: number;
  step: number;
  chartMode: "both" | "oi_chg" | "total_oi";
}

export function InteractiveStrikeBarChart({
  strikes,
  spot,
  maxPain,
  chartMode,
}: InteractiveStrikeBarChartProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [dim, setDim] = useState<{ width: number; height: number }>({ width: 900, height: 380 });
  const [hoveredIdx, setHoveredIdx] = useState<number | null>(null);
  const [hoverPos, setHoverPos] = useState<{ x: number; y: number } | null>(null);

  useEffect(() => {
    if (!containerRef.current) return;
    const obs = new ResizeObserver((entries) => {
      for (const entry of entries) {
        const { width, height } = entry.contentRect;
        if (width > 0 && height > 0) {
          setDim({ width: Math.floor(width), height: Math.floor(height) });
        }
      }
    });
    obs.observe(containerRef.current);
    return () => obs.disconnect();
  }, []);

  const N = strikes.length;
  const minSlotW = 34;
  const neededW = N * minSlotW + 100;
  const renderW = Math.max(dim.width, neededW);
  const renderH = Math.max(340, dim.height);

  const margin = { top: 38, right: 35, bottom: 44, left: 65 };
  const innerW = renderW - margin.left - margin.right;
  const innerH = renderH - margin.top - margin.bottom;

  const slotW = innerW / Math.max(1, N);
  const barW = Math.max(4, Math.min(24, (slotW - 6) / 2));

  // Max calculations
  const maxTotalVal = useMemo(() => {
    if (!strikes.length) return 1000000;
    const vals = strikes.flatMap((s) => [s.ce_oi, s.pe_oi]);
    return Math.max(...vals, 100000) * 1.15;
  }, [strikes]);

  const { deltaSpan, yZeroPos } = useMemo(() => {
    if (!strikes.length) return { deltaSpan: 100000, yZeroPos: margin.top + innerH };
    const posVals = strikes.flatMap((s) => [Math.max(0, s.ce_oi_chg), Math.max(0, s.pe_oi_chg)]);
    const negVals = strikes.flatMap((s) => [Math.min(0, s.ce_oi_chg), Math.min(0, s.pe_oi_chg)]);
    const maxP = Math.max(...posVals, 10000) * 1.15;
    const minN = Math.min(...negVals, 0) * 1.15;
    const span = maxP - minN || 1;
    const y0 = margin.top + (maxP / span) * innerH;
    return { deltaSpan: span, yZeroPos: y0 };
  }, [strikes, innerH, margin.top]);

  const getStrikeCenterX = useCallback(
    (idx: number) => margin.left + (idx + 0.5) * slotW,
    [margin.left, slotW]
  );

  const spotX = useMemo(() => {
    if (!strikes.length || !spot) return null;
    if (spot <= strikes[0].strike) return getStrikeCenterX(0);
    if (spot >= strikes[strikes.length - 1].strike) return getStrikeCenterX(strikes.length - 1);
    for (let i = 0; i < strikes.length - 1; i++) {
      const s1 = strikes[i].strike;
      const s2 = strikes[i + 1].strike;
      if (spot >= s1 && spot <= s2) {
        const frac = (spot - s1) / (s2 - s1);
        return getStrikeCenterX(i) + frac * (getStrikeCenterX(i + 1) - getStrikeCenterX(i));
      }
    }
    return null;
  }, [strikes, spot, getStrikeCenterX]);

  const maxPainX = useMemo(() => {
    if (!strikes.length || !maxPain) return null;
    const exactIdx = strikes.findIndex((s) => s.strike === maxPain);
    if (exactIdx >= 0) return getStrikeCenterX(exactIdx);
    for (let i = 0; i < strikes.length - 1; i++) {
      const s1 = strikes[i].strike;
      const s2 = strikes[i + 1].strike;
      if (maxPain >= s1 && maxPain <= s2) {
        const frac = (maxPain - s1) / (s2 - s1);
        return getStrikeCenterX(i) + frac * (getStrikeCenterX(i + 1) - getStrikeCenterX(i));
      }
    }
    return null;
  }, [strikes, maxPain, getStrikeCenterX]);

  const handleMouseMove = (e: React.MouseEvent<SVGSVGElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const y = e.clientY - rect.top;
    if (x < margin.left || x > margin.left + innerW || y < margin.top || y > margin.top + innerH + 30) {
      setHoveredIdx(null);
      setHoverPos(null);
      return;
    }
    const idx = Math.min(strikes.length - 1, Math.max(0, Math.floor((x - margin.left) / slotW)));
    setHoveredIdx(idx);
    setHoverPos({ x, y });
  };

  const handleMouseLeave = () => {
    setHoveredIdx(null);
    setHoverPos(null);
  };

  // Y-axis gridlines
  const yTicks = useMemo(() => {
    const ticks: { y: number; val: number }[] = [];
    if (chartMode === "oi_chg") {
      ticks.push({ y: yZeroPos, val: 0 });
      ticks.push({ y: margin.top, val: deltaSpan });
      ticks.push({ y: margin.top + innerH, val: -deltaSpan });
    } else {
      const steps = 4;
      for (let i = 0; i <= steps; i++) {
        const frac = i / steps;
        const y = margin.top + innerH - frac * innerH;
        const val = frac * maxTotalVal;
        ticks.push({ y, val });
      }
    }
    return ticks;
  }, [chartMode, yZeroPos, deltaSpan, margin.top, innerH, maxTotalVal]);

  const hoveredStrike = hoveredIdx !== null && strikes[hoveredIdx] ? strikes[hoveredIdx] : null;
  const netDiff = hoveredStrike ? hoveredStrike.pe_oi - hoveredStrike.ce_oi : 0;

  return (
    <div ref={containerRef} className="relative h-full w-full overflow-x-auto select-none scrollbar-thin">
      <svg
        width={renderW}
        height={renderH}
        onMouseMove={handleMouseMove}
        onMouseLeave={handleMouseLeave}
        className="overflow-visible block"
      >
        <defs>
          {/* Hatched pattern for Call intraday fresh addition */}
          <pattern id="hash-ce" width="6" height="6" patternTransform="rotate(45 0 0)" patternUnits="userSpaceOnUse">
            <line x1="0" y1="0" x2="0" y2="6" stroke="#fecaca" strokeWidth="2" strokeOpacity="0.9" />
          </pattern>
          {/* Hatched pattern for Put intraday fresh addition */}
          <pattern id="hash-pe" width="6" height="6" patternTransform="rotate(45 0 0)" patternUnits="userSpaceOnUse">
            <line x1="0" y1="0" x2="0" y2="6" stroke="#a7f3d0" strokeWidth="2" strokeOpacity="0.9" />
          </pattern>

          {/* Gradients — StockMojo Default: CE = Green (#22c55e), PE = Red (#ef4444) */}
          <linearGradient id="ce-grad" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#22c55e" stopOpacity="0.95" />
            <stop offset="100%" stopColor="#15803d" stopOpacity="0.85" />
          </linearGradient>
          <linearGradient id="pe-grad" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#ef4444" stopOpacity="0.95" />
            <stop offset="100%" stopColor="#991b1b" stopOpacity="0.85" />
          </linearGradient>
        </defs>

        {/* Horizontal gridlines */}
        {yTicks.map((t, idx) => (
          <g key={idx}>
            <line
              x1={margin.left}
              y1={t.y}
              x2={margin.left + innerW}
              y2={t.y}
              stroke="#162238"
              strokeDasharray={t.val === 0 ? "none" : "2 2"}
              strokeWidth={t.val === 0 ? "1.5" : "1"}
            />
            <text
              x={margin.left - 8}
              y={t.y + 3.5}
              textAnchor="end"
              fill="#64748b"
              fontSize="9.5"
              fontFamily="monospace"
            >
              {inr(t.val)}
            </text>
          </g>
        ))}

        {/* Strikes & Bars */}
        {strikes.map((s, i) => {
          const cx = getStrikeCenterX(i);
          const ceLeft = cx - barW - 1;
          const peLeft = cx + 1;

          let ceH = 0, peH = 0, ceY = 0, peY = 0;
          let ceOverlayH = 0, peOverlayH = 0, ceOverlayY = 0, peOverlayY = 0;

          if (chartMode === "total_oi") {
            ceH = (s.ce_oi / maxTotalVal) * innerH;
            peH = (s.pe_oi / maxTotalVal) * innerH;
            ceY = margin.top + innerH - ceH;
            peY = margin.top + innerH - peH;
          } else if (chartMode === "both") {
            ceH = (s.ce_oi / maxTotalVal) * innerH;
            peH = (s.pe_oi / maxTotalVal) * innerH;
            ceY = margin.top + innerH - ceH;
            peY = margin.top + innerH - peH;

            if (s.ce_oi_chg > 0) {
              ceOverlayH = (Math.min(s.ce_oi_chg, s.ce_oi) / maxTotalVal) * innerH;
              ceOverlayY = ceY;
            }
            if (s.pe_oi_chg > 0) {
              peOverlayH = (Math.min(s.pe_oi_chg, s.pe_oi) / maxTotalVal) * innerH;
              peOverlayY = peY;
            }
          } else {
            // oi_chg mode
            if (s.ce_oi_chg >= 0) {
              ceH = (s.ce_oi_chg / deltaSpan) * innerH;
              ceY = yZeroPos - ceH;
            } else {
              ceH = (Math.abs(s.ce_oi_chg) / deltaSpan) * innerH;
              ceY = yZeroPos;
            }

            if (s.pe_oi_chg >= 0) {
              peH = (s.pe_oi_chg / deltaSpan) * innerH;
              peY = yZeroPos - peH;
            } else {
              peH = (Math.abs(s.pe_oi_chg) / deltaSpan) * innerH;
              peY = yZeroPos;
            }
          }

          const isHovered = hoveredIdx === i;

          return (
            <g key={s.strike}>
              {/* Hover highlight band */}
              {isHovered && (
                <rect
                  x={cx - slotW / 2}
                  y={margin.top}
                  width={slotW}
                  height={innerH}
                  fill="#38bdf8"
                  fillOpacity="0.08"
                  rx="4"
                />
              )}

              {/* Call (CE) Bar — Green */}
              <rect
                x={ceLeft}
                y={ceY}
                width={barW}
                height={Math.max(ceH, 1)}
                rx="2"
                fill={chartMode === "oi_chg" && s.ce_oi_chg < 0 ? "#22c55e55" : "url(#ce-grad)"}
                stroke={chartMode === "oi_chg" && s.ce_oi_chg < 0 ? "#22c55e" : "#4ade80"}
                strokeWidth="0.75"
                strokeDasharray={chartMode === "oi_chg" && s.ce_oi_chg < 0 ? "2 2" : "none"}
              />

              {/* Call (CE) Delta Hash Overlay for 'both' mode */}
              {chartMode === "both" && ceOverlayH > 0 && (
                <rect
                  x={ceLeft}
                  y={ceOverlayY}
                  width={barW}
                  height={ceOverlayH}
                  rx="2"
                  fill="url(#hash-ce)"
                  stroke="#86efac"
                  strokeWidth="0.75"
                />
              )}

              {/* Put (PE) Bar — Red */}
              <rect
                x={peLeft}
                y={peY}
                width={barW}
                height={Math.max(peH, 1)}
                rx="2"
                fill={chartMode === "oi_chg" && s.pe_oi_chg < 0 ? "#ef444455" : "url(#pe-grad)"}
                stroke={chartMode === "oi_chg" && s.pe_oi_chg < 0 ? "#ef4444" : "#f87171"}
                strokeWidth="0.75"
                strokeDasharray={chartMode === "oi_chg" && s.pe_oi_chg < 0 ? "2 2" : "none"}
              />

              {/* Put (PE) Delta Hash Overlay for 'both' mode */}
              {chartMode === "both" && peOverlayH > 0 && (
                <rect
                  x={peLeft}
                  y={peOverlayY}
                  width={barW}
                  height={peOverlayH}
                  rx="2"
                  fill="url(#hash-pe)"
                  stroke="#fca5a5"
                  strokeWidth="0.75"
                />
              )}

              {/* Strike label */}
              <text
                x={cx}
                y={margin.top + innerH + 16}
                textAnchor="middle"
                fill={s.is_atm ? "#38bdf8" : s.strike === maxPain ? "#f59e0b" : "#94a3b8"}
                fontWeight={s.is_atm || s.strike === maxPain ? "bold" : "normal"}
                fontSize={N > 25 ? "9" : "10.5"}
                fontFamily="monospace"
              >
                {s.strike}
              </text>

              {/* ATM badge */}
              {s.is_atm && (
                <g transform={`translate(${cx}, ${margin.top + innerH + 21})`}>
                  <rect x="-14" y="0" width="28" height="12" rx="3" fill="#0284c7" />
                  <text x="0" y="9" textAnchor="middle" fill="#ffffff" fontSize="8" fontWeight="bold">
                    ATM
                  </text>
                </g>
              )}
            </g>
          );
        })}

        {/* Spot Price vertical dashed line */}
        {spotX != null && (
          <g>
            <line
              x1={spotX}
              y1={margin.top - 6}
              x2={spotX}
              y2={margin.top + innerH}
              stroke="#38bdf8"
              strokeWidth="1.8"
              strokeDasharray="4 3"
              opacity="0.9"
            />
            <g transform={`translate(${spotX}, ${margin.top - 18})`}>
              <rect
                x="-38"
                y="-11"
                width="76"
                height="18"
                rx="4"
                fill="#0c2340"
                stroke="#38bdf8"
                strokeWidth="1"
              />
              <text textAnchor="middle" y="2" fill="#38bdf8" fontSize="9.5" fontWeight="bold">
                Spot {spot.toFixed(1)}
              </text>
            </g>
          </g>
        )}

        {/* Max Pain vertical dashed line */}
        {maxPainX != null && (
          <g>
            <line
              x1={maxPainX}
              y1={margin.top - 6}
              x2={maxPainX}
              y2={margin.top + innerH}
              stroke="#f59e0b"
              strokeWidth="1.8"
              strokeDasharray="4 3"
              opacity="0.9"
            />
            <g transform={`translate(${maxPainX}, ${margin.top - 18})`}>
              <rect
                x="-44"
                y="-11"
                width="88"
                height="18"
                rx="4"
                fill="#2b1b05"
                stroke="#f59e0b"
                strokeWidth="1"
              />
              <text textAnchor="middle" y="2" fill="#f59e0b" fontSize="9.5" fontWeight="bold">
                🎯 Max Pain {maxPain}
              </text>
            </g>
          </g>
        )}
      </svg>

      {/* Floating Glassmorphic Hover Card */}
      {hoveredStrike && hoverPos && (
        <div
          className="pointer-events-none absolute z-30 transition-transform duration-75"
          style={{
            left: hoverPos.x > renderW - 280 ? Math.max(10, hoverPos.x - 275) : hoverPos.x + 18,
            top: Math.max(8, Math.min(renderH - 190, hoverPos.y - 30)),
          }}
        >
          <div className="w-64 rounded-lg border border-slate-700/80 bg-[#070d1a]/95 p-3 shadow-2xl backdrop-blur-md text-[11px] font-sans">
            <div className="flex items-center justify-between border-b border-slate-800 pb-1.5 mb-1.5">
              <div className="flex items-center gap-1.5">
                <span className="font-mono font-black text-sm text-white">Strike {hoveredStrike.strike}</span>
                {hoveredStrike.is_atm && (
                  <span className="rounded bg-sky-500/30 px-1.5 py-0.2 text-[9px] font-bold text-sky-300 border border-sky-500/50">
                    ATM
                  </span>
                )}
                {hoveredStrike.strike === maxPain && (
                  <span className="rounded bg-amber-500/30 px-1.5 py-0.2 text-[9px] font-bold text-amber-300 border border-amber-500/50">
                    🎯 Max Pain
                  </span>
                )}
              </div>
              <span className="text-[10px] text-slate-400 font-mono">
                {hoveredStrike.dist_atm > 0 ? `+${hoveredStrike.dist_atm} pts` : hoveredStrike.dist_atm < 0 ? `${hoveredStrike.dist_atm} pts` : "ATM"}
              </span>
            </div>

            {/* Call Line — Green (#22c55e) */}
            <div className="flex items-center justify-between py-0.5">
              <div className="flex items-center gap-1.5">
                <span className="h-2 w-2 rounded-full bg-emerald-500" />
                <span className="font-bold text-emerald-300">Call OI:</span>
              </div>
              <div className="font-mono text-right">
                <b className="text-white">{inr(hoveredStrike.ce_oi)}</b>
                <span className={`ml-1 text-[10px] ${hoveredStrike.ce_oi_chg > 0 ? "text-emerald-400" : "text-rose-400"}`}>
                  ({inr(hoveredStrike.ce_oi_chg, true)})
                </span>
              </div>
            </div>
            <div className="text-[9.5px] text-slate-400 pl-3.5 mb-1">
              {hoveredStrike.ce_buildup_label} • {hoveredStrike.ce_meaning}
            </div>

            {/* Put Line — Red (#ef4444) */}
            <div className="flex items-center justify-between py-0.5">
              <div className="flex items-center gap-1.5">
                <span className="h-2 w-2 rounded-full bg-rose-500" />
                <span className="font-bold text-rose-300">Put OI:</span>
              </div>
              <div className="font-mono text-right">
                <b className="text-white">{inr(hoveredStrike.pe_oi)}</b>
                <span className={`ml-1 text-[10px] ${hoveredStrike.pe_oi_chg > 0 ? "text-rose-400" : "text-emerald-400"}`}>
                  ({inr(hoveredStrike.pe_oi_chg, true)})
                </span>
              </div>
            </div>
            <div className="text-[9.5px] text-slate-400 pl-3.5 mb-1.5">
              {hoveredStrike.pe_buildup_label} • {hoveredStrike.pe_meaning}
            </div>

            {/* Net Sentiment */}
            <div className="border-t border-slate-800/80 pt-1.5 space-y-0.5">
              <div className="flex items-center justify-between text-[10.5px]">
                <span className="text-slate-400">Net Sentiment:</span>
                <span className={`font-bold ${netDiff > 0 ? "text-emerald-300" : "text-rose-300"}`}>
                  {netDiff > 0 ? "Put Support Dominant" : "Call Resistance Dominant"}
                </span>
              </div>
              <div className="flex items-center justify-between text-[10px] font-mono text-slate-400">
                <span>OI Spread:</span>
                <span className="text-slate-200">{inr(Math.abs(netDiff))} ({netDiff > 0 ? "PE Lead" : "CE Lead"})</span>
              </div>
              <div className="pt-0.5 text-[9.5px] font-bold text-gold-300">
                {hoveredStrike.dominant_tag}
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// ────────────────────────────────────────── Main Component ──────────────────────────────────────────
export interface OIMatrixSuiteProps {
  tick?: number;
  onClose?: () => void;
  modeToggle?: React.ReactNode;
}

export default function OIMatrixSuite({ tick = 0, onClose, modeToggle }: OIMatrixSuiteProps) {
  const [expiryTab, setExpiryTab] = useState<"cur" | "next" | "both">(() => {
    try {
      const s = localStorage.getItem("ict_oi_matrix_exp");
      if (s === "cur" || s === "next" || s === "both") return s;
    } catch { /* ignore */ }
    return "cur";
  });

  const [scope, setScope] = useState<"atm_10" | "atm_5" | "full">(() => {
    try {
      const s = localStorage.getItem("ict_oi_matrix_scope");
      if (s === "atm_10" || s === "atm_5" || s === "full") return s;
    } catch { /* ignore */ }
    return "atm_10";
  });

  // Visual Multi-Tab Switcher Mode: [ 📊 OI Change + Total ] | [ ⚡ OI Change Only ] | [ 🌊 Total OI Only ] | [ 📋 Buildup Matrix Table ]
  const [chartTab, setChartTab] = useState<"both" | "oi_chg" | "total_oi" | "table">(() => {
    try {
      const s = localStorage.getItem("ict_strike_map_tab");
      if (s === "both" || s === "oi_chg" || s === "total_oi" || s === "table") return s;
    } catch { /* ignore */ }
    return "both";
  });

  const [showTableDrawer, setShowTableDrawer] = useState<boolean>(false);
  const [filterActiveOnly, setFilterActiveOnly] = useState<boolean>(false);
  const [searchQuery, setSearchQuery] = useState<string>("");

  const [data, setData] = useState<OIMatrixPayload | null>(null);
  const [busy, setBusy] = useState<boolean>(false);

  const fetchMatrix = useCallback(() => {
    setBusy(true);
    fetch(`/api/oi/matrix?expiry=${expiryTab}&scope=${scope}`)
      .then((r) => r.json())
      .then((json: OIMatrixPayload) => {
        if (json && json.ok) {
          setData(json);
        }
      })
      .catch((e) => console.error("fetch oi-matrix error:", e))
      .finally(() => setBusy(false));
  }, [expiryTab, scope]);

  useEffect(() => {
    fetchMatrix();
    const interval = window.setInterval(fetchMatrix, 15000);
    return () => window.clearInterval(interval);
  }, [fetchMatrix, tick]);

  const setExpiry = (e: "cur" | "next" | "both") => {
    setExpiryTab(e);
    try { localStorage.setItem("ict_oi_matrix_exp", e); } catch { /* ignore */ }
  };

  const setScopeFilter = (s: "atm_10" | "atm_5" | "full") => {
    setScope(s);
    try { localStorage.setItem("ict_oi_matrix_scope", s); } catch { /* ignore */ }
  };

  const setTab = (t: "both" | "oi_chg" | "total_oi" | "table") => {
    setChartTab(t);
    try { localStorage.setItem("ict_strike_map_tab", t); } catch { /* ignore */ }
  };

  const rawStrikes = data?.strikes || [];

  const filteredStrikes = useMemo(() => {
    return rawStrikes.filter((s) => {
      if (searchQuery) {
        const q = searchQuery.trim();
        if (!String(s.strike).includes(q)) return false;
      }
      if (filterActiveOnly) {
        const hasActivity =
          Math.abs(s.ce_oi_chg) > 25000 || Math.abs(s.pe_oi_chg) > 25000 || s.is_atm;
        if (!hasActivity) return false;
      }
      return true;
    });
  }, [rawStrikes, searchQuery, filterActiveOnly]);

  const totals = data?.totals;
  const sentiment = data?.sentiment;

  return (
    <div className="flex h-full w-full flex-col bg-[#070c17] text-slate-200 select-none overflow-hidden font-sans">
      {/* ────────────────────────────────── Top Control Header ────────────────────────────────── */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-[#162238] bg-[#091122] px-3 py-1.5 text-[11px] shrink-0">
        <div className="flex items-center gap-2">
          <span className="font-extrabold text-gold-400">⚡ Strike Map 2.0 (Institutional OI Matrix)</span>
          {modeToggle}
          <span className="text-slate-500 hidden xl:inline">
            • Real-Time Buildup • Support/Resistance Walls • Max Pain
          </span>
        </div>

        {/* Middle: Scope Filter Pills */}
        <div className="flex items-center gap-1.5">
          <span className="text-slate-400 font-bold uppercase text-[10px] mr-0.5">Scope:</span>
          <button
            onClick={() => setScopeFilter("atm_10")}
            className={`rounded px-2.5 py-0.5 text-[11px] font-bold border transition-all ${
              scope === "atm_10"
                ? "bg-emerald-500/30 text-emerald-200 border-emerald-500/60 shadow-sm"
                : "bg-[#0c1424] text-slate-400 border-slate-800 hover:text-slate-200"
            }`}
            title="Auto ATM ±10 Strikes (±500 pts) — Filters deep OTM noise"
          >
            Auto ATM ±10
          </button>
          <button
            onClick={() => setScopeFilter("atm_5")}
            className={`rounded px-2.5 py-0.5 text-[11px] font-bold border transition-all ${
              scope === "atm_5"
                ? "bg-emerald-500/30 text-emerald-200 border-emerald-500/60 shadow-sm"
                : "bg-[#0c1424] text-slate-400 border-slate-800 hover:text-slate-200"
            }`}
            title="Auto ATM ±5 Strikes (±250 pts) — Fast Scalp Scope"
          >
            Auto ATM ±5
          </button>
          <button
            onClick={() => setScopeFilter("full")}
            className={`rounded px-2.5 py-0.5 text-[11px] font-bold border transition-all ${
              scope === "full"
                ? "bg-emerald-500/30 text-emerald-200 border-emerald-500/60 shadow-sm"
                : "bg-[#0c1424] text-slate-400 border-slate-800 hover:text-slate-200"
            }`}
            title="Full Chain (All active strikes)"
          >
            Full Chain
          </button>
        </div>

        {/* Right: Expiry Tabs + Refresh */}
        <div className="flex items-center gap-2">
          <div className="flex rounded bg-[#0c1424] p-0.5 border border-slate-800">
            {(["cur", "next", "both"] as const).map((e) => (
              <button
                key={e}
                onClick={() => setExpiry(e)}
                className={`rounded px-2 py-0.5 text-[11px] font-bold transition-all ${
                  expiryTab === e
                    ? "bg-sky-500/30 text-sky-200 border border-sky-500/60 shadow-sm"
                    : "text-slate-400 hover:text-slate-200"
                }`}
              >
                {e === "cur" ? "This Wk" : e === "next" ? "Next Wk" : "Both"}
              </button>
            ))}
          </div>

          <button
            onClick={fetchMatrix}
            className="rounded bg-[#0c1424] px-2 py-0.5 text-[11px] font-bold text-slate-300 border border-slate-800 hover:bg-slate-800"
            title="Refresh now"
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

      {/* ────────────────────────────────── Executive Telemetry Cockpit (Cards) ────────────────────────────────── */}
      <div className="grid grid-cols-1 md:grid-cols-4 gap-2 p-2 bg-[#091122] border-b border-[#142036] shrink-0">
        {/* Card 1: Market Sentiment Speedometer */}
        <div className="flex flex-col items-center justify-between rounded-lg bg-[#070c17] p-2 border border-[#1b273d] shadow">
          {sentiment ? (
            <SentimentSpeedometer
              score={sentiment.score}
              bias={sentiment.bias}
              label={sentiment.gauge_label}
            />
          ) : (
            <div className="text-slate-500 text-xs">Computing sentiment...</div>
          )}
          <div className="text-[10px] text-center text-slate-400 mt-1 line-clamp-1">
            {sentiment?.summary}
          </div>
        </div>

        {/* Card 2: Cumulative PCR & Flow Balance */}
        <div className="flex flex-col justify-between rounded-lg bg-[#070c17] p-2.5 border border-[#1b273d] shadow">
          <div className="text-[11px] font-extrabold uppercase text-slate-400 tracking-wider">
            📊 Institutional PCR & Flow
          </div>
          <div className="space-y-1 my-1">
            <div className="flex items-center justify-between text-[11.5px]">
              <span className="text-slate-400">Cumulative PCR (OI):</span>
              <span className={`font-mono font-black ${
                (totals?.pcr_oi ?? 1) >= 1.15
                  ? "text-emerald-400"
                  : (totals?.pcr_oi ?? 1) <= 0.85
                  ? "text-rose-400"
                  : "text-amber-300"
              }`}>
                {totals?.pcr_oi?.toFixed(2) ?? "—"}
              </span>
            </div>
            <div className="flex items-center justify-between text-[11px]">
              <span className="text-slate-400">Total Put OI (Support):</span>
              <span className="font-mono text-emerald-400 font-semibold">{inr(totals?.pe_oi)}</span>
            </div>
            <div className="flex items-center justify-between text-[11px]">
              <span className="text-slate-400">Total Call OI (Resistance):</span>
              <span className="font-mono text-rose-400 font-semibold">{inr(totals?.ce_oi)}</span>
            </div>
            <div className="flex items-center justify-between text-[11px]">
              <span className="text-slate-400">Today's Fresh Net ΔOI:</span>
              <span className={`font-mono font-bold ${
                (totals?.net_oi_chg ?? 0) > 0 ? "text-emerald-400" : "text-rose-400"
              }`}>
                {inr(totals?.net_oi_chg, true)}
              </span>
            </div>
          </div>
          <div className="text-[10px] text-slate-400 flex justify-between">
            <span>Volume PCR: <b className="text-white font-mono">{totals?.pcr_vol?.toFixed(2) ?? "—"}</b></span>
            <span>Total OI Gap: <b className="text-sky-300 font-mono">{inr(totals?.net_oi, true)}</b></span>
          </div>
        </div>

        {/* Card 3: Key Walls & Max Pain */}
        <div className="flex flex-col justify-between rounded-lg bg-[#070c17] p-2.5 border border-[#1b273d] shadow">
          <div className="text-[11px] font-extrabold uppercase text-slate-400 tracking-wider">
            🎯 Max Pain & Key Walls
          </div>
          <div className="space-y-1 my-1">
            <div className="flex items-center justify-between text-[11.5px]">
              <span className="text-gold-400 font-bold flex items-center gap-1">
                <span>🎯 Max Pain:</span>
              </span>
              <span className="font-mono font-black text-white">
                {data?.max_pain ? `${data.max_pain.toLocaleString("en-IN")}` : "—"}
                {data?.spot && data.max_pain ? (
                  <span className="text-[10px] text-slate-400 font-normal ml-1">
                    ({data.spot > data.max_pain ? "+" : ""}
                    {(data.spot - data.max_pain).toFixed(0)} pts)
                  </span>
                ) : null}
              </span>
            </div>
            <div className="flex items-center justify-between text-[11px]">
              <span className="text-rose-400 font-bold">🔴 Call Resistance Wall:</span>
              <span className="font-mono text-white font-semibold">
                {data?.call_wall} <span className="text-slate-400 text-[10px]">({inr(data?.call_wall_oi)})</span>
              </span>
            </div>
            <div className="flex items-center justify-between text-[11px]">
              <span className="text-emerald-400 font-bold">🟢 Put Support Wall:</span>
              <span className="font-mono text-white font-semibold">
                {data?.put_wall} <span className="text-slate-400 text-[10px]">({inr(data?.put_wall_oi)})</span>
              </span>
            </div>
          </div>
          <div className="text-[10px] text-slate-400">
            NIFTY Spot: <b className="text-sky-300 font-mono">{data?.spot}</b> · ATM: <b className="text-white font-mono">{data?.atm_strike}</b>
          </div>
        </div>

        {/* Card 4: Top Institutional Fresh Action */}
        <div className="flex flex-col justify-between rounded-lg bg-[#070c17] p-2.5 border border-[#1b273d] shadow">
          <div className="text-[11px] font-extrabold uppercase text-slate-400 tracking-wider">
            🔥 Top Active Strike Action
          </div>
          <div className="space-y-1 my-1 overflow-y-auto max-h-16 scrollbar-none text-[11px]">
            {data?.top_buildups && data.top_buildups.length > 0 ? (
              data.top_buildups.slice(0, 3).map((s) => (
                <div key={s.strike} className="flex items-center justify-between border-b border-slate-800/60 pb-0.5">
                  <span className="font-bold text-white font-mono">{s.strike}</span>
                  <span className="text-[10px] text-slate-300">{s.dominant_tag}</span>
                  <span className="font-mono text-[10.5px] font-semibold text-sky-300">
                    {inr(s.net_oi_chg, true)}
                  </span>
                </div>
              ))
            ) : (
              <div className="text-slate-500 text-[10px]">Scanning active strikes...</div>
            )}
          </div>
          <div className="text-[10px] text-slate-400 flex justify-between">
            <span>Range: <b className="text-emerald-300">{data?.strike_range_label}</b></span>
          </div>
        </div>
      </div>

      {/* ────────────────────────────────── Tab Switcher Header ────────────────────────────────── */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-[#142036] bg-[#081020] px-3 py-1.5 text-[11px] shrink-0">
        <div className="flex items-center gap-1.5">
          <span className="text-slate-400 font-extrabold uppercase text-[10px] mr-1">View Mode:</span>
          <div className="flex rounded bg-[#0b1324] p-0.5 border border-slate-700">
            <button
              onClick={() => setTab("both")}
              className={`rounded px-2.5 py-0.5 text-[11px] font-extrabold transition-all ${
                chartTab === "both"
                  ? "bg-sky-500/30 text-sky-200 border border-sky-500/60 shadow-sm"
                  : "text-slate-400 hover:text-slate-200"
              }`}
              title="Dual-bar chart showing total standing commitment overlaid with today's intraday additions"
            >
              📊 OI Change + Total
            </button>
            <button
              onClick={() => setTab("oi_chg")}
              className={`rounded px-2.5 py-0.5 text-[11px] font-extrabold transition-all ${
                chartTab === "oi_chg"
                  ? "bg-sky-500/30 text-sky-200 border border-sky-500/60 shadow-sm"
                  : "text-slate-400 hover:text-slate-200"
              }`}
              title="Focuses purely on today's fresh institutional money inflow / outflow per strike"
            >
              ⚡ OI Change Only
            </button>
            <button
              onClick={() => setTab("total_oi")}
              className={`rounded px-2.5 py-0.5 text-[11px] font-extrabold transition-all ${
                chartTab === "total_oi"
                  ? "bg-sky-500/30 text-sky-200 border border-sky-500/60 shadow-sm"
                  : "text-slate-400 hover:text-slate-200"
              }`}
              title="Clean structural view of standing support and resistance walls across the active expiry"
            >
              🌊 Total OI Only
            </button>
            <button
              onClick={() => setTab("table")}
              className={`rounded px-2.5 py-0.5 text-[11px] font-extrabold transition-all ${
                chartTab === "table"
                  ? "bg-gold-500/30 text-gold-200 border border-gold-500/60 shadow-sm"
                  : "text-slate-400 hover:text-slate-200"
              }`}
              title="Detailed strike-by-strike buildup classification table"
            >
              📋 Buildup Matrix Table
            </button>
          </div>
        </div>

        {/* Legend pills & Filter */}
        <div className="flex items-center gap-3 text-[10px]">
          <div className="flex items-center gap-2">
            <span className="flex items-center gap-1 font-bold text-rose-400">
              <span className="h-2 w-2 rounded-full bg-rose-500" />
              Call Resistance (CE)
            </span>
            <span className="flex items-center gap-1 font-bold text-emerald-400">
              <span className="h-2 w-2 rounded-full bg-emerald-500" />
              Put Support (PE)
            </span>
            {chartTab === "both" && (
              <span className="flex items-center gap-1 font-bold text-slate-300">
                <span className="h-2.5 w-3 border border-slate-400 bg-[linear-gradient(45deg,#fff_25%,transparent_25%,transparent_50%,#fff_50%,#fff_75%,transparent_75%)] bg-[length:4px_4px]" />
                Hatched = Today's ΔOI
              </span>
            )}
          </div>

          <div className="h-3 w-px bg-slate-700 hidden sm:block" />

          {/* Search input & Active-only toggle */}
          <div className="flex items-center gap-1.5">
            <button
              onClick={() => setFilterActiveOnly((v) => !v)}
              className={`rounded px-1.5 py-0.5 text-[10px] font-bold border transition-all ${
                filterActiveOnly
                  ? "bg-purple-950 text-purple-300 border-purple-500/70"
                  : "bg-[#0c1424] text-slate-400 border-slate-800 hover:text-slate-200"
              }`}
            >
              {filterActiveOnly ? "⚡ Active Only" : "🌐 All Strikes"}
            </button>
            <input
              type="text"
              placeholder="Search..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="w-20 rounded bg-[#0c1424] border border-slate-800 px-1.5 py-0.5 text-[10px] font-mono text-white placeholder-slate-600 focus:outline-none focus:border-sky-500"
            />
          </div>
        </div>
      </div>

      {/* ────────────────────────────────── Main Body: Chart vs Table ────────────────────────────────── */}
      <div className="flex-1 min-h-0 flex flex-col overflow-hidden relative">
        {chartTab !== "table" ? (
          <div className="flex-1 min-h-0 flex flex-col overflow-hidden relative">
            <div className="flex-1 min-h-0 w-full relative">
              <InteractiveStrikeBarChart
                strikes={filteredStrikes}
                spot={data?.spot ?? 0}
                maxPain={data?.max_pain ?? 0}
                step={data?.step ?? 50}
                chartMode={chartTab}
              />
            </div>

            {/* Collapsible Table Drawer Toggle */}
            <div className="border-t border-[#142036] bg-[#091122] px-3 py-1 text-[11px] flex items-center justify-between shrink-0">
              <button
                onClick={() => setShowTableDrawer((v) => !v)}
                className="flex items-center gap-1 text-[10.5px] font-bold text-slate-400 hover:text-white"
              >
                <span>{showTableDrawer ? "▼ Hide Buildup Table" : "▲ Show Buildup Table"}</span>
                <span className="text-slate-500 font-mono">({filteredStrikes.length} strikes)</span>
              </button>
              <div className="text-[10px] text-slate-500">
                Tip: Hover over any strike to see institutional commitments and flow spread.
              </div>
            </div>

            {/* Drawer Table */}
            {showTableDrawer && (
              <div className="h-56 border-t border-slate-800 bg-[#070c17] overflow-auto scrollbar-thin shrink-0">
                <BuildupTable strikes={filteredStrikes} maxPain={data?.max_pain} />
              </div>
            )}
          </div>
        ) : (
          <div className="flex-1 min-h-0 flex flex-col bg-[#070c17] overflow-hidden">
            <div className="flex items-center justify-between border-b border-[#162238] bg-[#091122] px-3 py-1.5 text-[11px] shrink-0">
              <span className="font-extrabold text-slate-300">
                📋 Real-Time Strike Buildup Classification Matrix ({filteredStrikes.length} strikes in scope)
              </span>
              <div className="text-[10.5px] text-slate-400">
                Put = Support Floor • Call = Overhead Resistance
              </div>
            </div>
            <div className="flex-1 min-h-0 overflow-auto scrollbar-thin">
              <BuildupTable strikes={filteredStrikes} maxPain={data?.max_pain} />
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// ────────────────────────────────────────── Reusable Buildup Table ──────────────────────────────────────────
function BuildupTable({
  strikes,
  maxPain,
}: {
  strikes: StrikeData[];
  maxPain?: number;
}) {
  return (
    <table className="w-full border-collapse text-left font-mono text-[11px]">
      <thead className="sticky top-0 z-10 bg-[#091122] border-b border-slate-800 text-[10px] uppercase text-slate-400 tracking-wider">
        <tr>
          <th className="px-2 py-1.5 text-rose-300">Call OI</th>
          <th className="px-2 py-1.5 text-rose-300">Call Δ</th>
          <th className="px-2 py-1.5 text-rose-300">Call Buildup</th>
          <th className="px-2 py-1.5 text-center bg-[#0d1627] text-white">Strike</th>
          <th className="px-2 py-1.5 text-emerald-300">Put Buildup</th>
          <th className="px-2 py-1.5 text-emerald-300">Put Δ</th>
          <th className="px-2 py-1.5 text-emerald-300">Put OI</th>
          <th className="px-2 py-1.5 text-slate-300">Dominant Implication</th>
        </tr>
      </thead>
      <tbody className="divide-y divide-[#101b2e]">
        {strikes.map((s) => {
          return (
            <tr
              key={s.strike}
              className={`hover:bg-[#0c1629] transition-colors ${
                s.is_atm ? "bg-sky-950/25" : ""
              }`}
            >
              <td className="px-2 py-1 text-slate-300">{inr(s.ce_oi)}</td>
              <td
                className={`px-2 py-1 font-bold ${
                  s.ce_oi_chg > 0
                    ? "text-emerald-400"
                    : s.ce_oi_chg < 0
                    ? "text-rose-400"
                    : "text-slate-500"
                }`}
              >
                {inr(s.ce_oi_chg, true)}
              </td>
              <td className="px-2 py-1 whitespace-nowrap">
                <BuildupTag code={s.ce_buildup} label={s.ce_buildup_label} isCe={true} />
              </td>
              <td
                className={`px-2 py-1 text-center font-black ${
                  s.is_atm
                    ? "bg-sky-500/20 text-sky-200 border-x border-sky-500/50"
                    : s.strike === maxPain
                    ? "bg-amber-500/20 text-amber-200"
                    : "bg-[#0b1324] text-white"
                }`}
              >
                {s.strike}
                {s.is_atm && <span className="ml-1 text-[9px] text-sky-400">ATM</span>}
              </td>
              <td className="px-2 py-1 whitespace-nowrap">
                <BuildupTag code={s.pe_buildup} label={s.pe_buildup_label} isCe={false} />
              </td>
              <td
                className={`px-2 py-1 font-bold ${
                  s.pe_oi_chg > 0
                    ? "text-rose-400"
                    : s.pe_oi_chg < 0
                    ? "text-emerald-400"
                    : "text-slate-500"
                }`}
              >
                {inr(s.pe_oi_chg, true)}
              </td>
              <td className="px-2 py-1 text-slate-300">{inr(s.pe_oi)}</td>
              <td className="px-2 py-1 text-[10.5px] font-semibold text-slate-300 whitespace-nowrap">
                {s.dominant_tag}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}
