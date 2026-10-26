// KronosPanel.tsx — NIFTY 50 Kronos forecast tab.
//
// 360 one-hour candles in, 24 forecast candles out, 30 sampled runs kept (never averaged away).
// Shows the mean path, the full band, every individual run on demand, and — the part that keeps
// it honest — Step back + Reveal, which marks a past forecast against what actually happened.
//
// Every number on this panel is a COUNT of the model's own runs. It is never presented as a
// probability of NIFTY moving, because the published backtests put direction at roughly a coin
// flip. The hover text says so on every control.
import { useCallback, useEffect, useRef, useState } from "react";
import {
  CandlestickSeries, ColorType, createChart, CrosshairMode, LineSeries, LineStyle,
  type IChartApi, type ISeriesApi, type UTCTimestamp,
} from "lightweight-charts";

type Candle = { ts: number; o: number; h: number; l: number; c: number; v?: number };

type Summary = {
  ok: boolean; runs: number; horizon: number; last_close: number;
  up_count: number; down_count: number; up_of: string; down_of: string; undecided: boolean;
  mean_path: number[]; band_low: number[]; band_high: number[];
  mean_end: number; mean_end_pct: number; range_low: number; range_high: number;
  vol_amp_count: number; vol_amp_of: string; vol_amp_pct: number;
  expected_volume: number | null; has_real_volume: boolean;
};

type Forecast = {
  ok: boolean; message?: string; sidecar?: string; symbol?: string;
  origin_ts?: number; origin_label?: string; step_back?: number; is_live?: boolean;
  context?: Candle[]; future_ts?: number[]; paths?: number[][][];
  summary?: Summary;
  verdict?: { label: string; tone: string; text: string; caveat: string };
  actual?: Candle[];
  reveal?: { ok: boolean; revealed?: number; actual_end?: number; actual_pct?: number;
             direction_hit?: boolean; inside_band?: number; outside_band?: number;
             inside_band_pct?: number; mean_miss_pts?: number; note?: string };
  settings?: Record<string, unknown>;
  volume?: { source?: string; coverage_pct?: number; covered?: number; total?: number; used?: boolean; why?: string };
  max_step_back?: number;
  model?: { status?: string; done?: number; total?: number; error?: string };
};

type Status = {
  ok: boolean; sidecar?: string; message?: string;
  model?: { status?: string; done?: number; total?: number; error?: string };
  torch?: string; loaded?: boolean;
  settings?: { hist: number; pred: number; paths: number; T: number; top_p: number };
};

