// App.tsx — layout + data flow. REST for the initial state (so a refresh restores everything from SQLite),
// WebSocket for changes. Candles/analysis are re-fetched whenever the backend says a candle CLOSED.
import { useCallback, useEffect, useRef, useState } from "react";
import { api, type Analysis, type Candle, type Dashboard, type LiveMsg, type PaperStats, type PaperTrade, type Quote, type Scorecard, type Strikes, type TF, useLive } from "./api";
import InstrumentPanel from "./components/InstrumentPanel";
import Chart, { type Layers } from "./components/Chart";
import { DragBar, FullPanel, PeceTable, WritersFlow, useStoredHeight } from "./components/OcLab";
import SplitPane from "./components/SplitPane";
import { AutoPilotPanel, useAutopilot } from "./components/AutoPilot";
import WriterMap from "./components/WriterMap";
import FootprintDesk from "./components/FootprintDesk";
import { Alerts, BiasGrid, Builds, Header, Paper, Signals } from "./components/Panels";
import DeltaPanel from "./components/DeltaPanel";
import AuctionPanel from "./components/AuctionPanel";
import QmlPanel from "./components/QmlPanel";
import CrtCockpit from "./components/CrtCockpit";
import BiasPanel from "./components/BiasPanel";
import { AlertControls, AlertToasts, useRvolAlerts } from "./components/rvolAlerts";
import { SignalControls, useSigPrefs } from "./components/signalAlerts";
import BookPanel, { BookChip } from "./components/BookPanel";
import CombinedRvolChip from "./components/CombinedRvolChip";
import StdvPanel from "./components/StdvPanel";
import { type StdvLeg } from "./components/stdv";
import AlertsTable from "./components/AlertsTable";

const TFS: TF[] = ["1m", "5m", "15m", "30m", "1h", "4h", "1D"];
const DAYS: Record<TF, number> = { "1m": 1, "5m": 3, "15m": 8, "30m": 15, "1h": 30, "4h": 60, "1D": 60 };
const LAYER_NAMES: [keyof Layers, string][] = [
  ["ob", "Order blocks"], ["fvg", "FVG"], ["liq", "Liquidity"], ["structure", "BOS / MSS"],
  ["swings", "Swings"], ["range", "Premium / OTE"], ["session", "PDH · PDL · OR"], ["delta", "Aggression / Absorption"], ["deltaDetail", "All delta detail"], ["crt", "CRT + TBS"], ["auction", "Auction (value · accept / reject)"], ["volume", "Volume · VWAP"], ["oi", "Futures OI build-up"], ["rvol", "Cumulative RVOL"], ["qml", "QML · RBS / SBR"], ["fp", "Footprint Δ (est.)"], ["heat", "Heatmap"], ["ocAct", "PE−CE action"], ["dol", "D.O.L (IRL⇄ERL)"], ["smt", "SMT (vs BankNifty)"], ["bias", "⚖ Bias 19"], ["walls", "🧱 OI walls"], ["bubbles", "🫧 Big trades"], ["cells", "▦ FP cells"], ["cvd", "〽 CVD"], ["nodes", "🟨 Stacked nodes"], ["gamma", "γ Gamma 3✓"], ["tsoup", "🐢 Turtle soup"], ["rblock", "⟂ Rejection blocks"], ["p3", "📐 Power of 3 (AMD)"], ["stdv", "📐 STDV"],
];

