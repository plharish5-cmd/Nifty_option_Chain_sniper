// InstrumentPanel.tsx — strike mode: Index Structure (default) / Option Chart (experimental), expiry, Priyanka
// selector, manual override, CE/PE chips, focused strike panel and the context verdict (context only).
import { useEffect, useState } from "react";
import { api, fmt, lakh, type Chain, type Focus, type Pick, type Strikes } from "../api";

function Chip({ p, side, on, manual, onClick }: { p: Pick | null; side: "CE" | "PE"; on: boolean; manual: boolean; onClick: () => void }) {
  const col = side === "CE" ? "border-bull-400/70 text-bull-400" : "border-bear-400/70 text-bear-400";
  return (
    <button onClick={onClick} disabled={!p}
      className={`rounded-full border px-3 py-1 text-[12px] font-semibold ${col} ${on ? "bg-ink-700" : "bg-ink-850"} disabled:opacity-40`}>
      {p ? p.instrument.label.replace("NIFTY ", "") : `no ${side}`}{manual ? " · manual" : ""}
    </button>
  );
}

function FocusCard({ f }: { f: Focus }) {
  if (!f.available) return <div className="text-[11px] text-slate-500">{f.line}</div>;
  const lean = f.lean === "bull" ? "text-bull-400" : f.lean === "bear" ? "text-bear-400" : "text-slate-300";
  const c = (k: string, v: string) => <div><div className="text-[10px] text-slate-500 uppercase">{k}</div><div className="num text-slate-100">{v}</div></div>;
  return (
    <div className="rounded-lg border border-ink-700 bg-ink-850 p-2 space-y-1.5">
      <div className="text-[12px] font-semibold text-slate-100">{f.label} <span className={`ml-1 ${lean}`}>{f.kind}</span></div>
      <div className="grid grid-cols-4 gap-2 text-[11px]">
        {c("LTP", fmt(f.ltp, 2))}{c("Chg", `${(f.chg ?? 0) >= 0 ? "+" : ""}${fmt(f.chg, 1)}`)}
        {c("OI", lakh(f.oi ?? 0))}{c("ΔOI", `${(f.oi_chg ?? 0) >= 0 ? "+" : ""}${lakh(f.oi_chg ?? 0)}`)}
        {c("IV", fmt(f.iv, 1))}{c("Δ", fmt(f.delta, 2))}{c("Γ", (f.gamma ?? 0).toFixed(4))}{c("θ", fmt(f.theta, 1))}
      </div>
      <div className={`text-[11px] ${lean}`}>{f.line}</div>
    </div>
  );
}

