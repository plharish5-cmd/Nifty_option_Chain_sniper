// PaCoursePanel.tsx — Dedicated inspector for the Institutional Price Action Course Engine (pa_course_engine.py).
import { useEffect, useState } from "react";
import type { TF } from "../api";

export interface PaCourseView {
  tf: string;
  available: boolean;
  spot: number;
  atr: number;
  signal: string;
  reason: string;
  gpx_box: {
    range_high: number;
    range_eq: number;
    range_low: number;
    zone: "PREMIUM" | "DISCOUNT";
    ote_short_zone: [number, number];
    ote_long_zone: [number, number];
  };
  msb_setups: {
    dir: "BULL" | "BEAR";
    kind: string;
    ts: number;
    hm: string;
    time: number;
    break_level: number;
    entry_zone: { top: number; bot: number };
    ob: { top: number; bot: number; mid: number; touches: number };
    dragon_fruit: boolean;
    super_ob_score: number;
    super_ob_checks: Record<string, boolean>;
    trap_warning?: string | null;
  }[];
  sfp_events: {
    kind: string;
    side: string;
    level: number;
    sweep_high?: number;
    sweep_low?: number;
    ts: number;
    hm: string;
    note: string;
  }[];
  mib_zones: {
    kind: string;
    side: string;
    top: number;
    bot: number;
    bos_hm: string;
    note: string;
  }[];
  rote_setups: {
    side: string;
    badge: string;
    sweep_extreme: number;
    target_liquidity: number;
    eq_50: number;
    ote_zone: [number, number];
    sl: number;
    tp1_partial: number;
    tp2_final: number;
    strike: string;
    rule: string;
  }[];
  ftr_zones: {
    kind: string;
    side: string;
    top: number;
    bot: number;
    hm: string;
    status: string;
  }[];
  eq_pools: {
    kind: "EQH" | "EQL";
    price: number;
    i1: number;
    i2: number;
    ts: number;
    swept: boolean;
  }[];
  bpr_zones?: {
    kind: "BPR" | "SUPER BPR" | string;
    top: number;
    bot: number;
    mid: number;
    bull_fvg_i: number;
    bear_fvg_i: number;
    ts: number;
    hm: string;
    overlaps: number;
    label: string;
  }[];
  sbb_setups?: {
    kind: string;
    dir: "BULL" | "BEAR" | string;
    side: "LONG" | "SHORT" | string;
    top: number;
    bot: number;
    break_level: number;
    ts: number;
    hm: string;
    is_super: boolean;
    sfp_confirmed: boolean;
    fvg_touch: boolean;
    in_aligned_zone: boolean;
    label: string;
  }[];
  trade_management?: {
    entry?: number | null;
    sl?: number | null;
    r_dist?: number | null;
    be_1r?: number | null;
    directive: string;
  } | null;
  chain_metrics: {
    available: boolean;
    pcr?: number | null;
    max_pain?: number | null;
    call_wall: number;
    put_wall: number;
    best_ce: { strike: number; ltp?: number | null; delta?: number | null; iv?: number | null };
    best_pe: { strike: number; ltp?: number | null; delta?: number | null; iv?: number | null };
    highlighted_strikes: number[];
  };
  config: Record<string, any>;
}

