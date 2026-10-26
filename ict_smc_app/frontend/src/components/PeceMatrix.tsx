// PeceMatrix.tsx — Institutional PE-CE Difference & Skew Matrix 2.0.
// Surpasses commercial platforms like StockMojo with:
//   1. Auto-ATM Strike Range Filtering (ATM ±10, ATM ±5, Full Chain)
//   2. Center-Zero Diverging Visual Bars (Day Net Bar + Interval Impulse Bar)
//   3. Magnitude Position-Sizing & Regime Classifier (<5L, 5L-20L, 20L-30L, >30L)
//   4. 15-30m Slope Lead-Time Reversal Detector
//   5. Equidistant ATM PE vs CE Premium Skew Meter
//   6. Futures LTP + Day OI Change + Buildup Classification (LB/SB/SC/LU)
//   7. 6-Stage Action Lifecycle Verdicts
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { inr, playOiBeep } from "./OcLab";

export interface MatrixRow {
  time: number;
  time_label: string;
  put_oi_day_chg: number;
  put_oi_step_chg: number;
  call_oi_day_chg: number;
  call_oi_step_chg: number;
  pe_ce_day_diff: number;
  pe_ce_step_diff: number;
  pcr_day: number;
  fut_ltp: number;
  fut_oi_day_chg: number;
  fut_buildup: "LONG_BUILDUP" | "SHORT_BUILDUP" | "SHORT_COVERING" | "LONG_UNWINDING" | "NEUTRAL";
  stage_label: string;
}

export interface PeceMatrixPayload {
  ok: boolean;
  day: string;
  expiry: string;
  tf: string;
  scope: "atm_10" | "atm_5" | "full";
  strike_range_label: string;
  spot: number;
  atm_strike: number;
  regime: {
    tier_key: "BALANCED_RANGE" | "MODERATE_CONVICTION" | "LARGE_HIGH_CONVICTION" | "EXTREME_CROWDED_ALERT";
    tier_text: string;
    sizing_mult: number;
    current_diff: number;
    slope_flip_state: "NONE" | "BULLISH_SLOPE_FLIP" | "BEARISH_SLOPE_FLIP";
    slope_flip_text: string;
  };
  skew: {
    skew_state: "STEEP_PUT_SKEW" | "NORMAL_PUT_SKEW" | "FLAT_SKEW" | "CALL_SKEW_NARROWING" | "EXTREME_CALL_SKEW";
    skew_label: string;
    skew_pct: number;
    pe_ltp: number;
    ce_ltp: number;
    interpretation: string;
  };
  first_hour: {
    bias: "BULLISH_FIRST_HOUR" | "BEARISH_FIRST_HOUR" | "BALANCED_FIRST_HOUR" | "FORMING";
    label: string;
    diff_l: number;
    text: string;
    anchor_hhmm: string;
  };
  rows: MatrixRow[];
  snapshots: number;
  message?: string;
}

// ────────────────────────────────────────── Center-Zero Diverging Bars ──────────────────────────────────────────
export function DayNetBar({ val, maxVal }: { val: number; maxVal: number }) {
  const normMax = Math.max(1000000, maxVal);
  const pct = Math.min(100, Math.max(0, (Math.abs(val) / normMax) * 50));
  const isPos = val >= 0;

  return (
    <div className="relative flex items-center h-4 w-28 bg-[#060b16] rounded border border-slate-800/80 overflow-hidden shadow-inner">
      {/* Center 0 Axis */}
      <div className="absolute left-1/2 top-0 bottom-0 w-[1px] bg-slate-600 z-10 opacity-70" />

      {/* Bar extending Left if Negative, Right if Positive */}
      {val !== 0 && (
        <div
          className={`absolute top-0.5 bottom-0.5 rounded-sm transition-all duration-300 ${
            isPos
              ? "left-1/2 bg-gradient-to-r from-emerald-600 to-emerald-400 shadow-[0_0_8px_rgba(16,185,129,0.5)]"
              : "right-1/2 bg-gradient-to-l from-rose-600 to-rose-400 shadow-[0_0_8px_rgba(239,68,68,0.5)]"
          }`}
          style={{ width: `${pct}%` }}
        />
      )}
    </div>
  );
}

