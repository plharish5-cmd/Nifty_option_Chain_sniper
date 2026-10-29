// FootprintPanel.tsx — ESTIMATED footprint per closed candle (5m · 15m · 30m · 1h) from the desk's recorded
// NIFTY-future ticks (quote rule). Three views: Bid × Ask ladder · Delta profile · Summary strip. Information only.
import { useEffect, useMemo, useState } from "react";

type Row = { p: number; buy: number; sell: number; d: number; v: number; bi: boolean; si: boolean; poc: boolean; va: boolean };
type Tag = { k: string; dir: "bull" | "bear"; t: string };
type Bar = { time: number; at: string; o: number; h: number; l: number; c: number; coverage: number; vol: number; buy: number; sell: number;
  delta: number; cvd: number; max_d: number; min_d: number; partial?: boolean; poc?: number; va_hi?: number; va_lo?: number;
  delta_pct?: number; rows: Row[]; tags: Tag[]; read: string };
type FP = { tf: string; available: boolean; message?: string; summary?: string; source?: string; step?: number; bars: Bar[] };

const k = (v: number) => { const a = Math.abs(v); return a >= 1e5 ? `${(v / 1e5).toFixed(1)}L` : a >= 1e3 ? `${(v / 1e3).toFixed(1)}K` : `${Math.round(v)}`; };
const TAGC: Record<string, string> = { bull: "bg-bull-900/60 text-bull-400 border-bull-500/50", bear: "bg-bear-900/60 text-bear-400 border-bear-500/50" };
const SHORT: Record<string, string> = { ABSORPTION: "ABSORB", MANIPULATION: "MANIP", DISTRIBUTION: "DISTRIB", ACCUMULATION: "ACCUM",
  INITIATIVE: "INITIATIVE", STACKED_BUY: "STACK▲", STACKED_SELL: "STACK▼", DIVERGENCE: "DIV", UNFINISHED_HIGH: "UNF-H", UNFINISHED_LOW: "UNF-L" };

