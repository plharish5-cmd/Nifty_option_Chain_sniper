// AdvPaPanel.tsx — dedicated inspector for the 10 Advanced Price Action & Institutional Concepts.
import { useEffect, useState } from "react";
import type { PaAdvancedView, TF } from "../api";

export default function AdvPaPanel({ tick, chartTf }: { tick: number; chartTf?: TF }) {
  const [tf, setTf] = useState<TF>(chartTf || "5m");
  const [activeTab, setActiveTab] = useState<string>("all");
  const [d, setD] = useState<PaAdvancedView | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [sniperMode, setSniperMode] = useState<boolean>(() => {
    try {
      return localStorage.getItem("ict_adv_pa_mode") !== "raw";
    } catch {
      return true;
    }
  });

  const toggleSniperMode = () => {
    setSniperMode((prev) => {
      const next = !prev;
      try {
        localStorage.setItem("ict_adv_pa_mode", next ? "sniper" : "raw");
      } catch { /* ignore */ }
      return next;
    });
  };

  useEffect(() => {
    let dead = false;
    setLoading(true);
    fetch(`/api/pa-advanced?tf=${tf}`)
      .then((r) => r.json())
      .then((j) => {
        if (!dead) {
          setD(j);
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

  const n = (v: number | null | undefined) =>
    v == null ? "—" : v.toLocaleString("en-IN", { maximumFractionDigits: 1 });

  const gpx = d?.gpx;
  const traps = d?.traps || [];
  const msbs = d?.msb_quality || [];
  const dfs = d?.dragon_fruit || [];
  const mibs = d?.mitigation_blocks || [];
  const rotes = d?.rote || [];
  const sfps = d?.sfp_tests || [];
  const bases = d?.sd_bases || [];
  const ftrs = d?.ftr_zones || [];
  const tt3s = d?.tt3 || [];
  const bprs = d?.bpr_zones || [];
  const b20 = d?.bias_20 ?? d?.sniper?.bias_20;

  return (
    <div className="flex h-full flex-col text-[12px] bg-ink-950 text-slate-200">
      {/* Top Controls & GPX Banner */}
      <div className="flex flex-wrap items-center gap-3 border-b border-ink-700 px-3 py-2 bg-ink-900/60">
        <div className="flex items-center gap-1">
          <span className="text-[10px] uppercase font-bold text-slate-500 mr-1">Timeframe</span>
          {(["1m", "5m", "15m", "1h"] as TF[]).map((t) => (
            <button
              key={t}
              className={`tab text-xs px-2.5 py-0.5 ${t === tf ? "tab-on !border-pink-500 !text-pink-300 font-bold" : ""}`}
              onClick={() => setTf(t)}
            >
              {t}
            </button>
          ))}
          <button
            onClick={toggleSniperMode}
            title="Toggle Sniper Mode (Clean POIs + Synthesized Decision) vs Raw Diagnostic Mode"
            className={`ml-2 flex items-center gap-1 px-2 py-0.5 rounded text-[10px] font-bold border transition-colors ${
              sniperMode
                ? "bg-gold-500/20 text-gold-300 border-gold-500/60 hover:bg-gold-500/30"
                : "bg-purple-500/20 text-purple-300 border-purple-500/60 hover:bg-purple-500/30"
            }`}
          >
            <span>{sniperMode ? "🎯 Sniper Mode" : "🔬 Raw 10-Core"}</span>
          </button>
        </div>

        {/* View Filter Tabs */}
        <div className="flex items-center gap-1 ml-auto">
          {[
            ["all", "All 10 Concepts"],
            ["bias20", "⚖ Bias 20 Matrix"],
            ["msb", `MSBs (${msbs.length})`],
            ["df", `Dragon Fruit (${dfs.length})`],
            ["mib", `MiB (${mibs.length})`],
            ["rote", `ROTE (${rotes.length})`],
            ["sfp", `SFP / Gray (${sfps.length})`],
            ["bases", `S/D Bases (${bases.length})`],
            ["ftr", `FTR (${ftrs.length})`],
            ["tt3", `TT3 (${tt3s.length})`],
            ["bpr", `BPR (${bprs.length})`],
          ].map(([id, label]) => (
            <button
              key={id}
              onClick={() => setActiveTab(id)}
              className={`px-2 py-0.5 rounded text-[11px] transition-colors ${
                activeTab === id ? "bg-pink-600/30 border border-pink-500 text-pink-200 font-semibold" : "text-slate-400 hover:text-slate-200"
              }`}
            >
              {label}
            </button>
          ))}
        </div>
      </div>

      {/* 🎯 Sniper Decision & Confluence Cockpit Banner */}
      {d?.sniper && (
        <div className="px-3 py-2 bg-gradient-to-r from-ink-950 via-[#0a1020] to-ink-950 border-b border-gold-500/40 text-[12px]">
          <div className="flex flex-wrap items-center gap-2 mb-1.5">
            {(() => {
              const s = d.sniper;
              const isRejPe = s.action.includes("BUY PE") || s.state.includes("REJECTION") || s.state.includes("SHORT");
              const isRejCe = s.action.includes("BUY CE");
              const isBreak = s.state.includes("BREAKOUT") || s.state.includes("BREAKDOWN");
              const isTrap = s.state.includes("TRAP");
              const badgeClass = isTrap
                ? "bg-rose-950/90 text-rose-300 border-rose-600/70"
                : isBreak
                ? "bg-cyan-950/90 text-cyan-300 border-cyan-500/70"
                : isRejCe
                ? "bg-emerald-950/90 text-emerald-300 border-emerald-500/70"
                : isRejPe
                ? "bg-rose-950/90 text-rose-300 border-rose-500/70"
                : "bg-amber-950/90 text-amber-300 border-amber-500/70";
              const icon = isTrap ? "🚫" : isBreak ? "⚡" : isRejCe || isRejPe ? "🎯" : "⏳";
              return (
                <span className={`flex items-center gap-1 px-2 py-0.5 rounded font-extrabold text-[11px] border ${badgeClass}`}>
                  <span>{icon}</span>
                  <span>{s.state}</span>
                </span>
              );
            })()}

            <span className="px-2 py-0.5 rounded bg-slate-900 border border-slate-700/60 text-slate-300 font-semibold text-[11px]">
              🛡️ {d.sniper.behavior}
            </span>

            <span className="font-bold text-slate-100 text-[12px] ml-1">
              {d.sniper.headline}
            </span>
          </div>

          {/* Trade Plan & Top Confluence POIs */}
          <div className="grid gap-2 md:grid-cols-3 mt-2 text-[11px]">
            {d.sniper.trade_plan ? (
              <div className="p-2 rounded bg-ink-900/90 border border-gold-500/30 flex flex-col justify-between">
                <div className="flex items-center justify-between font-bold">
                  <span className={d.sniper.trade_plan.side === "LONG" ? "text-emerald-400" : "text-rose-400"}>
                    {d.sniper.trade_plan.side === "LONG" ? "▲ LONG SETUP" : "▼ SHORT SETUP"}
                  </span>
                  <span className="text-gold-300 font-mono">{d.sniper.trade_plan.strike}</span>
                </div>
                <div className="grid grid-cols-2 gap-x-2 gap-y-1 mt-1 text-slate-300">
                  <div>Entry: <b className="text-slate-100">{n(d.sniper.trade_plan.entry)}</b></div>
                  <div>SL: <b className="text-rose-400">{n(d.sniper.trade_plan.sl)}</b></div>
                  <div>1R BE: <b className="text-cyan-300">{n(d.sniper.trade_plan.be_1r)}</b></div>
                  <div>TP1: <b className="text-emerald-400">{n(d.sniper.trade_plan.tp1)}</b> {d.sniper.trade_plan.rr ? `(${d.sniper.trade_plan.rr}R)` : ""}</div>
                </div>
              </div>
            ) : (
              <div className="p-2 rounded bg-ink-900/60 border border-ink-800 text-slate-400 flex items-center justify-center">
                <span>⏳ No Trigger Active · Awaiting POI Tap</span>
              </div>
            )}

            {/* Nearest Supply POI */}
            <div className="p-2 rounded bg-rose-950/20 border border-rose-900/40 flex flex-col justify-between">
              <div className="flex items-center justify-between">
                <span className="font-bold text-rose-300">🎯 NEAREST SUPPLY POI</span>
                {d.sniper.best_supply && (
                  <span className="px-1.5 py-0.2 rounded bg-rose-900/60 text-rose-200 text-[10px] font-bold">
                    {d.sniper.best_supply.score.toFixed(1)}/10 ★
                  </span>
                )}
              </div>
              {d.sniper.best_supply ? (
                <div className="mt-1 text-slate-300">
                  <div className="font-semibold text-slate-100">
                    [{n(d.sniper.best_supply.bot)} – {n(d.sniper.best_supply.top)}] · {d.sniper.best_supply.grade}
                  </div>
                  <div className="text-[10px] text-rose-400/90 truncate mt-0.5">
                    Stack: {d.sniper.best_supply.tags.join(" + ")}
                  </div>
                </div>
              ) : (
                <div className="text-slate-500 text-[10px] mt-1">No active high-conviction supply above spot.</div>
              )}
            </div>

            {/* Nearest Demand POI */}
            <div className="p-2 rounded bg-emerald-950/20 border border-emerald-900/40 flex flex-col justify-between">
              <div className="flex items-center justify-between">
                <span className="font-bold text-emerald-300">🎯 NEAREST DEMAND POI</span>
                {d.sniper.best_demand && (
                  <span className="px-1.5 py-0.2 rounded bg-emerald-900/60 text-emerald-200 text-[10px] font-bold">
                    {d.sniper.best_demand.score.toFixed(1)}/10 ★
                  </span>
                )}
              </div>
              {d.sniper.best_demand ? (
                <div className="mt-1 text-slate-300">
                  <div className="font-semibold text-slate-100">
                    [{n(d.sniper.best_demand.bot)} – {n(d.sniper.best_demand.top)}] · {d.sniper.best_demand.grade}
                  </div>
                  <div className="text-[10px] text-emerald-400/90 truncate mt-0.5">
                    Stack: {d.sniper.best_demand.tags.join(" + ")}
                  </div>
                </div>
              ) : (
                <div className="text-slate-500 text-[10px] mt-1">No active high-conviction demand below spot.</div>
              )}
            </div>
          </div>
        </div>
      )}

      {/* Option Strike Selection & Greeks Filter (BUY CE / BUY PE Scanner) */}
      {d?.strikes && (d.strikes.CE || d.strikes.PE) && (
        <div className="flex flex-wrap items-center justify-between gap-3 px-3 py-1.5 bg-ink-900 border-b border-ink-700 text-[11px]">
          <div className="flex items-center gap-2">
            <span className="px-1.5 py-0.5 rounded bg-gold-950/80 text-gold-400 font-bold text-[10px] border border-gold-700/60">
              🎯 OPTION STRIKE SCANNER
            </span>
            {d.strikes.CE && (
              <div className="flex items-center gap-1.5 px-2 py-0.5 rounded bg-bull-950/40 border border-bull-800/60">
                <span className="font-bold text-bull-400">BUY CE:</span>
                <span className="font-semibold text-slate-100">{d.strikes.CE.label || `${d.strikes.CE.strike} CE`}</span>
                {d.strikes.CE.ltp != null && <span className="text-slate-300">₹{n(d.strikes.CE.ltp)}</span>}
                {d.strikes.CE.delta != null && <span className="text-bull-400">Δ {d.strikes.CE.delta > 0 ? "+" : ""}{d.strikes.CE.delta.toFixed(2)}</span>}
                {d.strikes.CE.theta != null && <span className="text-slate-400">θ {d.strikes.CE.theta.toFixed(1)}</span>}
                {d.strikes.CE.iv != null && <span className="text-amber-400 font-mono">IV {d.strikes.CE.iv.toFixed(1)}%</span>}
                {d.strikes.CE.focus?.kind && <span className="text-slate-400 text-[10px]">({d.strikes.CE.focus.kind})</span>}
              </div>
            )}
            {d.strikes.PE && (
              <div className="flex items-center gap-1.5 px-2 py-0.5 rounded bg-bear-950/40 border border-bear-800/60">
                <span className="font-bold text-bear-400">BUY PE:</span>
                <span className="font-semibold text-slate-100">{d.strikes.PE.label || `${d.strikes.PE.strike} PE`}</span>
                {d.strikes.PE.ltp != null && <span className="text-slate-300">₹{n(d.strikes.PE.ltp)}</span>}
                {d.strikes.PE.delta != null && <span className="text-bear-400">Δ {d.strikes.PE.delta.toFixed(2)}</span>}
                {d.strikes.PE.theta != null && <span className="text-slate-400">θ {d.strikes.PE.theta.toFixed(1)}</span>}
                {d.strikes.PE.iv != null && <span className="text-amber-400 font-mono">IV {d.strikes.PE.iv.toFixed(1)}%</span>}
                {d.strikes.PE.focus?.kind && <span className="text-slate-400 text-[10px]">({d.strikes.PE.focus.kind})</span>}
              </div>
            )}
          </div>
          <span className="text-slate-500 text-[10px]">Picks ATM / 1-ITM strike · |Δ| 0.50–0.65 · spread ≤ 0.6% · StrikeBook stability</span>
        </div>
      )}
      {gpx && (
        <div className="flex flex-wrap items-center justify-between gap-3 px-3 py-1.5 bg-purple-950/20 border-b border-purple-900/30 text-[11px]">
          <div className="flex items-center gap-2">
            <span className="px-1.5 py-0.5 rounded bg-purple-900/60 text-purple-300 font-bold text-[10px]">
              GPX {gpx.htf} CORRIDOR
            </span>
            <span>
              Floor: <b className="text-emerald-400">{n(gpx.corridor_low)}</b> ↔ Ceiling:{" "}
              <b className="text-rose-400">{n(gpx.corridor_high)}</b>
            </span>
            <span className="text-slate-500">|</span>
            <span>
              EQ: <b className="text-purple-300">{n(gpx.corridor_mid)}</b>
            </span>
            <span
              className={`px-1.5 py-0.5 rounded font-semibold ${
                gpx.zone === "PREMIUM" ? "bg-rose-950/80 text-rose-300 border border-rose-800/60" : "bg-emerald-950/80 text-emerald-300 border border-emerald-800/60"
              }`}
            >
              Zone: {gpx.zone}
            </span>
            {gpx.vwap_val && (
              <span className="text-slate-400">
                Session VWAP: <b className="text-cyan-300">{n(gpx.vwap_val)}</b> ({gpx.vwap_state})
              </span>
            )}
          </div>

          <div className="flex items-center gap-2">
            {gpx.veto_short && (
              <span className="px-2 py-0.5 rounded bg-amber-950/90 border border-amber-600/80 text-amber-300 font-bold text-[10px]">
                ⚠ VETO SHORT (Discount / Untouched Demand)
              </span>
            )}
            {gpx.veto_long && (
              <span className="px-2 py-0.5 rounded bg-amber-950/90 border border-amber-600/80 text-amber-300 font-bold text-[10px]">
                ⚠ VETO LONG (Premium / Untouched Supply)
              </span>
            )}
          </div>
        </div>
      )}

      {/* Traps & Inducements Warnings */}
      {traps.length > 0 && (
        <div className="px-3 py-1.5 bg-rose-950/30 border-b border-rose-800/40 text-[11px] flex flex-col gap-1">
          {traps.map((tr, idx) => (
            <div key={idx} className="flex items-center gap-2 text-rose-300 font-medium">
              <span>🛑</span>
              <span>{tr.warning}</span>
              <span className="ml-auto text-rose-400/80 text-[10px] font-mono">
                Distance: {tr.distance_atr.toFixed(2)} ATR
              </span>
            </div>
          ))}
        </div>
      )}

      {/* Main Content Area */}
      <div className="min-h-0 flex-1 overflow-auto p-3">
        {loading && <div className="text-center py-8 text-slate-500">Loading Advanced PA Analysis...</div>}

        {!loading && d && d.available === false && (
          <div className="text-center py-8 text-slate-500">{d.message || "No data available."}</div>
        )}

        {!loading && d && d.available && (
          <div className="space-y-4">
            {/* ⚖ Bias 20 Confluence Engine & Discrete Sniper Weights */}
            {(activeTab === "all" || activeTab === "bias20") && b20 && (
              <div className="rounded-lg border border-gold-500/40 bg-gradient-to-b from-ink-900/90 to-ink-950/90 p-3">
                <div className="flex flex-wrap items-center justify-between gap-2 pb-2 mb-2 border-b border-ink-800">
                  <div className="flex items-center gap-2">
                    <span className="font-extrabold text-[13px] text-gold-300">
                      ⚖ Bias 20 Confluence Engine & Discrete Sniper Weights
                    </span>
                    <span className="px-2 py-0.5 rounded bg-gold-950/80 border border-gold-600/60 text-gold-300 font-mono text-[11px] font-bold">
                      {b20.bull_score.toFixed(1)}▲ / {b20.bear_score.toFixed(1)}▼ of 20.0
                    </span>
                  </div>
                  <div className="flex items-center gap-2">
                    <span className={`px-2 py-0.5 rounded font-extrabold text-[11px] border ${
                      b20.verdict === "BUY"
                        ? "bg-emerald-950/90 text-emerald-300 border-emerald-500/70"
                        : b20.verdict === "SELL"
                        ? "bg-rose-950/90 text-rose-300 border-rose-500/70"
                        : "bg-ink-800 text-slate-300 border-ink-700"
                    }`}>
                      {b20.verdict === "BUY" ? "🎯 BUY BIAS" : b20.verdict === "SELL" ? "🎯 SELL BIAS" : "⏳ MIXED / CHOP"} · {b20.confidence_pct}% Conf
                    </span>
                    <span className="text-[10px] text-slate-400 font-mono">
                      Net: {b20.net_score > 0 ? `+${b20.net_score}` : b20.net_score}
                    </span>
                  </div>
                </div>

                {/* 6 Discrete Sniper Triggers */}
                <div className="grid gap-2 grid-cols-2 md:grid-cols-3 lg:grid-cols-6 mb-3">
                  {[
                    { key: "IFVG", label: "IFVG", weight: "±0.5" },
                    { key: "RJB+", label: "RJB+", weight: "±0.5" },
                    { key: "CRT", label: "CRT", weight: "±1.0" },
                    { key: "DOL", label: "D.O.L", weight: "+1.0" },
                    { key: "HTF_Wick", label: "HTF Wick", weight: "±0.75" },
                    { key: "APlus", label: "★ A+", weight: "+1.5" },
                  ].map(({ key, label, weight }) => {
                    const trig = b20.triggers?.[key];
                    const status = trig?.status || "NEUTRAL";
                    const isAct = status === "ACTIVE";
                    const isVeto = status === "VETOED";
                    const borderCls = isVeto
                      ? "border-rose-600/70 bg-rose-950/30 text-rose-200"
                      : isAct
                      ? "border-emerald-500/70 bg-emerald-950/30 text-emerald-200"
                      : "border-ink-800 bg-ink-900/40 text-slate-400";
                    return (
                      <div key={key} className={`rounded border p-2 flex flex-col justify-between ${borderCls}`}>
                        <div className="flex items-center justify-between font-bold text-[11px]">
                          <span>{label}</span>
                          <span className="font-mono text-[10px] opacity-80">{weight}</span>
                        </div>
                        <div className="mt-1 flex items-center justify-between">
                          <span className={`text-[10px] font-extrabold uppercase px-1 rounded ${
                            isVeto ? "bg-rose-900/80 text-rose-200" : isAct ? "bg-emerald-900/80 text-emerald-200" : "bg-ink-800 text-slate-400"
                          }`}>
                            {isVeto ? "VETOED" : isAct ? `${trig?.dir === "bull" ? "▲ BULL" : trig?.dir === "bear" ? "▼ BEAR" : "ACTIVE"}` : "NEUTRAL"}
                          </span>
                        </div>
                        <div className="text-[9.5px] opacity-75 mt-1 truncate" title={trig?.desc || "No trigger"}>
                          {trig?.desc || "—"}
                        </div>
                      </div>
                    );
                  })}
                </div>

                {/* 20 Factors Explicit Table (Available in bias20 tab) */}
                {activeTab === "bias20" && b20.factors_20 && (
                  <div className="mt-2 border-t border-ink-800 pt-2">
                    <div className="font-semibold text-slate-300 text-[11px] mb-1">
                      Explicit 20-Factor Vector Breakdown (Factors 1 to 20):
                    </div>
                    <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-5 gap-1.5 text-[10px]">
                      {b20.factors_20.map((f) => (
                        <div key={f.id} className="rounded bg-ink-900/70 border border-ink-800 p-1.5 flex flex-col justify-between">
                          <div className="flex items-center justify-between font-medium">
                            <span className="text-slate-400">#{f.id} {f.name}</span>
                            <span className="font-mono text-slate-500">{f.weight ? `${f.weight > 0 ? "+" : ""}${f.weight}` : "1.0"}</span>
                          </div>
                          <div className="mt-1 flex items-center justify-between">
                            <span className={`px-1 rounded font-bold uppercase text-[9px] ${
                              f.vote === "bull" ? "bg-emerald-950 text-emerald-300 border border-emerald-800" :
                              f.vote === "bear" ? "bg-rose-950 text-rose-300 border border-rose-800" : "bg-ink-850 text-slate-500"
                            }`}>
                              {f.vote === "bull" ? "▲ BULL" : f.vote === "bear" ? "▼ BEAR" : "NEUTRAL"}
                            </span>
                          </div>
                          <div className="text-[9px] text-slate-500 truncate mt-0.5" title={f.desc}>
                            {f.desc}
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            )}

            {/* 1. Strong vs. Weak MSB (7-Point Scorer) */}
            {(activeTab === "all" || activeTab === "msb") && (
              <div>
                <div className="flex items-center justify-between pb-1 mb-2 border-b border-ink-800">
                  <span className="font-bold text-slate-200">
                    1. Strong vs. Weak MSB & 7-Point Quality Scorer
                  </span>
                  <span className="text-[10px] text-slate-500">
                    True origin LH (deepest LL) / HL (deepest HH)
                  </span>
                </div>
                {msbs.length === 0 ? (
                  <div className="text-slate-500 text-[11px] py-1">No confirmed MSBs on this timeframe.</div>
                ) : (
                  <div className="grid gap-2 md:grid-cols-2">
                    {msbs.map((m, idx) => (
                      <div
                        key={idx}
                        className={`rounded-lg border p-2.5 ${
                          m.strong ? "border-emerald-500/60 bg-emerald-950/10" : "border-ink-700 bg-ink-900/40"
                        }`}
                      >
                        <div className="flex items-center gap-2">
                          <span
                            className={`px-1.5 py-0.5 rounded font-bold text-[10px] ${
                              m.strong ? "bg-emerald-500 text-black" : "bg-amber-500/20 text-amber-300 border border-amber-500/40"
                            }`}
                          >
                            {m.strong ? "STRONG MSB" : `WEAK MSB (${m.weak_type})`}
                          </span>
                          <b className={m.dir === "bull" ? "text-emerald-400" : "text-rose-400"}>
                            {m.dir.toUpperCase()} @ {n(m.level)}
                          </b>
                          <span className="ml-auto num text-[10px] text-slate-400">{m.time_str}</span>
                        </div>

                        <div className="mt-1.5 flex items-center justify-between text-[11px] text-slate-300">
                          <span>
                            Origin: <b>{n(m.origin_swing_price)}</b>
                          </span>
                          <span>
                            Deepest Extreme: <b>{n(m.extreme_swing_price)}</b>
                          </span>
                          <span>
                            Close: <b>{n(m.close_price)}</b>
                          </span>
                          <span className="px-1.5 py-0.5 rounded bg-ink-800 font-bold text-gold-300">
                            Score: {m.score}/7
                          </span>
                        </div>

                        {/* 7 Checkpoints */}
                        <div className="mt-2 grid grid-cols-4 gap-1 text-[10px]">
                          {Object.entries(m.scorecard).map(([k, pass]) => (
                            <span
                              key={k}
                              className={`px-1 py-0.5 rounded text-center truncate ${
                                pass ? "bg-emerald-950/60 text-emerald-300 border border-emerald-800/40" : "bg-ink-850 text-slate-500"
                              }`}
                              title={k}
                            >
                              {pass ? "✓ " : "✗ "}
                              {k.replace(/_/g, " ")}
                            </span>
                          ))}
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}

            {/* 2. Dragon Fruit Confluence Detector */}
            {(activeTab === "all" || activeTab === "df") && (
              <div>
                <div className="flex items-center justify-between pb-1 mb-2 border-b border-ink-800">
                  <span className="font-bold text-slate-200">
                    2. Dragon Fruit Confluence Detector (OB + FVG Touching)
                  </span>
                  <span className="text-[10px] text-slate-500">
                    Touching = Lowest Risk Sweet Spot (A+)
                  </span>
                </div>
                {dfs.length === 0 ? (
                  <div className="text-slate-500 text-[11px] py-1">No displacement OB + FVG clusters.</div>
                ) : (
                  <div className="grid gap-2 md:grid-cols-3">
                    {dfs.map((df_z, idx) => (
                      <div
                        key={idx}
                        className={`rounded-lg border p-2.5 ${
                          df_z.risk === "LOW" ? "border-pink-500 bg-pink-950/20" : "border-ink-700 bg-ink-900/30"
                        }`}
                      >
                        <div className="flex items-center justify-between">
                          <span
                            className={`px-1.5 py-0.5 rounded font-bold text-[10px] ${
                              df_z.risk === "LOW" ? "bg-pink-600 text-white" : df_z.risk === "MID" ? "bg-purple-900 text-purple-300" : "bg-ink-800 text-slate-400"
                            }`}
                          >
                            {df_z.label}
                          </span>
                          <span className="text-[10px] text-slate-400">{df_z.time_str}</span>
                        </div>
                        <div className="mt-1.5 text-[11px] flex justify-between">
                          <span>
                            Bounds: <b>{n(df_z.bottom)} – {n(df_z.top)}</b>
                          </span>
                          <span className="text-pink-300 font-mono">Gap: {df_z.gap_pts} pts</span>
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}

            {/* 3. Mitigation Blocks */}
            {(activeTab === "all" || activeTab === "mib") && (
              <div>
                <div className="flex items-center justify-between pb-1 mb-2 border-b border-ink-800">
                  <span className="font-bold text-slate-200">
                    3. Mitigation Block (MiB) Engine
                  </span>
                  <span className="text-[10px] text-slate-500">
                    Failure swing (no liquidity sweep) broken by body close
                  </span>
                </div>
                {mibs.length === 0 ? (
                  <div className="text-slate-500 text-[11px] py-1">No failure swing Mitigation Blocks detected.</div>
                ) : (
                  <div className="grid gap-2 md:grid-cols-3">
                    {mibs.map((mib, idx) => (
                      <div key={idx} className="rounded-lg border border-ink-700 bg-ink-900/30 p-2.5">
                        <div className="flex items-center justify-between">
                          <span
                            className={`px-1.5 py-0.5 rounded font-bold text-[10px] ${
                              mib.dir === "bear" ? "bg-rose-950 text-rose-300 border border-rose-800/60" : "bg-emerald-950 text-emerald-300 border border-emerald-800/60"
                            }`}
                          >
                            MiB {mib.dir === "bear" ? "Supply (Failed HH)" : "Demand (Failed LL)"}
                          </span>
                          <span className={`text-[10px] ${mib.mitigated ? "text-slate-500 line-through" : "text-emerald-400 font-semibold"}`}>
                            {mib.mitigated ? "Mitigated" : "Fresh"}
                          </span>
                        </div>
                        <div className="mt-1 text-[11px]">
                          Zone: <b>{n(mib.bottom)} – {n(mib.top)}</b>
                        </div>
                        <div className="text-[10px] text-slate-400 mt-0.5">
                          Failed Swing: {n(mib.fail_swing_price)} | Broken Interim: {n(mib.interim_swing_price)}
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}

            {/* 4. ROTE (Reverse Optimal Trade Entry) */}
            {(activeTab === "all" || activeTab === "rote") && (
              <div>
                <div className="flex items-center justify-between pb-1 mb-2 border-b border-ink-800">
                  <span className="font-bold text-slate-200">
                    4. ROTE — Reverse Optimal Trade Entry (Counter-Bot Setup)
                  </span>
                  <span className="text-[10px] text-slate-500">
                    Target Magnet First → Opposing Sweep → 0.50 EQ Crossed → 0.70-0.80 Retracement
                  </span>
                </div>
                {rotes.length === 0 ? (
                  <div className="text-slate-500 text-[11px] py-1">No active ROTE algorithmic structures.</div>
                ) : (
                  <div className="grid gap-2 md:grid-cols-2">
                    {rotes.map((r, idx) => (
                      <div
                        key={idx}
                        className={`rounded-lg border p-2.5 ${
                          r.status === "TRIGGERED" ? "border-gold-500 bg-gold-950/20" : r.status === "IN_ZONE" ? "border-cyan-500 bg-cyan-950/20" : "border-ink-700 bg-ink-900/40"
                        }`}
                      >
                        <div className="flex items-center justify-between">
                          <b className={r.dir === "bull" ? "text-emerald-400" : "text-rose-400"}>
                            {r.dir.toUpperCase()} ROTE ({r.target_type} Target @ {n(r.target_price)})
                          </b>
                          <span className="px-1.5 py-0.5 rounded bg-gold-500/20 text-gold-300 font-bold text-[10px]">
                            {r.status}
                          </span>
                        </div>
                        <div className="mt-1 text-[11px] grid grid-cols-4 gap-1 text-slate-300">
                          <span>Sweep: <b>{n(r.sweep_price)}</b></span>
                          <span>EQ 0.50: <b>{n(r.eq_050)}</b></span>
                          <span className="text-cyan-300">Entry: <b>{n(r.entry_low)}–{n(r.entry_high)}</b></span>
                          <span className="text-rose-400">SL: <b>{n(r.sl)}</b></span>
                        </div>
                        <div className="mt-1 text-[10px] text-slate-400 flex justify-between">
                          <span>TP1 (0.20-0.30): {n(r.tp1_low)}–{n(r.tp1_high)}</span>
                          <span className="text-emerald-400 font-semibold">Final TP2: {n(r.tp2)}</span>
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}

            {/* 5. Pure SFP vs. cSFP vs. Gray Accumulation */}
            {(activeTab === "all" || activeTab === "sfp") && (
              <div>
                <div className="flex items-center justify-between pb-1 mb-2 border-b border-ink-800">
                  <span className="font-bold text-slate-200">
                    5. Pure SFP vs. cSFP vs. Gray Accumulation
                  </span>
                  <span className="text-[10px] text-slate-500">
                    Gray Accumulation = 3+ bars hovering: No-Trade Veto
                  </span>
                </div>
                {sfps.length === 0 ? (
                  <div className="text-slate-500 text-[11px] py-1">No swing test classifications in recent bars.</div>
                ) : (
                  <div className="grid gap-2 md:grid-cols-4">
                    {sfps.map((s, idx) => (
                      <div key={idx} className="rounded-lg border border-ink-700 bg-ink-900/30 p-2">
                        <div className="flex items-center justify-between">
                          <span
                            className={`px-1.5 py-0.5 rounded text-[10px] font-bold ${
                              s.state === "PURE_SFP" ? "bg-cyan-500 text-black" : s.state === "CSFP" ? "bg-purple-500 text-white" : "bg-amber-500 text-black"
                            }`}
                          >
                            {s.state}
                          </span>
                          <span className="text-[10px] text-slate-400">{s.time_str}</span>
                        </div>
                        <div className="mt-1 text-[11px]">
                          Swing {s.kind}: <b>{n(s.swing_price)}</b>
                        </div>
                        <div className="text-[10px] text-slate-400">
                          Pierce: {s.pierce_atr.toFixed(2)} ATR {s.bars_hovered > 1 ? `· Hover: ${s.bars_hovered} bars` : ""}
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}

            {/* 6. S/D Bases & FTR & TT3 */}
            {(activeTab === "all" || activeTab === "bases" || activeTab === "ftr" || activeTab === "tt3") && (
              <div className="grid gap-3 md:grid-cols-3">
                {/* S/D Bases */}
                <div className="rounded-lg border border-ink-800 bg-ink-900/20 p-2.5">
                  <div className="font-bold text-slate-200 border-b border-ink-800 pb-1 mb-2">
                    6. S/D Bases (RBR/DBD/RBD/DBR)
                  </div>
                  {bases.length === 0 ? (
                    <div className="text-slate-500 text-[11px]">No institutional bases detected.</div>
                  ) : (
                    <div className="space-y-1.5">
                      {bases.slice(-3).map((b_z, idx) => (
                        <div key={idx} className="text-[11px] p-1.5 rounded bg-ink-850 flex justify-between">
                          <span>
                            <b>{b_z.kind}</b> ({b_z.polarity}) [{n(b_z.bottom)}–{n(b_z.top)}]
                          </span>
                          <span className="text-gold-400 font-mono">Q:{b_z.score.toFixed(1)}</span>
                        </div>
                      ))}
                    </div>
                  )}
                </div>

                {/* FTR Bagholder Zones */}
                <div className="rounded-lg border border-ink-800 bg-ink-900/20 p-2.5">
                  <div className="font-bold text-slate-200 border-b border-ink-800 pb-1 mb-2">
                    7. FTR / IC "Bagholder" Zones
                  </div>
                  {ftrs.length === 0 ? (
                    <div className="text-slate-500 text-[11px]">No FTR pause zones.</div>
                  ) : (
                    <div className="space-y-1.5">
                      {ftrs.slice(-3).map((f_z, idx) => (
                        <div key={idx} className="text-[11px] p-1.5 rounded bg-ink-850 flex justify-between">
                          <span>
                            <b>{f_z.dir.toUpperCase()} FTR</b> [{n(f_z.bottom)}–{n(f_z.top)}]
                          </span>
                          <span className={f_z.ftb_tested ? "text-amber-400" : "text-emerald-400"}>
                            {f_z.ftb_tested ? "FTB Tested" : "Fresh"}
                          </span>
                        </div>
                      ))}
                    </div>
                  )}
                </div>

                {/* TT3 Range Setups */}
                <div className="rounded-lg border border-ink-800 bg-ink-900/20 p-2.5">
                  <div className="font-bold text-slate-200 border-b border-ink-800 pb-1 mb-2">
                    8. TT3 (Three Tap Range Setups)
                  </div>
                  {tt3s.length === 0 ? (
                    <div className="text-slate-500 text-[11px]">No Three Tap setups active.</div>
                  ) : (
                    <div className="space-y-1.5">
                      {tt3s.slice(-2).map((t, idx) => (
                        <div key={idx} className="text-[11px] p-1.5 rounded bg-ink-850 flex flex-col gap-0.5">
                          <div className="flex justify-between">
                            <b className={t.side === "bull" ? "text-emerald-400" : "text-rose-400"}>
                              TT3 {t.side.toUpperCase()}
                            </b>
                            <span className="px-1 rounded bg-gold-500/20 text-gold-300 font-bold text-[10px]">
                              {t.status}
                            </span>
                          </div>
                          <div className="text-[10px] text-slate-400">
                            Hidden: {n(t.hidden_zone_bot)}–{n(t.hidden_zone_top)} | SL: {n(t.sl)}
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>

                {/* BPR & Super BPR Zones */}
                <div className="rounded-lg border border-teal-800/80 bg-teal-950/20 p-2.5">
                  <div className="font-bold text-teal-300 border-b border-teal-900/80 pb-1 mb-2 flex items-center justify-between">
                    <span>9. BPR & Super BPR (Ch. 24)</span>
                    <span className="text-[10px] text-teal-400 font-normal">S/R Flips</span>
                  </div>
                  {bprs.length === 0 ? (
                    <div className="text-slate-500 text-[11px]">No active BPR collisions.</div>
                  ) : (
                    <div className="space-y-1.5">
                      {bprs.slice(-3).map((b, idx) => (
                        <div key={idx} className="text-[11px] p-1.5 rounded bg-ink-850 flex justify-between items-center border border-teal-900/40">
                          <div>
                            <b className={b.kind === "SUPER BPR" ? "text-teal-300 font-extrabold" : "text-teal-400"}>{b.kind}</b>
                            <span className="text-slate-300 ml-1.5">[{n(b.bot)}–{n(b.top)}]</span>
                          </div>
                          <span className="text-[10px] text-slate-400 font-mono">{b.hm}</span>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
