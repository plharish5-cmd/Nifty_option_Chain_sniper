// OptionChain.tsx — CE | strike | PE, OI bars scaled to the largest OI in view, ATM highlighted, ITM shaded.
import { useEffect, useMemo, useRef, useState } from "react";
import { api, fmt, lakh, type Chain } from "../api";

export default function OptionChain({ expiries, tick }: { expiries: string[]; tick: number }) {
  const [exp, setExp] = useState<string | undefined>(undefined);
  const [ch, setCh] = useState<Chain | null>(null);
  const [width, setWidth] = useState(10);
  const atmRow = useRef<HTMLTableRowElement>(null);
  const scrolled = useRef(false);

  useEffect(() => { if (!exp && expiries.length) setExp(expiries[0]); }, [expiries, exp]);
  useEffect(() => {
    let dead = false;
    api.chain(exp).then((c) => { if (!dead) setCh(c); }).catch(() => {});
    return () => { dead = true; };
  }, [exp, tick]);

  const rows = useMemo(() => {
    if (!ch?.rows?.length) return [];
    const i = ch.rows.findIndex((r) => r.strike === ch.atm);
    return ch.rows.slice(Math.max(0, i - width), i + width + 1);
  }, [ch, width]);
  const maxOI = useMemo(() => Math.max(1, ...rows.flatMap((r) => [r.ce.oi, r.pe.oi])), [rows]);
  useEffect(() => {
    if (!scrolled.current && atmRow.current) { atmRow.current.scrollIntoView({ block: "center" }); scrolled.current = true; }
  }, [rows]);

  if (!ch || !ch.rows?.length) {
    return <div className="p-6 text-sm text-slate-400">Option chain not loaded yet{ch?.error ? ` — ${ch.error}` : ""}.</div>;
  }
  const T = ch.totals;
  return (
    <div className="flex flex-col h-full">
      <div className="flex flex-wrap items-center gap-x-5 gap-y-1 px-4 py-2 text-xs border-b border-ink-700">
        <div className="flex gap-1">
          {expiries.map((e) => (
            <button key={e} onClick={() => { setExp(e); scrolled.current = false; }} className={`tab ${e === (exp ?? expiries[0]) ? "tab-on" : ""}`}>
              {new Date(e + "T00:00:00").toLocaleDateString("en-IN", { day: "2-digit", month: "short", weekday: "short" })}
            </button>
          ))}
        </div>
        <span>Spot <b className="num text-slate-100">{fmt(ch.spot, 2)}</b></span>
        <span>ATM <b className="num text-gold-400">{ch.atm}</b></span>
        <span>PCR <b className="num">{T.pcr ?? "—"}</b></span>
        <span>Max pain <b className="num">{ch.max_pain}</b></span>
        <span>Call wall <b className="num text-bear-400">{ch.call_wall ?? "—"}</b></span>
        <span>Put wall <b className="num text-bull-400">{ch.put_wall ?? "—"}</b></span>
        <span className="text-slate-500">ΔOI CE {lakh(T.ce_oi_chg)} · PE {lakh(T.pe_oi_chg)}</span>
        <select value={width} onChange={(e) => setWidth(+e.target.value)} className="ml-auto bg-ink-800 border border-ink-600 rounded px-1.5 py-0.5">
          {[6, 10, 15, 25].map((w) => <option key={w} value={w}>±{w} strikes</option>)}
        </select>
      </div>
      <div className="overflow-auto flex-1">
        <table className="w-full text-[12px] num">
          <thead className="sticky top-0 bg-ink-850 text-slate-400 text-[10px] uppercase tracking-wider">
            <tr>
              <th className="px-2 py-1.5 text-right font-medium">IV</th><th className="px-2 text-right font-medium">Δ</th>
              <th className="px-2 text-right font-medium">LTP</th><th className="px-2 text-right font-medium">Chg</th>
              <th className="px-2 text-right font-medium">OI chg</th><th className="px-2 font-medium w-[22%] text-right">Call OI</th>
              <th className="px-2 text-center font-medium text-gold-400">Strike</th>
              <th className="px-2 font-medium w-[22%] text-left">Put OI</th><th className="px-2 text-left font-medium">OI chg</th>
              <th className="px-2 text-left font-medium">Chg</th><th className="px-2 text-left font-medium">LTP</th>
              <th className="px-2 text-left font-medium">Δ</th><th className="px-2 text-left font-medium">IV</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => {
              const atm = r.strike === ch.atm;
              const ceITM = r.strike < ch.spot, peITM = r.strike > ch.spot;
              return (
                <tr key={r.strike} ref={atm ? atmRow : undefined}
                  className={`border-b border-ink-800 ${atm ? "bg-gold-500/10 outline outline-1 outline-gold-500/50" : ""}`}>
                  <td className={`px-2 py-1 text-right ${ceITM ? "bg-ink-800/60" : ""}`}>{fmt(r.ce.iv, 1)}</td>
                  <td className={`px-2 text-right text-slate-400 ${ceITM ? "bg-ink-800/60" : ""}`}>{r.ce.delta.toFixed(2)}</td>
                  <td className={`px-2 text-right text-slate-100 ${ceITM ? "bg-ink-800/60" : ""}`}>{fmt(r.ce.ltp, 2)}</td>
                  <td className={`px-2 text-right ${r.ce.chg >= 0 ? "text-bull-400" : "text-bear-400"} ${ceITM ? "bg-ink-800/60" : ""}`}>{fmt(r.ce.chg, 1)}</td>
                  <td className={`px-2 text-right ${r.ce.oi_chg >= 0 ? "text-bull-400" : "text-bear-400"} ${ceITM ? "bg-ink-800/60" : ""}`}>{lakh(r.ce.oi_chg)}</td>
                  <td className={`px-2 ${ceITM ? "bg-ink-800/60" : ""}`}>
                    <div className="flex items-center justify-end gap-1.5">
                      <span className="text-slate-300">{lakh(r.ce.oi)}</span>
                      <div className="h-2.5 w-24 bg-ink-800 rounded-sm overflow-hidden flex justify-end">
                        <div className="h-full bg-bear-500/80" style={{ width: `${(100 * r.ce.oi) / maxOI}%` }} />
                      </div>
                    </div>
                  </td>
                  <td className={`px-2 text-center font-semibold ${atm ? "text-gold-400" : "text-slate-200"}`}>{r.strike}</td>
                  <td className={`px-2 ${peITM ? "bg-ink-800/60" : ""}`}>
                    <div className="flex items-center gap-1.5">
                      <div className="h-2.5 w-24 bg-ink-800 rounded-sm overflow-hidden">
                        <div className="h-full bg-bull-500/80" style={{ width: `${(100 * r.pe.oi) / maxOI}%` }} />
                      </div>
                      <span className="text-slate-300">{lakh(r.pe.oi)}</span>
                    </div>
                  </td>
                  <td className={`px-2 ${r.pe.oi_chg >= 0 ? "text-bull-400" : "text-bear-400"} ${peITM ? "bg-ink-800/60" : ""}`}>{lakh(r.pe.oi_chg)}</td>
                  <td className={`px-2 ${r.pe.chg >= 0 ? "text-bull-400" : "text-bear-400"} ${peITM ? "bg-ink-800/60" : ""}`}>{fmt(r.pe.chg, 1)}</td>
                  <td className={`px-2 text-slate-100 ${peITM ? "bg-ink-800/60" : ""}`}>{fmt(r.pe.ltp, 2)}</td>
                  <td className={`px-2 text-slate-400 ${peITM ? "bg-ink-800/60" : ""}`}>{r.pe.delta.toFixed(2)}</td>
                  <td className={`px-2 ${peITM ? "bg-ink-800/60" : ""}`}>{fmt(r.pe.iv, 1)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <div className="px-4 py-1.5 text-[10px] text-slate-500 border-t border-ink-700">
        Updated {new Date(ch.ts * 1000).toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata" })} IST · OI change vs previous close ·
        shaded = in the money · red bars = call OI (resistance), green = put OI (support)
      </div>
    </div>
  );
}
