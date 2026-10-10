// App.tsx — layout + data flow. REST for the initial state (so a refresh restores everything from SQLite),
// WebSocket for changes. Candles/analysis are re-fetched whenever the backend says a candle CLOSED.
import { useCallback, useEffect, useRef, useState } from "react";
import { api, type Analysis, type Candle, type Dashboard, type LiveMsg, type PaperStats, type PaperTrade, type Quote, type Scorecard, type Strikes, type TF, useLive } from "./api";
import InstrumentPanel from "./components/InstrumentPanel";
import Chart, { type Layers } from "./components/Chart";
import OptionChain from "./components/OptionChain";
import { Alerts, BiasGrid, Builds, Header, Paper, Signals } from "./components/Panels";
import Story from "./components/Story";
import Guide from "./components/Guide";
import DeltaPanel from "./components/DeltaPanel";
import AuctionPanel from "./components/AuctionPanel";

const TFS: TF[] = ["1m", "5m", "15m", "1h", "1D"];
const DAYS: Record<TF, number> = { "1m": 1, "5m": 3, "15m": 8, "1h": 30, "1D": 60 };
const LAYER_NAMES: [keyof Layers, string][] = [
  ["ob", "Order blocks"], ["fvg", "FVG"], ["liq", "Liquidity"], ["structure", "BOS / MSS"],
  ["swings", "Swings"], ["range", "Premium / OTE"], ["session", "PDH · PDL · OR"], ["delta", "Aggression / Absorption"], ["deltaDetail", "All delta detail"], ["crt", "CRT + TBS"], ["auction", "Auction (value · accept / reject)"], ["volume", "Volume · VWAP"],
];

export default function App() {
  const [tf, setTf] = useState<TF>("5m");
  const [candles, setCandles] = useState<Candle[]>([]);
  const [analysis, setAnalysis] = useState<Analysis | null>(null);
  const [dash, setDash] = useState<Dashboard | null>(null);
  const [quote, setQuote] = useState<Quote | null>(null);
  const [layers, setLayers] = useState<Layers>({ ob: true, fvg: true, liq: true, structure: true, swings: false, range: true, session: true, delta: true, deltaDetail: false, crt: true, auction: true, volume: true });
  const [bottom, setBottom] = useState<"chain" | "paper" | "delta" | "auction">("chain");
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

  const loadChart = useCallback(async (t: TF) => {
    const inst = instRef.current;
    const tt: TF = inst !== "NIFTY" && t === "1D" ? "5m" : t;
    const [c, a] = await Promise.all([api.candles(tt, inst !== "NIFTY" ? 2 : DAYS[tt], inst), tt === "1D" ? Promise.resolve(null) : api.analysis(tt, inst)]);
    if (tfRef.current !== t || instRef.current !== inst) return;
    setCandles(c.candles);
    setAnalysis(a);
  }, []);
  const loadDash = useCallback(() => api.dashboard().then((d) => { setDash(d); setQuote((q) => q ?? d.quote); }).catch(() => {}), []);
  const loadPaper = useCallback(() => api.paper().then(setPaper).catch(() => {}), []);

  useEffect(() => { loadChart(tf).catch(() => {}); }, [tf, chartInst, loadChart]);
  useEffect(() => { if (dash?.strikes) setStrikes(dash.strikes); }, [dash]);
  useEffect(() => { loadDash(); loadPaper(); const id = window.setInterval(loadDash, 15000); return () => window.clearInterval(id); }, [loadDash, loadPaper]);

  const live = useLive((m: LiveMsg) => {
    if (m.type === "quote") setQuote(m.quote);
    else if (m.type === "analysis") {
      if (m.tf === tfRef.current) loadChart(m.tf).catch(() => {});
      if (m.tf === "5m") { loadDash(); setStoryTick((x) => x + 1); }
    } else if (m.type === "chain") {
      if (Date.now() - lastChain.current > 5000) { lastChain.current = Date.now(); setChainTick((x) => x + 1); }
    } else if (m.type === "alert" || m.type === "paper") { loadDash(); loadPaper(); }
    else if (m.type === "strikes") setStrikes(m.strikes);
  });

  const q = quote ?? dash?.quote ?? null;
  return (
    <div className="min-h-full flex flex-col">
      <Header q={q} live={live} status={dash?.status ?? null} />
      <main className="flex-1 grid grid-cols-1 xl:grid-cols-[minmax(0,1fr)_380px] gap-3 p-3">
        <section className="flex flex-col gap-3 min-w-0">
          <div className="card flex flex-col h-[62vh] min-h-[420px]">
            <div className="card-h flex-wrap">
              <div className="flex gap-1">
                {TFS.map((t) => <button key={t} onClick={() => setTf(t)} className={`tab ${t === tf ? "tab-on" : ""}`}>{t}</button>)}
              </div>
              <div className="flex flex-wrap gap-1">
                {LAYER_NAMES.map(([k, label]) => (
                  <button key={k} onClick={() => setLayers((l) => ({ ...l, [k]: !l[k] }))}
                    className={`tab ${layers[k] ? "tab-on" : ""}`}>{label}</button>
                ))}
              </div>
            </div>
            <div className="flex-1 min-h-0">
              <Chart tf={tf} candles={candles} analysis={analysis} layers={layers} spot={chartInst === "NIFTY" ? (q?.spot ?? null) : null} spotTs={q?.ts ?? 0} marketOpen={chartInst === "NIFTY" && !!q?.market_open} />
            </div>
            <div className="px-4 py-1.5 text-[11px] text-slate-400 border-t border-ink-700 flex flex-wrap gap-x-4">
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
            </div>
          </div>
          <div className="card">
            <div className="card-h"><span className="card-t">Read of the market</span></div>
            <ul className="px-4 py-2 space-y-1 text-[12px] text-slate-300">
              {(dash?.narrative ?? []).map((n, i) => <li key={i}><span className="text-gold-400 font-semibold">{n.split(":")[0]}</span>{n.slice(n.indexOf(":"))}</li>)}
            </ul>
          </div>
          <Story dash={dash} tick={storyTick} />
          <Guide />
          <div className="card flex flex-col h-[520px]">
            <div className="card-h">
              <div className="flex gap-1">
                <button className={`tab ${bottom === "chain" ? "tab-on" : ""}`} onClick={() => setBottom("chain")}>Option chain</button>
                <button className={`tab ${bottom === "paper" ? "tab-on" : ""}`} onClick={() => setBottom("paper")}>Paper trades</button>
                <button className={`tab ${bottom === "delta" ? "tab-on" : ""}`} onClick={() => setBottom("delta")}>Delta / Footprint</button>
                <button className={`tab ${bottom === "auction" ? "tab-on" : ""}`} onClick={() => setBottom("auction")}>Auction</button>
              </div>
            </div>
            <div className="flex-1 min-h-0">
              {bottom === "chain" ? <OptionChain expiries={dash?.expiries ?? []} tick={chainTick} />
                : bottom === "delta" ? <DeltaPanel tick={storyTick} strikes={strikes} />
                : bottom === "auction" ? <AuctionPanel tick={storyTick} />
                : paper ? <Paper trades={paper.trades} stats={paper.stats} scorecard={paper.scorecard} /> : null}
            </div>
          </div>
        </section>
        <aside className="flex flex-col gap-3 min-w-0">
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
            <div className="card-h"><span className="card-t">Alerts</span></div>
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
