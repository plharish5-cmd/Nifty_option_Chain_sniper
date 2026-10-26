// WriterMap.tsx — Writer Positioning Map.
// Integrates Institutional Strike Map 2.0 (OIMatrixSuite) alongside Classic Writer Ladder with seamless 1-click toggling.
import { useEffect, useState } from "react";
import { api } from "../api";
import OIMatrixSuite from "./OIMatrixSuite";

type Leg = { oi: number; oi_chg: number };
type Row = { strike: number; ce: Leg; pe: Leg };
type Chain = { rows?: Row[]; spot?: number; atm?: number } | null;

const cr = (v: number) => v >= 1e7 ? `${(v / 1e7).toFixed(2)}Cr` : v >= 1e5 ? `${(v / 1e5).toFixed(1)}L` : `${Math.round(v)}`;

function useChain(ms = 30000): Chain {
  const [c, setC] = useState<Chain>(null);
  useEffect(() => {
    let dead = false;
    const go = () => api.chain().then((ch) => { if (!dead) setC(ch); }).catch(() => {});
    go(); const id = window.setInterval(go, ms);
    return () => { dead = true; window.clearInterval(id); };
  }, [ms]);
  return c;
}

// building (writers adding) vs unwinding (covering) → brightness, like the OI-wall coloring
function tone(leg: Leg, base: string): { bg: string; tag: string } {
  const fresh = leg.oi > 0 ? leg.oi_chg / leg.oi : 0;
  if (leg.oi_chg > 0 && fresh >= 0.12) return { bg: base, tag: "▲" };     // building — full colour
  if (leg.oi_chg < 0 && fresh <= -0.10) return { bg: base + "44", tag: "▼" }; // unwinding — faded
  return { bg: base + "99", tag: "" };
}

function ModeSwitcher({ mode, setMode }: { mode: "matrix" | "classic"; setMode: (m: "matrix" | "classic") => void }) {
  return (
    <div className="flex items-center rounded bg-[#0b1324] p-0.5 border border-slate-700">
      <button
        onClick={() => setMode("matrix")}
        className={`rounded px-2.5 py-0.5 text-[11px] font-extrabold transition-all ${
          mode === "matrix"
            ? "bg-gold-500/30 text-gold-200 border border-gold-500/60 shadow-sm"
            : "text-slate-400 hover:text-slate-200"
        }`}
        title="Institutional Open Interest Matrix & Strike Map 2.0"
      >
        ⚡ Strike Map 2.0
      </button>
      <button
        onClick={() => setMode("classic")}
        className={`rounded px-2.5 py-0.5 text-[11px] font-bold transition-all ${
          mode === "classic"
            ? "bg-slate-700 text-slate-100 border border-slate-600 shadow-sm"
            : "text-slate-400 hover:text-slate-200"
        }`}
        title="Classic Strike-wise Writer Ladder"
      >
        Classic Ladder
      </button>
    </div>
  );
}

