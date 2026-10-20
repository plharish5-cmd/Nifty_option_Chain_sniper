// BiasPanel.tsx — the 20-factor confluence grid & Sniper Confluence Matrix for closed candles (information only).
import { useEffect, useMemo, useState } from "react";
import type { Analysis, Candle, TF } from "../api";
import { computeBias, FACTORS } from "./bias";
import type { FpBar } from "./footprint";
import { BAR_SEC } from "./chartKit";
import { useOcFeed, useSmt } from "./ocfeed";

const SNIPER_ATTRS = [
  { key: "IFVG", label: "IFVG", weight: "±0.5", factorName: "FVG/IFVG" },
  { key: "RJB+", label: "RJB+", weight: "±0.5", factorName: "Rejection block" },
  { key: "CRT", label: "CRT", weight: "±1.0", factorName: "CRT/TBS" },
  { key: "DOL", label: "D.O.L", weight: "+1.0", factorName: "Structure" },
  { key: "HTF_Wick", label: "HTF Wick", weight: "±0.75", factorName: "SMT" },
  { key: "APlus", label: "★ A+", weight: "+1.5", factorName: "★ A+ Confluence" },
] as const;

export default function BiasPanel({ tf, candles, analysis }: { tf: TF; candles: Candle[]; analysis: Analysis | null }) {
  const [fp, setFp] = useState<FpBar[]>([]);
  const [pct, setPct] = useState(0.7);
  const [filterAttr, setFilterAttr] = useState<string | null>(null);
  const oc = useOcFeed(true);
  const smt = useSmt(tf, true);
  const smtArg = tf === "5m" || tf === "15m" || tf === "1h" ? smt : null;

  useEffect(() => {
    if (!(tf === "5m" || tf === "15m" || tf === "1h")) { setFp([]); return; }
    let dead = false;
    const go = () => fetch(`/api/footprints?tf=${tf}&n=30`).then((r) => r.json()).then((j) => { if (!dead) setFp(j.available ? j.bars ?? [] : []); }).catch(() => {});
    go(); const id = window.setInterval(go, 30000);
    return () => { dead = true; window.clearInterval(id); };
  }, [tf]);

  const all = useMemo(() => computeBias(candles, analysis, fp, BAR_SEC[tf] ?? 300, pct, oc, true, smtArg), [candles, analysis, fp, tf, pct, oc, smtArg]);
  const live = all.length && all[all.length - 1].forming ? all[all.length - 1] : null;
  const rows = all.filter((r) => !r.forming).slice(-24).reverse();
  const now = rows[0];

  const triggers = analysis?.pa_advanced?.bias_20?.triggers ?? analysis?.pa_advanced?.sniper?.bias_20?.triggers;
  const b20Score = analysis?.pa_advanced?.bias_20 ?? analysis?.pa_advanced?.sniper?.bias_20;

  const dot = (v: string | null) => v === "bull" ? "bg-bull-500" : v === "bear" ? "bg-bear-500" : "bg-ink-700";
  const hm = (t: number) => { const d = new Date(t * 1000); return `${String(d.getUTCHours()).padStart(2, "0")}:${String(d.getUTCMinutes()).padStart(2, "0")}`; };

  return (
    <div className="flex h-full flex-col text-[12px] bg-ink-950 text-slate-200">
      {/* Top Header & Aggregate Bias 20 Status */}
      <div className="flex flex-wrap items-center gap-3 border-b border-ink-700 px-3 py-1.5 bg-ink-900/60">
        {now && (
          <span className={`rounded px-2 py-0.5 text-[13px] font-bold ${
            now.verdict === "BUY" ? "bg-bull-500 text-black" : now.verdict === "SELL" ? "bg-bear-500 text-white" : "bg-ink-700 text-slate-200"
          }`}>
            {hm(now.time)} · {now.verdict === "MIXED" ? `MIXED ${now.bull}▲ / ${now.bear}▼` : `${now.verdict} bias ${Math.max(now.bull, now.bear)}/${now.avail}`}
          </span>
        )}

        <span className="text-slate-400 font-medium">
          {tf} · last closed candle · {now ? `${now.avail} of 20 factors have data` : "20-Factor Matrix"}
        </span>

        {b20Score && (
          <span className="flex items-center gap-1.5 px-2 py-0.5 rounded bg-gold-950/40 border border-gold-500/40 text-gold-300 font-mono text-[11px]">
            <span>⚖ BIAS 20:</span>
            <span className="text-bull-400 font-bold">{b20Score.bull_score.toFixed(1)}▲</span> /
            <span className="text-bear-400 font-bold">{b20Score.bear_score.toFixed(1)}▼</span>
            <span className="text-slate-400">({b20Score.net_score > 0 ? `+${b20Score.net_score}` : b20Score.net_score} net)</span>
          </span>
        )}

        {live && (
          <span className="rounded border border-dashed border-slate-500 px-2 py-0.5 text-slate-400" title="Forming candle — the score can change completely before the close. Never enter on this.">
            forming {hm(live.time)} · <span className="text-bull-400">{live.bull}▲</span> / <span className="text-bear-400">{live.bear}▼</span> of {live.avail} · <i>provisional</i>
          </span>
        )}

        <label className="ml-auto text-slate-400 text-[11px]">
          badge when ≥
          <select className="ml-1 rounded bg-ink-850 px-1 border border-ink-700" value={pct} onChange={(e) => setPct(+e.target.value)}>
            {[[0.6, "60 %"], [0.7, "70 %"], [0.8, "80 %"]].map(([v, l]) => <option key={v} value={v}>{l}</option>)}
          </select> of available factors agree
        </label>
      </div>

      {/* 🎯 Sniper Confluence Attributes & Filter Pills Bar */}
      <div className="flex flex-wrap items-center gap-2 border-b border-ink-800 px-3 py-1.5 bg-ink-900/40 text-[11px]">
        <span className="font-bold text-[10px] uppercase text-gold-400 tracking-wider">
          🎯 Sniper Confluence Triggers:
        </span>

        {SNIPER_ATTRS.map((sa) => {
          const trig = triggers?.[sa.key];
          const status = trig?.status || "NEUTRAL";
          const isAct = status === "ACTIVE";
          const isVeto = status === "VETOED";
          const isSelected = filterAttr === sa.key || filterAttr === sa.factorName;

          const badgeClass = isVeto
            ? "bg-rose-950/80 text-rose-300 border-rose-500/70"
            : isAct
            ? "bg-emerald-950/80 text-emerald-300 border-emerald-500/70"
            : "bg-ink-800/80 text-slate-400 border-ink-700/80";

          const icon = isVeto ? "🚫" : isAct ? "✓" : "○";

          return (
            <button
              key={sa.key}
              onClick={() => setFilterAttr((prev) => (prev === sa.key ? null : sa.key))}
              title={`${sa.label} (${sa.weight}): ${status} — ${trig?.desc || "No active signal"}. Click to filter.`}
              className={`flex items-center gap-1.5 px-2 py-0.5 rounded text-[11px] font-semibold border transition-all ${badgeClass} ${
                isSelected ? "ring-2 ring-gold-400 shadow-sm" : "hover:border-slate-400"
              }`}
            >
              <span className="text-[10px]">{icon}</span>
              <span className="font-bold">{sa.label}</span>
              <span className="text-[10px] opacity-75 font-mono">{sa.weight}</span>
              <span className="text-[10px] uppercase font-bold tracking-tight">
                {isVeto ? "VETO" : isAct ? (trig?.dir === "bull" ? "▲" : trig?.dir === "bear" ? "▼" : "ACT") : "NEUT"}
              </span>
            </button>
          );
        })}

        {filterAttr && (
          <button
            onClick={() => setFilterAttr(null)}
            className="ml-auto text-[10px] text-gold-400 hover:text-gold-300 underline"
          >
            Clear Filter (Show All 20)
          </button>
        )}
      </div>

      {/* 20-Factor Matrix Table */}
      <div className="min-h-0 flex-1 overflow-auto px-2 pb-2">
        <table className="w-full num">
          <thead>
            <tr className="text-[10px] uppercase text-slate-400">
              <th className="sticky top-0 bg-ink-900 px-2 py-1 text-left z-10">Time</th>
              {FACTORS.map((f, idx) => {
                const sa = SNIPER_ATTRS.find((a) => a.factorName === f);
                const trig = sa ? triggers?.[sa.key] : null;
                const status = trig?.status;
                const isSelected = filterAttr === f || (sa && filterAttr === sa.key);

                return (
                  <th
                    key={f}
                    onClick={() => setFilterAttr((prev) => (prev === f ? null : f))}
                    title={`#${idx + 1} ${f}${sa ? ` [Sniper: ${sa.label} ${sa.weight}]` : ""}`}
                    className={`sticky top-0 px-1 py-1 cursor-pointer transition-colors z-10 ${
                      isSelected ? "bg-amber-950/80 text-gold-300 border-b-2 border-gold-400" : "bg-ink-900 hover:text-slate-200"
                    }`}
                  >
                    <div className="flex flex-col items-center">
                      <span className="text-[9px] text-slate-500">#{idx + 1}</span>
                      <span className="truncate max-w-[55px] font-semibold">{f}</span>
                      {sa && (
                        <span
                          className={`text-[8px] px-1 rounded font-bold mt-0.5 ${
                            status === "ACTIVE"
                              ? "bg-emerald-900 text-emerald-200"
                              : status === "VETOED"
                              ? "bg-rose-900 text-rose-200"
                              : "bg-ink-800 text-slate-400"
                          }`}
                        >
                          {sa.label}
                        </span>
                      )}
                    </div>
                  </th>
                );
              })}
              <th className="sticky top-0 bg-ink-900 px-2 py-1 z-10">Score</th>
              <th className="sticky top-0 bg-ink-900 px-2 py-1 text-left z-10">Bias</th>
            </tr>
          </thead>
          <tbody>
            {live && (
              <tr className="border-b border-dashed border-slate-600 italic opacity-60">
                <td className="px-2 py-1 text-slate-400">{hm(live.time)} forming</td>
                {FACTORS.map((f) => (
                  <td key={f} className="px-1 text-center" title={live.why[f] || "no vote"}>
                    <span className={`inline-block h-3 w-3 rounded-full ${dot(live.votes[f])}`} />
                  </td>
                ))}
                <td className="px-2 text-center">
                  <span className="text-bull-400">{live.bull}▲</span> <span className="text-bear-400">{live.bear}▼</span>
                </td>
                <td className="px-2 text-slate-400">provisional</td>
              </tr>
            )}
            {rows.map((r) => (
              <tr key={r.time} className="border-b border-ink-800 hover:bg-ink-900/30">
                <td className="px-2 py-1 font-semibold text-slate-200">
                  {hm(r.time)} <span className={r.c >= r.o ? "text-bull-400" : "text-bear-400"}>{r.c >= r.o ? "▲" : "▼"}</span>
                </td>
                {FACTORS.map((f) => (
                  <td key={f} className="px-1 text-center" title={r.why[f] || "no vote"}>
                    <span className={`inline-block h-3 w-3 rounded-full ${dot(r.votes[f])}`} />
                  </td>
                ))}
                <td className="px-2 text-center font-mono">
                  <span className="text-bull-400">{r.bull}▲</span> <span className="text-bear-400">{r.bear}▼</span>
                </td>
                <td className={`px-2 font-bold ${r.verdict === "BUY" ? "text-bull-400" : r.verdict === "SELL" ? "text-bear-400" : "text-slate-500"}`}>
                  {r.warn ? "⚠ " : ""}{r.verdict === "MIXED" ? "mixed" : `${r.verdict} ${Math.max(r.bull, r.bear)}/${r.avail}${r.strong ? " ★" : ""}`}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* Detailed Footer & Legend */}
      <div className="border-t border-ink-700 px-3 py-1 text-[10.5px] text-slate-400">
        <b>Bias 20 Confluence System:</b> Green dot = votes BUY, red = SELL, grey = no vote. Hover a dot for reason.
        <span className="ml-1 text-slate-500">
          Factors 1–20: 1. FVG/IFVG · 2. Order block · 3. Rejection block · 4. CRT/TBS · 5. Footprint · 6. RVOL · 7. VWAP · 8. Structure · 9. PE−CE action · 10. Writers gap · 11. Δput−Δcall · 12. Writer dominance · 13. Writer flip · 14. PCR trend · 15. SMT · 16. CCT · 17. MMXM · 18. CVD flow · 19. Gamma flip · 20. ★ A+ Confluence.
        </span>
        <div className="mt-0.5 text-gold-400/90">
          🎯 Sniper Confluence Attributes: IFVG (±0.5) · RJB+ (±0.5) · CRT (±1.0) · D.O.L (±1.0) · HTF Wick (±0.75) · ★ A+ Confluence (Dragon Fruit / Super OB ≥ 6/7: +1.5). Green = Active, Red = Vetoed, Gray = Neutral. Information only — not an order.
        </div>
      </div>
    </div>
  );
}
