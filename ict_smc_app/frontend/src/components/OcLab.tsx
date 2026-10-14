// OcLab.tsx — two Options Lab views from the OC app (8501), served through /api/oclab:
//   PeceTable   — the PE − CE OI table row for row (🎯 LEVELS · ACTION, READ, ★ PRO READ), newest candle on top
//   WritersFlow — "OI Change (Call vs Put) — since yesterday" + writer-flow strip (Δput − Δcall, 30-min balance)
// Both refresh every 30 s; the backend recomputes only when a new snapshot has been stored (never stale).
import { type ReactNode, useCallback, useEffect, useRef, useState } from "react";
import {
  ColorType, createChart, createSeriesMarkers, HistogramSeries, LineSeries, LineStyle,
  type IChartApi, type ISeriesApi, type SeriesMarker, type Time, type UTCTimestamp,
} from "lightweight-charts";

type Tup = [string, string, string?, string?];
type Row = {
  ts: string; pe_day: number | null; pe_chg: number | null; ce_day: number | null; ce_chg: number | null;
  pe_ce_day: number | null; pe_ce_chg: number | null; pcr: number | null; vol_pcr: number | null; close: number | null;
  sup: number | null; res: number | null; lv: Tup | null; act: Tup | null; read: Tup; meaning: string; pro: Tup | null;
};
type Pece = { ok: boolean; message?: string; range?: string; spot?: number; rows?: Row[]; snapshots?: number; day?: string };
type Pt = { t: number; hm: string; fut: number | null; ce: number | null; pe: number | null; flow: number | null; bal: number | null;
  regime: string; sig: boolean; flip: boolean; conf: string | null };
type Flow = { ok: boolean; message?: string; label?: string; expiry?: string; unwind?: [number, number] | null; read?: string; note?: string; points?: Pt[]; snapshots?: number; day?: string };

export function inr(v: number | null | undefined, sign = false): string {
  if (v == null || !isFinite(v)) return "—";
  const a = Math.abs(v), s = v < 0 ? "-" : sign && v > 0 ? "+" : "";
  if (a < 0.5) return "0";
  if (a >= 1e7) return `${s}${(a / 1e7).toFixed(2)}Cr`;
  if (a >= 1e5) return `${s}${(a / 1e5).toFixed(2)}L`;
  if (a >= 1e3) return `${s}${(a / 1e3).toFixed(1)}K`;
  return `${s}${a.toFixed(0)}`;
}
const KC: Record<string, string> = { bull: "text-bull-400", bear: "text-bear-400", skip: "text-slate-500", warn: "text-amber-300", flat: "text-slate-400" };
const KB: Record<string, string> = { bull: "bg-bull-900/40", bear: "bg-bear-900/40", skip: "bg-ink-850", flat: "" };
const clean = (s: string) => s.replace(/<[^>]+>/g, "").replace(/\*\*/g, "");

function useOc<T>(what: "pece" | "flow", q: string, tick: number): [T | null, boolean] {
  const [d, setD] = useState<T | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    let dead = false;
    const go = () => { setBusy(true); fetch(`/api/oclab/${what}?${q}`).then((r) => r.json()).then((j) => { if (!dead) setD(j); })
      .catch(() => {}).finally(() => { if (!dead) setBusy(false); }); };
    go();
    const id = window.setInterval(go, 30000);
    return () => { dead = true; window.clearInterval(id); };
  }, [what, q, tick]);
  return [d, busy];
}

function Controls({ tf, setTf, mode, setMode, n, setN, tfs, busy, info }: {
  tf: string; setTf: (s: string) => void; mode: string; setMode: (s: string) => void; n: number; setN: (n: number) => void;
  tfs: string[]; busy: boolean; info: string;
}) {
  return (
    <div className="flex flex-wrap items-center gap-2 border-b border-ink-700 px-3 py-1.5 text-[11px]">
      <div className="flex gap-1">{tfs.map((t) => <button key={t} className={`tab ${t === tf ? "tab-on" : ""}`} onClick={() => setTf(t)}>{t}</button>)}</div>
      <div className="flex gap-1">{[["auto", "Auto ATM"], ["all", "All strikes"]].map(([k, l]) =>
        <button key={k} className={`tab ${k === mode ? "tab-on" : ""}`} onClick={() => setMode(k)}>{l}</button>)}</div>
      {mode === "auto" && <select className="rounded bg-ink-850 px-1 py-0.5 text-slate-300" value={n} onChange={(e) => setN(+e.target.value)}>
        {[5, 10, 15, 20].map((x) => <option key={x} value={x}>±{x} strikes</option>)}</select>}
      <span className="text-slate-500">{busy ? "⟳ updating…" : info}</span>
    </div>
  );
}

