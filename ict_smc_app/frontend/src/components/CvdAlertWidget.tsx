// CvdAlertWidget.tsx — Real-Time CVD vs. VWAP Divergence Alert Engine & Live Dashboard Widget.
import React, { useCallback, useEffect, useRef, useState } from "react";

export interface CvdDivergenceEvent {
  state: string;
  setup_id: string;
  updated_ts: number;
  structure_trigger: number;
  invalidation: number;
  bar_index: number;
  ts: number;
  key: string;
  type: "BEARISH_DIVERGENCE" | "BULLISH_DIVERGENCE";
  dir: "short" | "long";
  name: string;
  headline: string;
  action: string;
  entry: number | null;
  sl: number | null;
  tp: number | null;
  risk_pts: number | null;
  rr: number | null;
  vwap_status: string;
  time_str: string;
  price_swing: { curr: number; prev: number; diff: number };
  cvd_swing: { curr: number; prev: number; diff: number };
}

export interface CvdBar {
  ts: number;
  time: number;
  time_str: string;
  o: number;
  h: number;
  l: number;
  c: number;
  vol: number;
  delta: number;
  cvd: number;
  vwap: number;
  is_swing_high?: boolean;
  is_swing_low?: boolean;
  divergence_tag?: string | null;
}

export interface CvdDivergencePayload {
  ok: boolean;
  message?: string;
  alerts_enabled?: boolean;
  data_quality?: { source: string; estimated: boolean; contract_id?: string; coverage?: number; age_s?: number; reason?: string; cvd_anchor?: string; missing_delta_minutes?: string[]; provenance_verified?: boolean };
  timeframe: string;
  current_price: number;
  current_cvd: number;
  current_vwap: number;
  vwap_diff_pts: number;
  cvd_slope_status: string;
  cvd_slope_delta: number;
  active_divergence: CvdDivergenceEvent | null;
  summary_card: {
    title: string;
    type: string;
    badge_color: string;
    headline: string;
    action: string;
    entry: number | null;
    sl: number | null;
    tp: number | null;
    risk_pts: number | null;
    vwap_status: string;
    time_str: string;
    price_swing: { curr: number; prev: number; diff: number };
    cvd_swing: { curr: number; prev: number; diff: number };
  } | null;
  recent_divergences: CvdDivergenceEvent[];
  bars: CvdBar[];
  total_bars: number;
}

interface CvdAlertWidgetProps {
  initialTf?: "3m" | "5m";
  onClose?: () => void;
  compact?: boolean;
}

