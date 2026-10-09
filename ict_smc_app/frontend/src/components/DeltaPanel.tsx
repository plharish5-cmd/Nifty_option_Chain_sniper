// DeltaPanel.tsx — Delta / Footprint tab: source status, CSV import, delta bars, and the event inspector
// (one timeline of BOS / MSS / sweeps + delta events, each with its zone record and plain-English reason).
import { useEffect, useMemo, useState } from "react";
import { api, fmt, istHM, type DeltaRes, type EventRec, type Strikes, type TF } from "../api";

const TYPE_TXT: Record<string, string> = {
  DELTA_AGGRESSION: "Aggression", DELTA_ABSORPTION: "Absorption", DELTA_FLIP: "Delta flip",
  FOOTPRINT_IMBALANCE: "Imbalance", BOS: "BOS", MSS: "MSS", SWEEP: "Sweep", FALSE_BREAKOUT: "False breakout",
  PROXY_FLOW_SURGE: "Flow surge · PROXY (not real delta)", PROXY_FLOW_FADE: "Flow fade · PROXY (not real delta)",
};
const isDelta = (t: string) => t.startsWith("DELTA_") || t === "FOOTPRINT_IMBALANCE" || t.startsWith("PROXY_FLOW");

function Bars({ d }: { d: DeltaRes }) {
  const bars = d.bars.slice(-90);
  const max = Math.max(1, ...bars.map((b) => Math.abs(b.delta ?? 0)));
  const evAt = new Set(d.events.filter((e) => e.event_type === "DELTA_ABSORPTION").map((e) => e.ts));
  const W = 900, H = 120, bw = W / Math.max(bars.length, 1);
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full h-[120px]" preserveAspectRatio="none" role="img" aria-label="Delta per closed candle">
      <line x1={0} x2={W} y1={H / 2} y2={H / 2} stroke="rgba(148,163,184,0.3)" />
      {bars.map((b, i) => {
        if (b.delta == null) return <rect key={i} x={i * bw + 1} y={H / 2 - 1} width={Math.max(bw - 2, 1)} height={2} fill="rgba(148,163,184,0.25)" />;
        const h = (Math.abs(b.delta) / max) * (H / 2 - 6);
        const up = b.delta >= 0;
        const strong = (b.z ?? 0) !== 0 && Math.abs(b.z ?? 0) >= 2;
        return (
          <g key={i}>
            <rect x={i * bw + 1} y={up ? H / 2 - h : H / 2} width={Math.max(bw - 2, 1)} height={Math.max(h, 1)}
              fill={up ? (strong ? "#34d399" : "rgba(52,211,153,0.45)") : (strong ? "#f87171" : "rgba(248,113,113,0.45)")}>
              <title>{`${istHM(b.ts)} delta ${b.delta.toLocaleString("en-IN")} · z ${b.z ?? "—"}`}</title>
            </rect>
            {evAt.has(b.ts) && <circle cx={i * bw + bw / 2} cy={up ? H / 2 - h - 4 : H / 2 + h + 4} r={2.5} fill="#e8c35a" />}
          </g>
        );
      })}
    </svg>
  );
}

function Inspector({ e }: { e: EventRec | null }) {
  if (!e) return <div className="text-[12px] text-slate-500 p-3">Tap an event to see the zone it hit and why it fired.</div>;
  const z = e.zone_record;
  const row = (k: string, v: React.ReactNode) => <div className="flex justify-between gap-3"><span className="text-slate-500">{k}</span><span className="num text-right">{v}</span></div>;
  return (
    <div className="p-3 space-y-2 text-[12px]">
      <div className={`font-semibold ${e.direction === "bull" ? "text-bull-400" : "text-bear-400"}`}>
        {e.time} · {TYPE_TXT[e.event_type] ?? e.event_type} {e.direction === "bull" ? "▲" : "▼"} {e.proxy ? "(proxy)" : ""}
      </div>
      <div className="text-slate-200">{e.explanation}</div>
      <div className="space-y-0.5 text-slate-300">
        {e.delta != null && row(e.rule === "rule2_no_follow_through" ? "Entry-candle delta" : "Delta", `${e.delta >= 0 ? "+" : ""}${e.delta.toLocaleString("en-IN")}${e.z != null ? ` · z ${e.z}` : ""}`)}
        {e.confidence != null && row("Confidence", `${Math.round(e.confidence * 100)} %`)}
        {e.rule && row("Rule", e.rule.replace(/_/g, " "))}
        {row("Price", fmt(e.price, 1))}
        {e.mock && <div className="text-amber-300">MOCK data — not real order flow.</div>}
      </div>
      {z ? (
        <div className="rounded border border-ink-700 bg-ink-900 p-2 space-y-0.5">
          <div className="text-gold-400 font-semibold">Zone · {z.label}</div>
          {row("Type / side", `${z.type} · ${z.side}`)}
          {row("Range", z.price_low === z.price_high ? fmt(z.price_low, 1) : `${fmt(z.price_low, 1)} – ${fmt(z.price_high, 1)}`)}
          {row("State", z.state)}
          {row("Created", istHM(z.created_at))}
          {row("Mitigated", istHM(z.mitigated_at))}
          {row("Invalidated", istHM(z.invalidated_at))}
          {z.source_event_id && row("Source event", z.source_event_id.split("|").slice(0, 3).join(" "))}
          <div className="text-[10px] text-slate-500 break-all">{z.id}</div>
        </div>
      ) : e.zone_id ? null : <div className="text-slate-500">Not at a registry zone.</div>}
    </div>
  );
}