const OFF: Layers = { ob: false, fvg: false, liq: false, structure: false, swings: false, range: false, session: false, delta: false, deltaDetail: false, crt: false, auction: false, volume: false, oi: false, rvol: false, qml: false, fp: false, bias: false, dol: false, smt: false, heat: false, ocAct: false, cvd: false, cells: false, walls: false, bubbles: false, nodes: false, gamma: false, tsoup: false, rblock: false, p3: false, stdv: false };
const PRESETS: [string, string, Partial<Layers>][] = [
  ["Clean", "structure + key levels + VWAP", { structure: true, session: true, volume: true }],
  ["SMC", "order blocks, FVG, liquidity, BOS/MSS, OTE, rejection blocks", { ob: true, fvg: true, liq: true, structure: true, range: true, session: true, rblock: true }],
  ["CRT", "CRT ranges, TBS, structure, D.O.L, SMT, rejection blocks", { crt: true, structure: true, session: true, dol: true, smt: true, fvg: true, rblock: true }],
  ["Order flow", "delta, auction value, volume, RVOL", { delta: true, auction: true, volume: true, rvol: true, fp: true, heat: true }],
  ["Order flow PRO", "OI walls, big-trade bubbles, footprint cells, CVD + BUY/SELL/WAIT read", { walls: true, bubbles: true, cells: true, cvd: true, volume: true, structure: true }],
  ["Nodes + σ", "stacked D/W/M volume nodes + VWAP ±2σ → ENTRY", { nodes: true, volume: true, structure: true }],
  ["Gamma 3✓", "gamma map + EMA stack + flag + volume → QUALIFIES / WALK AWAY", { gamma: true }],
  ["Futures", "volume, futures OI, RVOL panes", { volume: true, oi: true, rvol: true, structure: true }],
];