export function IntervalImpulseBar({ val, maxVal }: { val: number; maxVal: number }) {
  const normMax = Math.max(500000, maxVal);
  const pct = Math.min(100, Math.max(0, (Math.abs(val) / normMax) * 50));
  const isPos = val >= 0;

  return (
    <div className="relative flex items-center h-4 w-20 bg-[#060b16] rounded border border-slate-800/80 overflow-hidden shadow-inner">
      {/* Center 0 Axis */}
      <div className="absolute left-1/2 top-0 bottom-0 w-[1px] bg-slate-600 z-10 opacity-70" />

      {/* Interval Capsule */}
      {val !== 0 && (
        <div
          className={`absolute top-0.5 bottom-0.5 rounded-sm transition-all duration-300 ${
            isPos
              ? "left-1/2 bg-emerald-400 shadow-[0_0_6px_rgba(16,185,129,0.6)]"
              : "right-1/2 bg-rose-400 shadow-[0_0_6px_rgba(239,68,68,0.6)]"
          }`}
          style={{ width: `${pct}%` }}
        />
      )}
    </div>
  );
}

export interface PeceMatrixProps {
  tick?: number;
  onClose?: () => void;
}

export default function PeceMatrix({ tick = 0 }: PeceMatrixProps) {
  const [tf, setTf] = useState<string>("3m");
  const [scope, setScope] = useState<"atm_10" | "atm_5" | "full">(() => {
    try {
      const saved = localStorage.getItem("ict_pece_scope");
      if (saved === "atm_10" || saved === "atm_5" || saved === "full") return saved;
    } catch { /* ignore */ }
    return "atm_10";
  });
  const [expiryTab, setExpiryTab] = useState<"cur" | "next" | "both">("cur");
  const [displayMode, setDisplayMode] = useState<"day_chg" | "total_oi">("day_chg");

  // Audio chimes
  const [soundOn, setSoundOn] = useState<boolean>(() => {
    try { return localStorage.getItem("oi_beep_alerts") !== "0"; } catch { return true; }
  });

  const toggleSound = () => {
    setSoundOn((prev) => {
      const next = !prev;
      try { localStorage.setItem("oi_beep_alerts", next ? "1" : "0"); } catch { /* ignore */ }
      return next;
    });
  };

  const [data, setData] = useState<PeceMatrixPayload | null>(null);
  const [busy, setBusy] = useState<boolean>(false);
  const lastTopTsRef = useRef<string | null>(null);

  const fetchMatrix = useCallback(() => {
    setBusy(true);
    fetch(`/api/chain/pece-matrix?expiry=${expiryTab}&tf=${tf}&scope=${scope}`)
      .then((r) => r.json())
      .then((json: PeceMatrixPayload) => {
        if (json && json.ok) {
          setData(json);
        }
      })
      .catch((e) => console.error("fetch pece-matrix error:", e))
      .finally(() => setBusy(false));
  }, [expiryTab, tf, scope]);

  useEffect(() => {
    fetchMatrix();
    const interval = window.setInterval(fetchMatrix, 15000); // 15s refresh
    return () => window.clearInterval(interval);
  }, [fetchMatrix, tick]);

  // Audio chime trigger on new row or slope flip
  useEffect(() => {
    if (!data?.rows || data.rows.length === 0) return;
    const topRow = data.rows[0];
    if (topRow.time_label === "Prev Day") return;

    if (lastTopTsRef.current && topRow.time_label !== lastTopTsRef.current) {
      if (soundOn) {
        if (data.regime.slope_flip_state === "BULLISH_SLOPE_FLIP") {
          playOiBeep("angleBull");
        } else if (data.regime.slope_flip_state === "BEARISH_SLOPE_FLIP") {
          playOiBeep("angleBear");
        } else if (
          topRow.stage_label.includes("Dip Buy") ||
          topRow.stage_label.includes("Bullish Acceleration") ||
          topRow.stage_label.includes("Bullish Breakout")
        ) {
          playOiBeep("angleBull");
        } else if (
          topRow.stage_label.includes("Pullback Sell") ||
          topRow.stage_label.includes("Bearish Acceleration") ||
          topRow.stage_label.includes("Bearish Breakdown")
        ) {
          playOiBeep("angleBear");
        } else if (
          topRow.stage_label.includes("Book") ||
          topRow.stage_label.includes("Digestion")
        ) {
          playOiBeep("scissor");
        }
      }
    }
    lastTopTsRef.current = topRow.time_label;
  }, [data, soundOn]);

  const setScopeFilter = (s: "atm_10" | "atm_5" | "full") => {
    setScope(s);
    try { localStorage.setItem("ict_pece_scope", s); } catch { /* ignore */ }
  };

  const rows = data?.rows || [];

  // Max values for auto-scaling center-zero diverging bars
  const maxDayDiff = useMemo(() => {
    const vals = rows.map((r) => Math.abs(r.pe_ce_day_diff)).filter((v) => v > 0);
    return vals.length ? Math.max(...vals) : 2000000;
  }, [rows]);

  const maxStepDiff = useMemo(() => {
    const vals = rows.map((r) => Math.abs(r.pe_ce_step_diff)).filter((v) => v > 0);
    return vals.length ? Math.max(...vals) : 1000000;
  }, [rows]);

  const th = "sticky top-0 z-20 bg-[#070d18] px-2.5 py-1.5 text-[11px] font-bold uppercase tracking-wider text-slate-400 whitespace-nowrap border-b border-[#1b273d]";
  const td = "px-2.5 py-1.5 whitespace-nowrap text-[11.5px] border-b border-[#111c2e]";

  return (
    <div className="flex h-full flex-col bg-[#070c17] text-slate-200 select-none overflow-hidden">
      {/* ────────────────────────────────── Top Control Bar ────────────────────────────────── */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-[#162238] bg-[#091122] px-3 py-1.5 text-[11px]">
        {/* Left: Timeframe Interval Pills */}
        <div className="flex items-center gap-1">
          <span className="text-slate-400 font-bold uppercase text-[10px] mr-1">Interval:</span>
          {["1m", "3m", "5m", "15m", "1h"].map((t) => (
            <button
              key={t}
              onClick={() => setTf(t)}
              className={`rounded px-2 py-0.5 text-[11px] font-bold transition-all ${
                tf === t
                  ? "bg-sky-500/30 text-sky-200 border border-sky-500/60 shadow-sm"
                  : "bg-[#0c1424] text-slate-400 border border-slate-800 hover:text-slate-200"
              }`}
            >
              {t}
            </button>
          ))}
        </div>

        {/* Middle: Strike Scope Selector */}
        <div className="flex items-center gap-1.5">
          <span className="text-slate-400 font-bold uppercase text-[10px] mr-0.5">Scope:</span>
          <button
            onClick={() => setScopeFilter("atm_10")}
            className={`rounded px-2.5 py-0.5 text-[11px] font-bold border transition-all ${
              scope === "atm_10"
                ? "bg-emerald-500/30 text-emerald-200 border-emerald-500/60 shadow-sm"
                : "bg-[#0c1424] text-slate-400 border-slate-800 hover:text-slate-200"
            }`}
            title="Auto ATM ±10 Strikes (±500 pts) — Filters deep OTM margin hedges"
          >
            Auto ATM ±10 {data?.atm_strike ? `(${data.atm_strike - 500}–${data.atm_strike + 500})` : ""}
          </button>
          <button
            onClick={() => setScopeFilter("atm_5")}
            className={`rounded px-2.5 py-0.5 text-[11px] font-bold border transition-all ${
              scope === "atm_5"
                ? "bg-emerald-500/30 text-emerald-200 border-emerald-500/60 shadow-sm"
                : "bg-[#0c1424] text-slate-400 border-slate-800 hover:text-slate-200"
            }`}
            title="Auto ATM ±5 Strikes (±250 pts) — Fast Gamma Scalping Window"
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
            title="Full Chain (All Strikes including deep OTM)"
          >
            Full Chain
          </button>
        </div>

        {/* Right: Expiry Filter + Sound + Refresh */}
        <div className="flex items-center gap-2">
          {/* Expiry Selector */}
          <div className="flex rounded bg-[#0c1424] p-0.5 border border-slate-800">
            {(["cur", "next", "both"] as const).map((e) => (
              <button
                key={e}
                onClick={() => setExpiryTab(e)}
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

          {/* Sound Toggle */}
          <button
            onClick={toggleSound}
            className={`rounded px-2 py-0.5 text-[10.5px] font-bold border transition-all ${
              soundOn
                ? "bg-emerald-950/70 text-emerald-300 border-emerald-600/70"
                : "bg-[#0c1424] text-slate-400 border-slate-800 hover:text-slate-200"
            }`}
            title="Toggle acoustic alert chimes on Stage 2 / Slope Flips"
          >
            {soundOn ? "🔊 Audio: ON" : "🔇 Audio: OFF"}
          </button>

          <button
            onClick={fetchMatrix}
            className="rounded bg-[#0c1424] px-2 py-0.5 text-[11px] font-bold text-slate-300 border border-slate-800 hover:bg-slate-800"
            title="Refresh now"
          >
            {busy ? "⟳" : "↻"}
          </button>
        </div>
      </div>

      {/* ────────────────────────────────── Executive Regime, Magnitude & Skew Banner (3 Cards) ────────────────────────────────── */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-2.5 p-2.5 border-b border-[#162238] bg-[#070d1a]">
        {/* Card A: Magnitude & Position-Sizing Verdict */}
        <div className="rounded-lg border border-slate-800 bg-gradient-to-b from-[#0a1224] to-[#060b16] p-2.5 shadow flex flex-col justify-between">
          <div className="flex items-center justify-between text-[11px] mb-1">
            <span className="font-extrabold uppercase tracking-wider text-slate-400">
              ⚡ Sizing & Magnitude
            </span>
            <span
              className={`px-2 py-0.5 rounded text-[10px] font-black border ${
                data?.regime.tier_key === "LARGE_HIGH_CONVICTION"
                  ? "bg-emerald-950 text-emerald-300 border-emerald-500/80 ring-1 ring-emerald-500/30"
                  : data?.regime.tier_key === "EXTREME_CROWDED_ALERT"
                  ? "bg-amber-950 text-amber-300 border-amber-500/80 ring-1 ring-amber-500/30"
                  : data?.regime.tier_key === "MODERATE_CONVICTION"
                  ? "bg-sky-950 text-sky-300 border-sky-500/80"
                  : "bg-slate-900 text-slate-400 border-slate-800"
              }`}
            >
              {data?.regime.sizing_mult ? `${data.regime.sizing_mult}x Size` : "1.0x Size"}
            </span>
          </div>
          <div>
            <div className="text-sm font-black text-slate-100 flex items-center gap-1.5 mb-1">
              <span className={data?.regime.current_diff && data.regime.current_diff > 0 ? "text-emerald-400" : "text-rose-400"}>
                {inr(data?.regime.current_diff, true)}
              </span>
              <span className="text-xs font-semibold text-slate-400">Net Day Difference</span>
            </div>
            <div className="text-[11px] text-slate-300 leading-snug">
              {data?.regime.tier_text || "Evaluating institutional conviction..."}
            </div>
          </div>
        </div>

        {/* Card B: Slope & 10:30 First-Hour Bias */}
        <div className="rounded-lg border border-slate-800 bg-gradient-to-b from-[#0a1224] to-[#060b16] p-2.5 shadow flex flex-col justify-between">
          <div className="flex items-center justify-between text-[11px] mb-1">
            <span className="font-extrabold uppercase tracking-wider text-slate-400">
              📐 Slope & 10:30 Anchor
            </span>
            {data?.regime.slope_flip_state !== "NONE" && (
              <span
                className={`px-2 py-0.5 rounded text-[9.5px] font-black border animate-pulse ${
                  data?.regime.slope_flip_state === "BULLISH_SLOPE_FLIP"
                    ? "bg-emerald-950 text-emerald-300 border-emerald-500 ring-1 ring-emerald-500/40"
                    : "bg-rose-950 text-rose-300 border-rose-500 ring-1 ring-rose-500/40"
                }`}
              >
                {data?.regime.slope_flip_state === "BULLISH_SLOPE_FLIP" ? "🚀 Bullish Flip" : "🩸 Bearish Flip"}
              </span>
            )}
          </div>
          <div>
            <div className="text-xs font-bold text-sky-300 mb-1 flex items-center gap-1">
              <span>{data?.first_hour.label || "10:30 Anchor Forming"}</span>
            </div>
            <div className="text-[11px] text-slate-300 leading-snug">
              {data?.regime.slope_flip_text || data?.first_hour.text || "Tracking slope momentum across session."}
            </div>
          </div>
        </div>

        {/* Card C: ATM PE vs CE Premium Skew Meter */}
        <div className="rounded-lg border border-slate-800 bg-gradient-to-b from-[#0a1224] to-[#060b16] p-2.5 shadow flex flex-col justify-between">
          <div className="flex items-center justify-between text-[11px] mb-1">
            <span className="font-extrabold uppercase tracking-wider text-slate-400">
              ⚖️ Equidistant Skew Meter
            </span>
            <span className="font-mono text-[10px] text-slate-400">
              PE ₹{data?.skew.pe_ltp ?? 0} vs CE ₹{data?.skew.ce_ltp ?? 0}
            </span>
          </div>
          <div>
            <div className="text-xs font-bold text-amber-300 mb-1">
              {data?.skew.skew_label || "⚖️ Evaluating Premium Skew..."}
            </div>
            <div className="text-[11px] text-slate-300 leading-snug">
              {data?.skew.interpretation || "Comparing extrinsic Put vs Call premiums."}
            </div>
          </div>
        </div>
      </div>

      {/* ────────────────────────────────── Enhanced PE-CE Table with Center-Zero Diverging Bars ────────────────────────────────── */}
      <div className="min-h-0 flex-1 overflow-auto">
        <table className="w-full border-separate border-spacing-0 text-[11.5px] num">
          <thead>
            <tr>
              <th className={`${th} w-20 sticky left-0 z-30`}>Time</th>
              <th className={`${th} w-32`}>Put OI (Floor)</th>
              <th className={`${th} w-32`}>Call OI (Cap)</th>
              <th className={`${th} w-28`}>PE − CE Difference</th>
              <th className={`${th} w-32 text-center`}>Day Net Bar</th>
              <th className={`${th} w-24 text-center`}>Interval Δ Bar</th>
              <th className={`${th} w-16 text-center`}>PCR</th>
              <th className={`${th} w-44`}>NIFTY Future & Buildup</th>
              <th className={`${th} min-w-[200px]`}>6-Stage Action Verdict</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r, idx) => {
              const isPrevDay = r.time_label === "Prev Day";
              const isPeLead = r.pe_ce_day_diff >= 0;
              const isStepPos = r.pe_ce_step_diff >= 0;

              return (
                <tr
                  key={`${r.time_label}-${idx}`}
                  className={`border-b border-[#111c2e] transition-colors ${
                    isPrevDay
                      ? "bg-[#0b101c] opacity-60 text-slate-400"
                      : idx % 2 === 0
                      ? "bg-[#070c17] hover:bg-[#0d1627]"
                      : "bg-[#080e1b] hover:bg-[#0d1627]"
                  }`}
                >
                  {/* 1. Time */}
                  <td className={`${td} font-bold text-slate-300 sticky left-0 z-10 bg-inherit border-r border-[#152033]`}>
                    {r.time_label}
                  </td>

                  {/* 2. Put OI (Day + Step) */}
                  <td className={td}>
                    <div className="flex flex-col">
                      <span className="font-extrabold text-emerald-400">
                        {inr(r.put_oi_day_chg, true)}
                      </span>
                      {!isPrevDay && (
                        <span className={`text-[10px] ${r.put_oi_step_chg >= 0 ? "text-emerald-500" : "text-slate-500"}`}>
                          Δ {inr(r.put_oi_step_chg, true)}
                        </span>
                      )}
                    </div>
                  </td>

                  {/* 3. Call OI (Day + Step) - Color Discipline: Crimson for writing, Green for covering */}
                  <td className={td}>
                    <div className="flex flex-col">
                      <span
                        className={`font-extrabold ${
                          r.call_oi_day_chg > 0 ? "text-rose-400" : r.call_oi_day_chg < 0 ? "text-emerald-400" : "text-slate-400"
                        }`}
                      >
                        {inr(r.call_oi_day_chg, true)}
                      </span>
                      {!isPrevDay && (
                        <span
                          className={`text-[10px] ${
                            r.call_oi_step_chg > 0 ? "text-rose-500" : r.call_oi_step_chg < 0 ? "text-emerald-400" : "text-slate-500"
                          }`}
                        >
                          Δ {inr(r.call_oi_step_chg, true)}
                        </span>
                      )}
                    </div>
                  </td>

                  {/* 4. PE - CE Difference */}
                  <td className={td}>
                    <div className="flex flex-col">
                      <span
                        className={`font-black ${
                          isPeLead ? "text-emerald-300" : "text-rose-300"
                        }`}
                      >
                        {inr(r.pe_ce_day_diff, true)}
                      </span>
                      {!isPrevDay && (
                        <span className={`text-[10px] font-semibold ${isStepPos ? "text-emerald-400" : "text-rose-400"}`}>
                          ({isStepPos ? "+" : ""}{inr(r.pe_ce_step_diff)})
                        </span>
                      )}
                    </div>
                  </td>

                  {/* 5. Center-Zero Day Net Bar */}
                  <td className={`${td} text-center`}>
                    <div className="flex items-center justify-center">
                      <DayNetBar val={r.pe_ce_day_diff} maxVal={maxDayDiff} />
                    </div>
                  </td>

                  {/* 6. Center-Zero Interval Impulse Bar */}
                  <td className={`${td} text-center`}>
                    <div className="flex items-center justify-center">
                      <IntervalImpulseBar val={r.pe_ce_step_diff} maxVal={maxStepDiff} />
                    </div>
                  </td>

                  {/* 7. PCR */}
                  <td className={`${td} text-center font-mono font-bold text-sky-300`}>
                    {r.pcr_day.toFixed(2)}
                  </td>

                  {/* 8. Future LTP + Buildup */}
                  <td className={td}>
                    <div className="flex items-center gap-1.5">
                      <span className="font-mono text-slate-100 font-bold">{r.fut_ltp.toFixed(1)}</span>
                      {!isPrevDay && (
                        <span
                          className={`px-1.5 py-0.2 rounded text-[9.5px] font-black border ${
                            r.fut_buildup === "LONG_BUILDUP"
                              ? "bg-emerald-950 text-emerald-300 border-emerald-600/70"
                              : r.fut_buildup === "SHORT_BUILDUP"
                              ? "bg-rose-950 text-rose-300 border-rose-600/70"
                              : r.fut_buildup === "SHORT_COVERING"
                              ? "bg-cyan-950 text-cyan-300 border-cyan-600/70"
                              : "bg-slate-900 text-slate-400 border-slate-700"
                          }`}
                        >
                          {r.fut_buildup === "LONG_BUILDUP"
                            ? "LB"
                            : r.fut_buildup === "SHORT_BUILDUP"
                            ? "SB"
                            : r.fut_buildup === "SHORT_COVERING"
                            ? "SC"
                            : "LU"}
                          {r.fut_oi_day_chg !== 0 && ` ${inr(r.fut_oi_day_chg, true)}`}
                        </span>
                      )}
                    </div>
                  </td>

                  {/* 9. 6-Stage Action Verdict */}
                  <td className={td}>
                    <span
                      className={`inline-flex items-center gap-1 px-2 py-0.5 rounded text-[11px] font-black border ${
                        r.stage_label.includes("Dip Buy")
                          ? "bg-emerald-950 text-emerald-300 border-emerald-500/80 ring-1 ring-emerald-500/30"
                          : r.stage_label.includes("Pullback Sell")
                          ? "bg-rose-950 text-rose-300 border-rose-500/80 ring-1 ring-rose-500/30"
                          : r.stage_label.includes("Add CE") || r.stage_label.includes("Bullish Acceleration")
                          ? "bg-emerald-900/60 text-emerald-200 border-emerald-600/60 ring-1 ring-emerald-500/20"
                          : r.stage_label.includes("Add PE") || r.stage_label.includes("Bearish Acceleration")
                          ? "bg-rose-900/60 text-rose-200 border-rose-600/60 ring-1 ring-rose-500/20"
                          : r.stage_label.includes("Bullish Breakout")
                          ? "bg-emerald-950 text-emerald-300 border-emerald-400/80"
                          : r.stage_label.includes("Bearish Breakdown")
                          ? "bg-rose-950 text-rose-300 border-rose-400/80"
                          : r.stage_label.includes("Book") || r.stage_label.includes("Digestion")
                          ? "bg-amber-950 text-amber-300 border-amber-500/70"
                          : r.stage_label.includes("EOD")
                          ? "bg-indigo-950 text-indigo-300 border-indigo-700/70 ring-1 ring-indigo-500/30"
                          : "bg-slate-900 text-slate-400 border-slate-800"
                      }`}
                    >
                      {r.stage_label}
                    </span>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {/* ────────────────────────────────── Bottom Status Bar ────────────────────────────────── */}
      <div className="flex flex-wrap items-center justify-between border-t border-[#162238] bg-[#070c17] px-3 py-1 text-[11px] text-slate-400">
        <div className="flex items-center gap-3">
          <span>Active Scope: <b className="text-emerald-300">{data?.strike_range_label}</b></span>
          <span>Expiry: <b className="text-sky-300">{data?.expiry}</b></span>
          <span>NIFTY Spot: <b className="text-white font-mono">{data?.spot}</b></span>
        </div>
        <div className="flex items-center gap-2 text-slate-500 text-[10px]">
          <span>Put OI = Emerald Support • Call OI = Crimson Resistance (Green on Covering)</span>
        </div>
      </div>
    </div>
  );
}