// ── hover guidance: what the number is, what to do with it, when to stand aside ────────────────
const HOVER = {
  panel:
    "KRONOS — a forecasting model trained on 12 billion candles from 45 exchanges (Tsinghua, arXiv 2508.02739).\n\n" +
    "It reads the last 360 one-hour NIFTY candles and writes the next 24, thirty separate times.\n\n" +
    "DO: Use the spread of those 30 runs as a first look at the plausible range for the next ~4 sessions.\n" +
    "DO NOT: Treat it as an entry signal. Published backtests on Indian equities put direction at about a coin flip.",
  runCount:
    "DIRECTION — how many of the 30 runs ended above or below the last close.\n\n" +
    "⚠️ THIS IS A COUNT, NOT A PROBABILITY. '22 of 30 up' means 22 of the model's own sampled paths ended higher.\n" +
    "It does NOT mean a 73% chance NIFTY rises.\n\n" +
    "DO: Use a lopsided count as weak context alongside your own OI and price-action read.\n" +
    "WAIT: A split near 15/15 means the model has no lean — treat the band as a range and nothing more.",
  meanPath:
    "MEAN FORECAST — the average of all 30 runs after 24 one-hour candles (~4 trading sessions).\n\n" +
    "DO: Read it as the centre of gravity of the model's paths, not a target.\n" +
    "⚠️ The average of 30 paths is a line no individual run actually took. Never set a stop or a target off it.",
  band:
    "FULL RANGE — the lowest and highest of the 30 runs at each step.\n\n" +
    "DO: Treat it as the model's plausible envelope for the next 4 sessions.\n" +
    "⚠️ Tested honestly, the real close finished OUTSIDE this band roughly 2 times in 3 — the band runs too narrow. " +
    "Widen your own expectations accordingly; do not size a position as if the edges were hard limits.",
  volAmp:
    "VOLATILITY AMPLIFICATION — how many runs swing harder over the next 24 candles than the last 24 real candles did.\n\n" +
    "HIGH (most runs expanding): generally favours option BUYERS — rising volatility lifts premiums.\n" +
    "LOW (few runs expanding): generally favours option SELLERS — a quiet, theta-friendly read.\n\n" +
    "DO: Use it to choose buying vs selling structure, not direction.",
  volume:
    "EXPECTED VOLUME over the next 24 candles.\n\n" +
    "NIFTY 50 is an index — it has no traded quantity of its own, so Dhan returns volume 0. " +
    "This panel does what the rest of the desk already does for RVOL: it attaches the NIFTY FUTURES volume " +
    "that actually traded in each hour (near + next month summed, the same basis as futures.combined_1m).\n\n" +
    "DO: Read a high expected volume as the model expecting participation, which usually accompanies range expansion.\n" +
    "⚠️ If coverage of the context window is below 95%, the volume column is DROPPED rather than fed in with holes — " +
    "gaps would be read by the model as genuinely quiet hours. The strip below the panel says which happened.",
  showRuns:
    "SHOW 30 RUNS — draws every individual Monte Carlo path instead of just the mean and the band.\n\n" +
    "DO: Look at how widely they fan out. A tight fan means the model is confident; a wide fan means " +
    "the band is being set by one or two outlier runs and is not a consensus.",
  stepBack:
    "STEP BACK — rewinds 'now' to an earlier candle and re-forecasts from there, with the real future hidden.\n\n" +
    "DO: Use this before you trust any forecast. Step back, read what the model said, then press Reveal.\n" +
    "This is the single most useful control on the panel — it is how you find out the model is wrong.",
  reveal:
    "REVEAL — draws what actually happened after a stepped-back forecast, one candle at a time.\n\n" +
    "The scorecard then marks the forecast: direction right or wrong, and how many real closes stayed inside the band.\n\n" +
    "⚠️ A unanimous 30-of-30 forecast can still be completely wrong. Reveal is there so you see that for yourself.",
  settlementNote:
    "SETTINGS — 360 candles of context, 24 forecast candles, 30 runs, temperature 1.0, top-p 0.9.\n\n" +
    "24 one-hour candles ≈ 4 trading sessions, so this is a POSITIONAL read. " +
    "It has no bearing on 0-DTE or intraday expiry decisions, which live on the other tabs.",
} as const;

const fmt = (v: number | undefined | null, d = 2) =>
  v === undefined || v === null ? "—" : v.toLocaleString("en-IN", { minimumFractionDigits: d, maximumFractionDigits: d });

