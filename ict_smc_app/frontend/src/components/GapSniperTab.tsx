// GapSniperTab.tsx — 09:08–09:45 AM Opening Gap & Confluence Sniper Suite
// Direct Dhan Live Feed & Real-Time Pre-Market Matrix
// Features:
//   1. Direct Dhan REST/WebSocket Feed (GIFT Nifty 5024, Cash Spot 13, Near-Month FUT, Next-Month FUT)
//   2. Clean Live Multi-Contract Benchmark Table (Instrument, Live LTP, Basis/Diff vs Oct Fut, Live Gap Status, Action Bias)
//   3. Auto-Voice Synthesizer Banner (Window Speech API with 09:10 AM IST threshold and replay controls)
//   4. 5-Point Confluence Checklist & Dynamic 09:15–09:45 AM Execution Playbook
import React, { useCallback, useEffect, useRef, useState } from "react";
import { inr } from "./OcLab";

export interface MultiContractRow {
  instrument: string;
  symbol: string;
  ltp: number;
  pdc?: number;
  diff_vs_oct?: number;
  basis_vs_spot?: number;
  basis_vs_gift?: number;
  diff_vs_gift?: number;
  gap_pts?: number;
  gap_pct?: number;
  proximity?: string;
  roll_spread?: number;
  live_gap_status?: string;
  classification?: string;
  action_bias: string;
  badge_color: string;
  is_anchor?: boolean;
}

export interface GapChecklistItem {
  id: string;
  title: string;
  detail: string;
  passed: boolean;
  tone: "bull" | "bear" | "neutral";
}

export interface GapTimelineItem {
  time: string;
  title: string;
  instruction: string;
  status: "COMPLETED" | "ACTIVE" | "PENDING";
}

export interface GapPlaybook {
  title: string;
  code: string;
  theme: "bullish" | "bearish" | "neutral";
  color: string;
  action: string;
  entry_zone: string;
  stop_loss: number;
  target_1: number;
  target_2: number;
  narrative: string;
}

export interface GapConfluenceData {
  ok: boolean;
  is_simulated: boolean;
  primary_anchor?: "SPOT" | "GIFT_NIFTY";
  live_spot?: number;
  pdc_spot?: number;
  spot_gap_pts?: number;
  spot_gap_pct?: number;
  oct_fut_ltp?: number;
  oct_fut_pdc?: number;
  oct_fut_gap_pts?: number;
  oct_fut_gap_pct?: number;
  nov_fut_ltp?: number;
  nov_fut_pdc?: number;
  nov_fut_gap_pts?: number;
  nov_fut_gap_pct?: number;
  roll_spread?: number;
  gap_tier?: string;
  gap_tier_label?: string;
  gift_nifty_ltp: number;
  near_fut_close: number;
  spot_close: number;
  gap_pts: number;
  gap_pct: number;
  gap_type: "GAP_DOWN" | "GAP_UP" | "FLAT";
  is_significant_gap: boolean;
  is_major_gap: boolean;
  expected_open_fut: number;
  expected_open_spot: number;
  speak_alert?: boolean;
  voice_message?: string;
  voice_alert?: {
    speak_alert: boolean;
    text: string;
    alert_time_ist: string;
    timestamp: string;
  };
  levels: {
    pdc: number;
    pdh: number;
    pdl: number;
    put_wall: number;
    call_wall: number;
    lower_straddle_be: number;
    upper_straddle_be: number;
    vwap: number;
  };
  wicks: {
    has_opened: boolean;
    status_text: string;
    lower_wick_detected: boolean;
    upper_wick_detected: boolean;
    lower_wick_ratio: number;
    upper_wick_ratio: number;
  };
  writer_flow: {
    fresh_put_delta: number;
    fresh_call_delta: number;
    pcr: number;
  };
  checklist: GapChecklistItem[];
  confluence_score: string;
  confluence_pct: number;
  playbook: GapPlaybook;
  timeline: GapTimelineItem[];
  multi_contract_table?: MultiContractRow[];
  updated_at: string;
}

interface GapSniperTabProps {
  tick: number;
  modeToggle?: React.ReactNode;
  optExpiry?: "cur" | "next" | "both";
  futContract?: "near" | "next" | "combined";
}