function ClassicWriterMap({ modeToggle }: { modeToggle: React.ReactNode }) {
  const ch = useChain();
  const [span, setSpan] = useState(10);
  if (!ch?.rows?.length) return <div className="flex h-full items-center justify-center text-slate-500">Loading option chain…</div>;
  const spot = ch.spot ?? ch.atm ?? 0;
  const atm = Math.round(spot / 50) * 50;
  const rows = [...ch.rows].filter((r) => Math.abs(r.strike - atm) <= span * 50).sort((a, b) => b.strike - a.strike);
  if (!rows.length) return <div className="flex h-full items-center justify-center text-slate-500">No strikes near spot.</div>;
  const maxOi = Math.max(...rows.flatMap((r) => [r.ce.oi, r.pe.oi]), 1);
  const totCe = rows.reduce((a, r) => a + r.ce.oi, 0), totPe = rows.reduce((a, r) => a + r.pe.oi, 0);
  const pcr = totCe > 0 ? totPe / totCe : 0;
  const topPe = [...rows].sort((a, b) => b.pe.oi - a.pe.oi)[0];   // biggest support
  const topCe = [...rows].sort((a, b) => b.ce.oi - a.ce.oi)[0];   // biggest resistance

  return (
    <div className="flex h-full flex-col text-[12px]">
      {/* header */}
      <div className="flex flex-wrap items-center gap-x-5 gap-y-1 border-b border-ink-700 px-3 py-2">
        <div className="flex items-center gap-2">
          <span className="font-semibold text-gold-400">🎯 Writer Positioning Map</span>
          {modeToggle}
        </div>
        <span className="num text-slate-300">Spot <b className="text-slate-100">{spot.toFixed(0)}</b></span>
        <span className="num">PCR <b className={pcr >= 1.2 ? "text-bull-400" : pcr <= 0.8 ? "text-bear-400" : "text-slate-200"}>{pcr.toFixed(2)}</b>
          <span className="ml-1 text-[10px] text-slate-500">{pcr >= 1.2 ? "put-heavy · bullish" : pcr <= 0.8 ? "call-heavy · bearish" : "balanced"}</span></span>
        <span className="num text-bull-400">🟢 Support <b>{topPe.strike}</b> ({cr(topPe.pe.oi)})</span>
        <span className="num text-bear-400">🔴 Resistance <b>{topCe.strike}</b> ({cr(topCe.ce.oi)})</span>
        <span className="ml-auto flex items-center gap-1 text-[10px] text-slate-500">range
          {[6, 10, 15].map((n) => <button key={n} onClick={() => setSpan(n)} className={`tab !py-0.5 ${span === n ? "tab-on" : ""}`}>±{n}</button>)}</span>
      </div>
      {/* column headers */}
      <div className="grid grid-cols-[1fr_84px_1fr] items-center border-b border-ink-800 px-3 py-1 text-[10px] uppercase tracking-wide text-slate-500">
        <div className="text-right">◀ PE writers (support)</div>
        <div className="text-center">Strike</div>
        <div>CE writers (resistance) ▶</div>
      </div>
      {/* ladder */}
      <div className="min-h-0 flex-1 overflow-auto px-3 py-1">
        {rows.map((r) => {
          const isAtm = Math.abs(r.strike - atm) < 25;
          const pe = tone(r.pe, "#22c55e"), ce = tone(r.ce, "#ef4444");
          const pePct = (r.pe.oi / maxOi) * 100, cePct = (r.ce.oi / maxOi) * 100;
          const bigPe = r.strike === topPe.strike, bigCe = r.strike === topCe.strike;
          return (
            <div key={r.strike} className={`grid grid-cols-[1fr_84px_1fr] items-center gap-1 ${isAtm ? "bg-gold-500/5" : ""}`} style={{ minHeight: 22 }}>
              {/* PE bar — grows leftward */}
              <div className="flex items-center justify-end gap-1">
                {r.pe.oi_chg ? <span className={`num text-[9px] ${r.pe.oi_chg > 0 ? "text-bull-400" : "text-slate-500"}`}>{r.pe.oi_chg > 0 ? "+" : ""}{cr(r.pe.oi_chg)}</span> : null}
                <span className="num text-[10px] text-bull-300">{cr(r.pe.oi)}</span>
                <div className="h-3.5 rounded-l" style={{ width: `${pePct}%`, minWidth: r.pe.oi ? 2 : 0, background: pe.bg, boxShadow: bigPe ? "0 0 6px #22c55e" : "none", outline: bigPe ? "1px solid #86efac" : "none" }} title={`PE ${r.strike}: ${cr(r.pe.oi)} OI (${r.pe.oi_chg >= 0 ? "+" : ""}${cr(r.pe.oi_chg)} today)`} />
              </div>
              {/* strike */}
              <div className={`text-center num font-semibold ${isAtm ? "text-gold-300" : "text-slate-300"}`}>{r.strike}{isAtm ? " ◀" : ""}</div>
              {/* CE bar — grows rightward */}
              <div className="flex items-center gap-1">
                <div className="h-3.5 rounded-r" style={{ width: `${cePct}%`, minWidth: r.ce.oi ? 2 : 0, background: ce.bg, boxShadow: bigCe ? "0 0 6px #ef4444" : "none", outline: bigCe ? "1px solid #fca5a5" : "none" }} title={`CE ${r.strike}: ${cr(r.ce.oi)} OI (${r.ce.oi_chg >= 0 ? "+" : ""}${cr(r.ce.oi_chg)} today)`} />
                <span className="num text-[10px] text-bear-300">{cr(r.ce.oi)}</span>
                {r.ce.oi_chg ? <span className={`num text-[9px] ${r.ce.oi_chg > 0 ? "text-bear-400" : "text-slate-500"}`}>{r.ce.oi_chg > 0 ? "+" : ""}{cr(r.ce.oi_chg)}</span> : null}
              </div>
            </div>
          );
        })}
      </div>
      <div className="border-t border-ink-700 px-3 py-1.5 text-[10px] text-slate-500">
        Bar = total OI · bright = writers ADDING today (wall hardening) · faded = UNWINDING (weakening) · glow = biggest wall.
        Price tends to pin between the biggest 🟢 support and 🔴 resistance; a wall that unwinds is the side likely to break.
      </div>
    </div>
  );
}

export default function WriterMap() {
  const [viewMode, setViewMode] = useState<"matrix" | "classic">(() => {
    try {
      const v = localStorage.getItem("ict_wmap_view_mode");
      if (v === "matrix" || v === "classic") return v;
    } catch { /* ignore */ }
    return "matrix";
  });

  const handleSetMode = (m: "matrix" | "classic") => {
    setViewMode(m);
    try {
      localStorage.setItem("ict_wmap_view_mode", m);
    } catch { /* ignore */ }
  };

  const switcher = <ModeSwitcher mode={viewMode} setMode={handleSetMode} />;

  if (viewMode === "matrix") {
    return <OIMatrixSuite modeToggle={switcher} />;
  }

  return <ClassicWriterMap modeToggle={switcher} />;
}