export default function KronosPanel() {
  const box = useRef<HTMLDivElement | null>(null);
  const chart = useRef<IChartApi | null>(null);
  const candleS = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const actualS = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const meanS = useRef<ISeriesApi<"Line"> | null>(null);
  const loS = useRef<ISeriesApi<"Line"> | null>(null);
  const hiS = useRef<ISeriesApi<"Line"> | null>(null);
  const runS = useRef<ISeriesApi<"Line">[]>([]);

  const [status, setStatus] = useState<Status | null>(null);
  const [fc, setFc] = useState<Forecast | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string>("");
  const [stepBack, setStepBack] = useState(0);
  const [showRuns, setShowRuns] = useState(false);
  const [revealed, setRevealed] = useState(0);

  // ── data ────────────────────────────────────────────────────────────────────────────────────
  const loadStatus = useCallback(async () => {
    try {
      const r = await fetch("/api/kronos/status");
      setStatus(await r.json());
    } catch (e) {
      setStatus({ ok: false, sidecar: "down", message: String(e) });
    }
  }, []);

  const loadForecast = useCallback(async (sb: number, refresh = false) => {
    setBusy(true);
    setErr("");
    try {
      const r = await fetch(`/api/kronos/forecast?step_back=${sb}${refresh ? "&refresh=true" : ""}`);
      const j: Forecast = await r.json();
      setFc(j);
      setRevealed(0);
      if (!j.ok) setErr(j.message || "forecast unavailable");
    } catch (e) {
      setErr(String(e));
      setFc(null);
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => { void loadStatus(); }, [loadStatus]);
  useEffect(() => { void loadForecast(stepBack); }, [stepBack, loadForecast]);

  const downloadModel = async () => {
    setBusy(true);
    try {
      await fetch("/api/kronos/download-model", { method: "POST" });
      const poll = window.setInterval(async () => {
        await loadStatus();
      }, 3000);
      window.setTimeout(() => window.clearInterval(poll), 15 * 60 * 1000);
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  };

  // keyboard: ← steps back, R reveals
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement | null)?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA") return;
      if (e.key === "ArrowLeft") { e.preventDefault(); setStepBack((s) => Math.min(s + 6, fc?.max_step_back ?? 0)); }
      if (e.key === "ArrowRight") { e.preventDefault(); setStepBack((s) => Math.max(0, s - 6)); }
      if (e.key === "r" || e.key === "R") {
        e.preventDefault();
        setRevealed((n) => Math.min(n + 1, fc?.actual?.length ?? 0));
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [fc]);

  // ── chart ───────────────────────────────────────────────────────────────────────────────────
  useEffect(() => {
    if (!box.current || chart.current) return;
    const ch = createChart(box.current, {
      layout: { background: { type: ColorType.Solid, color: "#060b16" }, textColor: "#94a3b8", fontSize: 11 },
      grid: { vertLines: { color: "rgba(148,163,184,0.06)" }, horzLines: { color: "rgba(148,163,184,0.06)" } },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderColor: "rgba(148,163,184,0.2)" },
      timeScale: { borderColor: "rgba(148,163,184,0.2)", timeVisible: true, secondsVisible: false },
      autoSize: true,
    });
    candleS.current = ch.addSeries(CandlestickSeries, {
      upColor: "#10b981", downColor: "#ef4444", borderUpColor: "#10b981", borderDownColor: "#ef4444",
      wickUpColor: "#10b981", wickDownColor: "#ef4444",
    });
    actualS.current = ch.addSeries(CandlestickSeries, {
      upColor: "#22d3ee", downColor: "#f472b6", borderUpColor: "#22d3ee", borderDownColor: "#f472b6",
      wickUpColor: "#22d3ee", wickDownColor: "#f472b6", priceLineVisible: false, lastValueVisible: false,
    });
    hiS.current = ch.addSeries(LineSeries, {
      color: "rgba(232,195,90,0.45)", lineWidth: 1, lineStyle: LineStyle.Dashed,
      priceLineVisible: false, lastValueVisible: false, title: "run high",
    });
    loS.current = ch.addSeries(LineSeries, {
      color: "rgba(232,195,90,0.45)", lineWidth: 1, lineStyle: LineStyle.Dashed,
      priceLineVisible: false, lastValueVisible: false, title: "run low",
    });
    meanS.current = ch.addSeries(LineSeries, {
      color: "#fb923c", lineWidth: 2, priceLineVisible: false, lastValueVisible: true, title: "mean of 30",
    });
    chart.current = ch;
    return () => { ch.remove(); chart.current = null; };
  }, []);

  useEffect(() => {
    const ch = chart.current;
    if (!ch || !fc?.ok || !fc.context || !fc.summary || !fc.future_ts) return;

    candleS.current?.setData(fc.context.map((c) => ({
      time: c.ts as UTCTimestamp, open: c.o, high: c.h, low: c.l, close: c.c,
    })));

    const ft = fc.future_ts;
    const line = (vals: number[]) => vals.map((v, i) => ({ time: ft[i] as UTCTimestamp, value: v }));
    meanS.current?.setData(line(fc.summary.mean_path));
    hiS.current?.setData(line(fc.summary.band_high));
    loS.current?.setData(line(fc.summary.band_low));

    // individual runs
    runS.current.forEach((s) => { try { ch.removeSeries(s); } catch { /* already gone */ } });
    runS.current = [];
    if (showRuns && fc.paths) {
      for (const p of fc.paths) {
        const s = ch.addSeries(LineSeries, {
          color: "rgba(148,163,184,0.28)", lineWidth: 1, priceLineVisible: false,
          lastValueVisible: false, crosshairMarkerVisible: false,
        });
        s.setData(p.map((step, i) => ({ time: ft[i] as UTCTimestamp, value: step[3] })));
        runS.current.push(s);
      }
    }

    actualS.current?.setData((fc.actual ?? []).slice(0, revealed).map((c) => ({
      time: c.ts as UTCTimestamp, open: c.o, high: c.h, low: c.l, close: c.c,
    })));

    ch.timeScale().fitContent();
  }, [fc, showRuns, revealed]);

  // ── setup gates ─────────────────────────────────────────────────────────────────────────────
  const sidecarDown = status && !status.ok;
  const modelStatus = status?.model?.status ?? fc?.model?.status;
  const needsModel = !sidecarDown && modelStatus && modelStatus !== "ready";
  const dlPct = status?.model?.total
    ? Math.round(((status.model.done ?? 0) / status.model.total) * 100) : 0;

  const s = fc?.summary;
  const v = fc?.verdict;
  const rv = fc?.reveal;

  return (
    <div className="flex flex-col h-full min-h-0 bg-[#060b16] text-slate-200">
      {/* header */}
      <div className="flex flex-wrap items-center gap-2 px-2.5 py-1.5 border-b border-[#162238] text-[11px]">
        <span className="font-black uppercase tracking-wider text-orange-300 cursor-help" title={HOVER.panel}>
          🔮 Kronos · NIFTY 50
        </span>
        <span className="font-mono text-slate-400 cursor-help" title={HOVER.settlementNote}>
          1h · 360 in / {s?.horizon ?? 24} out · {s?.runs ?? 30} runs
        </span>
        <span className="text-slate-600">|</span>
        <span className="font-mono text-slate-300">
          {fc?.is_live ? "LIVE" : `stepped back ${fc?.step_back ?? 0}`} · origin {fc?.origin_label ?? "—"}
        </span>

        <div className="ml-auto flex items-center gap-1.5">
          <button
            onClick={() => setShowRuns((x) => !x)}
            title={HOVER.showRuns}
            className={`tab cursor-help ${showRuns ? "tab-on !border-slate-400" : ""}`}
          >
            {showRuns ? "◉" : "○"} Show {s?.runs ?? 30} runs
          </button>
          <button
            onClick={() => setStepBack((x) => Math.min(x + 6, fc?.max_step_back ?? 0))}
            title={HOVER.stepBack}
            className="tab cursor-help !border-sky-500/50 !text-sky-300"
          >
            ← Step back
          </button>
          <button
            onClick={() => setStepBack((x) => Math.max(0, x - 6))}
            title="Move 'now' forward again, back toward the live forecast."
            className="tab !border-sky-500/30 !text-sky-400/80"
          >
            Forward →
          </button>
          <button
            onClick={() => setRevealed((n) => Math.min(n + 1, fc?.actual?.length ?? 0))}
            disabled={!fc?.actual?.length}
            title={HOVER.reveal}
            className={`tab cursor-help ${fc?.actual?.length ? "!border-cyan-500/60 !text-cyan-300" : "opacity-40"}`}
          >
            R · Reveal {revealed > 0 ? `(${revealed}/${fc?.actual?.length})` : ""}
          </button>
          <button onClick={() => void loadForecast(stepBack, true)} title="Re-fetch candles and re-run the model."
            className="tab">{busy ? "…" : "↻"}</button>
        </div>
      </div>

      {/* setup / error strip */}
      {sidecarDown && (
        <div className="px-2.5 py-2 bg-amber-950/40 border-b border-amber-800/50 text-[11px] text-amber-200">
          <b>Kronos service is not running.</b> {status?.message}
          <div className="mt-1 font-mono text-[10px] text-amber-300/80">
            cd ict_smc_app/kronos &amp;&amp; ./run_kronos.sh
          </div>
          <div className="mt-1 text-amber-300/70">
            The model runs in its own process and virtual environment — the rest of this desk is unaffected while it is down.
          </div>
        </div>
      )}
      {needsModel && (
        <div className="px-2.5 py-2 bg-sky-950/40 border-b border-sky-800/50 text-[11px] text-sky-200 flex items-center gap-3">
          <span>
            <b>Kronos-base model not downloaded.</b> ~407 MB, fetched once from the authors' official release
            and verified file-by-file against a pinned SHA-256.
          </span>
          {modelStatus === "downloading"
            ? <span className="font-mono text-sky-300">downloading… {dlPct}%</span>
            : <button onClick={() => void downloadModel()} className="tab !border-sky-500 !text-sky-200">Download the model</button>}
          {status?.model?.error && <span className="text-rose-300 font-mono text-[10px]">{status.model.error}</span>}
        </div>
      )}
      {err && !sidecarDown && (
        <div className="px-2.5 py-1.5 bg-rose-950/30 border-b border-rose-900/50 text-[11px] text-rose-300">{err}</div>
      )}

      {/* body: chart + read-out */}
      <div className="flex-1 min-h-0 grid grid-cols-1 lg:grid-cols-[1fr_300px]">
        <div ref={box} className="min-h-[260px]" />

        <div className="border-l border-[#162238] p-2.5 space-y-2 overflow-auto text-[11px]">
          {/* verdict */}
          <div className={`rounded border p-2 ${
            v?.tone === "up" ? "border-emerald-700/60 bg-emerald-950/30"
              : v?.tone === "down" ? "border-rose-700/60 bg-rose-950/30"
                : "border-slate-700 bg-slate-900/40"}`}>
            <div className="font-black">{v?.label ?? "…"}</div>
            <div className="text-slate-300 mt-0.5 leading-snug">{v?.text ?? "Waiting for a forecast."}</div>
          </div>

          {/* run counts */}
          <div className="rounded border border-slate-800 p-2 cursor-help" title={HOVER.runCount}>
            <div className="text-slate-400 uppercase tracking-wide text-[10px] mb-1">Direction of the runs</div>
            <div className="flex items-center justify-between font-mono">
              <span className="text-emerald-400">▲ {s?.up_of ?? "—"}</span>
              <span className="text-rose-400">▼ {s?.down_of ?? "—"}</span>
            </div>
            <div className="mt-1 h-1.5 w-full rounded bg-rose-900/60 overflow-hidden">
              <div className="h-full bg-emerald-500"
                style={{ width: s ? `${(s.up_count / s.runs) * 100}%` : "0%" }} />
            </div>
            <div className="text-[9.5px] text-amber-400/80 mt-1 italic">A count of the model's runs — not a probability.</div>
          </div>

          {/* mean + range */}
          <div className="rounded border border-slate-800 p-2 space-y-1 font-mono">
            <div className="flex justify-between cursor-help" title={HOVER.meanPath}>
              <span className="text-slate-400">Mean forecast</span>
              <span className={s && s.mean_end_pct >= 0 ? "text-emerald-400" : "text-rose-400"}>
                {fmt(s?.mean_end)} ({s ? `${s.mean_end_pct >= 0 ? "+" : ""}${s.mean_end_pct}%` : "—"})
              </span>
            </div>
            <div className="flex justify-between cursor-help" title={HOVER.band}>
              <span className="text-slate-400">Full range</span>
              <span className="text-amber-200">{fmt(s?.range_low)} – {fmt(s?.range_high)}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-slate-400">Last close</span>
              <span className="text-white">{fmt(s?.last_close)}</span>
            </div>
          </div>

          {/* volatility + volume */}
          <div className="rounded border border-slate-800 p-2 space-y-1 font-mono">
            <div className="flex justify-between cursor-help" title={HOVER.volAmp}>
              <span className="text-slate-400">Vol. amplification</span>
              <span className={s && s.vol_amp_pct >= 60 ? "text-amber-300" : "text-slate-300"}>
                {s?.vol_amp_of ?? "—"} ({s?.vol_amp_pct ?? 0}%)
              </span>
            </div>
            <div className="flex justify-between cursor-help" title={HOVER.volume}>
              <span className="text-slate-400">Expected volume</span>
              <span className={s?.has_real_volume ? "text-sky-300" : "text-slate-500"}>
                {s?.has_real_volume ? fmt(s?.expected_volume, 0) : "dropped"}
              </span>
            </div>
            <div className="text-[9.5px] text-slate-500 leading-tight pt-0.5">
              {fc?.volume?.used
                ? `volume: ${fc.volume.source} · ${fc.volume.coverage_pct}% of context bars covered`
                : (fc?.volume?.why ?? "volume: none attached")}
            </div>
          </div>

          {/* reveal scorecard */}
          {rv?.ok && revealed > 0 && (
            <div className={`rounded border p-2 ${rv.direction_hit ? "border-emerald-700/60 bg-emerald-950/20" : "border-rose-700/60 bg-rose-950/20"}`}
              title={HOVER.reveal}>
              <div className="text-slate-400 uppercase tracking-wide text-[10px] mb-1">Reveal scorecard</div>
              <div className="font-mono space-y-0.5">
                <div className="flex justify-between">
                  <span>Direction</span>
                  <span className={rv.direction_hit ? "text-emerald-400" : "text-rose-400"}>
                    {rv.direction_hit ? "RIGHT" : "WRONG"}
                  </span>
                </div>
                <div className="flex justify-between">
                  <span>Actual close</span>
                  <span className="text-white">{fmt(rv.actual_end)} ({rv.actual_pct! >= 0 ? "+" : ""}{rv.actual_pct}%)</span>
                </div>
                <div className="flex justify-between">
                  <span>Inside the band</span>
                  <span className={(rv.inside_band_pct ?? 0) >= 70 ? "text-emerald-400" : "text-amber-300"}>
                    {rv.inside_band}/{(rv.inside_band ?? 0) + (rv.outside_band ?? 0)} ({rv.inside_band_pct}%)
                  </span>
                </div>
                <div className="flex justify-between">
                  <span>Mean path miss</span>
                  <span className="text-slate-300">{fmt(rv.mean_miss_pts)} pts</span>
                </div>
              </div>
              {rv.note && <div className="mt-1 text-[10px] text-amber-300 italic leading-tight">{rv.note}</div>}
            </div>
          )}

          {/* standing caveat */}
          <div className="rounded border border-amber-900/40 bg-amber-950/10 p-2 text-[10px] text-amber-200/90 leading-snug">
            {v?.caveat}
          </div>
          <div className="text-[9.5px] text-slate-500 leading-snug">
            Educational only — not investment advice. This desk never places an order.
            24 one-hour candles ≈ 4 sessions, so this is a positional read with no bearing on 0-DTE decisions.
          </div>
        </div>
      </div>
    </div>
  );
}