export default function InstrumentPanel({ s, chartInst, setChartInst, onChange }: {
  s: Strikes | null; chartInst: string; setChartInst: (k: string) => void; onChange: (s: Strikes) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [right, setRight] = useState<"CE" | "PE">("CE");
  const [strike, setStrike] = useState<number | "">("");
  const [chain, setChain] = useState<Chain | null>(null);
  const [showWhy, setShowWhy] = useState(false);
  useEffect(() => { if (s?.expiry) api.chain(s.expiry).then(setChain).catch(() => {}); }, [s?.expiry]);
  if (!s) return null;
  const call = async (path: string, body: unknown) => {
    setBusy(true); setErr("");
    try {
      const r = await api.post<Strikes | { strikes: Strikes }>(path, body);
      onChange("strikes" in (r as object) ? (r as { strikes: Strikes }).strikes : (r as Strikes));
    } catch (e) { setErr((e as Error).message); }
    setBusy(false);
  };
  const setMode = async (m: "INDEX" | "OPTION_CHART") => {
    await api.post("/api/mode", { mode: m }).catch(() => {});
    onChange({ ...s, mode: m });
    if (m === "INDEX") setChartInst("NIFTY");
    else { const k = s.current.CE?.instrument.key ?? s.current.PE?.instrument.key; if (k) setChartInst(k); }
  };
  const optChart = s.mode === "OPTION_CHART";
  const strikes = (chain?.rows ?? []).map((r) => r.strike).filter((k) => Math.abs(k - (chain?.atm ?? k)) <= 600);
  const vcol = s.verdict.verdict === "supportive" ? "text-bull-400" : s.verdict.verdict === "opposing" ? "text-bear-400" : "text-slate-300";
  return (
    <div className="p-3 space-y-3 text-[12px]">
      <div className="flex flex-wrap items-center gap-1">
        <button className={`tab ${!optChart ? "tab-on" : ""}`} onClick={() => setMode("INDEX")}>Index structure</button>
        <button className={`tab ${optChart ? "tab-on" : ""}`} onClick={() => setMode("OPTION_CHART")}>Option chart · experimental</button>
      </div>
      {optChart && <div className="rounded border border-amber-400/60 bg-amber-400/10 px-2 py-1 text-[11px] text-amber-200">
        EXPERIMENTAL: zones on option candles are noisy (IV and theta move the premium). Trades, zones and the playbook stay on NIFTY.</div>}

      <div className="flex flex-wrap items-center gap-2">
        <label className="text-slate-400">Expiry
          <select value={s.expiry ?? ""} disabled={busy} onChange={(e) => call("/api/strikes/expiry", { expiry: e.target.value })}
            className="ml-1 bg-ink-900 border border-ink-700 rounded px-1">{s.expiries.map((x) => <option key={x}>{x}</option>)}</select>
        </label>
        <button disabled={busy} onClick={() => call("/api/strikes/select", {})} className="tab tab-on">Select strikes (Priyanka)</button>
        <label className="flex items-center gap-1 text-slate-400"><input type="checkbox" checked={s.auto} onChange={(e) => call("/api/strikes/auto", { on: e.target.checked })} /> auto</label>
      </div>
      <div className="text-[10px] text-slate-500">Auto: scores refresh with every chain update; the strike only {s.switch_rule}.</div>

      <div className="flex flex-wrap gap-2">
        {(["CE", "PE"] as const).map((sd) => {
          const p = s.current[sd];
          return <Chip key={sd} p={p} side={sd} manual={s.manual[sd]} on={chartInst === p?.instrument.key}
            onClick={() => p && optChart && setChartInst(chartInst === p.instrument.key ? "NIFTY" : p.instrument.key)} />;
        })}
        {optChart && chartInst !== "NIFTY" && <button className="tab" onClick={() => setChartInst("NIFTY")}>back to NIFTY</button>}
      </div>

      <div className="flex flex-wrap items-center gap-1">
        <span className="text-slate-400">Manual</span>
        <select value={right} onChange={(e) => setRight(e.target.value as "CE" | "PE")} className="bg-ink-900 border border-ink-700 rounded px-1"><option>CE</option><option>PE</option></select>
        <select value={strike} onChange={(e) => setStrike(e.target.value ? +e.target.value : "")} className="bg-ink-900 border border-ink-700 rounded px-1">
          <option value="">strike…</option>{strikes.map((k) => <option key={k} value={k}>{k}</option>)}</select>
        <button disabled={busy || strike === ""} onClick={() => call("/api/strikes/manual", { right, strike, expiry: s.expiry })} className="tab">Pin</button>
        {(s.manual.CE || s.manual.PE) && <button disabled={busy} onClick={() => call("/api/strikes/manual", { right, clear: true })} className="tab">Clear {right}</button>}
      </div>
      {err && <div className="text-amber-300">{err}</div>}

      <div className="space-y-2">{s.focus.map((f, i) => <FocusCard key={i} f={f} />)}</div>
      <div className="rounded-lg border border-ink-700 p-2">
        <div className={`font-semibold ${vcol}`}>Context verdict: {s.verdict.verdict}</div>
        <ul className="mt-0.5 space-y-0.5 text-[11px] text-slate-400">{s.verdict.lines.map((l, i) => <li key={i}>{l}</li>)}</ul>
      </div>

      <button className="text-[11px] text-gold-400" onClick={() => setShowWhy((x) => !x)}>{showWhy ? "▼" : "▶"} Why these strikes (Priyanka / MJ rule)</button>
      {showWhy && (
        <div className="space-y-1 text-[11px] text-slate-300">
          {s.reasons.map((r, i) => <div key={i}>· {r}</div>)}
          <div className="text-slate-500">Best course pick = MJ: ITM 50–250, multiple of 100, premium ≈ ₹200, best reward:risk from the Greeks scenario (Δ·move + ½Γ·move² + θ·time + ν·ΔIV). Fast pick = ATM…100 OTM, best % return. Same maths as the OC Strike Advisor.</div>
          {s.log.length > 0 && <div className="pt-1 text-slate-500">{s.log.slice(-5).map((l, i) => <div key={i}>{l}</div>)}</div>}
        </div>
      )}
    </div>
  );
}
