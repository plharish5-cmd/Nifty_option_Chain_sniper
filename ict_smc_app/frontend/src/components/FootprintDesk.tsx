// FootprintDesk.tsx — one grid, one switch. Futures (real tick footprint, with an Oct / Nov / Oct+Nov volume
// source strip) ↔ Options (real CE/PE tape). Keeps the two instruments SEPARATE (different axes) under a single
// control instead of two scattered tabs. Display only.
import { useEffect, useRef, useState } from "react";
import FootprintPanel from "./FootprintPanel";
import OptionTape from "./OptionTape";
import FuturesVolHeat from "./FuturesVolHeat";

type Cr = { value: number | null; state: string; trend: string | null } | null;
type CombResp = { available: boolean; oct_vol_today?: number; nov_vol_today?: number; combined_vol_today?: number;
  nov_share_pct?: number; crvol_oct?: Cr; crvol_combined?: Cr; fut2_status?: string };

const cr = (v: number) => (v >= 1e7 ? `${(v / 1e7).toFixed(2)}Cr` : v >= 1e5 ? `${(v / 1e5).toFixed(1)}L` : `${Math.round(v)}`);

// Oct / Nov / Oct+Nov volume + cRVOL source strip. Drives the displayed futures volume context; the tick-footprint
// grid itself is the active (Oct) contract — Nov is REST-only, so it has no live tick ladder.
function FuturesSource() {
  const [d, setD] = useState<CombResp | null>(null);
  const [src, setSrc] = useState<"oct" | "nov" | "combined">("combined");
  const timer = useRef<number | null>(null);
  useEffect(() => {
    let dead = false;
    const pull = () => fetch(`/api/combined_vol?src=${src}`).then((r) => r.json()).then((j) => { if (!dead) setD(j); }).catch(() => {});
    pull();
    timer.current = window.setInterval(pull, 30000);
    return () => { dead = true; if (timer.current) window.clearInterval(timer.current); };
  }, [src]);
  const vol = !d ? null : src === "oct" ? d.oct_vol_today : src === "nov" ? d.nov_vol_today : d.combined_vol_today;
  const crv = src === "combined" ? d?.crvol_combined : src === "oct" ? d?.crvol_oct : null;
  return (
    <div className="mb-2 flex flex-wrap items-center gap-2 rounded-lg border border-indigo-500/30 bg-indigo-500/5 px-2 py-1.5 text-[11px]">
      <b className="text-indigo-300">Futures volume source</b>
      {(["oct", "nov", "combined"] as const).map((s) => (
        <button key={s} onClick={() => setSrc(s)} className={`tab ${src === s ? "tab-on" : ""}`}>
          {s === "oct" ? "Oct" : s === "nov" ? "Nov" : "Oct+Nov"}
        </button>
      ))}
      {d?.available && (
        <span className="ml-auto flex items-center gap-3 text-slate-300">
          <span>vol today <b className="num text-slate-100">{vol != null ? cr(vol) : "—"}</b></span>
          {crv && <span>cRVOL <b className="num text-indigo-300">{crv.value != null ? `${crv.value.toFixed(2)}×` : crv.state}</b></span>}
          {src === "combined" && d.nov_share_pct != null && <span className="text-indigo-400/80">Nov {d.nov_share_pct}%</span>}
        </span>
      )}
    </div>
  );
}

export default function FootprintDesk({ tick }: { tick: number }) {
  const [inst, setInst] = useState<"fut" | "opt">("fut");
  const [futView, setFutView] = useState<"tick" | "heat">("tick");
  return (
    <div className="flex h-full flex-col">
      <div className="flex flex-wrap items-center gap-2 px-3 py-1.5 text-[12px]">
        <span className="text-slate-400">Instrument:</span>
        <button onClick={() => setInst("fut")} className={`tab ${inst === "fut" ? "tab-on" : ""} !border-cyan-500/60 !text-cyan-300`}>📈 Futures footprint</button>
        <button onClick={() => setInst("opt")} className={`tab ${inst === "opt" ? "tab-on" : ""} !border-indigo-500/60 !text-indigo-300`}>🪜 Options tape</button>
        {inst === "fut" && (
          <>
            <span className="mx-1 w-px self-stretch bg-ink-700" />
            <button onClick={() => setFutView("tick")} className={`tab ${futView === "tick" ? "tab-on" : ""}`}>Tick footprint</button>
            <button onClick={() => setFutView("heat")} className={`tab ${futView === "heat" ? "tab-on" : ""}`}>🔥 Vol heatmap</button>
          </>
        )}
        <span className="ml-2 text-[10px] text-slate-500">
          {inst === "fut" ? "price × time · real aggressor (est.) tick tape" : "strike × positioning · real CE/PE volume & OI"}
        </span>
      </div>
      <div className="min-h-0 flex-1 overflow-auto">
        {inst === "fut" ? (
          <div className="px-3">
            <FuturesSource />
            {futView === "tick" ? <FootprintPanel tick={tick} /> : <FuturesVolHeat />}
          </div>
        ) : (
          <OptionTape />
        )}
      </div>
    </div>
  );
}