// ─────────────────────────────────────────── PE − CE OI table ───────────────────────────────────────────
export function PeceTable({ tick }: { tick: number }) {
  const [tf, setTf] = useState("3m"); const [mode, setMode] = useState("auto"); const [n, setN] = useState(10);
  const [d, busy] = useOc<Pece>("pece", `tf=${tf}&mode=${mode}&n=${n}`, tick);
  const rows = d?.rows ?? [];
  const mx = (k: "pe_ce_day" | "pe_ce_chg") => Math.max(1, ...rows.map((r) => Math.abs(r[k] ?? 0)));
  const m1 = mx("pe_ce_day"), m2 = mx("pe_ce_chg");
  const bar = (v: number | null, m: number) => v == null ? null :
    <div className="mx-auto h-2 rounded" style={{ width: `${Math.max(3, Math.abs(v) / m * 92)}%`, background: v < 0 ? "#ef4444" : "#10b981" }} />;
  const c = (v: number | null) => (v ?? 0) >= 0 ? "text-bull-400" : "text-bear-400";
  const th = "sticky top-0 z-10 bg-ink-900 px-2 py-1 text-[10px] font-semibold uppercase tracking-wide text-slate-400 whitespace-nowrap";
  const td = "px-2 py-1 whitespace-nowrap";
  return (
    <div className="flex h-full flex-col">
      <Controls tf={tf} setTf={setTf} mode={mode} setMode={setMode} n={n} setN={setN} tfs={["1m", "3m", "5m", "15m", "1h"]} busy={busy}
        info={d?.ok ? `${d.day} · ${rows.length} candles · ${d.range} · NIFTY ${d.spot?.toLocaleString("en-IN")} · ${d.snapshots} snapshots` : d?.message ?? "loading…"} />
      <div className="min-h-0 flex-1 overflow-auto">
        <table className="w-full border-separate border-spacing-0 text-[11.5px] num">
          <thead><tr>
            {["Time", "Put Δ day", "Put Δ", "Call Δ day", "Call Δ", "PE−CE day", "PE−CE Δ", "day", "Δ", "PCR", "Vol PCR", "NIFTY",
              "Support", "Resistance", "🎯 Level event", "🎯 Action", "Signal", "Meaning", "★ PRO READ"].map((h) =>
              <th key={h} className={`${th} ${h.startsWith("🎯") ? "!text-gold-400" : ""}`}>{h}</th>)}
          </tr></thead>
          <tbody>
            {rows.map((r, i) => {
              const prev = rows[i + 1];
              const dim = r.pro?.[2] === "·" && !(r.lv && (r.lv[1] === "bull" || r.lv[1] === "bear"));
              const buy = r.act?.[0]?.startsWith("BUY");
              return (
                <tr key={r.ts} className={`border-b border-ink-800 hover:bg-ink-850 ${dim ? "opacity-50" : ""}`}>
                  <td className={`${td} sticky left-0 bg-ink-900 font-semibold text-slate-200`}>{r.ts}</td>
                  <td className={`${td} ${c(r.pe_chg)}`}>{inr(r.pe_day)}</td><td className={`${td} ${c(r.pe_chg)}`}>{inr(r.pe_chg, true)}</td>
                  <td className={`${td} ${c(r.ce_chg)}`}>{inr(r.ce_day)}</td><td className={`${td} ${c(r.ce_chg)}`}>{inr(r.ce_chg, true)}</td>
                  <td className={`${td} font-bold ${c(r.pe_ce_day)}`}>{inr(r.pe_ce_day)}</td><td className={`${td} ${c(r.pe_ce_chg)}`}>{inr(r.pe_ce_chg, true)}</td>
                  <td className={`${td} w-16`}>{bar(r.pe_ce_day, m1)}</td><td className={`${td} w-16`}>{bar(r.pe_ce_chg, m2)}</td>
                  <td className={`${td} ${prev && (r.pcr ?? 0) < (prev.pcr ?? 0) ? "text-bear-400" : "text-bull-400"}`}>{r.pcr?.toFixed(2) ?? "—"}</td>
                  <td className={`${td} text-amber-300`}>{r.vol_pcr?.toFixed(2) ?? "—"}</td>
                  <td className={`${td} ${prev && (r.close ?? 0) < (prev.close ?? 0) ? "text-bear-400" : "text-bull-400"}`}>{r.close?.toLocaleString("en-IN", { maximumFractionDigits: 1 })}</td>
                  <td className={`${td} font-bold text-bull-400`}>{r.sup?.toLocaleString("en-IN") ?? ""}</td>
                  <td className={`${td} font-bold text-bear-400`}>{r.res?.toLocaleString("en-IN") ?? ""}</td>
                  <td className={`px-2 py-1 min-w-[260px] ${KB[r.lv?.[1] ?? "flat"] ?? ""} ${KC[r.lv?.[1] ?? "flat"]} ${["break_up", "break_dn", "fail"].includes(r.lv?.[2] ?? "") ? "font-extrabold" : ""}`}>{r.lv?.[0]}</td>
                  <td className={`${td} font-bold ${buy ? (r.act?.[1] === "bull" ? "bg-bull-500 text-white" : "bg-bear-500 text-white") : KC[r.act?.[1] ?? "flat"]}`}>{r.act?.[0]}</td>
                  <td className={`${td} ${KC[r.read[1]] ?? ""}`}>{r.read[0]}</td>
                  <td className="px-2 py-1 min-w-[230px] text-slate-400">{r.meaning}</td>
                  <td className={`px-2 py-1 min-w-[240px] ${KC[r.pro?.[1] ?? "flat"]} ${["●●", "●●●"].includes(r.pro?.[2] ?? "") && ["bull", "bear"].includes(r.pro?.[1] ?? "") ? "font-bold" : ""}`}>
                    {r.pro?.[2] && <span className="mr-1 tracking-wider text-amber-400">{r.pro[2]}</span>}{r.pro?.[0]}
                    {r.pro?.[3] && <div className="text-[10.5px] font-semibold text-violet-300">{r.pro[3]}</div>}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ─────────────────────────────────────── OI change · writer flow chart ───────────────────────────────────────
export function WritersFlow({ tick }: { tick: number }) {
  const [tf, setTf] = useState("1m"); const [mode, setMode] = useState("all"); const [n, setN] = useState(10);
  const [d, busy] = useOc<Flow>("flow", `tf=${tf}&mode=${mode}&n=${n}`, tick);
  const box = useRef<HTMLDivElement>(null);
  const S = useRef<{ ch: IChartApi; fut: ISeriesApi<"Line">; ce: ISeriesApi<"Line">; pe: ISeriesApi<"Line">; uw: ISeriesApi<"Histogram">;
    fl: ISeriesApi<"Histogram">; bal: ISeriesApi<"Line">; mk: ReturnType<typeof createSeriesMarkers<Time>>; mk2: ReturnType<typeof createSeriesMarkers<Time>> } | null>(null);
  const first = useRef(true);

  useEffect(() => {
    if (!box.current) return;
    const ch = createChart(box.current, {
      autoSize: true, layout: { background: { type: ColorType.Solid, color: "#0b1220" }, textColor: "#94a3b8", fontFamily: "Inter, system-ui" },
      grid: { vertLines: { color: "#131d33" }, horzLines: { color: "#131d33" } },
      rightPriceScale: { borderColor: "#1d2a45" }, leftPriceScale: { visible: true, borderColor: "#1d2a45" },
      timeScale: { borderColor: "#1d2a45", timeVisible: true, secondsVisible: false, rightOffset: 4 },
      localization: { locale: "en-IN" },
    });
    const uw = ch.addSeries(HistogramSeries, { priceScaleId: "uw", color: "rgba(212,175,55,0.16)", priceLineVisible: false, lastValueVisible: false });
    ch.priceScale("uw").applyOptions({ scaleMargins: { top: 0, bottom: 0 }, visible: false });
    const fut = ch.addSeries(LineSeries, { priceScaleId: "left", color: "rgba(226,232,240,0.7)", lineWidth: 1, lineStyle: LineStyle.Dotted,
      title: "NIFTY fut", priceFormat: { type: "custom", formatter: (p: number) => p.toLocaleString("en-IN", { maximumFractionDigits: 1 }) } });
    const ce = ch.addSeries(LineSeries, { color: "#22c55e", lineWidth: 2, title: "Call", priceFormat: { type: "custom", formatter: (p: number) => inr(p) } });
    const pe = ch.addSeries(LineSeries, { color: "#ef4444", lineWidth: 2, title: "Put", priceFormat: { type: "custom", formatter: (p: number) => inr(p) } });
    const fl = ch.addSeries(HistogramSeries, { priceLineVisible: false, lastValueVisible: false, title: "flow Δput−Δcall",
      priceFormat: { type: "custom", formatter: (p: number) => inr(p) } }, 1);
    const bal = ch.addSeries(LineSeries, { color: "#e8c35a", lineWidth: 2, title: "balance 30m", priceLineVisible: false }, 2);
    for (const y of [0.25, -0.25]) bal.createPriceLine({ price: y, color: "rgba(148,163,184,0.5)", lineStyle: LineStyle.Dashed, lineWidth: 1, axisLabelVisible: false, title: "" });
    bal.createPriceLine({ price: 0, color: "rgba(148,163,184,0.25)", lineWidth: 1, axisLabelVisible: false, title: "" });
    try { const P = ch.panes(); P[0]?.setStretchFactor(5); P[1]?.setStretchFactor(1.6); P[2]?.setStretchFactor(1.2); } catch { /* */ }
    S.current = { ch, fut, ce, pe, uw, fl, bal, mk: createSeriesMarkers(fut, []), mk2: createSeriesMarkers(fl, []) };
    return () => { ch.remove(); S.current = null; };
  }, []);

  useEffect(() => { first.current = true; }, [tf, mode, n]);
  useEffect(() => {
    const s = S.current; const P = d?.points;
    if (!s || !P || !d?.ok) return;
    const T = (p: Pt) => p.t as UTCTimestamp;
    s.fut.setData(P.filter((p) => p.fut != null).map((p) => ({ time: T(p), value: p.fut as number })));
    s.ce.setData(P.filter((p) => p.ce != null).map((p) => ({ time: T(p), value: p.ce as number })));
    s.pe.setData(P.filter((p) => p.pe != null).map((p) => ({ time: T(p), value: p.pe as number })));
    s.fl.setData(P.filter((p) => p.flow != null).map((p) => ({ time: T(p), value: p.flow as number,
      color: p.sig ? "#e8c35a" : (p.flow as number) >= 0 ? "rgba(34,197,94,.8)" : "rgba(239,68,68,.8)" })));
    s.bal.setData(P.filter((p) => p.bal != null).map((p) => ({ time: T(p), value: p.bal as number })));
    const U = d.unwind;
    s.uw.setData(P.map((p) => ({ time: T(p), value: U && p.t >= U[0] && p.t <= U[1] ? 1 : 0 })));
    const mk: SeriesMarker<Time>[] = P.filter((p) => p.flip && p.conf).map((p) => ({ time: T(p),
      position: p.conf === "BULL" ? "belowBar" : "aboveBar", shape: p.conf === "BULL" ? "arrowUp" : "arrowDown",
      color: p.conf === "BULL" ? "#22c55e" : "#ef4444", text: p.conf === "BULL" ? "▲ BULL" : "▼ BEAR" }));
    if (U) mk.push({ time: U[0] as UTCTimestamp, position: "aboveBar", shape: "square", color: "#e8c35a", text: "EXPIRY UNWIND →" });
    mk.sort((a, b) => (a.time as number) - (b.time as number));
    s.mk.setMarkers(mk);
    s.mk2.setMarkers(P.filter((p) => p.sig).map((p) => ({ time: T(p), position: "inBar", shape: "circle", color: "#e8c35a", size: 0.5 })));
    if (first.current) { s.ch.timeScale().fitContent(); first.current = false; }
  }, [d]);

  const last = d?.points?.[d.points.length - 1];
  return (
    <div className="flex h-full flex-col">
      <Controls tf={tf} setTf={setTf} mode={mode} setMode={setMode} n={n} setN={setN} tfs={["1m", "3m", "5m", "15m"]} busy={busy}
        info={d?.ok ? `${d.day} · expiry ${d.expiry ?? "?"} · ${d.label} · ${d.snapshots} snapshots` : d?.message ?? "loading…"} />
      <div className="flex flex-wrap gap-x-4 px-3 py-1 text-[11px] text-slate-400">
        <span><span className="text-bull-400">━ Call OI chg</span> · <span className="text-bear-400">━ Put OI chg</span> (since yesterday, right axis) · ┈ NIFTY future (left)</span>
        <span>strip: <span className="text-bull-400">■</span>/<span className="text-bear-400">■</span> Δput − Δcall per candle, <span className="text-amber-300">●</span> unusual (≥ 2.5×), gold line = 30-min writers balance (±0.25)</span>
        {last && <span className="text-slate-300">now: call {inr(last.ce)} · put {inr(last.pe)} · balance {last.bal?.toFixed(2)} · regime <b>{last.conf ?? last.regime}</b></span>}
      </div>
      <div ref={box} className="min-h-0 flex-1" />
      {(d?.read || d?.note) && <div className="border-t border-ink-700 px-3 py-1.5 text-[11.5px] text-slate-300">{clean(`${d?.read ?? ""} ${d?.note ?? ""}`)}</div>}
    </div>
  );
}


// ─────────────────────────────── a card with its own Full view / Normal view ───────────────────────────────
export function FullPanel({ title, hint, h, children }: { title: string; hint?: string; h: number; children: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  const [full, setFull] = useState(false);
  const toggle = useCallback(() => {
    if (document.fullscreenElement) { document.exitFullscreen().catch(() => {}); return; }
    if (full) { setFull(false); return; }
    const el = ref.current;
    if (el?.requestFullscreen) el.requestFullscreen().then(() => setFull(true)).catch(() => setFull(true)); else setFull(true);
  }, [full]);
  useEffect(() => {
    const fs = () => { if (!document.fullscreenElement) setFull(false); };
    const k = (e: KeyboardEvent) => { if (e.key === "Escape" && !document.fullscreenElement) setFull(false); };
    document.addEventListener("fullscreenchange", fs); window.addEventListener("keydown", k);
    return () => { document.removeEventListener("fullscreenchange", fs); window.removeEventListener("keydown", k); };
  }, []);
  return (
    <div ref={ref} className={full ? "card fixed inset-0 z-50 flex flex-col rounded-none !bg-[#0b1220]" : "card flex flex-col min-w-0"}
      style={full ? undefined : { height: h }}>
      <div className="card-h">
        <span className="card-t">{title}</span>
        {hint && <span className="text-[10px] text-slate-500">{hint}</span>}
        <button onClick={toggle} className="tab ml-auto !border-gold-500/70 !text-gold-400 font-semibold">{full ? "▣ Normal view" : "⛶ Full view"}</button>
      </div>
      <div className="min-h-0 flex-1">{children}</div>
    </div>
  );
}

export type OcBeat = { time: string; tone: "bull" | "bear" | "wait" | "info" | "trade"; title: string; meaning: string; action: string };
export type OcSummary = { ok: boolean; lines: string[]; beats: OcBeat[]; message?: string };

export function useOcSummary(tick: number): OcSummary | null {
  const [d, setD] = useState<OcSummary | null>(null);
  useEffect(() => {
    let dead = false;
    const go = () => fetch("/api/oclab-summary").then((r) => r.json()).then((j) => { if (!dead) setD(j); }).catch(() => {});
    go();
    const id = window.setInterval(go, 60000);
    return () => { dead = true; window.clearInterval(id); };
  }, [tick]);
  return d;
}


// ─────────────────────────── drag bar: pull up / down to resize the block above it ───────────────────────────
export function useStoredHeight(key: string, def: number): [number, (v: number) => void] {
  const [h, setH] = useState<number>(() => { try { const v = Number(localStorage.getItem(key)); return v > 0 ? v : def; } catch { return def; } });
  const set = useCallback((v: number) => { setH(v); try { localStorage.setItem(key, String(Math.round(v))); } catch { /* ignore */ } }, [key]);
  return [h, set];
}

export function DragBar({ h, setH, min = 260, max = 1600, label }: { h: number; setH: (v: number) => void; min?: number; max?: number; label: string }) {
  const start = useRef<{ y: number; h: number } | null>(null);
  const onDown = (e: React.PointerEvent<HTMLDivElement>) => {
    start.current = { y: e.clientY, h };
    (e.target as HTMLElement).setPointerCapture(e.pointerId);
    document.body.style.cursor = "row-resize"; document.body.style.userSelect = "none";
  };
  const onMove = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!start.current) return;
    setH(Math.max(min, Math.min(max, start.current.h + e.clientY - start.current.y)));
  };
  const onUp = () => { start.current = null; document.body.style.cursor = ""; document.body.style.userSelect = ""; };
  return (
    <div onPointerDown={onDown} onPointerMove={onMove} onPointerUp={onUp} onDoubleClick={() => setH(-1)}
      title={`Drag up / down to resize the ${label} · double-click to reset`}
      className="group -my-1.5 flex h-3 cursor-row-resize items-center justify-center select-none">
      <div className="h-1 w-24 rounded-full bg-ink-600 transition group-hover:w-40 group-hover:bg-gold-500" />
    </div>
  );
}
