// BiasPanel.tsx — the 7-factor confluence grid for the last closed candles (information only).
import { useEffect, useMemo, useState } from "react";
import type { Analysis, Candle, TF } from "../api";
import { computeBias, FACTORS } from "./bias";
import type { FpBar } from "./footprint";
import { BAR_SEC } from "./chartKit";
import { useOcFeed } from "./ocfeed";

export default function BiasPanel({ tf, candles, analysis }: { tf: TF; candles: Candle[]; analysis: Analysis | null }) {
  const [fp, setFp] = useState<FpBar[]>([]);
  const [pct, setPct] = useState(0.7);
  const oc = useOcFeed(true);
  useEffect(() => {
    if (!(tf === "5m" || tf === "15m" || tf === "1h")) { setFp([]); return; }
    let dead = false;
    const go = () => fetch(`/api/footprints?tf=${tf}&n=30`).then((r) => r.json()).then((j) => { if (!dead) setFp(j.available ? j.bars ?? [] : []); }).catch(() => {});
    go(); const id = window.setInterval(go, 30000);
    return () => { dead = true; window.clearInterval(id); };
  }, [tf]);
  const all = useMemo(() => computeBias(candles, analysis, fp, BAR_SEC[tf] ?? 300, pct, oc, true), [candles, analysis, fp, tf, pct, oc]);
  const live = all.length && all[all.length - 1].forming ? all[all.length - 1] : null;
  const rows = all.filter((r) => !r.forming).slice(-24).reverse();
  const now = rows[0];
  const dot = (v: string | null) => v === "bull" ? "bg-bull-500" : v === "bear" ? "bg-bear-500" : "bg-ink-700";
  const hm = (t: number) => { const d = new Date(t * 1000); return `${String(d.getUTCHours()).padStart(2, "0")}:${String(d.getUTCMinutes()).padStart(2, "0")}`; };
  return (
    <div className="flex h-full flex-col text-[12px]">
      <div className="flex flex-wrap items-center gap-3 border-b border-ink-700 px-3 py-1.5">
        {now && <span className={`rounded px-2 py-0.5 text-[13px] font-bold ${now.verdict === "BUY" ? "bg-bull-500 text-black" : now.verdict === "SELL" ? "bg-bear-500 text-white" : "bg-ink-700 text-slate-200"}`}>
          {hm(now.time)} · {now.verdict === "MIXED" ? `MIXED ${now.bull}▲ / ${now.bear}▼` : `${now.verdict} bias ${Math.max(now.bull, now.bear)}/${now.avail}`}</span>}
        <span className="text-slate-400">{tf} · last closed candle · {now ? `${now.avail} of 12 factors have data` : ""}</span>
        {live && <span className="rounded border border-dashed border-slate-500 px-2 py-0.5 text-slate-400" title="Forming candle — the score can change completely before the close. Never enter on this.">
          forming {hm(live.time)} · <span className="text-bull-400">{live.bull}▲</span> / <span className="text-bear-400">{live.bear}▼</span> of {live.avail} · <i>provisional</i></span>}
        <label className="ml-auto text-slate-400">badge when ≥
          <select className="ml-1 rounded bg-ink-850 px-1" value={pct} onChange={(e) => setPct(+e.target.value)}>
            {[[0.6, "60 %"], [0.7, "70 %"], [0.8, "80 %"]].map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select> of available factors agree</label>
      </div>
      <div className="min-h-0 flex-1 overflow-auto px-2 pb-2">
        <table className="w-full num">
          <thead><tr className="text-[10px] uppercase text-slate-500">
            <th className="sticky top-0 bg-ink-900 px-2 py-1 text-left">Time</th>
            {FACTORS.map((f) => <th key={f} className="sticky top-0 bg-ink-900 px-1 py-1">{f}</th>)}
            <th className="sticky top-0 bg-ink-900 px-2 py-1">Score</th><th className="sticky top-0 bg-ink-900 px-2 py-1 text-left">Bias</th>
          </tr></thead>
          <tbody>
            {live && <tr className="border-b border-dashed border-slate-600 italic opacity-60">
              <td className="px-2 py-1 text-slate-400">{hm(live.time)} forming</td>
              {FACTORS.map((f) => <td key={f} className="px-1 text-center" title={live.why[f] || "no vote"}><span className={`inline-block h-3 w-3 rounded-full ${dot(live.votes[f])}`} /></td>)}
              <td className="px-2 text-center"><span className="text-bull-400">{live.bull}▲</span> <span className="text-bear-400">{live.bear}▼</span></td>
              <td className="px-2 text-slate-400">provisional</td></tr>}
            {rows.map((r) => (
            <tr key={r.time} className="border-b border-ink-800">
              <td className="px-2 py-1 font-semibold text-slate-200">{hm(r.time)} <span className={r.c >= r.o ? "text-bull-400" : "text-bear-400"}>{r.c >= r.o ? "▲" : "▼"}</span></td>
              {FACTORS.map((f) => <td key={f} className="px-1 text-center" title={r.why[f] || "no vote"}>
                <span className={`inline-block h-3 w-3 rounded-full ${dot(r.votes[f])}`} /></td>)}
              <td className="px-2 text-center"><span className="text-bull-400">{r.bull}▲</span> <span className="text-bear-400">{r.bear}▼</span></td>
              <td className={`px-2 font-bold ${r.verdict === "BUY" ? "text-bull-400" : r.verdict === "SELL" ? "text-bear-400" : "text-slate-500"}`}>
                {r.verdict === "MIXED" ? "mixed" : `${r.verdict} ${Math.max(r.bull, r.bear)}/${r.avail}${r.strong ? " ★" : ""}`}</td>
            </tr>))}</tbody>
        </table>
      </div>
      <div className="border-t border-ink-700 px-3 py-1 text-[10.5px] text-slate-500">
        Green dot = votes BUY, red = SELL, grey = no vote. Hover a dot for the reason. FVG/IFVG &amp; Order block: live zone tested &amp; held, or price within 1 ATR above support / below resistance ·
        CRT/TBS: last 6 bars · Footprint: flow tag + delta (estimated) · RVOL ≥ 1.2× votes with the candle colour · VWAP: close vs futures VWAP ·
        Structure: last BOS/MSS · PE−CE action (8501 table) · Writers gap: put−call OI gap widening/narrowing over 30 min · Δput−Δcall: unusual writer candle, else 30-min balance ±0.25 · Writer dominance: put line above call = BUY, degree by separation (mild ≥20 %, moderate ≥50 %, strong ≥75 %) · Writer flip: ▲BULL/▼BEAR marker in the last 6 candles. Badges on closed candles only; the dashed top row is the forming candle (provisional, changes every tick). Not backtested — information, not an order.
      </div>
    </div>
  );
}