export default function DeltaPanel({ tick, strikes }: { tick: number; strikes?: Strikes | null }) {
  const [tf, setTf] = useState<TF>("5m");
  const [d, setD] = useState<DeltaRes | null>(null);
  const [ev, setEv] = useState<EventRec[]>([]);
  const [sel, setSel] = useState<EventRec | null>(null);
  const [only, setOnly] = useState<"all" | "delta">("all");
  const [msg, setMsg] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [secs, setSecs] = useState(60);
  const [tsIs, setTsIs] = useState("end");
  const [csvInst, setCsvInst] = useState("NIFTY");
  const [proxyOn, setProxyOn] = useState<boolean>(!!strikes?.proxy_on);
  useEffect(() => { setProxyOn(!!strikes?.proxy_on); }, [strikes?.proxy_on]);
  const [mis, setMis] = useState("floor");

  const load = () => {
    api.delta(tf).then(setD).catch(() => {});
    api.events(tf).then((r) => setEv(r.events)).catch(() => {});
  };
  useEffect(load, [tf, tick]);   // eslint-disable-line react-hooks/exhaustive-deps

  const list = useMemo(() => [...ev].reverse().filter((e) => only === "all" || isDelta(e.event_type)).slice(0, 120), [ev, only]);
  const st = d?.status;

  const choose = async (name: string) => {
    setBusy(true); setMsg("");
    try { await api.setAdapter(name); load(); } catch (e) { setMsg(String((e as Error).message)); }
    setBusy(false);
  };
  const upload = async () => {
    if (!file) return;
    setBusy(true); setMsg("");
    try {
      const r = await api.uploadCsv(file, secs, tsIs, mis, csvInst);
      const p = r.report;
      setMsg(`Imported ${p.accepted}/${p.rows} rows (${p.kind}) for ${p.instrument} · floored ${p.floored} · outside session ${p.outside_session} · misaligned ${p.misaligned} · duplicates ${p.duplicates}${p.errors.length ? ` · ${p.errors.length} bad rows: ${p.errors[0]}` : ""}`);
      load();
    } catch (e) { setMsg(`Upload failed: ${(e as Error).message}`); }
    setBusy(false);
  };

  return (
    <div className="h-full overflow-auto p-3 space-y-3">
      <div className="flex flex-wrap items-center gap-2 text-[12px]">
        <span className="text-slate-400">Source</span>
        {(st?.adapters ?? ["none", "csv", "footprint_feed", "mock"]).map((n) => (
          <button key={n} disabled={busy} onClick={() => choose(n)} className={`tab ${st?.name === n ? "tab-on" : ""}`}>{st?.labels?.[n] ?? n}</button>
        ))}
        <label className="ml-2 flex items-center gap-1 text-amber-300">
          <input type="checkbox" checked={proxyOn} onChange={async (e) => { const on = e.target.checked; setProxyOn(on); await api.post("/api/proxy", { enabled: on }).catch(() => {}); load(); }} />
          Options Flow Proxy — PROXY (not real delta)
        </label>
        <span className="ml-2 text-slate-400">TF</span>
        {(["1m", "5m", "15m", "1h"] as TF[]).map((t) => <button key={t} onClick={() => setTf(t)} className={`tab ${t === tf ? "tab-on" : ""}`}>{t}</button>)}
        {d?.has_levels && <span className="pill bg-ink-800 text-sky-300">footprint levels</span>}
      </div>
      {st?.mock && <div className="rounded border border-amber-400/60 bg-amber-400/10 px-3 py-1.5 text-[12px] text-amber-200">MOCK delta — deterministic fake numbers to test the screen. Never trade from it.</div>}
      {st?.name === "fut_quote_rule" && <div className="rounded border border-sky-400/50 bg-sky-400/10 px-3 py-1.5 text-[12px] text-sky-200">ESTIMATED delta — each print of the NIFTY future is classed buy / sell by where it traded against the previous bid / ask (tick rule in between). Dhan sends snapshots, not every trade, so read it as a good estimate, not exchange-tagged truth. Recorded live only: no history before the desk first ran in market hours.</div>}
      {!(st?.mock && d?.available) && <div className={`text-[12px] ${d?.available ? "text-slate-300" : "text-amber-300"}`}>
        {d?.available ? (st?.message ?? "") : `Delta unavailable — ${d?.message || st?.message || "no source"}`}
      </div>}

      {strikes && (strikes.current.CE || strikes.current.PE) && (
        <div className="text-[11px] text-slate-400">Strike flow: {(["CE", "PE"] as const).map((sd) => strikes.current[sd]?.instrument.label).filter(Boolean).join(" · ")} — read on the strike's candles, scored only while NIFTY is at an index zone.</div>
      )}
      {st?.name === "csv" && (
        <div className="rounded-lg border border-ink-700 bg-ink-850 p-2 text-[12px] space-y-1.5">
          <div className="text-slate-300">Upload delta CSV. Bars: <code>timestamp,delta</code> or <code>timestamp,buy_vol,sell_vol</code>. Footprint: <code>timestamp,price,buy_vol,sell_vol</code>. Naive times are IST.</div>
          <div className="flex flex-wrap items-center gap-2">
            <input type="file" accept=".csv,text/csv" onChange={(e) => setFile(e.target.files?.[0] ?? null)} className="text-[11px]" />
            <label>for <select value={csvInst} onChange={(e) => setCsvInst(e.target.value)} className="bg-ink-900 border border-ink-700 rounded px-1">
              <option value="NIFTY">NIFTY index</option>
              {(["CE", "PE"] as const).map((sd) => strikes?.current[sd] && <option key={sd} value={strikes.current[sd]!.instrument.key}>{strikes.current[sd]!.instrument.label}</option>)}
            </select></label>
            <label>bar <select value={secs} onChange={(e) => setSecs(+e.target.value)} className="bg-ink-900 border border-ink-700 rounded px-1">{[60, 300, 900, 3600].map((s) => <option key={s} value={s}>{s / 60}m</option>)}</select></label>
            <label>time is <select value={tsIs} onChange={(e) => setTsIs(e.target.value)} className="bg-ink-900 border border-ink-700 rounded px-1"><option value="end">candle close (default)</option><option value="start">candle start</option></select></label>
            <label>off-grid <select value={mis} onChange={(e) => setMis(e.target.value)} className="bg-ink-900 border border-ink-700 rounded px-1"><option value="floor">floor</option><option value="reject">reject</option></select></label>
            <button disabled={!file || busy} onClick={upload} className="tab tab-on">Upload</button>
          </div>
        </div>
      )}
      {msg && <div className="text-[12px] text-slate-300">{msg}</div>}

      {d?.available && (
        <div className="rounded-lg border border-ink-700 bg-ink-850 p-2">
          <div className="text-[11px] text-slate-400 mb-1">Delta per CLOSED {tf} candle (bright = |z| ≥ 2 vs its baseline · gold dot = absorption · grey = no data)</div>
          <Bars d={d} />
        </div>
      )}

      <div className="grid grid-cols-1 md:grid-cols-[minmax(0,1fr)_320px] gap-3">
        <div className="rounded-lg border border-ink-700">
          <div className="flex items-center gap-2 px-3 py-1.5 border-b border-ink-700 text-[11px]">
            <span className="text-slate-400 uppercase tracking-wide">Events</span>
            <button onClick={() => setOnly("all")} className={`tab ${only === "all" ? "tab-on" : ""}`}>all</button>
            <button onClick={() => setOnly("delta")} className={`tab ${only === "delta" ? "tab-on" : ""}`}>delta only</button>
          </div>
          <ul className="max-h-[300px] overflow-auto divide-y divide-ink-800 text-[12px]">
            {list.length === 0 && <li className="px-3 py-2 text-slate-500">No events.</li>}
            {list.map((e) => (
              <li key={e.id}>
                <button onClick={() => setSel(e)} className={`w-full text-left px-3 py-1.5 hover:bg-ink-800 ${sel?.id === e.id ? "bg-ink-800" : ""}`}>
                  <span className="num text-slate-500 mr-2">{e.time}</span>
                  <span className={e.direction === "bull" ? "text-bull-400" : "text-bear-400"}>{e.direction === "bull" ? "▲" : "▼"}</span>
                  <span className={`ml-1 ${isDelta(e.event_type) ? "text-gold-400" : "text-slate-200"}`}>{TYPE_TXT[e.event_type] ?? e.event_type}</span>
                  {e.zone && <span className="text-slate-400"> · {e.zone}</span>}
                  {e.confidence != null && <span className="text-slate-500"> · {Math.round(e.confidence * 100)} %</span>}
                </button>
              </li>
            ))}
          </ul>
        </div>
        <div className="rounded-lg border border-ink-700 bg-ink-850"><Inspector e={sel} /></div>
      </div>
    </div>
  );
}