export default function FootprintPanel({ tick }: { tick: number }) {
  const [tf, setTf] = useState("5m");
  const [mode, setMode] = useState<"ladder" | "delta" | "summary">("ladder");
  const [cols, setCols] = useState(8);
  // How many bars back from the newest the window starts. 0 = pinned to the latest candle.
  const [back, setBack] = useState(0);
  const [d, setD] = useState<FP | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    let dead = false;
    // cover=session, not n=16. The old request asked for 16 bars no matter what the dropdown said, so the
    // panel could never hold a session: 16 x 15m is 4h against a 6h15 day, and 16 x 5m is 80 minutes. The
    // dropdown only trimmed what was already fetched, which is why selecting "16 candles" still started at
    // 14:50. The server now sizes the window from rules.json (footprints.heat_sessions).
    const go = () => { setBusy(true); fetch(`/api/footprints?tf=${tf}&cover=session`).then((r) => r.json()).then((j) => { if (!dead) setD(j); })
      .catch(() => {}).finally(() => { if (!dead) setBusy(false); }); };
    go();
    const id = window.setInterval(go, 30000);
    return () => { dead = true; window.clearInterval(id); };
  }, [tf, tick]);

  const all = useMemo(() => d?.bars ?? [], [d]);
  const maxBack = Math.max(0, all.length - cols);
  const back0 = Math.min(back, maxBack);                      // clamp: new data or a wider column count
  const end = all.length - back0;
  const bars = all.slice(Math.max(0, end - cols), end);
  const prices = useMemo(() => [...new Set(bars.flatMap((b) => b.rows.map((r) => r.p)))].sort((a, b) => b - a), [all, cols, back0]);
  const maxV = Math.max(1, ...bars.flatMap((b) => b.rows.map((r) => r.v)));
  const maxD = Math.max(1, ...bars.flatMap((b) => b.rows.map((r) => Math.abs(r.d))));

  return (
    <div className="flex h-full flex-col text-[11.5px]">
      <div className="flex flex-wrap items-center gap-2 border-b border-ink-700 px-3 py-1.5">
        {["5m", "15m", "30m", "1h"].map((t) => <button key={t} className={`tab ${t === tf ? "tab-on" : ""}`} onClick={() => setTf(t)}>{t}</button>)}
        <span className="mx-1 text-ink-600">|</span>
        {([["ladder", "Bid × Ask ladder"], ["delta", "Delta profile"], ["summary", "Summary strip"]] as const).map(([m, l]) =>
          <button key={m} className={`tab ${m === mode ? "tab-on" : ""}`} onClick={() => setMode(m)}>{l}</button>)}
        {mode !== "summary" && <select className="rounded bg-ink-850 px-1 py-0.5 text-slate-300" value={cols} onChange={(e) => setCols(+e.target.value)}>
          {[4, 6, 8, 12, 16, 25, 40, 75].map((x) => <option key={x} value={x}>{x} candles</option>)}</select>}
        {/* Scroll back through the session. The window used to be slice(-cols) — always the newest N, with
            no way to look at the morning. Right edge = latest. */}
        {mode !== "summary" && maxBack > 0 && <>
          <input type="range" min={0} max={maxBack} value={maxBack - back0} title="Scroll back through the session"
            onChange={(e) => setBack(maxBack - +e.target.value)} className="h-1 w-40 cursor-pointer accent-amber-400" />
          <span className="num text-[10px] text-slate-400">
            {bars.length ? `${bars[0].at} → ${bars[bars.length - 1].at}` : ""}
            <span className="text-slate-600"> · {all.length} bars</span>
          </span>
          {back0 > 0 && <button className="tab !py-0.5 !text-[10px]" onClick={() => setBack(0)} title="Jump to the latest candle">⇥ latest</button>}
        </>}
        <span className="rounded border border-amber-400/60 px-1.5 text-[10px] font-bold text-amber-300">ESTIMATED</span>
        <span className="text-slate-500">{busy ? "⟳" : d?.source ?? ""}</span>
      </div>
      <div className="px-3 py-1 text-[12px] text-slate-200">{d?.available ? d.summary : d?.message ?? "loading…"}</div>

      <div className="min-h-0 flex-1 overflow-auto px-2 pb-2">
        {mode === "summary" ? (
          <table className="w-full num">
            <thead><tr className="text-[10px] uppercase text-slate-500">
              {["Time", "Vol", "Δ", "Δ %", "CVD", "max Δ", "min Δ", "POC", "Value area", "Rec.", "Footprint read"].map((h) => <th key={h} className="sticky top-0 bg-ink-900 px-2 py-1 text-left">{h}</th>)}
            </tr></thead>
            <tbody>{[...(d?.bars ?? [])].reverse().map((b) => (
              <tr key={b.time} className={`border-b border-ink-800 ${b.rows.length ? "" : "opacity-40"}`}>
                <td className="px-2 py-1 font-semibold text-slate-200">{b.at}</td>
                <td className="px-2">{k(b.vol)}</td>
                <td className={`px-2 font-bold ${b.delta >= 0 ? "text-bull-400" : "text-bear-400"}`}>{b.delta >= 0 ? "+" : ""}{k(b.delta)}</td>
                <td className="px-2">{b.delta_pct != null ? `${b.delta_pct > 0 ? "+" : ""}${b.delta_pct}%` : "—"}</td>
                <td className={`px-2 ${b.cvd >= 0 ? "text-bull-400" : "text-bear-400"}`}>{k(b.cvd)}</td>
                <td className="px-2 text-bull-400">{k(b.max_d)}</td><td className="px-2 text-bear-400">{k(b.min_d)}</td>
                <td className="px-2 text-gold-400">{b.poc?.toLocaleString("en-IN") ?? "—"}</td>
                <td className="px-2 text-slate-400">{b.va_lo != null ? `${b.va_lo.toLocaleString("en-IN")}–${b.va_hi!.toLocaleString("en-IN")}` : "—"}</td>
                <td className={`px-2 ${b.coverage < 0.95 ? "text-amber-300" : "text-slate-500"}`}>{Math.round(b.coverage * 100)}%</td>
                <td className="px-2 py-1">
                  <div className="flex flex-wrap gap-1">{b.tags.map((t, i) => <span key={i} title={t.t} className={`rounded border px-1 text-[10px] font-bold ${TAGC[t.dir]}`}>{SHORT[t.k] ?? t.k}</span>)}</div>
                  <div className="text-slate-400">{b.read}</div>
                </td>
              </tr>))}</tbody>
          </table>
        ) : (
          <table className="border-separate border-spacing-0 num">
            <thead>
              <tr>
                <th className="sticky left-0 top-0 z-20 bg-ink-900 px-2 py-1 text-[10px] text-slate-500">Price</th>
                {bars.map((b) => (
                  <th key={b.time} className="sticky top-0 z-10 min-w-[118px] bg-ink-900 px-1 py-1 align-top">
                    <div className="text-slate-200">{b.at} <span className={b.c >= b.o ? "text-bull-400" : "text-bear-400"}>{b.c >= b.o ? "▲" : "▼"}{Math.abs(b.c - b.o).toFixed(0)}</span></div>
                    <div className={`text-[10px] ${b.delta >= 0 ? "text-bull-400" : "text-bear-400"}`}>Δ {b.delta >= 0 ? "+" : ""}{k(b.delta)} · V {k(b.vol)}</div>
                    <div className="mt-0.5 flex flex-wrap justify-center gap-0.5">{b.tags.map((t, i) =>
                      <span key={i} title={t.t} className={`rounded border px-0.5 text-[9px] font-bold ${TAGC[t.dir]}`}>{SHORT[t.k] ?? t.k}</span>)}</div>
                    {!b.rows.length && <div className="text-[10px] text-amber-300">no ticks</div>}
                  </th>))}
              </tr>
            </thead>
            <tbody>
              {prices.map((p) => (
                <tr key={p}>
                  <td className="sticky left-0 z-10 bg-ink-900 px-2 text-right text-slate-400">{p.toLocaleString("en-IN")}</td>
                  {bars.map((b) => {
                    const r = b.rows.find((x) => x.p === p);
                    if (!r) return <td key={b.time} className={`border-b border-ink-850 ${p > b.h + 0.01 || p < b.l - (d?.step ?? 5) ? "" : "bg-ink-850/40"}`} />;
                    const bg = mode === "ladder"
                      ? (r.bi ? "rgba(16,185,129,0.35)" : r.si ? "rgba(239,68,68,0.35)" : `rgba(148,163,184,${0.05 + 0.25 * r.v / maxV})`)
                      : (r.d >= 0 ? `rgba(16,185,129,${0.1 + 0.6 * r.d / maxD})` : `rgba(239,68,68,${0.1 + 0.6 * -r.d / maxD})`);
                    return (
                      <td key={b.time} title={`${p}: sell ${r.sell} × buy ${r.buy} · Δ ${r.d}${r.bi ? " · BUY imbalance" : ""}${r.si ? " · SELL imbalance" : ""}${r.poc ? " · POC" : ""}`}
                        className={`border-b border-ink-850 px-1 text-center ${r.poc ? "outline outline-2 -outline-offset-2 outline-gold-500" : ""} ${r.va ? "" : "opacity-75"}`}
                        style={{ background: bg }}>
                        {mode === "ladder"
                          ? <span><span className={r.si ? "font-bold text-bear-400" : "text-rose-200"}>{k(r.sell)}</span><span className="text-slate-500"> × </span><span className={r.bi ? "font-bold text-bull-400" : "text-emerald-200"}>{k(r.buy)}</span></span>
                          : <span className={r.d >= 0 ? "text-bull-400" : "text-bear-400"}>{r.d >= 0 ? "+" : ""}{k(r.d)}</span>}
                      </td>);
                  })}
                </tr>))}
              <tr>
                <td className="sticky left-0 bg-ink-900 px-2 text-[10px] text-slate-500">read</td>
                {bars.map((b) => <td key={b.time} className="px-1 py-1 align-top text-[10px] text-slate-400">{b.read}</td>)}
              </tr>
            </tbody>
          </table>
        )}
      </div>
      <div className="border-t border-ink-700 px-3 py-1 text-[10.5px] text-slate-500">
        Cells = sell × buy volume per price row (gold box = POC, faded = outside the 70 % value area). Green / red cell = diagonal imbalance ≥ 3×.
        Tags: ABSORB (heavy volume, price didn't follow) · MANIP (wick sweep bought/sold into, closed back) · DISTRIB / ACCUM (where volume built + delta + close) ·
        INITIATIVE · STACK (≥ 3 imbalances) · DIV (delta divergence) · UNF (unfinished extreme). Aggressor side is ESTIMATED by the quote rule and exists only for minutes the desk recorded.
      </div>
    </div>
  );
}
