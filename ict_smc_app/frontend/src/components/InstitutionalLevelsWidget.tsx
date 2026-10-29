// InstitutionalLevelsWidget.tsx — Dedicated panel for Institutional ICT Reference Levels (PDH, PDL, Session Open) & True Breakout Engine.
import React, { useEffect, useState } from "react";
import { api, type InstitutionalLevelsRes, fmt, lakh } from "../api";

export default function InstitutionalLevelsWidget({
  secId = "13",
  rvol,
  peCeDeltaL,
  onToggleLayer,
  isLayerActive = true,
}: {
  secId?: string;
  rvol?: number;
  peCeDeltaL?: number;
  onToggleLayer?: () => void;
  isLayerActive?: boolean;
}) {
  const [data, setData] = useState<InstitutionalLevelsRes | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    const fetchLevels = async () => {
      try {
        const res = await api.institutionalLevels(secId, rvol, peCeDeltaL);
        if (alive && res.ok) {
          setData(res);
          setError(null);
        }
      } catch (e: any) {
        if (alive) setError(e.message || "Failed to load ICT levels");
      } finally {
        if (alive) setLoading(false);
      }
    };

    fetchLevels();
    const timer = setInterval(fetchLevels, 6000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [secId, rvol, peCeDeltaL]);

  if (loading && !data) {
    return (
      <div className="flex h-32 items-center justify-center rounded-lg border border-slate-800 bg-[#060b16] text-xs text-slate-400">
        Loading Institutional ICT Levels...
      </div>
    );
  }

  if (error && !data) {
    return (
      <div className="flex h-32 items-center justify-center rounded-lg border border-rose-900/50 bg-[#060b16] text-xs text-rose-400">
        {error}
      </div>
    );
  }

  if (!data) return null;

  const { levels, current_price, high_today, low_today, status, status_label, alert_text, action_tone, is_sweep, is_breakout, details } = data;
  // Placeholder levels are worse than none: the whole panel reads as fact. Say it before anything else.
  const stale = data.levels_available === false;
  const pdhDiff = current_price - levels.pdh;
  const pdlDiff = current_price - levels.pdl;

  return (
    <div className="rounded-xl border border-slate-800/80 bg-[#060b16]/95 p-3.5 shadow-2xl backdrop-blur">
      {/* Header with Title and Layer Toggle */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-800/70 pb-2.5">
        <div className="flex items-center gap-2">
          <span className="text-base font-extrabold tracking-tight text-gold-300">
            🏛️ Institutional ICT Reference Levels
          </span>
          <span className="rounded bg-slate-800/80 px-1.5 py-0.5 text-[10px] font-semibold text-slate-300">
            PDH · PDL · 09:15 Open
          </span>
        </div>
        {onToggleLayer && (
          <button
            onClick={onToggleLayer}
            className={`rounded px-2 py-0.5 text-[11px] font-black transition-all border ${
              isLayerActive
                ? "bg-gold-500/20 border-gold-500/80 text-gold-300"
                : "border-slate-700 bg-slate-800/50 text-slate-400"
            }`}
          >
            {isLayerActive ? "☑ Chart Overlay Active" : "☐ Show on Chart"}
          </button>
        )}
      </div>

      {/* Breakout / Sweep Status Banner */}
      <div
        className={`my-3 rounded-lg border p-2.5 text-xs font-semibold leading-relaxed transition-all ${
          status === "PDH_TRUE_BREAKOUT"
            ? "border-emerald-500/80 bg-emerald-950/70 text-emerald-200 ring-1 ring-emerald-500/40"
            : status === "PDL_TRUE_BREAKDOWN"
            ? "border-rose-500/80 bg-rose-950/70 text-rose-200 ring-1 ring-rose-500/40"
            : is_sweep
            ? "border-amber-500/80 bg-amber-950/70 text-amber-200 ring-1 ring-amber-500/40"
            : "border-slate-700/60 bg-slate-900/60 text-slate-300"
        }`}
      >
        <div className="flex items-center gap-2">
          <span className="text-sm">{status_label}</span>
          {stale && (
            <span className="ml-2 rounded border border-amber-500/70 bg-amber-950/70 px-1.5 py-0.5 text-[10px] font-bold text-amber-300"
                  title={"PDH/PDL/PDC could not be read from a real previous session, so they fell back to the "
                    + "placeholder values in rules.json. They are NOT yesterday's levels, the lines on the chart "
                    + "are not where they should be, and no breakout or sweep is being classified until real "
                    + "daily data returns."}>
              ⚠ PLACEHOLDER LEVELS — NOT YESTERDAY&apos;S
            </span>
          )}
          {is_breakout && (
            <span className="rounded bg-emerald-500/30 px-1.5 py-0.5 text-[10px] font-black uppercase text-emerald-300">
              True Breakout
            </span>
          )}
          {is_sweep && (
            <span className="rounded bg-amber-500/30 px-1.5 py-0.5 text-[10px] font-black uppercase text-amber-300">
              Judas Swing
            </span>
          )}
        </div>
        <div className="mt-1 text-[11px] text-slate-300/90">{details}</div>
      </div>

      {/* Grid of Key ICT Reference Levels */}
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 md:grid-cols-6">
        {/* PDH */}
        <div className="rounded-lg border border-gold-500/40 bg-gold-950/20 p-2 text-center">
          <div className="text-[10px] font-bold uppercase tracking-wider text-gold-400">PDH (Gold)</div>
          <div className="mt-0.5 text-sm font-extrabold text-gold-200">{fmt(levels.pdh, 1)}</div>
          <div className={`text-[10px] font-semibold ${pdhDiff >= 0 ? "text-emerald-400" : "text-slate-400"}`}>
            {pdhDiff >= 0 ? `+${fmt(pdhDiff, 1)}` : fmt(pdhDiff, 1)} pts
          </div>
        </div>

        {/* PDL */}
        <div className="rounded-lg border border-gold-500/40 bg-gold-950/20 p-2 text-center">
          <div className="text-[10px] font-bold uppercase tracking-wider text-gold-400">PDL (Gold)</div>
          <div className="mt-0.5 text-sm font-extrabold text-gold-200">{fmt(levels.pdl, 1)}</div>
          <div className={`text-[10px] font-semibold ${pdlDiff <= 0 ? "text-rose-400" : "text-slate-400"}`}>
            {pdlDiff >= 0 ? `+${fmt(pdlDiff, 1)}` : fmt(pdlDiff, 1)} pts
          </div>
        </div>

        {/* 09:15 Cash Open */}
        <div className="rounded-lg border border-cyan-500/40 bg-cyan-950/20 p-2 text-center">
          <div className="text-[10px] font-bold uppercase tracking-wider text-cyan-400">09:15 Open</div>
          <div className="mt-0.5 text-sm font-extrabold text-cyan-200">{fmt(levels.session_open, 1)}</div>
          <div className="text-[10px] font-medium text-slate-400">Session Anchor</div>
        </div>

        {/* Midnight Open */}
        <div className="rounded-lg border border-slate-700/60 bg-slate-900/40 p-2 text-center">
          <div className="text-[10px] font-bold uppercase tracking-wider text-slate-400">Midnight Open</div>
          <div className="mt-0.5 text-sm font-extrabold text-slate-200">{fmt(levels.midnight_open, 1)}</div>
          <div className="text-[10px] font-medium text-slate-400">Daily Bias</div>
        </div>

        {/* RVOL Confirmation */}
        <div className="rounded-lg border border-slate-700/60 bg-slate-900/40 p-2 text-center">
          <div className="text-[10px] font-bold uppercase tracking-wider text-slate-400">Volume RVOL</div>
          <div className={`mt-0.5 text-sm font-extrabold ${data.rvol >= 1.5 ? "text-emerald-400" : data.rvol < 1.0 ? "text-amber-400" : "text-slate-200"}`}>
            {fmt(data.rvol, 1)}x
          </div>
          <div className="text-[10px] font-medium text-slate-400">
            {data.rvol >= 1.5 ? "Breakout Fuel" : data.rvol < 1.0 ? "Low Vol Sweep" : "Normal"}
          </div>
        </div>

        {/* PE-CE Writer Delta */}
        <div className="rounded-lg border border-slate-700/60 bg-slate-900/40 p-2 text-center">
          <div className="text-[10px] font-bold uppercase tracking-wider text-slate-400">Net PE−CE Flow</div>
          <div className={`mt-0.5 text-sm font-extrabold ${data.pe_ce_delta_l >= 50 ? "text-emerald-400" : data.pe_ce_delta_l <= -50 ? "text-rose-400" : "text-slate-200"}`}>
            {data.pe_ce_delta_l >= 0 ? `+${fmt(data.pe_ce_delta_l, 1)}L` : `${fmt(data.pe_ce_delta_l, 1)}L`}
          </div>
          <div className="text-[10px] font-medium text-slate-400">
            {data.pe_ce_delta_l >= 50 ? "Bull Supportive" : data.pe_ce_delta_l <= -50 ? "Bear Pressure" : "Neutral Flow"}
          </div>
        </div>
      </div>
    </div>
  );
}