export function GapSniperTab({
  tick,
  modeToggle,
  optExpiry = "cur",
  futContract = "near",
}: GapSniperTabProps) {
  const [data, setData] = useState<GapConfluenceData | null>(null);
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  // Audio / Voice Alert state
  const [voiceEnabled, setVoiceEnabled] = useState<boolean>(() => {
    try {
      const v = localStorage.getItem("ict_gap_voice_alert");
      return v === null ? true : v === "true";
    } catch {
      return true;
    }
  });

  const lastSpokenRef = useRef<string>("");

  const speakText = useCallback((text: string) => {
    if (!("speechSynthesis" in window) || !text) return;
    try {
      window.speechSynthesis.cancel();
      const utterance = new SpeechSynthesisUtterance(text);
      utterance.rate = 1.0;
      utterance.pitch = 1.0;
      window.speechSynthesis.speak(utterance);
    } catch (e) {
      console.warn("Speech synthesis error:", e);
    }
  }, []);

  const toggleVoice = () => {
    setVoiceEnabled((prev) => {
      const next = !prev;
      try {
        localStorage.setItem("ict_gap_voice_alert", String(next));
      } catch { /* ignore */ }
      if (!next && "speechSynthesis" in window) {
        window.speechSynthesis.cancel();
      }
      return next;
    });
  };

  const fetchData = useCallback(async () => {
    try {
      setLoading(true);
      const params = new URLSearchParams();
      if (optExpiry) params.set("opt_expiry", optExpiry);
      if (futContract) params.set("fut_contract", futContract);

      const res = await fetch(`/api/desk/gap-confluence?${params.toString()}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const json: GapConfluenceData = await res.json();
      if (json.ok) {
        setData(json);
        setError(null);
      } else {
        setError("Failed to fetch gap confluence data");
      }
    } catch (e: any) {
      setError(e?.message || "Network error loading live gap feed");
    } finally {
      setLoading(false);
    }
  }, [optExpiry, futContract]);

  useEffect(() => {
    fetchData();
  }, [fetchData, tick]);

  // Trigger speech alert when speak_alert is true and message has not been spoken yet
  useEffect(() => {
    if (!voiceEnabled || !data?.voice_message) return;
    if (data.speak_alert && lastSpokenRef.current !== data.voice_message) {
      lastSpokenRef.current = data.voice_message;
      speakText(data.voice_message);
    }
  }, [voiceEnabled, data?.speak_alert, data?.voice_message, speakText]);

  const pb = data?.playbook;
  const levels = data?.levels;
  const flow = data?.writer_flow;

  // Calculate quick R:R
  const risk = pb && levels ? Math.abs(pb.entry_zone.includes("–") ? parseFloat(pb.entry_zone.split("–")[0]) - pb.stop_loss : 20) : 20;
  const reward1 = pb ? Math.abs(pb.target_1 - (levels?.pdc || 22700)) : 40;
  const reward2 = pb ? Math.abs(pb.target_2 - (levels?.pdc || 22700)) : 90;
  const rr1 = risk > 0 ? (reward1 / risk).toFixed(1) : "—";
  const rr2 = risk > 0 ? (reward2 / risk).toFixed(1) : "—";

  return (
    <div className="flex flex-1 min-h-0 flex-col h-full w-full bg-[#070c17] text-slate-100 overflow-y-auto">
      {/* ── Sub-Header & Controls Bar ── */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-800 bg-[#091122] px-4 py-2 text-xs shrink-0">
        <div className="flex items-center gap-2">
          <span className="font-extrabold text-emerald-400 text-sm flex items-center gap-1.5">
            <span>🎯</span> Opening Gap & Confluence Sniper
          </span>
          <span className="text-slate-400 hidden sm:inline">• Direct Live Dhan Feed • 09:10–09:45 AM Confluence Matrix</span>
          {loading && <span className="text-[10px] text-amber-400 animate-pulse">Syncing Dhan feed...</span>}
        </div>

        <div className="flex items-center gap-2.5">
          {modeToggle}
          <div className="h-4 w-px bg-slate-700 mx-1 hidden sm:block" />

          {/* Voice Alert Toggle */}
          <button
            onClick={toggleVoice}
            className={`flex items-center gap-1.5 px-2.5 py-1 rounded text-[11px] font-bold transition-all border ${
              voiceEnabled
                ? "bg-amber-950/40 text-amber-300 border-amber-500/60 shadow-sm shadow-amber-500/20"
                : "bg-slate-800/80 text-slate-400 border-slate-700 hover:text-slate-300"
            }`}
            title="Toggle Automated 09:10 AM Voice Alert Announcement"
          >
            <span>{voiceEnabled ? "🔊" : "🔇"}</span>
            <span>Voice Alert: {voiceEnabled ? "ON" : "OFF"}</span>
          </button>

          {/* Live Sync button */}
          <button
            onClick={fetchData}
            disabled={loading}
            className="px-2.5 py-1 rounded text-[11px] font-bold border border-slate-700 bg-slate-800/80 text-slate-300 hover:text-white transition-all flex items-center gap-1"
            title="Fetch live quotes now"
          >
            <span>🔄</span>
            <span className="hidden sm:inline">Sync</span>
          </button>
        </div>
      </div>

      {/* ── Main Dashboard Body ── */}
      <div className="p-4 space-y-4 max-w-[1600px] w-full mx-auto">
        {error && (
          <div className="rounded-lg border border-rose-500/40 bg-rose-950/20 px-4 py-2 text-rose-300 text-xs">
            ⚠️ {error}
          </div>
        )}

        {/* ── 🎙️ Auto-Voice Broadcast Banner ── */}
        {data?.voice_message && (
          <div className="rounded-xl border border-amber-500/40 bg-gradient-to-r from-amber-950/30 via-[#0e1628] to-amber-950/30 p-3 shadow-lg flex flex-col sm:flex-row items-start sm:items-center justify-between gap-3">
            <div className="flex items-start gap-2.5">
              <span className="text-xl mt-0.5">🎙️</span>
              <div className="space-y-0.5">
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="text-[10px] font-black uppercase tracking-wider text-amber-400">
                    09:10 AM IST Voice Announcement
                  </span>
                  <span className={`px-1.5 py-0.2 rounded text-[9px] font-bold uppercase ${
                    data.speak_alert ? "bg-emerald-950 text-emerald-300 border border-emerald-500/50" : "bg-slate-800 text-slate-400 border border-slate-700"
                  }`}>
                    {data.speak_alert ? "🚨 Alert Broadcast Triggered" : "🕒 Standard Window"}
                  </span>
                </div>
                <div className="text-xs font-medium text-slate-200 italic leading-relaxed">
                  "{data.voice_message}"
                </div>
              </div>
            </div>

            <button
              onClick={() => speakText(data.voice_message || "")}
              className="px-3 py-1 rounded text-[11px] font-bold bg-amber-500/20 text-amber-200 border border-amber-500/50 hover:bg-amber-500/30 transition-all flex items-center gap-1.5 shrink-0 self-end sm:self-auto"
              title="Play voice verdict announcement"
            >
              <span>▶</span>
              <span>Listen Again</span>
            </button>
          </div>
        )}

        {/* ── Top Hero Flashcard Banner — Real NIFTY 50 Spot vs. Yesterday PDC Front & Center ── */}
        <div className="rounded-xl border border-slate-700/80 bg-gradient-to-r from-[#0b101e] via-[#0e1628] to-[#0b101e] p-4 shadow-xl shadow-black/40 relative overflow-hidden">
          <div className="absolute top-0 left-0 right-0 h-[2px] bg-gradient-to-r from-transparent via-emerald-500/60 to-transparent" />
          
          <div className="flex flex-col lg:flex-row items-start lg:items-center justify-between gap-4">
            {/* Left: Real NIFTY 50 Spot vs Yesterday PDC & Live Gap */}
            <div className="space-y-2">
              <div className="flex items-center gap-2 flex-wrap">
                <span className="px-2 py-0.5 rounded text-[10px] font-black uppercase tracking-wider border bg-emerald-950/60 text-emerald-300 border-emerald-500/50">
                  ⚡ Live Dhan Feed
                </span>

                <span className="px-2 py-0.5 rounded text-[10px] font-black uppercase tracking-wider border bg-sky-950/60 text-sky-300 border-sky-500/50">
                  🇮🇳 Primary Anchor: NIFTY 50 Spot vs. PDC (Post-09:00 IST)
                </span>

                {/* Gap Tier Badge */}
                <span className={`px-2.5 py-0.5 rounded text-[10px] font-black uppercase tracking-wider border ${
                  (data?.spot_gap_pts ?? data?.gap_pts ?? 0) <= -80
                    ? "bg-rose-950/70 text-rose-300 border-rose-500/60 shadow-sm shadow-rose-900/30"
                    : (data?.spot_gap_pts ?? data?.gap_pts ?? 0) <= -25
                    ? "bg-orange-950/70 text-orange-300 border-orange-500/60"
                    : (data?.spot_gap_pts ?? data?.gap_pts ?? 0) >= 80
                    ? "bg-emerald-950/70 text-emerald-300 border-emerald-500/60 shadow-sm shadow-emerald-900/30"
                    : (data?.spot_gap_pts ?? data?.gap_pts ?? 0) >= 25
                    ? "bg-teal-950/70 text-teal-300 border-teal-500/60"
                    : "bg-slate-800 text-slate-300 border-slate-700"
                }`}>
                  {data?.gap_tier_label || (
                    (data?.spot_gap_pts ?? data?.gap_pts ?? 0) >= 80
                      ? `🚀 MAJOR GAP UP (+${(data?.spot_gap_pts ?? data?.gap_pts ?? 0).toFixed(1)} pts)`
                      : (data?.spot_gap_pts ?? data?.gap_pts ?? 0) >= 25
                      ? `🟢 MODERATE GAP UP (+${(data?.spot_gap_pts ?? data?.gap_pts ?? 0).toFixed(1)} pts)`
                      : (data?.spot_gap_pts ?? data?.gap_pts ?? 0) <= -80
                      ? `🩸 MAJOR GAP DOWN (${(data?.spot_gap_pts ?? data?.gap_pts ?? 0).toFixed(1)} pts)`
                      : (data?.spot_gap_pts ?? data?.gap_pts ?? 0) <= -25
                      ? `🟠 MODERATE GAP DOWN (${(data?.spot_gap_pts ?? data?.gap_pts ?? 0).toFixed(1)} pts)`
                      : `⚖️ FLAT / EQUILIBRIUM OPEN (${(data?.spot_gap_pts ?? data?.gap_pts ?? 0) > 0 ? "+" : ""}${(data?.spot_gap_pts ?? data?.gap_pts ?? 0).toFixed(1)} pts)`
                  )}
                </span>
              </div>

              {/* Main Numbers: Live Spot | Yesterday PDC | Live Gap */}
              <div className="flex items-baseline gap-3 sm:gap-4 flex-wrap pt-0.5">
                <div className="text-2xl sm:text-3xl font-black font-mono tracking-tight text-white flex items-center gap-2">
                  <span className="text-[11px] text-slate-400 font-sans uppercase font-bold tracking-wider">LIVE NIFTY 50:</span>
                  <span className="text-amber-300">
                    {(data?.live_spot ?? data?.spot_close ?? 22777.8).toLocaleString("en-IN", { minimumFractionDigits: 1, maximumFractionDigits: 1 })}
                  </span>
                </div>

                <div className="h-6 w-px bg-slate-700 hidden sm:block self-center" />

                <div className="text-base sm:text-lg font-mono text-slate-200 flex items-center gap-2">
                  <span className="text-[11px] text-slate-400 font-sans uppercase font-bold tracking-wider">Yesterday Close (PDC):</span>
                  <span className="text-slate-100 font-bold">
                    {(data?.pdc_spot ?? data?.levels?.pdc ?? 22753.2).toLocaleString("en-IN", { minimumFractionDigits: 1, maximumFractionDigits: 1 })}
                  </span>
                </div>

                <div className="h-6 w-px bg-slate-700 hidden sm:block self-center" />

                <div className="text-lg sm:text-xl font-black font-mono flex items-center gap-2">
                  <span className="text-[11px] text-slate-400 font-sans uppercase font-bold tracking-wider">LIVE GAP:</span>
                  <span className={
                    (data?.spot_gap_pts ?? data?.gap_pts ?? 0) > 0
                      ? "text-emerald-400"
                      : (data?.spot_gap_pts ?? data?.gap_pts ?? 0) < 0
                      ? "text-rose-400"
                      : "text-slate-300"
                  }>
                    {(data?.spot_gap_pts ?? data?.gap_pts ?? 0) > 0 ? "+" : ""}
                    {(data?.spot_gap_pts ?? data?.gap_pts ?? 0).toFixed(1)} pts
                    <span className="text-xs font-semibold ml-1.5 opacity-90">
                      ({(data?.spot_gap_pct ?? data?.gap_pct ?? 0) > 0 ? "+" : ""}
                      {(data?.spot_gap_pct ?? data?.gap_pct ?? 0).toFixed(2)}%)
                    </span>
                  </span>
                </div>
              </div>

              {/* Multi-contract context summary footer */}
              <div className="text-[11px] text-slate-400 flex items-center gap-2 flex-wrap pt-0.5">
                <span>Near Oct FUT: <strong className="font-mono text-slate-200">{(data?.oct_fut_ltp ?? data?.near_fut_close ?? 22774.8).toFixed(1)}</strong> ({(data?.oct_fut_gap_pts ?? 0) > 0 ? `+${(data?.oct_fut_gap_pts ?? 0).toFixed(1)}` : (data?.oct_fut_gap_pts ?? 0).toFixed(1)} pts vs Fut PDC)</span>
                <span>•</span>
                <span>Next Nov FUT: <strong className="font-mono text-indigo-300">{(data?.nov_fut_ltp ?? 22889.8).toFixed(1)}</strong> (Carry: {(data?.roll_spread ?? 115.0) > 0 ? `+${(data?.roll_spread ?? 115.0).toFixed(1)}` : (data?.roll_spread ?? 115.0).toFixed(1)} pts vs Oct)</span>
                <span>•</span>
                <span>GIFT Nifty Ref: <strong className="font-mono text-slate-300">{(data?.gift_nifty_ltp ?? 22642.0).toFixed(1)}</strong></span>
              </div>
            </div>

            {/* Right: Confluence Bias Pill */}
            <div className="flex flex-col items-start lg:items-end gap-1.5 bg-[#070b14] p-3 rounded-lg border border-slate-700/60 min-w-[280px]">
              <div className="text-[10px] font-bold uppercase tracking-wider text-slate-400 flex items-center justify-between w-full">
                <span>Opening Confluence Score</span>
                <span className="font-mono text-emerald-400 font-bold">{data?.confluence_score} ({data?.confluence_pct}%)</span>
              </div>

              {/* Progress bar */}
              <div className="w-full bg-slate-800 h-1.5 rounded-full overflow-hidden">
                <div
                  className={`h-full transition-all duration-500 rounded-full ${
                    (data?.confluence_pct || 0) >= 80
                      ? "bg-emerald-500"
                      : (data?.confluence_pct || 0) >= 60
                      ? "bg-teal-400"
                      : (data?.confluence_pct || 0) >= 40
                      ? "bg-amber-400"
                      : "bg-rose-500"
                  }`}
                  style={{ width: `${data?.confluence_pct || 0}%` }}
                />
              </div>

              <div className="text-sm font-black tracking-tight" style={{ color: pb?.color || "#38bdf8" }}>
                {pb?.title || "Evaluating Opening Confluence..."}
              </div>

              <div className="text-[11px] text-slate-400 text-left lg:text-right">
                {data?.updated_at ? `Telemetry synced at ${data.updated_at}` : "Stand by for live telemetry"}
              </div>
            </div>
          </div>
        </div>

        {/* ── 📊 Live Multi-Contract Pre-Market Benchmark Table (Redesigned with Row 1 = NIFTY 50 Spot vs PDC) ── */}
        <div className="rounded-xl border border-slate-700/80 bg-[#090f1d] p-3.5 shadow-xl shadow-black/40 overflow-hidden">
          <div className="flex flex-wrap items-center justify-between pb-2.5 mb-2.5 border-b border-slate-800 gap-2">
            <div className="flex items-center gap-2">
              <span className="text-sm">📊</span>
              <span className="font-extrabold text-sm text-slate-100 tracking-tight">
                Live Multi-Contract Pre-Market Benchmark Table
              </span>
              <span className="text-[10px] text-slate-400 hidden md:inline">
                • Synchronized Live Dhan Feed: Row 1 = Domestic Spot vs. PDC • Row 2 = Near-Month Fut • Row 3 = Next-Month Fut • Row 4 = GIFT Nifty
              </span>
            </div>
            <div className="flex items-center gap-2">
              <span className="px-2 py-0.5 rounded text-[10px] font-bold uppercase tracking-wider bg-emerald-950/50 text-emerald-300 border border-emerald-600/40 animate-pulse">
                ⚡ Direct Dhan Live Feed
              </span>
            </div>
          </div>

          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs border-collapse">
              <thead>
                <tr className="border-b border-slate-800 text-[10px] uppercase font-bold text-slate-400 tracking-wider bg-slate-900/60">
                  <th className="py-2.5 px-3">Instrument</th>
                  <th className="py-2.5 px-3">Live LTP</th>
                  <th className="py-2.5 px-3">Yesterday Close (PDC)</th>
                  <th className="py-2.5 px-3">Gap vs PDC / Diff</th>
                  <th className="py-2.5 px-3">Proximity / Basis</th>
                  <th className="py-2.5 px-3">Action Bias</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800/60 font-mono">
                {data?.multi_contract_table?.map((row, idx) => {
                  const isAnchor = row.is_anchor;
                  const isSpot = row.symbol === "NIFTY 50" || idx === 0;
                  const isOct = !isSpot && (row.symbol.includes("Oct") || row.instrument.includes("October") || idx === 1);
                  const isNov = !isSpot && !isOct && (row.symbol.includes("Nov") || row.instrument.includes("November") || idx === 2);
                  const isGift = row.symbol === "GIFT NIFTY" || (!isSpot && !isOct && !isNov);
                  const gapVal = row.gap_pts ?? row.diff_vs_oct ?? 0.0;
                  const pdcVal = row.pdc ?? (isSpot ? (data.pdc_spot ?? data.levels?.pdc) : (isOct ? (data.oct_fut_pdc ?? 22771.2) : (isNov ? (data.nov_fut_pdc ?? 22900.2) : (data.near_fut_close ?? 22771.2))));

                  return (
                    <tr
                      key={row.symbol}
                      className={`transition-colors ${
                        isSpot
                          ? "bg-amber-950/20 hover:bg-amber-950/30 font-semibold"
                          : isOct
                          ? "bg-emerald-950/15 hover:bg-emerald-950/25"
                          : isNov
                          ? "bg-indigo-950/15 hover:bg-indigo-950/25"
                          : "bg-slate-900/30 hover:bg-slate-800/40 text-slate-400"
                      }`}
                    >
                      {/* Instrument */}
                      <td className="py-2.5 px-3">
                        <div className="flex items-center gap-1.5 font-sans flex-wrap">
                          {isSpot ? (
                            <span className="text-amber-400 font-bold text-sm">🇮🇳</span>
                          ) : isOct ? (
                            <span className="text-emerald-400 font-bold text-sm">📈</span>
                          ) : isNov ? (
                            <span className="text-indigo-400 font-bold text-sm">📅</span>
                          ) : (
                            <span className="text-sky-400 font-bold text-sm">🌐</span>
                          )}

                          <span className={`font-bold ${
                            isSpot ? "text-amber-300" : isOct ? "text-emerald-300" : isNov ? "text-indigo-300" : "text-slate-300"
                          }`}>
                            {row.instrument}
                          </span>

                          {isSpot && isAnchor && (
                            <span className="px-1.5 py-0.5 rounded text-[9px] font-black uppercase tracking-wider bg-emerald-950 text-emerald-300 border border-emerald-500/50">
                              PRIMARY ANCHOR
                            </span>
                          )}

                          {isGift && !isAnchor && (
                            <span className="px-1.5 py-0.5 rounded text-[9px] font-bold uppercase tracking-wider bg-slate-800 text-slate-400 border border-slate-700">
                              SECONDARY AFTER 09:00 IST
                            </span>
                          )}

                          <span className="text-[10px] text-slate-500 font-mono">({row.symbol})</span>
                        </div>
                      </td>

                      {/* Live LTP */}
                      <td className="py-2.5 px-3 font-bold text-white text-[13px]">
                        {row.ltp.toLocaleString("en-IN", { minimumFractionDigits: 1, maximumFractionDigits: 1 })}
                      </td>

                      {/* Yesterday Close (PDC) */}
                      <td className="py-2.5 px-3 font-bold text-slate-300 text-xs">
                        {pdcVal ? pdcVal.toLocaleString("en-IN", { minimumFractionDigits: 1, maximumFractionDigits: 1 }) : "—"}
                      </td>

                      {/* Gap vs PDC / Diff */}
                      <td className="py-2.5 px-3">
                        <div className="flex items-center gap-1.5">
                          <span className={`font-bold ${
                            gapVal > 0 ? "text-emerald-400" : gapVal < 0 ? "text-rose-400" : "text-slate-400"
                          }`}>
                            {gapVal > 0 ? `+${gapVal.toFixed(1)}` : gapVal.toFixed(1)} pts
                          </span>
                          {row.gap_pct !== undefined && (
                            <span className="text-[10px] text-slate-400 font-sans">
                              ({row.gap_pct > 0 ? `+${row.gap_pct.toFixed(2)}` : row.gap_pct.toFixed(2)}%)
                            </span>
                          )}
                        </div>
                      </td>

                      {/* Proximity / Basis */}
                      <td className="py-2.5 px-3 font-sans">
                        {isSpot ? (
                          <span className="text-[11px] font-bold text-slate-200">
                            {row.proximity || (data?.levels?.pdh ? `${(data.levels.pdh - row.ltp).toFixed(1)} pts below PDH ${data.levels.pdh.toFixed(0)}` : "—")}
                          </span>
                        ) : isOct ? (
                          <span className="text-[11px] font-semibold text-emerald-300">
                            Basis vs Spot: {row.basis_vs_spot !== undefined ? `${row.basis_vs_spot > 0 ? `+${row.basis_vs_spot.toFixed(1)}` : row.basis_vs_spot.toFixed(1)} pts` : `${(row.ltp - (data?.live_spot ?? 22777.8)).toFixed(1)} pts`}
                          </span>
                        ) : isNov ? (
                          <span className="text-[11px] font-semibold text-indigo-300">
                            Carry Spread: {(row.roll_spread ?? (row.ltp - (data?.oct_fut_ltp ?? 22774.8))) > 0 ? `+${(row.roll_spread ?? (row.ltp - (data?.oct_fut_ltp ?? 22774.8))).toFixed(1)}` : (row.roll_spread ?? (row.ltp - (data?.oct_fut_ltp ?? 22774.8))).toFixed(1)} pts vs Oct
                          </span>
                        ) : (
                          <span className="text-[11px] text-slate-400">
                            {row.diff_vs_oct !== undefined ? `${row.diff_vs_oct > 0 ? `+${row.diff_vs_oct.toFixed(1)}` : row.diff_vs_oct.toFixed(1)} pts vs Oct Fut` : "—"}
                          </span>
                        )}
                      </td>

                      {/* Action Bias */}
                      <td className="py-2.5 px-3 font-sans">
                        <span className="text-[11px] font-bold text-slate-200 flex items-center gap-1">
                          <span>🎯</span>
                          {row.action_bias}
                        </span>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>

        {/* ── 3 Main Analytical Cards Grid ── */}
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
          {/* ════════════════════════════════════════════════════════════════
              Card 1: Opening Gap & Key Reference Levels
             ════════════════════════════════════════════════════════════════ */}
          <div className="rounded-xl border border-slate-800 bg-[#0a101f] p-4 flex flex-col justify-between shadow-lg">
            <div>
              <div className="flex items-center justify-between pb-3 border-b border-slate-800 mb-3">
                <div className="flex items-center gap-2">
                  <span className="text-base">📍</span>
                  <span className="font-bold text-sm text-slate-200">Key Reference Levels</span>
                </div>
                <span className="text-[10px] text-slate-500 uppercase tracking-wider font-semibold">
                  Structural Map
                </span>
              </div>

              {/* Levels Table */}
              <div className="space-y-2 text-xs">
                <div className="flex items-center justify-between py-1 px-2 rounded bg-slate-900/60 border border-slate-800/80">
                  <span className="text-slate-400">Previous Day Close (PDC):</span>
                  <span className="font-mono font-bold text-slate-200">{levels?.pdc?.toFixed(1) ?? "—"}</span>
                </div>

                <div className="flex items-center justify-between py-1 px-2 rounded bg-slate-900/60 border border-slate-800/80">
                  <span className="text-slate-400">PDH / PDL Range:</span>
                  <span className="font-mono text-slate-300">
                    <span className="text-emerald-400 font-bold">{levels?.pdh?.toFixed(1) ?? "—"}</span> / <span className="text-rose-400 font-bold">{levels?.pdl?.toFixed(1) ?? "—"}</span>
                  </span>
                </div>

                <div className="flex items-center justify-between py-1 px-2 rounded bg-emerald-950/30 border border-emerald-500/30">
                  <span className="text-emerald-300 font-semibold flex items-center gap-1">
                    <span>🛡️</span> S1 Put Wall (Support Floor):
                  </span>
                  <span className="font-mono font-black text-emerald-400">{levels?.put_wall?.toFixed(0) ?? "—"}</span>
                </div>

                <div className="flex items-center justify-between py-1 px-2 rounded bg-rose-950/30 border border-rose-500/30">
                  <span className="text-rose-300 font-semibold flex items-center gap-1">
                    <span>🏰</span> R1 Call Wall (Ceiling):
                  </span>
                  <span className="font-mono font-black text-rose-400">{levels?.call_wall?.toFixed(0) ?? "—"}</span>
                </div>

                <div className="flex items-center justify-between py-1 px-2 rounded bg-indigo-950/30 border border-indigo-500/30">
                  <span className="text-indigo-300 font-semibold flex items-center gap-1">
                    <span>🎯</span> Straddle Pin Box (BEs):
                  </span>
                  <span className="font-mono text-indigo-300 font-bold">
                    {levels?.lower_straddle_be?.toFixed(0) ?? "—"} – {levels?.upper_straddle_be?.toFixed(0) ?? "—"}
                  </span>
                </div>

                <div className="flex items-center justify-between py-1 px-2 rounded bg-sky-950/30 border border-sky-500/30">
                  <span className="text-sky-300 font-semibold flex items-center gap-1">
                    <span>⚡</span> Session VWAP Target:
                  </span>
                  <span className="font-mono font-black text-sky-300">{levels?.vwap?.toFixed(1) ?? "—"}</span>
                </div>
              </div>
            </div>

            {/* Structural Location Insight */}
            <div className="mt-4 pt-3 border-t border-slate-800 text-[11px] text-slate-400 space-y-1">
              <div className="font-bold text-slate-300">Open Positioning Assessment:</div>
              {data?.expected_open_spot && levels ? (
                <div>
                  {data.expected_open_spot <= levels.put_wall + 40 && data.expected_open_spot >= levels.put_wall - 30 ? (
                    <span className="text-emerald-400 font-semibold">
                      ⚡ Open lands directly on Put Support ({levels.put_wall.toFixed(0)}). High probability of absorption & bounce!
                    </span>
                  ) : data.expected_open_spot >= levels.call_wall - 40 && data.expected_open_spot <= levels.call_wall + 30 ? (
                    <span className="text-rose-400 font-semibold">
                      🏰 Open lands directly against Call Ceiling ({levels.call_wall.toFixed(0)}). Expect rejection or explosive squeeze!
                    </span>
                  ) : (
                    <span>
                      Open sits at {data.expected_open_spot.toFixed(1)}, between Put floor {levels.put_wall.toFixed(0)} and Call roof {levels.call_wall.toFixed(0)}.
                    </span>
                  )}
                </div>
              ) : null}
            </div>
          </div>

          {/* ════════════════════════════════════════════════════════════════
              Card 2: 5-Point Confluence Checklist
             ════════════════════════════════════════════════════════════════ */}
          <div className="rounded-xl border border-slate-800 bg-[#0a101f] p-4 flex flex-col justify-between shadow-lg">
            <div>
              <div className="flex items-center justify-between pb-3 border-b border-slate-800 mb-3">
                <div className="flex items-center gap-2">
                  <span className="text-base">✅</span>
                  <span className="font-bold text-sm text-slate-200">5-Point Confluence Checklist</span>
                </div>
                <span className="px-2 py-0.5 rounded text-[10px] font-black uppercase tracking-wider bg-slate-800 border border-slate-700 text-slate-300 font-mono">
                  {data?.confluence_score ?? "0/5"} Confirmed
                </span>
              </div>

              {/* Checklist Rows */}
              <div className="space-y-2.5">
                {data?.checklist?.map((item) => {
                  const isPassed = item.passed;
                  return (
                    <div
                      key={item.id}
                      className={`p-2.5 rounded-lg border text-xs transition-all ${
                        isPassed
                          ? "bg-emerald-950/20 border-emerald-500/40"
                          : "bg-slate-900/40 border-slate-800"
                      }`}
                    >
                      <div className="flex items-center justify-between gap-2">
                        <span className="font-bold text-slate-200 flex items-center gap-1.5">
                          <span>{isPassed ? "🟢" : "⚪"}</span>
                          {item.title}
                        </span>
                        <span
                          className={`px-1.5 py-0.5 rounded text-[9px] font-black uppercase tracking-wider ${
                            isPassed
                              ? "bg-emerald-900/60 text-emerald-300 border border-emerald-500/50"
                              : "bg-slate-800 text-slate-400 border border-slate-700"
                          }`}
                        >
                          {isPassed ? "CONFIRMED" : "WATCHING"}
                        </span>
                      </div>
                      <div className="mt-1 text-[11px] text-slate-400 leading-relaxed pl-5">
                        {item.detail}
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>

            {/* Writer Telemetry strip */}
            <div className="mt-3 pt-3 border-t border-slate-800 flex items-center justify-between text-[11px]">
              <span className="text-slate-400">Writer Flow Telemetry:</span>
              <div className="flex items-center gap-2 font-mono text-[10px]">
                <span className="text-emerald-400">Put Δ: {inr(flow?.fresh_put_delta, true)}</span>
                <span className="text-slate-600">•</span>
                <span className="text-rose-400">Call Δ: {inr(flow?.fresh_call_delta, true)}</span>
                <span className="text-slate-600">•</span>
                <span className="text-sky-300">PCR: {flow?.pcr?.toFixed(3) ?? "—"}</span>
              </div>
            </div>
          </div>

          {/* ════════════════════════════════════════════════════════════════
              Card 3: 09:15–09:45 AM Execution Plan & Timeline
             ════════════════════════════════════════════════════════════════ */}
          <div className="rounded-xl border border-slate-800 bg-[#0a101f] p-4 flex flex-col justify-between shadow-lg">
            <div>
              <div className="flex items-center justify-between pb-3 border-b border-slate-800 mb-3">
                <div className="flex items-center gap-2">
                  <span className="text-base">⚡</span>
                  <span className="font-bold text-sm text-slate-200">Execution Plan (09:15–09:45)</span>
                </div>
                <span className="text-[10px] text-slate-500 uppercase tracking-wider font-semibold">
                  Prop Desk Rule
                </span>
              </div>

              {/* Action Banner */}
              <div
                className="rounded-lg p-3 border mb-2 shadow-md"
                style={{
                  backgroundColor: `${pb?.color || "#10b981"}15`,
                  borderColor: `${pb?.color || "#10b981"}50`,
                }}
              >
                <div className="text-[10px] uppercase font-black tracking-wider text-slate-400">Tactical Action</div>
                <div className="text-sm font-black text-white mt-0.5 tracking-wide">
                  {pb?.action || "STAND ASIDE & OBSERVE OPENING RANGE"}
                </div>
              </div>

              {/* Dynamic Contract Gap Context Pills */}
              <div className="flex flex-wrap items-center gap-1.5 mb-3">
                {data?.multi_contract_table?.slice(1).map((r) => (
                  <span
                    key={r.symbol}
                    className="px-2 py-0.5 rounded text-[10px] font-mono border bg-slate-900/60"
                    style={{
                      borderColor: `${r.badge_color || "#64748b"}40`,
                      color: r.badge_color || "#cbd5e1",
                    }}
                  >
                    <span className="font-sans font-bold text-slate-400">{r.symbol.replace("NIFTY-", "").replace("-FUT", "")}: </span>
                    {(r.diff_vs_oct ?? 0) > 0 ? `+${(r.diff_vs_oct ?? 0).toFixed(1)}` : (r.diff_vs_oct ?? 0).toFixed(1)} pts
                  </span>
                ))}
              </div>

              {/* Trade Execution Matrix */}
              <div className="grid grid-cols-2 gap-2 text-xs mb-3">
                <div className="p-2 rounded bg-slate-900/60 border border-slate-800">
                  <div className="text-[10px] text-slate-400 uppercase font-semibold">Entry Zone</div>
                  <div className="font-mono font-bold text-emerald-300 mt-0.5">{pb?.entry_zone || "—"}</div>
                </div>

                <div className="p-2 rounded bg-slate-900/60 border border-slate-800">
                  <div className="text-[10px] text-slate-400 uppercase font-semibold">Strict Index SL</div>
                  <div className="font-mono font-bold text-rose-400 mt-0.5">
                    {pb?.stop_loss ? `< ${pb.stop_loss.toFixed(1)}` : "—"}
                  </div>
                </div>

                <div className="p-2 rounded bg-slate-900/60 border border-slate-800">
                  <div className="text-[10px] text-slate-400 uppercase font-semibold">Target 1 (VWAP / Scalp)</div>
                  <div className="font-mono font-bold text-sky-300 mt-0.5">
                    {pb?.target_1?.toFixed(1) ?? "—"} <span className="text-[10px] text-slate-400">({rr1}R)</span>
                  </div>
                </div>

                <div className="p-2 rounded bg-slate-900/60 border border-slate-800">
                  <div className="text-[10px] text-slate-400 uppercase font-semibold">Target 2 (PDC / Gap Fill)</div>
                  <div className="font-mono font-bold text-amber-300 mt-0.5">
                    {pb?.target_2?.toFixed(1) ?? "—"} <span className="text-[10px] text-slate-400">({rr2}R)</span>
                  </div>
                </div>
              </div>

              {/* Narrative Box */}
              <div className="p-2.5 rounded bg-slate-950/60 border border-slate-800 text-[11px] text-slate-300 leading-relaxed mb-3">
                <span className="font-bold text-slate-200">Playbook Directive: </span>
                {pb?.narrative || "Monitoring morning structure..."}
              </div>

              {/* 4-Step Timeline Strip */}
              <div className="space-y-1.5">
                <div className="text-[10px] uppercase font-bold text-slate-400 tracking-wider">
                  Morning Execution Timeline:
                </div>
                <div className="space-y-1">
                  {data?.timeline?.map((step, idx) => {
                    const isDone = step.status === "COMPLETED";
                    const isActive = step.status === "ACTIVE";
                    return (
                      <div
                        key={idx}
                        className={`flex items-start gap-2 p-1.5 rounded text-[10px] border ${
                          isActive
                            ? "bg-amber-950/30 border-amber-500/50 text-amber-200 font-medium"
                            : isDone
                            ? "bg-slate-900/40 border-slate-800/80 text-slate-400 line-through"
                            : "bg-slate-900/20 border-slate-800/40 text-slate-500"
                        }`}
                      >
                        <span className="font-mono font-bold shrink-0">{step.time}</span>
                        <div className="flex-1">
                          <span className="font-bold">{step.title}: </span>
                          <span>{step.instruction}</span>
                        </div>
                      </div>
                    );
                  })}
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