export default function PaCoursePanel({ tick, chartTf }: { tick: number; chartTf?: TF }) {
  const [tf, setTf] = useState<TF>(chartTf || "5m");
  const [activeTab, setActiveTab] = useState<string>("all");
  const [d, setD] = useState<PaCourseView | null>(null);
  const [loading, setLoading] = useState<boolean>(true);

  useEffect(() => {
    let dead = false;
    setLoading(true);
    fetch(`/api/pa-course?tf=${tf}`)
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

  const sig = d?.signal || "NO TRADE";
  const isBuyCe = sig.startsWith("BUY CE");
  const isBuyPe = sig.startsWith("BUY PE");
  const isTrap = sig.includes("TRAP VETO");

  return (
    <div className="flex h-full flex-col text-[12px] bg-ink-950 text-slate-200">
      {/* Top Bar: TF + Master Directive + Filter Tabs */}
      <div className="flex flex-wrap items-center gap-3 border-b border-ink-700 px-3 py-2 bg-ink-900/70">
        <div className="flex items-center gap-1">
          <span className="text-[10px] uppercase font-bold text-slate-500 mr-1">Timeframe</span>
          {(["1m", "5m", "15m", "1h"] as TF[]).map((t) => (
            <button
              key={t}
              className={`tab text-xs px-2.5 py-0.5 ${t === tf ? "tab-on !border-gold-500 !text-gold-300 font-bold" : ""}`}
              onClick={() => setTf(t)}
            >
              {t}
            </button>
          ))}
        </div>

        {/* Actionable Signal Pill & 1R Risk Management Directive */}
        <div className="flex flex-col gap-0.5">
          <div className="flex items-center gap-2">
            <span
              className={`px-2.5 py-1 rounded font-bold text-xs shadow-sm ${
                isBuyCe
                  ? "bg-emerald-900/80 text-emerald-200 border border-emerald-500 animate-pulse"
                  : isBuyPe
                  ? "bg-rose-900/80 text-rose-200 border border-rose-500 animate-pulse"
                  : isTrap
                  ? "bg-amber-900/80 text-amber-200 border border-amber-500"
                  : "bg-ink-800 text-slate-400 border border-ink-700"
              }`}
            >
              {sig}
            </span>
            <span className="text-slate-400 text-[11px] truncate max-w-[420px]" title={d?.reason}>
              {d?.reason}
            </span>
          </div>
          {d?.trade_management && (
            <div className="text-[10px] text-amber-300 font-medium">
              ⚡ {d.trade_management.directive}
            </div>
          )}
        </div>

        {/* Tab Filters */}
        <div className="flex items-center gap-1 ml-auto">
          {[
            ["all", "All"],
            ["msb", `MSBs (${d?.msb_setups?.length || 0})`],
            ["bpr", `BPR (${d?.bpr_zones?.length || 0})`],
            ["sbb", `SBB (${d?.sbb_setups?.length || 0})`],
            ["rote", `ROTE (${d?.rote_setups?.length || 0})`],
            ["sfp", `SFP (${d?.sfp_events?.length || 0})`],
            ["mib", `MiB (${d?.mib_zones?.length || 0})`],
            ["ftr", `FTR (${d?.ftr_zones?.length || 0})`],
            ["chain", "Dhan Chain"],
          ].map(([id, label]) => (
            <button
              key={id}
              onClick={() => setActiveTab(id)}
              className={`px-2 py-0.5 rounded text-[11px] transition-colors ${
                activeTab === id ? "bg-gold-600/30 border border-gold-500 text-gold-200 font-semibold" : "text-slate-400 hover:text-slate-200"
              }`}
            >
              {label}
            </button>
          ))}
        </div>
      </div>

      {/* GPX Box & Dhan Chain Metrics Bar */}
      {d?.gpx_box && (
        <div className="flex flex-wrap items-center justify-between gap-3 px-3 py-1.5 bg-ink-900/40 border-b border-ink-800 text-[11px]">
          <div className="flex items-center gap-2">
            <span className="px-1.5 py-0.5 rounded bg-purple-900/60 text-purple-300 font-bold text-[10px]">
              GPX BOX
            </span>
            <span>
              Low: <b className="text-emerald-400">{n(d.gpx_box.range_low)}</b> ↔ High:{" "}
              <b className="text-rose-400">{n(d.gpx_box.range_high)}</b>
            </span>
            <span className="text-slate-500">|</span>
            <span>
              EQ: <b className="text-purple-300">{n(d.gpx_box.range_eq)}</b>
            </span>
            <span
              className={`px-1.5 py-0.5 rounded font-semibold ${
                d.gpx_box.zone === "PREMIUM" ? "bg-rose-950/80 text-rose-300 border border-rose-800/60" : "bg-emerald-950/80 text-emerald-300 border border-emerald-800/60"
              }`}
            >
              {d.gpx_box.zone}
            </span>
          </div>

          {d.chain_metrics && (
            <div className="flex items-center gap-2.5 text-[11px]">
              <span>Put Wall (Sup): <b className="text-emerald-400">{d.chain_metrics.put_wall}</b></span>
              <span>Call Wall (Res): <b className="text-rose-400">{d.chain_metrics.call_wall}</b></span>
              {d.chain_metrics.max_pain && <span>Max Pain: <b className="text-amber-300">{d.chain_metrics.max_pain}</b></span>}
              {d.chain_metrics.pcr && <span>PCR: <b className="text-cyan-300">{d.chain_metrics.pcr.toFixed(2)}</b></span>}
              <div className="flex items-center gap-1.5 ml-2">
                <span className="px-1.5 py-0.5 rounded bg-bull-950 border border-bull-800/80 text-bull-400 font-bold text-[10px]">
                  CE: {d.chain_metrics.best_ce.strike} {d.chain_metrics.best_ce.ltp != null ? `(₹${n(d.chain_metrics.best_ce.ltp)})` : ""}
                </span>
                <span className="px-1.5 py-0.5 rounded bg-bear-950 border border-bear-800/80 text-bear-400 font-bold text-[10px]">
                  PE: {d.chain_metrics.best_pe.strike} {d.chain_metrics.best_pe.ltp != null ? `(₹${n(d.chain_metrics.best_pe.ltp)})` : ""}
                </span>
              </div>
            </div>
          )}
        </div>
      )}

      {/* Main Content Area */}
      <div className="flex-1 overflow-auto p-3">
        {loading ? (
          <div className="flex h-full items-center justify-center text-slate-500">Evaluating course concepts on closed candles...</div>
        ) : !d || !d.available ? (
          <div className="p-4 text-slate-500 text-center">{d?.reason || "No course data available."}</div>
        ) : (
          <div className="space-y-4">
            {/* 1. MSB & 7-Point Super OB Section */}
            {(activeTab === "all" || activeTab === "msb") && (
              <div>
                <div className="text-[11px] uppercase tracking-wider font-bold text-gold-400 mb-2">
                  1. Strong vs. Weak MSB & 7-Point Super OB Scorecard ({d.msb_setups.length})
                </div>
                {d.msb_setups.length === 0 ? (
                  <div className="text-slate-500 text-[11px]">No recent Market Structure Breaks detected on this timeframe.</div>
                ) : (
                  <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
                    {d.msb_setups.map((m, idx) => (
                      <div
                        key={idx}
                        className={`p-2.5 rounded border ${
                          m.dir === "BULL" ? "bg-emerald-950/20 border-emerald-900/50" : "bg-rose-950/20 border-rose-900/50"
                        }`}
                      >
                        <div className="flex items-center justify-between mb-1.5">
                          <span className={`font-bold ${m.dir === "BULL" ? "text-emerald-400" : "text-rose-400"}`}>
                            {m.kind} ({m.dir}) @ {n(m.break_level)}
                          </span>
                          <span className="num text-slate-400 text-[10px]">{m.hm}</span>
                        </div>

                        {/* Super OB Score & Badges */}
                        <div className="flex flex-wrap items-center gap-1.5 mb-2">
                          <span
                            className={`px-1.5 py-0.5 rounded font-bold text-[10px] ${
                              m.super_ob_score >= 5 ? "bg-gold-500 text-black font-extrabold" : "bg-ink-800 text-slate-300"
                            }`}
                          >
                            Super OB: {m.super_ob_score}/7
                          </span>
                          {m.dragon_fruit && (
                            <span className="px-1.5 py-0.5 rounded bg-fuchsia-900/80 border border-fuchsia-500 text-fuchsia-300 font-bold text-[10px]">
                              🍉 DRAGON FRUIT (A+ Sweet Spot)
                            </span>
                          )}
                          <span className="text-slate-400 text-[10px]">
                            OB: {n(m.ob.bot)}–{n(m.ob.top)} (Mid: {n(m.ob.mid)}, Touches: {m.ob.touches})
                          </span>
                        </div>

                        {/* Checkpoints Checklist */}
                        <div className="grid grid-cols-2 sm:grid-cols-4 gap-1 text-[10px] pt-1 border-t border-ink-800 text-slate-400">
                          {Object.entries(m.super_ob_checks).map(([k, v]) => (
                            <div key={k} className="flex items-center gap-1">
                              <span className={v ? "text-emerald-400 font-bold" : "text-slate-600"}>{v ? "✓" : "✗"}</span>
                              <span className="truncate">{k.replace(/_/g, " ")}</span>
                            </div>
                          ))}
                        </div>

                        {m.trap_warning && (
                          <div className="mt-2 text-rose-300 font-bold text-[10px] bg-rose-950/60 p-1 rounded border border-rose-800/80">
                            {m.trap_warning}
                          </div>
                        )}
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}

            {/* 2. ROTE (Reverse Optimal Trade Entry) */}
            {(activeTab === "all" || activeTab === "rote") && (
              <div>
                <div className="text-[11px] uppercase tracking-wider font-bold text-gold-400 mb-2">
                  2. ROTE — Reverse Optimal Trade Entry 0.70–0.80 ({d.rote_setups.length})
                </div>
                {d.rote_setups.length === 0 ? (
                  <div className="text-slate-500 text-[11px]">No active ROTE counter-bot setups awaiting entry.</div>
                ) : (
                  <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
                    {d.rote_setups.map((r, idx) => (
                      <div key={idx} className="p-2.5 rounded bg-ink-900/60 border border-ink-800">
                        <div className="flex items-center justify-between mb-1">
                          <span className={`font-bold ${r.side === "LONG" ? "text-emerald-400" : "text-rose-400"}`}>
                            {r.badge} (ROTE {r.side})
                          </span>
                          <span className="px-1.5 py-0.5 rounded bg-ink-800 text-slate-300 font-mono text-[10px]">{r.strike}</span>
                        </div>
                        <div className="text-[11px] text-slate-300 mb-1.5">{r.rule}</div>
                        <div className="grid grid-cols-4 gap-1.5 text-[10px] text-slate-400 bg-ink-950/80 p-1.5 rounded">
                          <div>Sweep: <b className="text-slate-200">{n(r.sweep_extreme)}</b></div>
                          <div>EQ (0.50): <b className="text-purple-300">{n(r.eq_50)}</b></div>
                          <div>OTE: <b className="text-gold-300">{n(r.ote_zone[0])}–{n(r.ote_zone[1])}</b></div>
                          <div>SL: <b className="text-rose-400">{n(r.sl)}</b></div>
                          <div>TP1 (20-30%): <b className="text-cyan-300">{n(r.tp1_partial)}</b></div>
                          <div>Final TP: <b className="text-emerald-400">{n(r.tp2_final)}</b></div>
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}

            {/* 3. Pure SFP vs. cSFP vs. Gray Accumulation */}
            {(activeTab === "all" || activeTab === "sfp") && (
              <div>
                <div className="text-[11px] uppercase tracking-wider font-bold text-gold-400 mb-2">
                  3. Pure SFP vs. cSFP vs. Gray-Area Accumulation ({d.sfp_events.length})
                </div>
                {d.sfp_events.length === 0 ? (
                  <div className="text-slate-500 text-[11px]">No SFP or Gray Accumulation tests on major swings.</div>
                ) : (
                  <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 gap-2">
                    {d.sfp_events.map((s, idx) => (
                      <div key={idx} className="p-2 rounded bg-ink-900 border border-ink-800">
                        <div className="flex items-center justify-between mb-1">
                          <span
                            className={`font-bold text-[10px] px-1.5 py-0.5 rounded ${
                              s.kind.startsWith("PURE")
                                ? "bg-cyan-950 text-cyan-300 border border-cyan-800"
                                : s.kind.startsWith("cSFP")
                                ? "bg-purple-950 text-purple-300 border border-purple-800"
                                : "bg-amber-950 text-amber-300 border border-amber-800"
                            }`}
                          >
                            {s.kind}
                          </span>
                          <span className="text-slate-500 text-[10px]">{s.hm}</span>
                        </div>
                        <div className="text-slate-300 text-[11px]">Level: <b className="text-slate-100">{n(s.level)}</b></div>
                        <div className="text-slate-400 text-[10px] mt-1">{s.note}</div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}

            {/* 4. Mitigation Blocks (MiB) & FTR */}
            {(activeTab === "all" || activeTab === "mib" || activeTab === "ftr") && (
              <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                <div>
                  <div className="text-[11px] uppercase tracking-wider font-bold text-gold-400 mb-2">
                    4. Mitigation Blocks — MiB ({d.mib_zones.length})
                  </div>
                  {d.mib_zones.length === 0 ? (
                    <div className="text-slate-500 text-[11px]">No failed-swing Mitigation Blocks.</div>
                  ) : (
                    <div className="space-y-1.5">
                      {d.mib_zones.map((m, idx) => (
                        <div key={idx} className="p-2 rounded bg-ink-900 border border-ink-800">
                          <div className="flex items-center justify-between">
                            <span className={`font-bold ${m.side === "LONG" ? "text-emerald-400" : "text-rose-400"}`}>
                              {m.kind}
                            </span>
                            <span className="text-slate-400 text-[10px]">{m.bos_hm}</span>
                          </div>
                          <div className="text-slate-300 text-[10px] mt-0.5">Zone: {n(m.bot)}–{n(m.top)}</div>
                          <div className="text-slate-400 text-[10px]">{m.note}</div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>

                <div>
                  <div className="text-[11px] uppercase tracking-wider font-bold text-gold-400 mb-2">
                    5. FTR / Indecision Candle Bagholder Zones ({d.ftr_zones.length})
                  </div>
                  {d.ftr_zones.length === 0 ? (
                    <div className="text-slate-500 text-[11px]">No FTR pause zones recorded.</div>
                  ) : (
                    <div className="space-y-1.5">
                      {d.ftr_zones.map((f, idx) => (
                        <div key={idx} className="p-2 rounded bg-ink-900 border border-ink-800">
                          <div className="flex items-center justify-between">
                            <span className={`font-bold ${f.side === "LONG" ? "text-emerald-400" : "text-rose-400"}`}>
                              {f.kind}
                            </span>
                            <span className="px-1.5 py-0.5 rounded bg-ink-800 text-gold-300 font-bold text-[9px]">{f.status}</span>
                          </div>
                          <div className="text-slate-300 text-[10px] mt-0.5">Zone: {n(f.bot)}–{n(f.top)} (at {f.hm})</div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              </div>
            )}

            {/* 6. BPR (Balanced Price Range) & Super BPR Section */}
            {(activeTab === "all" || activeTab === "bpr") && (
              <div>
                <div className="text-[11px] uppercase tracking-wider font-bold text-teal-400 mb-2">
                  6. BPR & Super BPR — Balanced Price Range Flip Zones ({d.bpr_zones?.length || 0})
                </div>
                {!d.bpr_zones || d.bpr_zones.length === 0 ? (
                  <div className="text-slate-500 text-[11px]">No active opposing FVG overlap collisions (BPR).</div>
                ) : (
                  <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 gap-2">
                    {d.bpr_zones.map((b, idx) => (
                      <div
                        key={idx}
                        className={`p-2 rounded border ${
                          b.kind === "SUPER BPR"
                            ? "bg-teal-950/40 border-teal-500 text-teal-200"
                            : "bg-ink-900 border-teal-800/60 text-slate-200"
                        }`}
                      >
                        <div className="flex items-center justify-between mb-1">
                          <span className={`font-bold text-[10px] px-1.5 py-0.5 rounded ${
                            b.kind === "SUPER BPR" ? "bg-teal-600 text-slate-950 font-extrabold" : "bg-teal-950 text-teal-300 border border-teal-800"
                          }`}>
                            {b.kind}
                          </span>
                          <span className="text-slate-400 text-[10px]">{b.hm}</span>
                        </div>
                        <div className="text-[11px] font-semibold text-slate-100">
                          Flip Zone: {n(b.bot)}–{n(b.top)} (Mid: {n(b.mid)})
                        </div>
                        <div className="text-[10px] text-teal-400/90 mt-1">
                          {b.overlaps >= 3 ? `🔥 3+ Imbalance Collision (Super BPR)` : `⚡ Opposing Bull & Bear FVG Intersection`}
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}

            {/* 7. SBB (Super Breaker Blocks) Section */}
            {(activeTab === "all" || activeTab === "sbb") && (
              <div>
                <div className="text-[11px] uppercase tracking-wider font-bold text-gold-400 mb-2">
                  7. SBB — Super Breaker Blocks & Failed Order Blocks ({d.sbb_setups?.length || 0})
                </div>
                {!d.sbb_setups || d.sbb_setups.length === 0 ? (
                  <div className="text-slate-500 text-[11px]">No active Breaker Blocks or SBB setups detected.</div>
                ) : (
                  <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
                    {d.sbb_setups.map((s, idx) => (
                      <div
                        key={idx}
                        className={`p-2.5 rounded border ${
                          s.is_super
                            ? (s.dir === "BULL" ? "bg-emerald-950/40 border-emerald-500/80" : "bg-rose-950/40 border-rose-500/80")
                            : "bg-ink-900 border-ink-800"
                        }`}
                      >
                        <div className="flex items-center justify-between mb-1.5">
                          <div className="flex items-center gap-1.5">
                            <span className={`font-bold ${s.dir === "BULL" ? "text-emerald-400" : "text-rose-400"}`}>
                              {s.label}
                            </span>
                            {s.is_super && (
                              <span className="px-1.5 py-0.5 rounded bg-gold-500 text-black font-extrabold text-[9px] animate-pulse">
                                ★ SBB UPGRADE
                              </span>
                            )}
                          </div>
                          <span className="text-slate-400 text-[10px]">{s.hm}</span>
                        </div>
                        <div className="text-[11px] text-slate-300 mb-1">
                          Break Level: <b className="text-slate-100">{n(s.break_level)}</b> | Zone: {n(s.bot)}–{n(s.top)}
                        </div>
                        <div className="grid grid-cols-3 gap-1 text-[10px] pt-1 border-t border-ink-800 text-slate-400">
                          <span className={s.sfp_confirmed ? "text-emerald-400" : "text-slate-600"}>
                            {s.sfp_confirmed ? "✓ SFP Sweep" : "✗ No SFP Sweep"}
                          </span>
                          <span className={s.fvg_touch ? "text-emerald-400" : "text-slate-600"}>
                            {s.fvg_touch ? "✓ FVG Touch" : "✗ No FVG Touch"}
                          </span>
                          <span className={s.in_aligned_zone ? "text-emerald-400" : "text-slate-600"}>
                            {s.in_aligned_zone ? "✓ Prem/Disc Aligned" : "✗ Counter-Zone"}
                          </span>
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
