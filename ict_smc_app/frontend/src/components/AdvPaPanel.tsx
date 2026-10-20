// AdvPaPanel.tsx — dedicated inspector for the 10 Advanced Price Action & Institutional Concepts.
import { useEffect, useState } from "react";
import type { PaAdvancedView, TF } from "../api";

export default function AdvPaPanel({ tick, chartTf }: { tick: number; chartTf?: TF }) {
  const [tf, setTf] = useState<TF>(chartTf || "5m");
  const [activeTab, setActiveTab] = useState<string>("all");
  const [d, setD] = useState<PaAdvancedView | null>(null);
  const [loading, setLoading] = useState<boolean>(true);

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
        </div>

        {/* View Filter Tabs */}
        <div className="flex items-center gap-1 ml-auto">
          {[
            ["all", "All 10 Concepts"],
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
