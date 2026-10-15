// CrtCockpit.tsx — DISPLAY-ONLY phase matrix for Setup D (CRT + TBS). Reads /api/crt-phase; changes nothing, no orders.
import { useEffect, useState } from "react";

type Step = { phase: string; at: string; text: string };
type Row = {
  pair: string; name: string; htf: string; ltf: string; time: string; k_ts: number; crh: number; crm: number; crl: number;
  bias: string; range_status: string; live?: boolean; phase: string; progress: number; note: string; aplus?: boolean;
  sweep: { at: string; price: number; rvol?: number | null; absorption: boolean } | null;
  trade: { side: string; entry: number; sl: number; tp1: number; tp2: number; rr: number; status: string; reasons?: string[]; strike?: string } | null;
  ready: string; steps: Step[]; keylevels: string[]; rvol_c1?: number | null;
  dol?: { kind: string; lo: number; hi: number; text: string };
};
const SHORT = ["C1 Accum", "C2 Sweep", "C3 MSS", "50% CRM", "Extreme"];
const n = (v?: number | null) => (v == null ? "—" : v.toLocaleString("en-IN", { maximumFractionDigits: 0 }));

export default function CrtCockpit({ tick }: { tick: number }) {
  const [d, setD] = useState<{ rows: Row[]; errors: string[] } | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  useEffect(() => {
    let dead = false;
    const go = () => fetch("/api/crt-phase").then((r) => r.json()).then((j) => { if (!dead) setD(j); }).catch(() => {});
    go();
    const id = window.setInterval(go, 20000);
    return () => { dead = true; window.clearInterval(id); };
  }, [tick]);
  const rows = d?.rows ?? [];
  return (
    <div className="flex h-full flex-col text-[12px]">
      <div className="border-b border-ink-700 px-3 py-1.5 text-[11px] text-slate-500">
        CRT phase cockpit — where each Setup D range is in its 3-candle story (C1 accumulation → C2 TBS sweep → C3 LTF MSS → 50 % CRM → opposing extreme).
        Display only: Setup D rules, signals and paper trades are unchanged.
      </div>
      <div className="min-h-0 flex-1 overflow-auto px-2 pb-2">
        {rows.length === 0 && <div className="py-6 text-center text-slate-500">No CRT ranges yet (needs closed HTF candles).</div>}
        <table className="w-full num">
          <thead><tr className="text-[10px] uppercase text-slate-500">
            {["Pair", "CRT candle", "Bias", "Phase", "CRH / CRM / CRL", "Sweep", "Trade plan", "Readiness"].map((h) =>
              <th key={h} className="sticky top-0 bg-ink-900 px-2 py-1 text-left">{h}</th>)}
          </tr></thead>
          <tbody>{rows.map((r) => {
            const id = `${r.pair}|${r.k_ts}`;
            const bad = r.phase === "INVALIDATED";
            return [
              <tr key={id} onClick={() => setOpen(open === id ? null : id)}
                className={`cursor-pointer border-b border-ink-800 ${r.ready === "TRADE LIVE" ? "bg-gold-500/10" : ""} ${r.ready === "done" ? "opacity-60" : ""}`}>
                <td className="px-2 py-1 font-semibold text-slate-200">{r.htf}›{r.ltf}{r.aplus ? <span className="ml-1 text-gold-400">A+</span> : null}</td>
                <td className="px-2">{r.time}</td>
                <td className={`px-2 font-bold ${r.bias === "bear" ? "text-bear-400" : r.bias === "bull" ? "text-bull-400" : "text-slate-400"}`}>{r.bias}</td>
                <td className="px-2 min-w-[190px]">
                  <div className="flex gap-0.5">{SHORT.map((s, i) => <div key={s} title={s}
                    className={`h-1.5 flex-1 rounded ${bad ? "bg-bear-500/50" : i < r.progress ? "bg-gold-500" : "bg-ink-700"}`} />)}</div>
                  <div className={`mt-0.5 text-[11px] ${bad ? "text-bear-400" : "text-slate-200"}`}>{r.phase}{r.note ? <span className="text-slate-500"> · {r.note}</span> : null}</div>
                </td>
                <td className="px-2"><span className="text-bear-400">{n(r.crh)}</span> / <span className="text-gold-400">{n(r.crm)}</span> / <span className="text-bull-400">{n(r.crl)}</span></td>
                <td className="px-2">{r.sweep ? <>{r.sweep.at} @ {n(r.sweep.price)}
                  {r.sweep.rvol != null && <span className={r.sweep.rvol >= 1.5 ? "text-gold-400" : "text-slate-500"}> · RVOL {r.sweep.rvol.toFixed(1)}×</span>}
                  {r.sweep.absorption && <span className="ml-1 rounded border border-cyan-400/60 px-1 text-[10px] text-cyan-300">ABSORB</span>}
                  {!r.trade && r.dol && <div className="text-[11px] font-semibold text-violet-300">{r.dol.text}</div>}</> : "—"}</td>
                <td className="px-2">{r.trade ? <>
                  <span className={r.trade.side.startsWith("SHORT") ? "text-bear-400" : "text-bull-400"}>{r.trade.side}</span>{r.trade.strike ? ` · ${r.trade.strike}` : ""}
                  {r.dol && <div className="text-[11px] font-semibold text-violet-300">{r.dol.text}</div>}
                  <div className="text-[11px] text-slate-400">E {n(r.trade.entry)} · <span className="text-bear-400">SL {n(r.trade.sl)}</span> · <span className="text-bull-400">TP1 {n(r.trade.tp1)} · TP2 {n(r.trade.tp2)}</span> · {r.trade.rr}R · {r.trade.status}</div>
                </> : "—"}</td>
                <td className={`px-2 font-semibold ${r.ready === "TRADE LIVE" ? "text-gold-400" : r.ready.startsWith("WATCH") ? "text-cyan-300" : "text-slate-400"}`}>{r.ready}</td>
              </tr>,
              open === id && <tr key={id + "x"} className="bg-ink-850/50"><td colSpan={8} className="px-4 py-1.5 text-[11px]">
                {r.steps.map((s, i) => <div key={i}><span className="text-gold-400">{s.at}</span> <b className="text-slate-300">{s.phase}</b> — <span className="text-slate-400">{s.text}</span></div>)}
                {r.keylevels.length > 0 && <div className="text-slate-500">Key levels: {r.keylevels.join(", ")}</div>}
                {r.trade?.reasons?.length ? <div className="text-amber-300/80">{r.trade.reasons.join(" · ")}</div> : null}
              </td></tr>,
            ];
          })}</tbody>
        </table>
        {d?.errors?.length ? <div className="mt-1 text-[11px] text-amber-300">⚠ {d.errors.join(" · ")}</div> : null}
      </div>
    </div>
  );
}