export default function CvdAlertWidget({ initialTf = "3m", onClose, compact = false }: CvdAlertWidgetProps) {
  const [tf, setTf] = useState<"3m" | "5m">(initialTf);
  const [data, setData] = useState<CvdDivergencePayload | null>(null);
  const [loading, setLoading] = useState(false);
  const [quality, setQuality] = useState<{ minutes: number; eligible_minutes: number; quality: { assignment_pct: Record<string, number | null> } } | null>(null);
  const [qualityError, setQualityError] = useState<string | null>(null);
  const [fetchError, setFetchError] = useState<string | null>(null);
  const [audioEnabled, setAudioEnabled] = useState<boolean>(() => {
    try {
      return localStorage.getItem("ict_cvd_audio_enabled") !== "false";
    } catch {
      return true;
    }
  });

  const lastBeepKeyRef = useRef<string | null>(null);
  const pollingRef = useRef<number | null>(null);

  // Synthesize Web Audio alert chime
  const playAlertChime = useCallback((isBull: boolean) => {
    try {
      const AudioCtx = window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
      if (!AudioCtx) return;
      const ctx = new AudioCtx();
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();

      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.type = "sine";

      const now = ctx.currentTime;
      if (isBull) {
        // Ascending chime: D5 (587Hz) -> A5 (880Hz)
        osc.frequency.setValueAtTime(587.33, now);
        osc.frequency.exponentialRampToValueAtTime(880.0, now + 0.22);
      } else {
        // Descending chime: A5 (880Hz) -> E4 (329Hz)
        osc.frequency.setValueAtTime(880.0, now);
        osc.frequency.exponentialRampToValueAtTime(329.63, now + 0.28);
      }

      gain.gain.setValueAtTime(0.35, now);
      gain.gain.exponentialRampToValueAtTime(0.005, now + 0.35);

      osc.start(now);
      osc.stop(now + 0.35);
    } catch (err) {
      console.warn("Audio chime failed to play:", err);
    }
  }, []);

  const toggleAudio = () => {
    setAudioEnabled((prev) => {
      const next = !prev;
      try {
        localStorage.setItem("ict_cvd_audio_enabled", String(next));
      } catch {
        /* noop */
      }
      return next;
    });
  };

  const fetchData = useCallback(async () => {
    try {
      const res = await fetch(`/api/desk/cvd-divergence?tf=${tf}`);
      if (!res.ok) throw new Error(`CVD API returned HTTP ${res.status}. Restart the backend if it has not loaded the update.`);
      const json: CvdDivergencePayload = await res.json();
      setData(json);
      setFetchError(null);

      // Trigger audio chime if a new divergence is detected
      const event = json.active_divergence;
      const beepKey = event ? `${event.key}:${event.state}` : null;
      const fresh = event && Date.now() / 1000 - event.updated_ts < 300;
      if (event && fresh && event.state === "VWAP_CONFIRMED" && beepKey !== lastBeepKeyRef.current && sessionStorage.getItem("cvd_beep:" + beepKey) !== "1") {
        lastBeepKeyRef.current = beepKey;
        sessionStorage.setItem("cvd_beep:" + beepKey, "1");
        if (audioEnabled) {
          playAlertChime(event.type === "BULLISH_DIVERGENCE");
        }
      }
    } catch (e) {
      setData(null);
      setFetchError(e instanceof Error ? e.message : "Cannot reach the CVD API.");
      console.warn("Failed to fetch CVD divergence:", e);
    } finally {
      setLoading(false);
    }
  }, [tf, audioEnabled, playAlertChime]);

  useEffect(() => {
    setLoading(true);
    fetchData();
    pollingRef.current = window.setInterval(fetchData, 6000);
    return () => {
      if (pollingRef.current) clearInterval(pollingRef.current);
    };
  }, [fetchData]);

  useEffect(() => {
    let cancelled = false;
    const refresh = async () => {
      try {
        const response = await fetch("/api/desk/cvd-quality");
        if (!response.ok) throw new Error(`Quality API HTTP ${response.status}`);
        const payload = await response.json();
        if (!cancelled) { setQuality(payload); setQualityError(null); }
      } catch (error) {
        if (!cancelled) { setQuality(null); setQualityError(error instanceof Error ? error.message : "Quality unavailable"); }
      }
    };
    refresh();
    const timer = window.setInterval(refresh, 6000);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, []);

  const activeDiv = data?.active_divergence;
  const isBear = activeDiv?.type === "BEARISH_DIVERGENCE";
  const isBull = activeDiv?.type === "BULLISH_DIVERGENCE";

  const cvdFmt = (v: number | undefined) => {
    if (v == null || isNaN(v)) return "—";
    const sign = v >= 0 ? "+" : "−";
    const abs = Math.abs(v);
    if (abs >= 1e5) return `${sign}${(abs / 1e5).toFixed(1)}L`;
    if (abs >= 1e3) return `${sign}${(abs / 1e3).toFixed(1)}K`;
    return `${sign}${Math.round(abs)}`;
  };

  return (
    <div className="flex flex-col w-full bg-[#080d19] border border-[#1b2a47] rounded-xl shadow-2xl text-slate-200 overflow-hidden text-xs select-none">
      {/* ──────────────── Top Navigation & Status Bar ──────────────── */}
      <div className="flex flex-wrap items-center justify-between gap-2 px-3 py-2 bg-[#0c1426] border-b border-[#1b2a47]">
        <div className="flex items-center gap-2">
          <span className="flex items-center gap-1.5 font-black text-sm tracking-wide text-sky-300">
            <span>📊</span>
            <span>CVD vs. VWAP DIVERGENCE ENGINE</span>
          </span>

          {/* Status Pill */}
          {data?.cvd_slope_status && (
            <span
              className={`px-2.5 py-0.5 rounded-full font-bold text-[11px] border tracking-wide transition-all shadow-sm ${
                data.cvd_slope_status.includes("Short Trap")
                  ? "bg-rose-950/90 border-rose-500/80 text-rose-300 ring-1 ring-rose-500/40 animate-pulse"
                  : data.cvd_slope_status.includes("Dip Accumulation")
                  ? "bg-emerald-950/90 border-emerald-500/80 text-emerald-300 ring-1 ring-emerald-500/40 animate-pulse"
                  : data.cvd_slope_status.includes("RISING")
                  ? "bg-emerald-950/50 border-emerald-600/50 text-emerald-300"
                  : data.cvd_slope_status.includes("FALLING")
                  ? "bg-rose-950/50 border-rose-600/50 text-rose-300"
                  : "bg-slate-800/80 border-slate-600/50 text-slate-300"
              }`}
            >
              {data.cvd_slope_status}
            </span>
          )}
        </div>

        {/* Controls: Timeframe, Audio Mute, Refresh, Close */}
        <div className="flex items-center gap-1.5 text-[11px]">
          {/* Timeframe Toggle */}
          <div className="flex rounded bg-[#101b33] p-0.5 border border-slate-700/60 font-bold">
            {(["3m", "5m"] as const).map((t) => (
              <button
                key={t}
                onClick={() => setTf(t)}
                className={`px-2 py-0.5 rounded transition-all ${
                  tf === t
                    ? "bg-sky-500/30 text-sky-200 border border-sky-500/70 shadow-sm"
                    : "text-slate-400 hover:text-slate-200"
                }`}
              >
                {t}
              </button>
            ))}
          </div>

          {/* Audio Alert Toggle */}
          <button
            onClick={toggleAudio}
            title={audioEnabled ? "Audio Alert Chime Enabled (Click to Mute)" : "Audio Muted (Click to Enable)"}
            className={`flex items-center gap-1 px-2 py-0.5 rounded border font-semibold transition-all ${
              audioEnabled
                ? "bg-emerald-950/40 border-emerald-500/60 text-emerald-300"
                : "bg-slate-800/60 border-slate-700 text-slate-400"
            }`}
          >
            <span>{audioEnabled ? "🔊" : "🔇"}</span>
            <span className="hidden sm:inline">{audioEnabled ? "Audio ON" : "Muted"}</span>
          </button>

          {/* Refresh button */}
          <button
            onClick={() => {
              setLoading(true);
              fetchData();
            }}
            title="Refresh CVD & VWAP Data"
            className="px-2 py-0.5 rounded border border-slate-700 bg-slate-800/60 hover:bg-slate-700 text-slate-300 transition-all font-semibold"
          >
            {loading ? "…" : "↻"}
          </button>

          {onClose && (
            <button
              onClick={onClose}
              className="px-1.5 py-0.5 rounded text-slate-400 hover:text-white hover:bg-rose-900/60 border border-transparent hover:border-rose-500/50"
            >
              ✕
            </button>
          )}
        </div>
      </div>

      <div className="px-3 py-2 text-[11px] text-slate-400 border-b border-slate-800">
        Source: {data?.data_quality?.source || "—"} (estimated) · Contract: {data?.data_quality?.contract_id || "—"}
        {data?.data_quality?.coverage != null && ` · Coverage: ${(data.data_quality.coverage * 100).toFixed(1)}%`}
        {data?.data_quality?.age_s != null && ` · Age: ${data.data_quality.age_s}s`}
        {data?.data_quality?.reason && ` · ${data.data_quality.reason}`}
        {data?.data_quality?.cvd_anchor && ` · CVD since ${data.data_quality.cvd_anchor}`}
        {data?.data_quality?.missing_delta_minutes?.length ? ` · Missing delta: ${data.data_quality.missing_delta_minutes.join(", ")}` : ""}
      </div>
      <div className="px-3 py-2 text-[11px] text-amber-200 border-b border-slate-800">
        Research provenance: {quality ? `${quality.eligible_minutes}/${quality.minutes} recorded minutes pass the gate` : qualityError || "checking…"}.
        {quality && <> Assignment by volume increment: quote {quality.quality.assignment_pct.quote_v ?? "—"}% · tick fallback {quality.quality.assignment_pct.tick_v ?? "—"}% · unclassified {quality.quality.assignment_pct.unclassified_v ?? "—"}%.</>}
        <span className="block text-slate-400">Research collection only. Legacy history is unverified; these percentages are not an error rate. Existing chart signals are unchanged.</span>
      </div>
      {/* ──────────────── Live Metric Strip ──────────────── */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 px-3 py-2 bg-[#0a0f1d] border-b border-[#17233c] text-[11px]">
        <div className="flex flex-col">
          <span className="text-[10px] text-slate-500 uppercase font-bold tracking-wider">Futures Price</span>
          <span className="text-sm font-black text-slate-100 font-mono">
            {data?.current_price ? data.current_price.toLocaleString("en-IN", { minimumFractionDigits: 1 }) : "—"}
          </span>
        </div>

        <div className="flex flex-col">
          <span className="text-[10px] text-slate-500 uppercase font-bold tracking-wider">Session VWAP (approx.)</span>
          <span className="text-sm font-black text-amber-300 font-mono flex items-center gap-1">
            <span>{data?.current_vwap ? data.current_vwap.toLocaleString("en-IN", { minimumFractionDigits: 1 }) : "—"}</span>
            {data?.ok && data.vwap_diff_pts != null && (
              <span
                className={`text-[10px] font-bold ${
                  data.vwap_diff_pts >= 0 ? "text-emerald-400" : "text-rose-400"
                }`}
              >
                ({data.vwap_diff_pts >= 0 ? "+" : ""}
                {data.vwap_diff_pts}p)
              </span>
            )}
          </span>
        </div>

        <div className="flex flex-col">
          <span className="text-[10px] text-slate-500 uppercase font-bold tracking-wider">{data?.data_quality?.cvd_anchor ? `CVD since ${data.data_quality.cvd_anchor}` : "Cumulative CVD"}</span>
          <span
            className={`text-sm font-black font-mono ${
              (data?.current_cvd ?? 0) >= 0 ? "text-emerald-300" : "text-rose-300"
            }`}
          >
            {data?.ok ? cvdFmt(data.current_cvd) : "—"}
          </span>
        </div>

        <div className="flex flex-col">
          <span className="text-[10px] text-slate-500 uppercase font-bold tracking-wider">Last 3-Bar CVD Δ</span>
          <span
            className={`text-sm font-black font-mono ${
              (data?.cvd_slope_delta ?? 0) >= 0 ? "text-emerald-300" : "text-rose-300"
            }`}
          >
            {data?.ok ? cvdFmt(data.cvd_slope_delta) : "—"}
          </span>
        </div>
      </div>

      {/* ──────────────── Real-Time Flashing Alert Card ──────────────── */}
      {activeDiv ? (
        <div
          className={`mx-3 my-2.5 p-3 rounded-lg border shadow-lg transition-all ${
            isBear
              ? "bg-rose-950/40 border-rose-500/80 ring-1 ring-rose-500/30 text-rose-100"
              : "bg-emerald-950/40 border-emerald-500/80 ring-1 ring-emerald-500/30 text-emerald-100"
          }`}
        >
          <div className="flex flex-wrap items-center justify-between gap-2 border-b pb-2 border-white/10">
            <div className="flex items-center gap-2">
              <span className="text-base animate-pulse">{isBear ? "⚠️" : "🟢"}</span>
              <span className="font-black text-sm tracking-wide uppercase">{activeDiv.name}</span>
              <span className="text-[11px] font-mono text-slate-300 bg-black/40 px-1.5 py-0.5 rounded border border-white/10">
                {activeDiv.time_str}
              </span>
            </div>

            <span
              className={`px-2 py-0.5 rounded text-[11px] font-bold border ${
                isBear
                  ? "bg-rose-500/20 text-rose-300 border-rose-500/40"
                  : "bg-emerald-500/20 text-emerald-300 border-emerald-500/40"
              }`}
            >
              {activeDiv.vwap_status}
            </span>
          </div>

          {/* Description & Divergence Swing Geometry */}
          <div className="mt-2 text-xs font-medium text-slate-300 space-y-1">
            <p className="font-semibold text-slate-200">{activeDiv.headline}</p>
            <p>Structure trigger: {activeDiv.structure_trigger.toFixed(1)} · Invalidation: {activeDiv.invalidation.toFixed(1)}</p>
            <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] font-mono text-slate-300 bg-black/30 p-2 rounded border border-white/5">
              <span>
                <b>Price Swing:</b> {activeDiv.price_swing.prev} → {activeDiv.price_swing.curr}{" "}
                <b className={activeDiv.price_swing.diff >= 0 ? "text-emerald-400" : "text-rose-400"}>
                  ({activeDiv.price_swing.diff >= 0 ? "+" : ""}
                  {activeDiv.price_swing.diff} pts)
                </b>
              </span>
              <span>
                <b>CVD Swing:</b> {cvdFmt(activeDiv.cvd_swing.prev)} → {cvdFmt(activeDiv.cvd_swing.curr)}{" "}
                <b className={activeDiv.cvd_swing.diff >= 0 ? "text-emerald-400" : "text-rose-400"}>
                  ({cvdFmt(activeDiv.cvd_swing.diff)})
                </b>
              </span>
            </div>
          </div>

          {/* Tactical Action & Risk Box */}
          <div className="mt-2.5 grid grid-cols-1 sm:grid-cols-3 gap-2 text-xs">
            <div className="sm:col-span-2 flex items-center gap-2 bg-[#0c1426] px-2.5 py-1.5 rounded border border-sky-500/40">
              <span className="font-black text-sky-400 text-xs">STATUS:</span>
              <span className="font-extrabold text-slate-100">{activeDiv.action}</span>
            </div>
            <div className="flex items-center justify-between sm:justify-end gap-3 bg-[#0c1426] px-2.5 py-1.5 rounded border border-slate-700/60 font-mono text-[11px]">
              <span>
                SL: <b className="text-rose-400">{activeDiv.sl ?? "—"}</b>
              </span>
              <span>
                TP: <b className="text-emerald-400">{activeDiv.tp ?? "—"}</b>
              </span>
              <span className="text-slate-400">({activeDiv.risk_pts ?? "—"}p risk)</span>
            </div>
          </div>
        </div>
      ) : (
        <div className="mx-3 my-2 px-3 py-2 rounded-lg border border-slate-800 bg-[#0c1324] text-center text-slate-400 text-[11px]">
          {data?.ok ? (data.alerts_enabled === false ? data.message : "No active setup. Waiting for confirmed price pivots.") : fetchError || data?.message || "Waiting for futures data."}
        </div>
      )}

      {/* ──────────────── Recent Divergences History Log ──────────────── */}
      {!compact && (
        <div className="px-3 pb-3 flex flex-col gap-1.5">
          <div className="flex items-center justify-between text-[11px] font-bold text-slate-400 px-0.5">
            <span>TODAY'S CVD DIVERGENCE LOG ({data?.recent_divergences?.length || 0})</span>
            <span className="text-[10px] text-slate-500">Closed bars · estimated futures delta</span>
          </div>

          {data?.recent_divergences && data.recent_divergences.length > 0 ? (
            <div className="max-h-48 overflow-y-auto space-y-1.5 pr-1">
              {data.recent_divergences.map((d, i) => {
                const isDivergenceBear = d.type === "BEARISH_DIVERGENCE";
                return (
                  <div
                    key={d.key || i}
                    className={`flex flex-wrap items-center justify-between gap-2 px-2.5 py-1.5 rounded border text-[11px] ${
                      isDivergenceBear
                        ? "bg-rose-950/20 border-rose-500/30 text-rose-200"
                        : "bg-emerald-950/20 border-emerald-500/30 text-emerald-200"
                    }`}
                  >
                    <div className="flex items-center gap-2">
                      <span className="font-mono text-slate-400 font-bold">{d.time_str}</span>
                      <span className="font-black">{d.name}</span>
                    </div>

                    <div className="flex items-center gap-3 font-mono text-[10.5px]">
                      <span>
                        Px: {d.price_swing.prev} → {d.price_swing.curr} (
                        {d.price_swing.diff >= 0 ? "+" : ""}
                        {d.price_swing.diff}p)
                      </span>
                      <span>
                        CVD: {cvdFmt(d.cvd_swing.prev)} → {cvdFmt(d.cvd_swing.curr)}
                      </span>
                      <span className="font-sans font-bold text-slate-300 bg-black/40 px-1.5 py-0.5 rounded">
                        {d.action}
                      </span>
                    </div>
                  </div>
                );
              })}
            </div>
          ) : (
            <div className="p-3 text-center text-slate-500 text-[11px] border border-dashed border-slate-800 rounded">
              No divergence signals recorded earlier today.
            </div>
          )}
        </div>
      )}
    </div>
  );
}
