import { useEffect, useRef, useState } from "react";

/**
 * CombinedRvolChip — Oct / Nov / Oct+Nov futures volume SOURCE toggle for the main chart's RVOL bar.
 * Picks which futures contract(s) the volume + cumulative-RVOL readout reflects: the active month (Oct), the
 * next month (Nov, REST-only), or both combined. Additive, display-only; the per-bar volume histogram on the
 * chart stays the active contract (Nov has no live tick bars), this drives the headline volume/cRVOL readout.
 */
type Cr = { value: number | null; state: string; trend: string | null } | null;
type Resp = {
  available: boolean;
  oct_vol_today?: number; nov_vol_today?: number; combined_vol_today?: number;
  nov_share_pct?: number; crvol_oct?: Cr; crvol_nov?: Cr; crvol_combined?: Cr;
  fut2_status?: string; error?: string;
};

const cr = (v: number) => (v >= 1e7 ? `${(v / 1e7).toFixed(2)}Cr` : v >= 1e5 ? `${(v / 1e5).toFixed(1)}L` : `${Math.round(v)}`);

export default function CombinedRvolChip() {
  const [d, setD] = useState<Resp | null>(null);
  const [src, setSrc] = useState<"oct" | "nov" | "combined">("combined");
  const [open, setOpen] = useState(false);
  const timer = useRef<number | null>(null);

  useEffect(() => {
    let dead = false;
    const pull = async () => {
      try {
        const r = await fetch(`/api/combined_vol?src=${src}`);
        const j = (await r.json()) as Resp;
        if (!dead) setD(j);
      } catch {
        /* keep last */
      }
    };
    pull();
    timer.current = window.setInterval(pull, 30000);
    return () => { dead = true; if (timer.current) window.clearInterval(timer.current); };
  }, [src]);

  if (!d || !d.available) return null;
  const vol = src === "oct" ? d.oct_vol_today : src === "nov" ? d.nov_vol_today : d.combined_vol_today;
  const crv = src === "oct" ? d.crvol_oct : src === "nov" ? d.crvol_nov : d.crvol_combined;
  const nov = d.nov_share_pct ?? 0;

  return (
    <span className="relative flex items-center gap-1">
      <span className="text-[10px] uppercase tracking-wide text-slate-500">Vol src</span>
      {(["oct", "nov", "combined"] as const).map((s) => (
        <button key={s} onClick={() => setSrc(s)}
          className={`rounded border px-1.5 py-0.5 text-[11px] ${src === s ? "border-indigo-400 bg-indigo-500/20 text-indigo-200" : "border-ink-700 text-slate-400 hover:text-slate-200"}`}>
          {s === "oct" ? "Oct" : s === "nov" ? "Nov" : "Oct+Nov"}
        </button>
      ))}
      <button onClick={() => setOpen((o) => !o)} title="volume + cRVOL for the chosen source · click for detail"
        className="ml-1 rounded border border-indigo-500/50 bg-indigo-500/10 px-1.5 py-0.5 text-[11px] text-indigo-300 hover:bg-indigo-500/20">
        {vol != null ? cr(vol) : "—"} · {crv?.value != null ? `${crv.value.toFixed(2)}×` : (crv?.state ?? "—")}
      </button>
      {open && (
        <div className="absolute right-0 top-full z-50 mt-1 w-72 rounded-lg border border-ink-700 bg-ink-900/98 p-2.5 text-[11px] leading-relaxed text-slate-300 shadow-xl">
          <div className="mb-1 font-semibold text-indigo-300">Futures volume source — {src === "oct" ? "Oct only" : src === "nov" ? "Nov only" : "Oct + Nov"}</div>
          <div className="flex justify-between"><span className="text-slate-400">Volume today</span><span className="num">{vol != null ? cr(vol) : "—"}</span></div>
          <div className="flex justify-between"><span className="text-slate-400">cRVOL</span><span className="num">{crv?.value != null ? `${crv.value.toFixed(2)}× (${crv.state})` : (crv?.state ?? "—")}</span></div>
          <div className="flex justify-between"><span className="text-slate-400">Nov participation</span><span className="num">{nov}% of total</span></div>
          <p className="mt-1.5 text-slate-400">
            Nov is REST-only (costs no live connection). ~{nov}% of real participation sits in the next month during
            rollover; an Oct-only read misses it. The chart's per-bar volume bars stay the active contract — this
            toggle drives the headline volume &amp; cRVOL readout.
          </p>
          <div className="mt-1 text-[10px] text-slate-500">{d.fut2_status}</div>
        </div>
      )}
    </span>
  );
}
