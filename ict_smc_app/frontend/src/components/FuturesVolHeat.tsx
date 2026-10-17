// FuturesVolHeat.tsx — futures volume heatmap, price × time, switchable Oct / Nov / Oct+Nov. Each 1-min candle's
// volume is placed at its (basis-adjusted) close price; cells are binned into price rows × time columns. Display only.
import { useEffect, useRef, useState } from "react";

type Bar = { ts: number; c: number; v: number };
type Resp = { available: boolean; src?: string; series_today?: Bar[] | null; nov_share_pct?: number; fut2_status?: string; error?: string };

const cr = (v: number) => (v >= 1e7 ? `${(v / 1e7).toFixed(2)}Cr` : v >= 1e5 ? `${(v / 1e5).toFixed(1)}L` : `${Math.round(v)}`);
const istHM = (ts: number) => { const d = new Date(ts * 1000); return `${String(d.getUTCHours()).padStart(2, "0")}:${String(d.getUTCMinutes()).padStart(2, "0")}`; };
const TF = { "5m": 300, "15m": 900, "30m": 1800 } as const;

export default function FuturesVolHeat() {
  const [src, setSrc] = useState<"oct" | "nov" | "combined">("combined");
  const [tf, setTf] = useState<keyof typeof TF>("15m");
  const [d, setD] = useState<Resp | null>(null);
  const timer = useRef<number | null>(null);
  useEffect(() => {
    let dead = false;
    const go = () => fetch(`/api/combined_vol?src=${src}&heat=1`).then((r) => r.json()).then((j) => { if (!dead) setD(j); }).catch(() => {});
    go();
    timer.current = window.setInterval(go, 30000);
    return () => { dead = true; if (timer.current) window.clearInterval(timer.current); };
  }, [src]);

  const bars = d?.series_today ?? [];
  const tfs = TF[tf];
  // bin: price row (50-pt) × time bucket → volume
  const priceStep = 50;
  const grid: Record<number, Record<number, number>> = {};
  let maxV = 1, minP = Infinity, maxP = -Infinity;
  for (const b of bars) {
    const pr = Math.round(b.c / priceStep) * priceStep;
    const tb = Math.floor(b.ts / tfs) * tfs;
    (grid[pr] ??= {})[tb] = (grid[pr][tb] ?? 0) + b.v;
    if (grid[pr][tb] > maxV) maxV = grid[pr][tb];
    if (pr < minP) minP = pr;
    if (pr > maxP) maxP = pr;
  }
  const prices: number[] = [];
  for (let p = maxP; p >= minP; p -= priceStep) prices.push(p);
  const times = Array.from(new Set(bars.map((b) => Math.floor(b.ts / tfs) * tfs))).sort((a, b) => a - b);
  // per-candle intraday RVOL = column volume ÷ running-average column volume
  const colVol: Record<number, number> = {};
  for (const t of times) colVol[t] = prices.reduce((s, p) => s + (grid[p]?.[t] ?? 0), 0);
  const rvol: Record<number, number | null> = {};
  let rsum = 0;
  times.forEach((t, i) => { rsum += colVol[t]; const m = rsum / (i + 1); rvol[t] = m > 0 ? colVol[t] / m : null; });

  return (
    <div className="p-1 text-slate-200">
      <div className="mb-2 flex flex-wrap items-center gap-2 text-[11px]">
        <b className="text-indigo-300">🔥 Futures volume heatmap · price × time</b>
        <span className="mx-1 w-px self-stretch bg-ink-700" />
        {(["oct", "nov", "combined"] as const).map((s) => <button key={s} onClick={() => setSrc(s)} className={`tab ${src === s ? "tab-on" : ""}`}>{s === "oct" ? "Oct" : s === "nov" ? "Nov" : "Oct+Nov"}</button>)}
        <span className="mx-1 w-px self-stretch bg-ink-700" />
        {(Object.keys(TF) as (keyof typeof TF)[]).map((t) => <button key={t} onClick={() => setTf(t)} className={`tab ${tf === t ? "tab-on" : ""}`}>{t}</button>)}
        {src === "combined" && d?.nov_share_pct != null && <span className="ml-auto text-indigo-400/80">Nov {d.nov_share_pct}%</span>}
      </div>
      {d && !d.available && <div className="text-[12px] text-amber-300">{d.error}</div>}
      {d?.available && bars.length === 0 && (
        <div className="rounded-lg border border-ink-700 bg-ink-850 p-4 text-[12px] text-slate-400">
          <b className="text-slate-300">No bars for today yet.</b>
          <p className="mt-1">Fills during live hours. {src === "nov" ? "Nov is REST-only — it has volume but no live tick bars, so its grid is coarser." : ""}</p>
        </div>
      )}
      {bars.length > 0 && (
        <div className="overflow-auto">
          <table className="border-collapse text-[9px]">
            <tbody>
              {prices.map((p) => (
                <tr key={p}>
                  <td className="sticky left-0 bg-ink-900 px-1 text-right text-slate-400 num">{p}</td>
                  {times.map((t) => {
                    const v = grid[p]?.[t] ?? 0;
                    const a = v > 0 ? 0.12 + 0.88 * (v / maxV) : 0;
                    return <td key={t} title={`${p} @ ${istHM(t)} · ${cr(v)}`} style={{ background: `rgba(99,102,241,${a})`, width: 10, height: 12 }} />;
                  })}
                </tr>
              ))}
              <tr className="border-t border-ink-700">
                <td className="sticky left-0 bg-ink-900 px-1 text-right text-slate-500" title="intraday RVOL per candle: column volume ÷ the day's running-average column">RVOL</td>
                {times.map((t) => <td key={t} className={`text-center num ${rvol[t] == null ? "text-slate-600" : rvol[t]! >= 1.5 ? "text-amber-300" : rvol[t]! >= 1 ? "text-slate-200" : "text-slate-500"}`} style={{ fontSize: 8 }}>{rvol[t] != null ? rvol[t]!.toFixed(1) : "—"}</td>)}
              </tr>
              <tr><td className="sticky left-0 bg-ink-900" />{times.filter((_, i) => i % 4 === 0).map((t) => <td key={t} colSpan={4} className="pt-0.5 text-left text-slate-500">{istHM(t)}</td>)}</tr>
            </tbody>
          </table>
          <p className="mt-1 text-[10px] text-slate-500">Each cell = volume at that price row in that {tf} bucket (brighter = more). Source: {src === "combined" ? "Oct + Nov combined" : src === "nov" ? "Nov (next month, REST)" : "Oct (active)"}.</p>
        </div>
      )}
    </div>
  );
}