export default function App() {
  const [tf, setTf] = useState<TF>("5m");
  const [candles, setCandles] = useState<Candle[]>([]);
  const [analysis, setAnalysis] = useState<Analysis | null>(null);
  const [dash, setDash] = useState<Dashboard | null>(null);
  const [quote, setQuote] = useState<Quote | null>(null);
  const [layers, setLayers] = useState<Layers>(() => {
    try { const s = localStorage.getItem("ict_layers"); if (s) return { ...OFF, ...JSON.parse(s) }; } catch { /* private mode */ }
    return { ...OFF, ob: true, fvg: true, liq: true, structure: true, range: true, session: true, rblock: true };
  });
  useEffect(() => { try { localStorage.setItem("ict_layers", JSON.stringify(layers)); } catch { /* ignore */ } }, [layers]);
  const [full, setFull] = useState(false);
  const defChart = Math.max(420, Math.round(window.innerHeight * 0.62));
  const [chartH0, setChartH0] = useStoredHeight("ict_h_chart", defChart);
  const [oiH0, setOiH0] = useStoredHeight("ict_h_oi", 460);
  const chartH = chartH0 > 0 ? chartH0 : defChart, oiH = oiH0 > 0 ? oiH0 : 460;
  const setChartH = (v: number) => setChartH0(v < 0 ? defChart : v), setOiH = (v: number) => setOiH0(v < 0 ? 460 : v);
  // right panel (Instrument · strikes, bias, playbook, builds, alerts) — hide to give the charts the full width
  const [side, setSide] = useState<boolean>(() => { try { return localStorage.getItem("ict_side") !== "0"; } catch { return true; } });
  useEffect(() => { try { localStorage.setItem("ict_side", side ? "1" : "0"); } catch { /* ignore */ } }, [side]);
  const chartCard = useRef<HTMLDivElement>(null);
  // Full view = real browser full screen of the chart card (hides tabs/address bar); falls back to a page overlay.
  const toggleFull = useCallback(() => {
    const el = chartCard.current;
    if (document.fullscreenElement) { document.exitFullscreen().catch(() => {}); return; }
    if (full) { setFull(false); return; }
    if (el?.requestFullscreen) el.requestFullscreen().then(() => setFull(true)).catch(() => setFull(true));
    else setFull(true);
  }, [full]);
  useEffect(() => {
    const k = (e: KeyboardEvent) => { if (e.key === "Escape" && !document.fullscreenElement) setFull(false); };
    const fs = () => { if (!document.fullscreenElement) setFull(false); };
    window.addEventListener("keydown", k); document.addEventListener("fullscreenchange", fs);
    return () => { window.removeEventListener("keydown", k); document.removeEventListener("fullscreenchange", fs); };
  }, []);
  const [bottom, setBottom] = useState<"paper" | "delta" | "footprint" | "auction" | "qml" | "crt" | "bias" | "book" | "auto" | "wmap" | "stdv" | "alerts">("paper");
  const [selectedStdvLeg, setSelectedStdvLeg] = useState<StdvLeg | null>(null);
  const [isStdvPicking, setIsStdvPicking] = useState(false);
  const [mainView, setMainView] = useState<"candles" | "writers" | "both">(() => { try { return (localStorage.getItem("ict_mainview") as "candles" | "writers" | "both") || "candles"; } catch { return "candles"; } });
  const switchMain = (v: "candles" | "writers" | "both") => { setMainView(v); try { localStorage.setItem("ict_mainview", v); } catch { /* */ } };
  const [chainTick, setChainTick] = useState(0);
  const [storyTick, setStoryTick] = useState(0);
  const [strikes, setStrikes] = useState<Strikes | null>(null);
  const [chartInst, setChartInst] = useState("NIFTY");
  const instRef = useRef(chartInst);
  instRef.current = chartInst;
  const [paper, setPaper] = useState<{ trades: PaperTrade[]; stats: PaperStats; scorecard?: Scorecard } | null>(null);
  const tfRef = useRef(tf);
  tfRef.current = tf;
  const lastChain = useRef(0);
  const ra = useRvolAlerts();
  const ap = useAutopilot();
  const [sig, setSig] = useSigPrefs();
  const fireRef = useRef(ra.fire);
  fireRef.current = ra.fire;

  const loadChart = useCallback(async (t: TF) => {
    const inst = instRef.current;
    const tt: TF = inst !== "NIFTY" && t === "1D" ? "5m" : t;
    const [c, a] = await Promise.all([api.candles(tt, inst !== "NIFTY" ? 2 : DAYS[tt], inst), tt === "1D" ? Promise.resolve(null) : api.analysis(tt, inst)]);
    if (tfRef.current !== t || instRef.current !== inst) return;
    setCandles(c.candles);
    setAnalysis(a);
  }, []);
  const loadDash = useCallback(() => api.dashboard().then((d) => {
    setDash(d);
    if (d.quote) {
      setQuote((prev) => (!prev || !prev.ts || (d.quote.ts && d.quote.ts >= prev.ts) ? d.quote : prev));
    }
  }).catch(() => {}), []);
  const loadPaper = useCallback(() => api.paper().then(setPaper).catch(() => {}), []);

  useEffect(() => { loadChart(tf).catch(() => {}); }, [tf, chartInst, loadChart]);
  useEffect(() => { if (dash?.strikes) setStrikes(dash.strikes); }, [dash]);
  useEffect(() => { loadDash(); loadPaper(); const id = window.setInterval(loadDash, 15000); return () => window.clearInterval(id); }, [loadDash, loadPaper]);

  // When tab is restored from minimized or switched back into focus, immediately catch up on candles, dashboard, and paper
  useEffect(() => {
    const onWake = () => {
      if (document.visibilityState === "visible") {
        loadChart(tfRef.current).catch(() => {});
        loadDash();
        loadPaper();
      }
    };
    document.addEventListener("visibilitychange", onWake);
    window.addEventListener("focus", onWake);
    return () => {
      document.removeEventListener("visibilitychange", onWake);
      window.removeEventListener("focus", onWake);
    };
  }, [loadChart, loadDash, loadPaper]);

  const live = useLive((m: LiveMsg) => {
    if (m.type === "quote") setQuote(m.quote);
    else if (m.type === "analysis") {
      if (m.tf === tfRef.current) loadChart(m.tf).catch(() => {});
      if (m.tf === "5m") { loadDash(); setStoryTick((x) => x + 1); }
    } else if (m.type === "chain") {
      if (Date.now() - lastChain.current > 5000) { lastChain.current = Date.now(); setChainTick((x) => x + 1); }
    } else if (m.type === "alert" || m.type === "paper") {
      if (m.type === "alert" && m.alert?.grade === "rvol") fireRef.current(m.alert);
      loadDash(); loadPaper();
    }
    else if (m.type === "strikes") setStrikes(m.strikes);
  });

  const q = quote ?? dash?.quote ?? null;
  // autopilot: evaluate TP/SL/EOD on each candle update (NIFTY only)
  useEffect(() => { if (chartInst === "NIFTY") ap.onCandles(candles, q?.spot ?? null); }, [candles, chartInst, q?.spot, ap]);
  return (
    <div className="min-h-full flex flex-col">
      <Header q={q} live={live} status={dash?.status ?? null} />
      <AlertToasts toasts={ra.toasts} dismiss={ra.dismiss} />
      {(() => {
        const rs = analysis?.volume?.rvol_state;
        const C: Record<string, string> = { ACTIVE: "bg-bull-900/70 border-bull-400 text-bull-400", ABSENT: "bg-ink-850 border-slate-600 text-slate-400",
          NORMAL: "bg-ink-850 border-ink-700 text-slate-300", UNKNOWN: "bg-ink-850 border-ink-700 text-slate-500" };
        return (
          <div className={`mx-3 mt-3 rounded-lg border px-3 py-1.5 text-[12px] flex flex-wrap items-center gap-x-3 gap-y-1 ${C[rs?.state ?? "UNKNOWN"]}`}>
            <b className="tracking-wide">{rs?.state === "ACTIVE" ? "⚡ RVOL ACTIVE" : rs?.state === "ABSENT" ? "RVOL LOW" : rs?.state === "NORMAL" ? "RVOL NORMAL" : "RVOL"}</b>
            {rs?.value != null && <span className="num text-base font-semibold">{rs.value.toFixed(2)}×</span>}
            {rs?.trend && <span>{rs.trend === "BUILDING" ? "▲ building" : rs.trend === "FADING" ? "▼ fading" : "■ holding"}</span>}
            <span className="text-slate-400">{rs?.text ?? "cumulative RVOL of the NIFTY future — loading"}</span>
            <span className="ml-auto flex flex-wrap items-center gap-2"><CombinedRvolChip /><BookChip /><SignalControls p={sig} setP={setSig} /><AlertControls prefs={ra.prefs} setPrefs={ra.setPrefs} enable={ra.enable} /></span>
          </div>
        );
      })()}
      {!side && (
        <button onClick={() => setSide(true)} title="Show the right panel (instrument, strikes, bias, playbook, alerts)"
          className="fixed right-0 top-1/3 z-40 rounded-l-lg border border-r-0 border-gold-500/70 bg-ink-900/95 px-1.5 py-3 text-[11px] font-semibold text-gold-400 shadow-lg [writing-mode:vertical-rl] hover:bg-ink-850">
          ◀ Show panel
        </button>
      )}
      <main className={`flex-1 grid grid-cols-1 ${side ? "xl:grid-cols-[minmax(0,1fr)_380px]" : ""} gap-3 p-3`}>
        <section className="flex flex-col gap-3 min-w-0">
          <div ref={chartCard} className={full ? "card fixed inset-0 z-50 flex flex-col rounded-none !bg-[#0b1220]" : "card flex flex-col"}
            style={full ? undefined : { height: chartH }}>
            <div className="card-h flex-wrap">
              <div className="flex gap-1">
                <button onClick={() => switchMain("candles")} className={`tab ${mainView === "candles" ? "tab-on" : ""}`}>📈 Candles</button>
                <button onClick={() => switchMain("writers")} title="OI change · call vs put writers + hairy spikes (unusual writer flow ≥ 2.5× normal)"
                  className={`tab ${mainView === "writers" ? "tab-on" : ""} !border-emerald-500/50 !text-emerald-300`}>🪝 OI · Writers</button>
                <button onClick={() => switchMain("both")} title="Candles on top + OI-change / writer-flow (hairy spikes) below"
                  className={`tab ${mainView === "both" ? "tab-on" : ""} !border-cyan-500/50 !text-cyan-300`}>📊 Both</button>
                <span className="mx-1 w-px self-stretch bg-ink-700" />
                {mainView !== "writers" && TFS.map((t) => <button key={t} onClick={() => setTf(t)} className={`tab ${t === tf ? "tab-on" : ""}`}>{t}</button>)}
                <button onClick={toggleFull} title={full ? "Back to normal view (Esc)" : "Chart full screen (Esc to exit)"}
                  className="tab ml-2 !border-gold-500/70 !text-gold-400 font-semibold">{full ? "▣ Normal view" : "⛶ Full view"}</button>
                <button
                  onClick={() => {
                    setIsStdvPicking((p) => {
                      const next = !p;
                      if (next) setLayers((l) => ({ ...l, stdv: true }));
                      return next;
                    });
                  }}
                  title="Interactive 2-Click STDV Tool: Click any 2 candle wicks on chart to anchor custom STDV manipulation leg"
                  className={`tab font-bold flex items-center gap-1.5 transition-all ${
                    isStdvPicking
                      ? "!bg-red-600 !text-white animate-pulse shadow-lg ring-2 ring-red-400"
                      : selectedStdvLeg
                      ? "!border-amber-500/80 !text-amber-300 hover:!bg-amber-950/40"
                      : "!border-red-500/80 !text-red-400 hover:!bg-red-950/40"
                  }`}
                >
                  <span className="text-[13px]">{isStdvPicking ? "✕" : "📍"}</span>
                  <span>{isStdvPicking ? "Cancel Picking (Esc)" : selectedStdvLeg ? "Re-Pick 2-Click STDV" : "2-Click STDV Tool"}</span>
                </button>
                {selectedStdvLeg ? (
                  <button
                    onClick={() => setSelectedStdvLeg(null)}
                    title="Switch back to system auto marking: clears manual 2-click anchors and lets the system mark the ICT leg automatically"
                    className="tab font-bold flex items-center gap-1.5 !border-cyan-400 !bg-cyan-500/20 !text-cyan-200 hover:!bg-cyan-500/30 shadow-md"
                  >
                    <span>🤖</span>
                    <span>Use System Auto Marking</span>
                  </button>
                ) : (
                  <button
                    onClick={() => setLayers((l) => ({ ...l, stdv: !l.stdv }))}
                    title="System Auto STDV is active — click to toggle layer on/off"
                    className={`tab font-semibold flex items-center gap-1.5 ${layers.stdv ? "!border-cyan-500/60 !text-cyan-300 !bg-cyan-950/30" : "text-slate-400"}`}
                  >
                    <span>🤖</span>
                    <span>System STDV: {layers.stdv ? "Active" : "Off"}</span>
                  </button>
                )}
                <button
                  onClick={() => {
                    setBottom("stdv");
                    setLayers((l) => ({ ...l, stdv: true }));
                  }}
                  title="Open the STD DEV cockpit panel below"
                  className={`tab !border-gold-500/70 !text-gold-300 font-semibold ${bottom === "stdv" ? "tab-on !bg-gold-500/20" : ""}`}
                >
                  📐 STD DEV Cockpit
                </button>
              </div>
              {mainView !== "writers" && <>
              <div className="flex flex-wrap items-center gap-1">
                <span className="mr-0.5 text-[10px] uppercase tracking-wider text-slate-500">View</span>
                {PRESETS.map(([name, hint, l]) => {
                  const on = (Object.keys(OFF) as (keyof Layers)[]).every((k) => !!l[k] === layers[k]);
                  return <button key={name} title={hint} onClick={() => { setLayers({ ...OFF, ...l }); if (name === "Order flow" || name === "Order flow PRO") setBottom("footprint"); if (name === "CRT") setBottom("crt"); }}
                    className={`tab ${on ? "tab-on" : ""} !border-gold-500/40`}>{name}</button>;
                })}
              </div>
              <div className="flex flex-wrap gap-1">
                {LAYER_NAMES.map(([k, label]) => (
                  <button key={k} onClick={() => setLayers((l) => ({ ...l, [k]: !l[k] }))}
                    className={`tab ${layers[k] ? "tab-on" : ""}`}>{label}</button>
                ))}
              </div>
              </>}
            </div>
            <div className="flex-1 min-h-0">
              {mainView === "writers"
                ? <WritersFlow tick={chainTick} />
                : mainView === "both"
                ? <SplitPane
                    top={<Chart tf={tf} candles={candles} analysis={analysis} layers={layers} spot={chartInst === "NIFTY" ? (q?.spot ?? null) : null} spotTs={q?.ts ?? 0} marketOpen={chartInst === "NIFTY" && !!q?.market_open} onExpand={toggleFull} expanded={full} sig={chartInst === "NIFTY" ? sig : undefined} onSignal={ra.fire} onAutoSignal={chartInst === "NIFTY" ? ap.onSignal : undefined} expiries={dash?.expiries} stdvLeg={selectedStdvLeg} isStdvPicking={isStdvPicking} onStartStdvPicking={() => { setIsStdvPicking(true); setLayers((l) => ({ ...l, stdv: true })); }} onStdvPicked={(leg) => { setSelectedStdvLeg(leg); setIsStdvPicking(false); setLayers((l) => ({ ...l, stdv: true })); }} onCancelStdvPicking={() => setIsStdvPicking(false)} onResetToAutoStdv={() => setSelectedStdvLeg(null)} />}
                    bottom={<WritersFlow tick={chainTick} />} />
                : <Chart tf={tf} candles={candles} analysis={analysis} layers={layers} spot={chartInst === "NIFTY" ? (q?.spot ?? null) : null} spotTs={q?.ts ?? 0} marketOpen={chartInst === "NIFTY" && !!q?.market_open} onExpand={toggleFull} expanded={full} sig={chartInst === "NIFTY" ? sig : undefined} onSignal={ra.fire} onAutoSignal={chartInst === "NIFTY" ? ap.onSignal : undefined} expiries={dash?.expiries} stdvLeg={selectedStdvLeg} isStdvPicking={isStdvPicking} onStartStdvPicking={() => { setIsStdvPicking(true); setLayers((l) => ({ ...l, stdv: true })); }} onStdvPicked={(leg) => { setSelectedStdvLeg(leg); setIsStdvPicking(false); setLayers((l) => ({ ...l, stdv: true })); }} onCancelStdvPicking={() => setIsStdvPicking(false)} onResetToAutoStdv={() => setSelectedStdvLeg(null)} />}
            </div>
            {mainView === "candles" && <div className="px-4 py-1.5 text-[11px] text-slate-400 border-t border-ink-700 flex flex-wrap gap-x-4">
              {analysis && <span>ATR {analysis.atr} pts · trend <b className={analysis.trend === "bull" ? "text-bull-400" : analysis.trend === "bear" ? "text-bear-400" : ""}>{analysis.trend}</b></span>}
              {analysis?.range && <span>Range {analysis.range.low.toFixed(1)}–{analysis.range.high.toFixed(1)} · EQ {analysis.range.eq.toFixed(1)} · price in <b className={analysis.range.zone === "premium" ? "text-bear-400" : "text-bull-400"}>{analysis.range.zone}</b> ({analysis.range.pos_pct} %)</span>}
              {analysis?.experimental && <span className="text-amber-300 font-semibold">{analysis.instrument?.label} · {analysis.experimental}</span>}
              {analysis?.proxy?.enabled && <span className="text-amber-300">{analysis.proxy.label}: {analysis.proxy.available ? "on" : analysis.proxy.message}</span>}
              {analysis?.volume && (analysis.volume.available
                ? <span className="text-slate-300">Volume: NIFTY FUT {analysis.volume.contract?.split(" (")[0]} · basis {analysis.volume.basis?.toFixed(1)}{analysis.volume.last_vwap ? ` · VWAP ${analysis.volume.last_vwap.v.toFixed(1)}` : ""}{analysis.volume.last?.last_rvol != null ? ` · last bar ${analysis.volume.last.last_rvol.toFixed(1)}× vol` : ""}{analysis.volume.oi_last?.build && analysis.volume.oi_last.build !== "flat" ? ` · FUT OI: ${analysis.volume.oi_last.build}` : ""}{analysis.volume.iv_trend ? ` · ATM IV ${analysis.volume.iv_trend.to.toFixed(1)} (${analysis.volume.iv_trend.word})` : ""}{analysis.volume.avwaps?.length ? ` · AVWAP: ${analysis.volume.avwaps.map((x, i) => `${i ? "amber" : "violet"} = ${x.label.replace("AVWAP · ", "")}`).join(", ")}` : ""}</span>
                : <span className="text-slate-500">Volume: {analysis.volume.message}</span>)}
              {analysis?.delta && <span className={analysis.delta.mock ? "text-amber-300" : analysis.delta.available ? "text-slate-300" : "text-slate-500"}>
                Delta: {analysis.delta.available ? `${analysis.delta.adapter}${analysis.delta.mock ? " (MOCK)" : ""}` : "unavailable"}</span>}
              <span className="text-slate-500">Overlays use CLOSED candles only — the forming candle never paints a signal.</span>
            </div>}
          </div>
          <DragBar h={chartH} setH={setChartH} min={320} label="main chart" />
          {/* PE − CE OI table + call vs put writers flow, right under the main chart (each has its own Full view) */}
          <div className="grid grid-cols-1 lg:grid-cols-[3fr_2fr] gap-3">
            <FullPanel title="PE − CE OI table" hint="same as Options Lab · 🎯 levels · ★ pro read" h={oiH}>
              <PeceTable tick={chainTick} />
            </FullPanel>
            <FullPanel title="OI change · call vs put writers" hint="since yesterday · writer-flow strip · 30-min balance" h={oiH}>
              <WritersFlow tick={chainTick} />
            </FullPanel>
          </div>
          <DragBar h={oiH} setH={setOiH} min={240} label="PE − CE table and writers chart" />
          <div className="card flex flex-col h-[560px]">
            <div className="card-h">
              <div className="flex gap-1">
                <button className={`tab ${bottom === "paper" ? "tab-on" : ""}`} onClick={() => setBottom("paper")}>Paper trades</button>
                <button className={`tab ${bottom === "alerts" ? "tab-on" : ""} !border-gold-500/60 !text-gold-300 font-semibold`} onClick={() => setBottom("alerts")}>🔔 Alerts log</button>
                <button className={`tab ${bottom === "delta" ? "tab-on" : ""}`} onClick={() => setBottom("delta")}>Delta / Footprint</button>
                <button className={`tab ${bottom === "footprint" ? "tab-on" : ""} !border-cyan-500/50`} onClick={() => setBottom("footprint")}>📊 Footprint desk</button>
                <button className={`tab ${bottom === "stdv" ? "tab-on" : ""} !border-gold-500/60 !text-gold-300 font-semibold`} onClick={() => setBottom("stdv")}>📐 STD DEV</button>
                <button className={`tab ${bottom === "auction" ? "tab-on" : ""}`} onClick={() => setBottom("auction")}>Auction</button>
                <button className={`tab ${bottom === "qml" ? "tab-on" : ""}`} onClick={() => setBottom("qml")}>QML · RBS / SBR</button>
                <button className={`tab ${bottom === "crt" ? "tab-on" : ""}`} onClick={() => setBottom("crt")}>CRT cockpit</button>
                <button className={`tab ${bottom === "bias" ? "tab-on" : ""} !border-gold-500/60 !text-gold-400`} onClick={() => setBottom("bias")}>⚖ Bias meter 19</button>
                <button className={`tab ${bottom === "book" ? "tab-on" : ""} !border-emerald-500/60 !text-emerald-300`} onClick={() => setBottom("book")}>📗 Order book</button>
                <button className={`tab ${bottom === "auto" ? "tab-on" : ""} !border-cyan-500/60 !text-cyan-300`} onClick={() => setBottom("auto")}>🤖 Autopilot{ap.s.cfg.on ? " ●" : ""}</button>
                <button className={`tab ${bottom === "wmap" ? "tab-on" : ""} !border-gold-500/60 !text-gold-400`} onClick={() => setBottom("wmap")}>🎯 Writer map</button>
              </div>
            </div>
            <div className="flex-1 min-h-0">
              {bottom === "alerts" ? <AlertsTable alerts={dash?.alerts} onRefresh={loadDash} />
                : bottom === "stdv" ? <StdvPanel candles={candles} spot={chartInst === "NIFTY" ? (q?.spot ?? null) : null} chartTf={tf} selectedLeg={selectedStdvLeg} onSelectLeg={setSelectedStdvLeg} isLayerActive={!!layers.stdv} onToggleLayer={() => setLayers((l) => ({ ...l, stdv: !l.stdv }))} isPicking={isStdvPicking} onStartPicking={() => { setIsStdvPicking(true); setLayers((l) => ({ ...l, stdv: true })); }} onCancelPicking={() => setIsStdvPicking(false)} onSetChartTf={setTf} />
                : bottom === "wmap" ? <WriterMap />
                : bottom === "auto" ? <AutoPilotPanel ap={ap} spot={chartInst === "NIFTY" ? (q?.spot ?? null) : null} />
                : bottom === "book" ? <BookPanel />
                : bottom === "bias" ? <BiasPanel tf={tf} candles={candles} analysis={analysis} />
                : bottom === "crt" ? <CrtCockpit tick={storyTick} />
                : bottom === "qml" ? <QmlPanel tick={storyTick} />
                : bottom === "footprint" ? <FootprintDesk tick={storyTick} />
                : bottom === "delta" ? <DeltaPanel tick={storyTick} strikes={strikes} />
                : bottom === "auction" ? <AuctionPanel tick={storyTick} />
                : paper ? <Paper trades={paper.trades} stats={paper.stats} scorecard={paper.scorecard} /> : null}
            </div>
          </div>
        </section>
        <aside className={side ? "flex flex-col gap-3 min-w-0" : "hidden"}>
          <button onClick={() => setSide(false)} title="Hide this panel — the charts and tables take the full width"
            className="tab self-end !border-gold-500/70 !text-gold-400 font-semibold">Hide panel ▶</button>
          <div className="card">
            <div className="card-h"><span className="card-t">Instrument · strikes</span>
              <span className="text-[10px] text-slate-500">{strikes?.mode === "OPTION_CHART" ? "option chart (experimental)" : "structure on NIFTY"}</span></div>
            <InstrumentPanel s={strikes} chartInst={chartInst} setChartInst={setChartInst} onChange={setStrikes} />
          </div>
          <div className="card">
            <div className="card-h"><span className="card-t">Bias by timeframe</span></div>
            <div className="p-3">{dash && <BiasGrid bias={dash.bias} />}</div>
          </div>
          <div className="card">
            <div className="card-h"><span className="card-t">Playbook · today (5m, 15m bias)</span></div>
            <div className="p-3 max-h-[46vh] overflow-auto">{dash && <Signals list={dash.signals} />}</div>
          </div>
          <div className="card">
            <div className="card-h"><span className="card-t">Top OI builds / unwinds</span>
              <span className="text-[10px] text-slate-500">{dash?.chain?.expiry}</span></div>
            <div className="px-3 py-2">{dash?.chain && <Builds list={dash.chain.builds} />}</div>
          </div>
          <div className="card">
            <div className="card-h flex items-center justify-between">
              <span className="card-t">Alerts</span>
              <button onClick={() => setBottom("alerts")} className="text-[11px] text-gold-400 hover:text-gold-300 underline font-normal cursor-pointer">Table ↗</button>
            </div>
            <div className="px-3 py-1 max-h-[32vh] overflow-auto">{dash && <Alerts list={dash.alerts} />}</div>
          </div>
          {(dash?.paper_open_all ?? (dash?.paper_open ? [dash.paper_open] : [])).length > 0 && (
            <div className="card border-gold-500/60">
              <div className="card-h"><span className="card-t">Open paper trades</span></div>
              <div className="px-3 py-2 text-[12px] num space-y-1">
                {(dash?.paper_open_all ?? [dash!.paper_open!]).map((t) => (
                  <div key={t.id}>
                    <span className="text-slate-500">{t.slot ?? t.setup}</span> · {t.side.toUpperCase()} @ {t.entry} · SL {t.sl}
                    {t.tp1 != null ? ` · TP-1 ${t.tp1}${t.half_done ? " ✓ (BE)" : ""}` : ""} · TP {t.tp} · MFE {t.mfe_r}R
                  </div>
                ))}
              </div>
            </div>
          )}
        </aside>
      </main>
    </div>
  );
}
