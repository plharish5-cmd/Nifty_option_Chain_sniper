// CctMmxmPanel.tsx — Institutional Cockpit for Candle Continuity Theory (CCT) & Market Maker Models (MMXM)
// Based on Chapters 23 & 25 of the 12-Hour ICT & SMC Masterclass. Display only.
import { useEffect, useState } from "react";
import { api, type CctView, type MmxmView, type TF } from "../api";

const STAGES = [
  { id: "ORIG_CONS", name: "0. Orig Cons", desc: "Target Anchor Range" },
  { id: "ENGINEERING", name: "1. Engineering", desc: "Low-Resistance Liquidity" },
  { id: "SMR_CONFIRMED", name: "2. SMR Confirmed", desc: "MSS Break + Displacement" },
  { id: "LOW_RISK_BUY", name: "3. Low-Risk Entry", desc: "20%–38% FVG Retest" },
  { id: "REACCUMULATION", name: "4. Re-accumulation", desc: "50%–75% Displacement" },
  { id: "TARGET_REACHED", name: "5. Delivered", desc: "Orig Cons Rebalanced" },
];

export default function CctMmxmPanel({ tick }: { tick: number }) {
  const [tf, setTf] = useState<TF>("5m");
  const [cct, setCct] = useState<CctView | null>(null);
  const [mmxm, setMmxm] = useState<MmxmView | null>(null);
  const [showConf, setShowConf] = useState(false);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let dead = false;
    setLoading(true);
    Promise.all([api.cct(tf).catch(() => null), api.mmxm(tf).catch(() => null)])
      .then(([c, m]) => {
        if (!dead) {
          if (c) setCct(c);
          if (m) setMmxm(m);
          setLoading(false);
        }
      })
      .catch(() => {
        if (!dead) setLoading(false);
      });
    return () => {
      dead = true;
    };
  }, [tf, tick]);

  const primaryModel = mmxm?.primary;
  const cctStats = cct?.stats;
  const mmxmStats = mmxm?.stats;

  return (
    <div className="flex h-full flex-col text-[12px] bg-ink-950 text-slate-200">
      {/* Top Controls & Header Bar */}
      <div className="flex flex-wrap items-center justify-between border-b border-ink-800 px-3 py-2 bg-ink-900/60 gap-2">
        <div className="flex items-center gap-2">
          <span className="font-bold text-gold-400">🏛️ MMXM State Engine &amp; 🕯️ CCT Cockpit</span>
          <span className="text-[11px] text-slate-500">· Zero Repainting · Graded Execution</span>
        </div>
        <div className="flex items-center gap-3">
          {cct && (
            <span
              className={`px-2 py-0.5 rounded text-[10px] font-semibold uppercase tracking-wider ${
                cct.in_killzone
                  ? "bg-emerald-950/80 border border-emerald-500/50 text-emerald-300"
                  : "bg-ink-800 border border-ink-700 text-slate-400"
              }`}
            >
              {cct.in_killzone ? `● ${cct.killzone_name}` : `○ ${cct.killzone_name}`}
            </span>
          )}
          <div className="flex gap-1 bg-ink-800/80 p-0.5 rounded border border-ink-700">
            {(["1m", "5m", "15m", "1h"] as TF[]).map((t) => (
              <button
                key={t}
                onClick={() => setTf(t)}
                className={`px-2 py-0.5 rounded text-[11px] font-semibold transition ${
                  tf === t ? "bg-gold-500 text-black shadow" : "text-slate-400 hover:text-slate-200"
                }`}
              >
                {t}
              </button>
            ))}
          </div>
        </div>
      </div>

      {/* Aggregate Stats Bar */}
      <div className="grid grid-cols-2 border-b border-ink-800 px-3 py-1.5 bg-ink-900/30 text-[11px]">
        <div className="flex items-center gap-3">
          <span className="text-cyan-400 font-semibold">🕯️ CCT Performance:</span>
          {cctStats ? (
            <span className="text-slate-300">
              {cctStats.total_setups} setups ({cctStats.grade_a_count} A / {cctStats.grade_b_count} B) ·
              Win Rate:{" "}
              <span className={cctStats.win_rate != null && cctStats.win_rate >= 60 ? "text-emerald-400 font-bold" : "text-slate-300"}>
                {cctStats.win_rate != null ? `${cctStats.win_rate}%` : "—"}
              </span>{" "}
              · Avg R:{" "}
              <span className={cctStats.avg_r > 0 ? "text-emerald-400 font-bold" : "text-slate-300"}>
                {cctStats.avg_r >= 0 ? `+${cctStats.avg_r}R` : `${cctStats.avg_r}R`}
              </span>{" "}
              · Avg Drawdown (MAE): <span className="text-amber-400">-{cctStats.avg_mae_pts} pts</span>
            </span>
          ) : (
            <span className="text-slate-500">Calculating...</span>
          )}
        </div>
        <div className="flex items-center gap-3 justify-end">
          <span className="text-gold-400 font-semibold">🏛️ MMXM Performance:</span>
          {mmxmStats ? (
            <span className="text-slate-300">
              {mmxmStats.total_cycles} cycles · Completed:{" "}
              <span className="text-emerald-400 font-bold">{mmxmStats.completion_rate}%</span> · Avg Target R:{" "}
              <span className="text-gold-400 font-bold">+{mmxmStats.avg_r_potential}R</span>
            </span>
          ) : (
            <span className="text-slate-500">Calculating...</span>
          )}
        </div>
      </div>

      {/* Main Two-Column Cockpit */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-3 p-3 flex-1 overflow-auto min-h-0">
        {/* COLUMN 1: CANDLE CONTINUITY THEORY (CCT) */}
        <div className="card flex flex-col p-3 border-ink-700/80 bg-ink-900/40">
          <div className="flex items-center justify-between border-b border-ink-800 pb-2 mb-2">
            <div className="flex items-center gap-2">
              <span className="text-sm font-bold text-cyan-400">🕯️ Candle Continuity Theory</span>
              <span className="text-[10px] bg-cyan-950 text-cyan-300 border border-cyan-800 px-1.5 py-0.2 rounded font-mono">
                {tf} CCT
              </span>
            </div>
            {cct && (
              <span
                className={`text-[11px] font-bold px-2 py-0.5 rounded ${
                  cct.continuity.includes("BULL")
                    ? "bg-emerald-950 border border-emerald-500/60 text-emerald-300"
                    : cct.continuity.includes("BEAR")
                    ? "bg-rose-950 border border-rose-500/60 text-rose-300"
                    : "bg-amber-950 border border-amber-500/60 text-amber-300"
                }`}
              >
                {cct.continuity}
              </span>
            )}
          </div>

          {cct ? (
            <div className="space-y-3 flex-1 overflow-auto">
              {/* Benchmark Open Cards */}
              <div className="grid grid-cols-3 gap-2 text-center">
                <div className="p-2 rounded bg-ink-800/50 border border-ink-700/60">
                  <div className="text-[10px] text-slate-400 uppercase">Daily Open (09:15)</div>
                  <div className="text-sm font-bold font-mono text-slate-100">{cct.day_open.toLocaleString("en-IN")}</div>
                  <div
                    className={`text-[10px] font-semibold ${
                      cct.day_delta >= 0 ? "text-emerald-400" : "text-rose-400"
                    }`}
                  >
                    {cct.day_delta >= 0 ? `+${cct.day_delta.toFixed(1)}` : cct.day_delta.toFixed(1)} pts ({cct.day_bias})
                  </div>
                </div>

                <div className="p-2 rounded bg-ink-800/50 border border-ink-700/60">
                  <div className="text-[10px] text-slate-400 uppercase">1H Benchmark Open</div>
                  <div className="text-sm font-bold font-mono text-slate-100">{cct.h1_open.toLocaleString("en-IN")}</div>
                  <div
                    className={`text-[10px] font-semibold ${
                      cct.h1_delta >= 0 ? "text-emerald-400" : "text-rose-400"
                    }`}
                  >
                    {cct.h1_delta >= 0 ? `+${cct.h1_delta.toFixed(1)}` : cct.h1_delta.toFixed(1)} pts ({cct.h1_bias})
                  </div>
                </div>

                <div className="p-2 rounded bg-ink-800/50 border border-ink-700/60">
                  <div className="text-[10px] text-slate-400 uppercase">{tf} Bar Open</div>
                  <div className="text-sm font-bold font-mono text-slate-100">{cct.bar_open.toLocaleString("en-IN")}</div>
                  <div
                    className={`text-[10px] font-semibold ${
                      cct.bar_bias === "BULL" ? "text-emerald-400" : "text-rose-400"
                    }`}
                  >
                    Current: {cct.curr_price.toLocaleString("en-IN")}
                  </div>
                </div>
              </div>

              {/* Institutional Rule Directive Box */}
              <div
                className={`p-2.5 rounded border text-[11px] leading-relaxed ${
                  cct.continuity.includes("BULL")
                    ? "bg-emerald-950/30 border-emerald-500/40 text-emerald-200"
                    : cct.continuity.includes("BEAR")
                    ? "bg-rose-950/30 border-rose-500/40 text-rose-200"
                    : "bg-amber-950/30 border-amber-500/40 text-amber-200"
                }`}
              >
                <div className="font-bold uppercase tracking-wider text-[10px] mb-0.5">
                  ★ Institutional Execution Directive:
                </div>
                {cct.guidance}
              </div>

              {/* Graded 4-Candle Execution Triggers */}
              <div>
                <div className="text-[11px] font-bold text-slate-300 uppercase tracking-wide mb-1.5 flex items-center justify-between">
                  <span>Graded 4-Candle Triggers (A vs B)</span>
                  <span className="text-[10px] text-slate-400 font-normal">With SL, TP1, and MMXM Context</span>
                </div>
                {cct.triggers.length === 0 ? (
                  <div className="p-3 text-center text-slate-500 text-[11px] border border-ink-800 rounded">
                    No active 4-candle triggers on this timeframe. Waiting for 3-bar pivot in POI.
                  </div>
                ) : (
                  <div className="space-y-2">
                    {cct.triggers.slice(-5).reverse().map((t) => (
                      <div
                        key={t.id}
                        className={`p-2.5 rounded border text-[11px] transition ${
                          t.is_counter
                            ? "bg-ink-900/60 border-amber-500/40 opacity-75"
                            : t.grade === "A"
                            ? "bg-emerald-950/20 border-emerald-500/40"
                            : "bg-ink-850 border-ink-700"
                        }`}
                      >
                        <div className="flex items-center justify-between mb-1">
                          <div className="flex items-center gap-1.5 font-semibold">
                            <span
                              className={`px-1.5 py-0.5 rounded text-[10px] font-bold ${
                                t.grade === "A"
                                  ? "bg-emerald-600 text-white"
                                  : "bg-slate-700 text-slate-300 border border-slate-600"
                              }`}
                            >
                              Grade {t.grade}
                            </span>
                            <span
                              className={`px-1 rounded text-[10px] ${
                                t.dir === "bull" ? "bg-emerald-950 text-emerald-300" : "bg-rose-950 text-rose-300"
                              }`}
                            >
                              {t.dir === "bull" ? "BUY TRIGGER" : "SELL TRIGGER"}
                            </span>
                            <span className="text-slate-300">{t.at}</span>
                            <span className="text-slate-400 text-[10px]">· {t.poi}</span>
                          </div>

                          <div className="flex items-center gap-1.5">
                            <span
                              className={`px-1.5 py-0.5 rounded text-[10px] font-medium ${
                                t.is_counter
                                  ? "bg-amber-950/80 border border-amber-500/50 text-amber-300"
                                  : "bg-cyan-950 border border-cyan-800 text-cyan-300"
                              }`}
                            >
                              {t.context}
                            </span>
                            <span
                              className={`px-1.5 py-0.5 rounded text-[10px] font-mono ${
                                t.outcome === "TP_HIT"
                                  ? "bg-emerald-950 text-emerald-300 font-bold"
                                  : t.outcome === "STOPPED"
                                  ? "bg-rose-950 text-rose-300"
                                  : "bg-ink-800 text-slate-300"
                              }`}
                            >
                              {t.outcome === "TP_HIT" ? `TP HIT (+${t.r_achieved}R)` : t.outcome === "STOPPED" ? "-1.0R (SL)" : `OPEN (${t.r_achieved >= 0 ? "+" : ""}${t.r_achieved}R)`}
                            </span>
                          </div>
                        </div>

                        {/* Trade Risk / Reward Levels Strip */}
                        <div className="grid grid-cols-4 gap-1 p-1.5 rounded bg-ink-950/60 border border-ink-800 font-mono text-[10px] mt-1 text-center">
                          <div>
                            <span className="text-slate-500">Entry (Bar 4 Open): </span>
                            <span className="text-slate-200 font-bold">{t.bar4_open}</span>
                          </div>
                          <div>
                            <span className="text-slate-500">Stop Loss: </span>
                            <span className="text-rose-400 font-bold">{t.sl}</span>
                          </div>
                          <div>
                            <span className="text-slate-500">Target (1:2 R): </span>
                            <span className="text-emerald-400 font-bold">{t.tp1}</span>
                          </div>
                          <div>
                            <span className="text-slate-500">Max DD (MAE): </span>
                            <span className="text-amber-400">-{t.mae_pts} pts</span>
                          </div>
                        </div>

                        <div className="text-[10px] text-slate-400 mt-1 flex items-center justify-between">
                          <span>{t.instruction}</span>
                          <span className="text-slate-500 italic">{t.grade_desc}</span>
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </div>
          ) : (
            <div className="py-8 text-center text-slate-500">Loading Candle Continuity data...</div>
          )}
        </div>

        {/* COLUMN 2: MARKET MAKER MODELS (MMBM / MMSM) */}
        <div className="card flex flex-col p-3 border-ink-700/80 bg-ink-900/40">
          <div className="flex items-center justify-between border-b border-ink-800 pb-2 mb-2">
            <div className="flex items-center gap-2">
              <span className="text-sm font-bold text-gold-400">🏛️ Market Maker Model (MMXM)</span>
              <span className="text-[10px] bg-gold-950 text-gold-300 border border-gold-800 px-1.5 py-0.2 rounded font-mono">
                {primaryModel ? primaryModel.model : "SCANNING"}
              </span>
            </div>
            {primaryModel && (
              <span className="text-[11px] font-bold px-2 py-0.5 rounded bg-gold-950/80 border border-gold-500/50 text-gold-300">
                Phase: {primaryModel.phase.replace("_", " ")}
              </span>
            )}
          </div>

          {primaryModel ? (
            <div className="space-y-3 flex-1 overflow-auto">
              {/* Curve Stage Progress Flow */}
              <div>
                <div className="text-[10px] text-slate-400 uppercase tracking-wide mb-1">MMXM Curve Progress</div>
                <div className="grid grid-cols-6 gap-1">
                  {STAGES.map((s, idx) => {
                    const isDone =
                      s.id === primaryModel.phase ||
                      (primaryModel.phase === "TARGET_REACHED" && idx <= 5) ||
                      (primaryModel.phase === "REACCUMULATION" && idx <= 4) ||
                      (primaryModel.phase === "LOW_RISK_BUY" && idx <= 3) ||
                      (primaryModel.phase === "SMR_CONFIRMED" && idx <= 2);
                    const isCurrent = s.id === primaryModel.phase;
                    return (
                      <div
                        key={s.id}
                        className={`p-1.5 rounded text-center border transition ${
                          isCurrent
                            ? "bg-gold-500/20 border-gold-400 text-gold-300 font-bold"
                            : isDone
                            ? "bg-emerald-950/40 border-emerald-500/30 text-emerald-400"
                            : "bg-ink-800/40 border-ink-800 text-slate-600"
                        }`}
                      >
                        <div className="text-[9px] truncate">{s.name}</div>
                      </div>
                    );
                  })}
                </div>
              </div>

              {/* Model Summary & Narrative */}
              <div className="p-2.5 rounded bg-ink-800/60 border border-ink-700/80 text-[11px]">
                <div className="font-semibold text-slate-200 mb-1">{primaryModel.name}</div>
                <div className="text-slate-400 leading-relaxed">{primaryModel.summary}</div>
              </div>

              {/* Smart Money Reversal (SMR) with Clickable / Hoverable Confluences */}
              <div className="p-2.5 rounded bg-ink-800/40 border border-ink-700/60 space-y-2">
                <div className="flex items-center justify-between text-[11px]">
                  <div>
                    <span className="text-slate-400">SMR Pivot Extreme: </span>
                    <span className="font-mono font-bold text-slate-200">
                      {primaryModel.smr.price.toLocaleString("en-IN")} @ {primaryModel.smr.at}
                    </span>
                    <span className="ml-2 text-[10px] text-slate-500">(hollow dot)</span>
                  </div>
                  <div>
                    <span className="text-slate-400">MSS Confirmed: </span>
                    <span className="font-mono font-bold text-gold-300">
                      {primaryModel.mss.level.toLocaleString("en-IN")}
                    </span>
                    <span className="ml-2 text-[10px] text-slate-500">(solid label)</span>
                  </div>
                </div>

                {/* Clickable / Hoverable "4 Conf" Pill */}
                <div className="pt-1">
                  <div className="flex items-center justify-between mb-1">
                    <span className="text-[10px] text-slate-400 uppercase">SMR Confluence Checklist:</span>
                    <button
                      onClick={() => setShowConf(!showConf)}
                      className="text-[10px] text-gold-400 hover:text-gold-300 underline font-semibold cursor-pointer"
                    >
                      {showConf ? "Hide details ▲" : `Inspect ${primaryModel.smr.confluence_score} Confirmations ▼`}
                    </button>
                  </div>

                  <div className="flex flex-wrap gap-1">
                    {primaryModel.smr.confluences.map((c) => (
                      <span
                        key={c}
                        onClick={() => setShowConf(true)}
                        className="px-2 py-0.5 rounded text-[10px] font-semibold bg-gold-950 border border-gold-500/60 text-gold-300 cursor-pointer hover:bg-gold-900/80 transition"
                      >
                        ✓ {c}
                      </span>
                    ))}
                  </div>

                  {showConf && (
                    <div className="mt-2 p-2 rounded bg-ink-950 border border-gold-500/40 text-[10px] text-slate-300 space-y-1">
                      <div className="font-bold text-gold-400">How these {primaryModel.smr.confluence_score} confirmations were validated:</div>
                      {primaryModel.smr.confluences.map((c, i) => (
                        <div key={i} className="flex items-start gap-1">
                          <span className="text-gold-400 font-bold">•</span>
                          <span>
                            {c === "MSS Shift" && "Market Structure Shift confirmed with candle body close beyond previous swing."}
                            {c === "Displacement Candle" && "Forceful displacement bar with body ≥ 60% of total bar range."}
                            {c === "Displacement FVG" && "Institutional imbalance gap left behind on the reversal leg."}
                            {c === "HTF POI Key Level" && "Direct interaction with an active Higher Timeframe Fair Value Gap or Order Block."}
                            {c === "SMT Divergence" && "Divergence between NIFTY and BANKNIFTY confirming smart money accumulation."}
                          </span>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              </div>

              {/* Execution Zones & Target */}
              <div className="grid grid-cols-2 gap-2 text-[11px]">
                <div className="p-2 rounded bg-ink-800/50 border border-ink-700">
                  <div className="text-[10px] text-slate-400">Low-Risk Entry Zone (~25% Dealing Range)</div>
                  <div className="font-mono font-bold text-cyan-300">
                    {primaryModel.lre_zone[0]} – {primaryModel.lre_zone[1]}
                  </div>
                  <div className="text-[10px] text-slate-500 mt-0.5">SL: {primaryModel.sl} (behind SMR pivot)</div>
                </div>

                <div className="p-2 rounded bg-ink-800/50 border border-ink-700">
                  <div className="text-[10px] text-slate-400">Orig Cons Target (DOL)</div>
                  <div className="font-mono font-bold text-emerald-300">
                    {primaryModel.target.toLocaleString("en-IN")} (EQ {primaryModel.target_eq})
                  </div>
                  <div className="text-[10px] text-gold-400 mt-0.5">Potential RR: {primaryModel.r_potential}:1</div>
                </div>
              </div>
            </div>
          ) : (
            <div className="space-y-3 text-[11px] text-slate-400 p-2 flex-1 overflow-auto">
              <div className="p-3 rounded bg-ink-800/40 border border-ink-700 text-center">
                <div className="text-sm font-semibold text-slate-300 mb-1">Scanning for MMXM Cycles</div>
                <div>{mmxm?.message || "Analyzing chart for Original Consolidation & SMR..."}</div>
              </div>

              <div className="p-2.5 rounded border border-ink-800 bg-ink-900/60 text-[10px] space-y-1">
                <div className="font-bold text-gold-400 uppercase">Institutional MMXM Blueprint:</div>
                <div>• MMBM (Buy Model): Orig Cons at highs → Multi-leg liquidity engineering curve → SMR reversal (hollow dot at low, solid label on MSS break) → Low Risk Buy entry → Target back to Orig Cons.</div>
                <div>• MMSM (Sell Model): Orig Cons at lows → Multi-leg liquidity engineering curve → SMR reversal → Low Risk Sell entry → Target back to Orig Cons.</div>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
